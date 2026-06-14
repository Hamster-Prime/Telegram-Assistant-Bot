# 状态行替代光标闪烁 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 去掉群聊/Guest 渲染器的光标闪烁，改为 Agent 事件驱动的底部状态行（「正在思考 ...」「正在搜索 ...」「正在生成 ...」），idle 期间不再产生编辑以缓解 429。

**Architecture:** `_TickLoopMixin` 的 idle 分支改为「不发编辑」；新增 `set_status(status)` 协议方法，仅暂存状态字符串，由 tick 循环合并到「正文 + 空行 + 状态行」一起渲染。`Agent.run` 在思考/调用工具/续写节点调用 `set_status`。`_status_for_tool` 纯函数按语义分类映射工具名到文案。

**Tech Stack:** Python 3.11、aiogram 3.28、pytest（asyncio_mode=auto）、ruff。

参考设计：`docs/superpowers/specs/2026-06-14-status-line-replace-cursor-blink.md`

---

## 文件结构

| 文件 | 职责 | 动作 |
|------|------|------|
| `app/core/streaming.py` | 流式渲染（Draft/Edit/Guest + `_TickLoopMixin`） | 修改 |
| `app/core/agent.py` | Agent 主循环，驱动状态切换 | 修改 |
| `tests/test_streaming.py` | 渲染器测试 | 改写闪烁断言 + 新增 |
| `tests/test_agent.py` | Agent 测试 | `FakeRenderer` 加 `set_status` + 新增 |

不改 `DraftRenderer`（私聊，走原生草稿预览）。

---

## Task 1: 状态文案映射纯函数

新增 `_status_for_tool`，纯函数、无副作用、易测试。先写测试再实现。

**Files:**
- Modify: `app/core/streaming.py`（文件顶部常量区之后新增函数）
- Test: `tests/test_streaming.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_streaming.py` 文件末尾追加：

```python
# ── 状态行:工具文案映射 ─────────────────────────────────────

def test_status_for_tool_classification():
    """_status_for_tool 按语义分类映射工具名到状态文案。"""
    from app.core.streaming import _status_for_tool
    assert _status_for_tool("web_search") == "正在搜索 ..."
    assert _status_for_tool("web_fetch") == "正在搜索 ..."
    assert _status_for_tool("generate_image") == "正在生成 ..."
    assert _status_for_tool("generate_video") == "正在生成 ..."
    assert _status_for_tool("synthesize_speech") == "正在生成 ..."
    assert _status_for_tool("generate_music") == "正在生成 ..."
    assert _status_for_tool("save_memory") == "正在调用工具 ..."
    assert _status_for_tool("search_memory") == "正在调用工具 ..."
    assert _status_for_tool("get_current_time") == "正在调用工具 ..."
    # 默认/未知工具
    assert _status_for_tool("unknown_tool") == "正在调用工具 ..."
    assert _status_for_tool("") == "正在调用工具 ..."
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `pytest tests/test_streaming.py::test_status_for_tool_classification -v`
Expected: FAIL with `ImportError: cannot import name '_status_for_tool'`

- [ ] **Step 3: 实现纯函数**

在 `app/core/streaming.py` 中，找到文件顶部常量区（`_PLACEHOLDER_ON` / `_PLACEHOLDER_OFF` 定义之后，`def clip` 之前），新增：

```python
# 状态行文案:按语义分类映射工具名(供 set_status 驱动)
_SEARCH_TOOLS = frozenset({"web_search", "web_fetch"})
_GENERATE_TOOLS = frozenset({
    "generate_image", "generate_video", "synthesize_speech", "generate_music",
})
_STATUS_THINKING = "正在思考 ..."
_STATUS_TOOL_DEFAULT = "正在调用工具 ..."


def _status_for_tool(name: str) -> str:
    """工具名 → 状态行文案(按语义分类)。未知工具归「正在调用工具」。"""
    if name in _SEARCH_TOOLS:
        return "正在搜索 ..."
    if name in _GENERATE_TOOLS:
        return "正在生成 ..."
    return _STATUS_TOOL_DEFAULT
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `pytest tests/test_streaming.py::test_status_for_tool_classification -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add app/core/streaming.py tests/test_streaming.py
git commit -m "feat: 新增 _status_for_tool 工具状态文案映射"
```

