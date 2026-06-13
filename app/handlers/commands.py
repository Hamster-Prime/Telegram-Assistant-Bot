"""命令 handler —— /start /help /reset + 授权/配额/超管命令(plan §14.3)。"""
from __future__ import annotations

import time

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from app.core.auth import require_role
from app.db.models import User
from app.handlers.commands_registry import build_help_text
from app.logging import get_logger
from app.services import Services

log = get_logger("handlers.commands")

router = Router(name="commands")

HELP_TEXT = build_help_text()


def _parse_target_id(message: Message, command: CommandObject) -> int | None:
    """从「回复目标」或「命令参数」解析目标用户 ID。"""
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user.id
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


# ── 纯逻辑(返回应答文本,供 message handler 与 guest 共用) ──────
async def logic_start(svc: Services, user: User) -> str:
    return f"你好,{user.first_name or '朋友'}!我是助理机器人。\n{HELP_TEXT}"


async def logic_help() -> str:
    return HELP_TEXT


async def logic_whoami(svc: Services, user: User) -> str:
    role_zh = {"superadmin": "超级管理员", "admin": "管理员", "user": "用户"}[user.role]
    quota_text = await svc.quota.status_text(user)
    return (f"🪪 你的信息\nID:{user.tg_id}\n角色:{role_zh}\n"
            f"授权:{'✅ 已授权' if user.is_allowed else '⛔ 未授权'}\n{quota_text}")


async def logic_reset(svc: Services, user: User, chat_id: int) -> str:
    async with svc.user_lock.for_user(user.tg_id):
        await svc.daos.messages.clear_chat(chat_id)
    log.info("会话已重置", 用户=user.tg_id, 会话=chat_id)
    return "🧹 当前会话上下文已清空(长期记忆保留)"


async def logic_quota(svc: Services, user: User) -> str:
    return "📊 " + await svc.quota.status_text(user)


async def logic_remember(svc: Services, user: User, scope: str, owner: int,
                         args: str) -> str:
    if not args:
        return "用法:/remember 要记住的内容"
    mem_id = await svc.memory.remember(scope, owner, args)
    return f"🧠 已记住(编号 {mem_id})"


async def logic_memories(svc: Services, user: User, scope: str, owner: int) -> str:
    mems = await svc.daos.memories.list_all(scope, owner, limit=30)
    if not mems:
        return "🧠 暂无长期记忆"
    lines = [f"{m.id}. {m.text[:80]}({m.source})" for m in mems]
    return "🧠 长期记忆:\n" + "\n".join(lines)


async def logic_forget(svc: Services, user: User, scope: str, owner: int,
                       args: str) -> str:
    if not args or not args.strip().isdigit():
        return "用法:/forget 记忆编号"
    ok = await svc.daos.memories.delete(int(args.strip()), scope, owner)
    return "🗑 已删除" if ok else "未找到该记忆(只能删除自己范围内的)"


# ── 基础命令 ───────────────────────────────────────────────────
@router.message(Command("start"))
async def cmd_start(message: Message, user: User, svc: Services) -> None:
    await message.answer(await logic_start(svc, user))


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(await logic_help())


@router.message(Command("whoami"))
async def cmd_whoami(message: Message, user: User, svc: Services) -> None:
    await message.answer(await logic_whoami(svc, user))


@router.message(Command("reset"))
async def cmd_reset(message: Message, user: User, svc: Services) -> None:
    await message.answer(await logic_reset(svc, user, message.chat.id))


@router.message(Command("quota"))
async def cmd_quota(message: Message, user: User, svc: Services) -> None:
    await message.answer(await logic_quota(svc, user))


# ── 记忆命令 ───────────────────────────────────────────────────
def _scope_of(message: Message, user: User) -> tuple[str, int]:
    if message.chat.type == "private":
        return "user", user.tg_id
    return "chat", message.chat.id


