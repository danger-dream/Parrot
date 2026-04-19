# 09 — 实施里程碑

按"每完成一步都能跑、anthropic 全绿回归"的原则切片。每个 MS 末尾运行：

- `pytest src/tests/`（anthropic 既有测试必须 100% 通过）
- 对应 MS 的新测试
- 手跑场景验证

## MS-1 —— 基础设施接入点（~1.5 天）

**目标**：能在 config 里声明 `protocol="openai-*"`，服务启动时 registry 分派到新 Channel 类；还不能真正服务请求。

- [ ] `src/channel/registry.py`：register_channel_factory + rebuild 分派（§7.12）
- [ ] `src/channel/base.py`：加 `protocol` 默认字段 + `build_upstream_request` 签名加 kwarg（§7.9）
- [ ] `src/channel/base.py::UpstreamRequest`：加 `translator_ctx` 字段（§8.8）
- [ ] `src/channel/api_channel.py`、`oauth_channel.py`：读 protocol + 吞 ingress kwarg（§7.10、§7.11）
- [ ] `src/openai/__init__.py` + `channel/api_channel.py` + `channel/registration.py`（§8.4、§8.5）
- [ ] `src/config.py`：加 `openai` 默认值（§7.1）
- [ ] `src/server.py`：lifespan 里调 `openai.channel.registration.register_factories()`（§7.13）
- [ ] TG 渠道菜单向导"协议"步骤（§7.14）

**验收**：
- `pytest` 全绿
- TG bot 添加 `protocol=openai-chat` 的渠道，`/health` 正常，`registry.all_channels()` 含新渠道
- 此时请求 `/v1/chat/completions` 会 404（路由未挂）

## MS-2 —— 同协议透传（~2 天）

**目标**：chat → openai-chat 上游、responses → openai-responses 上游，流式 + 非流式均跑通。**不做跨变体翻译**。

- [ ] `src/upstream.py`：新增 Chat/Responses SSE 工具类 + first-event parser（§7.5）
- [ ] `src/errors.py`：新增 openai 错误格式函数（§7.3）
- [ ] `src/auth.py`：`get_allowed_protocols`（§7.2）
- [ ] `src/scheduler.py`：ingress_protocol + family 过滤（§7.6）
- [ ] `src/failover.py`：toolkit 分派 + 错误格式分派（§7.7.1–7.7.3）
- [ ] `src/probe.py`：按 protocol 分派 probe payload（§7.8）
- [ ] `src/openai/transform/common.py`（字段白名单 + usage 抽取）
- [ ] `src/openai/transform/guard.py`（首版只判"同 ingress 自检"：chat ingress 里 n>1 / audio 等）
- [ ] `src/openai/handler.py`：透传路径 + 所有 auth/guard/log/scheduler/failover 流转
- [ ] `src/server.py`：/v1/chat/completions + /v1/responses 路由（§7.13）
- [ ] `src/server.py`：/v1/models 家族过滤（§7.13 改动 B）
- [ ] TG apikey 菜单"允许协议"按钮（§7.15）

**验收**：
- `curl /v1/chat/completions` → openai-chat 上游非流式、流式均通
- `curl /v1/responses` → openai-responses 上游非流式、流式均通
- 失败自动重试切候选
- 冷却 / 评分 / 亲和 按预期工作（用多渠道场景 smoke test）
- `/v1/models` 对只含 chat/responses 的 Key 返回 openai 家族模型，对 anthropic Key 返回 anthropic 家族

## MS-3 —— 请求非流式跨变体翻译（~2 天）

**目标**：chat → openai-responses 上游、responses → openai-chat 上游，**非流式**双向打通。**不含 previous_response_id**。

- [ ] `src/openai/transform/chat_to_responses.translate_request`
- [ ] `src/openai/transform/chat_to_responses.translate_response`（响应反向成 chat 格式）
- [ ] `src/openai/transform/responses_to_chat.translate_request`（先不接 store，prev_id 直接 400）
- [ ] `src/openai/transform/responses_to_chat.translate_response`
- [ ] `src/openai/transform/guard`：补满跨变体死角（built-in tools / background / prev_id 无 store 等）
- [ ] `OpenAIApiChannel.build_upstream_request`：按 (ingress, protocol) 分派到 translate_request
- [ ] `failover._consume_non_stream`：响应体翻译接入（在 `ch.restore_response` 之后，按 `translator_ctx.response_reverse` 走）

