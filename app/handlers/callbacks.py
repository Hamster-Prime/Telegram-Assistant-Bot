"""内联键盘回调处理 —— 列表翻页 / 视图导航 / 关闭(plan §斜杠命令输出优化)。

三类回调:
- ListPageCB:列表翻页(memories/quotas/users/audit/stats),服务端分页
- NavCB:信息视图间导航(help/whoami/quota)
- CloseCB:关闭(删除消息)
- "noop":页码指示器,空应答(止住 loading 圈)

访问控制:每个回调校验 callback.from_user.id == uid(发起人),
并复核授权态(可能已撤销);管理员/超管列表复核角色。

回调不挂 AuthMiddleware(只注册了 message/guest_message/inline_query),
故在此手动从 DB 取 user 复核授权。
"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery

from app.handlers.lists import (
    CloseCB,
    ListPageCB,
    NavCB,
    render_audit,
    render_help,
    render_memories,
    render_quota_view,
    render_quotas,
    render_stats,
    render_users,
    render_whoami,
)
from app.logging import get_logger
from app.services import Services

log = get_logger("handlers.callbacks")

router = Router(name="callbacks")

# 管理员列表 → 需要的角色校验
_ADMIN_LISTS = {"quotas", "users", "stats"}
_SUPER_LISTS = {"audit"}


async def _check_owner(cb: CallbackQuery, expected_uid: int) -> bool:
    """校验点击者即发起人。失败时弹 alert。"""
    if cb.from_user.id != expected_uid:
        await cb.answer("这不是你的消息", show_alert=True)
        return False
    return True


async def _load_user(svc: Services, cb: CallbackQuery, uid: int):
    """复核授权态。返回 user 或 None(并已应答错误)。"""
    user = await svc.daos.users.get(uid)
    if user is None or not user.is_allowed:
        await cb.answer("你的授权已失效", show_alert=True)
        return None
    return user


async def _safe_edit(cb: CallbackQuery, text: str, kb) -> bool:
    """编辑消息文本;消息已失效(超时/已删)时静默应答。返回是否成功。"""
    try:
        await cb.message.edit_text(
            text, parse_mode="HTML", reply_markup=kb,
            disable_web_page_preview=True,
        )
        return True
    except Exception as e:
        await cb.answer("消息已失效", show_alert=True)
        log.debug("编辑消息失败", 错误=str(e)[:120])
        return False


async def _dispatch_list(svc: Services, cb_data: ListPageCB):
    """按 kind 路由到对应渲染函数。"""
    kind, page, uid = cb_data.kind, cb_data.page, cb_data.uid
    if kind == "memories":
        return await render_memories(svc, cb_data.scope, cb_data.owner, uid, page)
    if kind == "quotas":
        return await render_quotas(svc, uid, page)
    if kind == "users":
        return await render_users(svc, uid, page)
    if kind == "audit":
        return await render_audit(svc, uid, page)
    if kind == "stats":
        return await render_stats(svc, uid, page)
    return "未知列表类型", None


@router.callback_query(ListPageCB.filter())
async def on_list_page(cb: CallbackQuery, callback_data: ListPageCB,
                       svc: Services) -> None:
    """列表翻页:校验权限 → 取数渲染 → 编辑消息。"""
    if not await _check_owner(cb, callback_data.uid):
        return
    user = await _load_user(svc, cb, callback_data.uid)
    if user is None:
        return
    kind = callback_data.kind
    # 管理员/超管列表:复核角色(可能已被降级)
    if kind in _ADMIN_LISTS and not user.is_admin:
        await cb.answer("需要管理员权限", show_alert=True)
        return
    if kind in _SUPER_LISTS and not user.is_superadmin:
        await cb.answer("需要超级管理员权限", show_alert=True)
        return
    text, kb = await _dispatch_list(svc, callback_data)
    if await _safe_edit(cb, text, kb):
        await cb.answer()


@router.callback_query(NavCB.filter())
async def on_nav(cb: CallbackQuery, callback_data: NavCB,
                 svc: Services) -> None:
    """信息视图导航:help/whoami/quota 间切换。"""
    if not await _check_owner(cb, callback_data.uid):
        return
    user = await _load_user(svc, cb, callback_data.uid)
    if user is None:
        return
    scope, owner, uid = callback_data.scope, callback_data.owner, callback_data.uid
    view = callback_data.view
    if view == "help":
        text, kb = await render_help(scope=scope, owner=owner, uid=uid)
    elif view == "whoami":
        text, kb = await render_whoami(svc, user, scope=scope, owner=owner, uid=uid)
    elif view == "quota":
        text, kb = await render_quota_view(svc, user, scope=scope, owner=owner, uid=uid)
    else:
        await cb.answer()
        return
    if await _safe_edit(cb, text, kb):
        await cb.answer()


@router.callback_query(CloseCB.filter())
async def on_close(cb: CallbackQuery, callback_data: CloseCB,
                   svc: Services) -> None:
    """关闭:校验发起人后删除消息。"""
    if callback_data.uid and not await _check_owner(cb, callback_data.uid):
        return
    try:
        await cb.message.delete()
    except Exception as e:
        log.debug("删除消息失败(忽略)", 错误=str(e)[:120])
    await cb.answer()


@router.callback_query(F.data == "noop")
async def on_noop(cb: CallbackQuery) -> None:
    """页码指示器:空应答,止住按钮 loading 圈。"""
    await cb.answer()
