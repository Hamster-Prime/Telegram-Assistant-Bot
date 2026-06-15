from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.concurrency import SendRateLimiter
from app.core.delivery import DirectDelivery, GuestDelivery
from app.core.richmsg import RichAttachmentCollector


class FakeFilesAPI:
    def __init__(self) -> None:
        self.downloaded: list[str] = []

    async def download(self, url: str) -> bytes:
        self.downloaded.append(url)
        return b"MP3_BYTES"


class FakeBot:
    def __init__(self) -> None:
        self.voice_error: Exception | None = None
        self.voices: list[tuple[int, str]] = []
        self.audios: list[tuple[int, str]] = []

    async def send_voice(self, chat_id, voice, **kwargs):
        filename = getattr(voice, "filename", "")
        if self.voice_error is not None:
            raise self.voice_error
        self.voices.append((chat_id, filename))
        return SimpleNamespace(message_id=1)

    async def send_audio(self, chat_id, audio, **kwargs):
        self.audios.append((chat_id, getattr(audio, "filename", "")))
        return SimpleNamespace(message_id=2)


class FakeGuestRenderer:
    def __init__(self) -> None:
        self._inline_message_id = "inline-1"
        self.rich_attachments = RichAttachmentCollector()
        self.pending: list[tuple[str, str, str | None]] = []

    def attach_pending(self, kind: str, url: str, note: str | None) -> None:
        self.pending.append((kind, url, note))

    def attach_rich_media(
        self,
        kind: str,
        url: str,
        *,
        label: str | None = None,
        note: str | None = None,
    ):
        return self.rich_attachments.add(kind, url, label=label, note=note)


@pytest.mark.asyncio
async def test_direct_delivery_sends_voice_bubble_with_converted_ogg(monkeypatch):
    bot = FakeBot()
    converted: list[bytes] = []

    async def fake_to_ogg_opus(data: bytes) -> bytes:
        converted.append(data)
        return b"OGG_OPUS_BYTES"

    monkeypatch.setattr("app.core.delivery.to_ogg_opus", fake_to_ogg_opus)
    delivery = DirectDelivery(bot, 100, SendRateLimiter(10_000), FakeFilesAPI())

    ok = await delivery.send_voice("https://cdn.test/speech.mp3")

    assert ok is True
    assert converted == [b"MP3_BYTES"]
    assert bot.voices == [(100, "speech.ogg")]
    assert bot.audios == []


@pytest.mark.asyncio
async def test_direct_delivery_falls_back_to_original_audio_only_after_voice_fails(monkeypatch):
    bot = FakeBot()
    bot.voice_error = RuntimeError("voice rejected")
    converted: list[bytes] = []

    async def fake_to_ogg_opus(data: bytes) -> bytes:
        converted.append(data)
        return b"OGG_OPUS_BYTES"

    monkeypatch.setattr("app.core.delivery.to_ogg_opus", fake_to_ogg_opus)
    delivery = DirectDelivery(bot, 100, SendRateLimiter(10_000), FakeFilesAPI())

    ok = await delivery.send_voice("https://cdn.test/speech.mp3")

    assert ok is True
    assert converted == [b"MP3_BYTES"]
    assert bot.voices == []
    assert bot.audios == [(100, "speech.mp3")]


@pytest.mark.asyncio
async def test_direct_delivery_infers_audio_filename_from_url_for_fallback(monkeypatch):
    bot = FakeBot()
    delivery = DirectDelivery(bot, 100, SendRateLimiter(10_000), FakeFilesAPI())

    async def fail_conversion(data: bytes) -> bytes:
        raise RuntimeError("conversion rejected")

    monkeypatch.setattr("app.core.delivery.to_ogg_opus", fail_conversion)

    ok = await delivery.send_voice("https://cdn.test/demo.mp3?token=1")

    assert ok is True
    assert bot.voices == []
    assert bot.audios == [(100, "demo.mp3")]


@pytest.mark.asyncio
async def test_guest_delivery_voice_attaches_rich_audio_without_pending_media():
    renderer = FakeGuestRenderer()
    delivery = GuestDelivery(renderer)

    ok = await delivery.send_voice("https://cdn.test/speech.mp3")

    assert ok is True
    assert renderer.pending == []
    pending = renderer.rich_attachments.pending()
    assert len(pending) == 1
    assert pending[0].kind == "audio"
    assert pending[0].url == "https://cdn.test/speech.mp3"
    assert pending[0].markdown == "![生成语音](https://cdn.test/speech.mp3)"
