"""DuckDuckGo —— 兜底 provider:ddgs 同步库,to_thread 包裹(plan §7.5)。"""
from __future__ import annotations

import asyncio

from app.logging import get_logger
from app.search.base import ProviderError, SearchResult

log = get_logger("search.duckduckgo")


class DuckDuckGoProvider:
    name = "duckduckgo"
    available = True  # 无需 Key

    def __init__(self, timeout_s: float = 8.0) -> None:
        self._timeout = timeout_s

    def _search_sync(self, query: str, count: int) -> list[dict]:
        from ddgs import DDGS

        with DDGS(timeout=self._timeout) as ddgs:
            return list(ddgs.text(query, max_results=count))

    async def search(self, query: str, count: int) -> list[SearchResult]:
        try:
            raw = await asyncio.wait_for(
                asyncio.to_thread(self._search_sync, query, count),
                timeout=self._timeout + 4,
            )
        except TimeoutError as e:
            raise ProviderError("DuckDuckGo 搜索超时") from e
        except Exception as e:
            raise ProviderError(f"DuckDuckGo 搜索异常: {e}") from e
        results: list[SearchResult] = [
            {
                "title": item.get("title", ""),
                "url": item.get("href", "") or item.get("url", ""),
                "snippet": item.get("body", ""),
                "source": self.name,
            }
            for item in raw
            if item.get("href") or item.get("url")
        ]
        log.info("DuckDuckGo搜索完成", 查询=query[:50], 结果数=len(results))
        return results

    async def fetch(self, url: str) -> str | None:
        return None  # 由 router 的直连兜底处理
