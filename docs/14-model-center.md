# 14 — 模型中心：使用、API 与兼容

本文说明模型中心开发分支的行为与兼容边界，不表示已发布、部署或完成线上验收。TG 与 Management API 复用同一生命周期中的 `ModelCenterControl` 及专业 controls，不各自维护配置副本。

## 模型身份、状态与来源

- 对话模型以真实客户端 `modelId` 聚合来源；`outboundModel` 是当前来源实际发给上游的名称，不是独立全局别名。
- 来源使用已有公开 OAuth accountId / API channelId。内部 `api:<name>` 等持久化键不作为客户端提交的身份。
- 图片/视频按模型类别及实际来源消歧；全局 provider 名单、API 渠道模型和 OAuth 账户专属名单分别保留其归属。Antigravity 图片支持已退役。
- `resourceKey` 是服务器生成的 opaque 键。客户端应使用查询返回值，不解析、拼接或按名字猜测。
- 全局禁用阻止所有来源；来源禁用只影响该来源。恢复全局启用不清除来源/账户原有禁用。API 渠道模型禁用不删除路由。
- 隐藏只影响下游发现，别名随目标隐藏；合法的真实名/别名显式调用仍受原 Key、协议、账户和来源权限约束。隐藏不等于禁用。
- 管理目录包括禁用项及来源的最后成功目录。它与 `/v1/models` 的下游发现用途不同；媒体维持原发现规则，不自动全部加入或按同名全部排除。

## Telegram 模型中心（2026-09-16）

- 不再有独立「模型设置」页。对话列表直接提供「同步元数据」「同步上游模型」；压缩指定在对话模型详情里即时设置，当前压缩模型详情可清除指定。
- 元数据同步刷新公共目录并匹配元数据，保留人工匹配和手工字段；不等同于上游可用模型目录同步。
- OAuth 自动模型同步覆盖 Claude、OpenAI、Grok、Antigravity、Cursor 和 WorkBuddy：后台每 60 秒检查到期，距成功同步 6 小时再拉取，失败后 15 分钟重试；缺少模型目录或原生元数据时提前补齐。普通 API 渠道仍由手动同步触发。
- 自动批次与模型中心手动批次中，只要成功保存了模型增删或原生模型元数据变化，就在全部来源处理结束后统一拉取一次 models.dev 公共目录并重新匹配；不会按账号重复拉取。仅刷新时间或顺序变化、无变化/304、全部失败或批次取消不触发。部分来源失败但其他来源有更新仍触发。该自动后续步骤遵守 `pricing.enabled/autoUpdate`，原 24 小时周期刷新保留；下载失败沿用本地目录匹配，匹配失败不撤销已保存的模型目录。手动任务报告单独显示元数据结果。
- 上游同步不带来源筛选时覆盖所有当前 API 渠道和 OAuth 账户（含空目录账户），选定来源时仅同步该来源。查询、状态筛选和多选不缩小同步范围。操作后台执行，任务页分别展示成功、部分失败、失败及逐来源结果。
- API 同步仅把实时发现的新模型追加到目录，不覆盖手工 alias→real、既有顺序、停用状态或元数据；空结果、错误、仅静态目录不覆盖原配置。OAuth 沿用账户正式同步，失败继续用最后成功目录。OAuth 备用模型入口、管理 API、配置和运行时回落均退役，首次没有目录需成功同步后才能参与普通对话路由；媒体模型配置不受影响。
- 对话列表显示当前保留日志范围的累计用量、请求成功率/失败数及 TPS。输入含普通输入、缓存写入和缓存读取；缓存占比为读取量 / 总输入。TPS 沿用实际输出与有效耗时加权算法，不对来源均速再做算术平均；没有测量不显示虚假零值。
- 统计按实际执行来源/上游模型归属，来源筛选只投影当前来源。全库批量分组快照由现有统计协调器后台单航班加载，所有分页共享缓存；菜单不执行 SQL。冷加载显示提示并仅补全仍停留的页面；刷新失败保留旧快照。累计只涵盖尚保留的日志，不代表已删除日志的永久总量。API 的多个公开名若指向同一执行路由，会展示相同执行累计，不能相加作全局总量；历史公开名曾改指向的份额未知，不以当前别名映射倒推。
- 别名页无查询和模型设置，「新增别名 / 返回主菜单」同排，原有即时改名/目标选择与分页保持。
- 图片页「多媒体日志 / 返回主菜单」同排；视频页「设置任务 TTL / 多媒体日志」同排。两者无模型设置按钮，不再显示未知产物数量/大小注释和底部历史统计说明；后台原始统计字段、独立用途与缓存控制不变。

