"""Inbound Telegram media conversion tests."""
from __future__ import annotations

from types import SimpleNamespace

from aiogram.types import Message

from app.handlers.media import build_content


class FakeBot:
    def __init__(self, files: dict[str, tuple[str, bytes, int | None]]):
        self.files = files
        self.downloaded_file_ids: list[str] = []

    async def get_file(self, file_id: str):
        path, _data, size = self.files[file_id]
        self.downloaded_file_ids.append(file_id)
        return SimpleNamespace(file_path=path, file_size=size)

    async def download_file(self, file_path: str, *, destination):
        for path, data, _size in self.files.values():
            if path == file_path:
                destination.write(data)
                return destination
        raise AssertionError(f"unexpected file path: {file_path}")


class FakeFilesAPI:
    async def upload(self, data: bytes, name: str) -> str:
        raise AssertionError("unexpected Files API upload")


def make_svc(files: dict[str, tuple[str, bytes, int | None]]):
    return SimpleNamespace(bot=FakeBot(files), files_api=FakeFilesAPI())


def make_message(**fields) -> Message:
    payload = {
        "message_id": 1,
        "date": 0,
        "chat": {"id": 100, "type": "private"},
        "from": {"id": 1, "is_bot": False, "first_name": "U"},
    }
    payload.update(fields)
    return Message.model_validate(payload)


def image_urls(content) -> list[str]:
    assert isinstance(content, list)
    return [
        block["image_url"]["url"]
        for block in content
        if block.get("type") == "image_url"
    ]


async def test_photo_message_becomes_image_url():
    svc = make_svc({"photo-2": ("photos/p2.jpg", b"jpg-bytes", 9)})
    message = make_message(
        photo=[
            {
                "file_id": "photo-1",
                "file_unique_id": "u1",
                "width": 16,
                "height": 16,
                "file_size": 4,
            },
            {
                "file_id": "photo-2",
                "file_unique_id": "u2",
                "width": 64,
                "height": 64,
                "file_size": 9,
            },
        ],
    )

    content, query_text = await build_content(svc, message)

    assert image_urls(content)[0].startswith("data:image/jpeg;base64,")
    assert query_text == "(多媒体消息)"
    assert svc.bot.downloaded_file_ids == ["photo-2"]


async def test_static_sticker_becomes_image_url():
    svc = make_svc({"sticker-1": ("stickers/s1.webp", b"webp-bytes", 10)})
    message = make_message(
        sticker={
            "file_id": "sticker-1",
            "file_unique_id": "su1",
            "type": "regular",
            "width": 512,
            "height": 512,
            "is_animated": False,
            "is_video": False,
            "file_size": 10,
        },
    )

    content, _query_text = await build_content(svc, message)

    assert image_urls(content)[0].startswith("data:image/webp;base64,")
    assert svc.bot.downloaded_file_ids == ["sticker-1"]


async def test_animated_sticker_uses_thumbnail_as_image_url():
    svc = make_svc({"thumb-1": ("stickers/thumb.jpg", b"thumb-bytes", 11)})
    message = make_message(
        sticker={
            "file_id": "animated-1",
            "file_unique_id": "au1",
            "type": "regular",
            "width": 512,
            "height": 512,
            "is_animated": True,
            "is_video": False,
            "thumbnail": {
                "file_id": "thumb-1",
                "file_unique_id": "tu1",
                "width": 128,
                "height": 128,
                "file_size": 11,
            },
            "file_size": 100,
        },
    )

    content, _query_text = await build_content(svc, message)

    assert image_urls(content)[0].startswith("data:image/jpeg;base64,")
    assert svc.bot.downloaded_file_ids == ["thumb-1"]


async def test_gif_animation_uses_thumbnail_as_image_url():
    svc = make_svc({"thumb-2": ("animations/thumb.jpg", b"thumb-bytes", 11)})
    message = make_message(
        animation={
            "file_id": "gif-1",
            "file_unique_id": "gu1",
            "width": 320,
            "height": 240,
            "duration": 1,
            "file_name": "clip.gif",
            "mime_type": "image/gif",
            "thumbnail": {
                "file_id": "thumb-2",
                "file_unique_id": "tu2",
                "width": 128,
                "height": 96,
                "file_size": 11,
            },
            "file_size": 200,
        },
    )

    content, _query_text = await build_content(svc, message)

    assert image_urls(content)[0].startswith("data:image/jpeg;base64,")
    assert svc.bot.downloaded_file_ids == ["thumb-2"]


async def test_video_is_ignored_not_sent_as_multimodal_content():
    svc = make_svc({"video-1": ("videos/v1.mp4", b"video-bytes", 11)})
    message = make_message(
        video={
            "file_id": "video-1",
            "file_unique_id": "vu1",
            "width": 320,
            "height": 240,
            "duration": 1,
            "file_size": 11,
        },
    )

    content, query_text = await build_content(svc, message)

    assert content is None
    assert query_text == ""
    assert svc.bot.downloaded_file_ids == []
