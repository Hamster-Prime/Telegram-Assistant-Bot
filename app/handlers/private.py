"""私聊 handler —— sendMessageDraft 原生草稿流(plan §11)。"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.types import Message

from app.core.streaming import DraftRenderer
from app.db.models import User
from app.handlers.media import build_content
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
    log.info("私聊消息", 用户=user.tg_id, 用户名=user.username or "无",
             类型="文本" if message.text else "多媒体",
             预览=(message.text or message.caption or "")[:60])
    content, query_text = await build_content(svc, message)
    if content is None:
        return
    # 回复/引用是用户的强语义信号,显式带进 content,避免模型只看到主消息文本。
    content, query_text = await fold_reply_context(svc, message, content, query_text)
    renderer = DraftRenderer(svc.bot, message.chat.id, svc.limiter,
                             throttle_ms=svc.settings.edit_throttle_ms)
    await run_chat_pipeline(svc, user, message, content, renderer,
                            scope="user", query_text=query_text)
