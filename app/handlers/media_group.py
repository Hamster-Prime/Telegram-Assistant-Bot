"""相册(media group)聚合 —— Telegram 发相册时每张图是独立 Message(共享 media_group_id),
若不做聚合,每张图各触发一次 pipeline,导致多次回复/多次 API 调用。

本模块按 (chat_id, media_group_id) 缓冲消息,等待短超时(无新消息到达)后
一次性回调,把整组消息交给 handler 处理。

注:仅用于私聊/群聊(message 更新)。Guest 模式的 guest_message 仅投递相册首帧
(Telegram 平台限制),不支持多图相册,无需聚合。
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram.types import Message

from app.logging import get_logger

log = get_logger("handlers.media_group")

_FLUSH_TIMEOUT_S = 0.6  # 聚合等待窗口:最后一帧到达后 0.6s 无新帧则 flush


class MediaGroupBuffer:
    """按 media_group_id 聚合相册消息,超时后一次性回调。

    用法:handler 检测到 message.media_group_id 后调用 add_or_dispatch():
    - 返回 True = 已缓冲(album),handler 应立即 return
    - 返回 False = 非相册,handler 走正常单消息流程
    聚合完成时调用 on_complete(messages: list[Message])。
    """

    def __init__(self, timeout_s: float = _FLUSH_TIMEOUT_S) -> None:
        self._timeout_s = timeout_s
        self._groups: dict[tuple[int, str], dict[str, Any]] = {}

    async def add_or_dispatch(
        self,
        message: Message,
        on_complete: Callable[[list[Message]], Awaitable[None]],
    ) -> bool:
        """缓冲相册消息。返回 True=已缓冲(相册),False=非相册。

        on_complete 在聚合完成时被调用(收到的 messages 按 message_id 排序)。
        同一 group 的多条消息各自调用本方法,回调以首次注册的为准(等价闭包)。
        """
        group_id = getattr(message, "media_group_id", None)
        if not group_id:
            return False

        key = (message.chat.id, group_id)
        if key not in self._groups:
            self._groups[key] = {"messages": [], "task": None,
                                 "callback": on_complete}
        group = self._groups[key]
        group["messages"].append(message)

        # 重置 flush 定时器(每收到新帧就推迟 flush)
        old_task = group.get("task")
        if old_task is not None and not old_task.done():
            old_task.cancel()
        group["task"] = asyncio.create_task(self._flush(key))

        log.debug("相册消息已缓冲", 会话=message.chat.id, 组ID=group_id,
                  当前帧数=len(group["messages"]))
        return True

    async def _flush(self, key: tuple[int, str]) -> None:
        """超时后触发:排序消息 → 调用回调 → 清理。"""
        try:
            await asyncio.sleep(self._timeout_s)
        except asyncio.CancelledError:
            return  # 被新帧取消,等下一次 flush

        group = self._groups.pop(key, None)
        if group is None:
            return
        messages: list[Message] = group["messages"]
        callback = group["callback"]
        messages.sort(key=lambda m: m.message_id)
        log.info("相册聚合完成,触发单次处理", 会话=key[0],
                 组ID=key[1], 帧数=len(messages))
        try:
            await callback(messages)
        except Exception as e:
            log.error("相册回调执行失败", 会话=key[0], 组ID=key[1],
                      异常类型=type(e).__name__, 详情=str(e)[:200])
