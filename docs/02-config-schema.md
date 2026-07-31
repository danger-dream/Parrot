# 02 — config.json Schema

所有可变配置集中于 `config.json`，目录根下。支持热加载（`config.py` 用 `mtime` 检测文件改动）。

## 2.1 完整 Schema（带默认值）

```jsonc
{
  // ─── 监听 ───
  "listen": {
    "host": "0.0.0.0",
    "port": 18082
  },

  // ─── 下游 API Key（客户端调代理时用的 key） ───
  "apiKeys": {
    "default": "ccp-d4aacba392d5b6a30cfb029049f02351b79414fee39e0efe",
    "custom": {
      "key": "sk-REPLACE_WITH_YOUR_CUSTOM_KEY",
      "enabled": true,                 // API Key 自身可用开关；缺失/null 默认为 true
      "allowedModels": [],
      "allowImages": false,
      "allowVideos": false,           // 视频费用较高，默认关闭
      "limits": {                      // 单 Key 限流覆盖；字段缺失/null 继承 apiKeyConcurrency
        "enabled": null,               // 优先级高于 apiKeyConcurrency.enabled
        "maxConcurrent": null,         // 0 = 不限并发
        "maxQueue": null,              // 0 = 不排队，满并发直接 429
        "queueWaitSeconds": null,      // 0 = 不等待，满并发直接 429
        "maxQueuedBodySpoolBytes": null // 单 Key 临时磁盘预算；null 继承全局
      }
    }
  },

  // ─── 下游 API Key 级并发限制默认值 ───
  "apiKeyConcurrency": {
    "enabled": true,
    "defaultMaxConcurrent": 5,
    "defaultMaxQueue": 50,
    "defaultQueueWaitSeconds": 1800,
    "defaultMaxRequestBodyBytes": 8388608,       // 单个排队请求最多预读/回放 8 MiB
    "defaultMaxRequestBodyEvents": 4096,         // 单个排队请求最多缓存 4096 个 ASGI body 事件
    "defaultMaxQueuedBodyBytesPerKey": 33554432, // 单 Key 排队请求体估算内存上限 32 MiB
    "maxQueuedBodyBytes": 134217728,             // 全进程排队请求体估算内存上限 128 MiB
    "queuedBodySpoolThresholdBytes": 1048576,    // 单请求超过 1 MiB 后转入临时文件
    "defaultMaxQueuedBodySpoolBytesPerKey": 536870912, // 单 Key 临时磁盘上限 512 MiB
    "maxQueuedBodySpoolBytes": 2147483648        // 全进程临时磁盘上限 2 GiB
  },

  // ─── OAuth 账户列表 ───
  "oauthAccounts": [
    {
      "email": "marlenaplocheroei79@gmail.com",
      "access_token": "sk-ant-oat01-...",
      "refresh_token": "sk-ant-ort01-...",
      "expired": "2026-04-18T05:26:49Z",
      "last_refresh": "2026-04-17T21:26:49Z",
      "type": "claude",
      "enabled": true,
      "disabled_reason": null,       // null | "user" | "quota" | "auth_error"
      "disabled_until": null,        // ISO 时间；quota 模式下为下次 resets_at
      "models": [                    // 该账号支持的模型，留空则用 oauthDefaultModels
        "claude-opus-4-5",
        "claude-opus-4-6",
        "claude-opus-4-7",
        "claude-sonnet-4-5",
        "claude-sonnet-4-6",
        "claude-haiku-4-5-20251001"
      ]
      // cc_mimicry 字段对 OAuth 强制 true，不读取 config 里的值
    }
  ],

  // ─── 第三方 API 渠道列表 ───
  "channels": [
    {
      "name": "智谱Coding Plan Max",   // 唯一标识
      "type": "api",
      "baseUrl": "https://coding.example.com/anthropic",  // 不带尾斜杠，自动裁剪
      "apiKey": "sk-xxx",
      "models": [
        { "real": "GLM-5", "alias": "glm-5" },
        { "real": "GLM-5-Turbo", "alias": "glm-5-turbo" }
      ],
      "enabled": true,
      "disabled_reason": null,       // null | "user"（API 渠道不会触发 quota）
      "cc_mimicry": true,            // 默认 true，用户可切换
      "omitTemperature": false        // 默认 false。开启后向上游发送前剔除 temperature 字段，
                                       // 兼容废弃 temperature 的第三方中转（如某些 claude-opus-4-7 转发）
    }
  ],

  // ─── 上游超时（秒） ───
  "timeouts": {
    "connect": 10,                   // TCP 连接建立
    "firstByte": 30,                 // 连接后到首个数据包
    "idle": 30,                      // 两次数据包之间的最长空闲
    "total": 600                     // 单次请求总时长
  },

  // ─── 出站网络设置 ───
  "network": {
    "dns": {
      "servers": ["8.8.8.8"],        // 出站域名解析使用的 DNS；可配置多个。支持 IP/域名、dot://、https://.../dns-query；DNS 服务器域名本身用系统 DNS 解析
      "bootstrapFromSystem": true,    // 首次启动时从系统 /etc/resolv.conf 同步一次
      "bootstrapped": false,          // 同步完成后自动置 true，后续不再自动覆盖
      "timeoutSeconds": 3,
      "cacheTtlSeconds": 300
    },
    "socks5": {
      "enabled": false,
      "url": ""                       // socks5://host:port / tcp://host:port / host:port
    },
    "monitor": {
      "enabled": true,                 // 网络健康检测总开关
      "intervalSeconds": 60,           // 检测间隔；最小 5 秒
      "timeoutSeconds": 5,
      "dns": false,                    // 定时检测 DNS 解析
      "socks5": false,                 // 定时检测 SOCKS5 代理可用性
      "channels": {
        "enabled": false,              // 渠道连接性检测总开关
        "byKey": {}                    // 每个渠道单独开关，如 {"api:foo": true}
      },
      "core": {
        "openai": false,
        "claude": false,
        "cloudflare": false
      }
    }
  },

  // ─── 错误冷却阶梯（分钟，0 = 永久拉黑） ───
  "errorWindows": [1, 3, 5, 10, 15, 0],

  // ─── 会话亲和 ───
  "affinity": {
    "ttlMinutes": 30,                // 30 分钟无新请求即释放绑定
    "cleanupIntervalSeconds": 300,
    "clientTtlMinutes": 120          // client-level soft affinity TTL
  },

  // ─── 评分参数 ───
  "scoring": {
    "emaAlpha": 0.25,                // EMA 平滑系数
    "recentWindow": 50,              // 滑动窗口大小
    "defaultScore": 3000,            // 未测或陈旧时的默认分
    "errorPenaltyFactor": 8,         // 失败率惩罚倍数
    "staleMinutes": 15,              // 多久未用开始向默认分漂移
    "staleFullDecayMinutes": 30,     // 30 分钟完全回归默认分
    "explorationRate": 0.2           // 20% 探索率
  },

  // ─── 冷却自动恢复探测（仅 API 渠道） ───
  "cooldownRecovery": {
    "enabled": true,
    "intervalSeconds": 30,
    "timeoutSeconds": 15
  },

  // ─── OAuth 配额监控 ───
  "quotaMonitor": {
    "enabled": true,
    "intervalSeconds": 60,
    "disableThresholdPercent": 95,   // 任一指标 ≥ 95% 即禁用
    "resumeThresholdPercent": 95     // 全部指标 < 95% 且 resets_at 已过 → 自动恢复
  },

  // ─── 首包文本黑名单 ───
  "contentBlacklist": {
    "default": [],                   // 对所有渠道生效
    "byChannel": {                   // 按渠道 name 分组
      "智谱Coding Plan Max": ["content_policy_violation"]
    }
  },

  // ─── CCH 模式（Claude Code 伪装） ───
  "cchMode": "disabled",             // "dynamic" | "disabled"

  // ─── OAuth 默认模型（当账号的 models 字段留空时使用） ───
  "oauthDefaultModels": [
    "claude-opus-4-5",
    "claude-opus-4-6",
    "claude-opus-4-7",
    "claude-sonnet-4-5",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001"
  ],

  // ─── 渠道测试（添加渠道时的 probe） ───
  "probe": {
    "timeoutSeconds": 60,
    "maxTokens": 50,
    "userMessage": "1+1=?"
  },

  // ─── Telegram Bot ───
  "telegram": {
    "botToken": "",
    "adminIds": []
  },

  // ─── Telegram UI 展示增强 ───
  // providerCustomEmoji 只用于消息正文 HTML（<tg-emoji>），可稳定显示 Telegram custom emoji；
  // providerBtnEmoji 用于 inline keyboard / code block / 纯文本兜底，因为按钮不支持 rich entity。
  "telegramUi": {
    "providerCustomEmoji": {
      "openai": "5861557411784957025",
      "claude": "5872779796257184592",
      "xai": "5819115571463068721"
    },
    "providerBtnEmoji": {
      "openai": "🅾️",
      "claude": "🅰️",
      "xai": "𝕏"
    }
  },

  // ─── OAuth 开发期开关 ───
  // mockMode=true 时，oauth_manager 不发真实 HTTP 到 api.anthropic.com / auth.openai.com / auth.x.ai
  // 用于开发期避免风控；生产部署时置为 false
  "oauth": {
    "mockMode": false
  },

  // ─── xAI / Grok OAuth ───
  // OAuth 参数默认对齐官方 xAI CLI/Grok OAuth；如 xAI 调整 client/scope/redirect，可在这里覆盖。
  // 请求级 token/cost 用量来自 xAI 响应 usage.cost_in_usd_ticks；
  // 账号级历史 usage / prepaid balance / spending limit 属于 xAI Management API，需额外 management key + team_id。
  "xaiOAuth": {
    "issuer": "https://auth.x.ai",
    "discoveryUrl": "https://auth.x.ai/.well-known/openid-configuration",
    "clientId": "b1a00492-073a-47ea-816f-4c329264a828",
    "redirectUri": "http://127.0.0.1:56121/callback",
    "scope": "openid profile email offline_access grok-cli:access api:access",
    "apiBaseUrl": "https://api.x.ai/v1",
    "baseUrl": "https://api.x.ai/v1",          // 兼容旧命名；新配置优先用 apiBaseUrl
    "responsesPath": "",                       // 非空时覆盖 /responses 路径拼接
    "isolateSessionId": true,                  // prompt_cache_key → x-grok-conv-id 时按 API key 隔离
    "userAgent": "parrot/xai-oauth-adapter",
    "imageModels": ["grok-imagine-image", "grok-imagine-image-quality"],
    "videoModels": ["grok-imagine-video", "grok-imagine-video-1.5"],
    "videoJobTtlSeconds": 10800,                // request_id → OAuth 账号绑定保留 3 小时
    "mediaRequestTimeoutSeconds": 180,
    "defaultModels": ["grok-4.5"]               // 仅文本 /responses 调度
  },

  // ─── 调度算法 / 负载均衡 ───
  "channelSelection": "smart",      // "smart" | "order" | "priority"
  "loadBalancing": {
    "initialized": false,
    "priorityOrders": {
      "anthropic": [],               // priority 模式下 Anthropic 家族渠道 key 顺序
      "openai": []                   // priority 模式下 OpenAI 家族渠道 key 顺序
    }
  },

  // ─── models.dev 元数据绑定 / 独立压缩模型 ───
  "modelBindings": {
    "defaults": {
      "gpt-5.4": {"target": "openai/gpt-5.4", "source": "auto"}
    },
    "scoped": {
      "api:Vendor": {
        "client-alias": {
          "target": "openai/gpt-5.4",
          "outboundModel": "Vendor-Real-Model",
          "source": "manual"
        }
      }
    }
  },
  "compressionModel": "gpt-5.4",

  // ─── models.dev 目录刷新 / Token 金额统计 ───
  "pricing": {
    "enabled": true,
    "autoUpdate": true,
    "sourceUrl": "https://models.dev/api.json",
    "modelsUrl": "https://models.dev/models.json",
    "refreshHours": 24
  },

  // ─── 路径 / 请求日志留存 ───
  "logDir": "logs",
  "logRetention": {
    "mode": "forever",              // "forever" | "days"；默认永久保留
    "days": null                      // mode="days" 时为整数，最少 1；无业务上限
  },
  "stateDbPath": "state.db"
}
```

