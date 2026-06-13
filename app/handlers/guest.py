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
from app.handlers.pipeline import run_chat_pipeline
from app.logging import get_logger
from app.services import Services

log = get_logger("handlers.guest")

router = Router(name="guest")


@router.guest_message()
async def handle_guest(message: Message, user: User, svc: Services) -> None:
    await process_guest_message(message, user, svc)


async def process_guest_message(message: Message, user: User, svc: Services) -> None:
    guest_query_id = getattr(message, "guest_query_id", None)
    caller = getattr(message, "guest_bot_caller_user", None)
    log.info("Guest召唤消息", 召唤者=user.tg_id,
             召唤者名=getattr(caller, "username", None) or "?",
             会话=message.chat.id, 查询ID=guest_query_id or "无",
             预览=(message.text or "")[:60])

    text = message.text or message.caption or ""
    # Guest 无历史:附引用消息作为唯一上下文
    reply = message.reply_to_message
    if reply and (reply.text or reply.caption):
        text = f"[引用的消息]\n{reply.text or reply.caption}\n\n[召唤者的问题]\n{text}"

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
    await run_chat_pipeline(svc, user, message, text, renderer,
                            scope="chat", query_text=text, persist=False)
