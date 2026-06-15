"""GenWorkerPool 测试 —— 后台轮询、回调幂等、失败通知、重启恢复。"""
from __future__ import annotations

import asyncio
import sqlite3

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
        self.rich_messages: list = []
        self.edits: list = []
        self.rich_edits: list = []
        self.inline_media_calls: list = []  # EditMessageMedia(inline) 调用
        self.inline_text_edits: list = []
        self.inline_rich_edits: list = []
        self.rich_error: Exception | None = None

    async def __call__(self, method):
        # Guest inline 回填路径:bot(EditMessageMedia(inline_message_id=...))
        from aiogram.methods import EditMessageMedia, SendRichMessage
        if isinstance(method, EditMessageMedia):
            self.inline_media_calls.append(method)
            from types import SimpleNamespace
            return SimpleNamespace(ok=True)
        if isinstance(method, SendRichMessage):
            if self.rich_error is not None:
                raise self.rich_error
            self.rich_messages.append((method.chat_id, method.rich_message.markdown))
            from types import SimpleNamespace
            return SimpleNamespace(message_id=777)
        return None

    async def send_video(self, chat_id, video, caption=None, **kw):
        self.videos.append((chat_id, caption))

    async def send_audio(self, chat_id, audio, caption=None, **kw):
        self.audios.append((chat_id, caption))

    async def send_message(self, chat_id, text, **kw):
        self.messages.append((chat_id, text))

        class M:
            message_id = 555
        return M()

    async def edit_message_text(self, text=None, chat_id=None, message_id=None,
                                inline_message_id=None, rich_message=None, **kw):
        recorded = text if text is not None else rich_message.markdown
        if rich_message is not None and self.rich_error is not None:
            raise self.rich_error
        if inline_message_id:
            if rich_message is not None:
                self.inline_rich_edits.append((inline_message_id, recorded))
            else:
                self.inline_text_edits.append((inline_message_id, recorded))
        else:
            if rich_message is not None:
                self.rich_edits.append((chat_id, message_id, recorded))
            else:
                self.edits.append((chat_id, message_id, recorded))


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


async def _wait_generation(daos: DAOBundle, gen_id: int, *, attempts: int = 200):
    last = None
    for _ in range(attempts):
        try:
            last = await daos.generations.get(gen_id)
        except sqlite3.OperationalError as e:
            if "locked" not in str(e):
                raise
            await asyncio.sleep(0.02)
            continue
        if last and last.status in {"success", "failed"}:
            return last
        await asyncio.sleep(0.02)
    return last


async def test_video_submit_and_poll_success(env):
    pool, daos, bot, user, registry = env
    gen_id, task_id = await pool.submit_video(user, 100, "雪山日落",
                                              placeholder_msg_id=55)
    assert task_id == "task-1"
    # 等后台轮询完成(0.01s 起步)
    gen = await _wait_generation(daos, gen_id)
    assert gen.status == "success"
    assert bot.videos == []
    assert bot.rich_edits and bot.rich_edits[0][0:2] == (100, 55)
    assert "https://cdn.test/file-1" in bot.rich_edits[0][2]


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
    assert len(bot.rich_messages) == 1
    assert bot.videos == []


async def test_music_background(env):
    pool, daos, bot, user, _ = env
    gen_id = await pool.submit_music(user, 200, "欢快的钢琴曲",
                                     is_instrumental=True, placeholder_msg_id=66)
    gen = await _wait_generation(daos, gen_id, attempts=100)
    assert gen.status == "success"
    assert bot.audios and bot.audios[0][0] == 200


async def test_music_url_edits_placeholder_as_rich_message(env):
    pool, daos, bot, user, _ = env
    pool._music.url_mode = True
    gen_id = await pool.submit_music(user, 200, "欢快的钢琴曲",
                                     is_instrumental=True, placeholder_msg_id=66)
    gen = await _wait_generation(daos, gen_id, attempts=100)
    assert gen.status == "success"
    assert bot.audios == []
    assert bot.rich_edits and bot.rich_edits[0][0:2] == (200, 66)
    assert "https://cdn.test/music.mp3" in bot.rich_edits[0][2]