## Management API：统一查询与状态

前缀均为 `/api/management/v1`。沿用已有管理 Session Bearer、Origin、Capability 与审计保护。

### 查询与详情

```http
GET /models?type=chat&text=example&sourceType=api&sourceId=<channelId>&page=1&pageSize=50
GET /models/<resourceKey>
```

| 参数 | 范围 |
|---|---|
| `type` | `chat` / `image` / `video`，可重复传参 |
| `text` | 匹配真实名、别名、出站名或来源显示名称，最长 300 字符 |
| `sourceType`、`sourceId` | OAuth/API 来源必须成对提供；全局范围不带 ID |
| `status` | `enabled` / `disabled` / `visible` / `hidden`，可重复传参 |
| `page` | 从 1 开始 |
| `pageSize` | 1–200，默认 50；独立于 TG 分页 |

不支持 provider、protocol、高级排序等额外查询参数。非法查询返回 422，实际资源不存在返回 404，不回退为“全部来源”。

默认保持对话 / 图片 / 视频分组；对话组先返回存在启用来源的模型，再返回停用或无可用来源的模型，各组内按名称稳定排序。指定 OAuth/API 来源时只按该来源的 `effectiveRoutable` 判断，不能借用另一来源的可用性；排序在分页前完成，不改变总数、既有状态筛选含义或资源/revision。这里是配置生效状态，不代表主动上游健康探测，也不把下游隐藏当作不可调用。管理库存保留不可用项供重新启用；发现接口不列出没有可用来源的模型。

列表返回 `data[]` 和 `meta.{page,pageSize,total,hasNext,revision}`；详情为 `data`。模型包含 `identity/resourceKey/modelId/aliases/globalEnabled/visible/commonMetadata/sources/editable/revision`。多来源有效值分别放在 `sources[].effectiveMetadata/valueSource/constrainedBy`，不能挑一个来源冒充全局有效值。状态写使用列表读取的 `meta.revision`，不是自行计算的版本。

### 显式目标状态

```http
PATCH /models/actions/state
If-Match: <读取时的revision>
Content-Type: application/json

{
  "scope": {"type": "api", "id": "<channelId>"},
  "selection": {"mode": "ids", "modelIds": ["example-a", "example-b"]},
  "target": {"enabled": false}
}
```

- `scope.type=global` 不带 ID；`oauth/api` 必须带公开 ID。
- `target` 恰好包含一个 `enabled` 或 `visible` 布尔值。没有远程 toggle 接口。
- 下游可见性始终是全局语义，必须使用 `scope={"type":"global"}`。来源页操作隐藏时，保留来源筛出的选集，但不能把来源作为可见性写范围。
- `selection.mode=ids` 提交 1–10,000 个明确模型 ID；`mode=filter` 提交 `filter` 和可选 `excludedModelIds`，在同一版本下物化完整查询结果，不仅操作当前页。
- 批量状态完整校验后一次提交；未知、跨来源或已移除对象不静默跳过。参与筛选的来源显示名称也纳入 revision；改名后旧 filter 提交冲突，整批零写。
- 返回 `data.items[].{modelId,status}`，状态为 `updated/unchanged`，以及新 `data.revision`。
- 重复发送旧 revision 可能返回冲突，但不会反向翻转。客户端刷新后明确决定目标，不能自动把冲突重试解释为新的 toggle。

### 错误与异步动作

