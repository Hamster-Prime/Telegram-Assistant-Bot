"""搜索 Provider 抽象(plan §7.2)。"""
from __future__ import annotations

from typing import Protocol, TypedDict


class SearchResult(TypedDict):
    title: str
    url: str
    snippet: str
    source: str


class ProviderError(Exception):
    """provider 业务失败(鉴权、配额、格式异常等)。"""


class Provider(Protocol):
    name: str

    async def search(self, query: str, count: int) -> list[SearchResult]: ...

    async def fetch(self, url: str) -> str | None:
        """返回 markdown 正文;不支持抓取则返回 None。"""
        ...
