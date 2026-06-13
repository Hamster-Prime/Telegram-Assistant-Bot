"""GenWorkerPool ★ —— 视频/音乐生成后台轮询/回调回填 + 重启恢复(plan §4 L2/L5)。

铁律:handler 绝不 await 生成任务直到完成。
- 视频:建任务(带 callback_url)→ 落库 → 发占位 → 立即返回;
  完成由 ① /mmx/callback 回调 ② 兜底轮询(指数退避 ≤10 分钟)送达。
- 音乐:MiniMax 为同步接口但耗时较长 → 同样放后台任务执行。
- 幂等:按 task_id 去重,回填前查 generations.status。
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from aiogram import Bot
from aiogram.types import BufferedInputFile, URLInputFile

from app.core.concurrency import ConcurrencyGuard, SendRateLimiter, TaskRegistry
from app.db.dao import DAOBundle
from app.db.models import Generation, User
from app.logging import get_logger

if TYPE_CHECKING:
    from app.core.quota import QuotaManager
    from app.minimax.files import FilesAPI
    from app.minimax.music import MusicAPI
    from app.minimax.video import VideoAPI

log = get_logger("core.workers")

POLL_MAX_TOTAL_S = 600  # 轮询硬上限 10 分钟


class GenWorkerPool:
    def __init__(
        self,
        bot: Bot,
        daos: DAOBundle,
        video_api: "VideoAPI",
        music_api: "MusicAPI",
        files_api: "FilesAPI",
        quota: "QuotaManager",
        guard: ConcurrencyGuard,
        limiter: SendRateLimiter,
        registry: TaskRegistry,
        *,
        poll_interval_s: float = 5.0,
        callback_url: str = "",
    ) -> None:
        self._bot = bot
        self._daos = daos
        self._video = video_api
        self._music = music_api
        self._files = files_api
        self._quota = quota
        self._guard = guard
        self._limiter = limiter
        self._registry = registry
        self._poll_interval = poll_interval_s
        self._callback_url = callback_url
        # 幂等回填:同一 gen_id 仅回填一次
        self._finalizing: set[int] = set()

    # ── 视频 ───────────────────────────────────────────────────
    async def submit_video(self, user: User, chat_id: int, prompt: str,
                           *, duration: int = 6, resolution: str = "768P",
                           placeholder_msg_id: int | None = None) -> tuple[int, str]:
        """建视频任务并启动后台轮询。返回 (gen_id, task_id)。handler 快速返回。"""
        task_id = await self._video.create_task(
            prompt, duration=duration, resolution=resolution,
            callback_url=self._callback_url or None,
        )
        gen = Generation(
            id=None, user_id=user.tg_id, chat_id=chat_id, kind="video",
            model=self._video._model, prompt=prompt, status="processing",
            task_id=task_id, placeholder_msg_id=placeholder_msg_id,
        )
        gen_id = await self._daos.generations.create(gen)
        self._registry.spawn(self._poll_video(gen_id, task_id, user),
                             name=f"poll-video-{gen_id}")
        log.info("视频任务已提交后台", 生成编号=gen_id, 任务ID=task_id,
                 用户=user.tg_id, 会话=chat_id)
        return gen_id, task_id

    async def _poll_video(self, gen_id: int, task_id: str, user: User) -> None:
        """兜底轮询(指数退避)。回调先到则发现 status 已终态,直接退出。"""
        interval = self._poll_interval
        waited = 0.0
        while waited < POLL_MAX_TOTAL_S:
            await asyncio.sleep(interval)
            waited += interval
            interval = min(interval * 1.5, 30.0)  # 指数退避,封顶 30s

            gen = await self._daos.generations.get(gen_id)
            if gen is None or gen.status in ("success", "failed"):
                log.debug("轮询退出(任务已终态或不存在)", 生成编号=gen_id,
                          状态=gen.status if gen else "不存在")
                return

            try:
                status, file_id = await self._video.query_task(task_id)
            except Exception as e:
                log.warning("视频任务查询失败(继续轮询)", 生成编号=gen_id,
                            任务ID=task_id, 异常类型=type(e).__name__,
                            详情=str(e)[:200], 已等待秒=round(waited))
                continue

            if status == "success" and file_id:
                await self.finalize_video(gen_id, file_id, source="轮询")
                return
            if status in ("failed", "fail", "error"):
                await self._fail_generation(gen_id, "MiniMax 视频生成失败", user)
                return
            log.debug("视频生成中", 生成编号=gen_id, 状态=status,
                      已等待秒=round(waited))

        await self._fail_generation(gen_id, f"视频生成超时(>{POLL_MAX_TOTAL_S}秒)", user)

    async def finalize_video(self, gen_id: int, file_id: str, *, source: str) -> None:
        """成功回填:取文件 URL → 下载 → sendVideo → 结算配额。幂等。"""
        if gen_id in self._finalizing:
            log.info("回填去重(已在处理)", 生成编号=gen_id, 来源=source)
            return
        self._finalizing.add(gen_id)
        try:
            gen = await self._daos.generations.get(gen_id)
            if gen is None or gen.status == "success":
                log.info("回填跳过(已完成或不存在)", 生成编号=gen_id, 来源=source)
                return
            log.info("视频回填开始", 生成编号=gen_id, 文件ID=file_id, 来源=source)
            url = await self._files.retrieve_url(file_id)
            data = await self._files.download(url)
            await self._limiter.acquire()
            await self._bot.send_video(
                gen.chat_id,
                BufferedInputFile(data, filename=f"video_{gen_id}.mp4"),
                caption=f"🎬 视频已生成:{gen.prompt[:100]}",
            )
            await self._daos.generations.update_status(
                gen_id, "success", file_id=file_id, result_url=url, finished=True
            )
            await self._edit_placeholder(gen, "✅ 视频已生成完毕,见下方")
            user = await self._daos.users.get(gen.user_id)
            if user:
                await self._quota.settle(user, "calls", self._quota.call_weight("video"),
                                         chat_id=gen.chat_id, kind="video")
            log.info("视频回填完成", 生成编号=gen_id, 来源=source,
                     大小KB=round(len(data) / 1024, 1))
        except Exception as e:
            log.error("视频回填失败", 生成编号=gen_id, 异常类型=type(e).__name__,
                      详情=str(e)[:300])
            gen = await self._daos.generations.get(gen_id)
            if gen and gen.status != "success":
                user = await self._daos.users.get(gen.user_id)
                await self._fail_generation(gen_id, f"视频文件回传失败:{e}", user)
        finally:
            self._finalizing.discard(gen_id)

    async def handle_video_failed_callback(self, task_id: str, reason: str) -> None:
        gen = await self._daos.generations.get_by_task(task_id)
        if gen is None or gen.status in ("success", "failed"):
            return
        user = await self._daos.users.get(gen.user_id)
        await self._fail_generation(gen.id, f"MiniMax 回调通知失败:{reason}", user)

    # ── 音乐 ───────────────────────────────────────────────────
    async def submit_music(self, user: User, chat_id: int, prompt: str,
                           *, lyrics: str | None = None, is_instrumental: bool = False,
                           placeholder_msg_id: int | None = None) -> int:
        """音乐生成放后台任务(接口同步但耗时长)。返回 gen_id,handler 立即返回。"""
        gen = Generation(
            id=None, user_id=user.tg_id, chat_id=chat_id, kind="music",
            model=self._music._model, prompt=prompt, status="processing",
            placeholder_msg_id=placeholder_msg_id,
        )
        gen_id = await self._daos.generations.create(gen)
        self._registry.spawn(
            self._run_music(gen_id, user, prompt, lyrics, is_instrumental),
            name=f"music-{gen_id}",
        )
        log.info("音乐任务已提交后台", 生成编号=gen_id, 用户=user.tg_id, 会话=chat_id)
        return gen_id

    async def _run_music(self, gen_id: int, user: User, prompt: str,
                         lyrics: str | None, is_instrumental: bool) -> None:
        try:
            async with self._guard.generation_slot(user.tg_id):
                audio_bytes, audio_url = await self._music.generate(
                    prompt, lyrics=lyrics, is_instrumental=is_instrumental,
                )
            gen = await self._daos.generations.get(gen_id)
            if gen is None:
                return
            await self._limiter.acquire()
            if audio_bytes:
                await self._bot.send_audio(
                    gen.chat_id,
                    BufferedInputFile(audio_bytes, filename=f"music_{gen_id}.mp3"),
                    caption=f"🎵 音乐已生成:{prompt[:100]}",
                )
            elif audio_url:
                data = await self._files.download(audio_url)
                await self._bot.send_audio(
                    gen.chat_id,
                    BufferedInputFile(data, filename=f"music_{gen_id}.mp3"),
                    caption=f"🎵 音乐已生成:{prompt[:100]}",
                )
            else:
                raise RuntimeError("MiniMax 未返回音频数据")
            await self._daos.generations.update_status(gen_id, "success",
                                                       result_url=audio_url, finished=True)
            await self._edit_placeholder(gen, "✅ 音乐已生成完毕,见下方")
            await self._quota.settle(user, "calls", self._quota.call_weight("music"),
                                     chat_id=gen.chat_id, kind="music")
            log.info("音乐任务完成", 生成编号=gen_id)
        except Exception as e:
            log.error("音乐任务失败", 生成编号=gen_id, 异常类型=type(e).__name__,
                      详情=str(e)[:300])
            await self._fail_generation(gen_id, str(e), user)

    # ── 公共 ───────────────────────────────────────────────────
    async def _edit_placeholder(self, gen: Generation, text: str) -> None:
        if not gen.placeholder_msg_id:
            return
        try:
            await self._limiter.acquire()
            await self._bot.edit_message_text(
                text, chat_id=gen.chat_id, message_id=gen.placeholder_msg_id,
            )
        except Exception as e:
            log.debug("占位消息编辑失败(忽略)", 生成编号=gen.id, 错误=str(e)[:120])

    async def _fail_generation(self, gen_id: int, reason: str, user: User | None) -> None:
        gen = await self._daos.generations.get(gen_id)
        if gen is None or gen.status in ("success", "failed"):
            return
        await self._daos.generations.update_status(gen_id, "failed", error=reason[:500],
                                                   finished=True)
        kind_zh = {"video": "视频", "music": "音乐", "image": "图片", "tts": "语音"}.get(
            gen.kind, gen.kind)
        text = f"❌ {kind_zh}生成失败:{reason[:200]}"
        await self._edit_placeholder(gen, text)
        if not gen.placeholder_msg_id:
            try:
                await self._limiter.acquire()
                await self._bot.send_message(gen.chat_id, text)
            except Exception as e:
                log.error("失败通知发送失败", 生成编号=gen_id, 错误=str(e)[:120])
        log.warning("生成任务标记失败", 生成编号=gen_id, 类型=gen.kind, 原因=reason[:200])

    # ── 重启恢复(L5) ─────────────────────────────────────────
    async def recover_pending(self) -> int:
        """启动时扫描未决任务,重新挂载轮询。返回恢复数量。"""
        pending = await self._daos.generations.pending()
        recovered = 0
        for gen in pending:
            if gen.kind == "video" and gen.task_id:
                user = await self._daos.users.get(gen.user_id)
                if user is None:
                    continue
                self._registry.spawn(self._poll_video(gen.id, gen.task_id, user),
                                     name=f"recover-video-{gen.id}")
                recovered += 1
            else:
                # 音乐等无 task_id 的任务无法续传 → 标记失败并通知
                await self._fail_generation(gen.id, "服务重启,任务中断,请重新发起",
                                            None)
        log.info("重启恢复完成", 未决任务数=len(pending), 已恢复轮询=recovered)
        return recovered
