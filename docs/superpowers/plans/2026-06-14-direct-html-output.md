# 模型直出 Telegram HTML + Sanitizer 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让模型直接输出 Telegram HTML(支持折叠引用块、剧透等),用标准库 sanitizer 替换自研 Markdown→HTML 正则转换,覆盖全部输出路径。

**Architecture:** 新建 `app/core/htmlfmt.py`(`html.parser` 白名单清洗 + 自动补全未闭合标签 + 兜底)。删除 `streaming.py` 的 `format_for_telegram`,`_render_for_telegram` 改调 sanitizer。容错路径(流式 edit 跳过、定稿降级)原样保留。重写系统提示词约束 HTML 输出。

**Tech Stack:** Python 3.11+,标准库 `html.parser` / `html` / `urllib.parse`,aiogram 3,pytest(asyncio_mode=auto)。

参考设计:`docs/superpowers/specs/2026-06-14-direct-html-output-design.md`

---

## 文件结构

| 文件 | 责任 | 动作 |
|---|---|---|
| `app/core/htmlfmt.py` | `sanitize_telegram_html(text)` —— 白名单校验/转义/补全/兜底 | 新建 |
| `tests/test_htmlfmt.py` | sanitizer 单元测试 | 新建 |
| `app/core/streaming.py` | 流式渲染;`_render_for_telegram` 改调 sanitizer;删 `format_for_telegram` | 修改 |
| `tests/test_streaming.py` | 改输入 Markdown→HTML | 修改 |
| `app/core/context.py` | 重写"输出格式规则"段 | 修改 |
| `tests/test_context.py` | 修复脱节断言 | 修改 |
| `app/core/agent.py` | show_thinking 改 `<blockquote expandable>` | 修改 |
| `tests/test_agent.py` | show_thinking 断言改 blockquote | 修改 |
| `app/core/delivery.py` | 文本/caption 包 sanitize + `parse_mode=HTML` | 修改 |
| `app/core/workers.py` | caption/edit/通知 包 sanitize + `parse_mode=HTML` | 修改 |
| `app/handlers/commands.py` | broadcast 包 sanitize + `parse_mode=HTML` | 修改 |

---

### Task 1: 新建 htmlfmt.py —— Telegram HTML sanitizer(TDD)

**Files:**
- Create: `app/core/htmlfmt.py`
- Test: `tests/test_htmlfmt.py`

- [ ] **Step 1: 写失败测试 `tests/test_htmlfmt.py`**