> **Grok Imagine 升级兼容：**从不含 Imagine 配置的旧版本升级时无需手工修改 `config.json`。缺失的 `xaiOAuth.imageModels`、`videoModels`、`videoJobTtlSeconds`、`mediaRequestTimeoutSeconds` 会按默认值补齐；既有 xAI 文本配置、OAuth 账号及 token 原样保留。历史 API Key 保留原 `allowedModels` / `allowImages`，仅新增默认关闭的 `allowVideos: false`。启动时 `state.db` 会幂等创建 `xai_video_jobs`；`image_logs.db` 的历史图片表只原地新增统一多媒体字段，旧行按 OpenAI 图片解释，不替换现有表或清空历史数据。

`apiKeys.<name>.key` 是下游客户端作为 Bearer / x-api-key 使用的密钥字符串。配置层不要求 `ccp-` 前缀，任意字符串都可；TG bot 自动生成时仍使用 `ccp-<48 hex>`，也可以在菜单里输入自定义 key。

`apiKeys.<name>.enabled` 控制该 Key 是否可用，缺失或 `null` 时按 `true` 处理。`apiKeyConcurrency` 是 API Key 级限流默认值；`apiKeys.<name>.limits.enabled/maxConcurrent/maxQueue/queueWaitSeconds` 是单 Key 覆盖，其中 `limits.enabled` 优先级高于全局 `apiKeyConcurrency.enabled`。默认单 Key 5 并发、50 队列、最长等待 1800 秒；队列满、等待超时或客户端断开时请求会从队列移除并返回/结束。

