# Telegram 助理机器人 · 实施计划 (plan.md)

> 一个支持 **Guest 模式** 的助理型 Telegram Bot，接入 MiniMax **M3** 多模态大模型，集成 MiniMax 的 **语音合成 / 图片生成 / 视频生成 / 音乐生成**，并具备 **联网搜索 (WebSearch) / 网页抓取 (WebFetch)**、**实时时间**、**持久记忆**、**上下文自动压缩**、**鉴权授权**、**配额控制** 与 **多用户并发处理**。
> 私聊用 `sendMessageDraft` 原生流式，群聊与 Guest 模式用 `editMessageText` 模拟流式。

---

## 1. 目标与范围

| 能力 | 说明 | 关键模型 / 方法 / 服务 |
|---|---|---|
| 对话助理 | 多模态对话，支持工具调用 (Agent) | `MiniMax-M3` (1M 上下文) |
| 输入消息类型 | 文本 / 图片 / 视频 / 文件 | M3 多模态 `image_url` / `video_url` + Files API |
| 语音合成 (TTS) | 文本转语音，回传语音消息 | `speech-2.8-hd` / `speech-2.8-turbo` |
| 图片生成 | 文生图 | `image-01` / `image-01-live` |
| 视频生成 | 文生视频（异步任务） | `MiniMax-Hailuo-2.3` |
| 音乐生成 | 描述 + 歌词生成歌曲 | `music-2.6` |
| 联网搜索 (WebSearch) | 三家回退链 + 重试 | Firecrawl → Brave → DuckDuckGo |
| 网页抓取 (WebFetch) | 抓取 URL 正文为 markdown | Firecrawl scrape → 兜底 |
| 实时时间 | 获取当前时间，时区 `Asia/Shanghai` | `zoneinfo` 本地工具 |
| 流式输出 | 私聊原生草稿流；群聊/Guest 编辑流 | `sendMessageDraft` / `editMessageText` |
| 持久记忆 | 跨会话长期记忆 + 检索 | SQLite + FTS5 |
| 上下文压缩 | 滚动摘要，控制 token 预算 | `MiniMax-M2.7-highspeed` |
| 鉴权 | 授权制：授权用户全功能全场景；未授权直接 Permission Denied | 自建中间件 |
| 配额 | 按「调用次数」或「Token」限额 + 管理命令 | QuotaManager |
| 超管授权 | 授权/撤权/晋升命令 | 超管命令集 + 审计日志 |
| **并发处理** | **多用户互不阻塞；同一用户多请求并行** | **asyncio 任务 + 后台 worker + 信号量背压** |

**非目标（v1 不做）**：语音克隆 / 音色设计、视频 Agent 模板、群组消息历史抓取（Guest 模式本就无权限）、多租户计费。

---

## 2. 技术栈

- **语言/运行时**：Python 3.11（本机已装 3.11.14）
- **Bot 框架**：`aiogram` 3.x（原生 `async`；`SimpleRequestHandler(handle_in_background=True)` 提供更新级并发；最新 10.x 方法如 `answerGuestQuery` 用 `bot(Raw(...))` 兜底）
- **HTTP 服务**：`aiohttp`（Webhook 接收 + MiniMax 回调，复用同一端口）
- **HTTP 客户端**：`httpx`（异步**单例**，调用 MiniMax / Firecrawl / Brave）
- **DuckDuckGo**：`ddgs` 库（无需 Key；同步 → `asyncio.to_thread` 包裹）
- **数据库**：**SQLite**（`aiosqlite`，**WAL 模式**），含 **FTS5** 全文索引
- **数据访问**：`SQLAlchemy 2.x`（async）或轻量手写 DAO
- **后台任务**：`asyncio.create_task` + 任务注册表（可选 `aiojobs.Scheduler` 做监督/限并发）
- **配置**：`pydantic-settings` + `.env`
- **时间**：标准库 `zoneinfo`（`Asia/Shanghai`），Windows 配 `tzdata`
- **日志/重试**：`structlog` / `tenacity`
- **部署**：systemd / Docker + 反向代理（Caddy/Nginx 提供 TLS）
- **数据库备选**：高并发写入若 SQLite 成瓶颈，可平滑切 **PostgreSQL**（`asyncpg`）。

---

## 3. 整体架构

```
                         ┌──────────────────────────────────────────────┐
   Telegram  ───webhook──▶│  aiohttp Server (single port, TLS via proxy) │
                         │   ├─ POST /tg/<secret>  ← 立即ACK, 后台处理     │
   MiniMax   ──callback──▶│   └─ POST /mmx/callback ← 视频/音乐回调         │
                         └───────────────┬──────────────────────────────┘
                                         │ feed_update() → 每个Update = 独立asyncio任务
                                ┌────────▼─────────┐
                                │  aiogram Dispatcher│
                                │  + Middlewares:    │
                                │   1) Auth (授权门控) │
                                │   2) Quota (配额)   │
                                │   3) Concurrency   │  ← 信号量/背压
                                │   4) ChatContext   │
                                └────────┬───────────┘
            ┌──────────────┬────────────┼─────────────┬───────────────┐
            ▼              ▼            ▼             ▼               ▼
      private.py       group.py     guest.py     commands.py      media.py
   (Draft 流式)     (Edit 流式)  (answerGuestQuery (授权/配额/超管) (入站图/视/文件)
                                  + Edit 流式)
            └──────────────┴────────────┬─────────────┴───────────────┘
                                        ▼
                              ┌───────────────────┐        ┌──────────────────┐
                              │   Agent Core       │        │  GenWorkerPool    │
                              │  ContextBuilder    │        │ (后台轮询/回调回填) │
                              │  + M3 + ToolCall    │◀──────▶│  视频/音乐异步任务  │
                              └─────────┬──────────┘        └──────────────────┘
                          tool dispatch │
   ┌──────────┬──────────┬──────────┬──┴───────┬──────────┬──────────┬─────────┐
   ▼          ▼          ▼          ▼          ▼          ▼          ▼         ▼
 chat.py    tts.py    image.py   video.py   music.py   search/    time.py  files.py
   └──── MiniMax HTTP Client (httpx 单例) ─────┘          │
                                                          ▼
                                          Firecrawl → Brave → DuckDuckGo
                                          (回退 + 单次重试)
```

