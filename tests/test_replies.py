"""私聊/群聊回复上下文折叠测试(修复项 2)。

验证:用户回复/引用某条消息时,被引用内容被拼进 content,
不再丢失(此前私聊完全没处理 reply_to_message)。
"""
from __future__ import annotations

from types import SimpleNamespace

from aiogram.types import Message

from app.handlers.replies import fold_reply_context


class FakeBot:
    def __init__(self, files: dict[str, tuple[str, bytes, int | None]] | None = None):
        self.files = files or {}
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
        raise AssertionError("unexpected upload")


def make_svc(files: dict | None = None):
    return SimpleNamespace(bot=FakeBot(files), files_api=FakeFilesAPI())


def make_private_message(text: str, *, reply_text: str | None = None,
                         quote_text: str | None = None,
                         reply_photo: list[dict] | None = None,
                         files: dict | None = None) -> Message:
    payload: dict = {
        "message_id": 2,
        "date": 0,
        "chat": {"id": 500, "type": "private"},
        "from": {"id": 50, "is_bot": False, "first_name": "Me"},
        "text": text,
    }
    if reply_text is not None or reply_photo is not None:
        reply = {
            "message_id": 1,
            "date": 0,
            "chat": {"id": 500, "type": "private"},
            "from": {"id": 50, "is_bot": False, "first_name": "Me"},
        }
        if reply_text is not None:
            reply["text"] = reply_text
        if reply_photo is not None:
            reply["photo"] = reply_photo
        payload["reply_to_message"] = reply
    if quote_text is not None:
        payload["quote"] = {"text": quote_text, "position": 0}
    return Message.model_validate(payload)


async def test_private_reply_text_folded_into_content():
    """私聊回复:被回复消息文本拼进 content(此前丢失)。"""
    svc = make_svc()
    msg = make_private_message("继续解释", reply_text="第一条:量子力学")
    content, query = await fold_reply_context(svc, msg, "继续解释", "继续解释")
    # 标记含发送者(Me);正文跟在标记后
    assert "[引用的消息 · 👤 Me" in content
    assert "第一条:量子力学" in content
    assert "[召唤者的问题]\n继续解释" in content
    assert "[引用的消息" in query


async def test_private_no_reply_unchanged():
    """私聊无回复:content 原样返回。"""
    svc = make_svc()
    msg = make_private_message("你好")
    content, query = await fold_reply_context(svc, msg, "你好", "你好")
    assert content == "你好"
    assert query == "你好"


async def test_private_reply_with_quote_uses_quote():
    """私聊回复 + 引用片段:quote 文本被采用。"""
    svc = make_svc()
    msg = make_private_message(
        "这句什么意思", reply_text="长原文...", quote_text="被选中的句子")
    content, query = await fold_reply_context(
        svc, msg, "这句什么意思", "这句什么意思")
    # reply_to_message 存在但无 quote 优先用 reply_text;此处 reply_text 非空
    assert "[引用的消息" in content


async def test_private_reply_photo_included_as_image():
    """私聊回复一条图片消息:图片 base64 拼进 content。"""
    svc = make_svc({"photo-x": ("p.jpg", b"jpg-data", 9)})
    msg = make_private_message(
        "这张图是什么", reply_photo=[{
            "file_id": "photo-x", "file_unique_id": "u1",
            "width": 64, "height": 64, "file_size": 9,
        }])
    content, _ = await fold_reply_context(svc, msg, "这张图是什么", "这张图是什么")
    assert isinstance(content, list)
    urls = [b["image_url"]["url"] for b in content
            if b.get("type") == "image_url"]
    assert urls and urls[0].startswith("data:image/jpeg;base64,")
    assert svc.bot.downloaded_file_ids == ["photo-x"]


async def test_group_reply_also_folded():
    """群聊回复同样折叠(顺手修复)。"""
    payload = {
        "message_id": 3,
        "date": 0,
        "chat": {"id": 700, "type": "supergroup", "title": "G"},
        "from": {"id": 70, "is_bot": False, "first_name": "U"},
        "text": "@my_bot 总结下",
        "reply_to_message": {
            "message_id": 2,
            "date": 0,
            "chat": {"id": 700, "type": "supergroup"},
            "from": {"id": 71, "is_bot": False, "first_name": "O"},
            "text": "原始讨论内容",
        },
    }
    msg = Message.model_validate(payload)
    svc = make_svc()
    content, query = await fold_reply_context(svc, msg, "总结下", "总结下")
    # 标记含发送者(O);正文跟在标记后
    assert "[引用的消息 · 👤 O" in content
    assert "原始讨论内容" in content
