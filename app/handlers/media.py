"""入站媒体解析 —— Telegram 媒体 → M3 多模态 content 块(plan §12)。"""
from __future__ import annotations

from typing import Any

from aiogram.types import Message, PhotoSize

from app.logging import get_logger
from app.services import Services
from app.utils.tg_files import (
    IMAGE_INLINE_LIMIT,
    download_file,
    guess_mime,
    to_data_url,
)

log = get_logger("handlers.media")

_DOC_EXTS = {"pdf", "docx", "txt"}
_IMG_EXTS = {"jpg", "jpeg", "png", "gif", "webp"}


async def _append_image(
    svc: Services,
    blocks: list[dict[str, Any]],
    file_id: str,
    mime: str,
    chat_id: int,
    *,
    file_size: int | None = None,
    label: str = "图片",
) -> None:
    if (file_size or 0) > IMAGE_INLINE_LIMIT:
        raise ValueError(f"{label}超过 10MB,无法处理")
    data, _path = await download_file(svc.bot, file_id)
    url = await to_data_url(data, mime)
    blocks.append({"type": "image_url", "image_url": {"url": url}})
    log.info("入站图片已转base64", 会话=chat_id, 类型=label,
             大小KB=round(len(data) / 1024, 1))


def _thumbnail_mime(thumbnail: PhotoSize) -> str:
    return guess_mime(thumbnail.file_id, "image/jpeg")


async def build_content(svc: Services, message: Message) -> tuple[Any | None, str]:
    """把 Telegram 消息转为 M3 content(字符串或多模态块列表)。

    返回 (content, 纯文本提要 query_text)。content 为 None 表示该消息应被忽略。
    """
    text = message.text or message.caption or ""

    # 纯文本
    if message.text and not (
        message.photo or message.video or message.document or message.sticker
        or message.animation
    ):
        return text, text

    # Telegram video 按需求直接忽略,不下载也不传给模型。
    if message.video and not (message.photo or message.document or message.sticker
                              or message.animation or text):
        return None, ""

    blocks: list[dict[str, Any]] = []
    if text:
        blocks.append({"type": "text", "text": text})

    try:
        # 图片
        if message.photo:
            photo = message.photo[-1]  # 最大尺寸
            await _append_image(
                svc, blocks, photo.file_id, "image/jpeg", message.chat.id,
                file_size=photo.file_size, label="图片",
            )

        # 贴纸:普通贴纸按图片处理;动态贴纸/GIF 贴纸使用缩略图。
        if message.sticker:
            sticker = message.sticker
            if sticker.is_animated or sticker.is_video:
                if sticker.thumbnail is None:
                    raise ValueError("动态贴纸缺少缩略图")
                await _append_image(
                    svc, blocks, sticker.thumbnail.file_id,
                    _thumbnail_mime(sticker.thumbnail), message.chat.id,
                    file_size=sticker.thumbnail.file_size, label="贴纸缩略图",
                )
            else:
                await _append_image(
                    svc, blocks, sticker.file_id, "image/webp", message.chat.id,
                    file_size=sticker.file_size, label="贴纸",
                )

        # GIF/动画消息:使用缩略图作为图片传给模型。
        if message.animation:
            animation = message.animation
            if animation.thumbnail is None:
                raise ValueError("GIF 缺少缩略图")
            await _append_image(
                svc, blocks, animation.thumbnail.file_id,
                _thumbnail_mime(animation.thumbnail), message.chat.id,
                file_size=animation.thumbnail.file_size, label="GIF缩略图",
            )

        # 文档
        if message.document:
            doc = message.document
            name = doc.file_name or "file"
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            data, path = await download_file(svc.bot, doc.file_id)
            if ext in _IMG_EXTS:
                url = await to_data_url(data, guess_mime(name, "image/png"))
                blocks.append({"type": "image_url", "image_url": {"url": url}})
            elif ext in _DOC_EXTS:
                file_id = await svc.files_api.upload(data, name)
                blocks.append({"type": "text",
                               "text": f"(用户上传了文档 {name},文件引用 mm_file://{file_id})"})
                log.info("入站文档已转存FilesAPI", 会话=message.chat.id,
                         文件名=name, 文件ID=file_id)
            else:
                blocks.append({"type": "text",
                               "text": f"(用户上传了暂不支持解析的文件:{name})"})
    except ValueError as e:
        blocks.append({"type": "text", "text": f"(媒体处理失败:{e})"})
        log.warning("入站媒体处理失败", 会话=message.chat.id, 原因=str(e))

    if not blocks:
        return text or "(空消息)", text
    if len(blocks) == 1 and blocks[0].get("type") == "text":
        return blocks[0]["text"], blocks[0]["text"]
    return blocks, text or "(多媒体消息)"
