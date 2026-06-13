"""服务容器 —— 接线所有组件,handler 通过它取依赖。"""
from __future__ import annotations

import httpx
from aiogram import Bot

from app.config import Settings
from app.core.agent import Agent
from app.core.compaction import Compactor
from app.core.concurrency import ConcurrencyGuard, SendRateLimiter, TaskRegistry, UserLock
from app.core.context import ContextBuilder
from app.core.memory import MemoryService
from app.core.quota import QuotaManager
from app.core.workers import GenWorkerPool
from app.db.dao import DAOBundle
from app.db.engine import Database
from app.logging import get_logger
from app.minimax.chat import ChatAPI
from app.minimax.client import MiniMaxClient
from app.minimax.files import FilesAPI
from app.minimax.image import ImageAPI
from app.minimax.music import MusicAPI
from app.minimax.tts import TTSAPI
from app.minimax.video import VideoAPI
from app.search.brave import BraveProvider
from app.search.duckduckgo import DuckDuckGoProvider
from app.search.firecrawl import FirecrawlProvider
from app.search.router import SearchRouter

log = get_logger("services")


class Services:
    """应用级单例集合。main.py 启动时构建一次。"""

    def __init__(self, settings: Settings, bot: Bot) -> None:
        self.settings = settings
        self.bot = bot

        # 数据库
        self.db = Database(settings.db_path, wal=settings.sqlite_wal)
        self.daos = DAOBundle(self.db)

        # 并发原语
        self.guard = ConcurrencyGuard(
            settings.max_concurrent_chats,
            settings.max_concurrent_generations,
            settings.per_user_concurrency,
        )
        self.user_lock = UserLock()
        self.limiter = SendRateLimiter(settings.tg_global_send_rate)
        self.registry = TaskRegistry()

        # MiniMax(多 Key fallback)
        self.mmx = MiniMaxClient(
            settings.minimax_keys,
            settings.minimax_base_url,
            max_connections=settings.httpx_max_connections,
        )
        self.chat_api = ChatAPI(self.mmx, settings.model_chat)
        self.tts_api = TTSAPI(self.mmx, settings.model_tts)
        self.image_api = ImageAPI(self.mmx, settings.model_image)
        self.video_api = VideoAPI(self.mmx, settings.model_video)
        self.music_api = MusicAPI(self.mmx, settings.model_music)
        self.files_api = FilesAPI(self.mmx)

        # 搜索(独立 httpx 单例,与 MiniMax 隔离)
        self.search_http = httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=8.0),
            limits=httpx.Limits(max_connections=settings.httpx_max_connections),
        )
        providers = []
        for name in settings.search_order_list:
            if name == "firecrawl":
                providers.append(FirecrawlProvider(
                    self.search_http, settings.firecrawl_api_key,
                    settings.firecrawl_timeout_s))
            elif name == "brave":
                providers.append(BraveProvider(
                    self.search_http, settings.brave_api_key, settings.brave_timeout_s))
            elif name == "duckduckgo":
                providers.append(DuckDuckGoProvider(settings.ddg_timeout_s))
        self.search = SearchRouter(providers, self.search_http)

        # 上层服务
        self.quota = QuotaManager(self.daos, settings)
        self.context = ContextBuilder(self.daos, default_budget=settings.default_token_budget)
        self.compactor = Compactor(
            self.daos, self.chat_api,
            summary_model=settings.model_summary,
            trigger_ratio=settings.compact_trigger_ratio,
        )
        self.memory = MemoryService(self.daos, self.chat_api,
                                    extract_model=settings.model_summary)
        self.agent = Agent(self.chat_api)
        self.workers = GenWorkerPool(
            bot, self.daos, self.video_api, self.music_api, self.files_api,
            self.quota, self.guard, self.limiter, self.registry,
            poll_interval_s=settings.worker_poll_interval_s,
            callback_url=settings.mmx_callback_url,
        )

    async def startup(self) -> None:
        await self.db.connect()
        for sid in self.settings.superadmin_id_list:
            await self.daos.users.ensure_superadmin(sid)
        if self.settings.superadmin_id_list:
            log.info("超级管理员已就位", 名单=self.settings.superadmin_id_list)
        recovered = await self.workers.recover_pending()
        log.info("服务容器启动完成", MiniMaxKey数=self.mmx.key_count,
                 恢复任务数=recovered, 模式=self.settings.mode)

    async def shutdown(self) -> None:
        await self.registry.shutdown()
        await self.mmx.close()
        await self.search_http.aclose()
        await self.db.close()
        log.info("服务容器已关停")
