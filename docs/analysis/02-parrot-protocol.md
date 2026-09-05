# 02 · Parrot OpenAI 协议转换 + WebSocket + 图片链路分析

> 只读分析笔记。覆盖 chat ⇄ responses 双向翻译、上游请求字段白名单、SSE 事件全集、
> WebSocket `/v1/responses` 中继、CapabilityGuard 拒绝矩阵、reasoning/response 双 Store。
> 行号引用基于 2026-06-06 当前源码。

---

## 0. 总览：四条链路 + 两种 transport

Parrot 把「下游入口协议（ingress）」与「上游渠道协议（channel.protocol）」解耦，
`OpenAIApiChannel.build_upstream_request` 按 `(ingress, protocol)` 二维分派
（`channel/api_channel.py:79-115`）：

| ingress（下游） | upstream（上游 protocol） | 处理方式 | 代码入口 |
|---|---|---|---|
| chat | openai-chat | 同协议透传 | `_build_chat_passthrough` (api_channel.py:119) |
| responses | openai-responses | 同协议透传 | `_build_responses_passthrough` (api_channel.py:128) |
| chat | openai-responses | **chat→responses 翻译** | `_build_chat_to_responses` (api_channel.py:139) |
| responses | openai-chat | **responses→chat 翻译** | `_build_responses_to_chat` (api_channel.py:155) |

- 透传路径用 `common.filter_chat_passthrough` / `filter_responses_passthrough` 做白名单过滤。
- 跨变体路径调对应 `translate_request`，并在 `UpstreamRequest.translator_ctx` 里写
  `response_translator`（反向函数名）+ `model_for_response` 等，供 failover 在响应阶段做反向翻译。
- transport 两种：HTTP/SSE（failover 路径，调 `stream_c2r`/`stream_r2c`）与
  WebSocket（`responses_ws.py`，**仅 openai-responses↔openai-responses 透明中继**，不翻译）。

方向命名约定（贯穿全篇）：
- **chat_to_responses**：下游 chat `/v1/chat/completions` → 上游 `/v1/responses`
- **responses_to_chat**：下游 responses `/v1/responses` → 上游 `/v1/chat/completions`
- **stream_c2r**：上游 **chat SSE** → 下游 **responses SSE**（即 responses ingress 对 chat 上游）
- **stream_r2c**：上游 **responses SSE** → 下游 **chat SSE**（即 chat ingress 对 responses 上游）

> ⚠️ 注意 stream 文件名的 c2r/r2c 是按「上游→下游」方向命名的，跟请求方向 translate_request 相反，
> 容易看反：`stream_c2r` 服务于 `responses_to_chat` 这条 ingress 链路，`stream_r2c` 服务于
> `chat_to_responses` 这条 ingress 链路。

---

## 1. 请求字段映射表（chat ⇄ responses）

### 1.1 chat → responses（`chat_to_responses.translate_request`, 行 41-104）

| chat 字段 | responses 字段 | 处理 | 行号 |
|---|---|---|---|
| `model` | `model` | 直拷（必填） | 50 |
| `messages` | `input` | `_messages_to_input_items` 展开 | 51 |
| `stream` | `stream` | 透传 | 55 |
| `temperature` | `temperature` | 透传 | 55 |
| `top_p` | `top_p` | 透传 | 55 |
| `parallel_tool_calls` | `parallel_tool_calls` | 透传 | 55 |
| `user` | `user` | 透传 | 55 |
| `max_completion_tokens` | `max_output_tokens` | 改名（首选） | 63-64 |
| `max_tokens` | `max_output_tokens` | 改名（次选，旧字段） | 65-66 |
| `response_format` | `text.format` | 嵌套搬运（结构同构 text/json_object/json_schema） | 68-70 |
| `reasoning_effort` | `reasoning.effort` | 嵌套搬运 | 72-74 |
| `reasoning_summary`（非官方） | `reasoning.summary` | 嵌套搬运（DeepSeek 等生态，取值 auto/concise/detailed） | 75-78 |
| `verbosity` | `text.verbosity` | 嵌套搬运（enum low/medium/high） | 79-81 |
| `tools` | `tools` | `_flatten_tool` 逐个扁平化 | 83-84 |
| `tool_choice` | `tool_choice` | `_translate_tool_choice_c2r` | 86-88 |
| `metadata` | `metadata` | 透传 | 90-93 |
| `service_tier` | `service_tier` | 透传 | 90-93 |
| `safety_identifier` | `safety_identifier` | 透传 | 90-93 |
| `prompt_cache_key` | `prompt_cache_key` | 透传 | 90-93 |
| `prompt_cache_retention` | `prompt_cache_retention` | 透传 | 90-93 |
| `store` | `store` | 透传 | 90-93 |
| `stream_options.include_usage` | — | **丢弃**（responses usage 总在 response.completed） | 57 注释 |

