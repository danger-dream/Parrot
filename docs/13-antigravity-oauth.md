# 13 — Antigravity OAuth 内嵌方案

> 状态：里程碑 1–3 已落地；隔离实打已用 soarsky 账号打通 daily generateContent。
> 生产 parrot 不得重启、不得热改线上 `config.json`。
> 对照实现：`/opt/workspace/CLIProxyAPI`（v7.2.140 / `a7e3596b`）。
> 决策日期：2026-08-23。

## 0. 已拍板决策

1. **v1 模型范围：全部 Antigravity 目录都要做**，包括 Gemini 文本、Claude-on-Antigravity、GPT-OSS、出图。不能先砍成「只做 Gemini 文本」。
2. **线上 CPA 继续跑**。允许只读使用其已登录的 2 份凭证做联调，但：
   - 必须按官方客户端伪装发包；
   - **禁止私自 refresh token**，以免轮换 refresh_token 让 CPA 历史凭证作废；
   - 不得把 token 写入 parrot 仓库、测试 fixture、日志、回复或长期 Memory 明文。
3. 测试必须走独立 docker / 独立端口 / 独立 config，**不得影响线上 parrot**（当前上游）。
4. 编码优先对齐 CPA 已验证行为，但落点必须是 parrot 现有 OAuth / Channel / TG / quota 模型。

## 1. 目标

把 CPA 的 Antigravity OAuth 内嵌进 parrot，替代「单独跑一个 CPA 只为 Google 账号」：

- TG 新增 / 覆盖 / 列表 / 详情 / 默认模型
- 三入口调用：`/v1/messages`、`/v1/chat/completions`、`/v1/responses`
- Gemini generateContent + Cloud Code 信封
- credits / 429 额度展示与自动冷却
- 离线 fixture + 隔离环境实打上游

## 2. 非目标（v1 仍不做）

- 官方 Gemini API Key / Vertex
- 新增 parrot `gemini` protocol family
- 原生 `/v1beta/generateContent` 入口
- 把 Antigravity 的 `claude-*` 伪装成 Claude OAuth 渠道
- credits 显示成货币或伪造 5h/7d 百分比
- hosted tools、`previous_response_id`、audio、file_id、多 candidate 聚合

## 3. 架构

```
TG「新增 OAuth → Antigravity」
  → Google 授权码 + 粘贴 localhost callback
  → userinfo email + loadCodeAssist project_id
  → config.oauthAccounts[]
      provider=antigravity
      account_key=antigravity:{email}:{project_id}

下游三入口
  → ProtocolMatrix 按 openai-responses 路由/guard
  → AntigravityOAuthChannel.build_upstream_request()
       ingress → Gemini generateContent
       → Cloud Code 信封（参考 CPA geminiToAntigravity）
  → POST cloudcode-pa.googleapis.com/v1internal:generateContent
       或 streamGenerateContent?alt=sse
  → AntigravityOAuthAdapter（toolkit 之前）
       Gemini JSON/SSE → 标准 Responses JSON/SSE
  → 现有 Responses toolkit / 回译 / commit gate / failover
```

调度身份学 xAI：`protocol="openai-responses"`，继续归 `openai` family。  
**不要**把 Gemini candidates SSE 直接喂给现有 Responses tracker。

## 4. OAuth

对齐 CPA，不要套 xAI 的 PKCE：

- Authorize: `https://accounts.google.com/o/oauth2/v2/auth`
- Token: `https://oauth2.googleapis.com/token`
- Userinfo: `https://www.googleapis.com/oauth2/v2/userinfo?alt=json`
- Client / scopes / callback port 与 CPA `internal/auth/antigravity/constants.go` 一致
- 登录后：`loadCodeAssist` 取 `cloudaicompanionProject`；没有则 `onboardUser`
- 缺 `project_id` fail-closed
- TG UX 对齐 OpenAI/xAI：打开链接 → 粘贴完整 callback URL
- 同 identity 再登录走现有覆盖确认

身份：

```
account_key  = antigravity:{email}:{project_id}
channel.key  = oauth:antigravity:{email}:{project_id}
展示         = email
```

同 Gmail 可同时存在 Claude / Codex / Antigravity。  
同 email 不同 project 是两个账户。refresh 不得改 `project_id`。

联调约束：隔离测试环境读取 CPA 当前 `access_token`；**token 过期就停，等 CPA 自己刷新后再拷贝，测试进程不得调 token 端点。**

## 5. 模型与调用

权威目录：CPA `internal/registry/models/models.json` 的 `antigravity` 段（13 个）。  
`fetchAvailableModels` 只补 Web Search capability，不换目录。

| 上游 ID | 处理 |
|---|---|
| Gemini 文本 / agent / lite / low | `requestType=agent` |
| `gemini-3.1-flash-image` | `requestType=image_gen`，不要进普通文本调度器（学 xAI `imageModels`） |
| `claude-opus-4-6-thinking` / `claude-sonnet-4-6` | 仍走 Antigravity 信封；Claude 工具 schema 按 CPA 做 VALIDATED / sanitize |
| `gpt-oss-120b-medium` | 同 agent 信封，能力按模型而不是按 Google |