**核心数据流（一次请求）**：
0. Webhook 收到 Update → **立即 ACK Telegram**（`handle_in_background=True`），该 Update 作为**独立 asyncio 任务**处理；不同用户、同一用户的多条消息互不阻塞。
1. 中间件链：**鉴权**（未授权直接拒绝）→ **配额**（超额拒绝）→ **并发/背压**（信号量取槽，超限排队）→ 装载会话上下文。
2. `ContextBuilder` 组装：`system`（含**当前 `Asia/Shanghai` 时间**）+ 持久记忆 + 滚动摘要 + 近 N 条原始消息 + 本轮（含多模态块）。
3. 调用 M3（`stream=true`，`tools=[...]`，含生成类 + `web_search` / `web_fetch` / `get_current_time`）。
4. 流式渲染器按聊天类型分流（私聊 Draft / 群聊·Guest Edit）。
5. 工具调用 → 执行：搜索/抓取/时间**同步取结果回灌**；生成类（视频/音乐）**交 GenWorkerPool 后台处理并立即返回占位**，handler 不阻塞。
6. 落库：写 `messages`、累加**配额用量**、触发压缩与记忆抽取（均 `create_task` 异步执行）。

---

## 4. 并发模型（Concurrency）★

> 设计目标：**多用户同时使用互不阻塞**；**同一用户的多个请求也并行**——例如视频在后台生成时，用户的新提问立即得到响应，而非排队等视频完成。
> 整体形态：单事件循环 + asyncio 任务并发。绝大多数工作是 **IO 密集**（调 MiniMax / Telegram / 搜索的 HTTP），单循环即可支撑大量并发连接；少数**阻塞 / CPU** 操作下放线程池。

### 4.1 五个层次

**L1 · 更新级并发（不同用户互不阻塞）**
- Webhook：`SimpleRequestHandler(dp, bot, handle_in_background=True)`（默认 True）——收到 Update 立即 ACK Telegram，每个 Update 作为独立任务进入 `Dispatcher.feed_update()`。一个慢请求只占用它自己的任务，不卡事件循环。
- Polling（退化模式）：`start_polling(handle_as_tasks=True, tasks_concurrency_limit=N)`。

**L2 · 长任务不阻塞（同一用户并行）**
- 铁律：**handler 绝不 `await` 一个生成任务直到完成**。
- 视频/音乐：handler 只做"快"动作——POST 建任务、落 `generations`、发占位消息，然后**立即返回**。完成由两条路送达：① MiniMax 回调 `/mmx/callback`；② 兜底的**后台轮询任务**（`GenWorkerPool` 内 `create_task`）。于是视频在后台生成期间，同一用户的下一条消息作为**另一个 Update 任务**被并发处理、即时响应。
- 流式对话：每个请求自带独立 `draft_id` / 占位消息、独立任务。同一用户可同时拥有「一个正在流式的回答 + 一个正在生成的视频 + 第三个新问题」三个互不干扰的任务。

**L3 · 有界资源（背压 / backpressure）**
- 无限并发会耗尽内存、触发上游限流。用信号量设上限，超限**排队等待**（非拒绝），并在占位消息提示"排队中"：
  - `MAX_CONCURRENT_CHATS`：全局并发 M3 对话数。
  - `MAX_CONCURRENT_GENERATIONS`：全局并发生成数（视频尤贵）。
  - `PER_USER_CONCURRENCY`：单用户并发重任务上限（默认 3），防止一个用户占满全部槽位、饿死他人。
  - `TG_GLOBAL_SEND_RATE`：全局 Telegram 发送限速（≈30 msg/s 全局，群内 ≈1 msg/s/chat）→ 统一发送队列限流器。
- 配额（§14.2）管"长周期总量"，信号量管"瞬时并发"，二者互补。

**L4 · 共享资源并发安全**
- `httpx.AsyncClient`：**单例共享**，并发安全且复用连接池；切勿每请求新建。
- **SQLite 开 WAL**（`PRAGMA journal_mode=WAL`）：多读 + 单写并发；事务短小；读-改-写（配额结算）用 `BEGIN IMMEDIATE` 取写锁防竞态；设 `busy_timeout`。高写入压力下可切 PostgreSQL（§2）。
- 阻塞/CPU 操作下放线程：`ddgs`（同步）、大音频 hex 解码、图片/base64 处理 → `asyncio.to_thread(...)`。
- 无未加锁的全局可变状态；一切按 user/chat 维度键控。

**L5 · 生命周期与持久性**
- 后台任务注册表（或 `aiojobs.Scheduler`）跟踪所有 `create_task` 出的轮询任务；优雅关停时统一取消/等待。
- **重启恢复**：启动时扫描 `generations` 中 `status in (queued, processing)` 的任务，重新挂载轮询——重启不丢"生成中"的视频。
- **回调幂等**：按 `task_id` 去重，回填前查 `generations.status`（防回调重复、以及回调与轮询重复送达）。

### 4.2 并发 vs 顺序的取舍
- 同一聊天的两条消息会并行处理，回复可能**不严格按发送顺序**到达——这是为满足"不阻塞"的明确取舍。
- 防流式串扰：每个回复绑定**自己的 `draft_id` / 占位消息**，互不覆盖；编辑节流按消息独立计算。
- 若个别命令需严格串行（如同一用户的 `/reset` 与正在写库），用**按 `user_id` 的轻量异步锁**仅串行该类关键段，不影响其余并发。

### 4.3 并发相关配置
```
MAX_CONCURRENT_CHATS=32
MAX_CONCURRENT_GENERATIONS=8
PER_USER_CONCURRENCY=3
TG_GLOBAL_SEND_RATE=28          # msg/s, 留余量
WORKER_POLL_INTERVAL_S=5        # 视频轮询起始间隔（指数退避）
HTTPX_MAX_CONNECTIONS=100
```

---

## 5. 目录结构

