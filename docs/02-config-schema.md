# 02 — config.json Schema

所有可变配置集中于 `config.json`，目录根下。支持热加载（`config.py` 用 `mtime` 检测文件改动）。

## 2.1 完整 Schema（带默认值）

```jsonc
{
  // ─── 监听 ───
  "listen": {
    "host": "0.0.0.0",
    "port": 22122
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

  // OpenAI OAuth workspace 账户的 Codex device-only 收敛默认开启：
  // "codexDeviceInstallationId": "123e4567-e89b-42d3-a456-426614174000",
  // "codexDeviceConvergenceEnabled": false  // 仅显式退出时配置
  // 启动迁移及新账户原子写入会为已知 workspace_id/chatgpt_account_id 的账户
  // 生成并持久化随机规范 RFC4122 UUIDv4；同 workspace 重导入/刷新保留它。
  // 显式 false 会保留已有 UUID 以便可逆重启用；缺少 workspace 的账户不参与。
  // 缺失或空 UUID 在默认开启状态下都会生成；唯一关闭方式是显式 false。
  // 其他无效 UUID fail-closed，不静默重生成。
  // 该值仅作用于既有冻结的 Codex Responses installation carriers，
  // 不扩展到 session/full 或非 OpenAI OAuth 渠道。

  // Cursor 账户由 TG「Cursor 登录」自动写入：provider/type=cursor；subject/sub
  // 来自 Access Token JWT，并始终作为稳定内部主键。Parrot 会用同一 Token 合成
  // Cursor Web 会话并调用 /api/auth/me，同步真实 email、姓名、用户 ID 与邮箱验证状态；
  // Web 资料临时失败时只回退哈希展示名，不阻断登录或覆盖此前已验证资料。
  // models 是下游可见 canonical ids；cursor_model_catalog 保存该账号 AvailableModels
  // 的上下文、Max Context 和真实 legacy_slugs。这些账号专属元数据不写入
  // modelBindings，也不继承 models.dev。所有具备独立 Max Context 档位的模型
  // 默认开启；cursor_max_context_disabled_models 仅保存用户在 TG 详情中关闭的
  // canonical id 例外列表，并按账号持久化。cursor_disabled_models 保存该账号在
  // Cursor 模型目录「批量禁用」中选定的 canonical ids；它们仍保留在原始目录供
  // 查看和恢复，但不会注册到该账号渠道、进入负载均衡候选或被调度使用。

  // ─── WorkBuddy CN CLI 请求模板适配（完整默认规则见 2.2） ───
  "workbuddy": {
    "requestRewrite": {
      "enabled": true
      // 未配置 rules：使用两条内置默认规则；显式 rules: []：不做替换。
    }
  },

  // ─── 第三方 API 渠道列表 ───
  "channels": [
    {
      "name": "智谱Coding Plan Max",   // 唯一标识
      "type": "api",
      "generationId": "<opaque-id>",   // 内部代际身份；新增渠道自动生成，普通 reload/rename 保持
      "baseUrl": "https://open.bigmodel.cn", // resolved 主机/基础路径，不带尾斜杠
      "apiKey": "sk-xxx",
      "providerId": "zhipu",          // 可选：内置目录品牌身份；缺失表示 custom/旧渠道
      "providerPresetId": "coding-cn", // 可选：具体 preset 身份
      "protocol": "anthropic",
      "apiPath": "/api/anthropic/v1/messages", // preset/完整 endpoint 拆分后的可选路径
      "models": [
        { "real": "GLM-5", "alias": "glm-5" },
        { "real": "GLM-5-Turbo", "alias": "glm-5-turbo" }
      ],
      "enabled": true,
      "disabled_reason": null,       // null | "user"（API 渠道不会触发 quota）
      "cc_mimicry": true,            // custom Anthropic 默认 true；preset 仅按目录显式值，OpenAI 恒 false
      "omitTemperature": false        // 默认 false。开启后向上游发送前剔除 temperature 字段，
                                       // 兼容废弃 temperature 的第三方中转（如某些 claude-opus-4-7 转发）
    }
  ],

  // providerId/providerPresetId 只记录向导选择身份。运行时始终使用已保存的
  // baseUrl/apiPath；目录更新不会自动重写已有渠道。未知身份也不会禁用渠道。
  // generationId 不参与 UI 名称、路由 key 或日志 identity，只用于隔离 delete 前后的
  // 在途副作用和 concurrency slot。旧配置缺失时会获得进程内稳定的兼容代际；普通
  // reload 不会换代，后续编辑/rename 会把该代际写回配置。delete → add 同名会生成新值。

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
  // providerCustomEmoji 用于消息正文 HTML（<tg-emoji>），也可传给 Telegram
  // InlineKeyboardButton.icon_custom_emoji_id；providerBtnEmoji 用于 code block、
  // 不设置按钮 icon 字段时及旧客户端的纯文本兜底。Anthropic/API 与
  // Claude/OAuth 共用 claude 键，避免旧 claude 自定义覆盖被新增默认遮蔽。
  "telegramUi": {
    "providerCustomEmoji": {
      "openai": "6141162084857031383",
      "claude": "6140995813788099525",
      "antigravity": "6077644693984779782",
      "cursor": "6062261319426390107",
      "ollama-cloud": "6138524734419116492",
      "workbuddy": "6120617435214132136",
      "xai": "6138882363460952713",
      "kimi": "6140905172798284383",
      "deepseek": "6138914554240836667",
      "zhipu": "6140727700454645813",
      "minimax": "6141114311935796161",
      "alibaba-bailian": "6138926816372465673",
      "tencent-cloud": "6140662000339918826",
      "jd-cloud": "6138855790498291964",
      "volcengine-ark": "6141018834812806707",
      "baidu-qianfan": "6138964148228204290",
      "xiaomi-mimo": "6138428226503975259",
      "ctyun-xirang": "6138918874977936421",
      "openrouter": "6140767025175209650"
    },
    "providerBtnEmoji": {
      "openai": "🅾️",
      "claude": "🅰️",
      "xai": "𝕏",
      "cursor": "🖱️",
      "antigravity": "✨"
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

  // ─── Cursor OAuth / 内部 AgentService bridge ───
  "cursorOAuth": {
    "bridgeHost": "127.0.0.1",       // 只允许 loopback
    "bridgePort": 0,                  // 0 = 每次启动自动选择空闲端口
    "maxRetries": 2,
    "requestTimeoutSeconds": 300,
    "modelSyncHours": 6               // 每账号 canonical 模型目录刷新间隔
  },

  // ─── 调度算法 / 负载均衡 ───
  "channelSelection": "smart",      // "smart" | "order" | "priority"
  "loadBalancing": {
    "initialized": false,
    "channelPriorityOrder": [],       // 所有 OAuth账户/API渠道的统一默认顺序
    "modelPriorityOrders": {           // canonical 模型专属顺序，优先于统一顺序
      "glm-5.2": ["api:智谱 Max", "oauth:cursor:subject"]
    },
    "priorityOrders": {                // 旧版家族顺序，仅迁移/降级兼容
      "anthropic": [],
      "openai": []
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
  "stateDbPath": "state.db",          // 仅旧版只读迁移源
  "runtimeStatePath": "runtime-cache.json",
  "durableStatePath": "durable-state.json"
}
```