```python
"""htmlfmt —— Telegram HTML sanitizer 单元测试。"""
from __future__ import annotations

from app.core.htmlfmt import sanitize_telegram_html


def test_plain_text_passthrough():
    assert sanitize_telegram_html("hello world") == "hello world"


def test_empty():
    assert sanitize_telegram_html("") == ""


def test_passthrough_bold():
    assert sanitize_telegram_html("<b>粗体</b>") == "<b>粗体</b>"


def test_passthrough_all_simple_tags():
    for tag in ("b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
                "tg-spoiler"):
        out = sanitize_telegram_html(f"<{tag}>x</{tag}>")
        assert out == f"<{tag}>x</{tag}>"


def test_strip_unknown_tag_keeps_text():
    assert sanitize_telegram_html("<div>内容</div>") == "内容"
    assert sanitize_telegram_html("<p>hi</p>") == "hi"
    assert sanitize_telegram_html("<h1>标题</h1>") == "标题"


def test_strip_unknown_attrs_on_allowed_tag():
    assert sanitize_telegram_html('<b class="x" id="y">粗</b>') == "<b>粗</b>"


def test_escape_bare_special_chars():
    assert sanitize_telegram_html("a < b & c > d") == "a &lt; b &amp; c &gt; d"


def test_entity_redecode_single_escape():
    # 输入的 &lt; 被解码为 <,再转义回 &lt; —— 单层,无双重转义
    assert sanitize_telegram_html("a &lt; b") == "a &lt; b"
    assert sanitize_telegram_html("a &amp; b") == "a &amp; b"


def test_autocomplete_unclosed_bold():
    assert sanitize_telegram_html("<b>hello") == "<b>hello</b>"


def test_autocomplete_unclosed_blockquote():
    assert sanitize_telegram_html("<blockquote>x") == "<blockquote>x</blockquote>"


def test_autocomplete_nested_unclosed():
    assert sanitize_telegram_html("<b><i>x") == "<b><i>x</i></b>"


def test_blockquote_expandable_passthrough():
    assert sanitize_telegram_html(
        "<blockquote expandable>x</blockquote>") == "<blockquote expandable>x</blockquote>"


def test_blockquote_plain_passthrough():
    assert sanitize_telegram_html("<blockquote>x</blockquote>") == "<blockquote>x</blockquote>"


def test_code_with_language():
    out = sanitize_telegram_html('<code class="language-python">x</code>')
    assert out == '<code class="language-python">x</code>'


def test_code_without_language():
    assert sanitize_telegram_html("<code>x</code>") == "<code>x</code>"


def test_code_strip_non_language_class():
    assert sanitize_telegram_html('<code class="highlight">x</code>') == "<code>x</code>"


def test_pre_code_escape_inner_specials():
    out = sanitize_telegram_html("<pre><code>a & b < c</code></pre>")
    assert out == "<pre><code>a &amp; b &lt; c</code></pre>"


def test_anchor_safe_url():
    assert sanitize_telegram_html(
        '<a href="https://x.com">链接</a>') == '<a href="https://x.com">链接</a>'


def test_anchor_tg_scheme_allowed():
    assert sanitize_telegram_html(
        '<a href="tg://user?id=1">用户</a>') == '<a href="tg://user?id=1">用户</a>'


def test_anchor_dangerous_scheme_stripped():
    # javascript: 被拒 → a 标签剥离,保留文字
    assert sanitize_telegram_html(
        '<a href="javascript:alert(1)">点我</a>') == "点我"


def test_anchor_no_href_stripped():
    assert sanitize_telegram_html("<a>无链接</a>") == "无链接"


def test_span_tg_spoiler_passthrough():
    assert sanitize_telegram_html(
        '<span class="tg-spoiler">剧透</span>') == '<span class="tg-spoiler">剧透</span>'


def test_span_wrong_class_stripped():
    assert sanitize_telegram_html('<span class="foo">x</span>') == "x"


def test_nested_anchor_stripped():
    # 内层 a 剥离,外层保留
    out = sanitize_telegram_html(
        '<a href="https://x.com">外<a href="https://y.com">内</a></a>')
    assert out == '<a href="https://x.com">外内</a>'


def test_block_inside_code_stripped():
    # code 内 blockquote 剥离
    out = sanitize_telegram_html("<code><blockquote>x</blockquote></code>")
    assert out == "<code>x</code>"


def test_malformed_falls_back_to_escape():
    # 极端垃圾输入不抛错,返回转义文本
    out = sanitize_telegram_html("<<<<<>>>")
    assert "<" not in out or "&lt;" in out
    assert ">" not in out or "&gt;" in out
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_htmlfmt.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.core.htmlfmt'`

- [ ] **Step 3: 写实现 `app/core/htmlfmt.py`**

```python
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

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag not in _ALLOWED_TAGS:
            return  # 剥离:不输出,不入栈(保留内部文本)
        if self._code_depth and tag in _BLOCK_TAGS:
            return  # code 上下文内禁止块级标签
        if tag == "a" and "a" in self._stack:
            return  # 禁止 a 嵌套 a
        rendered = self._render_start(tag, attrs)
        if rendered is None:
            return  # 属性校验失败,按剥离处理
        self._out.append(rendered)
        self._stack.append(tag)
        if tag in _CODE_TAGS:
            self._code_depth += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # 自闭合形式(如 <tg-spoiler/>):作为立即开闭处理
        tag = tag.lower()
        if tag not in _ALLOWED_TAGS:
            return
        if self._code_depth and tag in _BLOCK_TAGS:
            return
        if tag == "a" and "a" in self._stack:
            return
        rendered = self._render_start(tag, attrs)
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
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_htmlfmt.py -v`
Expected: 全部 PASS(若个别用例因 html.parser 行为细节不符,调整实现使测试通过;不可放宽测试断言中关于安全性的部分)

- [ ] **Step 5: 提交**

