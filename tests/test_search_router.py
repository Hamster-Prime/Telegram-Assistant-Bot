"""搜索回退链测试 —— Firecrawl→Brave→DDG,每家重试 1 次,空结果回落。"""
from __future__ import annotations

import httpx
import pytest

from app.search.base import ProviderError, SearchResult
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