`runtimeStatePath` 与 `durableStatePath` 的默认值和相对路径始终以可写 `DATA_DIR` 为根，绝不跟随绝对 `stateDbPath` 的目录；显式绝对 JSON 路径仍原样使用且父目录必须可写。`stateDbPath` 可位于任意只读位置，只作为迁移源。

> **Grok Imagine 升级兼容：**从不含 Imagine 配置的旧版本升级时无需手工修改 `config.json`。缺失的 `xaiOAuth.imageModels`、`videoModels`、`videoJobTtlSeconds`、`mediaRequestTimeoutSeconds` 会按默认值补齐；既有 xAI 文本配置、OAuth 账号及 token 原样保留。历史 API Key 保留原 `allowedModels` / `allowImages`，仅新增默认关闭的 `allowVideos: false`。启动时旧 `state.db` 中的 `xai_video_jobs` 会只读迁移到 `durable-state.json`；`image_logs.db` 的历史图片表只原地新增统一多媒体字段，旧行按 OpenAI 图片解释，不替换现有表或清空历史数据。

`apiKeys.<name>.key` 是下游客户端作为 Bearer / x-api-key 使用的密钥字符串。配置层不要求 `ccp-` 前缀，任意字符串都可；TG bot 自动生成时仍使用 `ccp-<48 hex>`，也可以在菜单里输入自定义 key。

