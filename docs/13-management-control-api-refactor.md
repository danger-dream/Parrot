# 13. Management Control / Management API 重构实施与验收

> **后续模型中心变更**：本文保留 v0.31.13 管理层迁移的历史基线。模型导航、显式 model、启停/可见性、别名原子编辑等后续有意变更，以 [模型中心使用与兼容说明](14-model-center.md) 为准；原“TG 零变化”要求不用于否定已认可的模型中心交互，其余无关行为仍须保留。

> 状态：**以功能主线为优先的实施与验收文档**
> 基线：`feature/management-api`，`b8ba47cc7568aee31fc2587354b5bc14d9773c9a`（v0.31.13）  
> 范围：Shared Management Control、统一管理身份/授权、Management API Adapter，以及 Telegram Adapter 向共享控制层的等价迁移  
> 不在范围：Web UI、推理 API 协议改造、配置格式迁移、Telegram UI 重设计、安全专项

本文仍是本轮功能实施与验收依据，但条款有明确优先级：**v0.31.13 Telegram 原版行为完全一致**和**完整、真实可达的 Management API**优先，其次是两个 Adapter 共享同一控制层及权威业务副作用，最后才是本轮必要的基础认证与秘密字段边界。安全增强不得自行扩张范围、删减功能或把本轮改造成安全专项；第 4.6 节列出的后续加固候选不是本轮合并门禁。

本文中的“必须”“禁止”“不得”仅在上述本轮范围及完成定义内构成门禁。不得以“Web UI 尚未开发”或“Telegram 看起来差不多”为理由降低功能标准，也不得以自设纵深安全要求阻断已经满足本轮功能与基础安全边界的实现。

---

## 1. 目标、完成定义与不可协商项

### 1.1 唯一架构结果

最终调用方向必须是：

```text
Telegram Bot Adapter ─┐
                      ├─> Management Identity + Authorization
Management API Adapter┘                 │
                                        v
                           Shared Management Control
                                        │
           ┌───────────────┬────────────┼───────────────┐
           v               v            v               v
         OAuth          Channels     API Keys      Settings / Runtime
                                        │
                                        v
                                  Logs / Stats
```

两个 Adapter 只负责各自的传输、输入解析、输出渲染和交互状态；业务校验、配置变更、跨模块级联、运行时动作和查询编排只允许存在于 Shared Management Control 或现有业务模块中。

本轮是 **API only**：不交付 Web UI，但 Management API 必须用 typed schema、稳定 operation 和可发现元数据表达完整管理能力，便于后续 Web 直接作为普通客户端使用，不需要再开私有捷径。

### 1.2 本轮 Done

只有同时满足下列条件才算完成：

1. `/api/management/v1` 下第 4～12 节列出的 Management API 已完整实现，可由 OpenAPI 发现，并由实际应用组合根挂载全部领域 router；仅生成 schema、只在测试 app 挂载或存在不可达 endpoint 均不算完成；
2. 除创建/交换 Session 所需的认证引导路由外，所有管理领域资源只接受有效 Management Session；固定管理密钥和 Telegram 带外批准是两种换取 Session 的入口，不能直接访问领域资源；
3. Telegram 和 Management API 对同一管理动作调用同一个控制层用例和同一组运行时依赖，而不是各自复制业务逻辑或在生产组合中实例化互不相干的 control；
4. 第 14 节 53 项 Telegram 功能均有基线轨迹和重构后轨迹；所有既有 Telegram 方法、正文、按钮、`callback_data`、分页、状态流、确认、错误显示和业务副作用与 v0.31.13 完全一致；
5. 第 15 节 API 清单的每个 operationId 均有可达性、schema/control 接线及与其语义相关的成功、校验和关键副作用测试；
6. 完整测试套件不低于已知基线 `2524 passed`，且本轮新增功能门禁全部通过；不得把尚未执行的最终验收写成已完成事实；
7. 每个新增源码文件不超过 1000 行，按领域维护性拆分；依赖门禁未发现 Adapter 直连业务模块或反向依赖；
8. 实施、开发服务和测试均使用隔离副本、临时配置/数据库及 fake/专用凭据，不触碰生产 `/opt/src-space/parrot`、生产进程或生产数据。

任一 Telegram 差异、缺失或实际不可达的 API operation、生产组合未挂载领域 router、绕过共享 Control 的写路径或错误的业务级联，均直接判定失败。第 4.6 节的后续安全候选不参与本轮 Done 判定。

### 1.3 明确不做

- 不开发 Web UI，也不加入前端框架、静态资源构建或页面模板。
- 不改变 `/v1/messages`、OpenAI/Responses、images、xAI video 等下游推理接口。
- 不把 `src/auth.py::validate()` 复用为管理鉴权。它验证的是推理侧 Bearer/`x-api-key`，返回 key 名称和模型权限，与管理身份是两个安全域。
- 不提供通用的 `GET/PUT config.json` API；管理 API 只能暴露本文件列出的、经过类型校验且已脱敏的资源。
- 不因现有文件过大而重排 Telegram UI、改文案或更换 callback；大文件只随业务逻辑自然下沉而缩小。
- 不为本轮引入消息总线、插件框架、通用工作流引擎、Web UI 专属 BFF、新角色体系或通用安全扫描框架。Session/approval/operation 所需状态只按当前功能的最小职责实现，不演变成第二套业务配置、渠道或 OAuth 数据库。

---

## 2. 源码基线与已确认语义

本文以以下源码为基线：

- Telegram 核心：`src/telegram/bot.py`、`states.py`、`ui.py`、`menu_cache.py`、`log_inspector.py`；
- 菜单：`src/telegram/menus/*.py`；
- 业务模块：`oauth_manager.py`、`channel/registry.py`、`model_mapping.py`、`model_metadata.py`、`load_balancing.py`、`proxy/manager.py`、`log_db.py`、`media_db.py`、`state_db.py`、`state_store.py`、`updater.py`、`provider_usage.py`、`network.py`、`network_monitor.py`、`status_monitor.py`、`translation.py`、`cooldown.py`、`affinity.py`、`apikey_limiter.py` 等；
- 服务和推理鉴权：`server.py`、`src/auth.py`；
- 既有 TG 测试：`test_m6_tgbot.py`、`test_m7_oauth_menu.py`、`test_m8_channel_menu.py`、`test_m9_stats_logs.py`、`test_m10_system_menu.py`、`test_tg_menu_performance.py`、`test_tg_safe_send.py`、`test_telegram_dns_recovery.py`、`test_tg_xai_imagine.py`、`test_oauth_account_models.py`、`test_oauth_defaults_models.py`、`test_oauth_overwrite_confirmation.py`、`test_log_retention.py`、`test_media_logs.py`。

实施时不得破坏以下已确认事实：

- `states.py` 是按 `chat_id` 的单状态槽，新状态覆盖旧状态，默认 TTL 为 600 秒；
- Telegram `callback_data` 有 64 字节限制，长标识通过 `ui.register_code()` / `resolve_code()` 间接引用；
- `menu_cache.py` 由唯一的 `tg-stats-scheduler` 线程串行执行统计 SQL，含视图 token、消息锁和初始化提示；
- OAuth 覆盖确认使用 nonce、`secrets.compare_digest` 和 `states.pop_state()`；日志留存计划使用服务端 8 字符 code，TTL 600 秒；
- provider usage handler 只调度刷新，不在 TG polling handler 中等待供应商网络；
- 渠道手工测试存在必须冻结的基线矛盾：已存在渠道的页面声明“本次测试不会修改冷却状态”，但当前共用 helper 在测试成功时实际调用 `cooldown.clear()`；已存在渠道失败路径不记录新错误，向导初始探测失败则会 `record_error()`。本重构同时保留原文案和这些实际副作用，不得顺手修复；
- 渠道删除必须走 `channel.registry` 的级联：冻结上限转移、scorer 清理、cooldown 清理、服务端与 client affinity 清理、registry rebuild、孤儿 provider-usage cache 清理；
- `quotaProgressBar` 默认 `true`，`oauthUsageDisplayMode` 为 `used|remaining`；`telegram.statsVisibility` 的五项默认为 `true`；
- 日志列表和检查器、媒体日志、OAuth/渠道/API Key 等各自分页常量属于 Telegram 展示合同，不是 Management API 的分页合同。

---

## 3. 包边界、文件所有权与依赖规则

### 3.1 目标包布局

实现必须使用下列职责边界；可以在同一目录内按规模继续拆分，但不得合并成一个巨型 service/router：

```text
src/management_auth/
  principal.py          # ManagementPrincipal、认证来源与管理员身份
  policy.py             # 统一 authorize(principal, action)
  sessions.py           # session 生命周期；不含 HTTP/TG 渲染
  approvals.py          # TG 带外 challenge 生命周期

src/management_control/
  context.py            # ManagementContext、actor、request/operation id
  errors.py             # 稳定错误码；不依赖 HTTP 或 TG
  operations.py         # 有界异步操作状态与查询
  overview.py
  oauth.py
  channels.py
  api_keys.py
  stats.py
  logs.py
  media.py
  mapping.py
  load_balancing.py
  proxies.py
  system_settings.py
  network.py
  translation.py
  status_alerts.py
  updates.py
  media_settings.py

src/management_api/
  router.py             # 只聚合子 router
  dependencies.py       # session -> ManagementPrincipal/Context
  error_mapping.py      # ManagementError -> HTTP
  schemas/              # 纯 Pydantic 输入/输出；按领域拆分
  routers/              # 每领域一个或多个 router

src/telegram/
  ...                    # 保留现有 transport、状态机、文案、按钮和 callback
```

`server.py` 仅负责初始化/关闭 management auth、挂载总 router，以及把生命周期依赖传入组合根；不得放领域 endpoint 或管理业务逻辑。

### 3.2 允许依赖

| 调用方 | 允许依赖 | 禁止依赖 |
|---|---|---|
| `management_api` | `management_auth`、`management_control`、API schemas、FastAPI | `config`、`oauth_manager`、`registry`、`log_db` 等业务模块；`telegram` |
| `telegram` Adapter | `management_auth`、`management_control`、Telegram 自身 UI/state/cache | 新增对更多业务模块的直连；`management_api`、FastAPI |
| `management_control` | `management_auth` 的 principal/policy、现有业务模块 | FastAPI、HTTP request/response、Telegram UI/state/callback |
| `management_auth` | 最小必要的 config/state/clock/random 抽象 | Telegram 菜单、Management API router、业务领域 control |
| 现有业务模块 | 原有依赖 | 反向依赖 `management_*` 或 `telegram` |

最终门禁要求：Telegram 中“查询或改变管理领域”的调用均经 control；只允许以下 Adapter 内部职责继续直接存在：Telegram API 传输、HTML/emoji/单位格式化、callback short-code、chat 状态、消息锁和 TG 专用快照调度。若 `menu_cache` 需要统计数据，其 worker 调 control query，不直接新增 `log_db` 调用。

### 3.3 共享控制层不是 UI helper

控制层输入/输出必须是 typed command/query DTO 或稳定字典模型：

- 输入包含 `ManagementContext`，由 Adapter 提供 actor；
- 输出只含机器字段、枚举、时间、数量、资源标识和脱敏状态；
- 不含 Telegram HTML、emoji、按钮文字、callback、chat/message id；
- 不返回 FastAPI `Response`、status code 或 cookie；
- 不把供应商异常文本原样作为公共错误码；
- 所有 mutation 在控制层再次执行统一管理授权，Adapter 的前置检查不是授权边界。

