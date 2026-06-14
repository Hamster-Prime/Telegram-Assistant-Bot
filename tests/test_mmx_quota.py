"""MiniMax Token Plan 配额查询测试(/mmxquota)。

覆盖:
- render_mmx_quota 纯函数:状态图标 / 进度条(剩余填充)/ 不限量 / 耗尽 / 空列表 /
  total>0 显示「已用 n/m」 vs total=0 显示「剩余 X%」
- _parse_one:JSON → QuotaRemain(remaining_pct 保留原值不反转 / boost permille)
- 真实 API 响应样本(general total=0 + video total>0)端到端渲染
- on_mmx_quota 回调:非发起人/未授权/非管理员被拒;key_idx=-1 返回选择键盘;
  越界拒绝;成功 edit_text;MiniMaxError 显示 user_message
- mmx_quota_kb / mmx_quota_result_kb 键盘构造
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.db.models import User
from app.handlers.callbacks import on_mmx_quota
from app.handlers.lists import (
    MmxQuotaCB,
    mmx_quota_kb,
    mmx_quota_result_kb,
    render_mmx_quota,
)
from app.minimax.client import MiniMaxError
from app.minimax.quota import QuotaAPI, QuotaRemain, _parse_one


# ── 测试数据 ────────────────────────────────────────────────────
def _make_user(uid: int = 1, *, role: str = "admin", allowed: bool = True) -> User:
    return User(
        tg_id=uid,
        username="u",
        first_name="U",
        role=role,
        authorized=1 if allowed else 0,
        authorized_by=None,
        authorized_at=None,
        settings="{}",
        created_at=0,
        updated_at=0,
    )


def _make_remain(
    *,
    model: str = "general",
    i_status: int = 1,
    w_status: int = 1,
    i_remaining: float = 80.0,
    w_remaining: float = 90.0,
    i_total: int = 0,
    i_usage: int = 0,
    w_total: int = 0,
    w_usage: int = 0,
    boost: int = 1000,
    i_remains_ms: int = 1800000,
    w_remains_ms: int = 86400000,
) -> QuotaRemain:
    """便捷构造 QuotaRemain;字段语义为「剩余」百分比(服务端原值,不反转)。"""
    return QuotaRemain(
        model_name=model,
        interval_total=i_total,
        interval_usage=i_usage,
        interval_remaining_pct=i_remaining,
        interval_status=i_status,
        weekly_total=w_total,
        weekly_usage=w_usage,
        weekly_remaining_pct=w_remaining,
        weekly_status=w_status,
        interval_start=0,
        interval_end=0,
        weekly_start=0,
        weekly_end=0,
        interval_remains_ms=i_remains_ms,
        weekly_remains_ms=w_remains_ms,
        weekly_boost_permille=boost,
    )


class FakeMessage:
    def __init__(self):
        self.edit_text = AsyncMock()
        self.delete = AsyncMock()
        self.answer = AsyncMock()


class FakeCallbackQuery:
    def __init__(self, from_id: int, message: FakeMessage | None = None):
        self.from_user = SimpleNamespace(id=from_id)
        self.message = message or FakeMessage()
        self.answer = AsyncMock()


def _fake_svc(
    *,
    user: User | None = None,
    keys: list[str] | None = None,
    remains: list[QuotaRemain] | None = None,
    raises: Exception | None = None,
):
    """Services 替身:daos.users.get + settings.minimax_keys + quota_api.remains。"""
    daos = SimpleNamespace(
        users=SimpleNamespace(get=AsyncMock(return_value=user)),
    )
    if keys is None:
        keys = ["sk-test1234567890"]
    settings = SimpleNamespace(minimax_keys=keys)
    quota_api = SimpleNamespace(remains=AsyncMock())
    if raises is not None:
        quota_api.remains.side_effect = raises
    else:
        quota_api.remains.return_value = remains or []
    return SimpleNamespace(daos=daos, settings=settings, quota_api=quota_api)


# ── render_mmx_quota 纯函数 ─────────────────────────────────────
def test_render_normal_status_percent_mode():
    """total=0(按百分比计,如 general):显示「剩余 X%」,不显示 0/0。"""
    r = _make_remain(i_remaining=98.0, w_remaining=97.0, i_total=0, w_total=0)
    html = render_mmx_quota(0, "sk-test1234567890", [r])
    assert "🟢" in html
    assert "剩余 98%" in html  # 5h 窗口按百分比
    assert "剩余 97%" in html  # 周窗口按百分比
    assert "0/0" not in html  # 关键:不再显示 0/0
    assert "通用额度" in html  # 中文标签


def test_render_normal_status_count_mode():
    """total>0(按次计,如 video):显示「已用 usage/total」。"""
    r = _make_remain(
        model="video",
        i_total=3,
        i_usage=0,
        w_total=21,
        w_usage=2,
        i_remaining=100.0,
        w_remaining=90.0,
    )
    html = render_mmx_quota(0, "sk-test1234567890", [r])
    assert "视频" in html
    assert "已用 0/3" in html  # 5h 窗口
    assert "已用 2/21" in html  # 周窗口


def test_render_exhausted_status():
    """已耗尽状态(status=2):🔴 图标。"""
    r = _make_remain(i_status=2, w_status=2, i_remaining=0, w_remaining=0)
    html = render_mmx_quota(0, "sk-test1234567890", [r])
    assert "🔴" in html


def test_render_unlimited_status():
    """不限量状态(status=3):♾️ 图标 + 不限量文本。"""
    r = _make_remain(i_status=3, w_status=3)
    html = render_mmx_quota(0, "sk-test1234567890", [r])
    assert "♾️" in html
    assert "不限量" in html


def test_render_empty_remains():
    """空列表:显示提示文本,不崩溃。"""
    html = render_mmx_quota(0, "sk-test1234567890", [])
    assert "暂无可用资源" in html


def test_render_escapes_model_name():
    """模型名含 HTML 特殊字符时被转义(未知模型保留原名)。"""
    r = _make_remain(model="<script>x</script>")
    html = render_mmx_quota(0, "sk-test1234567890", [r])
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_render_multiple_models():
    """多个模型:每个独立渲染,以空行分隔。"""
    r1 = _make_remain(model="general")
    r2 = _make_remain(model="video", i_total=3, i_usage=1, w_total=21, w_usage=5)
    html = render_mmx_quota(1, "sk-test1234567890", [r1, r2])
    assert "通用额度" in html
    assert "视频" in html
    assert "账号2" in html  # key_idx=1 → 账号2


def test_render_boost_permille_applies_to_weekly_pct():
    """weekly_boost_permille=1500 → 周窗口显示百分比放大 1.5 倍。

    general(total=0)按百分比显示:97% × 1.5 = 145.5% → round() = 146。
    """
    r = _make_remain(
        model="general",
        i_total=0,
        w_total=0,
        w_remaining=97.0,
        boost=1500,
    )
    html = render_mmx_quota(0, "sk-test1234567890", [r])
    assert "剩余 146%" in html  # round(97*1.5)=round(145.5)=146(banker's rounding)


def test_render_boost_does_not_affect_count_display():
    """total>0(video)时 boost 仅影响进度条,不影响「已用 n/m」计数显示。"""
    r = _make_remain(
        model="video",
        i_total=3,
        i_usage=0,
        w_total=21,
        w_usage=2,
        w_remaining=90.0,
        boost=1500,
    )
    html = render_mmx_quota(0, "sk-test1234567890", [r])
    assert "已用 2/21" in html  # 计数不变


def test_render_reset_countdown():
    """重置倒计时用 remains_time(毫秒),显示「X时Y分后」。"""
    r = _make_remain(i_remains_ms=2 * 3600 * 1000 + 30 * 60 * 1000)  # 2h30m
    html = render_mmx_quota(0, "sk-test1234567890", [r])
    assert "2时30分后" in html


def test_render_week_range():
    """渲染周周期范围行(取首条 weekly_start/end)。"""
    r = QuotaRemain(
        model_name="general",
        interval_total=0,
        interval_usage=0,
        interval_remaining_pct=98.0,
        interval_status=1,
        weekly_total=0,
        weekly_usage=0,
        weekly_remaining_pct=97.0,
        weekly_status=1,
        interval_start=0,
        interval_end=0,
        weekly_start=1780848000.0,
        weekly_end=1781452800.0,
        interval_remains_ms=0,
        weekly_remains_ms=0,
        weekly_boost_permille=1500,
    )
    html = render_mmx_quota(0, "sk-test1234567890", [r])
    assert "周期" in html


def test_bar_function():
    """进度条:0%全空,100%全满,50%半满(按剩余填充)。"""
    from app.handlers.lists import _bar

    assert _bar(0) == "░" * 10
    assert _bar(100) == "█" * 10
    assert _bar(50) == "█████" + "░" * 5
    # boost 后 >100% → 全满(不溢出)
    assert _bar(150) == "█" * 10


# ── _parse_one JSON 解析 ────────────────────────────────────────
def test_parse_one_normal():
    """正常解析:remaining_percent 保留原值(不反转)。"""
    raw = {
        "model_name": "general",
        "current_interval_total_count": 0,
        "current_interval_usage_count": 0,
        "current_interval_remaining_percent": 98.0,
        "current_interval_status": 1,
        "current_weekly_total_count": 0,
        "current_weekly_usage_count": 0,
        "current_weekly_remaining_percent": 97.0,
        "current_weekly_status": 1,
        "start_time": 1700000000000,
        "end_time": 1700000000000,
        "weekly_start_time": 1700000000000,
        "weekly_end_time": 1700000000000,
        "remains_time": 1800000,
        "weekly_remains_time": 86400000,
        "weekly_boost_permille": 1500,
    }
    r = _parse_one(raw)
    assert r.model_name == "general"
    assert r.interval_remaining_pct == 98.0  # 保留原值,不反转
    assert r.weekly_remaining_pct == 97.0
    assert r.interval_status == 1
    assert r.interval_end == 1700000000.0  # ms→s
    assert r.interval_remains_ms == 1800000
    assert r.weekly_remains_ms == 86400000
    assert r.weekly_boost_permille == 1500


def test_parse_one_infers_remaining_from_counts():
    """remaining_percent 缺失时,由 (total-usage)/total 推算剩余。"""
    raw = {
        "model_name": "video",
        "current_interval_total_count": 3,
        "current_interval_usage_count": 1,
        "current_weekly_total_count": 21,
        "current_weekly_usage_count": 2,
    }
    r = _parse_one(raw)
    assert r.interval_remaining_pct == 66.66666666666666  # (3-1)/3*100
    assert r.weekly_remaining_pct == 90.47619047619048    # (21-2)/21*100


def test_parse_one_missing_fields_default():
    """缺失字段不崩溃,给默认值。"""
    r = _parse_one({"model_name": "x"})
    assert r.model_name == "x"
    assert r.interval_total == 0
    assert r.interval_status == 1  # 默认
    assert r.interval_end == 0.0
    assert r.interval_remains_ms == 0


def test_parse_one_boost_permille():
    """weekly_boost_permille 正确解析。"""
    r = _parse_one({"model_name": "x", "weekly_boost_permille": 1500})
    assert r.weekly_boost_permille == 1500


# ── 真实 API 响应样本端到端测试 ─────────────────────────────────
_REAL_RESPONSE = {
    "model_remains": [
        {
            "start_time": 1781402400000,
            "end_time": 1781420400000,
            "remains_time": 2774457,
            "current_interval_total_count": 0,
            "current_interval_usage_count": 0,
            "model_name": "general",
            "current_weekly_total_count": 0,
            "current_weekly_usage_count": 0,
            "weekly_start_time": 1780848000000,
            "weekly_end_time": 1781452800000,
            "weekly_remains_time": 35174457,
            "current_interval_status": 1,
            "current_interval_remaining_percent": 98,
            "current_weekly_status": 1,
            "current_weekly_remaining_percent": 97,
            "weekly_boost_permille": 1500,
        },
        {
            "start_time": 1781366400000,
            "end_time": 1781452800000,
            "remains_time": 35174457,
            "current_interval_total_count": 3,
            "current_interval_usage_count": 0,
            "model_name": "video",
            "current_weekly_total_count": 21,
            "current_weekly_usage_count": 2,
            "weekly_start_time": 1780848000000,
            "weekly_end_time": 1781452800000,
            "weekly_remains_time": 35174457,
            "current_interval_status": 1,
            "current_interval_remaining_percent": 100,
            "current_weekly_status": 1,
            "current_weekly_remaining_percent": 90,
        },
    ],
}


def test_real_response_parse_and_render():
    """用真实 API 响应样本验证:general(百分比) + video(计数) 双模式正确渲染。

    这是回归测试:之前 general 显示 0/0 的 bug 必须不再出现。
    """
    remains = [_parse_one(r) for r in _REAL_RESPONSE["model_remains"]]
    assert len(remains) == 2

    # general: total=0 → 按百分比
    g = remains[0]
    assert g.model_name == "general"
    assert g.interval_total == 0
    assert g.interval_remaining_pct == 98.0
    assert g.weekly_remaining_pct == 97.0
    assert g.weekly_boost_permille == 1500

    # video: total>0 → 按计数
    v = remains[1]
    assert v.model_name == "video"
    assert v.interval_total == 3
    assert v.interval_usage == 0
    assert v.weekly_total == 21
    assert v.weekly_usage == 2

    # 渲染:验证关键文本
    html = render_mmx_quota(0, "sk-test1234567890", remains)
    # general 不应再出现 0/0,应显示「剩余 98%」
    assert "剩余 98%" in html
    assert "剩余 145%" in html or "剩余 146%" in html  # 97*1.5=145.5 → round
    # video 应显示计数
    assert "已用 0/3" in html
    assert "已用 2/21" in html
    # 中文标签
    assert "通用额度" in html
    assert "视频" in html
    # 关键:0/0 绝不出现
    assert "0/0" not in html


# ── QuotaAPI.remains 集成 ───────────────────────────────────────
async def test_quota_api_remains_calls_get_with_key():
    """QuotaAPI.remains 调用 client.get_with_key 并解析 model_remains。"""
    client = SimpleNamespace(
        get_with_key=AsyncMock(
            return_value={
                "model_remains": [
                    {
                        "model_name": "M1",
                        "current_interval_total_count": 100,
                        "current_interval_usage_count": 10,
                    },
                    {
                        "model_name": "M2",
                        "current_interval_total_count": 200,
                        "current_interval_usage_count": 20,
                    },
                ],
            }
        ),
    )
    api = QuotaAPI(client)
    result = await api.remains("sk-test")
    client.get_with_key.assert_awaited_once_with("sk-test", "/token_plan/remains")
    assert len(result) == 2
    assert result[0].model_name == "M1"
    assert result[1].model_name == "M2"


async def test_quota_api_remains_empty():
    """model_remains 为空 → 返回空列表。"""
    client = SimpleNamespace(get_with_key=AsyncMock(return_value={}))
    api = QuotaAPI(client)
    result = await api.remains("sk-test")
    assert result == []


# ── on_mmx_quota 回调 ───────────────────────────────────────────
async def test_callback_rejects_non_owner():
    """非发起人点击 → show_alert,不编辑。"""
    cb = FakeCallbackQuery(from_id=888)
    cb_data = MmxQuotaCB(key_idx=0, uid=1)
    svc = _fake_svc(user=_make_user(1))
    await on_mmx_quota(cb, cb_data, svc)
    cb.answer.assert_awaited_once()
    assert cb.answer.call_args.kwargs.get("show_alert") is True
    cb.message.edit_text.assert_not_awaited()


async def test_callback_rejects_unauthorized():
    """发起人授权失效 → 拒绝(用 user 角色避免 is_admin 干扰)。"""
    cb = FakeCallbackQuery(from_id=1)
    cb_data = MmxQuotaCB(key_idx=0, uid=1)
    svc = _fake_svc(user=_make_user(1, role="user", allowed=False))
    await on_mmx_quota(cb, cb_data, svc)
    assert cb.answer.call_args.kwargs.get("show_alert") is True
    cb.message.edit_text.assert_not_awaited()


async def test_callback_rejects_non_admin():
    """普通用户(非管理员)→ 拒绝。"""
    cb = FakeCallbackQuery(from_id=1)
    cb_data = MmxQuotaCB(key_idx=0, uid=1)
    svc = _fake_svc(user=_make_user(1, role="user"))
    await on_mmx_quota(cb, cb_data, svc)
    assert cb.answer.call_args.kwargs.get("show_alert") is True
    cb.message.edit_text.assert_not_awaited()


async def test_callback_key_idx_negative_shows_selector():
    """key_idx=-1(返回选择)→ 显示账号选择键盘。"""
    cb = FakeCallbackQuery(from_id=1)
    cb_data = MmxQuotaCB(key_idx=-1, uid=1)
    svc = _fake_svc(user=_make_user(1), keys=["sk-aaa1234", "sk-bbb5678"])
    await on_mmx_quota(cb, cb_data, svc)
    cb.message.edit_text.assert_awaited_once()
    text = cb.message.edit_text.call_args.args[0]
    assert "选择" in text
    kb = cb.message.edit_text.call_args.kwargs.get("reply_markup")
    assert kb is not None
    # 2 个账号 + 1 个关闭行
    assert len(kb.inline_keyboard) == 3
    cb.answer.assert_awaited_once()


async def test_callback_key_idx_out_of_range():
    """key_idx 越界(配置已变更)→ 拒绝。"""
    cb = FakeCallbackQuery(from_id=1)
    cb_data = MmxQuotaCB(key_idx=5, uid=1)
    svc = _fake_svc(user=_make_user(1), keys=["sk-only"])
    await on_mmx_quota(cb, cb_data, svc)
    assert cb.answer.call_args.kwargs.get("show_alert") is True
    assert "失效" in cb.answer.call_args.args[0]
    svc.quota_api.remains.assert_not_awaited()


async def test_callback_success_edits_message():
    """管理员查询成功 → edit_text 被调用,内容为 HTML 渲染结果。"""
    cb = FakeCallbackQuery(from_id=1)
    cb_data = MmxQuotaCB(key_idx=0, uid=1)
    remains = [_make_remain()]
    svc = _fake_svc(user=_make_user(1), keys=["sk-test1234567890"], remains=remains)
    await on_mmx_quota(cb, cb_data, svc)
    svc.quota_api.remains.assert_awaited_once_with("sk-test1234567890")
    cb.message.edit_text.assert_awaited_once()
    kwargs = cb.message.edit_text.call_args.kwargs
    assert kwargs.get("parse_mode") == "HTML"
    assert "Token Plan" in cb.message.edit_text.call_args.args[0]
    cb.answer.assert_awaited_once()


async def test_callback_minimax_error_shows_user_message():
    """查询抛 MiniMaxError → edit_text 显示 user_message。"""
    cb = FakeCallbackQuery(from_id=1)
    cb_data = MmxQuotaCB(key_idx=0, uid=1)
    err = MiniMaxError(1004, "鉴权失败")
    svc = _fake_svc(user=_make_user(1), keys=["sk-bad"], raises=err)
    await on_mmx_quota(cb, cb_data, svc)
    cb.message.edit_text.assert_awaited_once()
    text = cb.message.edit_text.call_args.args[0]
    assert "鉴权" in text or "MiniMax" in text  # user_message 内容


async def test_callback_no_keys_alerts():
    """未配置 API Key → 拒绝。"""
    cb = FakeCallbackQuery(from_id=1)
    cb_data = MmxQuotaCB(key_idx=0, uid=1)
    svc = _fake_svc(user=_make_user(1), keys=[])
    await on_mmx_quota(cb, cb_data, svc)
    assert cb.answer.call_args.kwargs.get("show_alert") is True
    svc.quota_api.remains.assert_not_awaited()


# ── 键盘构造 ────────────────────────────────────────────────────
def test_mmx_quota_kb_buttons():
    """选择键盘:每个 Key 一行 + 关闭行。"""
    keys = ["sk-aaa1234567890", "sk-bbb1234567890"]
    kb = mmx_quota_kb(keys, uid=1)
    # 2 个账号行 + 1 个关闭行
    assert len(kb.inline_keyboard) == 3
    assert "账号1" in kb.inline_keyboard[0][0].text
    assert "账号2" in kb.inline_keyboard[1][0].text
    assert "关闭" in kb.inline_keyboard[2][0].text


def test_mmx_quota_result_kb_single_account():
    """单账号:仅刷新 + 关闭(无返回选择)。"""
    kb = mmx_quota_result_kb(0, keys_count=1, uid=1)
    assert len(kb.inline_keyboard) == 2  # 刷新行 + 关闭行
    assert "刷新" in kb.inline_keyboard[0][0].text
    assert "关闭" in kb.inline_keyboard[1][0].text


def test_mmx_quota_result_kb_multi_account():
    """多账号:刷新 + 返回选择 + 关闭。"""
    kb = mmx_quota_result_kb(0, keys_count=3, uid=1)
    assert len(kb.inline_keyboard[0]) == 2  # 刷新 + 返回选择
    assert "刷新" in kb.inline_keyboard[0][0].text
    assert "选择账号" in kb.inline_keyboard[0][1].text
    assert "关闭" in kb.inline_keyboard[1][0].text
