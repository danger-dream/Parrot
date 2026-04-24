"""Capability guard：在 ingress 入口 + upstream 选型阶段拦截无法完成的请求。

MS-2 只实现"同 ingress 自检"与"跨变体未实现"的拒绝路径：
  - Chat ingress：拒绝 `n>1` / `audio` 输出（本版本不支持，暂 400）
  - Responses ingress：`background:true` 静默降级为同步模式（透明兼容），拒绝 `conversation` 对象（首版不做）
  - 当需要跨变体翻译但 `openai.translation.enabled=false` 时，handler 在调度阶段
    自然得到空候选，返回 503 —— 不在此处干预。

真正的跨变体翻译死角（built-in tools / previous_response_id 无 Store 等）
在 MS-3 / MS-5 补齐。
"""

from __future__ import annotations

from typing import Any


class GuardError(Exception):
    """带 HTTP status + OpenAI error type + 人类可读 message，供 handler 映射。"""

    def __init__(self, status: int, err_type: str, message: str,
                 *, param: str | None = None):
        super().__init__(message)
        self.status = int(status)
        self.err_type = err_type
        self.message = message
        self.param = param


def _fail(status: int, err_type: str, message: str, *, param: str | None = None):
    raise GuardError(status, err_type, message, param=param)


# ─── Chat ingress ────────────────────────────────────────────────

def guard_chat_ingress(body: dict) -> None:
    """Chat 入口自检（不管上游）：拒绝本 proxy 不支持的特性。

    现阶段拒绝：
      - `n>1`：本 proxy 不聚合多候选
      - `audio` 输出（modalities 含 "audio"）：本版本不支持 audio 输出
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

    n = body.get("n")
    if isinstance(n, int) and n > 1:
        _fail(400, "invalid_request_error",
              f"n={n} is not supported by this proxy", param="n")

    modalities = body.get("modalities")
    if isinstance(modalities, list) and "audio" in modalities:
        _fail(400, "invalid_request_error",
              "audio output modality is not supported by this proxy",
              param="modalities")


# ─── Responses ingress ───────────────────────────────────────────

def guard_responses_ingress(body: dict, *, store_enabled: bool = True) -> None:
    """Responses 入口自检。

    - background:true → 静默剥除（透明兼容）：代理无状态，不维护 response store 的
      pending → completed 状态机；客户端 (Codex CLI / OpenAI SDK) 在 background:true
      下通常也直接读 SSE 流，剥除该字段后走同步路径行为等价。
    - conversation 对象 → 400（首版不支持 conversation 资源，仅 previous_response_id）
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

    # background 字段静默剥除（无论 true/false）：
    # Codex OAuth 上游 /backend-api/codex/responses 不接受 background 参数，
    # 即使传 false 也会返回 HTTP 400 "Unsupported parameter: background"。
    # 代理无状态，不实现 background 异步模式；客户端行为等价于同步/流式调用。
    if "background" in body:
        had_true = body.get("background") is True
        body.pop("background", None)
        try:
            import logging
            logging.getLogger("parrot.openai").info(
                "[guard] background field stripped (compat, had_true=%s)", had_true
            )
        except Exception:
            pass

    # 只在实际提供了 conversation 值时拒绝；显式 null 占位（某些客户端默认带）应放行
    if body.get("conversation"):
        _fail(400, "invalid_request_error",
              "conversation resource is not yet supported; use previous_response_id instead",
              param="conversation")

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

    绝大部分丢失字段（stop / seed / logprobs / prediction / logit_bias 等）
    在翻译时静默丢弃——上游客户端也不指望 proxy 保留这些（它们本来就上不了
    responses API）。只拦住"拒绝更安全"的几类：
      - `n>1`（多候选）：responses 不原生支持（ingress guard 已拦，保留防御）
      - `logprobs/top_logprobs`：下游客户端可能强依赖，默认拒绝以免沉默性能降级
      - 用户 message 的 content 里含 `input_audio` part：Responses API 的
        ResponseInputContent 只支持 text/image/file，发过去会被上游 400 拒绝；
        提前拦截让错误信号更清晰（同协议 chat→chat 不受影响）
    """
    if not isinstance(body, dict):
        _fail(400, "invalid_request_error", "request body must be a JSON object")

    if reject_on_multi_candidate:
        n = body.get("n")
        if isinstance(n, int) and n > 1:
            _fail(400, "invalid_request_error",
                  f"n={n} is not supported when routing to responses upstream",
                  param="n")

    if body.get("logprobs") or isinstance(body.get("top_logprobs"), int):
        _fail(400, "invalid_request_error",
              "logprobs/top_logprobs are not supported when routing to responses upstream",
              param="logprobs")

    for msg in body.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for p in content:
            if isinstance(p, dict) and p.get("type") == "input_audio":
                _fail(400, "invalid_request_error",
                      "input_audio content parts are not supported when routing to responses upstream",
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


def guard_responses_to_chat(body: dict,
                            *, store_enabled: bool = True,
                            reject_on_builtin_tools: bool = True) -> None:
    """responses ingress → openai-chat 上游 的死角检查。

    - `tools` 含非 function 类型（web_search_preview 等）→ 400
    - `input` 含 built-in call item（web_search_call 等）→ 400
    - `previous_response_id`：MS-3 不接 Store，一律拒绝；
      Store 接入后（MS-5 起）仅在 Store 关闭时拒绝
    - `include` 包含 "reasoning.encrypted_content"：chat 上游没有 encrypted
      概念 → 静默从 include 列表中剥除（兼容客户端默认带这个开关的场景；
      客户端拿到的 reasoning 块不会带 encrypted_content 字段，这与 chat
      上游能力一致，不强迫客户端去识别上游协议来裁请求）
    - `conversation` / `background`：由 guard_responses_ingress 已拒
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

    # input items 检查
    inp = body.get("input")
    if isinstance(inp, list):
        for it in inp:
            if isinstance(it, dict) and it.get("type") in _BUILTIN_INPUT_ITEM_TYPES:
                _fail(400, "invalid_request_error",
                      f"input item type '{it.get('type')}' is not supported when routing to chat upstream",
                      param="input")
            if isinstance(it, dict) and it.get("type") == "item_reference":
                _fail(400, "invalid_request_error",
                      "input item_reference is not supported (requires server-side store)",
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

    # include：reasoning.encrypted_content 在 chat 上游不可得，
    # 静默从 include 列表中剥除（兼容客户端默认带这个开关的场景），
    # 不再 400 阻断请求。
    include = body.get("include")
    if isinstance(include, list):
        stripped: list[str] = []
        kept: list[Any] = []
        for inc in include:
            if inc == "reasoning.encrypted_content":
                stripped.append(inc)
                continue
            kept.append(inc)
        if stripped:
            body["include"] = kept
            try:
                import logging
                logging.getLogger("parrot.openai").info(
                    "[guard] include items stripped for chat upstream: %s", stripped
                )
            except Exception:
                pass