---

## Task 2: `set_status` 协议方法 + tick 循环暂存

为 `_TickLoopMixin` 增加 `_status` 字段和 `set_status` 方法。`set_status` 仅暂存，不直接发编辑。tick 循环改为：有内容时拼接状态行；idle 时跳过；占位阶段渲染纯状态行。

注意：本任务暂不删除旧的光标分支，先把暂存与渲染接通，下一任务删除光标。这样每个任务测试边界清晰。

**Files:**
- Modify: `app/core/streaming.py`（`StreamRenderer` 协议、`_TickLoopMixin`）
- Test: `tests/test_streaming.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_streaming.py` 末尾追加：

```python
# ── 状态行:set_status 驱动 ──────────────────────────────────

async def test_set_status_drives_status_line(limiter):
    """set_status 暂存状态,下次 tick 落地的编辑含该状态行。"""
    bot = GuestFakeBot()
    r = GuestRenderer(bot, chat_id=9, guest_query_id="gq-x",
                      limiter=limiter, throttle_ms=1)
    await r.start()
    await r.set_status("正在搜索 ...")
    await r.update("正文内容")
    await asyncio.sleep(0.03)  # 让 tick 落地
    # 编辑应同时含正文与状态行
    assert bot.text_edits, "应有编辑落地"
    last = bot.text_edits[-1][0]
    assert "正文内容" in last
    assert "正在搜索 ..." in last
    await r.finalize("正文内容完成")
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `pytest tests/test_streaming.py::test_set_status_drives_status_line -v`
Expected: FAIL with `AttributeError: 'GuestRenderer' object has no attribute 'set_status'`

- [ ] **Step 3: 协议层声明 `set_status`**

在 `app/core/streaming.py` 的 `StreamRenderer` Protocol 类中，找到 `async def fail(...)` 定义，在它之前插入：

```python
    async def set_status(self, status: str) -> None:
        """设置当前状态行文案(如「正在思考 ...」)。

        仅暂存,不直接发编辑;由轮询循环在下次 tick 合并到正文后缀渲染。
        """
        ...
```

- [ ] **Step 4: `_TickLoopMixin` 增加 `_status` 与 `set_status`**

在 `_TickLoopMixin` 类中：

(a) 在类属性声明区（`_cursor_on: bool` 那几行）新增：

```python
    _status: str
```

(b) 在 `_tick_init` 方法中，把 `self._cursor_on = True` 之后、`self._last_edit_time = 0.0` 之前，加入：

```python
        self._status = "正在思考 ..."
```

(c) 在 `_tick_init` 方法之后、`_ensure_interval_elapsed` 之前，新增方法：

```python
    async def set_status(self, status: str) -> None:
        """仅暂存状态文案;下次 tick 合并渲染(与 update 同为非阻塞暂存)。"""
        self._status = status
```

- [ ] **Step 5: tick 循环改为拼接状态行（保留光标分支,下一任务删除）**

将 `_tick_loop` 方法中**内容写入分支**（注释 `① 内容写入:不带光标;预设下次 idle 闪烁为亮` 那一段）：

```python
                if self._pending is not None and self._pending != self._committed:
                    # ① 内容写入:不带光标;预设下次 idle 闪烁为亮
                    text = self._pending
                    self._committed = self._pending
                    self._on_committed(self._pending)
                    self._pending = None
                    self._cursor_on = False  # 下次 idle toggle → True(亮)
                    suffix = ""
                    try:
                        await do_edit(text + suffix)
                    except Exception as e:
                        log.warning("轮询编辑失败(忽略)", 错误=str(e)[:120])
```

整体替换为（拼接 `\n\n<i>状态行</i>`，不再带光标）：

```python
                if self._pending is not None and self._pending != self._committed:
                    # ① 内容写入:正文 + 空行 + 状态行
                    text = self._pending
                    self._committed = self._pending
                    self._on_committed(self._pending)
                    self._pending = None
                    try:
                        await do_edit(text + "\n\n<i>" + self._status + "</i>")
                    except Exception as e:
                        log.warning("轮询编辑失败(忽略)", 错误=str(e)[:120])
