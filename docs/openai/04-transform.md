# 04 — 请求 / SSE 转换器

实现目录：`src/openai/transform/`。

```
src/openai/transform/
├── __init__.py
├── common.py                # 字段白名单、usage 抽取、错误抽取、SSE 帧工具
├── guard.py                 # CapabilityGuard（拒绝死角特性）
├── chat_to_responses.py     # 请求：chat body → responses body
├── responses_to_chat.py     # 请求：responses body → chat body（含 previous_response_id 展开）
├── stream_c2r.py            # SSE 状态机：上游 chat SSE → 下游 responses SSE
└── stream_r2c.py            # SSE 状态机：上游 responses SSE → 下游 chat SSE
```

所有函数为纯函数，无 I/O；Store 读写发生在 `responses_to_chat.translate_request` 的内部但通过 `store.lookup(prev_id)` 接口调用，不与转换逻辑混在一起。

## 4.1 `common.py` 关键接口

```python
CHAT_REQ_ALLOWED   = {"model","messages","stream","stream_options","temperature","top_p","n",
                      "max_completion_tokens","max_tokens","stop","frequency_penalty",
                      "presence_penalty","logprobs","top_logprobs","logit_bias",
                      "tools","tool_choice","parallel_tool_calls","response_format",
                      "modalities","audio","store","metadata","seed","prediction",
                      "reasoning_effort","verbosity","web_search_options","service_tier",
                      "user","safety_identifier","prompt_cache_key","prompt_cache_retention"}

RESPONSES_REQ_ALLOWED = {"model","input","stream","stream_options","instructions",
                         "previous_response_id","conversation","context_management",
                         "include","temperature","top_p","top_logprobs","max_output_tokens",
                         "max_tool_calls","tools","tool_choice","parallel_tool_calls",
                         "text","reasoning","truncation","store","metadata","prompt",
                         "background","service_tier","user","safety_identifier",
                         "prompt_cache_key","prompt_cache_retention"}

def filter_chat_passthrough(body: dict) -> dict
def filter_responses_passthrough(body: dict) -> dict

def extract_usage_chat(obj: dict) -> dict        # 抽取到统一结构
def extract_usage_responses(obj: dict) -> dict

# 统一 usage 结构（与现有 anthropic 无关，openai 独立语义）
# {"input_tokens","output_tokens","reasoning_tokens","cache_read_tokens","total_tokens"}

def sse_frame_chat(obj: dict) -> bytes            # 构造 "data: {json}\n\n"
def sse_frame_responses(event: str, obj: dict) -> bytes  # "event: x\ndata: ..."
def sse_done_chat() -> bytes                       # "data: [DONE]\n\n"
```

## 4.2 `guard.py` 关键接口

```python
class GuardError(Exception):
    """带 status + err_type + message 属性，供入口层直接映射成 400/403/...。"""
    def __init__(self, status: int, err_type: str, message: str): ...

def guard_chat_to_responses(body: dict) -> None:
    # n>1 / logprobs / prediction / audio 输出 modalities → 400
    ...

def guard_responses_to_chat(body: dict, *, store_enabled: bool) -> None:
    # tools 含非 function 类型 → 400
    # input 含 built-in call items → 400
    # background:true → 400
    # previous_response_id && not store_enabled → 400
    ...
```

## 4.3 `chat_to_responses.py`

