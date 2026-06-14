"""Inline 模式 handler —— 启动器 + 命令快捷菜单(plan §inline)。

让 Bot 出现在任意聊天输入 @ 后的下拉菜单(需在 @BotFather 用 /setinline 开启)。
交互设计:
1. 启动器模式 —— 用户输入 @bot 查询词,返回"向助理提问"结果;点选发送后,
   消息文本含 @bot 提及,触发 Guest Mode → Bot 以自己身份回复。
2. 命令快捷菜单 —— 空查询或 / 开头查询时,返回 Guest 可用的命令文章列表,
   点选即发送 "@bot /cmd",触发 Guest 命令分流(/help /whoami /quota 等)。

鉴权:复用 AuthMiddleware(挂在 dp.inline_query);未授权用户看到"未授权"提示结果。

关于缓存:Telegram 客户端在输入 @bot 后会强制发起 InlineQuery 并展示"搜索中…",
直到 Bot 应答。开启 cache_time 让相同查询走 Telegram 服务端缓存秒回,
大幅减少重复输入的等待感(首次查询的服务器往返是客户端固有行为,无法消除)。
- 未授权:cache_time=0(授权变更后需即时生效)
- 静态菜单(空查询):cache_time=300(5 分钟)
- 文本/命令查询:cache_time=60(1 分钟)
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

# Guest 模式可用的 inline 快捷命令(均为无参数信息命令)。
# message_text 形如 "@bot /cmd" → 发送后触发 Guest 命令分流。
# 顺序即展示顺序。
INLINE_COMMANDS: list[tuple[str, str, str]] = [
    # (命令, 图标标题, 命令说明)
    ("/help", "📋 查看帮助", "查看完整功能与命令一览"),
    ("/whoami", "🪪 我的身份", "查看你的角色、授权与配额"),
    ("/quota", "📊 我的配额", "查看本期配额用量与剩余"),
    ("/reset", "🧹 清空会话", "清空当前会话上下文(保留长期记忆)"),
    ("/start", "👋 开始使用", "查看介绍与帮助"),
]

# 管理员追加命令(仅 admin+ 可见;需先回复目标用户消息)
ADMIN_INLINE_COMMANDS: list[tuple[str, str, str]] = [
    ("/grant", "✅ 授权此用户", "需先回复对方消息"),
    ("/revoke", "🚫 取消授权", "需先回复对方消息"),
    ("/userinfo", "📊 查看此人信息", "查看目标用户身份与配额(需先回复对方消息)"),
]

# cache_time 预设
_CACHE_STATIC = 300   # 空查询(静态菜单):5 分钟
_CACHE_QUERY = 60     # 文本/命令查询:1 分钟
_CACHE_DENIED = 0     # 未授权:不缓存(授权变更后即时生效)


@router.inline_query()
async def handle_inline_query(
    query: InlineQuery, user: User | None, svc: Services,
) -> None:
    """启动器 + 命令快捷菜单。

    - 未授权 → 单条"未授权"提示(cache_time=0)
    - 空查询 → 命令菜单 + 启动器(cache_time=300)
    - / 开头 → 过滤匹配命令 + 启动器(cache_time=60)
    - 普通文本 → 仅启动器(cache_time=60)
    """
    me = await svc.bot.me()
    bot_username = me.username or ""
    raw = (query.query or "").strip()[:MAX_INLINE_QUERY]

    # 未授权:返回提示结果(inline 无法直接发 Permission Denied 文本)
    if user is None or not user.is_allowed:
        await query.answer(
            [_article(_id="denied", title="⛔ 未授权",
                      text="你未被授权使用本 Bot,请联系管理员开通。")],
            cache_time=_CACHE_DENIED,
            is_personal=True,
        )
        return

    mention = f"@{bot_username} " if bot_username else ""

    # 管理员可见的追加命令列表
    admin_cmds = ADMIN_INLINE_COMMANDS if user.is_admin else []

    if not raw:
        # 空查询:命令菜单 + 启动器引导
        results = _build_command_results(
            INLINE_COMMANDS, mention, bot_username,
        )
        if admin_cmds:
            results.extend(_build_command_results(admin_cmds, mention, bot_username))
        results.append(_article(
            _id="start",
            title="💬 向助理提问",
            text=(f"{mention}你的问题…" if bot_username else "你的问题…"),
            description="输入问题后发送,我会回答你",
        ))
        await query.answer(results, cache_time=_CACHE_STATIC, is_personal=True)
        return

    if raw.startswith("/"):
        # 命令前缀:过滤匹配的命令 + 启动器
        matched = _filter_commands(raw, admin_cmds)
        results = _build_command_results(matched, mention, bot_username)
        results.append(_article(
            _id="q_" + secrets.token_hex(8),
            title=f"💬 向助理提问:{raw[:40]}",
            text=mention + raw,
            description="点按发送,助理将以自己身份回复",
        ))
        await query.answer(results, cache_time=_CACHE_QUERY, is_personal=True)
        return

    # 普通文本查询:启动器(保持原行为)
    await query.answer(
        [_article(
            _id="q_" + secrets.token_hex(8),
            title=f"💬 向助理提问:{raw[:40]}",
            text=mention + raw,
            description="点按发送,助理将以自己身份回复",
        )],
        cache_time=_CACHE_QUERY,
        is_personal=True,
    )
    log.info("Inline启动器应答", 用户=user.tg_id, 预览=raw[:60])


def _filter_commands(
    prefix: str, extra: list[tuple[str, str, str]] | None = None,
) -> list[tuple[str, str, str]]:
    """按 / 前缀过滤命令(去掉前导 / 后做前缀匹配,大小写不敏感)。

    无匹配时返回全部命令(用户可能只是输入了 / 想看可用命令)。
    extra 为管理员追加命令,合并到基础命令后一起过滤。
    """
    pool = list(INLINE_COMMANDS) + (extra or [])
    q = prefix.lstrip("/").lower()
    matched = [c for c in pool if c[0].lstrip("/").startswith(q)]
    return matched if matched else pool


def _build_command_results(
    commands: list[tuple[str, str, str]],
    mention: str,
    bot_username: str,
) -> list[InlineQueryResultArticle]:
    """把命令清单构造为 InlineQueryResultArticle 列表。

    message_text = "@bot /cmd" → 发送后触发 Guest 命令分流。
    """
    out: list[InlineQueryResultArticle] = []
    for cmd, title, desc in commands:
        out.append(_article(
            _id=f"cmd{cmd}",
            title=title,
            text=f"{mention}{cmd}",
            description=desc,
        ))
    return out


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
