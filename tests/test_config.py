"""配置启动校验测试。"""
from __future__ import annotations

from app.config import Settings


def _settings(**kw) -> Settings:
    base = dict(
        _env_file=None,
        bot_token="123:abc",
        minimax_api_keys="k1",
        mode="webhook",
        webhook_host="https://bot.example.com",
        webhook_secret="a-strong-random-secret",
    )
    base.update(kw)
    return Settings(**base)


def test_startup_ok_with_strong_secret():
    assert _settings().validate_for_startup() == []


def test_startup_rejects_default_webhook_secret():
    errs = _settings(webhook_secret="secret").validate_for_startup()
    assert any("WEBHOOK_SECRET" in e for e in errs)


def test_startup_rejects_empty_webhook_secret():
    errs = _settings(webhook_secret="").validate_for_startup()
    assert any("WEBHOOK_SECRET" in e for e in errs)


def test_startup_requires_webhook_host_in_webhook_mode():
    errs = _settings(webhook_host="").validate_for_startup()
    assert any("WEBHOOK_HOST" in e for e in errs)


def test_polling_mode_ignores_webhook_secret():
    """polling 模式不校验 webhook secret/host。"""
    errs = _settings(mode="polling", webhook_secret="secret",
                     webhook_host="").validate_for_startup()
    assert errs == []


def test_startup_requires_bot_token():
    errs = _settings(bot_token="").validate_for_startup()
    assert any("BOT_TOKEN" in e for e in errs)


def test_startup_requires_minimax_keys():
    errs = _settings(minimax_api_keys="", minimax_api_key="").validate_for_startup()
    assert any("MINIMAX_API_KEYS" in e for e in errs)
