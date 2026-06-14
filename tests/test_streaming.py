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
    TG_MESSAGE_LIMIT,
    DraftRenderer,
    EditRenderer,
    GuestRenderer,
    _render_for_telegram,
    clip,
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
        self.send_error: Exception | None = None
        self.send_error_always: bool = False
        self._send_error_fired = False

    async def __call__(self, method):
        self.methods.append(method)

    async def send_message(self, chat_id, text, **kwargs):
        if self.send_error is not None and (
            self.send_error_always or not self._send_error_fired
        ):
            self._send_error_fired = True
            raise self.send_error
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


def test_render_for_telegram_passes_through_valid_html():
    assert _render_for_telegram("<b>粗体</b> 和 <code>代码</code>") == (
        "<b>粗体</b> 和 <code>代码</code>"
    )


def test_render_for_telegram_respects_limit_after_html_escaping():
    assert len(_render_for_telegram("&" * 5000)) <= TG_MESSAGE_LIMIT


async def test_edit_renderer_sends_telegram_html(limiter):
    bot = FakeBot()
    r = EditRenderer(bot, chat_id=42, limiter=limiter, throttle_ms=1,
                     typing_refresh_s=10)
    await r.start()
    await r.finalize("<b>粗体</b>")

    assert bot.sent_kwargs[0].get("parse_mode") == "HTML"
    assert bot.edit_kwargs[-1].get("parse_mode") == "HTML"
    assert bot.edits[-1][2] == "<b>粗体</b>"


async def test_edit_renderer_fail_path(limiter):
    bot = FakeBot()
    r = EditRenderer(bot, chat_id=42, limiter=limiter, throttle_ms=1,
                     typing_refresh_s=10)
    await r.start()
    await r.fail("❌ 出错了")
    assert bot.edits[-1][2] == "❌ 出错了"


async def test_edit_renderer_empty_final(limiter):
    bot = FakeBot()
    r = EditRenderer(bot, chat_id=42, limiter=limiter, throttle_ms=1,
                     typing_refresh_s=10)
    await r.start()
    await r.finalize("")
    assert bot.edits[-1][2] == "(空回复)"


async def test_draft_renderer_does_not_send_initial_placeholder_in_private(limiter):
    bot = FakeBot()
    r = DraftRenderer(bot, chat_id=42, limiter=limiter, typing_refresh_s=10)

    await r.start()
    await r.finalize("<b>最终回复</b>")

    assert bot.sent == [(42, "<b>最终回复</b>")]
    assert bot.sent_kwargs[-1].get("parse_mode") == "HTML"


async def test_draft_finalize_falls_back_to_plain_text_on_html_error(limiter):
    """私聊定稿:HTML 解析失败时降级纯文本,不丢整条回复。"""
    bot = FakeBot()
    bot.send_error = TelegramBadRequest(
        method=EditMessageText, message="Bad Request: can't parse entities")
    r = DraftRenderer(bot, chat_id=42, limiter=limiter, typing_refresh_s=10)
    await r.start()
    mid = await r.finalize("好的,<b>我来</b>为您生成")

    # 第一次 HTML 发送失败 → 降级纯文本(parse_mode=None)成功
    assert bot.sent, "降级后应有一条成功发送"
    assert bot.sent_kwargs[-1].get("parse_mode") is None
    assert "好的" in bot.sent[-1][1] and "我来" in bot.sent[-1][1]
    assert mid is not None


async def test_draft_finalize_swallows_persistent_error_no_raise(limiter):
    """私聊定稿全部失败也不抛异常(避免冒泡 errors.py 覆盖消息)。"""
    bot = FakeBot()
    bot.send_error = TelegramBadRequest(
        method=EditMessageText, message="can't parse entities")
    bot.send_error_always = True
    r = DraftRenderer(bot, chat_id=42, limiter=limiter, typing_refresh_s=10)
    await r.start()
    mid = await r.finalize("完整回复文本")  # 不应抛出
    assert mid is None


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
    """内容写入不带光标;静默时翻转后缀光标闪烁。"""
    bot = GuestFakeBot()
    r = GuestRenderer(bot, chat_id=9, guest_query_id="gq-x",
                      limiter=limiter, throttle_ms=1)
    await r.start()
    await r.update("固定文本")
    await asyncio.sleep(0.05)  # 让轮询循环 tick 多次(内容更新 + 闪烁)
    # 应有多条编辑(内容更新 + 后续闪烁)
    assert len(bot.text_edits) >= 2
    # 首次编辑是内容写入,必须不带光标
    assert "固定文本" in bot.text_edits[0][0]
    assert not bot.text_edits[0][0].endswith(" ▌")
    # 后续静默编辑:一些带光标,一些不带(翻转)
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