```bash
git add app/core/htmlfmt.py tests/test_htmlfmt.py
git commit -m "feat: 新增 Telegram HTML sanitizer(htmlfmt)
```

---

### Task 2: streaming.py 接入 sanitizer

**Files:**
- Modify: `app/core/streaming.py:60-102`(删 `format_for_telegram`,改 `_render_for_telegram`)
- Modify: `app/core/streaming.py:80`(顶部 `import re` 若仅服务于已删函数则保留或移除,见下)
- Modify: `tests/test_streaming.py:13-21,124-168`(导入与用例改 HTML 输入)

- [ ] **Step 1: 改 `tests/test_streaming.py` 导入与转换器测试**

把导入块(13-21 行)中的 `format_for_telegram` 删除,新增 `format_for_telegram` 的替代断言改为验证 sanitizer 直通。具体:

将
```python
from app.core.streaming import (
    DraftRenderer,
    EditRenderer,
    GuestRenderer,
    TG_MESSAGE_LIMIT,
    _render_for_telegram,
    clip,
    format_for_telegram,
)
```
改为
```python
from app.core.streaming import (
    DraftRenderer,
    EditRenderer,
    GuestRenderer,
    TG_MESSAGE_LIMIT,
    _render_for_telegram,
    clip,
)
```

把 `test_format_for_telegram_converts_common_markdown_to_html`(约 124 行):
```python
def test_format_for_telegram_converts_common_markdown_to_html():
    assert format_for_telegram("**粗体** 和 `代码`") == "<b>粗体</b> 和 <code>代码</code>"
```
改为(模型直出 HTML,sanitizer 直通):
```python
def test_render_for_telegram_passes_through_valid_html():
    assert _render_for_telegram("<b>粗体</b> 和 <code>代码</code>") == (
        "<b>粗体</b> 和 <code>代码</code>"
    )
```

把 `test_render_for_telegram_respects_limit_after_html_escaping`(约 128 行)中,若其依赖 `**` 转 `<b>` 的输入,改为直接输入大量 `&` 字符(转义后膨胀)以保持长度裁剪语义:
```python
def test_render_for_telegram_respects_limit_after_html_escaping():
    # & 转义为 &amp; 后膨胀;超限时回退为纯转义文本(不超过上限)
    long = "a&b" * 2000  # 转义后 a&amp;b → 6 字符/组
    out = _render_for_telegram(long)
    assert len(out) <= TG_MESSAGE_LIMIT
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_streaming.py::test_render_for_telegram_passes_through_valid_html -v`
Expected: FAIL(`format_for_telegram` 仍把 `<b>` 当字面量转义,或 `format_for_telegram` 导入已删导致实现仍用旧逻辑)

- [ ] **Step 3: 改 `app/core/streaming.py`**

(a) 顶部 import 区(约 26-41 行)新增:
```python
from app.core.htmlfmt import sanitize_telegram_html
```

(b) 删除整个 `format_for_telegram` 函数(60-92 行)。

(c) 把 `_render_for_telegram`(95-102 行):
```python
def _render_for_telegram(text: str) -> str:
    raw = clip(text)
    rendered = format_for_telegram(raw)
    if len(rendered) <= TG_MESSAGE_LIMIT:
        return rendered
    while raw and len(html.escape(raw)) > TG_MESSAGE_LIMIT:
        raw = raw[:-1]
    return html.escape(raw)
```
改为:
```python
def _render_for_telegram(text: str) -> str:
    raw = clip(text)
    rendered = sanitize_telegram_html(raw)
    if len(rendered) <= TG_MESSAGE_LIMIT:
        return rendered
    while raw and len(html.escape(raw)) > TG_MESSAGE_LIMIT:
        raw = raw[:-1]
    return html.escape(raw)
```

