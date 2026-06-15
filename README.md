# Telegram 助理机器人

[![Python 3.11+](https://img.shields.io/badge/python-≥3.11-blue.svg)](https://www.python.org/)
[![Tests](https://img.shields.io/badge/tests-29%20files-green.svg)](#测试)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](#license)

接入 MiniMax M3 多模态大模型的助理型 Telegram Bot，支持语音合成 / 图片生成 / 视频生成 / 音乐生成，具备联网搜索、持久记忆、上下文自动压缩、鉴权授权与配额控制。

---

## 功能亮点

| 分类 | 能力 |
|------|------|
| 🤖 **AI 对话** | MiniMax M3 多模态大模型，支持文本 + 图片输入，流式输出 |
| 🎨 **多模态生成** | 语音合成 (TTS)、图片生成、视频生成、音乐生成，异步 Worker 后台处理 |
| 🔍 **联网搜索** | Firecrawl → Brave → DuckDuckGo 三级回退链，自动重试与切换 |
| 🧠 **持久记忆** | SQLite 持久化，用户可自主管理（/remember /forget /memories） |
| 🔐 **鉴权配额** | 授权制鉴权 + calls/tokens 双模式配额，支持 day/month/total 周期 |
| ⚡ **高并发** | 多用户并行，同一用户可同时「流式回答 + 后台生成 + 新提问」 |

---

## 系统要求

- **Python** ≥ 3.11
- **uv** 包管理器（[安装指南](https://docs.astral.sh/uv/getting-started/installation/)）
- **Telegram Bot Token**（[@BotFather](https://t.me/BotFather) 创建）
- **MiniMax API Key**（[MiniMax 开放平台](https://platform.minimaxi.com/) 获取，支持多个 Key）

---

## 快速开始

```bash
# 1. 克隆项目
git clone https://github.com/your-username/Telegram-Assistant-Bot.git
cd Telegram-Assistant-Bot

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env，填入 BOT_TOKEN、MINIMAX_API_KEYS、SUPERADMIN_IDS 等

# 3. 启动（本地调试用 polling 模式）
#    确保 .env 中 MODE=polling
uv sync
uv run python -X utf8 -m app.main
```

启动后在 Telegram 中向你的 Bot 发送 `/start` 即可。

---

## Docker 部署

生产环境推荐使用 webhook 模式 + Caddy 自动 TLS：

```bash
# 1. 配置 .env
MODE=webhook
WEBHOOK_HOST=https://bot.example.com

# 2. 编辑 Caddyfile，将 bot.example.com 替换为你的域名

# 3. 启动
docker compose up -d
```

架构：`Caddy (80/443) → Bot (8080)`，Caddy 自动签发 HTTPS 证书。

数据持久化在 `./data` 目录（SQLite 数据库）。

---

## 配置参考

### Telegram

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `BOT_TOKEN` | — | Telegram Bot Token |
| `MODE` | `webhook` | `webhook` 或 `polling` |
| `WEBHOOK_HOST` | — | Webhook URL（如 `https://bot.example.com`） |
| `WEBHOOK_SECRET` | — | Webhook 路径密钥 |
| `SUPERADMIN_IDS` | — | 超级管理员 Telegram ID，逗号分隔 |

### MiniMax

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `MINIMAX_API_KEYS` | — | API Key，逗号分隔（多 Key 自动 fallback） |
| `MINIMAX_BASE_URL` | `https://api.minimaxi.com/v1` | API 地址 |
| `MODEL_CHAT` | `MiniMax-M3` | 对话模型 |
| `MODEL_TTS` | `speech-2.8-hd` | 语音合成模型 |
| `MODEL_IMAGE` | `image-01` | 图片生成模型 |
| `MODEL_VIDEO` | `MiniMax-Hailuo-2.3` | 视频生成模型 |
| `MODEL_MUSIC` | `music-2.6` | 音乐生成模型 |

### 联网搜索

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `SEARCH_ORDER` | `minimax,firecrawl,brave,duckduckgo` | 搜索源优先级 |
| `SEARCH_RESULT_COUNT` | `5` | 返回结果数 |
| `FIRECRAWL_API_KEY` | — | Firecrawl API Key |
| `BRAVE_API_KEY` | — | Brave Search API Key |

### 鉴权与配额

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DEFAULT_QUOTA_MODE` | `tokens` | `tokens` 或 `calls` |
| `DEFAULT_QUOTA_LIMIT` | `200000` | 配额上限（`-1` = 无限） |
| `DEFAULT_QUOTA_PERIOD` | `day` | `day` / `month` / `total` |

### 并发与行为

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `MAX_CONCURRENT_CHATS` | `32` | 最大并行会话数 |
| `PER_USER_CONCURRENCY` | `3` | 单用户并行任务数 |
| `EDIT_THROTTLE_MS` | `1000` | 私聊编辑节流间隔 (ms) |
| `GROUP_EDIT_THROTTLE_MS` | `3000` | 群聊编辑节流间隔 (ms) |
| `DEFAULT_TOKEN_BUDGET` | `128000` | 上下文 Token 预算 |
| `DB_PATH` | `./data/bot.db` | SQLite 数据库路径 |

完整配置项见 [`.env.example`](.env.example)。

---

## 使用指南

### 命令一览

| 命令 | 权限 | 说明 |
|------|------|------|
| `/start` `/help` | 所有人 | 欢迎与帮助信息 |
| `/whoami` `/quota` | 授权用户 | 查看自身信息与配额 |
| `/reset` | 授权用户 | 重置当前会话上下文 |
| `/remember` `/memories` `/forget` | 授权用户 | 持久记忆管理 |
| `/image` `/video` `/tts` `/music` | 授权用户 | 显式调用多模态生成 |
| `/search` `/fetch` | 授权用户 | 联网搜索 / 网页抓取 |
| `/grant` `/revoke` `/setquota` | Admin+ | 用户授权与配额管理 |
| `/promote` `/demote` `/broadcast` | Superadmin | 管理员晋升与广播 |

### 常见场景

- **对话**：直接发文字消息，Bot 自动流式回复
- **图片理解**：发送图片 + 文字描述，模型会结合图片内容回答
- **生成图片**：发送 `/image 一只戴墨镜的猫` 或在对话中描述需求
- **生成视频**：`/video 海边日落延时摄影`，后台异步生成，完成后推送
- **搜索**：`/search Python 异步编程最佳实践`，自动 fallback 多个搜索引擎

---

## 架构概览

```
app/
├── main.py              # 入口：webhook / polling 双模式启动
├── config.py            # pydantic-settings 配置解析
├── logging.py           # 中文结构化日志
├── server.py            # aiohttp 服务：/tg/<secret> /mmx/callback /healthz
├── services.py          # 服务容器：组件接线与依赖注入
├── handlers/            # 消息处理
│   ├── private.py       #   私聊消息
│   ├── group.py         #   群聊消息
│   ├── guest.py         #   Guest 模式
│   ├── commands.py      #   斜杠命令
│   ├── inline.py        #   Inline Query
│   ├── media.py         #   图片/视频/音频处理
│   ├── pipeline.py      #   消息处理流水线
│   └── errors.py        #   全局错误处理
├── core/                # 核心能力
│   ├── agent.py         #   Agent 工具调用循环
│   ├── streaming.py     #   流式输出（草稿流 / 编辑流）
│   ├── context.py       #   上下文管理
│   ├── compaction.py    #   上下文自动压缩
│   ├── memory.py        #   持久记忆
│   ├── auth.py          #   授权鉴权中间件
│   ├── quota.py         #   配额控制
│   ├── concurrency.py   #   并发控制与信号量
│   ├── workers.py       #   后台 Worker（视频/音乐异步生成）
│   └── tools.py         #   Agent 工具定义（搜索/抓取/时间等）
├── minimax/             # MiniMax API 封装
│   ├── client.py        #   多 Key fallback HTTP 客户端
│   ├── chat.py          #   对话补全
│   ├── tts.py           #   语音合成
│   ├── image.py         #   图片生成
│   ├── video.py         #   视频生成
│   └── music.py         #   音乐生成
├── search/              # 联网搜索
│   ├── router.py        #   回退链路由器
│   ├── firecrawl.py     #   Firecrawl
│   ├── brave.py         #   Brave Search
│   └── duckduckgo.py    #   DuckDuckGo
├── db/                  # 数据层
│   ├── engine.py        #   aiosqlite 引擎（WAL 模式）
│   ├── models.py        #   数据模型
│   ├── dao.py           #   数据访问对象
│   └── schema.sql       #   DDL + FTS5 全文索引
└── utils/               # 工具函数
    ├── clock.py         #   时间处理
    ├── tokens.py        #   Token 计数
    └── hexaudio.py      #   音频格式转换
```

---

## 多 Key Fallback

支持多个 MiniMax API Key，逗号分隔配置：

```
MINIMAX_API_KEYS=key1,key2,key3
```

**行为**：`key1 → 失败重试 1 次 → key2 → 失败重试 1 次 → key3 → 全部失败报错`

- **Key 级错误**（限流 1002 / 鉴权 1004 / 余额不足 1008 / 超时 / 5xx）→ 重试 + 换 Key
- **请求级错误**（内容敏感 1026 / 参数错误 2013）→ 立即报错，不浪费其他 Key
- 日志逐次记录：Key 序号、脱敏 Key、尝试次数、错误码、耗时、下一步动作

---

## 搜索回退链

联网搜索按配置顺序自动 fallback：

```
Minimax 搜索 → Firecrawl → Brave → DuckDuckGo
```

每家重试 1 次，全部失败才向用户报错。可通过 `SEARCH_ORDER` 自定义顺序。

---

## 测试

```bash
uv sync --extra dev
uv run python -X utf8 -m pytest tests/ -v
```

测试覆盖 29 个模块，包括：

- 多 Key fallback 逻辑（8 用例）
- 并发控制原语
- 鉴权与配额中间件
- 上下文压缩
- 搜索回退链
- 流式输出节流
- Agent 工具调用循环
- 后台 Worker（幂等 / 恢复）
- Server 回调端点
- 消息处理流水线

---

## 故障排查

**Q: Bot 无响应？**
检查 `MODE` 设置。polling 模式下确保没有其他 Bot 实例在运行；webhook 模式下检查 `WEBHOOK_HOST` 是否可达。

**Q: MiniMax 调用全部失败？**
检查 `MINIMAX_API_KEYS` 是否正确配置，Key 是否有余额。Bot 会自动尝试所有 Key 并在日志中记录详细错误。

**Q: 视频/音乐生成后没收到？**
这些是异步任务，生成需要时间。检查日志中 Worker 状态，确保 `WORKER_POLL_INTERVAL_S` 配置合理。服务重启后会自动恢复未完成的任务。

**Q: 群聊中 Bot 太活跃？**
Bot 在群聊中需要被 @提及或回复才会响应。检查 `GROUP_EDIT_THROTTLE_MS` 设置是否合理（默认 3 秒，贴近 Telegram 限流阈值）。

---

## 贡献

欢迎 Issue 和 Pull Request！

- 代码风格：[Ruff](https://github.com/astral-sh/ruff)（`ruff check` + `ruff format`）
- 提交前请确保测试通过：`uv run pytest tests/ -v`
- 建议遵循 [Conventional Commits](https://www.conventionalcommits.org/) 规范

---

## License

[MIT](LICENSE)
