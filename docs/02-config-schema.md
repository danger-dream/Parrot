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
      "limits": {                      // 单 Key 限流覆盖；字段缺失/null 继承 apiKeyConcurrency
        "enabled": null,               // 优先级高于 apiKeyConcurrency.enabled
        "maxConcurrent": null,         // 0 = 不限并发
        "maxQueue": null,              // 0 = 不排队，满并发直接 429
        "queueWaitSeconds": null       // 0 = 不等待，满并发直接 429
      }
    }
  },

  // ─── 下游 API Key 级并发限制默认值 ───
  "apiKeyConcurrency": {
    "enabled": true,
    "defaultMaxConcurrent": 5,
    "defaultMaxQueue": 50,
    "defaultQueueWaitSeconds": 1800
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
    "defaultModels": ["grok-4.5"]
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

  // ─── 路径 ───
  "logDir": "logs",
  "stateDbPath": "state.db"
}
```

`apiKeys.<name>.key` 是下游客户端作为 Bearer / x-api-key 使用的密钥字符串。配置层不要求 `ccp-` 前缀，任意字符串都可；TG bot 自动生成时仍使用 `ccp-<48 hex>`，也可以在菜单里输入自定义 key。

`apiKeys.<name>.enabled` 控制该 Key 是否可用，缺失或 `null` 时按 `true` 处理。`apiKeyConcurrency` 是 API Key 级限流默认值；`apiKeys.<name>.limits.enabled/maxConcurrent/maxQueue/queueWaitSeconds` 是单 Key 覆盖，其中 `limits.enabled` 优先级高于全局 `apiKeyConcurrency.enabled`。默认单 Key 5 并发、50 队列、最长等待 1800 秒；队列满、等待超时或客户端断开时请求会从队列移除并返回/结束。

## 2.2 字段语义详解

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
  - `stateDbPath` / `logDir`（需重启）
  - `telegram.botToken` / `telegram.adminIds`（需重启）

## 2.4 TG Bot 对 config.json 的写入

TG Bot 修改的所有操作都走 `config.save()`，采用 `tmp + os.replace` 原子写：
- 添加/编辑/删除渠道
- 添加/编辑/删除 OAuth 账户
- 添加/删除 API Key
- 修改超时 / 错误阶梯 / 黑名单 / CCH 模式

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