```

- [ ] **Step 6: 运行新测试，确认通过**

Run: `pytest tests/test_streaming.py::test_set_status_drives_status_line -v`
Expected: PASS

- [ ] **Step 7: 提交**

```bash
git add app/core/streaming.py tests/test_streaming.py
git commit -m "feat: _TickLoopMixin 支持 set_status,内容编辑拼接状态行"
```

---

## Task 3: 删除光标闪烁,占位阶段用状态行,idle 不发编辑

移除 idle 闪烁分支（改为 `continue`）与占位 `▌↔nbsp` 交替分支（改为渲染纯状态行）；删除旧光标常量与 `_cursor_on` 字段。

**Files:**
- Modify: `app/core/streaming.py`（`_TickLoopMixin`、模块常量）
- Test: `tests/test_streaming.py`（改写/删除光标断言）

- [ ] **Step 1: 改写「idle 不闪烁」测试**

在 `tests/test_streaming.py` 中，找到 `test_tick_loop_cursor_blinks_when_idle`，整体替换为：

```python
async def test_idle_period_no_edits(limiter):
    """内容静默(idle)期间不再产生编辑 —— 缓解 429 的核心。"""
    bot = GuestFakeBot()
    r = GuestRenderer(bot, chat_id=9, guest_query_id="gq-x",
                      limiter=limiter, throttle_ms=1)
    await r.start()
    await r.update("固定文本")
    await asyncio.sleep(0.03)  # 让内容写入落地
    edits_after_content = len(bot.text_edits)
    assert edits_after_content >= 1
    # 等待多个 tick 过去(无新内容、无 set_status)
    await asyncio.sleep(0.08)
    # 编辑计数不应增长(idle 不闪烁)
    assert len(bot.text_edits) == edits_after_content, (
        "idle 期间不应产生编辑")
    await r.finalize("固定文本")
```

- [ ] **Step 2: 运行该测试,确认失败(旧逻辑仍会闪烁)**

Run: `pytest tests/test_streaming.py::test_idle_period_no_edits -v`
Expected: FAIL（旧 idle 分支会发编辑,计数增长）

- [ ] **Step 3: 改写「占位阶段显示状态行」测试**

找到 `test_placeholder_blinks_before_first_content`,整体替换为：

```python
async def test_placeholder_shows_status_line(limiter):
    """首条内容前,占位消息显示状态行文本(默认「正在思考 ...」),不再 ▌/nbsp 交替。"""
    bot = FakeBot()
    r = EditRenderer(bot, chat_id=42, limiter=limiter, throttle_ms=10,
                     typing_refresh_s=10)
    await r.start()  # 占位发送 "▌"(sendMessage,记入 bot.sent)
    # 占位阶段改状态,让 tick 落地状态行
    await r.set_status("正在思考 ...")
    await asyncio.sleep(0.04)
    await r.finalize("▌")  # 停止 tick;传占位字符避免影响断言
    # 文本编辑(排除 send 占位)应含状态行,不应出现纯 nbsp 闪烁
    edits_text = [e[2] for e in bot.edits]
    assert any("正在思考 ..." in t for t in edits_text), (
        f"占位阶段应显示状态行,实际编辑 {edits_text}")
    assert " " not in edits_text, "不应再有 nbsp 占位闪烁"
```

- [ ] **Step 4: 改写「内容编辑带状态行」测试**

找到 `test_content_edit_has_no_cursor_suffix`,整体替换为：

```python
async def test_content_edit_has_status_line_suffix(limiter):
    """内容写入编辑:正文 + 空行 + 状态行,无光标后缀。"""
    bot = GuestFakeBot()
    r = GuestRenderer(bot, chat_id=9, guest_query_id="gq-x",
                      limiter=limiter, throttle_ms=1)
    await r.start()
    await r.update("正文内容")
    await asyncio.sleep(0.02)
    assert bot.text_edits, "应有一次内容写入编辑"
    first = bot.text_edits[0][0]
    assert "正文内容" in first
    assert "▌" not in first, "不应含光标"
    assert "正在思考 ..." in first, "应含默认状态行"
    await r.finalize("正文内容完成")
