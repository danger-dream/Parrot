# 05 — Claude Code v2.1.258 mimicry

`src/transform/cc_mimicry.py` 是 Parrot 的 Claude Code（CC）messages 伪装实现。当前协议基线来自 v2.1.258 的本地 wire 语料与定稿验证器；旧 `cc-proxy` 只代表历史实现，**不再是 v2.1.258 oracle**。

## 5.1 启用边界

- Anthropic OAuth channel 固定走 CC mimicry。
- Anthropic API channel 仅在 `cc_mimicry=true` 时走该路径；`false` 继续使用 `src/transform/standard.py`。
- OpenAI/Codex provider 不走 CC mimicry；OpenAI ingress 只有被协议矩阵路由到 Anthropic CC channel 时才会在翻译后构造 CC body。
- `cchMode` 保持 `dynamic` / `static` / `disabled` 三种语义。代码升级不会修改 `config.json`，也不会自动启用渠道。

## 5.2 v2.1.258 常量与 fingerprint

- `CC_VERSION = "2.1.258"`
- `CC_ENTRYPOINT = "sdk-cli"`
- UA：`claude-cli/2.1.258 (external, sdk-cli)`
- fingerprint salt：`59cf53e54c78`
- indices：`[4, 7, 20]`

Prompt 在注入 downstream `system` 之前，从原始 messages 选择：第一条非 meta user turn 的第一个有效 text block。显式 `isMeta` 和完整 `<system-reminder>...</system-reminder>` block 被跳过；`<session>...</session>` side query 是有效文本。

索引遵循 JavaScript UTF-16 code unit，而不是 Python Unicode code point。若选中 surrogate 的单边，按 Node/Bun UTF-8 行为以 U+FFFD 参与 SHA-256；越界使用字符 `"0"`。emoji 固定向量 `ab🚀d🚀f🚀hijklmnopqr🚀t` 的结果为 `963`。

## 5.3 CCH v258

Seed 保持：

```text
0x4D659218E32A3268
```

计算过程：

```text
CCH = XXH64(hash_view(body_with_generated_billing_cch=00000), seed) & 0xFFFFF
```

`hash_view` 规则：

1. 只删除**顶层** `max_tokens`。
2. 任意层级 key 为 `model` 且 value 为 string 时，把 value 清空为 `""`；key、dict/schema 形态和递归结构保留。
3. `fallbacks`、`fallback_credit_token`、嵌套 `max_tokens` 及其余字段原样参与。
4. 只把 Parrot 生成的第一个 billing block 中 CCH 重置为 `00000`。

wire payload 与 hash view 分离。`sign_body()` 先把最终 payload 序列化一次，再以独立规范化视图计算 hash，最后按已定位的 system billing block byte offset 修改五位 CCH；不会发送规范化副本，也不会用裸 `bytes.replace(..., 1)` 误改 messages 中的用户正文。签名后不再重序列化。

真实 v2.1.258 代表语料结果为 25/26；唯一 mismatch 是 `body_race_anomaly`（原客户端并发竞态使 wire 与签名时点不一致），Parrot 不复刻该 bug。

## 5.4 Body profiles

最终主要 key 顺序为：

```text
model, messages, system, tools, metadata, max_tokens,
thinking, context_management, temperature, fallbacks,
output_config, diagnostics, stream
```

不存在的可选字段不会占位；`tool_choice`、显式 cache 等兼容字段按原有代理能力保留。

### 普通 main

- 默认 `max_tokens=64000`
- 未显式指定 thinking 时：`{"type":"adaptive","display":"omitted"}`
- thinking 为 enabled/adaptive 且未显式指定 context management 时，生成 `clear_thinking_20251015`
- 未显式指定 output config 时：`{"effort":"high"}`
- `diagnostics={"previous_message_id":null}`

### Fable API-key main

实证 wire model 是 `claude-fable-5`。不创建未经模型发现或显式映射证实的 `claude-fable-5.1`。

在普通 main 基础上增加：

```json
{
  "fallbacks": "default",
  "diagnostics": {"previous_message_id": null},
  "thinking": {"type": "adaptive", "display": "omitted"},
  "output_config": {"effort": "high"}
}
```

OAuth Fable main 没有本地权威 body 样本：transform 保留 downstream 显式字段，不自动制造未知 body 组合；header 只应用已实证的 OAuth auth 差异。

