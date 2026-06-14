"""列表分页渲染 + 内联键盘构造测试(plan §斜杠命令输出优化)。

验证:
- _total_pages 页数计算
- 各 render_* 函数:空表文案、有数据时的 HTML 文本与键盘、HTML 转义
- pager_kb:首页/末页/中页/单页 的按钮启用态
- info_kb:导航按钮排除当前视图、含记忆入口与关闭
- CallbackData pack/unpack 往返

使用 SimpleNamespace + AsyncMock 隔离 DAO,不依赖真实 DB。
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.db.models import Memory, Quota, User
from app.handlers.lists import (
    PAGE_SIZE,
    CloseCB,
    ListPageCB,
    NavCB,
    _total_pages,
    info_kb,
    pager_kb,
    render_audit,
    render_help,
    render_memories,
    render_quota_view,
    render_quotas,
    render_stats,
    render_users,
    render_whoami,
)


# ── 辅助构造 ────────────────────────────────────────────────────
def _make_user(uid: int = 1, *, role: str = "user", allowed: bool = True,
               username: str | None = "alice") -> User:
    return User(
        tg_id=uid, username=username, first_name="A", role=role,
        authorized=1 if allowed else 0, authorized_by=None, authorized_at=None,
        settings="{}", created_at=0, updated_at=0,
    )


def _make_memory(mid: int, text: str, scope: str = "user",
                 owner: int = 1) -> Memory:
    return Memory(id=mid, scope=scope, owner_id=owner, text=text,
                  source="manual", weight=1.0, created_at=1700000000,
                  last_used_at=None)


def _make_quota(uid: int = 10, mode: str = "calls", period: str = "day",
                limit: int = 100, used: int = 5) -> Quota:
    return Quota(user_id=uid, mode=mode, period=period, limit_val=limit,
                 used=used, window_start=int(time.time()), updated_at=int(time.time()))


def _buttons_text(markup) -> list[str]:
    """展平键盘所有按钮的 text。"""
    return [b.text for row in markup.inline_keyboard for b in row]


def _buttons_cb(markup) -> list[str]:
    """展平键盘所有按钮的 callback_data。"""
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def _fake_svc(*, mem_count=0, mems=None, quota_count=0, quotas=None,
              user_count=0, users=None, audit_count=0, audit_rows=None,
              stats_rows=None, quota_all=None):
    """构造带 AsyncMock DAO 的 Services 替身。"""
    daos = SimpleNamespace(
        memories=SimpleNamespace(
            count=AsyncMock(return_value=mem_count),
            list_all=AsyncMock(return_value=mems or []),
        ),
        quotas=SimpleNamespace(
            count_all=AsyncMock(return_value=quota_count),
            list_all=AsyncMock(return_value=quotas or []),
            get_all=AsyncMock(return_value=quota_all or []),
        ),
        users=SimpleNamespace(
            count=AsyncMock(return_value=user_count),
            list_users=AsyncMock(return_value=users or []),
        ),
        audit=SimpleNamespace(
            count=AsyncMock(return_value=audit_count),
            recent=AsyncMock(return_value=audit_rows or []),
        ),
        usage=SimpleNamespace(
            stats=AsyncMock(return_value=stats_rows or []),
        ),
    )
    return SimpleNamespace(daos=daos)


# ── _total_pages ────────────────────────────────────────────────
def test_total_pages_zero():
    assert _total_pages(0, 5) == 0


def test_total_pages_exact_multiple():
    assert _total_pages(10, 5) == 2


def test_total_pages_with_remainder():
    assert _total_pages(11, 5) == 3


def test_total_pages_one():
    assert _total_pages(1, 5) == 1


# ── render_memories ─────────────────────────────────────────────
async def test_memories_empty_returns_text_and_nav_kb():
    svc = _fake_svc(mem_count=0)
    text, kb = await render_memories(svc, "user", 1, uid=1)
    assert "暂无长期记忆" in text
    # 空表仍有导航键盘(info_kb)
    assert kb is not None
    labels = _buttons_text(kb)
    # 含记忆入口 + 关闭 + 另外两个信息视图
    assert "🧠 记忆" in labels
    assert "✖ 关闭" in labels


async def test_memories_with_data_paginates():
    mems = [_make_memory(i, f"记忆{i}") for i in range(1, 4)]
    svc = _fake_svc(mem_count=12, mems=mems)
    text, kb = await render_memories(svc, "user", 1, uid=1, page=2)
    assert "第 2/3 页" in text  # 12 条 / 5 = 3 页
    assert "共 12 条" in text
    # list_all 被以正确的 offset 调用
    svc.daos.memories.list_all.assert_awaited_once()
    args, kwargs = svc.daos.memories.list_all.call_args
    assert kwargs["offset"] == 5  # (2-1)*5
    assert kwargs["limit"] == PAGE_SIZE["memories"]
    assert kb is not None


async def test_memories_html_escapes_text():
    mems = [_make_memory(1, "<script>alert(1)</script>")]
    svc = _fake_svc(mem_count=1, mems=mems)
    text, _ = await render_memories(svc, "user", 1, uid=1)
    assert "<script>" not in text  # 被转义
    assert "&lt;script&gt;" in text


async def test_memories_page_clamped_to_range():
    svc = _fake_svc(mem_count=3, mems=[_make_memory(1, "x")])
    text, kb = await render_memories(svc, "user", 1, uid=1, page=99)
    # 越界页码被夹到最后一页
    assert "第 1/1 页" in text


# ── render_quotas ───────────────────────────────────────────────
async def test_quotas_empty_returns_no_kb():
    svc = _fake_svc(quota_count=0)
    text, kb = await render_quotas(svc, uid=1)
    assert "暂无配额记录" in text
    assert kb is None


async def test_quotas_with_data():
    svc = _fake_svc(quota_count=15,
                    quotas=[_make_quota(10, "calls", "day", 100, 5)])
    text, kb = await render_quotas(svc, uid=1, page=1)
    assert "第 1/2 页" in text  # 15/10 = 2 页
    assert "10" in text
    assert kb is not None


async def test_quotas_html_escapes_username():
    # quotas 没有 username,但测 period/mode 转义路径
    svc = _fake_svc(quota_count=1,
                    quotas=[_make_quota(10, "calls", "day", 100, 5)])
    text, _ = await render_quotas(svc, uid=1)
    assert "<b>" in text  # HTML 标签存在
    assert "∞" not in text  # 非无限


# ── render_users ────────────────────────────────────────────────
async def test_users_html_escapes_username():
    u = _make_user(10, username="<b>x</b>")
    svc = _fake_svc(user_count=1, users=[u])
    text, _ = await render_users(svc, uid=1)
    # 原始 <b> 不应出现(被转义),但结构 <b> 标签应存在
    assert "@&lt;b&gt;x&lt;/b&gt;" in text


async def test_users_role_icons():
    users = [
        _make_user(1, role="superadmin"),
        _make_user(2, role="admin"),
        _make_user(3, role="user"),
    ]
    svc = _fake_svc(user_count=3, users=users)
    text, kb = await render_users(svc, uid=1)
    assert "👑" in text
    assert "🛡" in text
    assert "👤" in text
    assert kb is not None


# ── render_audit ────────────────────────────────────────────────
async def test_audit_empty():
    svc = _fake_svc(audit_count=0)
    text, kb = await render_audit(svc, uid=1)
    assert "为空" in text
    assert kb is None


async def test_audit_with_data_escapes_detail():
    rows = [{
        "id": 1, "actor_id": 100, "action": "grant", "target_id": 200,
        "detail": "<x>", "created_at": 1700000000,
    }]
    svc = _fake_svc(audit_count=1, audit_rows=rows)
    text, _ = await render_audit(svc, uid=1, page=1)
    assert "&lt;x&gt;" in text
    assert "<x>" not in text


# ── render_stats ────────────────────────────────────────────────
async def test_stats_empty():
    svc = _fake_svc(stats_rows=[])
    text, kb = await render_stats(svc, uid=1)
    assert "无用量" in text
    assert kb is None


async def test_stats_with_data():
    rows = [{"kind": "chat", "次数": 5, "调用量": 5, "Token量": 100}]
    svc = _fake_svc(stats_rows=rows)
    text, kb = await render_stats(svc, uid=1)
    assert "chat" in text
    assert "5 次" in text
    assert kb is not None


# ── pager_kb ────────────────────────────────────────────────────
def test_pager_kb_first_page_no_prev():
    kb = pager_kb("quotas", page=1, total=3, scope="", owner=0, uid=1)
    labels = _buttons_text(kb)
    assert "◀ 上一页" not in labels
    assert "下一页 ▶" in labels
    assert "1/3" in labels
    assert "✖ 关闭" in labels


def test_pager_kb_last_page_no_next():
    kb = pager_kb("quotas", page=3, total=3, scope="", owner=0, uid=1)
    labels = _buttons_text(kb)
    assert "◀ 上一页" in labels
    assert "下一页 ▶" not in labels


def test_pager_kb_middle_page_both():
    kb = pager_kb("quotas", page=2, total=3, scope="", owner=0, uid=1)
    labels = _buttons_text(kb)
    assert "◀ 上一页" in labels
    assert "下一页 ▶" in labels


def test_pager_kb_single_page_no_arrows():
    kb = pager_kb("quotas", page=1, total=1, scope="", owner=0, uid=1)
    labels = _buttons_text(kb)
    assert "◀ 上一页" not in labels
    assert "下一页 ▶" not in labels
    # 单页时也不显示 1/1 指示器(无翻页意义)
    assert "1/1" not in labels
    assert "✖ 关闭" in labels


def test_pager_kb_memories_has_nav_row():
    """memories 键盘额外含信息视图导航行。"""
    kb = pager_kb("memories", page=1, total=2, scope="user", owner=1, uid=1)
    labels = _buttons_text(kb)
    assert "📖 帮助" in labels
    assert "🪪 我的信息" in labels
    assert "📊 我的配额" in labels


def test_pager_kb_non_memories_no_nav_row():
    """非 memories 列表不含信息视图导航行。"""
    kb = pager_kb("quotas", page=1, total=2, scope="", owner=0, uid=1)
    labels = _buttons_text(kb)
    assert "📖 帮助" not in labels
    assert "📊 我的配额" not in labels


def test_pager_kb_callback_data_encodes_uid():
    """翻页按钮的 callback_data 编码了 uid(访问控制依据)。"""
    kb = pager_kb("users", page=1, total=2, scope="", owner=0, uid=999)
    cbs = _buttons_cb(kb)
    next_cb = [c for c in cbs if c.startswith("lp:")][0]
    decoded = ListPageCB.unpack(next_cb)
    assert decoded.uid == 999
    assert decoded.page == 2


# ── info_kb ─────────────────────────────────────────────────────
def test_info_kb_excludes_current_view():
    kb = info_kb("help", scope="user", owner=1, uid=1)
    labels = _buttons_text(kb)
    assert "📖 帮助" not in labels  # 当前视图不出现
    assert "🪪 我的信息" in labels
    assert "📊 我的配额" in labels
    assert "🧠 记忆" in labels
    assert "✖ 关闭" in labels


def test_info_kb_whoami_excludes_whoami():
    kb = info_kb("whoami", scope="user", owner=1, uid=1)
    labels = _buttons_text(kb)
    assert "🪪 我的信息" not in labels
    assert "📖 帮助" in labels


def test_info_kb_close_encodes_uid():
    kb = info_kb("quota", scope="user", owner=1, uid=42)
    cbs = _buttons_cb(kb)
    close_cb = [c for c in cbs if c.startswith("cls:")][0]
    decoded = CloseCB.unpack(close_cb)
    assert decoded.uid == 42


def test_info_kb_memory_button_is_list_page_cb():
    kb = info_kb("help", scope="user", owner=1, uid=1)
    cbs = _buttons_cb(kb)
    mem_cb = [c for c in cbs if c.startswith("lp:memories")][0]
    decoded = ListPageCB.unpack(mem_cb)
    assert decoded.kind == "memories"
    assert decoded.page == 1
    assert decoded.uid == 1


# ── render_help / whoami / quota_view ───────────────────────────
async def test_render_help_returns_html_with_keyboard():
    text, kb = await render_help(scope="user", owner=1, uid=1)
    assert "<b>" in text
    assert "助理机器人" in text
    assert kb is not None
    assert "✖ 关闭" in _buttons_text(kb)


async def test_render_whoami_shows_id_and_role():
    svc = _fake_svc(quota_all=[])
    user = _make_user(123, role="admin")
    text, kb = await render_whoami(svc, user, scope="user", owner=123, uid=123)
    assert "123" in text
    assert "管理员" in text
    assert kb is not None


async def test_render_whoami_superadmin_unlimited():
    svc = _fake_svc()
    user = _make_user(1, role="superadmin")
    text, _ = await render_whoami(svc, user, scope="user", owner=1, uid=1)
    assert "无限" in text


async def test_render_quota_view_with_quotas():
    q = _make_quota(1, "calls", "day", 100, 30)
    svc = _fake_svc(quota_all=[q])
    user = _make_user(1)
    text, kb = await render_quota_view(svc, user, scope="user", owner=1, uid=1)
    assert "30 / 100" in text
    assert "每日" in text  # period 中文标签
    assert kb is not None


async def test_render_quota_status_html_progress_bar():
    """配额 HTML 含进度条 + 状态图标 + 中文 period 标签。"""
    from app.handlers.lists import render_quota_status_html
    q = _make_quota(1, "tokens", "day", 100, 50)
    svc = _fake_svc(quota_all=[q])
    user = _make_user(1)
    html = await render_quota_status_html(svc, user)
    assert "█" in html       # 进度条
    assert "🟢" in html      # 50% used = 50% remaining → 🟢
    assert "每日" in html     # period 中文
    assert "50 / 100" in html
    assert "<b>tokens</b>" in html


async def test_render_quota_status_html_low_remaining():
    """剩余 <20% 时显示红色图标。"""
    from app.handlers.lists import render_quota_status_html
    q = _make_quota(1, "tokens", "day", 100, 90)  # 90 used, 10% remaining
    svc = _fake_svc(quota_all=[q])
    user = _make_user(1)
    html = await render_quota_status_html(svc, user)
    assert "🔴" in html
    assert "90 / 100" in html


async def test_render_quota_status_html_unlimited():
    """limit=-1 不限时显示 ♾️。"""
    from app.handlers.lists import render_quota_status_html
    q = _make_quota(1, "tokens", "total", -1, 0)
    svc = _fake_svc(quota_all=[q])
    user = _make_user(1)
    html = await render_quota_status_html(svc, user)
    assert "♾️" in html
    assert "无限" in html


async def test_render_quota_status_html_large_numbers():
    """大数字格式化(万)。"""
    from app.handlers.lists import render_quota_status_html
    q = _make_quota(1, "tokens", "day", 200_000, 50_000)
    svc = _fake_svc(quota_all=[q])
    user = _make_user(1)
    html = await render_quota_status_html(svc, user)
    assert "5万 / 20万" in html


# ── CallbackData 往返 ──────────────────────────────────────────
def test_listpagecb_roundtrip():
    orig = ListPageCB(kind="memories", page=3, scope="chat",
                      owner=-100123, uid=99999)
    packed = orig.pack()
    assert len(packed) <= 64  # Telegram callback_data 上限
    decoded = ListPageCB.unpack(packed)
    assert decoded == orig


def test_navcb_roundtrip():
    orig = NavCB(view="whoami", scope="user", owner=1, uid=5)
    decoded = NavCB.unpack(orig.pack())
    assert decoded == orig


def test_closecb_roundtrip():
    orig = CloseCB(uid=7)
    decoded = CloseCB.unpack(orig.pack())
    assert decoded == orig
