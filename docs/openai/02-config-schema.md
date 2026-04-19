# 02 — Config Schema 变更

所有变更均为**纯追加字段**。现有字段默认值保持不变，读旧 `config.json` 不需要迁移。

## 2.1 新增顶层节点：`openai`

```jsonc
{
  "openai": {
    // previous_response_id 本地 store 相关（见 05）
    "store": {
      "enabled": true,              // 关闭则下游 responses 调 chat 上游时拒绝带 previous_response_id 的请求
      "ttlMinutes": 60,             // 记录 TTL
      "cleanupIntervalSeconds": 300 // 后台清理周期
    },
    // reasoning 跨协议桥接（见 06）
    "reasoningBridge": "passthrough",  // "passthrough" | "drop"
    // 跨变体翻译时的能力开关
    "translation": {
      "enabled": true,              // false = 禁止 chat↔responses 跨变体，只允许同协议
      "rejectOnBuiltinTools": true, // responses→chat 时遇到 built-in tool 即 400
      "rejectOnMultiCandidate": true // chat→responses 时遇 n>1 / logprobs 等即 400
    }
  }
}
```

## 2.2 `channels[]` 追加字段

```jsonc
{
  "channels": [
    {
      "name": "deepseek",
      "baseUrl": "https://api.deepseek.com",
      "apiKey": "sk-...",
      "protocol": "openai-chat",     // ★ 新增，取值："anthropic" | "openai-chat" | "openai-responses"；缺省 "anthropic"
      "models": [{"real": "deepseek-chat", "alias": "dp"}],
      "cc_mimicry": false,            // openai-* 时此字段被忽略（内部强制 false）
      "enabled": true
    }
  ]
}
```

**兼容性**：未设 `protocol` 字段的渠道一律视为 `"anthropic"`（与现状等价）。

**CC 伪装规则**（不变）：仅对 `protocol == "anthropic"` 的渠道生效；OpenAI 家族渠道不做任何 CC 伪装。

## 2.3 `apiKeys[]` 追加字段

```jsonc
{
  "apiKeys": {
    "default": {
      "key": "ccp-xxx",
      "allowedModels": [],                    // 已有
      "allowedProtocols": ["chat", "responses"] // ★ 新增
    }
  }
}
```

`allowedProtocols` 语义：
- 空数组 / 未设置 → **全部放行**（向后兼容，与 `allowedModels` 空 = 无限制一致）
- 非空 → Key 只能调用列表中声明的入口协议
- 合法值：`"anthropic"` / `"chat"` / `"responses"`（三选任意组合）

鉴权时序：
1. `auth.validate(headers)` 原样（返回 3 元组 `key_name, allowed_models, err`）
2. OpenAI 入口额外调 `auth_ex.get_allowed_protocols(key_name)` → 若非空且不含当前 ingress → 403
3. Anthropic 入口不感知本字段（不受影响）

同样的检查还会影响 `/v1/models` 返回的渠道家族过滤（见 04）。

## 2.4 OAuth 账户配置

**不追加字段**。`oauthAccounts[]` 永远是 anthropic 家族，对应 `OAuthChannel` 的 `protocol="anthropic"`（类属性硬编码，不从 config 读）。

## 2.5 不可热加载的字段

与现状一致：`listen.*` / `stateDbPath` / `logDir` / `telegram.*`。

新增字段 `openai.*` / `channels[].protocol` / `apiKeys[].allowedProtocols` **全部支持热加载**（`config.update` 触发 `registry.rebuild_from_config`，后者按新 `protocol` 重新实例化渠道）。

## 2.6 默认值清单（加到 `config.DEFAULT_CONFIG`）

```python
DEFAULT_CONFIG["openai"] = {
    "store": {
        "enabled": True,
        "ttlMinutes": 60,
        "cleanupIntervalSeconds": 300,
    },
    "reasoningBridge": "passthrough",
    "translation": {
        "enabled": True,
        "rejectOnBuiltinTools": True,
        "rejectOnMultiCandidate": True,
    },
}
```

`channels` / `apiKeys` 的默认值结构不变，新字段各取"零值"（`protocol` 默认 `"anthropic"` 由 `ApiChannel.__init__` 兜底，`allowedProtocols` 缺省视作空列表）。

## 2.7 一个完整示例

```jsonc
{
  "listen": { "host": "0.0.0.0", "port": 22122 },
  "apiKeys": {
    "claude_only": { "key": "ccp-a", "allowedModels": [], "allowedProtocols": ["anthropic"] },
    "openai_only": { "key": "ccp-b", "allowedModels": [], "allowedProtocols": ["chat","responses"] },
    "full":        { "key": "ccp-c", "allowedModels": [], "allowedProtocols": [] }
  },
  "oauthAccounts": [ /* 现状 */ ],
  "channels": [
    { "name": "智谱",    "type": "api", "baseUrl": "https://...", "apiKey": "...",
      "protocol": "anthropic", "models": [{"real":"GLM-5","alias":"glm-5"}],
      "cc_mimicry": true, "enabled": true },
    { "name": "openai",  "type": "api", "baseUrl": "https://api.openai.com", "apiKey":"sk-...",
      "protocol": "openai-responses", "models": [{"real":"gpt-5","alias":"gpt-5"}],
      "enabled": true },
    { "name": "deepseek","type": "api", "baseUrl": "https://api.deepseek.com","apiKey":"sk-...",
      "protocol": "openai-chat", "models": [{"real":"deepseek-chat","alias":"dc"}],
      "enabled": true }
  ],
  "openai": {
    "store": { "enabled": true, "ttlMinutes": 60, "cleanupIntervalSeconds": 300 },
    "reasoningBridge": "passthrough",
    "translation": { "enabled": true, "rejectOnBuiltinTools": true, "rejectOnMultiCandidate": true }
  }
  // 其余字段按现状
}
```