Telegram Adapter 继续用原有格式化函数把控制结果渲染成**完全相同**的字符串；Management API Adapter 只做 DTO/schema 转换和 HTTP 错误映射。

### 3.4 跨模块副作用的唯一所有者

下列动作只能由对应 control 用例编排，不得在两个 Adapter 内复制：

| 动作 | 必须保留的编排 |
|---|---|
| 删除/改名渠道 | 调用 registry 权威接口并完成 cooldown、scorer、affinity、provider usage 等现有级联 |
| 渠道探测 | 根据 `existing_diagnostic` 与 `pre_save` 两个固定用例复现基线副作用：前者成功仍按当前 helper 清 cooldown、失败不新增错误；后者成功清 cooldown、失败 record_error。调用方不能传任意开关改变基线 |
| 删除/重置/换值 API Key | config 原子更新后同步 `apikey_limiter.forget_key()` 或相应 runtime reset |
| OAuth 新增/覆盖/删除 | exact identity 检查、nonce/plan 语义、账号 registry rebuild、cooldown/affinity/额度状态处理保持现状 |
| OAuth refresh/usage | 复用 `oauth_manager` / `provider_usage` 的节流、部分成功持久化和 retry-after；TG 调用始终非阻塞 |
| 留存 | `scan -> server-side plan -> commit`，commit 只调用 `log_db.apply_retention_plan(..., activate_policy=True)` |
| 网络配置 | test -> result -> normal/force commit；保存后仍执行现有网络重建/缓存处理 |
| 更新 | check -> backup/pull/stage -> explicit restart -> health/rollback，不允许 API 绕过 stage |
| 模型映射 | 全局映射的一层解析语义、legacy ingress 清理、默认模型与 metadata binding 语义不变 |

---

## 4. 统一管理身份、授权与 Session

### 4.1 Principal 与本轮授权边界

`ManagementPrincipal` 至少包含：

```text
subjectId, authMethod, issuedAt, sessionId?
```

本轮只需要表达“已认证的管理者”这一授权事实，不新增产品角色层级、细粒度 capability 矩阵或治理机制。`management_auth` 统一判断 principal 是否可执行管理动作；Control 对 mutation 和受保护读取再次调用 `authorize(principal, action)`，不得把授权散落为 Adapter 内的 `if admin`。

现有 Telegram `adminIds` 映射为同类管理 principal。未授权 TG update 的忽略/提示行为保持基线，不因统一授权增加消息。Management API 的领域 principal 只能来自有效 Management Session；固定管理密钥、推理 API Key 或 approval credential 本身都不是领域 principal。

### 4.2 固定管理密钥换 Session

```http
POST /api/management/v1/auth/sessions
Content-Type: application/json

{"grantType":"managementKey","managementKey":"<writeOnly>"}
```

成功返回 session 摘要和一次性可交付的 session credential；失败统一为 `AUTHENTICATION_FAILED`，不回显提交值或具体匹配细节。固定管理密钥只用于换取 Session，不得作为领域 API 的长期直接凭据，也不得复用下游推理 API Key。程序化客户端使用 OpenAPI 声明的 `Authorization: Bearer <management-session>` 传递 Session；未来 Web UI 的传输选择不在本轮决定。

按用户给出的认证方向，Session 默认 3 天滑动有效：有效期内有访问则续期；家、公司、手机等多个 Session 可以并存，不要求唯一登录。token 具体格式、持久化和更严格的绝对期限属于实现或后续加固细节，不扩张本轮功能范围。

### 4.3 Telegram 带外批准换 Session

协议入口为：

1. `POST /auth/telegram-approvals` 创建自创建时起 3 分钟有效的 challenge，返回 `approvalId`、`exchangeSecret`、`expiresAt` 和客户端轮询所需状态；
2. 系统向配置中的 Telegram admin 发送一条**新增且隔离**的批准/拒绝消息；callback 使用新命名空间 `mauth:`，不得改动任何既有命令、菜单或 callback；
3. `GET /auth/telegram-approvals/{approvalId}` 使用该 challenge 的 exchange credential 查询 `pending|approved|denied|expired|consumed`；
4. `POST /auth/sessions` 以 `grantType=telegramApproval` 和该 challenge 的交换材料换取 Management Session；未获批准不能创建 Session。

批准消息不得展示 management key、session credential 或 exchange credential。TG approval 只承担第二种 Session 入口，不为 Telegram 增加新的业务管理路径；既有 TG surface 仍按第 14 节冻结。3 分钟有效期是本轮认证方向；在此之外更严格的重放、浏览器绑定和过期处置属于第 4.6 节后续加固，不扩张本轮验收矩阵。

### 4.4 Session 路由

| method/path | operationId | 语义 |
|---|---|---|
| `POST /auth/sessions` | `createManagementSession` | 两种 grant 换 session |
| `GET /auth/session` | `getCurrentManagementSession` | 当前 subject、method 和 session 摘要 |
| `DELETE /auth/session` | `revokeCurrentManagementSession` | 当前 session 失效 |
| `POST /auth/telegram-approvals` | `createTelegramApproval` | 创建带外 challenge |
| `GET /auth/telegram-approvals/{approvalId}` | `getTelegramApproval` | 仅对应 challenge credential 可查询 |

### 4.5 本轮必要的基础安全边界

以下条款是本轮门禁，边界以已声明的结构化 API 为限：

- 除第 4.2～4.4 节创建、交换或查询 Session 所需的认证引导路由外，第 6～12 节所有领域 operation 均须有效 Management Session；领域 router 使用同一认证依赖，Control 使用同一管理 principal；
- 固定管理密钥和 TG approval 是仅有的两种换 Session 入口；推理 API Key 与管理认证严格分离，不能互相替代；
- request schema 中语义明确的 secret 字段标记 `writeOnly`；创建、生成或轮换 secret 时可按 operation 合同返回一次，后续普通 GET 不主动回显；
- 不提供 raw config dump；已知 URL credential 字段以 URL 结构解析并遮蔽 userinfo，不靠扫描任意文本猜测秘密；
- 公共 `Operation`、公共错误和 API 自有诊断摘要不主动塞入 raw exception、提交的 credential 或供应商 credential；Control 提供稳定错误信息，由 API/TG Adapter 分别渲染；
- Telegram 继续使用既有错误文案和未授权行为，API 认证错误不得反向改变第 14 节轨迹。

### 4.6 后续安全加固候选（不阻断本轮）

以下事项可在用户另行确定威胁模型、部署方式和兼容策略后开展，但**不属于本轮 Done、逐 operation 测试矩阵或 Gate**：更严格的 session/challenge replay 防护和绝对过期策略、认证限速、CSRF/Origin、cookie policy、TLS 与 network exposure；对任意自由文本、任意 nested object、转义 JSON 中 token/key/secret/credential 变体的穷举扫描；Camel/Pascal/ALL_CAPS/concatenated suffix 分类；把普通 Bearer/Basic 文案启发式判为 credential；为 generic `key` 建 schema-depth marker/exception；递归遍历异常 `__cause__`/`__context__`。

本轮不得为这些候选引入通用秘密分类器、递归异常审计系统、额外角色/策略框架，也不得用无休止的相邻安全 probe 阻断功能验收。发现明确违反第 4.5 或 5.3 节的已知结构化泄露仍应修复；这不等于扩大到任意文本推断。

---

## 5. Management API 通用合同

### 5.1 基础约定

- 基础路径固定为 `/api/management/v1`；破坏性变更必须新增版本，不得静默改变 v1。
- JSON 字段使用 `camelCase`，UTF-8；时间为 RFC 3339 UTC，持续时间字段显式带 `Seconds`/`Milliseconds`。
- 资源 ID 使用 API 返回的稳定完整 ID；不得把 TG short-code 暴露为资源 ID。路径中的账号、模型等 ID 必须进行标准 percent-encoding。
- 默认列表参数：`page=1`、`pageSize=50`，`1 <= pageSize <= 200`；响应 `meta.page/pageSize/total/hasNext`。TG 的 4/5/6/8/10 条分页不适用于 API。
- 排序必须用已声明 enum；未知 filter/sort/field 返回 422，不得静默忽略。
- 所有资源 GET 返回 `revision`（不透明字符串）。客户端可在 mutation 上发送 `If-Match`；不匹配返回 `REVISION_CONFLICT`。高风险 commit、reorder 和 secret rotation 必须要求 `If-Match` 或 plan token。
- 成功响应使用 `{"data":...,"meta":{"requestId":"..."}}`；`204` 无 body。列表分页信息并入 `meta`。
- 创建返回 201；同步动作返回 200；长动作返回 202 和 `Operation`；删除成功返回 204。

### 5.2 错误合同

```json
{
  "error": {
    "code": "VALIDATION_FAILED",
    "message": "human-readable summary",
    "fields": [{"path":"models[0]","code":"UNKNOWN_MODEL","message":"..."}],
    "retryable": false,
    "requestId": "...",
    "operationId": null
  }
}
```

稳定映射：

| HTTP | code |
|---|---|
| 400 | `INVALID_REQUEST`, `CONFIRMATION_REQUIRED`, `INVALID_OPERATION_STATE` |
| 401 | `SESSION_REQUIRED`, `SESSION_EXPIRED`, `AUTHENTICATION_FAILED` |
| 403 | `CAPABILITY_DENIED` |
| 404 | `RESOURCE_NOT_FOUND`, `OPERATION_NOT_FOUND` |
| 409 | `RESOURCE_CONFLICT`, `IDENTITY_CONFLICT`, `REVISION_CONFLICT`, `STATE_CONFLICT` |
| 422 | `VALIDATION_FAILED`, `UNSUPPORTED_VALUE` |
| 429 | `OPERATION_ALREADY_RUNNING` |
| 502 | `UPSTREAM_ERROR` |
| 503 | `SERVICE_NOT_READY`, `DEPENDENCY_UNAVAILABLE` |
| 504 | `UPSTREAM_TIMEOUT` |

`error.code` 是客户端分支依据；英文/中文 message 均不构成稳定契约。`CAPABILITY_DENIED` 在本轮只是统一授权拒绝的稳定错误名，不引入角色/capability 产品矩阵。Control 抛 `ManagementError(code, fields, retryable)`；HTTP mapper 和 Telegram mapper分别处理，禁止 Control 内硬编码 HTTP 或 TG 文案。

### 5.3 已知秘密字段与公共输出

- OAuth access/refresh token、API channel key、proxy 密码、TG bot token、management key/session/challenge secret 等**语义明确的 request 字段**标记 `writeOnly`；普通 GET 只返回功能所需的 `configured`，必要时返回既有不可逆 `maskedHint`。
- 下游 API Key 的新值只在 create/generate/rotate 成功响应中返回一次；之后只能看到名称、前缀提示和状态。其他 secret 若 operation 明确承担创建或轮换，也遵循“一次返回、后续 GET 不主动回显”。一次性交付的响应字段不得标为 `writeOnly`；该标记只用于请求秘密字段，响应继续禁止缓存。
- 不提供 config dump，不回显提交的 credential，不把已知 secret 放入公共 Operation result 或错误对象。
- 已知可携带 credential 的 URL 字段必须结构化解析并遮蔽 userinfo 后再输出；不能通过字符串替换声称完成 URL 遮蔽。
- 公共错误和 Operation 只承载稳定 code、结构化字段及面向调用方的摘要，不主动复制 raw exception 或 credential。这里不要求扫描任意业务文本、转义 JSON、任意嵌套键或递归异常链；此类增强按第 4.6 节处理。
- 日志 body 属于受保护业务数据，不是 credential API；须有有效 Management Session，并沿用现有解密/不可读加密内容的展示语义。不得用通用 credential 猜测器改变原始业务日志的功能语义。

