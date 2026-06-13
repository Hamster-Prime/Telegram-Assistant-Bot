"""持久记忆 —— 写入 / FTS5 检索 / 每轮自动抽取(plan §10.3)。"""
from __future__ import annotations

from app.db.dao import DAOBundle
from app.logging import get_logger
from app.minimax.chat import ChatAPI

log = get_logger("core.memory")

_EXTRACT_PROMPT = """从下面这轮对话中提取值得长期记住的用户信息(偏好、事实、约定等)。
每条一行,简洁陈述;没有值得记的就只输出"无"。最多 3 条。

用户:{user_text}
助手:{assistant_text}"""


class MemoryService:
    def __init__(self, daos: DAOBundle, chat_api: ChatAPI, *, extract_model: str) -> None:
        self._daos = daos
        self._chat = chat_api
        self._model = extract_model

    async def remember(self, scope: str, owner_id: int, text: str,
                       source: str = "manual") -> int:
        return await self._daos.memories.add(scope, owner_id, text.strip(), source)

    async def recall(self, scope: str, owner_id: int, query: str, top_k: int = 5):
        return await self._daos.memories.search(scope, owner_id, query, top_k)

    async def auto_extract(self, scope: str, owner_id: int,
                           user_text: str, assistant_text: str) -> int:
        """每轮自动抽取(廉价模型,后台 create_task 调用)。返回新增条数。"""
        if len(user_text) < 4:
            return 0
        try:
            out = await self._chat.complete(
                [{"role": "user", "content": _EXTRACT_PROMPT.format(
                    user_text=user_text[:1000], assistant_text=assistant_text[:1000])}],
                model=self._model,
                max_completion_tokens=256,
            )
        except Exception as e:
            log.warning("记忆自动抽取失败", 归属=owner_id, 异常类型=type(e).__name__,
                        详情=str(e))
            return 0
        count = 0
        for line in out.splitlines():
            line = line.strip().lstrip("-•· ").strip()
            if not line or line in ("无", "无。"):
                continue
            await self._daos.memories.add(scope, owner_id, line, source="auto")
            count += 1
            if count >= 3:
                break
        if count:
            log.info("记忆自动抽取完成", 范围=scope, 归属=owner_id, 新增条数=count)
        return count