async def test_video_failed_marks_and_notifies(env):
    pool, daos, bot, user, _ = env
    pool._video.status_seq = [("failed", None)]
    gen_id, _ = await pool.submit_video(user, 100, "x", placeholder_msg_id=77)
    gen = await _wait_generation(daos, gen_id, attempts=100)
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
    g = await _wait_generation(daos, gen_id, attempts=100)
    assert g.status == "success"
    assert bot.videos == []
    assert len(bot.rich_messages) == 1


async def test_video_rich_message_failure_falls_back_to_media(env):
    pool, daos, bot, user, _ = env
    bot.rich_error = RuntimeError("rich rejected")
    gen = Generation(id=None, user_id=1, chat_id=100, kind="video",
                     model="m", prompt="p", status="processing", task_id="t-x")
    gen_id = await daos.generations.create(gen)

    await pool.finalize_video(gen_id, "file-9", source="回调")

    assert bot.rich_messages == []
    assert len(bot.videos) == 1


async def test_video_rich_message_not_modified_is_treated_as_delivered(env):
    pool, daos, bot, user, _ = env
    bot.rich_error = RuntimeError(
        "Telegram server says - Bad Request: message is not modified: "
        "specified new message content and reply markup are exactly the same "
        "as a current content and reply"
    )
    gen = Generation(id=None, user_id=1, chat_id=100, kind="video",
                     model="m", prompt="p", status="processing", task_id="t-x",
                     placeholder_msg_id=55)
    gen_id = await daos.generations.create(gen)

    await pool.finalize_video(gen_id, "file-9", source="回调")

    updated = await daos.generations.get(gen_id)
    assert updated.status == "success"
    assert updated.result_url == "https://cdn.test/file-9"
    assert bot.videos == []
    assert bot.edits == []


async def test_video_completion_records_assistant_status_for_future_context(env):
    pool, daos, bot, user, _ = env
    gen = Generation(id=None, user_id=1, chat_id=100, kind="video",
                     model="m", prompt="p", status="processing", task_id="t-x",
                     placeholder_msg_id=55)
    gen_id = await daos.generations.create(gen)

    await pool.finalize_video(gen_id, "file-9", source="回调")

    recent = await daos.messages.recent_uncompacted(100)
    assert any(
        m.role == "assistant"
        and "视频生成完成" in m.content
        and "https://cdn.test/file-9" in m.content
        and "不是用户上传素材" in m.content
        for m in recent
    )


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
    gen = await _wait_generation(daos, gen_id)
    assert gen.status == "success"
    assert gen.inline_message_id == "guest-inline-1"
    assert bot.videos == []  # 不走直发
    assert bot.inline_media_calls == []
    assert len(bot.inline_rich_edits) == 1
    assert bot.inline_rich_edits[0][0] == "guest-inline-1"
    assert "https://cdn.test/file-1" in bot.inline_rich_edits[0][1]


async def test_music_guest_inline_backfill(env):
    """Guest 音乐:有 inline_message_id + URL → editMessageMedia 回填。"""
    pool, daos, bot, user, _ = env
    pool._music.url_mode = True  # 返回 URL(Guest 必须 URL)
    gen_id = await pool.submit_music(
        user, 200, "钢琴曲", is_instrumental=True,
        inline_message_id="guest-inline-2")
    gen = await _wait_generation(daos, gen_id, attempts=100)
    assert gen.status == "success"
    assert bot.audios == []  # 不走直发
    assert bot.inline_media_calls == []
    assert len(bot.inline_rich_edits) == 1
    assert bot.inline_rich_edits[0][0] == "guest-inline-2"
    assert "https://cdn.test/music.mp3" in bot.inline_rich_edits[0][1]
