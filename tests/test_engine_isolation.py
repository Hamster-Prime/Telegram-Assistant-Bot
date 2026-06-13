"""数据库引擎隔离性测试 —— 读不应泄漏进未提交的写事务(生产配置:文件 + WAL)。"""
from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

from app.db.engine import Database


@pytest.fixture
async def db():
    # 用临时文件 + WAL(生产配置):独立只读连接看到的是已提交快照,
    # 不会脏读、也不会被写事务阻塞。内存库无法复现 WAL 的快照语义。
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    d = Database(path, wal=True)
    await d.connect()
    yield d
    await d.close()
    for ext in ("", "-wal", "-shm"):
        try:
            os.remove(path + ext)
        except OSError:
            pass


async def test_read_does_not_see_uncommitted_write(db: Database):
    """immediate() 事务进行中,并发读不得看到未提交的脏数据。

    WAL 下读连接拿到的是「最近已提交快照」:写事务把 v 改成 99 但未提交时,
    读到的必须仍是已提交的 0(绝不能是 99),且读不被阻塞。
    """
    await db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v INTEGER)")
    await db.execute("INSERT INTO t (id, v) VALUES (1, 0)")

    read_result: list[int] = []
    in_tx = asyncio.Event()
    release_tx = asyncio.Event()

    async def writer():
        async with db.immediate() as conn:
            await conn.execute("UPDATE t SET v=99 WHERE id=1")
            in_tx.set()              # 已写未提交
            await release_tx.wait()  # 拖住事务不提交

    async def reader():
        await in_tx.wait()
        row = await db.fetch_one("SELECT v FROM t WHERE id=1")
        read_result.append(row["v"])
        release_tx.set()            # 读完放行提交

    await asyncio.wait_for(asyncio.gather(writer(), reader()), timeout=5.0)

    assert read_result == [0]       # 已提交快照,不是脏的 99
    row = await db.fetch_one("SELECT v FROM t WHERE id=1")
    assert row["v"] == 99           # 提交后可见


async def test_read_write_basic_roundtrip(db: Database):
    """基础读写经独立连接仍正确(读连接能看到已提交写)。"""
    await db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    rid = await db.execute("INSERT INTO t (v) VALUES ('hello')")
    row = await db.fetch_one("SELECT v FROM t WHERE id=?", (rid,))
    assert row["v"] == "hello"