**验收**：
- 非流式 chat → responses 上游：支持文本 + function tool
- 非流式 responses → chat 上游：支持文本 + function tool
- guard 死角全部 400（无 502）
- anthropic 回归绿

## MS-4 —— SSE 流式跨变体翻译（~3 天）

**目标**：双向 SSE 状态机。

- [ ] `src/openai/transform/stream_c2r.py` 全量实现（chat SSE → responses SSE）
- [ ] `src/openai/transform/stream_r2c.py` 全量实现（responses SSE → chat SSE）
- [ ] `failover._consume_stream`：translator 接入（§8.7 伪码）
- [ ] 单元测试：
  - function tool 的 index/id 首次出现语义
  - `finish_reason="length"` 收尾 → responses `status=incomplete` + `incomplete_details.reason="max_output_tokens"`
  - 空 content 流（仅 tool_calls）
  - 中途错误事件（`response.failed` / chat error chunk）→ 对面格式的错误收尾

**验收**：
- 流式 chat→responses、responses→chat 双向通
- SSE 序列与官方参考客户端解析无异常
- anthropic 流式测试回归绿

## MS-5 —— previous_response_id Store（~1.5 天）

- [ ] `src/openai/store.py`（§5）
- [ ] `server.py::lifespan`：`openai_store.init()` + 挂 `cleanup_loop()`（§7.13 改动 C）
- [ ] `responses_to_chat.translate_request` 接 `_resolve_input` 的 Store 路径
- [ ] 响应落地后调 `store.save`（chat→responses 方向在 stream translator close 时 / 非流式在 translate_response 时）
- [ ] guard 里 `previous_response_id` 无 store 或未找到 → 400/404
- [ ] 测试：多轮对话链展开、过期清理、Key 隔离

**验收**：
- 下游客户端能用 `previous_response_id` 正确续接
- 链深度 >1 正确
- 过期自动清理

## MS-6 —— Reasoning Bridge（~1 天）

- [ ] `chat_to_responses`：`reasoning_content` → reasoning item（非流式 + 流式）
- [ ] `responses_to_chat`：reasoning summary → `reasoning_content`（非流式 + 流式）
- [ ] 配置开关 `openai.reasoningBridge`（`drop` 路径）
- [ ] 测试：DeepSeek-R1 上游回测

## MS-7 —— 亲和（fingerprint）接入（~1 天）

- [ ] `src/fingerprint.py`：openai 两套归一化（§7.4）
- [ ] `handler.py` 按 ingress 选 fp 函数
- [ ] `failover._consume_*` 成功完成后的 `fingerprint_write_*` 按 ingress 选
- [ ] 测试：同 Key + IP 连续 openai 请求粘同一渠道

## MS-8 —— 联调 + 打磨（~2 天）

- [ ] 真实 openai / deepseek / 类似兼容上游联调（两种 ingress × 两种上游 protocol = 4 组合）
- [ ] TG 渠道菜单的"协议"展示与"测试模型"按钮验证
- [ ] /v1/models 家族过滤实机验证
- [ ] 错误边界：超时、连接断开、上游 429、上游 invalid_request、跨 TTL 的 previous_response_id
- [ ] 日志详情页查看 openai 请求的重试链显示是否合理（如需调整 log_db 的字段呈现视情况）
- [ ] 文档：README.md 加"OpenAI 支持"一节链接到 docs/openai/

## 总工期

约 **14 个工作日**（专注）。包含 20% 的 buffer 用于真实上游联调的意外。

## 风险登记

| 风险 | 概率 | 缓解 |
|---|---|---|
| OpenAI SSE 事件 schema 在 GA 后还有小变动 | 中 | translator 按已观察事件名严格匹配；未知事件原样透传（同协议路径）或丢弃（跨协议路径） |
| reasoning_content 字段名在不同厂商有分歧（deepseek vs moonshot vs qwen）| 中 | 首版只处理 `reasoning_content`；其他字段在配置里支持别名映射（后续扩展） |
| function_call arguments 流式拼接时跨 chunk 的 JSON 不完整 | 低 | 与 chat/responses 两侧同构，上游本来就吐碎片，下游拼出来就是；翻译器只搬字节 |
| Store 链路在高并发下的 SQLite 写入瓶颈 | 低 | 已有 WAL + `_write_lock`；qps 足够；极端场景可按 key 分库后续优化 |
| 既有 anthropic 测试因"signature 扩展"产生偶发断言失败 | 低 | MS-1 之后 run full suite；如有就换"不动签名、加新函数"的 validate_ex 风格 |