| 情况 | HTTP / code |
|---|---|
| 缺新写操作版本 | 400 `CONFIRMATION_REQUIRED` |
| 权限不足 | 403 `CAPABILITY_DENIED` |
| 资源不存在 | 404 `RESOURCE_NOT_FOUND` |
| 版本过期 | 409 `REVISION_CONFLICT` |
| 名称冲突 | 409 `RESOURCE_CONFLICT` / `IDENTITY_CONFLICT` |
| 非法字段 | 422 `VALIDATION_FAILED` |
| 不支持范围/只读归属 | 422 `UNSUPPORTED_VALUE`，只读字段码 `READ_ONLY_SCOPE` |

`CONFIRMATION_REQUIRED` 是现有版本前置条件错误名，不表示给 TG 即时状态按钮增加确认页面。旧端点原来允许省略 revision 的行为不由此统一收紧。

上游同步、渠道发现和元数据同步复用现有 operation：202 仅表示受理，须查询终态；运行、失败、完成不是同一结果。当前 operation 为进程内状态，重启后不保证可恢复。模型状态配置已经保存但 reload 未确认时返回 HTTP 503 / `DEPENDENCY_UNAVAILABLE`，`retryable=false`，`error.fields` 中 `path=runtime,code=SAVED_RELOAD_UNCONFIRMED`，提示“配置已保存，运行时重载未确认；请刷新状态，勿重放旧操作”。这不是未写入的普通失败。OAuth 禁用即时从账户权威配置过滤，旧 channel/候选不得继续提供该禁用项。

## 独立别名原子编辑

```http
PATCH /model-mappings/<旧alias>
If-Match: <该映射的revision>
Content-Type: application/json

{"alias":"new-name","realModel":"target-model"}
```

名称与目标均 1–300 字符，在一次配置事务内更新；名称冲突或过期版本不产生半改记录。只处理当前映射及其已有 global/legacy 同名兼容记录，不自动改写 Key 白名单、负载均衡、压缩配置、历史元数据或其他别名。改名后外部调用方和已有引用需明确改用新名称，不暗中扩大授权。原 GET/PUT/DELETE 映射接口保持各自用途。PUT 新建与 PATCH 改名均保留真实目录名称，包括 OAuth 同步目录和禁用模型，不能用独立别名占用这些 ID。

## 元数据覆盖、继承与同步

### 逐字段覆盖

```http
PATCH /model-metadata/example-model/overrides
If-Match: <元数据查询返回的revision>
Content-Type: application/json

{
  "scope": "api",
  "channelId": "<channelId>",
  "outboundModel": "upstream-model",
  "set": {"contextWindow": 300000, "cost": {"input": 0}},
  "unset": ["vision"]
}
```

`scope=global` 不带来源选择器；`oauth` 使用 `accountId`，`api` 使用 `channelId`。`set` 为稀疏字段，`unset` 使用字段名（价格为 `cost.input` 等点分名），同一字段不能同时 set 和 unset。不能用 null 冒充恢复继承。

| 字段 | 含义 / 输入 |
|---|---|
| `contextWindow` | 上下文 Token 上限 |
| `maxInputTokens` | 最大输入 Token |
| `maxOutputTokens` | 最大输出 Token |
| `compactTriggerTokens` | 压缩触发阈值，不等同于最终安全容量 |
| `vision` | 图片输入能力，bool |
| `toolCall` | 工具调用能力，bool |
| `structuredOutput` | 结构化输出能力，bool |
| `reasoningEfforts` | 思考档位，字符串数组 |
| `serviceTiers` | 服务档位，字符串数组 |
| `knowledgeCutoff` | `YYYY-MM` / `YYYY-MM-DD` |
| `cost.input` / `cost.output` | 输入 / 输出价格 |
| `cost.cacheRead` / `cost.cacheWrite` | 缓存读 / 写价格 |
| `cost.longContextInput` / `cost.longContextOutput` | 长上下文输入 / 输出价格 |

