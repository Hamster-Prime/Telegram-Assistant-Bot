"""搜索/抓取回退链编排 —— Firecrawl → Brave → DuckDuckGo,每家重试 1 次(plan §7.1)。

判定失败:超时、5xx、429、鉴权失败、空结果(空结果视为失利,继续回落)。
整体硬上限 40s,防拖垮流式回复。
"""
from __future__ import annotations

import asyncio
import random
import re

import httpx

from app.logging import get_logger
from app.search.base import ProviderError, SearchResult
from app.utils.tokens import truncate_to_tokens

log = get_logger("search.router")


class AllProvidersFailed(Exception):
    def __init__(self, query: str, detail: str = "") -> None:
        self.query = query
        super().__init__(f"全部搜索源失败: {query} {detail}")

    def user_message(self) -> str:
        return "🌐 暂时无法联网搜索(所有搜索源均失败),请稍后再试。"


_TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_HTML_RE = re.compile(r"<[^>]+>")


def _html_to_text(html: str) -> str:
    """直连兜底的极简正文抽取(无 trafilatura 时)。"""
    text = _TAG_RE.sub(" ", html)
    text = _HTML_RE.sub(" ", text)
    return re.sub(r"\s{2,}", " ", text).strip()


class SearchRouter:
    RETRY_PER_PROVIDER = 1  # 每家额外重试 1 次 = 共 2 次尝试
    OVERALL_TIMEOUT_S = 40.0

    def __init__(self, providers: list, http: httpx.AsyncClient) -> None:
        self._providers = providers
        self._http = http

    async def search(self, query: str, count: int = 5) -> list[SearchResult]:
        """三家回退链搜索。全败抛 AllProvidersFailed。"""
        try:
            return await asyncio.wait_for(
                self._search_chain(query, count), timeout=self.OVERALL_TIMEOUT_S
            )
        except TimeoutError:
            log.error("搜索整体超时", 查询=query[:50], 上限秒=self.OVERALL_TIMEOUT_S)
            raise AllProvidersFailed(query, "整体超时")

    async def _search_chain(self, query: str, count: int) -> list[SearchResult]:
        failures: list[str] = []
        for provider in self._providers:
            if not getattr(provider, "available", True):
                log.info("跳过未配置的搜索源", 搜索源=provider.name)
                continue
            for attempt in range(1, self.RETRY_PER_PROVIDER + 2):
                try:
                    log.info("搜索尝试", 搜索源=provider.name, 尝试=attempt,
                             查询=query[:50])
                    res = await provider.search(query, count)
                    if res:
                        log.info("搜索命中", 搜索源=provider.name, 尝试=attempt,
                                 结果数=len(res))
                        return res
                    # 空结果视为失利
                    failures.append(f"{provider.name}#{attempt}:空结果")
                    log.warning("搜索源返回空结果", 搜索源=provider.name, 尝试=attempt)
                except (TimeoutError, ProviderError, httpx.TimeoutException, httpx.HTTPError) as e:
                    failures.append(f"{provider.name}#{attempt}:{type(e).__name__} {e}")
                    log.warning("搜索源调用失败", 搜索源=provider.name, 尝试=attempt,
                                异常类型=type(e).__name__, 详情=str(e)[:200],
                                下一步="短退避重试" if attempt == 1 else "回落下一家")
                if attempt == 1:
                    await asyncio.sleep(0.5 * (1 + random.random()))  # 0.5s × jitter
        log.error("全部搜索源失败", 查询=query[:50], 失败明细="; ".join(failures))
        raise AllProvidersFailed(query, "; ".join(failures))

    async def fetch(self, url: str, *, max_tokens: int = 6000) -> str:
        """WebFetch:Firecrawl scrape → 直连 httpx + 本地抽取,各含 1 次重试。"""
        failures: list[str] = []
        # 1) 支持 fetch 的 provider(目前 Firecrawl)
        for provider in self._providers:
            if not getattr(provider, "available", True):
                continue
            for attempt in range(1, self.RETRY_PER_PROVIDER + 2):
                try:
                    md = await provider.fetch(url)
                except (ProviderError, httpx.TimeoutException, httpx.HTTPError) as e:
                    failures.append(f"{provider.name}#{attempt}:{type(e).__name__}")
                    log.warning("抓取失败", 搜索源=provider.name, 尝试=attempt,
                                地址=url[:80], 异常类型=type(e).__name__, 详情=str(e)[:200])
                    if attempt == 1:
                        await asyncio.sleep(0.5)
                    continue
                if md is None:
                    break  # 该 provider 不支持抓取,换下一家
                if md.strip():
                    log.info("抓取命中", 搜索源=provider.name, 地址=url[:80],
                             正文长度=len(md))
                    return truncate_to_tokens(md, max_tokens)
                failures.append(f"{provider.name}#{attempt}:空正文")
                if attempt == 1:
                    await asyncio.sleep(0.5)

        # 2) 直连兜底(含 1 次重试)
        for attempt in (1, 2):
            try:
                log.info("直连抓取兜底", 地址=url[:80], 尝试=attempt)
                resp = await self._http.get(
                    url, timeout=15.0, follow_redirects=True,
                    headers={"User-Agent": "Mozilla/5.0 (compatible; AssistantBot/1.0)"},
                )
                resp.raise_for_status()
                text = _html_to_text(resp.text)
                if text:
                    log.info("直连抓取成功", 地址=url[:80], 正文长度=len(text))
                    return truncate_to_tokens(text, max_tokens)
                failures.append(f"direct#{attempt}:空正文")
            except (httpx.TimeoutException, httpx.HTTPError) as e:
                failures.append(f"direct#{attempt}:{type(e).__name__}")
                log.warning("直连抓取失败", 地址=url[:80], 尝试=attempt,
                            异常类型=type(e).__name__, 详情=str(e)[:200])
            if attempt == 1:
                await asyncio.sleep(0.5)

        log.error("网页抓取全部失败", 地址=url[:80], 失败明细="; ".join(failures))
        raise AllProvidersFailed(url, "; ".join(failures))
