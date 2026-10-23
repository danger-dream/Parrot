# 15. WebUI 后端接口（第一阶段）

本文是浏览器 WebUI 与 Parrot 后端之间的接口约定，前端以此为准。所有 HTTP 路径都相对
`/api/management/v1`，沿用管理 API 的 `DataEnvelope{data, meta}`、camelCase、RFC3339 UTC
（`Z` 结尾）时间和错误码体系（错误体 `{"error": {code, message, fields, retryable, requestId}}`）。
分层规则见 `docs/13-management-control-api-refactor.md`。

本轮新增 7 个 HTTP operation（管理 API 共 259 个）和 1 个 WebSocket：

| operationId | 方法与路径 | 认证 |
|---|---|---|
| `getManagementLoginOptions` | `GET /auth/login-options` | 无需登录 |
| `listManagementSessions` | `GET /auth/sessions` | 会话，`management.read` |
| `revokeManagementSession` | `DELETE /auth/sessions/{sessionId}` | 会话，`management.write` |
| `revokeOtherManagementSessions` | `POST /auth/sessions/actions/revoke-others` | 会话，`management.write` |
| `getWebUiSettings` | `GET /settings/web-ui` | 会话，`management.read` |
| `updateWebUiSettings` | `PATCH /settings/web-ui` | 会话，`management.write` |
| `getStatsTimeseries` | `GET /stats/timeseries` | 会话，`management.read` |
| `getStatsCompare` | `GET /stats/compare` | 会话，`management.read` |
| （WebSocket） | `WS /events` | Cookie 或 Bearer |

另有：`POST /auth/sessions` 新增字段，`POST /auth/telegram-approvals` 响应新增 `confirmCode`，
`GET /overview` 新增三个字段，`/ui/` 静态托管。

---

## 1. 配置项 `management.webUi`

```json
{ "management": { "webUi": { "enabled": true, "publicUrl": "" } } }
```

- `enabled` 默认 **true**（新装与从旧版本升级都默认开启；登录仍需 Telegram 批准或管理密钥）。
  缺少该段的旧配置在加载时自动补默认值并写回 `config.json`，不会启动失败；
  已显式写成 `false` 的保持关闭。
- `publicUrl` 是控制台的访问地址，显示在 TG 主菜单和「⚙ 系统设置 → 🖥 WebUI」里。
  留空时按监听地址与公网 IP 推算 `http://<IP>:<端口>/ui/`；经域名或反向代理访问时填写实际地址
  （网页「系统设置 → 网页控制台」可一键「使用当前地址」）。必须是 http(s) 绝对地址，
  不能带账号密码、`?` 或 `#`。
- 值必须是布尔；非布尔值会让管理面初始化失败（与其它 `management.*` 校验一致），
  此时 `/ui` 一律按关闭处理（fail-closed）。
- 切换立即生效，不需要重启：静态托管每个请求都读当前配置。
- 开关入口：`PATCH /settings/web-ui`，以及 TG「⚙ 系统设置 → 🖥 WebUI」。两者调用同一个
  control 用例 `SettingsControl.update_web_ui`。

### `GET /settings/web-ui` / `PATCH /settings/web-ui`

响应 `data`：

```json
{ "enabled": true, "publicUrl": "", "revision": "rev_3c1f…" }
```

PATCH 请求体（稀疏，未知字段 422）：`{"enabled": true}`、`{"publicUrl": "https://example.com/ui/"}`
（空字符串表示清除）。支持 `If-Match: <revision>`，不一致返回 409 `REVISION_CONFLICT`；空体、
非布尔、不合法地址返回 422 `VALIDATION_FAILED`。每次写入都记审计 `settings.web-ui.update`。

---

## 2. 静态托管 `/ui/`

构建产物放在 `src/webui/`（含 `index.html` 和 `assets/`）。

| 请求 | 开启时 | 关闭时 |
|---|---|---|
| `GET /` | 307 → `/ui/` | 404（保持原状） |
| `GET /ui` | 307 → `/ui/` | 404 |
| `GET /ui/`、`/ui/index.html` | `index.html`，`Cache-Control: no-cache` | 404 |
| `GET /ui/assets/<file>` | 文件，`Cache-Control: public, max-age=31536000, immutable`；不存在 → 404 | 404 |
| `GET /ui/<其它路径>` | 文件存在则返回（`no-cache`），否则返回 `index.html`（SPA 回退） | 404 |

- 关闭时的 404 与框架默认 404 完全相同（`{"detail":"Not Found"}`）。
- 开启但 `src/webui/index.html` 不存在：404，`text/plain`，正文
  `Parrot WebUI is enabled, but its build output (src/webui) is not installed.`
- 只响应 GET/HEAD；其它方法一律 404（与之前无路由时一致）。路径穿越被拒（解析后必须位于
  `src/webui` 内，否则走 SPA 回退）。
- 推理鉴权只在 `/v1/*` 等推理路由内执行；CORS、drain、管理 Origin 中间件都不拦截 `/ui`
  （已用真实 `server.app` 中间件栈测试）。

---

## 3. 会话：Cookie 传输与同源放行

### `POST /auth/sessions` 新字段

两种 grant 都可带 `sessionTransport`：`"bearer"`（默认）或 `"cookie"`。`managementKey` grant
另可带 `clientName`（1–80 字符）、`deviceSummary`（1–160 字符），用于登录设备列表。
`telegramApproval` grant 的设备信息取自创建批准时的记录，不接受这两个字段。

```json
{
  "grantType": "telegramApproval",
  "approvalId": "map_…",
  "exchangeSecret": "max_…",
  "sessionTransport": "cookie"
}
```

cookie 模式响应（201）：`credential` 为 **null**，会话凭据只存在于 HttpOnly cookie 中：

```json
{
  "data": {
    "credential": null,
    "session": {
      "sessionId": "ms_…", "subjectId": "administrator", "authMethod": "telegramApproval",
      "roles": ["administrator"], "capabilities": ["management.destructive", "…"],
      "issuedAt": "2026-10-22T07:31:36.123456Z",
      "expiresAt": "2026-11-21T07:31:36.123456Z",
      "idleExpiresAt": "2026-10-25T07:31:36.123456Z"
    }
  },
  "meta": { "requestId": "…" }
}
```

### Cookie 规则

```
Set-Cookie: parrot_management_session=<credential>; HttpOnly; Max-Age=<绝对过期剩余秒数>;
            Path=/api/management; SameSite=strict[; Secure]
```

- `Secure`：请求为 https，或 `X-Forwarded-Proto` 第一个值为 `https` 时添加。
- `Max-Age`：会话绝对过期时间减当前时间（默认约 30 天）。空闲过期（默认 3 天无请求）
  仍由服务端判断。
- `Path=/api/management` 覆盖 REST 与 `WS /api/management/v1/events`；`/ui` 页面本身拿不到。
- `DELETE /auth/session`：吊销当前会话；请求带有该 cookie 时同时清除它
  （`Max-Age=0`，同 Path）。用 `DELETE /auth/sessions/{自己的 sessionId}` 吊销自己时同样清除。
- 会话过期或被吊销时服务端**不**主动清 cookie；之后请求返回 401
  `SESSION_EXPIRED`/`SESSION_REQUIRED`，前端回登录页即可（下次登录会覆盖 cookie）。

### 认证顺序与 CSRF

1. 有 `Authorization: Bearer …` 时只用 Bearer（行为与以前完全相同）。
2. 否则读 `parrot_management_session` cookie。
3. cookie 认证的 **非 GET/HEAD** 请求必须带 `Origin`，且 Origin 通过下面的检查，否则 403
   `ORIGIN_DENIED`。浏览器 `fetch` 发写请求会自动带 Origin，前端无需额外处理。

