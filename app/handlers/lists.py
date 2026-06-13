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
    kind: str        # memories|quotas|users|audit|stats
    page: int
    scope: str = ""
    owner: int = 0
    uid: int = 0


class NavCB(CallbackData, prefix="nv"):
    """信息视图导航。"""
    view: str        # help|whoami|quota
    scope: str = ""
    owner: int = 0
    uid: int = 0


class CloseCB(CallbackData, prefix="cls"):
    """关闭(删除消息)。uid 用于访问控制:仅发起人可关闭。"""
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
        callback_data=ListPageCB(kind="memories", page=1, scope=scope,
                                 owner=owner, uid=uid).pack(),
    )


def _nav_buttons(exclude: str, scope: str, owner: int, uid: int,
                 ) -> list[InlineKeyboardButton]:
    """返回信息视图导航按钮(排除当前视图)。"""
    btns: list[InlineKeyboardButton] = []
    for view, label in NAV_LABELS.items():
        if view == exclude:
            continue
        btns.append(InlineKeyboardButton(
            text=label,
            callback_data=NavCB(view=view, scope=scope, owner=owner, uid=uid).pack(),
        ))
    return btns


# ── 键盘构造 ────────────────────────────────────────────────────
def info_kb(current: str, *, scope: str, owner: int,
            uid: int) -> InlineKeyboardMarkup:
    """信息视图(help/whoami/quota)键盘:其它视图 + 记忆 + 关闭。

    current ∈ {help, whoami, quota};记忆按钮始终存在。
    """
    row = _nav_buttons(current, scope, owner, uid)
    row.append(_mem_btn(scope, owner, uid))
    return InlineKeyboardMarkup(inline_keyboard=[row, [_close_btn(uid)]])


def pager_kb(kind: str, page: int, total: int, *,
             scope: str, owner: int, uid: int) -> InlineKeyboardMarkup:
    """列表翻页键盘。

    - 分页行:首页无「上一页」,末页无「下一页」,单页时仅显示页码指示器
    - memories 额外追加信息视图导航行(可从记忆跳到帮助/信息/配额)
    - 始终末行:关闭
    """
    rows: list[list[InlineKeyboardButton]] = []
    pag: list[InlineKeyboardButton] = []
    if page > 1:
        pag.append(InlineKeyboardButton(
            text="◀ 上一页",
            callback_data=ListPageCB(kind=kind, page=page - 1, scope=scope,
                                     owner=owner, uid=uid).pack(),
        ))
    if total > 1 or page > 1:
        pag.append(InlineKeyboardButton(
            text=f"{page}/{total}", callback_data="noop"))
    if page < total:
        pag.append(InlineKeyboardButton(
            text="下一页 ▶",
            callback_data=ListPageCB(kind=kind, page=page + 1, scope=scope,
                                     owner=owner, uid=uid).pack(),
        ))
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
    svc: Services, scope: str, owner: int, uid: int, page: int = 1,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """长期记忆列表(5/页)。空表返回提示文本 + 导航键盘。"""
    size = PAGE_SIZE["memories"]
    total = await svc.daos.memories.count(scope, owner)
    total_p = _total_pages(total, size)
    if total == 0:
        # 空:仍给导航键盘,便于跳到其它视图
        return "🧠 暂无长期记忆", info_kb("help", scope=scope, owner=owner, uid=uid)
    page = max(1, min(page, total_p))
    mems = await svc.daos.memories.list_all(scope, owner, limit=size,
                                            offset=(page - 1) * size)
    lines = [
        f"<b>{m.id}.</b> {_esc(m.text[:80])} <i>({_esc(m.source)})</i>"
        for m in mems
    ]
    text = (f"<b>🧠 长期记忆</b>  <i>第 {page}/{total_p} 页 · 共 {total} 条</i>\n\n"
            + "\n".join(lines))
    return text, pager_kb("memories", page, total_p, scope=scope, owner=owner, uid=uid)


