"""Brave Search —— 次选 provider:纯搜索,不带正文(plan §7.4)。"""
from __future__ import annotations

import httpx

from app.logging import get_logger
from app.search.base import ProviderError, SearchResult

log = get_logger("search.brave")


class BraveProvider:
    name = "brave"

    def __init__(self, http: httpx.AsyncClient, api_key: str, timeout_s: float = 8.0) -> None:
        self._http = http
        self._key = api_key
        self._timeout = timeout_s

    @property
    def available(self) -> bool:
        return bool(self._key)

    async def search(self, query: str, count: int) -> list[SearchResult]:
        if not self._key:
            raise ProviderError("未配置 BRAVE_API_KEY")
        resp = await self._http.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers={
                "X-Subscription-Token": self._key,
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
            },
            params={"q": query, "count": min(count, 20), "extra_snippets": "true"},
            timeout=self._timeout,
        )
        if resp.status_code in (401, 403):
            raise ProviderError(f"Brave 鉴权失败 HTTP {resp.status_code}")
        resp.raise_for_status()
        data = resp.json()
        items = ((data.get("web") or {}).get("results")) or []
        results: list[SearchResult] = []
        for item in items:
            snippet = item.get("description", "")
            extra = item.get("extra_snippets") or []
            if extra:
                snippet += " " + " ".join(extra[:2])
            results.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": snippet,
                "source": self.name,
            })
        log.info("Brave搜索完成", 查询=query[:50], 结果数=len(results))
        return results

    async def fetch(self, url: str) -> str | None:
        return None  # Brave 不支持抓取正文