默认模型：`antigravityOAuth.defaultModels`，不要复用顶层 `oauthDefaultModels`。

最小请求（对齐 CPA：generate/stream 走 daily，loadCodeAssist 走 prod）：

```http
POST https://daily-cloudcode-pa.googleapis.com/v1internal:generateContent
Authorization: Bearer <token>
Content-Type: application/json
User-Agent: antigravity/hub/<>=2.9.1> darwin/arm64
```

```json
{
  "project": "<project_id>",
  "model": "<upstream id>",
  "userAgent": "antigravity",
  "requestType": "agent",
  "requestId": "agent-<uuid>",
  "request": {
    "sessionId": "-<int64>",
    "contents": [],
    "generationConfig": {},
    "tools": []
  }
}
```

流式：`:streamGenerateContent?alt=sse`。  
下游非流式打非流式接口，`upstream_stream_only=False`。

客户端伪装（已授权）：

- 稳定合法 Antigravity UA（版本门槛真实存在，`<2.9.0` 会拒新模型）
- 信封字段与 CPA 对齐
- HTTP/1.1 / 关 ALPN / 按凭证拆连接池：能按 CPA 做就做，但隔离测试环境优先保证协议正确
- 参考 CPA 的 `antigravity-coding-filter` 语义：不要把明显的逆向/泄漏类请求原样打上去

## 6. 协议转换

Channel 内：ingress → Responses-like → Gemini → 信封。  
Adapter 在 toolkit 前：Gemini JSON/SSE → 标准 Responses。

必做：多轮文本、function calling 闭环、非流式、流式、finishReason、usageMetadata。  
后置但不许从 v1 目录拿掉：图片、structured output、同账号 thought signature。  
Matrix 现有 guard（Anthropic thinking、hosted tools、file_id、audio、`n>1`）保持。

SSE 文本是增量还是累计，必须用真实录制 fixture 确认，不能猜。

`adapter_for_channel()` 对未知 `openai-*` OAuth 会落到 Codex。Antigravity **必须显式注册自己的 adapter**。

## 7. 额度

`POST .../v1internal:loadCodeAssist` + `{"metadata":{"ideType":"ANTIGRAVITY"}}`  
读 `paidTier.id` 与 `GOOGLE_ONE_AI` 的 `creditAmount` / `minimumCreditAmountForUsage`。  
没有总额、没有重置。展示独立 `raw_data.antigravity` block，学 xAI/Cursor。

429：

- `<3s`：同账户短重试
- `3s–5m`：账户+模型冷却
- `QUOTA_EXHAUSTED` / `≥5m` / `INSUFFICIENT_G1_CREDITS_BALANCE`：整账户 `disabled_reason=quota`

`loadCodeAssist` 至少 10 分钟节流。`known=false` 不恢复、不当 0%。

联调拉 credits 用当前 access_token，不 refresh。

## 8. TG

第五个 OAuth provider。改 `oauth_menu` / `oauth_defaults_menu` / `help_menu` / `ui.py` / `notifier.py` / `oauth_errors.py` / `network_monitor.py`。  
网络探测不得误打 `chatgpt.com/codex`。

## 9. 文件落点

新增：

- `src/oauth/antigravity.py`
- `src/channel/antigravity_oauth_channel.py`
- `src/providers/` Antigravity adapter + Gemini↔Responses codec
- `src/tests/test_antigravity_*.py`

必改：`oauth/__init__.py`、`oauth_ids.py`、`oauth_manager.py`、`channel/registry.py`、`providers/registry.py`、`protocols/matrix.py`、`config.py`、上述 TG / notifier / network / state_db。

## 10. 测试环境

- 线上 parrot：只读，不重启，不改 `config.json`
- 隔离环境：独立 docker 或独立工作目录 + 独立端口 + 独立 config/state/logs
- CPA 凭证：只读拷贝到测试目录，gitignore；测试代码禁止调用 Google token 端点
- 先离线 fixture，再对上游打最小真实请求
- 实打前确认 access_token 未过期；过期则等 CPA 刷新后再拷贝

## 11. 实施顺序

1. 身份 / config / oauth 模块 / manager 分派 / 离线测试
2. Channel + 信封 + Gemini↔Responses codec + fake upstream
3. TG 登录 / 覆盖 / 默认模型 / credits 展示
4. 隔离环境实打：文本、工具、Claude-on-Antigravity、出图
5. 流式 + Chat/Anthropic 回译
6. quota 冷却与通知

## 12. 对照资源

- CPA 源码：`/opt/workspace/CLIProxyAPI`
- CPA 部署：`/opt/docker/cpa`，容器 `cpa`，UI `http://10.0.9.2:8317`
- 凭证目录：`/opt/docker/cpa/auths/`
  - `antigravity-6768656@gmail.com.json`
  - `antigravity-soarsky0204@gmail.com.json`
- parrot 模板：`src/oauth/xai.py`、`src/channel/xai_oauth_channel.py`、`src/telegram/menus/oauth_menu.py`