(d) 检查 `import re`(约 28 行)与 `import html`(约 27 行):若 `re` 仅服务于已删的 `format_for_telegram`,则删除 `import re`;`html` 仍被 `_render_for_telegram` / `_commit_final_edit` 使用,保留。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_streaming.py -v`
Expected: 全部 PASS。重点确认 `test_edit_renderer_sends_telegram_html`、`test_draft_renderer_does_not_send_initial_placeholder_in_private` 中把模型输出从 `**最终回复**` 改为 `<b>最终回复</b>`(若这些用例当前传入 Markdown 语法,改为 HTML):

检查 `test_edit_renderer_sends_telegram_html`(约 132 行)与 `test_draft_renderer_*`(约 160 行):若其传入 `"**最终回复**"` 期望 `"<b>最终回复</b>"`,改为传入 `"<b>最终回复</b>"` 期望直通 `"<b>最终回复</b>"`。具体逐处:
- 断言 `"parse_mode" == "HTML"` 保留不变;
- 文本输入由 Markdown 改为对应 HTML;
- 期望输出由转换结果改为 sanitizer 直通结果。

- [ ] **Step 5: 运行 sanitizer 测试 + streaming 测试整体确认**

Run: `pytest tests/test_htmlfmt.py tests/test_streaming.py -v`
Expected: 全部 PASS

- [ ] **Step 6: 提交**

```bash
git add app/core/streaming.py tests/test_streaming.py
git commit -m "refactor: streaming 接入 sanitizer,删除 Markdown 转换"
```

---

### Task 3: 重写系统提示词(context.py)

**Files:**
- Modify: `app/core/context.py:50-66`
- Modify: `tests/test_context.py:42-50`

- [ ] **Step 1: 改 `tests/test_context.py` 断言**

将 `test_system_prompt_limits_output_to_telegram_supported_format`(42-50 行):
```python
async def test_system_prompt_limits_output_to_telegram_supported_format(daos: DAOBundle):
    cb = ContextBuilder(daos)
    msgs = await cb.build(100, 1, "说明格式要求")
    system = msgs[0]["content"]

    assert "Telegram 支持的格式" in system
    assert "不要使用 Markdown 标题" in system
    assert "# 标题" in system
    assert "不要输出井号" in system
```
改为:
```python
async def test_system_prompt_instructs_direct_html_output(daos: DAOBundle):
    cb = ContextBuilder(daos)
    msgs = await cb.build(100, 1, "说明格式要求")
    system = msgs[0]["content"]

    assert "直接以 HTML 发送到 Telegram" in system
    assert "<blockquote expandable>" in system
    assert "<code>" in system
    assert "<pre>" in system
    assert "<tg-spoiler>" in system
    assert "不要输出 Markdown" in system
    assert "&lt;" in system  # 转义规则
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_context.py::test_system_prompt_instructs_direct_html_output -v`
Expected: FAIL(当前提示词无这些短语)

- [ ] **Step 3: 重写 `app/core/context.py` 的"输出格式规则"段(50-66 行)**

将
```python
━━━━━━━━━━━━━━━━━━━━
输出格式规则:
━━━━━━━━━━━━━━━━━━━━

你的回复将通过 Markdown→HTML 转换后发送到 Telegram,按需使用格式,不要为格式而格式。

**加粗** — 关键结论、重要术语,复杂回复中使用
*斜体* — 补充说明、引用内容
`行内代码` — 命令、参数、路径、技术字符串,涉及时必须用
代码块(三个反引号)— 多行代码、配置文件、终端输出
列表 — 3 项及以上的并列信息;有顺序用编号,无顺序用短横线
分节标题 — 用加粗文字加冒号,例 **操作步骤:**,不要用 # 井号(Telegram 不渲染)

禁止项:
- 不要使用 # 井号标题
- 不要使用 HTML 标签、脚注、引用链接定义
- 不要在不需要时强行使用格式
```
改为
```python
━━━━━━━━━━━━━━━━━━━━
输出格式规则(直接输出 Telegram HTML):
━━━━━━━━━━━━━━━━━━━━

你的回复直接以 HTML 发送到 Telegram,不经 Markdown 转换。只能使用以下标签:

<b>加粗</b>      关键结论、重要术语(复杂回复中使用)
<i>斜体</i>      补充说明、引用语句
<u>下划线</u> <s>删除线</s> <tg-spoiler>剧透</tg-spoiler>   按需使用
<code>行内代码</code>    命令、参数、路径、技术字符串,涉及时必须用
<pre><code class="language-python">…</code></pre>   多行代码/配置/终端输出(写明语言)
<a href="https://…">链接</a>   超链接
<blockquote>…</blockquote>    简短引用
<blockquote expandable>…</blockquote>   较长的次要内容(长引用/补充资料/推导过程/免责声明),默认折叠,保持主体紧凑

