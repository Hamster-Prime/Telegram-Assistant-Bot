"""内联键盘回调处理测试(plan §斜杠命令输出优化)。

验证:
- ListPageCB:非发起人被拒(show_alert);发起人翻页 → edit_text 被调
- NavCB:非发起人被拒;发起人导航 → edit_text 被调
- CloseCB:触发 message.delete
- noop:空应答(止住 loading 圈)
- 管理员列表复核角色(已被降级时拒绝)

使用 FakeCallbackQuery + AsyncMock 隔离,不依赖真实 Telegram。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.db.models import Memory, User
from app.handlers.callbacks import on_close, on_list_page, on_nav, on_noop
from app.handlers.lists import CloseCB, ListPageCB, NavCB


def _make_user(uid: int = 1, *, role: str = "user", allowed: bool = True) -> User:
    return User(
        tg_id=uid, username="u", first_name="U", role=role,
        authorized=1 if allowed else 0, authorized_by=None, authorized_at=None,
        settings="{}", created_at=0, updated_at=0,
    )


class FakeMessage:
    """最小 Message 替身:记录 edit_text / delete 调用。"""
    def __init__(self):
        self.edit_text = AsyncMock()
        self.delete = AsyncMock()


class FakeCallbackQuery:
    """最小 CallbackQuery 替身。"""
    def __init__(self, from_id: int, message: FakeMessage | None = None):
        self.from_user = SimpleNamespace(id=from_id)
        self.message = message or FakeMessage()
        self.answer = AsyncMock()


def _fake_svc(*, user: User | None = None, mem_count: int = 0,
              mems=None, quota_all=None):
    """带 AsyncMock DAO 的 Services 替身。

    render_memories 需要 memories.count + list_all;其它 render_* 各自的 DAO
    也一并 mock(返回空,避免列表渲染时 AttributeError)。
    """
    daos = SimpleNamespace(
        users=SimpleNamespace(get=AsyncMock(return_value=user)),
        memories=SimpleNamespace(
            count=AsyncMock(return_value=mem_count),
            list_all=AsyncMock(return_value=mems or []),
        ),
        quotas=SimpleNamespace(
            count_all=AsyncMock(return_value=0),
            list_all=AsyncMock(return_value=[]),
            get_all=AsyncMock(return_value=quota_all or []),
        ),
        audit=SimpleNamespace(
            count=AsyncMock(return_value=0),
            recent=AsyncMock(return_value=[]),
        ),
        users_list=SimpleNamespace(
            count=AsyncMock(return_value=0),
            list_users=AsyncMock(return_value=[]),
        ),
        usage=SimpleNamespace(stats=AsyncMock(return_value=[])),
    )
    # render_users 用 daos.users.count / list_users;但 users.get 也在这。
    # 用单一 users 对象同时承担 get + count + list_users。
    daos.users.count = AsyncMock(return_value=0)
    daos.users.list_users = AsyncMock(return_value=[])
    return SimpleNamespace(daos=daos)


# ── ListPageCB ──────────────────────────────────────────────────
async def test_list_page_rejects_non_owner():
    """非发起人点击 → show_alert,不编辑消息。"""
    cb = FakeCallbackQuery(from_id=888)  # 点击者
    cb_data = ListPageCB(kind="memories", page=1, scope="user", owner=1, uid=1)
    svc = _fake_svc(user=_make_user(1))
    await on_list_page(cb, cb_data, svc)
    cb.answer.assert_awaited_once()
    args, kwargs = cb.answer.call_args
    assert kwargs.get("show_alert") is True
    cb.message.edit_text.assert_not_awaited()


async def test_list_page_unauthorized_user_rejected():
    """发起人授权已失效 → 拒绝。"""
    cb = FakeCallbackQuery(from_id=1)
    cb_data = ListPageCB(kind="memories", page=1, scope="user", owner=1, uid=1)
    svc = _fake_svc(user=_make_user(1, allowed=False))
    await on_list_page(cb, cb_data, svc)
    cb.answer.assert_awaited_once()
    assert cb.answer.call_args.kwargs.get("show_alert") is True
    cb.message.edit_text.assert_not_awaited()


async def test_list_page_owner_paginates():
    """发起人翻页 → edit_text 被调用,内容为 HTML。"""
    cb = FakeCallbackQuery(from_id=1)
    cb_data = ListPageCB(kind="memories", page=1, scope="user", owner=1, uid=1)
    mems = [Memory(id=1, scope="user", owner_id=1, text="hello",
                   source="manual", weight=1.0, created_at=1700000000)]
    svc = _fake_svc(user=_make_user(1), mem_count=1, mems=mems)
    await on_list_page(cb, cb_data, svc)
    cb.message.edit_text.assert_awaited_once()
    args, kwargs = cb.message.edit_text.call_args
    assert kwargs.get("parse_mode") == "HTML"
    assert "长期记忆" in args[0]
    cb.answer.assert_awaited_once()
    # 翻页成功不应弹 alert
    assert not cb.answer.call_args.kwargs.get("show_alert")


async def test_list_page_admin_list_requires_admin():
    """普通用户翻管理员列表(quotas)→ 被拒。"""
    cb = FakeCallbackQuery(from_id=1)
    cb_data = ListPageCB(kind="quotas", page=1, uid=1)
    svc = _fake_svc(user=_make_user(1, role="user"))
    await on_list_page(cb, cb_data, svc)
    assert cb.answer.call_args.kwargs.get("show_alert") is True
    cb.message.edit_text.assert_not_awaited()


async def test_list_page_audit_requires_superadmin():
    """管理员翻审计日志 → 被拒(需超管)。"""
    cb = FakeCallbackQuery(from_id=1)
    cb_data = ListPageCB(kind="audit", page=1, uid=1)
    svc = _fake_svc(user=_make_user(1, role="admin"))
    await on_list_page(cb, cb_data, svc)
    assert cb.answer.call_args.kwargs.get("show_alert") is True
    cb.message.edit_text.assert_not_awaited()


async def test_list_page_edit_failure_alerts():
    """edit_text 失败(消息超时/已删)→ 弹 alert 而非崩溃。"""
    cb = FakeCallbackQuery(from_id=1)
    cb.message.edit_text = AsyncMock(side_effect=RuntimeError("timed out"))
    cb_data = ListPageCB(kind="memories", page=1, scope="user", owner=1, uid=1)
    svc = _fake_svc(user=_make_user(1), mem_count=1)
    await on_list_page(cb, cb_data, svc)
    cb.answer.assert_awaited()
    assert cb.answer.call_args.kwargs.get("show_alert") is True


# ── NavCB ───────────────────────────────────────────────────────
async def test_nav_rejects_non_owner():
    cb = FakeCallbackQuery(from_id=999)
    cb_data = NavCB(view="help", scope="user", owner=1, uid=1)
    svc = _fake_svc(user=_make_user(1))
    await on_nav(cb, cb_data, svc)
    assert cb.answer.call_args.kwargs.get("show_alert") is True
    cb.message.edit_text.assert_not_awaited()


async def test_nav_help_edits_message():
    cb = FakeCallbackQuery(from_id=1)
    cb_data = NavCB(view="help", scope="user", owner=1, uid=1)
    svc = _fake_svc(user=_make_user(1))
    await on_nav(cb, cb_data, svc)
    cb.message.edit_text.assert_awaited_once()
    assert cb.message.edit_text.call_args.kwargs.get("parse_mode") == "HTML"


async def test_nav_whoami_edits_message():
    cb = FakeCallbackQuery(from_id=1)
    cb_data = NavCB(view="whoami", scope="user", owner=1, uid=1)
    svc = _fake_svc(user=_make_user(1, role="user"))
    await on_nav(cb, cb_data, svc)
    cb.message.edit_text.assert_awaited_once()
    assert "我的信息" in cb.message.edit_text.call_args.args[0]


async def test_nav_quota_edits_message():
    cb = FakeCallbackQuery(from_id=1)
    cb_data = NavCB(view="quota", scope="user", owner=1, uid=1)
    svc = _fake_svc(user=_make_user(1, role="user"))
    await on_nav(cb, cb_data, svc)
    cb.message.edit_text.assert_awaited_once()


# ── CloseCB ─────────────────────────────────────────────────────
async def test_close_deletes_message():
    cb = FakeCallbackQuery(from_id=1)
    cb_data = CloseCB(uid=1)
    svc = _fake_svc()
    await on_close(cb, cb_data, svc)
    cb.message.delete.assert_awaited_once()
    cb.answer.assert_awaited_once()


async def test_close_rejects_non_owner():
    cb = FakeCallbackQuery(from_id=999)
    cb_data = CloseCB(uid=1)
    svc = _fake_svc()
    await on_close(cb, cb_data, svc)
    cb.message.delete.assert_not_awaited()
    assert cb.answer.call_args.kwargs.get("show_alert") is True


async def test_close_swallows_delete_failure():
    """删除失败(无权限/已删)不崩溃。"""
    cb = FakeCallbackQuery(from_id=1)
    cb.message.delete = AsyncMock(side_effect=RuntimeError("forbidden"))
    cb_data = CloseCB(uid=1)
    svc = _fake_svc()
    await on_close(cb, cb_data, svc)  # 不抛
    cb.answer.assert_awaited_once()


# ── noop ────────────────────────────────────────────────────────
async def test_noop_just_answers():
    cb = FakeCallbackQuery(from_id=1)
    await on_noop(cb)
    cb.answer.assert_awaited_once()
    # noop 不弹 alert
    assert not cb.answer.call_args.kwargs.get("show_alert")