排队期间，限流器会独占并读取 ASGI `receive` 以尽早发现客户端断开，并把期间读到的 `http.request` 事件按原边界完整回放给下游。`defaultMaxRequestBodyBytes` 和 `defaultMaxRequestBodyEvents` 只约束这种排队预读/回放资源，超限返回 413；它们不会给非排队的 `/v1/messages`、`/v1/chat/completions`、`/v1/responses` 新增通用协议上限。图片 HTTP 入口有独立的 endpoint 协议上限：编辑端点会按 `images.maxInputImageBytes`、multipart/JSON（data URL 的 base64 膨胀）、标准多图与 mask 合同自动提高总 body 上限，保证合法图片请求不会被通用 8 MiB replay 默认值误拒绝。中间件只通过公开 ASGI `scope/receive/send` 交接所有权，不修改 Starlette `Request` 私有属性。

待回放 body 不会全部常驻内存：单请求累计正文超过 `queuedBodySpoolThresholdBytes`（默认 1 MiB）时，已缓存和后续正文会迁移到数据目录下固定的 `queued-body-spool/` 私有临时目录。`defaultMaxQueuedBodyBytesPerKey` / `maxQueuedBodyBytes` 继续限制单 Key / 全进程的内存正文与 ASGI 事件开销；`defaultMaxQueuedBodySpoolBytesPerKey` / `maxQueuedBodySpoolBytes` 独立限制临时磁盘，单 Key 还可用 `limits.maxQueuedBodySpoolBytes` 覆盖。任一聚合资源达到上限均返回 429 并带 `Retry-After`；旧配置名 `maxQueuedBodyBytesTotal` 仅在没有公开键 `maxQueuedBodyBytes` 时作为兼容回退。请求获得并发槽位、缓存事件回放完毕后，后续 body 由下游直接读取；成功、异常、等待超时、任务取消、客户端断开、热禁用及 FIFO handoff 都会归零 accounting，并关闭、删除临时文件。

