"""SQLite 异步引擎 —— aiosqlite + WAL 模式。

单写多读:WAL 模式 + busy_timeout;配额读-改-写用 BEGIN IMMEDIATE 防竞态。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import aiosqlite

from app.logging import get_logger

log = get_logger("db.engine")

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


class Database:
    """写连接 + 独立只读连接 + 写锁。

    - 写:单写连接 `_conn`,写事务用 asyncio.Lock 串行化(避免 database is locked)。
    - 读:独立只读连接 `_read_conn`,**不参与写锁**。WAL 模式下读连接看到的是
      最近一次已提交的快照,因此读永远不会泄漏进写连接尚未提交的 BEGIN IMMEDIATE
      事务(杜绝脏读),也不会被写事务阻塞。
    - 文件库:两个连接指向同一文件;内存库:用 shared-cache URI 让两连接共享同一库。
    """

    def __init__(self, db_path: str | Path, wal: bool = True) -> None:
        self._path = Path(db_path)
        self._wal = wal
        self._conn: aiosqlite.Connection | None = None
        self._read_conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()
        # 内存库:两个连接必须共享同一份数据 → 用具名 shared-cache URI
        self._is_memory = str(db_path) in (":memory:", "")
        self._uri = self._is_memory

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("数据库尚未连接,请先调用 connect()")
        return self._conn

    @property
    def read_conn(self) -> aiosqlite.Connection:
        if self._read_conn is None:
            raise RuntimeError("数据库尚未连接,请先调用 connect()")
        return self._read_conn

    def _dsn(self) -> str:
        """连接串。内存库用 shared-cache 具名 URI 让读写连接共享同一库;
        名字带实例 id,保证不同 Database 实例(如各测试)互不共享。"""
        if self._is_memory:
            return f"file:tgmem_{id(self)}?mode=memory&cache=shared"
        return str(self._path)

    async def connect(self) -> None:
        if not self._is_memory:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        dsn = self._dsn()
        self._conn = await aiosqlite.connect(dsn, uri=self._uri)
        self._conn.row_factory = aiosqlite.Row
        if self._wal:
            await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        schema = _SCHEMA_PATH.read_text(encoding="utf-8")
        await self._conn.executescript(schema)
        await self._migrate_generations_inline()
        await self._conn.commit()
        # 独立只读连接(读不抢写锁、不进写事务)
        self._read_conn = await aiosqlite.connect(dsn, uri=self._uri)
        self._read_conn.row_factory = aiosqlite.Row
        await self._read_conn.execute("PRAGMA busy_timeout=5000")
        log.info("数据库已连接", 路径=str(self._path), WAL模式=self._wal)

    async def _migrate_generations_inline(self) -> None:
        """老库无 inline_message_id 列时幂等补列(Guest 媒体回填所需)。"""
        async with self.conn.execute("PRAGMA table_info(generations)") as cur:
            cols = {row[1] for row in await cur.fetchall()}
        if "inline_message_id" not in cols:
            await self.conn.execute(
                "ALTER TABLE generations ADD COLUMN inline_message_id TEXT")
            log.info("已迁移:generations 增加 inline_message_id 列")

    async def close(self) -> None:
        if self._read_conn is not None:
            await self._read_conn.close()
            self._read_conn = None
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
            log.info("数据库连接已关闭", 路径=str(self._path))

    # ── 查询助手 ───────────────────────────────────────────────
    async def fetch_one(self, sql: str, params: tuple = ()) -> aiosqlite.Row | None:
        async with self.read_conn.execute(sql, params) as cur:
            return await cur.fetchone()

    async def fetch_all(self, sql: str, params: tuple = ()) -> list[aiosqlite.Row]:
        async with self.read_conn.execute(sql, params) as cur:
            return list(await cur.fetchall())

    async def execute(self, sql: str, params: tuple = ()) -> int:
        """普通写入(自动提交),返回 lastrowid。"""
        async with self._write_lock:
            cur = await self.conn.execute(sql, params)
            await self.conn.commit()
            return cur.lastrowid or 0

    async def execute_many(self, statements: list[tuple[str, tuple]]) -> None:
        """同一事务内执行多条写入。"""
        async with self._write_lock:
            for sql, params in statements:
                await self.conn.execute(sql, params)
            await self.conn.commit()

    def immediate(self) -> _ImmediateTx:
        """BEGIN IMMEDIATE 写事务上下文(读-改-写防竞态,如配额结算)。"""
        return _ImmediateTx(self)


class _ImmediateTx:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def __aenter__(self) -> aiosqlite.Connection:
        await self._db._write_lock.acquire()
        await self._db.conn.execute("BEGIN IMMEDIATE")
        return self._db.conn

    async def __aexit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                await self._db.conn.commit()
            else:
                await self._db.conn.rollback()
        finally:
            self._db._write_lock.release()
