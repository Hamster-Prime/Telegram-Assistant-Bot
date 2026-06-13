"""Agent 主循环测试 —— 工具调用回灌、流式事件、错误兜底。"""
from __future__ import annotations

import pytest

from app.core.agent import Agent
from app.core.tools import ToolDispatcher
from app.minimax.chat import ChatStreamEvent, ToolCallDelta
from app.minimax.client import AllKeysFailedError


class FakeRenderer:
    def __init__(self):
        self.updates: list[str] = []
        self.final: str | None = None
        self.failed: str | None = None

    async def start(self): ...

    async def update(self, t: str):
        self.updates.append(t)

    async def finalize(self, t: str):
        self.final = t
        return 1

    async def fail(self, t: str):
        self.failed = t

    @property
    def last_rendered_text(self) -> str:
        return self.final or ""


class ScriptedChat:
    """按脚本回放流式事件;记录每轮收到的 messages。"""

    def __init__(self, rounds: list[list[ChatStreamEvent]]):
        self._rounds = rounds
        self.seen_messages: list[list[dict]] = []

    async def stream_chat(self, messages, *, tools=None, **kwargs):
        self.seen_messages.append(list(messages))
        events = self._rounds.pop(0)
        for ev in events:
            yield ev


async def test_plain_answer():
    chat = ScriptedChat([[
        ChatStreamEvent(kind="content", text="你好"),
        ChatStreamEvent(kind="content", text="世界"),
        ChatStreamEvent(kind="usage", total_tokens=42),
        ChatStreamEvent(kind="finish", finish_reason="stop"),
    ]])
    agent = Agent(chat)
    r = FakeRenderer()
    result = await agent.run([{"role": "user", "content": "hi"}], r, ToolDispatcher())
    assert r.final == "你好世界"
    assert result.total_tokens == 42
    assert result.tool_rounds == 0


async def test_tool_call_roundtrip():
    """第一轮发起工具调用 → 执行 → 回灌 → 第二轮续写。"""
    chat = ScriptedChat([
        [  # 第一轮:要求调用时间工具
            ChatStreamEvent(kind="tool_calls", finish_reason="tool_calls",
                            tool_calls=[ToolCallDelta(id="c1", name="get_current_time",
                                                      arguments="{}")]),
            ChatStreamEvent(kind="finish", finish_reason="tool_calls"),
        ],
        [  # 第二轮:基于工具结果回答
            ChatStreamEvent(kind="content", text="现在是白天"),
            ChatStreamEvent(kind="usage", total_tokens=10),
            ChatStreamEvent(kind="finish", finish_reason="stop"),
        ],
    ])
    agent = Agent(chat)
    r = FakeRenderer()
    result = await agent.run([{"role": "user", "content": "几点了"}], r, ToolDispatcher())

    assert result.tools_used == ["get_current_time"]
    assert result.tool_rounds == 1
    assert r.final == "现在是白天"
    # 第二轮请求里应包含 assistant(tool_calls) + tool 回灌消息
    second = chat.seen_messages[1]
    roles = [m["role"] for m in second]
    assert "tool" in roles
    tool_msg = next(m for m in second if m["role"] == "tool")
    assert "当前时间" in tool_msg["content"]


async def test_unknown_tool_feeds_error_back():
    chat = ScriptedChat([
        [
            ChatStreamEvent(kind="tool_calls", finish_reason="tool_calls",
                            tool_calls=[ToolCallDelta(id="c1", name="not_exist",
                                                      arguments="{}")]),
            ChatStreamEvent(kind="finish", finish_reason="tool_calls"),
        ],
        [
            ChatStreamEvent(kind="content", text="抱歉,该功能不可用"),
            ChatStreamEvent(kind="finish", finish_reason="stop"),
        ],
    ])
    agent = Agent(chat)
    r = FakeRenderer()
    await agent.run([{"role": "user", "content": "x"}], r, ToolDispatcher())
    tool_msg = next(m for m in chat.seen_messages[1] if m["role"] == "tool")
    assert "不可用" in tool_msg["content"]


async def test_all_keys_failed_before_output():
    """首块前全 Key 失败 → renderer.fail 输出中文用户报错。"""

    class FailingChat:
        async def stream_chat(self, messages, *, tools=None, **kwargs):
            raise AllKeysFailedError("/chat/completions", [
                {"key_index": 1, "key": "k1…", "attempt": 1, "error": "超时"},
                {"key_index": 1, "key": "k1…", "attempt": 2, "error": "超时"},
            ])
            yield  # pragma: no cover

    agent = Agent(FailingChat())
    r = FakeRenderer()
    result = await agent.run([{"role": "user", "content": "x"}], r, ToolDispatcher())
    assert r.failed is not None
    assert "API Key" in r.failed and "失败" in r.failed
    assert result.text == ""


