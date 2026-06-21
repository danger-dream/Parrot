"""Capability guard：在 ingress 入口 + upstream 选型阶段拦截无法完成的请求。

MS-2 只实现"同 ingress 自检"与"跨变体未实现"的拒绝路径：
  - Chat ingress：`n>1` 与 audio output 允许 native Chat passthrough，
    跨协议由 matrix/translator guard。
  - Responses ingress：`background` / `conversation` 允许 native Responses
    passthrough，跨协议/Codex 由 matrix/provider guard。
  - 当需要跨变体翻译但 `openai.translation.enabled=false` 时，handler 在调度阶段
    自然得到空候选，返回 503 —— 不在此处干预。

真正的跨变体翻译死角（built-in tools / previous_response_id 无 Store 等）
在 MS-3 / MS-5 补齐。
"""

from __future__ import annotations

from typing import Any, Literal


GuardScope = Literal["request", "candidate"]


class GuardError(Exception):
    """带 HTTP status + OpenAI error type + 人类可读 message，供 handler 映射。

    ``scope`` 区分两类 guard：
    - ``request``：请求本身无论换哪个候选都不可安全服务，应直接返回 4xx。
    - ``candidate``：当前 provider/model 不支持，但 failover 里后续候选可能支持。
    """

    def __init__(self, status: int, err_type: str, message: str,
                 *, param: str | None = None, scope: GuardScope = "request"):
        super().__init__(message)
        self.status = int(status)
        self.err_type = err_type
        self.message = message
        self.param = param
        self.scope = scope


def _fail(status: int, err_type: str, message: str, *, param: str | None = None,
          scope: GuardScope = "request"):
    raise GuardError(status, err_type, message, param=param, scope=scope)


# ─── Chat ingress ────────────────────────────────────────────────

def guard_chat_ingress(body: dict) -> None:
    """Chat 入口自检（不管上游）：拒绝本 proxy 不支持的特性。

    `n>1` / audio output 不在入口拒绝：native OpenAI Chat 上游可以
    直接处理；只有需要 Chat→Responses/Anthropic fallback 时才拒绝。
    """
    from typing import Any as _Any  # noqa: F401
    if not isinstance(body, dict):
        _fail(400, "invalid_request_error", "request body must be a JSON object")

    # spec: CreateChatCompletionRequest.model required
    # 02-bug-findings #2: missing model would KeyError to 500; convert to 400 here.
    model = body.get("model")
    if not model or not isinstance(model, str):
        _fail(400, "invalid_request_error",
              "missing required field 'model'", param="model")


# ─── Responses ingress ───────────────────────────────────────────

def guard_responses_ingress(body: dict, *, store_enabled: bool = True) -> None:
    """Responses 入口自检。

    - background / conversation → native Responses API 可透传；跨协议/Codex
      不支持时由 scheduler 的 matrix/provider capability 拒绝对应候选。
    - previous_response_id 带了但 Store 关闭 → 400

    跨变体特有的 built-in tools 等在上游选型阶段（OpenAIApiChannel.build_upstream_request
    或 MS-3 的 responses_to_chat.guard）再拦一次，这里只做 ingress 无关检查。
    """
    if not isinstance(body, dict):
        _fail(400, "invalid_request_error", "request body must be a JSON object")

    # spec: CreateResponse.model required
    # 02-bug-findings #2: cross-variant chat→responses also relies on body["model"]; pre-reject.
    model = body.get("model")
    if not model or not isinstance(model, str):
        _fail(400, "invalid_request_error",
              "missing required field 'model'", param="model")

    if body.get("previous_response_id") and not store_enabled:
        _fail(400, "invalid_request_error",
              "previous_response_id requires openai.store.enabled=true",
              param="previous_response_id")


# ═══════════════════════════════════════════════════════════════
# 跨变体 guard
# ═══════════════════════════════════════════════════════════════
#
# 调用时机：OpenAIApiChannel.build_upstream_request 在发现 (ingress, protocol)
# 需要跨变体翻译时，先跑对应的跨变体 guard，再调 translate_request。


