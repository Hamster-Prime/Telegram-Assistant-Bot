"""Telegram 文件下载 —— getFile → bytes → base64 data: URL。

getFile 下载链接含 bot token,MiniMax 无法直接拉取,
必须由 Bot 先下载字节再 base64 内联或转存 Files API。
"""
from __future__ import annotations

import asyncio
import base64
from io import BytesIO

from aiogram import Bot

from app.logging import get_logger

log = get_logger("utils.tg_files")

# Telegram Bot API 默认下载上限约 20MB
TG_DOWNLOAD_LIMIT = 20 * 1024 * 1024
# M3 多模态:图片 ≤10MB(base64 内联)
IMAGE_INLINE_LIMIT = 10 * 1024 * 1024
# 视频 URL/base64 ≤50MB,更大走 Files API
VIDEO_INLINE_LIMIT = 50 * 1024 * 1024

_MIME_BY_EXT = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
    "gif": "image/gif", "webp": "image/webp",
    "mp4": "video/mp4", "mov": "video/quicktime", "avi": "video/x-msvideo",
    "pdf": "application/pdf", "txt": "text/plain",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def guess_mime(file_path: str, default: str = "application/octet-stream") -> str:
    ext = file_path.rsplit(".", 1)[-1].lower() if "." in file_path else ""
    return _MIME_BY_EXT.get(ext, default)


async def download_file(bot: Bot, file_id: str) -> tuple[bytes, str]:
    """下载 Telegram 文件,返回 (字节, 服务器文件路径)。超限抛 ValueError。"""
    tg_file = await bot.get_file(file_id)
    size = tg_file.file_size or 0
    if size > TG_DOWNLOAD_LIMIT:
        raise ValueError(
            f"文件过大:{size / 1024 / 1024:.1f}MB,超过 Telegram 下载上限 20MB"
        )
    buf = BytesIO()
    await bot.download_file(tg_file.file_path, destination=buf)
    data = buf.getvalue()
    log.info("Telegram文件已下载", 文件ID=file_id[:32],
             大小KB=round(len(data) / 1024, 1), 路径=tg_file.file_path)
    return data, tg_file.file_path or ""


async def to_data_url(data: bytes, mime: str) -> str:
    """bytes → base64 data: URL(编码放线程池)。"""
    b64 = await asyncio.to_thread(lambda: base64.b64encode(data).decode("ascii"))
    return f"data:{mime};base64,{b64}"