async def test_partial_output_then_error_finalizes():
    """已产出部分内容后中断 → 定稿已收内容 + 错误说明。"""

    class HalfChat:
        async def stream_chat(self, messages, *, tools=None, **kwargs):
            yield ChatStreamEvent(kind="content", text="前半段")
            raise AllKeysFailedError("/chat/completions", [
                {"key_index": 1, "key": "k1…", "attempt": 1, "error": "断流"}])

    agent = Agent(HalfChat())
    r = FakeRenderer()
    result = await agent.run([{"role": "user", "content": "x"}], r, ToolDispatcher())
    assert result.text == "前半段"
    assert r.final and r.final.startswith("前半段")
    assert "失败" in r.final


async def test_reasoning_hidden_by_default():
    chat = ScriptedChat([[
        ChatStreamEvent(kind="reasoning", text="我想想…"),
        ChatStreamEvent(kind="content", text="答案"),
        ChatStreamEvent(kind="finish", finish_reason="stop"),
    ]])
    agent = Agent(chat)
    r = FakeRenderer()
    result = await agent.run([{"role": "user", "content": "x"}], r, ToolDispatcher())
    assert r.final == "答案"  # 思考不外显
    assert result.reasoning == "我想想…"


async def test_show_thinking_renders_expandable_blockquote():
    chat = ScriptedChat([[
        ChatStreamEvent(kind="reasoning", text="思考中"),
        ChatStreamEvent(kind="content", text="答案"),
        ChatStreamEvent(kind="finish", finish_reason="stop"),
    ]])
    agent = Agent(chat)
    r = FakeRenderer()
    await agent.run([{"role": "user", "content": "x"}], r, ToolDispatcher(),
                    show_thinking=True)
    assert r.final.startswith("<blockquote expandable>")
    assert "思考中" in r.final
    assert "答案" in r.final


async def test_preamble_replaced_by_final_answer_per_round():
    """覆盖式渲染:工具调用前的前导语在新轮开始时被覆盖,最终只留末轮完整答案。

    体验:第1轮显示"好的,我来帮您查询。" → 工具执行 → 第2轮覆盖为"现在是白天"。
    入 convo 的 assistant 消息仍按真实轮次内容(供模型看完整工具历史)。
    """
    chat = ScriptedChat([
        [  # 第一轮:先说前导语,再发起工具调用
            ChatStreamEvent(kind="content", text="好的,我来帮您查询。"),
            ChatStreamEvent(kind="tool_calls", finish_reason="tool_calls",
                            tool_calls=[ToolCallDelta(id="c1", name="get_current_time",
                                                      arguments="{}")]),
            ChatStreamEvent(kind="finish", finish_reason="tool_calls"),
        ],
        [  # 第二轮:基于工具结果回答(覆盖第1轮显示)
            ChatStreamEvent(kind="content", text="现在是白天"),
            ChatStreamEvent(kind="usage", total_tokens=10),
            ChatStreamEvent(kind="finish", finish_reason="stop"),
        ],
    ])
    agent = Agent(chat)
    r = FakeRenderer()
    result = await agent.run([{"role": "user", "content": "几点了"}], r,
                             ToolDispatcher())

    # 最终结果只含末轮答案(前导语已被覆盖,不累积)
    assert result.text == "现在是白天"
    assert "好的,我来帮您查询。" not in result.text
    # 定稿文本即末轮
    assert r.final == "现在是白天"
    # 流式更新里第1轮确实短暂显示过前导语(后被覆盖)
    assert any("好的,我来帮您查询。" in u for u in r.updates)
    # 入会话的 assistant 消息 content 仍是本轮原始前导语(模型需看完整工具历史)
    second = chat.seen_messages[1]
    assistant = next(m for m in second if m["role"] == "assistant")
    assert assistant["content"] == "好的,我来帮您查询。"


async def test_multiple_tool_rounds_overwrite_display():
    """连续两轮工具调用 → 每轮覆盖上一轮显示,最终只留末轮。"""
    chat = ScriptedChat([
        [
            ChatStreamEvent(kind="content", text="先查时间。"),
            ChatStreamEvent(kind="tool_calls", finish_reason="tool_calls",
                            tool_calls=[ToolCallDelta(id="c1", name="get_current_time",
                                                      arguments="{}")]),
            ChatStreamEvent(kind="finish", finish_reason="tool_calls"),
        ],
        [
            ChatStreamEvent(kind="content", text="再查一下。"),
            ChatStreamEvent(kind="tool_calls", finish_reason="tool_calls",
                            tool_calls=[ToolCallDelta(id="c2", name="get_current_time",
                                                      arguments="{}")]),
            ChatStreamEvent(kind="finish", finish_reason="tool_calls"),
        ],
        [
            ChatStreamEvent(kind="content", text="好了"),
            ChatStreamEvent(kind="finish", finish_reason="stop"),
        ],
    ])
    agent = Agent(chat)
    r = FakeRenderer()
    result = await agent.run([{"role": "user", "content": "x"}], r, ToolDispatcher())
    # 覆盖式:只留末轮
    assert result.text == "好了"
    assert r.final == "好了"
    assert result.tool_rounds == 2
