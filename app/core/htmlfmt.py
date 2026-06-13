"""htmlfmt —— Telegram HTML sanitizer。

校验模型输出的 HTML,使其严格符合 Telegram Bot API 的 HTML 模式:
- 白名单标签与属性(其余标签剥离 markup,保留内部文本)
- 游离的 < > & 转义
- 自动补全未闭合标签(流式安全)
- 危险 scheme 拒绝

基于标准库 html.parser,无第三方依赖。
"""
from __future__ import annotations

import html
from html.parser import HTMLParser
from urllib.parse import urlsplit

# 允许的标签(其余标签剥离 markup,保留内部文本)
_ALLOWED_TAGS = frozenset({
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
    "tg-spoiler", "code", "pre", "a", "blockquote", "span",
})

# pre/code 标签:进入后禁止块级标签
_CODE_TAGS = frozenset({"pre", "code"})

# code 上下文内禁止的标签
_BLOCK_TAGS = frozenset({"blockquote"})


def _safe_url(url: str) -> bool:
    """仅允许 http/https/tg scheme。"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    scheme = parts.scheme.lower()
    if not scheme:  # 无 scheme(相对路径)→ Telegram 链接需绝对 URL,拒绝
        return False
    return scheme in ("http", "https", "tg")


class _Sanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._stack: list[str] = []  # 已输出、待闭合的标签名(小写)
        self._code_depth = 0  # pre/code 嵌套深度

    def _render_start(self, tag: str, attrs: list[tuple[str, str | None]]) -> str | None:
        """渲染白名单标签的开始标记;返回 None 表示剥离。"""
        if tag == "a":
            href = None
            for k, v in attrs:
                if k.lower() == "href" and v:
                    href = v
                    break
            if href is None or not _safe_url(href):
                return None  # a 必须有合法 href,否则剥离(保留文字)
            return f'<a href="{html.escape(href, quote=True)}">'
        if tag == "span":
            cls = None
            for k, v in attrs:
                if k.lower() == "class":
                    cls = v
                    break
            if cls != "tg-spoiler":
                return None  # span 仅允许 tg-spoiler
            return '<span class="tg-spoiler">'
        if tag == "code":
            cls = None
            for k, v in attrs:
                if k.lower() == "class":
                    cls = v
                    break
            if cls and cls.startswith("language-"):
                return f'<code class="{html.escape(cls, quote=True)}">'
            return "<code>"
        if tag == "blockquote":
            for k, _v in attrs:
                if k.lower() == "expandable":
                    return "<blockquote expandable>"
            return "<blockquote>"
        # b strong i em u ins s strike del tg-spoiler pre
        return f"<{tag}>"

    def _accept(self, tag: str, attrs: list[tuple[str, str | None]]) -> str | None:
        """Pre-flight checks; returns rendered start tag to emit, or None to strip."""
        if tag not in _ALLOWED_TAGS:
            return None
        if self._code_depth and tag in _BLOCK_TAGS:
            return None
        if tag == "a" and "a" in self._stack:
            return None
        return self._render_start(tag, attrs)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        rendered = self._accept(tag, attrs)
        if rendered is None:
            return
        self._out.append(rendered)
        self._stack.append(tag)
        if tag in _CODE_TAGS:
            self._code_depth += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # 自闭合形式(如 <tg-spoiler/>):作为立即开闭处理
        tag = tag.lower()
        rendered = self._accept(tag, attrs)
        if rendered is None:
            return
        self._out.append(rendered)
        self._out.append(f"</{tag}>")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag not in _ALLOWED_TAGS or tag not in self._stack:
            return  # 无对应开标签,忽略
        # 弹栈直到匹配(处理中间未闭合的同级)
        while self._stack:
            top = self._stack.pop()
            if top in _CODE_TAGS:
                self._code_depth -= 1
            self._out.append(f"</{top}>")
            if top == tag:
                break

    def handle_data(self, data: str) -> None:
        self._out.append(html.escape(data))

    def finish(self) -> str:
        """返回最终 HTML,自动补全残余未闭合标签。"""
        while self._stack:
            top = self._stack.pop()
            if top in _CODE_TAGS:
                self._code_depth -= 1
            self._out.append(f"</{top}>")
        return "".join(self._out)


def sanitize_telegram_html(text: str) -> str:
    """校验并清洗文本为合法的 Telegram HTML。

    - 白名单标签直通,其余标签剥离(保留内部文本)
    - 游离的 < > & 转义
    - 未闭合标签自动补全(流式安全)
    - 解析异常 → 兜底整体 html.escape
    """
    if not text:
        return ""
    parser = _Sanitizer()
    try:
        parser.feed(text)
        parser.close()
    except Exception:
        return html.escape(text)
    return parser.finish()