@router.message(Command("remember"))
async def cmd_remember(message: Message, command: CommandObject, user: User,
                       svc: Services) -> None:
    scope, owner = _scope_of(message, user)
    await message.answer(await logic_remember(svc, user, scope, owner,
                                              command.args or ""))


@router.message(Command("memories"))
async def cmd_memories(message: Message, user: User, svc: Services) -> None:
    scope, owner = _scope_of(message, user)
    await message.answer(await logic_memories(svc, user, scope, owner))


@router.message(Command("forget"))
async def cmd_forget(message: Message, command: CommandObject, user: User,
                     svc: Services) -> None:
    scope, owner = _scope_of(message, user)
    await message.answer(await logic_forget(svc, user, scope, owner,
                                            command.args or ""))


# ── 管理命令(admin+) ─────────────────────────────────────────
@router.message(Command("grant"))
async def cmd_grant(message: Message, command: CommandObject, user: User,
                    svc: Services) -> None:
    if not require_role(user, "admin"):
        await message.answer("⛔ 需要管理员权限")
        return
    target = _parse_target_id(message, command)
    if target is None:
        await message.answer("用法:/grant <用户ID>(或回复目标用户的消息)")
        return
    await svc.daos.users.upsert_basic(target, None, None)
    await svc.daos.users.set_authorized(target, True, by=user.tg_id)
    await svc.quota.ensure_default(target)
    await svc.daos.audit.add(user.tg_id, "grant", target, "授权用户")
    # 刷新该用户私聊命令菜单(若已是 admin/superadmin 则反映对应级别)
    target_user = await svc.daos.users.get(target)
    if target_user is not None and target_user.role in ("admin", "superadmin"):
        from app.handlers.commands_registry import refresh_user_commands
        await refresh_user_commands(svc.bot, target, target_user.role)
    await message.answer(f"✅ 已授权用户 {target}(套用默认配额)")


@router.message(Command("revoke"))
async def cmd_revoke(message: Message, command: CommandObject, user: User,
                     svc: Services) -> None:
    if not require_role(user, "admin"):
        await message.answer("⛔ 需要管理员权限")
        return
    target = _parse_target_id(message, command)
    if target is None:
        await message.answer("用法:/revoke <用户ID>")
        return
    if target in svc.settings.superadmin_id_list:
        await message.answer("⛔ 不能撤销超级管理员")
        return
    await svc.daos.users.set_authorized(target, False, by=user.tg_id)
    await svc.daos.audit.add(user.tg_id, "revoke", target, "撤销授权")
    # 撤权后该用户降为普通菜单(若曾有 admin 命令,现在不再显示)
    from app.handlers.commands_registry import refresh_user_commands
    await refresh_user_commands(svc.bot, target, "user")
    await message.answer(f"🚫 已撤销用户 {target} 的授权")


@router.message(Command("setquota"))
async def cmd_setquota(message: Message, command: CommandObject, user: User,
                       svc: Services) -> None:
    if not require_role(user, "admin"):
        await message.answer("⛔ 需要管理员权限")
        return
    parts = (command.args or "").split()
    if len(parts) < 3 or parts[1] not in ("calls", "tokens"):
        await message.answer(
            "用法:/setquota <用户ID> <calls|tokens> <上限> [day|month|total]\n-1 = 无限")
        return
    try:
        target, mode, limit = int(parts[0]), parts[1], int(parts[2])
    except ValueError:
        await message.answer("参数错误:用户ID 与上限必须是数字")
        return
    period = parts[3] if len(parts) > 3 and parts[3] in ("day", "month", "total") else "day"
    await svc.daos.quotas.set(target, mode, limit, period)
    await svc.daos.audit.add(user.tg_id, "setquota", target,
                             f"{mode}={limit}/{period}")
    await message.answer(f"📊 已设置用户 {target} 配额:{mode} {limit}({period})")


