"""Firecrawl —— 首选 provider:/v2/search 搜索 + /v2/scrape 抓取(plan §7.3)。"""
from __future__ import annotations

import httpx

from app.logging import get_logger
from app.search.base import ProviderError, SearchResult

log = get_logger("search.firecrawl")


class FirecrawlProvider:
    name = "firecrawl"

    def __init__(self, http: httpx.AsyncClient, api_key: str, timeout_s: float = 15.0) -> None:
        self._http = http
        self._key = api_key
        self._timeout = timeout_s

    @property
    def available(self) -> bool:
        return bool(self._key)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}

    async def search(self, query: str, count: int) -> list[SearchResult]:
        if not self._key:
            raise ProviderError("未配置 FIRECRAWL_API_KEY")
        resp = await self._http.post(
            "https://api.firecrawl.dev/v2/search",
            headers=self._headers(),
            json={
                "query": query,
                "limit": count,
                "sources": [{"type": "web"}],
            },
            timeout=self._timeout,
        )
        if resp.status_code in (401, 403):
            raise ProviderError(f"Firecrawl 鉴权失败 HTTP {resp.status_code}")
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success", True):
            raise ProviderError(f"Firecrawl 返回失败: {data.get('error', '')}")
        web = (data.get("data") or {}).get("web") or []
        results: list[SearchResult] = [
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("description", "") or item.get("markdown", "")[:300],
                "source": self.name,
            }
            for item in web
            if item.get("url")
        ]
        log.info("Firecrawl搜索完成", 查询=query[:50], 结果数=len(results))
        return results

    async def fetch(self, url: str) -> str | None:
        if not self._key:
            raise ProviderError("未配置 FIRECRAWL_API_KEY")
        resp = await self._http.post(
            "https://api.firecrawl.dev/v2/scrape",
            headers=self._headers(),
            json={"url": url, "formats": [{"type": "markdown"}]},
            timeout=self._timeout,
        )
        if resp.status_code in (401, 403):
            raise ProviderError(f"Firecrawl 鉴权失败 HTTP {resp.status_code}")
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success"):
            raise ProviderError(f"Firecrawl scrape 失败: {data.get('error', '')}")
        markdown = (data.get("data") or {}).get("markdown") or ""
        log.info("Firecrawl抓取完成", 地址=url[:80], 正文长度=len(markdown))
        return markdown or None
