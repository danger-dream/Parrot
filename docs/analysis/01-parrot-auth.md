# Parrot — OpenAI OAuth 与 Codex 应用层身份生命周期

> 当前实现说明。协议画像以本仓库配置选择的 `rust-v0.153.4` 为准；account、thread、turn 与 window 均由下述权威生命周期生成。

## 1. 权威实现位置

| 文件 | 职责 |
|---|---|
| `src/oauth/openai.py` / `src/oauth_manager.py` | PKCE 登录、token 交换/刷新、workspace 解析与账号持久化 |
| `src/openai/codex_constants.py` | 严格加载 `openaiOAuth.codexProtocolProfile` 与 `codexCliVersion`，提供 version/originator/UA/WS beta/model policy |
| `src/openai/codex_identity.py` | account installation、logical session/thread、turn、window、turn-state、explicit turn mapping 与 per-thread queue |
| `src/state_db.py` / `src/state_store.py` | installation tombstone、logical session/window、compaction owner/确认标记的 durable 原子状态 |
| `src/channel/openai_oauth_channel.py` | 最终 OAuth owner 确定后解析身份、排队并把同一 snapshot 投影到 HTTP/WS 请求 |
| `src/openai/responses_ws.py` / `src/failover.py` | HTTP/SSE/WS 生命周期、重试、event-level turn-state 捕获和 commit 边界 |
| `src/openai/compaction_owner.py` | compaction owner/session/model 验证和确认成功后的 window CAS |
| `src/openai/codex_identity_mapper.py` | 只对协议身份字段做结构化下游值还原，不扫描或替换普通文本 |

## 2. OAuth 与 owner 绑定

OpenAI 使用 authorization-code + PKCE 和 refresh-token：

- authorize scope 包含 `openid profile email offline_access api.connectors.read api.connectors.invoke`；
- 真实 refresh 请求不传 scope；
- access token 作为 `Authorization: Bearer …`；
- `id_token`/accounts 信息提供 canonical `workspace_id` / `chatgpt_account_id`；
- 上游请求同时发送 `chatgpt-account-id`。

Codex identity 的 owner 不是 email、channel key 或 access token，而是 canonical workspace 的不可逆 digest：

```text
owner_digest = sha256("openai\0" + workspace_id)
```

refresh、重命名、重导入或 channel registry 重建不会改变同一 workspace 的 owner。workspace 在最终 refresh 后仍未知时，Codex dispatch fail closed，不生成无主身份。

## 3. 三层身份模型

### 3.1 Account installation

每个 canonical OAuth workspace 恰有一个随机 UUIDv4 `installation_id`：

- versioned `codexIdentity` 写入账号配置；
- durable `codex_identity_tombstones` 绑定 owner→installation，防止普通删号/重导入静默换号；
- 不同 workspace 共享 installation 会被拒绝；
- 只有显式高风险 forget 路径才删除 tombstone 及该 owner 的 session/compaction 状态。

不存在 `codexDeviceConvergenceEnabled` 退出开关，也不存在全账户共享 installation。

### 3.2 Durable logical session/thread

稳定 downstream anchor 按 downstream API principal 隔离并先哈希，再解析为 durable `LogicalSession`：

- `session_id == root_thread_id == upstream_prompt_cache_key`，均为 UUIDv7；
- durable key 为 `(owner_digest, principal_digest, anchor_digest)`；
- native `client_metadata.session_id`、已有 `x-codex-turn-metadata.session_id`、受支持 header carrier，以及显式/内部 prompt-cache affinity 可作为 lookup anchor；
- raw downstream principal、session、prompt cache 或 installation 不写入 registry，也不发上游；
- 无稳定 anchor 的请求只得到 request-local session。

HTTP 和 WS 都从一个不可变 `RequestIdentitySnapshot` 生成 `session-id`、`thread-id`、`x-client-request-id`、`x-codex-window-id`、`x-codex-turn-metadata` 和 `client_metadata`。旧的下划线 `session_id` header、`conversation_id` 和 downstream 身份 carrier 会被清除/覆盖。

### 3.3 Turn 与显式 continuation

