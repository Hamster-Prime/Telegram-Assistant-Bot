# Telegram 助理机器人

支持 **Guest 模式** 的助理型 Telegram Bot:接入 MiniMax **M3** 多模态大模型,集成 **语音合成 / 图片生成 / 视频生成 / 音乐生成**,具备 **联网搜索 / 网页抓取**、**实时时间**、**持久记忆**、**上下文自动压缩**、**鉴权授权**、**配额控制** 与 **多用户并发处理**。

完整设计见 [plan.md](plan.md)。

## 核心特性

- **多 API Key fallback**:`MINIMAX_API_KEYS` 逗号分隔填写多个 key;每个 key 失败后重试 1 次,再失败自动切换下一个,全部失败才向用户报错
- **三场景流式输出**:私聊 `sendMessageDraft` 原生草稿流;群聊/Guest `editMessageText` 模拟流(节流防限流)
- **并发不阻塞**:多用户互不阻塞;同一用户「流式回答 + 后台生成视频 + 新提问」三任务并行
- **后台生成 Worker**:视频/音乐异步生成,回调 + 轮询双路送达,服务重启自动恢复未决任务
- **搜索回退链**:Firecrawl → Brave → DuckDuckGo,每家重试 1 次,全败才报错
- **授权制鉴权**:授权用户全功能全场景;未授权一律 Permission Denied
- **配额双模式**:calls / tokens 计量,day/month/total 周期,惰性重置,并发安全结算
- **中文结构化日志**:`时间 | 级别 | [模块] | 事件 | 键=值 | 键=值`,每项分明,排错友好

## 快速开始

```bash
# 1. 创建虚拟环境并安装依赖
python -m venv .venv
.venv/Scripts/pip install aiogram aiohttp httpx ddgs aiosqlite pydantic-settings structlog tenacity tzdata

# 2. 配置环境变量
copy .env.example .env
# 编辑 .env:填 BOT_TOKEN、MINIMAX_API_KEYS(逗号分隔多个)、SUPERADMIN_IDS 等

# 3. 启动(本地调试用 polling 模式)
#    .env 设 MODE=polling
.venv/Scripts/python -X utf8 -m app.main
```

生产部署用 webhook 模式:`MODE=webhook`,配 `WEBHOOK_HOST`(TLS 由 Caddy/Nginx 反代),Bot 监听 8080 端口。

## 多 Key fallback 行为

```
MINIMAX_API_KEYS=key1,key2,key3
```

调用顺序:`key1 尝试 → 失败重试 1 次 → key2 尝试 → 失败重试 1 次 → key3 …`

- key 级失败(限流 1002 / 鉴权 1004,2049 / 余额不足 1008 / 超时 / 5xx / 429)→ 重试 + 换 key
- 请求级失败(内容敏感 1026 / 参数错误 2013 / 非法字符 1042)→ 立即报错,不浪费其余 key
- 全部失败 → 用户收到:`❌ MiniMax 服务调用失败:已尝试全部 N 个 API Key(每个重试 1 次)…`
- 日志逐次记录:Key 序号、脱敏 Key、尝试次数、错误码、含义、耗时、下一步动作

## 命令一览

| 命令 | 权限 | 说明 |
|---|---|---|
| `/start` `/help` `/whoami` `/quota` | 所有/授权 | 基础信息 |
| `/reset` `/remember` `/memories` `/forget` | 授权 | 会话与记忆 |
| `/image` `/video` `/tts` `/music` `/search` `/fetch` | 授权 | 显式生成/联网 |
| `/grant` `/revoke` `/setquota` `/resetquota` `/quotas` `/users` `/stats` | admin+ | 管理 |
| `/promote` `/demote` `/broadcast` `/audit` | superadmin | 超管 |

## 测试

```bash
.venv/Scripts/python -X utf8 -m pytest tests/ -v
```

覆盖:多 Key fallback(8 用例)、并发原语、鉴权配额、上下文压缩、搜索回退链、流式节流、Agent 工具循环、后台 Worker(幂等/恢复)、server 回调端点。

## 目录结构

```
app/
├── main.py          # 入口:webhook/polling 双模式
├── config.py        # pydantic-settings(多 Key 解析)
├── logging.py       # 中文结构化日志
├── server.py        # /tg/<secret> /mmx/callback /healthz
├── services.py      # 服务容器(组件接线)
├── handlers/        # private/group/guest/commands/media/errors/pipeline
├── core/            # agent/streaming/context/compaction/memory/tools/
│                    # auth/quota/concurrency/workers/ratelimit
├── minimax/         # client(多Key fallback)/chat/tts/image/video/music/files
├── search/          # router(回退链)/firecrawl/brave/duckduckgo
├── db/              # engine(WAL)/models/dao/schema.sql(FTS5)
└── utils/           # clock/tokens/hexaudio/tg_files
```
