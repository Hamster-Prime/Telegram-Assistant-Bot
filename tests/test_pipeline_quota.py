"""pipeline 工具配额测试 —— web_fetch 应与 web_search 一样计配额。"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.core.quota import QuotaManager
from app.db.dao import DAOBundle
from app.db.engine import Database
from app.handlers.pipeline import build_dispatcher


@pytest.fixture
async def env():
    db = Database(":memory:", wal=False)
    await db.connect()
    daos = DAOBundle(db)
    settings = Settings(
        _env_file=None,
        minimax_api_keys="k1",
        default_quota_mode="calls",
        default_quota_limit=1000,
        default_quota_period="day",
        gen_call_weights="image:1,video:5,music:5,tts:1,search:1,fetch:1",
    )
    quota = QuotaManager(daos, settings)

    await daos.users.upsert_basic(1, "bob", "Bob")
    await daos.users.set_authorized(1, True, by=999)
    user = await daos.users.get(1)

    svc = SimpleNamespace(
        bot=None, limiter=None, files_api=None, daos=daos, quota=quota,
        settings=settings,
        search=SimpleNamespace(
            fetch=AsyncMock(return_value="网页正文 markdown"),
            search=AsyncMock(return_value=[
                {"title": "t", "url": "u", "snippet": "s", "source": "brave"}]),
        ),
    )
    yield svc, daos, quota, user
    await db.close()


async def test_web_fetch_settles_quota(env):
    svc, daos, quota, user = env
    await quota.ensure_default(1)
    d = build_dispatcher(svc, user, chat_id=10, scope="user", scope_owner=1)

    result = await d.dispatch("web_fetch", json.dumps({"url": "https://x.com"}))
    assert "网页正文" in result

    q = await daos.quotas.get(1, "calls")
    assert q.used == 1  # fetch 已计 1 次


async def test_web_fetch_denied_when_over_quota(env):
    svc, daos, quota, user = env
    # 配额上限 1,先用满
    await daos.quotas.set(1, "calls", 1, "day")
    await quota.settle(user, "calls", 1, chat_id=10, kind="search")

    d = build_dispatcher(svc, user, chat_id=10, scope="user", scope_owner=1)
    result = await d.dispatch("web_fetch", json.dumps({"url": "https://x.com"}))

    assert "配额不足" in result
    svc.search.fetch.assert_not_awaited()  # 配额拦截,不真正抓取


async def test_web_search_still_settles(env):
    """回归:web_search 仍正常计配额。"""
    svc, daos, quota, user = env
    await quota.ensure_default(1)
    d = build_dispatcher(svc, user, chat_id=10, scope="user", scope_owner=1)

    await d.dispatch("web_search", json.dumps({"query": "hello"}))
    q = await daos.quotas.get(1, "calls")
    assert q.used == 1
