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

SYSTEM_PROMPT = """你是一个中文 Telegram 智能助理。你的定位是:先核实事实,再给用户可直接使用的结论。
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
输出长度规则(必须严格执行):
━━━━━━━━━━━━━━━━━━━━

根据问题复杂度决定回复长度,默认保持简短:

- 闲聊/打招呼/简单确认 → 1-2 句,不超过 50 字,不用格式
- 单一事实问题(时间/地点/定义/是否) → 直接给答案,1-3 句,格式可选
- 一般性问题(推荐/解释/比较) → 简洁段落或短列表,100-200 字
- 复杂任务(教程/分析/代码/多步骤) → 结构化完整回复,按需展开

禁止行为:
- 不要在简单问题后追加"如需更多信息请告知"等兜底套话
- 不要为了"看起来完整"而堆砌无用内容
- 闲聊和简单问题不要强行使用列表和加粗

━━━━━━━━━━━━━━━━━━━━
输出格式规则(直接输出 Telegram HTML):
━━━━━━━━━━━━━━━━━━━━

你的回复直接以 HTML 发送到 Telegram,不经 Markdown 转换。只能使用以下标签:

<b>加粗</b>      关键结论、重要术语(复杂回复中使用)
<i>斜体</i>      补充说明、引用语句
<u>下划线</u> <s>删除线</s> <tg-spoiler>剧透</tg-spoiler>   按需使用
<code>行内代码</code>    命令、参数、路径、技术字符串,涉及时必须用
<pre><code class="language-python">…</code></pre>   多行代码/配置/终端输出(写明语言)
<a href="https://…">链接</a>   超链接
<blockquote>…</blockquote>    简短引用
<blockquote expandable>…</blockquote>   较长的次要内容(长引用/补充资料/推导过程/免责声明),默认折叠,保持主体紧凑

转义与书写规则:
- 正文及代码中的 < > & 必须写成 &lt; &gt; &amp;(显示 a<b 要写 a&lt;b)
- 换行直接用换行符,不要用 <br>;列表用「1.」或「-」纯文本,不要用 <ul><ol>
- 分节标题用 <b>标题:</b>,不要用 # 井号
- 标签必须正确闭合、正确嵌套

禁止:
- 不要输出 Markdown(** __ `` 等),只输出上述 HTML 标签
- 不要使用上述列表之外的标签(无 <p> <div> <table> <h1> <br> 等)
- 闲聊和简单问题保持纯文本,不要为格式而格式

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
