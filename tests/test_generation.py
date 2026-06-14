"""VoiceAPI / 参考素材 / 图生图 / 图生视频 模式测试。"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from aiogram.types import Message

from app.handlers.media import ReferenceAssets, extract_references
from app.minimax.voice import VoiceAPI

# ── VoiceAPI ────────────────────────────────────────────────

class FakeClient:
    """模拟 MiniMaxClient,仅实现 post。"""

    def __init__(self, responses: dict[str, dict[str, Any]] | None = None):
        self._responses = responses or {}
        self.calls: list[tuple[str, dict]] = []

    async def post(self, endpoint: str, payload: dict) -> dict[str, Any]:
        self.calls.append((endpoint, payload))
        return self._responses.get(endpoint, {"base_resp": {"status_code": 0}})


async def test_voice_clone_builds_payload():
    client = FakeClient({
        "/voice_clone": {"demo_audio": "https://demo.mp3",
                         "base_resp": {"status_code": 0}},
    })
    api = VoiceAPI(client)
    result = await api.clone(
        123456, "MyVoice001",
        text="试听", model="speech-2.8-hd",
    )
    ep, payload = client.calls[0]
    assert ep == "/voice_clone"
    assert payload["file_id"] == 123456
    assert payload["voice_id"] == "MyVoice001"
    assert payload["text"] == "试听"
    assert payload["model"] == "speech-2.8-hd"
    assert result["demo_audio"] == "https://demo.mp3"


async def test_voice_design_returns_voice_id_and_audio():
    trial_hex = "48656c6c6f"  # "Hello"
    client = FakeClient({
        "/voice_design": {"voice_id": "ttv-voice-123",
                          "trial_audio": trial_hex,
                          "base_resp": {"status_code": 0}},
    })
    api = VoiceAPI(client)
    vid, audio = await api.design("温柔女声", "你好世界")
    assert vid == "ttv-voice-123"
    assert audio == b"Hello"
    _, payload = client.calls[0]
    assert payload["prompt"] == "温柔女声"
    assert payload["preview_text"] == "你好世界"


async def test_list_voices_passes_voice_type():
    client = FakeClient({
        "/get_voice": {
            "system_voice": [{"voice_id": "v1", "voice_name": "测试"}],
            "voice_cloning": [],
            "voice_generation": [],
            "base_resp": {"status_code": 0},
        },
    })
    api = VoiceAPI(client)
    data = await api.list_voices("system")
    _, payload = client.calls[0]
    assert payload["voice_type"] == "system"
    assert len(data["system_voice"]) == 1


# ── ReferenceAssets ─────────────────────────────────────────

def test_reference_assets_default_empty():
    refs = ReferenceAssets()
    assert not refs.has_any
    assert refs.images == []
    assert refs.audio is None


def test_reference_assets_has_any_with_image():
    refs = ReferenceAssets(images=["data:image/jpeg;base64,abc"])
    assert refs.has_any


def test_reference_assets_has_any_with_audio():
    refs = ReferenceAssets(audio=b"mp3", audio_filename="a.mp3")
    assert refs.has_any


# ── extract_references ──────────────────────────────────────

class FakeRefBot:
    def __init__(self, files: dict[str, tuple[str, bytes, int | None]]):
        self.files = files

    async def get_file(self, file_id: str):
        path, _data, size = self.files[file_id]
        return SimpleNamespace(file_path=path, file_size=size)

    async def download_file(self, file_path: str, *, destination):
        for path, data, _size in self.files.values():
            if path == file_path:
                destination.write(data)
                return destination
        raise AssertionError(f"unexpected path: {file_path}")


def _make_svc(files):
    return SimpleNamespace(bot=FakeRefBot(files))


def _make_message(**fields) -> Message:
    payload = {
        "message_id": 1, "date": 0,
        "chat": {"id": 100, "type": "private"},
        "from": {"id": 1, "is_bot": False, "first_name": "U"},
    }
    payload.update(fields)
    return Message.model_validate(payload)


async def test_extract_references_from_photo():
    svc = _make_svc({"p1": ("photos/p1.jpg", b"jpg", 5)})
    msg = _make_message(photo=[{
        "file_id": "p1", "file_unique_id": "u1",
        "width": 64, "height": 64, "file_size": 5,
    }])
    refs = await extract_references(svc, msg)
    assert len(refs.images) == 1
    assert refs.images[0].startswith("data:image/jpeg;base64,")
    assert refs.audio is None


async def test_extract_references_from_reply_photo():
    svc = _make_svc({
        "p1": ("photos/p1.jpg", b"jpg", 5),
        "p2": ("photos/p2.jpg", b"jpg2", 5),
    })
    reply = _make_message(photo=[{
        "file_id": "p2", "file_unique_id": "u2",
        "width": 64, "height": 64, "file_size": 5,
    }])
    msg = _make_message(reply_to_message=reply.model_dump(exclude_none=True))
    refs = await extract_references(svc, msg)
    assert len(refs.images) == 1


async def test_extract_references_from_two_photos_for_fl2v():
    """2张图 → refs.images 有 2 个元素(供首尾帧)"""
    svc = _make_svc({
        "p1": ("photos/p1.jpg", b"jpg1", 5),
        "p2": ("photos/p2.jpg", b"jpg2", 5),
    })
    reply = _make_message(photo=[{
        "file_id": "p2", "file_unique_id": "u2",
        "width": 64, "height": 64, "file_size": 5,
    }])
    msg = _make_message(
        photo=[{
            "file_id": "p1", "file_unique_id": "u1",
            "width": 64, "height": 64, "file_size": 5,
        }],
        reply_to_message=reply.model_dump(exclude_none=True),
    )
    refs = await extract_references(svc, msg)
    assert len(refs.images) == 2


async def test_extract_references_from_voice():
    svc = _make_svc({"v1": ("voice/v1.ogg", b"ogg-bytes", 10)})
    msg = _make_message(voice={
        "file_id": "v1", "file_unique_id": "uv1",
        "duration": 5, "file_size": 10,
    })
    refs = await extract_references(svc, msg)
    assert refs.audio == b"ogg-bytes"
    assert refs.audio_filename == "voice.ogg"


async def test_extract_references_empty_for_text_only():
    svc = _make_svc({})
    msg = _make_message(text="你好")
    refs = await extract_references(svc, msg)
    assert not refs.has_any


# ── Video API 模式选择 ─────────────────────────────────────

class FakeVideoClient:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def post(self, endpoint: str, payload: dict) -> dict[str, Any]:
        self.calls.append((endpoint, payload))
        return {"task_id": "task-1", "base_resp": {"status_code": 0}}


async def test_video_t2v_uses_default_model():
    from app.minimax.video import VideoAPI
    api = VideoAPI(FakeVideoClient(), "MiniMax-Hailuo-2.3")
    await api.create_task("测试")
    assert api._client.calls[0][0] == "/video_generation"
    payload = api._client.calls[0][1]
    assert payload["model"] == "MiniMax-Hailuo-2.3"
    assert "first_frame_image" not in payload


async def test_video_i2v_with_first_frame():
    from app.minimax.video import VideoAPI
    api = VideoAPI(FakeVideoClient(), "MiniMax-Hailuo-2.3")
    await api.create_task("让图片动起来", first_frame_image="data:image/jpeg;base64,xxx")
    payload = api._client.calls[0][1]
    assert payload["first_frame_image"].startswith("data:image")
    assert payload["model"] == "MiniMax-Hailuo-2.3"


async def test_video_fl2v_with_first_and_last_frame():
    from app.minimax.video import VideoAPI
    api = VideoAPI(FakeVideoClient(), "MiniMax-Hailuo-2.3")
    await api.create_task("成长",
                          first_frame_image="data:image/jpeg;base64,a",
                          last_frame_image="data:image/jpeg;base64,b")
    payload = api._client.calls[0][1]
    assert payload["model"] == "MiniMax-Hailuo-02"
    assert payload["first_frame_image"].startswith("data:image")
    assert payload["last_frame_image"].startswith("data:image")


async def test_video_s2v_with_subject_reference():
    from app.minimax.video import VideoAPI
    api = VideoAPI(FakeVideoClient(), "MiniMax-Hailuo-2.3")
    await api.create_task("人物动作",
                          subject_reference=[{"type": "character",
                                              "image": ["data:image/jpeg;base64,x"]}])
    payload = api._client.calls[0][1]
    assert payload["model"] == "S2V-01"
    assert payload["subject_reference"][0]["image"][0].startswith("data:image")


# ── Image API 图生图 ────────────────────────────────────────

class FakeImageClient:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def post(self, endpoint: str, payload: dict) -> dict[str, Any]:
        self.calls.append((endpoint, payload))
        return {"data": {"image_urls": ["https://img.test/1.jpg"]},
                "base_resp": {"status_code": 0}}


async def test_image_t2i_no_subject_reference():
    from app.minimax.image import ImageAPI
    api = ImageAPI(FakeImageClient(), "image-01")
    urls = await api.generate("风景")
    payload = api._client.calls[0][1]
    assert "subject_reference" not in payload
    assert len(urls) == 1


async def test_image_i2i_with_subject_reference():
    from app.minimax.image import ImageAPI
    api = ImageAPI(FakeImageClient(), "image-01")
    await api.generate("油画风格",
                       subject_reference=[{"type": "character",
                                           "image_file": "data:image/jpeg;base64,x"}])
    payload = api._client.calls[0][1]
    assert payload["subject_reference"][0]["type"] == "character"
    assert payload["subject_reference"][0]["image_file"].startswith("data:image")


async def test_image_style_uses_live_model():
    from app.minimax.image import ImageAPI
    api = ImageAPI(FakeImageClient(), "image-01")
    await api.generate("漫画风", model="image-01-live",
                       style={"style_type": "漫画", "style_weight": 0.8})
    payload = api._client.calls[0][1]
    assert payload["model"] == "image-01-live"
    assert payload["style"]["style_type"] == "漫画"
