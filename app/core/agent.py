"""Agent 主循环 —— M3 流式 + tool-calling 调度(plan §13)。

流程:M3 流式 → finish_reason=tool_calls 则执行工具 → role:tool 回灌 → 续写 → 定稿。
- 同步工具直接 await;异步生成工具内部只"提交后台 + 返回占位话术"。
- 流式渲染经 StreamRenderer 抽象,与场景无关。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.streaming import StreamRenderer
from app.core.tools import TOOL_SCHEMAS, ToolDispatcher
from app.logging import get_logger
from app.minimax.chat import ChatAPI
from app.minimax.client import AllKeysFailedError, MiniMaxError

log = get_logger("core.agent")

MAX_TOOL_ROUNDS = 6  # 防工具循环失控


@dataclass(slots=True)
class AgentResult:
    text: str = ""
    reasoning: str = ""
    total_tokens: int = 0
    tool_rounds: int = 0
    tools_used: list[str] = field(default_factory=list)


class Agent:
    def __init__(self, chat_api: ChatAPI) -> None:
        self._chat = chat_api

    async def run(
        self,
        messages: list[dict[str, Any]],
        renderer: StreamRenderer,
        dispatcher: ToolDispatcher,
        *,
        tools: list[dict[str, Any]] | None = None,
        show_thinking: bool = False,
    ) -> AgentResult:
        """执行一轮 Agent 对话。renderer 已 start();本方法负责 update + finalize。"""
        result = AgentResult()
        convo = list(messages)
        tools = tools if tools is not None else TOOL_SCHEMAS
        # 覆盖式渲染:每轮工具调用后,新文本覆盖上一轮显示(不累积前导语)。
        # 用户体验:第1轮"我去搜一下" → 工具执行 → 第2轮覆盖为"查找到..." →
        # 最终覆盖为完整答案。每段在其展示期内完整(由 finalize 防弹化保证),
        # 最终消息即末轮完整文本。各轮 assistant 消息仍按真实内容入 convo(供
        # 模型下一轮看到完整工具调用历史),仅「用户可见的流式显示」是覆盖式。

        for round_no in range(1, MAX_TOOL_ROUNDS + 2):
            full_text = ""
            reasoning_text = ""
            pending_calls = []
            finish = ""

            try:
                async for ev in self._chat.stream_chat(convo, tools=tools):
                    if ev.kind == "content":
                        full_text += ev.text
                        display = full_text
                        if show_thinking and reasoning_text:
                            display = (
                                f"<blockquote expandable>\n{reasoning_text}"
                                f"\n</blockquote>\n\n{full_text}"
                            )
                        await renderer.update(display)
                    elif ev.kind == "reasoning":
                        reasoning_text += ev.text
                    elif ev.kind == "tool_calls":
                        pending_calls = ev.tool_calls
                        finish = "tool_calls"
                    elif ev.kind == "usage":
                        result.total_tokens += ev.total_tokens
                    elif ev.kind == "finish" and not finish:
                        finish = ev.finish_reason
            except (AllKeysFailedError, MiniMaxError) as e:
                # 已流出的部分尽量定稿,再附错误说明
                user_msg = e.user_message() if isinstance(e, (AllKeysFailedError, MiniMaxError)) else str(e)
                log.error("Agent对话中断", 轮次=round_no, 异常类型=type(e).__name__,
                          已收文本长度=len(full_text), 报错=user_msg)
                if full_text.strip():
                    await renderer.finalize(full_text + f"\n\n{user_msg}")
                else:
                    await renderer.fail(user_msg)
                result.text = full_text
                result.reasoning = reasoning_text
                return result

            result.reasoning += reasoning_text

            if finish == "tool_calls" and pending_calls:
                if round_no > MAX_TOOL_ROUNDS:
                    log.warning("工具调用轮次超限,强制收尾", 轮次=round_no)
                    combined = full_text or ""
                    await renderer.finalize(combined or "(工具调用轮次过多,已中止)")
                    result.text = combined
                    return result
                result.tool_rounds += 1
                # assistant 消息(含 tool_calls)入会话 —— 供模型下一轮看到完整工具历史
                convo.append({
                    "role": "assistant",
                    "content": full_text or None,
                    "tool_calls": [
                        {
                            "id": tc.id or f"call_{i}",
                            "type": "function",
                            "function": {"name": tc.name, "arguments": tc.arguments or "{}"},
                        }
                        for i, tc in enumerate(pending_calls)
                    ],
                })
                # 逐个执行工具回灌
                for i, tc in enumerate(pending_calls):
                    result.tools_used.append(tc.name)
                    log.info("Agent执行工具", 轮次=round_no, 工具=tc.name,
                             参数=tc.arguments[:200])
                    tool_result = await dispatcher.dispatch(tc.name, tc.arguments)
                    convo.append({
                        "role": "tool",
                        "tool_call_id": tc.id or f"call_{i}",
                        "content": tool_result,
                    })
                continue  # 续写下一轮(新 full_text 覆盖上一轮显示)

            # 正常结束
            result.text = full_text
            display = full_text
            if show_thinking and result.reasoning:
                display = (
                    f"<blockquote expandable>\n{result.reasoning}"
                    f"\n</blockquote>\n\n{full_text}"
                )
            await renderer.finalize(display or "(空回复)")
            # 终稿对账:末次编辑可能因限流/HTML错误未落地,消息停在较短中间状态。
            # 若 renderer 记录的最后渲染文本与最终文本不一致,强制再定稿一次。
            last_rendered = getattr(renderer, "last_rendered_text", "")
            reconciled = False
            if last_rendered and last_rendered != display.strip():
                log.warning("终稿对账:末次编辑疑似未落地,强制重发",
                            轮次=round_no, 期望长度=len(display),
                            实际渲染长度=len(last_rendered))
                await renderer.finalize(display or "(空回复)")
                reconciled = True
            log.info("Agent对话完成", 轮次=round_no, 工具轮数=result.tool_rounds,
                     使用工具=result.tools_used or "无", 回复长度=len(full_text),
                     Token用量=result.total_tokens, 终稿对账="重发" if reconciled else "一致")
            return result

        result.text = ""
        await renderer.fail("(对话异常结束)")
        return result
