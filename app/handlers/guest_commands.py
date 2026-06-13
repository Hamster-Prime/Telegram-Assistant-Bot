"""Guest 模式斜杠命令执行 —— 文本命令(修复项)。

Guest 消息(guest_message 更新)无法触发 @router.message(Command) 路由,
故在 guest handler 入口对 / 开头消息做命令分流。响应只能经 answerGuestQuery
(Guest bot 非成员,sendMessage 会失败)。逻辑复用 commands.py 的 logic_* 纯函数。
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
    logic_memories,
    logic_quota,
    logic_remember,
    logic_reset,
    logic_start,
    logic_whoami,
)
from app.logging import get_logger
from app.services import Services

log = get_logger("handlers.guest_commands")


async def answer_guest_text(bot: Bot, guest_query_id: str, text: str) -> None:
    """经 answerGuestQuery 把命令应答作为 inline 消息发出(Guest 唯一应答通道)。"""
    await bot(AnswerGuestQuery(
        guest_query_id=str(guest_query_id),
        result=InlineQueryResultArticle(
            id=str(guest_query_id)[:60] or "cmd",
            title="命令响应",
            input_message_content=InputTextMessageContent(message_text=text),
        ),
    ))


async def execute_guest_command(
    svc: Services, user: User, message: Message,
) -> str | None:
    """解析并执行 guest 消息中的斜杠命令。返回应答文本;非已知命令返回 None。

    返回 None 时,调用方应把消息作为普通对话交给 AI 流程。
    """
    text = message.text or ""
    cmd, args = _split_command(text)
    if not cmd:
        return None

    # 管理类命令在 Guest 场景不可用:明确告知而非静默落入 AI 流程
    _ADMIN_CMDS = {
        "grant", "revoke", "setquota", "resetquota", "quotas",
        "users", "stats", "promote", "demote", "broadcast", "audit",
    }
    if cmd in _ADMIN_CMDS:
        return "ℹ️ 该管理命令请直接在与我的私聊中使用,群聊/Guest 场景不支持。"

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
    if cmd in ("remember", "memories", "forget"):
        scope, owner = _scope_of(message, user)
        if cmd == "remember":
            return await logic_remember(svc, user, scope, owner, args)
        if cmd == "memories":
            return await logic_memories(svc, user, scope, owner)
        return await logic_forget(svc, user, scope, owner, args)

    log.debug("Guest 未知命令,落入 AI 流程", 命令=cmd, 用户=user.tg_id)
    return None