```
Telegram-Assistant-Bot/
├── plan.md
├── README.md
├── pyproject.toml
├── .env.example
├── docker-compose.yml          # 可选：bot + caddy
├── data/
│   ├── bot.db                  # SQLite 主库（WAL）
│   └── cache/                  # 入站/出站媒体临时缓存
├── app/
│   ├── main.py                 # 入口：建 aiohttp app、注册 webhook、恢复未决任务、启动
│   ├── config.py               # pydantic-settings
│   ├── server.py               # 路由：/tg/<secret>, /mmx/callback, /healthz
│   ├── handlers/
│   │   ├── private.py          # 私聊：Draft 流式
│   │   ├── group.py            # 群聊（成员）：Edit 流式 + @提及触发
│   │   ├── guest.py            # Guest 模式：answerGuestQuery + Edit 流式
│   │   ├── media.py            # 入站 photo/video/document 解析
│   │   ├── commands.py         # /start /help /reset + 授权/配额/超管命令
│   │   └── errors.py           # 全局异常处理
│   ├── core/
│   │   ├── agent.py            # Agent 主循环：M3 + tool-calling 调度
│   │   ├── streaming.py        # 统一流式抽象（Draft / Edit / Guest）
│   │   ├── context.py          # ContextBuilder：预算、摘要、系统时间注入
│   │   ├── compaction.py       # 滚动摘要压缩（后台任务）
│   │   ├── memory.py           # 持久记忆写入 / FTS5 检索 / 抽取
│   │   ├── tools.py            # 工具 schema 定义 + 分发表
│   │   ├── auth.py             # 授权门控（authorized? 全场景一致）
│   │   ├── quota.py            # QuotaManager：calls / tokens 计量与门控
│   │   ├── concurrency.py      # ★ 信号量、按用户锁、Telegram 发送限流器、任务注册表
│   │   ├── workers.py          # ★ GenWorkerPool：生成任务后台轮询/回填、重启恢复
│   │   └── ratelimit.py        # 令牌桶 + Telegram 编辑节流
│   ├── minimax/
│   │   ├── client.py           # httpx 单例、重试、错误码映射
│   │   ├── chat.py             # /v1/chat/completions（M3 多模态 + 流式）
│   │   ├── tts.py              # /v1/t2a_v2
│   │   ├── image.py            # /v1/image_generation
│   │   ├── video.py            # /v1/video_generation + 查询/回调
│   │   ├── music.py            # /v1/music_generation
│   │   └── files.py            # 文件上传/检索（mm_file://）
│   ├── search/                 # 联网搜索 / 抓取（三家回退）
│   │   ├── router.py           # 回退链 + 单次重试编排
│   │   ├── base.py             # Provider 抽象：search() / fetch()
│   │   ├── firecrawl.py        # Firecrawl /v2/search + /v2/scrape
│   │   ├── brave.py            # Brave /res/v1/web/search
│   │   └── duckduckgo.py       # ddgs（text + 抓取兜底）
│   ├── db/
│   │   ├── engine.py           # async engine / WAL / 连接池
│   │   ├── models.py
│   │   ├── dao.py
│   │   └── schema.sql          # 建表 + FTS5
│   └── utils/
│       ├── tg_files.py         # Telegram getFile 下载 → bytes/base64
│       ├── hexaudio.py         # hex/base64 音频解码（线程）
│       ├── clock.py            # now(Asia/Shanghai) 格式化
│       └── tokens.py           # token 估算
└── tests/
    ├── test_concurrency.py
    ├── test_streaming.py
    ├── test_context.py
    ├── test_auth_quota.py
    ├── test_search_router.py
    └── test_minimax_client.py
```

---

## 6. MiniMax 接口对照表（实现依据）

> Base URL：`https://api.minimaxi.com/v1`（备用：`https://api-bj.minimaxi.com`）
> 通用请求头：`Authorization: Bearer <MINIMAX_API_KEY>`、`Content-Type: application/json`
> `/v1` OpenAI 兼容端点**无需** `GroupId`。

### 6.1 对话 / 多模态 — `MiniMax-M3`
- `POST /v1/chat/completions`
- 关键参数：`model="MiniMax-M3"`、`messages`、`stream=true`、`stream_options={"include_usage":true}`、`tools`、`reasoning_split=true`、`thinking={"type":"adaptive"|"disabled"}`、`max_completion_tokens`。
- **多模态 content 块**：`{"type":"text"}` / `{"type":"image_url","image_url":{"url":...}}` / `{"type":"video_url","video_url":{"url":...,"fps":1}}`。
- 限制：图片 ≤10MB；视频 URL/base64 ≤50MB，更大走 Files API 用 `mm_file://{file_id}`（≤512MB）；单请求体 ≤64MB。
- 流式：`delta.content`（正文）、`delta.reasoning_details[].text`（思考）。
- **usage**：`stream_options.include_usage` 末块返回 `total_tokens` → 用于 **Token 配额计量**。

### 6.2 语音合成 (TTS) — `speech-2.8-hd` / `speech-2.8-turbo`
- `POST /v1/t2a_v2`；body：`model`、`text`(≤10000)、`voice_setting{voice_id,speed,vol,pitch,emotion}`、`audio_setting{sample_rate,bitrate,format,channel}`、`output_format`(`hex`默认/`url`)。
- 返回：`data.audio`（hex，需解码）或 `url`；`extra_info.audio_length`(ms)。默认用 `url` 省解码。

### 6.3 图片生成 — `image-01` / `image-01-live`
- `POST /v1/image_generation`；body：`model`、`prompt`(≤1500)、`aspect_ratio`、`n`(1–9)、`prompt_optimizer`、`response_format`(`url`默认/`base64`)。
- 返回：`data.image_urls[]`（24h）；n>1 用 `sendMediaGroup`。

### 6.4 视频生成 — `MiniMax-Hailuo-2.3`（异步）
- 建任务：`POST /v1/video_generation` → `task_id`；body：`model`、`prompt`、`duration`(6/10)、`resolution`(`768P`/`1080P`)、`prompt_optimizer`、可选 `callback_url`。
- 查询：`GET /v1/query/video_generation?task_id=<id>` → `status` ∈ `processing/success/failed`，成功带 `file_id`。
- 取文件：`GET /v1/files/retrieve?file_id=<id>` → 临时 URL。
- 实现：优先 `callback_url`（先回显 `challenge`，3 秒内）；回调失败兜底轮询（GenWorkerPool，指数退避，≤10 分钟）。