@router.message(Command("resetquota"))
async def cmd_resetquota(message: Message, command: CommandObject, user: User,
                         svc: Services) -> None:
    if not require_role(user, "admin"):
        await message.answer("⛔ 需要管理员权限")
        return
    parts = (command.args or "").split()
    if not parts:
        await message.answer("用法:/resetquota <用户ID> [calls|tokens]")
        return
    try:
        target = int(parts[0])
    except ValueError:
        await message.answer("用户ID 必须是数字")
        return
    mode = parts[1] if len(parts) > 1 and parts[1] in ("calls", "tokens") else None
    await svc.daos.quotas.reset_used(target, mode)
    await svc.daos.audit.add(user.tg_id, "resetquota", target, mode or "all")
    await message.answer(f"🔄 已清零用户 {target} 的{mode or '全部'}配额用量")


@router.message(Command("quotas"))
async def cmd_quotas(message: Message, command: CommandObject, user: User,
                     svc: Services) -> None:
    if not require_role(user, "admin"):
        await message.answer("⛔ 需要管理员权限")
        return
    page = 1
    if command.args and command.args.strip().isdigit():
        page = max(1, int(command.args.strip()))
    quotas = await svc.daos.quotas.list_all(offset=(page - 1) * 20, limit=20)
    if not quotas:
        await message.answer("(无配额记录)")
        return
    lines = [
        f"{q.user_id}:{q.mode} {q.used}/{'∞' if q.unlimited else q.limit_val}({q.period})"
        for q in quotas
    ]
    await message.answer(f"📊 配额列表(第{page}页):\n" + "\n".join(lines))


@router.message(Command("users"))
async def cmd_users(message: Message, command: CommandObject, user: User,
                    svc: Services) -> None:
    if not require_role(user, "admin"):
        await message.answer("⛔ 需要管理员权限")
        return
    page = 1
    if command.args and command.args.strip().isdigit():
        page = max(1, int(command.args.strip()))
    users = await svc.daos.users.list_users(offset=(page - 1) * 20, limit=20)
    total = await svc.daos.users.count()
    role_icon = {"superadmin": "👑", "admin": "🛡", "user": "👤"}
    lines = [
        f"{role_icon.get(u.role, '👤')} {u.tg_id} @{u.username or '-'} "
        f"{'✅' if u.is_allowed else '⛔'}"
        for u in users
    ]
    await message.answer(f"👥 用户列表(共{total}人,第{page}页):\n" + "\n".join(lines))


@router.message(Command("stats"))
async def cmd_stats(message: Message, user: User, svc: Services) -> None:
    if not require_role(user, "admin"):
        await message.answer("⛔ 需要管理员权限")
        return
    day_ago = int(time.time()) - 86400
    stats = await svc.daos.usage.stats(since=day_ago)
    if not stats:
        await message.answer("📈 近24小时无用量")
        return
    lines = [
        f"{s['kind']}:{s['次数']}次,calls={s['调用量'] or 0},tokens={s['Token量'] or 0}"
        for s in stats
    ]
    await message.answer("📈 近24小时用量:\n" + "\n".join(lines))


# ── 超管命令 ───────────────────────────────────────────────────
@router.message(Command("promote"))
async def cmd_promote(message: Message, command: CommandObject, user: User,
                      svc: Services) -> None:
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
async def cmd_demote(message: Message, command: CommandObject, user: User,
                     svc: Services) -> None:
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
async def cmd_broadcast(message: Message, command: CommandObject, user: User,
                        svc: Services) -> None:
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
            await svc.bot.send_message(uid, f"📢 管理员广播:\n{command.args}")
            sent += 1
        except Exception:
            failed += 1
    await svc.daos.audit.add(user.tg_id, "broadcast", None,
                             f"成功{sent} 失败{failed}")
    await message.answer(f"📢 广播完成:成功 {sent},失败 {failed}")
    log.info("广播完成", 操作人=user.tg_id, 成功=sent, 失败=failed)


