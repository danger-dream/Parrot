# 04 · 实施笔记 — 与 03 计划的偏离 / 实施期发现

## Patch 1

### 偏离 / 调整

1. **`test_openai_audit.py::test_guard_conversation_null_allowed` 测试更新（必要）**
   - 该测试为验证 conversation 字段的 null/空字典放行行为而构造了 `{"conversation": None}` 等极简 body，未带 `model`。
   - Patch 1 实现 #2（model required）后，guard 在到达 conversation 检查之前就会以 missing model 拒绝。
   - 修复方式：把测试 body 补上 `"model": "x", "input": []`；语义不变（仍然测 conversation 字段处理）。
   - 这是**已存在测试的最小适配**，不是新功能/不是行为回退。

### 补充说明

- `common.py` 同时新增了 `build_chat_usage`（与 `build_response_usage` 对称），使 chat-side 的 details 字段也始终写入。03 文档只提到 `build_response_usage`，但既然 02#9 要求"四处 _usage_* 全部改用此函数"，对称的 chat-side helper 是合理拓展，避免后续 patch 重复手抄。
- #20 的实现采用"`terminal_status` 一旦置位，下次 `feed` 进来的 event 全部短路"。03 的描述是"_on_completed/_on_incomplete 之后立即 set self.state.terminal_status 并 return；后续事件直接忽略"，本实现把忽略逻辑统一放在 `_handle_event_block` 入口，逻辑等价但更清晰。

## Patch 3

### 偏离 / 调整

1. **`test_openai_m3.py::test_c2r_translate_request_basics` 与 `test_chat_to_responses_function_tool` 测试更新（必要）**
   - 这两个测试断言 `out["tools"]` 与某个**精确字典**相等，未包含 `strict` 字段。
   - Patch 3 / #30 实现后，FunctionTool.strict 自动补默认 `False`，原断言因新增字段失配。
   - 修复：在期望字典里也加 `"strict": False`，注释指明 Patch 3 / #30。

### 设计说明

- `_BUILTIN_TOOL_TYPES` 名单按 02#21 全部补齐；`custom` **不**进 built-in 名单（它是用户定义工具），允许 translate 层正确处理。
- guard 新增 `_NON_CHAT_TOOL_CHOICE_TYPES` 集合用于 #25 的 tool_choice 预拦；MCP 等带 server_label 的也覆盖。
- stream_c2r 新增 `_CustomToolCallItem` 数据类与 `_handle_custom_tool_call_delta` 状态机，与 `_FunctionCallItem` 共享 output_index 顺序但事件名分别为 `response.custom_tool_call_input.delta/done`。
- 同 chat 流的 type=custom tool_call 与 type=function tool_call 通过 `tc.get("type") == "custom"` 在首包识别；后续续包仅按 index 路由，**不会**因为续包不再带 type 而退化处理。
- `_collect_output_items` 与 `_close_all_function_calls` 都更新为同时收集 function_call 与 custom_tool_call。
