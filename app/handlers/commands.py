"""命令 handler —— /start /help /reset + 授权/配额/超管命令(plan §14.3)。

输出优化(plan §斜杠命令输出优化):
- 列表命令(/memories /quotas /users /audit /stats)分页 + 内联键盘翻页/关闭
- 信息命令(/start /help /whoami /quota)HTML 排版 + 视图间导航键盘
- logic_* 纯函数返回 HTML 文本(无键盘),供 guest 模式复用
"""

from __future__ import annotations

import json

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from app.core.auth import require_role
from app.core.htmlfmt import sanitize_telegram_html
from app.db.models import User
from app.handlers.admin_actions import (
    extract_target_info,
    logic_grant,
    logic_resetquota,
    logic_revoke,
    logic_setquota,
    logic_userinfo,
)
from app.handlers.lists import (
    mmx_quota_kb,
    mmx_quota_result_kb,
    render_audit,
    render_help,
    render_memories,
    render_mmx_quota,
    render_quota_view,
    render_quotas,
    render_stats,
    render_users,
    render_whoami,
)
from app.logging import get_logger
from app.services import Services

log = get_logger("handlers.commands")

router = Router(name="commands")


def _reply_target_id(message: Message) -> int | None:
    """仅从回复目标提取用户 ID(不含参数回退);供 setquota/resetquota 区分回复模式。"""
    reply = getattr(message, "reply_to_message", None) or getattr(message, "external_reply", None)
    if reply and getattr(reply, "from_user", None):
        return reply.from_user.id
    return None


def _parse_target_id(message: Message, command: CommandObject) -> int | None:
    """从「回复目标」或「命令参数」解析目标用户 ID。"""
    rid = _reply_target_id(message)
    if rid is not None:
        return rid
    if command.args:
        first = command.args.split()[0]
        try:
            return int(first)
        except ValueError:
            return None
    return None


def _split_command(text: str) -> tuple[str, str]:
    """从 '/cmd@bot args...' 解析出 (cmd_lower, args)。非命令返回 ('', '')。"""
    if not text.startswith("/"):
        return "", ""
    parts = text.split(None, 1)
    head = parts[0].lstrip("/")
    # 去掉 @bot 后缀
    cmd = head.split("@", 1)[0].lower()
    args = parts[1].strip() if len(parts) > 1 else ""
    return cmd, args


# ── 纯逻辑(返回 HTML 文本,供 message handler 与 guest 共用) ─────
async def logic_start(svc: Services, user: User) -> str:
    text, _ = await render_help(scope="user", owner=user.tg_id, uid=user.tg_id)
    return f"您好,{user.first_name or '朋友'}! 我是您的助理机器人。\n\n{text}"


async def logic_help() -> str:
    text, _ = await render_help(scope="user", owner=0, uid=0)
    return text


async def logic_whoami(svc: Services, user: User) -> str:
    text, _ = await render_whoami(svc, user, scope="user", owner=user.tg_id, uid=user.tg_id)
    return text


async def logic_reset(svc: Services, user: User, chat_id: int) -> str:
    async with svc.user_lock.for_user(user.tg_id):
        await svc.daos.messages.clear_chat(chat_id)
    log.info("会话已重置", 用户=user.tg_id, 会话=chat_id)
    return "🧹 当前会话上下文已清空(长期记忆保留)"


async def logic_quota(svc: Services, user: User) -> str:
    text, _ = await render_quota_view(svc, user, scope="user", owner=user.tg_id, uid=user.tg_id)
    return text


async def logic_remember(svc: Services, user: User, scope: str, owner: int, args: str) -> str:
    if not args:
        return "用法:/remember 要记住的内容"
    mem_id = await svc.memory.remember(scope, owner, args)
    return f"🧠 已记住(编号 {mem_id})"


async def logic_forget(svc: Services, user: User, scope: str, owner: int, args: str) -> str:
    if not args or not args.strip().isdigit():
        return "用法:/forget 记忆编号"
    ok = await svc.daos.memories.delete(int(args.strip()), scope, owner)
    return "🗑 已删除" if ok else "未找到该记忆(只能删除自己范围内的)"


# ── 基础命令 ───────────────────────────────────────────────────
async def _send_info(message: Message, text: str, kb) -> None:
    """统一发送信息视图(HTML + 内联键盘)。"""
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.message(Command("start"))
async def cmd_start(message: Message, user: User, svc: Services) -> None:
    help_text, kb = await render_help(scope="user", owner=user.tg_id, uid=user.tg_id)
    text = f"您好,{user.first_name or '朋友'}! 我是您的助理机器人。\n\n{help_text}"
    await _send_info(message, text, kb)