`apiKeys.<name>.enabled` 控制该 Key 是否可用，缺失或 `null` 时按 `true` 处理。`apiKeyConcurrency` 是 API Key 级限流默认值；`apiKeys.<name>.limits.enabled/maxConcurrent/maxQueue/queueWaitSeconds` 是单 Key 覆盖，其中 `limits.enabled` 优先级高于全局 `apiKeyConcurrency.enabled`。默认单 Key 5 并发、50 队列、最长等待 1800 秒；队列满、等待超时或客户端断开时请求会从队列移除并返回/结束。

排队期间，限流器会独占并读取 ASGI `receive` 以尽早发现客户端断开，并把期间读到的 `http.request` 事件按原边界完整回放给下游。`defaultMaxRequestBodyBytes` 和 `defaultMaxRequestBodyEvents` 只约束这种排队预读/回放资源，超限返回 413；它们不会给非排队的 `/v1/messages`、`/v1/chat/completions`、`/v1/responses` 新增通用协议上限。图片 HTTP 入口有独立的 endpoint 协议上限：编辑端点会按 `images.maxInputImageBytes`、multipart/JSON（data URL 的 base64 膨胀）、标准多图与 mask 合同自动提高总 body 上限，保证合法图片请求不会被通用 8 MiB replay 默认值误拒绝。中间件只通过公开 ASGI `scope/receive/send` 交接所有权，不修改 Starlette `Request` 私有属性。

待回放 body 不会全部常驻内存：单请求累计正文超过 `queuedBodySpoolThresholdBytes`（默认 1 MiB）时，已缓存和后续正文会迁移到数据目录下固定的 `queued-body-spool/` 私有临时目录。`defaultMaxQueuedBodyBytesPerKey` / `maxQueuedBodyBytes` 继续限制单 Key / 全进程的内存正文与 ASGI 事件开销；`defaultMaxQueuedBodySpoolBytesPerKey` / `maxQueuedBodySpoolBytes` 独立限制临时磁盘，单 Key 还可用 `limits.maxQueuedBodySpoolBytes` 覆盖。任一聚合资源达到上限均返回 429 并带 `Retry-After`；旧配置名 `maxQueuedBodyBytesTotal` 仅在没有公开键 `maxQueuedBodyBytes` 时作为兼容回退。请求获得并发槽位、缓存事件回放完毕后，后续 body 由下游直接读取；成功、异常、等待超时、任务取消、客户端断开、热禁用及 FIFO handoff 都会归零 accounting，并关闭、删除临时文件。

## 2.2 字段语义详解

### WorkBuddy 模板适配 `workbuddy.requestRewrite`

仅作用于 **WorkBuddy OAuth 中国区 CLI**。Chat / Responses / Anthropic 的请求统一转换成 Chat payload 后、JSON 序列化前应用规则。国际区（CLI/IDE）及其他渠道均不应用；配置无需按模型重复填写。已有 CodeBuddy 身份、角色顺序、分支实际值和其他字段保持不变。