```python
def translate_request(body: dict) -> dict:
    """chat body → responses body；调用方已确保通过 guard。"""
    payload = {
        "model": body["model"],
        "input": _messages_to_input_items(body.get("messages", [])),
    }
    if "stream" in body: payload["stream"] = body["stream"]
    if "temperature" in body: payload["temperature"] = body["temperature"]
    if "top_p" in body: payload["top_p"] = body["top_p"]
    if "parallel_tool_calls" in body: payload["parallel_tool_calls"] = body["parallel_tool_calls"]
    if "user" in body: payload["user"] = body["user"]

    # max_tokens 映射
    if "max_completion_tokens" in body:
        payload["max_output_tokens"] = body["max_completion_tokens"]
    elif "max_tokens" in body:
        payload["max_output_tokens"] = body["max_tokens"]

    # response_format → text.format
    if "response_format" in body:
        payload.setdefault("text", {})["format"] = body["response_format"]

    # reasoning_effort → reasoning.effort
    if "reasoning_effort" in body:
        payload.setdefault("reasoning", {})["effort"] = body["reasoning_effort"]

    # tools 扁平化
    if body.get("tools"):
        payload["tools"] = [_flatten_tool(t) for t in body["tools"]]

    # tool_choice
    if "tool_choice" in body:
        payload["tool_choice"] = _translate_tool_choice_c2r(body["tool_choice"])

    # 透传兼容
    for k in ("metadata","service_tier","safety_identifier",
              "prompt_cache_key","prompt_cache_retention","store"):
        if k in body: payload[k] = body[k]

    return payload

# --- 工具 ---

def _messages_to_input_items(messages: list) -> list:
    """展开 messages 为 input items。"""
    items = []
    for msg in messages:
        role = msg.get("role")
        if role == "system":
            role = "developer"     # Responses 推荐用 developer 代替 system（可配置）
        if role == "tool":
            items.append({
                "type": "function_call_output",
                "call_id": msg["tool_call_id"],
                "output": _stringify_tool_content(msg.get("content")),
            })
            continue
        if role == "assistant":
            # 可能同时有 content 和 tool_calls
            c = msg.get("content")
            if c:
                items.append({
                    "type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": _stringify(c), "annotations": []}],
                })
            for tc in msg.get("tool_calls") or []:
                items.append({
                    "type": "function_call",
                    "id": f"fc_{tc['id']}",   # 自造 fc_ 前缀占位
                    "call_id": tc["id"],
                    "name": tc["function"]["name"],
                    "arguments": tc["function"]["arguments"],
                    "status": "completed",
                })
            if msg.get("refusal"):
                items.append({
                    "type": "message", "role": "assistant",
                    "content": [{"type": "refusal", "refusal": msg["refusal"]}],
                })
            continue
        # user / system(developer)：content 转成 input_* 结构
        items.append({
            "type": "message", "role": role,
            "content": _content_chat_to_responses(msg.get("content", "")),
        })
    return items

def _content_chat_to_responses(content) -> list:
    if isinstance(content, str):
        return [{"type": "input_text", "text": content}]
    out = []
    for p in content:
        t = p.get("type")
        if t == "text":
            out.append({"type": "input_text", "text": p["text"]})
        elif t == "image_url":
            iu = p["image_url"]
            out.append({
                "type": "input_image",
                "image_url": iu["url"] if isinstance(iu, dict) else iu,
                "detail": (iu.get("detail") if isinstance(iu, dict) else None) or "auto",
            })
        elif t == "input_audio":
            out.append({"type": "input_audio", "input_audio": p["input_audio"]})
        elif t == "file":
            out.append({"type": "input_file", **p["file"]})
        else:
            # 未知 part 类型原样丢掉（或记日志）
            pass
    return out

def _flatten_tool(t: dict) -> dict:
    if t.get("type") == "function":
        fn = t["function"]
        return {
            "type": "function",
            "name": fn["name"],
            "description": fn.get("description"),
            "parameters": fn.get("parameters"),
            "strict": fn.get("strict"),
        }
    return dict(t)  # 未知 type 原样（但 guard 已拦非 function）

def _translate_tool_choice_c2r(tc):
    if isinstance(tc, str): return tc   # "auto"/"none"/"required"
    if tc.get("type") == "function":
        return {"type": "function", "name": tc["function"]["name"]}
    return tc
```

### 响应反向（非流式）：收到 responses JSON → 回 chat JSON

```python
def translate_response(resp: dict, model: str) -> dict:
    """把上游 responses 的非流式 JSON 反向成 chat 风格返回给下游。"""
    # 拼 assistant message
    content_text = resp.get("output_text") or _gather_output_text(resp.get("output") or [])
    tool_calls = _gather_function_calls(resp.get("output") or [])
    refusal = _gather_refusal(resp.get("output") or [])
    reasoning_content = _gather_reasoning_summary(resp.get("output") or [])  # 见 06

    message = {"role": "assistant", "content": content_text or None}
    if tool_calls: message["tool_calls"] = tool_calls
    if refusal:    message["refusal"] = refusal
    if reasoning_content is not None:
        message["reasoning_content"] = reasoning_content   # 非官方字段（见 06）

    finish_reason = _status_to_finish_reason(resp)

    return {
        "id": f"chatcmpl-{resp['id'].replace('resp_','')}",
        "object": "chat.completion",
        "created": resp.get("created_at") or int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason, "logprobs": None}],
        "usage": _usage_resps_to_chat(resp.get("usage") or {}),
    }
```

## 4.4 `responses_to_chat.py`

