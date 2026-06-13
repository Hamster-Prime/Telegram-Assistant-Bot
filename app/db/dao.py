"""数据访问层(DAO)—— 所有 SQL 集中在此,带中文日志。"""
from __future__ import annotations

import time
from typing import Any

from app.db.engine import Database
from app.db.models import ChatRow, Generation, Memory, MessageRow, Quota, User
from app.logging import get_logger

log = get_logger("db.dao")


def _now() -> int:
    return int(time.time())


class UserDAO:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def get(self, tg_id: int) -> User | None:
        row = await self.db.fetch_one("SELECT * FROM users WHERE tg_id=?", (tg_id,))
        return User(**dict(row)) if row else None

    async def upsert_basic(self, tg_id: int, username: str | None, first_name: str | None) -> User:
        """更新用户名等基础信息;不存在则创建(默认未授权)。"""
        now = _now()
        await self.db.execute(
            """INSERT INTO users (tg_id, username, first_name, created_at, updated_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(tg_id) DO UPDATE SET
                 username=excluded.username, first_name=excluded.first_name, updated_at=excluded.updated_at""",
            (tg_id, username, first_name, now, now),
        )
        user = await self.get(tg_id)
        assert user is not None
        return user

    async def set_authorized(self, tg_id: int, authorized: bool, by: int | None) -> None:
        now = _now()
        await self.db.execute(
            """UPDATE users SET authorized=?, authorized_by=?, authorized_at=?, updated_at=? WHERE tg_id=?""",
            (1 if authorized else 0, by, now if authorized else None, now, tg_id),
        )
        log.info("用户授权状态变更", 用户=tg_id, 授权=authorized, 操作人=by)

    async def set_role(self, tg_id: int, role: str) -> None:
        await self.db.execute(
            "UPDATE users SET role=?, updated_at=? WHERE tg_id=?", (role, _now(), tg_id)
        )
        log.info("用户角色变更", 用户=tg_id, 新角色=role)

    async def ensure_superadmin(self, tg_id: int) -> None:
        """启动时强制超管:role=superadmin, authorized=1。"""
        now = _now()
        await self.db.execute(
            """INSERT INTO users (tg_id, role, authorized, created_at, updated_at)
               VALUES (?, 'superadmin', 1, ?, ?)
               ON CONFLICT(tg_id) DO UPDATE SET role='superadmin', authorized=1, updated_at=excluded.updated_at""",
            (tg_id, now, now),
        )

    async def list_users(self, offset: int = 0, limit: int = 20) -> list[User]:
        rows = await self.db.fetch_all(
            "SELECT * FROM users ORDER BY created_at DESC LIMIT ? OFFSET ?", (limit, offset)
        )
        return [User(**dict(r)) for r in rows]

    async def list_authorized_ids(self) -> list[int]:
        rows = await self.db.fetch_all(
            "SELECT tg_id FROM users WHERE authorized=1 OR role IN ('admin','superadmin')"
        )
        return [r["tg_id"] for r in rows]

    async def count(self) -> int:
        row = await self.db.fetch_one("SELECT COUNT(*) AS c FROM users")
        return row["c"] if row else 0


class QuotaDAO:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def get(self, user_id: int, mode: str) -> Quota | None:
        row = await self.db.fetch_one(
            "SELECT * FROM quotas WHERE user_id=? AND mode=?", (user_id, mode)
        )
        return Quota(**dict(row)) if row else None

    async def get_all(self, user_id: int) -> list[Quota]:
        rows = await self.db.fetch_all("SELECT * FROM quotas WHERE user_id=?", (user_id,))
        return [Quota(**dict(r)) for r in rows]

    async def set(self, user_id: int, mode: str, limit_val: int, period: str) -> None:
        now = _now()
        await self.db.execute(
            """INSERT INTO quotas (user_id, mode, period, limit_val, used, window_start, updated_at)
               VALUES (?,?,?,?,0,?,?)
               ON CONFLICT(user_id, mode) DO UPDATE SET
                 limit_val=excluded.limit_val, period=excluded.period, updated_at=excluded.updated_at""",
            (user_id, mode, period, limit_val, now, now),
        )
        log.info("配额已设置", 用户=user_id, 模式=mode, 上限=limit_val, 周期=period)

    async def reset_used(self, user_id: int, mode: str | None = None) -> None:
        now = _now()
        if mode:
            await self.db.execute(
                "UPDATE quotas SET used=0, window_start=?, updated_at=? WHERE user_id=? AND mode=?",
                (now, now, user_id, mode),
            )
        else:
            await self.db.execute(
                "UPDATE quotas SET used=0, window_start=?, updated_at=? WHERE user_id=?",
                (now, now, user_id),
            )
        log.info("配额用量已清零", 用户=user_id, 模式=mode or "全部")

    async def list_all(self, offset: int = 0, limit: int = 20) -> list[Quota]:
        rows = await self.db.fetch_all(
            "SELECT * FROM quotas ORDER BY user_id LIMIT ? OFFSET ?", (limit, offset)
        )
        return [Quota(**dict(r)) for r in rows]


