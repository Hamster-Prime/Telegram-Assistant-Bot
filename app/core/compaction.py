"""滚动摘要压缩 —— 后台任务,M2.7-highspeed 生成结构化摘要(plan §10.2)。"""
from __future__ import annotations

from app.db.dao import DAOBundle
from app.logging import get_logger
from app.minimax.chat import ChatAPI
from app.utils.tokens import estimate_tokens

log = get_logger("core.compaction")

_SUMMARY_PROMPT = """请将以下对话历史压缩为结构化中文摘要,保留:
1. 关键要点与事实
2. 已做出的决定
3. 用户表达的偏好
4. 未决问题
直接输出摘要正文,不要客套。

{existing}对话历史:
{history}"""


class Compactor:
    def __init__(self, daos: DAOBundle, chat_api: ChatAPI, *,
                 summary_model: str, trigger_ratio: float = 0.6,
                 keep_recent: int = 8) -> None:
        self._daos = daos
        self._chat = chat_api
        self._model = summary_model
        self._ratio = trigger_ratio
        self._keep = keep_recent

    async def maybe_compact(self, chat_id: int, budget: int) -> bool:
        """检查并执行压缩。返回是否执行了压缩。回复发出后异步调用,不阻塞。"""
        recent = await self._daos.messages.recent_uncompacted(chat_id, 200)
        total_tokens = sum(m.tokens or estimate_tokens(m.content) for m in recent)
        threshold = int(budget * self._ratio)
        if total_tokens <= threshold and len(recent) <= 60:
            return False
        if len(recent) <= self._keep:
            return False

        old, kept = recent[:-self._keep], recent[-self._keep:]
        log.info("触发上下文压缩", 会话=chat_id, 未压缩条数=len(recent),
                 累计Token=total_tokens, 阈值=threshold,
                 将压缩条数=len(old), 保留条数=len(kept))

        history_text = "\n".join(f"[{m.role}] {m.content[:500]}" for m in old)
        existing = ""
        prev = await self._daos.summaries.latest(chat_id)
        if prev:
            existing = f"已有摘要(请增量合并):\n{prev['summary']}\n\n"

        try:
            summary = await self._chat.complete(
                [{"role": "user",
                  "content": _SUMMARY_PROMPT.format(existing=existing, history=history_text)}],
                model=self._model,
                max_completion_tokens=1024,
            )
        except Exception as e:
            log.error("压缩摘要生成失败", 会话=chat_id, 异常类型=type(e).__name__,
                      详情=str(e))
            return False

        if not summary.strip():
            log.warning("压缩摘要为空,跳过", 会话=chat_id)
            return False

        up_to = old[-1].id or 0
        await self._daos.summaries.add(chat_id, summary, up_to,
                                       estimate_tokens(summary))
        await self._daos.messages.mark_compacted(chat_id, up_to)
        log.info("上下文压缩完成", 会话=chat_id, 覆盖至消息=up_to,
                 摘要Token=estimate_tokens(summary))
        return True
