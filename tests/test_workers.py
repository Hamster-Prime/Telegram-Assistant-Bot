"""GenWorkerPool 测试 —— 后台轮询、回调幂等、失败通知、重启恢复。"""
from __future__ import annotations

import asyncio

import pytest

from app.config import Settings
from app.core.concurrency import ConcurrencyGuard, SendRateLimiter, TaskRegistry
from app.core.quota import QuotaManager
from app.core.workers import GenWorkerPool
from app.db.dao import DAOBundle
from app.db.engine import Database
from app.db.models import Generation


class FakeBot:
    def __init__(self):
        self.videos: list = []
        self.audios: list = []
        self.messages: list = []
        self.edits: list = []
        self.inline_media_calls: list = []  # EditMessageMedia(inline) 调用
        self.inline_text_edits: list = []

    async def __call__(self, method):
        # Guest inline 回填路径:bot(EditMessageMedia(inline_message_id=...))
        from aiogram.methods import EditMessageMedia
        if isinstance(method, EditMessageMedia):
            self.inline_media_calls.append(method)
            from types import SimpleNamespace
            return SimpleNamespace(ok=True)
        return None

    async def send_video(self, chat_id, video, caption=None, **kw):
        self.videos.append((chat_id, caption))

    async def send_audio(self, chat_id, audio, caption=None, **kw):
        self.audios.append((chat_id, caption))

    async def send_message(self, chat_id, text, **kw):
        self.messages.append((chat_id, text))

        class M: message_id = 555
        return M()

    async def edit_message_text(self, text, chat_id=None, message_id=None,
                                inline_message_id=None, **kw):
        if inline_message_id:
            self.inline_text_edits.append((inline_message_id, text))
        else:
            self.edits.append((chat_id, message_id, text))


class FakeVideoAPI:
    _model = "MiniMax-Hailuo-2.3"

    def __init__(self):
        self.created: list[str] = []
        self.status_seq: list[tuple[str, str | None]] = [("processing", None),
                                                         ("success", "file-1")]

    async def create_task(self, prompt, **kw):
        self.created.append(prompt)
        return f"task-{len(self.created)}"

    async def query_task(self, task_id):
        return self.status_seq.pop(0) if self.status_seq else ("processing", None)


class FakeMusicAPI:
    _model = "music-2.6"

    def __init__(self):
        self.url_mode = False  # True 时返回 (None, url)

    async def generate(self, prompt, *, lyrics=None, is_instrumental=False):
        if self.url_mode:
            return None, "https://cdn.test/music.mp3"
        return b"FAKE_MP3", None


class FakeFilesAPI:
    async def retrieve_url(self, file_id):
        return f"https://cdn.test/{file_id}"

    async def download(self, url):
        return b"FAKE_VIDEO_BYTES"


@pytest.fixture
async def env():
    db = Database(":memory:", wal=False)
    await db.connect()
    daos = DAOBundle(db)
    settings = Settings(_env_file=None, minimax_api_keys="k1")
    bot = FakeBot()
    registry = TaskRegistry()
    pool = GenWorkerPool(
        bot, daos, FakeVideoAPI(), FakeMusicAPI(), FakeFilesAPI(),
        QuotaManager(daos, settings),
        ConcurrencyGuard(4, 4, 2),
        SendRateLimiter(10_000),
        registry,
        poll_interval_s=0.01,
    )
    await daos.users.upsert_basic(1, "u", "U")
    user = await daos.users.get(1)
    yield pool, daos, bot, user, registry
    await registry.shutdown()
    await db.close()


async def test_video_submit_and_poll_success(env):
    pool, daos, bot, user, registry = env
    gen_id, task_id = await pool.submit_video(user, 100, "雪山日落",
                                              placeholder_msg_id=55)
    assert task_id == "task-1"
    # 等后台轮询完成(0.01s 起步)
    for _ in range(200):
        gen = await daos.generations.get(gen_id)
        if gen.status == "success":
            break
        await asyncio.sleep(0.02)
    assert gen.status == "success"
    assert bot.videos and bot.videos[0][0] == 100
    assert any("✅" in e[2] for e in bot.edits)  # 占位被改为完成