```python
def translate_request(body: dict, *, store=None, api_key_name: str = "") -> dict:
    """
    responses body → chat body。
    若带 previous_response_id：调 store.expand_history(prev_id) 把历史 items 展开拼回去。
    """
    input_items = _resolve_input(body, store=store, api_key_name=api_key_name)
    messages = _input_items_to_messages(input_items)

    # instructions → 首条 system
    if body.get("instructions"):
        messages.insert(0, {"role": "system", "content": body["instructions"]})

    payload = {"model": body["model"], "messages": messages}
    if "stream" in body: payload["stream"] = body["stream"]
    if "temperature" in body: payload["temperature"] = body["temperature"]
    if "top_p" in body: payload["top_p"] = body["top_p"]
    if "parallel_tool_calls" in body: payload["parallel_tool_calls"] = body["parallel_tool_calls"]
    if "user" in body: payload["user"] = body["user"]

    if "max_output_tokens" in body:
        payload["max_completion_tokens"] = body["max_output_tokens"]

    if (text := body.get("text")) and (fmt := text.get("format")):
        payload["response_format"] = fmt

    if (reasoning := body.get("reasoning")) and (eff := reasoning.get("effort")):
        payload["reasoning_effort"] = eff

    if body.get("tools"):
        payload["tools"] = [_nest_tool(t) for t in body["tools"]]

    if "tool_choice" in body:
        payload["tool_choice"] = _translate_tool_choice_r2c(body["tool_choice"])

    for k in ("metadata","service_tier","safety_identifier",
              "prompt_cache_key","prompt_cache_retention","store"):
        if k in body: payload[k] = body[k]

    return payload

def _resolve_input(body, *, store, api_key_name) -> list:
    """把 body.input + previous_response_id + conversation 解析为完整 items 列表。"""
    prev_id = body.get("previous_response_id")
    history: list = []
    if prev_id:
        rec = store.lookup(prev_id, api_key_name=api_key_name)   # 抛 NotFoundError
        history = rec.input_items + rec.output_items
    # conversation 资源暂不实现（首版拒绝，见 guard）；后续可在 store 扩展表

    cur = body.get("input")
    if isinstance(cur, str):
        cur_items = [{"type":"message","role":"user",
                      "content":[{"type":"input_text","text":cur}]}]
    else:
        cur_items = list(cur or [])

    return history + cur_items

def _input_items_to_messages(items: list) -> list:
    """倒推 messages；function_call 合并到前一条 assistant 的 tool_calls。"""
    messages: list = []
    pending_assistant: dict | None = None

    def _flush():
        nonlocal pending_assistant
        if pending_assistant is not None:
            messages.append(pending_assistant)
            pending_assistant = None

    for item in items:
        t = item.get("type")
        if t == "message":
            _flush()
            role = item.get("role") or "user"
            if role == "developer":
                role = "system"
            messages.append({"role": role,
                             "content": _content_responses_to_chat(item.get("content") or [])})
        elif t == "function_call":
            if pending_assistant is None:
                pending_assistant = {"role":"assistant","content":None,"tool_calls":[]}
            pending_assistant.setdefault("tool_calls", []).append({
                "id": item["call_id"], "type": "function",
                "function": {"name": item["name"], "arguments": item["arguments"]},
            })
        elif t == "function_call_output":
            _flush()
            messages.append({"role":"tool", "tool_call_id": item["call_id"],
                             "content": item.get("output","")})
        elif t == "reasoning":
            # drop 或 映射（见 06）
            pass
        elif t in ("web_search_call","file_search_call","computer_call",
                   "image_generation_call","code_interpreter_call",
                   "mcp_call","mcp_list_tools","mcp_approval_request",
                   "mcp_approval_response","local_shell_call","local_shell_call_output"):
            # guard 已拦，但防御性 skip
            pass
        elif t == "item_reference":
            # 首版拒绝（guard 里处理），或扩展 store 支持
            pass
    _flush()
    return messages

def _content_responses_to_chat(content) -> list | str:
    out = []
    for p in content or []:
        pt = p.get("type")
        if pt == "input_text":
            out.append({"type":"text","text":p.get("text","")})
        elif pt == "output_text":
            out.append({"type":"text","text":p.get("text","")})
        elif pt == "input_image":
            out.append({"type":"image_url",
                        "image_url":{"url":p.get("image_url",""),
                                     "detail":p.get("detail","auto")}})
        elif pt == "input_file":
            out.append({"type":"file","file":{k:p[k] for k in ("file_id","file_data","filename") if k in p}})
        elif pt == "input_audio":
            out.append({"type":"input_audio","input_audio":p.get("input_audio") or {}})
        elif pt == "refusal":
            out.append({"type":"text","text":""})   # chat 里没有 refusal part，放空字符串；refusal 字段由调用方单独处理
    # 若只有一条 text，为了兼容老客户端可以直接返回字符串
    if len(out) == 1 and out[0].get("type") == "text":
        return out[0]["text"]
    return out

def _nest_tool(t: dict) -> dict:
    if t.get("type") == "function":
        return {"type":"function","function":{k:t.get(k) for k in ("name","description","parameters","strict") if k in t}}
    return dict(t)

def _translate_tool_choice_r2c(tc):
    if isinstance(tc, str): return tc
    if tc.get("type") == "function":
        return {"type":"function","function":{"name":tc["name"]}}
    return tc
```

