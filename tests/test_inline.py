"""Inline handler 测试 —— 启动器 + 命令快捷菜单应答结果构造。

验证:
- 授权用户空查询得到「命令菜单 + 启动器」(多条结果,缓存 300s)
- / 开头查询过滤匹配命令 + 启动器(缓存 60s)
- 普通文本查询得到启动器(缓存 60s)
- 命令文章 message_text 形如 "@bot /cmd"(发送后触发 Guest 命令分流)
- 未授权得到拒绝结果(缓存 0,授权变更后即时生效)
不发起真实 Telegram 请求,只校验 query.answer 被调用且 results 内容正确。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.db.dao import DAOBundle
from app.db.engine import Database
from app.db.models import User
from app.handlers.inline import INLINE_COMMANDS, handle_inline_query


def _make_user(uid: int, allowed: bool, role: str = "user") -> User:
    return User(
        tg_id=uid, username=f"u{uid}", first_name="U", role=role,
        authorized=1 if allowed else 0, authorized_by=None, authorized_at=None,
        settings="{}", created_at=0, updated_at=0,
    )


class FakeInlineQuery:
    """最小可用 InlineQuery 替身:记录 answer 调用参数。"""

    def __init__(self, query: str = "") -> None:
        self.id = "iq-test"
        self.query = query
        self.answer = AsyncMock()


class FakeBot:
    def __init__(self, username: str = "testbot"):
        self._username = username

    async def me(self):
        return SimpleNamespace(username=self._username)


@pytest.fixture
async def svc():
    """最小 Services 替身(只需 .bot.me())。"""
    return SimpleNamespace(bot=FakeBot("testbot"))


# ── 空查询:命令菜单 + 启动器 ──────────────────────────────────
async def test_empty_query_returns_commands_plus_starter(svc):
    iq = FakeInlineQuery("")
    await handle_inline_query(iq, _make_user(1, True), svc)
    iq.answer.assert_awaited_once()
    args, kwargs = iq.answer.call_args
    results = args[0]
    # 命令菜单(5) + 启动器(1)
    assert len(results) == len(INLINE_COMMANDS) + 1
    # 前 5 条是命令文章
    cmd_titles = [r.title for r in results[:len(INLINE_COMMANDS)]]
    assert any("查看帮助" in t for t in cmd_titles)
    assert any("我的身份" in t for t in cmd_titles)
    # 末条是启动器
    assert "向助理提问" in results[-1].title
    # 命令文章 message_text 形如 "@testbot /cmd"
    for r in results[:len(INLINE_COMMANDS)]:
        assert r.input_message_content.message_text.startswith("@testbot /")
    # 静态菜单缓存 300s
    assert kwargs.get("cache_time") == 300
    assert kwargs.get("is_personal") is True


async def test_empty_query_starter_message_text_contains_mention(svc):
    """空查询的启动器引导文本含 @bot 提及。"""
    iq = FakeInlineQuery("")
    await handle_inline_query(iq, _make_user(2, True), svc)
    args, _ = iq.answer.call_args
    starter = args[0][-1]
    assert "@testbot" in starter.input_message_content.message_text


# ── / 前缀查询:过滤匹配命令 + 启动器 ───────────────────────────
async def test_slash_prefix_filters_matching_commands(svc):
    iq = FakeInlineQuery("/he")
    await handle_inline_query(iq, _make_user(3, True), svc)
    args, kwargs = iq.answer.call_args
    results = args[0]
    # /he 命中 /help;无匹配则返回全部,这里应只命中 help
    cmd_results = [r for r in results if r.id.startswith("cmd")]
    assert len(cmd_results) == 1
    assert "查看帮助" in cmd_results[0].title
    # 命令文章 message_text 含 @bot /help
    assert cmd_results[0].input_message_content.message_text == "@testbot /help"
    # 末尾附启动器
    assert "向助理提问" in results[-1].title
    assert kwargs.get("cache_time") == 60


async def test_slash_only_returns_all_commands(svc):
    """只输入 / 的查询(无前缀字符)→ 返回全部命令 + 启动器。"""
    iq = FakeInlineQuery("/")
    await handle_inline_query(iq, _make_user(4, True), svc)
    args, _ = iq.answer.call_args
    results = args[0]
    cmd_results = [r for r in results if r.id.startswith("cmd")]
    assert len(cmd_results) == len(INLINE_COMMANDS)


async def test_slash_unknown_prefix_returns_all_commands(svc):
    """输入 /xyz 无匹配 → 返回全部命令(便于用户看到可用选项)。"""
    iq = FakeInlineQuery("/xyznomatch")
    await handle_inline_query(iq, _make_user(5, True), svc)
    args, _ = iq.answer.call_args
    results = args[0]
    cmd_results = [r for r in results if r.id.startswith("cmd")]
    assert len(cmd_results) == len(INLINE_COMMANDS)


# ── 普通文本查询:仅启动器 ─────────────────────────────────────
async def test_text_query_returns_launcher_only(svc):
    iq = FakeInlineQuery("今天天气怎么样")
    await handle_inline_query(iq, _make_user(6, True), svc)
    iq.answer.assert_awaited_once()
    args, kwargs = iq.answer.call_args
    results = args[0]
    assert len(results) == 1
    title = results[0].title
    text = results[0].input_message_content.message_text
    # 启动器标题含查询词预览
    assert "向助理提问" in title
    assert "今天天气怎么样" in title
    # message_text 带 @bot 提及 → 发送后触发 Guest Mode 应答
    assert text.startswith("@testbot ")
    assert "今天天气怎么样" in text
    # 文本查询缓存 60s
    assert kwargs.get("cache_time") == 60
    assert kwargs.get("is_personal") is True


# ── 未授权 ─────────────────────────────────────────────────────
async def test_unauthorized_returns_denied(svc):
    iq = FakeInlineQuery("anything")
    await handle_inline_query(iq, _make_user(7, False), svc)
    iq.answer.assert_awaited_once()
    args, kwargs = iq.answer.call_args
    results = args[0]
    assert len(results) == 1
    assert "未授权" in results[0].title
    # 未授权结果不缓存(is_personal):授权后即时生效
    assert kwargs.get("cache_time") == 0
    assert kwargs.get("is_personal") is True


async def test_unauthorized_empty_query_returns_denied(svc):
    """未授权用户空查询也只看到拒绝结果(不暴露命令菜单)。"""
    iq = FakeInlineQuery("")
    await handle_inline_query(iq, _make_user(8, False), svc)
    args, kwargs = iq.answer.call_args
    assert len(args[0]) == 1
    assert "未授权" in args[0][0].title
    assert kwargs.get("cache_time") == 0


async def test_none_user_returns_denied(svc):
    """防御:user 为 None 时也返回拒绝结果。"""
    iq = FakeInlineQuery("anything")
    await handle_inline_query(iq, None, svc)
    iq.answer.assert_awaited_once()
    args, _ = iq.answer.call_args
    assert "未授权" in args[0][0].title


# ── INLINE_COMMANDS 元数据校验 ─────────────────────────────────
def test_inline_commands_all_start_with_slash():
    """所有命令均以 / 开头(无参数信息命令)。"""
    for cmd, _title, _desc in INLINE_COMMANDS:
        assert cmd.startswith("/")


def test_inline_commands_count():
    """快捷菜单命令数量 = 5(/help /whoami /quota /reset /start)。"""
    cmds = [c[0] for c in INLINE_COMMANDS]
    assert cmds == ["/help", "/whoami", "/quota", "/reset", "/start"]