### Origin 检查（HTTP 中间件、cookie 写请求、WebSocket 共用）

带 `Origin` 的请求满足任一条件即允许：

- Origin 在 `management.allowedOrigins` 中；或
- Origin 的 `host[:port]` 等于请求的 `Host` 头，或等于 `X-Forwarded-Host` 的第一个值
  （按 Origin 的 scheme 归一默认端口，例如 `http://example.test` 与 `Host: example.test:80` 相同）。

其它 Origin（含 `null`）一律 403 `ORIGIN_DENIED`。不带 Origin 的请求（CLI、Bearer）不受影响。
反向代理需要透传 `Host` 或设置 `X-Forwarded-Host`。

---

## 4. `GET /auth/login-options`（无需登录）

```json
{
  "data": {
    "webUiEnabled": true,
    "telegramApproval": { "available": true, "ttlSeconds": 180 },
    "managementKey": { "available": true }
  },
  "meta": { "requestId": "…" }
}
```

- `telegramApproval.available`：已配置 TG Bot token 且至少有一个管理员 id。
- `ttlSeconds`：批准有效期（`management.telegramApprovalTtlSeconds`）。
- `managementKey.available`：管理面运行即为 true（管理密钥是管理面必需项）。
- 前端约定：平时只显示 TG 登录；只有 `telegramApproval.available=false`，或
  `POST /auth/telegram-approvals` 返回 503 `DEPENDENCY_UNAVAILABLE` 时，才显示管理密钥登录。
- 响应 `Cache-Control: no-store`。管理面未就绪时 503 `SERVICE_NOT_READY`。

---

## 5. Telegram 批准确认码

`POST /auth/telegram-approvals` 响应新增 `confirmCode`：

```json
{
  "data": {
    "approvalId": "map_…",
    "exchangeSecret": "max_…",
    "expiresAt": "2026-10-22T07:34:36.123456Z",
    "pollAfterSeconds": 2,
    "confirmCode": "7KQ2"
  },
  "meta": { "requestId": "…" }
}
```

- 4 位，字符集 `23456789ABCDEFGHJKLMNPQRSTUVWXYZ`（无 0/1/O/I），存入 approvals 表
  `confirm_code` 列。
- TG 批准消息醒目显示：`确认码：<b><code>7KQ2</code></b>`，下一行
  「请确认与网页上显示的一致」。前端应在等待批准页面大号显示该确认码。

---

## 6. 登录设备

sessions 表新增 `client_name`、`source_address`、`device_summary`（旧库启动时自动
`ALTER TABLE` 迁移，旧会话这三项为 null）。TG 批准换来的会话复制批准记录中的三项；
管理密钥换来的会话用请求来源 IP 和 grant 里的可选字段。

### `GET /auth/sessions`

列出未吊销、未过期（绝对与空闲）且属于当前凭据代的会话，按最近使用时间倒序：

```json
{
  "data": [
    {
      "sessionId": "ms_…",
      "authMethod": "telegramApproval",
      "clientName": "Parrot WebUI",
      "deviceSummary": "Chrome on macOS",
      "sourceAddress": "203.0.113.7",
      "issuedAt": "2026-10-22T07:31:36.123456Z",
      "lastSeenAt": "2026-10-22T07:40:00.000000Z",
      "expiresAt": "2026-11-21T07:31:36.123456Z",
      "idleExpiresAt": "2026-10-25T07:40:00.000000Z",
      "current": true
    }
  ],
  "meta": { "requestId": "…" }
}
```

`current` 表示是否为发起本次请求的会话。`lastSeenAt` 按 `sessionTouchIntervalSeconds`
（默认 5 分钟）粒度更新。

### `DELETE /auth/sessions/{sessionId}`

204。会话不存在/已失效返回 404 `RESOURCE_NOT_FOUND`。吊销的是自己且请求带 cookie 时清除 cookie。

### `POST /auth/sessions/actions/revoke-others`

吊销除当前会话外的所有会话：`{"data": {"revoked": 2}, "meta": {…}}`。

以上三个接口都写审计：`session.list`、`session.revoke`（`succeeded`/`not-found`）、
`session.revoke-others`。cookie 认证时 DELETE/POST 需满足第 3 节的 Origin 规则。

---

## 7. `GET /stats/timeseries`

| 参数 | 取值 | 说明 |
|---|---|---|
| `range` | `1h` `24h` `7d` `30d` | 必填 |
| `bucket` | `minute` `hour` `day` | 可选；默认 1h→minute、24h→hour、7d→hour、30d→day |
| `groupBy` | `none` `model` `channel` `apiKey` | 默认 `none` |
| `limit` | 1–12 | 默认 6，分组时取前 N |

桶数 = ⌈range / bucket⌉，超过 800 返回 422 `VALIDATION_FAILED`（`fields[].path = "bucket"`，
例如 `24h+minute`=1440、`30d+minute`）。未知参数、非法枚举同样 422。

```json
{
  "data": {
    "range": "24h",
    "bucket": "hour",
    "bucketSeconds": 3600,
    "from": "2026-10-21T08:00:00Z",
    "to": "2026-10-22T08:00:00Z",
    "timezone": "Asia/Shanghai",
    "series": [
      {
        "key": "claude-sonnet-4",
        "label": "claude-sonnet-4",
        "points": [
          {
            "t": "2026-10-21T08:00:00Z",
            "total": 12, "successCount": 11, "errorCount": 1,
            "inputTokens": 5400, "outputTokens": 2100,
            "cacheReadTokens": 80000, "cacheCreationTokens": 1200,
            "costTicks": 1234567890,
            "averageFirstTokenMilliseconds": 812.5,
            "averageTotalMilliseconds": 6230.0
          }
        ]
      },
      { "key": "__other__", "label": "其他", "points": [ … ] }
    ],
    "revision": "rev_…"
  },
  "meta": { "requestId": "…" }
}
```

- 窗口：最后一个桶包含当前时刻（`to` 为当前桶结束，可能略晚于现在），`from = to - 桶数×bucketSeconds`；
  `t` 为桶起点。点连续，空桶各计数为 0、两个平均值为 null。
- 时区：与现有统计的「今日/本月」一致，使用固定 UTC+8（返回 `Asia/Shanghai`，该时区无夏令时，
  两者等价）。日桶边界是北京时间 0 点（即 UTC 16:00）；分钟/小时桶与 UTC 对齐（整小时偏移，结果相同）。
- `groupBy=none` 只有一个 `key="total"`（`label="全部"`）的序列。分组时按窗口内请求量降序（同量按 key）
  取前 `limit` 个，其余合并为 `key="__other__"`（`label="其他"`，只在有剩余时出现）。分组 key：
  model=请求模型 `requestedModel`，channel=渠道 key（如 `api:xxx`、`oauth:…`），apiKey=API Key 名；
  缺失值为 `?`。
- 口径与 `GET /stats/summary` 相同：`errorCount` 只计 `status=error`（cancelled/pending 只计入 `total`）；
  Token 在有上游尝试账本时按所有已观测尝试计（与 summary 的替换规则一致）；费用优先用尝试账本的
  实际/估算结算，无账本的旧请求按价格表估算，xAI OAuth 优先取响应中的实际费用。费用单位 tick，
  `1 USD = 10_000_000_000 ticks`。按渠道分组时，尝试费用/Token 计入尝试实际使用的渠道。