**被丢弃（翻译层静默 drop，不报错）**：`n`/`stop`/`seed`/`logprobs`/`top_logprobs`/`logit_bias`/
`prediction`/`frequency_penalty`/`presence_penalty`/`modalities`/`audio`/`functions`/`function_call`/
`web_search_options` 等。这些字段在 responses 没对应概念，guard 只拦其中"拒绝更安全"的几类（见 §5）。

#### messages → input items（`_messages_to_input_items`, 行 107-194）

| chat message.role | responses input item | 关键点 | 行号 |
|---|---|---|---|
| `tool` | `function_call_output` {call_id, output} | call_id 取 `tool_call_id`，output 经 `_stringify_tool_content` 归一为 string | 113-120 |
| `function`（legacy） | `function_call_output` | call_id 用 `name` 充当（02-bug #15：旧代码会误落 user 分支→400） | 127-134 |
| `assistant` + `reasoning_content`（非官方） | `reasoning` item {summary:[summary_text]} | 仅 passthrough 模式映射；drop 模式不映射 | 137-149 |
| `assistant` + `content`(str) | `message` {output_text} | | 151-156 |
| `assistant` + `content`(list) | `message` {output_text}（拼接） | | 157-164 |
| `assistant` + `tool_calls[]` type=function | `function_call` {id:fc_*, call_id, name, arguments} | 合成稳定 fc_ 前缀 id | 184-192 |
| `assistant` + `tool_calls[]` type=custom | `custom_tool_call` {id:ctc_*, call_id, name, input} | 02-bug #27 | 172-182 |
| `assistant` + `refusal` | `message` {refusal} | | 193-198 |
| `system`/`developer`/`user` | `message` {role 原样保留} | 02-bug #22：不再强制 system→developer | 200-210 |

content parts 映射（`_content_chat_to_responses`, 行 213-263）：
`text→input_text`、`image_url→input_image`（带 detail/file_id）、`input_audio→input_audio`、
`file→input_file`（透传 file_id/file_data/filename/file_url/detail，02-bug #5），未知 part **丢弃**；
空 content 兜底补 `{input_text:""}`（防 responses 拒收）。

### 1.2 responses → chat（`responses_to_chat.translate_request`, 行 38-99）

| responses 字段 | chat 字段 | 处理 | 行号 |
|---|---|---|---|
| `model` | `model` | 直拷 | 56 |
| `input` | `messages` | `_resolve_input`→`_input_items_to_messages` | 53-54 |
| `instructions` | `messages[0]` system | **插入到最前**（任何历史之前） | 57-58 |
| `stream`/`temperature`/`top_p`/`parallel_tool_calls`/`user` | 同名 | 透传 | 60-62 |
| `max_output_tokens` | `max_completion_tokens` | 改名 | 64-65 |
| `text.format` | `response_format` | 反嵌套 | 67-70 |
| `reasoning.effort` | `reasoning_effort` | 反嵌套 | 72-75 |
| `reasoning.summary` | `reasoning_summary`（非官方） | 反嵌套（02-bug #12） | 76-79 |
| `text.verbosity` | `verbosity` | 反嵌套（02-bug #11） | 81-83 |
| `tools` | `tools` | `_nest_tool` 反扁平 | 85-88 |
| `tool_choice` | `tool_choice` | `_translate_tool_choice_r2c`；**tools=[] 时 strip 掉 parallel_tool_calls 避免上游 400** | 90-95 |
| `metadata`/`service_tier`/`safety_identifier`/`prompt_cache_key`/`prompt_cache_retention`/`store` | 同名 | 透传 | 97-99 |
| `previous_response_id` | —（展开成历史 messages） | `_resolve_input` 调 `store.expand_history` 沿 parent_id 链拼历史 | 102-148 |

