"""请求处理管道 —— 私聊/群聊/Guest 共用:配额→并发→上下文→Agent→落库→后处理。"""
from __future__ import annotations

import json
import time
from typing import Any

from aiogram.types import Message

from app.core.delivery import DirectDelivery, GuestDelivery, MediaDelivery
from app.core.streaming import GuestRenderer, StreamRenderer
from app.core.tools import ToolDispatcher, tools_without_memory
from app.db.models import User
from app.handlers.media import ReferenceAssets
from app.handlers.mentions import strip_bot_mention
from app.logging import get_logger
from app.search.router import AllProvidersFailed
from app.services import Services
from app.utils.tokens import estimate_tokens

log = get_logger("handlers.pipeline")


def build_dispatcher(
    svc: Services, user: User, chat_id: int, scope: str, scope_owner: int,
    delivery: MediaDelivery | None = None,
    references: ReferenceAssets | None = None,
    *,
    enable_memory: bool = True,
) -> ToolDispatcher:
    """构造绑定了 user/chat 上下文的工具分发表。

    delivery 决定媒体投递方式:None/Direct=直发(私聊/群聊);Guest=暂存到 renderer。
    references 携带当前消息及被回复消息中的参考素材(图片/音频),
    供图生图/图生视频/音色复刻等工具自动使用。
    enable_memory=False 时(Guest)不注册 save_memory/search_memory:
    模型即便误调也找不到入口,确保 Guest 不写入或读取任何永久记忆。
    """
    if delivery is None:
        delivery = DirectDelivery(svc.bot, chat_id, svc.limiter, svc.files_api)
    refs = references or ReferenceAssets()
    d = ToolDispatcher()

    async def generate_image(args: dict[str, Any]) -> str:
        check = await svc.quota.precheck(user, "calls", svc.quota.call_weight("image"))
        if not check.ok:
            return check.denial_text()
        # 参考图片:自动图生图(用户发送/回复了图片)
        subject_ref: list[dict[str, Any]] | None = None
        style: dict[str, Any] | None = None
        model: str | None = None
        if refs.images:
            subject_ref = [{"type": "character", "image_file": refs.images[0]}]
        # 画风预设 → image-01-live 模型
        style_type = args.get("style_type")
        if style_type:
            model = "image-01-live"
            style = {"style_type": style_type, "style_weight": 0.8}

        urls = await svc.image_api.generate(
            args["prompt"],
            aspect_ratio=args.get("aspect_ratio", "1:1"),
            n=int(args.get("n", 1)),
            subject_reference=subject_ref,
            model=model,
            style=style,
        )
        if not urls:
            return "图片生成失败:未返回图片"
        total = len(urls)
        sent = 0
        mode_tag = "图生图" if subject_ref else "文生图"
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
        return f"已生成并发送 {sent} 张图片({mode_tag})。"

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

        # 参考图片自动选择模式
        first_frame: str | None = None
        last_frame: str | None = None
        subject_ref: list[dict[str, Any]] | None = None
        mode = "文生视频"
        if refs.images:
            ref_mode = args.get("reference_mode", "first_frame")
            if ref_mode == "subject_reference" and refs.images:
                # 主体参考 S2V:结构为 {type, image:[url]}
                subject_ref = [{"type": "character",
                                "image": [refs.images[0]]}]
                mode = "主体参考视频"
            elif len(refs.images) >= 2:
                # 首尾帧 FL2V
                first_frame = refs.images[0]
                last_frame = refs.images[1]
                mode = "首尾帧视频"
            else:
                # 单图 → 图生视频 I2V
                first_frame = refs.images[0]
                mode = "图生视频"

        gen_id, task_id = await svc.workers.submit_video(
            user, chat_id, args["prompt"],
            duration=int(args.get("duration", 6)),
            resolution=args.get("resolution", "768P"),
            placeholder_msg_id=ph_msg_id,
            inline_message_id=inline_id,
            first_frame_image=first_frame,
            last_frame_image=last_frame,
            subject_reference=subject_ref,
        )
        return (f"{mode}任务已入队,正在后台生成,完成后会自动发送给用户。"
                "请告知用户可以继续聊其他话题。")

    async def synthesize_speech(args: dict[str, Any]) -> str:
        check = await svc.quota.precheck(user, "calls", svc.quota.call_weight("tts"))
        if not check.ok:
            return check.denial_text()
        audio_bytes, audio_url, dur_ms = await svc.tts_api.synthesize(
            args["text"],
            voice_id=args.get("voice_id", "male-qn-qingse"),
            emotion=args.get("emotion"),
            language_boost=args.get("language_boost"),
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

    async def clone_voice(args: dict[str, Any]) -> str:
        check = await svc.quota.precheck(user, "calls", svc.quota.call_weight("voice_clone"))
        if not check.ok:
            return check.denial_text()
        if not refs.audio:
            return ("音色复刻需要音频素材。请用户回复一条语音/音频消息,"
                    "或直接发送音频文件后再试。")
        # 上传承载音频 → 调用复刻接口
        try:
            file_id_str = await svc.files_api.upload(
                refs.audio, refs.audio_filename or "clone.mp3",
                purpose="voice_clone",
            )
            file_id = int(file_id_str)
        except ValueError:
            return "音频上传失败:无法获取有效的 file_id。"

        voice_id = args["voice_id"]
        preview_text = args.get("preview_text")
        clone_prompt = None
        result = await svc.voice_api.clone(
            file_id, voice_id,
            clone_prompt=clone_prompt,
            text=preview_text,
            model=svc.settings.model_tts if preview_text else None,
        )
        demo = result.get("demo_audio") or ""
        msg = f"音色复刻成功!音色ID:<code>{voice_id}</code>。"
        if demo:
            # 发送试听音频
            await delivery.send_voice(demo)
            msg += "试听音频已发送。"
        msg += "7天内用该音色合成一次语音即可永久保留。"
        await svc.quota.settle(user, "calls", svc.quota.call_weight("voice_clone"),
                               chat_id=chat_id, kind="voice_clone")
        return msg

    async def design_voice(args: dict[str, Any]) -> str:
        check = await svc.quota.precheck(user, "calls", svc.quota.call_weight("voice_design"))
        if not check.ok:
            return check.denial_text()
        voice_id, trial_bytes = await svc.voice_api.design(
            args["prompt"],
            args["preview_text"],
            voice_id=args.get("voice_id"),
        )
        msg = f"音色设计成功!音色ID:<code>{voice_id}</code>。"
        if trial_bytes:
            # 试听音频为 hex 解码后的原始字节,转投递
            ok = await delivery.send_voice(None, trial_bytes)
            if ok:
                msg += "试听音频已发送。"
            else:
                msg += "试听音频已生成但当前场景无法投递。"
        msg += "7天内用该音色合成一次语音即可永久保留。"
        await svc.quota.settle(user, "calls", svc.quota.call_weight("voice_design"),
                               chat_id=chat_id, kind="voice_design")
        return msg

    async def list_voices(args: dict[str, Any]) -> str:
        voice_type = args.get("voice_type", "all")
        data = await svc.voice_api.list_voices(voice_type)
        lines: list[str] = []
        sys_voices = data.get("system_voice") or []
        if sys_voices:
            lines.append("【系统音色】")
            for v in sys_voices[:30]:
                name = v.get("voice_name") or v.get("voice_id", "?")
                vid = v.get("voice_id", "?")
                desc = (v.get("description") or [""])[0][:40] if v.get("description") else ""
                lines.append(f"• {name} → <code>{vid}</code>" + (f" ({desc})" if desc else ""))
            if len(sys_voices) > 30:
                lines.append(f"  …共 {len(sys_voices)} 个,已显示前 30")
        cloned = data.get("voice_cloning") or []
        if cloned:
            lines.append("【复刻音色】")
            for v in cloned:
                lines.append(f"• <code>{v.get('voice_id', '?')}</code>"
                             f" ({v.get('created_time', '?')})")
        generated = data.get("voice_generation") or []
        if generated:
            lines.append("【设计音色】")
            for v in generated:
                lines.append(f"• <code>{v.get('voice_id', '?')}</code>"
                             f" ({v.get('created_time', '?')})")
        if not lines:
            return f"未查询到 {voice_type} 类型的音色。"
        return "\n".join(lines)

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
    d.register("clone_voice", clone_voice)
    d.register("design_voice", design_voice)
    d.register("list_voices", list_voices)
    d.register("web_search", web_search)
    d.register("web_fetch", web_fetch)
    if enable_memory:
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
    enable_memory: bool = True,
) -> None:
    """统一对话管道:配额预检 → 并发槽 → 上下文 → Agent → 落库/结算/压缩/记忆。

    auto_clear:仅 Guest 启用 —— 进入管道前懒检查 30 分钟无活动则清空上下文,
    实现 Guest 的「短期持续记忆 + 自动过期」语义。Private/Group 不启用,
    由 compaction + /reset 管理上下文。
    enable_memory:False 时(Guest)彻底关闭永久记忆 —— 不注册记忆工具、
    不注入历史记忆、不做自动抽取,模型既无法写也无法读永久记忆。
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
                    # Guest 超时清空:messages + summaries + 该 chat 的 scope 记忆残留
                    # (Guest 本不应有记忆,这里清理旧版本/历史残留,兑现"清空即失效")。
                    await svc.daos.messages.clear_chat(
                        chat_id, scope=scope, owner=scope_owner)
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
            enable_memory=enable_memory,
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
        # 提取参考素材(当前消息 + 被回复消息中的图片/音频)
        from app.handlers.media import extract_references
        references = await extract_references(svc, message)
        dispatcher = build_dispatcher(svc, user, chat_id, scope, scope_owner,
                                      delivery, references=references,
                                      enable_memory=enable_memory)
        # Guest(enable_memory=False):从工具 schema 中剔除记忆工具,
        # 让模型根本看不到这两个工具,杜绝调用尝试。
        active_tools = tools_without_memory() if not enable_memory else None
        result = await svc.agent.run(messages, renderer, dispatcher,
                                     tools=active_tools,
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
            # Guest(enable_memory=False):不抽取/写入永久记忆。
            if enable_memory and query_text and result.text:
                await svc.memory.auto_extract(scope, scope_owner, query_text, result.text)

        svc.registry.spawn(_post(), name=f"post-{chat_id}-{user.tg_id}")
