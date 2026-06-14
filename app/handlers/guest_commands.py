"""Guest 模式斜杠命令执行 —— 文本命令(修复项)。

Guest 消息(guest_message 更新)无法触发 @router.message(Command) 路由,
故在 guest handler 入口对 / 开头消息做命令分流。响应只能经 answerGuestQuery
(Guest bot 非成员,sendMessage 会失败)。逻辑复用 commands.py 的 logic_* 纯函数。

列表命令(/memories 等)在 Guest 场景不支持翻页键盘(inline 消息键盘状态脆弱),
提示用户到私聊发送。信息命令(/help /whoami /quota /start)以 HTML 纯文本应答。
"""
from __future__ import annotations

from aiogram import Bot
from aiogram.methods import AnswerGuestQuery
from aiogram.types import (
    InlineQueryResultArticle,
    InputTextMessageContent,
    Message,
)

from app.db.models import User
from app.handlers.commands import (
    _scope_of,
    _split_command,
    logic_forget,
    logic_help,
    logic_quota,
    logic_remember,
    logic_reset,
    logic_start,
    logic_whoami,
)
from app.logging import get_logger
from app.services import Services

log = get_logger("handlers.guest_commands")

# 列表/分页类命令:Guest 不支持翻页键盘,提示去私聊
_PRIVATE_ONLY_CMDS = {"memories", "quotas", "users", "audit", "stats"}


async def answer_guest_text(bot: Bot, guest_query_id: str, text: str) -> None:
    """经 answerGuestQuery 把命令应答作为 inline 消息发出(Guest 唯一应答通道)。

    统一以 HTML 解析模式发送(logic_* 返回的为 HTML 文本)。
    标题取首行纯文本(剥离 HTML 标签),让 inline 卡片标题有意义。
    """
    import re

    first_line = text.lstrip().split("\n", 1)[0].strip()
    title = re.sub(r"<[^>]+>", "", first_line).strip()[:64] or "命令响应"
    await bot(AnswerGuestQuery(
        guest_query_id=str(guest_query_id),
        result=InlineQueryResultArticle(
            id=str(guest_query_id)[:60] or "cmd",
            title=title,
            input_message_content=InputTextMessageContent(
                message_text=text, parse_mode="HTML"),
        ),
    ))


async def execute_guest_command(
    svc: Services, user: User, message: Message, bot_username: str = "",
) -> str | None:
    """解析并执行 guest 消息中的斜杠命令。返回应答文本;非已知命令返回 None。

    返回 None 时,调用方应把消息作为普通对话交给 AI 流程。
    bot_username 用于剥离消息中的 @bot 提及(inline 启动器发出的命令带提及前缀)。
    """
    raw = message.text or ""
    if bot_username:
        from app.handlers.mentions import strip_bot_mention
        raw = strip_bot_mention(raw, bot_username)
    cmd, args = _split_command(raw)
    if not cmd:
        return None

    # 管理员命令在 Guest 场景下可用:授权/撤销/配额/查看用户
    # (需先回复目标用户的消息,或传用户 ID 作参数)
    _ADMIN_ACTION_CMDS = {"grant", "revoke", "setquota", "resetquota", "userinfo"}
    if cmd in _ADMIN_ACTION_CMDS:
        from app.core.auth import require_role
        from app.handlers.admin_actions import (
            extract_target_info,
            logic_grant,
            logic_resetquota,
            logic_revoke,
            logic_setquota,
            logic_userinfo,
        )
        if not require_role(user, "admin"):
            return "⛔ 需要管理员权限"
        info = extract_target_info(message, args)
        if info is None:
            return ("ℹ️ 请先回复目标用户的消息,或提供用户 ID\n"
                    "例如:回复对方消息后发送 @bot /grant")
        target = info.tg_id
        if cmd == "grant":
            return await logic_grant(
                svc, user, target, username=info.username, first_name=info.first_name)
        if cmd == "revoke":
            return await logic_revoke(
                svc, user, target, username=info.username, first_name=info.first_name)
        if cmd == "userinfo":
            return await logic_userinfo(svc, user, target)
        if cmd == "setquota":
            # 回复模式下首个参数是 mode;否则首个是 target(已被 extract_target_info 消费)
            rest = args.split()
            # 若首个参数是数字(target),跳过它取后续参数
            if rest and rest[0].isdigit():
                rest = rest[1:]
            if len(rest) < 2 or rest[0] not in ("calls", "tokens"):
                return "用法:/setquota <calls|tokens> <上限> [day|month|total]\n-1 = 无限"
            try:
                limit = int(rest[1])
            except ValueError:
                return "参数错误:上限必须是数字"
            period = rest[2] if len(rest) > 2 and rest[2] in ("day", "month", "total") else "day"
            return await logic_setquota(svc, user, target, rest[0], limit, period)
        if cmd == "resetquota":
            rest = args.split()
            if rest and rest[0].isdigit():
                rest = rest[1:]
            mode = rest[0] if rest and rest[0] in ("calls", "tokens") else None
            return await logic_resetquota(svc, user, target, mode)

    # 其余管理类命令(分页/敏感信息)在 Guest 场景不可用
    _ADMIN_CMDS = {
        "quotas", "users", "stats", "promote", "demote", "broadcast", "audit",
    }
    if cmd in _ADMIN_CMDS:
        return "ℹ️ 该管理命令请直接在与我的私聊中使用。"

    # 分页列表命令:Guest inline 消息键盘状态脆弱,提示去私聊
    if cmd in _PRIVATE_ONLY_CMDS:
        return ("ℹ️ 列表视图请在与我的私聊中发送 "
                f"<code>/{cmd}</code> 查看(支持翻页)。")

    chat_id = message.chat.id
    if cmd == "start":
        return await logic_start(svc, user)
    if cmd == "help":
        return await logic_help()
    if cmd == "whoami":
        return await logic_whoami(svc, user)
    if cmd == "reset":
        return await logic_reset(svc, user, chat_id)
    if cmd == "quota":
        return await logic_quota(svc, user)
    if cmd in ("remember", "forget"):
        scope, owner = _scope_of(message, user)
        if cmd == "remember":
            return await logic_remember(svc, user, scope, owner, args)
        return await logic_forget(svc, user, scope, owner, args)

    log.debug("Guest 未知命令,落入 AI 流程", 命令=cmd, 用户=user.tg_id)
    return None
