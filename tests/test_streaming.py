"""流式渲染测试 —— 节流判定 + Edit/Guest 渲染器行为(统一轮询循环)。"""
from __future__ import annotations

import asyncio
import time

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText

from app.core.concurrency import SendRateLimiter
from app.core.ratelimit import EditThrottle
from app.core.streaming import (
    DraftRenderer,
    EditRenderer,
    GuestRenderer,
    TG_MESSAGE_LIMIT,
    _render_for_telegram,
    clip,
    format_for_telegram,
)


def test_clip():
    assert clip("短文本") == "短文本"
    long = "x" * 5000
    assert len(clip(long)) == 4096
    assert clip(long).endswith("…")


def test_throttle_no_change_no_commit():
    t = EditThrottle(throttle_ms=100)
    t.mark_committed("hello")
    assert not t.should_commit("hello")
    assert not t.should_commit("hello", final=True)


def test_throttle_interval():
    t = EditThrottle(throttle_ms=50, min_delta_chars=1000)
    t.mark_committed("a")
    assert not t.should_commit("ab")
    time.sleep(0.06)
    assert t.should_commit("ab")


def test_throttle_delta_chars():
    t = EditThrottle(throttle_ms=10_000, min_delta_chars=80)
    t.mark_committed("")
    assert not t.should_commit("x" * 79)
    assert t.should_commit("x" * 80)


def test_throttle_final_forces():
    t = EditThrottle(throttle_ms=10_000, min_delta_chars=10_000)
    t.mark_committed("")
    assert t.should_commit("结尾", final=True)


class FakeMessage:
    def __init__(self, message_id: int):
        self.message_id = message_id


class FakeBot:
    """记录调用的假 Bot(支持 chat_action + 错误注入)。"""

    def __init__(self):
        self.sent: list[tuple] = []
        self.sent_kwargs: list[dict] = []
        self.edits: list[tuple] = []
        self.edit_kwargs: list[dict] = []
        self.chat_actions: list[tuple] = []
        self.methods: list[object] = []
        self._next_id = 100
        self.edit_error: Exception | None = None
        self.edit_error_always: bool = False
        self._edit_error_fired = False

    async def __call__(self, method):
        self.methods.append(method)

    async def send_message(self, chat_id, text, **kwargs):
        self._next_id += 1
        self.sent.append((chat_id, text))
        self.sent_kwargs.append(kwargs)
        return FakeMessage(self._next_id)

    async def edit_message_text(self, text, chat_id=None, message_id=None, **kwargs):
        if self.edit_error is not None and (
            self.edit_error_always or not self._edit_error_fired
        ):
            self._edit_error_fired = True
            raise self.edit_error
        self.edits.append((chat_id, message_id, text))
        self.edit_kwargs.append(kwargs)

    async def send_chat_action(self, chat_id, action, **kwargs):
        self.chat_actions.append((chat_id, action))


@pytest.fixture
def limiter():
    return SendRateLimiter(rate_per_sec=10_000)


# ── 基础生命周期 ─────────────────────────────────────────────

async def test_edit_renderer_lifecycle(limiter):
    bot = FakeBot()
    r = EditRenderer(bot, chat_id=42, limiter=limiter, throttle_ms=1,
                     typing_refresh_s=10)
    await r.start()
    assert bot.sent == [(42, "▌")]  # 占位

    await r.update("第一段")
    await asyncio.sleep(0.02)  # 让轮询循环 tick 一次
    mid = await r.finalize("第一段完整回复")

    assert mid == 101
    # 末次编辑是定稿全文
    assert bot.edits[-1] == (42, 101, "第一段完整回复")


def test_format_for_telegram_converts_common_markdown_to_html():
    assert format_for_telegram("**粗体** 和 `代码`") == "<b>粗体</b> 和 <code>代码</code>"


def test_render_for_telegram_respects_limit_after_html_escaping():
    assert len(_render_for_telegram("&" * 5000)) <= TG_MESSAGE_LIMIT


async def test_edit_renderer_sends_telegram_html(limiter):
    bot = FakeBot()
    r = EditRenderer(bot, chat_id=42, limiter=limiter, throttle_ms=1,
                     typing_refresh_s=10)
    await r.start()
    await r.finalize("**粗体**")

    assert bot.sent_kwargs[0].get("parse_mode") == "HTML"
    assert bot.edit_kwargs[-1].get("parse_mode") == "HTML"
    assert bot.edits[-1][2] == "<b>粗体</b>"


async def test_edit_renderer_fail_path(limiter):
    bot = FakeBot()
    r = EditRenderer(bot, chat_id=42, limiter=limiter, typing_refresh_s=10)
    await r.start()
    await r.fail("❌ 出错了")
    assert bot.edits[-1][2] == "❌ 出错了"


