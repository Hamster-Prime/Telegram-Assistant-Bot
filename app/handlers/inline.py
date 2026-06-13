"""Inline 模式 handler —— 启动器模式(plan §inline)。

让 Bot 出现在任意聊天输入 @ 后的下拉菜单(需在 @BotFather 用 /setinline 开启)。
交互设计:启动器模式 —— 用户输入 @bot 查询词,返回单条"向助理提问"结果;
用户点选发送后,消息文本含 @bot 提及,触发 Guest Mode(若已开)→ Bot 以自己身份回复,
形成对话。Guest Mode 未开时,降级为把查询词原样发出。

鉴权:复用 AuthMiddleware(挂在 dp.inline_query);未授权用户看到"未授权"提示结果。
"""
from __future__ import annotations

import secrets

from aiogram import Router
from aiogram.types import (
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
)

from app.db.models import User
from app.logging import get_logger
from app.services import Services

log = get_logger("handlers.inline")

router = Router(name="inline")

MAX_INLINE_QUERY = 256  # InlineQuery.query 上限 256


@router.inline_query()
async def handle_inline_query(
    query: InlineQuery, user: User | None, svc: Services,
) -> None:
    """启动器:返回一条"向助理提问:<查询词>"结果。

    用户点选 → 把带 @bot 提及的消息发到当前聊天 → 触发 Guest Mode 应答。
    """
    me = await svc.bot.me()
    bot_username = me.username or ""
    raw = (query.query or "").strip()[:MAX_INLINE_QUERY]

    # 未授权:返回提示结果(inline 无法直接发 Permission Denied 文本)
    if user is None or not user.is_allowed:
        await query.answer(
            [_article(_id="denied", title="⛔ 未授权",
                      text="你未被授权使用本 Bot,请联系管理员开通。")],
            cache_time=0,
            is_personal=True,
        )
        return

    if not raw:
        # 空查询:引导用户输入
        await query.answer(
            [_article(
                _id="start",
                title="💬 向助理提问",
                text=(f"@{bot_username} 你的问题…" if bot_username else "你的问题…"),
                description="输入问题后发送,我会回答你",
            )],
            cache_time=0,
            is_personal=True,
        )
        return

    # 有查询词:启动器。message_text 带 @bot 提及 → 发送后触发 Guest Mode 应答
    mention = f"@{bot_username} " if bot_username else ""
    await query.answer(
        [_article(
            _id="q_" + secrets.token_hex(8),
            title=f"💬 向助理提问:{raw[:40]}",
            text=mention + raw,
            description="点按发送,助理将以自己身份回复",
        )],
        cache_time=0,
        is_personal=True,
    )
    log.info("Inline启动器应答", 用户=user.tg_id, 预览=raw[:60])


def _article(*, _id: str, title: str, text: str,
             description: str | None = None) -> InlineQueryResultArticle:
    """构造一条 InlineQueryResultArticle 结果。"""
    kw: dict = {
        "id": _id,
        "title": title,
        "input_message_content": InputTextMessageContent(message_text=text),
    }
    if description:
        kw["description"] = description
    return InlineQueryResultArticle(**kw)
