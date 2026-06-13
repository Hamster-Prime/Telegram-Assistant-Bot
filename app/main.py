"""入口 —— 建 Bot/Dispatcher、注册中间件与路由、webhook/polling 双模式启动。"""
from __future__ import annotations

import asyncio
import sys

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from app.config import get_settings
from app.core.auth import AuthMiddleware
from app.handlers import commands, errors, group, guest, private
from app.logging import get_logger, setup_logging
from app.server import build_app
from app.services import Services

log = get_logger("main")

ALLOWED_UPDATES = ["message", "edited_message", "callback_query", "guest_message"]


def build_dispatcher(svc: Services) -> Dispatcher:
    dp = Dispatcher()
    dp["svc"] = svc

    # 鉴权中间件:message 与 guest_message 都过授权门控
    auth = AuthMiddleware(svc.daos, svc.settings)
    dp.message.middleware(auth)
    dp.guest_message.middleware(auth)

    # 路由顺序:错误兜底 → 命令 → 三场景
    dp.include_router(errors.router)
    dp.include_router(commands.router)
    dp.include_router(private.router)
    dp.include_router(group.router)
    dp.include_router(guest.router)
    return dp


async def run() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    log.info("机器人启动中", 模式=settings.mode,
             MiniMaxKey数=len(settings.minimax_keys),
             对话模型=settings.model_chat)

    if not settings.bot_token:
        log.error("缺少 BOT_TOKEN,无法启动(复制 .env.example 为 .env 并填写)")
        sys.exit(1)
    if not settings.minimax_keys:
        log.error("缺少 MINIMAX_API_KEYS,无法启动")
        sys.exit(1)

    bot = Bot(settings.bot_token,
              default=DefaultBotProperties(parse_mode=None))
    svc = Services(settings, bot)
    await svc.startup()
    dp = build_dispatcher(svc)

    try:
        if settings.mode == "webhook":
            url = f"{settings.webhook_host.rstrip('/')}/tg/{settings.webhook_secret}"
            await bot.set_webhook(
                url,
                secret_token=settings.webhook_secret,
                allowed_updates=ALLOWED_UPDATES,
                drop_pending_updates=False,
            )
            log.info("Webhook已注册", 地址=url, 允许更新=ALLOWED_UPDATES)
            app = build_app(dp, bot, svc)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, host="0.0.0.0", port=8080)
            await site.start()
            log.info("aiohttp服务已启动", 端口=8080)
            await asyncio.Event().wait()  # 常驻
        else:
            await bot.delete_webhook(drop_pending_updates=False)
            log.info("Polling模式启动", 并发上限=settings.max_concurrent_chats)
            await dp.start_polling(
                bot,
                allowed_updates=ALLOWED_UPDATES,
                handle_as_tasks=True,
                tasks_concurrency_limit=settings.max_concurrent_chats * 2,
            )
    finally:
        await svc.shutdown()
        await bot.session.close()
        log.info("机器人已退出")


def main() -> None:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()