async def test_edit_renderer_empty_final(limiter):
    bot = FakeBot()
    r = EditRenderer(bot, chat_id=42, limiter=limiter, typing_refresh_s=10)
    await r.start()
    await r.finalize("")
    assert bot.edits[-1][2] == "(空回复)"


async def test_draft_renderer_does_not_send_initial_placeholder_in_private(limiter):
    bot = FakeBot()
    r = DraftRenderer(bot, chat_id=42, limiter=limiter, typing_refresh_s=10)

    await r.start()
    await r.finalize("**最终回复**")

    assert bot.sent == [(42, "<b>最终回复</b>")]
    assert bot.sent_kwargs[-1].get("parse_mode") == "HTML"


# ── GuestRenderer:媒体投递 ──────────────────────────────────
class GuestFakeBot:
    """支持 __call__(AnswerGuestQuery/EditMessageMedia)的假 Bot。"""

    def __init__(self, inline_message_id: str = "inline-1"):
        self._inline_id = inline_message_id
        self.media_edits: list = []
        self.text_edits: list = []

    async def __call__(self, method):
        from aiogram.methods import AnswerGuestQuery, EditMessageMedia
        if isinstance(method, AnswerGuestQuery):
            from types import SimpleNamespace
            return SimpleNamespace(inline_message_id=self._inline_id)
        if isinstance(method, EditMessageMedia):
            self.media_edits.append(method)
            from types import SimpleNamespace
            return SimpleNamespace(ok=True)
        return None

    async def edit_message_text(self, text, **kwargs):
        self.text_edits.append((text, kwargs))


async def test_guest_renderer_text_finalize_no_media(limiter):
    bot = GuestFakeBot()
    r = GuestRenderer(bot, chat_id=9, guest_query_id="gq-x",
                      limiter=limiter, throttle_ms=1)
    await r.start()
    assert r._inline_message_id == "inline-1"
    await r.finalize("普通文本回复")
    assert bot.media_edits == []
    assert bot.text_edits and bot.text_edits[-1][0] == "普通文本回复"


async def test_guest_renderer_finalizes_as_photo_when_pending(limiter):
    bot = GuestFakeBot()
    r = GuestRenderer(bot, chat_id=9, guest_query_id="gq-x",
                      limiter=limiter, throttle_ms=1)
    await r.start()
    r.attach_pending("photo", "https://cdn.test/img.jpg", note="备注")
    await r.finalize("看这张图")

    assert len(bot.media_edits) == 1
    media = bot.media_edits[0].media
    from aiogram.types import InputMediaPhoto
    assert isinstance(media, InputMediaPhoto)
    assert media.media == "https://cdn.test/img.jpg"
    assert "看这张图" in media.caption
    assert "备注" in media.caption


async def test_guest_renderer_finalizes_as_audio_for_voice(limiter):
    bot = GuestFakeBot()
    r = GuestRenderer(bot, chat_id=9, guest_query_id="gq-x",
                      limiter=limiter, throttle_ms=1)
    await r.start()
    r.attach_pending("audio", "https://cdn.test/speech.mp3", note=None)
    await r.finalize("已朗读")

    assert len(bot.media_edits) == 1
    from aiogram.types import InputMediaAudio
    assert isinstance(bot.media_edits[0].media, InputMediaAudio)
    assert bot.media_edits[0].media.media == "https://cdn.test/speech.mp3"


async def test_guest_renderer_only_first_media_attached(limiter):
    bot = GuestFakeBot()
    r = GuestRenderer(bot, chat_id=9, guest_query_id="gq-x",
                      limiter=limiter, throttle_ms=1)
    await r.start()
    r.attach_pending("photo", "https://cdn.test/a.jpg", note=None)
    r.attach_pending("photo", "https://cdn.test/b.jpg", note=None)
    assert r._pending_media["url"] == "https://cdn.test/a.jpg"
    await r.finalize("x")


# ── 统一轮询循环:内容更新与闪烁共用间隔 ───────────────────

async def test_tick_loop_updates_content(limiter):
    """update() 暂存文本,轮询循环在下一个 tick 编辑它。"""
    bot = FakeBot()
    r = EditRenderer(bot, chat_id=42, limiter=limiter, throttle_ms=1,
                     typing_refresh_s=10)
    await r.start()
    await r.update("新内容")
    await asyncio.sleep(0.03)  # 让轮询循环 tick
    # 占位之后的编辑里应包含"新内容"
    content_edits = [e for e in bot.edits if "新内容" in e[2]]
    assert len(content_edits) >= 1
    await r.finalize("新内容完成")


async def test_tick_loop_cursor_blinks_when_idle(limiter):
    """内容静默时轮询循环翻转光标闪烁。"""
    bot = GuestFakeBot()
    r = GuestRenderer(bot, chat_id=9, guest_query_id="gq-x",
                      limiter=limiter, throttle_ms=1)
    await r.start()
    await r.update("固定文本")
    await asyncio.sleep(0.05)  # 让轮询循环 tick 多次(内容更新 + 闪烁)
    # 应有多条编辑(内容更新 + 后续闪烁)
    assert len(bot.text_edits) >= 2
    # 其中一些带光标,一些不带(翻转)
    with_cursor = [e for e in bot.text_edits if e[0].endswith(" ▌")]
    without_cursor = [e for e in bot.text_edits if not e[0].endswith(" ▌")]
    assert len(with_cursor) >= 1
    assert len(without_cursor) >= 1
    await r.finalize("固定文本")


