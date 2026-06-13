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
    assert "当前时间" in msgs[0]["content"]
    assert msgs[-1] == {"role": "user", "content": "你好"}


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


async def test_system_prompt_limits_output_to_telegram_supported_format(daos: DAOBundle):
    cb = ContextBuilder(daos)
    msgs = await cb.build(100, 1, "说明格式要求")
    system = msgs[0]["content"]

    assert "Telegram 支持的格式" in system
    assert "不要使用 Markdown 标题" in system
    assert "# 标题" in system
    assert "不要输出井号" in system


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
    assert msgs[1]["content"] == "第一句"


async def test_context_budget_trims_history(daos: DAOBundle):
    for i in range(20):
        await daos.messages.add(100, 1, "user", f"消息{i}" * 100, tokens=2000)
    cb = ContextBuilder(daos, default_budget=8000)  # 很小的预算
    msgs = await cb.build(100, 1, "新问题")
    # 预算受限,历史只保留少量
    assert len(msgs) < 10


async def test_context_multimodal_content(daos: DAOBundle):
    content = [
        {"type": "text", "text": "这是什么"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,xxx"}},
    ]
    cb = ContextBuilder(daos)
    msgs = await cb.build(100, 1, content, query_text="这是什么")
    assert msgs[-1]["content"] == content


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
