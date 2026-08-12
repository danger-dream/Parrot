# 03 · 修复方案 & 重构骨架

## 一、问题分类与根因

把 02 列的 46 条 bug 按根因聚成 6 类：

| 根因 | bug # | 共同点 |
|---|---|---|
| A. `_input_items_to_messages` 与 `_messages_to_input_items` 用 if/elif 链 + 隐式 fallthrough | 1, 3, 6, 15, 27, 33, 38 | 没有"未知/裸输入"的明确策略；guard 放行 ≠ translate 接住 |
| B. tool / tool_choice schema 形态枚举不全 | 21, 23, 24, 25, 26, 27, 30 | 仅认 string + function；spec 已扩到 8 种 |
| C. usage / status / response shape 必填字段缺失 | 8, 9, 13, 14, 17, 29 | 写出口时按"有值才写"，但 spec 标 required |
| D. annotations / verbosity / file_url / detail / file_id 字段双向丢失 | 4, 5, 11, 12, 19, 28, 35 | 双侧都没读没写，纯遗漏 |
| E. SSE 状态机边界 | 16, 18, 20, 33, 34, 41, 43, 44, 45, 46 | content_index、queued、断连 store、include_usage:null 等 |
| F. 错误/事件帧规范 | 7, 8, 10, 31, 32 | error code/type、incomplete details 不规范 |

## 二、统一架构建议

### 2.1 引入 item dispatcher（解 A 类）

新建 `src/openai/transform/_dispatch.py`：

```python
"""Responses input items 与 chat messages 的双向 dispatcher。

设计原则：
- 类型识别先看 type，缺 type 时按 role 兜底
- 每个 item type 必须显式有 handler（可选 raise NotImplemented）
- 未知 type 走 _on_unknown_item，默认 log warning + skip，可改 strict=raise
"""

from typing import Callable

# Responses input → chat messages
class R2CItemDispatcher:
    def __init__(self, strict: bool = False):
        self.strict = strict
        self._handlers: dict[str, Callable] = {
            "message": self._on_message,
            "function_call": self._on_function_call,
            "function_call_output": self._on_function_call_output,
            "reasoning": self._on_reasoning,
            "custom_tool_call": self._on_custom_tool_call,
            "custom_tool_call_output": self._on_custom_tool_call_output,
            # built-in 相关 type 由 guard 已拦；这里仍登记 noop 供 strict=False 兜底
            "web_search_call": self._noop,
            "file_search_call": self._noop,
            ...
        }

    def dispatch(self, item, ctx):
        t = item.get("type")
        # 裸消息：没 type 但有合法 role
        if t is None and item.get("role") in {"user","assistant","system","developer"}:
            t = "message"
        handler = self._handlers.get(t)
        if handler is None:
            return self._on_unknown(item, ctx)
        handler(item, ctx)
```

`ctx` 携带 messages list、pending_assistant、pending_reasoning 等共享状态。

### 2.2 引入 Chat→Responses item builder

类似 `C2RMessageDispatcher`，按 chat role 分发：`developer/system/user/assistant/tool/function`，function 显式映射到 function_call_output 或 raise。

### 2.3 引入 stream event dispatcher（解 E 类）

`stream_r2c.StreamTranslator` 和 `stream_c2r.StreamTranslator` 都用 `dict[event_name, handler]`，未知事件走 `_on_unknown_event(name, data)`，默认 log debug + skip；strict 模式 raise。这样新事件出现时只需登记 noop 而不是漏掉就出错。

### 2.4 schema field codec（解 D 类）

在 `common.py` 增加：

```python
# 字段双向编码：(chat_key, resp_key, codec)
PASSTHROUGH_FIELDS = [
    # (chat_field_path, resp_field_path)
    ("verbosity", "text.verbosity"),
    ("reasoning_effort", "reasoning.effort"),
    ("max_completion_tokens", "max_output_tokens"),
    # 复杂的字段写函数
]

def chat_to_resp_field(chat_body, resp_body): ...
def resp_to_chat_field(resp_body, chat_body): ...
```

避免每个翻译函数手抄一遍。

### 2.5 ResponseUsage builder（解 C 类）

```python
def build_response_usage(input_tokens, output_tokens, cached, reasoning, total=None):
    return {
        "input_tokens": int(input_tokens or 0),
        "input_tokens_details": {"cached_tokens": int(cached or 0)},
        "output_tokens": int(output_tokens or 0),
        "output_tokens_details": {"reasoning_tokens": int(reasoning or 0)},
        "total_tokens": int(total if total is not None else (input_tokens + output_tokens)),
    }
```

四处 `_usage_*` 全部改用此函数。

### 2.6 Response skeleton builder（解 #13）

