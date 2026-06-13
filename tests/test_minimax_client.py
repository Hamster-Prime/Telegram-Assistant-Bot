"""MiniMax 客户端多 Key fallback 测试。

验证用户需求:每个 key 重试 1 次,失败切换下一个,直到全部失败后报错。
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.minimax.client import (
    AllKeysFailedError,
    MiniMaxClient,
    MiniMaxError,
    _parse_minimax_error,
    mask_key,
)


def make_client(keys: list[str], handler) -> MiniMaxClient:
    client = MiniMaxClient(keys, base_url="https://api.test/v1")
    # 替换内部 httpx 客户端为 MockTransport
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


def ok_json(payload: dict) -> httpx.Response:
    return httpx.Response(200, json={**payload, "base_resp": {"status_code": 0, "status_msg": "success"}})


class CallRecorder:
    """记录每次请求用的 key 与次数。"""

    def __init__(self):
        self.calls: list[str] = []

    def key_of(self, request: httpx.Request) -> str:
        auth = request.headers.get("Authorization", "")
        return auth.removeprefix("Bearer ").strip()


async def test_first_key_success():
    """第一个 key 直接成功:只调用 1 次。"""
    rec = CallRecorder()

    def handler(request: httpx.Request) -> httpx.Response:
        rec.calls.append(rec.key_of(request))
        return ok_json({"choices": []})

    client = make_client(["k1", "k2"], handler)
    data = await client.post("/chat/completions", {"model": "m"})
    assert data["base_resp"]["status_code"] == 0
    assert rec.calls == ["k1"]
    await client.close()


async def test_retry_same_key_then_succeed():
    """key1 第一次失败,重试 1 次成功:不切换 key。"""
    rec = CallRecorder()

    def handler(request: httpx.Request) -> httpx.Response:
        key = rec.key_of(request)
        rec.calls.append(key)
        if len(rec.calls) == 1:
            return httpx.Response(500, text="boom")
        return ok_json({})

    client = make_client(["k1", "k2"], handler)
    await client.post("/x", {})
    assert rec.calls == ["k1", "k1"]  # 同 key 重试 1 次
    await client.close()


async def test_fallback_to_second_key():
    """key1 两次都失败 → 切换 key2 成功。"""
    rec = CallRecorder()

    def handler(request: httpx.Request) -> httpx.Response:
        key = rec.key_of(request)
        rec.calls.append(key)
        if key == "k1":
            return httpx.Response(429, text="rate limited")
        return ok_json({})

    client = make_client(["k1", "k2"], handler)
    await client.post("/x", {})
    assert rec.calls == ["k1", "k1", "k2"]  # k1 两次(原始+重试1) → k2
    await client.close()


async def test_all_keys_failed():
    """所有 key 全败:每个 key 尝试 2 次,最后抛 AllKeysFailedError。"""
    rec = CallRecorder()

    def handler(request: httpx.Request) -> httpx.Response:
        rec.calls.append(rec.key_of(request))
        return httpx.Response(500, text="down")

    client = make_client(["k1", "k2", "k3"], handler)
    with pytest.raises(AllKeysFailedError) as ei:
        await client.post("/x", {})
    # 3 个 key × 2 次尝试 = 6 次
    assert rec.calls == ["k1", "k1", "k2", "k2", "k3", "k3"]
    assert len(ei.value.attempts) == 6
    # 用户报错为中文且包含 key 数量
    msg = ei.value.user_message()
    assert "3 个 API Key" in msg and "失败" in msg
    await client.close()


async def test_auth_error_switches_key():
    """鉴权失败(1004/2049)也按 key 级失败处理:重试后切下一个。"""
    rec = CallRecorder()

    def handler(request: httpx.Request) -> httpx.Response:
        key = rec.key_of(request)
        rec.calls.append(key)
        if key == "k1":
            return httpx.Response(200, json={
                "base_resp": {"status_code": 2049, "status_msg": "invalid api key"}})
        return ok_json({})

    client = make_client(["k1", "k2"], handler)
    await client.post("/x", {})
    assert rec.calls == ["k1", "k1", "k2"]
    await client.close()


async def test_non_retryable_error_raises_immediately():
    """请求级错误(1026 内容敏感):不重试、不换 key,立即抛出。"""
    rec = CallRecorder()

    def handler(request: httpx.Request) -> httpx.Response:
        rec.calls.append(rec.key_of(request))
        return httpx.Response(200, json={
            "base_resp": {"status_code": 1026, "status_msg": "sensitive"}})

    client = make_client(["k1", "k2"], handler)
    with pytest.raises(MiniMaxError) as ei:
        await client.post("/x", {})
    assert ei.value.code == 1026
    assert rec.calls == ["k1"]  # 只调用 1 次
    await client.close()


async def test_stream_fallback_before_first_chunk():
    """流式:首块前失败 → 按链切 key;k2 正常产出。"""
    rec = CallRecorder()

    def handler(request: httpx.Request) -> httpx.Response:
        key = rec.key_of(request)
        rec.calls.append(key)
        if key == "k1":
            return httpx.Response(500, text="down")
        sse = (
            'data: {"choices":[{"delta":{"content":"你好"}}],"base_resp":{"status_code":0}}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(200, content=sse.encode(), headers={"Content-Type": "text/event-stream"})

    client = make_client(["k1", "k2"], handler)
    chunks = [c async for c in client.stream_sse("/chat/completions", {})]
    assert rec.calls == ["k1", "k1", "k2"]
    assert chunks and chunks[0]["choices"][0]["delta"]["content"] == "你好"
    await client.close()


async def test_stream_all_failed():
    """流式:全部 key 失败 → AllKeysFailedError。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    client = make_client(["k1", "k2"], handler)
    with pytest.raises(AllKeysFailedError):
        async for _ in client.stream_sse("/x", {}):
            pass
    await client.close()