```

- [ ] **Step 5: 删除「写后必亮光标」测试（光标概念已不存在）**

找到 `test_cursor_bright_after_content_write`,整段删除。

- [ ] **Step 6: 运行被改写的测试,确认失败**

Run: `pytest tests/test_streaming.py::test_idle_period_no_edits tests/test_streaming.py::test_placeholder_shows_status_line tests/test_streaming.py::test_content_edit_has_status_line_suffix -v`
Expected: 全部 FAIL（旧实现仍闪烁/无状态行）

- [ ] **Step 7: 改写 tick 循环的 idle 与占位分支**

在 `app/core/streaming.py` 的 `_tick_loop` 方法中，找到内容写入分支之后的两个分支（`② 已有内容静默:后缀式闪烁` 与 `③ 占位阶段`），当前为：

```python
                elif self._committed:
                    # ② 已有内容静默:后缀式闪烁
                    self._cursor_on = not self._cursor_on
                    text = self._committed
                    suffix = _CURSOR if self._cursor_on else ""
                    try:
                        await do_edit(text + suffix)
                    except Exception as e:
                        log.warning("轮询编辑失败(忽略)", 错误=str(e)[:120])
                else:
                    # ③ 占位阶段(首条内容前):整条光标独立闪烁
                    self._cursor_on = not self._cursor_on
                    text = _PLACEHOLDER_ON if self._cursor_on else _PLACEHOLDER_OFF
                    suffix = ""
                    try:
                        await do_edit(text + suffix)
                    except Exception as e:
                        log.warning("占位闪烁失败(忽略)", 错误=str(e)[:120])
```

整体替换为：

```python
                elif self._committed:
                    # ② 内容静默(idle):不再编辑 —— 这是缓解 429 的核心。
                    # 状态行的切换由 set_status 驱动,会落到「有新内容」或
                    # 「占位阶段」分支;idle 期无需任何编辑。
                    pass
                else:
                    # ③ 占位阶段(首条内容前):渲染纯状态行
                    try:
                        await do_edit("<i>" + self._status + "</i>")
                    except Exception as e:
                        log.warning("占位状态行编辑失败(忽略)", 错误=str(e)[:120])
```

- [ ] **Step 8: 删除旧光标常量与字段**

(a) 在 `app/core/streaming.py` 顶部，找到并删除这三行常量：

```python
_CURSOR = " ▌"  # 输入占位光标(已有内容时,作为后缀)
_PLACEHOLDER_ON = "▌"   # 占位阶段(首条内容前)光标亮态
_PLACEHOLDER_OFF = " "  # 占位阶段光标灭态(不间断空格,Telegram 拒绝纯空编辑)
```

(b) 在 `_TickLoopMixin` 类的类属性声明区,删除这一行：

```python
    _cursor_on: bool
```

(c) 在 `_tick_init` 方法中,删除这一行：

```python
        self._cursor_on = True
