"""鉴权门控 —— 授权制,全场景一致(plan §14.1)。

核心规则:只看「是否被授权」,不分场景。
- 私聊/群聊:message.from_user.id
- Guest 模式:guest_bot_caller_user.id(召唤者)
未授权 → ⛔ Permission Denied + 审计,终止后续处理。
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject, Update

from app.config import Settings
from app.db.dao import DAOBundle
from app.db.models import User
from app.logging import get_logger

log = get_logger("core.auth")


def extract_actor(event: TelegramObject) -> tuple[int | None, str | None, str | None]:
    """从事件中提取判定主体(发起人)的 (id, username, first_name)。

    Guest 模式:消息带 guest_query_id 时按 guest_bot_caller_user(召唤者)判定。
    """
    msg: Message | None = None
    if isinstance(event, Message):
        msg = event
    elif isinstance(event, Update):
        msg = event.message or getattr(event, "guest_message", None)

    if msg is None:
        return None, None, None

    # Guest 模式:按召唤者
    caller = getattr(msg, "guest_bot_caller_user", None)
    if caller is not None and getattr(msg, "guest_query_id", None):
        return caller.id, getattr(caller, "username", None), getattr(caller, "first_name", None)

    if msg.from_user:
        return msg.from_user.id, msg.from_user.username, msg.from_user.first_name
    return None, None, None


class AuthMiddleware(BaseMiddleware):
    """授权门控中间件 —— 在所有 handler 之前执行。"""

    def __init__(self, daos: DAOBundle, settings: Settings) -> None:
        self._daos = daos
        self._settings = settings

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        actor_id, username, first_name = extract_actor(event)
        if actor_id is None:
            # 无法识别发起人的更新(如频道帖),直接放行给具体 handler 决定
            return await handler(event, data)

        user = await self._daos.users.upsert_basic(actor_id, username, first_name)

        if not user.is_allowed:
            log.warning("鉴权拒绝", 用户=actor_id, 用户名=username or "无",
                        角色=user.role, 授权状态=user.authorized)
            await self._daos.audit.add(actor_id, "denied", None,
                                       f"未授权访问 username={username}")
            msg = event if isinstance(event, Message) else None
            if msg is None and isinstance(event, Update):
                msg = event.message or getattr(event, "guest_message", None)
            if msg is not None:
                await self._send_denial(msg)
            return None  # 终止链路(aiogram3:返回而不调用 handler 即取消)

        log.debug("鉴权通过", 用户=actor_id, 角色=user.role)
        data["user"] = user
        data["daos"] = self._daos
        data["settings"] = self._settings
        return await handler(event, data)

    async def _send_denial(self, msg: Message) -> None:
        """发送拒绝提示。Guest 召唤(bot 非群成员)只能经 answerGuestQuery 应答。"""
        guest_query_id = getattr(msg, "guest_query_id", None)
        try:
            if guest_query_id:
                from aiogram.methods import AnswerGuestQuery
                from aiogram.types import (
                    InlineQueryResultArticle,
                    InputTextMessageContent,
                )
                await msg.bot(AnswerGuestQuery(
                    guest_query_id=str(guest_query_id),
                    result=InlineQueryResultArticle(
                        id="denied",
                        title="Permission Denied",
                        input_message_content=InputTextMessageContent(
                            message_text=self._settings.permission_denied_text),
                    ),
                ))
            else:
                await msg.answer(self._settings.permission_denied_text)
        except Exception as e:
            log.warning("发送拒绝提示失败", 错误=str(e)[:120])


def require_role(user: User, *roles: str) -> bool:
    """命令级角色校验:superadmin 永远满足。"""
    if user.is_superadmin:
        return True
    return user.role in roles
