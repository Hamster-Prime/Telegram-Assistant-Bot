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
        settings=SimpleNamespace(edit_throttle_ms=1, group_edit_throttle_ms=3,
                                 typing_refresh_s=4.0),
    )


def make_guest_message(
    text: str | None = None,
    *,
    caption: str | None = None,
    photo: list[dict] | None = None,
    video: dict | None = None,
    quote_text: str | None = None,
    reply_text: str | None = None,
    reply_caption: str | None = None,
    reply_photo: list[dict] | None = None,
    external_reply: dict | None = None,
    external_reply_photo: list[dict] | None = None,
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
    if quote_text is not None:
        payload["quote"] = {"text": quote_text, "position": 0}
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
    if external_reply is not None or external_reply_photo is not None:
        payload["external_reply"] = external_reply or {
            "origin": {
                "type": "user",
                "date": 0,
                "sender_user": {
                    "id": 9,
                    "is_bot": False,
                    "first_name": "R",
                },
            },
            "chat": {"id": 200, "type": "group"},
            "message_id": 1,
            "photo": external_reply_photo,
        }
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

    # 标记含发送者(R);正文跟在标记后
    assert "[引用的消息 · 👤 R" in captured["content"]
    assert "@my_bot 原文里提到了机器人用户名" in captured["content"]
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

    # 标记含发送者(R);正文跟在标记后
    assert captured["content"][0]["text"].startswith("[引用的消息 · 👤 R")
    assert "@my_bot 原消息里的用户名不要删" in captured["content"][0]["text"]
    assert "[召唤者的问题]\n看看这个" in captured["content"][0]["text"]
    assert image_urls(captured["content"])[0].startswith("data:image/jpeg;base64,")
    assert "[引用的消息" in captured["query_text"]


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

    # 标记含发送者(R);正文跟在标记后
    assert captured["content"][0]["text"].startswith("[引用的消息 · 👤 R")
    assert "@my_bot 原图说明不要删" in captured["content"][0]["text"]
    assert "[召唤者的问题]\n描述一下" in captured["content"][0]["text"]
    assert image_urls(captured["content"])[0].startswith("data:image/jpeg;base64,")
    assert "[引用的消息" in captured["query_text"]
    assert svc.bot.downloaded_file_ids == ["reply-photo-2"]


async def test_guest_includes_photo_from_external_reply(monkeypatch):
    captured: dict = {}

    async def fake_pipeline(svc, user, message, content, renderer, **kwargs):
        captured["content"] = content
        captured["query_text"] = kwargs["query_text"]

    class FakeGuestRenderer:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(guest, "run_chat_pipeline", fake_pipeline)
    monkeypatch.setattr(guest, "GuestRenderer", FakeGuestRenderer)

    svc = make_svc({"external-photo": ("photos/external.jpg", b"reply-jpg", 9)})
    user = User(tg_id=77, username="caller", first_name="C")
    message = make_guest_message(
        "@my_bot 描述一下",
        external_reply_photo=[{
            "file_id": "external-photo",
            "file_unique_id": "epu1",
            "width": 64,
            "height": 64,
            "file_size": 9,
        }],
    )

    await guest.process_guest_message(message, user, svc)

    assert captured["content"][0]["text"] == "[召唤者的问题]\n描述一下"
    assert image_urls(captured["content"])[0].startswith("data:image/jpeg;base64,")
    assert captured["query_text"] == "[召唤者的问题]\n描述一下"
    assert svc.bot.downloaded_file_ids == ["external-photo"]


async def test_guest_preserves_quote_text_from_external_photo_reply(monkeypatch):
    captured: dict = {}

    async def fake_pipeline(svc, user, message, content, renderer, **kwargs):
        captured["content"] = content
        captured["query_text"] = kwargs["query_text"]

    class FakeGuestRenderer:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(guest, "run_chat_pipeline", fake_pipeline)
    monkeypatch.setattr(guest, "GuestRenderer", FakeGuestRenderer)

    svc = make_svc({"external-photo-caption": ("photos/external.jpg", b"reply-jpg", 9)})
    user = User(tg_id=77, username="caller", first_name="C")
    message = make_guest_message(
        "@my_bot 描述一下",
        quote_text="@my_bot 原图说明不要删",
        external_reply_photo=[{
            "file_id": "external-photo-caption",
            "file_unique_id": "epcu1",
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
    assert svc.bot.downloaded_file_ids == ["external-photo-caption"]


async def test_guest_includes_static_sticker_from_external_reply(monkeypatch):
    captured: dict = {}

    async def fake_pipeline(svc, user, message, content, renderer, **kwargs):
        captured["content"] = content
        captured["query_text"] = kwargs["query_text"]

    class FakeGuestRenderer:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(guest, "run_chat_pipeline", fake_pipeline)
    monkeypatch.setattr(guest, "GuestRenderer", FakeGuestRenderer)

    svc = make_svc({"external-sticker": ("stickers/s1.webp", b"webp-bytes", 10)})
    user = User(tg_id=77, username="caller", first_name="C")
    message = make_guest_message(
        "@my_bot 描述一下",
        external_reply={
            "origin": {
                "type": "user",
                "date": 0,
                "sender_user": {"id": 9, "is_bot": False, "first_name": "R"},
            },
            "sticker": {
                "file_id": "external-sticker",
                "file_unique_id": "esu1",
                "type": "regular",
                "width": 512,
                "height": 512,
                "is_animated": False,
                "is_video": False,
                "file_size": 10,
            },
        },
    )

    await guest.process_guest_message(message, user, svc)

    assert captured["content"][0]["text"] == "[召唤者的问题]\n描述一下"
    assert image_urls(captured["content"])[0].startswith("data:image/webp;base64,")
    assert captured["query_text"] == "[召唤者的问题]\n描述一下"
    assert svc.bot.downloaded_file_ids == ["external-sticker"]


async def test_guest_includes_animated_sticker_thumbnail_from_external_reply(monkeypatch):
    captured: dict = {}

    async def fake_pipeline(svc, user, message, content, renderer, **kwargs):
        captured["content"] = content

    class FakeGuestRenderer:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(guest, "run_chat_pipeline", fake_pipeline)
    monkeypatch.setattr(guest, "GuestRenderer", FakeGuestRenderer)

    svc = make_svc({"external-thumb": ("stickers/thumb.jpg", b"thumb-bytes", 11)})
    user = User(tg_id=77, username="caller", first_name="C")
    message = make_guest_message(
        "@my_bot 描述一下",
        external_reply={
            "origin": {
                "type": "user",
                "date": 0,
                "sender_user": {"id": 9, "is_bot": False, "first_name": "R"},
            },
            "sticker": {
                "file_id": "animated-sticker",
                "file_unique_id": "asu1",
                "type": "regular",
                "width": 512,
                "height": 512,
                "is_animated": True,
                "is_video": False,
                "thumbnail": {
                    "file_id": "external-thumb",
                    "file_unique_id": "etu1",
                    "width": 128,
                    "height": 128,
                    "file_size": 11,
                },
                "file_size": 100,
            },
        },
    )

    await guest.process_guest_message(message, user, svc)

    assert image_urls(captured["content"])[0].startswith("data:image/jpeg;base64,")
    assert svc.bot.downloaded_file_ids == ["external-thumb"]


async def test_guest_includes_gif_thumbnail_from_external_reply(monkeypatch):
    captured: dict = {}

    async def fake_pipeline(svc, user, message, content, renderer, **kwargs):
        captured["content"] = content

    class FakeGuestRenderer:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(guest, "run_chat_pipeline", fake_pipeline)
    monkeypatch.setattr(guest, "GuestRenderer", FakeGuestRenderer)

    svc = make_svc({"external-gif-thumb": ("animations/thumb.jpg", b"thumb-bytes", 11)})
    user = User(tg_id=77, username="caller", first_name="C")
    message = make_guest_message(
        "@my_bot 描述一下",
        external_reply={
            "origin": {
                "type": "user",
                "date": 0,
                "sender_user": {"id": 9, "is_bot": False, "first_name": "R"},
            },
            "animation": {
                "file_id": "gif-1",
                "file_unique_id": "gu1",
                "width": 320,
                "height": 240,
                "duration": 1,
                "file_name": "clip.gif",
                "mime_type": "image/gif",
                "thumbnail": {
                    "file_id": "external-gif-thumb",
                    "file_unique_id": "egtu1",
                    "width": 128,
                    "height": 96,
                    "file_size": 11,
                },
                "file_size": 200,
            },
        },
    )

    await guest.process_guest_message(message, user, svc)

    assert image_urls(captured["content"])[0].startswith("data:image/jpeg;base64,")
    assert svc.bot.downloaded_file_ids == ["external-gif-thumb"]


async def test_guest_ignores_video_from_external_reply(monkeypatch):
    captured: dict = {}

    async def fake_pipeline(svc, user, message, content, renderer, **kwargs):
        captured["content"] = content
        captured["query_text"] = kwargs["query_text"]

    class FakeGuestRenderer:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(guest, "run_chat_pipeline", fake_pipeline)
    monkeypatch.setattr(guest, "GuestRenderer", FakeGuestRenderer)

    svc = make_svc({"external-video": ("videos/v1.mp4", b"video-bytes", 9)})
    user = User(tg_id=77, username="caller", first_name="C")
    message = make_guest_message(
        "@my_bot 描述一下",
        external_reply={
            "origin": {
                "type": "user",
                "date": 0,
                "sender_user": {"id": 9, "is_bot": False, "first_name": "R"},
            },
            "video": {
                "file_id": "external-video",
                "file_unique_id": "evu1",
                "width": 64,
                "height": 64,
                "duration": 1,
                "file_size": 9,
            },
        },
    )

    await guest.process_guest_message(message, user, svc)

    assert captured["content"] == "描述一下"
    assert captured["query_text"] == "描述一下"
    assert svc.bot.downloaded_file_ids == []


# ── Guest 命令分流(修复项 3)──────────────────────────────────
async def test_guest_split_command_parses_cmd_and_args():
    from app.handlers import guest_commands
    assert guest_commands._split_command("/reset") == ("reset", "")
    assert guest_commands._split_command("/reset@my_bot") == ("reset", "")
    assert guest_commands._split_command("/remember 记住这个") == ("remember", "记住这个")
    assert guest_commands._split_command("普通消息") == ("", "")


async def test_guest_command_help_returns_help_text():
    from app.handlers import guest_commands
    result = await guest_commands.execute_guest_command(
        make_svc(), User(tg_id=77, first_name="C"),
        make_guest_message("/help"))
    assert result is not None
    assert "助理机器人" in result


async def test_guest_command_start_greeting():
    from app.handlers import guest_commands
    result = await guest_commands.execute_guest_command(
        make_svc(), User(tg_id=77, first_name="小明"),
        make_guest_message("/start"))
    assert result is not None
    assert "小明" in result


async def test_guest_unknown_command_returns_none():
    """未知命令返回 None,调用方应落入 AI 流程(自然语言处理)。"""
    from app.handlers import guest_commands
    svc = make_svc()
    user = User(tg_id=77, username="caller", first_name="C")
    message = make_guest_message("/unknowncmd 参数")
    result = await guest_commands.execute_guest_command(svc, user, message)
    assert result is None


async def test_guest_non_command_returns_none():
    """非斜杠消息不是命令,返回 None。"""
    from app.handlers import guest_commands
    svc = make_svc()
    user = User(tg_id=77, username="caller", first_name="C")
    message = make_guest_message("帮我画一只猫")
    result = await guest_commands.execute_guest_command(svc, user, message)
    assert result is None


async def test_guest_reset_command_uses_logic(monkeypatch):
    """/reset 走 logic_reset(带 scope/owner 清理 Guest 残留记忆),不再落入 AI。"""
    from app.handlers import guest_commands
    called = {"reset": False, "scope": None, "owner": None}

    async def fake_reset(svc, user, chat_id, *, scope=None, owner=None):
        called["reset"] = True
        called["scope"] = scope
        called["owner"] = owner
        return "🧹 已清空"

    monkeypatch.setattr(guest_commands, "logic_reset", fake_reset)
    result = await guest_commands.execute_guest_command(
        make_svc(), User(tg_id=77, first_name="C"),
        make_guest_message("/reset"))
    assert called["reset"] is True
    # Guest /reset 必须透传 scope=chat/owner=chat_id,以便清理残留记忆
    assert called["scope"] == "chat"
    assert called["owner"] == 200
    assert result == "🧹 已清空"


# ── handle_guest 入口:命令路由(修复 @bot /cmd 漏判)─────────────
async def test_handle_guest_routes_mention_prefixed_command(monkeypatch):
    """@my_bot /help 经 handle_guest 走命令分流,不落入 AI。

    回归:inline 启动器发出的命令形如 "@bot /help",以 @ 开头,
    旧逻辑 startswith("/") 漏判导致落入 AI 流程。
    """
    answered: dict = {}

    async def fake_answer(bot, guest_query_id, text):
        answered["text"] = text

    pipeline_called = {"n": 0}

    async def fake_pipeline(*args, **kwargs):
        pipeline_called["n"] += 1

    monkeypatch.setattr(guest, "answer_guest_text", fake_answer)
    monkeypatch.setattr(guest, "run_chat_pipeline", fake_pipeline)

    svc = make_svc()
    user = User(tg_id=77, username="caller", first_name="C")
    message = make_guest_message("@my_bot /help")

    await guest.handle_guest(message, user, svc)

    assert "助理机器人" in answered.get("text", "")
    assert pipeline_called["n"] == 0  # 未落入 AI


async def test_handle_guest_routes_bare_command(monkeypatch):
    """纯 /help(无 @bot 前缀)仍正常走命令分流。"""
    answered: dict = {}

    async def fake_answer(bot, guest_query_id, text):
        answered["text"] = text

    async def fake_pipeline(*args, **kwargs):
        raise AssertionError("不应落入 AI 流程")

    monkeypatch.setattr(guest, "answer_guest_text", fake_answer)
    monkeypatch.setattr(guest, "run_chat_pipeline", fake_pipeline)

    svc = make_svc()
    user = User(tg_id=77, username="caller", first_name="C")
    message = make_guest_message("/help")

    await guest.handle_guest(message, user, svc)
    assert "助理机器人" in answered.get("text", "")


async def test_handle_guest_unknown_command_falls_through_to_ai(monkeypatch):
    """@my_bot /unknowncmd 未知命令 → 落入 AI 流程。"""
    answered: dict = {}

    async def fake_answer(bot, guest_query_id, text):
        answered["text"] = text

    pipeline_called = {"n": 0}

    async def fake_pipeline(*args, **kwargs):
        pipeline_called["n"] += 1

    class FakeGuestRenderer:
        def __init__(self, *a, **kw):
            pass

    monkeypatch.setattr(guest, "answer_guest_text", fake_answer)
    monkeypatch.setattr(guest, "run_chat_pipeline", fake_pipeline)
    monkeypatch.setattr(guest, "GuestRenderer", FakeGuestRenderer)

    svc = make_svc()
    user = User(tg_id=77, username="caller", first_name="C")
    message = make_guest_message("@my_bot /totallyunknown")

    await guest.handle_guest(message, user, svc)
    assert pipeline_called["n"] == 1  # 落入 AI


async def test_handle_guest_plain_text_goes_to_ai(monkeypatch):
    """@my_bot 普通文本 → 走 AI 流程(非命令)。"""
    pipeline_called = {"n": 0}

    async def fake_pipeline(*args, **kwargs):
        pipeline_called["n"] += 1

    class FakeGuestRenderer:
        def __init__(self, *a, **kw):
            pass

    monkeypatch.setattr(guest, "run_chat_pipeline", fake_pipeline)
    monkeypatch.setattr(guest, "GuestRenderer", FakeGuestRenderer)

    svc = make_svc()
    user = User(tg_id=77, username="caller", first_name="C")
    message = make_guest_message("@my_bot 讲个笑话")

    await guest.handle_guest(message, user, svc)
    assert pipeline_called["n"] == 1


async def test_execute_guest_command_strips_mention_prefix():
    """execute_guest_command 带 bot_username 时剥离 @bot 前缀后再解析命令。"""
    from app.handlers import guest_commands
    svc = make_svc()
    user = User(tg_id=77, first_name="C")
    # @my_bot /help → 剥离后 /help → 命中 help 命令
    message = make_guest_message("@my_bot /help")
    result = await guest_commands.execute_guest_command(
        svc, user, message, bot_username="my_bot")
    assert result is not None
    assert "助理机器人" in result


# ── Guest 模式永久记忆彻底禁用 ──────────────────────────────────
async def test_guest_pipeline_called_with_memory_disabled(monkeypatch):
    """process_guest_message 必须以 enable_memory=False 调用 pipeline(Guest 无永久记忆)。"""
    captured: dict = {}

    async def fake_pipeline(svc, user, message, content, renderer, **kwargs):
        captured["enable_memory"] = kwargs.get("enable_memory")
        captured["scope"] = kwargs.get("scope")

    class FakeGuestRenderer:
        def __init__(self, *a, **kw):
            pass

    monkeypatch.setattr(guest, "run_chat_pipeline", fake_pipeline)
    monkeypatch.setattr(guest, "GuestRenderer", FakeGuestRenderer)

    svc = make_svc()
    user = User(tg_id=77, username="caller", first_name="C")
    message = make_guest_message("@my_bot 你好")

    await guest.process_guest_message(message, user, svc)
    assert captured["enable_memory"] is False
    assert captured["scope"] == "chat"


async def test_guest_remember_command_returns_hint_not_writing():
    """/remember 在 Guest 场景返回提示,绝不调用 logic_remember 写入。"""
    from app.handlers import guest_commands

    svc = make_svc()
    user = User(tg_id=77, first_name="C")
    result = await guest_commands.execute_guest_command(
        svc, user, make_guest_message("/remember 记住我喜欢猫"))
    assert result is not None
    assert "不保存永久记忆" in result
    assert "30 分钟" in result


async def test_guest_forget_command_returns_hint():
    """/forget 在 Guest 场景同样返回提示(无记忆可删)。"""
    from app.handlers import guest_commands

    svc = make_svc()
    user = User(tg_id=77, first_name="C")
    result = await guest_commands.execute_guest_command(
        svc, user, make_guest_message("/forget 1"))
    assert result is not None
    assert "不保存永久记忆" in result