### 响应反向（非流式）：收到 chat JSON → 回 responses JSON

```python
def translate_response(chat: dict, model: str, *, store=None, api_key_name: str = "",
                       previous_response_id: str | None = None,
                       translated_input_items: list | None = None) -> dict:
    """
    把上游 chat 的非流式 JSON 反向成 responses 风格。
    同时写 Store：resp_id → (展开后的 input_items, 本次 output_items)。
    """
    choice = (chat.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    finish_reason = choice.get("finish_reason")

    output_items = []

    # reasoning_content（非官方字段）→ reasoning item（见 06）
    if msg.get("reasoning_content"):
        output_items.append(_reasoning_item_from_text(msg["reasoning_content"]))

    # content text → message item
    if msg.get("content"):
        output_items.append({
            "type":"message","id":_gen_id("msg_"),"role":"assistant","status":"completed",
            "content":[{"type":"output_text","text":msg["content"],"annotations":[]}],
        })

    # refusal → message item with refusal part
    if msg.get("refusal"):
        output_items.append({
            "type":"message","id":_gen_id("msg_"),"role":"assistant","status":"completed",
            "content":[{"type":"refusal","refusal":msg["refusal"]}],
        })

    # tool_calls → function_call items
    for tc in msg.get("tool_calls") or []:
        output_items.append({
            "type":"function_call","id":_gen_id("fc_"),
            "call_id":tc["id"],"name":tc["function"]["name"],
            "arguments":tc["function"]["arguments"],"status":"completed",
        })

    resp_id = _gen_id("resp_")
    status = _finish_reason_to_status(finish_reason)
    incomplete = None
    if status == "incomplete":
        incomplete = {"reason":"max_output_tokens" if finish_reason=="length" else "content_filter"}

    resp = {
        "id": resp_id, "object": "response", "created_at": int(time.time()),
        "status": status, "error": None, "incomplete_details": incomplete,
        "model": model, "previous_response_id": previous_response_id,
        "output": output_items,
        "output_text": "".join(
            c["text"] for it in output_items if it["type"]=="message"
            for c in it["content"] if c.get("type")=="output_text"
        ),
        "usage": _usage_chat_to_resps(chat.get("usage") or {}),
    }

    if store is not None:
        store.save(resp_id, previous_response_id, api_key_name, model,
                   translated_input_items or [], output_items)

    return resp
```

## 4.5 `stream_c2r.py` —— 上游 chat SSE → 下游 responses SSE

上游 chat 每个 chunk 都是 `choices[0].delta` 的增量。下游要还原成 responses 的细粒度事件。