@router.message(Command("audit"))
async def cmd_audit(message: Message, command: CommandObject, user: User,
                    svc: Services) -> None:
    if not user.is_superadmin:
        await message.answer("⛔ 需要超级管理员权限")
        return
    n = 20
    if command.args and command.args.strip().isdigit():
        n = min(100, int(command.args.strip()))
    rows = await svc.daos.audit.recent(n)
    if not rows:
        await message.answer("(审计日志为空)")
        return
    from datetime import datetime
    lines = [
        f"{datetime.fromtimestamp(r['created_at']).strftime('%m-%d %H:%M')} "
        f"{r['actor_id']} {r['action']} → {r['target_id'] or '-'} {r['detail'] or ''}"
        for r in rows
    ]
    await message.answer("📋 审计日志:\n" + "\n".join(lines))


# ── 显式生成/搜索命令 ─────────────────────────────────────────
@router.message(Command("image"))
async def cmd_image(message: Message, command: CommandObject, user: User,
                    svc: Services) -> None:
    if not command.args:
        await message.answer("用法:/image 图片描述")
        return
    from app.handlers.pipeline import build_dispatcher
    import json as _json
    scope, owner = _scope_of(message, user)
    d = build_dispatcher(svc, user, message.chat.id, scope, owner)
    result = await d.dispatch("generate_image", _json.dumps({"prompt": command.args}))
    if "已生成" not in result:
        await message.answer(result)


@router.message(Command("video"))
async def cmd_video(message: Message, command: CommandObject, user: User,
                    svc: Services) -> None:
    if not command.args:
        await message.answer("用法:/video 视频描述")
        return
    from app.handlers.pipeline import build_dispatcher
    import json as _json
    scope, owner = _scope_of(message, user)
    d = build_dispatcher(svc, user, message.chat.id, scope, owner)
    result = await d.dispatch("generate_video", _json.dumps({"prompt": command.args}))
    if "已入队" not in result:
        await message.answer(result)


@router.message(Command("tts"))
async def cmd_tts(message: Message, command: CommandObject, user: User,
                  svc: Services) -> None:
    if not command.args:
        await message.answer("用法:/tts 要朗读的文本")
        return
    from app.handlers.pipeline import build_dispatcher
    import json as _json
    scope, owner = _scope_of(message, user)
    d = build_dispatcher(svc, user, message.chat.id, scope, owner)
    result = await d.dispatch("synthesize_speech", _json.dumps({"text": command.args}))
    if "已发送" not in result:
        await message.answer(result)


@router.message(Command("music"))
async def cmd_music(message: Message, command: CommandObject, user: User,
                    svc: Services) -> None:
    if not command.args:
        await message.answer("用法:/music 音乐描述")
        return
    from app.handlers.pipeline import build_dispatcher
    import json as _json
    scope, owner = _scope_of(message, user)
    d = build_dispatcher(svc, user, message.chat.id, scope, owner)
    result = await d.dispatch("generate_music", _json.dumps({"prompt": command.args}))
    if "已入队" not in result:
        await message.answer(result)


@router.message(Command("search"))
async def cmd_search(message: Message, command: CommandObject, user: User,
                     svc: Services) -> None:
    if not command.args:
        await message.answer("用法:/search 搜索关键词")
        return
    from app.handlers.pipeline import build_dispatcher
    import json as _json
    scope, owner = _scope_of(message, user)
    d = build_dispatcher(svc, user, message.chat.id, scope, owner)
    result = await d.dispatch("web_search", _json.dumps({"query": command.args}))
    await message.answer(result[:4000])


@router.message(Command("fetch"))
async def cmd_fetch(message: Message, command: CommandObject, user: User,
                    svc: Services) -> None:
    if not command.args:
        await message.answer("用法:/fetch 网址")
        return
    from app.handlers.pipeline import build_dispatcher
    import json as _json
    scope, owner = _scope_of(message, user)
    d = build_dispatcher(svc, user, message.chat.id, scope, owner)
    result = await d.dispatch("web_fetch", _json.dumps({"url": command.args.strip()}))
    await message.answer(result[:4000])
