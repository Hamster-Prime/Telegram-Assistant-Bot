# Telegram 助理机器人

[![Python 3.11+](https://img.shields.io/badge/python-≥3.11-blue.svg)](https://www.python.org/)
[![aiogram 3.29+](https://img.shields.io/badge/aiogram-≥3.29-0097e6.svg)](https://docs.aiogram.dev/)
[![Tests](https://img.shields.io/badge/tests-29%20files-green.svg)](#测试)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](#license)

基于 **MiniMax M3 多模态大模型**的全功能 Telegram AI 助理。支持文本 / 图片 / 视频 / 文档多模态输入，集成语音合成、图片生成、视频生成、音乐生成等 AIGC 能力，内置四级联网搜索回退链、FTS5 全文持久记忆、上下文自动压缩、多 Key 容错以及完善的鉴权与配额体系。

---

## 功能全景

### AI 对话

- MiniMax M3 多模态大模型，100 万 Token 上下文窗口
- 支持文本 + 图片 + 视频 + 文档混合输入
- 流式输出：私聊使用草稿流（Draft），群聊 / Guest 使用编辑流（Edit）
- Agent 工具调用循环（最多 6 轮），自动决策调用搜索、抓取、生成等工具
- 思维链展示（可配置）

### 多模态生成

| 能力 | 模型 | 说明 |
|------|------|------|
| 语音合成 (TTS) | `speech-2.8-hd` | 文字 → 语音消息 |
| 图片生成 | `image-01` | 文字描述 → 图片，支持多张 |
| 视频生成 | `MiniMax-Hailuo-2.3` | 文字描述 → 视频，异步生成 + 回调 / 轮询 |
| 音乐生成 | `music-2.6` | 描述 + 歌词 → 音乐 |

视频 / 音乐生成通过后台 Worker 异步执行，服务重启后自动恢复未完成任务。

### 联网搜索（四级回退链）

```
MiniMax Search → Firecrawl → Brave → DuckDuckGo
```

每级最多重试 1 次，通过 `SEARCH_ORDER` 自定义优先级。全部失败才向用户报错。

### 持久记忆

- SQLite + FTS5 全文索引，BM25 排名 + 时间衰减 + 权重评分
- 用户自主管理：`/remember`、`/memories`、`/forget`
- 支持从对话中自动提取关键信息

### 鉴权与配额

- **授权制鉴权**：管理员手动授权，私聊 / 群聊 / Guest 统一鉴权
- **角色层级**：`superadmin > admin > authorized > unauthorized`
- **配额双模式**：`tokens`（基于 MiniMax 用量）或 `calls`（按操作类型加权）
- **周期选项**：`day` / `month` / `total`，窗口懒重置，并发安全

### 并发架构

五层并发模型：

1. **Update 级别**：`handle_in_background` 避免阻塞 Telegram 长轮询
2. **长任务非阻塞**：`asyncio.create_task` + `TaskRegistry` 跟踪
3. **有界资源反压**：`BoundedSemaphore` 控制全局并发生成数
4. **共享资源安全**：per-user `asyncio.Lock` 串行化写入
5. **生命周期持久化**：重启恢复 pending 任务

---

## 系统要求

| 依赖 | 版本 | 用途 |
|------|------|------|
| Python | ≥ 3.11 | 运行时 |
| [uv](https://docs.astral.sh/uv/getting-started/installation/) | 最新 | 包管理器 |
| Telegram Bot Token | — | [@BotFather](https://t.me/BotFather) 创建 |
| MiniMax API Key | — | [MiniMax 开放平台](https://platform.minimaxi.com/) 获取，支持多 Key |

可选依赖（用于联网搜索增强）：

| 服务 | 用途 |
|------|------|
| [Firecrawl](https://www.firecrawl.dev/) API Key | 网页搜索 + 抓取 |
| [Brave Search](https://brave.com/search/api/) API Key | 搜索引擎 |

---

## 快速开始

```bash
# 1. 克隆项目
git clone https://github.com/your-username/Telegram-Assistant-Bot.git
cd Telegram-Assistant-Bot

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env，至少填入以下必填项：
#   BOT_TOKEN=<你的 Telegram Bot Token>
#   MINIMAX_API_KEYS=<你的 MiniMax API Key，多个用逗号分隔>
#   SUPERADMIN_IDS=<你的 Telegram 用户 ID>
#   MODE=polling

# 3. 安装依赖
uv sync

# 4. 启动
uv run python -X utf8 -m app.main
```

启动后在 Telegram 中向 Bot 发送 `/start` 即可。

---

## Docker 部署

生产环境推荐 webhook 模式 + Caddy 自动 TLS：

```bash
# 1. 配置 .env
MODE=webhook
WEBHOOK_HOST=https://bot.example.com
WEBHOOK_SECRET=<随机密钥>

# 2. 编辑 Caddyfile，将 bot.example.com 替换为你的域名

# 3. 启动
docker compose up -d
```

**架构**：`Caddy (80/443, 自动 TLS) → Bot (8080)`

**数据持久化**：`./data/` 目录挂载到容器内，包含 SQLite 数据库和临时缓存。

```bash
# 查看日志
docker compose logs -f bot

# 重启
docker compose restart bot
```

---

## 配置参考

所有配置项均可通过环境变量或 `.env` 文件设置。完整列表见 [`.env.example`](.env.example)。

### Telegram

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `BOT_TOKEN` | — | Telegram Bot Token（必填） |
| `MODE` | `webhook` | `webhook` 或 `polling` |
| `WEBHOOK_HOST` | — | Webhook URL（如 `https://bot.example.com`） |
| `WEBHOOK_SECRET` | `secret` | Webhook 路径密钥（生产环境必须随机设置） |
| `SUPERADMIN_IDS` | — | 超级管理员 Telegram ID，逗号分隔（必填） |

### MiniMax API

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `MINIMAX_API_KEYS` | — | API Key，逗号分隔，多 Key 自动 fallback（必填） |
| `MINIMAX_BASE_URL` | `https://api.minimaxi.com/v1` | API 地址 |
| `MMX_CALLBACK_URL` | — | 视频 / 音乐异步回调 URL |
| `MMX_CALLBACK_SECRET` | — | 回调端点鉴权 Token |

### 模型配置

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `MODEL_CHAT` | `MiniMax-M3` | 对话模型 |
| `MODEL_SUMMARY` | `MiniMax-M2.7-highspeed` | 上下文压缩摘要模型 |
| `MODEL_TTS` | `speech-2.8-hd` | 语音合成模型 |
| `MODEL_IMAGE` | `image-01` | 图片生成模型 |
| `MODEL_VIDEO` | `MiniMax-Hailuo-2.3` | 视频生成模型 |
| `MODEL_MUSIC` | `music-2.6` | 音乐生成模型 |

### 联网搜索

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `SEARCH_ORDER` | `minimax,firecrawl,brave,duckduckgo` | 搜索源优先级 |
| `SEARCH_RESULT_COUNT` | `5` | 返回结果数 |
| `SEARCH_RETRY` | `1` | 每级重试次数 |
| `FIRECRAWL_API_KEY` | — | Firecrawl API Key |
| `BRAVE_API_KEY` | — | Brave Search API Key |

### 鉴权与配额

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PERMISSION_DENIED_TEXT` | `⛔ Permission Denied` | 未授权提示文本 |
| `DEFAULT_QUOTA_MODE` | `tokens` | `tokens` 或 `calls` |
| `DEFAULT_QUOTA_LIMIT` | `200000` | 配额上限（`-1` = 无限） |
| `DEFAULT_QUOTA_PERIOD` | `day` | `day` / `month` / `total` |

### 并发与行为

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `MAX_CONCURRENT_CHATS` | `32` | 最大并行会话数 |
| `MAX_CONCURRENT_GENERATIONS` | `8` | 最大并行生成任务数 |
| `PER_USER_CONCURRENCY` | `3` | 单用户并行任务数 |
| `TG_GLOBAL_SEND_RATE` | `28` | 全局发送速率限制（msg/s） |
| `EDIT_THROTTLE_MS` | `1000` | 私聊编辑节流间隔（ms） |
| `GROUP_EDIT_THROTTLE_MS` | `3000` | 群聊编辑节流间隔（ms） |
| `DEFAULT_TOKEN_BUDGET` | `128000` | 上下文 Token 预算 |
| `COMPACT_TRIGGER_RATIO` | `0.6` | 上下文压缩触发比例 |
| `AUTO_CLEAR_MINUTES` | `30` | 自动清除会话时间（分钟） |

### 数据库与日志

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DB_PATH` | `./data/bot.db` | SQLite 数据库路径 |
| `SQLITE_WAL` | `1` | 启用 WAL 模式 |
| `LOG_LEVEL` | `INFO` | 日志级别 |
| `DEFAULT_TZ` | `Asia/Shanghai` | 默认时区 |

---

## 使用指南

### 命令一览

| 命令 | 权限 | 说明 |
|------|------|------|
| `/start` | 所有人 | 欢迎信息 |
| `/help` | 所有人 | 帮助信息 |
| `/whoami` | 授权用户 | 查看自身信息 |
| `/quota` | 授权用户 | 查看配额使用情况 |
| `/reset` | 授权用户 | 重置当前会话上下文 |
| `/remember` | 授权用户 | 保存一条持久记忆 |
| `/memories` | 授权用户 | 查看所有记忆 |
| `/forget` | 授权用户 | 删除指定记忆 |
| `/image` | 授权用户 | 生成图片 |
| `/video` | 授权用户 | 生成视频 |
| `/tts` | 授权用户 | 文字转语音 |
| `/music` | 授权用户 | 生成音乐 |
| `/search` | 授权用户 | 联网搜索 |
| `/fetch` | 授权用户 | 抓取网页内容 |
| `/grant` | Admin+ | 授权用户 |
| `/revoke` | Admin+ | 撤销授权 |
| `/setquota` | Admin+ | 设置用户配额 |
| `/promote` | Superadmin | 提升为管理员 |
| `/demote` | Superadmin | 降级管理员 |
| `/broadcast` | Superadmin | 广播消息 |

### 交互模式

| 模式 | 触发方式 | 流式输出方式 |
|------|----------|-------------|
| **私聊** | 直接发消息 | 草稿流（Draft） |
| **群聊** | @提及 Bot 或回复 Bot 消息 | 编辑流（Edit） |
| **Guest** | Inline Query | 编辑流（Edit） |

### 常见场景

```
# 对话
直接发文字，Bot 自动流式回复

# 图片理解
发送图片 + 文字描述，模型结合图片内容回答

# 生成图片
/image 一只戴墨镜的猫

# 生成视频
/video 海边日落延时摄影
→ 后台异步生成，完成后自动推送

# 联网搜索
/search Python 异步编程最佳实践
→ 自动 fallback 多个搜索引擎

# 持久记忆
/remember 我的生日是 6 月 15 日
/memories
/forget 1
```

---

## 架构概览

```
app/
├── main.py                  # 入口：webhook / polling 双模式启动
├── config.py                # pydantic-settings 配置解析（50+ 字段）
├── logging.py               # structlog 中文结构化日志
├── server.py                # aiohttp 服务：/tg/<secret> /mmx/callback /healthz
├── services.py              # 服务容器：依赖注入与组件接线
│
├── handlers/                # 消息处理层
│   ├── private.py           #   私聊消息（草稿流）
│   ├── group.py             #   群聊消息（@提及触发，编辑流）
│   ├── guest.py             #   Guest 模式（answerGuestQuery）
│   ├── commands.py          #   斜杠命令
│   ├── inline.py            #   Inline Query
│   ├── media.py             #   图片 / 视频 / 音频 / 文档处理
│   ├── media_group.py       #   媒体组聚合缓冲
│   ├── pipeline.py          #   消息处理流水线
│   ├── callbacks.py         #   Inline Keyboard 回调（分页 / 导航）
│   ├── replies.py           #   回复消息处理
│   ├── mentions.py          #   @提及解析
│   └── errors.py            #   全局错误处理
│
├── core/                    # 核心能力层
│   ├── agent.py             #   Agent 工具调用循环（最多 6 轮）
│   ├── streaming.py         #   StreamRenderer 抽象（Draft / Edit / Guest）
│   ├── context.py           #   ContextBuilder：Token 预算、摘要、系统时间注入
│   ├── compaction.py        #   上下文自动压缩（滚动摘要）
│   ├── memory.py            #   持久记忆（FTS5 BM25 + 时间衰减）
│   ├── auth.py              #   授权鉴权中间件
│   ├── quota.py             #   QuotaManager：calls / tokens 双模式
│   ├── concurrency.py       #   信号量、per-user 锁、发送限速、任务注册
│   ├── ratelimit.py         #   Token Bucket + Telegram 编辑节流
│   ├── workers.py           #   GenWorkerPool：视频 / 音乐异步生成
│   ├── tools.py             #   Agent 工具定义 + 分发器
│   ├── delivery.py          #   消息投递工具
│   ├── htmlfmt.py           #   Telegram HTML 格式化
│   └── richmsg.py           #   富文本消息格式化
│
├── minimax/                 # MiniMax API 封装
│   ├── client.py            #   多 Key fallback httpx 单例（连接池）
│   ├── chat.py              #   /v1/chat/completions（M3 多模态 + 流式）
│   ├── tts.py               #   /v1/t2a_v2（语音合成）
│   ├── image.py             #   /v1/image_generation（图片生成）
│   ├── video.py             #   /v1/video_generation（视频生成）
│   ├── music.py             #   /v1/music_generation（音乐生成）
│   ├── files.py             #   Files API 上传 / 检索（mm_file://）
│   ├── voice.py             #   声音克隆 / 设计 API
│   └── quota.py             #   MiniMax 配额查询 API
│
├── search/                  # 联网搜索
│   ├── router.py            #   回退链路由器 + 重试编排
│   ├── base.py              #   Provider 抽象（search / fetch 协议）
│   ├── minimax.py           #   MiniMax 内置搜索
│   ├── firecrawl.py         #   Firecrawl v2 搜索 + 抓取
│   ├── brave.py             #   Brave Search
│   └── duckduckgo.py        #   DuckDuckGo（无需 Key，异步包装）
│
├── db/                      # 数据层
│   ├── engine.py            #   aiosqlite 引擎（WAL 模式，busy_timeout）
│   ├── models.py            #   数据模型
│   ├── dao.py               #   数据访问对象
│   └── schema.sql           #   DDL：9 张表 + FTS5 虚拟表 + 触发器
│
└── utils/                   # 工具函数
    ├── clock.py             #   Asia/Shanghai 时区处理
    ├── tokens.py            #   Token 估算（中文 ~1.5 字/Token）
    ├── hexaudio.py          #   hex / base64 音频解码（线程池卸载）
    └── tg_files.py          #   Telegram getFile 下载 → bytes/base64
```

### 数据库表结构

| 表名 | 用途 |
|------|------|
| `users` | 用户档案、角色、授权状态 |
| `quotas` | 用户配额（calls / tokens，day / month / total） |
| `usage_log` | 用量审计日志 |
| `chats` | 会话 session，Token 预算 |
| `messages` | 原始对话消息（压缩前） |
| `summaries` | 滚动上下文摘要 |
| `memories` | 持久记忆 + FTS5 全文索引 |
| `generations` | 异步生成任务状态（视频 / 音乐） |
| `audit_log` | 管理操作审计日志 |

---

## 多 Key 容错

支持多个 MiniMax API Key，逗号分隔配置：

```env
MINIMAX_API_KEYS=key1,key2,key3
```

**容错逻辑**：`key1 → 失败重试 1 次 → key2 → 失败重试 1 次 → key3 → 全部失败报错`

- **Key 级错误**（限流 1002 / 鉴权 1004 / 余额不足 1008 / 超时 / 5xx）→ 重试 + 换 Key
- **请求级错误**（内容敏感 1026 / 参数错误 2013）→ 立即报错，不浪费其他 Key
- 日志逐次记录：Key 序号、脱敏 Key、尝试次数、错误码、耗时、下一步动作

---

## 测试

```bash
uv sync --extra dev
uv run python -X utf8 -m pytest tests/ -v
```

29 个测试文件，覆盖以下模块：

| 测试文件 | 覆盖范围 |
|---------|---------|
| `test_minimax_client.py` | 多 Key fallback 逻辑 |
| `test_concurrency.py` | 并发控制原语 |
| `test_auth_middleware.py` / `test_auth_quota.py` | 鉴权与配额中间件 |
| `test_context.py` / `test_engine_isolation.py` | 上下文管理与隔离 |
| `test_search_router.py` | 搜索回退链 |
| `test_streaming.py` | 流式输出节流 |
| `test_agent.py` / `test_tool_schemas.py` | Agent 工具调用循环 |
| `test_workers.py` | 后台 Worker（幂等 / 恢复） |
| `test_server.py` | Server 回调端点 |
| `test_pipeline.py` | 消息处理流水线 |
| `test_guest.py` / `test_inline.py` | Guest 模式 / Inline Query |
| `test_media.py` / `test_media_group.py` | 媒体处理与聚合 |
| `test_mentions.py` | @提及解析 |
| `test_pagination.py` | 分页渲染 |
| `test_htmlfmt.py` | HTML 格式化 |
| `test_generation.py` | 生成任务 |
| `test_mmx_quota.py` / `test_pipeline_quota.py` | 配额计算 |
| `test_auto_clear.py` | 自动清除 |
| `test_replies.py` | 回复处理 |
| `test_admin_actions.py` | 管理员操作 |

---

## 技术栈

| 组件 | 技术 |
|------|------|
| 语言 | Python 3.11+ |
| 包管理 | uv |
| Bot 框架 | aiogram 3.29+ |
| HTTP 服务器 | aiohttp 3.9+ |
| HTTP 客户端 | httpx 0.27+ |
| 数据库 | SQLite（aiosqlite 0.20+） |
| 全文搜索 | FTS5（SQLite 内置） |
| 配置管理 | pydantic 2.7+ / pydantic-settings 2.2+ |
| 日志 | structlog 24.1+ |
| 重试 | tenacity 9.0+ |
| LLM | MiniMax M3（多模态）、MiniMax M2.7-highspeed（摘要） |
| 反向代理 | Caddy 2（自动 TLS） |
| 容器化 | Docker + Docker Compose |
| 代码检查 | Ruff 0.6+ |
| 测试 | pytest 8.0+ / pytest-asyncio 0.23+ |

---

## 故障排查

**Bot 无响应？**

检查 `MODE` 设置。polling 模式下确保没有其他 Bot 实例在运行；webhook 模式下检查 `WEBHOOK_HOST` 是否可达，`/healthz` 端点是否正常。

**MiniMax 调用全部失败？**

检查 `MINIMAX_API_KEYS` 是否正确配置，Key 是否有余额。Bot 会自动尝试所有 Key 并在日志中记录详细错误信息。

**视频 / 音乐生成后没收到？**

这些是异步任务，生成需要时间。检查日志中 Worker 状态，确保 `WORKER_POLL_INTERVAL_S` 配置合理。服务重启后会自动恢复未完成的任务。

**群聊中 Bot 太活跃？**

Bot 在群聊中需要被 @提及或回复 Bot 消息才会响应。检查 `GROUP_EDIT_THROTTLE_MS` 设置是否合理（默认 3 秒，贴近 Telegram 限流阈值）。

**上下文丢失 / 回答变短？**

当 Token 使用量超过 `COMPACT_TRIGGER_RATIO`（默认 60%）时，Bot 会自动压缩上下文。可通过 `/reset` 手动重置。

---

## 贡献

欢迎 Issue 和 Pull Request！

- 代码风格：[Ruff](https://github.com/astral-sh/ruff)（`ruff check` + `ruff format`）
- 提交前请确保测试通过：`uv run pytest tests/ -v`
- 建议遵循 [Conventional Commits](https://www.conventionalcommits.org/) 规范

---

## License

[MIT](LICENSE)