```python
@dataclass
class C2RState:
    resp_id: str
    model: str
    created_ts: int
    previous_response_id: str | None
    # 累积
    message_item_id: str | None = None
    message_output_index: int | None = None
    message_content_part_opened: bool = False
    # chat delta.tool_calls[index] → function_call output_index + fc_item_id
    fc_by_index: dict[int, dict] = field(default_factory=dict)   # {chat_index: {"output_index","fc_id","call_id","name","args_buf"}}
    next_output_index: int = 0
    # 收尾
    finish_reason: str | None = None
    usage: dict | None = None
    # 累积的 output items（用于 Store.save）
    finalized_output_items: list = field(default_factory=list)

    def next_index(self) -> int:
        i = self.next_output_index
        self.next_output_index += 1
        return i

def translate_stream(upstream_iter, *, model, previous_response_id=None,
                     store_save_cb=None) -> AsyncIterator[bytes]:
    """
    消费上游 chat SSE 字节流，产出下游 responses SSE 帧。
    store_save_cb(resp_id, output_items) 在 response.completed 之前调用一次。
    """
    state = C2RState(resp_id=_gen_id("resp_"), model=model,
                     created_ts=int(time.time()),
                     previous_response_id=previous_response_id)
    first_emitted = False
    async for evt in _parse_chat_sse(upstream_iter):
        if not first_emitted:
            yield _emit_created(state)         # response.created
            yield _emit_in_progress(state)     # response.in_progress
            first_emitted = True

        if evt == "done":
            break
        # evt 是 chat chunk 对象
        delta = (evt.get("choices") or [{}])[0].get("delta") or {}
        fr = (evt.get("choices") or [{}])[0].get("finish_reason")

        # content 文本增量
        if "content" in delta and delta["content"]:
            if state.message_item_id is None:
                state.message_item_id = _gen_id("msg_")
                state.message_output_index = state.next_index()
                yield _emit_output_item_added_message(state)
                yield _emit_content_part_added_output_text(state)
                state.message_content_part_opened = True
            yield _emit_output_text_delta(state, delta["content"])

        if "refusal" in delta and delta["refusal"]:
            # refusal 在 responses 里是 message 的独立 part；首版：关闭当前 text part，加 refusal part
            if state.message_item_id and state.message_content_part_opened:
                yield _emit_content_part_done_output_text(state, ...)  # text 完结
                state.message_content_part_opened = False
            yield _emit_refusal_delta(state, delta["refusal"])

        # reasoning_content（非官方，DeepSeek-R1 等）→ reasoning_summary_text.delta（见 06）

        # tool_calls 增量
        for tc in delta.get("tool_calls") or []:
            idx = tc["index"]
            st = state.fc_by_index.get(idx)
            if st is None:
                # 首次出现
                st = {
                    "output_index": state.next_index(),
                    "fc_id": _gen_id("fc_"),
                    "call_id": tc.get("id") or _gen_id("call_"),
                    "name": (tc.get("function") or {}).get("name", ""),
                    "args_buf": "",
                }
                state.fc_by_index[idx] = st
                yield _emit_output_item_added_function_call(state, st)

            args_delta = (tc.get("function") or {}).get("arguments")
            if args_delta:
                st["args_buf"] += args_delta
                yield _emit_fc_args_delta(state, st, args_delta)

        if fr:
            state.finish_reason = fr

        # usage chunk（stream_options.include_usage=true 时最后一帧）
        if evt.get("usage"):
            state.usage = extract_usage_chat(evt)

    # 收尾
    # 1. message item 收尾
    if state.message_item_id:
        if state.message_content_part_opened:
            yield _emit_content_part_done_output_text(state, ...)
        yield _emit_output_text_done(state)
        yield _emit_output_item_done_message(state)
        state.finalized_output_items.append(...)
    # 2. function_call items 收尾
    for idx, st in state.fc_by_index.items():
        yield _emit_fc_args_done(state, st)
        yield _emit_output_item_done_function_call(state, st)
        state.finalized_output_items.append(...)
    # 3. Store
    if store_save_cb:
        store_save_cb(state.resp_id, state.finalized_output_items)
    # 4. response.completed
    yield _emit_completed(state)
```

关键点：
- `_emit_*` 每个对应一个 SSE 帧构造（`sse_frame_responses("response.X", {...})`）
- `_gen_id("resp_")` 等使用 `openai.util` 统一 id 工厂
- `finish_reason` 如为 `"tool_calls"`：`response.completed.response.status = "completed"`（responses 里 tool_calls 不算 incomplete）
- `finish_reason == "length"` → `status="incomplete"`, `incomplete_details={"reason":"max_output_tokens"}`
- `finish_reason == "content_filter"` → `status="incomplete"`, `incomplete_details={"reason":"content_filter"}`

## 4.6 `stream_r2c.py` —— 上游 responses SSE → 下游 chat SSE

反向：把细粒度事件合并成 chat 的 `delta` 增量。