input items → messages（`_input_items_to_messages`, 行 169-302）反向映射：
`message`（developer→system 折叠，行 195-196）、`function_call`→聚合到前一条 assistant 的 tool_calls、
`function_call_output`→`tool` message（output 经 `_flatten_function_call_output` 拍扁为 string，02-bug #3）、
`custom_tool_call`→assistant.tool_calls type=custom、`reasoning`→拼到下条 assistant 的 reasoning_content
（passthrough，含 Codex `encrypted_content` fallback，行 287-291）。所有 built-in call item（web_search_call 等）
防御性 skip（已被 guard 拦，行 293-301）。

**`previous_response_id` 展开异常**（`_resolve_input`, 行 110-148）映射为 GuardError：
Store 未开→400、NotFound→404、Expired→410、Forbidden→403。

---

## 2. 上游请求字段白名单（最终字段集合）

定义在 `transform/common.py`。**透传路径**（同协议）只拷这些键，过滤掉 proxy 内部字段（`_api_key_name` 等）
和上游不认的字段。跨变体路径由 translate_request 决定字段集（见 §1）。

### 2.1 `CHAT_REQ_ALLOWED`（common.py:26-43）— 发给上游 `/v1/chat/completions`

```
model, messages, stream, stream_options,
temperature, top_p, n,
max_completion_tokens, max_tokens, stop,
frequency_penalty, presence_penalty,
logprobs, top_logprobs, logit_bias,
tools, tool_choice, parallel_tool_calls,
functions, function_call,          # deprecated legacy，老 SDK 仍带
response_format, modalities, audio,
store, metadata, seed, prediction,
reasoning_effort, verbosity, web_search_options,
service_tier, user, safety_identifier,
prompt_cache_key, prompt_cache_retention
```
共 36 个。

### 2.2 `RESPONSES_REQ_ALLOWED`（common.py:46-61）— 发给上游 `/v1/responses`

```
model, input, stream, stream_options, instructions,
previous_response_id, conversation, context_management,
include, temperature, top_p, top_logprobs,
max_output_tokens, max_tool_calls,
tools, tool_choice, parallel_tool_calls,
text, reasoning, truncation,
store, metadata, prompt, background,
service_tier, user, safety_identifier,
prompt_cache_key, prompt_cache_retention,
client_metadata                    # Codex WS-only metadata（HTTP 透传一般不带）
```
共 32 个。

> 注意：透传白名单是「字段名级」的，**不递归校验内部结构**；body 里 `_` 前缀字段（如 `_api_key_name`）
> 因不在白名单内自然被滤除（也是 WS 日志 `log_body` 的过滤依据，responses_ws.py:340）。

---

## 3. SSE 流式事件全集

### 3.1 stream_c2r：chat SSE → responses SSE（`stream_c2r.py`）

上游只有一种 chat chunk（`data:{choices:[{delta}]}`），翻译器拆成细粒度 responses 事件。
**parrot 主动 emit 的 responses 事件类型（全集）**：

| 事件 | 触发 | 行号 |
|---|---|---|
| `response.created` | 首包前 | 304-308 |
| `response.in_progress` | 首包前（紧跟 created） | 309-313 |
| `response.output_item.added` | 打开 message / reasoning / function_call / custom_tool_call item | 322/377/491/591 |
| `response.content_part.added` | 打开 output_text / refusal part | 338/393 |
| `response.output_text.delta` | delta.content（带 `logprobs:[]`，02-bug #29） | 351 |
| `response.output_text.done` | 关 message text part | 367 |
| `response.refusal.delta` | delta.refusal | 388 |
| `response.refusal.done` | 关 refusal part | 411 |
| `response.content_part.done` | 关 text / refusal part | 376/420 |
| `response.reasoning_summary_part.added` | 打开 reasoning summary part | 462 |
| `response.reasoning_summary_text.delta` | delta.reasoning_content（仅 passthrough） | 471 |
| `response.reasoning_summary_text.done` | 关 reasoning | 489 |
| `response.reasoning_summary_part.done` | 关 reasoning | 497 |
| `response.function_call_arguments.delta` | tool_calls[i].function.arguments | 587 |
| `response.function_call_arguments.done` | 关 function_call（带 `name`，02-bug #17） | 616 |
| `response.custom_tool_call_input.delta` | type=custom 的 tool_call input | 681 |
| `response.custom_tool_call_input.done` | 关 custom_tool_call | 705 |
| `response.output_item.done` | 任一 item 关闭 | 387/445/506/625/715 |
| `response.completed` | 终态 status=completed | 779 |
| `response.incomplete` | 终态 status=incomplete（finish_reason=length/content_filter） | 781 |
| `response.failed` | 上游 error chunk / 流异常（error.code 经 `map_response_error_code` 合规化，02-bug #8） | 800 |

