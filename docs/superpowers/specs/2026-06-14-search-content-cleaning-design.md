# 搜索结果敏感内容清洗层(Gemini 中转网关)设计

**日期:** 2026-06-14
**状态:** 待评审

---

## 背景与根因

用户问「今天的新闻以及和 AI 相关新闻」时,机器人对全部 7 个 MiniMax API Key 报错(14 次尝试),最终向用户显示「请检查 Key 配置」。

通过原样重放生产请求体 + 二分定位,确认了**两个独立 bug**(均非代码注释中所猜测的 `content: null`):

1. **真正的触发点:** MiniMax 对**输入**做内容审核,把搜到的时政类摘要(VOA、美中关系等)判为敏感,返回 HTTP 422、错误码 **1026(内容涉及敏感信息)**。
   - 实测证据:同一段时政内容,无论 `content: null`、省略 content、空串、带正文,**全部触发 1026**;换成无害内容(天气)则相同结构返回 200。证明 tool_calls 报文结构完全合法,问题在内容审核。
2. **放大器 bug:** `client.py` 对流式响应直接 `raise_for_status()`,**从不读 422 响应体**,抛出的裸 `HTTPStatusError` 被当成「可重试的网络异常」→ 烧光全部 key(还真把某个 key 打出 429),最后报误导性的「检查 Key 配置」。错误码 1026 本就在 `_NON_RETRYABLE_CODES` 里,但分类逻辑没机会看到它。

搜索引擎侧的 SafeSearch 开关只过滤成人内容(NSFW),对时政敏感**无作用**,因此无法在搜索源解决。

## 目标

1. 让涉及时政等敏感内容的搜索/抓取结果,经一个**境外轻量模型(Gemini Flash Lite,走 OpenAI 兼容中转网关)中性化改写**后,再回灌主模型,从源头避免 1026。
2. 修复 `client.py` 的 1026 误分类,作为兜底防线(独立必修项)。

## 非目标

- 不依赖搜索引擎的 SafeSearch(无效)。
- 不引入 google 官方 SDK(走 OpenAI 兼容网关)。
- 不做真实网关连通性自动化测试(留手动验证)。
- 不改动 `agent.py`(`content: null` 伪修复已由用户手动撤销;实测证明该处无必要)。

---

## 架构(方案 A:工具函数层清洗)

清洗发生在 `pipeline.py` 的 `web_search` / `web_fetch` 出口 —— 即结果字符串「即将回灌给主模型」的那一点。主模型**永远看不到原始敏感文本**。

否决的备选:
- **方案 B(在 SearchRouter 内清洗):** 混淆了「多源回退编排」与「内容清洗」两个关注点,且 `web_fetch` 走另一条 `fetch()` 路径需插两处。
- **方案 C(在 ChatAPI 发请求前清洗整个 payload):** 会无差别改写用户问题与历史,破坏对话,并在无网络内容的请求上浪费 token。

### 组件

新增聚焦模块 `app/core/cleaner.py`,含两部分:

**`GatewayClient`** —— 极简 OpenAI 兼容客户端
- 一次**非流式** `POST {GEMINI_BASE_URL}/chat/completions`,Header 带 `Authorization: Bearer {GEMINI_API_KEY}`。
- 超时 `GEMINI_TIMEOUT_S`,失败**重试 1 次**,仍失败抛 `ProviderError`。
- 复用一个独立 httpx 客户端(与 MiniMax、搜索隔离),支持配置 proxy 备用(本期默认不配)。
- 不引入 google SDK。

**`ContentCleaner`** —— 业务层
- `async clean_search(results: list[SearchResult]) -> str`:中性化改写每条 title/snippet,**并按标准格式**组装为「搜索结果(来源 X):…」字符串返回(格式化收进此处)。
- `async clean_fetch(markdown: str) -> str`:中性化改写整页正文,返回「网页正文(markdown):…」字符串。
- **直通模式:** 未配置 `GEMINI_BASE_URL`/`GEMINI_API_KEY`,或 `CLEAN_SEARCH_RESULTS=false` 时,跳过网关、原样格式化返回(行为与当前完全一致)。
- **兜底:** 内部 `try/except` 全包,任何异常都退回原始格式化结果,绝不抛进 agent 循环;记 warning 日志。

### 接线

- `services.py`:构造一次 `self.cleaner = ContentCleaner(GatewayClient(...), settings)`,与现有 `search_http` 类似新增独立 httpx 或复用。
- `build_dispatcher(...)`:新增 `cleaner` 入参(从 `svc.cleaner` 取),供 `web_search`/`web_fetch` 闭包调用。

### 配置(`config.py` + `.env`,全部有默认值)

```
GEMINI_BASE_URL=                                  # 中转网关地址,如 https://openrouter.ai/api/v1;空=关闭清洗
GEMINI_API_KEY=                                   # 网关 key;空=关闭清洗
GEMINI_MODEL=google/gemini-2.5-flash-lite         # 具体 id 按网关命名填
GEMINI_TIMEOUT_S=8
CLEAN_SEARCH_RESULTS=true                          # 总开关
```

缺省安全:未配 `GEMINI_BASE_URL`/`GEMINI_API_KEY` → 直通模式,系统行为与现状一致。

---

## 数据流