async def test_tick_loop_respects_interval(limiter):
    """轮询循环按 interval 节奏编辑,不超速(统一门控)。"""
    bot = FakeBot()
    r = EditRenderer(bot, chat_id=42, limiter=limiter, throttle_ms=50,
                     typing_refresh_s=10)
    await r.start()
    # 密集调用 update(模拟流式 token 高速到达)
    for i in range(20):
        await r.update(f"内容{i}")
        await asyncio.sleep(0.001)
    await asyncio.sleep(0.08)  # ~1.6 个 tick(50ms 间隔)
    await r.finalize("最终")
    # 在 ~80ms 内(含 start 的占位),编辑次数应受 interval 限制(≤3-4 次)
    # 占位(1) + ≤2 次 tick 编辑 + 定稿(1)
    assert len(bot.edits) <= 5


# ── 定稿防弹化(修复「语句截断」)─────────────────────────────

async def test_finalize_falls_back_to_plain_text_on_html_error(limiter):
    """HTML 解析失败时,finalize 降级为纯文本编辑,保证完整文本落地。"""
    bot = FakeBot()
    bot.edit_error = TelegramBadRequest(
        method=EditMessageText, message="Bad Request: can't parse entities")
    r = EditRenderer(bot, chat_id=42, limiter=limiter, throttle_ms=1,
                     typing_refresh_s=10)
    await r.start()
    await r.finalize("好的,我来为您生成")

    # 纯文本降级:parse_mode=None,文本为完整原文(转义后)
    assert bot.edit_kwargs[-1].get("parse_mode") is None
    assert "好的,我来为您生成" in bot.edits[-1][2]
    assert r.last_rendered_text == "好的,我来为您生成"


async def test_finalize_swallows_persistent_error_no_raise(limiter):
    """finalize 全部失败时不抛异常(避免冒泡 errors.py)。"""
    bot = FakeBot()
    bot.edit_error = TelegramBadRequest(
        method=EditMessageText, message="can't parse entities")
    bot.edit_error_always = True
    r = EditRenderer(bot, chat_id=42, limiter=limiter, throttle_ms=1,
                     typing_refresh_s=10)
    await r.start()
    await r.finalize("完整回复文本")  # 不应抛出
    assert r.last_rendered_text == ""


async def test_finalize_lands_complete_text_after_throttle_skip(limiter):
    """节流跳过中间 update,finalize 仍落地完整末次文本(不截断)。"""
    bot = FakeBot()
    r = EditRenderer(bot, chat_id=42, limiter=limiter, throttle_ms=1,
                     typing_refresh_s=10)
    await r.start()
    await r.update("好的")
    await r.update("好的,我来为您")
    await r.finalize("好的,我来为您生成")
    assert bot.edits[-1][2] == "好的,我来为您生成"
    assert r.last_rendered_text == "好的,我来为您生成"


# ── typing 状态 ─────────────────────────────────────────────

async def test_edit_renderer_sends_typing_action(limiter):
    """群聊 EditRenderer.start 后发送 typing chat action。"""
    bot = FakeBot()
    r = EditRenderer(bot, chat_id=42, limiter=limiter, throttle_ms=1,
                     typing_refresh_s=0.05)
    await r.start()
    await asyncio.sleep(0.1)
    assert len(bot.chat_actions) >= 1
    assert bot.chat_actions[0][0] == 42
    await r.finalize("完成")


async def test_draft_renderer_typing_stops_after_first_token(limiter):
    """私聊 DraftRenderer 首次 update 后停止 typing。"""
    bot = FakeBot()
    r = DraftRenderer(bot, chat_id=42, limiter=limiter, throttle_ms=1,
                      typing_refresh_s=0.05)
    await r.start()
    await asyncio.sleep(0.08)
    actions_before = len(bot.chat_actions)
    assert actions_before >= 1
    await r.update("首个token")
    await asyncio.sleep(0.1)
    actions_after = len(bot.chat_actions)
    assert actions_after == actions_before  # typing 已停
    await r.finalize("首个token 完整")


async def test_guest_renderer_does_not_send_typing(limiter):
    """Guest 不支持 sendChatAction。"""
    bot = GuestFakeBot()
    r = GuestRenderer(bot, chat_id=9, guest_query_id="gq-x",
                      limiter=limiter, throttle_ms=1)
    await r.start()
    # GuestFakeBot 无 send_chat_action;若调用会 AttributeError,但 Guest 不调
    await r.finalize("Guest回复")
