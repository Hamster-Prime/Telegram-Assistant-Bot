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
    # 强化:知识截止日期 + 激进搜索
    assert "截止日期" in desc
    assert "现实世界实体" in desc


def test_web_fetch_description_guides_usage():
    """web_fetch 描述应指引用户在搜索后核对正文,而非凭摘要猜测。"""
    desc = _tool_description("web_fetch")

    assert "核对" in desc
    assert "正文" in desc
    assert "不要凭搜索摘要猜测" in desc


def test_generation_tools_forbid_faking():
    """所有生成类工具描述必须含反伪造条款(禁止文字扮演 + 要求实际调用)。"""
    generation_tools = [
        "generate_image",
        "generate_video",
        "synthesize_speech",
        "generate_music",
        "clone_voice",
        "design_voice",
    ]
    for name in generation_tools:
        desc = _tool_description(name)
        assert "禁止" in desc, f"{name} 描述缺少'禁止'反伪造条款"
        assert "扮演" in desc or "实际调用" in desc, (
            f"{name} 描述缺少'扮演/实际调用'约束")
        assert "实际调用" in desc, f"{name} 描述缺少'实际调用'要求"


def test_generate_video_description_targets_async_faking():
    """generate_video 特别禁止'已开始生成/后台生成中'这类伪造话术。"""
    desc = _tool_description("generate_video")
    assert "后台生成中" in desc
    assert "tool_call" in desc
