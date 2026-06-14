"""搜索回退链测试 —— Firecrawl→Brave→DDG,每家重试 1 次,空结果回落。"""
from __future__ import annotations

import httpx
import pytest

from app.search.base import ProviderError, SearchResult
from app.search.minimax import MiniMaxSearchProvider
from app.search.router import AllProvidersFailed, SearchRouter, _html_to_text


class FakeProvider:
    def __init__(self, name: str, *, results=None, error: Exception | None = None,
                 fail_times: int = 0, fetch_result: str | None = None):
        self.name = name
        self.available = True
        self._results = results if results is not None else []
        self._error = error
        self._fail_times = fail_times
        self._fetch_result = fetch_result
        self.search_calls = 0
        self.fetch_calls = 0

    async def search(self, query: str, count: int) -> list[SearchResult]:
        self.search_calls += 1
        if self._fail_times >= self.search_calls:
            raise self._error or ProviderError(f"{self.name} 故障")
        if self._error and self._fail_times == 0:
            raise self._error
        return self._results

    async def fetch(self, url: str) -> str | None:
        self.fetch_calls += 1
        return self._fetch_result


def hit(name: str) -> list[SearchResult]:
    return [{"title": "t", "url": "https://x", "snippet": "s", "source": name}]


@pytest.fixture
def http():
    return httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, text="<html><body>正文</body></html>")))


async def test_first_provider_hits(http):
    p1 = FakeProvider("firecrawl", results=hit("firecrawl"))
    p2 = FakeProvider("brave", results=hit("brave"))
    router = SearchRouter([p1, p2], http)
    res = await router.search("查询")
    assert res[0]["source"] == "firecrawl"
    assert p1.search_calls == 1 and p2.search_calls == 0


async def test_retry_once_then_succeed(http):
    """第 1 次失败 → 同家重试 1 次成功,不回落。"""
    p1 = FakeProvider("firecrawl", results=hit("firecrawl"), fail_times=1)
    p2 = FakeProvider("brave", results=hit("brave"))
    router = SearchRouter([p1, p2], http)
    res = await router.search("查询")
    assert res[0]["source"] == "firecrawl"
    assert p1.search_calls == 2 and p2.search_calls == 0


async def test_fallback_after_two_failures(http):
    """每家 2 次都失败 → 回落下一家。"""
    p1 = FakeProvider("firecrawl", fail_times=99)
    p2 = FakeProvider("brave", results=hit("brave"))
    router = SearchRouter([p1, p2], http)
    res = await router.search("查询")
    assert res[0]["source"] == "brave"
    assert p1.search_calls == 2  # 1 + 重试1


async def test_empty_results_treated_as_failure(http):
    """空结果视为失利:重试后回落。"""
    p1 = FakeProvider("firecrawl", results=[])
    p2 = FakeProvider("duckduckgo", results=hit("duckduckgo"))
    router = SearchRouter([p1, p2], http)
    res = await router.search("查询")
    assert res[0]["source"] == "duckduckgo"
    assert p1.search_calls == 2


async def test_all_providers_failed(http):
    p1 = FakeProvider("firecrawl", fail_times=99)
    p2 = FakeProvider("brave", fail_times=99)
    p3 = FakeProvider("duckduckgo", results=[])
    router = SearchRouter([p1, p2, p3], http)
    with pytest.raises(AllProvidersFailed) as ei:
        await router.search("查询")
    assert p1.search_calls == 2 and p2.search_calls == 2 and p3.search_calls == 2
    assert "无法联网搜索" in ei.value.user_message()


async def test_unavailable_provider_skipped(http):
    p1 = FakeProvider("firecrawl", results=hit("firecrawl"))
    p1.available = False  # 未配置 Key
    p2 = FakeProvider("brave", results=hit("brave"))
    router = SearchRouter([p1, p2], http)
    res = await router.search("查询")
    assert res[0]["source"] == "brave"
    assert p1.search_calls == 0


async def test_fetch_provider_markdown(http):
    p1 = FakeProvider("firecrawl", fetch_result="# 标题\n正文内容")
    router = SearchRouter([p1], http)
    md = await router.fetch("https://example.com")
    assert "正文内容" in md


