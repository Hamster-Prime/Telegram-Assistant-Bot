"""列表分页渲染 + 内联键盘构造(plan §斜杠命令输出优化)。

集中定义:
- CallbackData 类型(列表翻页 / 视图导航 / 关闭)
- 各列表「取数 + HTML 渲染」函数,返回 (HTML 文本, 键盘)
- 信息视图(/help /whoami /quota)的 HTML 渲染 + 导航键盘

被 commands.py(首次发送)与 callbacks.py(翻页/导航回调)共用,
确保命令入口与回调入口渲染完全一致。所有动态文本经 html.escape 转义。
"""

from __future__ import annotations

import html
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.core.quota import _reset_at_text, _window_expired
from app.db.models import User
from app.minimax.client import mask_key
from app.minimax.quota import QuotaRemain
from app.services import Services

_TZ = ZoneInfo("Asia/Shanghai")

# 各列表每页条数:记忆/审计文本长用 5,用户/配额每行短适当加大
PAGE_SIZE: dict[str, int] = {
    "memories": 5,
    "audit": 5,
    "users": 10,
    "quotas": 10,
    "stats": 5,
}

# 信息视图导航按钮文案
NAV_LABELS: dict[str, str] = {
    "help": "📖 帮助",
    "whoami": "🪪 我的信息",
    "quota": "📊 我的配额",
}


# ── CallbackData ────────────────────────────────────────────────
class ListPageCB(CallbackData, prefix="lp"):
    """列表翻页。scope/owner 仅 memories 用;uid 为发起人,用于访问控制。"""

    kind: str  # memories|quotas|users|audit|stats
    page: int
    scope: str = ""
    owner: int = 0
    uid: int = 0


class NavCB(CallbackData, prefix="nv"):
    """信息视图导航。"""

    view: str  # help|whoami|quota
    scope: str = ""
    owner: int = 0
    uid: int = 0


class CloseCB(CallbackData, prefix="cls"):
    """关闭(删除消息)。uid 用于访问控制:仅发起人可关闭。"""

    uid: int = 0


class MmxQuotaCB(CallbackData, prefix="mmxq"):
    """Token Plan 配额查询。key_idx 为 settings.minimax_keys 的下标,
    uid 为发起人(管理员),用于访问控制。"""

    key_idx: int
    uid: int = 0


# ── 工具 ────────────────────────────────────────────────────────
def _esc(text: object) -> str:
    """HTML 转义动态文本(属性 quote=False 即可,按钮文案同理)。"""
    return html.escape(str(text), quote=False)


def _total_pages(total: int, size: int) -> int:
    if total <= 0:
        return 0
    return (total + size - 1) // size


def _close_btn(uid: int) -> InlineKeyboardButton:
    return InlineKeyboardButton(text="✖ 关闭", callback_data=CloseCB(uid=uid).pack())


def _mem_btn(scope: str, owner: int, uid: int) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text="🧠 记忆",
        callback_data=ListPageCB(kind="memories", page=1, scope=scope, owner=owner, uid=uid).pack(),
    )


def _nav_buttons(
    exclude: str,
    scope: str,
    owner: int,
    uid: int,
) -> list[InlineKeyboardButton]:
    """返回信息视图导航按钮(排除当前视图)。"""
    btns: list[InlineKeyboardButton] = []
    for view, label in NAV_LABELS.items():
        if view == exclude:
            continue
        btns.append(
            InlineKeyboardButton(
                text=label,
                callback_data=NavCB(view=view, scope=scope, owner=owner, uid=uid).pack(),
            )
        )
    return btns


# ── 键盘构造 ────────────────────────────────────────────────────
def info_kb(current: str, *, scope: str, owner: int, uid: int) -> InlineKeyboardMarkup:
    """信息视图(help/whoami/quota)键盘:其它视图 + 记忆 + 关闭。

    current ∈ {help, whoami, quota};记忆按钮始终存在。
    """
    row = _nav_buttons(current, scope, owner, uid)
    row.append(_mem_btn(scope, owner, uid))
    return InlineKeyboardMarkup(inline_keyboard=[row, [_close_btn(uid)]])