**输入侧识别的 chat delta**（`_handle_choice`, 行 234-287）：`reasoning_content`（优先，drop 模式丢弃）、
`content`、`refusal`、`tool_calls[]`（按 index）、`function_call`（legacy，等价 tool_calls[0]，02-bug #18）、
`finish_reason`（function_call 等价 tool_calls）、`usage`。

状态机要点：text-ish item（message/reasoning）切换前先 close（`_switch_text_kind`, 行 290-305）；
sequence_number 全局自增；`_response_skeleton` 用 `build_response_skeleton` 造 spec 14 必填字段
（02-bug #13，common.py:227-264）。空流/立即 [DONE] 也保证 `created→in_progress→...` 合法序列（close, 行 169-198）。

### 3.2 stream_r2c：responses SSE → chat SSE（`stream_r2c.py`）

上游发细粒度 `event:/data:` 帧，翻译器还原成 chat chunk。dispatch 在 `_handle_event_block`（行 196-251），
各 handler 方法见下。**parrot 认识（会处理）的上游 response.* 事件**：

| 上游事件 | 转成 chat | handler 行号 |
|---|---|---|
| `response.output_item.added` | message→新 role chunk 分段（02-bug #33）；function_call→记录 output_index→tc_index 映射 + 发首个 tool_calls chunk | `_on_output_item_added` 237 |
| `response.output_text.delta` | `delta.content` | `_on_output_text_delta` 272 |
| `response.refusal.delta` | `delta.refusal` | `_on_refusal_delta` 280 |
| `response.reasoning_summary_text.delta` | `delta.reasoning_content`（非官方，drop 模式丢弃） | `_on_reasoning_delta` 288 |
| `response.reasoning_text.delta` | `delta.reasoning_content`（同上，与上一行共用 handler） | 288 |
| `response.function_call_arguments.delta` | `delta.tool_calls[{index, function.arguments}]` | `_on_fc_args_delta` 300 |
| `response.output_text.annotation.added` | 累积到 state.annotations（chat SSE 无增量事件，**不 yield**；close 前汇总到 message.annotations，02-bug #35） | `_on_annotation_added` 318 |
| `response.completed` | 记终态 + usage + finish_reason（output 含 function_call→tool_calls） | `_on_completed` 330 |
| `response.incomplete` | 记终态（reason=max_output_tokens→length / content_filter） | `_on_incomplete` 339 |
| `response.failed` | 立即 emit error 帧 + [DONE]，锁 terminal_emitted | `_on_error` 383 |
| `error` | 同上（裸 error payload，与 failed 共用 `_on_error`） | 383 |

**主动忽略**（行 248-251 注释明列）：`response.created`、`response.in_progress`、`output_item.done`、
`content_part.added/done`、`output_text.done`、`reasoning_summary_part.*`、`reasoning_summary_text.done`、
`function_call_arguments.done`、`web_search_call.*` 等——对 chat 下游无用。

chat chunk 序列：首个 delta 前发 role chunk（`delta.role="assistant"`，行 298-302）；
正常收尾发空 delta + finish_reason chunk，可选 usage chunk（include_usage 时每帧带 usage：
中间 null、末帧真值，02-bug #43），最后 `[DONE]`（close, 行 175-216）。
**02-bug #20**：一旦观察到终态，后续事件直接短路（行 242-244），防 close 后被 feed 注入改写。
`get_downstream_chat_assistant`（行 412-438）给 failover 的 fingerprint 亲和写入用。

---

## 4. WebSocket `/v1/responses` 与 Codex 身份生命周期

### 4.1 Downstream frame 与 native carrier

首帧及后续帧都必须是 `response.create`。`request_body_from_ws_create` 会从普通业务 body 移除 `client_metadata`，但 `responses_ws.py` 在移除前把现有 `client_metadata` 和既有 session/turn-metadata headers 保存到内部 `_codex_native_identity`。该对象只供身份 resolver 做哈希 lookup，不进入 provider allowlist，也不会原样发上游。

若 carrier 有稳定 `session_id`，HTTP 与 WS 会命中同一 owner+principal+anchor logical thread；若有明确 `turn_id`，同一 downstream turn 的跨 HTTP/WS continuation 会命中同一 upstream UUIDv7。后续新 downstream turn ID 得到新 UUIDv7；未提供 explicit turn 的顺序 create 按普通新 turn 处理。

