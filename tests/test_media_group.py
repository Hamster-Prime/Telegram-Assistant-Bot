"""媒体组聚合 + 元数据前缀剥离测试。"""
from __future__ import annotations

import asyncio

import pytest

from app.core.agent import _strip_meta_prefix
from app.handlers.media_group import MediaGroupBuffer

# ── _strip_meta_prefix ─────────────────────────────────────

def test_strip_meta_prefix_removes_assistant_header():
    """模型模仿了 [时间 · 🤖 助理] 前缀 → 剥离。"""
    text = "[2026-06-14 21:39:05(周日) · 🤖 助理]\n你好呀~"
    result = _strip_meta_prefix(text)
    assert result == "你好呀~"


def test_strip_meta_prefix_removes_user_header():
    """[时间 · 👤 用户] 前缀也剥离。"""
    text = "[2026-06-14 21:39:05(周六) · 👤 Alice]\n你好"
    result = _strip_meta_prefix(text)
    assert result == "你好"


def test_strip_meta_prefix_no_header_unchanged():
    """无前缀的文本不变。"""
    text = "你好呀~ 今天天气不错呢"
    assert _strip_meta_prefix(text) == text


def test_strip_meta_prefix_preserves_inline_brackets():
    """正文中的合法方括号不被误伤。"""
    text = "请参考 [这个文档] 和 [那个链接]"
    result = _strip_meta_prefix(text)
    assert result == text


def test_strip_meta_prefix_only_first_match():
    """只剥离开头第一个,不处理后续。"""
    text = "[2026-06-14(周六) · 🤖 助理]\n开头\n中间有 [2026-06-14(周六) · 👤 用户] 不应被删"
    result = _strip_meta_prefix(text)
    assert result.startswith("开头")
    assert "[2026-06-14(周六) · 👤 用户]" in result


def test_strip_meta_prefix_empty_after_strip():
    """全是前缀 → 剥离后为空。"""
    text = "[2026-06-14(周六) · 🤖 助理]\n"
    result = _strip_meta_prefix(text)
    assert result == ""


def test_strip_meta_prefix_with_leading_whitespace():
    """前缀前有空白也能剥离。"""
    text = "  \n [2026-06-14(周六) · 🤖 助理]\n你好"
    result = _strip_meta_prefix(text)
    assert result == "你好"


def test_strip_meta_prefix_non_meta_bracket_not_stripped():
    """不含 · 的方括号开头不被误剥(如 [引用] 这种)。"""
    text = "[引用消息]\n这是回复"
    result = _strip_meta_prefix(text)
    assert result == text


# ── MediaGroupBuffer ────────────────────────────────────────

def _make_msg(chat_id: int, msg_id: int, group_id: str | None = None,
              caption: str | None = None):
    from types import SimpleNamespace
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        message_id=msg_id,
        media_group_id=group_id,
        caption=caption,
        photo=None,
    )


async def test_media_group_buffer_aggregates_multiple_frames():
    """同一 media_group_id 的多条消息聚合后单次回调。"""
    buffer = MediaGroupBuffer(timeout_s=0.1)
    collected = []

    async def on_complete(messages):
        collected.append([m.message_id for m in messages])

    # 3 帧陆续到达
    await buffer.add_or_dispatch(_make_msg(100, 1, "grp1", "描述"), on_complete)
    await buffer.add_or_dispatch(_make_msg(100, 2, "grp1"), on_complete)
    await buffer.add_or_dispatch(_make_msg(100, 3, "grp1"), on_complete)

    # 等待 flush
    await asyncio.sleep(0.3)
    assert len(collected) == 1
    assert collected[0] == [1, 2, 3]  # 按 message_id 排序


async def test_media_group_buffer_non_group_returns_false():
    """非相册消息返回 False,不缓冲。"""
    buffer = MediaGroupBuffer()
    called = []

    async def on_complete(messages):
        called.append(messages)

    msg = _make_msg(100, 1, group_id=None)
    result = await buffer.add_or_dispatch(msg, on_complete)
    assert result is False
    await asyncio.sleep(0.2)
    assert len(called) == 0


async def test_media_group_buffer_separate_groups():
    """不同 chat_id / media_group_id 的消息分别聚合。"""
    buffer = MediaGroupBuffer(timeout_s=0.1)
    results = {}

    async def make_callback(label):
        async def on_complete(messages):
            results[label] = [m.message_id for m in messages]
        return on_complete

    await buffer.add_or_dispatch(_make_msg(100, 1, "grpA"),
                                 await make_callback("A"))
    await buffer.add_or_dispatch(_make_msg(200, 10, "grpB"),
                                 await make_callback("B"))
    await asyncio.sleep(0.3)

    assert results.get("A") == [1]
    assert results.get("B") == [10]


async def test_media_group_buffer_different_groups_same_chat():
    """同一会话不同 media_group_id 分别聚合。"""
    buffer = MediaGroupBuffer(timeout_s=0.1)
    results = {}

    async def cb_a(messages):
        results["A"] = len(messages)

    async def cb_b(messages):
        results["B"] = len(messages)

    await buffer.add_or_dispatch(_make_msg(100, 1, "grpA"), cb_a)
    await buffer.add_or_dispatch(_make_msg(100, 2, "grpA"), cb_a)
    await buffer.add_or_dispatch(_make_msg(100, 10, "grpB"), cb_b)
    await asyncio.sleep(0.3)

    assert results.get("A") == 2
    assert results.get("B") == 1