默认配置如下，可直接放在 `config.json` 顶层。新指纹仍位于下面两类模板位置时，只需追加规则，无需改代码或重启：

```json
{
  "workbuddy": {
    "requestRewrite": {
      "enabled": true,
      "rules": [
        {
          "id": "cc-identity",
          "enabled": true,
          "scope": "system_prefix_line",
          "match": "You are Claude Code, Anthropic's official CLI for Claude.",
          "replace": "You are a coding agent."
        },
        {
          "id": "cc-env-main-branch",
          "enabled": true,
          "scope": "system_env_line",
          "match": "Main branch (you will usually use this for PRs)",
          "replace": "Main branch (normally used for pull requests)"
        }
      ]
    }
  }
}
```

- `enabled`：总开关，默认 `true`。每条规则也可单独设置 `enabled: false`（省略时开启）。
- `rules`：按数组顺序处理，**同一原始行第一条匹配规则生效，不级联替换**。显式列表替代整个默认列表，不与默认规则按 ID 合并；`[]` 禁用所有替换。旧配置缺少该项时自动回填上述默认规则，显式关闭/空列表不被重新启用。
- `id`：必填且唯一，1–64 位 ASCII 字母/数字/`.`/`_`/`-`，以字母或数字开头。DEBUG 日志仅输出规则 ID 和命中数，不输出正文、match、replace 或账号凭据。
- `scope: system_prefix_line`：仅每条转换后 `system` 消息的第一条非空行，整行（忽略首尾空白）必须等于 `match`。
- `scope: system_env_line`：仅完整、独立的 `<env> … </env>` 内的行；整行等于 `match`，或以 `match` 紧接 `:` 开始。只替换匹配部分，冒号后的真实值、原有缩进和换行不变。不处理未闭合/带属性的 env、Markdown 代码围栏、XML 引用、注释或 CDATA 里的 env；行内引用标签同样排除。前面的独立自闭合标签（包括带属性/空白的写法）不会阻止后续正常 bare env 匹配。
- `match` / `replace`：必填的**字面字符串**，不解释正则或脚本；match 为 1–2048 字符，replace 为 0–4096 字符，不允许换行和控制字符。最多 64 条规则，未知 scope/字段、重复 ID、空 match 等均拒绝。
- 仅访问 `system` 的字符串或纯文本 content parts，不搜索 developer/user/assistant/tool 消息、工具定义/schema/参数/结果、图片或其他 JSON 字段。混有未知/非文本 part 的 system 消息保守跳过；不跨 part 拼接触发串。未标记为引用、但结构与模板完全相同的文本无法自动辨别来源，应关闭相应规则而不是依赖内容猜测。
- 适配不修改原始入包；每次派发从原文生成出包副本，不污染重试或其他渠道。保存合法配置后，已有渠道对象的下一次请求即读取新规则；无需重建账号、刷新令牌或重启服务。
- 通过配置保存函数提交时先校验，失败不写盘、不发布缓存。直接手工编辑出错时，运行中的热加载保留上一份完整有效配置并报错；非法文件修正前，`save/update` 及依赖它们的账号/其他设置保存会明确拒绝，不允许用旧快照覆盖手工修改。修正文件后，下次保存从已修正的完整配置继续，保留其他手工字段并恢复热加载回调；首次启动或显式强制 reload 遇到非法规则则明确报配置错误。不会自动修复或覆盖非法规则。

该功能是定向模板兼容，不保证任意业务正文都能通过上游策略。没有命中或适配后仍被上游明确拒绝时，不扩大改写范围、不循环“清洗到成功”。

