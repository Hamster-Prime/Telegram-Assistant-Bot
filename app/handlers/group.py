"""群聊 handler —— @提及/回复机器人触发,Edit 流式(plan §11)。"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.types import Message

from app.core.streaming import EditRenderer
from app.db.models import User
from app.handlers.media import build_content
from app.handlers.pipeline import run_chat_pipeline
from app.logging import get_logger
from app.services import Services

log = get_logger("handlers.group")

router = Router(name="group")


def _is_triggered(message: Message, bot_username: str, bot_id: int) -> bool:
    """仅 @提及 或 回复机器人消息 时触发,避免刷屏。"""
    text = message.text or message.caption or ""
    if f"@{bot_username}" in text:
        return True
    reply = message.reply_to_message
    if reply and reply.from_user and reply.from_user.id == bot_id:
        return True
    return False


def _strip_mention(text: str, bot_username: str) -> str:
    return text.replace(f"@{bot_username}", "").strip()


@router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
                F.text | F.photo | F.video | F.document,
                ~F.text.startswith("/"))
async def handle_group(message: Message, user: User, svc: Services) -> None:
    me = await svc.bot.me()
    if not _is_triggered(message, me.username or "", me.id):
        return  # 未被提及,忽略

    log.info("群聊消息(被@或回复)", 用户=user.tg_id, 群=message.chat.id,
             群名=message.chat.title or "?",
             预览=(message.text or message.caption or "")[:60])

    content, query_text = await build_content(svc, message)
    if isinstance(content, str):
        content = _strip_mention(content, me.username or "")
        query_text = content
    else:
        for blk in content:
            if blk.get("type") == "text":
                blk["text"] = _strip_mention(blk["text"], me.username or "")
        query_text = _strip_mention(query_text, me.username or "")

    renderer = EditRenderer(svc.bot, message.chat.id, svc.limiter,
                            throttle_ms=svc.settings.edit_throttle_ms,
                            reply_to_message_id=message.message_id)
    # 群聊隐私隔离:scope=chat
    await run_chat_pipeline(svc, user, message, content, renderer,
                            scope="chat", query_text=query_text)
