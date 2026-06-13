"""请求处理管道 —— 私聊/群聊/Guest 共用:配额→并发→上下文→Agent→落库→后处理。"""
from __future__ import annotations

import json
from typing import Any

from aiogram.types import BufferedInputFile, Message, URLInputFile

from app.core.streaming import StreamRenderer
from app.core.tools import ToolDispatcher
from app.db.models import User
from app.logging import get_logger
from app.search.router import AllProvidersFailed
from app.services import Services
from app.utils.tokens import estimate_tokens

log = get_logger("handlers.pipeline")


def build_dispatcher(svc: Services, user: User, chat_id: int, scope: str,
                     scope_owner: int) -> ToolDispatcher:
    """构造绑定了 user/chat 上下文的工具分发表。"""
    d = ToolDispatcher()

    async def generate_image(args: dict[str, Any]) -> str:
        check = await svc.quota.precheck(user, "calls", svc.quota.call_weight("image"))
        if not check.ok:
            return check.denial_text()
        urls = await svc.image_api.generate(
            args["prompt"],
            aspect_ratio=args.get("aspect_ratio", "1:1"),
            n=int(args.get("n", 1)),
        )
        if not urls:
            return "图片生成失败:未返回图片"
        sent = 0
        for u in urls:
            try:
                await svc.limiter.acquire()
                await svc.bot.send_photo(chat_id, URLInputFile(u))
                sent += 1
            except Exception as e:
                log.warning("图片发送失败", 会话=chat_id, 错误=str(e)[:120])
        await svc.quota.settle(user, "calls", svc.quota.call_weight("image"),
                               chat_id=chat_id, kind="image")
        return f"已生成并发送 {sent} 张图片。"

    async def generate_video(args: dict[str, Any]) -> str:
        check = await svc.quota.precheck(user, "calls", svc.quota.call_weight("video"))
        if not check.ok:
            return check.denial_text()
        await svc.limiter.acquire()
        ph = await svc.bot.send_message(chat_id, "🎬 视频生成中,完成后会发到这里…")
        gen_id, task_id = await svc.workers.submit_video(
            user, chat_id, args["prompt"],
            duration=int(args.get("duration", 6)),
            resolution=args.get("resolution", "768P"),
            placeholder_msg_id=ph.message_id,
        )
        return (f"视频生成任务已入队(任务ID {task_id}),正在后台生成,"
                f"完成后会自动发送给用户。请告知用户可以继续聊其他话题。")

    async def synthesize_speech(args: dict[str, Any]) -> str:
        check = await svc.quota.precheck(user, "calls", svc.quota.call_weight("tts"))
        if not check.ok:
            return check.denial_text()
        audio_bytes, audio_url, dur_ms = await svc.tts_api.synthesize(
            args["text"],
            voice_id=args.get("voice_id", "male-qn-qingse"),
            emotion=args.get("emotion"),
        )
        await svc.limiter.acquire()
        if audio_bytes:
            await svc.bot.send_voice(
                chat_id, BufferedInputFile(audio_bytes, filename="speech.mp3"))
        elif audio_url:
            data = await svc.files_api.download(audio_url)
            await svc.bot.send_voice(
                chat_id, BufferedInputFile(data, filename="speech.mp3"))
        else:
            return "语音合成失败:无音频返回"
        await svc.quota.settle(user, "calls", svc.quota.call_weight("tts"),
                               chat_id=chat_id, kind="tts")
        return f"语音已发送(时长 {dur_ms / 1000:.1f} 秒)。"

    async def generate_music(args: dict[str, Any]) -> str:
        check = await svc.quota.precheck(user, "calls", svc.quota.call_weight("music"))
        if not check.ok:
            return check.denial_text()
        await svc.limiter.acquire()
        ph = await svc.bot.send_message(chat_id, "🎵 音乐生成中,完成后会发到这里…")
        await svc.workers.submit_music(
            user, chat_id, args["prompt"],
            lyrics=args.get("lyrics"),
            is_instrumental=bool(args.get("is_instrumental", False)),
            placeholder_msg_id=ph.message_id,
        )
        return ("音乐生成任务已入队,正在后台生成,完成后会自动发送给用户。"
                "请告知用户可以继续聊其他话题。")

    async def web_search(args: dict[str, Any]) -> str:
        check = await svc.quota.precheck(user, "calls", svc.quota.call_weight("search"))
        if not check.ok:
            return check.denial_text()
        try:
            results = await svc.search.search(args["query"],
                                              count=int(args.get("count", 5)))
        except AllProvidersFailed as e:
            return e.user_message()
        await svc.quota.settle(user, "calls", svc.quota.call_weight("search"),
                               chat_id=chat_id, kind="search")
        lines = [
            f"{i}. {r['title']}\n   {r['url']}\n   {r['snippet'][:200]}"
            for i, r in enumerate(results, 1)
        ]
        source = results[0]["source"] if results else "?"
        return f"搜索结果(来源 {source}):\n" + "\n".join(lines)

    async def web_fetch(args: dict[str, Any]) -> str:
        try:
            md = await svc.search.fetch(args["url"])
        except AllProvidersFailed as e:
            return e.user_message()
        await svc.daos.usage.add(user.tg_id, chat_id, "fetch")
        return f"网页正文(markdown):\n{md}"

    async def save_memory(args: dict[str, Any]) -> str:
        await svc.memory.remember(scope, scope_owner, args["text"], source="tool")
        return "已存入长期记忆。"

    async def search_memory(args: dict[str, Any]) -> str:
        mems = await svc.memory.recall(scope, scope_owner, args["query"])
        if not mems:
            return "长期记忆中没有相关内容。"
        return "相关记忆:\n" + "\n".join(f"- {m.text}" for m in mems)

    d.register("generate_image", generate_image)
    d.register("generate_video", generate_video)
    d.register("synthesize_speech", synthesize_speech)
    d.register("generate_music", generate_music)
    d.register("web_search", web_search)
    d.register("web_fetch", web_fetch)
    d.register("save_memory", save_memory)
    d.register("search_memory", search_memory)
    return d


