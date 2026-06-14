"""MiniMax 搜索 —— 首选 provider:Token Plan /v1/coding_plan/search。

复用 MiniMax 主 Key 列表(Token Plan 订阅),多 Key 按顺序 fallback:
- Key 级失败(鉴权 401/403、限流 429/1002、余额 1008、无效Key 2049、5xx):
  切换下一个 Key 重试,直到全部失败。
- 请求级失败(内容敏感 1026、参数错误 2013、非法字符 1042):
  换 Key 无意义,立即抛出 ProviderError。

返回经 MiniMax 内容审核预过滤的摘要,回灌 chat API 时触发 1026 敏感信息的概率
低于第三方搜索源(Brave/Firecrawl)。不支持 web_fetch(交由 Firecrawl + 直连兜底)。

底层 API(源自 MiniMax CLI 源码):
  POST {base_url}/coding_plan/search   (base_url 已含 /v1)
  Authorization: Bearer <token_plan_key>
  body: {"q": query}
  resp: {"organic": [{"title","link","snippet","date"}], "base_resp": {...}}
  最多返回 10 条,无分页参数。
"""
from __future__ import annotations

import httpx

from app.logging import get_logger
from app.minimax.client import mask_key
from app.search.base import ProviderError, SearchResult

log = get_logger("search.minimax")

# Key 级错误码:换 Key 可能有意义(MiniMax client 同款分类)
# 鉴权 1004 / 无效Key 2049 / 限流 1002 / 余额 1008
_KEY_LEVEL_CODES = {1002, 1004, 2049, 1008}

# 请求级错误码:换 Key 无意义(内容敏感/参数错误/非法字符),立即抛出
_NON_RETRYABLE_CODES = {1026, 2013, 1042}


class _KeyLevelError(Exception):
    """MiniMax 搜索 Key 级失败(鉴权/限流/余额),换下一个 Key 重试。"""


class MiniMaxSearchProvider:
    name = "minimax"

    def __init__(self, http: httpx.AsyncClient, api_keys,
                 base_url: str = "https://api.minimaxi.com/v1",
                 timeout_s: float = 15.0) -> None:
        # 兼容单个 Key(str)与多个 Key(list)
        if isinstance(api_keys, str):
            api_keys = [api_keys]
        self._http = http
        self._keys = [k.strip() for k in api_keys if k and k.strip()]
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_s

    @property
    def available(self) -> bool:
        return bool(self._keys)

    def _headers(self, key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    async def search(self, query: str, count: int) -> list[SearchResult]:
        if not self._keys:
            raise ProviderError("未配置 MINIMAX_API_KEYS(Token Plan)")
        failures: list[str] = []
        for idx, key in enumerate(self._keys, start=1):
            try:
                return await self._search_once(key, query, count, idx)
            except _KeyLevelError as e:
                failures.append(f"Key#{idx}({mask_key(key)}): {e}")
                if idx < len(self._keys):
                    log.warning("MiniMax搜索Key失败,切换下一个Key",
                                Key序号=idx, Key=mask_key(key), 详情=str(e),
                                下一个Key=mask_key(self._keys[idx]))
                else:
                    log.error("MiniMax搜索全部Key失败", 查询=query[:50], Key总数=len(self._keys),
                              失败明细="; ".join(failures))
                continue
        raise ProviderError(f"所有 {len(self._keys)} 个 MiniMax Key 搜索均失败: "
                            + "; ".join(failures))

    async def _search_once(self, key: str, query: str, count: int, idx: int) -> list[SearchResult]:
        """单 Key 单次搜索。Key 级失败抛 _KeyLevelError;请求级失败抛 ProviderError。"""
        resp = await self._http.post(
            f"{self._base_url}/coding_plan/search",
            headers=self._headers(key),
            json={"q": query},
            timeout=self._timeout,
        )
        # HTTP 层:鉴权/限流为 Key 级;5xx 也按 Key 级处理(与 MiniMaxClient 一致,可换 Key 试)
        if resp.status_code in (401, 403):
            raise _KeyLevelError(f"鉴权失败 HTTP {resp.status_code}")
        if resp.status_code == 429:
            raise _KeyLevelError("HTTP 429 限流")
        if resp.status_code >= 500:
            raise _KeyLevelError(f"HTTP {resp.status_code} 服务端错误")
        if resp.status_code >= 400:
            # 其余 4xx(内容审核等):解析 base_resp,按请求级错误处理
            try:
                data = resp.json()
            except (ValueError, httpx.DecodingError):
                raise ProviderError(f"MiniMax 搜索 HTTP {resp.status_code} 请求错误")
            base = data.get("base_resp") or {}
            code = base.get("status_code", 0)
            raise ProviderError(f"MiniMax 搜索失败 code={code} {base.get('status_msg', '')}")
        resp.raise_for_status()
        data = resp.json()
        # MiniMax 统一 base_resp 错误码(status_code != 0 为失败)
        base = data.get("base_resp") or {}
        code = base.get("status_code", 0)
        if code != 0:
            msg = base.get("status_msg", "")
            if code in _KEY_LEVEL_CODES:
                raise _KeyLevelError(f"code={code}({msg})")
            # 请求级错误(1026/2013/1042 等):换 Key 无意义,立即抛出
            raise ProviderError(f"MiniMax 搜索失败 code={code}({msg})")
        items = data.get("organic") or []
        results: list[SearchResult] = [
            {
                "title": item.get("title", ""),
                "url": item.get("link", ""),
                "snippet": item.get("snippet", ""),
                "source": self.name,
            }
            for item in items
            if item.get("link")
        ]
        results = results[:count]  # API 无 count 参数,本地截断(最多返回 10 条)
        log.info("MiniMax搜索完成", 查询=query[:50], 结果数=len(results),
                 Key序号=idx, Key=mask_key(key))
        return results

    async def fetch(self, url: str) -> str | None:
        return None  # MiniMax 搜索不支持抓取正文