# ── 光标行为:占位闪烁 / 内容无光标 / 写后必亮 ─────────────────

async def test_placeholder_blinks_before_first_content(limiter):
    """首条内容到达前,占位消息整条光标 ▌ ↔ nbsp 交替闪烁。"""
    bot = FakeBot()
    r = EditRenderer(bot, chat_id=42, limiter=limiter, throttle_ms=10,
                     typing_refresh_s=10)
    await r.start()  # 占位发送 "▌",_cursor_on=True
    # 不调用 update,让占位阶段闪烁多次(interval=10ms)
    await asyncio.sleep(0.055)
    await r.finalize("▌")  # 停止 tick;传占位字符避免影响断言
    # 取出文本编辑(排除 send 占位)
    edits_text = [e[2] for e in bot.edits]
    on_off = [t for t in edits_text if t in ("▌", "\u00a0")]
    assert len(on_off) >= 2, f"应有多于一次占位闪烁,实际 {on_off}"
    # 应同时存在亮态 ▌ 与灭态 nbsp(交替)
    assert "▌" in on_off
    assert "\u00a0" in on_off


async def test_content_edit_has_no_cursor_suffix(limiter):
    """内容写入编辑不含光标后缀 " ▌"。"""
    bot = GuestFakeBot()
    r = GuestRenderer(bot, chat_id=9, guest_query_id="gq-x",
                      limiter=limiter, throttle_ms=1)
    await r.start()
    await r.update("正文内容")
    await asyncio.sleep(0.02)  # 让内容写入 tick 落地
    # 首次编辑是内容写入,无光标
    assert bot.text_edits, "应有一次内容写入编辑"
    first = bot.text_edits[0][0]
    assert "正文内容" in first
    assert not first.endswith(" ▌")
    await r.finalize("正文内容完成")


async def test_cursor_bright_after_content_write(limiter):
    """写完内容后,首个静默闪烁编辑必带亮光标 " ▌"。"""
    bot = GuestFakeBot()
    r = GuestRenderer(bot, chat_id=9, guest_query_id="gq-x",
                      limiter=limiter, throttle_ms=1)
    await r.start()
    await r.update("一段文字")
    await asyncio.sleep(0.02)  # 内容写入落地(_cursor_on 被置 False)
    # 内容写入编辑(无光标)应在
    content_edits = [e for e in bot.text_edits if "一段文字" in e[0]
                     and not e[0].endswith(" ▌")]
    assert content_edits, "应先有一次无光标的内容写入"
    await asyncio.sleep(0.03)  # 静默闪烁 tick
    # 紧随其后的闪烁编辑必须带亮光标
    bright = [e for e in bot.text_edits if e[0].endswith(" ▌")]
    assert bright, "写完内容后下一个闪烁应带亮光标"
    await r.finalize("一段文字完成")


async def test_ensure_interval_elapsed_enforces_gap(limiter):
    """_ensure_interval_elapsed 在间隔不足时补齐 sleep,已超间隔则零等待。"""
    bot = FakeBot()
    r = EditRenderer(bot, chat_id=42, limiter=limiter, throttle_ms=100,
                     typing_refresh_s=10)
    # 情景 A:刚编辑过(gap 不足)→ sleep 补齐到 ~100ms
    r._last_edit_time = time.monotonic()
    t0 = time.monotonic()
    await r._ensure_interval_elapsed()
    assert time.monotonic() - t0 >= 0.09, "间隔不足时应 sleep 补齐"

    # 情景 B:上次编辑在 1s 前(已超间隔)→ 立即返回不 sleep
    r._last_edit_time = time.monotonic() - 1.0
    t0 = time.monotonic()
    await r._ensure_interval_elapsed()
    assert time.monotonic() - t0 < 0.01, "已超间隔应零等待"

    # 情景 C:从未编辑(_last_edit_time=0)→ 不 sleep
    r._last_edit_time = 0.0
    t0 = time.monotonic()
    await r._ensure_interval_elapsed()
    assert time.monotonic() - t0 < 0.01


