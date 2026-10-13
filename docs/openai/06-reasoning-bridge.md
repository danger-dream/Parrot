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

## 6.8 Responses → Anthropic 的历史推理

历史 `reasoning` 不得降级为普通 assistant `text`，也不添加推理摘要标签。显式 `input` 和 `previous_response_id` 展开的历史使用同一策略：

- 可读摘要和外来 OpenAI `encrypted_content` 均不能当作 Claude 原生签名。无法回放的摘要跳过，不冒充正文或签名；最新工具续接缺少必需思考块时，清理不可用历史并按下述模式策略继续执行。
- 已确认支持无签名 thinking 的上游，由通道代码显式启用 `allow_unsigned_thinking`。当前智谱 Coding Plan 通道启用此能力，将可读摘要恢复为 `thinking` 块，保留其与正文、工具调用的相对顺序。此参数不是客户端请求字段，也不根据客户端模型名猜测能力。
- 优先读取 `summary_text`；没有可读摘要时才读取 `content.reasoning_text`，避免客户端重复携带两者导致推理重复。
- Claude 原生签名随 reasoning item 的 `encrypted_content` 携带：`claude#` 前缀表示签名，`claude-redacted-thinking:` 表示原生 redacted 数据；原始 thinking 文本保存在 `summary`，恢复时不改写文本或伪造签名。这是带来源标记的回放载体，不是 OpenAI 密文；切到原生 OpenAI Responses 出口时移除该类 item，以及仅指向这些已知删除目标的 `item_reference`，其他引用和正文、工具内容不变。
- 流式签名分片累积至 `content_block_stop` 后，放入 `response.output_item.done` 和最终 output；中断且未结束的块不生成回放载体。已有 Store 保存完整 item，`previous_response_id` 展开后使用相同恢复逻辑。签名回放不依赖进程内缓存或小时级 TTL；客户端持有的完整 item 可跨重启回传。Store 自身的历史保留和访问控制仍适用。
- 能恢复时保留原生思考块、正文及工具调用的相对顺序。最新工具轮缺少签名、reasoning 仅剩 ID 或整段思考历史被省略时，先在完整历史仍可见时清理不可用思考历史；最终 Claude Code 默认参数加工后再次修复，不重复读取 Store。保留正文、工具调用和工具结果，不修改全局配置。CC 剥离旧轮思考后若 assistant 内容变空，只省略该消息，不新造 `[Thinking removed]` 正文；已有同名真实文本不删除。
- 模式策略不是统一关闭推理：`adaptive` 保留原配置及 `output_config.effort`；签名严格路径的 legacy manual `enabled` 在必要时省略显式 `thinking`（并清理相依的 `clear_thinking_*` 编辑），而非改成 `disabled`。智谱 `allow_unsigned_thinking` 路径保留 `enabled` 和 effort。其他格式控制保持原语义。
- 如 Anthropic 协议上游仍明确以 HTTP 400 拒绝思考签名、顺序或完整性，在尚未成功且未向下游提交输出时，同通道清理思考历史并按上述模式策略重试一次。该恢复适用于 Responses→Anthropic 和原生 Anthropic 请求；不因普通参数错误、鉴权错误、5xx 或已开始的输出盲目重试。明确历史 400 即使使用纯文本或非标准 JSON，耗尽恢复后也保留 400，不冷却或处罚健康通道。
- 已验证范围：GLM-5.3 的完整/缺失/Store 历史流式及非流式均可继续执行且思考/正文分离；Opus 4.5 的真实签名回放、缺失后省略 thinking、坏签名后恢复均成功。Sonnet 5.5、Opus 5.5、Fable 5.1 已以合成坏签名历史验证真实上游拒绝后保留 adaptive 与 high effort 的恢复；这不代表现代模型的真实完整签名生成/回放均已覆盖。这些始终思考模型不支持用 `disabled` 关闭推理，省略 thinking 也不承诺关闭。
- `reasoningBridge=drop` 不输出或恢复推理 item；严格上游需要历史思考的工具续接使用上述请求级降级。智谱兼容路径保持既有 drop 行为。

历史上已存成普通正文的摘要不按标签自动删除，以免损坏真实文本或正常引用。
