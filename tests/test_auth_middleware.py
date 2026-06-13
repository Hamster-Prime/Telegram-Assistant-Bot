"""鉴权中间件集成测试 —— extract_actor(含 Guest 召唤者)与门控行为。"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import Chat, InlineQuery, Message, Update
from aiogram.types import User as TgUser

from app.config import Settings
from app.core.auth import AuthMiddleware, extract_actor
from app.db.dao import DAOBundle
from app.db.engine import Database


def make_message(uid: int, text: str = "hi") -> Message:
    return Message.model_validate({
        "message_id": 1, "date": 0,
        "chat": {"id": 100, "type": "private"},
        "from": {"id": uid, "is_bot": False, "first_name": "U", "username": f"u{uid}"},
        "text": text,
    })


def make_guest_message(caller_id: int) -> Message:
    return Message.model_validate({
        "message_id": 2, "date": 0,
        "chat": {"id": 200, "type": "group"},
        "text": "@bot 你好",
        "guest_query_id": "gq-1",
        "guest_bot_caller_user": {
            "id": caller_id, "is_bot": False, "first_name": "C", "username": "caller"},
    })


def make_inline_query(uid: int, query: str = "你好") -> InlineQuery:
    return InlineQuery.model_validate({
        "id": "iq-1", "query": query, "offset": "",
        "from_user": {"id": uid, "is_bot": False, "first_name": "I", "username": f"i{uid}"},
    })


def test_extract_actor_private():
    msg = make_message(42)
    actor_id, username, first = extract_actor(msg)
    assert actor_id == 42 and username == "u42"


def test_extract_actor_guest_uses_caller():
    """Guest 模式按召唤者 guest_bot_caller_user 判定,而非 from_user。"""
    msg = make_guest_message(77)
    actor_id, username, _ = extract_actor(msg)
    assert actor_id == 77 and username == "caller"


def test_extract_actor_update_wrapping():
    u = Update.model_validate({
        "update_id": 1,
        "message": {
            "message_id": 1, "date": 0,
            "chat": {"id": 100, "type": "private"},
            "from": {"id": 5, "is_bot": False, "first_name": "X"},
            "text": "hello",
        },
    })
    actor_id, _, _ = extract_actor(u)
    assert actor_id == 5


@pytest.fixture
async def daos():
    db = Database(":memory:", wal=False)
    await db.connect()
    bundle = DAOBundle(db)
    yield bundle
    await db.close()


@pytest.fixture
def settings():
    return Settings(_env_file=None, minimax_api_keys="k1")


async def test_middleware_blocks_unauthorized(daos, settings):
    mw = AuthMiddleware(daos, settings)
    msg = make_message(1)
    # 绕过 frozen model:用 mock 替代 answer
    object.__setattr__(msg, "_bot", None)
    handler = AsyncMock(return_value="HANDLED")
    mw_send = AsyncMock()
    mw._send_denial = mw_send  # 不真发 Telegram

    result = await mw(handler, msg, {})
    assert result is None  # 链路被终止
    handler.assert_not_awaited()
    mw_send.assert_awaited_once()
    # 审计已记录
    rows = await daos.audit.recent(5)
    assert rows and rows[0]["action"] == "denied"


async def test_middleware_passes_authorized(daos, settings):
    mw = AuthMiddleware(daos, settings)
    await daos.users.upsert_basic(2, "u2", "U")
    await daos.users.set_authorized(2, True, by=999)

    msg = make_message(2)
    handler = AsyncMock(return_value="HANDLED")
    data: dict = {}
    result = await mw(handler, msg, data)
    assert result == "HANDLED"
    assert data["user"].tg_id == 2
    assert data["user"].is_allowed


async def test_middleware_superadmin_passes(daos, settings):
    mw = AuthMiddleware(daos, settings)
    await daos.users.ensure_superadmin(999)
    msg = make_message(999)
    handler = AsyncMock(return_value="OK")
    result = await mw(handler, msg, {})
    assert result == "OK"


async def test_middleware_guest_caller_authorized(daos, settings):
    """Guest 召唤:授权按召唤者判定。"""
    mw = AuthMiddleware(daos, settings)
    await daos.users.upsert_basic(77, "caller", "C")
    await daos.users.set_authorized(77, True, by=999)

    msg = make_guest_message(77)
    handler = AsyncMock(return_value="GUEST_OK")
    data: dict = {}
    result = await mw(handler, msg, data)
    assert result == "GUEST_OK"
    assert data["user"].tg_id == 77


async def test_middleware_guest_caller_denied(daos, settings):
    mw = AuthMiddleware(daos, settings)
    msg = make_guest_message(88)  # 未授权召唤者
    handler = AsyncMock()
    mw._send_denial = AsyncMock()
    result = await mw(handler, msg, {})
    assert result is None
    handler.assert_not_awaited()


def test_extract_actor_inline_query():
    """Inline 模式按 InlineQuery.from_user 判定发起人。"""
    iq = make_inline_query(55, "查天气")
    actor_id, username, _ = extract_actor(iq)
    assert actor_id == 55 and username == "i55"


async def test_middleware_inline_authorized_passes(daos, settings):
    """授权用户的 inline_query 通过门控并注入 user。"""
    mw = AuthMiddleware(daos, settings)
    await daos.users.upsert_basic(55, "i55", "I")
    await daos.users.set_authorized(55, True, by=999)
    iq = make_inline_query(55, "你好")
    handler = AsyncMock(return_value="INLINE_OK")
    data: dict = {}
    result = await mw(handler, iq, data)
    assert result == "INLINE_OK"
    assert data["user"].tg_id == 55


async def test_middleware_inline_denied_answers_query(daos, settings):
    """未授权用户的 inline_query 被拦截并返回"未授权"结果,handler 不执行。"""
    mw = AuthMiddleware(daos, settings)
    iq = make_inline_query(66, "你好")
    handler = AsyncMock()
    mw._deny_inline = AsyncMock()
    result = await mw(handler, iq, {})
    assert result is None
    handler.assert_not_awaited()
    mw._deny_inline.assert_awaited_once()
