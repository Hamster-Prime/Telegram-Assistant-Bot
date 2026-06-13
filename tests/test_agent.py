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


async def test_show_thinking_renders_quote():
    chat = ScriptedChat([[
        ChatStreamEvent(kind="reasoning", text="思考中"),
        ChatStreamEvent(kind="content", text="答案"),
        ChatStreamEvent(kind="finish", finish_reason="stop"),
    ]])
    agent = Agent(chat)
    r = FakeRenderer()
    await agent.run([{"role": "user", "content": "x"}], r, ToolDispatcher(),
                    show_thinking=True)
    assert r.final.startswith("> 思考中")
    assert "答案" in r.final


async def test_preamble_preserved_across_tool_round():
    """工具调用前的前导语(如"好的,我来帮您查询")不能在工具轮后丢失。

    回归:此前每轮 full_text 重置,工具轮前导语被丢弃,最终只剩末轮文本,
    在 Guest 单 inline 消息 + persist=False 下表现为首句被截断。
    """
    chat = ScriptedChat([
        [  # 第一轮:先说前导语,再发起工具调用
            ChatStreamEvent(kind="content", text="好的,我来帮您查询。"),
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
    result = await agent.run([{"role": "user", "content": "几点了"}], r,
                             ToolDispatcher())

    # 前导语必须保留在最终结果里
    assert "好的,我来帮您查询。" in result.text
    assert "现在是白天" in result.text
    # 定稿文本同样保留前导语
    assert r.final is not None
    assert "好的,我来帮您查询。" in r.final
    assert "现在是白天" in r.final
    # 流式更新里也应该出现前导语
    assert any("好的,我来帮您查询。" in u for u in r.updates)
    # 入会话的 assistant 消息 content 仍是本轮原始前导语(不含续写)
    second = chat.seen_messages[1]
    assistant = next(m for m in second if m["role"] == "assistant")
    assert assistant["content"] == "好的,我来帮您查询。"


async def test_multiple_tool_rounds_accumulate_all_preamble():
    """连续两轮工具调用 → 两轮前导语都保留 + 末轮回答。"""
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
    assert result.text == "先查时间。再查一下。好了"
    assert r.final == "先查时间。再查一下。好了"
    assert result.tool_rounds == 2
