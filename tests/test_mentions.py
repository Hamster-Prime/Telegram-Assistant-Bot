"""Mention stripping helpers."""
from __future__ import annotations

from app.handlers.mentions import contains_bot_mention, strip_bot_mention


def test_strip_bot_mention_removes_only_target_username():
    text = "@my_bot 帮我问问 @alice 今天的进度"

    assert strip_bot_mention(text, "my_bot") == "帮我问问 @alice 今天的进度"


def test_strip_bot_mention_is_case_insensitive():
    assert strip_bot_mention("@My_Bot ping @Other", "my_bot") == "ping @Other"


def test_strip_bot_mention_does_not_remove_prefix_match():
    text = "@my_bot_helper 帮我问 @my_bot 这件事"

    assert strip_bot_mention(text, "my_bot") == "@my_bot_helper 帮我问 这件事"


def test_contains_bot_mention_requires_exact_username():
    assert contains_bot_mention("@my_bot 帮忙", "my_bot")
    assert not contains_bot_mention("@my_bot_helper 帮忙", "my_bot")