class UsageDAO:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def add(self, user_id: int, chat_id: int, kind: str, calls: int = 1, tokens: int = 0) -> None:
        await self.db.execute(
            "INSERT INTO usage_log (user_id, chat_id, kind, calls, tokens, created_at) VALUES (?,?,?,?,?,?)",
            (user_id, chat_id, kind, calls, tokens, _now()),
        )

    async def stats(self, since: int = 0) -> list[dict[str, Any]]:
        rows = await self.db.fetch_all(
            """SELECT kind, COUNT(*) AS 次数, SUM(calls) AS 调用量, SUM(tokens) AS Token量
               FROM usage_log WHERE created_at>=? GROUP BY kind ORDER BY 次数 DESC""",
            (since,),
        )
        return [dict(r) for r in rows]


class ChatDAO:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def ensure(self, chat_id: int, type_: str, title: str | None, default_budget: int) -> ChatRow:
        row = await self.db.fetch_one("SELECT * FROM chats WHERE chat_id=?", (chat_id,))
        if row:
            return ChatRow(**dict(row))
        await self.db.execute(
            "INSERT OR IGNORE INTO chats (chat_id, type, title, token_budget, created_at) VALUES (?,?,?,?,?)",
            (chat_id, type_, title, default_budget, _now()),
        )
        return ChatRow(chat_id=chat_id, type=type_, title=title, token_budget=default_budget)


