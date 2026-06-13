"""配额管理 —— calls / tokens 双模式 + 并发安全结算 + 惰性周期重置(plan §14.2)。"""
from __future__ import annotations

import calendar
import time
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import aiosqlite

from app.config import Settings
from app.db.dao import DAOBundle
from app.db.models import Quota, User
from app.logging import get_logger

log = get_logger("core.quota")

_TZ = ZoneInfo("Asia/Shanghai")


@dataclass(slots=True)
class QuotaCheck:
    ok: bool
    mode: str = ""
    used: int = 0
    limit: int = 0
    period: str = ""
    reset_at: str = ""

    def denial_text(self) -> str:
        return (
            f"📊 配额不足:{self.mode} 已用 {self.used}/{self.limit}"
            f"({self.period}),将于 {self.reset_at} 重置"
        )


def _next_reset_ts(period: str, window_start: int) -> int | None:
    """计算下次重置的时间戳(基于窗口起点 window_start)。

    - day:+1 天(固定 86400 秒)
    - month:+1 自然月(按日历月天数,处理月末截断与闰年,杜绝 30 天漂移)
    - total:None(永不重置)
    """
    if period == "day":
        return window_start + 86400
    if period == "month":
        dt = datetime.fromtimestamp(window_start, _TZ)
        new_year = dt.year + (1 if dt.month == 12 else 0)
        new_month = 1 if dt.month == 12 else dt.month + 1
        last_day = calendar.monthrange(new_year, new_month)[1]
        new_day = min(dt.day, last_day)
        return int(dt.replace(year=new_year, month=new_month, day=new_day).timestamp())
    return None


def _window_expired(q: Quota, now_ts: int) -> bool:
    if q.window_start is None:
        return False
    nxt = _next_reset_ts(q.period, q.window_start)
    return nxt is not None and now_ts >= nxt


def _reset_at_text(q: Quota) -> str:
    start = q.window_start or int(time.time())
    nxt = _next_reset_ts(q.period, start)
    if nxt is None:
        return "永不(total 模式)"
    return datetime.fromtimestamp(nxt, _TZ).strftime("%Y-%m-%d %H:%M")