Token 字段为正整数，价格为有限非负数，单位 USD / 1M Token；指数溢出、Infinity 和 NaN 写入前拒绝。明确的 `0/false/[]` 会保存；未指定字段继续继承。类型、日期、容量交叉约束与服务方原生硬限制由同一控制层验证，不能靠手工值开启已知不支持的能力。返回包括 `effective/raw/valueSource/constrainedBy/commonOverride/sourceOverride/revision`，多来源模型仍需看各自有效值。

整层恢复使用 `DELETE /model-metadata/{modelId}/overrides?scope=api&channelId=<id>`，带读取时 `If-Match`；成功 204，仅清该层手工覆盖，不改目录匹配、自动快照或别的来源。原 `/binding` PUT/DELETE 继续处理匹配，与手工字段接口分开。

OAuth 来源身份使用含禁用项的完整管理目录；停用不阻止读取、覆盖、恢复继承或同步该模型的元数据，不为管理编辑临时启用模型。原生 `serviceTiers` 对象按真实 `id` 规范化为公共字符串数组，原始账户目录保留名称，不把合法对象数组抹成空。

### 精确范围同步

```http
POST /model-metadata/actions/sync
If-Match: <元数据查询返回的revision>
Content-Type: application/json

{
  "mode": "selected",
  "targets": [
    {"modelId": "example-model", "source": {"type": "api", "id": "<channelId>"}}
  ],
  "refreshCatalog": true
}
```

- `one` 恰好一个 target，`selected` 为明确 targets；target 不带 source 表示该模型通用范围。
- `source` 使用 `source={"type":"oauth|api","id":"..."}`，不同时提交 targets。
- `full` 不带 targets/source。旧 `scope=provider/account/channel` 与匹配的旧选择器仍可解析，兼容省略 If-Match；worker 使用受理时服务器 revision 检查漂移。`mode` 和旧 `scope` 不同时提交。
- `one/selected/source` 不发布全局活动 catalog，即使 `refreshCatalog=true` 也只是下载临时候选，再将目标模型的基础 metadata/tariff/revision 保存到 binding 的 `autoSnapshot`。未选模型/其他来源有效值与活动目录版本保持不变。
- `full` 及已有后台全量刷新才发布公共 catalog 并协调自动快照；不增加另一套刷新 worker。人工匹配、字段覆盖与原生硬限制均保留。全量/后台快照计算在提交时 CAS 输入配置；并发人工改动导致旧计算冲突，不能盲写覆盖。此保护不声明跨目录文件与配置存储的事务原子性。
- 返回已有 operation，按终态查看逐目标 `created/updated/unchanged/protected/unmatched/missing/failed`；202 不代表同步完成。失败、受保护、未匹配不是“已更新”。

持久化结构和旧 binding 无快照兼容见 [配置 Schema](02-config-schema.md)。真实出站更名只清对应来源的旧匹配及手工覆盖；同名保存、启停和隐藏不清这些资料。显式快照 `tariff=null` 表示无价，不回退旧活动目录价；仅无 snapshot 的旧 binding 沿用活动目录兼容。

## 实际请求预算与故障切换

