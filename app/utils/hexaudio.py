"""hex/base64 音频解码 —— CPU 密集,放线程池执行。"""
from __future__ import annotations

import asyncio
import base64
import binascii


def _decode_hex_sync(hex_str: str) -> bytes:
    return binascii.unhexlify(hex_str.strip())


def _decode_b64_sync(b64_str: str) -> bytes:
    return base64.b64decode(b64_str)


async def decode_hex_audio(hex_str: str) -> bytes:
    """hex 字符串 → bytes(线程池,避免大音频阻塞事件循环)。"""
    return await asyncio.to_thread(_decode_hex_sync, hex_str)


async def decode_b64(b64_str: str) -> bytes:
    return await asyncio.to_thread(_decode_b64_sync, b64_str)


async def encode_b64(data: bytes) -> str:
    return await asyncio.to_thread(lambda: base64.b64encode(data).decode("ascii"))