## 2.2 字段语义详解

### 请求日志留存 `logRetention`

- `mode="forever"`：默认值，永久保留 `logs/YYYY-MM.db` 的业务请求日志。
- `mode="days"`：仅保留从当前时刻向前回溯 `days` 天内的数据；`days` 必须为整数且 `>= 1`，无业务上限。模式与天数是独立字段，已处于该模式时可单独修改 `days`。
- 在按天留存模式增大 `days`（如 3 → 5）只更新配置、不触发即时清理；首次启用或缩短 `days`（如 5 → 3）会扩大删除范围，TG Bot 必须先展示警告、扫描并逐月列出待清理项，第二次确认后才写入配置并执行删除。确认页的计划短期有效，执行前会重新验证，避免确认期间数据范围变化。
- 仅影响月度业务日志：请求摘要、原始请求/响应、重试链、代理链与本地 Web 明细；**不影响** `state.db`、统一多媒体日志/图片缓存和翻译缓存。
- 完整过期月份会删除整个 DB 文件（及其 WAL/SHM sidecar）；留存临界落在某个月中间时，会精确删除关联记录并执行 SQLite 压缩，才能实际释放磁盘空间。压缩前会做磁盘余量预检，空间不足时 fail-closed。
- 已启用的策略由后台维护循环每天最多执行一次到期检查；不会在正常 API 请求的同步写入路径执行大型删除或 `VACUUM`。

