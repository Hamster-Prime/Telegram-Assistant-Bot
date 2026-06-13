"""ContextBuilder —— 按 token 预算自底向上组装上下文(plan §10.1)。

结构:
[system 提示 + 工具说明 + 当前 Asia/Shanghai 时间]
[持久记忆块] ← FTS5 检索 top_k
[历史摘要]   ← 最新 summaries.summary
[近 N 条原始消息](未 compacted)
[本轮用户消息(含多模态 content 块)]
"""
from __future__ import annotations

from typing import Any

from app.db.dao import DAOBundle
from app.logging import get_logger
from app.utils.clock import format_now
from app.utils.tokens import estimate_tokens

log = get_logger("core.context")

SYSTEM_PROMPT = """你是一个乐于助人的中文智能助理,运行在 Telegram 上。
你可以:联网搜索、抓取网页、生成图片/视频/语音/音乐、保存与检索长期记忆、获取当前时间。
回答使用简体中文(除非用户要求其他语言),格式适配 Telegram(可用少量 emoji,代码用等宽块)。
生成视频/音乐为后台异步任务:调用工具后告知用户已开始生成,完成后会另行发送。
当前时间:{now}"""


class ContextBuilder:
    def __init__(self, daos: DAOBundle, *, default_budget: int = 128_000,
                 recent_limit: int = 24, memory_top_k: int = 5) -> None:
        self._daos = daos
        self._budget = default_budget
        self._recent_limit = recent_limit
        self._memory_top_k = memory_top_k

    async def build(
        self,
        chat_id: int,
        user_id: int,
        current_content: Any,
        *,
        scope: str = "user",
        scope_owner: int | None = None,
        query_text: str = "",
        extra_system: str = "",
    ) -> list[dict[str, Any]]:
        """组装 messages 数组。current_content 为字符串或多模态块列表。"""
        owner = scope_owner if scope_owner is not None else (
            user_id if scope == "user" else chat_id
        )

        system_text = SYSTEM_PROMPT.format(now=format_now())
        if extra_system:
            system_text += "\n" + extra_system

        # 持久记忆
        memories = await self._daos.memories.search(scope, owner, query_text,
                                                    top_k=self._memory_top_k)
        if memories:
            mem_lines = "\n".join(f"- {m.text}" for m in memories)
            system_text += f"\n\n[长期记忆]\n{mem_lines}"

        # 历史摘要
        summary = await self._daos.summaries.latest(chat_id)
        if summary:
            system_text += f"\n\n[此前对话摘要]\n{summary['summary']}"

        messages: list[dict[str, Any]] = [{"role": "system", "content": system_text}]

        # 近 N 条原始消息(按预算裁剪:从最新往回收)
        recent = await self._daos.messages.recent_uncompacted(chat_id, self._recent_limit)
        sys_tokens = estimate_tokens(system_text)
        cur_tokens = estimate_tokens(
            current_content if isinstance(current_content, str) else str(current_content)
        )
        budget_left = self._budget - sys_tokens - cur_tokens - 2048  # 留回答余量

        picked: list[dict[str, Any]] = []
        used = 0
        for m in reversed(recent):
            t = m.tokens or estimate_tokens(m.content)
            if used + t > budget_left:
                break
            picked.append({"role": m.role, "content": m.content})
            used += t
        picked.reverse()
        messages.extend(picked)

        messages.append({"role": "user", "content": current_content})

        log.info("上下文已组装", 会话=chat_id, 用户=user_id,
                 系统段Token=sys_tokens, 记忆条数=len(memories),
                 有摘要=bool(summary), 历史条数=len(picked),
                 历史Token=used, 本轮Token=cur_tokens)
        return messages
