"""私聊 handler —— sendMessageDraft 原生草稿流(plan §11)。"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.types import Message

from app.core.streaming import DraftRenderer
from app.db.models import User
from app.handlers.media import build_content, build_group_content
from app.handlers.pipeline import run_chat_pipeline
from app.handlers.replies import fold_reply_context
from app.logging import get_logger
from app.services import Services

log = get_logger("handlers.private")

router = Router(name="private")


@router.message(F.chat.type == "private",
                F.text | F.photo | F.video | F.document | F.sticker | F.animation,
                ~F.text.startswith("/"))
async def handle_private(message: Message, user: User, svc: Services) -> None:
    # 相册(media group)聚合:同组多帧合并为单次处理
    if getattr(message, "media_group_id", None):
        async def _on_group_complete(messages: list[Message]) -> None:
            await _process_album(svc, user, messages)

        buffered = await svc.media_group_buffer.add_or_dispatch(
            message, _on_group_complete,
        )
        if buffered:
            return  # 等聚合完成后统一处理

    log.info("私聊消息", 用户=user.tg_id, 用户名=user.username or "无",
             类型="文本" if message.text else "多媒体",
             预览=(message.text or message.caption or "")[:60])
    content, query_text = await build_content(svc, message)
    if content is None:
        return
    content, query_text = await fold_reply_context(svc, message, content, query_text)
    renderer = DraftRenderer(svc.bot, message.chat.id, svc.limiter,
                             throttle_ms=svc.settings.edit_throttle_ms,
                             typing_refresh_s=svc.settings.typing_refresh_s)
    await run_chat_pipeline(svc, user, message, content, renderer,
                            scope="user", query_text=query_text)


async def _process_album(svc: Services, user: User, messages: list[Message]) -> None:
    """处理聚合后的相册:多图 content + 参考素材 → 单次 pipeline。"""
    if not messages:
        return
    message = messages[0]  # 用首条作为 canonical message(回复/落库)
    log.info("私聊相册(已聚合)", 用户=user.tg_id,
             图片数=len(messages),
             预览=(message.caption or "")[:60])

    content, query_text, image_urls = await build_group_content(svc, messages)
    content, query_text = await fold_reply_context(svc, message, content, query_text)
    renderer = DraftRenderer(svc.bot, message.chat.id, svc.limiter,
                             throttle_ms=svc.settings.edit_throttle_ms,
                             typing_refresh_s=svc.settings.typing_refresh_s)
    await run_chat_pipeline(svc, user, message, content, renderer,
                            scope="user", query_text=query_text)