async def test_fetch_falls_back_to_direct(http):
    """provider 不支持抓取(返回 None)→ 直连兜底抽取 HTML 正文。"""
    p1 = FakeProvider("brave", fetch_result=None)
    router = SearchRouter([p1], http)
    text = await router.fetch("https://example.com")
    assert "正文" in text


def test_html_to_text_strips_script():
    html = "<html><script>evil()</script><body><p>你好 世界</p></body></html>"
    text = _html_to_text(html)
    assert "你好" in text and "evil" not in text


# ── MiniMaxSearchProvider 单元测试 ─────────────────────────


def _mmx_http(handler):
    """构造带 MockTransport 的 httpx client,路由到 handler。"""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _mmx_ok_body(items: list[dict]) -> dict:
    return {"organic": items, "base_resp": {"status_code": 0, "status_msg": "success"}}


async def test_minimax_search_maps_organic_to_search_result():
    body = _mmx_ok_body([
        {"title": "标题A", "link": "https://a.com", "snippet": "摘要A", "date": "2025-01-01"},
        {"title": "标题B", "link": "https://b.com", "snippet": "摘要B"},
    ])
    http = _mmx_http(lambda req: httpx.Response(200, json=body))
    p = MiniMaxSearchProvider(http, "test-key")
    res = await p.search("测试", count=5)
    assert len(res) == 2
    assert res[0] == {"title": "标题A", "url": "https://a.com",
                      "snippet": "摘要A", "source": "minimax"}
    assert res[1]["url"] == "https://b.com"


async def test_minimax_search_truncates_to_count():
    items = [{"title": f"T{i}", "link": f"https://x{i}.com", "snippet": "s"} for i in range(8)]
    http = _mmx_http(lambda req: httpx.Response(200, json=_mmx_ok_body(items)))
    p = MiniMaxSearchProvider(http, "test-key")
    res = await p.search("测试", count=3)
    assert len(res) == 3


async def test_minimax_search_empty_organic():
    http = _mmx_http(lambda req: httpx.Response(200, json=_mmx_ok_body([])))
    p = MiniMaxSearchProvider(http, "test-key")
    res = await p.search("测试", count=5)
    assert res == []


async def test_minimax_search_base_resp_error_raises():
    body = {"organic": [], "base_resp": {"status_code": 1004, "status_msg": "鉴权失败"}}
    http = _mmx_http(lambda req: httpx.Response(200, json=body))
    p = MiniMaxSearchProvider(http, "test-key")
    with pytest.raises(ProviderError, match="code=1004"):
        await p.search("测试", count=5)


async def test_minimax_search_auth_failure_raises():
    http = _mmx_http(lambda req: httpx.Response(401))
    p = MiniMaxSearchProvider(http, "test-key")
    with pytest.raises(ProviderError, match="鉴权失败"):
        await p.search("测试", count=5)


async def test_minimax_search_posts_correct_body():
    captured = {}

    def handler(req: httpx.Request):
        captured["url"] = str(req.url)
        captured["auth"] = req.headers.get("authorization", "")
        captured["body"] = req.content.decode()
        return httpx.Response(200, json=_mmx_ok_body([]))

    http = _mmx_http(handler)
    p = MiniMaxSearchProvider(http, "sk-test", "https://api.minimaxi.com/v1")
    await p.search("你好世界", count=5)
    assert captured["url"] == "https://api.minimaxi.com/v1/coding_plan/search"
    assert captured["auth"] == "Bearer sk-test"
    assert '"q"' in captured["body"] and "你好世界" in captured["body"]


async def test_minimax_unavailable_without_key():
    http = _mmx_http(lambda req: httpx.Response(200, json=_mmx_ok_body([])))
    p = MiniMaxSearchProvider(http, "")
    assert p.available is False
    with pytest.raises(ProviderError, match="未配置"):
        await p.search("测试", count=5)


async def test_minimax_fetch_returns_none():
    http = _mmx_http(lambda req: httpx.Response(200))
    p = MiniMaxSearchProvider(http, "test-key")
    assert await p.fetch("https://example.com") is None


async def test_minimax_router_integration_first_choice():
    """router 中 minimax 命中后不调用后续 provider。"""
    body = _mmx_ok_body([
        {"title": "结果", "link": "https://r.com", "snippet": "s"}])
    http = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json=body)))
    minimax = MiniMaxSearchProvider(http, "test-key")
    fallback = FakeProvider("firecrawl", results=hit("firecrawl"))
    router = SearchRouter([minimax, fallback], http)
    res = await router.search("查询")
    assert res[0]["source"] == "minimax"
    assert fallback.search_calls == 0


