"""Bot 命令菜单注册 —— setMyCommands 分级 scope(默认中文标注)。

单一数据源:命令→中文描述在本文件集中定义,build_help_html() 复用之。
分级 scope:
  - 默认(BotCommandScopeDefault):所有用户看到的用户命令
  - AllChatAdministrators:群管理员在群里追加看到 admin 命令
  - Chat(chat_id=uid):admin/superadmin 私聊里看到对应级别的命令
不传 language_code = 全员默认即中文(Bot 本就是中文定位)。
"""
from __future__ import annotations

from dataclasses import dataclass

from aiogram import Bot
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllChatAdministrators,
    BotCommandScopeChat,
    BotCommandScopeDefault,
)

from app.db.dao import DAOBundle
from app.logging import get_logger

log = get_logger("handlers.commands_registry")

BOT_DESCRIPTION = "🤖 助理机器人 · 支持对话/绘图/视频/语音/联网搜索/持久记忆。直接发消息即可聊天。"


@dataclass(frozen=True)
class CmdSpec:
    command: str
    description: str


# 用户级命令(所有授权用户)
USER_COMMANDS: list[CmdSpec] = [
    CmdSpec("start", "开始使用 / 介绍"),
    CmdSpec("help", "命令帮助"),
    CmdSpec("whoami", "我的信息(身份/授权/配额)"),
    CmdSpec("reset", "清空当前会话上下文"),
    CmdSpec("quota", "查看我的配额"),
    CmdSpec("remember", "记住长期记忆"),
    CmdSpec("memories", "查看长期记忆"),
    CmdSpec("forget", "删除记忆(编号)"),
    CmdSpec("image", "生成图片"),
    CmdSpec("video", "生成视频"),
    CmdSpec("tts", "文本转语音"),
    CmdSpec("music", "生成音乐"),
    CmdSpec("search", "联网搜索"),
    CmdSpec("fetch", "抓取网页正文"),
]

# 管理员追加(admin+)
ADMIN_COMMANDS: list[CmdSpec] = [
    CmdSpec("grant", "授权用户"),
    CmdSpec("revoke", "撤销授权"),
    CmdSpec("setquota", "设置用户配额"),
    CmdSpec("resetquota", "清零用户配额"),
    CmdSpec("quotas", "配额列表"),
    CmdSpec("users", "用户列表"),
    CmdSpec("stats", "用量统计"),
]

# 超管追加(superadmin)
SUPERADMIN_COMMANDS: list[CmdSpec] = [
    CmdSpec("promote", "提升为管理员"),
    CmdSpec("demote", "降级管理员"),
    CmdSpec("broadcast", "群发广播"),
    CmdSpec("audit", "审计日志(页码)"),
]


def _to_bot_commands(specs: list[CmdSpec]) -> list[BotCommand]:
    return [BotCommand(command=s.command, description=s.description) for s in specs]


def build_help_html() -> str:
    """供 /help 与导航键盘展示的 HTML(单一数据源,避免与菜单漂移)。

    按类别分组,每条命令带中文描述;命令用 <code> 包裹,类别标题加粗。
    """
    def _group(title: str, specs: list[CmdSpec]) -> str:
        items = "\n".join(f"  /{s.command} · {s.description}" for s in specs)
        return f"<b>{title}</b>\n{items}"

    parts = [
        "<b>🤖 助理机器人</b>",
        "直接发消息即可聊天,支持对话 / 绘图 / 视频 / 语音 / 联网搜索 / 持久记忆。",
        _group("基础", USER_COMMANDS[:2]),
        _group("会话与账户", USER_COMMANDS[2:5]),
        _group("持久记忆", USER_COMMANDS[5:8]),
        _group("生成", USER_COMMANDS[8:12]),
        _group("联网", USER_COMMANDS[12:14]),
        _group("管理员", ADMIN_COMMANDS),
        _group("超管", SUPERADMIN_COMMANDS),
    ]
    return "\n\n".join(parts)


async def register_commands(
    bot: Bot,
    daos: DAOBundle,
    superadmin_ids: list[int],
) -> int:
    """注册分级命令菜单到 Telegram。返回已注册的 scope 数。

    任何单步失败只告警不中断(命令菜单不可用不应阻止 bot 启动)。
    """
    user_cmds = _to_bot_commands(USER_COMMANDS)
    admin_cmds = _to_bot_commands(USER_COMMANDS + ADMIN_COMMANDS)
    super_cmds = _to_bot_commands(USER_COMMANDS + ADMIN_COMMANDS + SUPERADMIN_COMMANDS)
    n = 0

    # 1. 默认 scope:所有用户
    try:
        await bot.set_my_commands(user_cmds, scope=BotCommandScopeDefault())
        n += 1
    except Exception as e:
        log.warning("注册默认命令菜单失败", 错误=str(e)[:160])

    # 2. 群管理员 scope:在群里追加 admin 命令
    try:
        await bot.set_my_commands(admin_cmds, scope=BotCommandScopeAllChatAdministrators())
        n += 1
    except Exception as e:
        log.warning("注册管理员命令菜单失败", 错误=str(e)[:160])

    # 3. admin/superadmin 私聊:按角色逐个注册(私聊里也能看到对应命令)
    try:
        staff = await daos.users.list_staff()
    except Exception as e:
        log.warning("读取管理员列表失败,跳过私聊菜单注册", 错误=str(e)[:160])
        staff = []

    for u in staff:
        cmds = super_cmds if u.role == "superadmin" else admin_cmds
        try:
            await bot.set_my_commands(cmds, scope=BotCommandScopeChat(chat_id=u.tg_id))
            n += 1
        except Exception as e:
            log.warning("注册私聊命令菜单失败", 用户=u.tg_id, 错误=str(e)[:160])

    # 4. 超管(可能在 .env 配置但 DB 尚无记录):确保 superadmin 一定有全量菜单
    for sid in superadmin_ids:
        if sid in {u.tg_id for u in staff}:
            continue
        try:
            await bot.set_my_commands(super_cmds, scope=BotCommandScopeChat(chat_id=sid))
            n += 1
        except Exception as e:
            log.warning("注册超管私聊命令菜单失败", 用户=sid, 错误=str(e)[:160])

    # 5. bot 描述(空聊天页展示)
    try:
        await bot.set_my_description(BOT_DESCRIPTION)
    except Exception as e:
        log.warning("设置 bot 描述失败", 错误=str(e)[:160])

    log.info("命令菜单注册完成", scope数=n)
    return n


def commands_for_role(role: str) -> list[BotCommand]:
    """按角色返回该用户应看到的命令列表。"""
    if role == "superadmin":
        return _to_bot_commands(USER_COMMANDS + ADMIN_COMMANDS + SUPERADMIN_COMMANDS)
    if role == "admin":
        return _to_bot_commands(USER_COMMANDS + ADMIN_COMMANDS)
    return _to_bot_commands(USER_COMMANDS)


async def refresh_user_commands(bot: Bot, user_tg_id: int, role: str) -> None:
    """单个用户私聊命令菜单刷新(grant/revoke/promote/demote 后即时生效)。

    superadmin_id_list 里的用户即使 DB 角色暂未同步也按超管处理。
    """
    cmds = commands_for_role(role)
    try:
        await bot.set_my_commands(cmds, scope=BotCommandScopeChat(chat_id=user_tg_id))
        log.info("用户私聊命令菜单已刷新", 用户=user_tg_id, 角色=role)
    except Exception as e:
        log.warning("用户私聊命令菜单刷新失败", 用户=user_tg_id, 错误=str(e)[:160])
