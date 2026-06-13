"""配额管理 —— calls / tokens 双模式 + 并发安全结算 + 惰性周期重置(plan §14.2)。"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

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


def _period_seconds(period: str) -> int | None:
    if period == "day":
        return 86400
    if period == "month":
        return 86400 * 30
    return None  # total:永不重置


def _window_expired(q: Quota, now_ts: int) -> bool:
    secs = _period_seconds(q.period)
    if secs is None or q.window_start is None:
        return False
    return now_ts >= q.window_start + secs


def _reset_at_text(q: Quota) -> str:
    secs = _period_seconds(q.period)
    if secs is None:
        return "永不(total 模式)"
    start = q.window_start or int(time.time())
    dt = datetime.fromtimestamp(start + secs, _TZ)
    return dt.strftime("%Y-%m-%d %H:%M")


class QuotaManager:
    def __init__(self, daos: DAOBundle, settings: Settings) -> None:
        self._daos = daos
        self._settings = settings

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
        """结算真实用量。BEGIN IMMEDIATE 防并发竞态;附 usage_log 流水。"""
        if amount <= 0:
            return
        if not user.is_superadmin:
            now_ts = int(time.time())
            async with self._daos.db.immediate() as conn:
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
        await self._daos.usage.add(
            user.tg_id, chat_id, kind,
            calls=amount if mode == "calls" else 1,
            tokens=amount if mode == "tokens" else 0,
        )
        log.info("配额已结算", 用户=user.tg_id, 模式=mode, 本次用量=amount,
                 类型=kind, 会话=chat_id)

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