- 压缩触发阈值与最终输入容量分开：超过触发阈值不等于已超过硬容量，也不能通过提高阈值绕过已知容量限制。
- 每次对话 HTTP / Responses WebSocket 上游尝试在构造最终 JSON payload 后、发送前，按实际来源与出站模型重新解析有效输入/输出限制。模型列表中的某个来源值不能代替另一个候选的预算。
- 输入和输出独立处理：输入预算取 `maxInputTokens` 与 `contextWindow - 显式协议安全预留` 中可用的较小值，不预扣客户端最大输出。目录明确提供的 `limit.input` 投影为 `maxInputTokens`；最大输出等于 context 不代表输入为零，人工收紧 context 也不派生更小输出上限。Cursor normal / Max Context 分别与人工 context 上限求交；显式输入上限和压缩阈值不随 Max Context 偷增。
- 普通请求显式指定的输出上限超过当前候选 `maxOutputTokens` 时，在最终发送前按该候选钳制，并保持 thinking 等关联协议字段的合法性；故障切换重新按新候选计算，不改写原始客户端请求供其他候选复用。没有显式输出字段时保留上游默认规则，未知元数据不虚构容量。
- 例如 xAI 候选 context/maxOutput 都为 500k，约 17k 输入和 `max_output_tokens=500000` 不会因预扣输出而被判输入超限；换到 maxOutput=64k 的 Cursor 候选时，输出按 64k 钳制，不仅因客户端声明 500k 而直接拒绝。
- 通用 failover 最终传输路径不以本地 Token 估算作输入硬拒绝；不同供应商分词不同，估算用于既有压缩/分段逻辑而非保证上游一定接受。**保留的入口例外：** Messages 在只有一个非 Anthropic 候选时仍执行本地输入预检，估计超过该候选有效输入预算可返回 400；HTTP Responses 和多候选不走此例外。
- 压缩阈值与客户端输出上限分开，沿用既有压缩识别、直接压缩、分段和 reduce 流程，各实际请求的输出按当前候选限制处理；不承诺任何长度的输入均可成功压缩或被上游接受。
- 生效规则包含稀疏手工覆盖、来源自动快照与原生硬限制。恢复继承影响下一次解析；真实 dispatch 的来源、出站模型、费率与目录信息按尝试冻结，后续改价不会追溯修改已冻结结算。

## 媒体模型与独立缓存

### 设置与模型操作

下列路径使用 `/api/management/v1` 前缀。读取需要 READ，修改需要 WRITE，单项删除需要 DESTRUCTIVE；写操作携带相应设置读取时的 `If-Match`。

| 用途 | 路径 / 请求 |
|---|---|
| 图片设置 | `GET/PATCH /images/settings`；字段为 `enabled/defaultModel/models/requestTimeoutSeconds/cacheEnabled/cachePath/cacheRetentionDays/cacheMaxBytes` |
| 视频设置 | `GET/PATCH /videos/settings`；同类字段，另有 `jobTtlSeconds` |
| 查询媒体来源 | `GET /media/{image\|video}/sources` |
| 修改来源用途 | `PATCH /media/{image\|video}/sources/{sourceId}`；按对应来源 revision 修改用途开关 |
| Grok 兼容设置 | `GET/PATCH /xai/media-settings`；`imageModels/videoModels/jobTtlSeconds/requestTimeoutSeconds` |
| Grok 添加单项 | `POST /xai/media-models/{image\|video}`，`{"modelId":"new-model"}` |
| Grok 改名单项 | `PATCH /xai/media-models/{image\|video}/{旧modelId}`，`{"newModelId":"new-model"}` |
| Grok 删除单项 | `DELETE /xai/media-models/{image\|video}/{modelId}` |

`models` 按 provider 分组，实际持久化到 `image_models` 或 `video_models`。API 渠道媒体模型和 OAuth 账户专属名单也参与可路由目录；全局名单操作不重写账户专属范围。`defaultModel` 为空表示没有指定默认，供 MCP 等自动选择入口按可用目录处理，不用于补齐标准 HTTP 请求的 `model`。

单项写返回 `data.{provider,kind,owner,modelId,models,status,revision}`；名称占用返回 409，未知对象返回 404，旧 revision 返回 409 且不写。单项操作保持兄弟模型及顺序，整组替换则是显式整体操作。Grok 每组最多 50 项、每名最多 128 字符；时间字段为 1–2,147,483,647 的整数秒。TG 任务 TTL 支持 `d`，请求超时不支持 `d`。

GPT 主模型/`image_generation` 工具模型内部管线以及 Antigravity 图片支持已退役；不再提供 AG 图片管理/生成能力。旧 TG 消息仅落到当前图片或视频面板，不恢复模型名称编辑操作。现行 TG 面板维护用途、默认模型、缓存和运行参数；模型名称由配置或 Management API 管理。

### 缓存与交付

图片和视频各自使用 `images.*` / `videos.*` 缓存策略。旧视频配置缺失时只读继承旧共享值；修改图片缓存前固定视频的原有效值，避免跨用途串改。缓存默认关闭，保留天数 0 表示永久，空间上限 0 表示不设该类媒体聚合上限，单文件保护仍保留。

