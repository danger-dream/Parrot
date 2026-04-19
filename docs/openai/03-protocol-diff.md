# 03 — OpenAI Chat vs Responses 协议对比

本章是互转逻辑的依据。实现层在 `src/openai/transform/`（见 [04-transform.md](./04-transform.md)）。

## 3.1 请求字段对照

| Chat (`/v1/chat/completions`) | Responses (`/v1/responses`) | 说明 |
|---|---|---|
| `model` | `model` | |
| `messages[]` | `input`（string 或 item[]） | 结构不同，见 3.3 |
| `system`/`developer` 首消息 | `instructions`（或 input 里 `role:"system"`） | |
| `max_tokens` / `max_completion_tokens` | `max_output_tokens` | |
| `response_format` | `text.format` | `{type:"text"|"json_object"|"json_schema",...}` 字段同构 |
| `reasoning_effort` | `reasoning.effort` | `"minimal"/"low"/"medium"/"high"` |
| — | `reasoning.summary` | `"auto"/"concise"/"detailed"/null` |
| `tools[]` 嵌 `{type:"function",function:{...}}` | `tools[]` 扁平 `{type:"function", name,...}` | |
| `tool_choice.function.name` | `tool_choice.name` | |
| `parallel_tool_calls` | `parallel_tool_calls` | |
| `stream` | `stream` | |
| `stream_options.include_usage` | 无（usage 总是出现在 `response.completed` 事件里） | |
| `stop` | — | responses 无 |
| `n` | — | responses 无 |
| `logprobs` / `top_logprobs` | — | |
| `prediction` | — | |
| `modalities` / `audio` | — | audio 输出 responses 当前不支持 |
| `store`（默认 false）| `store`（默认 true）| |
| `metadata` | `metadata` | |
| `user` / `service_tier` / `safety_identifier` / `prompt_cache_key` / `prompt_cache_retention` | 同名字段 | |
| `seed` | — | |
| `logit_bias` | — | |
| — | `previous_response_id` | 有状态，见 [05](./05-store.md) |
| — | `conversation` | `{id:"conv_..."}` 或 string |
| — | `include[]` | 如 `"reasoning.encrypted_content"`、`"file_search_call.results"` |
| — | `background` | 异步模式 |
| — | `truncation` | `"auto"/"disabled"` |
| — | `max_tool_calls` | |
| — | `prompt` | `{id,version,variables}` Prompt 对象 |

## 3.2 响应 / Usage 字段对照

| Chat | Responses | 说明 |
|---|---|---|
| `choices[0].message.content` | `output[]` 里 `type:"message"` item 的 `content[].output_text.text` | responses 便利字段 `output_text` = 所有拼接 |
| `choices[0].message.tool_calls[i].id` | `output[] type:"function_call"` 的 `call_id` | 注意：`function_call.id` 是 `fc_...`，`call_id` 才是客户端要引用的 |
| `choices[0].message.tool_calls[i].function.{name,arguments}` | `output[] type:"function_call"` 的 `{name, arguments}` | |
| `choices[0].message.refusal` | `output[] message.content[] type:"refusal"` 的 `refusal` | |
| `choices[0].finish_reason` | `status` + `incomplete_details.reason` | 映射见 3.6 |
| `usage.prompt_tokens` | `usage.input_tokens` | |
| `usage.completion_tokens` | `usage.output_tokens` | |
| `usage.total_tokens` | `usage.total_tokens` | 相同 |
| `usage.prompt_tokens_details.cached_tokens` | `usage.input_tokens_details.cached_tokens` | |
| `usage.completion_tokens_details.reasoning_tokens` | `usage.output_tokens_details.reasoning_tokens` | |
| `usage.completion_tokens_details.audio_tokens` / `accepted_prediction_tokens` | — | |

## 3.3 消息 / Input Item 结构

### Chat `messages[]`

