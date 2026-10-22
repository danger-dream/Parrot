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
| （WebSocket） | `WS /events` | Cookie 或 Bearer |

另有：`POST /auth/sessions` 新增字段，`POST /auth/telegram-approvals` 响应新增 `confirmCode`，
`GET /overview` 新增三个字段，`/ui/` 静态托管。

---

## 1. 配置项 `management.webUi.enabled`

```json
{ "management": { "webUi": { "enabled": false } } }
```

- 默认 **false**（老用户升级后默认关闭）。缺少该段的旧配置在加载时自动补默认值并写回
  `config.json`，不会启动失败。
- 值必须是布尔；非布尔值会让管理面初始化失败（与其它 `management.*` 校验一致），
  此时 `/ui` 一律按关闭处理（fail-closed）。
- 切换立即生效，不需要重启：静态托管每个请求都读当前配置。
- 开关入口：`PATCH /settings/web-ui`，以及 TG「⚙ 系统设置 → 🖥 WebUI」。两者调用同一个
  control 用例 `SettingsControl.update_web_ui`。

### `GET /settings/web-ui` / `PATCH /settings/web-ui`

响应 `data`：

```json
{ "enabled": false, "revision": "rev_3c1f…" }
```

PATCH 请求体（稀疏，未知字段 422）：`{"enabled": true}`。支持 `If-Match: <revision>`，
不一致返回 409 `REVISION_CONFLICT`；空体、非布尔返回 422 `VALIDATION_FAILED`。每次写入都记审计
`settings.web-ui.update`。

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
