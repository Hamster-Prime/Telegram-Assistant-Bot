"""群聊 handler —— @提及/回复机器人触发,Edit 流式(plan §11)。"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.types import Message

from app.core.streaming import EditRenderer
from app.db.models import User
from app.handlers.media import build_content, build_group_content
from app.handlers.mentions import contains_bot_mention, strip_bot_mention
from app.handlers.pipeline import run_chat_pipeline
from app.handlers.replies import fold_reply_context
from app.logging import get_logger
from app.services import Services

log = get_logger("handlers.group")

router = Router(name="group")


def _is_triggered(message: Message, bot_username: str, bot_id: int) -> bool:
    """仅 @提及 或 回复机器人消息 时触发,避免刷屏。"""
    text = message.text or message.caption or ""
    if contains_bot_mention(text, bot_username):
        return True
    reply = message.reply_to_message
    if reply and reply.from_user and reply.from_user.id == bot_id:
        return True
    return False


@router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
                F.text | F.photo | F.video | F.document | F.sticker | F.animation,
                ~F.text.startswith("/"))
async def handle_group(message: Message, user: User, svc: Services) -> None:
    # 相册(media group)聚合
    if getattr(message, "media_group_id", None):
        async def _on_group_complete(messages: list[Message]) -> None:
            me = await svc.bot.me()
            if not any(_is_triggered(m, me.username or "", me.id) for m in messages):
                return
            await _process_group_album(svc, user, messages, me.username or "")

        buffered = await svc.media_group_buffer.add_or_dispatch(
            message, _on_group_complete,
        )
        if buffered:
            return

    me = await svc.bot.me()
    if not _is_triggered(message, me.username or "", me.id):
        return  # 未被提及,忽略

    log.info("群聊消息(被@或回复)", 用户=user.tg_id, 群=message.chat.id,
             群名=message.chat.title or "?",
             预览=(message.text or message.caption or "")[:60])

    content, query_text = await build_content(svc, message)
    if content is None:
        return
    if isinstance(content, str):
        content = strip_bot_mention(content, me.username or "")
        query_text = content
    else:
        for blk in content:
            if blk.get("type") == "text":
                blk["text"] = strip_bot_mention(blk["text"], me.username or "")
        query_text = strip_bot_mention(query_text, me.username or "")

    renderer = EditRenderer(svc.bot, message.chat.id, svc.limiter,
                            throttle_ms=svc.settings.group_edit_throttle_ms,
                            reply_to_message_id=message.message_id,
                            typing_refresh_s=svc.settings.typing_refresh_s)
    content, query_text = await fold_reply_context(svc, message, content, query_text)
    await run_chat_pipeline(svc, user, message, content, renderer,
                            scope="chat", query_text=query_text)


async def _process_group_album(
    svc: Services, user: User, messages: list[Message], bot_username: str,
) -> None:
    """处理群聊中聚合后的相册。"""
    if not messages:
        return
    message = messages[0]
    log.info("群聊相册(已聚合)", 用户=user.tg_id, 群=message.chat.id,
             群名=message.chat.title or "?", 图片数=len(messages))

    content, query_text, _urls = await build_group_content(svc, messages)
    for blk in content:
        if blk.get("type") == "text":
            blk["text"] = strip_bot_mention(blk["text"], bot_username)
    query_text = strip_bot_mention(query_text, bot_username)

    renderer = EditRenderer(svc.bot, message.chat.id, svc.limiter,
                            throttle_ms=svc.settings.group_edit_throttle_ms,
                            reply_to_message_id=message.message_id,
                            typing_refresh_s=svc.settings.typing_refresh_s)
    content, query_text = await fold_reply_context(svc, message, content, query_text)
    await run_chat_pipeline(svc, user, message, content, renderer,
                            scope="chat", query_text=query_text)