### 渠道 `disabled_reason` 状态机

```
┌─────────┐                         ┌──────────┐
│enabled  │──admin 点「禁用」──→    │ disabled │
│         │                         │ reason=  │
│         │←──admin 点「启用」──── │ "user"   │
└─────────┘                         └──────────┘

     │                                   ▲
     │ OAuth 配额 ≥ 95%                  │ quota 监控发现全部 < 95%
     ↓                                   │   且 resets_at 已过（自动）
┌──────────────┐                         │
│ disabled     │─────────────────────────┘
│ reason="quota"│
│ disabled_until=resets_at
└──────────────┘

若 admin 在 quota 状态下手动禁用：
    disabled_reason 改为 "user"，后台不再自动恢复
若 admin 在 quota 状态下手动启用：
    disabled_reason → null，但若配额仍 ≥ 95%，下次监控周期会再次禁为 quota
```

### 渠道模型的三种状态（`channel_errors` 表中体现）

- **ok**：`channel_errors` 无记录或 `cooldown_until` 已过
- **cooling**：`cooldown_until > now`（临时退避，时间取决于 `errorWindows[error_count]`）
- **permanent_blackout**：`cooldown_until = -1`（对应 Python `Infinity`，`errorWindows` 走到 `0` 时触发）

手动清除错误：删除对应 `channel_errors` 行即可。

### 模型别名语法

TG Bot 添加/编辑渠道时，"模型列表"输入格式：
```
GLM-5:glm-5, GLM-5-Turbo:glm-5-turbo ; gpt-5.4 ， gpt-5.3-codex:codex
```

解析规则（`src/channel/api_channel.py` 的 `parse_models_input`）：
1. 先按正则 `[,，;；]` 切分得到条目列表
2. 每条按正则 `[:：]` 切分：
   - 一项：`real == alias`（如 `gpt-5.4`）
   - 两项：`real:alias`
   - 其它：报错
3. 条目顺序保留，`alias` 不可重复

运行时：
- 客户端请求 `model=glm-5` → 匹配 `alias` → 向上游发 `model=GLM-5`（真实名）
- 客户端请求 `model=GLM-5`（真实名）→ 若 `alias` 列表中无此值，视为不支持（**除非 real==alias 同值**）

