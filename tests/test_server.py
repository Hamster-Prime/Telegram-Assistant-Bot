"""server 回调端点测试 —— 用真实 build_app 验证 challenge 握手、幂等回填、健康检查。"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Bot, Dispatcher
from aiohttp.test_utils import TestClient, TestServer

from app.core.concurrency import TaskRegistry
from app.db.dao import DAOBundle
from app.db.engine import Database
from app.db.models import Generation
from app.server import build_app


@pytest.fixture
async def client_env():
    db = Database(":memory:", wal=False)
    await db.connect()
    daos = DAOBundle(db)
    registry = TaskRegistry()

    svc = MagicMock()
    svc.daos = daos
    svc.registry = registry
    svc.mmx.key_count = 2
    svc.workers.finalize_video = AsyncMock()
    svc.workers.handle_video_failed_callback = AsyncMock()
    svc.settings.webhook_secret = "s3cret"

    bot = Bot("12345:TEST_FAKE_TOKEN")
    dp = Dispatcher()
    app = build_app(dp, bot, svc)

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    yield client, daos, svc
    await client.close()
    await registry.shutdown()
    await bot.session.close()
    await db.close()


async def _drain(registry: TaskRegistry):
    """等待 spawn 出的回填任务执行完。"""
    import asyncio
    for _ in range(50):
        if registry.count == 0:
            return
        await asyncio.sleep(0.02)


async def test_challenge_echo(client_env):
    client, _, _ = client_env
    resp = await client.post("/mmx/callback", json={"challenge": "abc123"})
    assert resp.status == 200
    assert (await resp.json())["challenge"] == "abc123"


async def test_success_callback_triggers_finalize(client_env):
    client, daos, svc = client_env
    gen = Generation(id=None, user_id=1, chat_id=1, kind="video", model="m",
                     prompt="p", status="processing", task_id="t-1")
    await daos.generations.create(gen)
    resp = await client.post("/mmx/callback", json={
        "task_id": "t-1", "status": "success", "file_id": "f-1"})
    assert resp.status == 200
    await _drain(svc.registry)
    svc.workers.finalize_video.assert_awaited_once()


async def test_failed_callback_routes_to_fail_handler(client_env):
    client, daos, svc = client_env
    gen = Generation(id=None, user_id=1, chat_id=1, kind="video", model="m",
                     prompt="p", status="processing", task_id="t-3")
    await daos.generations.create(gen)
    resp = await client.post("/mmx/callback", json={
        "task_id": "t-3", "status": "failed"})
    assert resp.status == 200
    await _drain(svc.registry)
    svc.workers.handle_video_failed_callback.assert_awaited_once()


async def test_callback_idempotent_on_done(client_env):
    """已终态任务再收回调:幂等忽略。"""
    client, daos, svc = client_env
    gen = Generation(id=None, user_id=1, chat_id=1, kind="video", model="m",
                     prompt="p", status="processing", task_id="t-2")
    gen_id = await daos.generations.create(gen)
    await daos.generations.update_status(gen_id, "success")
    resp = await client.post("/mmx/callback", json={
        "task_id": "t-2", "status": "success", "file_id": "f-2"})
    assert resp.status == 200
    await _drain(svc.registry)
    svc.workers.finalize_video.assert_not_awaited()


async def test_unknown_task_ignored(client_env):
    client, _, svc = client_env
    resp = await client.post("/mmx/callback", json={
        "task_id": "ghost", "status": "success", "file_id": "f"})
    assert resp.status == 200
    await _drain(svc.registry)
    svc.workers.finalize_video.assert_not_awaited()


async def test_invalid_json_400(client_env):
    client, _, _ = client_env
    resp = await client.post("/mmx/callback", data=b"not-json",
                             headers={"Content-Type": "application/json"})
    assert resp.status == 400


async def test_healthz(client_env):
    client, _, _ = client_env
    resp = await client.get("/healthz")
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True
    assert body["MiniMaxKey数"] == 2


async def test_webhook_requires_secret(client_env):
    """Webhook 端点缺少 secret 头应被拒(401/403)。"""
    client, _, _ = client_env
    resp = await client.post("/tg/s3cret", json={"update_id": 1})
    assert resp.status in (401, 403)