- `averageFirstTokenMilliseconds`：成功的流式请求首字时间均值；`averageTotalMilliseconds`：成功请求
  总耗时均值。
- 跨月：自动读取窗口覆盖到的历史月库（只读）。在线程池中执行，不阻塞事件循环；
  单月 8 万请求（一半带尝试账本）下 30d 查询实测约 0.6 s。

---

## 8. TG「🖥 WebUI」入口

「⚙ 系统设置」新增一行「🖥 WebUI」（`sys:show:webui`）。页面显示当前状态、访问地址
`http://<监听地址>:<端口>/ui/`（监听 `0.0.0.0`/`::` 时显示 `<服务器IP>` 并提示替换）、
「登录需在 Telegram 中点击批准」，以及「🟢 开启 WebUI / 🔴 关闭 WebUI」（`sys:webui:toggle`）
和「◀ 返回系统设置」按钮。切换调用与 `PATCH /settings/web-ui` 相同的 control 用例。

---

## 9. 实时事件 `WS /api/management/v1/events`

### 连接与认证

- 浏览器：`new WebSocket("wss://host/api/management/v1/events")`，由 cookie 认证；
  也接受 `Authorization: Bearer <credential>`（非浏览器客户端）。
- 握手时校验 Origin（第 3 节规则）。cookie 认证但没有 Origin 视为跨站劫持，拒绝。
- 服务端最多 20 个并发连接。

### 关闭码

| 关闭码 | 含义 |
|---|---|
| 4401 | 未认证、凭据无效，或连接期间会话过期/被吊销（约每 15 秒复核一次，reason `session expired`） |
| 4403 | Origin 不允许 |
| 1013 | 连接数已满或管理面未就绪，稍后重连 |
| 1001 | 服务端关闭（重启/停机） |

拒绝也是先 accept 再关闭，浏览器能拿到上述关闭码（而不是 1006）。客户端发来的消息被忽略。

### 消息格式

```json
{ "type": "request.started", "ts": "2026-10-22T07:31:36.123456Z", "data": { … } }
```

| type | data |
|---|---|
| `hello` | `{serverTime, version, sessionExpiresAt}`；`sessionExpiresAt` = min(绝对过期, 空闲过期) |
| `request.started` | `{id, createdAt, apiKeyName, requestedModel, protocol, isStream, clientIp, reasoningEffort, fastMode}` |
| `request.updated` | `{id, channelId?, model?, retryCount?}`（重试/换渠道，尽力而为；换代理路由时只有 `id`） |
| `request.finished` | `{log: RequestLogData}`，与 `GET /logs` 列表项完全相同，可直接 upsert |
| `runtime.snapshot` | 与 `GET /runtime/concurrency` 的 `data` 完全同形，每 2 秒一次 |
| `resync` | `{reason}`：`overflow`（该连接队列溢出，事件已丢弃）或 `enrichmentFailed`（读库补全失败）；收到后重新拉数据 |
| `ping` | `{}`，约每 20 秒一次 |

样例：

```json
{"type":"hello","ts":"2026-10-22T07:31:36.2Z","data":{"serverTime":"2026-10-22T07:31:36.200000Z","version":"0.35.0","sessionExpiresAt":"2026-10-25T07:31:36.123456Z"}}
{"type":"request.started","ts":"…","data":{"id":"5f0c…","createdAt":"2026-10-22T07:31:37.010000Z","apiKeyName":"cli","requestedModel":"claude-sonnet-4","protocol":"anthropic","isStream":true,"clientIp":"203.0.113.5","reasoningEffort":"high","fastMode":false}}
{"type":"request.updated","ts":"…","data":{"id":"5f0c…","channelId":"oauth:claude:…","model":"claude-sonnet-4","retryCount":1}}
{"type":"request.finished","ts":"…","data":{"log":{"id":"5f0c…","status":"success","createdAt":"…","apiKeyName":"cli","requestedModel":"claude-sonnet-4","finalModel":"claude-sonnet-4","channelId":"oauth:claude:…","protocol":"anthropic","transport":"http","retryCount":1,"durationMilliseconds":6230,"inputTokens":5400,"outputTokens":2100,"costTicks":1234567890,"billing":{…},"error":null,"revision":"rev_…"}}}
{"type":"runtime.snapshot","ts":"…","data":{"channelTotals":{"inFlight":1,"waiting":0,"trackedChannels":3},"channels":[…],"apiKeyTotals":{…},"apiKeys":[…],"revision":"rev_…"}}
{"type":"resync","ts":"…","data":{"reason":"overflow"}}
{"type":"ping","ts":"…","data":{}}
```

`request.updated` 的 `retryCount` 为本次尝试序号减一（第一次尝试为 0）。`request.started`
只在请求日志落库后发出；被拒绝在落库之前的请求（鉴权失败等）不会出现。

### 实现要点（供排障）

- 进程内总线 `src/management_control/observability/live_events.py`；请求路径钩子
  `src/request_events.py`。无订阅者时 `log_db` 只做一次元组真值判断，不构造负载。
- 有订阅者时请求线程只投递 id 和已有字段（`loop.call_soon_threadsafe`，O(1)、不阻塞、不抛错）。
  `request.finished` 的完整 `RequestLogData` 由总线自己的任务在线程中读库补全（50 ms 合批）。
- 每个连接一个有界队列（256 条），满了丢弃已排队事件并只留一条 `resync`。

---

## 10. `GET /overview` 新增字段

只追加，原有字段不变：

```json
{
  "publicIp": "203.0.113.10",
  "telegramBot": { "configured": true },
  "update": { "available": true, "latestVersion": "v0.36.0" }
}
```

- `publicIp`：TG 主菜单使用的同一缓存出口 IP（`public_ip` 后台任务），未获取到为 null。
- `telegramBot.configured`：已配置 Bot token 且至少一个管理员 id。
- `update`：只读 `update_checker` 已缓存的后台检查结果，不触发网络请求。`available` 与 TG 主菜单
  更新提示规则一致（最新版本高于当前版本且未被忽略）；`latestVersion` 为缓存的最新版本号，未检查过为 null。

---

# 第二轮（2026-10-22）

本节只记录第二轮新增/变化的接口与行为；未提到的接口形状不变。路径前缀均为 `/api/management/v1`。

## R2-0 统计查询不占用事件循环 + 统计缓存

- 管理 API 中所有 `async def` 路由只通过 `run_in_threadpool`（anyio 工作线程）调用 control；会话校验依赖
  `get_authenticated_session` 改为同步依赖（同样在工作线程执行）。原来用 `asyncio.to_thread` 的地方全部换成
  `run_in_threadpool`，不再占用推理请求日志写入所用的 asyncio 默认线程池。实时事件总线的补全读取同理。
  唯一的例外是渠道的 4 个 `discover/probe/refresh` 路由：它们只在内存里登记一个 Operation，实际工作在 Operation
  worker 中执行。`src/tests/test_webui_round2_backend.py` 用 AST 门禁锁定这条规则。
- 新增进程内统计缓存 `src/management_control/stats_cache.py`（stale-while-revalidate）：
  - 15 秒内直接返回；15 秒～10 分钟返回旧值并在后台单线程刷新；超过 10 分钟或无值时同步计算；
  - 同一个 key 并发请求只计算一次（single-flight），同时最多 2 个统计计算并行；
  - 当月 / 今日 / 全部时间窗口的 key 带起始时间戳，跨日、跨月不会返回上一窗口；
  - 已封存的月份由 `log_db` 原有的按文件签名缓存负责，长期有效。
