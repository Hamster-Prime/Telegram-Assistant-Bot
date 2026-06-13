"""媒体投递抽象 —— 私聊/群聊直发 vs Guest inline 投递(修复项)。

Guest bot 非群成员,**不能** sendMessage/sendPhoto 直接发送,只能:
1. answerGuestQuery 发出**一条** inline 消息(在 renderer.start 完成);
2. 之后对该 inline_message_id 做 editMessageText / editMessageMedia。

故 Guest 模式下,同步生成的图片/语音不立即发,而是暂存到 renderer,
在 finalize 时用 editMessageMedia 把这条 inline 消息**一次性**转成媒体
(caption 承载最终文本)。视频/音乐为异步 worker,经 generations.inline_message_id
回填(见 workers.py)。

DirectDelivery(私聊/群聊)= 现行直发行为,不变。
"""
from __future__ import annotations

from typing import Any, Protocol

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import BufferedInputFile, URLInputFile

from app.core.concurrency import SendRateLimiter
from app.logging import get_logger

log = get_logger("core.delivery")


async def _sleep_retry_after(e: TelegramRetryAfter) -> None:
    import asyncio
    await asyncio.sleep(e.retry_after + 0.5)


class MediaDelivery(Protocol):
    """场景无关的媒体投递接口(供 pipeline 工具调用)。"""

    is_guest: bool
    inline_message_id: str | None  # Guest:回填用;Direct:None

    async def send_photo(self, url: str, caption: str | None = None) -> bool: ...
    async def send_voice(self, url: str | None, data: bytes | None = None) -> bool: ...
    async def send_placeholder(self, text: str) -> int | None:
        """直接发一条文本占位。Guest 无需(文本经 renderer 流)。返回 message_id 或 None。"""
        ...
    async def edit_placeholder(self, msg_id: int | None, text: str) -> None: ...
    async def send_text(self, text: str) -> bool: ...


class DirectDelivery:
    """私聊/群聊:直接 sendMessage/sendPhoto/sendVoice 到 chat_id(现行行为)。"""

    def __init__(self, bot: Bot, chat_id: int, limiter: SendRateLimiter,
                 files_api: Any) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._limiter = limiter
        self._files_api = files_api

    @property
    def is_guest(self) -> bool:
        return False

    @property
    def inline_message_id(self) -> str | None:
        return None

    async def send_photo(self, url: str, caption: str | None = None) -> bool:
        try:
            await self._limiter.acquire()
            await self._bot.send_photo(self._chat_id, URLInputFile(url),
                                       caption=caption)
            return True
        except Exception as e:
            log.warning("图片直发失败", 会话=self._chat_id, 错误=str(e)[:120])
            return False

    async def send_voice(self, url: str | None, data: bytes | None = None) -> bool:
        try:
            if data is None and url:
                data = await self._files_api.download(url)
            if data is None:
                return False
            await self._limiter.acquire()
            await self._bot.send_voice(
                self._chat_id, BufferedInputFile(data, filename="speech.mp3"))
            return True
        except Exception as e:
            log.warning("语音直发失败", 会话=self._chat_id, 错误=str(e)[:120])
            return False

    async def send_placeholder(self, text: str) -> int | None:
        while True:
            try:
                await self._limiter.acquire()
                msg = await self._bot.send_message(self._chat_id, text)
                return msg.message_id
            except TelegramRetryAfter as e:
                await _sleep_retry_after(e)

    async def edit_placeholder(self, msg_id: int | None, text: str) -> None:
        if msg_id is None:
            return
        try:
            await self._limiter.acquire()
            await self._bot.edit_message_text(
                text, chat_id=self._chat_id, message_id=msg_id)
        except Exception as e:
            log.debug("占位编辑失败(忽略)", 会话=self._chat_id, 错误=str(e)[:120])

    async def send_text(self, text: str) -> bool:
        try:
            await self._limiter.acquire()
            await self._bot.send_message(self._chat_id, text)
            return True
        except Exception as e:
            log.warning("文本直发失败", 会话=self._chat_id, 错误=str(e)[:120])
            return False


class GuestDelivery:
    """Guest 模式:不立即发。图片/语音暂存到 renderer,finalize 转 editMessageMedia。
    文本/占位为空操作(文本经 renderer 的 inline 消息流承载)。
    """

    def __init__(self, renderer: Any) -> None:
        self._renderer = renderer

    @property
    def is_guest(self) -> bool:
        return True

    @property
    def inline_message_id(self) -> str | None:
        return getattr(self._renderer, "_inline_message_id", None)

    async def send_photo(self, url: str, caption: str | None = None) -> bool:
        self._renderer.attach_pending("photo", url, caption)
        return True

    async def send_voice(self, url: str | None, data: bytes | None = None) -> bool:
        # inline editMessageMedia 无 Voice 类型,按 Audio 投递(音频播放器,等价体验)。
        # inline 媒体仅支持 URL/file_id,无法上传新字节 → data-only 时无法投递。
        if not url:
            log.warning("Guest语音投递需要URL,字节模式不支持")
            return False
        self._renderer.attach_pending("audio", url, None)
        return True

    async def send_placeholder(self, text: str) -> int | None:
        return None  # Guest:文本状态经 renderer 流,不发独立占位

    async def edit_placeholder(self, msg_id: int | None, text: str) -> None:
        return None  # 无占位

    async def send_text(self, text: str) -> bool:
        return True  # 文本经 renderer
