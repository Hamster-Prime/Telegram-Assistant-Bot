"""回复/引用上下文折叠 —— 把被回复或被引用的消息拼进本轮 content(plan §12)。

Guest/私聊/群聊三场景共用。Guest 无历史,这是其唯一上下文来源;
私聊虽有历史,但「回复/引用」是用户的强语义信号,必须显式带进 content,
否则模型只能看到主消息文本,丢失用户意图(修复项)。
"""
from __future__ import annotations

from typing import Any

from aiogram.types import Message

from app.handlers.media import build_content
from app.logging import get_logger
from app.services import Services

log = get_logger("handlers.replies")


def _as_blocks(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return list(content)


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(
        str(block.get("text", ""))
        for block in content
        if block.get("type") == "text" and block.get("text")
    )


def _with_reply_context(
    content: Any,
    question_text: str,
    reply_content: Any | None,
    reply_text: str,
) -> Any:
    """合并:引用消息(文本+媒体)+ 召唤者的问题(文本+媒体)。

    文本上前置 [引用的消息] / [召唤者的问题] 两段标记;
    媒体块去重后追加(回复原文里的文本不重复拼)。
    """
    blocks: list[dict[str, Any]] = []
    text_parts: list[str] = []
    if reply_text:
        text_parts.append(f"[引用的消息]\n{reply_text}")

    text_parts.append(f"[召唤者的问题]\n{question_text}")
    blocks.append({"type": "text", "text": "\n\n".join(text_parts)})

    if reply_content is not None:
        for block in _as_blocks(reply_content):
            # 引用消息的文本已前置拼好,这里跳过,避免重复
            if block.get("type") == "text" and reply_text:
                continue
            blocks.append(block)

    blocks.extend(block for block in _as_blocks(content) if block.get("type") != "text")

    if len(blocks) == 1 and blocks[0].get("type") == "text":
        return blocks[0]["text"]
    return blocks


def _reply_source(message: Message) -> Any | None:
    """被回复/被引用的来源消息:同会话 reply_to_message 或跨会话 external_reply。"""
    return message.reply_to_message or getattr(message, "external_reply", None)


def _reply_quote_text(message: Message, reply: Any) -> str:
    """external_reply 场景下用户选中的引用片段(quote);同会话 reply 无 quote。"""
    if reply is message.reply_to_message:
        return ""
    quote = getattr(message, "quote", None)
    return getattr(quote, "text", "") or ""


async def fold_reply_context(
    svc: Services,
    message: Message,
    content: Any,
    query_text: str,
) -> tuple[Any, str]:
    """若该消息是对某条消息的回复/引用,把被引用消息拼进 content。

    返回 (新 content, 新 query_text)。无引用时原样返回。
    只清理当前主消息,不改被引用原文(原文里的 @bot 等保持不变)。
    """
    reply = _reply_source(message)
    if not reply:
        return content, query_text

    reply_content, _reply_query = await build_content(svc, reply)
    reply_text = _content_text(reply_content) if reply_content is not None else ""
    if not reply_text:
        reply_text = _reply_quote_text(message, reply)
    if reply_content is None and not reply_text:
        return content, query_text

    new_content = _with_reply_context(content, query_text, reply_content, reply_text)
    if reply_text:
        new_query = f"[引用的消息]\n{reply_text}\n\n[召唤者的问题]\n{query_text}"
    else:
        new_query = f"[召唤者的问题]\n{query_text}"
    return new_content, new_query
