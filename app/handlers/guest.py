"""Guest 模式 handler —— answerGuestQuery + Edit 流式(plan §11/§14.4)。

Bot API 10.0:Update.guest_message 投递召唤消息(aiogram 3.28 原生支持);
鉴权按召唤者 guest_bot_caller_user(AuthMiddleware 的 extract_actor 已处理)。

上下文模型:Guest **不保存任何永久记忆**,仅有 30 分钟临时上下文。
- 每次召唤把消息(含回复关系元数据)写入 messages 表(短期上下文);
- 下次召唤在 30 分钟内 → ContextBuilder 读到上一轮上下文(临时持续记忆);
- 超过 30 分钟无活动 → pipeline 入口的 auto_clear 懒检查清空(空会话自动跳过);
- 永久记忆彻底关闭:不注册 save_memory/search_memory 工具、不抽取记忆、
  /remember 与 /forget 命令返回提示。Guest 清空时一并清理残留 scope=chat 记忆。

注:Guest 模式的 guest_message 仅投递相册首帧(Telegram 平台限制),
故 Guest 场景不支持多图相册,单图处理即可。
"""
from __future__ import annotations

from typing import Any

from aiogram import Router
from aiogram.types import Message

from app.core.streaming import EditRenderer, GuestRenderer
from app.db.models import User
from app.handlers.guest_commands import answer_guest_text, execute_guest_command
from app.handlers.media import build_content
from app.handlers.mentions import strip_bot_mention
from app.handlers.pipeline import run_chat_pipeline
from app.handlers.replies import fold_reply_context
from app.logging import get_logger
from app.services import Services

log = get_logger("handlers.guest")

router = Router(name="guest")


@router.guest_message()
async def handle_guest(message: Message, user: User, svc: Services) -> None:
    # 斜杠命令分流:Guest 消息不走 @router.message,需在此显式拦截文本命令。
    # 注意:inline 启动器发出的消息形如 "@bot /help",以 @ 开头,
    # 必须先剥离 bot 提及再判断是否以 / 开头,否则命令会被漏判落入 AI 流程。
    guest_query_id = getattr(message, "guest_query_id", None)
    me = await svc.bot.me()
    bot_username = me.username or ""
    stripped_text = strip_bot_mention(message.text or "", bot_username)
    if stripped_text.startswith("/"):
        response = await execute_guest_command(svc, user, message, bot_username)
        if response is not None and guest_query_id:
            await answer_guest_text(svc.bot, str(guest_query_id), response)
            return
        # 未知命令 → 落入 AI 流程(自然语言处理)
    await process_guest_message(message, user, svc)


async def process_guest_message(message: Message, user: User, svc: Services) -> None:
    guest_query_id = getattr(message, "guest_query_id", None)
    caller = getattr(message, "guest_bot_caller_user", None)
    log.info("Guest召唤消息", 召唤者=user.tg_id,
             召唤者名=getattr(caller, "username", None) or "?",
             会话=message.chat.id, 查询ID=guest_query_id or "无",
             预览=(message.text or "")[:60])

    content, query_text = await build_content(svc, message)
    if content is None:
        return

    me = await svc.bot.me()
    if isinstance(content, str):
        content = strip_bot_mention(content, me.username or "")
        query_text = content
    else:
        for block in content:
            if block.get("type") == "text":
                block["text"] = strip_bot_mention(block["text"], me.username or "")
        query_text = strip_bot_mention(query_text, me.username or "")

    # Guest 无历史:附引用消息作为唯一上下文(逻辑见 replies.py,三场景共用)。
    content, query_text = await fold_reply_context(svc, message, content, query_text)

    if guest_query_id:
        renderer: Any = GuestRenderer(svc.bot, message.chat.id, str(guest_query_id),
                                      svc.limiter,
                                      throttle_ms=svc.settings.edit_throttle_ms)
    else:
        # 兜底:无 guest_query_id 时按普通编辑流(回复原消息)
        renderer = EditRenderer(svc.bot, message.chat.id, svc.limiter,
                                throttle_ms=svc.settings.edit_throttle_ms,
                                reply_to_message_id=message.message_id,
                                typing_refresh_s=svc.settings.typing_refresh_s)

    # Guest 现在落库:实现「临时上下文 + 30 分钟自动清空」语义。
    # 召唤 N → 召唤 N+1 在 30 分钟内可读到上一轮上下文;超过 30 分钟无活动
    # 由 pipeline 的 auto_clear 懒检查清空。用户也可手动 /reset 立即清空。
    # ★ enable_memory=False:Guest 不保存任何永久记忆 —— 工具集剔除记忆工具、
    #   不抽取/不注入记忆,/remember 与 /forget 命令被禁用。
    await run_chat_pipeline(svc, user, message, content, renderer,
                            scope="chat", query_text=query_text,
                            persist=True, auto_clear=True,
                            enable_memory=False)