- 走缓存的读取：`getStatsSummary` / `getStatsBreakdown` / `getModelStats`（同一份 period 快照）、
  `getStatsTimeseries`、`/overview` 的今日与累计统计、`listApiKeys` / `getApiKey` / `getApiKeyStats`、
  OAuth 账号详情的当月统计。统计口径不变：代码只决定什么时候调用原有的 `log_db` 聚合函数。
  Telegram 仍走原来的读取路径和它自己的菜单缓存。
- 服务启动 20 秒后会在后台预热当月快照。环境变量 `PARROT_STATS_CACHE=0` 可以关闭缓存（测试套件会设置）。
- 开发环境实测（13.6 万条日志）：
  - 冷启动第一次 `GET /api-keys` 约 4.8 秒，之后约 5 毫秒；
  - `getModelStats` 7 天 / 本月、`period=lifetime` 的 summary 和 breakdown，缓存命中后约 7 毫秒；
  - 10 个重统计请求并发进行时，`/health` 中位数 1.2 毫秒、最大 128 毫秒，`/v1/models` 最大 46 毫秒，
    推理请求延迟 3～5 毫秒（假令牌环境，返回 503）。

## R2-1 OAuth 登录流程轮询与取消（缺陷修复）

`device_for` 改为按前缀分派：`wbflow_` 交给 WorkBuddy，`zhflow_` 交给智谱，`qdflow_` 交给 Qoder，
`oflow_`（Claude / OpenAI / Grok / Cursor / Antigravity）交给新的浏览器流程状态记录。未知前缀返回 400
`INVALID_OPERATION_STATE`。

- `pollOAuthLoginFlow` 对 `oflow_*` 返回 `status`：
  - `pending`：已发起，等待提交回调或授权码；
  - `confirmed`：已提交授权码，正在保存；
  - `completed`：已完成，返回 `accountId` / `saveStatus` / `revision`；
  - `failed`、`cancelled`、`expired`。
- `cancelOAuthLoginFlow`：`pending` 状态下作废一次性流程，返回 204。重复取消，或对已失败、已过期的流程取消，
  同样返回 204；对已完成的流程取消返回 400 `INVALID_OPERATION_STATE`。
- 状态记录只保存 actor 和 flowSecret 的摘要，不保存凭据。

## R2-2 平台专属接口在上游失败时不再返回 500（缺陷修复）

- 根因：同步 provider 代码在 `except RuntimeError`（`asyncio.get_running_loop()` 探测）分支里执行
  `asyncio.run(...)`，导致 provider 的异常被链接到 “no running event loop” 上，最终以 500 抛出。
  已在 `src/oauth/zhipu/common.py` 和 `oauth/account_orchestration.py` 中把执行移到 `except` 之外。
- 智谱的 `getOAuthZhipuResetStatus`、`startOAuthZhipuProjectSelection`、`selectOAuthZhipuProject`、
  `planOAuthZhipuAction`、`executeOAuthZhipuAction`，以及 Qoder 的 `confirmOAuthQoderDevice`、
  `regenerateOAuthQoderDevice`、`refreshOAuthQoderStatus`、`checkinOAuthQoderAccount`，在 HTTP 适配层统一套上
  `call_upstream`。provider 抛出的异常映射为：
  - 超时：502 `UPSTREAM_TIMEOUT`；
  - 网络被禁用：503 `DEPENDENCY_UNAVAILABLE`；
  - 其他：502 `UPSTREAM_ERROR`。
  Telegram 仍收到 provider 原始异常，所以 TG 文案不变。
- 同类排查：WorkBuddy 的计划与执行走 Operation，已有错误映射；新增的 Claude 重置、OpenAI 重置卡实时查询也走同一套映射。

## R2-3 失效账号判定统一（缺陷修复）

`listInvalidOAuthAccounts`、账号摘要的 `invalid` 字段、`planInvalidOAuthAccountDeletion`（包括 `all:true`）
共用同一个判定（`contracts.invalid_account`）。判定与 TG「移除失效账户」一致：
`disabled_reason == "auth_error"`，并且账号有 e-mail，或者 provider 是 WorkBuddy / 智谱 / Qoder。

## R2-4 OpenAI 协议渠道的最大并发（缺陷修复）

`OpenAIApiChannel` 现在读取 `maxConcurrent`，语义与 Anthropic API 渠道相同：0 表示使用
`concurrency.defaultMaxConcurrent`，仍为 0 则不限。`getChannel` / `listChannels` 返回真实值，并发也会按它执行限流。
TG 渠道编辑页的「⚡ 并发上限」随之显示真实值（例如“（7）”）。对应 TG 合同用 webui overlay 覆盖了
`TG-CH-04.edit-fields-success` 这一条用例，只有这个按钮文本变化。

## R2-5 手动检查更新不受自动检查开关影响（行为变化）

`checkForUpdates`（API）和 TG「🔄 立即检查」在 `updates.enabled=false` 时也会联网检查。自动检查关闭时，
手动检查不推送通知，也不触发自动更新，结果只显示在发起检查的页面上。后台轮询仍受开关控制。TG 文案和按钮不变。

## R2-6 手动运行网络检测不受总开关影响（行为变化）

`runNetworkMonitor`（API）和 TG「立即检测」在总开关关闭时，也会按已配置的检测项执行一次并返回结果。
总开关关闭时，这次结果只用于展示：不写入状态库（不会出现主菜单红色 banner），也不发通知。

## R2-7 Claude 官方额度重置（新增）

| operationId | 方法 / 路径 | 能力 |
|---|---|---|
| `planOAuthClaudeReset` | `POST /oauth/accounts/{accountId}/claude-reset/plans` | destructive |
| `executeOAuthClaudeReset` | `POST /oauth/accounts/{accountId}/claude-reset/execute` | destructive |

- plan 请求：`{"program": "cedar_ember" | "juniper_tide"}`。`cedar_ember` 是周额度重置卡，`juniper_tide` 是 5 小时重置。
  这一步只读取上游状态，返回：
  ```json
  {"available": true, "reason": null, "program": "cedar_ember", "planToken": "...",
   "expiresAt": "...", "grantId": "grant-1", "status": {}}
  ```
  不可用时 `available=false`、`planToken=null`，`reason` 说明原因（与 TG 相同）。
- execute 请求：`{"planToken": "..."}`。这一步相当于 TG 的「最终确认」加实际消耗请求，一次完成。plan 只能使用一次，
  并且绑定账号的 revision；失败不会自动重试。返回 `program`、`grantId`、`result`、`reason`、`resetsLeft`、
  `cleared`、`cooldownUntil`、`nextAvailableAt`、`weeklyResetsAt`、`quotaAction`。
- 非 Claude 账号返回 `UNSUPPORTED_VALUE`；`program` 取值非法返回 `VALIDATION_FAILED`；上游失败按 R2-2 映射。

## R2-8 OpenAI 重置卡与积分开关（新增 / 扩展）

- 新增 `listOAuthOpenAiResetCredits`：`GET /oauth/accounts/{accountId}/openai/reset-credits?refresh=false`。
  - 默认读取缓存（`source:"cache"`），只需 read 权限；`refresh=true` 实时向上游查询（`source:"live"`），需要 write 权限。
  - 返回：
    ```json
    {"availableCount": 1, "fetchedAt": "...", "cards": [
      {"creditId": "...", "status": "...", "resetType": "...", "grantedAt": "...", "expiresAt": "..."}]}
    ```
- `planOAuthQuotaReset` 的请求体新增可选字段 `creditId`（仅 OpenAI），响应中原样带回 `creditId`。
  `resetOAuthQuota` 执行时使用这张卡；不传则由上游使用默认卡。非 OpenAI 账号传 `creditId` 返回 `UNSUPPORTED_VALUE`。