@router.message(Command("help"))
async def cmd_help(message: Message, user: User) -> None:
    text, kb = await render_help(scope="user", owner=user.tg_id, uid=user.tg_id)
    await _send_info(message, text, kb)


@router.message(Command("whoami"))
async def cmd_whoami(message: Message, user: User, svc: Services) -> None:
    text, kb = await render_whoami(svc, user, scope="user", owner=user.tg_id, uid=user.tg_id)
    await _send_info(message, text, kb)


@router.message(Command("reset"))
async def cmd_reset(message: Message, user: User, svc: Services) -> None:
    await message.answer(await logic_reset(svc, user, message.chat.id))


@router.message(Command("quota"))
async def cmd_quota(message: Message, user: User, svc: Services) -> None:
    text, kb = await render_quota_view(svc, user, scope="user", owner=user.tg_id, uid=user.tg_id)
    await _send_info(message, text, kb)


# ── 记忆命令 ───────────────────────────────────────────────────
def _scope_of(message: Message, user: User) -> tuple[str, int]:
    if message.chat.type == "private":
        return "user", user.tg_id
    return "chat", message.chat.id


def _explicit_dispatcher(svc: Services, message: Message, user: User, *,
                         references=None):
    """显式命令复用的工具分发器(生成/搜索命令共用)。

    references:可选的参考素材(图片/音频),用于图生图/图生视频/音色复刻。
    """
    from app.handlers.pipeline import build_dispatcher

    scope, owner = _scope_of(message, user)
    return build_dispatcher(svc, user, message.chat.id, scope, owner,
                            references=references)


@router.message(Command("remember"))
async def cmd_remember(message: Message, command: CommandObject, user: User, svc: Services) -> None:
    scope, owner = _scope_of(message, user)
    await message.answer(await logic_remember(svc, user, scope, owner, command.args or ""))


@router.message(Command("memories"))
async def cmd_memories(message: Message, user: User, svc: Services) -> None:
    scope, owner = _scope_of(message, user)
    text, kb = await render_memories(svc, scope, owner, user.tg_id, page=1)
    await message.answer(text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)


@router.message(Command("forget"))
async def cmd_forget(message: Message, command: CommandObject, user: User, svc: Services) -> None:
    scope, owner = _scope_of(message, user)
    await message.answer(await logic_forget(svc, user, scope, owner, command.args or ""))


# ── 管理命令(admin+) ─────────────────────────────────────────
@router.message(Command("grant"))
async def cmd_grant(message: Message, command: CommandObject, user: User, svc: Services) -> None:
    if not require_role(user, "admin"):
        await message.answer("⛔ 需要管理员权限")
        return
    info = extract_target_info(message, command.args or "")
    if info is None:
        await message.answer("用法:/grant <用户ID>(或回复目标用户的消息)")
        return
    await message.answer(
        await logic_grant(svc, user, info.tg_id, username=info.username, first_name=info.first_name),
        parse_mode="HTML",
    )


@router.message(Command("revoke"))
async def cmd_revoke(message: Message, command: CommandObject, user: User, svc: Services) -> None:
    if not require_role(user, "admin"):
        await message.answer("⛔ 需要管理员权限")
        return
    info = extract_target_info(message, command.args or "")
    if info is None:
        await message.answer("用法:/revoke <用户ID>(或回复目标用户的消息)")
        return
    await message.answer(
        await logic_revoke(svc, user, info.tg_id, username=info.username, first_name=info.first_name),
        parse_mode="HTML",
    )


@router.message(Command("setquota"))
async def cmd_setquota(message: Message, command: CommandObject, user: User, svc: Services) -> None:
    if not require_role(user, "admin"):
        await message.answer("⛔ 需要管理员权限")
        return
    parts = (command.args or "").split()
    if len(parts) < 3 or parts[1] not in ("calls", "tokens"):
        await message.answer(
            "用法:/setquota <用户ID> <calls|tokens> <上限> [day|month|total]\n-1 = 无限"
            "\n(或回复目标用户消息:/setquota <calls|tokens> <上限> [period])"
        )
        return
    # 回复模式:首个参数是 mode 而非用户ID
    reply_target = _reply_target_id(message)
    if reply_target is not None:
        mode, limit_str = parts[0], parts[1]
        target = reply_target
        period = parts[2] if len(parts) > 2 and parts[2] in ("day", "month", "total") else "day"
    else:
        try:
            target, mode, limit_str = int(parts[0]), parts[1], parts[2]
        except ValueError:
            await message.answer("参数错误:用户ID 与上限必须是数字")
            return
        period = parts[3] if len(parts) > 3 and parts[3] in ("day", "month", "total") else "day"
    try:
        limit = int(limit_str)
    except ValueError:
        await message.answer("参数错误:上限必须是数字")
        return
    await message.answer(await logic_setquota(svc, user, target, mode, limit, period), parse_mode="HTML")


