"""流式渲染测试 —— 节流判定 + Edit 渲染器行为(模拟 Bot)。"""
from __future__ import annotations

import asyncio
import time

import pytest

from app.core.concurrency import SendRateLimiter
from app.core.ratelimit import EditThrottle
from app.core.streaming import EditRenderer, clip


def test_clip():
    assert clip("短文本") == "短文本"
    long = "x" * 5000
    assert len(clip(long)) == 4096
    assert clip(long).endswith("…")


def test_throttle_no_change_no_commit():
    t = EditThrottle(throttle_ms=100)
    t.mark_committed("hello")
    assert not t.should_commit("hello")
    assert not t.should_commit("hello", final=True)  # 终稿但无变化也不提交


def test_throttle_interval():
    t = EditThrottle(throttle_ms=50, min_delta_chars=1000)
    t.mark_committed("a")
    assert not t.should_commit("ab")  # 间隔未到、增量不足
    time.sleep(0.06)
    assert t.should_commit("ab")  # 间隔已到


def test_throttle_delta_chars():
    t = EditThrottle(throttle_ms=10_000, min_delta_chars=80)
    t.mark_committed("")
    assert not t.should_commit("x" * 79)
    assert t.should_commit("x" * 80)  # 增量达标,无视间隔


def test_throttle_final_forces():
    t = EditThrottle(throttle_ms=10_000, min_delta_chars=10_000)
    t.mark_committed("")
    assert t.should_commit("结尾", final=True)


class FakeMessage:
    def __init__(self, message_id: int):
        self.message_id = message_id


class FakeBot:
    """记录调用的假 Bot。"""

    def __init__(self):
        self.sent: list[tuple] = []
        self.edits: list[tuple] = []
        self._next_id = 100

    async def send_message(self, chat_id, text, **kwargs):
        self._next_id += 1
        self.sent.append((chat_id, text))
        return FakeMessage(self._next_id)

    async def edit_message_text(self, text, chat_id=None, message_id=None, **kwargs):
        self.edits.append((chat_id, message_id, text))


@pytest.fixture
def limiter():
    return SendRateLimiter(rate_per_sec=10_000)  # 测试不限速


async def test_edit_renderer_lifecycle(limiter):
    bot = FakeBot()
    r = EditRenderer(bot, chat_id=42, limiter=limiter, throttle_ms=1)
    await r.start()
    assert bot.sent == [(42, "▌")]  # 占位

    await asyncio.sleep(0.01)
    await r.update("第一段")
    mid = await r.finalize("第一段完整回复")

    assert mid == 101
    # 末次编辑是定稿全文
    assert bot.edits[-1] == (42, 101, "第一段完整回复")


async def test_edit_renderer_fail_path(limiter):
    bot = FakeBot()
    r = EditRenderer(bot, chat_id=42, limiter=limiter)
    await r.start()
    await r.fail("❌ 出错了")
    assert bot.edits[-1][2] == "❌ 出错了"


async def test_edit_renderer_empty_final(limiter):
    bot = FakeBot()
    r = EditRenderer(bot, chat_id=42, limiter=limiter)
    await r.start()
    await r.finalize("")
    assert bot.edits[-1][2] == "(空回复)"