### 模型元数据绑定 `modelBindings` 与压缩模型 `compressionModel`

- `defaults`：键为客户端可见模型名/渠道 alias，值只保存 models.dev `provider/model` identity 与来源；不复制全量目录记录。
- `scoped`：先按稳定 scope key（`api:<name>` / `oauth:<provider>:<identity>`），再按客户端可见模型名索引；API alias 同时保存当时的 `outboundModel`，alias 被改指后旧专属绑定不再误用。
- 有效解析固定为 `scoped > default > none`。context window、max output、压缩阈值、能力展示和估算价格均从该绑定指向的同一份 models.dev 目录取得；没有有效绑定时保持无元数据/未计价，不按 provider 或模型前缀猜测。压缩阈值优先取 models.dev 第一档 context 价格阶梯的起点；没有该阶梯时按 `floor((contextWindow - maxOutputTokens) × 80%)` 计算。
- Telegram「自动同步元数据」会先拉取最新的 `api.json` 与 `models.json`：两份均下载、校验成功后原子保存为本地 gzip 目录；任一拉取失败则保留并继续使用上次成功保存的本地目录。随后从该本地目录扫描每个 OAuth/API scope 的已有客户端模型，去重后只按 `models.json` canonical 官方根与 `api.json` 的 exact 同名记录建立/更新默认绑定，不覆盖专属绑定。专属流程按 OAuth/API 账户或渠道 → 该 scope 内模型 → exact 同名候选优先选择；找不到合适候选时才按名称筛选或浏览 provider 与其模型。
- `compressionModel` 是独立的客户端可见模型名。运行时按实际 compact 路由解析相同的有效绑定来取得 context、max output 和压缩阈值；普通请求的 compact 预检、直连压缩判断和 map-reduce 分段目标都会使用该阈值。旧 `modelMetadata[*].compressionModel=true` 会一次性迁移，旧手工元数据只有 exact canonical 命中时才迁成默认绑定。

### Token 金额统计 `pricing`

- `enabled`：是否在 Telegram 的统计、日志和账户等页面计算金额；关闭后不读取响应正文做费用聚合。
- `autoUpdate`：是否后台同时刷新 models.dev 的供应商 API 目录与规范模型目录。启动时先读取 `$ANTHROPIC_PROXY_DATA_DIR/models_dev_catalog.json.gz`（Docker 默认 `/app/data/models_dev_catalog.json.gz`）缓存，缓存不存在或损坏时使用仓库内置 gzip 快照；任一远端失败都不会替换当前目录或影响代理请求。
- `sourceUrl`：models.dev 供应商模型与价格目录，默认 `https://models.dev/api.json`，只接受 `https://`。金额只从这里读取，单位为 USD / 1M Token。
- `modelsUrl`：规范模型身份目录，默认 `https://models.dev/models.json`，只接受 `https://`。该文件不提供价格，仅用于 canonical 官方 exact 同名匹配。
- `refreshHours`：远端刷新间隔，最小 1 小时。
- 旧 `channelProviders` / `aliases` / `overrides` 字段可继续留在配置中，避免升级时丢配置；新的 dispatch-time 估算不使用它们绕过元数据绑定，也不接受手工价格覆盖。

