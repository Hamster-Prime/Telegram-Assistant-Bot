"""MiniMax HTTP 客户端 ★ —— 多 API Key fallback + httpx 单例 + 错误码映射。

Key 调度策略(用户需求):
- MINIMAX_API_KEYS 支持逗号分隔填写多个 key
- 每次请求按顺序使用 key:每个 key 失败后重试 1 次(共 2 次尝试)
- 再失败切换下一个 key,直到所有 key 全部失败
- 全部失败抛 AllKeysFailedError,由上层向用户报错

错误分类:
- 「key 级失败」(限流 1002 / 鉴权 1004,2049 / 余额不足 1008 / 超时 / 5xx / 429):
  重试 + 换 key 有意义 → 走 fallback 链
- 「请求级失败」(内容敏感 1026 / 参数错误 2013 / 非法字符 1042):
  换 key 无意义 → 立即抛出,不消耗其余 key
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.logging import get_logger

log = get_logger("minimax.client")

# base_resp.status_code → 含义(中文)
_CODE_MEANING = {
    0: "成功",
    1002: "触发限流",
    1004: "鉴权失败",
    2049: "鉴权失败(无效API Key)",
    1008: "账户余额不足",
    1026: "内容涉及敏感信息",
    2013: "请求参数错误",
    1042: "非法字符超过10%",
}

# 请求级错误:换 key / 重试均无意义,立即抛出
_NON_RETRYABLE_CODES = {1026, 2013, 1042}


def _parse_minimax_error(body_text: str) -> tuple[int, str]:
    """从 4xx 响应体解析 MiniMax 错误码与消息。

    兼容两种形态:
    - 网关/OpenAI 风格:{"error": {"message": "input new_sensitive (1026)"}}
    - MiniMax base_resp:{"base_resp": {"status_code": 1026, "status_msg": "..."}}
    解析不出错误码时返回 (0, 截断原文)。
    """
    try:
        data = json.loads(body_text)
    except (json.JSONDecodeError, ValueError):
        return 0, body_text[:200]
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            msg = err.get("message", "") or ""
            m = re.search(r"\((\d{3,4})\)", msg)
            if m:
                return int(m.group(1)), msg
            return 0, msg or body_text[:200]
        base = data.get("base_resp")
        if isinstance(base, dict) and base.get("status_code"):
            return int(base["status_code"]), base.get("status_msg", "") or ""
    return 0, body_text[:200]


def mask_key(key: str) -> str:
    """日志中脱敏显示 key:前8后4。"""
    if len(key) <= 16:
        return key[:4] + "****"
    return f"{key[:8]}…{key[-4:]}"


class MiniMaxError(Exception):
    """MiniMax 业务错误(base_resp.status_code != 0 或 HTTP 错误)。"""

    def __init__(self, code: int, msg: str, trace_id: str = "") -> None:
        self.code = code
        self.msg = msg
        self.trace_id = trace_id
        meaning = _CODE_MEANING.get(code, "未知错误")
        super().__init__(f"MiniMax错误 code={code}({meaning}) msg={msg} trace_id={trace_id}")

    @property
    def retryable(self) -> bool:
        """是否值得重试/换 key。"""
        return self.code not in _NON_RETRYABLE_CODES

    def user_message(self) -> str:
        """面向用户的中文提示(供 agent / errors 统一调用)。"""
        if self.code == 1026:
            return ("🚫 抱歉,本次涉及的内容被判定为敏感信息,无法处理啦~"
                    "换个说法或换个话题再试试看哦。")
        if self.code == 1008:
            return "❌ MiniMax 账户余额不足,请联系管理员充值。"
        return f"❌ MiniMax 服务错误(code={self.code}):{self.msg}"


class AllKeysFailedError(Exception):
    """所有 API Key(各重试 1 次)全部失败。attempts 记录每次失败明细。"""

    def __init__(self, endpoint: str, attempts: list[dict[str, Any]]) -> None:
        self.endpoint = endpoint
        self.attempts = attempts
        lines = "; ".join(
            f"Key#{a['key_index']}({a['key']}) 第{a['attempt']}次: {a['error']}"
            for a in attempts
        )
        super().__init__(f"所有 MiniMax API Key 均调用失败 [{endpoint}]: {lines}")

    def user_message(self) -> str:
        """给用户看的中文报错。"""
        n_keys = len({a["key_index"] for a in self.attempts})
        return (
            f"❌ MiniMax 服务调用失败:已尝试全部 {n_keys} 个 API Key"
            f"(每个重试 1 次,共 {len(self.attempts)} 次尝试),均未成功。"
            f"请稍后再试或联系管理员检查 Key 配置。"
        )


class MiniMaxClient:
    """httpx 单例 + 多 Key fallback。所有 MiniMax API 模块共用本类。"""

    RETRIES_PER_KEY = 1  # 每个 key 失败后额外重试 1 次(共 2 次尝试)

    def __init__(
        self,
        api_keys: list[str],
        base_url: str = "https://api.minimaxi.com/v1",
        *,
        timeout_s: float = 120.0,
        max_connections: int = 100,
    ) -> None:
        if not api_keys:
            raise ValueError("至少需要配置一个 MINIMAX_API_KEYS")
        self._keys = list(api_keys)
        self._base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s, connect=10.0),
            limits=httpx.Limits(max_connections=max_connections),
        )
        log.info("MiniMax客户端已初始化", Key数量=len(self._keys),
                 Key列表=[mask_key(k) for k in self._keys], 基础地址=self._base_url)

    @property
    def key_count(self) -> int:
        return len(self._keys)

    async def close(self) -> None:
        await self._http.aclose()
        log.info("MiniMax客户端已关闭")

    # ── 内部:单次请求(指定 key) ────────────────────────────
    def _headers(self, key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    @staticmethod
    def _check_base_resp(data: dict[str, Any]) -> None:
        """检查 base_resp.status_code,非 0 抛 MiniMaxError。"""
        base = data.get("base_resp") or {}
        code = base.get("status_code", 0)
        if code != 0:
            raise MiniMaxError(code, base.get("status_msg", ""), data.get("trace_id", ""))

    async def _once_json(self, method: str, endpoint: str, key: str,
                         payload: dict | None, params: dict | None) -> dict[str, Any]:
        url = f"{self._base_url}{endpoint}"
        resp = await self._http.request(
            method, url, headers=self._headers(key),
            json=payload if method == "POST" else None,
            params=params,
        )
        if resp.status_code in (401, 403):
            raise MiniMaxError(1004, f"HTTP {resp.status_code} 鉴权被拒")
        if resp.status_code == 429:
            raise MiniMaxError(1002, "HTTP 429 限流")
        if resp.status_code >= 500:
            raise MiniMaxError(1002, f"HTTP {resp.status_code} 服务端错误")
        if resp.status_code >= 400:
            # 其余 4xx(尤其 422 内容审核 1026):读 body 解析错误码,按请求级错误处理。
            # 无法解析则默认 2013(请求参数错误,非可重试)—— 4xx 换 key 无意义。
            code, emsg = _parse_minimax_error(resp.text)
            raise MiniMaxError(code or 2013, emsg or f"HTTP {resp.status_code} 请求错误")
        resp.raise_for_status()
        data = resp.json()
        self._check_base_resp(data)
        return data

    # ── 对外:带 fallback 的 JSON 请求 ──────────────────────
    async def request_json(self, method: str, endpoint: str, *,
                           payload: dict | None = None,
                           params: dict | None = None) -> dict[str, Any]:
        """多 Key fallback 请求:每 key 重试 1 次 → 切下一个 → 全败抛 AllKeysFailedError。"""
        attempts: list[dict[str, Any]] = []
        for key_idx, key in enumerate(self._keys, start=1):
            for attempt in range(1, self.RETRIES_PER_KEY + 2):  # 1 + 重试1 = 2次
                t0 = time.monotonic()
                try:
                    log.debug("MiniMax请求开始", 接口=endpoint, Key序号=key_idx,
                              Key=mask_key(key), 尝试=attempt)
                    data = await self._once_json(method, endpoint, key, payload, params)
                    elapsed = round((time.monotonic() - t0) * 1000)
                    log.info("MiniMax请求成功", 接口=endpoint, Key序号=key_idx,
                             Key=mask_key(key), 尝试=attempt, 耗时毫秒=elapsed)
                    return data
                except MiniMaxError as e:
                    elapsed = round((time.monotonic() - t0) * 1000)
                    if not e.retryable:
                        log.error("MiniMax请求级错误(不重试不换Key)", 接口=endpoint,
                                  Key序号=key_idx, 错误码=e.code,
                                  含义=_CODE_MEANING.get(e.code, "未知"),
                                  详情=e.msg, 耗时毫秒=elapsed)
                        raise
                    attempts.append({"key_index": key_idx, "key": mask_key(key),
                                     "attempt": attempt, "error": f"code={e.code} {e.msg}"})
                    log.warning("MiniMax调用失败", 接口=endpoint, Key序号=key_idx,
                                Key=mask_key(key), 尝试=attempt, 错误码=e.code,
                                含义=_CODE_MEANING.get(e.code, "未知"),
                                详情=e.msg, 耗时毫秒=elapsed,
                                下一步="重试同Key" if attempt == 1 else "切换下一个Key")
                except (httpx.TimeoutException, httpx.HTTPError) as e:
                    elapsed = round((time.monotonic() - t0) * 1000)
                    attempts.append({"key_index": key_idx, "key": mask_key(key),
                                     "attempt": attempt,
                                     "error": f"{type(e).__name__}: {e}"})
                    log.warning("MiniMax网络异常", 接口=endpoint, Key序号=key_idx,
                                Key=mask_key(key), 尝试=attempt,
                                异常类型=type(e).__name__, 详情=str(e), 耗时毫秒=elapsed,
                                下一步="重试同Key" if attempt == 1 else "切换下一个Key")
                if attempt == 1:
                    await asyncio.sleep(0.5)  # 同 key 重试前短退避
            if key_idx < len(self._keys):
                log.info("切换到下一个API Key", 接口=endpoint,
                         旧Key序号=key_idx, 新Key序号=key_idx + 1,
                         新Key=mask_key(self._keys[key_idx]))
        err = AllKeysFailedError(endpoint, attempts)
        log.error("所有API Key全部失败", 接口=endpoint, Key总数=len(self._keys),
                  总尝试次数=len(attempts),
                  失败明细="; ".join(a["error"] for a in attempts))
        raise err

    async def post(self, endpoint: str, payload: dict) -> dict[str, Any]:
        return await self.request_json("POST", endpoint, payload=payload)

    async def get(self, endpoint: str, params: dict | None = None) -> dict[str, Any]:
        return await self.request_json("GET", endpoint, params=params)

    # ── 对外:带 fallback 的 SSE 流式请求 ───────────────────
    async def stream_sse(self, endpoint: str, payload: dict) -> AsyncIterator[dict[str, Any]]:
        """流式 POST,逐条 yield SSE data JSON。

        fallback 规则:在「收到第一个数据块之前」失败 → 按多 Key 链重试/切换;
        已开始产出后中断 → 直接抛出(无法透明重放,由上层按已收内容兜底)。
        """
        attempts: list[dict[str, Any]] = []
        url = f"{self._base_url}{endpoint}"
        for key_idx, key in enumerate(self._keys, start=1):
            for attempt in range(1, self.RETRIES_PER_KEY + 2):
                t0 = time.monotonic()
                yielded_any = False
                try:
                    log.debug("MiniMax流式请求开始", 接口=endpoint, Key序号=key_idx,
                              Key=mask_key(key), 尝试=attempt)
                    async with self._http.stream(
                        "POST", url, headers=self._headers(key), json=payload
                    ) as resp:
                        if resp.status_code in (401, 403):
                            raise MiniMaxError(1004, f"HTTP {resp.status_code} 鉴权被拒")
                        if resp.status_code == 429:
                            raise MiniMaxError(1002, "HTTP 429 限流")
                        if resp.status_code >= 500:
                            raise MiniMaxError(1002, f"HTTP {resp.status_code} 服务端错误")
                        if resp.status_code >= 400:
                            # 其余 4xx(尤其 422 内容审核 1026):读 body 解析错误码,
                            # 按请求级错误处理(非可重试),不再烧 key。
                            body = (await resp.aread()).decode("utf-8", "replace")
                            code, emsg = _parse_minimax_error(body)
                            raise MiniMaxError(code or 2013,
                                               emsg or f"HTTP {resp.status_code} 请求错误")
                        resp.raise_for_status()
                        async for line in resp.aiter_lines():
                            line = line.strip()
                            if not line or not line.startswith("data:"):
                                continue
                            raw = line[5:].strip()
                            if raw == "[DONE]":
                                log.info("MiniMax流式请求完成", 接口=endpoint,
                                         Key序号=key_idx,
                                         耗时毫秒=round((time.monotonic() - t0) * 1000))
                                return
                            try:
                                chunk = json.loads(raw)
                            except json.JSONDecodeError:
                                log.warning("SSE数据块JSON解析失败", 接口=endpoint,
                                            原始内容=raw[:200])
                                continue
                            # 流中亦可能带 base_resp 错误
                            self._check_base_resp(chunk)
                            if not yielded_any:
                                yielded_any = True
                                log.info("MiniMax流式首块到达", 接口=endpoint,
                                         Key序号=key_idx, Key=mask_key(key), 尝试=attempt,
                                         首块毫秒=round((time.monotonic() - t0) * 1000))
                            yield chunk
                    # 流自然结束(无 [DONE] 也算完成)
                    log.info("MiniMax流式请求结束", 接口=endpoint, Key序号=key_idx,
                             耗时毫秒=round((time.monotonic() - t0) * 1000))
                    return
                except MiniMaxError as e:
                    if yielded_any:
                        log.error("流式中途出错(已产出部分内容,不再fallback)",
                                  接口=endpoint, Key序号=key_idx, 错误码=e.code, 详情=e.msg)
                        raise
                    if not e.retryable:
                        log.error("MiniMax流式请求级错误(不重试不换Key)", 接口=endpoint,
                                  Key序号=key_idx, 错误码=e.code, 详情=e.msg)
                        raise
                    attempts.append({"key_index": key_idx, "key": mask_key(key),
                                     "attempt": attempt, "error": f"code={e.code} {e.msg}"})
                    log.warning("MiniMax流式调用失败", 接口=endpoint, Key序号=key_idx,
                                Key=mask_key(key), 尝试=attempt, 错误码=e.code, 详情=e.msg,
                                下一步="重试同Key" if attempt == 1 else "切换下一个Key")
                except (httpx.TimeoutException, httpx.HTTPError) as e:
                    if yielded_any:
                        log.error("流式中途网络中断(已产出部分内容,不再fallback)",
                                  接口=endpoint, Key序号=key_idx,
                                  异常类型=type(e).__name__, 详情=str(e))
                        raise
                    attempts.append({"key_index": key_idx, "key": mask_key(key),
                                     "attempt": attempt,
                                     "error": f"{type(e).__name__}: {e}"})
                    log.warning("MiniMax流式网络异常", 接口=endpoint, Key序号=key_idx,
                                Key=mask_key(key), 尝试=attempt,
                                异常类型=type(e).__name__, 详情=str(e),
                                下一步="重试同Key" if attempt == 1 else "切换下一个Key")
                if attempt == 1:
                    await asyncio.sleep(0.5)
            if key_idx < len(self._keys):
                log.info("切换到下一个API Key", 接口=endpoint,
                         旧Key序号=key_idx, 新Key序号=key_idx + 1,
                         新Key=mask_key(self._keys[key_idx]))
        err = AllKeysFailedError(endpoint, attempts)
        log.error("所有API Key全部失败(流式)", 接口=endpoint, Key总数=len(self._keys),
                  总尝试次数=len(attempts),
                  失败明细="; ".join(a["error"] for a in attempts))
        raise err
