"""上下文组装 + 压缩测试。"""
from __future__ import annotations

import pytest

from app.core.compaction import Compactor
from app.core.context import ContextBuilder
from app.db.dao import DAOBundle
from app.db.engine import Database


@pytest.fixture
async def daos():
    db = Database(":memory:", wal=False)
    await db.connect()
    bundle = DAOBundle(db)
    yield bundle
    await db.close()


async def test_context_basic_structure(daos: DAOBundle):
    cb = ContextBuilder(daos)
    msgs = await cb.build(100, 1, "你好", query_text="你好")
    assert msgs[0]["role"] == "system"
    # 系统提示已稳定:无实际渲染的时间戳(只有 [⏰ 当前时间: ...] 占位说明文字)
    # 实际时间戳格式为 [⏰ 当前时间: 2026-...],这里检查冒号后跟数字
    import re
    assert not re.search(r"\[⏰ 当前时间: \d", msgs[0]["content"])
    assert msgs[-1]["role"] == "user"
    assert msgs[-1]["content"].startswith("你好")
    assert re.search(r"\[⏰ 当前时间: \d", msgs[-1]["content"])


async def test_system_prompt_requires_search_for_fresh_factual_questions(daos: DAOBundle):
    cb = ContextBuilder(daos)
    msgs = await cb.build(100, 1, "帮我搜一下 Kimi K2 的最新动态", query_text="Kimi K2 最新动态")
    system = msgs[0]["content"]

    assert "事实优先" in system
    assert "不要依赖记忆回答" in system
    assert "必须调用 web_search" in system
    assert "最新" in system
    assert "搜一下" in system
    assert "不能只说" in system and "去搜索" in system


async def test_system_prompt_instructs_direct_html_output(daos: DAOBundle):
    cb = ContextBuilder(daos)
    msgs = await cb.build(100, 1, "说明格式要求")
    system = msgs[0]["content"]

    # 提示词约束模型直出 Telegram HTML(措辞可演进,这里断言稳定要点)
    assert "parse_mode=HTML" in system
    assert "Markdown" in system and "禁止" in system
    assert "<blockquote expandable>" in system
    assert "<code>" in system
    assert "<pre>" in system
    assert "<tg-spoiler>" in system
    assert "&lt;" in system  # 转义规则


async def test_context_includes_memory_and_summary(daos: DAOBundle):
    await daos.memories.add("user", 1, "用户在上海工作")
    await daos.summaries.add(100, "此前讨论了天气", 5, 10)
    cb = ContextBuilder(daos)
    msgs = await cb.build(100, 1, "今天穿什么", query_text="上海 工作")
    sys = msgs[0]["content"]
    assert "用户在上海工作" in sys
    assert "此前讨论了天气" in sys


async def test_context_includes_recent_messages(daos: DAOBundle):
    await daos.messages.add(100, 1, "user", "第一句", tokens=5)
    await daos.messages.add(100, 1, "assistant", "第一答", tokens=5)
    cb = ContextBuilder(daos)
    msgs = await cb.build(100, 1, "继续")
    roles = [m["role"] for m in msgs]
    assert roles == ["system", "user", "assistant", "user"]
    # 历史消息带元数据 header(时间 + 发送者),正文跟在 header 后
    assert "[·" in msgs[1]["content"] or "·" in msgs[1]["content"]
    assert msgs[1]["content"].endswith("第一句")
    assert msgs[2]["content"].endswith("第一答")


async def test_context_budget_trims_history(daos: DAOBundle):
    for i in range(20):
        await daos.messages.add(100, 1, "user", f"消息{i}" * 100, tokens=2000)
    cb = ContextBuilder(daos, default_budget=8000)  # 很小的预算
    msgs = await cb.build(100, 1, "新问题")
    # 预算受限,历史只保留少量
    assert len(msgs) < 10


async def test_history_message_has_sender_and_timestamp(daos: DAOBundle):
    """历史消息带 [时间 · 发送者] 元数据 header(上下文增强核心)。"""
    await daos.messages.add(100, 1, "user", "你好", sender_label="Alice")
    await daos.messages.add(100, None, "assistant", "你好呀")
    cb = ContextBuilder(daos)
    msgs = await cb.build(100, 1, "继续")
    # user 历史消息:header 含 Alice + 时间,正文为「你好」
    user_hist = msgs[1]
    assert user_hist["role"] == "user"
    assert "👤 Alice" in user_hist["content"]
    assert user_hist["content"].endswith("你好")
    # assistant 历史消息:不注入元数据头(避免模型模仿),content 为原始文本
    asst_hist = msgs[2]
    assert asst_hist["role"] == "assistant"
    assert "🤖 助理" not in asst_hist["content"]
    assert asst_hist["content"] == "你好呀"


async def test_history_message_shows_reply_snapshot(daos: DAOBundle):
    """被回复消息的快照出现在历史消息 header 的 ↩️ 行。"""
    await daos.messages.add(
        100, 1, "user", "继续解释", sender_label="Bob",
        reply_snapshot="Alice: 量子力学基础",
    )
    cb = ContextBuilder(daos)
    msgs = await cb.build(100, 1, "新问题")
    hist = msgs[1]
    assert "↩️ 回复「Alice: 量子力学基础」" in hist["content"]


async def test_system_prompt_is_stable_across_calls(daos: DAOBundle):
    """系统提示无 {now},多次调用字节一致(prompt cache 友好)。"""
    cb = ContextBuilder(daos)
    msgs1 = await cb.build(100, 1, "第一问")
    msgs2 = await cb.build(100, 1, "第二问")
    assert msgs1[0]["content"] == msgs2[0]["content"]


async def test_context_multimodal_content(daos: DAOBundle):
    content = [
        {"type": "text", "text": "这是什么"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,xxx"}},
    ]
    cb = ContextBuilder(daos)
    msgs = await cb.build(100, 1, content, query_text="这是什么")
    # 多模态 content 末尾追加时间戳 text 块
    last = msgs[-1]
    assert last["role"] == "user"
    assert isinstance(last["content"], list)
    # 原两个块保留
    assert last["content"][0] == {"type": "text", "text": "这是什么"}
    assert last["content"][1] == {"type": "image_url", "image_url": {"url": "data:image/png;base64,xxx"}}
    # 末尾追加时间戳块
    assert last["content"][-1]["type"] == "text"
    assert "[⏰ 当前时间:" in last["content"][-1]["text"]


class FakeChatAPI:
    """模拟摘要模型。"""

    def __init__(self):
        self.called = 0

    async def complete(self, messages, *, model=None, max_completion_tokens=0):
        self.called += 1
        return "要点:用户聊了很多。决定:无。偏好:简洁。未决:无。"


async def test_compaction_triggers_and_marks(daos: DAOBundle):
    for i in range(30):
        await daos.messages.add(100, 1, "user", f"长消息{i}" * 200, tokens=1500)
    fake = FakeChatAPI()
    compactor = Compactor(daos, fake, summary_model="m", keep_recent=8)
    done = await compactor.maybe_compact(100, budget=10000)  # 30×1500 ≫ 6000
    assert done and fake.called == 1

    summary = await daos.summaries.latest(100)
    assert summary and "要点" in summary["summary"]

    recent = await daos.messages.recent_uncompacted(100, 200)
    assert len(recent) == 8  # 只剩保留的 8 条


async def test_compaction_skips_small_history(daos: DAOBundle):
    await daos.messages.add(100, 1, "user", "短", tokens=1)
    fake = FakeChatAPI()
    compactor = Compactor(daos, fake, summary_model="m")
    done = await compactor.maybe_compact(100, budget=128000)
    assert not done and fake.called == 0
