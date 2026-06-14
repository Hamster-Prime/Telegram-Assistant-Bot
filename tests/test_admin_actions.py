"""管理员操作逻辑测试 —— logic_grant/revoke/setquota/resetquota/userinfo。

验证:DAO 写入、审计日志、配额套用、HTML 输出格式。
使用真实 Database (in-memory) + QuotaManager。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import Settings
from app.core.quota import QuotaManager
from app.db.dao import DAOBundle
from app.db.engine import Database
from app.db.models import User
from app.handlers.admin_actions import (
    TargetInfo,
    extract_target,
    extract_target_info,
    logic_grant,
    logic_resetquota,
    logic_revoke,
    logic_setquota,
    logic_userinfo,
)


class FakeBot:
    async def set_my_commands(self, commands, *, scope=None):
        pass


@pytest.fixture
async def daos():
    db = Database(":memory:", wal=False)
    await db.connect()
    bundle = DAOBundle(db)
    yield bundle
    await db.close()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        minimax_api_keys="k1",
        default_quota_mode="tokens",
        default_quota_limit=1000,
        default_quota_period="day",
    )


@pytest.fixture
def svc(daos: DAOBundle, settings: Settings):
    qm = QuotaManager(daos, settings)
    return SimpleNamespace(
        daos=daos,
        quota=qm,
        bot=FakeBot(),
        settings=settings,
    )


@pytest.fixture
def admin() -> User:
    return User(tg_id=900, username="admin", first_name="Admin", role="admin", authorized=1)


# ── extract_target / extract_target_info ──────────────────────


def test_extract_target_from_reply():
    msg = SimpleNamespace(
        reply_to_message=SimpleNamespace(
            from_user=SimpleNamespace(id=555, username="alice", first_name="Alice")),
        external_reply=None,
    )
    assert extract_target(msg, "") == 555


def test_extract_target_from_args():
    msg = SimpleNamespace(reply_to_message=None, external_reply=None)
    assert extract_target(msg, "12345") == 12345


def test_extract_target_reply_priority():
    """回复优先于参数。"""
    msg = SimpleNamespace(
        reply_to_message=SimpleNamespace(
            from_user=SimpleNamespace(id=777, username=None, first_name=None)),
        external_reply=None,
    )
    assert extract_target(msg, "999") == 777


def test_extract_target_none():
    msg = SimpleNamespace(reply_to_message=None, external_reply=None)
    assert extract_target(msg, "not_a_number") is None


def test_extract_target_info_captures_names_from_reply():
    """回复时携带目标的 username/first_name。"""
    msg = SimpleNamespace(
        reply_to_message=SimpleNamespace(
            from_user=SimpleNamespace(id=555, username="alice", first_name="Alice")),
        external_reply=None,
    )
    info = extract_target_info(msg, "")
    assert info == TargetInfo(tg_id=555, username="alice", first_name="Alice")


def test_extract_target_info_id_only_from_args():
    """纯 ID 参数时 username/first_name 为 None。"""
    msg = SimpleNamespace(reply_to_message=None, external_reply=None)
    info = extract_target_info(msg, "12345")
    assert info == TargetInfo(tg_id=12345, username=None, first_name=None)


def test_extract_target_info_none():
    msg = SimpleNamespace(reply_to_message=None, external_reply=None)
    assert extract_target_info(msg, "abc") is None


# ── logic_grant ────────────────────────────────────────────────


async def test_logic_grant_authorizes_and_sets_quota(svc, admin):
    result = await logic_grant(svc, admin, 100)
    assert "✅" in result
    u = await svc.daos.users.get(100)
    assert u is not None
    assert u.is_allowed
    q = await svc.daos.quotas.get(100, "tokens")
    assert q is not None
    assert q.limit_val == 1000
    # 审计日志
    audit = await svc.daos.audit.recent()
    assert any(a["action"] == "grant" and a["target_id"] == 100 for a in audit)


async def test_logic_grant_existing_user_preserves_role(svc, admin):
    """授权已有管理员不应改变其角色。"""
    await svc.daos.users.upsert_basic(200, "bob", "Bob")
    await svc.daos.users.set_role(200, "admin")
    await logic_grant(svc, admin, 200)
    u = await svc.daos.users.get(200)
    assert u.role == "admin"


async def test_logic_grant_with_names_stores_them(svc, admin):
    """授权时传入名称 → 落库存储。"""
    await logic_grant(svc, admin, 250, username="charlie", first_name="Charlie")
    u = await svc.daos.users.get(250)
    assert u.username == "charlie"
    assert u.first_name == "Charlie"
    assert u.is_allowed


async def test_logic_grant_without_names_does_not_clobber(svc, admin):
    """COALESCE:仅传 ID 授权不清空已有用户名。"""
    # 用户已存在且有名称
    await svc.daos.users.upsert_basic(260, "dave", "Dave")
    await logic_grant(svc, admin, 260)  # 不传 name
    u = await svc.daos.users.get(260)
    assert u.username == "dave"   # 未被清空
    assert u.first_name == "Dave"  # 未被清空


async def test_logic_grant_result_html_includes_name(svc, admin):
    """授权结果 HTML 含目标名称与配额进度条。"""
    result = await logic_grant(svc, admin, 270, username="eve", first_name="Eve")
    assert "<b>" in result
    assert "270" in result
    assert "Eve" in result
    assert "@eve" in result
    assert "tokens" in result  # 配额信息
    assert "█" in result  # 进度条


async def test_logic_grant_result_shows_dash_for_unknown_name(svc, admin):
    """仅 ID 授权(新用户)时结果只显示 ID,不含名称行。"""
    result = await logic_grant(svc, admin, 280)
    assert "<code>280</code>" in result
    assert "已授权" in result
    assert "█" in result  # 进度条


# ── logic_revoke ───────────────────────────────────────────────


async def test_logic_revoke_revokes(svc, admin):
    await logic_grant(svc, admin, 300)
    result = await logic_revoke(svc, admin, 300)
    assert "🚫" in result
    u = await svc.daos.users.get(300)
    assert not u.is_allowed
    audit = await svc.daos.audit.recent()
    assert any(a["action"] == "revoke" and a["target_id"] == 300 for a in audit)


async def test_logic_revoke_protects_superadmin(svc, admin):
    """不能撤销超级管理员(来自 settings.superadmin_ids)。"""
    svc.settings = Settings(
        _env_file=None, minimax_api_keys="k1", superadmin_ids="888",
    )
    result = await logic_revoke(svc, admin, 888)
    assert "⛔" in result


# ── logic_setquota ─────────────────────────────────────────────


async def test_logic_setquota(svc, admin):
    result = await logic_setquota(svc, admin, 400, "tokens", 50000, "month")
    assert "50000" in result
    assert "month" in result
    q = await svc.daos.quotas.get(400, "tokens")
    assert q.limit_val == 50000
    assert q.period == "month"
    audit = await svc.daos.audit.recent()
    assert any(a["action"] == "setquota" for a in audit)


async def test_logic_setquota_unlimited(svc, admin):
    await logic_setquota(svc, admin, 401, "calls", -1, "total")
    q = await svc.daos.quotas.get(401, "calls")
    assert q.unlimited


# ── logic_resetquota ───────────────────────────────────────────


async def test_logic_resetquota(svc, admin):
    await logic_setquota(svc, admin, 500, "tokens", 10000, "day")
    # 模拟已用量
    q = await svc.daos.quotas.get(500, "tokens")
    await svc.daos.db.execute(
        "UPDATE quotas SET used=5000 WHERE user_id=500 AND mode='tokens'"
    )
    result = await logic_resetquota(svc, admin, 500, "tokens")
    assert "🔄" in result
    q = await svc.daos.quotas.get(500, "tokens")
    assert q.used == 0


async def test_logic_resetquota_all_modes(svc, admin):
    await logic_setquota(svc, admin, 501, "tokens", 10000, "day")
    await logic_setquota(svc, admin, 501, "calls", 100, "day")
    await logic_resetquota(svc, admin, 501, None)
    for mode in ("tokens", "calls"):
        q = await svc.daos.quotas.get(501, mode)
        assert q.used == 0


# ── logic_userinfo ─────────────────────────────────────────────


async def test_logic_userinfo_shows_user_details(svc, admin):
    await logic_grant(svc, admin, 600)
    result = await logic_userinfo(svc, admin, 600)
    assert "600" in result
    assert "已授权" in result
    assert "配额" in result
    assert "█" in result  # 进度条
    assert "每日" in result  # period 中文标签


async def test_logic_userinfo_all_bold_labels(svc, admin):
    """userinfo 所有标签均为粗体。"""
    await svc.daos.users.upsert_basic(601, "bold", "Bold", )
    await svc.daos.users.set_authorized(601, True, by=900)
    result = await logic_userinfo(svc, admin, 601)
    assert "<b>ID</b>" in result
    assert "<b>名称</b>" in result
    assert "<b>角色</b>" in result
    assert "<b>授权</b>" in result
    assert "<b>配额</b>" in result


async def test_logic_userinfo_nonexistent_user(svc, admin):
    result = await logic_userinfo(svc, admin, 99999)
    assert "不存在" in result


async def test_logic_userinfo_unauthorized_user(svc, admin):
    await svc.daos.users.upsert_basic(700, "newbie", "Newbie")
    result = await logic_userinfo(svc, admin, 700)
    assert "未授权" in result


async def test_logic_userinfo_html_format(svc, admin):
    await svc.daos.users.upsert_basic(800, "htmluser", "HTML")
    await svc.daos.users.set_authorized(800, True, by=900)
    await svc.quota.ensure_default(800)
    result = await logic_userinfo(svc, admin, 800)
    assert "<b>" in result
    assert "<code>" in result
    assert "█" in result  # 进度条


# ── Guest 命令分流:管理员命令 ──────────────────────────────────
# 验证 execute_guest_command 能正确路由 grant/revoke/userinfo/setquota


def _make_guest_message(
    text: str, reply_from_id: int | None = None,
    reply_username: str | None = "target_user", reply_first_name: str | None = "Target",
):
    """构造 Guest 消息(含可选回复目标)。"""
    payload = {
        "message_id": 2,
        "date": 0,
        "chat": {"id": 200, "type": "group"},
        "guest_query_id": "gq-1",
        "guest_bot_caller_user": {
            "id": 900, "is_bot": False, "first_name": "Admin", "username": "admin",
        },
        "text": text,
    }
    if reply_from_id is not None:
        from_obj: dict = {"id": reply_from_id, "is_bot": False}
        if reply_first_name is not None:
            from_obj["first_name"] = reply_first_name
        if reply_username is not None:
            from_obj["username"] = reply_username
        payload["reply_to_message"] = {
            "message_id": 1,
            "date": 0,
            "chat": {"id": 200, "type": "group"},
            "from": from_obj,
        }
    from aiogram.types import Message
    return Message.model_validate(payload)


async def test_guest_grant_admin_with_reply(svc, admin):
    """管理员在 Guest 场景回复目标消息 + /grant → 授权成功。"""
    from app.handlers.guest_commands import execute_guest_command
    msg = _make_guest_message("/grant", reply_from_id=1000)
    result = await execute_guest_command(svc, admin, msg, "my_bot")
    assert result is not None
    assert "✅" in result
    u = await svc.daos.users.get(1000)
    assert u.is_allowed


async def test_guest_grant_captures_target_name(svc, admin):
    """Guest 回复授权时捕获目标 username/first_name → 落库 + 结果展示。"""
    from app.handlers.guest_commands import execute_guest_command
    msg = _make_guest_message(
        "/grant", reply_from_id=1050, reply_username="alice", reply_first_name="Alice")
    result = await execute_guest_command(svc, admin, msg, "my_bot")
    assert result is not None
    assert "Alice" in result
    assert "@alice" in result
    u = await svc.daos.users.get(1050)
    assert u.username == "alice"
    assert u.first_name == "Alice"


async def test_guest_revoke_admin_with_reply(svc, admin):
    """管理员在 Guest 场景回复目标消息 + /revoke → 撤销成功。"""
    from app.handlers.guest_commands import execute_guest_command
    # 先授权
    await logic_grant(svc, admin, 1100)
    msg = _make_guest_message("/revoke", reply_from_id=1100)
    result = await execute_guest_command(svc, admin, msg, "my_bot")
    assert result is not None
    assert "🚫" in result
    u = await svc.daos.users.get(1100)
    assert not u.is_allowed


async def test_guest_userinfo_admin_with_reply(svc, admin):
    """管理员在 Guest 场景回复目标消息 + /userinfo → 返回用户信息。"""
    from app.handlers.guest_commands import execute_guest_command
    await logic_grant(svc, admin, 1200)
    msg = _make_guest_message("/userinfo", reply_from_id=1200)
    result = await execute_guest_command(svc, admin, msg, "my_bot")
    assert result is not None
    assert "1200" in result


async def test_guest_grant_non_admin_denied(svc):
    """非管理员在 Guest 场景发 /grant → 返回权限不足提示。"""
    from app.handlers.guest_commands import execute_guest_command
    regular_user = User(tg_id=500, username="regular", first_name="Reg")
    msg = _make_guest_message("/grant", reply_from_id=1000)
    result = await execute_guest_command(svc, regular_user, msg, "my_bot")
    assert result is not None
    assert "管理员" in result


async def test_guest_grant_no_target_hint(svc, admin):
    """管理员发 /grant 但未回复/未传 ID → 提示用法。"""
    from app.handlers.guest_commands import execute_guest_command
    msg = _make_guest_message("/grant")
    result = await execute_guest_command(svc, admin, msg, "my_bot")
    assert result is not None
    assert "回复" in result or "用户" in result


async def test_guest_grant_with_id_arg(svc, admin):
    """管理员发 /grant 1300(数字参数)→ 授权成功(回退路径)。"""
    from app.handlers.guest_commands import execute_guest_command
    msg = _make_guest_message("/grant 1300")
    result = await execute_guest_command(svc, admin, msg, "my_bot")
    assert result is not None
    assert "✅" in result
    u = await svc.daos.users.get(1300)
    assert u.is_allowed


async def test_guest_other_admin_cmds_still_blocked(svc, admin):
    """quotas/users/stats/promote/demote/broadcast/audit 仍被阻止(私聊专用)。"""
    from app.handlers.guest_commands import execute_guest_command
    for cmd in ("quotas", "users", "stats", "promote", "demote", "broadcast", "audit"):
        msg = _make_guest_message(f"/{cmd}")
        result = await execute_guest_command(svc, admin, msg, "my_bot")
        assert result is not None
        assert "私聊" in result, f"/{cmd} 应提示去私聊"
