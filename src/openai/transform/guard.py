"""Capability guard：在 ingress 入口 + upstream 选型阶段拦截无法完成的请求。

MS-2 只实现"同 ingress 自检"与"跨变体未实现"的拒绝路径：
  - Chat ingress：拒绝 `n>1` / `audio` 输出（本版本不支持，暂 400）
  - Responses ingress：拒绝 `background:true` / `conversation` 对象（首版不做）
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

    - background:true → 400（首版不支持异步模式）
    - conversation 对象 → 400（首版不支持 conversation 资源，仅 previous_response_id）
    - previous_response_id 带了但 Store 关闭 → 400

    跨变体特有的 built-in tools 等在上游选型阶段（OpenAIApiChannel.build_upstream_request
    或 MS-3 的 responses_to_chat.guard）再拦一次，这里只做 ingress 无关检查。
    """
    if not isinstance(body, dict):
        _fail(400, "invalid_request_error", "request body must be a JSON object")

    if body.get("background") is True:
        _fail(400, "invalid_request_error",
              "background responses are not supported by this proxy",
              param="background")

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
    responses API）。只拦住"拒绝更安全"的两类：
      - `n>1`（多候选）：responses 不原生支持（ingress guard 已拦，保留防御）
      - `logprobs/top_logprobs`：下游客户端可能强依赖，默认拒绝以免沉默性能降级
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


# Responses 的 tools 中非 function 类型枚举（官方 built-in）。
# 遇到这些工具时，chat 上游没有等价实现 → 400。
_BUILTIN_TOOL_TYPES = {
    "web_search_preview", "file_search", "computer_use_preview",
    "code_interpreter", "image_generation", "mcp", "local_shell",
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
      概念 → 400（避免客户端误以为拿到了）
    - `conversation` / `background`：由 guard_responses_ingress 已拒
    """
    if not isinstance(body, dict):
        _fail(400, "invalid_request_error", "request body must be a JSON object")

    # tools 检查
    tools = body.get("tools") or []
    if isinstance(tools, list) and reject_on_builtin_tools:
        for t in tools:
            if not isinstance(t, dict):
                continue
            ttype = t.get("type")
            if ttype and ttype != "function" and ttype in _BUILTIN_TOOL_TYPES:
                _fail(400, "invalid_request_error",
                      f"built-in tool '{ttype}' is not supported when routing to chat upstream",
                      param="tools")
            if ttype and ttype != "function" and ttype not in _BUILTIN_TOOL_TYPES:
                # 未知 type 但明显不是 function：保守拒绝
                _fail(400, "invalid_request_error",
                      f"unsupported tool type '{ttype}' when routing to chat upstream",
                      param="tools")

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

    # include：禁用 encrypted_content 的 include
    include = body.get("include")
    if isinstance(include, list):
        for inc in include:
            if inc in ("reasoning.encrypted_content",):
                _fail(400, "invalid_request_error",
                      f"include '{inc}' is not available when routing to chat upstream",
                      param="include")