class QuotaManager:
    def __init__(self, daos: DAOBundle, settings: Settings) -> None:
        self._daos = daos
        self._settings = settings
        # settle 事务内临时登记当前连接与用户(写锁已串行化,实例状态安全)
        self._tx_conn: aiosqlite.Connection | None = None
        self._tx_user_id: int = 0

    async def ensure_default(self, user_id: int) -> None:
        """新授权用户套用默认配额(已有配额则不动)。"""
        existing = await self._daos.quotas.get(user_id, self._settings.default_quota_mode)
        if existing is None:
            await self._daos.quotas.set(
                user_id,
                self._settings.default_quota_mode,
                self._settings.default_quota_limit,
                self._settings.default_quota_period,
            )
            log.info("已套用默认配额", 用户=user_id,
                     模式=self._settings.default_quota_mode,
                     上限=self._settings.default_quota_limit,
                     周期=self._settings.default_quota_period)

    async def precheck(self, user: User, mode: str, estimated: int = 1) -> QuotaCheck:
        """预检:估算本次开销,余量不足则拒绝。超管不限。"""
        if user.is_superadmin:
            return QuotaCheck(ok=True)
        q = await self._daos.quotas.get(user.tg_id, mode)
        if q is None:
            return QuotaCheck(ok=True)  # 未设配额 = 不限
        if q.unlimited:
            return QuotaCheck(ok=True)

        now_ts = int(time.time())
        if _window_expired(q, now_ts):
            # 惰性重置:读取时发现窗口已过,先归零
            await self._daos.quotas.reset_used(user.tg_id, mode)
            log.info("配额窗口已过期,惰性重置", 用户=user.tg_id, 模式=mode,
                     周期=q.period)
            q = await self._daos.quotas.get(user.tg_id, mode) or q

        if q.used + estimated > q.limit_val:
            check = QuotaCheck(ok=False, mode=mode, used=q.used, limit=q.limit_val,
                               period=q.period, reset_at=_reset_at_text(q))
            log.warning("配额预检拒绝", 用户=user.tg_id, 模式=mode,
                        已用=q.used, 上限=q.limit_val, 本次估算=estimated,
                        周期=q.period)
            return check
        log.debug("配额预检通过", 用户=user.tg_id, 模式=mode,
                  已用=q.used, 上限=q.limit_val, 本次估算=estimated)
        return QuotaCheck(ok=True, mode=mode, used=q.used, limit=q.limit_val,
                          period=q.period)

    async def settle(self, user: User, mode: str, amount: int,
                     *, chat_id: int = 0, kind: str = "chat") -> None:
        """结算真实用量。配额 UPDATE 与 usage_log INSERT 在**同一** BEGIN IMMEDIATE
        事务内(防并发竞态 + 原子性:流水写失败则配额一并回滚)。"""
        if amount <= 0:
            return
        now_ts = int(time.time())
        async with self._daos.db.immediate() as conn:
            # 登记当前事务连接,供 _usage_insert_sql 使用
            self._tx_conn = conn
            self._tx_user_id = user.tg_id
            try:
                if not user.is_superadmin:
                    cur = await conn.execute(
                        "SELECT * FROM quotas WHERE user_id=? AND mode=?",
                        (user.tg_id, mode),
                    )
                    row = await cur.fetchone()
                    if row is not None:
                        q = Quota(**dict(row))
                        if _window_expired(q, now_ts):
                            await conn.execute(
                                "UPDATE quotas SET used=?, window_start=?, updated_at=? "
                                "WHERE user_id=? AND mode=?",
                                (amount, now_ts, now_ts, user.tg_id, mode),
                            )
                        else:
                            await conn.execute(
                                "UPDATE quotas SET used=used+?, updated_at=? "
                                "WHERE user_id=? AND mode=?",
                                (amount, now_ts, user.tg_id, mode),
                            )
                # usage 流水写进同一事务(与配额 UPDATE 原子)
                await self._usage_insert_sql(mode, amount, chat_id, kind)
            finally:
                self._tx_conn = None
                self._tx_user_id = 0
        log.info("配额已结算", 用户=user.tg_id, 模式=mode, 本次用量=amount,
                 类型=kind, 会话=chat_id)

    async def _usage_insert_sql(self, mode: str, amount: int,
                                chat_id: int, kind: str) -> None:
        """写入 usage_log 流水。

        优先用 settle 事务内登记的连接(保证与配额 UPDATE 原子);
        若不在事务上下文(防御性),退化为独立写入。拆为独立方法便于测试注入失败。
        """
        if self._tx_conn is not None:
            await self._tx_conn.execute(
                "INSERT INTO usage_log (user_id, chat_id, kind, calls, tokens, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (self._tx_user_id, chat_id, kind,
                 amount if mode == "calls" else 1,
                 amount if mode == "tokens" else 0,
                 int(time.time())),
            )
        else:
            await self._daos.usage.add(
                self._tx_user_id, chat_id, kind,
                calls=amount if mode == "calls" else 1,
                tokens=amount if mode == "tokens" else 0,
            )

    def call_weight(self, kind: str) -> int:
        """生成类调用权重(视频=5、音乐=5、图片=1、搜索=1…)。"""
        return self._settings.call_weights.get(kind, 1)

    async def status_text(self, user: User) -> str:
        """渲染用户配额状态(/quota /whoami 用)。"""
        quotas = await self._daos.quotas.get_all(user.tg_id)
        if user.is_superadmin:
            return "配额:无限(超级管理员)"
        if not quotas:
            return "配额:未设置(不限)"
        lines = []
        now_ts = int(time.time())
        for q in quotas:
            if _window_expired(q, now_ts):
                used = 0
            else:
                used = q.used
            limit_txt = "无限" if q.unlimited else str(q.limit_val)
            lines.append(
                f"· {q.mode}:{used}/{limit_txt}({q.period},重置于 {_reset_at_text(q)})"
            )
        return "配额:\n" + "\n".join(lines)