```

- [ ] **Step 9: 运行全部改写的测试,确认通过**

Run: `pytest tests/test_streaming.py::test_idle_period_no_edits tests/test_streaming.py::test_placeholder_shows_status_line tests/test_streaming.py::test_content_edit_has_status_line_suffix tests/test_streaming.py::test_set_status_drives_status_line -v`
Expected: 全部 PASS

- [ ] **Step 10: 提交**

```bash
git add app/core/streaming.py tests/test_streaming.py
git commit -m "feat: 删除光标闪烁,占位/内容均用状态行,idle 不发编辑"
```

---

## Task 4: 定稿清除状态行

`finalize` 落地纯正文（无状态行后缀）。当前 finalize 本就传纯 `full_text` 给 `_commit_final_edit`,但需确认 `_last_rendered` 不含状态行,且新测试覆盖。

**Files:**
- Modify: `app/core/streaming.py`（EditRenderer/GuestRenderer 的 `finalize` 已传纯文本,本任务主要是验证 + 加测试）
- Test: `tests/test_streaming.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_streaming.py` 末尾追加：

```python
async def test_finalize_strips_status_line(limiter):
    """finalize 落地纯正文,不含状态行后缀;last_rendered_text 为纯正文。"""
    bot = GuestFakeBot()
    r = GuestRenderer(bot, chat_id=9, guest_query_id="gq-x",
                      limiter=limiter, throttle_ms=1)
    await r.start()
    await r.set_status("正在搜索 ...")
    await r.update("流式正文片段")
    await asyncio.sleep(0.02)  # tick 落地含状态行的中间编辑
    # 末次编辑应含状态行(中间态)
    assert "正在搜索 ..." in bot.text_edits[-1][0]
    await r.finalize("最终完整正文")
    # 定稿编辑为纯正文,不含状态行
    assert bot.text_edits[-1][0] == "最终完整正文"
    assert "正在搜索" not in bot.text_edits[-1][0]
    assert r.last_rendered_text == "最终完整正文"
```

- [ ] **Step 2: 运行测试,确认通过**

Run: `pytest tests/test_streaming.py::test_finalize_strips_status_line -v`
Expected: PASS（finalize 已传纯文本,无需改实现；此为回归保护测试）

> 若 FAIL：检查 `finalize` 中传给 `_commit_final_edit` 的文本是否被意外拼接了状态行。当前实现传 `text`(纯正文),应通过。

- [ ] **Step 3: 提交**

```bash
git add tests/test_streaming.py
git commit -m "test: 定稿清除状态行的回归保护"
```

---

## Task 5: `DraftRenderer.set_status` 空实现

`DraftRenderer` 不参与状态行改造（私聊原生草稿预览）,但它是 `StreamRenderer` 协议的实现,Agent 会对所有 renderer 调 `set_status`。需提供空实现（no-op）以保持协议一致性。

**Files:**
- Modify: `app/core/streaming.py`（`DraftRenderer`）
- Test: `tests/test_streaming.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_streaming.py` 末尾追加：

```python
async def test_draft_set_status_is_noop(limiter):
    """私聊 DraftRenderer.set_status 是 no-op(不报错、不影响草稿)。"""
    bot = FakeBot()
    r = DraftRenderer(bot, chat_id=42, limiter=limiter, typing_refresh_s=10)
    await r.start()
    await r.set_status("正在思考 ...")  # 不应抛异常
    await r.update("草稿内容")
    await r.finalize("草稿内容完成")
    assert bot.sent[-1][1] == "草稿内容完成"
```

- [ ] **Step 2: 运行测试,确认失败**

Run: `pytest tests/test_streaming.py::test_draft_set_status_is_noop -v`
Expected: FAIL with `AttributeError: 'DraftRenderer' object has no attribute 'set_status'`

- [ ] **Step 3: 添加 no-op 实现**

在 `app/core/streaming.py` 的 `DraftRenderer` 类中,找到 `async def update(self, full_text: str)` 方法,在它之前插入：

```python
    async def set_status(self, status: str) -> None:
        """私聊不渲染状态行(原生草稿预览无后缀概念);接受调用但不做任何事。"""
        return
```

- [ ] **Step 4: 运行测试,确认通过**

Run: `pytest tests/test_streaming.py::test_draft_set_status_is_noop -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add app/core/streaming.py tests/test_streaming.py
git commit -m "feat: DraftRenderer.set_status no-op,保持协议一致"
```

---

## Task 6: Agent 驱动状态切换

在 `Agent.run` 的关键节点调用 `renderer.set_status`：进入流式循环前发「正在思考」；收到 tool_calls 执行首工具前发 `_status_for_tool(tc.name)`；工具回灌完进入续写前发「正在思考」。

**Files:**
- Modify: `app/core/agent.py`（`Agent.run`）
- Test: `tests/test_agent.py`（`FakeRenderer` 加 `set_status` + 新增用例）

- [ ] **Step 1: FakeRenderer 加 `set_status` 记录**

在 `tests/test_agent.py` 的 `FakeRenderer` 类中,找到 `async def update` 方法,在它之前插入：

```python
    async def set_status(self, status: str):
        self.statuses = getattr(self, "statuses", [])
        self.statuses.append(status)