async def render_quotas(
    svc: Services, uid: int, page: int = 1,
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
    text = (f"<b>📊 配额列表</b>  <i>第 {page}/{total_p} 页 · 共 {total} 条</i>\n\n"
            + "\n".join(lines))
    return text, pager_kb("quotas", page, total_p, scope="", owner=0, uid=uid)


async def render_users(
    svc: Services, uid: int, page: int = 1,
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
    text = (f"<b>👥 用户列表</b>  <i>第 {page}/{total_p} 页 · 共 {total} 人</i>\n\n"
            + "\n".join(lines))
    return text, pager_kb("users", page, total_p, scope="", owner=0, uid=uid)


async def render_audit(
    svc: Services, uid: int, page: int = 1,
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
    text = (f"<b>📋 审计日志</b>  <i>第 {page}/{total_p} 页 · 共 {total} 条</i>\n\n"
            + "\n".join(lines))
    return text, pager_kb("audit", page, total_p, scope="", owner=0, uid=uid)


async def render_stats(
    svc: Services, uid: int, page: int = 1,
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
    chunk = all_rows[(page - 1) * size: page * size]
    lines = [
        f"<b>{_esc(s['kind'])}</b> · {s['次数']} 次 · "
        f"calls={s['调用量'] or 0} · tokens={s['Token量'] or 0}"
        for s in chunk
    ]
    text = (f"<b>📈 近24小时用量</b>  <i>第 {page}/{total_p} 页 · 共 {total} 类</i>\n\n"
            + "\n".join(lines))
    return text, pager_kb("stats", page, total_p, scope="", owner=0, uid=uid)


# ── 信息视图渲染 ────────────────────────────────────────────────
async def render_help(
    *, scope: str, owner: int, uid: int,
) -> tuple[str, InlineKeyboardMarkup]:
    from app.handlers.commands_registry import build_help_html
    return build_help_html(), info_kb("help", scope=scope, owner=owner, uid=uid)


async def render_whoami(
    svc: Services, user: User, *, scope: str, owner: int, uid: int,
) -> tuple[str, InlineKeyboardMarkup]:
    role_zh = {"superadmin": "超级管理员", "admin": "管理员", "user": "用户"}[user.role]
    quota_html = await _quota_status_html(svc, user)
    text = (
        f"<b>🪪 我的信息</b>\n\n"
        f"<b>ID</b>  <code>{user.tg_id}</code>\n"
        f"<b>角色</b>  {_esc(role_zh)}\n"
        f"<b>授权</b>  {'✅ 已授权' if user.is_allowed else '⛔ 未授权'}\n\n"
        f"{quota_html}"
    )
    return text, info_kb("whoami", scope=scope, owner=owner, uid=uid)


async def render_quota_view(
    svc: Services, user: User, *, scope: str, owner: int, uid: int,
) -> tuple[str, InlineKeyboardMarkup]:
    quota_html = await _quota_status_html(svc, user)
    text = f"<b>📊 我的配额</b>\n\n{quota_html}"
    return text, info_kb("quota", scope=scope, owner=owner, uid=uid)


# ── 配额状态 HTML 渲染(/quota /whoami 共用) ────────────────────
async def _quota_status_html(svc: Services, user: User) -> str:
    """渲染用户配额状态为 HTML 片段。"""
    quotas = await svc.daos.quotas.get_all(user.tg_id)
    if user.is_superadmin:
        return "<b>配额</b>  无限(超级管理员)"
    if not quotas:
        return "<b>配额</b>  未设置(不限)"
    now_ts = int(time.time())
    lines = []
    for q in quotas:
        used = 0 if _window_expired(q, now_ts) else q.used
        limit_txt = "无限" if q.unlimited else str(q.limit_val)
        lines.append(
            f"· <b>{q.mode}</b>  {used}/{limit_txt} "
            f"<i>({_esc(q.period)},重置于 {_esc(_reset_at_text(q))})</i>"
        )
    return "<b>配额</b>\n" + "\n".join(lines)
