"""Helpers for Telegram mention handling."""
from __future__ import annotations

import re


def strip_bot_mention(text: str, bot_username: str) -> str:
    """Remove mentions of this bot while keeping other user mentions intact."""
    pattern = _bot_mention_pattern(bot_username)
    if pattern is None:
        return text.strip()

    stripped = pattern.sub("", text)
    stripped = re.sub(r"[ \t]{2,}", " ", stripped)
    stripped = re.sub(r" *\n *", "\n", stripped)
    return stripped.strip()


def contains_bot_mention(text: str, bot_username: str) -> bool:
    """Return whether text contains an exact mention of this bot."""
    pattern = _bot_mention_pattern(bot_username)
    return bool(pattern and pattern.search(text))


def _bot_mention_pattern(bot_username: str) -> re.Pattern[str] | None:
    username = bot_username.lstrip("@")
    if not username:
        return None
    return re.compile(rf"@{re.escape(username)}(?![A-Za-z0-9_])", re.IGNORECASE)
