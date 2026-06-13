# 设计:模型直出 Telegram HTML + Sanitizer 校验

**日期**:2026-06-14
**状态**:已批准

## 背景与动机

当前 Bot 的回复管线:模型输出 Markdown → `format_for_telegram`(`app/core/streaming.py:60`)用自研正则转成 HTML → `parse_mode="HTML"` 发往 Telegram。

这套正则转换器存在硬限制:

- 仅产出 5 种标签:`<b>` `<i>` `<code>` `<pre><code>` `<a href>`
- **不支持** blockquote、spoiler、strikethrough、underline、嵌套格式
- 系统提示词明令"不要使用 HTML 标签"(`context.py:65`),主动放弃能力

用户需求:

1. 支持 Telegram 折叠引用块 `<blockquote expandable>`(需 HTML,Markdown 无法表达)
2. 让模型直接输出 HTML 以解锁更多格式
3. 在提示词中详细约束 Telegram 支持的 HTML 标签

## 目标

- 删除自研 Markdown→HTML 正则转换层
- 让模型直接输出 Telegram HTML,提示词列出全量可用标签
- 新增 sanitizer:校验白名单、转义游离字符、自动补全未闭合标签、兜底降级
- 流式输出全程安全(未闭合标签不致 edit 失败塌陷)
- 覆盖所有输出路径(主聊天、show_thinking、媒体 caption、广播、workers 通知)

## Telegram 支持的 HTML 标签(官方文档核对)

完整清单:

```
<b>/<strong>  <i>/<em>  <u>/<ins>  <s>/<strike>/<del>
<span class="tg-spoiler">/<tg-spoiler>
<a href="...">  <code>  <pre>  <pre><code class="language-x">
<blockquote>  <blockquote expandable>
<tg-emoji emoji-id="...">  <tg-time unix="...">
```

约束(官方):

- 只支持上述标签
- 游离的 `<` `>` `&` 必须转义为 `&lt; &gt; &amp;`
- 只支持命名实体 `&lt; &gt; &amp; &quot;`(数值实体全支持)
- `pre`/`code` 嵌套以指定语言

**本方案白名单决策**:`tg-emoji`、`tg-time` 不放入白名单——模型无法可靠填入 `emoji-id`(Fragment ID)或 unix 时间戳,放行只会引入脏数据。其余全部放行。

## 方案

### 1. 新建 `app/core/htmlfmt.py`

独立模块,单一职责,~120 行。导出:

```python
def sanitize_telegram_html(text: str) -> str
```

**算法**(基于标准库 `html.parser.HTMLParser`,`convert_charrefs=True`,无新依赖):

1. **白名单标签**(通过):`b strong i em u ins s strike del`、`span`(仅 `class="tg-spoiler"`)、`tg-spoiler`、`code`、`pre`、`a`(仅 `href`)、`blockquote`(可带 `expandable`)。其余标签剥离 markup、保留内部文本。
2. **属性白名单**:
   - `a.href`:仅 `http`/`https`/`tg` scheme(拒绝 `javascript:` 等)
   - `span.class`:必须等于 `tg-spoiler`
   - `code.class`:仅 `language-*`
   - `blockquote.expandable`:布尔属性
   - 其余属性一律丢弃
3. **data 单层转义**:`handle_data` 中 `html.escape()` 一次。`convert_charrefs=True` 保证实体先解码再转义,代码块内部同样处理。
4. **自动补全**:维护标签栈,解析结束时栈中残余未闭合标签按逆序追加闭合标签。← **流式安全根**:让 mid-stream 的 `<blockquote>...`(未闭合)也能合法发送。
5. **嵌套校验**(轻量):`pre`/`code` 内禁止块级标签(`blockquote`);`a` 内禁止再嵌 `a`。
6. **兜底**:解析抛错或产物为空 → 调用方回退 `html.escape(text)`。

### 2. 改造 `app/core/streaming.py`

- **删除** `format_for_telegram`(`:60-92`)
- `_render_for_telegram`(`:95-102`)内部:`sanitize_telegram_html(raw)` 替换原 `format_for_telegram(raw)`;超限裁剪后重新 sanitize;最终回退 `html.escape(clip(text))`
- 容错路径原样保留:
  - `_raw_edit`(`:478-483`):流式 edit HTML 解析失败静默跳过
  - `_commit_final_edit`(`:191-210`):定稿失败降级 `html.escape` 纯文本
- `TG_PARSE_MODE = "HTML"`(`:50`)不变

### 3. 重写系统提示词(`app/core/context.py:50-66`)

"输出格式规则"段改为:

```
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
```

第 40-48 行的长度规则不变(仍要求简短回复)。

### 4. show_thinking 改用折叠引用块(`app/core/agent.py`)

`:67-69` 与 `:134-136`,把:

```python
quoted = "\n".join(f"> {ln}" for ln in reasoning_text.splitlines())
display = f"{quoted}\n\n{full_text}"
```

改为:

```python
display = f"<blockquote expandable>\n{reasoning_text}\n</blockquote>\n\n{full_text}"
```

思考过程较长,`expandable` 默认折叠,主体答案更清爽。这段是我们拼的合法 HTML,sanitize 直通。

### 5. 全部输出路径改造

所有发文本/caption 的地方统一走 `sanitize_telegram_html` + `parse_mode=HTML`:

| 文件 | 位置 | 改动 |
|---|---|---|
| `delivery.py` | `:66,76,90,94,104,112,154` | 包 sanitize + 显式 `parse_mode=HTML` |
| `workers.py` | `:159,239,246,270,293,316` | 同上 |
| `commands.py` | `:353` broadcast | 同上 |

这些路径目前是纯文本;改为 sanitize+HTML 后纯散文照常转义显示(无回归)。

## 测试计划

| 文件 | 动作 |
|---|---|
| `tests/test_htmlfmt.py` | **新建**:白名单直通、剥离未知标签、转义游离字符、自动补全未闭合标签(模拟流式中断)、代码块单层转义、`<blockquote expandable>` 直通、危险 scheme 拒绝、垃圾输入兜底 |
| `tests/test_streaming.py` | `:124` 等:输入从 Markdown 改为 HTML,断言 sanitizer 直通 |
| `tests/test_context.py` | `:42-50`:修复脱节(当前必然失败),改为断言新提示词关键句 |
| `tests/test_agent.py` | 新增 show_thinking 产出 `<blockquote expandable>` 断言 |

## 风险与缓解

| 风险 | 缓解 |
|---|---|
| 模型偶尔输出 Markdown(`**`) | 提示词强约束;sanitize 直通(可见 `**`,信号清晰但不崩) |
| 代码块内裸 `<` 致解析歧义 | 提示词要求转义;歧义时剥离→`_raw_edit` 跳过→定稿降级 |
| 流式期间未闭合标签 | sanitize 自动补全 |
| 历史消息(旧 Markdown)入上下文 | 仅作上下文文本,不渲染,影响可忽略 |
| caption/广播路径接入用户输入 | sanitize 转义保护,不会发消息失败 |

## 执行顺序

1. 建 `htmlfmt.py` + `test_htmlfmt.py`(TDD)
2. 改 `streaming.py` 接入 + 更新其测试
3. 重写 `context.py` 提示词 + 修 `test_context.py`
4. 改 `agent.py` show_thinking + 测试
5. 扩散到 delivery/workers/commands
6. `ruff check` + `pytest` 全量验证
