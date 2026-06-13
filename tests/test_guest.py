"""Guest handler behavior tests."""
from __future__ import annotations

from types import SimpleNamespace

from aiogram.types import Message

from app.db.models import User
from app.handlers import guest


class FakeBot:
    def __init__(self, files: dict[str, tuple[str, bytes, int | None]] | None = None):
        self.files = files or {}
        self.downloaded_file_ids: list[str] = []

    async def me(self):
        return SimpleNamespace(id=123, username="my_bot")

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


def make_svc(files: dict[str, tuple[str, bytes, int | None]] | None = None):
    return SimpleNamespace(
        bot=FakeBot(files),
        files_api=FakeFilesAPI(),
        limiter=object(),
        settings=SimpleNamespace(edit_throttle_ms=1),
    )


def make_guest_message(
    text: str | None = None,
    *,
    caption: str | None = None,
    photo: list[dict] | None = None,
    video: dict | None = None,
    reply_text: str | None = None,
    reply_caption: str | None = None,
    reply_photo: list[dict] | None = None,
) -> Message:
    payload = {
        "message_id": 2,
        "date": 0,
        "chat": {"id": 200, "type": "group"},
        "guest_query_id": "gq-1",
        "guest_bot_caller_user": {
            "id": 77,
            "is_bot": False,
            "first_name": "C",
            "username": "caller",
        },
    }
    if text is not None:
        payload["text"] = text
    if caption is not None:
        payload["caption"] = caption
    if photo is not None:
        payload["photo"] = photo
    if video is not None:
        payload["video"] = video
    if reply_text is not None or reply_caption is not None or reply_photo is not None:
        reply_payload = {
            "message_id": 1,
            "date": 0,
            "chat": {"id": 200, "type": "group"},
            "from": {"id": 9, "is_bot": False, "first_name": "R"},
        }
        if reply_text is not None:
            reply_payload["text"] = reply_text
        if reply_caption is not None:
            reply_payload["caption"] = reply_caption
        if reply_photo is not None:
            reply_payload["photo"] = reply_photo
        payload["reply_to_message"] = reply_payload
    return Message.model_validate(payload)


def image_urls(content) -> list[str]:
    assert isinstance(content, list)
    return [
        block["image_url"]["url"]
        for block in content
        if block.get("type") == "image_url"
    ]


async def test_guest_strips_only_bot_mention_from_question(monkeypatch):
    captured: dict = {}

    async def fake_pipeline(svc, user, message, content, renderer, **kwargs):
        captured["content"] = content
        captured["query_text"] = kwargs["query_text"]

    class FakeGuestRenderer:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(guest, "run_chat_pipeline", fake_pipeline)
    monkeypatch.setattr(guest, "GuestRenderer", FakeGuestRenderer)

    svc = make_svc()
    user = User(tg_id=77, username="caller", first_name="C")
    message = make_guest_message("@my_bot 帮我总结一下 @alice 的观点")

    await guest.process_guest_message(message, user, svc)

    assert captured["content"] == "帮我总结一下 @alice 的观点"
    assert captured["query_text"] == "帮我总结一下 @alice 的观点"


async def test_guest_strips_bot_mention_only_from_question_not_reply(monkeypatch):
    captured: dict = {}

    async def fake_pipeline(svc, user, message, content, renderer, **kwargs):
        captured["content"] = content

    class FakeGuestRenderer:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(guest, "run_chat_pipeline", fake_pipeline)
    monkeypatch.setattr(guest, "GuestRenderer", FakeGuestRenderer)

    svc = make_svc()
    user = User(tg_id=77, username="caller", first_name="C")
    message = make_guest_message(
        "@my_bot 回复 @bob",
        reply_text="@my_bot 原文里提到了机器人用户名",
    )

    await guest.process_guest_message(message, user, svc)

    assert "[引用的消息]\n@my_bot 原文里提到了机器人用户名" in captured["content"]
    assert "[召唤者的问题]\n回复 @bob" in captured["content"]


async def test_guest_photo_message_reaches_pipeline_as_image(monkeypatch):
    captured: dict = {}

    async def fake_pipeline(svc, user, message, content, renderer, **kwargs):
        captured["content"] = content
        captured["query_text"] = kwargs["query_text"]

    class FakeGuestRenderer:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(guest, "run_chat_pipeline", fake_pipeline)
    monkeypatch.setattr(guest, "GuestRenderer", FakeGuestRenderer)

    svc = make_svc({"photo-1": ("photos/p1.jpg", b"jpg-bytes", 9)})
    user = User(tg_id=77, username="caller", first_name="C")
    message = make_guest_message(
        caption="@my_bot 这张图是什么 @alice",
        photo=[{
            "file_id": "photo-1",
            "file_unique_id": "pu1",
            "width": 64,
            "height": 64,
            "file_size": 9,
        }],
    )

    await guest.process_guest_message(message, user, svc)

    assert captured["content"][0] == {"type": "text", "text": "这张图是什么 @alice"}
    assert image_urls(captured["content"])[0].startswith("data:image/jpeg;base64,")
    assert captured["query_text"] == "这张图是什么 @alice"
    assert svc.bot.downloaded_file_ids == ["photo-1"]