### 5.4 异步 Operation

供应商 OAuth/usage refresh、模型发现/metadata sync、网络探测、代理探测、留存执行、自更新等不得占用 HTTP worker 或 TG polling 线程。统一返回：

```json
{
  "id":"op_...", "kind":"oauth.usage.refresh", "status":"queued|running|succeeded|failed|cancelled",
  "progress":{"current":1,"total":4,"messageCode":"..."},
  "createdAt":"...", "startedAt":null, "finishedAt":null,
  "result":null, "error":null, "cancellable":false
}
```

路由：

- `GET /operations/{operationId}` (`getManagementOperation`)
- `DELETE /operations/{operationId}` (`cancelManagementOperation`，仅 `cancellable=true`)

Operation store 必须有界、按 session/actor 授权、不持久化秘密；服务重启后未知/中断状态必须明确返回，不得伪装成功。API 轮询 Operation 不改变 TG 的进度消息节奏。

TG 冻结兼容优先：原版同步执行的留存清理及导入后 usage/quota 副作用，继续通过同一 Control 的同步兼容用例保持原时点与异常顺序；不得为套用异步 Operation 改变 TG 既有交互。Management API 留存执行仍返回异步 Operation。

### 5.5 可发现性

`GET /meta` (`getManagementMetadata`) 返回 API 版本、应用版本、支持的功能摘要、枚举和文档链接；`GET /capabilities` (`getManagementCapabilities`) 返回按领域组织的 provider/preset/protocol/mode/action schema。这里的 capabilities 是客户端可发现的产品能力，不是本轮新增角色体系。每个 route 必须有唯一 `operationId`、tag、request/response schema、enum和已知敏感字段标记。禁止用 `dict[str, Any]` 作为对外主 schema 来逃避合同。

- `/meta.data.features[]` 为 `{id, actionCount}`，`enums[]` 为 `{name, values}`。
- 两个发现响应均以 `supportedCapabilities` 表达产品 capability vocabulary，以 `principalCapabilities` 表达当前 principal grants。
- `/capabilities.data.domains[]` 保留 v1 `actions: list[str]`，值为真实 operationId；加法字段 `actionDetails[]: {operationId, method, path}` 与其一一对应。另含 `domain`、`providers[]`、`presets[]`（`providerId/presetId`）、`protocols[]`、`modes[]`、`features[]`。旧 `capabilities` 字段保留为当前 principal grants 的 deprecated 别名；授权读取顶层 `principalCapabilities`，产品支持读取 `supportedCapabilities`。旧枚举名称 `authMethod`、`operationStatus` 保留为真实 schema 枚举的镜像别名。
- domain/action/enum 来自实际挂载的 Management OpenAPI；Channel catalog 复用 `ChannelControl.get_catalog()`。发现数据不等于 provider 已配置、健康或可联网，也不替代 Control 根据具体请求执行的最终授权。

---

## 6. 总览、状态与运行时 API

| method/path | operationId | 必须返回/行为 |
|---|---|---|
| `GET /overview` | `getManagementOverview` | version、uptime、监听摘要、渠道/OAuth/API Key 数、quota hot count、当日/累计统计摘要、活动告警；不含秘密 |
| `GET /runtime/status` | `getRuntimeStatus` | channel health、problem channels、按 family 最快渠道、quota warnings、并发/队列、cooldown/affinity 摘要、数据库状态 |
| `GET /runtime/background-jobs` | `listBackgroundJobs` | WAL checkpoint、pending cleanup、affinity cleanup、OAuth refresh/quota monitor、cooldown probe、provider usage 等 last/next/status/error 摘要 |
| `GET /runtime/cooldowns` | `listCooldowns` | active entries 和冻结/永久状态 |
| `GET /runtime/concurrency` | `getConcurrencySnapshot` | channel 与 API Key limiter totals/snapshot |

运行时列表是只读快照；不得通过 GET 触发 supplier refresh、清理或 rebuild。北京时间展示由 TG Adapter 完成，API 时间统一为 UTC。已有 worker 未提供权威 last/next/run/error 遥测的字段如实返回 `unknown`/`null`；本轮不为填满摘要创建第二套 registry、worker 或后台治理。

---

## 7. OAuth API 与控制层合同

### 7.1 控制层用例

`OAuthControl` 必须覆盖：账号 list/detail/order、exact identity 检查、各 provider 的登录/导入/手工 credential、显式覆盖、删除、enable/maxConcurrent、token refresh、usage refresh、quota reset、runtime error/cooldown/affinity 清理、模型同步、单个/批量模型禁用、Cursor max-context default、无效账号清理、quota monitor 和显示偏好。OAuth 备用模型于 2026-09-16 完整退役，不属于现行控制层用例。

控制层必须复用 `oauth_manager` 的权威接口（如 `find_exact_identity`、`add_account_if_identity_absent`、`replace_exact_identity`、`force_refresh`、`fetch_usage[_snapshot]`、`set_enabled`、`reset_quota`、`set_account_model_disabled`、`set_cursor_max_context_default`），不得由 Adapter 直接编辑 `oauthAccounts`。

### 7.2 账号、登录与导入路由

| method/path | operationId | 说明 |
|---|---|---|
| `GET /oauth/accounts` | `listOAuthAccounts` | filter=`all|available|quota|invalid`，provider、enabled、sort/page；返回完整稳定 accountId，不返 token |
| `POST /oauth/accounts` | `createOAuthAccount` | 手工/JSON/refresh-token credential union；exact identity 冲突返回 409 和可创建 replace plan 的摘要 |
| `GET /oauth/accounts/{accountId}` | `getOAuthAccount` | identity 摘要、状态、usage windows、local stats、model counts、runtime errors |
| `PATCH /oauth/accounts/{accountId}` | `updateOAuthAccount` | 仅 displayName/enabled/maxConcurrent 等已声明字段；credential 另走专用动作 |
| `DELETE /oauth/accounts/{accountId}` | `deleteOAuthAccount` | 要求 delete plan/If-Match；执行现有 registry/runtime 级联 |
| `PUT /oauth/account-order` | `reorderOAuthAccounts` | 完整 ID 排列；集合不一致返回 conflict |
| `POST /oauth/login-flows` | `startOAuthLoginFlow` | provider=`claude|cursor|openai|xai|antigravity`；返回 flowId、authUrl/instruction、expiresAt |
| `POST /oauth/login-flows/{flowId}/complete` | `completeOAuthLoginFlow` | 提交 code/state 或 provider 规定完成信号；结果为 created/conflict/Operation |
| `POST /oauth/imports/preview` | `previewOAuthImport` | 格式=`openai|cpa|sub2api`，解析候选、错误和 identity 冲突，不写状态 |
| `POST /oauth/imports/{importId}/commit` | `commitOAuthImport` | keep/overwrite 决策逐项显式；一次性 plan，返回结果/Operation |
| `GET /oauth/invalid-accounts` | `listInvalidOAuthAccounts` | 与 TG invalid list 同一判定 |
| `POST /oauth/invalid-accounts/delete-plan` | `planInvalidOAuthAccountDeletion` | all 或显式 accountIds |
| `POST /oauth/invalid-accounts/delete` | `deleteInvalidOAuthAccounts` | plan token 一次性 commit |

OAuth 覆盖不得变成隐式 upsert。Control 的 replace plan/nonce 必须绑定 actor、候选 identity、旧/新 revision、10 分钟以内 TTL，并恒时比较；Telegram 仍保留原有 nonce、state action 和原文案。

导入预览请求的 `payloadEncoding` 可选 `json|base64`，默认 `json` 兼容旧请求；`payload` 为 write-only string。JSON 直接传原文，ZIP bytes 使用标准 Base64，沿同一 preview/commit 路径进入既有 parser，不另建上传系统。编码字符串最多 2,000,000 字符，Base64 解码后最多 1,500,000 bytes，非法编码返回 `422 VALIDATION_FAILED`。TG 批量导入保持整批 usage/save/quota evaluate 完成后先展示进度，再启动模型同步的原时点。

### 7.3 账号动作与模型

| method/path | operationId | 说明 |
|---|---|---|
| `POST /oauth/accounts/{id}/actions/refresh-token` | `refreshOAuthToken` | 异步或同步结果；遵守节流/错误分类 |
| `POST /oauth/accounts/{id}/actions/refresh-usage` | `refreshOAuthUsage` | 返回 Operation；部分数据成功仍落权威 cache |
| `POST /oauth/actions/refresh-usage` | `refreshAllOAuthUsage` | 账号集合快照、每轮每账号至多一次 |
| `POST /oauth/accounts/{id}/actions/reset-quota-plan` | `planOAuthQuotaReset` | 返回额度/credit 影响摘要和 plan token |
| `POST /oauth/accounts/{id}/actions/reset-quota` | `resetOAuthQuota` | 一次性 commit；不支持者返回 `UNSUPPORTED_VALUE` |
| `POST /oauth/accounts/{id}/actions/clear-errors` | `clearOAuthAccountErrors` | 清理该账号 runtime error/cooldown 的现有范围 |
| `POST /oauth/accounts/{id}/actions/clear-affinity` | `clearOAuthAccountAffinity` | 同时保持 server/client affinity 现有语义 |
| `POST /oauth/actions/clear-errors` | `clearAllOAuthErrors` | 全局显式 destructive action |
| `GET /oauth/accounts/{id}/models` | `listOAuthAccountModels` | 状态、disabled、cooldown、metadata source、context、service tier；API pageSize 独立于 TG 的 6 |
| `PATCH /oauth/accounts/{id}/models` | `updateOAuthAccountModels` | body 为显式 modelIds + disabled；支持批量，全有或全无 |
| `PATCH /oauth/accounts/{id}/models/settings` | `updateOAuthAccountModelSettings` | Cursor `maxContextDefault` 等账号级模型设置 |
| `POST /oauth/accounts/{id}/models/actions/sync` | `syncOAuthAccountModels` | 返回 Operation |

### 7.4 OAuth 设置

| method/path | operationId | 字段/语义 |
|---|---|---|
| `GET/PATCH /oauth/settings` | `getOAuthSettings` / `updateOAuthSettings` | `quotaMonitor.enabled/intervalSeconds/thresholdPercent`、`cchMode=disabled|dynamic` |
| `GET/PATCH /preferences/telegram/oauth` | `getTelegramOAuthPreferences` / `updateTelegramOAuthPreferences` | `usageDisplayMode=used|remaining`、`quotaProgressBar`；只在显式调用后影响 TG |

provider usage 仅允许已注册的 `providerId + providerPresetId` adapter；不得从任意 URL 动态调用。现有 13 个 preset 及 persistent retry-after/孤儿清理语义保持不变。