```jsonc
// string content
{role:"user", content:"hi"}

// parts content（user / system 可）
{role:"user", content:[
  {type:"text", text:"..."},
  {type:"image_url", image_url:{url:"https://... or data:...", detail:"auto|low|high"}},
  {type:"input_audio", input_audio:{data:"<b64>", format:"wav|mp3"}},
  {type:"file", file:{file_id:"..." | file_data:"<b64>", filename}}
]}

// assistant
{role:"assistant", content:"..."|null, refusal:null|"...",
 tool_calls:[{id:"call_xxx", type:"function",
              function:{name:"...", arguments:"<JSON string>"}}]}

// 工具结果
{role:"tool", tool_call_id:"call_xxx", content:"..."|parts[]}
```

### Responses `input` 的 item 类型

```jsonc
// message（含 role=system/developer/user/assistant）
{type:"message", role:"user",
 content:[
   {type:"input_text", text:"..."},
   {type:"input_image", image_url:"...", detail:"auto|low|high", file_id?},
   {type:"input_file", file_id:"..." | file_data:"<b64>", filename?},
   {type:"input_audio", input_audio:{data,format}}         // 待确认
 ]}

// assistant 历史
{type:"message", role:"assistant",
 content:[{type:"output_text", text:"...", annotations:[]}]}

// 工具调用 / 结果
{type:"function_call", id:"fc_...", call_id:"call_...", name, arguments:"<JSON str>", status}
{type:"function_call_output", call_id:"call_...", output:"<string>"}

// 推理历史
{type:"reasoning", id:"rs_...", summary:[{type:"summary_text",text}],
 encrypted_content?, content?:[...]}

// 内置工具（built-in tools）历史
{type:"web_search_call", id, status, action}
{type:"file_search_call", id, status, queries, results}
{type:"computer_call" / "computer_call_output"}
{type:"image_generation_call", id, status, result}
{type:"code_interpreter_call", id, code, outputs, status}
{type:"local_shell_call" / "local_shell_call_output"}  // 待确认
{type:"mcp_call" / "mcp_list_tools" / "mcp_approval_request" / "mcp_approval_response"}

// 引用
{type:"item_reference", id:"..."}
```

## 3.4 Tools 定义

```jsonc
// Chat
[{"type":"function","function":{"name":"...", "description":"...", "parameters":{...}, "strict":true}}]

// Responses
[{"type":"function", "name":"...", "description":"...", "parameters":{...}, "strict":true}]
// 另加 built-in：
[{"type":"web_search_preview"}, {"type":"file_search", vector_store_ids:[...]},
 {"type":"computer_use_preview", ...}, {"type":"code_interpreter", ...},
 {"type":"image_generation", ...}, {"type":"mcp", server_label, server_url, ...},
 {"type":"local_shell"}]
```

## 3.5 SSE 事件对照

### Chat SSE

只有一种事件类型：`data: <chunk JSON>\n\n` 然后 `data: [DONE]\n\n`。Chunk 形状：

```jsonc
{
  id, object:"chat.completion.chunk", created, model,
  choices:[{
    index:0,
    delta:{
      role?:"assistant",            // 仅首 chunk
      content?:"片段",
      refusal?:"...",
      tool_calls?:[{
        index:0,                    // 累加键
        id?:"call_xxx",             // 仅首次
        type?:"function",
        function:{name?, arguments?}
      }]
    },
    finish_reason: null | "stop"|"length"|"tool_calls"|"content_filter"|"function_call"
  }],
  usage: null  // 或包含 usage 对象，仅最后 chunk（stream_options.include_usage=true）
}
```

### Responses SSE

`event: <name>\ndata: {...}\n\n`，每事件带 `type`（= event 名）和 `sequence_number`。

