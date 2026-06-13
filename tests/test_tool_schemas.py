"""Tool schema prompt behavior tests."""
from __future__ import annotations

from app.core.tools import TOOL_SCHEMAS


def _tool_description(name: str) -> str:
    for schema in TOOL_SCHEMAS:
        fn = schema["function"]
        if fn["name"] == name:
            return fn["description"]
    raise AssertionError(f"missing tool schema: {name}")


def test_web_search_description_forces_explicit_search_requests():
    desc = _tool_description("web_search")

    assert "必须调用" in desc
    assert "最新" in desc
    assert "搜一下" in desc
    assert "不要只承诺搜索" in desc
