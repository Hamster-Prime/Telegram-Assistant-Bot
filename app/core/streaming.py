"""统一流式渲染抽象 —— Draft(私聊)/ Edit(群聊)/ Guest 三路(plan §11)。

- 私聊:sendMessageDraft 原生草稿流 → ~30s 窗口内 sendMessage 定稿
- 群聊:sendMessage 占位 → 节流 editMessageText → 末次编辑定稿
- Guest:answerGuestQuery 一次应答入口 → 返回 inline_message_id 上节流编辑
并发隔离:每个回答独立 draft_id / 占位消息,互不覆盖。

aiogram 3.28 已原生封装 SendMessageDraft / AnswerGuestQuery(Bot API 10.x),
若运行时 Telegram 服务端不支持(旧 Bot API),自动退化为占位+编辑流。
"""
from __future__ import annotations

import html
import re
import secrets
from typing import Any, Protocol

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import AnswerGuestQuery, EditMessageMedia, SendMessageDraft
from aiogram.types import (
    InlineQueryResultArticle,
    InputMediaAudio,
    InputMediaPhoto,
    InputMediaVideo,
    InputTextMessageContent,
)

from app.core.concurrency import SendRateLimiter
from app.core.ratelimit import EditThrottle
from app.logging import get_logger

log = get_logger("core.streaming")

TG_MESSAGE_LIMIT = 4096  # Telegram 单消息长度上限
TG_PARSE_MODE = "HTML"


def clip(text: str, limit: int = TG_MESSAGE_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def format_for_telegram(text: str) -> str:
    """Convert common Markdown emitted by the model into Telegram HTML."""
    placeholders: list[str] = []

    def stash(value: str) -> str:
        placeholders.append(value)
        return f"\u0000TGFMT{len(placeholders) - 1}\u0000"

    def fence_repl(match: re.Match[str]) -> str:
        code = match.group(2)
        return stash(f"<pre><code>{html.escape(code)}</code></pre>")

    def inline_code_repl(match: re.Match[str]) -> str:
        return stash(f"<code>{html.escape(match.group(1))}</code>")

    def link_repl(match: re.Match[str]) -> str:
        label = html.escape(match.group(1))
        url = html.escape(match.group(2), quote=True)
        return stash(f'<a href="{url}">{label}</a>')

    text = re.sub(r"```([A-Za-z0-9_+\-.#]*)\n?([\s\S]*?)```", fence_repl, text)
    text = re.sub(r"`([^`\n]+)`", inline_code_repl, text)
    text = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)", link_repl, text)

    escaped = html.escape(text)
    escaped = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", escaped)
    escaped = re.sub(r"__([^_\n]+)__", r"<b>\1</b>", escaped)
    escaped = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", escaped)
    escaped = re.sub(r"(?<!_)_([^_\n]+)_(?!_)", r"<i>\1</i>", escaped)

    for i, value in enumerate(placeholders):
        escaped = escaped.replace(f"\u0000TGFMT{i}\u0000", value)
    return escaped


def _render_for_telegram(text: str) -> str:
    raw = clip(text)
    rendered = format_for_telegram(raw)
    if len(rendered) <= TG_MESSAGE_LIMIT:
        return rendered
    while raw and len(html.escape(raw)) > TG_MESSAGE_LIMIT:
        raw = raw[:-1]
    return html.escape(raw)


async def _sleep_retry_after(e: TelegramRetryAfter) -> None:
    import asyncio

    log.warning("Telegram限流,按retry_after退避", 等待秒=e.retry_after)
    await asyncio.sleep(e.retry_after + 0.5)


class StreamRenderer(Protocol):
    async def start(self) -> None: ...
    async def update(self, full_text: str) -> None: ...
    async def finalize(self, full_text: str) -> int | None:
        """定稿;返回最终 message_id(可得时)。"""
        ...
    async def fail(self, error_text: str) -> None: ...


