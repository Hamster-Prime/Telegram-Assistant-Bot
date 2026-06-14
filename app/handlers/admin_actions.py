"""管理操作纯逻辑 —— 授权/撤销/配额/查看用户(返回 HTML 文本)。

单一事实来源:message handler(私聊/群聊)与 guest 模式共用同一套逻辑,
仅输出通道不同(message.answer vs answerGuestQuery)。

所有函数:
- 不依赖 message 对象做输出(只用于 extract_target_info 读 reply_to_message)
- 返回 HTML 文本字符串
- 自行完成 DAO 写入 + 审计日志 + 命令菜单刷新
"""
from __future__ import annotations

from dataclasses import dataclass

from aiogram.types import Message

from app.db.models import User
from app.handlers.commands_registry import refresh_user_commands
from app.handlers.lists import render_quota_status_html
from app.logging import get_logger
from app.services import Services
from app.utils.clock import format_timestamp

log = get_logger("handlers.admin_actions")


@dataclass(frozen=True)
class TargetInfo:
    """目标用户信息(从回复消息或命令参数解析)。"""
    tg_id: int
    username: str | None = None
    first_name: str | None = None


def extract_target_info(message: Message, args: str) -> TargetInfo | None:
    """从「回复目标」或「命令参数」解析目标用户,尽可能携带 username/first_name。

    回复优先:Guest/私聊/群聊三场景的 reply_to_message.from_user。
    回退到 args 首个数字参数(仅有 ID,无名称)。
    """
    reply = getattr(message, "reply_to_message", None) or getattr(message, "external_reply", None)
    if reply and getattr(reply, "from_user", None):
        fu = reply.from_user
        return TargetInfo(
            tg_id=fu.id,
            username=getattr(fu, "username", None),
            first_name=getattr(fu, "first_name", None),
        )
    if args:
        first = args.split()[0]
        try:
            return TargetInfo(tg_id=int(first))
        except ValueError:
            return None
    return None


def extract_target(message: Message, args: str) -> int | None:
    """仅返回目标用户 ID(extract_target_info 的便捷封装)。"""
    info = extract_target_info(message, args)
    return info.tg_id if info else None


def _target_display_lines(tu: User | None, fallback_id: int) -> list[str]:
    """格式化目标用户显示行(grant/revoke/setquota 等结果共用)。"""
    if tu is None:
        return [f"· ID:<code>{fallback_id}</code>"]
    lines = [f"· ID:<code>{tu.tg_id}</code>"]
    if tu.first_name:
        lines.append(f"· 名称:{tu.first_name}")
    if tu.username:
        lines.append(f"· 用户名:@{tu.username}")
    return lines


async def logic_grant(
    svc: Services, actor: User, target: int,
    *, username: str | None = None, first_name: str | None = None,
) -> str:
    """授权目标用户并套用默认配额。"""
    await svc.daos.users.upsert_basic(target, username, first_name)
    await svc.daos.users.set_authorized(target, True, by=actor.tg_id)
    await svc.quota.ensure_default(target)
    await svc.daos.audit.add(actor.tg_id, "grant", target, "授权用户")
    tu = await svc.daos.users.get(target)
    if tu is not None and tu.role in ("admin", "superadmin"):
        await refresh_user_commands(svc.bot, target, tu.role)

    lines = ["✅ <b>已授权</b>"]
    if tu is not None:
        lines.extend(_target_display_lines(tu, target))
        quota_html = await render_quota_status_html(svc, tu)
    else:
        lines.append(f"<b>ID</b>  <code>{target}</code>")
        quota_html = "<b>配额</b>  已套用默认"
    lines.append("")
    lines.append(quota_html)
    return "\n".join(lines)


async def logic_revoke(
    svc: Services, actor: User, target: int,
    *, username: str | None = None, first_name: str | None = None,
) -> str:
    """撤销目标用户授权。"""
    if target in svc.settings.superadmin_id_list:
        return "⛔ <b>不能撤销超级管理员</b>"
    # 撤销前先读取用户信息用于结果展示
    tu = await svc.daos.users.get(target)
    await svc.daos.users.set_authorized(target, False, by=actor.tg_id)
    await svc.daos.audit.add(actor.tg_id, "revoke", target, "撤销授权")
    await refresh_user_commands(svc.bot, target, "user")

    lines = ["🚫 <b>已撤销授权</b>"]
    lines.extend(_target_display_lines(tu, target))
    return "\n".join(lines)


async def logic_setquota(
    svc: Services, actor: User, target: int, mode: str, limit: int, period: str = "day",
) -> str:
    """设置目标用户配额。"""
    await svc.daos.quotas.set(target, mode, limit, period)
    await svc.daos.audit.add(actor.tg_id, "setquota", target, f"{mode}={limit}/{period}")
    tu = await svc.daos.users.get(target)

    limit_txt = "无限" if limit < 0 else str(limit)
    lines = ["📊 <b>配额已设置</b>"]
    lines.extend(_target_display_lines(tu, target))
    lines.append(f"· {mode}:{limit_txt}({period})")
    return "\n".join(lines)


async def logic_resetquota(svc: Services, actor: User, target: int, mode: str | None = None) -> str:
    """清零目标用户配额用量。"""
    await svc.daos.quotas.reset_used(target, mode)
    await svc.daos.audit.add(actor.tg_id, "resetquota", target, mode or "all")
    tu = await svc.daos.users.get(target)

    mode_label = mode or "全部"
    lines = [f"🔄 <b>已清零{mode_label}配额用量</b>"]
    lines.extend(_target_display_lines(tu, target))
    return "\n".join(lines)


async def logic_userinfo(svc: Services, actor: User, target: int) -> str:
    """查看目标用户的身份、授权状态与配额(管理员用)。"""
    tu = await svc.daos.users.get(target)
    if tu is None:
        return f"⚠️ 用户 <code>{target}</code> 不存在(尚未与 Bot 交互过)"

    role_label = {"superadmin": "超级管理员", "admin": "管理员", "user": "普通用户"}.get(
        tu.role, tu.role,
    )

    lines = [
        "<b>📊 用户信息</b>",
        f"<b>ID</b>  <code>{tu.tg_id}</code>",
        f"<b>名称</b>  {tu.first_name or '—'}",
        f"<b>用户名</b>  @{tu.username}" if tu.username else "<b>用户名</b>  —",
        f"<b>角色</b>  {role_label}",
        f"<b>授权</b>  {'✅ 已授权' if tu.is_allowed else '❌ 未授权'}",
    ]

    if tu.authorized_at:
        lines.append(f"<b>授权时间</b>  {format_timestamp(tu.authorized_at)}")
    if tu.authorized_by:
        lines.append(f"<b>操作人</b>  <code>{tu.authorized_by}</code>")

    quota_html = await render_quota_status_html(svc, tu)
    lines.append("")
    lines.append(quota_html)

    return "\n".join(lines)
