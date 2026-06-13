"""鉴权 + 配额测试。"""
from __future__ import annotations

import time

import pytest

from app.config import Settings
from app.core.quota import QuotaManager, _window_expired
from app.db.dao import DAOBundle
from app.db.engine import Database
from app.db.models import Quota, User


@pytest.fixture
async def daos():
    db = Database(":memory:", wal=False)
    await db.connect()
    bundle = DAOBundle(db)
    yield bundle
    await db.close()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        minimax_api_keys="k1",
        default_quota_mode="tokens",
        default_quota_limit=1000,
        default_quota_period="day",
    )


async def test_user_authorization_flow(daos: DAOBundle):
    u = await daos.users.upsert_basic(1, "alice", "Alice")
    assert not u.is_allowed  # 默认未授权

    await daos.users.set_authorized(1, True, by=999)
    u = await daos.users.get(1)
    assert u.is_allowed

    await daos.users.set_authorized(1, False, by=999)
    u = await daos.users.get(1)
    assert not u.is_allowed


async def test_superadmin_always_allowed(daos: DAOBundle):
    await daos.users.ensure_superadmin(999)
    u = await daos.users.get(999)
    assert u.is_superadmin and u.is_allowed
    # ensure_superadmin 幂等且不可降级
    await daos.users.ensure_superadmin(999)
    u = await daos.users.get(999)
    assert u.role == "superadmin" and u.authorized == 1


async def test_quota_precheck_and_settle(daos: DAOBundle, settings: Settings):
    qm = QuotaManager(daos, settings)
    await daos.users.upsert_basic(1, "bob", "Bob")
    await daos.users.set_authorized(1, True, by=999)
    user = await daos.users.get(1)

    await qm.ensure_default(1)
    check = await qm.precheck(user, "tokens", estimated=500)
    assert check.ok

    await qm.settle(user, "tokens", 900, chat_id=10, kind="chat")
    q = await daos.quotas.get(1, "tokens")
    assert q.used == 900

    # 900 + 500 > 1000 → 拒绝
    check = await qm.precheck(user, "tokens", estimated=500)
    assert not check.ok
    assert "配额不足" in check.denial_text()
    assert "900/1000" in check.denial_text()


async def test_quota_unlimited(daos: DAOBundle, settings: Settings):
    qm = QuotaManager(daos, settings)
    await daos.users.upsert_basic(2, "carol", "Carol")
    user = await daos.users.get(2)
    await daos.quotas.set(2, "calls", -1, "day")  # -1 = 无限
    check = await qm.precheck(user, "calls", estimated=99999)
    assert check.ok


async def test_quota_lazy_window_reset(daos: DAOBundle, settings: Settings):
    qm = QuotaManager(daos, settings)
    await daos.users.upsert_basic(3, "dave", "Dave")
    user = await daos.users.get(3)
    await daos.quotas.set(3, "tokens", 1000, "day")
    await qm.settle(user, "tokens", 999, chat_id=1)

    # 手动把 window_start 拨回 2 天前 → 预检应触发惰性重置
    two_days_ago = int(time.time()) - 86400 * 2
    await daos.db.execute(
        "UPDATE quotas SET window_start=? WHERE user_id=3 AND mode='tokens'",
        (two_days_ago,),
    )
    check = await qm.precheck(user, "tokens", estimated=500)
    assert check.ok  # 窗口过期已归零
    q = await daos.quotas.get(3, "tokens")
    assert q.used == 0


async def test_quota_superadmin_bypass(daos: DAOBundle, settings: Settings):
    qm = QuotaManager(daos, settings)
    await daos.users.ensure_superadmin(999)
    su = await daos.users.get(999)
    await daos.quotas.set(999, "tokens", 10, "day")
    check = await qm.precheck(su, "tokens", estimated=99999)
    assert check.ok  # 超管绕过
    await qm.settle(su, "tokens", 99999, chat_id=1)
    q = await daos.quotas.get(999, "tokens")
    assert q.used == 0  # 超管不累计


async def test_concurrent_settle_no_race(daos: DAOBundle, settings: Settings):
    """并发结算不丢计数(BEGIN IMMEDIATE + 写锁)。"""
    import asyncio

    qm = QuotaManager(daos, settings)
    await daos.users.upsert_basic(4, "eve", "Eve")
    user = await daos.users.get(4)
    await daos.quotas.set(4, "tokens", 100000, "day")

    await asyncio.gather(*[
        qm.settle(user, "tokens", 10, chat_id=1) for _ in range(50)
    ])
    q = await daos.quotas.get(4, "tokens")
    assert q.used == 500  # 50×10 一个不丢


def test_window_expired_helper():
    now_ts = int(time.time())
    q = Quota(user_id=1, mode="tokens", period="day", limit_val=100,
              window_start=now_ts - 90000)
    assert _window_expired(q, now_ts)
    q2 = Quota(user_id=1, mode="tokens", period="total", limit_val=100,
               window_start=now_ts - 999999)
    assert not _window_expired(q2, now_ts)  # total 永不重置


async def test_settle_writes_quota_and_usage(daos: DAOBundle, settings: Settings):
    """结算同时落配额与 usage_log 流水。"""
    qm = QuotaManager(daos, settings)
    await daos.users.upsert_basic(5, "u5", "U5")
    user = await daos.users.get(5)
    await daos.quotas.set(5, "tokens", 100000, "day")

    await qm.settle(user, "tokens", 123, chat_id=7, kind="chat")

    q = await daos.quotas.get(5, "tokens")
    assert q.used == 123
    stats = await daos.usage.stats(since=0)
    assert any(s["kind"] == "chat" and (s["Token量"] or 0) == 123 for s in stats)


async def test_settle_atomic_quota_and_usage(daos: DAOBundle, settings: Settings):
    """配额更新与 usage 流水原子:usage 写入失败时配额不应被单独提交。"""
    qm = QuotaManager(daos, settings)
    await daos.users.upsert_basic(6, "u6", "U6")
    user = await daos.users.get(6)
    await daos.quotas.set(6, "tokens", 100000, "day")

    # 注入:让事务内的 usage 插入抛错(模拟写流水失败)
    import app.core.quota as quota_mod
    orig = quota_mod.QuotaManager._usage_insert_sql

    def boom(self, mode, amount, chat_id, kind):
        raise RuntimeError("usage insert failed")

    quota_mod.QuotaManager._usage_insert_sql = boom
    try:
        with pytest.raises(RuntimeError):
            await qm.settle(user, "tokens", 999, chat_id=7, kind="chat")
    finally:
        quota_mod.QuotaManager._usage_insert_sql = orig

    # 原子性:usage 失败 → 配额回滚,used 仍为 0
    q = await daos.quotas.get(6, "tokens")
    assert q.used == 0