普通新 turn 获得新的 UUIDv7 `turn_id` 和空 turn-state。若 downstream 已通过现有 native carrier 明确给出 `turn_id`：

- Parrot 只把 raw ID 哈希后用于 `(owner, logical session/thread, downstream turn digest)` lookup；
- 同一 downstream turn 的 HTTP/WS continuation 与 retry 复用同一 upstream UUIDv7 和 turn-state；
- 新 downstream turn ID 结束同 thread 的旧活动映射并生成新 UUIDv7；
- mapping 按 owner/session/thread 隔离，30 分钟 TTL，并有 4096 条硬上限；
- raw downstream turn ID 从不发送上游；响应只在 `turn_id` 等协议字段中结构化还原，不替换 assistant 普通文本。

## 4. Per-thread 串行化

每次 OpenAI OAuth Codex dispatch 在最终 owner 和 logical thread 确定后，进入 key 为 `(owner_digest, thread_id)` 的 async queue：

- 同一 owner/thread 的上游 turn 不并发；
- 不同 thread 可并行，不使用全账户锁；
- queue 覆盖请求发送、响应消费和成功/错误 finalization；
- 正常、异常及取消都会释放；无 holder/waiter 的 entry 立即回收；
- waiter 获准后重新读取 durable logical session，因此等待期间发生的 compaction window advance 不会发送旧 window。

## 5. Window 与 compaction

初始 window 为：

```text
window_number = 0
window_id = "{thread_id}:0"
context_window_id = UUIDv7
```

只有成功路径实际返回完整 `type="compaction"`（非空 `id` + `encrypted_content`），并且 owner、logical session/thread 与 model 都和当前请求匹配时，才执行 durable 原子确认：

1. compaction 请求/生成请求使用旧 window snapshot；
2. compaction owner rows 与 logical-session row 在同一 durable transaction 中写入确认标记并 CAS `n→n+1`；
3. 生成新的 UUIDv7 `context_window_id`；
4. 下一 queued request 刷新后使用 `{same thread}:{n+1}`。

失败响应、retry、请求 history 中已有 compaction、普通 history 截断和 model/session/owner 不匹配均不推进。相同成功结果重复或并发 finalization 只推进一次。

Parrot 当前没有 `/responses/compact` transport/route；本包没有虚构 endpoint。上述 consumer 接在已有 Responses 成功终态中返回 compaction item 的真实路径。将来若 transport 包增加 remote compact endpoint，必须复用同一确认事务和旧-window→新-window边界。

## 6. Turn-state 生命周期

`x-codex-turn-state` 是上游账号+turn 范围内的 opaque sticky-routing token：

- HTTP/SSE response headers、WS upgrade headers，以及官方 `response.metadata.headers` event carrier都会被捕获；
- 捕获时校验当前 snapshot 的 owner 和 turn；
- 同 turn retry/continuation 重新投影该 token；
- 新 turn 清空；跨 OAuth owner 解析目标账号自己的 context，不能携带外国 token；
- downstream 提供的 `x-codex-turn-state` 永不受信任，投影前会被删除。

## 7. Retry、failover 与 commit

- 同一账号、同一逻辑请求 retry 复用 request-local identity context，因此 IDs 和已捕获 turn-state 不变；
- 跨 OAuth 候选会创建目标 owner 自己的 account/session/turn context；foreign turn-state 不会跨 owner；
- encrypted content/response continuation 仍按已有 owner 与 portability 规则处理，compaction item 不允许跨 owner；
- `response.created` 表示上游已实例化请求。即使它不是 assistant 可见输出，也构成 dispatch commit；之后的错误不得换 OAuth 账号重放；
- 任一真正可见 output/tool event 后同样禁止 failover。

## 8. Wire 画像

Codex CLI 画像不是源码 fallback。`openaiOAuth.codexProtocolProfile` 选择 `src/openai/codex_profiles/*.json`，并要求 `openaiOAuth.codexCliVersion` 与 profile 匹配。当前基线为 `rust-v0.153.4`。HTTP/WS 从同一 profile 取得 version、originator、User-Agent、WS beta 和 model policy；缺配置或不匹配时 fail closed。