- `updateOAuthAccount` 新增 `allowCredits: bool`（仅 OpenAI，写入配置字段 `allowCredits`）。设为 false 时立即重新执行额度闸门，
  与 TG 一致；设为 true 时，要等下一次刷新用量后才会解除暂停。非 OpenAI 账号返回 `UNSUPPORTED_VALUE`。
  `getOAuthAccount` 新增 `allowCredits`：OpenAI 账号为 bool，其他平台为 null。

## R2-9 新增账号选代理、原账号重新登录（扩展）

- TG 的情况：「🔐 重新登录」对所有平台都可用（`oauth_menu.on_relogin`）；新增账号页的代理选择对所有平台都可用，
  重新登录时沿用原账号的路由。
- `startOAuthLoginFlow`：
  - `reloginAccountId` 现在支持所有平台。目标账号必须属于同一 provider，并且已经认证失效（Qoder 除外，与原来一致）；
    否则返回 `INVALID_OPERATION_STATE`。完成登录时，如果返回的身份与原账号不同，返回 `IDENTITY_CONFLICT`，不会覆盖原账号。
  - `proxyTarget` 现在支持所有平台的新增账号，取值为代理名或代理组名，校验方式与 Qoder 相同；目标未知返回
    `VALIDATION_FAILED`（字段 `proxyTarget`）。
  - 同时传 `proxyTarget` 和 `reloginAccountId` 返回 422。

## R2-10 结构化用量明细（扩展）

`getOAuthAccount` 新增两个字段：

```json
"usageFetchedAt": "2026-10-22T09:00:00Z",
"usageSections": [
  {"id": "windows", "title": "用量窗口", "items": [
    {"label": "5 小时", "usedPercent": 12.0, "remainingPercent": 88.0, "used": null, "limit": null,
     "remaining": null, "unit": null, "resetsAt": "2026-10-22T12:00:00Z", "note": null}]}
]
```

- 只从缓存的用量行和平台快照构建，不发网络请求。缺失的数值为 null。
- `unit` 取值为 `usd` / `credits` / `count`，对应 `used` / `limit` / `remaining` 的单位。
- section 按平台出现：
  - `windows`：5 小时 / 7 天 / 30 天 / Sonnet 7 天 / Opus 7 天 / Fable 7 天；
  - Claude：官方重置（周卡、5 小时重置）；
  - OpenAI：额外额度族、积分余额、spend control、重置卡数量；
  - Claude / OpenAI：额外用量（extra usage）；
  - Cursor：包含额度 / Auto / API 分池、spend limit；
  - Antigravity：按分组的各窗口；
  - xAI：账单；
  - 智谱：套餐与重置卡。
- 原来的 5 小时 / 7 天 / 30 天三个窗口字段保留不变。

## R2-11 更新状态查询（新增）

| operationId | 方法 / 路径 | 能力 |
|---|---|---|
| `getUpdateState` | `GET /updates/state` | read |
| `getCachedUpdateCheck` | `GET /updates/check` | read |
| `createUpdateActivationPlan` | `POST /updates/staged/actions/activation-plan` | update，必须带 If-Match |

- `getUpdateState` 返回：
  ```json
  {"stage": "staged", "mode": "git|docker|...", "fromVersion": null, "toVersion": "v0.36.0",
   "targetVersion": "v0.36.0", "message": null, "repository": "danger-dream/Parrot",
   "currentVersion": "0.35.0", "busy": false, "activationAvailable": true, "revision": "..."}
  ```
- `getCachedUpdateCheck` 返回上次检查的结果，形状与 `checkForUpdates` 相同，包含更新说明 `changelog`，不访问 GitHub。
- 激活凭据的重新获取：刷新页面或换浏览器后没有 `stageUpdate` 返回的 token。此时用 `getUpdateState.revision` 作为
  If-Match 调用 `createUpdateActivationPlan`，可以拿到绑定当前 actor 的新 `planToken`，然后照常调用 `activateStagedUpdate`。
  - 签发新 token 会作废之前的 token；
  - 未处于 staged 状态返回 `INVALID_OPERATION_STATE`；
  - revision 不符返回 `REVISION_CONFLICT`；
  - 缺少 If-Match 返回 `VALIDATION_FAILED`。

## R2-12 日志保留计划的按库明细（扩展）

保留计划响应（logs 和 bodies 两种范围）新增 `items[]`：

```json
{"month": "2026-09", "database": "2026-09.db", "action": "delete_file|trim_and_vacuum|purge_bodies",
 "totalRows": 50, "deletedRows": 20, "deletedCallRows": 3, "currentBytes": 8192,
 "releasableBytes": null, "willCompact": true, "compactionRequiredBytes": 9000, "note": null, "active": false}
```

- `releasableBytes` 只有整库删除（`delete_file`）时才是精确值。裁剪的库只有压缩后才会释放空间，此时为 null，
  由 `willCompact` 说明是否计划压缩。
- 无法识别动作的条目会被略过，不会填入猜测的值。

## R2-13 后台任务运行记录（扩展）

`listBackgroundJobs` 的 `lastRunAt`、`nextRunAt`、`status`（`running` / `succeeded` / `failed` / `unknown` / `disabled`）
和 `error`（异常类型名）来自进程内的 `src/job_telemetry.py`，重启后清空。

- 已接入的任务：`walCheckpoint`、`pendingCleanup`、`affinityCleanup`、`oauthRefresh`、`quotaMonitor`、
  `oauthModelSync`、`cooldownProbe`。
- 本进程内还没有运行过的任务为 `unknown`，`nextRunAt` 显示首次计划时间。
- `providerUsage` 没有独立循环，保持 `unknown`。

## R2-14 渠道向导的测试结果进入冷却（行为变化）

- `createChannel`（manual 和 preset 两种）新增 `initialProbeResults: [{model, ok, reason?}]`。`model` 必须是该渠道的
  `models[].real`，并且不能重复，否则返回 422 `VALIDATION_FAILED`（字段 `initialProbeResults`）。
  `ok=false` 的模型会在新建的正式渠道上记录错误并进入冷却，与 TG「不可用（已加入冷却）」一致。
- `probeChannelDraft` 不再在临时的 `api:<名称>__wiz` 上记录冷却。前端把每个模型的探测结果原样放进 `createChannel` 即可。

## R2-15 常用管理页面的接口性能

见 R2-0。`GET /api-keys`、`/api-keys/{id}`、`/api-keys/{id}/stats`、`/stats/breakdown?period=month` 共用同一份
当月快照缓存。只有服务冷启动后、预热完成前的第一次请求需要完整计算，大约 4～5 秒。

## R2-16 元数据列表的真实 scope

`listModelMetadata` 中由账号目录生成的绑定（例如 Cursor 账号按自身目录得到的 `claude-fable-5` 300K），现在返回真实的
`scope`（`oauth` / `api`）、`scopeId`（公开账号 id 或渠道 id）和 `outboundModel`，不再显示为 `global` / null。

## R2-17 删除匹配后恢复自动匹配（行为变化）

`deleteModelMetadataBinding` 的 `scope=global` 删除后，会立即按已发布的目录重新做一次自动匹配，与 TG「恢复自动匹配 / 继承」
一致。删除账号或渠道范围的绑定后，回到继承全局绑定，这部分行为不变。

## R2-18 推理强度与渠道顺序来源（新增 / 扩展）