### 4.2 Snapshot 投影与串行边界

最终 OAuth account 确定后，`OpenAIOAuthChannel` 解析 `RequestIdentityContext`。生产 failover/WS 路径设置内部 serialization 标记，使 channel 在构造 wire snapshot 前进入 `(owner_digest, thread_id)` async queue，并在获得 lease 后刷新 durable window。

HTTP headers 与 WS handshake/frame 都由同一个 `RequestIdentitySnapshot` 投影：

- `session-id` / `thread-id` / `x-client-request-id`；
- `x-codex-window-id={thread_id}:{window_number}`；
- canonical `x-codex-turn-metadata`；
- `client_metadata` 中的 installation/session/thread/turn/window metadata；
- 仅同 owner+turn 已捕获时才投影 `x-codex-turn-state`。

下游传入的 installation/session/thread/turn/window/turn-state 会被移除或覆盖。raw downstream ID 不上游；`ProtocolIdentityMap` 只在响应协议字段中还原，不做全文字符串替换。

同一 owner/thread 的 turn 不会并发；不同 thread 可并发。HTTP streaming iterator、WS 每个 active response、错误及取消路径都负责释放 lease，空 queue 立即回收。持久 WS 在 turn terminal 后先释放，再等待下一 `response.create`。

### 4.3 Event-level turn-state

除 HTTP/SSE response headers 和 WS upgrade headers 外，两个上游 WS consumer 都把每个 text frame 交给 `capture_turn_state_event`。只接受官方 `type="response.metadata"` 的 `headers.x-codex-turn-state`（header 名大小写不敏感），并用当前 translator snapshot 校验 owner+turn。

因此同 explicit turn 的后续 `response.create`/retry 会重放 token；新 turn 的 `TurnContext` 为空；切换 OAuth account 会解析目标 owner 独立 context。下游 token 永不受信任。

### 4.4 Commit 与 failover

`response.created` 虽不是 assistant 可见输出，却证明上游已实例化请求，是 dispatch commit。之后出现错误不得切换 OAuth account 重放。`response.output_text.delta`、tool call/delta 等真正可见事件同样使 attempt 不可逆。只有 commit 前的连接/可重试错误可以按现有策略 retry/failover。

WS tracker 继续旁路收集 response、usage 和终态；身份 mapper 在发送下游前做字段级映射。`response.completed`/`failed`/`incomplete` 结束当前 active response，并结算日志、capacity 和 thread lease。

### 4.5 Compaction window

已有 Responses 成功终态若返回完整 compaction item，会调用 `compaction_owner.persist_observed_safe(ch, request, response)`。consumer 只把 response 输出中的 compaction 视为确认；请求 history 中已有 item 不触发推进。owner/session/model 验证通过后，compaction 确认标记与 logical window CAS 在同一 durable transaction 中从 `n` 推进到 `n+1` 并生成新 UUIDv7 context-window。重复/并发 finalization 幂等。

当前仓库没有 `/responses/compact` route/transport，本阶段不虚构该 endpoint；未来 transport 必须复用上述确认事务。

## 5. CapabilityGuard 拒绝矩阵（`transform/guard.py`）

`GuardError(status, err_type, message, param)` 由 handler 映射成对应 HTTP/WS 状态。

### 5.1 Chat ingress 自检（`guard_chat_ingress`, 行 50-75）
- 非 JSON object → 400
- 缺 `model` → 400（02-bug #2，防 KeyError 500）
- `n>1` → 400（proxy 不聚合多候选）
- `modalities` 含 `audio` → 400（不支持 audio 输出）

### 5.2 Responses ingress 自检（`guard_responses_ingress`, 行 80-145）
- 非 JSON / 缺 model → 400
- `background`（无论 true/false）→ **静默剥除**（无状态代理，Codex 上游连 false 都 400；行 118-130）
- `conversation`（非空）→ 400（首版不支持，仅 previous_response_id）
- `previous_response_id` 但 Store 关闭 → 400

### 5.3 chat → responses 跨变体（`guard_chat_to_responses`, 行 158-217）
- `prediction` → **不阻断**，只 log warning（responses 无对应，会被 drop）
- `n>1` → 400
- `logprobs` / `top_logprobs`(int) → 400（responses 不支持，拒绝以免沉默降级）
- message.content 含 `input_audio` part → 400（responses ResponseInputContent 只支持 text/image/file）