### 6.5 音乐生成 — `music-2.6`
- `POST /v1/music_generation`；body：`model`、`prompt`(≤2000)、`lyrics`(≤3500，非纯音乐必填)、`is_instrumental`、`audio_setting`、`output_format`。
- 返回：`data.audio`（hex 或 url）；默认 `url` → `sendAudio`。

### 6.6 文件管理 — Files API
- 上传 `POST /v1/files/upload` → `file_id`；检索 `GET /v1/files/retrieve?file_id=<id>`。
- 用途：大视频入站、PDF/DOCX/TXT 文档 → 供 M3 引用（`mm_file://`）。

### 6.7 错误码映射（`base_resp.status_code`）
`0` 成功；`1002` 限流（退避重试）；`1004/2049` 鉴权失败；`1008` 余额不足（提示超管）；`1026` 内容敏感；`2013` 参数错误；`1042` 非法字符>10%。统一抛 `MiniMaxError(code, msg, trace_id)`。

---

## 7. 网络搜索与抓取（三家回退链）

### 7.1 调用顺序与回退策略
**顺序固定**：`Firecrawl → Brave → DuckDuckGo`，逐级回落；**每个 provider 含一次重试**。

```
async def search(query, *, count=5):
    for provider in [firecrawl, brave, duckduckgo]:   # 固定顺序
        for attempt in (1, 2):                        # 1 次重试 = 共 2 次尝试
            try:
                res = await provider.search(query, count)
                if res:                                # 命中即返回
                    return tag(res, provider.name)
            except (Timeout, HTTPError, ProviderError) as e:
                log.warning("search provider failed",
                            provider=provider.name, attempt=attempt, err=e)
                if attempt == 1:
                    await backoff(provider)            # 短退避后重试
    raise AllProvidersFailed(query)                    # 三家×2 全败才抛
```

- **判定失败**：超时、5xx、429、鉴权失败、或返回**空结果**（空结果视为该 provider 失利，继续重试/回落）。
- **重试退避**：`tenacity`，首次失败后 `0.5s × jitter` 退避再试；第二次失败立即回落下一家。
- **结果标注**：附带 `source`（哪家命中），便于在回答中标注来源链接。
- **超时**：单次请求超时（Firecrawl 抓取 15s、Brave/DDG 8s）；整体搜索硬上限（如 40s）防拖垮流式回复。

### 7.2 Provider 抽象（`search/base.py`）
```python
class SearchResult(TypedDict):
    title: str; url: str; snippet: str; source: str

class Provider(Protocol):
    name: str
    async def search(self, query: str, count: int) -> list[SearchResult]: ...
    async def fetch(self, url: str) -> str | None:   # 返回 markdown 正文；不支持则 None
```

### 7.3 Firecrawl（首选，搜索 + 抓取都最强）
- **搜索**：`POST https://api.firecrawl.dev/v2/search`，头 `Authorization: Bearer <FIRECRAWL_API_KEY>`
  - body：`{"query":"...","limit":5,"sources":[{"type":"web"}],"scrapeOptions":{"formats":[{"type":"markdown"}]}}`
  - 返回：`data.web[]`，每项 `title/url/description`，附 `scrapeOptions` 时含 `markdown`（整页正文）。`creditsUsed` 计费。
- **抓取 (WebFetch)**：`POST https://api.firecrawl.dev/v2/scrape`，body `{"url":"...","formats":[{"type":"markdown"}]}` → `{"success":true,"data":{"markdown":"...","metadata":{...}}}`。

### 7.4 Brave（次选，纯搜索）
- `GET https://api.search.brave.com/res/v1/web/search?q=<query>&count=<n>`
- 头：`X-Subscription-Token: <BRAVE_API_KEY>`、`Accept: application/json`、`Accept-Encoding: gzip`
- 参数：`q`(必填)、`count`(≤20)、`offset`(≤9)、`country`、`search_lang`、`freshness`(`pd/pw/pm/py`)、`extra_snippets=true`。
- 返回：`web.results[]`（`title/url/description`+可选 `extra_snippets`）。
- Brave 不返回正文 → 命中后若需正文，对选中 URL 调 WebFetch（Firecrawl scrape）补抓。

### 7.5 DuckDuckGo（兜底，无需 Key）
- 库：`ddgs`（`pip install ddgs`）。用法：`DDGS().text(query, max_results=n, region=..., safesearch=...)`（`max_results` 仅关键字传参）。
- **同步阻塞 → 用 `asyncio.to_thread` 包裹**，避免卡事件循环。
- 只返回摘要不含正文；正文需另抓。云服务器 IP 可能被 DDG 限流/封锁 → 仅作兜底，空结果按失败处理。

### 7.6 WebFetch（抓取单个 URL）回退
`Firecrawl /v2/scrape → 直连 httpx 取 HTML + 本地正文抽取`（`trafilatura`/`readability` 兜底），同样**含一次重试**。输出统一为 markdown，截断到 token 预算内再交给 M3。

---

## 8. 实时时间（`utils/clock.py`）

- 标准库 `zoneinfo.ZoneInfo("Asia/Shanghai")`；Windows 需 `tzdata` 包提供时区数据库。
- **两处使用**：
  1. **系统提示注入**：每轮 `ContextBuilder` 在 system 段写入「当前时间：YYYY-MM-DD HH:MM:SS（周X, Asia/Shanghai, UTC+8）」，让 M3 默认知道"现在几点"。
  2. **工具 `get_current_time(tz?)`**：供 M3 主动调用（算时差、给精确时间戳），默认 `Asia/Shanghai`。
- 统一经 `clock.now()` 出口（以系统墙钟为准），便于测试注入。

---

## 9. 数据模型（SQLite）