- `getModelRegistry` 新增 `globalDefaultEffort`，即全局默认推理强度的当前值（TG「跟随全局（…）」中显示的值）。
- 新增 `listModelChannelOrders`：`GET /load-balancing/model-orders?source=modelOverride|channelDefault`。
  一次返回所有模型的 `{modelId, order, source, revision}`。不带 `source` 时返回全部，带上则按来源过滤。
  响应形状与 `bulkReplaceModelChannelOrders` 相同，为 `{orders: [...], revision}`。

## R2-19 搜索测试结果与可选模型（扩展 / 新增）

- `testSearchBackend` 的 Operation 结果新增 `results: [{title, url}]`，最多 5 条。url 中的 userinfo 凭据会被去掉；
  title 最长 300 字，url 最长 2048 字。审计日志里仍然不记录查询词和结果。
- 新增 `getSearchBackendModels`：`GET /search/backends/{backendId}/models`，对应 `SearchControl.models()`，返回：
  ```json
  {"backendId": "...", "supported": true, "selected": "...", "default": "gpt-5.5", "models": ["..."]}
  ```
  API key 类型的搜索源没有模型维度，返回 `supported=false`，`models` 为空。

## R2-20 MCP 调用结果正文（新增）

`getMcpCallResult`：`GET /mcp/logs/{callId}/result`，需要 `management.logs.body.read` 权限（与请求日志正文一致），
对应 TG「📨 查看结果」。返回：

```json
{"callId": "...", "createdAt": 1761100000.0, "body": "...", "purgedAt": null}
```

- 正文已按 `logBodyRetention` 清理时，`body=null`，`purgedAt` 为清理时间；
- 调用不存在时返回 404 `RESOURCE_NOT_FOUND`。

## R2-21 翻译设置与测试（扩展）

- `getTranslationSettings` / `updateTranslationSettings` 的响应新增只读字段 `defaultPrompt`，即 `prompt` 为空时使用的
  内置默认提示词原文。
- `testTranslation` 的 Operation 失败时，`error.message` 为 `UPSTREAM_ERROR: <原因>`。原因来自翻译测试的返回值
  （例如“未就绪原因”或模型错误摘要），写入前会脱敏：去掉 Bearer/Basic 凭据、`sk-…` 一类 key、`api_key=` /
  `token=` / `password=` 等键值、URL 中的 userinfo，并截断到 300 字。有诊断信息时仍使用原来的诊断文案。

## R2-22 多媒体日志汇总与 Range 下载（新增 / 扩展）

- 新增 `getMediaLogSummary`：`GET /media-logs/summary`，字段与 TG 多媒体日志的「📊 汇总」和「👤 OAuth Top 3」一致：
  ```json
  {"total": 9, "openaiImages": 4, "xaiImages": 3, "videos": 2, "successCount": 7, "failedCount": 1,
   "pendingCount": 1, "expiredCount": 0, "costUsdTicks": 1234,
   "topAccounts": [{"accountKey": "...", "accountEmail": "...", "provider": "openai", "total": 4, "costUsdTicks": 100}]}
  ```
  `topAccounts` 只包含当前仍存在的 OAuth 账号，最多 3 个，规则与 TG 相同。
- `downloadMediaArtifact` 支持单段 HTTP Range，写法包括 `bytes=a-b`、`bytes=a-`、`bytes=-n`：
  - 命中时返回 206，带 `Content-Range: bytes a-b/total`；
  - 不带 Range、多段 Range 或非 bytes 单位时，返回完整文件 200；
  - Range 不可满足时返回 416，带 `Content-Range: bytes */total`；
  - 所有响应都带 `Accept-Ranges: bytes`。

## R2-23 图片 / 视频统计（新增）

`getMediaStatistics`：`GET /media/{kind}/statistics`，`kind` 取 `image` 或 `video`，对应 TG 模型中心面板中的
「当前缓存占用」和每个模型的「已生成」：

```json
{"cache": {"files": 3, "bytes": 1048576},
 "models": [{"model": "gpt-image-2", "completedCalls": 4, "generatedCount": 5, "unknownCountCalls": 0,
             "recordedBytes": 1048576, "unknownBytesCalls": 0}]}
```

## R2-24 缓存命中口径与未命中样本（修复 / 新增）

- 口径修复：
  - 原因：统计在用 attempt 账本校正 token 时，也把“缓存命中/写入请求数”从请求级替换成了 attempt 级。一次请求有多个
    计费 attempt（重试）时会被重复计数，失败请求的 attempt 也会被计入，所以出现 `cacheHitRequests > successCount`
    （例如 gpt-5.6-sol 7 天 25/20）。
  - 修复后：`cacheHitRequests` / `cacheWriteRequests` 恢复为请求级口径，即“成功、并且响应中读取/写入了缓存的请求数”，
    不会超过 `successCount`；token 总量仍然按 attempt 账本计算，不受影响。
  - 这是共享的 `log_db` 修复，TG 统计页的命中率也随之变正确，文案和按钮不变。
- 新增 `listCacheMissSamples`：`GET /stats/cache-misses?period=today|3d|7d|month|lifetime`，对应 TG「最近未命中样本」。
  返回最近最多 3 条成功但没有读取缓存的请求：
  ```json
  [{"id": "...", "createdAt": "...", "model": "...", "apiKeyName": "...", "channelId": "...",
    "inputTokens": 0, "cacheCreationTokens": 0, "outputTokens": 0, "messageCount": 0, "toolCount": 0}]
  ```
  数据取自缓存的 period 快照，不包含响应正文。

## 第二轮新增的 operationId

`getUpdateState`、`getCachedUpdateCheck`、`createUpdateActivationPlan`、`planOAuthClaudeReset`、
`executeOAuthClaudeReset`、`listOAuthOpenAiResetCredits`、`listModelChannelOrders`、`getSearchBackendModels`、
`getMcpCallResult`、`listCacheMissSamples`、`getMediaLogSummary`、`getMediaStatistics`。

形状有变化的 operationId（只新增字段或新增可选参数）：

- `pollOAuthLoginFlow`、`cancelOAuthLoginFlow`
- `startOAuthLoginFlow`
- `getOAuthAccount`、`updateOAuthAccount`
- `planOAuthQuotaReset`、`resetOAuthQuota`
- `createChannel`、`probeChannelDraft`
- `checkForUpdates`、`runNetworkMonitor`
- `createLogRetentionPlan`（新增 `items`）
- `listBackgroundJobs`
- `listModelMetadata`、`deleteModelMetadataBinding`
- `getModelRegistry`
- `testSearchBackend`
- `getTranslationSettings`、`updateTranslationSettings`（新增 `defaultPrompt`）、`testTranslation`（失败原因）
- `downloadMediaArtifact`（Range）
- `getStatsSummary`、`getStatsBreakdown`、`getModelStats`（`cacheHitRequests` / `cacheWriteRequests` 口径修复）

---

# 第三轮（2026-10-22）

目标：TG 能显示的信息，管理 API 都以结构化字段提供。本轮只**新增字段或新增可选参数**；除下面明确列出的行为变化外，原有字段的含义不变。

## R3-1 API Key 明文（用户要求，变更第一轮决定）

- `listApiKeys`、`getApiKey` 的每个 `ApiKeyData` 新增 `secret`（完整密钥），供列表和详情复制。
  - 只返回给持有 `management.secrets.write` 能力的会话（管理员会话都有）；只读会话该字段为 `null`。
  - 这两个响应带 `Cache-Control: no-store`。
- 其他位置一律不出现明文：`secret` 仍为 `null`，或者根本没有这个字段。包括：
  - 修改类响应，例如 `updateApiKey`；
  - 实时事件、Operation 结果、错误信息、审计和日志。
