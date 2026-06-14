# 用底部状态行替代光标闪烁

## 背景

群聊（`EditRenderer`）与 Guest（`GuestRenderer`）当前用「光标闪烁」作为流式渲染的"工作中"信号：

- 有内容时，正文末尾后缀 ` ▌` 翻转闪烁（`text ▌` / `text`）
- 占位阶段（首条内容前），整条消息 `▌` ↔ `nbsp` 交替闪烁
- 即使内容**完全静默**（idle），每个 interval tick 仍会发出一次带/不带光标的编辑来"闪烁"

这导致 idle 期间持续产生 `editMessageText` 调用，是高频 429（限流）的主要来源。

## 目标

1. 去掉光标闪烁，改为在消息最下方增加一个空行 + 状态行（如「正在思考 ...」「正在搜索 ...」），由 Agent 按阶段驱动切换。
2. idle 期间**不再产生任何编辑**，从根本上降低 edit 频率、缓解 429。
3. 不破坏现有的防弹化定稿、interval 门控、终稿对账等保证。

## 非目标

- 不改 `DraftRenderer`（私聊，走 `sendMessageDraft` 原生草稿预览，无闪烁）。
- 不改 typing（`sendChatAction`）行为：群聊全程刷新、Guest 不支持。
- 不改工具调度逻辑、不改 MiniMax 流式协议。

## 改造范围

仅 `EditRenderer` 与 `GuestRenderer`，二者都继承 `_TickLoopMixin`，改造点完全一致。涉及文件：

- `app/core/streaming.py`（核心）
- `app/core/agent.py`（驱动状态切换）
- `tests/test_streaming.py`（改写闪烁断言 + 新增用例）
- `tests/test_agent.py`（`FakeRenderer` 加 `set_status` + 新增用例）

## 核心机制

### 1. 状态行事件驱动

`StreamRenderer` 协议新增一个方法：

```python
async def set_status(self, status: str) -> None: ...
```

`Agent.run` 在关键节点调用：

| 时机 | 调用 |
|------|------|
| 进入流式循环（每轮 `stream_chat`）前 | `set_status("正在思考 ...")` |
| 收到 `tool_calls`、执行首个工具前 | `set_status(_status_for_tool(tc.name))` |
| 全部工具回灌完、进入下一轮续写前 | `set_status("正在思考 ...")` |

`_status_for_tool(name)` 是纯函数，按语义分类映射（见下）。

### 2. tick 循环行为变更

`_TickLoopMixin._tick_loop` 当前每个 tick 三选一（内容写入 / 后缀光标闪烁 / 占位光标闪烁）。改为：

| 分支 | 旧行为 | 新行为 |
|------|--------|--------|
| 有新内容（`_pending != _committed`） | 写纯文本，预设下次 idle 闪烁为亮 | 写「正文 + `\n\n` + 状态行」 |
| 内容静默（idle） | 翻转后缀光标闪烁（**发编辑**） | **跳过，不发任何编辑** |
| 占位阶段（首条内容前） | `▌` ↔ `nbsp` 交替闪烁 | 显示纯状态行（如 `<i>正在思考 ...</i>`），状态变化时才发编辑 |

关键收益：**idle tick 直接 continue，不产生 edit**。这是缓解 429 的核心。

### 3. 状态行渲染

- 有正文时：`<正文>\n\n<i>{status}</i>`
- 占位阶段（无正文）：`<i>{status}</i>`

状态行用 `<i>` 斜体，`sanitize_telegram_html` 已支持该标签。

不再需要 `_PLACEHOLDER_OFF`（不间断空格占位）。`_CURSOR`、`_PLACEHOLDER_ON` 常量随之移除（或保留供旧测试引用——按下方测试计划，相关测试会改写或删除，故移除）。

### 4. 状态文案映射

纯函数 `_status_for_tool(name: str) -> str`：

