"""Inline handler 测试 —— 启动器模式应答结果构造。

验证:授权用户得到含 @bot 提及的"提问"启动器;未授权得到拒绝结果。
不发起真实 Telegram 请求,只校验 query.answer 被调用且 results 内容正确。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.db.dao import DAOBundle
from app.db.engine import Database
from app.db.models import User
from app.handlers.inline import handle_inline_query


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


async def test_empty_query_returns_starter(svc):
    iq = FakeInlineQuery("")
    await handle_inline_query(iq, _make_user(1, True), svc)
    iq.answer.assert_awaited_once()
    args, kwargs = iq.answer.call_args
    results = args[0]
    assert len(results) == 1
    assert "向助理提问" in results[0].title
    # 空查询引导文本含 @bot
    assert "@testbot" in results[0].input_message_content.message_text


async def test_query_with_text_returns_launcher(svc):
    iq = FakeInlineQuery("今天天气怎么样")
    await handle_inline_query(iq, _make_user(2, True), svc)
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
    # 不缓存(is_personal)
    assert kwargs.get("cache_time") == 0
    assert kwargs.get("is_personal") is True


async def test_unauthorized_returns_denied(svc):
    iq = FakeInlineQuery("anything")
    await handle_inline_query(iq, _make_user(3, False), svc)
    iq.answer.assert_awaited_once()
    args, kwargs = iq.answer.call_args
    results = args[0]
    assert len(results) == 1
    assert "未授权" in results[0].title
    # 未授权结果缓存较久(避免重复打审计)
    assert kwargs.get("cache_time") == 300


async def test_none_user_returns_denied(svc):
    """防御:user 为 None 时也返回拒绝结果。"""
    iq = FakeInlineQuery("anything")
    await handle_inline_query(iq, None, svc)
    iq.answer.assert_awaited_once()
    args, _ = iq.answer.call_args
    assert "未授权" in args[0][0].title