---

## 8. Channels 与 API Keys

### 8.1 ChannelControl

| method/path | operationId | 说明 |
|---|---|---|
| `GET /channels` | `listChannels` | filter/sort/page，health、enabled、protocol、provider、model 数、usage 摘要 |
| `POST /channels` | `createChannel` | preset/manual union：name、baseUrl、apiKey(writeOnly)、protocol、models、maxConcurrent、compatibility |
| `GET /channels/{channelId}` | `getChannel` | 脱敏 detail、model/status/month stats/provider usage |
| `PATCH /channels/{channelId}` | `updateChannel` | name/url/key/models/protocol/maxConcurrent/cc/omitTemperature/omitThinking；使用既有 validator 和原子级联 |
| `DELETE /channels/{channelId}` | `deleteChannel` | plan/If-Match；只能走 registry delete cascade |
| `PUT /channels/order` | `reorderChannels` | 只排 API channel；完整集合校验 |
| `GET /channel-catalog` | `getChannelCatalog` | provider/preset/protocol/features schema，不含凭据 |
| `POST /channel-model-discoveries` | `discoverChannelModels` | draft URL/key/protocol 或已有 channel；返回 Operation |
| `POST /channel-drafts/probes` | `probeChannelDraft` | **pre-save 语义**：沿用向导 initial probe 对 cooldown/error 的现有影响 |
| `POST /channels/{id}/diagnostic-probes` | `probeExistingChannel` | 严格复现基线矛盾：成功路径会清对应 cooldown，失败路径不新增错误，affinity 不变；API 文档不得虚称 observe-only |
| `POST /channels/{id}/actions/refresh-usage` | `refreshChannelProviderUsage` | 只对受支持 preset；返回 Operation |
| `POST /channels/{id}/actions/clear-errors` | `clearChannelErrors` | 与 TG 单渠道范围一致 |
| `POST /channels/actions/clear-errors` | `clearAllChannelErrors` | 全局显式动作 |
| `POST /channels/{id}/actions/clear-affinity` | `clearChannelAffinity` | server/client 两套均按现有语义清理 |
| `POST /channels/actions/clear-affinity` | `clearAllChannelAffinity` | 全局显式动作 |
| `GET/PATCH /channels/{id}/compatibility` | `getChannelCompatibility` / `updateChannelCompatibility` | feature mode、all-model/per-model override；保留 `auto|force` 等权威 enum |

create/update 的模型发现与保存是两步：发现结果不能自动持久化；客户端必须提交最终模型集合。URL protocol suffix 的 detect/adopt/force、base-only 修改及 API path 校验复用 `channel.url_utils`，不能在 router 重写。

### 8.2 ApiKeyControl

这里管理的是**下游推理 API Key**，不是 Management Session credential。

| method/path | operationId | 说明 |
|---|---|---|
| `GET /api-keys` | `listApiKeys` | name/order/enabled/source、permissions、media permissions、limit 摘要、month stats；value 仅 masked |
| `POST /api-keys` | `createApiKey` | mode=`generated|custom`，name；生成/自定义值只在响应中出现一次 |
| `GET /api-keys/{keyId}` | `getApiKey` | detail、limiter snapshot、allowedModels、model stats |
| `PATCH /api-keys/{keyId}` | `updateApiKey` | name（若既有支持）、enabled、allowImages、allowVideos、allowedModels、limit override |
| `DELETE /api-keys/{keyId}` | `deleteApiKey` | delete plan/If-Match；删除后 forget limiter state |
| `POST /api-keys/{keyId}/actions/generate-replacement-plan` | `planApiKeyRegeneration` | 预览影响，不返回新值 |
| `POST /api-keys/{keyId}/actions/generate-replacement` | `regenerateApiKey` | 一次性 commit，新值只返一次，旧值立即失效，runtime state 清理 |
| `PUT /api-keys/{keyId}/secret` | `replaceApiKeySecret` | custom secret(writeOnly)，同样清 runtime state |
| `PUT /api-keys/order` | `reorderApiKeys` | 与 TG sort save/reset 相同持久顺序语义 |
| `POST /api-keys/{keyId}/actions/reset-limiter` | `resetApiKeyLimiter` | 清该 key 在途之外的可清运行状态；不得伪造取消真实请求 |
| `GET /api-keys/{keyId}/stats` | `getApiKeyStats` | `log_db.apikey_model_stats` 对应数据 |

字段校验不得弱于基线：名称 `[A-Za-z0-9_.-]{1,64}`，custom key 为既有允许字符且长度 8～256，generated key 保持 `ccp-` 前缀。TG 列表每页 4 条、权限编辑暂存/保存/取消和排序状态机全部保持。

---

## 9. Stats、请求日志、媒体日志与留存

### 9.1 StatsControl

| method/path | operationId | 说明 |
|---|---|---|
| `GET /stats/summary` | `getStatsSummary` | period=`today|3d|7d|month|lifetime`，总体、family、tokens、cost、cache、latency、TPS |
| `GET /stats/breakdown` | `getStatsBreakdown` | dimension=`channel|model|apiKey`，period、sort/page |
| `GET /stats/models/{modelId}` | `getModelStats` | channel/model drilldown |
| `GET /stats/recent-calls` | `listRecentCalls` | 只读摘要，不代替日志 detail |
| `GET/PATCH /preferences/telegram/stats` | `getTelegramStatsPreferences` / `updateTelegramStatsPreferences` | `byChannel/byModel/byApiKey/cacheMisses/recentCalls`，缺失均为 true |

TG 继续接受 period `0|3|7|month` 和 dim `all|channel|model|apikey`；Adapter 映射到 control enum 后必须渲染原值。Control 不得使用 TG 的 `menu_cache`；TG scheduler 可以调用 Control 并继续缓存 `PERIOD_STATS/LIFETIME_STATS/DETAIL_STATS/WINDOW_STATS/HISTORY_TOTALS/BACKGROUND_JOBS`。

### 9.2 LogsControl

| method/path | operationId | 说明 |
|---|---|---|
| `GET /logs` | `listRequestLogs` | status、apiKey、model、channel、protocol、query、time range、sort/page；返回 retry-chain 和计费摘要 |
| `GET /logs/filter-options` | `getRequestLogFilterOptions` | 可选 key/model/channel/status 枚举与 counts |
| `GET /logs/{logId}` | `getRequestLog` | stages/attempts、usage、latency、billing、error、request/response body availability |
| `GET /logs/{logId}/body` | `getRequestLogBody` | kind=`request|response`，结构化 items、kind counts、search/sort/page；需有效 Management Session |
| `GET /logs/{logId}/body/items/{itemId}` | `getRequestLogBodyItem` | full item；沿用不可读 encrypted payload 的安全处理 |
| `GET /logs/{logId}/raw-body` | `getRequestLogRawBody` | 显式 body-read；只返回已允许的 request/response raw，不返回系统 credential |

API filter 状态由请求参数表达，不复用 TG 的 `loglist:`/`logfilter:`/`loginspect:` short-code。`log_inspector.py` 中与 Telegram 无关的解析、筛选、排序可自然下沉到 control；TG 保留原 wrapper/格式化，输出必须无差异。

### 9.3 MediaControl

| method/path | operationId | 说明 |
|---|---|---|
| `GET /media-logs` | `listMediaLogs` | status/provider/model/action=`generate|edit|extend`/time/page，含 progress、dimensions、cost/traffic 摘要 |
| `GET /media-logs/{mediaLogId}` | `getMediaLog` | job、paths、account、upstream request、timing、错误 detail |
| `GET /media-logs/{mediaLogId}/artifacts` | `listMediaArtifacts` | 可下载 artifact metadata |
| `GET /media-logs/{mediaLogId}/artifacts/{artifactId}` | `downloadMediaArtifact` | 鉴权流式下载缓存媒体；不存在/过期返回稳定错误 |

状态 enum 固定覆盖 `running|pending|success|failed|expired|cancelled`；TG 图标仍为 `⏳/✅/❌/⌛/⏹`，每页 6 条。v0.31.13 的 TG 缓存查看只有 `send_photo`/`send_video` 两个分支：`media_type == "video"` 走视频，其余均走图片；不存在 `sendDocument` 成功分支。API 的 artifact 下载能力不改变这一冻结事实。

### 9.4 RetentionControl

| method/path | operationId | 说明 |
|---|---|---|
| `GET /logs/retention` | `getLogRetentionSettings` | days/forever、`logStoreBodies`、当前数据量/策略摘要 |
| `PATCH /logs/retention` | `updateLogRetentionSettings` | 修改 policy 或 forever；仅修改策略，不隐式删除历史 |
| `POST /logs/retention/plans` | `createLogRetentionPlan` | scan 后返回 affected rows/files/bytes、planId、expiresAt；不删除 |
| `POST /logs/retention/plans/{planId}/commit` | `commitLogRetentionPlan` | 一次性、actor/revision 绑定；调用权威 apply，返回 Operation |
| `DELETE /logs/retention/plans/{planId}` | `cancelLogRetentionPlan` | 取消未执行 plan |

TG 的 8 字符 pending code、600 秒 TTL、确认/取消/进度原样保留；API planId 不得复用为 callback_data。

---

## 10. 模型映射、Metadata 与负载均衡

### 10.1 MappingControl

模型映射的运行时事实固定为：有效 ingress 为 `anthropic`、`openai-chat`、`openai-responses`；新管理面写 `global`，读取兼容 legacy；只解析一层；global 写同 alias 时清理 legacy 副本；默认模型在映射前应用。

| method/path | operationId | 说明 |
|---|---|---|
| `GET /model-mappings` | `listModelMappings` | global 合并视图，返回 alias/real/sourceLine；filter/page |
| `PUT /model-mappings/{alias}` | `putModelMapping` | body.realModel；alias != real；复用 set_mapping(global) |
| `DELETE /model-mappings/{alias}` | `deleteModelMapping` | 同时清同名 legacy，复用 remove_mapping(global) |
| `GET/PUT/DELETE /ingress-default-models/{ingress}` | `get/put/deleteIngressDefaultModel` | ingress 三枚举；真实模型校验 |
| `GET /models/inventory` | `listModelInventory` | 可用模型、family/provider/channel/account 来源 |
| `GET /model-metadata` | `listModelMetadata` | effective source、limits、pricing、long context、bindings |
| `GET /model-metadata/{modelId}` | `getModelMetadata` | detail 和 raw/effective 差异，不暴露 credential |
| `PUT /model-metadata/{modelId}/binding` | `putModelMetadataBinding` | scope=`global|oauth|api` 及 provider/account/channel 选择 |
| `DELETE /model-metadata/{modelId}/binding` | `deleteModelMetadataBinding` | 删除明确 binding |
| `POST /model-metadata/actions/sync` | `syncModelMetadata` | 全量/范围同步，单飞锁，返回 Operation |
| `GET /model-catalog` | `searchModelCatalog` | provider/query/page，供未来 Web UI 选择 |
| `GET/PUT/DELETE /compression-model` | `get/put/deleteCompressionModel` | compact rescue 模型设置/清空 |

TG 映射真实模型 picker 每页 10 条，metadata 每页 6 条；alias 编辑、删除确认、catalog 搜索、sync result、scope picker 和 compression model callback 路径必须保持。

### 10.2 LoadBalancingControl