@router.message(Command("resetquota"))
async def cmd_resetquota(
    message: Message, command: CommandObject, user: User, svc: Services
) -> None:
    if not require_role(user, "admin"):
        await message.answer("⛔ 需要管理员权限")
        return
    parts = (command.args or "").split()
    reply_target = _reply_target_id(message)
    if reply_target is not None:
        # 回复模式:/resetquota [calls|tokens]
        target = reply_target
        mode = parts[0] if parts and parts[0] in ("calls", "tokens") else None
    elif not parts:
        await message.answer("用法:/resetquota <用户ID> [calls|tokens]\n(或回复目标用户消息)")
        return
    else:
        try:
            target = int(parts[0])
        except ValueError:
            await message.answer("用户ID 必须是数字")
            return
        mode = parts[1] if len(parts) > 1 and parts[1] in ("calls", "tokens") else None
    await message.answer(await logic_resetquota(svc, user, target, mode), parse_mode="HTML")


@router.message(Command("userinfo"))
async def cmd_userinfo(message: Message, command: CommandObject, user: User, svc: Services) -> None:
    if not require_role(user, "admin"):
        await message.answer("⛔ 需要管理员权限")
        return
    target = _parse_target_id(message, command)
    if target is None:
        await message.answer("用法:/userinfo <用户ID>(或回复目标用户的消息)")
        return
    await message.answer(await logic_userinfo(svc, user, target), parse_mode="HTML")


def _require_private(message: Message) -> bool:
    """列表含敏感信息,仅私聊响应。群聊返回 False 并已发提示。"""
    if message.chat.type != "private":
        return False
    return True


@router.message(Command("quotas"))
async def cmd_quotas(message: Message, command: CommandObject, user: User, svc: Services) -> None:
    if not require_role(user, "admin"):
        await message.answer("⛔ 需要管理员权限")
        return
    if not _require_private(message):
        await message.answer("ℹ️ 该命令请在与我的私聊中使用(列表含敏感信息)。")
        return
    page = 1
    if command.args and command.args.strip().isdigit():
        page = max(1, int(command.args.strip()))
    text, kb = await render_quotas(svc, user.tg_id, page)
    await message.answer(text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)


@router.message(Command("users"))
async def cmd_users(message: Message, command: CommandObject, user: User, svc: Services) -> None:
    if not require_role(user, "admin"):
        await message.answer("⛔ 需要管理员权限")
        return
    if not _require_private(message):
        await message.answer("ℹ️ 该命令请在与我的私聊中使用(列表含敏感信息)。")
        return
    page = 1
    if command.args and command.args.strip().isdigit():
        page = max(1, int(command.args.strip()))
    text, kb = await render_users(svc, user.tg_id, page)
    await message.answer(text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)


@router.message(Command("stats"))
async def cmd_stats(message: Message, user: User, svc: Services) -> None:
    if not require_role(user, "admin"):
        await message.answer("⛔ 需要管理员权限")
        return
    if not _require_private(message):
        await message.answer("ℹ️ 该命令请在与我的私聊中使用(列表含敏感信息)。")
        return
    text, kb = await render_stats(svc, user.tg_id, page=1)
    await message.answer(text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)


@router.message(Command("mmxquota"))
async def cmd_mmxquota(message: Message, user: User, svc: Services) -> None:
    """查询 MiniMax Token Plan 用量与剩余额度(管理员)。

    多账号(每个 API Key = 一个 Token Plan 订阅)时弹出内联键盘选择;
    单账号直接查询。
    """
    if not require_role(user, "admin"):
        await message.answer("⛔ 需要管理员权限")
        return
    keys = svc.settings.minimax_keys
    if not keys:
        await message.answer("⚠️ 未配置 MINIMAX_API_KEYS")
        return
    # 单账号:直接查询,省去选择步骤
    if len(keys) == 1:
        await _answer_mmx_quota(message, svc, key_idx=0, uid=user.tg_id)
        return
    # 多账号:弹选择键盘
    kb = mmx_quota_kb(keys, user.tg_id)
    await message.answer("📊 请选择要查询的 Token Plan 账号:", reply_markup=kb)