转义与书写规则:
- 正文及代码中的 < > & 必须写成 &lt; &gt; &amp;(显示 a<b 要写 a&lt;b)
- 换行直接用换行符,不要用 <br>;列表用「1.」或「-」纯文本,不要用 <ul><ol>
- 分节标题用 <b>标题:</b>,不要用 # 井号
- 标签必须正确闭合、正确嵌套

禁止:
- 不要输出 Markdown(** __ `` 等),只输出上述 HTML 标签
- 不要使用上述列表之外的标签(无 <p> <div> <table> <h1> <br> 等)
- 闲聊和简单问题保持纯文本,不要为格式而格式
```

注:此段在 Python 三引号字符串内。注意保留 `SYSTEM_PROMPT` 的 `.format(now=...)` 调用——新文案中不含 `{` `}` 花括号,与 `.format` 不冲突;若文案中需要字面花括号则双写。本段不引入花括号,无需额外处理。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_context.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add app/core/context.py tests/test_context.py
git commit -m "feat: 系统提示词改为约束模型直出 Telegram HTML"
```

---

### Task 4: show_thinking 改用折叠引用块(agent.py)

**Files:**
- Modify: `app/core/agent.py:67-69,134-136`
- Modify: `tests/test_agent.py:163-174`

- [ ] **Step 1: 改 `tests/test_agent.py` 的 show_thinking 断言**

将 `test_show_thinking_renders_quote`(163-174 行):
```python
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
```
改为:
```python
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_agent.py::test_show_thinking_renders_expandable_blockquote -v`
Expected: FAIL(当前产出 `> 思考中`)

- [ ] **Step 3: 改 `app/core/agent.py`**

(a) 流式期间(67-69 行):
```python
                        if show_thinking and reasoning_text:
                            quoted = "\n".join(f"> {ln}" for ln in reasoning_text.splitlines())
                            display = f"{quoted}\n\n{full_text}"
```
改为
```python
                        if show_thinking and reasoning_text:
                            display = (
                                f"<blockquote expandable>\n{reasoning_text}"
                                f"\n</blockquote>\n\n{full_text}"
                            )
```

(b) finalize 时(134-136 行):
```python
            if show_thinking and result.reasoning:
                quoted = "\n".join(f"> {ln}" for ln in result.reasoning.splitlines())
                display = f"{quoted}\n\n{full_text}"
```
改为
```python
            if show_thinking and result.reasoning:
                display = (
                    f"<blockquote expandable>\n{result.reasoning}"
                    f"\n</blockquote>\n\n{full_text}"
                )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_agent.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add app/core/agent.py tests/test_agent.py
git commit -m "feat: show_thinking 改用 <blockquote expandable> 折叠引用"
```

---

### Task 5: delivery.py 接入 sanitizer

**Files:**
- Modify: `app/core/delivery.py:66-70`(send_photo caption)
- Modify: `app/core/delivery.py:90-97`(send_placeholder)
- Modify: `app/core/delivery.py:99-107`(edit_placeholder)
- Modify: `app/core/delivery.py:109-116`(send_text)

- [ ] **Step 1: 改 `app/core/delivery.py`**

(a) 顶部 import 区(约 22-23 行后)新增:
```python
from app.core.htmlfmt import sanitize_telegram_html
```

(b) `send_photo`(66-74 行)的 caption 处理:
```python
    async def send_photo(self, url: str, caption: str | None = None) -> bool:
        try:
            await self._limiter.acquire()
            await self._bot.send_photo(self._chat_id, URLInputFile(url),
                                       caption=caption)
            return True
```
改为(仅当 caption 非空时 sanitize + parse_mode):
```python
    async def send_photo(self, url: str, caption: str | None = None) -> bool:
        try:
            await self._limiter.acquire()
            kwargs: dict[str, Any] = {}
            if caption:
                kwargs["caption"] = sanitize_telegram_html(caption)
                kwargs["parse_mode"] = "HTML"
            await self._bot.send_photo(self._chat_id, URLInputFile(url), **kwargs)
            return True
```