| method/path | operationId | 说明 |
|---|---|---|
| `GET/PATCH /load-balancing` | `getLoadBalancing` / `updateLoadBalancingMode` | mode=`smart|order|priority` |
| `GET/PUT /load-balancing/channel-order` | `get/replaceChannelOrder` | unified channel order，完整集合校验 |
| `GET/PUT/DELETE /load-balancing/model-orders/{modelId}` | `get/replace/deleteModelChannelOrder` | per-model priority；删除恢复 effective default |
| `PUT /load-balancing/model-orders` | `bulkReplaceModelChannelOrders` | 显式 modelIds + order，原子批量 |
| `POST /affinity/actions/clear` | `clearAllAffinity` | 全部 server/client affinity |
| `POST /affinity/families/{family}/actions/clear` | `clearFamilyAffinity` | 保留 legacy family 清理范围 |

TG model list 每页 6 条，批量 model select 当前不分页；排序的 select/move top/bottom/up/down/reset/save/cancel 与文本输入状态不变。

---

## 11. 系统设置、代理与网络

### 11.1 显式 typed 设置资源

所有设置均为 `GET` + sparse `PATCH`，返回 effective 值和 revision；router 不得接受未声明字段。

| path | operationId 前缀 | 必须覆盖的字段 |
|---|---|---|
| `/settings/retry` | `RetrySettings` | `transient.enabled/maxExtraAttempts(1..5)/backoffSeconds(1..5项,0..60)`；errors=`openaiServerOverloaded/openaiServerError/claudeOverloaded/xaiUnavailable`；recovery=`oauthRefresh/invalidEncryptedContent/claudeContext1mFallback` |
| `/settings/timeouts` | `TimeoutSettings` | `connect/firstByte/idle/total`，正整数 |
| `/settings/error-cooldown` | `ErrorCooldownSettings` | `errorWindows`、`oauthGraceCount(0..100)`、`ladderMinIntervalSeconds(0..3600)`、`permanentMinAgeSeconds(0..86400)` |
| `/settings/scoring` | `ScoringSettings` | `emaAlpha(0..1)`、`recentWindow(1..1000)`、`errorPenaltyFactor(0..100)`、`explorationRate(0..1)` |
| `/settings/affinity` | `AffinitySettings` | `ttlMinutes(1..1440)` |
| `/settings/cch` | `CchSettings` | mode=`disabled|dynamic` |
| `/settings/concurrency` | `ConcurrencySettings` | `enabled`、`queueWaitSeconds>=0`、`defaultMaxConcurrent>=0` |
| `/settings/api-key-concurrency` | `ApiKeyConcurrencySettings` | `enabled`、`defaultMaxConcurrent>=0`、`defaultMaxQueue>=0`、`defaultQueueWaitSeconds>=0` |
| `/settings/quota-monitor` | `QuotaMonitorSettings` | `enabled`、`intervalSeconds`、`thresholdPercent`，与 OAuth 设置为同一 control，不复制 |
| `/settings/notifications` | `NotificationSettings` | 总开关及既有 event flags；未知 event 拒绝 |
| `/settings/openai-websocket` | `OpenAiWebSocketSettings` | `responsesUpstreamWsForOAuth`；修改时继续清理旧 transport 字段 |

API 字段可使用上表语义名映射现有根配置名，但 v1 schema 固定。所有数值 validator 必须与 TG 当前接受范围一致；TG 输入错误消息仍由 Adapter 用原字符串生成。

### 11.2 ContentBlacklistControl

| method/path | operationId | 说明 |
|---|---|---|
| `GET /content-blacklist` | `getContentBlacklist` | `default` 与 `byChannel`，保序 |
| `POST /content-blacklist/default` | `addDefaultBlacklistTerm` | 新增非空 term；重复处理与 TG 一致 |
| `DELETE /content-blacklist/default/{term}` | `deleteDefaultBlacklistTerm` | percent-encoded term |
| `POST /content-blacklist/channels/{channelId}` | `addChannelBlacklistTerm` | channel 必须存在 |
| `DELETE /content-blacklist/channels/{channelId}/{term}` | `deleteChannelBlacklistTerm` | 删除后清空空列表的现有语义 |

### 11.3 ProxyControl

| method/path | operationId | 说明 |
|---|---|---|
| `GET/POST /proxies` | `list/createProxies` | proxy CRUD，URL credential writeOnly，返回 masked URL、type、runtime stats |
| `GET/PATCH/DELETE /proxies/{proxyId}` | `get/update/deleteProxy` | 名称/URL；删除前返回引用冲突或 plan |
| `POST /proxies/{proxyId}/actions/test` | `testProxy` | 返回 Operation；latency/traffic/error 结构化 |
| `GET/POST /proxy-groups` | `list/createProxyGroups` | group 与有序 members |
| `GET/PATCH/DELETE /proxy-groups/{groupId}` | `get/update/deleteProxyGroup` | rename/members/clear；引用校验 |
| `POST /proxy-groups/{groupId}/actions/test` | `testProxyGroup` | 按成员顺序测试/故障转移摘要 |
| `GET/PATCH /proxy-routing` | `get/updateProxyRouting` | `default`、`directFallback`、function/account/channel/model 规则；target 必须为 direct/proxy/group |

保留 `parse_proxy_url`、`_mask_url`、Shadowsocks family 判定和 direct-fallback 语义。删除 proxy/group 必须同步处理路由引用，禁止留下悬空 target。TG proxy/group 每页 5 条，model routing picker 每页 8 条。

### 11.4 NetworkControl

| method/path | operationId | 说明 |
|---|---|---|
| `GET /network` | `getNetworkSettings` | DNS servers/cache TTL、legacy SOCKS5 脱敏状态、proxy/group/routing 摘要 |
| `POST /network/dns/tests` | `testDnsSettings` | parse + test，返回一次性 result/plan；不保存 |
| `POST /network/dns/commits` | `commitDnsSettings` | test plan + `force`；失败且 force=false 禁止保存 |
| `POST /network/dns/actions/sync-system` | `syncSystemDns` | 立即同步，返回最终 servers |
| `GET /network/dns/cache` | `listDnsCache` | host/IP/剩余 TTL，API 分页 |
| `DELETE /network/dns/cache` | `clearDnsCache` | 清全部缓存 |
| `POST /network/socks5/tests` | `testSocks5Settings` | normalize + async test，URL writeOnly |
| `POST /network/socks5/commits` | `commitSocks5Settings` | plan + force，保存并启用 |
| `PATCH /network/socks5` | `updateSocks5State` | 仅 enabled；无 URL 时启用返回 validation error |
| `GET/PATCH /network/monitor` | `get/updateNetworkMonitor` | enabled、interval、dns/socks5、core targets、channels 全局/逐项 |
| `GET /network/monitor/checks` | `listNetworkChecks` | state_db 历史检查 |
| `POST /network/monitor/actions/run` | `runNetworkMonitor` | 返回 Operation |

DNS/SOCKS test plan 必须绑定提交值、actor、revision 和短 TTL。API 的 `force` 与 TG “强制保存”语义一致，不能变成跳过 test。

---

## 12. Translation、状态告警、更新、Images/xAI

### 12.1 TranslationControl

`GET/PATCH /translation` 的 schema 必须显式覆盖：

- `enabled`、`model`、`fallbackModel`、`targetLanguage`；
- `timeoutSeconds`、`maxHistoryMessages`、`cacheTtlDays`、`cachePreloadCount`；
- `failureAlertThreshold`、`memoryCacheMaxMb`、`memoryCacheTtlSeconds`；
- `translateSystemMessages`；
- `scope.models[]`、`scope.channels[]`（空数组表示全部）；
- `modelOverrides[model].body`，拒绝 `_parrot_` 前缀键；
- 自定义 prompt 及 reset-to-default。

附加路由：

| method/path | operationId |
|---|---|
| `GET/PATCH /translation` | `getTranslationSettings` / `updateTranslationSettings` |
| `GET /translation/cache` | `getTranslationCacheStats` |
| `DELETE /translation/cache` | `clearTranslationCache` |
| `POST /translation/actions/test` | `testTranslation`（Operation） |
| `GET /translation/languages` | `listTranslationLanguages` |

TG model/scope picker 每页 10 条；开关、system message、model/fallback/lang、numeric fields、scope model/channel、model body/thinking、prompt、test、clear cache 的状态和确认原样保留。

### 12.2 StatusAlertControl

| method/path | operationId | 说明 |
|---|---|---|
| `GET/PATCH /status-alerts/settings` | `get/updateStatusAlertSettings` | enabled、intervalSeconds、targets=`claude|openai|cloudflare`、minImpact=`none|minor|major|critical` |
| `GET /status-alerts/incidents` | `listStatusIncidents` | active/history/muted filter |
| `POST /status-alerts/actions/refresh` | `refreshStatusAlerts` | 返回 Operation |
| `POST /status-alerts/incidents/{id}/actions/mute` | `muteStatusIncident` | 与现有 notification event 关系不变 |
| `DELETE /status-alerts/incidents/{id}/mute` | `unmuteStatusIncident` | 解除 muted |

### 12.3 UpdateControl

| method/path | operationId | 说明 |
|---|---|---|
| `GET/PATCH /updates/settings` | `get/updateSettings` | enabled、includePrerelease、autoUpdate、intervalSeconds、ignoredVersions |
| `POST /updates/actions/check` | `checkForUpdates` | 返回当前/候选/changelog；不隐式更新 |
| `PUT/DELETE /updates/ignored-versions/{version}` | `ignore/unignoreUpdateVersion` | 显式管理 ignoredVersions |
| `GET /updates/backups` | `listUpdateBackups` | 已有 backup metadata |
| `GET /updates/failure-log` | `getUpdateFailureLog` | 脱敏 failure log |
| `POST /updates/{version}/actions/stage` | `stageUpdate` | backup -> pull -> staged，返回 Operation |
| `POST /updates/staged/actions/restart` | `activateStagedUpdate` | 显式 plan/If-Match；重启后 health/rollback |
| `DELETE /updates/staged` | `cancelStagedUpdate` | 取消可取消的 staged 状态 |

不得提供绕过 backup/stage/health rollback 的“一步 shell command”接口。服务重启导致当前 session/operation 中断时，客户端通过 health 和新 session 恢复，不能返回虚假成功。

### 12.4 Image 与 xAI media settings

| method/path | operationId | 字段/行为 |
|---|---|---|
| `GET/PATCH /images/settings` | `get/updateImageSettings` | enabled、cacheEnabled、mainModel、toolModel、cachePath、cacheRetentionDays、cacheMaxBytes |
| `GET/PATCH /images/accounts/{accountId}` | `get/updateImageAccountState` | 账号 image enable 状态；不返 token |
| `GET/PATCH /xai/media-settings` | `get/updateXaiMediaSettings` | imageModels、videoModels、jobTtlSeconds、requestTimeoutSeconds |

路径、容量和 retention 使用现有 validator；API 不允许任意读取 cachePath 文件。缓存内容下载只经 media artifact 路由。

2026-09-16 产品修订取消 AG 图片支持，以下五个管理 operation 退役，不再出现在 OpenAPI、`/meta`、`/capabilities` 或能力 crosswalk；既有账户、历史数据不因路由退役而删除：