class MessageDAO:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def add(self, chat_id: int, user_id: int | None, role: str, content: str,
                  content_type: str = "text", tokens: int = 0) -> int:
        return await self.db.execute(
            """INSERT INTO messages (chat_id, user_id, role, content, content_type, tokens, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (chat_id, user_id, role, content, content_type, tokens, _now()),
        )

    async def recent_uncompacted(self, chat_id: int, limit: int = 50) -> list[MessageRow]:
        rows = await self.db.fetch_all(
            """SELECT * FROM messages WHERE chat_id=? AND compacted=0 ORDER BY id DESC LIMIT ?""",
            (chat_id, limit),
        )
        return [MessageRow(**dict(r)) for r in reversed(rows)]

    async def mark_compacted(self, chat_id: int, up_to_id: int) -> None:
        await self.db.execute(
            "UPDATE messages SET compacted=1 WHERE chat_id=? AND id<=?", (chat_id, up_to_id)
        )

    async def clear_chat(self, chat_id: int) -> None:
        await self.db.execute_many([
            ("DELETE FROM messages WHERE chat_id=?", (chat_id,)),
            ("DELETE FROM summaries WHERE chat_id=?", (chat_id,)),
        ])
        log.info("会话上下文已清空", 会话=chat_id)


class SummaryDAO:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def latest(self, chat_id: int) -> dict[str, Any] | None:
        row = await self.db.fetch_one(
            "SELECT * FROM summaries WHERE chat_id=? ORDER BY id DESC LIMIT 1", (chat_id,)
        )
        return dict(row) if row else None

    async def add(self, chat_id: int, summary: str, covers_up_to_id: int, tokens: int) -> None:
        await self.db.execute(
            "INSERT INTO summaries (chat_id, summary, covers_up_to_id, tokens, created_at) VALUES (?,?,?,?,?)",
            (chat_id, summary, covers_up_to_id, tokens, _now()),
        )


class MemoryDAO:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def add(self, scope: str, owner_id: int, text: str, source: str = "manual",
                  weight: float = 1.0) -> int:
        # 简单去重:同 scope+owner 下完全相同文本不重复入库
        row = await self.db.fetch_one(
            "SELECT id FROM memories WHERE scope=? AND owner_id=? AND text=?",
            (scope, owner_id, text),
        )
        if row:
            return row["id"]
        rid = await self.db.execute(
            "INSERT INTO memories (scope, owner_id, text, source, weight, created_at) VALUES (?,?,?,?,?,?)",
            (scope, owner_id, text, source, weight, _now()),
        )
        log.info("持久记忆已写入", 范围=scope, 归属=owner_id, 来源=source, 编号=rid)
        return rid

    async def search(self, scope: str, owner_id: int, query: str, top_k: int = 5) -> list[Memory]:
        """FTS5 BM25 检索;query 为空则按时间取最近。"""
        if query.strip():
            # FTS5 查询词需转义引号,按 OR 连接各词提高召回
            terms = [t.replace('"', '""') for t in query.split() if t.strip()]
            match = " OR ".join(f'"{t}"' for t in terms) if terms else '""'
            try:
                rows = await self.db.fetch_all(
                    """SELECT m.* FROM memories_fts f
                       JOIN memories m ON m.id=f.rowid
                       WHERE memories_fts MATCH ? AND m.scope=? AND m.owner_id=?
                       ORDER BY bm25(memories_fts) LIMIT ?""",
                    (match, scope, owner_id, top_k),
                )
            except Exception:
                rows = []
            if rows:
                ids = [r["id"] for r in rows]
                await self.db.execute(
                    f"UPDATE memories SET last_used_at=? WHERE id IN ({','.join('?'*len(ids))})",
                    (_now(), *ids),
                )
                return [Memory(**dict(r)) for r in rows]
        rows = await self.db.fetch_all(
            "SELECT * FROM memories WHERE scope=? AND owner_id=? ORDER BY created_at DESC LIMIT ?",
            (scope, owner_id, top_k),
        )
        return [Memory(**dict(r)) for r in rows]

    async def list_all(self, scope: str, owner_id: int, limit: int = 50) -> list[Memory]:
        rows = await self.db.fetch_all(
            "SELECT * FROM memories WHERE scope=? AND owner_id=? ORDER BY id DESC LIMIT ?",
            (scope, owner_id, limit),
        )
        return [Memory(**dict(r)) for r in rows]

    async def delete(self, mem_id: int, scope: str, owner_id: int) -> bool:
        row = await self.db.fetch_one(
            "SELECT id FROM memories WHERE id=? AND scope=? AND owner_id=?",
            (mem_id, scope, owner_id),
        )
        if not row:
            return False
        await self.db.execute("DELETE FROM memories WHERE id=?", (mem_id,))
        log.info("持久记忆已删除", 编号=mem_id, 范围=scope, 归属=owner_id)
        return True


class GenerationDAO:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(self, gen: Generation) -> int:
        rid = await self.db.execute(
            """INSERT INTO generations
               (user_id, chat_id, kind, model, prompt, status, task_id,
                placeholder_msg_id, inline_message_id, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (gen.user_id, gen.chat_id, gen.kind, gen.model, gen.prompt,
             gen.status, gen.task_id, gen.placeholder_msg_id,
             gen.inline_message_id, _now()),
        )
        log.info("生成任务已落库", 编号=rid, 类型=gen.kind, 模型=gen.model,
                 用户=gen.user_id, 任务ID=gen.task_id, 状态=gen.status)
        return rid

    async def get(self, gen_id: int) -> Generation | None:
        row = await self.db.fetch_one("SELECT * FROM generations WHERE id=?", (gen_id,))
        return Generation(**dict(row)) if row else None

    async def get_by_task(self, task_id: str) -> Generation | None:
        row = await self.db.fetch_one("SELECT * FROM generations WHERE task_id=?", (task_id,))
        return Generation(**dict(row)) if row else None

    async def update_status(self, gen_id: int, status: str, *, task_id: str | None = None,
                            file_id: str | None = None, result_url: str | None = None,
                            error: str | None = None, finished: bool = False) -> None:
        sets = ["status=?"]
        params: list[Any] = [status]
        if task_id is not None:
            sets.append("task_id=?"); params.append(task_id)
        if file_id is not None:
            sets.append("file_id=?"); params.append(file_id)
        if result_url is not None:
            sets.append("result_url=?"); params.append(result_url)
        if error is not None:
            sets.append("error=?"); params.append(error)
        if finished:
            sets.append("finished_at=?"); params.append(_now())
        params.append(gen_id)
        await self.db.execute(f"UPDATE generations SET {', '.join(sets)} WHERE id=?", tuple(params))
        log.info("生成任务状态更新", 编号=gen_id, 新状态=status, 错误=error or "无")

    async def pending(self) -> list[Generation]:
        """重启恢复:取所有未决任务。"""
        rows = await self.db.fetch_all(
            "SELECT * FROM generations WHERE status IN ('queued','processing')"
        )
        return [Generation(**dict(r)) for r in rows]


class AuditDAO:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def add(self, actor_id: int, action: str, target_id: int | None = None,
                  detail: str = "") -> None:
        await self.db.execute(
            "INSERT INTO audit_log (actor_id, action, target_id, detail, created_at) VALUES (?,?,?,?,?)",
            (actor_id, action, target_id, detail, _now()),
        )
        log.info("审计日志", 操作人=actor_id, 动作=action, 对象=target_id, 详情=detail)

    async def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
        )
        return [dict(r) for r in rows]


class DAOBundle:
    """聚合所有 DAO,挂在 dispatcher workflow_data 里传递。"""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.users = UserDAO(db)
        self.quotas = QuotaDAO(db)
        self.usage = UsageDAO(db)
        self.chats = ChatDAO(db)
        self.messages = MessageDAO(db)
        self.summaries = SummaryDAO(db)
        self.memories = MemoryDAO(db)
        self.generations = GenerationDAO(db)
        self.audit = AuditDAO(db)
