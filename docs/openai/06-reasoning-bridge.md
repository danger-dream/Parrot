# 06 — Reasoning 跨协议桥接

Responses 有原生的 `reasoning` 概念（输出 item 类型 + SSE 事件类型）；Chat 协议没有，但业界（DeepSeek-R1、月之暗面 等）事实上用 **非官方字段 `reasoning_content`**。

本方案用配置开关 `openai.reasoningBridge` 控制行为。

## 6.1 `reasoning_content` 约定

```jsonc
// Chat 非流式响应
{
  "choices":[{
    "message":{
      "role":"assistant",
      "reasoning_content":"...推理过程...",   // ★ 非 OpenAI 官方字段
      "content":"最终回答"
    }
  }]
}

// Chat 流式：
data: {"choices":[{"delta":{"reasoning_content":"片段"}}]}
data: {"choices":[{"delta":{"content":"回答片段"}}]}
```

该字段被 DeepSeek 生态广泛采用，不兼容的客户端会忽略（不会报错）。

## 6.2 配置

```jsonc
{
  "openai": {
    "reasoningBridge": "passthrough"   // 或 "drop"
  }
}
```

- `"passthrough"`（默认）：双向映射，让下游看到推理内容
- `"drop"`：彻底不携带

## 6.3 Responses → Chat

### 非流式

```python
def _gather_reasoning_summary(output: list) -> str | None:
    if _bridge_mode() != "passthrough": return None
    parts = []
    for item in output:
        if item.get("type") != "reasoning": continue
        for s in item.get("summary") or []:
            if s.get("type") == "summary_text" and s.get("text"):
                parts.append(s["text"])
    return "\n\n".join(parts) if parts else None
```

得到的字符串填进 `choices[0].message.reasoning_content`。

### 流式

```
response.reasoning_summary_text.delta  → chat chunk: delta.reasoning_content = evt.delta
response.reasoning_summary_part.added  → 无对应（忽略）
response.reasoning_summary_text.done   → 无对应
response.reasoning_text.delta          → 同 summary_text.delta 处理（若存在）
response.output_item.added (reasoning) → 仅作状态标记，不外发
response.output_item.done (reasoning)  → 若有累积，无需 flush（delta 已累积发完）
```

实现点在 `stream_r2c.py`。

**限制**：`encrypted_content` 不透出（Chat 侧没有存储位）。同多轮场景下如果后续又切回同家族 responses，历史推理无法恢复。默认可接受；需要保留时用户应走"同协议 responses→openai-responses"路径。

## 6.4 Chat → Responses

### 非流式

```python
# translate_response (chat → responses JSON)
if msg.get("reasoning_content") and _bridge_mode() == "passthrough":
    output_items.insert(0, {
        "type": "reasoning",
        "id": _gen_id("rs_"),
        "summary": [{"type":"summary_text","text": msg["reasoning_content"]}],
        # 不设 encrypted_content（没有）
    })
```

并把 `usage.completion_tokens_details.reasoning_tokens` 映射为 `usage.output_tokens_details.reasoning_tokens`。

### 流式

上游 chat 在 `stream_c2r.py` 里识别 `delta.reasoning_content`：

```python
# stream_c2r.translate_stream 内 delta 循环
if "reasoning_content" in delta and delta["reasoning_content"] and _bridge_mode() == "passthrough":
    if state.reasoning_item_id is None:
        state.reasoning_item_id = _gen_id("rs_")
        state.reasoning_output_index = state.next_index()
        yield _emit_output_item_added_reasoning(state)
        yield _emit_reasoning_summary_part_added(state)
    yield _emit_reasoning_summary_text_delta(state, delta["reasoning_content"])

# 结束时（首个非 reasoning_content 的 delta 或流结束时）
def _close_reasoning_if_open(state):
    if state.reasoning_item_id:
        yield _emit_reasoning_summary_text_done(state)
        yield _emit_reasoning_summary_part_done(state)
        yield _emit_output_item_done_reasoning(state)
        state.reasoning_item_id = None
```

**顺序约束**：responses 里 reasoning item 应当在 message item 之前（客户端会按 output 顺序展示）。实现时先检测 chat delta 中 `reasoning_content`，**在**收到任何 `content` 之前把 reasoning 完整 emit 出来，再开 message item。如果两类 delta 交错，按上游发来的顺序 emit 也可（responses 本身支持多个 output item）。

## 6.5 `openai.reasoningBridge == "drop"`

- responses→chat：忽略所有 `response.reasoning_*` 事件 / 不填 `reasoning_content`
- chat→responses：忽略 `delta.reasoning_content` / 不生成 reasoning item

## 6.6 与 `include: ["reasoning.encrypted_content"]` 的交互

当下游 responses 请求 `include: ["reasoning.encrypted_content"]`：
- 同协议透传 → 上游会返回带 encrypted 的 reasoning item，proxy 原样转发
- 跨变体（上游 chat）→ guard 里是否拒绝？
  - 方案 A：无脑拒绝（最清晰）
  - 方案 B：静默移除 include 中的 `reasoning.encrypted_content`（因为本来就不可能给出来）
  - **首版选 A**：400 error，`encrypted_content not available when upstream is chat`，避免客户端误以为拿到了但实际没有

## 6.7 Usage 的 `reasoning_tokens` 映射

| chat | responses |
|---|---|
| `usage.completion_tokens_details.reasoning_tokens` | `usage.output_tokens_details.reasoning_tokens` |

双向互转时原样映射（`reasoningBridge` 不影响 usage 字段，只影响推理文本是否透出）。
