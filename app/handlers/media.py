"""入站媒体解析 —— photo/video/document → M3 多模态 content 块(plan §12)。"""
from __future__ import annotations

from typing import Any

from aiogram.types import Message

from app.logging import get_logger
from app.services import Services
from app.utils.tg_files import (
    IMAGE_INLINE_LIMIT,
    VIDEO_INLINE_LIMIT,
    download_file,
    guess_mime,
    to_data_url,
)

log = get_logger("handlers.media")

_DOC_EXTS = {"pdf", "docx", "txt"}
_IMG_EXTS = {"jpg", "jpeg", "png", "gif", "webp"}


async def build_content(svc: Services, message: Message) -> tuple[Any, str]:
    """把 Telegram 消息转为 M3 content(字符串或多模态块列表)。

    返回 (content, 纯文本提要 query_text)。
    """
    text = message.text or message.caption or ""

    # 纯文本
    if message.text and not (message.photo or message.video or message.document):
        return text, text

    blocks: list[dict[str, Any]] = []
    if text:
        blocks.append({"type": "text", "text": text})

    try:
        # 图片
        if message.photo:
            photo = message.photo[-1]  # 最大尺寸
            if (photo.file_size or 0) > IMAGE_INLINE_LIMIT:
                raise ValueError("图片超过 10MB,无法处理")
            data, path = await download_file(svc.bot, photo.file_id)
            url = await to_data_url(data, "image/jpeg")
            blocks.append({"type": "image_url", "image_url": {"url": url}})
            log.info("入站图片已转base64", 会话=message.chat.id,
                     大小KB=round(len(data) / 1024, 1))

        # 视频
        if message.video:
            size = message.video.file_size or 0
            if size <= VIDEO_INLINE_LIMIT:
                data, path = await download_file(svc.bot, message.video.file_id)
                url = await to_data_url(data, "video/mp4")
                blocks.append({"type": "video_url", "video_url": {"url": url, "fps": 1}})
                log.info("入站视频已转base64", 会话=message.chat.id,
                         大小MB=round(size / 1024 / 1024, 1))
            else:
                data, path = await download_file(svc.bot, message.video.file_id)
                file_id = await svc.files_api.upload(data, "video.mp4")
                blocks.append({"type": "video_url",
                               "video_url": {"url": f"mm_file://{file_id}", "fps": 1}})
                log.info("入站大视频已转存FilesAPI", 会话=message.chat.id,
                         文件ID=file_id)

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