```sql
-- 用户与角色
CREATE TABLE users (
  tg_id        INTEGER PRIMARY KEY,
  username     TEXT, first_name TEXT,
  role         TEXT NOT NULL DEFAULT 'user',     -- superadmin|admin|user
  authorized   INTEGER NOT NULL DEFAULT 0,       -- 1=已授权(全功能全场景) 0=拒绝
  authorized_by INTEGER, authorized_at INTEGER,
  settings     TEXT DEFAULT '{}',
  created_at   INTEGER, updated_at INTEGER
);

-- 配额（每用户；calls 或 tokens 两种计量）
CREATE TABLE quotas (
  user_id    INTEGER NOT NULL,
  mode       TEXT NOT NULL,                       -- 'calls' | 'tokens'
  period     TEXT NOT NULL DEFAULT 'day',         -- 'day' | 'month' | 'total'
  limit_val  INTEGER NOT NULL,                    -- 上限；-1 = 无限
  used       INTEGER NOT NULL DEFAULT 0,
  window_start INTEGER, updated_at INTEGER,
  PRIMARY KEY (user_id, mode)
);

-- 用量流水（审计 / 统计）
CREATE TABLE usage_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER, chat_id INTEGER,
  kind TEXT,                                      -- chat|image|video|tts|music|search|fetch
  calls INTEGER DEFAULT 1, tokens INTEGER DEFAULT 0,
  created_at INTEGER
);
CREATE INDEX idx_usage_user_time ON usage_log(user_id, created_at);

-- 会话
CREATE TABLE chats (
  chat_id INTEGER PRIMARY KEY, type TEXT, title TEXT,
  settings TEXT DEFAULT '{}', token_budget INTEGER DEFAULT 128000, created_at INTEGER
);

-- 对话原始消息（压缩前）
CREATE TABLE messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id INTEGER, user_id INTEGER,
  role TEXT, content TEXT, content_type TEXT DEFAULT 'text',
  tokens INTEGER DEFAULT 0, compacted INTEGER DEFAULT 0, created_at INTEGER
);
CREATE INDEX idx_msg_chat ON messages(chat_id, id);

-- 滚动摘要
CREATE TABLE summaries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id INTEGER, summary TEXT, covers_up_to_id INTEGER, tokens INTEGER, created_at INTEGER
);

-- 持久记忆
CREATE TABLE memories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scope TEXT, owner_id INTEGER, text TEXT, source TEXT,
  weight REAL DEFAULT 1.0, created_at INTEGER, last_used_at INTEGER
);
CREATE VIRTUAL TABLE memories_fts USING fts5(text, content='memories', content_rowid='id');

-- 生成任务（图/视/音/乐）——并发后台 worker 的状态源 + 重启恢复依据
CREATE TABLE generations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER, chat_id INTEGER, kind TEXT, model TEXT, prompt TEXT,
  status TEXT,                                    -- queued|processing|success|failed
  task_id TEXT, file_id TEXT, result_url TEXT,
  placeholder_msg_id INTEGER, error TEXT, created_at INTEGER, finished_at INTEGER
);
CREATE INDEX idx_gen_task ON generations(task_id);
CREATE INDEX idx_gen_status ON generations(status);

-- 审计日志
CREATE TABLE audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  actor_id INTEGER, action TEXT, target_id INTEGER, detail TEXT, created_at INTEGER
);
```

---

## 10. 上下文压缩 + 持久记忆

### 10.1 上下文组装（`context.py`）
按 token 预算自底向上拼装：
```
[system 提示 + 工具说明 + 当前 Asia/Shanghai 时间]
[持久记忆块]    ← memory.retrieve(scope, query=本轮文本, top_k)
[历史摘要]      ← 最新 summaries.summary
[近 N 条原始消息]（未 compacted）
[本轮用户消息（含多模态 content 块）]
```
token 估算用 `utils/tokens.py`（中文≈1.5 char/token、英文≈4 char/token，或 tiktoken 兜底）。

### 10.2 自动压缩（`compaction.py`）
- **触发**：近 N 条原始消息累计 token > `budget × COMPACT_TRIGGER_RATIO(0.6)`，或条数 > 阈值。
- **策略**：保留最近 K=8 条，旧段用 **`MiniMax-M2.7-highspeed`** 生成结构化摘要（要点/决定/偏好/未决），写 `summaries`、标 `compacted=1`；已有摘要则增量合并。
- 回复发出后 `asyncio.create_task` 异步触发，不阻塞回复（属 L2 后台任务）。

### 10.3 持久记忆（`memory.py`）
- **写入**：显式 `/remember`、M3 工具 `save_memory`、每轮自动抽取（廉价模型，去重入库 `source=auto`）。
- **检索**：`memories_fts` BM25 + 时间衰减 + `weight`，取 top_k 注入；更新 `last_used_at`。
- **管理**：`/memories` 查看、`/forget <id>` 删除。
- **范围**：私聊 `scope=user`；群聊/Guest `scope=chat`（隐私隔离）。

---

## 11. 流式输出抽象（`streaming.py`）

| 场景 | 首发 | 流式更新 | 定稿 | 要点 |
|---|---|---|---|---|
| **私聊** | `sendMessageDraft(chat_id, draft_id, text="")` | 同 `draft_id`、文本递增 | `sendMessage(最终)` | 草稿是**临时预览(~30s)**，不自动落地；必须窗口内 `sendMessage` 定稿。失败无需清理。 |
| **群聊（成员）** | `sendMessage("▌")` 占位 | 节流 `editMessageText` | 末次 `editMessageText` | 仅 @提及/回复机器人时触发，避免刷屏。 |
| **Guest 模式** | `answerGuestQuery(guest_query_id, ...)` → `SentGuestMessage` | 对返回消息节流 `editMessageText` | 末次 `editMessageText` | 每次召唤仅一次应答入口，后续靠编辑增量更新。 |

**节流（Edit 路径）**：缓冲增量，满足任一才提交——距上次 ≥`EDIT_THROTTLE_MS(1500)`、新增 ≥80 字符、遇句末标点、或流结束；命中 429 按 `retry_after` 退避；文本未变化不发编辑（防 `message is not modified`）。
**并发隔离**：每个流式回答用独立 `draft_id`/占位消息，多个并发回答互不覆盖（见 §4.2）。
**思考内容**：`reasoning_details` 默认不展示；`settings.show_thinking=true` 时以 `> 引用块` 前置。

---