async def test_finalize_idempotent(env):
    """重复回填(回调+轮询同时到)只发一次视频。"""
    pool, daos, bot, user, _ = env
    gen = Generation(id=None, user_id=1, chat_id=100, kind="video",
                     model="m", prompt="p", status="processing", task_id="t-x")
    gen_id = await daos.generations.create(gen)
    await asyncio.gather(
        pool.finalize_video(gen_id, "file-9", source="回调"),
        pool.finalize_video(gen_id, "file-9", source="轮询"),
    )
    # 再补一次也不重复
    await pool.finalize_video(gen_id, "file-9", source="轮询")
    assert len(bot.videos) == 1


async def test_music_background(env):
    pool, daos, bot, user, _ = env
    gen_id = await pool.submit_music(user, 200, "欢快的钢琴曲",
                                     is_instrumental=True, placeholder_msg_id=66)
    for _ in range(100):
        gen = await daos.generations.get(gen_id)
        if gen.status == "success":
            break
        await asyncio.sleep(0.02)
    assert gen.status == "success"
    assert bot.audios and bot.audios[0][0] == 200


async def test_video_failed_marks_and_notifies(env):
    pool, daos, bot, user, _ = env
    pool._video.status_seq = [("failed", None)]
    gen_id, _ = await pool.submit_video(user, 100, "x", placeholder_msg_id=77)
    for _ in range(100):
        gen = await daos.generations.get(gen_id)
        if gen.status == "failed":
            break
        await asyncio.sleep(0.02)
    assert gen.status == "failed"
    assert any("失败" in e[2] for e in bot.edits)


async def test_recover_pending_restarts_polling(env):
    """重启恢复:processing 的视频任务重新挂轮询直至完成。"""
    pool, daos, bot, user, _ = env
    gen = Generation(id=None, user_id=1, chat_id=100, kind="video",
                     model="m", prompt="恢复测试", status="processing",
                     task_id="task-recover")
    gen_id = await daos.generations.create(gen)
    pool._video.status_seq = [("success", "file-r")]

    n = await pool.recover_pending()
    assert n == 1
    for _ in range(100):
        g = await daos.generations.get(gen_id)
        if g.status == "success":
            break
        await asyncio.sleep(0.02)
    assert g.status == "success"
    assert len(bot.videos) == 1


async def test_recover_music_fails_gracefully(env):
    """音乐任务无 task_id,重启后标记失败并通知。"""
    pool, daos, bot, user, _ = env
    gen = Generation(id=None, user_id=1, chat_id=300, kind="music",
                     model="m", prompt="x", status="processing")
    await daos.generations.create(gen)
    await pool.recover_pending()
    pend = await daos.generations.pending()
    assert pend == []
    assert any("中断" in m[1] for m in bot.messages)


# ── Guest inline 回填(修复项)──────────────────────────────────
async def test_video_guest_inline_backfill(env):
    """Guest 视频:有 inline_message_id → editMessageMedia 回填,不发 sendVideo。"""
    pool, daos, bot, user, registry = env
    gen_id, task_id = await pool.submit_video(
        user, 100, "海浪", placeholder_msg_id=None,
        inline_message_id="guest-inline-1")
    # 等后台轮询完成
    for _ in range(200):
        gen = await daos.generations.get(gen_id)
        if gen.status == "success":
            break
        await asyncio.sleep(0.02)
    assert gen.status == "success"
    assert gen.inline_message_id == "guest-inline-1"
    assert bot.videos == []  # 不走直发
    assert len(bot.inline_media_calls) == 1
    from aiogram.types import InputMediaVideo
    assert isinstance(bot.inline_media_calls[0].media, InputMediaVideo)
    assert bot.inline_media_calls[0].inline_message_id == "guest-inline-1"


async def test_music_guest_inline_backfill(env):
    """Guest 音乐:有 inline_message_id + URL → editMessageMedia 回填。"""
    pool, daos, bot, user, _ = env
    pool._music.url_mode = True  # 返回 URL(Guest 必须 URL)
    gen_id = await pool.submit_music(
        user, 200, "钢琴曲", is_instrumental=True,
        inline_message_id="guest-inline-2")
    for _ in range(100):
        gen = await daos.generations.get(gen_id)
        if gen.status == "success":
            break
        await asyncio.sleep(0.02)
    assert gen.status == "success"
    assert bot.audios == []  # 不走直发
    assert len(bot.inline_media_calls) == 1
    from aiogram.types import InputMediaAudio
    assert isinstance(bot.inline_media_calls[0].media, InputMediaAudio)