```python
def build_response_skeleton(*, resp_id, model, created_at, status,
                             previous_response_id, request_body=None) -> dict:
    rb = request_body or {}
    return {
        "id": resp_id, "object": "response", "created_at": created_at,
        "status": status, "error": None, "incomplete_details": None,
        "instructions": rb.get("instructions"),
        "model": model,
        "tools": rb.get("tools") or [],
        "output": [],
        "parallel_tool_calls": rb.get("parallel_tool_calls", True),
        "metadata": rb.get("metadata") or {},
        "tool_choice": rb.get("tool_choice", "auto"),
        "temperature": rb.get("temperature", 1),
        "top_p": rb.get("top_p", 1),
        "reasoning": rb.get("reasoning") or {"effort": None, "summary": None},
        "text": rb.get("text") or {"format": {"type": "text"}},
        "truncation": rb.get("truncation", "disabled"),
        "store": rb.get("store"),
        "previous_response_id": previous_response_id,
        "output_text": "",
        "usage": None,
    }
```

`stream_c2r.StreamTranslator` 构造时接收 `request_body`，skeleton 用此构造。

### 2.7 ResponseError code mapping（解 #8）

```python
RESPONSE_ERROR_CODES = {
    "server_error", "rate_limit_exceeded", "invalid_prompt",
    "vector_store_timeout", "invalid_image", ...  # 18 个
}

CHAT_TO_RESP_ERROR = {
    "rate_limit_error": "rate_limit_exceeded",
    "invalid_request_error": "invalid_prompt",
    # 默认
}

def map_error_code(code: str | None, type_: str | None) -> str:
    if code in RESPONSE_ERROR_CODES:
        return code
    if type_ in CHAT_TO_RESP_ERROR:
        return CHAT_TO_RESP_ERROR[type_]
    return "server_error"
```

---

## 三、修复顺序与 patch 合并建议

按 "最小阻塞" 优先级排，把可一起改的 bug 合并成 patch 提交（每个 patch 一组测试）。

### Patch 1：P0 阻塞修复（必发）

| 编号 | 内容 | 文件 |
|---|---|---|
| #1 | 裸消息识别 | responses_to_chat.py |
| #2 | model 缺失 → 400 | guard.py |
| #9 | usage details 始终写 | common.py + 4 处 _usage_* |
| #15 | role=function legacy 翻译 | chat_to_responses.py |
| #20 | _on_completed 设 terminal_status | stream_r2c.py |

测试：每条带 1 个回归用例。

### Patch 2：流式状态机一致性（紧随）

| #16 | content_index 用累计计数 | stream_c2r._MessageItem |
| #17 | function_call_arguments.done 补 name | stream_c2r._close_function_call |
| #13 | Response skeleton 完整 | stream_c2r._response_skeleton + 接收 request_body |
| #41 | error/断连时 store 也写 | stream_c2r.close |
| #43 | include_usage=true 中间 chunk 带 usage:null | stream_r2c._mk_chunk |

### Patch 3：tool / tool_choice 完整支持

合并 #21、#23、#24、#25、#26、#27、#30 一起改：
- guard 名单补全 + tool_choice hosted/mcp/allowed_tools/custom 拦截
- _flatten_tool / _nest_tool 加 custom 形态
- tool_choice 双向加 custom + allowed_tools
- assistant.tool_calls type=custom 翻译为 custom_tool_call item
- stream_c2r 加 custom_tool_call 流式状态机
- FunctionTool.strict 默认值

这一组只动 chat_to_responses.py、responses_to_chat.py、guard.py、stream_c2r.py，影响面收敛。

### Patch 4：字段补齐

| #4 | image_url.file_id 双向 | _content_chat_to_responses + _content_responses_to_chat |
| #5 | input_file.file_url + detail 双向 | 同上 |
| #11 | verbosity 双向映射 | translate_request 双向 |
| #12 | reasoning.summary 透传（按 PASSTHROUGH_FIELDS） | translate_request 双向 |
| #28 | annotations 双向 | translate_response 双向 |
| #35 | response.output_text.annotation.added 流式 | stream_r2c._handle_event_block |
| #29 | output_text.delta/done 始终带 logprobs:[] | stream_c2r._emit_output_text_delta/_close_message_item |

### Patch 5：错误规范

| #7 | chat error 帧透 code/type/param | stream_r2c._on_error |
| #8 | response.failed.error.code 映射到 ResponseErrorCode enum | stream_c2r._emit_failed |
| #18 | delta.function_call legacy 处理 | stream_c2r._handle_choice |
| #10 | assistant content 全空时 skip 或占位 | responses_to_chat._input_items_to_messages |

### Patch 6：边界 + 死代码清理