## 12. 入站消息处理（`media.py` + `utils/tg_files.py`）

| Telegram 入站 | 处理 | 交给 M3 |
|---|---|---|
| 文本 | 直接 | `{"type":"text"}` |
| 图片 photo | `getFile` 下载（≤10MB）→ base64 `data:` URL | `image_url`（base64 内联） |
| 视频 video | ≤50MB：下载→base64/临时URL；>50MB：上传 Files API | `video_url`（`mm_file://` 或 url，`fps=1`） |
| 文档 document | pdf/docx/txt：上传 Files API；图片类按图片 | `mm_file://file_id` |
| 语音 voice/audio | （可选）转写后入对话 | — |

> `getFile` 下载链接含 bot token，**MiniMax 无法直接拉取** → 必须由 Bot 先下载字节再 base64 内联或转存 Files API。大文件下载/解码走线程（§4 L4）。Telegram 默认下载约 20MB 上限（自建 Bot API Server 可放宽）。

---

## 13. 工具调用 / 意图路由（`tools.py` + `agent.py`）

**两条触发路径**：
1. **显式命令**：`/image` `/video` `/tts` `/music` `/search` `/fetch <url>`。
2. **自然语言**：M3 `tools` 函数调用，Agent 主循环执行→回灌→续写。

工具 schema（传给 M3 的 `tools`）：
- `generate_image(prompt, aspect_ratio?, n?)`
- `generate_video(prompt, duration?, resolution?)`
- `synthesize_speech(text, voice_id?, emotion?)`
- `generate_music(prompt, lyrics?, is_instrumental?)`
- `web_search(query, count?)` → 走 §7 回退链
- `web_fetch(url)` → 抓取正文 markdown
- `get_current_time(tz?)` → §8，默认 `Asia/Shanghai`
- `save_memory(text)` / `search_memory(query)`

**Agent 主循环**：M3 流式 → `finish_reason=tool_calls` 则暂停流、执行工具 → 工具结果以 `role:tool` 回灌 → 再请求 M3 续写 → 定稿。
- **同步工具**（搜索/抓取/时间/记忆）：直接 `await` 取结果回灌（受 §4 信号量约束）。
- **异步生成工具**（视频/音乐）：**不在主循环等待完成**——交 `GenWorkerPool`，先回灌"已入队/任务ID"让 M3 把控话术，handler 发占位消息后返回；完成后由 worker 回填并 `sendVideo/sendAudio`。这正是"视频后台生成时用户可继续提问"的关键（§4 L2）。
- **配额计量点**：chat 在 usage 末块按 `total_tokens` 计 tokens 配额；生成/搜索类按 `calls`（权重见 §14.2）。

---

## 14. 鉴权与配额

### 14.1 鉴权模型（授权制，全场景一致）
> 核心规则：**只看「是否被授权」，不分场景。** 授权用户在私聊、群聊、Guest 模式下都能用全部功能；未授权用户在任何场景下一律 `Permission Denied`。

- **判定主体**：始终用**发起人的 Telegram 用户 ID**：
  - 私聊 / 群聊：`message.from_user.id`。
  - **Guest 模式**：`guest_bot_caller_user.id`（召唤者），而非聊天本身。
- **门控位置**：`Auth` 中间件在所有 handler 之前：
  ```
  if user.authorized != 1 and user.role not in ('admin','superadmin'):
      reply("⛔ Permission Denied")
      audit("denied", user.tg_id, ctx)
      raise CancelHandler                     # 终止后续处理
  ```
- `SUPERADMIN_IDS`（`.env`）启动时强制 `role=superadmin, authorized=1`，不可降级/撤权。
- **能力一致**：授权后不再细分功能——文本/图片/语音/视频/音乐/搜索全开（费用由配额约束）。

### 14.2 配额控制（calls / tokens 双模式）
> 每用户独立配额，**按「调用次数」或「Token」计量**，周期 `day/month/total`。授权用户也受用量约束，防滥用超支。

- **两种计量模式**（同一用户可各设一条，常见：tokens 管对话、calls 管生成）：
  - **`calls`**：每次请求/生成计 1，或按权重（视频=5、音乐=5、图片=1、搜索=1）。
  - **`tokens`**：对话按 MiniMax 返回的 `total_tokens` 累加（需 `stream_options.include_usage`）。
- **检查时机**：`Quota` 中间件在 Auth 之后**预检**（估算本次开销，不足则拒绝并提示剩余）；请求完成后**结算**真实用量。
- **并发安全结算**：读-改-写用 `BEGIN IMMEDIATE` 事务 / 按用户异步锁，避免并发请求对同一用户配额竞态导致少计（§4 L4）。
- **周期重置**：读取时若 `now` 超出 `window_start + period`，先归零再判定（惰性重置，无需定时任务）。
- **超额响应**：`📊 配额不足：{mode} 已用 {used}/{limit}（{period}），将于 {reset_at} 重置`。
- **默认值**：`.env` 配 `DEFAULT_QUOTA_MODE/LIMIT/PERIOD`；新授权用户自动套用；`-1`=无限（超管默认无限）。

### 14.3 角色与管理命令
| 角色 | 说明 |
|---|---|
| `superadmin` | 全权；管理 admin、授权、配额、广播、审计；配额默认无限 |
| `admin` | 授权/撤权普通用户、设/查配额 |
| `user`（authorized=1） | 全功能全场景，受配额约束 |
| `user`（authorized=0） | 一律 Permission Denied |

| 命令 | 权限 | 作用 |
|---|---|---|
| `/grant <user_id\|回复>` | admin+ | 授权用户，套用默认配额 |
| `/revoke <user_id>` | admin+ | 撤销授权 |
| `/promote <user_id>` | superadmin | 提升为 admin |
| `/demote <user_id>` | superadmin | 降级 admin |
| `/setquota <user_id> <calls\|tokens> <limit> [day\|month\|total]` | admin+ | 设置/调整配额，`-1`=无限 |
| `/resetquota <user_id> [calls\|tokens]` | admin+ | 清零当前周期用量 |
| `/quota` | 授权用户 | 查看本人配额与剩余 |
| `/quotas [page]` | admin+ | 列出各用户配额与用量 |
| `/users [page]` | admin+ | 列出用户、角色、授权状态 |
| `/stats` | admin+ | 用量统计（聚合 usage_log） |
| `/broadcast <text>` | superadmin | 群发已授权用户 |
| `/audit [n]` | superadmin | 查看审计日志 |
| `/whoami` | 所有 | 查看自身角色/授权/配额 |

