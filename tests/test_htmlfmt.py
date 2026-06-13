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