def pager_kb(
    kind: str, page: int, total: int, *, scope: str, owner: int, uid: int
) -> InlineKeyboardMarkup:
    """列表翻页键盘。

    - 分页行:首页无「上一页」,末页无「下一页」,单页时仅显示页码指示器
    - memories 额外追加信息视图导航行(可从记忆跳到帮助/信息/配额)
    - 始终末行:关闭
    """
    rows: list[list[InlineKeyboardButton]] = []
    pag: list[InlineKeyboardButton] = []
    if page > 1:
        pag.append(
            InlineKeyboardButton(
                text="◀ 上一页",
                callback_data=ListPageCB(
                    kind=kind, page=page - 1, scope=scope, owner=owner, uid=uid
                ).pack(),
            )
        )
    if total > 1 or page > 1:
        pag.append(InlineKeyboardButton(text=f"{page}/{total}", callback_data="noop"))
    if page < total:
        pag.append(
            InlineKeyboardButton(
                text="下一页 ▶",
                callback_data=ListPageCB(
                    kind=kind, page=page + 1, scope=scope, owner=owner, uid=uid
                ).pack(),
            )
        )
    if pag:
        rows.append(pag)
    # 记忆视图:追加信息导航行(可跳回 help/whoami/quota)
    if kind == "memories":
        nav = _nav_buttons("memories", scope, owner, uid)
        if nav:
            rows.append(nav)
    rows.append([_close_btn(uid)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── 列表渲染(取数 + HTML 文本 + 键盘) ──────────────────────────
async def render_memories(
    svc: Services,
    scope: str,
    owner: int,
    uid: int,
    page: int = 1,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """长期记忆列表(5/页)。空表返回提示文本 + 导航键盘。"""
    size = PAGE_SIZE["memories"]
    total = await svc.daos.memories.count(scope, owner)
    total_p = _total_pages(total, size)
    if total == 0:
        # 空:仍给导航键盘,便于跳到其它视图
        return "🧠 暂无长期记忆", info_kb("help", scope=scope, owner=owner, uid=uid)
    page = max(1, min(page, total_p))
    mems = await svc.daos.memories.list_all(scope, owner, limit=size, offset=(page - 1) * size)
    lines = [f"<b>{m.id}.</b> {_esc(m.text[:80])} <i>({_esc(m.source)})</i>" for m in mems]
    text = f"<b>🧠 长期记忆</b>  <i>第 {page}/{total_p} 页 · 共 {total} 条</i>\n\n" + "\n".join(
        lines
    )
    return text, pager_kb("memories", page, total_p, scope=scope, owner=owner, uid=uid)


async def render_quotas(
    svc: Services,
    uid: int,
    page: int = 1,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """配额列表(10/页,管理员)。"""
    size = PAGE_SIZE["quotas"]
    total = await svc.daos.quotas.count_all()
    total_p = _total_pages(total, size)
    if total == 0:
        return "📊 暂无配额记录", None
    page = max(1, min(page, total_p))
    quotas = await svc.daos.quotas.list_all(offset=(page - 1) * size, limit=size)
    lines = [
        f"<code>{_esc(q.user_id)}</code> · {q.mode} "
        f"<b>{q.used}</b>/{'∞' if q.unlimited else q.limit_val} ({_esc(q.period)})"
        for q in quotas
    ]
    text = f"<b>📊 配额列表</b>  <i>第 {page}/{total_p} 页 · 共 {total} 条</i>\n\n" + "\n".join(
        lines
    )
    return text, pager_kb("quotas", page, total_p, scope="", owner=0, uid=uid)


async def render_users(
    svc: Services,
    uid: int,
    page: int = 1,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """用户列表(10/页,管理员)。"""
    size = PAGE_SIZE["users"]
    total = await svc.daos.users.count()
    total_p = _total_pages(total, size)
    if total == 0:
        return "👥 暂无用户", None
    page = max(1, min(page, total_p))
    users = await svc.daos.users.list_users(offset=(page - 1) * size, limit=size)
    role_icon = {"superadmin": "👑", "admin": "🛡", "user": "👤"}
    lines = [
        f"{role_icon.get(u.role, '👤')} <code>{u.tg_id}</code> "
        f"@{_esc(u.username or '-')} {'✅' if u.is_allowed else '⛔'}"
        for u in users
    ]
    text = f"<b>👥 用户列表</b>  <i>第 {page}/{total_p} 页 · 共 {total} 人</i>\n\n" + "\n".join(
        lines
    )
    return text, pager_kb("users", page, total_p, scope="", owner=0, uid=uid)


async def render_audit(
    svc: Services,
    uid: int,
    page: int = 1,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """审计日志(5/页,超管)。"""
    size = PAGE_SIZE["audit"]
    total = await svc.daos.audit.count()
    total_p = _total_pages(total, size)
    if total == 0:
        return "📋 审计日志为空", None
    page = max(1, min(page, total_p))
    rows = await svc.daos.audit.recent(limit=size, offset=(page - 1) * size)
    lines = []
    for r in rows:
        ts = datetime.fromtimestamp(r["created_at"], _TZ).strftime("%m-%d %H:%M")
        target = r["target_id"] if r["target_id"] is not None else "-"
        detail = f" <i>{_esc(r['detail'])}</i>" if r["detail"] else ""
        lines.append(
            f"<code>{ts}</code> <code>{_esc(r['actor_id'])}</code> "
            f"<b>{_esc(r['action'])}</b> → <code>{_esc(target)}</code>{detail}"
        )
    text = f"<b>📋 审计日志</b>  <i>第 {page}/{total_p} 页 · 共 {total} 条</i>\n\n" + "\n".join(
        lines
    )
    return text, pager_kb("audit", page, total_p, scope="", owner=0, uid=uid)


async def render_stats(
    svc: Services,
    uid: int,
    page: int = 1,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """近24小时用量统计(5/页,管理员)。

    stats 是聚合查询结果集较小,取全量后客户端分页。
    """
    size = PAGE_SIZE["stats"]
    day_ago = int(time.time()) - 86400
    all_rows = await svc.daos.usage.stats(since=day_ago)
    total = len(all_rows)
    total_p = _total_pages(total, size)
    if total == 0:
        return "📈 近24小时无用量", None
    page = max(1, min(page, total_p))
    chunk = all_rows[(page - 1) * size : page * size]
    lines = [
        f"<b>{_esc(s['kind'])}</b> · {s['次数']} 次 · "
        f"calls={s['调用量'] or 0} · tokens={s['Token量'] or 0}"
        for s in chunk
    ]
    text = f"<b>📈 近24小时用量</b>  <i>第 {page}/{total_p} 页 · 共 {total} 类</i>\n\n" + "\n".join(
        lines
    )
    return text, pager_kb("stats", page, total_p, scope="", owner=0, uid=uid)


# ── 信息视图渲染 ────────────────────────────────────────────────
async def render_help(
    *,
    scope: str,
    owner: int,
    uid: int,
) -> tuple[str, InlineKeyboardMarkup]:
    from app.handlers.commands_registry import build_help_html

    return build_help_html(), info_kb("help", scope=scope, owner=owner, uid=uid)


async def render_whoami(
    svc: Services,
    user: User,
    *,
    scope: str,
    owner: int,
    uid: int,
) -> tuple[str, InlineKeyboardMarkup]:
    role_zh = {"superadmin": "超级管理员", "admin": "管理员", "user": "用户"}[user.role]
    quota_html = await render_quota_status_html(svc, user)
    text = (
        f"<b>🪪 我的信息</b>\n\n"
        f"<b>ID</b>  <code>{user.tg_id}</code>\n"
        f"<b>角色</b>  {_esc(role_zh)}\n"
        f"<b>授权</b>  {'✅ 已授权' if user.is_allowed else '⛔ 未授权'}\n\n"
        f"{quota_html}"
    )
    return text, info_kb("whoami", scope=scope, owner=owner, uid=uid)


async def render_quota_view(
    svc: Services,
    user: User,
    *,
    scope: str,
    owner: int,
    uid: int,
) -> tuple[str, InlineKeyboardMarkup]:
    quota_html = await render_quota_status_html(svc, user)
    text = f"<b>📊 我的配额</b>\n\n{quota_html}"
    return text, info_kb("quota", scope=scope, owner=owner, uid=uid)


# ── 配额状态 HTML 渲染(/quota /whoami /userinfo 共用) ────────────

_PERIOD_CN: dict[str, str] = {"day": "每日", "month": "每月", "total": "总计"}


def _fmt_count(n: int) -> str:
    """数字格式化:<1万原样,>=1万用万表示。"""
    if abs(n) >= 10_000:
        return f"{n / 10_000:.1f}万".replace(".0万", "万")
    return str(n)


async def render_quota_status_html(svc: Services, user: User) -> str:
    """渲染用户配额状态为 HTML 片段(含进度条,对齐 mmx-cli 风格)。

    - 进度条按「剩余」百分比填充(剩余多则条满)
    - 状态图标 🟢/🟡/🔴/♾️ 与 MiniMax 配额视图一致
    - period 用中文(每日/每月/总计)
    """
    quotas = await svc.daos.quotas.get_all(user.tg_id)
    if user.is_superadmin:
        return "<b>配额</b>  ♾️ 无限 (超级管理员)"
    if not quotas:
        return "<b>配额</b>  未设置 (不限)"
    now_ts = int(time.time())
    lines = []
    for q in quotas:
        used = 0 if _window_expired(q, now_ts) else q.used
        if q.unlimited:
            lines.append(f"♾️ <b>{_esc(q.mode)}</b>  无限")
            continue
        remaining_pct = max(0.0, (q.limit_val - used) / q.limit_val * 100.0) if q.limit_val > 0 else 0.0
        bar = _bar(remaining_pct)
        icon = _status_icon(remaining_pct, 1)
        period_cn = _PERIOD_CN.get(q.period, q.period)
        reset_txt = _reset_at_text(q)
        reset_short = reset_txt.split(" ", 1)[1] if " " in reset_txt else reset_txt
        lines.append(
            f"{icon} <b>{_esc(q.mode)}</b>  "
            f"<code>{bar}</code> "
            f"{_fmt_count(used)} / {_fmt_count(q.limit_val)} · "
            f"{_esc(period_cn)} · {_esc(reset_short)} 重置"
        )
    return "<b>配额</b>\n" + "\n".join(lines)


# ── MiniMax Token Plan 配额(/mmxquota) ─────────────────────────
def mmx_quota_kb(keys: list[str], uid: int) -> InlineKeyboardMarkup:
    """账号选择键盘:每个 Key 一行 + 关闭。key_idx 即按钮对应的下标。"""
    rows: list[list[InlineKeyboardButton]] = []
    for idx, k in enumerate(keys):
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"账号{idx + 1} · {mask_key(k)}",
                    callback_data=MmxQuotaCB(key_idx=idx, uid=uid).pack(),
                )
            ]
        )
    rows.append([_close_btn(uid)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def mmx_quota_result_kb(key_idx: int, keys_count: int, uid: int) -> InlineKeyboardMarkup:
    """查询结果键盘:刷新(重查同账号)/返回选择/关闭。

    单账号时不显示「返回选择」(无处可返)。返回选择用 key_idx=-1 哨兵
    (由 on_mmx_quota 识别为「显示账号选择键盘」)。
    """
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text="🔄 刷新",
                callback_data=MmxQuotaCB(key_idx=key_idx, uid=uid).pack(),
            )
        ]
    ]
    if keys_count > 1:
        rows[0].append(
            InlineKeyboardButton(
                text="↩ 选择账号",
                callback_data=MmxQuotaCB(key_idx=-1, uid=uid).pack(),
            )
        )
    rows.append([_close_btn(uid)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# 服务端 model_name → 中文显示名(对齐 mmx-cli MODEL_NAME_CN)
_MMX_MODEL_CN: dict[str, str] = {
    "general": "通用额度 · 文本/图像/语音/音乐",
    "video": "视频",
    "image": "图像",
    "music": "音乐",
    "speech": "语音",
}


def _model_cn(name: str) -> str:
    """model_name → 中文显示名;未知模型保留原名。"""
    return _MMX_MODEL_CN.get(name, name)


def _bar(remaining_pct: float, width: int = 10) -> str:
    """Unicode 进度条:按「剩余」百分比填充(对齐 mmx-cli,剩余多则条长)。

    pct>100(boost 后)→ 全满;0 → 全空。
    """
    ratio = max(0.0, min(1.0, remaining_pct / 100.0))
    filled = round(width * ratio)
    filled = max(0, min(width, filled))
    return "█" * filled + "░" * (width - filled)


def _status_icon(remaining_pct: float, status: int) -> str:
    """按剩余百分比 / 服务端状态返回颜色图标。"""
    if status == 3:  # 不限量
        return "♾️"
    if status == 2:  # 已耗尽
        return "🔴"
    if remaining_pct >= 50:
        return "🟢"
    if remaining_pct >= 20:
        return "🟡"
    return "🔴"


def _fmt_metric(remain: QuotaRemain, *, weekly: bool) -> str:
    """渲染某窗口的用量文本。

    - 服务端 status=3(不限量)→ "不限量"
    - total>0(如视频,按次计)→ "已用 usage/total"
    - total<=0(如 general,按百分比计)→ "剩余 pct%"
    """
    status = remain.weekly_status if weekly else remain.interval_status
    total = remain.weekly_total if weekly else remain.interval_total
    usage = remain.weekly_usage if weekly else remain.interval_usage
    if status == 3:  # 不限量
        return "不限量"
    if total > 0:
        return f"已用 {usage}/{total}"
    # 按百分比计:显示剩余百分比(周窗口含 boost)
    pct = remain.weekly_remaining_pct if weekly else remain.interval_remaining_pct
    if weekly and remain.weekly_boost_permille != 1000:
        pct = pct * (remain.weekly_boost_permille / 1000.0)
    return f"剩余 {round(pct)}%"


def _fmt_remain_ms(ms: int) -> str:
    """距窗口重置的剩余时间文本(服务端 remains_time 毫秒)。"""
    if ms <= 0:
        return "即将重置"
    secs = ms // 1000
    d = secs // 86400
    h = (secs % 86400) // 3600
    m = (secs % 3600) // 60
    if d > 0:
        return f"{d}天{h}时后"
    if h > 0:
        return f"{h}时{m}分后"
    if m > 0:
        return f"{m}分后"
    return f"{secs}秒后"


def _fmt_date(ts_s: float) -> str:
    """Unix 秒 → YYYY-MM-DD(周窗口范围用)。0 → '-'。"""
    if ts_s <= 0:
        return "-"
    return datetime.fromtimestamp(ts_s, _TZ).strftime("%Y-%m-%d")


def render_mmx_quota(key_idx: int, key: str, remains: list[QuotaRemain]) -> str:
    """渲染单个账号 Token Plan 配额为 HTML 文本(纯函数,便于测试)。

    对齐 mmx-cli renderQuotaTable 的语义:
    - 进度条按「剩余」百分比填充(不反转)
    - total>0 的资源显示「已用 usage/total」,total=0 的资源显示「剩余 pct%」
    - 周窗口显示百分比时乘以 weekly_boost_permille(如 1500‰ → 显示达 150%)
    - 重置倒计时用服务端 remains_time / weekly_remains_time
    """
    head = f"<b>📊 Token Plan 用量</b>  账号{key_idx + 1} · <code>{mask_key(key)}</code>"
    if not remains:
        return f"{head}\n\n<i>该账号暂无可用资源 (可能非 Token Plan 订阅)。</i>"

    # 周期范围(取首条的周窗口时间)
    w_start = _fmt_date(remains[0].weekly_start)
    w_end = _fmt_date(remains[0].weekly_end)
    range_line = f"\n\n📅 周期 {w_start} — {w_end}" if w_start != "-" else ""

    blocks: list[str] = []
    for r in remains:
        # 取两个窗口中更紧张的状态作为模型行整体图标
        worse_pct = min(r.interval_remaining_pct, r.weekly_remaining_pct)
        worse_status = (
            r.interval_status
            if r.interval_status == 2
            else r.weekly_status
            if r.weekly_status == 2
            else max(r.interval_status, r.weekly_status)
        )
        icon = _status_icon(worse_pct, worse_status if worse_status != 1 else 1)

        # 5h 窗口行
        i_bar = _bar(r.interval_remaining_pct)
        i_metric = _fmt_metric(r, weekly=False)
        i_remain = _fmt_remain_ms(r.interval_remains_ms)
        i_line = f"  5h窗口  <code>{i_bar}</code> {i_metric}  <i>重置 {i_remain}</i>"

        # 周窗口行(显示百分比含 boost;进度条按 boost 后的剩余%填充)
        w_boosted_pct = r.weekly_remaining_pct * (r.weekly_boost_permille / 1000.0)
        w_bar = _bar(w_boosted_pct)
        w_metric = _fmt_metric(r, weekly=True)
        w_remain = _fmt_remain_ms(r.weekly_remains_ms)
        w_line = f"  周窗口   <code>{w_bar}</code> {w_metric}  <i>重置 {w_remain}</i>"

        blocks.append(f"{icon} <b>{_esc(_model_cn(r.model_name))}</b>\n{i_line}\n{w_line}")
    return head + range_line + "\n\n" + "\n\n".join(blocks)