**明确拒绝的错误处理（#31）**：仅当 WorkBuddy 返回 HTTP 400，且错误 code 为 `11128`、msg/message 为 `Illegal API invocation from an unapproved channel` 时，按本次请求拒绝直接返回 400、原 code 和该诊断文案，不轮遍账号、不刷新、不增加冷却/grace/失败评分。WorkBuddy SSE 中相同明确错误也按此处理；若下游流已提交，HTTP 状态无法改写，但会发送流内错误，不伪造正常结束。该路径保留原有历史冷却，不自动清除任何条目。未知 400、其他供应商同码，以及 401/403/429、402 余额、404 模型缺失等仍走各自原有策略，不将所有 11128 或所有 4xx 一概认定为请求内容问题。

### 请求日志留存 `logRetention`

- `mode="forever"`：默认值，永久保留 `logs/YYYY-MM.db` 的业务请求日志。
- `mode="days"`：仅保留从当前时刻向前回溯 `days` 天内的数据；`days` 必须为整数且 `>= 1`，无业务上限。模式与天数是独立字段，已处于该模式时可单独修改 `days`。
- 在按天留存模式增大 `days`（如 3 → 5）只更新配置、不触发即时清理；首次启用或缩短 `days`（如 5 → 3）会扩大删除范围，TG Bot 必须先展示警告、扫描并逐月列出待清理项，第二次确认后才写入配置并执行删除。确认页的计划短期有效，执行前会重新验证，避免确认期间数据范围变化。
- 仅影响月度业务日志：请求摘要、原始请求/响应、重试链、代理链与本地 Web 明细；**不影响**状态 JSON、只读旧 `state.db`、统一多媒体日志/图片缓存和翻译缓存。
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

### 模型中心状态 `modelCenter`

```json
"modelCenter": {
  "schemaVersion": 1,
  "disabledModels": [],
  "hiddenModels": [],
  "apiSourceDisabledModels": {}
}
```

- `disabledModels` 保存全局禁用的真实客户端模型 ID，阻止其全部来源候选。恢复全局启用不会清除来源或账户自身的禁用。
- `hiddenModels` 只控制下游发现结果；别名随目标隐藏。隐藏不撤销 Key 权限，也不禁止合法显式调用；启停和隐藏是两个独立状态。
- `apiSourceDisabledModels` 保存 API 渠道的可恢复模型禁用，键为内部稳定来源键 `api:<name>`，值为该来源禁用的客户端模型 ID 数组。不删除或重写原路由、出站模型名或元数据。Management API/TG 使用公开 channelId，由共享控制层转换，客户端不要拼接内部键。
- OAuth 来源仍复用账户既有 `disabledModels` / Cursor 状态，不在此处存第二份。账户禁用、冷却和故障状态继续独立生效。
- 新配置缺省为空，升级不自动禁用或隐藏任何模型。共享控制层通过 `config.update()` 原子保存；运行时读取同一配置，不以 TG 会话或内存缓存为持久真相。
- **回退注意**：旧版可能保留这些未知字段，但不会执行新启停/隐藏规则。字段未丢不等于回退后路由仍安全；回退必须同时验证目标代码行为并保留升级前、升级后两份独立配置备份。

### 稀疏手工元数据 `modelMetadataOverrides`

```json
"modelMetadataOverrides": {
  "defaults": {
    "example-model": {
      "fields": {"contextWindow": 1000000, "cost.input": 0}
    }
  },
  "scoped": {
    "api:example-channel": {
      "example-model": {
        "outboundModel": "upstream-model",
        "fields": {"contextWindow": 300000}
      }
    }
  }
}
```

`defaults` 是真实客户端模型的通用差异，`scoped` 仅保存当前来源修改过的字段，不复制整份通用元数据。来源只覆盖 context=300k 时，价格/输出等仍继承通用及目录基础。`outboundModel` 绑定当时的出站名称；真实出站改名清当前来源旧绑定和覆盖，同名保存、启停、隐藏不清其他层。