async def _answer_mmx_quota(message: Message, svc: Services, *, key_idx: int, uid: int) -> None:
    """查询并回复/编辑单个账号的 Token Plan 配额。供命令入口与刷新回调共用。

    出错时不抛(命令入口无键盘可编辑),直接回错误文本。
    """
    from app.minimax.client import MiniMaxError

    keys = svc.settings.minimax_keys
    key = keys[key_idx]
    notifying = await message.answer("⏳ 正在查询 Token Plan 用量…")
    try:
        remains = await svc.quota_api.remains(key)
    except MiniMaxError as e:
        await message.answer(e.user_message())
        return
    text = render_mmx_quota(key_idx, key, remains)
    kb = mmx_quota_result_kb(key_idx, len(keys), uid)
    await message.answer(text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
    try:
        await notifying.delete()
    except Exception as e:
        log.debug("删除查询中提示失败(忽略)", 错误=str(e)[:120])


# ── 超管命令 ───────────────────────────────────────────────────
@router.message(Command("promote"))
async def cmd_promote(message: Message, command: CommandObject, user: User, svc: Services) -> None:
    if not user.is_superadmin:
        await message.answer("⛔ 需要超级管理员权限")
        return
    target = _parse_target_id(message, command)
    if target is None:
        await message.answer("用法:/promote <用户ID>")
        return
    await svc.daos.users.upsert_basic(target, None, None)
    await svc.daos.users.set_role(target, "admin")
    await svc.daos.users.set_authorized(target, True, by=user.tg_id)
    await svc.daos.audit.add(user.tg_id, "promote", target, "提升为管理员")
    from app.handlers.commands_registry import refresh_user_commands

    await refresh_user_commands(svc.bot, target, "admin")
    await message.answer(f"🛡 已提升用户 {target} 为管理员")


@router.message(Command("demote"))
async def cmd_demote(message: Message, command: CommandObject, user: User, svc: Services) -> None:
    if not user.is_superadmin:
        await message.answer("⛔ 需要超级管理员权限")
        return
    target = _parse_target_id(message, command)
    if target is None:
        await message.answer("用法:/demote <用户ID>")
        return
    if target in svc.settings.superadmin_id_list:
        await message.answer("⛔ 不能降级超级管理员")
        return
    await svc.daos.users.set_role(target, "user")
    await svc.daos.audit.add(user.tg_id, "demote", target, "降级为普通用户")
    from app.handlers.commands_registry import refresh_user_commands

    await refresh_user_commands(svc.bot, target, "user")
    await message.answer(f"⬇️ 已将用户 {target} 降级为普通用户")


@router.message(Command("broadcast"))
async def cmd_broadcast(
    message: Message, command: CommandObject, user: User, svc: Services
) -> None:
    if not user.is_superadmin:
        await message.answer("⛔ 需要超级管理员权限")
        return
    if not command.args:
        await message.answer("用法:/broadcast 广播内容")
        return
    ids = await svc.daos.users.list_authorized_ids()
    sent = failed = 0
    for uid in ids:
        try:
            await svc.limiter.acquire()
            await svc.bot.send_message(
                uid, sanitize_telegram_html(f"📢 管理员广播:\n{command.args}"), parse_mode="HTML"
            )
            sent += 1
        except Exception:
            failed += 1
    await svc.daos.audit.add(user.tg_id, "broadcast", None, f"成功{sent} 失败{failed}")
    await message.answer(f"📢 广播完成:成功 {sent},失败 {failed}")
    log.info("广播完成", 操作人=user.tg_id, 成功=sent, 失败=failed)


@router.message(Command("audit"))
async def cmd_audit(message: Message, command: CommandObject, user: User, svc: Services) -> None:
    if not user.is_superadmin:
        await message.answer("⛔ 需要超级管理员权限")
        return
    if not _require_private(message):
        await message.answer("ℹ️ 该命令请在与我的私聊中使用(列表含敏感信息)。")
        return
    page = 1
    if command.args and command.args.strip().isdigit():
        page = max(1, int(command.args.strip()))
    text, kb = await render_audit(svc, user.tg_id, page)
    await message.answer(text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)


# ── 显式生成/搜索命令 ─────────────────────────────────────────
@router.message(Command("image"))
async def cmd_image(message: Message, command: CommandObject, user: User, svc: Services) -> None:
    if not command.args:
        await message.answer("用法:/image 图片描述\n"
                             "💡 回复一张图片再用此命令可进行图生图")
        return
    from app.handlers.media import extract_references
    refs = await extract_references(svc, message)
    d = _explicit_dispatcher(svc, message, user, references=refs)
    result = await d.dispatch("generate_image", json.dumps({"prompt": command.args}))
    if "已生成" not in result:
        await message.answer(result)


@router.message(Command("video"))
async def cmd_video(message: Message, command: CommandObject, user: User, svc: Services) -> None:
    if not command.args:
        await message.answer("用法:/video 视频描述\n"
                             "💡 回复图片可进行图生视频(1张=首帧,2张=首尾帧)")
        return
    from app.handlers.media import extract_references
    refs = await extract_references(svc, message)
    d = _explicit_dispatcher(svc, message, user, references=refs)
    result = await d.dispatch("generate_video", json.dumps({"prompt": command.args}))
    if "已入队" not in result:
        await message.answer(result)


@router.message(Command("tts"))
async def cmd_tts(message: Message, command: CommandObject, user: User, svc: Services) -> None:
    if not command.args:
        await message.answer("用法:/tts 要朗读的文本")
        return
    d = _explicit_dispatcher(svc, message, user)
    result = await d.dispatch("synthesize_speech", json.dumps({"text": command.args}))
    if "已发送" not in result:
        await message.answer(result)


@router.message(Command("music"))
async def cmd_music(message: Message, command: CommandObject, user: User, svc: Services) -> None:
    if not command.args:
        await message.answer("用法:/music 音乐描述")
        return
    d = _explicit_dispatcher(svc, message, user)
    result = await d.dispatch("generate_music", json.dumps({"prompt": command.args}))
    if "已入队" not in result:
        await message.answer(result)


@router.message(Command("clone"))
async def cmd_clone(message: Message, command: CommandObject, user: User, svc: Services) -> None:
    if not command.args:
        await message.answer("用法:回复一条语音/音频,发送 /clone 音色名\n"
                             "💡 音色名8-256字符,首字符必须字母")
        return
    from app.handlers.media import extract_references
    refs = await extract_references(svc, message)
    d = _explicit_dispatcher(svc, message, user, references=refs)
    result = await d.dispatch("clone_voice",
                              json.dumps({"voice_id": command.args.strip()}))
    await message.answer(result, parse_mode="HTML")


@router.message(Command("voices"))
async def cmd_voices(message: Message, command: CommandObject, user: User, svc: Services) -> None:
    voice_type = (command.args or "all").strip().lower()
    if voice_type not in ("system", "voice_cloning", "voice_generation", "all"):
        voice_type = "all"
    d = _explicit_dispatcher(svc, message, user)
    result = await d.dispatch("list_voices",
                              json.dumps({"voice_type": voice_type}))
    await message.answer(result[:4000], parse_mode="HTML")


@router.message(Command("design_voice"))
async def cmd_design_voice(message: Message, command: CommandObject, user: User, svc: Services) -> None:
    if not command.args:
        await message.answer("用法:/design_voice 音色描述\n"
                             "示例:/design_voice 低沉磁性的男声播音员")
        return
    d = _explicit_dispatcher(svc, message, user)
    result = await d.dispatch("design_voice", json.dumps({
        "prompt": command.args,
        "preview_text": "欢迎使用音色设计功能,这是试听音频。",
    }))
    await message.answer(result, parse_mode="HTML")


@router.message(Command("search"))
async def cmd_search(message: Message, command: CommandObject, user: User, svc: Services) -> None:
    if not command.args:
        await message.answer("用法:/search 搜索关键词")
        return
    d = _explicit_dispatcher(svc, message, user)
    result = await d.dispatch("web_search", json.dumps({"query": command.args}))
    await message.answer(result[:4000])


@router.message(Command("fetch"))
async def cmd_fetch(message: Message, command: CommandObject, user: User, svc: Services) -> None:
    if not command.args:
        await message.answer("用法:/fetch 网址")
        return
    d = _explicit_dispatcher(svc, message, user)
    result = await d.dispatch("web_fetch", json.dumps({"url": command.args.strip()}))
    await message.answer(result[:4000])
