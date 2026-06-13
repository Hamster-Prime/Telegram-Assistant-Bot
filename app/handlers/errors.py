"""全局异常处理 —— 兜底所有 handler 未捕获异常,中文日志 + 用户提示。"""
from __future__ import annotations

from aiogram import Router
from aiogram.types import ErrorEvent

from app.logging import get_logger
from app.minimax.client import AllKeysFailedError, MiniMaxError
from app.search.router import AllProvidersFailed

log = get_logger("handlers.errors")

router = Router(name="errors")


@router.error()
async def on_error(event: ErrorEvent) -> bool:
    exc = event.exception
    update = event.update

    chat_id = None
    msg = update.message or getattr(update, "guest_message", None)
    if msg is not None:
        chat_id = msg.chat.id

    if isinstance(exc, AllKeysFailedError):
        user_text = exc.user_message()
        log.error("全局异常:所有MiniMax Key失败", 会话=chat_id,
                  尝试明细=len(exc.attempts))
    elif isinstance(exc, MiniMaxError):
        user_text = f"❌ MiniMax 服务错误(code={exc.code}):{exc.msg}"
        if exc.code == 1008:
            user_text = "❌ MiniMax 账户余额不足,请联系管理员充值。"
        log.error("全局异常:MiniMax业务错误", 会话=chat_id, 错误码=exc.code,
                  详情=exc.msg, 追踪ID=exc.trace_id)
    elif isinstance(exc, AllProvidersFailed):
        user_text = exc.user_message()
        log.error("全局异常:搜索全败", 会话=chat_id, 查询=exc.query[:80])
    else:
        user_text = "⚠️ 处理消息时出现内部错误,请稍后重试。"
        log.error("全局异常:未分类", 会话=chat_id, 异常类型=type(exc).__name__,
                  详情=str(exc)[:300], exc_info=exc)

    if msg is not None:
        try:
            await msg.answer(user_text)
        except Exception as e:
            log.error("错误提示发送失败", 会话=chat_id, 错误=str(e)[:120])
    return True  # 已处理,不再向上抛
