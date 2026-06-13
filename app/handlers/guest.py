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
from app.handlers.media import build_content
from app.handlers.mentions import strip_bot_mention
from app.handlers.pipeline import run_chat_pipeline
from app.logging import get_logger
from app.services import Services

log = get_logger("handlers.guest")

router = Router(name="guest")


@router.guest_message()
async def handle_guest(message: Message, user: User, svc: Services) -> None:
    await process_guest_message(message, user, svc)


def _as_blocks(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return list(content)


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(
        str(block.get("text", ""))
        for block in content
        if block.get("type") == "text" and block.get("text")
    )


def _with_reply_context(
    content: Any,
    question_text: str,
    reply_content: Any | None,
    reply_text: str,
) -> Any:
    blocks: list[dict[str, Any]] = []
    if reply_text:
        blocks.append({"type": "text", "text": f"[引用的消息]\n{reply_text}"})
    elif reply_content is not None:
        blocks.append({"type": "text", "text": "[引用的消息]"})

    if reply_content is not None:
        for block in _as_blocks(reply_content):
            if block.get("type") == "text" and reply_text:
                continue
            blocks.append(block)

    blocks.append({"type": "text", "text": f"[召唤者的问题]\n{question_text}"})
    blocks.extend(block for block in _as_blocks(content) if block.get("type") != "text")

    if len(blocks) == 1 and blocks[0].get("type") == "text":
        return blocks[0]["text"]
    return blocks


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

    # Guest 无历史:附引用消息作为唯一上下文。只清理当前召唤消息,不改引用原文。
    reply = message.reply_to_message
    if reply:
        reply_content, _reply_query = await build_content(svc, reply)
        reply_text = _content_text(reply_content) if reply_content is not None else ""
        if reply_content is not None or reply_text:
            content = _with_reply_context(content, query_text, reply_content, reply_text)
            if reply_text:
                query_text = f"[引用的消息]\n{reply_text}\n\n[召唤者的问题]\n{query_text}"
            else:
                query_text = f"[召唤者的问题]\n{query_text}"

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
