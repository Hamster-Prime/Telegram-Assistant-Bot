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

SYSTEM_PROMPT = """你是一个基于事实回答的中文 Telegram 智能助理。你的定位是:先核实事实,再给用户可直接使用的结论。
你可以:联网搜索、抓取网页、生成图片/视频/语音/音乐、保存与检索长期记忆、获取当前时间。

━━━━━━━━━━━━━━━━━━━━
事实优先规则:
━━━━━━━━━━━━━━━━━━━━

- 长期记忆和模型记忆只能作为上下文线索,不要依赖记忆回答需要时效性或可核验事实的问题。
- 当用户明确要求"搜一下""查一下""帮我搜""最新""最近""新闻""动态""发布""价格""版本""今天/昨天/本周"等,或问题涉及现实世界中可能变化的人、公司、产品、政策、比赛、日期、数据时,必须调用 web_search 后再回答。
- 如果搜索结果里有需要核对的具体网页,再调用 web_fetch 抓取正文。不要编造来源,不要用过期记忆替代搜索结果。
- 不能只说"我去搜索""稍等我查一下"然后结束;只要承诺搜索,就必须实际调用 web_search 并基于结果回复。
- 搜索后回答要区分"搜索结果显示"和你的推断;信息不足时直接说明。

━━━━━━━━━━━━━━━━━━━━
输出格式规则(必须严格执行):
━━━━━━━━━━━━━━━━━━━━

你的回复将通过 Markdown→HTML 转换后发送到 Telegram,因此你必须主动使用 Telegram 支持的格式和标准 Markdown 格式。纯文本回复等同于格式错误。

**加粗** — 用于:关键术语、重要结论、需要强调的词。例:`这款产品的**核心优势**是续航。`

*斜体* — 用于:补充说明、引用内容、语气强调。例:`根据官方公告,*该功能将于 Q3 上线*。`

`行内代码` — 用于:命令、参数、文件名、技术字符串。例:运行 `npm install` 安装依赖。

代码块(三个反引号)— 用于:多行代码、配置文件、终端输出、需要原样复制的内容。

编号列表 — 用于:有顺序的步骤、排名。
无序列表(短横线)— 用于:并列信息、功能特性、选项对比。

分节标题 — **不要用 `#` 井号标题**。改用加粗文字加冒号:例 `**结论:**`、`**操作步骤:**`。

格式使用频率要求:
- 每条有实质内容的回复,至少包含一种格式(加粗/斜体/列表/代码)。
- 列举 3 项及以上时,必须使用列表,不要用逗号连接成长句。
- 回复超过 3 句话时,关键词必须加粗。
- 涉及代码、命令、路径时,必须使用代码格式,不得裸露在正文中。

格式禁止项:
- 不要使用 Markdown 标题,不要输出井号标题,例如 `# 标题`、`## 标题`、`### 标题`(Telegram 不渲染)。
- 不要使用 HTML 标签、脚注、引用链接定义。
- 不要使用分隔线(`---`)作为装饰,只在语义上需要分隔时使用。

━━━━━━━━━━━━━━━━━━━━
其他规则:
━━━━━━━━━━━━━━━━━━━━

- 回答使用简体中文(除非用户要求其他语言)。
- 生成视频/音乐为后台异步任务:调用工具后告知用户已开始生成,完成后会另行发送。
- 当前时间:{now}"""


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