16 个字段是 `contextWindow/maxInputTokens/maxOutputTokens/compactTriggerTokens`、`vision/toolCall/structuredOutput/reasoningEfforts/serviceTiers/knowledgeCutoff` 和 `cost.input/output/cacheRead/cacheWrite/longContextInput/longContextOutput`。价格必须为有限非负数，单位为 USD / 1M Token；非有限值在写入前拒绝。持久化价格字段采用点分扁平键，HTTP 的 `set.cost` 则是嵌套对象。原生 serviceTiers 对象保留在账户目录中，有效公共元数据按真实 id 规范化为字符串数组。

键存在即为明确覆盖，`0` 价格、`false` 能力、`[]` 档位不能当缺省。恢复继承必须删除键；整层恢复只删除所选层，不清目录匹配、自动快照或其他来源。有效值同时返回逐字段 `valueSource` 和 `constrainedBy`，未知资料不虚构容量、能力或价格。手工字段可收紧原生限制，不能扩张已知硬上限或开启明确不支持的能力。

### 模型元数据绑定 `modelBindings` 与压缩模型 `compressionModel`

- `defaults`：键为客户端可见模型名/渠道 alias，保存 models.dev `provider/model` identity 与绑定来源。自动同步 binding 可携带 `autoSnapshot.{catalogRevision,catalogSource,metadata,tariff}`，只冻结当前模型的基础元数据和价格，不保存整份候选目录、历史目录树或手工覆盖。
- `scoped`：先按稳定 scope key（`api:<name>` / `oauth:<provider>:<identity>`），再按客户端可见模型名索引；API alias 同时保存当时的 `outboundModel`，alias 被改指后旧专属绑定不再误用。
- 普通渠道先按 `scoped > default > none` 选择匹配基础，再叠加通用/来源稀疏手工字段，并受服务方原生硬限制约束。自动 binding 优先读自己的 `autoSnapshot`，显式 `tariff=null` 不回退活动目录旧价；旧 binding 缺快照仍读当前活动目录，不因加载而批量迁移。目录明确提供的 `limit.input` 投影为 `maxInputTokens`，与所选 context 约束输入；输出仅按有效 `maxOutputTokens` 独立判断，不从输入预算预扣客户端最大输出。缺少 maxInput 时才从 context 派生输入预算，不能因为显式值恰等于 normal context 就在 Max Context 下扩张。人工收紧 context 不联动降低输出上限；原生最大输出及人工输出收紧仍生效。容量、能力和价格使用同一来源有效解析；未知值保持未知，不按 provider 或模型前缀猜测。目录默认压缩阈值保留既有推导，但最终请求安全预算与触发阈值分开，不能把 Demo 的 80% 当作通用硬容量公式。
- Cursor OAuth scope 是有意设计的例外，固定优先级为 `Cursor AvailableModels > scoped > default > none`。它按账号自动生成只读 `cursor/<canonical-id>` 匹配基础，使用 Cursor 返回的 normal/max context 与 legacy slugs；不允许手动改绑/删除，也不拿同名 models.dev 限制覆盖。稀疏手工层仍可合法收紧：normal/Max Context 分别与 operator context 上限求交，输出/能力子集不被 native 还原；显式 maxInputTokens 与 compactTriggerTokens 不因 Max Context 扩大。所有具备独立 Max Context 档位的模型默认开启，TG 单模型开关保存关闭例外；下游显式 true/false 仍优先于账号默认。Cursor 模型目录支持按账号批量禁用 canonical 模型，禁用后该账号的 Channel 不再暴露、排列或调度这些模型；目录刷新和重新登录保留该设置，取消选择即可恢复。普通请求的压缩阈值按 `floor((contextWindow - maxOutputTokens) × 80%)` 计算；启用 Max Context 时，预检、直连压缩和 map-reduce 会改用 `contextWindowMaxMode` 动态重算阈值。`[1m]`、`~1000000`、`-context-1m`、`long_context`、`context_window=1000000` 与 Anthropic context-1m beta 会在三种 HTTP 入口统一归一，映射、白名单和调度仍使用 canonical id。Cursor 请求只记录 AgentService 实时返回的 input/output/cache usage，不抓取或覆盖 Cursor Web usage event；上游未返回缓存拆分时按 0 如实记录，Parrot 日志金额仅为本地实时记录，准确计价以 Cursor 官方为准。账号总额度仍单独以 Cursor DashboardService 为准。Cursor 展开目录中的 `low/medium/high/xhigh/max` 都是 reasoning effort；`*-thinking-max` 必须作为真实模型 ID 原样发送。Max Context 是独立维度，通过 `RequestedModel.max_mode` 和 `long_context` 表达，不得从模型 ID 的 `-max` 后缀推断。
- 元数据同步的 `one/selected/source` 只下载解析临时候选，提交对应 binding 自动快照，不发布活动目录及其磁盘 LKG；未选模型/其他来源的有效元数据、价格及活动目录 revision 必须不变。`full` 和原有后台全量刷新才发布公共目录并协调自动快照；仍遵守 `pricing.autoUpdate/refreshHours`。下载失败保留 LKG；人工匹配、稀疏手工字段与原生硬能力均受保护。full/background 的快照提交对计算输入配置执行 CAS，拒绝覆盖并发保存的 manual。兼容 account/channel/provider 同步省略 If-Match 时仍冻结受理 revision，不暗中改成强制 header。手工匹配仍可按来源模型浏览或查询 exact 候选，不与字段覆盖混用。
- `compressionModel` 是独立的客户端可见模型名。运行时按实际 compact 路由解析相同的有效绑定来取得 context、max output 和压缩阈值；普通请求的 compact 预检、直连压缩判断和 map-reduce 分段目标都会使用该阈值。旧 `modelMetadata[*].compressionModel=true` 会一次性迁移，旧手工元数据只有 exact canonical 命中时才迁成默认绑定。