- 测试用 AST 门禁锁定：只有 `list_api_keys` / `get_api_key` 两个路由请求明文。
- `createApiKey` / 换密钥接口仍按原样一次性返回新密钥。
- 列表另外新增：
  - 每项 `historyTotal`：全部历史调用次数，对应 TG「闲置（历史 N 次）」，加载中为 `null`；
  - `data.summary`：TG 列表头部的统计，字段为 `{total, active, disabled, idle, monthRequests, monthPromptTokens, monthOutputTokens}`，按全部 Key 统计；
  - `data.historyLoading`。
- `ApiKeyUsageData`（`monthStats`、`modelStats[].usage`）新增：
  - `promptTokens`：TG「↑」，= 输入 + 缓存写入 + 缓存读取；
  - `cacheReadRatio`、`successRate`。

## R3-2 请求日志（`listRequestLogs` / `getRequestLog` / 实时事件 `request.finished`）

`RequestLogData` 新增字段，取值口径与 TG 的 `ui.fmt_log_entry_headline/body`、`logs_menu._render_detail` 一致，全部可选：

| 字段 | TG 对应 |
|---|---|
| `httpStatus` | 详情「状态 (HTTP)」 |
| `channelName`、`channelProvider`、`channelType` | 渠道显示名（OpenAI 同邮箱多工作区时附带工作区名）、平台、`api` / `oauth` |
| `ingressProtocol`、`upstreamProtocol`、`websocket` | 协议，取值 `ingress` = WS、`upstream` = ↑WS |
| `isStream`、`affinityHit` | stream、★亲和 |
| `cacheCreationTokens`、`cacheReadTokens`、`promptTokens`、`cacheReadRatio` | ↑ / 缓存 N (x%) |
| `connectMilliseconds`、`firstTokenMilliseconds`、`idleMilliseconds`、`tokensPerSecond` | 耗时、⚡ TPS（公式同 `calc_row_tps`） |
| `reasoningEffort`、`serviceTier`、`speedMode`（`ultrafast` / `fast` / `tier`） | 🧠、⚡ Fast / Tier 徽标 |
| `proxyName`、`proxyBytesUp`、`proxyBytesDown`、`localSearchCount` | 出站代理、搜索 N 次 |
| `usageObserved`、`errorSummary` | 错误摘要（解开 `HTTP nnn: {json}`） |
| `upstreamActualModel`、`modelSignalConflict{httpHeader, eventHeaders, bodyModels}`、`safetyReview{flagged, reason}` | ⚠️ 上游模型变更 / 🛡️ 安全审查 |
| `clientIp`、`finalRoundId`、`requestElapsedMilliseconds`、`stageMilliseconds{dns, tcp, proxyTcp, proxyTunnel, targetTls｜tls, wsHandshake, requestUpload, responseHeadersWait, responseBodyFirstByteWait}` | 仅详情：客户端、最终轮次、请求全程、可靠阶段 |

- 详情的 `billing` 现在与 TG 一样，会带上存储的响应尾部再计价（影响 xAI）。
- 缺陷修复：详情中的 `log.localSearchCount` 原来恒为 0，现在按该请求的搜索日志条数计算，与列表一致。
- 实时事件 `request.updated` 新增可选 `proxyName`。

## R3-3 OAuth 账户

### 列表：`listOAuthAccounts` 每项新增 `card`

`card` 对应 TG `oauth_menu._format_account_block`，前端不必再逐个调用详情接口。Telegram 内部读取时为 `null`。结构如下：

- `statusTag`：取值 `userDisabled` / `quotaDisabled` / `authFailed` / `providerRestricted`，或 `null`。
- `plan`：套餐行，字段为 `label, status, billingType, startedAt, expiresAt, tier, access{state: ok|limited|blocked, grokCode}, textModelCount, imageModelCount, variantCount, workspaceLabel, resetCreditCount, credits{observedAt, source: official_usage|response_sample, stale}, globalDisabledModelCount, organizationId, projectId, site, credentialMode, entitlement, managementStatus, setupIncomplete, region, accountType, authMethod, deviceChat`。按平台各自取值，平台没有的字段为 `null`。
- `usageSections`、`usageFetchedAt`：与 R2-10 相同。本轮补上 WorkBuddy 积分（企业 / 个人 / 资源包）和 Qoder 套餐与积分池。`windows` 段的 5 小时 / 7 天条目新增 `local`，即该上游窗口内经 Parrot 的本地用量。
- `localPeriod`：`{source, label, moneyLabel, since, stats, statsLoading}`。周期规则同 TG 的 `_oauth_local_period`：
  - OpenAI / Claude：套餐本期；
  - Cursor：账单本期；
  - Grok：本周；
  - 其他平台：本地自然月。
- `stats` 的字段名与 `StatsMetricData` 一致：`total, successCount, errorCount, inputTokens, outputTokens, cacheCreationTokens, cacheReadTokens, promptTokens, cacheReadRatio, averageTokensPerSecond, maximumTokensPerSecond, minimumTokensPerSecond, costTicks, actualCostTicks, estimatedCostTicks, actualCostedSuccess, costedSuccess, unpricedSuccess`。
- `windowStats`：`{fiveHour?, sevenDay?: {since, stats, statsLoading}}`。窗口起点规则同 TG 的 `_quota_window_since_ts`。
- `weeklyProjection`：`{tokens, costTicks|null}`，对应 TG「周额度预测」。
- `cooldown`：`{accountPaused, quotaPausedModels, permanentModels, coolingModels}`。Cursor 的配额暂停单独计数，不算作故障。
- 列表中的统计不阻塞请求：服务冷启动后第一次请求时 `statsLoading=true`，效果等同 TG 的「统计初始化中」，后台算完后再次请求即有数据。

### 详情：`getOAuthAccount`

- `account.card` 同上。详情中的统计在请求时同步计算，不会出现 loading。
- 新增 `plan`、`weeklyProjection`、`windowStats`、`lastRefreshAt`（TG「🔄 刷新」）。
- 行为变化：`localStats` 改为 TG 的周期口径。原有的 `requestCount / inputTokens / outputTokens / costUsd` 保留，但改按该周期统计。另新增 `periodLabel, periodSource, moneyLabel, since` 以及上面 `stats` 中的全部字段。
- `usageSections` 与 `card.usageSections` 相同，带 `local`。
- `runtimeErrors[]` 新增：
  - `errorCount`：TG「累计失败 N 次」；
  - `kind`：取值 `fault` / `quotaPause` / `quotaPauseHidden` / `accountRestriction`；
  - `pool`：仅 Cursor，取值 `cursor_models` / `other_models` / `unknown`。

## R3-4 模型（`listModels` / `getModel`）

- `ModelData` 新增以下字段，列表和详情都有：
  - `stateCode`：TG 列表状态。`null` 表示可用，其余取值为 `sourceOverride / globalDisabled / accountDisabled / channelDisabled / sourceContainerDisabled / sourceDisabled / noSources / unavailable / configured`。
  - `creditRates`：`{workbuddy?: [倍率], qoder?: [标签]}`，对应 TG 💳 倍率行。
  - `sharedMetadata`：参与调度的各来源共有的「容量 / 能力 / 价格」值，即 TG 详情顶部那一块。列表直接从这里取压缩阈值、最大输出等。字段为 `contextWindow, contextWindowMaxMode, maxInputTokens, maxOutputTokens, compactTriggerTokens, vision, toolCall, structuredOutput, reasoningEfforts, serviceTiers, knowledgeCutoff, cost{input, output, cacheRead, cacheWrite, longContextInput, longContextOutput}`。
  - `usage`：TG 累计用量，按实际执行路由投影，与 `model_center_usage` 口径相同。字段为 `requestCount, successCount, errorCount, inputTokens, outputTokens, cacheCreationTokens, cacheReadTokens, promptTokens, cacheReadRatio, successRate, averageTokensPerSecond, maximumTokensPerSecond, minimumTokensPerSecond`。
  - `usageLoading`。