```

- [ ] **Step 2: 写失败测试（工具阶段切状态）**

在 `tests/test_agent.py` 末尾追加：

```python
async def test_agent_sets_status_on_tool_phase():
    """Agent 在思考/工具/续写节点驱动状态行切换。"""
    chat = ScriptedChat([
        [  # 第一轮:发起搜索工具
            ChatStreamEvent(kind="tool_calls", finish_reason="tool_calls",
                            tool_calls=[ToolCallDelta(id="c1", name="web_search",
                                                      arguments='{"query":"x"}')]),
            ChatStreamEvent(kind="finish", finish_reason="tool_calls"),
        ],
        [  # 第二轮:基于结果回答
            ChatStreamEvent(kind="content", text="搜到结果"),
            ChatStreamEvent(kind="finish", finish_reason="stop"),
        ],
    ])
    agent = Agent(chat)
    r = FakeRenderer()
    await agent.run([{"role": "user", "content": "搜一下"}], r, ToolDispatcher())

    statuses = getattr(r, "statuses", [])
    # 进入第一轮流式前应发「正在思考」
    assert "正在思考 ..." in statuses
    # 执行 web_search 前应发「正在搜索」
    assert "正在搜索 ..." in statuses
    # 进入第二轮续写前应再发「正在思考」
    assert statuses.count("正在思考 ...") >= 2
```

- [ ] **Step 3: 运行测试,确认失败**

Run: `pytest tests/test_agent.py::test_agent_sets_status_on_tool_phase -v`
Expected: FAIL（`statuses` 为空或属性不存在）

- [ ] **Step 4: 在 agent.py 引入 `_status_for_tool` 与 `set_status` 调用**

(a) 在 `app/core/agent.py` 顶部 import 区,把现有：

```python
from app.core.streaming import StreamRenderer
```

改为：

```python
from app.core.streaming import StreamRenderer, _status_for_tool
```

(b) 在 `Agent.run` 方法中,找到 for 循环内、`try:` 之前的位置,当前为：

```python
        for round_no in range(1, MAX_TOOL_ROUNDS + 2):
            full_text = ""
            reasoning_text = ""
            pending_calls = []
            finish = ""

            try:
```

在 `finish = ""` 之后、`try:` 之前,插入状态切换（每轮开始 = 思考态）：

```python
            await renderer.set_status("正在思考 ...")

            try:
```

(c) 找到工具执行段,当前为：

```python
                result.tool_rounds += 1
                # assistant 消息(含 tool_calls)入会话 —— 供模型下一轮看到完整工具历史
                convo.append({
                    "role": "assistant",
                    "content": full_text or None,
                    "tool_calls": [
                        {
                            "id": tc.id or f"call_{i}",
                            "type": "function",
                            "function": {"name": tc.name, "arguments": tc.arguments or "{}"},
                        }
                        for i, tc in enumerate(pending_calls)
                    ],
                })
                # 逐个执行工具回灌
                for i, tc in enumerate(pending_calls):