### 5.4 responses → chat 跨变体（`guard_responses_to_chat`, 行 261-372）
- `tools` 含 built-in 类型 → 400（chat 上游无等价）。`_BUILTIN_TOOL_TYPES`（行 222-232）：
  `web_search_preview`、`file_search`、`computer_use_preview`、`code_interpreter`、`image_generation`、
  `mcp`、`local_shell`、`web_search`、`web_search_2025_08_26`、`web_search_preview_2025_03_11`、
  `computer`、`computer_use`、`apply_patch`、`function_shell`。**未知 type 也兜底 400**（行 322-324）。
  function/custom 放行（custom 由 translate 转换）。
- `tool_choice` 为 hosted/MCP 形态 → 400（`_NON_CHAT_TOOL_CHOICE_TYPES`, 行 236-244）
- `input` 含 built-in call item → 400（`_BUILTIN_INPUT_ITEM_TYPES`, 行 249-254：web_search_call、
  file_search_call、computer_call、image_generation_call、code_interpreter_call、mcp_call、
  mcp_list_tools、mcp_approval_request、mcp_approval_response、local_shell_call、local_shell_call_output）
- `input` 含 `item_reference` → 400（需 server-side store）
- `previous_response_id` 但 Store 关闭 → 400
- `conversation`（非空）→ 400
- `include` 含 `reasoning.encrypted_content` → **静默从 include 剥除**（chat 上游无加密概念，不阻断；行 358-372）

> 关键差异：`background` / `include:reasoning.encrypted_content` 是**静默剥除**（兼容），
> 其余 built-in 能力是**硬 400 拒绝**。

---

## 6. 双 Store：response store 与 reasoning store

两套独立 Store，解决**不同问题**，都挂 SQLite（WAL）。

### 6.1 `store.py` — previous_response_id 历史展开

- **解决什么**：Responses API 有状态（客户端用 `previous_response_id` 续接），但 chat 上游无状态。
  responses→chat 翻译时必须本地展开历史、翻成 chat messages 发上游。
- **表**：`openai_response_store`（挂 state.db，行 119-129）。
- **key**：`response_id`（PRIMARY KEY）；附 `parent_id` 形成链。
- **value**：`input_items`（本次请求输入）+ `output_items`（本次响应）+ api_key_name/model/channel_key/expires_at。
- **写入时机**：responses ingress + chat 上游 + 有 api_key_name 时，非流式 `translate_response`（responses_to_chat.py:380-414）
  / 流式 `StreamTranslator._save_to_store_if_configured`（stream_c2r.py:828-865）/ WS `_write_responses_affinity`。
  **错误路径也写**（02-bug #41，stream_c2r.py:184-190），失败走 throttled 告警（下次续接会 404）。
- **展开**：`expand_history`（行 203-224）沿 parent_id 向上递归（max_depth=50，防环），
  老→新拼 `input_items+output_items`。
- **隔离**：`lookup` 校验 api_key_name 一致（不一致 ResponseForbidden），防 Key 间碰撞。
- **TTL**：默认 60min（`ttlMinutes`），`cleanup_loop` 周期清理。
- **save 字段对齐坑**：`save(response_id, parent_id, *, api_key_name, model, channel_key, input_items, output_items)`
  ——注意 WS 路径 `_write_responses_affinity` 调用是位置参数 `(prev_id, body.previous_response_id, ...)`（responses_ws.py:1090-1099）。

### 6.2 `reasoning_replay.py` — account-scoped encrypted reasoning replay

Codex `store=false` 下的 encrypted reasoning replay 使用由最终 OAuth owner、durable logical session 和 model 组成的 scope。成功终态缓存合法 reasoning/tool-call items；后续请求只在同 scope 内回填。invalid encrypted content 的同账号恢复可清该 scope；跨 OAuth failover 使用目标账号自己的 scope，并按既有 portable-body 规则清除 foreign encrypted content。compaction item 是不可分割 owner state，不参与跨账号 EC strip retry。

这套 replay 与 turn-state 不同：replay 是 logical-session/model 范围的加密内容连续性；turn-state 是严格 account+turn 范围的 sticky routing token。两者都不依赖或污染 raw downstream 身份。

## 7. 图片链路（`images_simple.py` + `images_openai_compat.py`）