所有授权与配额写操作记入 `audit_log`。

### 14.4 Guest 模式接入要点（Bot API 10.0）
- 需在 **BotFather MiniApp 开启 Guest Mode**；`getMe` 暴露 `supports_guest_queries`。
- `Update.guest_message` 投递召唤消息；`Message` 带 `guest_query_id`、`guest_bot_caller_user`、`guest_bot_caller_chat`。
- 用 `answerGuestQuery(guest_query_id, ...)` 应答（aiogram 未封装则 `bot(Raw(...))`）。
- **鉴权按召唤者 `guest_bot_caller_user.id`**：未授权 → 回 `⛔ Permission Denied`。
- **限制**：Guest 无群历史、无成员列表，不收后续消息（除非再次被 @/回复）→ 上下文仅"召唤消息 + 其引用消息 + `scope=chat` 记忆"。
- `allowed_updates` 必须显式含 `guest_message`。

---

## 15. Webhook 服务（`server.py`）

- 单 aiohttp app，`SimpleRequestHandler(handle_in_background=True)`：
  - `POST /tg/<WEBHOOK_SECRET>`：校验 `X-Telegram-Bot-Api-Secret-Token` 头 → 立即 ACK，后台 `feed_update`。
  - `POST /mmx/callback`：MiniMax 视频/音乐回调；先回显 `challenge`（3s 内），再按 `task_id` 更新 `generations` 并回填消息（按 `task_id` 去重）。
  - `GET /healthz`：探活。
- 启动：`setWebhook(url=WEBHOOK_HOST+/tg/<secret>, secret_token=..., allowed_updates=[..., "guest_message"])`；并**恢复未决生成任务**（§4 L5）。
- TLS 由前置反代（Caddy 自动证书 / Nginx）；Bot 监听内网端口。
- 退化方案：`MODE=polling` 用 `start_polling(handle_as_tasks=True, tasks_concurrency_limit=...)`（本地调试；视频改纯轮询）。

---

## 16. 配置与环境变量（`.env.example`）

```dotenv
# Telegram
BOT_TOKEN=123456:ABC...
WEBHOOK_HOST=https://bot.example.com
WEBHOOK_SECRET=long-random-string
MODE=webhook                              # webhook | polling
SUPERADMIN_IDS=11111111,22222222

# MiniMax
MINIMAX_API_KEY=eyJ...
MINIMAX_BASE_URL=https://api.minimaxi.com/v1
MMX_CALLBACK_URL=https://bot.example.com/mmx/callback

# 模型（默认最新）
MODEL_CHAT=MiniMax-M3
MODEL_SUMMARY=MiniMax-M2.7-highspeed
MODEL_TTS=speech-2.8-hd
MODEL_IMAGE=image-01
MODEL_VIDEO=MiniMax-Hailuo-2.3
MODEL_MUSIC=music-2.6

# 联网搜索 / 抓取（顺序固定 firecrawl→brave→duckduckgo）
FIRECRAWL_API_KEY=fc-...
BRAVE_API_KEY=BSA...
SEARCH_ORDER=firecrawl,brave,duckduckgo
SEARCH_RESULT_COUNT=5
SEARCH_RETRY=1                            # 每家额外重试次数
FIRECRAWL_TIMEOUT_S=15
BRAVE_TIMEOUT_S=8
DDG_TIMEOUT_S=8

# 时间
DEFAULT_TZ=Asia/Shanghai

# 鉴权 / 配额
PERMISSION_DENIED_TEXT=⛔ Permission Denied
DEFAULT_QUOTA_MODE=tokens                 # tokens | calls
DEFAULT_QUOTA_LIMIT=200000                # -1=无限
DEFAULT_QUOTA_PERIOD=day                  # day | month | total
GEN_CALL_WEIGHTS=image:1,video:5,music:5,tts:1,search:1

# 并发 / 背压
MAX_CONCURRENT_CHATS=32
MAX_CONCURRENT_GENERATIONS=8
PER_USER_CONCURRENCY=3
TG_GLOBAL_SEND_RATE=28                    # msg/s
WORKER_POLL_INTERVAL_S=5
HTTPX_MAX_CONNECTIONS=100

# 行为 / 存储
DEFAULT_TOKEN_BUDGET=128000
COMPACT_TRIGGER_RATIO=0.6
EDIT_THROTTLE_MS=1500
DB_PATH=./data/bot.db
SQLITE_WAL=1
LOG_LEVEL=INFO
```

---

## 17. 实施里程碑

| 阶段 | 内容 | 产出 |
|---|---|---|
| **M0 脚手架 + 并发地基** | 项目结构、config、SQLite 建表(WAL)、httpx 单例、信号量/发送限流器、aiohttp+aiogram Webhook(`handle_in_background=True`) 跑通、`/start` `/whoami` | 能并发收发消息 |
| **M1 对话核心** | MiniMax client、M3 多模态、私聊 `sendMessageDraft` 流式 + 定稿、入站文本/图片 | 私聊可流式问答 + 看图 |
| **M2 鉴权 + 配额** | Auth 中间件（授权制/Permission Denied）、QuotaManager（calls/tokens + 并发安全结算 + 周期重置）、授权与配额命令、审计 | 完整权限+用量门控 |
| **M3 上下文/记忆** | messages 落库、token 预算、滚动压缩、FTS5 记忆、`/reset` `/remember` | 长对话不爆、跨会话记忆 |
| **M4 群聊 + Guest** | 群聊 @触发 + Edit 流式；Guest `answerGuestQuery`+Edit；召唤者鉴权；`allowed_updates` | 三场景流式齐活 |
| **M5 生成 + 后台 worker** | tools + Agent 循环；TTS/图片（同步）、视频/音乐（GenWorkerPool 后台轮询/回调 + 重启恢复）；媒体回传 | 生成期间用户可继续对话 |
| **M6 搜索 + 时间** | search 回退链（Firecrawl→Brave→DDG + 重试）、WebFetch、`get_current_time`、系统时间注入 | 联网搜索/抓取/实时时间 |
| **M7 加固 + 压测** | 错误码映射与重试、429 退避、媒体校验、日志监控、并发压测（多用户/同用户多任务）、单测、部署（Docker+Caddy/systemd） | 可上线 |