新请求在每次上游尝试 dispatch 时按真实 scope、客户端可见 model 和出站真实 model 解析有效元数据绑定，并冻结其 models.dev provider/model、费率与目录版本；之后配置或目录更新不会重算该结算。没有有效绑定或绑定记录没有可用 Token 价格时保持 `unpriced`。只有没有尝试账本的历史请求才会按当前有效绑定做兼容估算。xAI OAuth 响应包含 `usage.cost_in_usd_ticks` 时优先采用该次尝试的真实上游金额。长上下文阶梯按**单次请求**的 `input + cache creation + cache read` 判断；当前结算结构只支持一档 context tier，目录若为同一模型提供多档阈值则该模型 fail-closed 为未计价。`experimental.modes.fast.cost` 是完整替换价，不与标准长上下文价叠加；没有响应/真实出站 fast 事实时不会从下游 intent 臆测加速价，实际为 priority/fast 但目录没有对应 tariff、或上游返回 `flex` 等未知计费档位时同样保持未计价。数据库只保存缓存写入总 Token、没有保存 Anthropic 5 分钟 / 1 小时 TTL 拆分，因此 Claude 请求只要包含 cache creation 就标记为“未计价”。目录若要求单独计费 reasoning/audio Token、但价格与聚合 input/output 不同，也会保持未计价，避免用缺失的 Token 维度生成假精确金额。

Telegram 界面只显示合并后的 USD 金额，不展示金额来源分类或未计价次数；统计页面保留两位小数，最近日志紧凑列表保留三位小数，均不加约等号。models.dev 计价结果与 xAI 上游金额会直接合并到同一个总额，内部仍保留各自结算来源及无法计价记录，以保证账本和聚合口径不变。Parrot 不做实时汇率换算。旧版 OpenAI 日志曾把缓存读取 Token 同时包含在 `input_tokens` 中；若历史行缺少明确的 usage 口径且无法确认新旧语义，内部不会把它作为已知金额计入总额。

### 超时语义（关键）

四段超时**独立**运行，任一段超时即中止：

```
 t=0          t_connect     t_first_byte            ...            t_idle_limit
  │─────────────┼───────────────┼────────────────────────────────────┼───
  │  connect    │  first_byte   │  chunk 1  │  chunk 2  │ ...  │ idle│
  │  ≤ 10s      │  ≤ 30s        │           │           │      │≤ 30s│
  │                                                                  │
  └──────────────────────── total ≤ 600s ───────────────────────────┘
```

实现：
- `connect_timeout`：httpx 的 `timeout=Timeout(connect=10)`
- `first_byte_timeout`：发起请求后 `asyncio.wait_for(resp.aiter_bytes().__anext__(), 30)`
- `idle_timeout`：每次 `chunk` 到达后 `asyncio.wait_for(next_chunk, 30)`
- `total_timeout`：外层 `asyncio.wait_for(whole_call, 600)`

详见 `docs/07-failover.md`。

## 2.3 配置热加载规则

- `config.py` 维护 `_config_cache` + `_config_mtime`
- 每次 `load_config()` 调用对比 mtime，若变更则重读
- 大部分字段（channels / oauthAccounts / timeouts / scoring / ...）热加载即生效
- **不热加载**：
  - `listen.host` / `listen.port`（需重启）
  - `stateDbPath` / `logDir` / `openai.store.dbPath`（需重启）
  - `telegram.botToken` / `telegram.adminIds`（需重启）

## 2.4 TG Bot 对 config.json 的写入

TG Bot 修改的所有操作都走 `config.save()`，采用 `tmp + os.replace` 原子写：
- 添加/编辑/删除渠道
- 添加/编辑/删除 OAuth 账户
- 添加/删除 API Key
- 修改超时 / 错误阶梯 / 黑名单 / CCH 模式 / 请求日志留存

写入后无需重启（热加载生效）。

## 2.5 首次启动

当 `config.json` 不存在时，`server.py` 自动生成最小化模板：
```json
{
  "listen": {"host": "0.0.0.0", "port": 18082},
  "apiKeys": {},
  "oauthAccounts": [],
  "channels": [],
  "timeouts": {"connect": 10, "firstByte": 30, "idle": 30, "total": 600},
  "errorWindows": [1, 3, 5, 10, 15, 0],
  "telegram": {"botToken": "", "adminIds": []},
  "logDir": "logs",
  "stateDbPath": "state.db"
}
```
其余字段使用 `src/config.py` 中的 `DEFAULT_CONFIG` 补齐。