不是协议翻译，但同属 OpenAI 家族。两个入口、同一管线。

### 7.1 两套入口
- **Parrot 私有**（`images_simple.py`）：`POST /v1/images/generate`、`/v1/images/edit`，
  简化 schema {prompt, size?, image?}。
- **OpenAI 兼容**（`images_openai_compat.py`）：`POST /v1/images/generations`、`/v1/images/edits`，
  完整解析 OpenAI 标准字段（JSON + multipart）。`n>1` 显式降为 1 并加 `parrot_warning`（Codex 一次一张）；
  `response_format=url` 因 OAuth 拿不到真实 CDN URL，回填 data URL。

### 7.2 共用管线（`_execute_pipeline`, images_simple.py:498-640）
- **上游**：ChatGPT 后端 `https://chatgpt.com/backend-api/codex/responses`（Codex Responses + `image_generation` tool）。
- **payload**（`_build_payload`, 行 153-205）：`tools=[{type:image_generation, action, model, size?, ...}]`、
  `tool_choice={type:image_generation}`、`stream=True`、`store=False`、`include=[reasoning.encrypted_content]`、
  `reasoning={effort:medium, summary:auto}`。native 字段透传：size/quality/background/output_format/moderation/style
  + output_compression/partial_images（int）+ input_image_mask。
- **账号**：遍历 OpenAI OAuth 账号（`_candidate_accounts`），failover + 独立冷却（`_IMAGE_COOLDOWNS`，
  不影响普通 API）；401 触发 force_refresh 重试一次。
- **SSE 解析**（`_iter_sse_events` + `_extract_images`, 行 311-388）：抓 `image_generation_call.result`（b64），
  `response.completed` 时 output 为空则用 `_patch_completed` 回填 by_index 累积项。
- **错误分类**（`_classify_error`, 行 234-290）：policy/moderation→400 user_visible、401→auth+refresh、
  403/404→permission+cooldown、429→rate_limit+cooldown+retry_after、5xx→retryable。
- **缓存**（可选，`_save_cached_images` + `_cleanup_cache`）：按天目录存 b64，retention/maxBytes 双策略 LRU 清理。
- **配额**：每次响应 header 经 `_update_codex_quota` 落 codex quota snapshot。

---

## 8. 关键发现速记

1. **上游请求字段白名单是两套 frozenset**（common.py）：CHAT_REQ_ALLOWED(36 键) / RESPONSES_REQ_ALLOWED(32 键)，
   **仅对同协议透传生效**；跨变体由 translate_request 重建字段。`_` 前缀内部字段靠不在白名单自然滤除。
2. **SSE 事件全集**：stream_c2r emit **19 种 literal 事件 + 终态 completed/incomplete/failed**（共约 22 类：
   created/in_progress/output_item.added/content_part.added·done/output_text.delta·done/refusal.delta·done/
   reasoning_summary_part.added·done/reasoning_summary_text.delta·done/function_call_arguments.delta·done/
   custom_tool_call_input.delta·done/output_item.done + 终态三选一）；stream_r2c 只**消费 11 种**上游事件
   （output_item.added/output_text.delta/refusal.delta/reasoning_summary_text.delta/reasoning_text.delta/
   function_call_arguments.delta/output_text.annotation.added/completed/incomplete/failed/error），其余明确忽略。
3. **WS 使用统一 identity snapshot**：OAuth 请求的 session/thread/turn/window 均由应用层 context 投影；`_WsTracker` 旁路解析 usage/output，`response.created` 或首个可见 output/tool event 后禁止跨账号 failover。
4. **Guard 双策略**：built-in tools/tool_choice/input-call-item/logprobs/input_audio/n>1 是硬 400；
   background 和 include:reasoning.encrypted_content 是静默剥除（兼容优先）。
5. **双 Store 解决两件不同事**：`store.py`（response store, key=response_id+parent_id 链）让无状态 chat 上游
   能续接 previous_response_id；`reasoning_store.py`（key=model+session_key，二级缓存）让 store=false 的 Codex
   多轮工具链不丢加密 reasoning——**不依赖下游回带**，自己缓存+回填。
6. **02-bug-findings 修复密集**：源码注释里有 40+ 处编号 bug 修复（字段双向透传 file_url/file_id/annotations、
   legacy function/function_call、custom tool、content_index 顺序、usage details 必写、ResponseError.code enum 映射、
   response skeleton 14 必填字段等），是协议严格性的核心增量。
```