### Token 金额统计 `pricing`

- `enabled`：是否在 Telegram 的统计、日志和账户等页面计算金额；关闭后不读取响应正文做费用聚合。
- `autoUpdate`：是否后台同时刷新 models.dev 的供应商 API 目录与规范模型目录。启动时先读取 `$ANTHROPIC_PROXY_DATA_DIR/models_dev_catalog.json.gz`（Docker 默认 `/app/data/models_dev_catalog.json.gz`）缓存，缓存不存在或损坏时使用仓库内置 gzip 快照；任一远端失败都不会替换当前目录或影响代理请求。
- `sourceUrl`：models.dev 供应商模型与价格目录，默认 `https://models.dev/api.json`，只接受 `https://`。金额只从这里读取，单位为 USD / 1M Token。
- `modelsUrl`：规范模型身份目录，默认 `https://models.dev/models.json`，只接受 `https://`。该文件不提供价格，仅用于 canonical 官方 exact 同名匹配。
- `refreshHours`：远端刷新间隔，最小 1 小时。
- 旧 `pricing.channelProviders` / `aliases` / `overrides` 字段可继续留在配置中，避免升级时丢配置；dispatch-time 估算不使用这些旧字段绕过元数据绑定。新的手工价格差异仅通过 `modelMetadataOverrides` 的六项价格字段生效，基础 tariff 与自动 binding 快照保持独立。