async def test_finalize_enforces_interval_after_recent_edit(limiter):
    """finalize 紧随末次编辑时由咽喉点补齐,定稿编辑距上次 ≥ interval。"""
    bot = GuestFakeBot()
    r = GuestRenderer(bot, chat_id=9, guest_query_id="gq-x",
                      limiter=limiter, throttle_ms=100)
    await r.start()
    await r.update("内容")
    # 等待内容写入 tick 落地(interval≈100ms,需略多)
    await asyncio.sleep(0.12)
    assert len(bot.text_edits) >= 1, "应已写入内容"
    # 立即 finalize:距末次编辑(约 20ms 前)< interval,必须补齐
    t_before = time.monotonic()
    await r.finalize("内容完成")
    elapsed = time.monotonic() - t_before
    # 无门控时 finalize 应 <5ms;补齐后应 ≥ interval 的大半(≥50ms 证明已补齐)
    assert elapsed >= 0.05, f"finalize 应补齐间隔,实际 {elapsed:.3f}s"
    assert bot.text_edits[-1][0] == "内容完成"


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


# ── 真实节流参数下的完整性回归(排查「定稿截断」)─────────────

async def test_realistic_guest_stream_lands_complete_text(limiter):
    """真实 Guest 节流(1000ms):多次 update 后 finalize,末次编辑须为完整文本。"""
    bot = GuestFakeBot()
    r = GuestRenderer(bot, chat_id=9, guest_query_id="gq-x",
                      limiter=limiter, throttle_ms=1000)
    await r.start()
    full = "你好,这是一段足够长的完整回复文本,用于验证定稿不被截断。"
    # 模拟流式:逐步投递前缀
    for i in range(1, len(full), 5):
        await r.update(full[:i])
    await r.finalize(full)
    # 核心断言:末次落地编辑必须是完整文本
    assert bot.text_edits[-1][0] == full, (
        f"末次编辑被截断! 期望 {len(full)} 字,实际 {len(bot.text_edits[-1][0])} 字")
    assert r.last_rendered_text == full


async def test_realistic_group_stream_lands_complete_text(limiter):
    """真实群聊节流(3000ms):update 被 tick 节流跳过,finalize 仍落地完整文本。"""
    bot = FakeBot()
    r = EditRenderer(bot, chat_id=42, limiter=limiter, throttle_ms=3000,
                     typing_refresh_s=10)
    await r.start()
    full = "这是一段在群聊场景下的完整回复,验证 3 秒节流不会导致定稿截断。"
    for i in range(1, len(full), 4):
        await r.update(full[:i])
        await asyncio.sleep(0.001)
    await r.finalize(full)
    assert bot.edits[-1][2] == full, (
        f"末次编辑被截断! 期望完整文本,实际: {bot.edits[-1][2]!r}")
    assert r.last_rendered_text == full


async def test_fast_response_finalize_complete(limiter):
    """快速响应:start 后立刻 finalize(无中间 update),完整文本须落地。"""
    bot = FakeBot()
    r = EditRenderer(bot, chat_id=42, limiter=limiter, throttle_ms=1000,
                     typing_refresh_s=10)
    await r.start()
    full = "快速回复的完整内容。"
    await r.finalize(full)
    assert bot.edits[-1][2] == full
    assert r.last_rendered_text == full


async def test_streaming_visible_content_eventually_complete(limiter):
    """端到端:tick 提交中间内容后,finalize 落地完整文本(末次编辑=完整)。"""
    bot = GuestFakeBot()
    r = GuestRenderer(bot, chat_id=9, guest_query_id="gq-x",
                      limiter=limiter, throttle_ms=50)
    await r.start()
    await r.update("开头部分")
    await asyncio.sleep(0.12)  # 让 tick 提交中间内容
    assert len(bot.text_edits) >= 1, "tick 应已提交中间内容"
    full = "开头部分,这里是流式追加的后续完整结尾文本。"
    await r.finalize(full)
    assert bot.text_edits[-1][0] == full, (
        f"末次编辑不完整! 期望 {full!r},实际 {bot.text_edits[-1][0]!r}")