### Opus 4.8 与 Opus 5

Opus 4.8 使用普通 main beta profile，默认不带 `context-1m`。实证 Opus 5 profile 带 `context-1m`、advisor 与 fallback-credit；因此不能全局删除 `context-1m`。下游显式 1M 信号与 Parrot 的有界 1M 降级逻辑仍保留。

### Haiku side query

模型为 Haiku 且 structured output 或 `<session>` prompt 表明 side query 时：

- `max_tokens=32000`
- `thinking={"type":"disabled"}`
- `temperature=1`
- 生成/保留 title JSON schema structured output
- 不带 `fallbacks`、`diagnostics` 或 `cc_prompt_id`

显式 downstream `thinking.type=disabled`、effort、temperature、stream 等语义优先，不为画像覆盖用户可见意图。Parrot 不复刻固定 12 tools 或完整私有 system prompt。

## 5.5 Header 与 auth

应用层顺序与 v2.1.258 语料一致：Accept、auth（OAuth 时）、Content-Type、UA、session、Stainless、beta、dangerous-direct-browser、version、API key（API-key 时）、x-app、x-client-request-id。

固定值：

- `X-Stainless-Package-Version: 0.112.1`
- `X-Stainless-Runtime-Version: v26.3.0`
- `X-Stainless-Retry-Count: 0`
- `X-Stainless-Timeout: 600`

认证：

- OAuth：`Authorization: Bearer ...`
- 官方 Anthropic API key（provider fact 或 `api.anthropic.com`）：`x-api-key: ...`
- 已有第三方 Anthropic-compatible API channel 继续使用其既有 Bearer 形态，不做全局硬切。
- messages 主链不带 `oauth-2025-04-20`。

Parrot 只声明 `Accept-Encoding: gzip, deflate`。在 Brotli/Zstandard 解码依赖与测试加入前，不声明 `br` / `zstd`。

## 5.6 Request / attempt 生命周期

一次逻辑请求在 body 私有上下文中保存：

- `_parrot_claude_code_session_id`
- `_parrot_cc_prompt_id`

这些字段只进入 CC 专用 provider allowlist，并在 transform 中消费，不上 wire。已有 downstream `x-claude-code-session-id` 被优先使用；缺失时 failover 在逻辑请求入口生成一次。OpenAI ingress 跨到 Anthropic CC channel 时，私有上下文会跨 bridge translation 保留。

`metadata.user_id` 中：

```json
{"device_id":"<stable>","account_uuid":"","session_id":"<same-as-header>"}
```

main billing 顺序固定为 version → entrypoint → cch → workload → is_subagent → prev_req → prompt_id。当前只有 prompt ID 有 request-scoped 权威来源；workload/subagent/prev_req 不凭空推断。side query 不带 prompt ID。

每次真实 HTTP dispatch 都在 `src/transports/http_runtime.py` 复制 headers 后刷新 `x-client-request-id`。同候选 529 或 OAuth refresh retry 会重建 channel request，但复用 logical session/prompt context，因此 body bytes、CCH、metadata 和 prompt ID 保持不变；`X-Stainless-Retry-Count` 始终为 `0`。headers 不在共享 dict 上原地修改，并发请求不会串线。

## 5.7 离线验证

严禁使用本测试路径向真实上游发送请求。项目隔离入口：

```bash
./venv/bin/python src/tests/isolated_pytest.py -q \
  src/tests/test_cc_v2_1_258_upgrade.py \
  src/tests/test_channel_compatibility.py
```

`test_cc_v2_1_258_upgrade.py` 覆盖：26 个 fingerprint、25/26 CCH、CCH 双体矩阵、1.13MB Fable、Fable/Opus/side/OAuth profiles、auth、header 顺序、private-field stripping 与非 CC 边界。

`test_protocol_fake_upstreams.py::test_cc_v258_529_reuses_body_context_and_isolates_concurrent_requests` 使用 `httpx.MockTransport` 验证 529 与两个并发逻辑请求，不触网。

`compare_transform.py` / `compare_channels.py` 仅作为兼容入口调用上述隔离测试，不再加载旧 `cc-proxy`。

## 5.8 device_id

`.anthropic_proxy_ids.json` 保存跨进程稳定的 32-byte hex `device_id`。测试 bootstrap 把该文件、config、state 与 logs 全部重定向到临时目录，不能写权威数据目录。
