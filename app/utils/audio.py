"""Audio conversion helpers for Telegram delivery."""
from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path


async def to_ogg_opus(data: bytes) -> bytes:
    """Convert arbitrary audio bytes to OGG/Opus for Telegram voice notes."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found")
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "source.audio"
        dst = Path(td) / "voice.ogg"
        src.write_bytes(data)
        proc = await asyncio.create_subprocess_exec(
            ffmpeg,
            "-y",
            "-i",
            str(src),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "48000",
            "-c:a",
            "libopus",
            "-b:a",
            "48k",
            str(dst),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            detail = stderr.decode("utf-8", "ignore")[-300:]
            raise RuntimeError(f"ffmpeg audio conversion failed:{detail}")
        return dst.read_bytes()