```

在 `result.tool_rounds += 1` 之后、`convo.append` 之前,插入（执行首工具前按工具名设状态）：

```python
                result.tool_rounds += 1
                # 即将执行工具:按首个工具名设置状态行
                await renderer.set_status(_status_for_tool(pending_calls[0].name))
                # assistant 消息(含 tool_calls)入会话 —— 供模型下一轮看到完整工具历史
                convo.append({
```

- [ ] **Step 5: 运行新测试,确认通过**

Run: `pytest tests/test_agent.py::test_agent_sets_status_on_tool_phase -v`
Expected: PASS

- [ ] **Step 6: 运行 agent 全部测试,确认无回归**

Run: `pytest tests/test_agent.py -v`
Expected: 全部 PASS（`test_plain_answer`/`test_tool_call_roundtrip` 等行为不变）

- [ ] **Step 7: 提交**

```bash
git add app/core/agent.py tests/test_agent.py
git commit -m "feat: Agent 在思考/工具/续写节点驱动状态行切换"
```

---

## Task 7: 全量回归 + 文档更新

跑全部 streaming + agent 测试,并更新模块/方法文档字符串与 plan.md 中遗留的光标描述（仅文档）。

**Files:**
- Modify: `app/core/streaming.py`（模块 docstring、`_TickLoopMixin` docstring）
- Verify: 全部测试

- [ ] **Step 1: 运行 streaming + agent 全量测试**

Run: `pytest tests/test_streaming.py tests/test_agent.py -v`
Expected: 全部 PASS

- [ ] **Step 2: 运行项目全量测试**

Run: `pytest -q`
Expected: 全部 PASS（若有与本改动无关的既有失败,记录但不视为本任务回归）

- [ ] **Step 3: ruff 检查改动文件**

Run: `ruff check app/core/streaming.py app/core/agent.py tests/test_streaming.py tests/test_agent.py`
Expected: All checks passed（若报未用 import 如旧 `_CURSOR` 引用残留,清理）

- [ ] **Step 4: 更新模块 docstring**

在 `app/core/streaming.py` 顶部模块 docstring 中,找到描述「统一轮询门控」的段落（`★ 统一轮询门控(Edit/Guest):` 开头的那块),将其中关于「光标闪烁」的描述更新为状态行机制。找到这段：

```
★ 统一轮询门控(Edit/Guest):
Edit/Guest 渲染器用「单一后台轮询循环」按设定间隔驱动所有编辑 ——
内容更新与光标闪烁共用同一个间隔,互斥排队。update() 仅暂存最新文本,
真正发编辑的是循环。每次 tick 三选一:
  ① 有新内容 → 写入纯文本(不带光标);并预设下次 idle 闪烁为「亮」
  ② 内容静默 → 已有文本 + 后缀光标翻转闪烁("text ▌" / "text")
  ③ 占位阶段(首条内容前)→ 整条消息即光标,"▌" / "nbsp" 交替
间隔门控保证「同一消息任意两次 edit(无论内容/闪烁/定稿)距离 ≥ 设定值」:
tick 循环 wait_for 强制间隔;_do_edit 咽喉点在 HTTP 调用前记录时间戳,
finalize 紧随末次编辑时由 _ensure_interval_elapsed 补齐 sleep,杜绝 429。
```

替换为：

```
★ 统一轮询门控(Edit/Guest):
Edit/Guest 渲染器用「单一后台轮询循环」按设定间隔驱动所有编辑。update()
仅暂存最新文本,set_status() 仅暂存状态行文案,真正发编辑的是循环。
每次 tick 三选一:
  ① 有新内容 → 写入「正文 + 空行 + 状态行」(<i>状态</i> 后缀)
  ② 内容静默(idle)→ 不发任何编辑(状态切换靠 set_status 事件驱动)
  ③ 占位阶段(首条内容前)→ 渲染纯状态行(默认「正在思考 ...」)
idle 不再闪烁,从源头降低 edit 频率、缓解 429。间隔门控保证「同一消息
任意两次 edit 距离 ≥ 设定值」:tick 循环 wait_for 强制间隔;_do_edit 咽喉点
在 HTTP 调用前记录时间戳,finalize 紧随末次编辑时由 _ensure_interval_elapsed
补齐 sleep,杜绝 429。状态行由 Agent 事件驱动(set_status),定稿时清除,
落地纯正文。
```

- [ ] **Step 5: 更新 `_TickLoopMixin` docstring**

在 `_TickLoopMixin` 类的 docstring 中,找到：

```
    """统一轮询循环:内容更新与光标闪烁共用同一间隔门控。

    update() 仅暂存 _pending(非阻塞);真正发编辑的是后台 _tick_loop,
    每个 interval 做 1 次 tick,三选一(见 _tick_loop 文档):
      ① 有新内容 → 写纯文本(不带光标);预设下次 idle 闪烁为亮
      ② 内容静默 → 已有文本 + 后缀光标翻转闪烁
      ③ 占位阶段 → 整条光标 "▌"/nbsp 交替
    内容编辑期间不闪烁(不带光标);写完内容后,下一个 idle tick 必带亮光标。
    间隔门控(_do_edit 咽喉点时间戳 + _ensure_interval_elapsed)保证「同一消息
    任意两次 edit 距离 ≥ interval」,杜绝多源并发与 finalize 触发的 429。
    """
```

替换为：

```
    """统一轮询循环:内容更新与状态行共用同一间隔门控。

    update() 仅暂存 _pending、set_status() 仅暂存 _status(均非阻塞);真正
    发编辑的是后台 _tick_loop,每个 interval 做 1 次 tick,三选一(见
    _tick_loop 文档):
      ① 有新内容 → 写「正文 + 空行 + 状态行」
      ② 内容静默(idle)→ 不发编辑(状态切换靠 set_status 事件驱动)
      ③ 占位阶段 → 渲染纯状态行(默认「正在思考 ...」)
    idle 不再闪烁,降低 edit 频率、缓解 429。间隔门控(_do_edit 咽喉点时间戳 +
    _ensure_interval_elapsed)保证「同一消息任意两次 edit 距离 ≥ interval」,
    杜绝多源并发与 finalize 触发的 429。状态行定稿时清除,落地纯正文。
    """
```

- [ ] **Step 6: 更新 `EditRenderer` docstring 中的「光标闪烁」措辞**

在 `EditRenderer` 类 docstring 中,找到：

```
    typing:群聊 bot 是成员,支持 sendChatAction。start 后全程发 typing 直到
    finalize/fail。光标闪烁由轮询循环在静默期驱动(与内容更新共用间隔)。
```

替换为：

```
    typing:群聊 bot 是成员,支持 sendChatAction。start 后全程发 typing 直到
    finalize/fail。状态行(set_status)由轮询循环渲染,与内容更新共用间隔。
```

- [ ] **Step 7: 更新 `GuestRenderer` docstring**

在 `GuestRenderer` 类 docstring 中,找到：

```
    typing:Guest bot 非会话成员,**不支持** sendChatAction(仅 answerGuestQuery
    通道)。故用「光标闪烁」作为唯一「工作中」信号 —— 由统一轮询循环在静默期驱动
    (与内容更新共用间隔)。
```

替换为：

```
    typing:Guest bot 非会话成员,**不支持** sendChatAction(仅 answerGuestQuery
    通道)。故用「状态行」(set_status,默认「正在思考 ...」)作为唯一「工作中」
    信号 —— 由统一轮询循环渲染(与内容更新共用间隔)。
```

- [ ] **Step 8: 提交文档更新**

```bash
git add app/core/streaming.py
git commit -m "docs: 更新模块/类 docstring,光标闪烁→状态行"
```

---

## Self-Review

**Spec 覆盖：**

| Spec 要求 | 对应任务 |
|-----------|----------|
| `_status_for_tool` 纯函数 + 语义分类映射 | Task 1 |
| `set_status` 协议方法 + 暂存合并 | Task 2 |
| 移除光标闪烁、idle 不发编辑、占位用状态行 | Task 3 |
| 定稿清除状态行、`_last_rendered` 纯正文 | Task 4 |
| Agent 思考/工具/续写节点驱动 | Task 6 |
| DraftRenderer no-op 协议一致 | Task 5 |
| 删除 `_CURSOR`/`_PLACEHOLDER_*`/`_cursor_on` | Task 3 Step 8 |
| 测试改写（4 个光标断言用例）+ 新增（5 个）| Task 3 Step 1-5、Task 4、Task 5、Task 6 |
| docstring 更新 | Task 7 |

**Placeholder 扫描：** 无 TBD/TODO；每个代码 step 含完整可粘贴代码与精确替换锚点。

**类型/签名一致性：** `set_status(self, status: str) -> None` 在 Protocol、`_TickLoopMixin`、`DraftRenderer`、`FakeRenderer` 一致；`_status_for_tool(name: str) -> str` 在 streaming 定义、agent import 一致；`_status` 字段在 `_tick_init` 初始化、tick 循环与 `set_status` 引用一致。
