"""MiniMax Token Plan 配额查询 —— /v1/token_plan/remains。

与 chat/tts/image 等模块不同:配额需按 Key 分别查询(每个 Key 对应一个
Token Plan 订阅),故不走多 Key fallback,而是用 client.get_with_key
指定单个 Key 发起请求。

字段语义(对齐服务端真实响应):
- remaining_percent:服务端直接给出「剩余」百分比(0-100),渲染时直接用作
  进度条填充率,不做 100-xxx 反转(与 mmx-cli renderQuotaTable 一致)。
- total_count:部分资源(如 general)是「按百分比计」,total=0 → 不显示
  用量计数,仅显示剩余百分比;视频等资源 total>0 → 显示 已用/总量。
- remains_time:窗口重置剩余毫秒(服务端直接返回),无需自己算 end-now。
- weekly_boost_permille:仅周窗口的显示倍率(‰),1500 ⇒ 显示可达 150%。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.logging import get_logger
from app.minimax.client import MiniMaxClient

log = get_logger("minimax.quota")

# 服务端状态码:1=正常(限量) 2=已耗尽 3=不限量
# 端点路径(minimax_base_url 已含 /v1)
_ENDPOINT = "/token_plan/remains"


@dataclass(slots=True)
class QuotaRemain:
    """单个资源在一个 Key(Token Plan)下的剩余额度。

    remaining_*_pct 字段为「剩余」百分比(服务端原值),用于进度条填充;
    remains_*_ms 为窗口重置剩余毫秒。时间戳为 Unix 秒(ms/1000)。
    """

    model_name: str
    # 5 小时窗口
    interval_total: int
    interval_usage: int
    interval_remaining_pct: float  # 剩余百分比(服务端原值,未反转)
    interval_status: int  # 1/2/3
    # 周窗口
    weekly_total: int
    weekly_usage: int
    weekly_remaining_pct: float  # 剩余百分比(周基础值,不含 boost)
    weekly_status: int
    # 时间(Unix 秒)
    interval_start: float
    interval_end: float
    weekly_start: float
    weekly_end: float
    # 重置剩余(毫秒,服务端 remains_time / weekly_remains_time)
    interval_remains_ms: int = 0
    weekly_remains_ms: int = 0
    weekly_boost_permille: int = 1000


def _ms_to_s(v: Any) -> float:
    """服务端返回毫秒,转秒;空值给 0.0。"""
    try:
        return float(v) / 1000.0
    except (TypeError, ValueError):
        return 0.0


def _to_int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _to_float(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _remaining_pct(pct_raw: float, total: int, usage: int) -> float:
    """解析「剩余」百分比:优先用服务端 remaining_percent,缺失则由
    total/usage 反推((total-usage)/total*100)。total<=0 时返回 pct_raw。
    """
    if pct_raw > 0:
        return max(0.0, min(100.0, pct_raw))
    if total > 0:
        return max(0.0, min(100.0, (total - usage) / total * 100.0))
    return pct_raw


def _parse_one(raw: dict[str, Any]) -> QuotaRemain:
    """从 model_remains 数组的一项解析为 QuotaRemain。

    remaining_percent 直接保留服务端原值(剩余),不做 100-xxx 反转。
    """
    i_total = _to_int(raw.get("current_interval_total_count"))
    i_usage = _to_int(raw.get("current_interval_usage_count"))
    i_pct = _remaining_pct(
        _to_float(raw.get("current_interval_remaining_percent")), i_total, i_usage
    )

    w_total = _to_int(raw.get("current_weekly_total_count"))
    w_usage = _to_int(raw.get("current_weekly_usage_count"))
    w_pct = _remaining_pct(_to_float(raw.get("current_weekly_remaining_percent")), w_total, w_usage)

    return QuotaRemain(
        model_name=str(raw.get("model_name", "?")),
        interval_total=i_total,
        interval_usage=i_usage,
        interval_remaining_pct=i_pct,
        interval_status=_to_int(raw.get("current_interval_status") or 1),
        weekly_total=w_total,
        weekly_usage=w_usage,
        weekly_remaining_pct=w_pct,
        weekly_status=_to_int(raw.get("current_weekly_status") or 1),
        interval_start=_ms_to_s(raw.get("start_time")),
        interval_end=_ms_to_s(raw.get("end_time")),
        weekly_start=_ms_to_s(raw.get("weekly_start_time")),
        weekly_end=_ms_to_s(raw.get("weekly_end_time")),
        interval_remains_ms=_to_int(raw.get("remains_time")),
        weekly_remains_ms=_to_int(raw.get("weekly_remains_time")),
        weekly_boost_permille=_to_int(raw.get("weekly_boost_permille") or 1000),
    )


class QuotaAPI:
    """Token Plan 配额查询。"""

    def __init__(self, client: MiniMaxClient) -> None:
        self._client = client

    async def remains(self, key: str) -> list[QuotaRemain]:
        """查询指定 Key 的 Token Plan 剩余额度。

        直接抛 MiniMaxError(鉴权失败/限流/HTTP 错误),由调用方捕获处理。
        """
        data = await self._client.get_with_key(key, _ENDPOINT)
        raw_list = data.get("model_remains") or []
        items = [_parse_one(r) for r in raw_list if isinstance(r, dict)]
        log.info("Token Plan 配额查询成功", 模型数=len(items))
        return items