---

## 18. 风险与注意事项

- **并发与事件循环**：任何阻塞调用（`ddgs`、大文件 hex/base64 解码、CPU 密集）必须 `asyncio.to_thread`，否则单点阻塞会拖垮所有用户。
- **handler 不可 `await` 长生成**：视频/音乐必须交后台 worker，否则同一用户/同一 worker 槽会被独占，违背"边生成边对话"。
- **SQLite 写并发**：务必开 WAL + 短事务 + `busy_timeout`；配额读-改-写用 `BEGIN IMMEDIATE`。写压力过大再切 PostgreSQL。
- **背压而非拒绝**：并发超限时排队并提示"排队中"，避免直接丢弃用户请求；同时用 `PER_USER_CONCURRENCY` 防单用户饿死他人。
- **重启恢复**：未决 `generations` 必须在启动时重挂轮询，否则重启丢任务。
- **草稿 30s 窗口**：私聊流式须在 ~30s 内 `sendMessage` 定稿；长生成用占位真实消息表进度，勿用草稿。
- **`editMessageText` / 发送限流**：群聊/Guest 必须节流；全局发送走限流器（≈30 msg/s）；命中 429 按 `retry_after` 退避；无变化不发编辑。
- **Guest 无历史 / 按召唤者鉴权**：上下文仅靠本次召唤；鉴权用 `guest_bot_caller_user.id`。
- **搜索回退依赖外部稳定性**：三家全挂给"暂时无法联网"；DDG 在云 IP 可能被封，仅兜底，空结果按失败处理。
- **临时 URL 24h 失效**：图/视/音频 MiniMax 链接须即时下载转存为 Telegram 文件，勿存裸链。
- **媒体下载上限**：Telegram 默认约 20MB；入站视频 >50MB 走 Files API。
- **Token 计量来源**：务必开 `stream_options.include_usage` 取末块 `total_tokens`；流中断按已收 delta 估算兜底。
- **时区数据**：Windows 下 `zoneinfo` 需 `tzdata` 包，否则 `Asia/Shanghai` 解析失败。
- **费用控制**：视频/音乐昂贵 → calls 模式给高权重；`1008 余额不足` 提示超管。
- **新方法库支持**：`answerGuestQuery`/`sendRichMessageDraft` 若 aiogram 未封装，用 `bot(Raw(...))` 直发并锁版本。
- **幂等回调**：MiniMax 回调可能重复，按 `task_id` 去重，回填前查 `generations.status`。

---

## 19. 依赖清单（`pyproject.toml` 草案）

```
python = ">=3.11"
aiogram = "^3.x"          # Bot 框架（含 send_message_draft；handle_in_background 并发）
aiohttp = "^3.x"          # Webhook / 回调服务
httpx = "^0.27"           # MiniMax / Firecrawl / Brave 异步客户端（单例）
ddgs = "^6.x"             # DuckDuckGo 搜索（无需 Key）
aiosqlite = "^0.20"       # SQLite async（WAL）
SQLAlchemy = "^2.0"       # ORM（async）
pydantic-settings = "^2"  # 配置
structlog = "^24"         # 日志
tenacity = "^9"           # 重试退避
tzdata = "*"              # Windows 时区数据库（Asia/Shanghai）
# 可选：aiojobs（后台任务监督）、tiktoken（token 估算）、
#       trafilatura/readability-lxml（WebFetch 正文兜底）、pillow（图片校验）
```

---

### 附：三条端到端时序

**A. 自然语言要视频（私聊）——后台生成，不阻塞后续提问**
```
用户: “做个 6 秒雪山日落视频” → Auth(已授权) → Quota(calls 预检, video=5, 充足) → 并发(取槽)
 → M3(stream, tools) 调 generate_video(prompt, 6s, 768P)
 → 交 GenWorkerPool: POST /v1/video_generation(callback_url) → task_id, 落 generations(queued)
 → Draft 定稿「🎬 已开始生成，完成后发你」→ handler 返回(释放槽)
 ── 同时 ──
用户: “顺便讲个冷笑话” → 作为【另一个 Update 任务】并发处理 → 立刻流式回笑话
 ── 后台 ──
[回调] /mmx/callback (challenge→echo; success+file_id) → /v1/files/retrieve → 下载字节(线程)
 → 编辑占位为「✅ 完成」并 sendVideo → 写 messages → 结算配额(calls+5)
```

**B. 联网搜索 + 抓取（群聊 @机器人）**
```
@bot “查下今天上海天气并总结” → Auth(发起人已授权) → Quota(tokens 预检) → 并发(取槽)
 → 占位「🔎 正在联网…」 → M3 调 web_search("上海天气 今天")
 → router: Firecrawl /v2/search (尝试1失败→重试2成功) → 命中, source=firecrawl
   (Firecrawl 两次皆败 → Brave；再败 → DuckDuckGo；全败→提示重试)
 → 取 top URL → web_fetch → Firecrawl /v2/scrape → markdown 正文
 → 结果回灌 M3 → Edit 流式输出总结(附来源链接 + 当前 Asia/Shanghai 时间)
 → 结算 tokens 配额 + usage_log(kind=search/chat)
```

**C. 多用户并发（不同用户互不阻塞）**
```
用户X(生成视频, 后台 worker 占用 1 个生成槽)  ┐
用户Y(流式问答, 占用 1 个 chat 槽)            ├─ 三者各自独立 asyncio 任务并行
用户Z(联网搜索, 占用 1 个 chat 槽 + 外部IO)   ┘
 → 任一慢任务只占自己的槽与任务；超 MAX_CONCURRENT_* 时新请求排队，不阻塞已在跑的
 → SQLite WAL 多读单写；httpx 单例连接池复用；Telegram 发送全局限流
```
