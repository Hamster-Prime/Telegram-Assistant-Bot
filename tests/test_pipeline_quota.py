"""pipeline 工具配额测试 —— web_fetch 应与 web_search 一样计配额。"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.core.richmsg import RichAttachmentCollector
from app.core.quota import QuotaManager
from app.db.dao import DAOBundle
from app.db.engine import Database
from app.handlers.media import ReferenceAssets
from app.handlers.pipeline import _reference_system_note, build_dispatcher


@pytest.fixture
async def env():
    db = Database(":memory:", wal=False)
    await db.connect()
    daos = DAOBundle(db)
    settings = Settings(
        _env_file=None,
        minimax_api_keys="k1",
        default_quota_mode="calls",
        default_quota_limit=1000,
        default_quota_period="day",
        gen_call_weights="image:1,video:5,music:5,tts:1,search:1,fetch:1",
    )
    quota = QuotaManager(daos, settings)

    await daos.users.upsert_basic(1, "bob", "Bob")
    await daos.users.set_authorized(1, True, by=999)
    user = await daos.users.get(1)

    svc = SimpleNamespace(
        bot=None, limiter=None, files_api=None, daos=daos, quota=quota,
        settings=settings,
        search=SimpleNamespace(
            fetch=AsyncMock(return_value="网页正文 markdown"),
            search=AsyncMock(return_value=[
                {"title": "t", "url": "u", "snippet": "s", "source": "brave"}]),
        ),
        image_api=SimpleNamespace(
            generate=AsyncMock(return_value=[
                "https://img.test/1.jpg",
                "https://img.test/2.jpg",
            ]),
        ),
        tts_api=SimpleNamespace(
            synthesize=AsyncMock(return_value=(None, "https://audio.test/speech.mp3", 1234)),
        ),
    )
    yield svc, daos, quota, user
    await db.close()


async def test_web_fetch_settles_quota(env):
    svc, daos, quota, user = env
    await quota.ensure_default(1)
    d = build_dispatcher(svc, user, chat_id=10, scope="user", scope_owner=1)

    result = await d.dispatch("web_fetch", json.dumps({"url": "https://x.com"}))
    assert "网页正文" in result

    q = await daos.quotas.get(1, "calls")
    assert q.used == 1  # fetch 已计 1 次


async def test_web_fetch_denied_when_over_quota(env):
    svc, daos, quota, user = env
    # 配额上限 1,先用满
    await daos.quotas.set(1, "calls", 1, "day")
    await quota.settle(user, "calls", 1, chat_id=10, kind="search")

    d = build_dispatcher(svc, user, chat_id=10, scope="user", scope_owner=1)
    result = await d.dispatch("web_fetch", json.dumps({"url": "https://x.com"}))

    assert "配额不足" in result
    svc.search.fetch.assert_not_awaited()  # 配额拦截,不真正抓取


async def test_web_search_still_settles(env):
    """回归:web_search 仍正常计配额。"""
    svc, daos, quota, user = env
    await quota.ensure_default(1)
    d = build_dispatcher(svc, user, chat_id=10, scope="user", scope_owner=1)

    await d.dispatch("web_search", json.dumps({"query": "hello"}))
    q = await daos.quotas.get(1, "calls")
    assert q.used == 1


class CollectorDelivery:
    is_guest = False
    inline_message_id = None

    def __init__(self):
        self.collector = RichAttachmentCollector()
        self.sent_photos: list[str] = []
        self.sent_voices: list[tuple[str | None, bytes | None]] = []

    async def attach_rich_media(
        self,
        kind: str,
        url: str,
        *,
        label: str | None = None,
        note: str | None = None,
    ):
        return self.collector.add(kind, url, label=label, note=note)

    async def send_photo(self, url: str, caption: str | None = None) -> bool:
        self.sent_photos.append(url)
        return True

    async def send_voice(
        self,
        url: str | None,
        data: bytes | None = None,
        *,
        filename: str | None = None,
    ) -> bool:
        self.sent_voices.append((url, data))
        return True

    async def send_placeholder(self, text: str) -> int | None:
        return None

    async def edit_placeholder(self, msg_id: int | None, text: str) -> None:
        return None

    async def send_text(self, text: str) -> bool:
        return True


async def test_generate_image_attaches_rich_media_instead_of_sending_photo(env):
    svc, daos, quota, user = env
    await quota.ensure_default(1)
    delivery = CollectorDelivery()
    d = build_dispatcher(
        svc, user, chat_id=10, scope="user", scope_owner=1,
        delivery=delivery,
    )

    result = await d.dispatch(
        "generate_image",
        json.dumps({"prompt": "画一只猫", "n": 2}, ensure_ascii=False),
    )

    assert delivery.sent_photos == []
    pending = delivery.collector.pending()
    assert [att.url for att in pending] == [
        "https://img.test/1.jpg",
        "https://img.test/2.jpg",
    ]
    assert "请在最终回复中" in result
    assert "合适的位置" in result
    assert "![生成图片 1](https://img.test/1.jpg)" in result
    assert "![生成图片 2](https://img.test/2.jpg)" in result
    q = await daos.quotas.get(1, "calls")
    assert q.used == 1


async def test_synthesize_speech_requests_mp3_sends_voice_and_attaches_rich_audio(env):
    svc, daos, quota, user = env
    await quota.ensure_default(1)
    delivery = CollectorDelivery()
    d = build_dispatcher(
        svc, user, chat_id=10, scope="user", scope_owner=1,
        delivery=delivery,
    )

    result = await d.dispatch(
        "synthesize_speech",
        json.dumps({"text": "你好呀"}, ensure_ascii=False),
    )

    assert delivery.sent_voices == [("https://audio.test/speech.mp3", None)]
    _, kwargs = svc.tts_api.synthesize.call_args
    assert kwargs["audio_format"] == "mp3"
    pending = delivery.collector.pending()
    assert len(pending) == 1
    assert pending[0].kind == "audio"
    assert pending[0].url == "https://audio.test/speech.mp3"
    assert pending[0].markdown == "![生成语音](https://audio.test/speech.mp3)"
    assert "Rich Markdown" in result
    assert "![生成语音](https://audio.test/speech.mp3)" in result


def test_reference_system_note_marks_missing_current_audio():
    note = _reference_system_note(ReferenceAssets())

    assert "本轮没有音频/语音参考" in note
    assert "历史消息" in note
    assert "助理生成语音" in note
    assert "文生视频" in note


def test_reference_system_note_reports_current_media_refs():
    note = _reference_system_note(
        ReferenceAssets(images=["data:image/png;base64,x"], audio=b"ogg")
    )

    assert "本轮包含 1 张图片参考" in note
    assert "本轮包含音频/语音参考" in note
