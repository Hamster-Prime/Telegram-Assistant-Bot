"""MiniMax 对话 —— /v1/chat/completions(M3 多模态 + 流式 + 工具调用)。"""
from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from app.logging import get_logger
from app.minimax.client import MiniMaxClient

log = get_logger("minimax.chat")


@dataclass(slots=True)
class ToolCallDelta:
    """累积中的工具调用(流式分片拼装)。"""
    id: str = ""
    name: str = ""
    arguments: str = ""


@dataclass(slots=True)
class ChatStreamEvent:
    """归一化的流式事件。"""
    kind: str  # "content" | "reasoning" | "tool_calls" | "usage" | "finish"
    text: str = ""
    tool_calls: list[ToolCallDelta] = field(default_factory=list)
    total_tokens: int = 0
    finish_reason: str = ""


class ChatAPI:
    def __init__(self, client: MiniMaxClient, model: str = "MiniMax-M3") -> None:
        self._client = client
        self._model = model

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_completion_tokens: int = 8192,
        thinking: str = "adaptive",
    ) -> AsyncIterator[ChatStreamEvent]:
        """流式对话。产出归一化事件:正文增量 / 思考增量 / 工具调用 / usage / 结束。"""
        payload: dict[str, Any] = {
            "model": model or self._model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_completion_tokens": max_completion_tokens,
            "reasoning_split": True,
            "thinking": {"type": thinking},
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        log.info("对话流式请求", 模型=payload["model"], 消息数=len(messages),
                 工具数=len(tools or []), 思考模式=thinking)

        # 工具调用分片按 index 聚合
        pending_tools: dict[int, ToolCallDelta] = {}
        finish_reason = ""

        async for chunk in self._client.stream_sse("/chat/completions", payload):
            usage = chunk.get("usage")
            if usage and usage.get("total_tokens"):
                yield ChatStreamEvent(kind="usage", total_tokens=usage["total_tokens"])

            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}

                # 思考内容(reasoning_split=true)
                for rd in delta.get("reasoning_details") or []:
                    txt = rd.get("text", "")
                    if txt:
                        yield ChatStreamEvent(kind="reasoning", text=txt)

                # 正文
                content = delta.get("content")
                if content:
                    yield ChatStreamEvent(kind="content", text=content)

                # 工具调用分片
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    slot = pending_tools.setdefault(idx, ToolCallDelta())
                    if tc.get("id"):
                        slot.id = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot.name = fn["name"]
                    if fn.get("arguments"):
                        slot.arguments += fn["arguments"]

                fr = choice.get("finish_reason")
                if fr:
                    finish_reason = fr

        if finish_reason == "tool_calls" and pending_tools:
            calls = [pending_tools[i] for i in sorted(pending_tools)]
            log.info("模型发起工具调用", 工具数=len(calls),
                     工具列表=[c.name for c in calls])
            yield ChatStreamEvent(kind="tool_calls", tool_calls=calls,
                                  finish_reason=finish_reason)
        yield ChatStreamEvent(kind="finish", finish_reason=finish_reason or "stop")

    async def complete(self, messages: list[dict[str, Any]], *,
                       model: str | None = None,
                       max_completion_tokens: int = 2048) -> str:
        """非流式补全(用于摘要压缩、记忆抽取等内部调用)。"""
        payload = {
            "model": model or self._model,
            "messages": messages,
            "stream": False,
            "max_completion_tokens": max_completion_tokens,
            "thinking": {"type": "disabled"},
        }
        log.info("对话非流式请求", 模型=payload["model"], 消息数=len(messages))
        data = await self._client.post("/chat/completions", payload)
        choices = data.get("choices") or []
        if not choices:
            return ""
        msg = choices[0].get("message") or {}
        return msg.get("content") or ""