| 退役 method/path | operationId |
|---|---|
| `GET /antigravity/media-settings` | `getAntigravityMediaSettings` |
| `PATCH /antigravity/media-settings` | `updateAntigravityMediaSettings` |
| `POST /antigravity/media-models/image` | `addAntigravityMediaModel` |
| `PATCH /antigravity/media-models/image/{modelId}` | `renameAntigravityMediaModel` |
| `DELETE /antigravity/media-models/image/{modelId}` | `removeAntigravityMediaModel` |

### 12.5 SearchControl（2026-09-16 新增）

所有路径均位于 `/api/management/v1`，域 tag 为 `management-search`；使用既有 Management Session，不是推理搜索业务端点。

| method/path | operationId | 基本能力 / 行为 |
|---|---|---|
| `GET /search` | `getSearchSettings` | `management.read`；脱敏有效配置与 revision |
| `PATCH /search` | `updateSearchSettings` | `management.write`；普通 function/hosted 独立三态及默认参数 |
| `POST /search/backends` | `createSearchBackend` | `management.write`；新增固定身份来源 |
| `PATCH /search/backends/{backendId}` | `updateSearchBackend` | `management.write`；稀疏修改来源 |
| `DELETE /search/backends/{backendId}` | `deleteSearchBackend` | `management.write`；必填 If-Match，成功 204，仅删除该来源配置 |
| `GET /search/backends/{backendId}/accounts` | `getSearchBackendAccounts` | `management.read`；完整公共账户身份及人类标签，无 token |
| `PUT /search/priority` | `updateSearchPriority` | `management.write`；完整 ID 顺序列表，不改变来源身份 |
| `POST /search/test` | `testSearchBackend` | `management.write`；用户主动单来源测试，返回 202 Operation，不跨来源 failover |
| `GET /search/logs` | `listSearchLogs` | `management.read`；专用搜索日志的近期真实上游调用，`period=today|month`、可选 `sourceId`、`limit`（≤200）与 `offset` |
| `GET /search/stats` | `getSearchStats` | `management.read`；按来源聚合的调用次数/成功率/平均耗时/Token/已结算费用，`period` 与可选 `sourceId` |

- 查询参数白名单按路由声明：既有 8 个端点仍不接受任何查询参数，新增的两个只读日志端点各自声明 `period`/`sourceId`/`limit`/`offset`，未声明的参数按 `extra_forbidden` 拒绝。
- 搜索调用记账与费用：`local_web_log` 是请求的子表（随请求清理），已停止写入；搜索日志改为独立顶层表 `search_call_log`，**每次真实上游调用一行**（含跨来源/跨 Key 重试），带来源、凭据类型与序号、账户键、模型、耗时、结果数，以及按 `normalize_response_billing` + `estimate_cost` 得到的 Token 结算（xAI 优先真实 `cost_in_usd_ticks`）。没有 usage 的响应保持 `unpriced`，不伪造零费用。搜索消耗汇入独立「搜索统计」，不改动 `累计统计`/账户自然月/渠道统计的既有口径。

- 新增/修改来源携带 `apiKeys`、`addApiKeys` 或 `removeKeyIndices` 时，还必须有 `management.secrets.write`；密钥只写不回显。模型仅 OAuth 来源可设非空值。
- 配置变更支持 If-Match；删除要求 If-Match；测试支持 Idempotency-Key。`allowDisabledAccounts` 默认 false，仅允许手动停用账户参与搜索，不改变普通对话状态或绕过认证/配额失效。
- 生产发现从已挂载 OpenAPI 动态生成：search 的 10 个 action/method/path、七种 backend type 和三态 schema enum 可发现；“可发现”不代表来源探活成功。无需复制维护另一套生产发现清单。
- 纳入图片/视频独立管理、退役 OAuth 备用模型并新增搜索日志/统计后，操作总数为 **232 = 235 − 3**，独立路径数为 **171 = 173 − 2**；router 为 **23 = foundation + 22 个有序领域 router**。严格集合以 `production-operation-ids.txt` 为准；搜索的 method/path/基本能力/条件密钥能力映射见 `search-operation-manifest.tsv`。
- 搜索菜单是系统设置新增子页，crosswalk 归属现有 `TG-SYS-01`（`SettingsControl+SearchControl`）。历史 53 个 TG capability ID 不伪造增加冻结轨迹；新增搜索交互由专有行为测试覆盖。`TG-IMG-01` 包括四项现有 ImageControl 操作及共享的媒体来源读取/用途开关；`TG-XIM-01` 增加独立视频设置读取/修改。
- 此次批准的旧系统菜单 golden 变化仅为“🔎 搜索工具 / srch:show”一行，插在重试/返回主菜单行之前；仅更新五个相关 case 的当前覆盖层，保持冻结 v0.31.13 源轨迹、其他菜单正文、按钮、状态及副作用不变。

---

### 12.6 图片/视频独立管理（2026-09-16）

新增 `GET/PATCH /videos/settings`、`GET /media/{kind}/sources`、`PATCH /media/{kind}/sources/{sourceId}`，`kind` 为 `image` 或 `video`。读取需 READ；修改需 WRITE 和 `If-Match`，来源身份/代次变化拒绝旧版本写入。

`PATCH /images/settings` 和视频设置均接受 `models`（provider → 模型名数组，按 provider 合并），分别写 `image_models` / `video_models`，立即生效；原 `mainModel/toolModel` 字段退役。TG 不再编辑模型名称。图片、视频缓存和账户用途授权分开保存；迁移继承旧共享缓存值，但首次图片修改先冻结视频原值，不移动或删除历史文件。

## 13. Telegram Adapter 迁移规则

### 13.1 Strangler 原则

每个菜单按“冻结 control/TG characterization -> 抽出 control -> Telegram 改调 control并通过基线轨迹 -> API 接入同一 control -> 领域 parity -> 删除菜单内旧业务写逻辑”的顺序迁移。禁止 API 与 TG 各写一套实现，也禁止先重写 UI 再试图恢复文案。

保留在 Telegram 的内容：

- `bot.py` polling、update 解析、命令/callback dispatch、启动时 delete/set command 顺序；
- `ui.py` Telegram HTTP、EADDRNOTAVAIL 恢复、safe send/edit/answer/delete/download/upload、HTML 和显示格式化；
- `states.py` 的 chat 状态和 TTL；
- 所有正文模板、emoji、NBSP、按钮排列、callback 字符串、short-code；
- `menu_cache.py` 的单 scheduler、view token、message lock 和 TG 专用缓存；
- 交互草稿，例如排序中的选择、picker 页码、日志 filter draft、向导当前步骤。

必须下沉到 Control 的内容：

- 对 config/state/runtime 的领域读写；
- 业务 validator 和 exact identity/引用检查；
- 跨模块级联、探测副作用策略、retention/update/network 两阶段操作；
- 供两个 Adapter 共用的统计查询、日志结构解析和业务 DTO；
- 对 `oauth_manager`、`registry`、`cooldown`、`affinity`、`log_db`、`updater` 等业务模块的调用。

### 13.2 禁止的“等价改写”

以下即使肉眼显示近似，也属于失败：

- `\n`、空格、NBSP (`"\u00a0"`) 数量变化；HTML tag/escape/parse mode 变化；
- emoji、custom emoji、按钮文字、按钮行列、URL button 变化；
- callback prefix、参数顺序、page 编号、noop/back callback 变化；
- `sendMessage` 改 `editMessageText`，或相反；answer callback 的文字、alert 标志、调用次序变化；
- 错误由 edit 变 send、确认由单步变双步、state pop 时机变化；
- provider 网络等待进入 polling thread；统计查询绕过 scheduler；
- 为“修 bug”而改变已有渠道诊断探测的真实副作用：当前成功会 clear cooldown、失败不 record_error，尽管页面文案声称不修改冷却；
- 修正既有命令 `startswith`/dispatch 顺序等历史行为。若发现 bug，另立变更，不得夹带在本重构中。

---

## 14. Telegram 零变化完整功能清单

本节 53 个 `TG-*` ID 是必须有自动化轨迹的验收项。`aa035ea` 的裁决继续有效：以 v0.31.13 实际源码和真实可达路径为准，不把 Management API 名词反向伪造成 Telegram 分支。轨迹需覆盖基线实际存在的 callback family/state 分支的成功、取消、过期/非法输入和业务失败；同一模式可参数化，但不得只测“打开首页”。若清单中的名词同时来自 API 合同、而基线源码不存在对应 TG 成功分支，必须按第 19 节核实并在本文明确记录，以“入口不存在”或真实 fallback/非法行为的负向轨迹冻结；不得伪造 trace，也不得新增 Telegram 交互来凑齐清单。

### 14.1 核心、命令、主菜单

- **TG-CORE-01 启停/权限**：drop pending updates；delete commands 后串行 set commands；admin allow-list；unauthorized update 行为；polling offset；notifier hook；scheduler start/stop。
- **TG-CORE-02 命令菜单**：`start/menu/stats/logs/channels/oauth/keys/mapping/loadbalancing/proxy/settings/help` 的描述、顺序完全相同。
- **TG-CORE-03 文本命令 dispatch**：依次覆盖 `/start`、`/menu`、`/status`、`/stats`、`/logs`、`/channels`、`/oauth`、`/keys`、`/settings`、`/mapping`、`/loadbalancing`、`/proxy|/proxies`、`/oauth_defaults`、`/help`；保留当前 `startswith` 和判断顺序。
- **TG-CORE-04 callback dispatch**：所有 menu handler 的优先级、命中后 `True` 短路、未知 callback 行为。
- **TG-CORE-05 state**：每 chat 单状态、新值覆盖、600 秒过期、get/pop/cleanup/clear；text/document state 的分派与取消。
- **TG-CORE-06 UI transport**：send/edit/answer/delete、文件下载/图片/视频/文档、safe parse fallback、EADDRNOTAVAIL session rebuild、按钮 label 截断、callback short-code 64 字节约束。
- **TG-MAIN-01**：welcome/start、main show/edit/back、首次运行 banner、overview/address/lifetime/quota hot count；按钮 callback 固定为 `menu:stats`、`menu:logs`、`menu:oauth`、`menu:channel`、`map:show`、`menu:loadbalancing`、`menu:apikey`、`menu:settings`。
- **TG-HELP-01 / TG-STATUS-01**：help 全文/按钮/version；status 的 uptime、channels、problem/fastest、quota warning、today/month TPS、limiter/concurrency 及 refresh 行为。

### 14.2 OAuth 及相关设置