# ── MiniMaxSearchProvider 多 Key fallback 测试 ─────────────


def _mmx_ok_item(title="结果", url="https://r.com", snippet="s"):
    return {"title": title, "link": url, "snippet": snippet}


async def test_minimax_first_key_401_falls_to_second_key():
    """Key#1 鉴权失败(401)→ 切换 Key#2 命中。"""
    calls = []

    def handler(req: httpx.Request):
        auth = req.headers.get("authorization", "")
        calls.append(auth)
        if auth == "Bearer bad-key":
            return httpx.Response(401)
        return httpx.Response(200, json=_mmx_ok_body([_mmx_ok_item()]))

    http = _mmx_http(handler)
    p = MiniMaxSearchProvider(http, ["bad-key", "good-key"])
    res = await p.search("测试", count=5)
    assert res[0]["source"] == "minimax"
    assert len(calls) == 2
    assert calls[1] == "Bearer good-key"


async def test_minimax_first_key_429_falls_to_second_key():
    """Key#1 限流(429)→ 切换 Key#2 命中。"""
    def handler(req: httpx.Request):
        auth = req.headers.get("authorization", "")
        if auth == "Bearer k1":
            return httpx.Response(429)
        return httpx.Response(200, json=_mmx_ok_body([_mmx_ok_item()]))

    http = _mmx_http(handler)
    p = MiniMaxSearchProvider(http, ["k1", "k2"])
    res = await p.search("测试", count=5)
    assert len(res) == 1


async def test_minimax_first_key_base_resp_auth_falls_to_second_key():
    """Key#1 base_resp 鉴权失败(1004)→ 切换 Key#2 命中。"""
    def handler(req: httpx.Request):
        auth = req.headers.get("authorization", "")
        if auth == "Bearer k1":
            return httpx.Response(200, json={
                "organic": [],
                "base_resp": {"status_code": 1004, "status_msg": "鉴权失败"}})
        return httpx.Response(200, json=_mmx_ok_body([_mmx_ok_item()]))

    http = _mmx_http(handler)
    p = MiniMaxSearchProvider(http, ["k1", "k2"])
    res = await p.search("测试", count=5)
    assert len(res) == 1


async def test_minimax_all_keys_fail_raises_with_detail():
    """所有 Key 均 401 → ProviderError 含失败明细。"""
    http = _mmx_http(lambda req: httpx.Response(401))
    p = MiniMaxSearchProvider(http, ["k1", "k2", "k3"])
    with pytest.raises(ProviderError, match="3 个 MiniMax Key"):
        await p.search("测试", count=5)


async def test_minimax_request_level_error_skips_other_keys():
    """请求级错误(1026)→ 立即抛出,不消耗其余 Key。"""
    calls = []

    def handler(req: httpx.Request):
        calls.append(req.headers.get("authorization", ""))
        return httpx.Response(200, json={
            "organic": [],
            "base_resp": {"status_code": 1026, "status_msg": "内容涉及敏感信息"}})

    http = _mmx_http(handler)
    p = MiniMaxSearchProvider(http, ["k1", "k2", "k3"])
    with pytest.raises(ProviderError, match="code=1026"):
        await p.search("测试", count=5)
    assert len(calls) == 1  # 只试了第一个 Key


async def test_minimax_accepts_single_string_key():
    """传单个 str Key(向后兼容)→ 正常工作。"""
    http = _mmx_http(lambda req: httpx.Response(200, json=_mmx_ok_body([_mmx_ok_item()])))
    p = MiniMaxSearchProvider(http, "only-key")
    assert p.available is True
    res = await p.search("测试", count=5)
    assert len(res) == 1


async def test_minimax_empty_keys_filtered():
    """Key 列表含空串 → 过滤后若无有效 Key 则不可用。"""
    http = _mmx_http(lambda req: httpx.Response(200))
    p = MiniMaxSearchProvider(http, ["", "  ", ""])
    assert p.available is False
    with pytest.raises(ProviderError, match="未配置"):
        await p.search("测试", count=5)