async def run_chat_pipeline(
    svc: Services,
    user: User,
    message: Message,
    content: Any,
    renderer: StreamRenderer,
    *,
    scope: str = "user",
    query_text: str = "",
    persist: bool = True,
) -> None:
    """统一对话管道:配额预检 → 并发槽 → 上下文 → Agent → 落库/结算/压缩/记忆。"""
    chat_id = message.chat.id
    scope_owner = user.tg_id if scope == "user" else chat_id

    # 配额预检(tokens 估算:本轮文本)
    est = max(64, estimate_tokens(query_text))
    check = await svc.quota.precheck(user, "tokens", est)
    if not check.ok:
        await renderer.fail(check.denial_text())
        return

    if svc.guard.is_busy("chat"):
        log.info("对话槽位已满,用户进入排队", 用户=user.tg_id, 会话=chat_id)

    async with svc.guard.chat_slot(user.tg_id):
        await renderer.start()

        chat_row = await svc.daos.chats.ensure(
            chat_id, message.chat.type, message.chat.title or message.chat.full_name,
            svc.settings.default_token_budget,
        )
        messages = await svc.context.build(
            chat_id, user.tg_id, content,
            scope=scope, scope_owner=scope_owner, query_text=query_text,
        )

        show_thinking = False
        try:
            show_thinking = bool(json.loads(user.settings or "{}").get("show_thinking"))
        except Exception:
            pass

        dispatcher = build_dispatcher(svc, user, chat_id, scope, scope_owner)
        result = await svc.agent.run(messages, renderer, dispatcher,
                                     show_thinking=show_thinking)

    # ── 后处理(不阻塞用户) ────────────────────────────────
    if persist:
        user_text = query_text if query_text else str(content)[:2000]
        await svc.daos.messages.add(chat_id, user.tg_id, "user", user_text,
                                    tokens=estimate_tokens(user_text))
        if result.text:
            await svc.daos.messages.add(chat_id, None, "assistant", result.text,
                                        tokens=estimate_tokens(result.text))

    if result.total_tokens > 0:
        await svc.quota.settle(user, "tokens", result.total_tokens,
                               chat_id=chat_id, kind="chat")
    elif result.text:
        # 流中断未拿到 usage:按已收 delta 估算兜底
        est_tokens = estimate_tokens(result.text) + est
        await svc.quota.settle(user, "tokens", est_tokens, chat_id=chat_id, kind="chat")
        log.info("usage缺失,按估算结算", 用户=user.tg_id, 估算Token=est_tokens)

    if persist:
        async def _post() -> None:
            await svc.compactor.maybe_compact(chat_id, chat_row.token_budget)
            if query_text and result.text:
                await svc.memory.auto_extract(scope, scope_owner, query_text, result.text)

        svc.registry.spawn(_post(), name=f"post-{chat_id}-{user.tg_id}")