| #3 | function_call_output array → 文本 | responses_to_chat |
| #6 | 删除 input_audio 死代码（或在 guard 拦） | responses_to_chat / guard |
| #19 | reasoning summary vs reasoning_text 区分 | _gather_reasoning_summary + 反向 |
| #22 | 不强制 system→developer 改名 | chat_to_responses._messages_to_input_items |
| #33 | 多 message item 切换发新 role chunk | stream_r2c._on_output_item_added 增加 message 分支 |
| #36 | _stringify_tool_content 严格化 | chat_to_responses |
| #38 | reasoning 跨轮保留连续性 | responses_to_chat（已观察为偏低优） |
| #40 | prediction 字段 log warning | guard.guard_chat_to_responses |
| #44/#45/#46 | SSE 多 data 行拼接 | stream_*._handle_block |

---

## 四、重构后目录建议

```
src/openai/transform/
├── __init__.py
├── common.py                  # 字段白名单、SSE 帧、usage builder、error code map、response skeleton
├── _dispatch.py               # ★新：R2CItemDispatcher / C2RMessageDispatcher / EventDispatcher 基类
├── guard.py                   # 跨变体守卫（补 tool_choice / role=function / prediction warning）
├── responses_to_chat.py       # 改用 R2CItemDispatcher
├── chat_to_responses.py       # 改用 C2RMessageDispatcher
├── stream_r2c.py              # event handler 字典化
├── stream_c2r.py              # event handler 字典化 + skeleton 用 builder
├── codex_oauth_transform.py   # 不动
└── tests/                     # ★新（如未存在）
    ├── test_field_mapping.py
    ├── test_dispatcher.py
    ├── test_stream_r2c.py
    ├── test_stream_c2r.py
    └── test_usage_codec.py
```

---

## 五、测试策略

### 5.1 单元测试

#### 5.1.1 字段映射 codec

`test_field_mapping.py`：每个 PASSTHROUGH_FIELDS 一个 case。

```python
def test_verbosity_chat_to_resp():
    chat = {"model":"x","messages":[{"role":"user","content":"hi"}],"verbosity":"low"}
    out = chat_to_responses.translate_request(chat)
    assert out["text"]["verbosity"] == "low"

def test_verbosity_resp_to_chat():
    resp = {"model":"x","input":[{"role":"user","content":"hi"}],
            "text":{"verbosity":"high","format":{"type":"text"}}}
    out = responses_to_chat.translate_request(resp)
    assert out["verbosity"] == "high"
```

#### 5.1.2 dispatcher 完整性

```python
def test_all_responses_input_types_have_handler():
    """spec 中每个 InputItem oneOf branch 都必须在 dispatcher 里登记（可以是 noop/raise）"""
    spec_types = {"message","function_call","function_call_output","reasoning",
                  "web_search_call","file_search_call","computer_call","computer_call_output",
                  "image_generation_call","code_interpreter_call",
                  "mcp_call","mcp_list_tools","mcp_approval_request","mcp_approval_response",
                  "local_shell_call","local_shell_call_output",
                  "function_shell_call","function_shell_call_output",
                  "apply_patch_tool_call","apply_patch_tool_call_output",
                  "custom_tool_call","custom_tool_call_output",
                  "compaction_summary","tool_search_call","tool_search_output",
                  "item_reference"}
    handlers = R2CItemDispatcher()._handlers.keys()
    missing = spec_types - set(handlers)
    assert not missing, f"missing handlers: {missing}"
```

#### 5.1.3 裸消息回归

```python
def test_bare_message_translates_to_chat():
    body = {"model":"x","input":[{"role":"user","content":"hi"}]}
    out = responses_to_chat.translate_request(body)
    assert out["messages"] == [{"role":"user","content":"hi"}]
```

#### 5.1.4 usage 必填

```python
def test_response_usage_required_fields():
    chat_usage = {"prompt_tokens":10,"completion_tokens":5,"total_tokens":15}
    out = responses_to_chat._usage_chat_to_resps(chat_usage)
    assert "input_tokens_details" in out
    assert out["input_tokens_details"]["cached_tokens"] == 0
    assert out["output_tokens_details"]["reasoning_tokens"] == 0
```

#### 5.1.5 tool_choice 形态

每个形态一个 case；包含 string/function/custom/allowed_tools/hosted。

#### 5.1.6 流式 sequence_number 单调

```python
def test_c2r_sequence_monotonic():
    tr = stream_c2r.StreamTranslator(model="x")
    chunks = [
        b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
        b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
        b'data: [DONE]\n\n',
    ]
    out = b''.join(b''.join(tr.feed(c)) for c in chunks) + b''.join(tr.close())
    seqs = [json.loads(line[5:]) for line in out.split(b'\n\n') if line.startswith(b'data: ') and not line.endswith(b'[DONE]\n')]
    seq_nums = [e.get("sequence_number") for e in seqs if "sequence_number" in e]
    assert seq_nums == sorted(seq_nums)
```