(c) `send_placeholder`(90-97 行):
```python
    async def send_placeholder(self, text: str) -> int | None:
        while True:
            try:
                await self._limiter.acquire()
                msg = await self._bot.send_message(self._chat_id, text)
                return msg.message_id
            except TelegramRetryAfter as e:
                await _sleep_retry_after(e)
```
改为:
```python
    async def send_placeholder(self, text: str) -> int | None:
        while True:
            try:
                await self._limiter.acquire()
                msg = await self._bot.send_message(
                    self._chat_id, sanitize_telegram_html(text), parse_mode="HTML")
                return msg.message_id
            except TelegramRetryAfter as e:
                await _sleep_retry_after(e)
```

(d) `edit_placeholder`(99-107 行):
```python
            await self._bot.edit_message_text(
                text, chat_id=self._chat_id, message_id=msg_id)
```
改为:
```python
            await self._bot.edit_message_text(
                sanitize_telegram_html(text), chat_id=self._chat_id,
                message_id=msg_id, parse_mode="HTML")
```

(e) `send_text`(109-116 行):
```python
            await self._bot.send_message(self._chat_id, text)
```
改为:
```python
            await self._bot.send_message(
                self._chat_id, sanitize_telegram_html(text), parse_mode="HTML")
```

- [ ] **Step 2: 运行现有测试确认无回归**

Run: `pytest tests/test_media.py tests/test_guest.py -v`
Expected: 全部 PASS(delivery 测试若有 mock bot,断言不受影响;若断言检查了发送的文本等于原始 text,需相应改为 sanitize 后的值)

- [ ] **Step 3: 提交**

```bash
git add app/core/delivery.py
git commit -m "feat: delivery 文本/-caption 接入 HTML sanitizer"
```

---

### Task 6: workers.py 接入 sanitizer

**Files:**
- Modify: `app/core/workers.py:146-163`(视频 caption + 发送)
- Modify: `app/core/workers.py:225-246`(音乐 caption + 发送)
- Modify: `app/core/workers.py:265-274`(_edit_placeholder)
- Modify: `app/core/workers.py:289-297`(_edit_inline_text)
- Modify: `app/core/workers.py:316`(失败通知 send_message)

- [ ] **Step 1: 改 `app/core/workers.py`**

(a) 顶部 import 区(找现有 `from app.core...` 附近)新增:
```python
from app.core.htmlfmt import sanitize_telegram_html
```

(b) 视频回填(146-163 行):把 `caption = f"🎬 视频已生成:{gen.prompt[:100]}"` 之后涉及 caption 传参处统一用 sanitized caption。最简改法:在 `caption = ...` 行后紧跟:
```python
            caption = f"🎬 视频已生成:{gen.prompt[:100]}"
            html_caption = sanitize_telegram_html(caption)
```
然后把
```python
                media = InputMediaVideo(media=url, caption=caption)
```
改为
```python
                media = InputMediaVideo(media=url, caption=html_caption, parse_mode="HTML")
```
把
```python
                await self._bot.send_video(
                    gen.chat_id,
                    BufferedInputFile(data, filename=f"video_{gen_id}.mp4"),
                    caption=caption,
                )
```
改为
```python
                await self._bot.send_video(
                    gen.chat_id,
                    BufferedInputFile(data, filename=f"video_{gen_id}.mp4"),
                    caption=html_caption, parse_mode="HTML",
                )
```
注意:`_edit_inline_text(gen.inline_message_id, f"{caption}\n\n{url}")` 处(降级文本+链接)见 (d) 修复——该方法本身将接入 sanitize,故保持传原始字符串即可。

(c) 音乐回填(225-246 行):同样模式。在 `caption = f"🎵 音乐已生成:{prompt[:100]}"` 后加:
```python
            html_caption = sanitize_telegram_html(caption)
```
把
```python
                media = InputMediaAudio(media=url, caption=caption)
```
改为
```python
                media = InputMediaAudio(media=url, caption=html_caption, parse_mode="HTML")
```
音乐直发分支(若有 `send_audio(...caption=caption)`)同样改 `caption=html_caption, parse_mode="HTML"`。定位:约 239-246 行的 `await self._bot.send_audio(...)`。