- **TG-OA-01 列表/详情**：`menu:oauth`、`oa:page:*`、`oa:view:*`，filter=`all/available/quota/invalid`，每页 4；排序 select/move/reset/save/cancel；所有 provider 显示和月统计。
- **TG-OA-02 usage**：token/usage refresh、refresh all 进度、窗口/额度/credit/本地账单、used/remaining、宽度 10 的 `█/░` progress bar、7 个 NBSP indent；后台 refresh 单飞且 handler 不等网络。
- **TG-OA-03 runtime 动作**：toggle、maxConcurrent、clear errors/all、clear affinity、quota reset ask/confirm/error，不支持 quota 的显示。2026-09-23 当前能力追加Claude官方重置 `oa:claude_reset_ask:* / confirm:* / execute:*`（完整前缀均为`oa:claude_reset_`）：cedar成功/重复、juniper未确认/重复、非法阶段/过期三个严格回放case归属本ID。新轨迹位于`fixtures/tg_contract/claude-reset-2026-09-23/additions/oauth.jsonl`，由当前loader合并；不改v0.31.13历史golden，不把新callback当作ignore或retired。
- **TG-OA-04 新增/登录**：Claude、Cursor、OpenAI、xAI、Antigravity 的 login/regenerate/code/done，JSON 和 refresh token 输入，所有 auth error、取消和返回。
- **TG-OA-05 identity 覆盖**：`oa:overwrite:confirm:{nonce}|cancel`、state `oa_oauth_overwrite_confirm`、恒时比较、过期/错 chat/重复点击、pop 时机和 exact identity 结果。
- **TG-OA-06 import/invalid**：OpenAI/cpa/sub2api preview、document/text、commit、overwrite confirm/cancel、sync wait/result；invalid list toggle/remove selected/all。
- **TG-OA-07 Cursor models**：`oa:cursor_models/model/disable/...`、`oam:*` list/detail/toggle/bulk clear/invert/save/cancel/sync/maxctx；每页 6，状态排序和 metadata source。
- **TG-ODM-01 retired（2026-09-16）**：冻结历史轨迹保留，但不再代表现行能力。crosswalk 明确 `retired`、无 operation；旧 `odm:*` / `/oauth_defaults` 只读提示。`getOAuthDefaultModels`、`replaceOAuthDefaultModels`、`discoverOAuthDefaultModels` 及对应 GET/PUT `/oauth/default-models/{family}`、POST `/oauth/default-models/{family}/actions/discover` 全部移除（404），控制方法及写入代码同时删除。首次无目录不得回落到配置或协议静态名单，需明确提示同步上游；LKG、媒体与协议资料保留。
- **TG-OA-SET-01**：quota monitor 开关/interval/threshold、CCH、usage mode、quota progress、跳转 image/default/xAI settings；所有 config 默认显示不变。
- **TG-XIM-01**：`xim:show` 及 image/video/ttl/timeout 四个输入状态，clear aliases 和 duration 解析。

### 14.3 Channels 与 API Keys

- **TG-CH-01 列表/详情**：`menu:channel`、`ch:page/view/toggle/usage`，每页 4；provider icon、protocol、health、model、monthly、usage reset 显示；排序完整状态流。
- **TG-CH-02 新建向导**：`chw:start/brand/brands/preset/proto/adopt/force/back/key/models/discover/page/toggle/all/invert/manual/confirm/test/skip/save/cancel`；name/url/key/model state 和返回路径。
- **TG-CH-03 探测陷阱**：向导单个/全部 initial probe 的失败 record_error 与成功 clear；已存在渠道 `ch:test/t1/tall` 必须继续显示“本次测试不会修改冷却状态，只反映联通性。”，但实际成功路径仍按当前共用 helper clear 对应 cooldown、失败路径不 record_error；成功/失败消息自动删除时间 8s/30s。必须分别锁定文案 trace 与副作用 trace，禁止在本重构中修正二者矛盾。
- **TG-CH-04 编辑/兼容**：name/url/base-only/protocol/key/models/maxConcurrent/CC/omit temperature/omit thinking；compat feature 的 auto/force、all models/per-model；所有 validation/error 文案。
- **TG-CH-05 清理/删除**：single/all errors、single/all affinity、delete ask/exec/cancel、registry 全级联。
- **TG-AK-01 列表/创建**：`menu:apikey`、`ak:page/view/add/add_auto/add_custom`，每页 4；name/key 输入校验；生成结果只显示一次。
- **TG-AK-02 修改/删除**：regen ask/exec、rekey、delete ask/exec、enabled、image/video toggles；runtime limiter 清理。
- **TG-AK-03 permission/limit**：allowed model picker clear/save/cancel；limit view/toggle/edit/reset，concurrent/queue/wait parsing；月度 model stats。
- **TG-AK-04 order**：sort start/select/move top/bottom/up/down/reset/save/cancel 及状态过期。

### 14.4 Stats、logs、media

- **TG-CACHE-01**：唯一 `tg-stats-scheduler`、BJT today/month 边界、初始化文案“统计正在初始化，请稍后再试”、snapshot cache、subscriber、view token 防旧结果覆盖、message lock、test reset。
- **TG-STATS-01**：`stats:view:<0|3|7|month>:<all|channel|model|apikey>` 的汇总/展开、family、channel/model/key、cache miss/recent calls、loading/error；正文和按钮逐字节。
- **TG-STATS-02**：`stats:vis` / `stats:vistog` 五项 `byChannel/byModel/byApiKey/cacheMisses/recentCalls`，默认 true，基础信息始终显示。
- **TG-LOG-01 列表**：`menu:logs`、page/refresh/query/queryclear/list，列表每页 6；status/protocol/transport/retry chain/preview、filter summary 和 suffix banner。
- **TG-LOG-02 filter**：server-side `loglist:` / `logfilter:` short state；v0.31.13 的 TG 成功选项只有 apiKey/model/channel，包含 toggle/all/invert/confirm/cancel/clear 与 search text state；`status` 是 Management API 查询能力，TG 不存在对应 picker/成功 callback，对人为 `logs:filter:status:*` 入口冻结现有“筛选状态已失效”负向轨迹。
- **TG-LOG-03 detail**：`logs:detail/dpage`，stage/round/attempt、usage/price/cost formula/error；detail pages 与 back state。
- **TG-LOG-04 inspector**：request/response/body/ins/full/search，检查器每页 6、item preview 1500 字符、kind counts/filter/sort、full item、encrypted/unreadable body sanitize。
- **TG-MEDIA-01**：`media:logs/page/refresh/detail/view`，每页 6；running/pending/success/failed/expired/cancelled 图标；生成/编辑/延长；cached image/video 重发和缺失错误。v0.31.13 无 TG document 重发分支；人为构造的非 video `media_type` 仍按现有图片路径处理，需以实际 fallback 轨迹锁定，禁止伪造 `sendDocument`。

### 14.5 Settings、mapping、routing 和运维

- **TG-SYS-01 首页**：`menu:settings` 与所有子入口、当前值摘要、back/home 布局。
- **TG-SYS-02 retry/timeouts/error ladder**：所有 `sys:retry:*`、四个 timeout、errorWindows、OAuth grace、ladder interval、permanent age；输入范围与逐条错误。
- **TG-SYS-03 scoring/affinity/CCH/channel selection/quota**：field picker、enum、开关、interval/threshold；未知 field 错误。
- **TG-SYS-04 notification/blacklist**：总开关、event toggles；default/byChannel 新增删除确认、短码和重复/空值处理。
- **TG-SYS-05 retention**：body storage toggle、days/forever、scan、8 字符 plan、600 秒 TTL、plan/commit/cancel/progress、过期/错 chat/重复 commit。
- **TG-SYS-06 network**：DNS edit/test/save/force/sync/cache/clear；SOCKS edit/test/save/force/toggle；confirm state 过期与 failure warning。
- **TG-SYS-07 monitor**：enabled/dns/socks toggles、interval、core claude/openai/cloudflare、channels all/individual、run now/history。
- **TG-SYS-08 concurrency/limiter/WS**：channel concurrency total/queue/default；API key limiter enabled/default concurrent/queue/wait/runtime；HTTP Responses -> OAuth WS toggle及旧字段清理。
- **TG-MAP-01 mappings/defaults**：`map:show/line/item/add/alias/edit_real/pick_real/rm/rm_ok/set_default/pick_default/clear_default/page*`；真实模型每页 10、单层 global/legacy 语义。
- **TG-MAP-02 metadata/compact**：inventory/scope/provider/models/catalog/search/result/binding save/delete/sync/unmatched/detail；metadata 每页 6；compression pick/clear。
- **TG-LB-01**：smart/order/priority，channel/model order，model 每页 6，批量选择，move/reset/save/cancel/text input，all/family affinity clear confirm。
- **TG-PX-01 proxies**：`px:show/page/view/add/test/delete`，每页 5，URL mask/type/stats、name/url state、确认分支。
- **TG-PX-02 groups/routing**：group 每页 5，member picker/add/remove/clear/save/edit/test/delete；default/directFallback、function/account/channel/model rules，model picker 每页 8，引用清理。
- **TG-TL-01**：translation 主设置、模型/备用/语言、numeric、system message、scope models/channels、model body/thinking、prompt reset、test、cache clear confirm；picker 每页 10。
- **TG-STAT-01**：status alert enabled/targets/interval/minImpact/refresh/history/mute/muted list/unmute；provider 和 impact enum。
- **TG-UPD-01**：enabled/pre-release/auto/interval、refresh、ignore/unignore/clear、backups/failure log、double-confirm update、stage/restart/cancel、health/rollback。
- **TG-IMG-01**：images enabled/cache、main/tool model、cache path/retention/max、OAuth accounts toggles、view image、media log navigation。

### 14.6 基线轨迹格式

后续实施必须在改菜单前，从基准 commit 生成并提交：

```text
src/tests/fixtures/tg_contract/v0.31.13/manifest.jsonl
```

每行至少包含 `caseId`、入口 update、初始 config/state/runtime fixture、按顺序的 Telegram API method 与**原始 payload**、每步 state、最终业务状态和预期异常。动态时间、随机数、message id、网络结果必须通过 fake clock/random/transport 固定；禁止通过 trim、HTML normalize、排序 keyboard、忽略 parse_mode 或删除空字段来消除差异。

比较规则：

1. `sendMessage/editMessageText/answerCallbackQuery/deleteMessage/sendPhoto/sendVideo/sendDocument/setMyCommands/deleteMyCommands` 方法序列相同；
2. payload 的 UTF-8 字符串、字段存在性、布尔值、inline keyboard 行列及每个 callback_data 相同；
3. state action/data/pop 时机相同；
4. config/state/runtime 副作用相同；
5. 仅测试固定的 Telegram message id、clock 和随机值可由 fixture 提供，比较器本身无忽略名单。

现有 recorded-payload 测试是起点，不是“完整”证明；必须用 dispatch callback/state pattern 集合与 manifest 覆盖集合做双向相等检查，防止新旧分支漏测。

---

## 15. Management API 完整性验收清单

### 15.1 operation 集合门禁

OpenAPI 中 `/api/management/v1` 的 operationId 集合必须与第 4～12 节表格的集合完全相等；允许按 HTTP 限制把写成 `GET/PATCH` 的表格行展开成两个明确 operationId，但必须在实现的 expected manifest 中列明。不得出现：

- 缺失 operation；
- 无 schema 的 catch-all endpoint；
- 绕过 session 的领域 endpoint；
- router 直接 import 业务模块；
- 返回 config raw 或敏感值的 debug endpoint。

建议固定：

```text
src/tests/fixtures/management_api/production-operation-ids.txt
```

测试从 `/openapi.json` 提取实际集合与其逐行比较。`v1-operation-ids.txt` 仅是 P0/foundation 的 9-operation 基础清单，不是完整生产 operation 清单。

### 15.2 operation 功能测试与横切门禁

每个 operationId 至少验证与自身语义相关的最小功能闭环：