```python
@dataclass
class R2CState:
    chunk_id: str                  # "chatcmpl-..."
    model: str
    created_ts: int
    role_sent: bool = False
    # responses 的 output_item.added 里 type==function_call 的 item_id → chat tool_calls.index
    fc_output_index_to_tc_index: dict[int, int] = field(default_factory=dict)
    fc_call_id_by_tc_index: dict[int, str] = field(default_factory=dict)
    fc_name_by_tc_index: dict[int, str] = field(default_factory=dict)
    next_tc_index: int = 0
    usage: dict | None = None
    finish_reason: str | None = None
    status: str = "in_progress"
    incomplete_details: dict | None = None

def translate_stream(upstream_iter, *, model, include_usage=False) -> AsyncIterator[bytes]:
    state = R2CState(chunk_id=f"chatcmpl-{_uuid_short()}", model=model,
                     created_ts=int(time.time()))
    async for event_name, payload in _parse_responses_sse(upstream_iter):
        if event_name == "response.created":
            continue   # chat 没有对应事件；等首个 delta 再开
        if event_name == "response.output_item.added":
            item = payload.get("item") or {}
            if item.get("type") == "function_call":
                tc_idx = state.next_tc_index
                state.next_tc_index += 1
                state.fc_output_index_to_tc_index[payload["output_index"]] = tc_idx
                state.fc_call_id_by_tc_index[tc_idx] = item.get("call_id") or _gen_id("call_")
                state.fc_name_by_tc_index[tc_idx] = item.get("name", "")
                yield _emit_chat_chunk_tool_call_head(state, tc_idx)
            continue
        if event_name == "response.output_text.delta":
            if not state.role_sent:
                yield _emit_chat_chunk_role(state)
                state.role_sent = True
            yield _emit_chat_chunk_content(state, payload["delta"])
            continue
        if event_name == "response.function_call_arguments.delta":
            tc_idx = state.fc_output_index_to_tc_index.get(payload["output_index"])
            if tc_idx is None: continue
            yield _emit_chat_chunk_tool_args(state, tc_idx, payload["delta"])
            continue
        if event_name == "response.refusal.delta":
            yield _emit_chat_chunk_refusal(state, payload["delta"])
            continue
        if event_name == "response.reasoning_summary_text.delta":
            if _bridge_mode() == "passthrough":
                yield _emit_chat_chunk_reasoning(state, payload["delta"])   # 非官方 delta.reasoning_content
            continue
        if event_name in ("response.reasoning_text.delta",):
            # 同上，视配置
            continue
        if event_name == "response.output_item.done":
            # responses 里 function_call 在 done 时才会把 arguments 整串校验，chat 不需要另发
            continue
        if event_name == "response.completed":
            resp = payload.get("response") or {}
            state.status = resp.get("status", "completed")
            state.incomplete_details = resp.get("incomplete_details")
            state.usage = extract_usage_responses(resp)
            state.finish_reason = _derive_finish_reason(resp)
            break
        if event_name == "response.failed" or event_name == "error":
            # 流内转 chat SSE 错误行
            yield sse_frame_chat({"error": {"message": _err_msg(payload), "type": "server_error"}})
            yield sse_done_chat()
            return

    # 收尾：空 delta + finish_reason
    yield _emit_chat_chunk_finish(state)
    if include_usage and state.usage:
        yield _emit_chat_usage_chunk(state)
    yield sse_done_chat()
```

## 4.7 非流式路径

非流式反向只涉及 JSON ↔ JSON（上两节 §4.3 / §4.4 的 `translate_response`）。`failover._consume_non_stream` 读取完整 body 后按 `(ingress, ch.protocol)` 选择是否过 translate_response，再交给下游。

## 4.8 首包安全检查（sse first-event 判定）

现有 `upstream.parse_first_sse_event` 只认 Anthropic。新增：

```python
# upstream.py 追加
def parse_first_chat_event(chunk: bytes) -> dict | None:
    """chat SSE：首帧一般是 {id, object:"chat.completion.chunk", choices:[{delta:{role}}]}。
       若是 error 形如 {error:{...}} 则返回 {"_error": ...}。"""
    ...

def parse_first_responses_event(chunk: bytes) -> dict | None:
    """responses SSE：首帧一般 event: response.created。
       若是 error 则识别为 error。"""
    ...
```

`failover._try_channel` 按 `ch.protocol` 选其中一个调用。

## 4.9 黑名单（blacklist）

当前黑名单按 channel_key 匹配首包字节。openai 渠道直接复用 `blacklist.py`（纯字符串匹配，协议无关）。无改动。

## 4.10 实现清单

| 文件 | 行数估计 |
|---|---|
| `common.py` | ~200 |
| `guard.py` | ~120 |
| `chat_to_responses.py` | ~320 |
| `responses_to_chat.py` | ~380 |
| `stream_c2r.py` | ~420 |
| `stream_r2c.py` | ~460 |
| 合计 | **~1900** |
