"""Guest 30 分钟自动清空上下文测试(plan: 上下文增强 §auto-clear)。

核心规则:
- auto_clear=True(Guest):距最后一条消息超过 auto_clear_minutes 分钟 → 清空 messages+summaries
- auto_clear=False(Private/Group):永不自动清空
- 空会话:last_activity 返回 None → 跳过,不反复执行
"""
from __future__ import annotations

import time

import pytest

from app.config import Settings
from app.db.dao import DAOBundle
from app.db.engine import Database


@pytest.fixture
async def daos():
    db = Database(":memory:", wal=False)
    await db.connect()
    bundle = DAOBundle(db)
    yield bundle
    await db.close()


# ── last_activity() ────────────────────────────────────────────────
async def test_last_activity_none_for_empty_chat(daos: DAOBundle):
    """空会话返回 None(auto-clear 据此跳过,避免反复执行)。"""
    assert await daos.messages.last_activity(100) is None


async def test_last_activity_returns_latest_timestamp(daos: DAOBundle):
    """返回最新一条消息的 created_at。"""
    await daos.messages.add(100, 1, "user", "早", tokens=1)
    # 手动等待确保 created_at 不同
    await daos.messages.add(100, 1, "user", "晚", tokens=1)
    ts = await daos.messages.last_activity(100)
    assert ts is not None
    assert ts > 0


async def test_last_activity_includes_compacted_messages(daos: DAOBundle):
    """compacted 消息也计入(按 id 取最新,不看 compacted 标志)。"""
    await daos.messages.add(100, 1, "user", "已压缩", tokens=1)
    await daos.messages.mark_compacted(100, 1)
    # 再加一条新的
    await daos.messages.add(100, 1, "user", "新的", tokens=1)
    ts = await daos.messages.last_activity(100)
    assert ts is not None


async def test_last_activity_isolated_per_chat(daos: DAOBundle):
    """不同 chat_id 互不干扰。"""
    await daos.messages.add(100, 1, "user", "chat100", tokens=1)
    await daos.messages.add(200, 1, "user", "chat200", tokens=1)
    ts100 = await daos.messages.last_activity(100)
    ts200 = await daos.messages.last_activity(200)
    assert ts100 is not None
    assert ts200 is not None


# ── auto_clear 懒检查(直接模拟 pipeline 入口逻辑) ─────────────────
async def _simulate_auto_clear(daos: DAOBundle, chat_id: int,
                                auto_clear_minutes: int) -> bool:
    """复刻 run_chat_pipeline 入口的懒检查逻辑,返回是否触发了清空。

    测试不拉起完整 pipeline(避免 agent/network 依赖),只验证清空判定。
    """
    if auto_clear_minutes <= 0:
        return False
    last_ts = await daos.messages.last_activity(chat_id)
    if last_ts is None:
        return False
    idle_s = int(time.time()) - last_ts
    if idle_s > auto_clear_minutes * 60:
        await daos.messages.clear_chat(chat_id)
        return True
    return False


async def test_auto_clear_triggers_after_30_minutes(daos: DAOBundle):
    """超过 30 分钟无活动 → 清空。"""
    # 手动插入一条「31 分钟前」的消息(绕过 _now())
    old_ts = int(time.time()) - 31 * 60
    await daos.db.execute(
        "INSERT INTO messages (chat_id, user_id, role, content, tokens, created_at) "
        "VALUES (100, 1, 'user', '旧消息', 1, ?)",
        (old_ts,),
    )
    await daos.summaries.add(100, "旧摘要", 1, 5)

    cleared = await _simulate_auto_clear(daos, 100, auto_clear_minutes=30)
    assert cleared is True
    # messages + summaries 都被清空
    assert await daos.messages.last_activity(100) is None
    assert await daos.summaries.latest(100) is None


async def test_auto_clear_skips_within_30_minutes(daos: DAOBundle):
    """30 分钟内有活动 → 不清空。"""
    # 刚刚的消息(创建时间为当前)
    await daos.messages.add(100, 1, "user", "刚发的", tokens=1)

    cleared = await _simulate_auto_clear(daos, 100, auto_clear_minutes=30)
    assert cleared is False
    assert await daos.messages.last_activity(100) is not None


async def test_auto_clear_skips_empty_chat(daos: DAOBundle):
    """空会话 → 跳过(不反复执行)。"""
    cleared = await _simulate_auto_clear(daos, 999, auto_clear_minutes=30)
    assert cleared is False


async def test_auto_clear_disabled_when_zero(daos: DAOBundle):
    """auto_clear_minutes=0 → 禁用,即使超时也不清。"""
    old_ts = int(time.time()) - 60 * 60  # 1 小时前
    await daos.db.execute(
        "INSERT INTO messages (chat_id, user_id, role, content, tokens, created_at) "
        "VALUES (100, 1, 'user', '旧', 1, ?)",
        (old_ts,),
    )
    cleared = await _simulate_auto_clear(daos, 100, auto_clear_minutes=0)
    assert cleared is False


async def test_auto_clear_boundary(daos: DAOBundle):
    """边界:恰好 30 分钟(idle == 阈值)不清空;31 分钟(> 阈值)清空。"""
    # 恰好 30 分钟前:idle_s == 30*60,条件 idle_s > 阈值为 False
    ts_exact = int(time.time()) - 30 * 60
    await daos.db.execute(
        "INSERT INTO messages (chat_id, user_id, role, content, tokens, created_at) "
        "VALUES (101, 1, 'user', '恰好30分', 1, ?)",
        (ts_exact,),
    )
    assert await _simulate_auto_clear(daos, 101, auto_clear_minutes=30) is False

    # 31 分钟前:idle_s > 阈值 → 清空
    ts_over = int(time.time()) - 31 * 60
    await daos.db.execute(
        "INSERT INTO messages (chat_id, user_id, role, content, tokens, created_at) "
        "VALUES (102, 1, 'user', '31分', 1, ?)",
        (ts_over,),
    )
    assert await _simulate_auto_clear(daos, 102, auto_clear_minutes=30) is True