def test_mask_key():
    assert mask_key("short") == "shor****"
    masked = mask_key("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9")
    assert masked.startswith("eyJhbGci") and masked.endswith("CJ9") is False or "…" in masked


def test_keys_required():
    with pytest.raises(ValueError):
        MiniMaxClient([], base_url="https://x")


# ── HTTP 4xx 内容审核(1026)不烧 key ─────────────────────────────

async def test_http_422_sensitive_content_no_key_burn():
    """HTTP 422 内容审核(1026):读 body 解析码,不重试不换 key,只 1 次尝试。"""
    rec = CallRecorder()

    def handler(request: httpx.Request) -> httpx.Response:
        rec.calls.append(rec.key_of(request))
        return httpx.Response(422, json={
            "error": {"type": "unprocessable_entity_error",
                      "message": "input new_sensitive (1026)", "http_code": "422"}})

    client = make_client(["k1", "k2", "k3"], handler)
    with pytest.raises(MiniMaxError) as ei:
        await client.post("/chat/completions", {"model": "m"})
    assert ei.value.code == 1026
    assert rec.calls == ["k1"]  # 只调用 1 次,没烧后续 key
    await client.close()


async def test_http_400_unparseable_defaults_non_retryable():
    """无法解析错误码的 4xx:默认按非可重试(2013)处理,只 1 次尝试。"""
    rec = CallRecorder()

    def handler(request: httpx.Request) -> httpx.Response:
        rec.calls.append(rec.key_of(request))
        return httpx.Response(400, text="Bad Request")

    client = make_client(["k1", "k2"], handler)
    with pytest.raises(MiniMaxError) as ei:
        await client.post("/x", {})
    assert ei.value.code == 2013
    assert rec.calls == ["k1"]
    await client.close()


async def test_stream_422_sensitive_content_no_key_burn():
    """流式:首块前 HTTP 422 内容审核 → 不换 key,立即抛 1026。"""
    rec = CallRecorder()

    def handler(request: httpx.Request) -> httpx.Response:
        rec.calls.append(rec.key_of(request))
        return httpx.Response(422, json={
            "error": {"message": "input new_sensitive (1026)"}})

    client = make_client(["k1", "k2"], handler)
    with pytest.raises(MiniMaxError) as ei:
        async for _ in client.stream_sse("/chat/completions", {}):
            pass
    assert ei.value.code == 1026
    assert rec.calls == ["k1"]
    await client.close()


def test_parse_minimax_error_gateway_style():
    code, msg = _parse_minimax_error(
        '{"error":{"message":"input new_sensitive (1026)"}}')
    assert code == 1026
    assert "1026" in msg


def test_parse_minimax_error_base_resp_style():
    code, msg = _parse_minimax_error(
        '{"base_resp":{"status_code":1042,"status_msg":"非法字符"}}')
    assert code == 1042
    assert msg == "非法字符"


def test_parse_minimax_error_unparseable():
    code, msg = _parse_minimax_error("totally not json")
    assert code == 0
    assert "totally not json" in msg


def test_minimax_error_user_message_1026():
    e = MiniMaxError(1026, "sensitive")
    assert "敏感" in e.user_message()
    assert MiniMaxError(1008, "x").user_message().startswith("❌")