- `listModels` 带 `sourceType` / `sourceId` 时，`usage` 只统计该来源，与 TG 相同。
- 仅 `getModel` 提供：
  - `schedule{mode: priority|order|smart, ownOrder, order[channelKey]}`；
  - 来源按 TG 排序：参与调度的按调度顺序在前，未参与调度的在后。
- 每个来源新增：
  - `scheduleRank`：在参与调度的来源中的位次；
  - `displayOrder`；
  - `notScheduledReason`：取值 `accountRestricted / containerDisabled / sourceDisabled / globalDisabled / unavailable`；
  - `account`：仅 OAuth 来源，内容为 `{statusTag, plan, usageSections, usageFetchedAt, cooldown, planType, available, quotaLimited, invalid, disabledReason, disabledUntil}`，即该账户的额度、余额和积分，只读缓存；
  - `channel`：仅 API 来源，内容为 `{balance{label, amount, currency, fetchedAt}, providerUsage{status, supported, stale, fetchedAt, snapshot}, health, cooldownCount}`。

## R3-5 统计

- `StatsMetricData` 新增 `promptTokens`、`cacheReadRatio`、`successRate`、`cacheHitRate`、`cacheWriteRate`、`retriedRequestRate`、`affinityHitRate`。分母为 0 时值为 `null`。这些字段出现在所有统计接口的指标中。
- `getStatsSummary` 新增：
  - `familyTops{anthropic|openai: {channels[Top3], models[Top3 + channels「所属」]}}`；
  - `topApiKeys[Top3 + familyTotals{anthropic, openai}]`，对应 TG 汇总页的 Top 块和 Key 的家族拆分。
- `getStatsBreakdown` 新增可选参数：
  - `family=anthropic|openai`：取该家族的 Top；
  - `channel=<api:渠道 | OAuth accountId | oauth:<id>>`：仅限 `dimension=model`，返回单个账号或渠道的按模型统计，对应 TG 详情里的按模型。
  - `channel` 与其他维度、或与 `family` 同时使用，返回 422。
- `dimension=model` 时，每项新增 `channels[{key, type, count}]`，即 TG 的「所属」，取前 5 个。
- 统计缓存预热增加 API Key 历史次数和模型累计用量两项。

## R3-6 渠道

- `ChannelData` 新增 `permanentCooldownCount`。
- `listChannels` 每项新增：
  - `monthStats`：TG「💎 Parrot 月度」，取共享的当月快照，不阻塞请求；
  - `monthStatsLoading`。
- `ChannelMonthStatsData`（列表和详情）新增 `promptTokens`、`successRate`、`cacheReadRatio`、`costTicks`、`costedSuccess`、`unpricedSuccess`。

## R3-7 其他行为变化

- `deleteCompressionModel`，以及 TG 的「清除压缩指定」：清除时会一并删掉旧字段 `modelMetadata.*.compressionModel`，避免取消后又回落到旧的压缩模型。

## 第四轮

### R4-1 10 分钟时间桶

- `getStatsTimeseries` 的 `bucket` 新增 `10m`（600 秒）。`range=24h&bucket=10m` 返回 144 个点，概览的「请求健康时间线」用它按 10 分钟着色。

### R4-2 `listRecentOutcomes`：`GET /stats/recent-outcomes`

- 会话，`management.read`。参数 `dimension`（`channel` / `apiKey` / `model`，必填）、`limit`（1–60，默认 20）。
- 返回近 7 天内每个渠道（含 OAuth 账号，键为 `oauth:<accountId>`）/ API Key / 模型最后 `limit` 次已结束请求的结果，`outcomes` 按时间从旧到新，取值 `success` / `error` / `cancelled`；另带 `successCount`、`errorCount` 和最后一次的 `lastAt`。进行中的请求不计入。
- 用于概览「渠道健康状态」、OAuth 账户 / API 渠道 / API Key 列表的「最近状态」条。结果走统计缓存。

### R4-3 `getStatsCompare`：`GET /stats/compare`

- 会话，`management.read`。返回一个时间窗口和它的上一周期（时长相同）的汇总，以及本期的趋势点，用于各处指标块右上角的变化和趋势线。
- 参数：`period`（`today` / `3d` / `7d` / `month`），或 `from` + `to`（ISO 时间，`to` 超过当前时间时按当前时间算），二者只能选一种；`model`、`channel`、`apiKey` 可任意组合，取值与请求日志的筛选相同（`channel` 为 `api:<名称>` 或 `oauth:<accountId>`）。`lifetime`、缺少时间参数、未知参数返回 422。
- 上一周期：
  - `today` 对比昨天 0 点到同一时刻；
  - `3d` / `7d` 对比紧挨着的前 3 天 / 前 7 天；
  - `month` 对比上月 1 号到同一天同一时刻（上月较短时截到本月 1 号）；
  - 自定义时间段对比紧挨着的前一段等长时间。
- 返回 `from`、`to`、`previousFrom`、`previousTo`、`previousComplete`、`bucketSeconds`、`current`、`previous`、`points`。`current` / `previous` / `points[]` 的字段：`total`、`successCount`、`errorCount`、`inputTokens`、`outputTokens`、`cacheReadTokens`、`cacheCreationTokens`、`costTicks`、`unpricedSuccess`、`cacheHitRequests`、`affinityHits`、`averageFirstTokenMilliseconds`、`averageTotalMilliseconds`、`averageTokensPerSecond`（输出 Token ÷ 生成耗时，与汇总相同）。
- 两个窗口与 `/stats/timeseries` 用同一套分桶统计，口径与 `getStatsSummary`、`getStatsBreakdown` 一致（请求数、失败、Token、金额、未计价、缓存命中请求数、亲和命中数、平均输出速度逐项相同）。
- `previousComplete=false` 表示日志最早的一条晚于 `previousFrom`（被保留策略清理或刚开始记录），上一周期的 0 不代表没有流量，WebUI 不显示变化。
- `points` 覆盖本期，步长取不超过 32 个点的最小档（今天约 30 分钟到 1 小时、3 天 3 小时、7 天 6 小时、本月 1 天）。

### R4-4 性能

- `/api/management/` 与 `/ui/` 的响应在请求带 `Accept-Encoding: gzip` 时压缩（1KB 以上的 JSON、JS、CSS、文本、SVG），返回 `Content-Encoding: gzip` 与 `Vary: Accept-Encoding`。图片、视频、压缩包、`Range` 请求和 206 响应、事件流原样返回；推理接口（`/v1/*` 等）不受影响。模型列表约 817KB → 56KB，WebUI 图表脚本 554KB → 186KB。
- `getStatsCompare` 把结束超过 1 小时的整点小时按「模型 × 渠道 × Key」聚合一次后缓存（价格设置变化时清空，早于日志最早一条的小时视为空），窗口只对边缘不足一小时的部分和最近未封存的小时实时查询；启动预热会算好本月与上月。结果与直接查询逐项相同。
- 代理与代理组列表的用量统计走统计缓存（15 秒内直接返回，过期后先返回旧值并在后台重算）。
