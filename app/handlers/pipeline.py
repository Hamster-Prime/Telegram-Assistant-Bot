"""请求处理管道 —— 私聊/群聊/Guest 共用:配额→并发→上下文→Agent→落库→后处理。"""
from __future__ import annotations

import json
import time
from typing import Any

from aiogram.types import Message

from app.core.delivery import DirectDelivery, GuestDelivery, MediaDelivery
from app.core.streaming import GuestRenderer, StreamRenderer
from app.core.tools import ToolDispatcher
from app.db.models import User
from app.handlers.mentions import strip_bot_mention
from app.logging import get_logger
from app.search.router import AllProvidersFailed
from app.services import Services
from app.utils.tokens import estimate_tokens

log = get_logger("handlers.pipeline")


def build_dispatcher(
    svc: Services, user: User, chat_id: int, scope: str, scope_owner: int,
    delivery: MediaDelivery | None = None,
) -> ToolDispatcher:
    """构造绑定了 user/chat 上下文的工具分发表。

    delivery 决定媒体投递方式:None/Direct=直发(私聊/群聊);Guest=暂存到 renderer。
    """
    if delivery is None:
        delivery = DirectDelivery(svc.bot, chat_id, svc.limiter, svc.files_api)
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
        total = len(urls)
        sent = 0
        for i, u in enumerate(urls):
            if delivery.is_guest:
                # Guest 单 inline 消息:仅首张作为媒体,其余不展示(在 note 注明)
                if i == 0:
                    note = (f"\n\n⚠️ 共生成 {total} 张,Guest 模式仅展示首张。"
                            if total > 1 else "")
                    ok = await delivery.send_photo(u, note)
                    if ok:
                        sent += 1
                else:
                    sent += 1  # 计数但不投递
            else:
                ok = await delivery.send_photo(u)
                if ok:
                    sent += 1
        await svc.quota.settle(user, "calls", svc.quota.call_weight("image"),
                               chat_id=chat_id, kind="image")
        return f"已生成并发送 {sent} 张图片。"

    async def generate_video(args: dict[str, Any]) -> str:
        check = await svc.quota.precheck(user, "calls", svc.quota.call_weight("video"))
        if not check.ok:
            return check.denial_text()
        if delivery.is_guest:
            # Guest 单 inline 消息:不发占位,worker 完成时 editMessageMedia 回填
            ph_msg_id = None
            inline_id = delivery.inline_message_id
        else:
            ph_msg_id = await delivery.send_placeholder(
                "🎬 视频生成中,完成后会发到这里…")
            inline_id = None
        gen_id, task_id = await svc.workers.submit_video(
            user, chat_id, args["prompt"],
            duration=int(args.get("duration", 6)),
            resolution=args.get("resolution", "768P"),
            placeholder_msg_id=ph_msg_id,
            inline_message_id=inline_id,
        )
        return ("视频生成任务已入队,正在后台生成,完成后会自动发送给用户。"
                "请告知用户可以继续聊其他话题。")

    async def synthesize_speech(args: dict[str, Any]) -> str:
        check = await svc.quota.precheck(user, "calls", svc.quota.call_weight("tts"))
        if not check.ok:
            return check.denial_text()
        audio_bytes, audio_url, dur_ms = await svc.tts_api.synthesize(
            args["text"],
            voice_id=args.get("voice_id", "male-qn-qingse"),
            emotion=args.get("emotion"),
        )
        if not audio_url and not audio_bytes:
            return "语音合成失败:无音频返回"
        ok = await delivery.send_voice(audio_url, audio_bytes)
        if not ok:
            return "语音已合成,但当前场景(Guest 模式)无法投递,请到私聊使用。"
        await svc.quota.settle(user, "calls", svc.quota.call_weight("tts"),
                               chat_id=chat_id, kind="tts")
        return f"语音已发送(时长 {dur_ms / 1000:.1f} 秒)。"

    async def generate_music(args: dict[str, Any]) -> str:
        check = await svc.quota.precheck(user, "calls", svc.quota.call_weight("music"))
        if not check.ok:
            return check.denial_text()
        if delivery.is_guest:
            ph_msg_id = None
            inline_id = delivery.inline_message_id
        else:
            ph_msg_id = await delivery.send_placeholder(
                "🎵 音乐生成中,完成后会发到这里…")
            inline_id = None
        await svc.workers.submit_music(
            user, chat_id, args["prompt"],
            lyrics=args.get("lyrics"),
            is_instrumental=bool(args.get("is_instrumental", False)),
            placeholder_msg_id=ph_msg_id,
            inline_message_id=inline_id,
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
        check = await svc.quota.precheck(user, "calls", svc.quota.call_weight("fetch"))
        if not check.ok:
            return check.denial_text()
        try:
            md = await svc.search.fetch(args["url"])
        except AllProvidersFailed as e:
            return e.user_message()
        await svc.quota.settle(user, "calls", svc.quota.call_weight("fetch"),
                               chat_id=chat_id, kind="fetch")
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


def _extract_user_metadata(message: Message, bot_username: str) -> dict[str, Any]:
    """从入站消息提取上下文元数据,用于持久化。

    返回 {text, tg_message_id, reply_to_tg_id, reply_snapshot, sender_label}。
    - text:已剥离 @bot 提及的干净正文(不含 fold_reply_context 的标记)
    - reply_snapshot:被回复消息的「发送者: 正文」快照(≤200 字)
    """
    # 干净文本:剥离 bot 提及
    raw = message.text or message.caption or ""
    clean_text = strip_bot_mention(raw, bot_username) if raw else ""

    # 发送者标签
    fu = message.from_user
    sender_label = ""
    if fu:
        sender_label = fu.first_name or fu.username or ""

    # 回复元数据
    reply_to_tg_id: int | None = None
    reply_snapshot: str | None = None
    reply_msg = message.reply_to_message or getattr(message, "external_reply", None)
    if reply_msg is not None:
        reply_to_tg_id = getattr(reply_msg, "message_id", None)
        rfu = getattr(reply_msg, "from_user", None) or getattr(reply_msg, "sender", None)
        r_sender = ""
        if rfu is not None:
            r_sender = (getattr(rfu, "first_name", None)
                        or getattr(rfu, "username", "")
                        or "")
        r_text = (getattr(reply_msg, "text", None)
                  or getattr(reply_msg, "caption", None)
                  or "")[:150]
        if r_sender or r_text:
            reply_snapshot = f"{r_sender}: {r_text}".strip()[:200]
        elif getattr(message, "quote", None):
            # external_reply 场景:用户选中的引用片段兜底
            q_text = getattr(message.quote, "text", "") or ""
            if q_text:
                reply_snapshot = q_text[:200]

    return {
        "text": clean_text or "(空消息)",
        "tg_message_id": message.message_id,
        "reply_to_tg_id": reply_to_tg_id,
        "reply_snapshot": reply_snapshot,
        "sender_label": sender_label,
    }


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
    auto_clear: bool = False,
) -> None:
    """统一对话管道:配额预检 → 并发槽 → 上下文 → Agent → 落库/结算/压缩/记忆。

    auto_clear:仅 Guest 启用 —— 进入管道前懒检查 30 分钟无活动则清空上下文,
    实现 Guest 的「短期持续记忆 + 自动过期」语义。Private/Group 不启用,
    由 compaction + /reset 管理上下文。
    """
    chat_id = message.chat.id
    scope_owner = user.tg_id if scope == "user" else chat_id

    # Guest(auto_clear=True):超时懒清空。空会话 last_activity 返回 None → 自动跳过,
    # 不会反复执行。
    if auto_clear and svc.settings.auto_clear_minutes > 0:
        last_ts = await svc.daos.messages.last_activity(chat_id)
        if last_ts is not None:
            idle_s = int(time.time()) - last_ts
            if idle_s > svc.settings.auto_clear_minutes * 60:
                async with svc.user_lock.for_user(user.tg_id):
                    await svc.daos.messages.clear_chat(chat_id)
                log.info("Guest 超时自动清空上下文", 会话=chat_id,
                         空闲分钟=idle_s // 60)

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

        # renderer.start() 已就位:Guest 的 inline_message_id 此时可取 → 决定投递方式
        if isinstance(renderer, GuestRenderer):
            delivery: MediaDelivery = GuestDelivery(renderer)
        else:
            delivery = DirectDelivery(svc.bot, chat_id, svc.limiter, svc.files_api)
        dispatcher = build_dispatcher(svc, user, chat_id, scope, scope_owner, delivery)
        result = await svc.agent.run(messages, renderer, dispatcher,
                                     show_thinking=show_thinking)

    # ── 后处理(不阻塞用户) ────────────────────────────────
    if persist:
        # 提取元数据:干净正文 + 发送者 + 回复关系快照
        me = await svc.bot.me()
        meta = _extract_user_metadata(message, me.username or "")
        user_text = meta["text"] if meta["text"] != "(空消息)" else query_text
        await svc.daos.messages.add(
            chat_id, user.tg_id, "user", user_text,
            tokens=estimate_tokens(user_text),
            tg_message_id=meta["tg_message_id"],
            reply_to_tg_id=meta["reply_to_tg_id"],
            reply_snapshot=meta["reply_snapshot"],
            sender_label=meta["sender_label"],
        )
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
