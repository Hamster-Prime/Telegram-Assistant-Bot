"""aiohttp 服务 —— /tg/<secret> Webhook + /mmx/callback + /healthz(plan §15)。"""
from __future__ import annotations

import json

from aiogram import Bot, Dispatcher
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from app.logging import get_logger
from app.services import Services

log = get_logger("server")


def build_app(dp: Dispatcher, bot: Bot, svc: Services) -> web.Application:
    app = web.Application()
    settings = svc.settings

    # Telegram Webhook:立即 ACK + 后台处理(handle_in_background=True 默认)
    SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=settings.webhook_secret,
        handle_in_background=True,
    ).register(app, path=f"/tg/{settings.webhook_secret}")
    setup_application(app, dp, bot=bot)

    async def mmx_callback(request: web.Request) -> web.Response:
        """MiniMax 视频/音乐回调。先回显 challenge(3s 内),再幂等回填。

        鉴权:若配置了 mmx_callback_secret,要求 ?token=<secret> 匹配,否则 401。
        """
        secret = settings.mmx_callback_secret
        if secret and request.query.get("token") != secret:
            log.warning("MiniMax回调:鉴权失败(token 不匹配),拒绝",
                        来源=request.remote or "?")
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            log.warning("MiniMax回调:非法JSON")
            return web.json_response({"error": "invalid json"}, status=400)

        # 校验握手:回显 challenge
        challenge = body.get("challenge")
        if challenge is not None:
            log.info("MiniMax回调握手", challenge=str(challenge)[:50])
            return web.json_response({"challenge": challenge})

        task_id = str(body.get("task_id", ""))
        status = str(body.get("status", "")).lower()
        file_id = body.get("file_id")
        log.info("MiniMax回调到达", 任务ID=task_id, 状态=status,
                 文件ID=file_id or "无")
        if not task_id:
            return web.json_response({"ok": True})

        gen = await svc.daos.generations.get_by_task(task_id)
        if gen is None:
            log.warning("MiniMax回调:未知任务(忽略)", 任务ID=task_id)
            return web.json_response({"ok": True})
        if gen.status in ("success", "failed"):
            log.info("MiniMax回调:任务已终态(幂等忽略)", 任务ID=task_id,
                     当前状态=gen.status)
            return web.json_response({"ok": True})

        if status == "success" and file_id:
            # 回填走后台,3 秒内先应答回调
            svc.registry.spawn(
                svc.workers.finalize_video(gen.id, str(file_id), source="回调"),
                name=f"callback-video-{gen.id}",
            )
        elif status in ("failed", "fail", "error"):
            svc.registry.spawn(
                svc.workers.handle_video_failed_callback(task_id, "生成失败"),
                name=f"callback-fail-{gen.id}",
            )
        return web.json_response({"ok": True})

    async def healthz(request: web.Request) -> web.Response:
        return web.json_response({
            "ok": True,
            "后台任务数": svc.registry.count,
            "MiniMaxKey数": svc.mmx.key_count,
        })

    app.router.add_post("/mmx/callback", mmx_callback)
    app.router.add_get("/healthz", healthz)
    log.info("aiohttp路由已注册",
             Webhook路径="/tg/<secret>", 回调路径="/mmx/callback")
    return app