def guard_chat_to_responses(body: dict,
                            *, reject_on_multi_candidate: bool = True) -> None:
    """chat ingress → openai-responses 上游 的死角检查。

    该 guard 只拦会丢内容/状态的情况。请求控制 hint 交给 translator 的
    目标 payload 白名单处理：能映射就映射，不能映射就不写入 Responses
    payload。logprobs/top_logprobs 会映射为 Responses include/top_logprobs。
      - `n>1`（多候选）：responses 不原生支持（ingress guard 已拦，保留防御）
      - `modalities` 含 audio 或顶层 `audio`：需要 audio response
        输出语义，当前 Chat→Responses fallback 不聚合/回放音频输出。
      - 用户 message 的 content 里含 `input_audio` part：Chat 与 Responses
        都能表达该音频输入结构，translator 会保真映射；不能与 audio
        output/history 混为一类拦截。
    """
    if not isinstance(body, dict):
        _fail(400, "invalid_request_error", "request body must be a JSON object")

    if reject_on_multi_candidate:
        n = body.get("n")
        if isinstance(n, int) and n > 1:
            _fail(400, "invalid_request_error",
                  f"n={n} is not supported when routing to responses upstream",
                  param="n")

    modalities = body.get("modalities")
    if body.get("audio") is not None or (isinstance(modalities, list) and "audio" in modalities):
        _fail(400, "invalid_request_error",
              "audio output is not supported when routing to responses upstream",
              param="modalities")

    for msg in body.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if msg.get("audio") is not None:
            _fail(400, "invalid_request_error",
                  "assistant audio references are not supported when routing to responses upstream",
                  param="messages")
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for p in content:
            if isinstance(p, str):
                continue
            if not isinstance(p, dict):
                _fail(400, "invalid_request_error",
                      "message content parts must be objects when routing to responses upstream",
                      param="messages")
            typ = p.get("type")
            if typ in ("text", "image_url", "file", "input_audio"):
                continue
            if typ == "refusal" and role == "assistant":
                continue
            _fail(400, "invalid_request_error",
                  f"content part '{typ}' is not supported when routing to responses upstream",
                  param="messages")


# Responses 的 tools 中非 function 类型枚举（官方 built-in）。
# 遇到这些工具时，chat 上游没有等价实现 → 400。
# 02-bug-findings #21: 名单需补全到 spec 全部 built-in tool type，
# 否则未知 type 会被兜底拒绝、错误信息看着像 bug 报告而不是预期拒绝。
_BUILTIN_TOOL_TYPES = {
    # 经典 built-in
    "web_search_preview", "file_search", "computer_use_preview",
    "code_interpreter", "image_generation", "mcp", "local_shell",
    # 新版本/别名（spec 中 oneOf 各分支）
    "web_search", "web_search_2025_08_26", "web_search_preview_2025_03_11",
    "computer", "computer_use",
    "apply_patch", "function_shell",
}

# tool_choice 中允许的 hosted/MCP/custom/allowed_tools 形态
# 02-bug-findings #25: 这些 tool_choice 直接发到 chat 上游会 400，提前拦。
_NON_CHAT_TOOL_CHOICE_TYPES = {
    "file_search", "web_search_preview", "web_search",
    "web_search_2025_08_26", "web_search_preview_2025_03_11",
    "computer_use_preview", "computer", "computer_use",
    "code_interpreter", "image_generation",
    "mcp",
    "apply_patch", "function_shell",
}


# Responses input 可能出现的 built-in call item 类型。
# 出现即表示历史里带了上游 built-in 调用，chat 上游没法"延续"这些状态 → 400。
_BUILTIN_INPUT_ITEM_TYPES = {
    "web_search_call", "file_search_call", "computer_call",
    "image_generation_call", "code_interpreter_call",
    "mcp_call", "mcp_list_tools", "mcp_approval_request",
    "mcp_approval_response", "local_shell_call", "local_shell_call_output",
}


def _function_call_output_part_unsupported_for_chat(output: Any) -> str | None:
    if output is None or isinstance(output, str):
        return None
    if not isinstance(output, list):
        return None
    for part in output:
        if isinstance(part, str):
            continue
        if not isinstance(part, dict):
            return type(part).__name__
        typ = part.get("type")
        if typ in ("input_text", "output_text", "text"):
            continue
        return str(typ or "object")
    return None


def _responses_input_file_unsupported_for_chat(part: dict[str, Any]) -> str | None:
    if part.get("file_url") is not None:
        return "file_url"
    if part.get("file_id") is not None:
        return None
    file_data = part.get("file_data")
    if not isinstance(file_data, str) or not file_data:
        return "file_data"
    return None


def _responses_item_reference_unresolved_for_current_body(body: dict[str, Any]) -> bool:
    inp = body.get("input")
    if not isinstance(inp, list):
        return False
    known_ids: set[str] = set()
    has_history_anchor = bool(body.get("previous_response_id"))
    for item in inp:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "item_reference":
            ref_id = item.get("id")
            if not isinstance(ref_id, str) or not ref_id:
                return True
            if ref_id in known_ids or has_history_anchor:
                continue
            return True
        item_id = item.get("id")
        if isinstance(item_id, str) and item_id:
            known_ids.add(item_id)
    return False


