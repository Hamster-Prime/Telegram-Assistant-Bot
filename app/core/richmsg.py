"""Rich Message 封装 —— Bot API 10.1 sendRichMessage 薄包装。

LLM 直接输出 Rich Markdown(GFM 兼容子集),本模块负责:
- 构造 InputRichMessage(markdown=...)
- 字符裁剪(32KB Rich Message 限制)
- 纯文本降级辅助

Rich Markdown 文档:https://core.telegram.org/bots/api#rich-message-formatting-options
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from aiogram.types import InputRichMessage

RICH_MESSAGE_LIMIT = 32_768  # Rich Message 字符上限(Bot API 10.1)
_MEDIA_MARKDOWN_BY_KIND = {
    "image": "!",
    "photo": "!",
    "video": "!",
    "audio": "!",
    "music": "!",
}


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


def is_rich_media_url(url: str) -> bool:
    """Rich Message 媒体块只接受可由 Telegram 拉取的 HTTP(S) URL。"""
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


@dataclass(slots=True)
class RichAttachment:
    """一次生成得到的 Rich Message 媒体附件。"""

    kind: str
    url: str
    label: str
    note: str = ""

    @property
    def markdown(self) -> str:
        prefix = _MEDIA_MARKDOWN_BY_KIND.get(self.kind, "")
        label = self.label.strip() or self.kind
        return f"{prefix}[{label}]({self.url})"


class RichAttachmentCollector:
    """请求级附件收集器,用于工具结果和最终 Rich Message 定稿合并。"""

    def __init__(self) -> None:
        self._items: list[RichAttachment] = []

    def add(
        self,
        kind: str,
        url: str,
        *,
        label: str | None = None,
        note: str | None = None,
    ) -> RichAttachment | None:
        if not is_rich_media_url(url):
            return None
        for item in self._items:
            if item.url == url:
                return item
        idx = len(self._items) + 1
        att = RichAttachment(
            kind=kind,
            url=url,
            label=label or f"{kind} {idx}",
            note=note or "",
        )
        self._items.append(att)
        return att

    def pending(self) -> list[RichAttachment]:
        return list(self._items)


def merge_attachments(markdown: str, collector: RichAttachmentCollector | None) -> str:
    """把未出现在正文里的附件追加到 Rich Markdown 末尾。"""
    if collector is None:
        return clip_markdown(markdown)
    text = markdown.strip()
    additions: list[str] = []
    for att in collector.pending():
        if att.markdown in text:
            continue
        block = att.markdown
        if att.note:
            block += f"\n{att.note.strip()}"
        additions.append(block)
    if additions:
        if text:
            text += "\n\n"
        text += "\n\n".join(additions)
    return clip_markdown(text)
