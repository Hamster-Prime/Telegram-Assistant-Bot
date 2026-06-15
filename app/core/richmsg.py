"""Rich Message 封装 —— Bot API 10.1 sendRichMessage 薄包装。

LLM 直接输出 Rich Markdown(GFM 兼容子集),本模块负责:
- 构造 InputRichMessage(markdown=...)
- 字符裁剪(32KB Rich Message 限制)
- 纯文本降级辅助

Rich Markdown 文档:https://core.telegram.org/bots/api#rich-message-formatting-options
"""
from __future__ import annotations

from aiogram.types import InputRichMessage

RICH_MESSAGE_LIMIT = 32_768  # Rich Message 字符上限(Bot API 10.1)


def clip_markdown(text: str, limit: int = RICH_MESSAGE_LIMIT) -> str:
    """裁剪 Markdown 文本到 Rich Message 限制内。"""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def to_rich_input(markdown: str) -> InputRichMessage:
    """Markdown 文本 → InputRichMessage(发送用)。"""
    return InputRichMessage(markdown=clip_markdown(markdown))


def to_plain(markdown: str) -> str:
    """Markdown → 纯文本(Rich 解析失败降级用)。

    不做语法去除 —— 直接发送原始文本(parse_mode=None),
    Telegram 不解析但完整内容可见,信息无损。
    """
    return clip_markdown(markdown)