| 工具 | 文案 |
|------|------|
| `web_search`, `web_fetch` | `正在搜索 ...` |
| `generate_image`, `generate_video`, `synthesize_speech`, `generate_music` | `正在生成 ...` |
| `save_memory`, `search_memory`, `get_current_time` | `正在调用工具 ...` |
| 其它/默认 | `正在调用工具 ...` |

### 5. set_status 的暂存与合并

`set_status(status)` 仅更新内部 `_status` 字段，**不直接发编辑**（与 `update()` 仅暂存 `_pending` 同理）。真正的编辑由 tick 循环在下个 tick 合并发送：

- 若 `_committed` 非空 → 编辑「正文 + 状态行」
- 若占位阶段 → 编辑纯状态行

这样状态变更频繁时（理论上 Agent 不会高频切状态），由 tick 的 interval 合并，杜绝超速。

### 6. 定稿清除状态行

`finalize(full_text)` 落地**纯正文**（去掉 `\n\n<i>...</i>` 后缀）。`_last_rendered` 记录纯正文，与 `agent.py` 终稿对账逻辑（`last_rendered_text != display.strip()`）一致。

占位阶段 `set_status` 设置的状态行不会污染最终文本。

## 保留不变

- `_ensure_interval_elapsed`、`_do_edit`、咽喉点时间戳 `_last_edit_time`——状态切换的编辑同样受 interval 门控。
- `_commit_final_edit` 防弹化定稿（HTML 失败降级纯文本 + 退避重试 + 不抛异常）。
- typing 循环：群聊全程刷新、Guest 不支持。
- `Agent.run` 的覆盖式渲染、终稿对账、工具轮次上限。

## 测试计划

### 改写（断言旧闪烁行为）

- `test_tick_loop_cursor_blinks_when_idle` → 改为 `test_idle_period_no_edits`：idle 期间编辑计数不增长。
- `test_placeholder_blinks_before_first_content` → 改为 `test_placeholder_shows_status_line`：占位阶段编辑内容为状态行文本（如 `正在思考 ...`），不再 `▌`/`nbsp` 交替。
- `test_content_edit_has_no_cursor_suffix` → 改为 `test_content_edit_has_status_line_suffix`：内容编辑带状态行后缀，无 ` ▌`。
- `test_cursor_bright_after_content_write` → 删除（光标概念已不存在）。

### 新增

- `test_status_for_tool_classification`：纯函数映射正确（搜索/生成/调用工具三类 + 默认）。
- `test_set_status_drives_status_line`：`set_status("正在搜索 ...")` 后，tick 落地的编辑含该状态行。
- `test_finalize_strips_status_line`：定稿文本为纯正文，不含状态行；`last_rendered_text` 为纯正文。
- `test_idle_period_no_edits`（见上）：核心 429 缓解验证。
- `test_agent_calls_set_status_on_tool_phase`（agent）：`FakeRenderer` 记录 `set_status` 调用，断言 Agent 在工具阶段调用了对应状态。

### 不变

`FakeRenderer`（agent 测试）增加同步 `set_status` 实现（记录调用）后，其余 agent 测试（`test_plain_answer`、`test_tool_call_roundtrip` 等）行为不变。

## 风险与对策

| 风险 | 对策 |
|------|------|
| 状态行后缀使正文超长被 `clip` 截断 | `clip` 作用于拼接前的正文；状态行是固定短串，拼接后若超 4096 由 `_render_for_telegram` 现有截断逻辑兜底（先截 raw 再 escape） |
| Guest 占位阶段无 `nbsp`，首次编辑可能因"内容未变"被跳过 | 占位阶段首次 `set_status`（默认"正在思考 ..."）即产生一次编辑，无空编辑问题 |
| `set_status` 在 `update` 之后调用，状态需立即刷新 | tick 下个 tick 即合并；Agent 切状态的时机天然间隔 ≥ 一次工具往返，远大于 interval |