新请求在每次上游尝试 dispatch 时按真实 scope、客户端可见 model 和出站真实 model 解析有效元数据绑定，并冻结其 models.dev provider/model、费率与目录版本；之后配置或目录更新不会重算该结算。没有有效绑定或绑定记录没有可用 Token 价格时保持 `unpriced`。只有没有尝试账本的历史请求才会按当前有效绑定做兼容估算。xAI OAuth 响应包含 `usage.cost_in_usd_ticks` 时优先采用该次尝试的真实上游金额。长上下文阶梯按**单次请求**的 `input + cache creation + cache read` 判断；当前结算结构只支持一档 context tier，目录若为同一模型提供多档阈值则该模型 fail-closed 为未计价。`experimental.modes.fast.cost` 是完整替换价，不与标准长上下文价叠加；没有响应/真实出站 fast 事实时不会从下游 intent 臆测加速价，实际为 priority/fast 但目录没有对应 tariff、或上游返回 `flex` 等未知计费档位时同样保持未计价。数据库只保存缓存写入总 Token、没有保存 Anthropic 5 分钟 / 1 小时 TTL 拆分，因此 Claude 请求只要包含 cache creation 就标记为“未计价”。目录若要求单独计费 reasoning/audio Token、但价格与聚合 input/output 不同，也会保持未计价，避免用缺失的 Token 维度生成假精确金额。

Telegram 界面只显示合并后的 USD 金额，不展示金额来源分类或未计价次数；统计页面保留两位小数，最近日志紧凑列表保留三位小数，均不加约等号。models.dev 计价结果与 xAI 上游金额会直接合并到同一个总额，内部仍保留各自结算来源及无法计价记录，以保证账本和聚合口径不变。Parrot 不做实时汇率换算。旧版 OpenAI 日志曾把缓存读取 Token 同时包含在 `input_tokens` 中；若历史行缺少明确的 usage 口径且无法确认新旧语义，内部不会把它作为已知金额计入总额。

### 媒体模型与共享生成缓存

- Grok 图片/视频名单继续分别存于 `xaiOAuth.imageModels/videoModels`，单项增删改名和整组替换由同一控制层写入，保序且不串改另一组。
- AG 全局图片名单存于 `antigravityOAuth.imageModels`；已有 `oauthAccounts[*].imageModels` 是账户专属覆盖，与同名全局项分离。模型中心只允许编辑 AG 全局范围，专属范围只读；全局清空不清账户专属数据。
- `images.enabled` 为 GPT/Grok/AG 图片总开关；`images.mainModel/toolModel` 仍仅属于 GPT 图片内部管线，不作为下游缺少 model 的默认值。
- `images.cacheEnabled` 默认 false。`cachePath` 默认 `images`；相对路径以 `DATA_DIR` 为根且不可逃逸，绝对路径按明确配置使用。缓存目录应专用于媒体，不能与无关文件混放。
- `images.cacheRetentionDays` 默认 0（永久），`cacheMaxBytes` 默认 1 GiB（0 表示不设聚合上限）。原 GPT/Grok 缓存和新增 AG 图片生成共用这些参数；不新增 AG 独立保留策略。聚合不限不等于取消单文件保护限制。
- AG 缓存保存已生成的 base64 图片，不修改 `b64_json` 或带 MIME 的 data URL 响应，不增加图片编辑支持。缓存写失败不把成功生成改成失败；统一媒体日志分别保存生成结果与缓存状态/错误类别。

接口、权限、revision、单项 owner 与下载边界见 [模型中心](14-model-center.md#媒体模型与共享缓存)。

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
  - `stateDbPath` / `runtimeStatePath` / `durableStatePath` / `logDir` / `openai.store.dbPath`（需重启）
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
  "listen": {"host": "0.0.0.0", "port": 22122},
  "apiKeys": {},
  "oauthAccounts": [],
  "channels": [],
  "timeouts": {"connect": 10, "firstByte": 30, "idle": 30, "total": 600},
  "errorWindows": [1, 3, 5, 10, 15, 0],
  "telegram": {"botToken": "", "adminIds": []},
  "logDir": "logs",
  "stateDbPath": "state.db",          // 仅旧版只读迁移源
  "runtimeStatePath": "runtime-cache.json",
  "durableStatePath": "durable-state.json"
}
```
其余字段使用 `src/config.py` 中的 `DEFAULT_CONFIG` 补齐。