class DraftRenderer:
    """私聊:sendMessageDraft 原生草稿流。

    草稿是临时预览(~30s),不自动落地;必须窗口内 sendMessage 定稿。
    Telegram 服务端不支持时自动退化为占位+编辑流。
    """

    def __init__(self, bot: Bot, chat_id: int, limiter: SendRateLimiter,
                 throttle_ms: int = 1500) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._limiter = limiter
        self._draft_id = secrets.randbelow(2**31 - 1) + 1  # 每回答独立 draft_id
        # 草稿更新比编辑廉价,节流间隔取 1/3
        self._throttle = EditThrottle(throttle_ms=max(300, throttle_ms // 3))
        self._draft_supported = True
        self._fallback: EditRenderer | None = None

    async def _send_draft(self, text: str) -> None:
        await self._bot(SendMessageDraft(
            chat_id=self._chat_id,
            draft_id=self._draft_id,
            text=_render_for_telegram(text) or "…",
            parse_mode=TG_PARSE_MODE,
        ))

    async def start(self) -> None:
        log.debug("私聊不发送初始占位消息", 会话=self._chat_id, 草稿ID=self._draft_id)

    async def update(self, full_text: str) -> None:
        if not self._draft_supported:
            assert self._fallback is not None
            await self._fallback.update(full_text)
            return
        if not self._throttle.should_commit(full_text):
            return
        try:
            await self._limiter.acquire()
            await self._send_draft(full_text)
            self._throttle.mark_committed(full_text)
        except TelegramRetryAfter as e:
            await _sleep_retry_after(e)
        except Exception as e:
            log.warning("草稿更新失败(忽略,等待定稿)", 会话=self._chat_id,
                        错误=str(e)[:120])

    async def finalize(self, full_text: str) -> int | None:
        text = clip(full_text) or "(空回复)"
        if not self._draft_supported:
            assert self._fallback is not None
            return await self._fallback.finalize(full_text)
        while True:
            try:
                await self._limiter.acquire()
                msg = await self._bot.send_message(
                    self._chat_id, _render_for_telegram(text),
                    parse_mode=TG_PARSE_MODE,
                )
                log.info("私聊回复已定稿", 会话=self._chat_id, 消息ID=msg.message_id,
                         长度=len(text))
                return msg.message_id
            except TelegramRetryAfter as e:
                await _sleep_retry_after(e)

    async def fail(self, error_text: str) -> None:
        try:
            await self._limiter.acquire()
            await self._bot.send_message(
                self._chat_id, _render_for_telegram(error_text),
                parse_mode=TG_PARSE_MODE,
            )
        except Exception as e:
            log.error("发送错误提示失败", 会话=self._chat_id, 错误=str(e)[:120])


class EditRenderer:
    """群聊(成员):sendMessage 占位 → 节流 editMessageText → 末次定稿。"""

    def __init__(self, bot: Bot, chat_id: int, limiter: SendRateLimiter,
                 throttle_ms: int = 1500,
                 reply_to_message_id: int | None = None,
                 placeholder: str = "▌") -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._limiter = limiter
        self._throttle = EditThrottle(throttle_ms=throttle_ms)
        self._reply_to = reply_to_message_id
        self._placeholder = placeholder
        self._message_id: int | None = None

    async def start(self) -> None:
        while True:
            try:
                await self._limiter.acquire()
                msg = await self._bot.send_message(
                    self._chat_id, _render_for_telegram(self._placeholder),
                    reply_to_message_id=self._reply_to,
                    parse_mode=TG_PARSE_MODE,
                )
                self._message_id = msg.message_id
                log.debug("占位消息已发送", 会话=self._chat_id, 消息ID=msg.message_id)
                return
            except TelegramRetryAfter as e:
                await _sleep_retry_after(e)

    async def _edit(self, text: str) -> None:
        assert self._message_id is not None
        while True:
            try:
                await self._limiter.acquire()
                await self._bot.edit_message_text(
                    _render_for_telegram(text), chat_id=self._chat_id,
                    message_id=self._message_id,
                    parse_mode=TG_PARSE_MODE,
                )
                return
            except TelegramRetryAfter as e:
                await _sleep_retry_after(e)
            except TelegramBadRequest as e:
                if "message is not modified" in str(e):
                    return  # 文本未变化,无须处理
                raise

    async def update(self, full_text: str) -> None:
        if self._message_id is None:
            return
        if not self._throttle.should_commit(full_text):
            return
        try:
            await self._edit(full_text + " ▌")
            self._throttle.mark_committed(full_text)
        except Exception as e:
            log.warning("编辑流更新失败(忽略)", 会话=self._chat_id, 错误=str(e)[:120])

    async def finalize(self, full_text: str) -> int | None:
        text = full_text.strip() or "(空回复)"
        if self._message_id is None:
            await self.start()
        await self._edit(text)
        log.info("编辑流回复已定稿", 会话=self._chat_id, 消息ID=self._message_id,
                 长度=len(text))
        return self._message_id

    async def fail(self, error_text: str) -> None:
        try:
            if self._message_id is not None:
                await self._edit(error_text)
            else:
                await self._limiter.acquire()
                await self._bot.send_message(
                    self._chat_id, _render_for_telegram(error_text),
                    parse_mode=TG_PARSE_MODE,
                )
        except Exception as e:
            log.error("发送错误提示失败", 会话=self._chat_id, 错误=str(e)[:120])

    @property
    def message_id(self) -> int | None:
        return self._message_id


class GuestRenderer:
    """Guest 模式:answerGuestQuery 一次应答入口(每次召唤仅此一次),
    返回 SentGuestMessage.inline_message_id,后续在其上节流 editMessageText。
    """

    def __init__(self, bot: Bot, chat_id: int, guest_query_id: str,
                 limiter: SendRateLimiter, throttle_ms: int = 1500) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._guest_query_id = guest_query_id
        self._limiter = limiter
        self._throttle = EditThrottle(throttle_ms=throttle_ms)
        self._inline_message_id: str | None = None
        # Guest 单 inline 消息:同步生成的图片/语音暂存于此,finalize 时转为媒体
        self._pending_media: dict[str, Any] | None = None

    def attach_pending(self, kind: str, url: str, note: str | None) -> None:
        """暂存一个待投递媒体(图片=photo / 语音=audio)。仅保留首个。

        Guest 仅一条 inline 消息,故多媒体只展示第一个;note 追加到 caption。
        """
        if self._pending_media is None:
            self._pending_media = {"kind": kind, "url": url, "note": note or ""}

    async def start(self) -> None:
        try:
            await self._limiter.acquire()
            sent = await self._bot(AnswerGuestQuery(
                guest_query_id=self._guest_query_id,
                result=InlineQueryResultArticle(
                    id=self._guest_query_id[:60] or "answer",
                    title="回复",
                    input_message_content=InputTextMessageContent(
                        message_text=_render_for_telegram("▌"),
                        parse_mode=TG_PARSE_MODE,
                    ),
                ),
            ))
            self._inline_message_id = getattr(sent, "inline_message_id", None)
            log.info("Guest应答已发出", 会话=self._chat_id,
                     查询ID=self._guest_query_id,
                     内联消息ID=self._inline_message_id or "无")
        except Exception as e:
            log.error("answerGuestQuery失败", 查询ID=self._guest_query_id,
                      错误=str(e)[:200])
            raise

    async def _edit(self, text: str) -> None:
        if self._inline_message_id is None:
            return
        while True:
            try:
                await self._limiter.acquire()
                await self._bot.edit_message_text(
                    _render_for_telegram(text),
                    inline_message_id=self._inline_message_id,
                    parse_mode=TG_PARSE_MODE,
                )
                return
            except TelegramRetryAfter as e:
                await _sleep_retry_after(e)
            except TelegramBadRequest as e:
                if "message is not modified" in str(e):
                    return
                raise

    async def update(self, full_text: str) -> None:
        if self._inline_message_id is None:
            return
        if not self._throttle.should_commit(full_text):
            return
        try:
            await self._edit(full_text + " ▌")
            self._throttle.mark_committed(full_text)
        except Exception as e:
            log.warning("Guest编辑更新失败(忽略)", 会话=self._chat_id,
                        错误=str(e)[:120])

    async def _edit_media(self, media: Any) -> bool:
        """把 inline 消息转成媒体(editMessageMedia)。成功返回 True。"""
        if self._inline_message_id is None:
            return False
        try:
            await self._limiter.acquire()
            await self._bot(EditMessageMedia(
                inline_message_id=self._inline_message_id,
                media=media,
            ))
            return True
        except Exception as e:
            log.warning("Guest媒体编辑失败(降级为文本)", 会话=self._chat_id,
                        错误=str(e)[:160])
            return False

    async def finalize(self, full_text: str) -> int | None:
        text = (full_text.strip() or "(空回复)")
        pm = self._pending_media
        if pm and self._inline_message_id is not None:
            caption = _render_for_telegram(text)
            note = pm.get("note") or ""
            if note:
                caption = (caption + "\n" + note)[:TG_MESSAGE_LIMIT]
            kind, url = pm["kind"], pm["url"]
            media = self._build_media(kind, url, caption)
            ok = await self._edit_media(media)
            if not ok:
                # 降级:文本 + 可点击链接(URL 媒体体积超限等情况)
                link_line = f"\n\n✅ 已生成:[查看]({url})"
                await self._edit(text + link_line)
        else:
            await self._edit(text)
        log.info("Guest回复已定稿", 会话=self._chat_id,
                 内联消息ID=self._inline_message_id or "无",
                 形式=pm["kind"] if pm else "文本", 长度=len(full_text))
        return None

    @staticmethod
    def _build_media(kind: str, url: str, caption: str) -> Any:
        if kind == "photo":
            return InputMediaPhoto(media=url, caption=caption, parse_mode=TG_PARSE_MODE)
        if kind == "video":
            return InputMediaVideo(media=url, caption=caption, parse_mode=TG_PARSE_MODE)
        # audio(语音合成):Guest 无 Voice 类型,按 Audio 投递
        return InputMediaAudio(media=url, caption=caption, parse_mode=TG_PARSE_MODE)

    async def fail(self, error_text: str) -> None:
        try:
            if self._inline_message_id is not None:
                await self._edit(error_text)
        except Exception as e:
            log.error("Guest错误提示发送失败", 会话=self._chat_id, 错误=str(e)[:120])