1. route 在实际应用组合中可达，happy path 的 status、response schema 和字段类型正确；
2. request schema 拒绝该 operation 已声明的非法 enum、范围或未知字段；资源型 operation 覆盖适用的 missing/conflict，而不是为不适用的分支凑矩阵；
3. Adapter 调用预期的 Control 用例且传入当前 principal/context；查询结果正确，mutation 的 config/state/runtime 权威副作用和业务级联正确；
4. 长动作按合同返回 Operation 并可到达终态；列表 operation 覆盖其已声明的 filter/sort/page。

认证和已知秘密边界采用集中式横切测试，不再给每个 operation 复制十项安全矩阵：证明认证引导路由以外的全部领域 route 使用统一 Session 依赖，固定管理密钥/TG approval 只能换 Session，推理 API Key 不能访问管理资源；检查声明为 secret 的 request schema 为 `writeOnly`，一次返回操作及后续 GET 符合第 5.3 节，并对公共错误、Operation 和已知 URL credential 做代表性结构化测试。测试到此边界即收口，不扩展为第 4.6 节的任意 nested key、自由文本、转义 JSON、命名变体、递归异常链或相邻 probe 审计。

### 15.3 领域级 parity

对 OAuth、channel、API key、mapping、LB、proxy、retention、network、translation、status、update 各选至少一个读和一个写 fixture：

- 路径 A：模拟 Telegram callback/text，调用迁移后的菜单；
- 路径 B：从相同初始快照调用 Management API；
- 比较最终 config/state/runtime/业务 DTO；
- 路径 A 另与 TG baseline payload 逐字节比较。

Parity 比较的是共享业务结果；不得要求 Management API 返回 Telegram HTML，也不得让 Telegram 使用 API 的 HTTP error message。

---

## 16. 多 Agent 实施包与合并顺序

所有 Agent 从同一基线/集成分支开始；一个文件同一时段只有一个 owner。领域 Agent 不编辑 `server.py`、顶层 router 聚合器、统一错误表或其他领域菜单，避免冲突。

| 包 | 独占范围 | 交付/门禁 | 明确不做 |
|---|---|---|---|
| **P0 合同与组合根** | `management_auth/*`、control context/errors/operations、API common/deps/error/schema base、`server.py`、router 聚合、依赖检查 | session 两 grant、OpenAPI/error/operation、组合根测试；给领域提供稳定接口 | 领域 endpoint/菜单迁移 |
| **P1 OAuth** | control/API OAuth、oauth schemas/tests；`oauth_menu.py`、`oauth_account_models_menu.py`（`oauth_defaults_menu.py` 已退役删除） | 第 7 节、TG-OA/ODM；identity/usage/model parity | channel/API key/通用 auth；不改 `xai_imagine_menu.py` |
| **P2 Channels** | control/API channels；`channel_menu.py`、`channel_wizard.py` | 第 8.1 节、TG-CH；两种 probe 副作用和删除级联 | LB/mapping/proxy |
| **P3 API Keys** | control/API API keys；`apikey_menu.py` | 第 8.2 节、TG-AK；secret one-shot、limiter parity | management auth |
| **P4 Observability** | overview/status/stats/logs/media/retention control/API；`status_menu.py`、`stats_menu.py`、`logs_menu.py`、`media_logs_menu.py`、`menu_cache.py`、`log_inspector.py` 的最小下沉 | 第 6、9 节、TG-CACHE/STATS/LOG/MEDIA；交付 retention control/API 但不改 `system_menu.py` | 通用 DB 重构；`system_menu.py` 由 P6 独占 |
| **P5 Models & routing** | mapping/LB/proxy control/API；对应三个 TG menu | 第 10、11.3 节、TG-MAP/LB/PX | channel CRUD |
| **P6 System & network** | typed system/content blacklist/network control/API；独占整个 `system_menu.py` 的适配迁移，包含接入 P4 已完成的 retention control | 第 11.1/11.2/11.4、TG-SYS（含 retention TG trace） | update/translation/status；开始前必须基于已合并 P4 |
| **P7 Auxiliary settings** | translation/status alerts/update/images/xAI media control/API；独占 `translation_menu.py`、`status_alert_menu.py`、`update_menu.py`、`image_menu.py`、`xai_imagine_menu.py` | 第 12 节、TG-TL/STAT/UPD/IMG/XIM | OAuth account 核心及 `system_menu.py` |
| **P8 验收整合** | expected manifests、依赖/行数/OpenAPI/TG diff 门禁；只解决集成冲突 | 第 14/15 全绿、全套测试、最终审计表 | 新功能、UI 美化、顺手修 bug |

合并顺序：P0 -> P1/P2/P3/P4/P5/P7 可在各自独立 worktree 并行 -> 合并并验收 P4 -> P6 -> P8。每包必须 rebase 到最新集成分支并重新生成**比较结果**（不得重写 baseline fixture）。P6 因独占 `system_menu.py` 且接入 retention control，明确依赖 P4，不得与 P4 并行。顶层 router、新公共 schema、共享测试 fixture 和统一错误表只由 P0 owner 或集成 owner修改；领域 Agent 只提交自己的 router/schema/tests，如需公共变更提交接口请求而不是直接编辑共享文件。

### 16.1 每包审查清单

- [ ] 所有新 `.py` 源码文件 `<= 1000` 行；接近上限时按资源拆分，不创建 `utils2.py` 式垃圾桶。
- [ ] API router 只调 control；Telegram 新代码只调 control；control 不含 transport/UI。
- [ ] mutation 只有一个权威实现，旧菜单直写已删除或有带到期包的临时 allow-list。
- [ ] 已知 secret 字段、一次返回、URL 结构化遮蔽及公共错误/Operation 满足第 4.5、5.3 节；不以第 4.6 节候选阻断。
- [ ] 本包全部 TG case baseline == actual；无 snapshot 更新。
- [ ] API expected operation、schema、Session 接线、Control 调用和业务副作用测试齐全。
- [ ] 未修改真实 config，测试使用 `tmp_path`、fake account/credential/transport。

---

## 17. 分阶段门禁与最终验收命令合同

### Gate A：冻结基线

- 确认基准 commit 和 branch；从独立只读 worktree 运行 TG characterization；
- 固定 clock/random/network，生成第 14.6 节 manifest；
- 记录当前全套 `2524 passed` 是已知输入，最终仍需在目标分支重跑，不能只引用该数字。

### Gate B：结构、组合根与基础认证边界

- 静态依赖检查：API 不 import 业务/TG；control 不 import FastAPI/TG；业务不反向 import management；
- 新源码文件行数检查，所有新增 `.py` 文件不超过 1000 行且按领域拆分；
- 实际应用组合根挂载全部领域 router；TG/API 注入同一 Control/业务依赖，领域 route 不只存在于测试 app；
- 集中验证所有领域 route 使用有效 Management Session，两个 grant 只换 Session，推理 API Key 不被接受；
- 检查已声明 secret 的 `writeOnly`、一次返回/后续 GET、无 raw config dump、已知 URL credential 结构化遮蔽，以及公共错误/Operation 不主动包含 raw exception 或 credential。

第 4.6 节的 replay/绝对过期/rate limit/CSRF/Origin/cookie/TLS/network exposure 与通用文本/异常链扫描明确不在 Gate B 中，不得恢复为本轮阻断项。

### Gate C：领域功能

- 第 15.1 节 OpenAPI operation set 精确匹配；
- 所有 route schema 和 control integration tests；
- 每个领域 parity；
- 特别验证 channel delete cascade、两种 probe、OAuth identity overwrite、retention、network force、update stage/rollback。

### Gate D：Telegram 绝对零变化

- 第 14 节每个 case 有测试；callback/state pattern 与 manifest 双向覆盖；
- baseline 与目标分支的 transport trace 逐字节相同；
- 现有全部 TG 测试无修改预期通过；
- 新的 `mauth:` 带外批准测试单独运行，并证明未改变任何既有入口的 trace。

### Gate E：全量与运行环境

所有实现、开发服务、smoke 和测试均只在隔离 worktree/开发副本中进行，使用临时配置、临时数据库及 fake/专用凭据；不得读取或写入生产 `/opt/src-space/parrot`，不得连接、重启或改变生产进程与生产数据。

完整 suite 必须通过项目规定的 `src/tests/isolated_pytest.py` 启动，并且测试进程必须显式 **unset `PARROT_NO_REFRESH`**（例如 `env -u PARROT_NO_REFRESH ...`）；该变量会短路显式刷新测试，已在基线实测造成 11 项误失败。测试隔离器负责阻断真实 provider/TG。`PARROT_NO_REFRESH=1` 只允许用于隔离开发副本中的长期服务及其 `127.0.0.1:22123` smoke；smoke 只使用 fake/专用凭据并覆盖 session -> meta -> 一个只读 endpoint -> revoke，且不替代自动化门禁。

最终验收报告必须逐项列出：

```text
Gate / checklist ID | PASS/FAIL | test/path | decisive evidence
```

不得只报告“pytest passed”。任何一项 FAIL、SKIP、无证据或 Telegram diff 都不得合并。

---

## 18. 生命周期、性能与兼容约束

- `server.py` 原有初始化顺序保持：state/network/bootstrap DNS/log/image/translation/background；provider usage startup refresh 在 TG 启动前调度且非阻塞；management 初始化不得读取供应商 secret 做探测。
- 原后台节奏保持：WAL/pending/affinity cleanup 300s、OAuth refresh/quota 60s、cooldown probe 30s；不得为 Management API 再启动一套重复 worker。
- stats SQL 不能进入 TG polling 线程；API 重查询要么走既有 DB 快照能力，要么返回 Operation，不抢占唯一 TG scheduler 导致 UI 时序改变。
- `config.update`、state DB、SQLite WAL 和现有 lifecycle lock 仍是权威并发边界；Control 组合它们，不绕开锁。
- 除新增命名空间明确、可回填默认值的 `management` 配置外，Management API 不迁移、重命名或改写现有 config schema；API DTO 与 config storage schema 分离。
- Management API 新增失败不得影响推理 API 可用性；管理 router/session store 初始化失败应明确记录并 fail closed，而非阻止核心代理启动，除非损坏共享状态会导致不安全写入。
- TG 带外批准是唯一允许的新 Telegram surface，使用隔离 namespace；既有 `/help`、命令列表和主菜单不得为它增加入口。

---

## 19. 权威语义冲突的处理

实现中若发现本文 endpoint 与源码能力不一致，按以下顺序处理：

1. 先确认基准 commit 上的实际业务函数、配置原子性和 TG 可达路径；
2. 若只是 DTO/HTTP 表达差异，在不改变本文件可观察结果的前提下由领域 control 适配；
3. 若会改变 Telegram 行为、删减 API 能力、改变安全边界或新增依赖，停止该包并提交明确冲突给集成 owner；
4. 不得用通用 config endpoint、router 直连业务、复制菜单逻辑或放宽测试来“完成”清单；
5. 只有经主控更新本文规范后，才允许改变公开 v1 合同或零变化标准。

本文冻结的是 v0.31.13 的既有 Telegram 合同和本轮 Management API v1 功能合同。冲突处理始终遵循本轮优先级：先保 TG 原版一致、API 完整可达、共享 Control 与正确副作用，再守住第 4.5/5.3 节基础边界；不得以第 4.6 节后续安全候选改写或阻断这些主线。后续 Web UI 必须消费本 API，不得反向要求 Telegram 菜单、业务模块或 config 为前端提供私有捷径。