(d) `_edit_placeholder`(265-274 行):
```python
            await self._bot.edit_message_text(
                text, chat_id=gen.chat_id, message_id=gen.placeholder_msg_id,
            )
```
改为
```python
            await self._bot.edit_message_text(
                sanitize_telegram_html(text), chat_id=gen.chat_id,
                message_id=gen.placeholder_msg_id, parse_mode="HTML",
            )
```

(e) `_edit_inline_text`(289-297 行):
```python
            await self._bot.edit_message_text(
                text, inline_message_id=inline_message_id,
            )
```
改为
```python
            await self._bot.edit_message_text(
                sanitize_telegram_html(text), inline_message_id=inline_message_id,
                parse_mode="HTML",
            )
```

(f) 失败通知 send_message(316 行):
```python
                    await self._bot.send_message(gen.chat_id, text)
```
改为
```python
                    await self._bot.send_message(
                        gen.chat_id, sanitize_telegram_html(text), parse_mode="HTML")
```

- [ ] **Step 2: 运行现有测试确认无回归**

Run: `pytest tests/test_workers.py -v`
Expected: 全部 PASS(若测试断言了发送文本/caption 等于原始字面量,改为 sanitize 后的值;`InputMediaVideo`/`InputMediaAudio` 的 `parse_mode` 若 mock 不校验则无影响)

- [ ] **Step 3: 提交**

```bash
git add app/core/workers.py
git commit -m "feat: workers caption/edit/通知 接入 HTML sanitizer"
```

---

### Task 7: commands.py broadcast 接入 sanitizer

**Files:**
- Modify: `app/handlers/commands.py:353`

- [ ] **Step 1: 改 `app/handlers/commands.py`**

(a) 顶部 import 区(现有 `from app.core...` 附近)新增:
```python
from app.core.htmlfmt import sanitize_telegram_html
```

(b) `cmd_broadcast`(353 行):
```python
            await svc.bot.send_message(uid, f"📢 管理员广播:\n{command.args}")
```
改为
```python
            await svc.bot.send_message(
                uid, sanitize_telegram_html(f"📢 管理员广播:\n{command.args}"),
                parse_mode="HTML")
```

- [ ] **Step 2: 运行相关测试确认无回归**

Run: `pytest tests/ -k broadcast -v`
Expected: 无失败(若无 broadcast 测试,跳过;本步不强制)

- [ ] **Step 3: 提交**

```bash
git add app/handlers/commands.py
git commit -m "feat: broadcast 接入 HTML sanitizer"
```

---

### Task 8: 全量验证

- [ ] **Step 1: ruff 检查**

Run: `ruff check app/ tests/`
Expected: 无错误(若有未使用 import 如残留 `import re` 在 streaming.py,删除后重跑)

- [ ] **Step 2: 全量 pytest**

Run: `pytest -v`
Expected: 全部 PASS

- [ ] **Step 3: 抽查提示词无 .format 冲突**

Run(powershell/git-bash 通用):`python -c "from app.core.context import SYSTEM_PROMPT; print(SYSTEM_PROMPT.format(now='2026-06-14 12:00'))"`
Expected: 正常打印,无 `KeyError` / `IndexError`(确认新文案无未转义花括号)

- [ ] **Step 4: 最终提交(若有零散修改)**

```bash
git add -A
git commit -m "chore: ruff 修复与最终清理"
```
(若 Step 1-3 均无改动则跳过本步)

---

## 自审记录

- **Spec 覆盖**:设计文档 7 节均有任务——sanitizer(T1)、streaming 接入(T2)、提示词(T3)、show_thinking(T4)、全输出路径(T5/6/7)、测试(T1-4 内嵌)、风险缓解(T2 容错路径保留 + T1 兜底)。
- **占位符扫描**:无 TBD/TODO;每步均含完整代码或精确命令。
- **类型/签名一致性**:`sanitize_telegram_html(text: str) -> str` 在 T1 定义,T2-7 一致调用;`_render_for_telegram` 签名不变;`_CODE_TAGS`/`_BLOCK_TAGS`/`_ALLOWED_TAGS` 名称一致。