```
用户提问
  └─ 主模型(MiniMax)发起 web_search 工具调用
       └─ pipeline.web_search():
            1. 配额预检
            2. svc.search.search() → 原始结果列表(可能含时政内容)
            3. svc.cleaner.clean_search(results)        ← 清洗点
                 └─ GatewayClient → Gemini:中性化改写
                 └─ 失败 → 退回原始格式化结果(兜底)
            4. 配额结算(search 命中即结算;清洗失败不退费)
            5. 返回【清洗后】标准格式字符串
       └─ 安全文本回灌主模型 → 正常总结 → 用户
```

`web_fetch` 同构:整页 markdown 在返回前过 `clean_fetch()`(此路风险更高,整页正文比摘要更易触发 1026)。

判断:
- **清洗失败不退费** —— 搜索本身成功了,清洗是附加的安全处理。
- **格式化收进 cleaner** —— 「清洗 + 按标准格式回丢」本是一件事,由一处负责。

---

## 错误处理(三层防线)

**第一层 —— 清洗失败退原始结果**
`ContentCleaner` 内部全包异常(网关超时/非 200/空/JSON 失败),退回原始格式化结果,用户无感,记 warning。

**第二层 —— 修 `client.py` 的 1026 误分类(独立必修项,一起合入)**
- `stream_sse` 与 `_once_json` 在 4xx(尤其 422)时**读响应体**,解析 MiniMax 错误结构,提取错误码(`1026`/`2013`/`1042` 等)。
- 命中 `_NON_RETRYABLE_CODES` → 抛 `MiniMaxError(code=...)`,**立即停止**,不换 key、不重试。
- 用户提示从「请检查 Key 配置」改为诚实的「内容被判定为敏感,无法处理」。
- 效果:最坏情况从「14 次失败 + 误导报错」降到「1 次失败 + 正确报错」。

> 注:MiniMax 的 422 响应体形如 `{"error":{"type":"unprocessable_entity_error","message":"input new_sensitive (1026)","http_code":"422"}}`,需从 `error.message` 中解析出 `(1026)`,或兼容 `base_resp.status_code` 结构。实现时以实测响应体为准。

**第三层 —— 开关与缺省安全**
`CLEAN_SEARCH_RESULTS=false` 或未配网关 → cleaner 直通,系统行为与现状一致,不因半配置而崩。

---

## 测试策略

全部用 `httpx.MockTransport` 打桩,离线可跑(与现有 `test_minimax_client.py` 同款)。

**`tests/test_cleaner.py`(新增)**
- `GatewayClient`:正常返回 → 拿到改写文本;超时/500/空 → 抛 `ProviderError`。
- `ContentCleaner.clean_search`:
  - 网关正常 → 返回清洗后的标准格式字符串;
  - 网关失败 → 退回原始格式化结果(断言原文还在);
  - 未配 key / `CLEAN_SEARCH_RESULTS=false` → 直通,MockTransport 零调用。
- `clean_fetch`:正常 / 失败退原文 各一例。

**`tests/test_minimax_client.py`(扩充,回归锁定)**
- `stream_sse` 收到 HTTP 422 + body `input new_sensitive (1026)` → 抛 `MiniMaxError(code=1026)`,且**只调用 1 次**(`rec.calls == ["k1"]`),不烧后续 key。← 复现并锁死本次 bug
- 非流式 `_once_json` 同理。
- 现有 8 个 fallback 用例保持通过(429/500/鉴权仍按 key 级重试)。

**`tests/test_pipeline.py`(新增或扩充)**
- `web_search` 命中后确实调用了 `cleaner`(fake cleaner 断言被调用 + 配额照常结算)。

**验收:** `pytest -v` 全绿 + `ruff check app/ tests/` 无错误。

---

## 变更清单

| 模块 | 动作 |
|---|---|
| `app/core/cleaner.py` | 新建:`GatewayClient`(OpenAI 兼容) + `ContentCleaner`(中性化改写+格式化+兜底直通) |
| `app/handlers/pipeline.py` | `web_search`/`web_fetch` 出口接入 cleaner;`build_dispatcher` 新增 cleaner 入参 |
| `app/minimax/client.py` | 修 1026 误分类:4xx 读 body → 解析错误码 → 非可重试码立即停 |
| `app/services.py` | 构造并注入 `ContentCleaner` + 其 httpx |
| `app/config.py` | 新增 `GEMINI_*` + `CLEAN_SEARCH_RESULTS` 配置项与派生属性 |
| `.env` | 新增上述配置项(留空=关闭清洗) |
| `tests/test_cleaner.py` | 新增 |
| `tests/test_minimax_client.py` | 加 1026 流式/非流式回归用例 |
| `tests/test_pipeline.py` | 新增/扩充 cleaner 接入断言 |

`app/core/agent.py`:**不改**(`content: null` 伪修复已由用户手动撤销)。

---

## 自审记录

- **占位符扫描:** 无 TBD/TODO;配置项均给默认值。
- **内部一致性:** 方案 A 贯穿架构/数据流/变更清单;`clean_search`/`clean_fetch` 签名前后一致。
- **歧义检查:** 「格式化收进 cleaner」「清洗失败不退费」「缺省关闭=直通」三处判断已在正文显式写定。
- **范围检查:** 单一实现计划可覆盖;client.py 修复虽独立但与本主题(同一次 1026 事件)强相关,合入合理。
- **待实现时以实测为准:** MiniMax 422 响应体的精确解析路径(`error.message` vs `base_resp`)需在实现时对照真实响应确定。