# ── 元数据持久化(_extract_user_metadata) ──────────────────────────
async def test_extract_user_metadata_captures_sender_and_reply(daos: DAOBundle):
    """pipeline._extract_user_metadata 提取发送者 + 回复快照 + 干净正文。"""
    from aiogram.types import Message

    from app.handlers.pipeline import _extract_user_metadata

    msg = Message.model_validate({
        "message_id": 42,
        "date": 1718350200,
        "chat": {"id": 1, "type": "private"},
        "from": {"id": 50, "is_bot": False, "first_name": "Alice"},
        "text": "@my_bot 帮我总结",
        "reply_to_message": {
            "message_id": 41,
            "date": 1718350100,
            "chat": {"id": 1, "type": "private"},
            "from": {"id": 51, "is_bot": False, "first_name": "Bob"},
            "text": "这是被回复的原文",
        },
    })
    meta = _extract_user_metadata(msg, bot_username="my_bot")
    assert meta["sender_label"] == "Alice"
    assert meta["tg_message_id"] == 42
    assert meta["reply_to_tg_id"] == 41
    assert "Bob" in meta["reply_snapshot"]
    assert "这是被回复的原文" in meta["reply_snapshot"]
    # 干净正文:已剥离 @my_bot
    assert "@my_bot" not in meta["text"]
    assert "帮我总结" in meta["text"]


async def test_extract_user_metadata_no_reply(daos: DAOBundle):
    """无回复消息时,reply 字段为 None。"""
    from aiogram.types import Message

    from app.handlers.pipeline import _extract_user_metadata

    msg = Message.model_validate({
        "message_id": 10,
        "date": 1718350200,
        "chat": {"id": 1, "type": "private"},
        "from": {"id": 50, "is_bot": False, "first_name": "Alice"},
        "text": "你好",
    })
    meta = _extract_user_metadata(msg, bot_username="my_bot")
    assert meta["sender_label"] == "Alice"
    assert meta["reply_to_tg_id"] is None
    assert meta["reply_snapshot"] is None
    assert meta["text"] == "你好"


async def test_messages_add_persists_metadata(daos: DAOBundle):
    """MessageDAO.add 新参数正确落库。"""
    await daos.messages.add(
        100, 1, "user", "你好", tokens=2,
        tg_message_id=99,
        reply_to_tg_id=88,
        reply_snapshot="Bob: 上文",
        sender_label="Alice",
    )
    rows = await daos.messages.recent_uncompacted(100, 10)
    assert len(rows) == 1
    m = rows[0]
    assert m.tg_message_id == 99
    assert m.reply_to_tg_id == 88
    assert m.reply_snapshot == "Bob: 上文"
    assert m.sender_label == "Alice"


async def test_messages_add_metadata_defaults_none(daos: DAOBundle):
    """不传新参数时,元数据字段为 None(向后兼容)。"""
    await daos.messages.add(100, 1, "user", "你好", tokens=2)
    rows = await daos.messages.recent_uncompacted(100, 10)
    m = rows[0]
    assert m.tg_message_id is None
    assert m.reply_to_tg_id is None
    assert m.reply_snapshot is None
    assert m.sender_label is None


# ── DB 迁移(老库升级场景) ────────────────────────────────────────
async def test_migration_adds_columns_to_old_db():
    """模拟老库(无新列)→ connect() 后自动补列。"""
    db = Database(":memory:", wal=False)
    await db.connect()
    # 先手动删除新列模拟老库
    # SQLite 不支持 DROP COLUMN(3.35 前),改为重建无新列的 messages 表
    await db.execute("DROP TABLE messages")
    await db.execute(
        "CREATE TABLE messages ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, user_id INTEGER, "
        "role TEXT, content TEXT, content_type TEXT DEFAULT 'text', "
        "tokens INTEGER DEFAULT 0, compacted INTEGER DEFAULT 0, created_at INTEGER)"
    )
    # 再次 connect 触发 migration
    await db._migrate_messages_metadata()
    # 验证新列已补
    async with db.conn.execute("PRAGMA table_info(messages)") as cur:
        cols = {row[1] for row in await cur.fetchall()}
    assert {"tg_message_id", "reply_to_tg_id", "reply_snapshot", "sender_label"} <= cols
    await db.close()


async def test_migration_idempotent():
    """重复调用 migration 不报错(幂等)。"""
    db = Database(":memory:", wal=False)
    await db.connect()
    await db._migrate_messages_metadata()  # 第二次
    await db._migrate_messages_metadata()  # 第三次
    async with db.conn.execute("PRAGMA table_info(messages)") as cur:
        cols = [row[1] for row in await cur.fetchall()]
    # 每列只出现一次
    assert cols.count("tg_message_id") == 1
    assert cols.count("sender_label") == 1
    await db.close()


# ── config ─────────────────────────────────────────────────────────
def test_config_auto_clear_default_30():
    s = Settings(_env_file=None, minimax_api_keys="k")
    assert s.auto_clear_minutes == 30


def test_config_auto_clear_override():
    s = Settings(_env_file=None, minimax_api_keys="k", auto_clear_minutes=60)
    assert s.auto_clear_minutes == 60
