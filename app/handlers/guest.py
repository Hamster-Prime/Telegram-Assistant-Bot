"""Guest 模式 handler —— answerGuestQuery + Edit 流式(plan §11/§14.4)。

Bot API 10.0:Update.guest_message 投递召唤消息(aiogram 3.28 原生支持);
鉴权按召唤者 guest_bot_caller_user(AuthMiddleware 的 extract_actor 已处理)。
上下文仅:召唤消息 + 其引用消息 + scope=chat 记忆(Guest 无群历史)。
"""
from __future__ import annotations

from typing import Any

from aiogram import Router
from aiogram.types import Message

from app.core.streaming import EditRenderer, GuestRenderer
from app.db.models import User
from app.handlers.guest_commands import answer_guest_text, execute_guest_command
from app.handlers.media import build_content
from app.handlers.mentions import strip_bot_mention
from app.handlers.pipeline import run_chat_pipeline
from app.handlers.replies import fold_reply_context
from app.logging import get_logger
from app.services import Services

log = get_logger("handlers.guest")

router = Router(name="guest")


@router.guest_message()
async def handle_guest(message: Message, user: User, svc: Services) -> None:
    # 斜杠命令分流:Guest 消息不走 @router.message,需在此显式拦截文本命令。
    guest_query_id = getattr(message, "guest_query_id", None)
    if (message.text or "").startswith("/"):
        response = await execute_guest_command(svc, user, message)
        if response is not None and guest_query_id:
            await answer_guest_text(svc.bot, str(guest_query_id), response)
            return
        # 未知命令 → 落入 AI 流程(自然语言处理)
    await process_guest_message(message, user, svc)


async def process_guest_message(message: Message, user: User, svc: Services) -> None:
    guest_query_id = getattr(message, "guest_query_id", None)
    caller = getattr(message, "guest_bot_caller_user", None)
    log.info("Guest召唤消息", 召唤者=user.tg_id,
             召唤者名=getattr(caller, "username", None) or "?",
             会话=message.chat.id, 查询ID=guest_query_id or "无",
             预览=(message.text or "")[:60])

    content, query_text = await build_content(svc, message)
    if content is None:
        return

    me = await svc.bot.me()
    if isinstance(content, str):
        content = strip_bot_mention(content, me.username or "")
        query_text = content
    else:
        for block in content:
            if block.get("type") == "text":
                block["text"] = strip_bot_mention(block["text"], me.username or "")
        query_text = strip_bot_mention(query_text, me.username or "")

    # Guest 无历史:附引用消息作为唯一上下文(逻辑见 replies.py,三场景共用)。
    content, query_text = await fold_reply_context(svc, message, content, query_text)

    if guest_query_id:
        renderer: Any = GuestRenderer(svc.bot, message.chat.id, str(guest_query_id),
                                      svc.limiter,
                                      throttle_ms=svc.settings.edit_throttle_ms)
    else:
        # 兜底:无 guest_query_id 时按普通编辑流(回复原消息)
        renderer = EditRenderer(svc.bot, message.chat.id, svc.limiter,
                                throttle_ms=svc.settings.edit_throttle_ms,
                                reply_to_message_id=message.message_id)

    # Guest 不落历史(收不到后续消息,持久化意义有限),记忆走 scope=chat
    await run_chat_pipeline(svc, user, message, content, renderer,
                            scope="chat", query_text=query_text, persist=False)