可选历史缓存不能破坏必需的 URL 交付。`b64_json` 返回实际图片数据，`url` 返回实际可下载的临时资源，不使用磁盘路径或虚构链接；日志区分上游生成、实际交付数量和缓存状态。历史缓存失败不抹掉已生成结果；无法交付 URL 或整批不足时应返回相应错误并保留可交付的部分结果，不能把整批失败说成全部成功。

缓存使用安全后缀、随机名和原子落盘，文件名不嵌入账户身份。管理下载仍鉴权，限制在当前缓存根内并拒绝 symlink/根外路径；对外临时资源按其能力 token 和有效期读取。更换目录不自动搬移旧文件；图片/视频临时资源分别按自身类型回收。

## 请求必须明确指定 model

HTTP 推理及图片/视频创建请求不再使用 `ingressDefaultModel`、压缩模型或视频列表首项补缺省。缺失、null、空串、空白或错误类型的 `model` 在调度、任务创建、绑定和成功日志前拒绝；HTTP 按 400，WebSocket 使用对应错误/4400 关闭契约。

这一变更涵盖 Messages、Chat Completions、Responses HTTP/WS、新 Realtime/Live 会话及 call 创建、标准/私有图片创建、视频生成/编辑/扩展。客户端必须在 JSON、表单或对应会话创建事件中按接口显式给出合法模型。

以下不新增本来不存在的必填项：`GET /v1/models`、operation/video 任务及内容查询、已有 call 绑定的 sideband。MCP 的自动模型选择是独立工具契约，不改变这些 HTTP 必填规则；已退役的 GPT 内部主模型/工具模型管线也不作为兜底。

旧 `/ingress-default-models/{ingress}` 的读/删用于兼容检查与清理；旧默认不再供运行时使用，写入明确报不支持，不静默保存无效默认，也不自动迁为压缩模型。依赖旧缺省行为的客户端需调整后再升级。

## 配置升级与回退

`modelCenter` 与 `modelMetadataOverrides` 缺省为空，参见 [配置 Schema](02-config-schema.md)。OAuth 来源禁用仍归原账户字段；API 来源禁用与全局启停/隐藏归新命名空间，其他路由与账户配置保持原样。隔离回退用例同时验证旧 loader/save 保留稀疏覆盖与 binding 自动快照，以及重新加载升级后备份时 `0/false/[]` 和来源单字段覆盖保持原值；不代表旧版会消费这些新字段。

**不能将“旧版能加载配置”理解为“旧版仍执行新策略”。** 对 0.32.2 / `55178591ddfb6b4caf6c530c1a0fa6e7d8951089` 的隔离演练显示：旧 loader/save 会保留 `modelCenter`，但旧 registry 仍将被新全局/来源策略禁用的模型列入可用集合。

回退准备必须区分两份独立备份：

1. **升级前备份**：配合旧代码恢复原基线；会丢失升级后新增的启停、隐藏、手工元数据、自动快照及后续配置修改。
2. **升级后备份**：保留新策略及其配置，用于排查或重新升级；不能直接交给旧代码就认为新规则仍生效。

现有滚动 `config.json.bak.*` 不替代这两份显式保留的快照。若需要在旧代码上继续保留新禁用策略，应另行制定并验证旧版兼容措施，不能只切换代码。业务数据库和媒体缓存也必须按实际部署版本单独评估；本配置演练不声称已完成生产数据库回滚。

定向演练（需要本地拥有上述 Git 对象）：

```bash
unshare --net -- sh -c 'ip link set lo up; exec ./venv/bin/python src/tests/isolated_pytest.py src/tests/test_model_center_rollback.py -q'
```

测试仅将旧版本源码解压到临时目录，执行临时配置读写与 registry 构造，不 checkout、不启动服务、不加载真实账号/数据库。最终部署、重启或恢复真实备份属于独立运维操作，不由本地测试授权。