#### 5.1.7 stream skeleton 完整性

```python
def test_response_created_skeleton_required_fields():
    tr = stream_c2r.StreamTranslator(model="gpt-4", request_body={"tools":[],"tool_choice":"auto"})
    out = b''.join(tr.feed(b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'))
    events = parse_sse(out)
    created = next(e for e in events if e["event"] == "response.created")
    resp = created["data"]["response"]
    for f in ("id","object","created_at","status","error","incomplete_details",
              "model","tools","output","parallel_tool_calls","metadata","tool_choice","temperature","top_p"):
        assert f in resp, f"required field {f} missing"
```

### 5.2 集成测试

#### 5.2.1 真实上游 round-trip（需要 mock）

写一个 `MockChatUpstream`，按 SSE 协议吐 chunk；让 stream_c2r 翻译；用 `openai.responses.create(stream=True)` 客户端 SDK 反序列化每个事件，断言无异常。

```python
async def test_round_trip_chat_upstream_responses_client():
    upstream_chunks = [
        b'data: {"id":"x","choices":[{"delta":{"role":"assistant","content":"hello"}}]}\n\n',
        b'data: {"id":"x","choices":[{"delta":{"content":" world"},"finish_reason":"stop"}]}\n\n',
        b'data: [DONE]\n\n',
    ]
    sse = stream_through_translator(upstream_chunks)
    # 用 OpenAI SDK 解析
    events = list(parse_responses_sse(sse))
    types = [e["type"] for e in events]
    assert "response.created" in types
    assert "response.output_text.delta" in types
    assert "response.completed" in types
```

#### 5.2.2 已知翻车 case 矩阵

把 02 的每条 P0/P1 写成集成 case：
- bare_message_round_trip
- function_role_round_trip
- function_call_output_array_round_trip
- custom_tool_call_round_trip
- allowed_tools_choice_round_trip
- usage_zero_cached_round_trip
- include_usage_intermediate_null_round_trip

#### 5.2.3 Codex OAuth 回归

`codex_oauth_transform.py` 不在审计重点，但 patch 3 会改 `_flatten_tool`，因此需要运行现有真实协议 fixture 防回归。

### 5.3 结合 spec 的合约测试

写一个 `tools/spec_validate.py`：用 `openapi.documented.yml` 编译出 JSON Schema，对每个翻译输出做 `jsonschema.validate`：

```python
def test_translate_request_responses_schema_compliant():
    chat = sample_chat_request()
    resp = chat_to_responses.translate_request(chat)
    validate_against_schema(resp, "CreateResponse")  # 抛异常即失败

def test_translate_response_chat_schema_compliant():
    resps_resp = sample_responses_response()
    chat = chat_to_responses.translate_response(resps_resp, model="x")
    validate_against_schema(chat, "CreateChatCompletionResponse")
```

把 14 个必填字段、tool_choice oneOf、ResponseUsage required 等约束**全部交给 schema 验证**而不是手写断言，能彻底兜底未来 spec 升级带来的字段缺失。

---

## 六、上线节奏建议

| 周次 | 内容 | 风险 | 回滚 |
|---|---|---|---|
| W1 | Patch 1（P0 修复）+ 5.1.3/5.1.4 单测 | 低 | 单 commit revert |
| W2 | Patch 2（流状态机）+ 5.1.6/5.1.7 单测 | 中（streaming 行为变） | 灰度一台节点 |
| W3 | Patch 3（tool/tool_choice）+ tool 矩阵单测 + Codex OAuth 回归 | 中 | 灰度 |
| W4 | Patch 4 + 5.2 集成测试通过 | 低 | — |
| W5 | Patch 5 + 6 + spec 合约测试 + 灰度全量 | 低 | — |

---

## 七、附录：可立刻动手的小修

下列改动每条不超过 5 行，可随其它 patch 顺手带：

1. `_emit_terminal` reasoning item 加 `"status":"completed"`
2. `guard.GuardError` 包含 status code 映射应有 422 选项（spec 中 input 验证失败 422 而非 400）
3. `common.RESPONSES_REQ_ALLOWED` 加 `context_management`（已有）+ 显式列出 `instructions`（已有）；新增 `prompt`（已有）
4. `chat_to_responses._messages_to_input_items` assistant 分支 array content 不要走 `_stringify_tool_content`，改为直接 list comprehension 提 text+refusal 拼成 content parts 列表（output_text + refusal）
5. `responses_to_chat._content_responses_to_chat` 收到未知 part type 时打 debug log，便于发现新 part 类型

---

完。