def guard_responses_to_chat(body: dict,
                            *, store_enabled: bool = True,
                            reject_on_builtin_tools: bool = True) -> None:
    """responses ingress → openai-chat 上游 的死角检查。

    - `tools` 含非 function 类型（web_search_preview 等）→ 400
    - `input` 含 built-in call item（web_search_call 等）→ 400
    - `previous_response_id`：MS-3 不接 Store，一律拒绝；
      Store 接入后（MS-5 起）仅在 Store 关闭时拒绝
    - `include` 包含 "reasoning.encrypted_content"：chat 上游没有 encrypted
      reasoning replay 概念；必须拒绝，不能静默剥离，否则客户端会以为
      下一轮仍可拿 encrypted_content 续接推理链。
    - `conversation` / `background:true`：native Responses only; Chat upstream
      cannot preserve async response state.
    - `prompt` / `truncation` / `max_tool_calls` / 其他 include 是 Responses
      请求/返回 hint，translator 使用目标 Chat payload 白名单处理；不在
      这里阻断普通 fallback。
    """
    if not isinstance(body, dict):
        _fail(400, "invalid_request_error", "request body must be a JSON object")

    # tools 检查
    # 02-bug-findings #21: built-in 名单已补全；custom 工具属于用户定义但 chat 端
    # 结构不同，由 translate 层负责转换、不在这里拦。
    tools = body.get("tools") or []
    if isinstance(tools, list) and reject_on_builtin_tools:
        for t in tools:
            if not isinstance(t, dict):
                continue
            ttype = t.get("type")
            if not ttype or ttype == "function" or ttype == "custom":
                continue
            if ttype in _BUILTIN_TOOL_TYPES:
                _fail(400, "invalid_request_error",
                      f"built-in tool '{ttype}' is not supported when routing to chat upstream",
                      param="tools")
            # 未知 type：保守拒绝（消息保持 not supported 风格便于客户端识别）
            _fail(400, "invalid_request_error",
                  f"tool type '{ttype}' is not supported when routing to chat upstream",
                  param="tools")

    # tool_choice 形态 hosted/MCP/... 预拦
    # 02-bug-findings #25: 这些 tool_choice 透传到 chat 上游会 400，提前给清晰错误。
    tc = body.get("tool_choice")
    if isinstance(tc, dict):
        tc_type = tc.get("type")
        if tc_type in _NON_CHAT_TOOL_CHOICE_TYPES:
            _fail(400, "invalid_request_error",
                  f"tool_choice type '{tc_type}' is not supported when routing to chat upstream",
                  param="tool_choice")
        if tc_type == "allowed_tools":
            for tool in tc.get("tools") or []:
                if not isinstance(tool, dict):
                    continue
                nested_type = tool.get("type")
                if nested_type in (None, "function", "custom"):
                    continue
                _fail(400, "invalid_request_error",
                      f"tool_choice allowed_tools contains unsupported tool type '{nested_type}' when routing to chat upstream",
                      param="tool_choice")

    # input items 检查
    inp = body.get("input")
    if isinstance(inp, list):
        for it in inp:
            if isinstance(it, dict) and it.get("type") in _BUILTIN_INPUT_ITEM_TYPES:
                _fail(400, "invalid_request_error",
                      f"input item type '{it.get('type')}' is not supported when routing to chat upstream",
                      param="input")
            if isinstance(it, dict) and it.get("type") == "item_reference":
                if _responses_item_reference_unresolved_for_current_body(body):
                    _fail(400, "invalid_request_error",
                          "input item_reference cannot be resolved from local input/history",
                          param="input")
            if isinstance(it, dict) and it.get("type") in ("function_call_output", "custom_tool_call_output"):
                unsupported = _function_call_output_part_unsupported_for_chat(it.get("output"))
                if unsupported:
                    _fail(400, "invalid_request_error",
                          f"{it.get('type')} output part '{unsupported}' is not supported when routing to chat upstream",
                          param="input")
            if isinstance(it, dict) and it.get("type") == "input_file":
                _fail(400, "invalid_request_error",
                      "top-level input_file is not supported when routing to chat upstream",
                      param="input")
            content = it.get("content") if isinstance(it, dict) else None
            parts = content if isinstance(content, list) else []
            for part in parts:
                if isinstance(part, dict) and part.get("type") == "input_file":
                    unsupported = _responses_input_file_unsupported_for_chat(part)
                    if unsupported:
                        _fail(400, "invalid_request_error",
                              f"input_file field '{unsupported}' is not supported when routing to chat upstream",
                              param="input")

    # previous_response_id：MS-5 起由 Store 支持；Store 关闭时仍拒绝
    if body.get("previous_response_id") and not store_enabled:
        _fail(400, "invalid_request_error",
              "previous_response_id requires openai.store.enabled=true",
              param="previous_response_id")

    # conversation：显式再查一次，避免依赖调用顺序；null 占位放行
    if body.get("conversation"):
        _fail(400, "invalid_request_error",
              "conversation resource is not supported when routing to chat upstream",
              param="conversation")

    if body.get("background") is True:
        _fail(400, "invalid_request_error",
              "background async response is not supported when routing to chat upstream",
              param="background")

    # include：reasoning.encrypted_content 在 chat 上游不可得。
    # 这是会影响下一轮推理链 replay 的高风险语义，禁止静默剥离。
    include = body.get("include")
    if isinstance(include, list):
        for inc in include:
            if inc == "reasoning.encrypted_content":
                _fail(400, "invalid_request_error",
                      "include reasoning.encrypted_content is not supported when routing to chat upstream",
                      param="include")