| 事件 | 关键 payload |
|---|---|
| `response.created` | `response:{id,status:"in_progress",...}` |
| `response.in_progress` | 同上，运行中快照 |
| `response.output_item.added` | `output_index`, `item` 的初态 |
| `response.output_item.done` | `output_index`, `item` 最终态 |
| `response.content_part.added` / `.done` | `item_id, output_index, content_index, part` |
| `response.output_text.delta` | `item_id, output_index, content_index, delta, logprobs?` |
| `response.output_text.done` | `item_id, output_index, content_index, text` |
| `response.output_text.annotation.added` | `item_id,...,annotation_index,annotation` |
| `response.refusal.delta` / `.done` | `delta` / `refusal` |
| `response.function_call_arguments.delta` | `item_id, output_index, delta(JSON 片段)` |
| `response.function_call_arguments.done` | `item_id, output_index, arguments` |
| `response.reasoning_summary_part.added` / `.done` | `item_id, summary_index, part` |
| `response.reasoning_summary_text.delta` / `.done` | `delta` / `text` |
| `response.reasoning_text.delta` / `.done` | 原文推理（部分模型） |
| `response.web_search_call.*` | `in_progress` / `searching` / `completed` |
| `response.file_search_call.*` | 同上 |
| `response.image_generation_call.*` | 含 `partial_image_b64` |
| `response.code_interpreter_call.*` | 含 `code.delta` / `code.done` |
| `response.completed` | 最终 `response`（含 usage） |
| `response.failed` | 最终 `response`（status=failed, error） |
| `response.incomplete` | 最终 `response`（incomplete_details） |
| `error` | 顶层错误 `{code, message, param}`（非 response 包裹） |

## 3.6 finish_reason / status 映射

| Chat finish_reason | Responses 状态 |
|---|---|
| `"stop"` | `status=="completed"`（无 function_call） |
| `"tool_calls"` | `status=="completed"` 且 output 含 function_call item |
| `"length"` | `status=="incomplete" && incomplete_details.reason=="max_output_tokens"` |
| `"content_filter"` | `status=="incomplete" && incomplete_details.reason=="content_filter"` 或含 refusal |
| `"function_call"`（legacy）| 映射同 `"tool_calls"` |

## 3.7 互转无损程度一览

### Chat → Responses

| 项 | 程度 | 备注 |
|---|---|---|
| messages / tools / tool_calls / tool_results | 无损 | |
| response_format / reasoning_effort / max_*_tokens | 无损 | |
| stream_options.include_usage | 无损（responses 默认带 usage） | |
| `n > 1` | **拒绝**（rejectOnMultiCandidate） | |
| `logprobs` / `top_logprobs` | 丢失（400 拒绝可选） | |
| `prediction` | 丢失 | |
| `stop` | 丢失（可 proxy 本地流式截断，首版不做） | |
| `modalities` / `audio` | 丢失（首版不支持 audio 输出） | |
| `seed` / `logit_bias` | 丢失 | |

### Responses → Chat

| 项 | 程度 | 备注 |
|---|---|---|
| message items / 标准 function tools | 无损 | |
| `instructions` | 无损（展开为 system 消息） | |
| `max_output_tokens` / `text.format` / `reasoning.effort` | 无损 | |
| `previous_response_id` / `conversation` | 有损但可支持（Store 展开，见 05） | |
| `reasoning` history item（`encrypted_content`） | 有损（encrypted 丢失，summary 可保留为 `reasoning_content` 字段） | |
| `tools` 含 built-in（web_search / file_search / computer_use / image_gen / code_interpreter / mcp / local_shell） | **拒绝**（rejectOnBuiltinTools） | |
| 历史 input 含 built-in call item | **拒绝** | |
| `background` | **拒绝** | |
| `include[]` 请求内置结果 | 仅 `"reasoning.encrypted_content"` 允许（丢弃）；built-in 相关一律拒绝 | |
| `truncation` / `max_tool_calls` | 丢失 | |
| `text.verbosity` | 丢失 | |

## 3.8 死角总结（CapabilityGuard 统一拦）

| 方向 | 触发条件 → 动作 |
|---|---|
| chat→responses | `n>1` / `audio` 输出 / （可选）`logprobs` → **400 invalid_request** |
| responses→chat | `tools` 含非 function 类型 → **400** |
| responses→chat | input 含 `web_search_call` / `file_search_call` / `computer_call` / `image_generation_call` / `code_interpreter_call` / `mcp_call` / `local_shell_call` → **400** |
| responses→chat | `background:true` → **400** |
| responses→chat | `previous_response_id` 在 Store 中不存在或已过期 → **404** |
| responses→chat | Store 功能被关闭（`openai.store.enabled=false`）但请求带 `previous_response_id` → **400** |
| 任一方向 | `openai.translation.enabled=false` 且 ingress != channel.protocol → 调度阶段候选为空 → **503 no available channels** |