async def test_guest_reply_text_preserved_and_current_photo_included(monkeypatch):
    captured: dict = {}

    async def fake_pipeline(svc, user, message, content, renderer, **kwargs):
        captured["content"] = content
        captured["query_text"] = kwargs["query_text"]

    class FakeGuestRenderer:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(guest, "run_chat_pipeline", fake_pipeline)
    monkeypatch.setattr(guest, "GuestRenderer", FakeGuestRenderer)

    svc = make_svc({"photo-2": ("photos/p2.jpg", b"jpg-bytes", 9)})
    user = User(tg_id=77, username="caller", first_name="C")
    message = make_guest_message(
        caption="@my_bot 看看这个",
        photo=[{
            "file_id": "photo-2",
            "file_unique_id": "pu2",
            "width": 64,
            "height": 64,
            "file_size": 9,
        }],
        reply_text="@my_bot 原消息里的用户名不要删",
    )

    await guest.process_guest_message(message, user, svc)

    assert captured["content"][0]["text"].startswith(
        "[引用的消息]\n@my_bot 原消息里的用户名不要删"
    )
    assert "[召唤者的问题]\n看看这个" in captured["content"][0]["text"]
    assert image_urls(captured["content"])[0].startswith("data:image/jpeg;base64,")
    assert captured["query_text"].startswith("[引用的消息]\n@my_bot 原消息里的用户名不要删")


async def test_guest_video_message_is_ignored(monkeypatch):
    called = False

    async def fake_pipeline(svc, user, message, content, renderer, **kwargs):
        nonlocal called
        called = True

    class FakeGuestRenderer:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(guest, "run_chat_pipeline", fake_pipeline)
    monkeypatch.setattr(guest, "GuestRenderer", FakeGuestRenderer)

    svc = make_svc({"video-1": ("videos/v1.mp4", b"video-bytes", 9)})
    user = User(tg_id=77, username="caller", first_name="C")
    message = make_guest_message(
        video={
            "file_id": "video-1",
            "file_unique_id": "vu1",
            "width": 64,
            "height": 64,
            "duration": 1,
            "file_size": 9,
        },
    )

    await guest.process_guest_message(message, user, svc)

    assert called is False
    assert svc.bot.downloaded_file_ids == []


async def test_guest_includes_photo_from_replied_photo_message(monkeypatch):
    captured: dict = {}

    async def fake_pipeline(svc, user, message, content, renderer, **kwargs):
        captured["content"] = content
        captured["query_text"] = kwargs["query_text"]

    class FakeGuestRenderer:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(guest, "run_chat_pipeline", fake_pipeline)
    monkeypatch.setattr(guest, "GuestRenderer", FakeGuestRenderer)

    svc = make_svc({"reply-photo": ("photos/reply.jpg", b"reply-jpg", 9)})
    user = User(tg_id=77, username="caller", first_name="C")
    message = make_guest_message(
        "@my_bot 描述一下",
        reply_photo=[{
            "file_id": "reply-photo",
            "file_unique_id": "rpu1",
            "width": 64,
            "height": 64,
            "file_size": 9,
        }],
    )

    await guest.process_guest_message(message, user, svc)

    assert captured["content"][0]["text"] == "[召唤者的问题]\n描述一下"
    assert image_urls(captured["content"])[0].startswith("data:image/jpeg;base64,")
    assert captured["query_text"] == "[召唤者的问题]\n描述一下"
    assert svc.bot.downloaded_file_ids == ["reply-photo"]


async def test_guest_preserves_replied_photo_caption_and_includes_photo(monkeypatch):
    captured: dict = {}

    async def fake_pipeline(svc, user, message, content, renderer, **kwargs):
        captured["content"] = content
        captured["query_text"] = kwargs["query_text"]

    class FakeGuestRenderer:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(guest, "run_chat_pipeline", fake_pipeline)
    monkeypatch.setattr(guest, "GuestRenderer", FakeGuestRenderer)

    svc = make_svc({"reply-photo-2": ("photos/reply2.jpg", b"reply-jpg", 9)})
    user = User(tg_id=77, username="caller", first_name="C")
    message = make_guest_message(
        "@my_bot 描述一下",
        reply_caption="@my_bot 原图说明不要删",
        reply_photo=[{
            "file_id": "reply-photo-2",
            "file_unique_id": "rpu2",
            "width": 64,
            "height": 64,
            "file_size": 9,
        }],
    )

    await guest.process_guest_message(message, user, svc)

    assert captured["content"][0]["text"].startswith(
        "[引用的消息]\n@my_bot 原图说明不要删"
    )
    assert "[召唤者的问题]\n描述一下" in captured["content"][0]["text"]
    assert image_urls(captured["content"])[0].startswith("data:image/jpeg;base64,")
    assert captured["query_text"].startswith("[引用的消息]\n@my_bot 原图说明不要删")
    assert svc.bot.downloaded_file_ids == ["reply-photo-2"]
