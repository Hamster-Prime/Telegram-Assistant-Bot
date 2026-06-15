from __future__ import annotations

import pytest

from app.utils.audio import to_ogg_opus


@pytest.mark.asyncio
async def test_to_ogg_opus_rejects_invalid_audio():
    with pytest.raises(RuntimeError, match="ffmpeg audio conversion failed"):
        await to_ogg_opus(b"not an audio file")
