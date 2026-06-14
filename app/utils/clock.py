"""实时时间 —— Asia/Shanghai,统一经 now() 出口便于测试注入。"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

DEFAULT_TZ = "Asia/Shanghai"

_WEEKDAY_ZH = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def now(tz: str = DEFAULT_TZ) -> datetime:
    return datetime.now(ZoneInfo(tz))


def format_now(tz: str = DEFAULT_TZ) -> str:
    """格式:2026-06-13 14:30:00(周六, Asia/Shanghai, UTC+8)"""
    dt = now(tz)
    offset = dt.utcoffset()
    hours = int(offset.total_seconds() // 3600) if offset else 0
    sign = "+" if hours >= 0 else "-"
    return (
        f"{dt.strftime('%Y-%m-%d %H:%M:%S')}"
        f"({_WEEKDAY_ZH[dt.weekday()]}, {tz}, UTC{sign}{abs(hours)})"
    )


def format_timestamp(ts: int, tz: str = DEFAULT_TZ) -> str:
    """Unix 秒 → '2026-06-13 14:30:00(周六)'。

    用于历史消息元数据头部(秒级精度,与数据库 created_at 一致)。
    """
    from datetime import datetime
    dt = datetime.fromtimestamp(ts, ZoneInfo(tz))
    return f"{dt.strftime('%Y-%m-%d %H:%M:%S')}({_WEEKDAY_ZH[dt.weekday()]})"
