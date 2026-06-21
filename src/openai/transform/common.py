"""OpenAI 两套对话接口的通用工具：字段白名单、usage 归一、SSE 帧工具。

所有函数为纯函数、无 I/O；调用方按需组合。
"""

from __future__ import annotations

import json
from typing import Any

from ...providers.capabilities import (
    ANTHROPIC_BRIDGE_REQ_ALLOWED,
    ANTHROPIC_MESSAGES_REQ_ALLOWED,
    CHAT_REQ_ALLOWED,
    RESPONSES_REQ_ALLOWED,
)


# ─── Cross-family capability helpers ─────────────────────────────

_OPENAI_REASONING_EFFORTS: frozenset[str] = frozenset({"low", "medium", "high", "xhigh"})
_OPENAI_SERVICE_TIERS: frozenset[str] = frozenset({"auto", "default", "flex", "priority"})


def supports_reasoning_effort(model: str | None) -> bool:
    """Return whether an OpenAI-family model is expected to accept reasoning_effort.

    Mirrors cc-switch's current rule: OpenAI o-series and GPT-5+ models support
    the effort field.  Keep this deliberately conservative so non-reasoning
    OpenAI-compatible backends do not receive unknown fields.
    """
    name = str(model or "").strip().lower()
    if not name:
        return False
    if name.startswith("deepseek-v4"):
        return True
    if name.startswith("o") and len(name) > 1 and name[1].isdigit():
        return True
    if not name.startswith("gpt-"):
        return False
    rest = name[4:]
    return bool(rest and rest[0].isdigit() and rest[0] >= "5")


def resolve_anthropic_reasoning_effort(body: dict[str, Any] | None) -> str | None:
    """Map Anthropic thinking/output_config effort to OpenAI reasoning effort.

    Priority and thresholds intentionally follow cc-switch:
    - output_config.effort: low/medium/high pass through, max -> xhigh
    - thinking.type=adaptive -> xhigh
    - thinking.type=enabled uses budget_tokens: <4000 low, <16000 medium,
      otherwise high; no budget defaults to high.
    - disabled/unknown/absent does not produce an effort.
    """
    if not isinstance(body, dict):
        return None
    output_config = body.get("output_config")
    if isinstance(output_config, dict):
        raw_effort = output_config.get("effort")
        effort = str(raw_effort).strip().lower() if isinstance(raw_effort, str) else ""
        if effort == "max":
            return "xhigh"
        if effort in _OPENAI_REASONING_EFFORTS:
            return effort

    thinking = body.get("thinking")
    if not isinstance(thinking, dict):
        return None
    typ = str(thinking.get("type") or "").strip().lower()
    if typ == "adaptive":
        return "xhigh"
    if typ != "enabled":
        return None
    budget_raw = thinking.get("budget_tokens")
    try:
        budget = int(budget_raw) if budget_raw is not None else None
    except (TypeError, ValueError):
        budget = None
    if budget is None:
        return "high"
    if budget < 4_000:
        return "low"
    if budget < 16_000:
        return "medium"
    return "high"


def anthropic_reasoning_config_is_mappable(body: dict[str, Any] | None) -> bool:
    """Whether top-level Anthropic reasoning controls can be represented.

    Historical message `thinking` / `redacted_thinking` blocks are handled by
    the individual translators because those require stateful replay semantics;
    this helper only covers request-level controls.
    """
    if not isinstance(body, dict):
        return False
    has_reasoning_control = body.get("thinking") is not None or body.get("output_config") is not None
    return bool(has_reasoning_control and resolve_anthropic_reasoning_effort(body))


def anthropic_thinking_is_disabled(body: dict[str, Any] | None) -> bool:
    if not isinstance(body, dict):
        return False
    thinking = body.get("thinking")
    return isinstance(thinking, dict) and str(thinking.get("type") or "").strip().lower() == "disabled" and body.get("output_config") is None


def anthropic_context_management_is_ignorable(value: Any) -> bool:
    """Whether Anthropic context_management can be dropped on OpenAI bridges.

    Claude Code sends ``context_management.edits`` with clear_thinking_* entries
    when thinking is enabled.  OpenAI-family targets do not understand those
    Anthropic signed-thinking cleanup controls; dropping them is safe because
    historical thinking blocks are already guarded/stripped by the bridge.
    Unknown context-management shapes are not silently ignored.
    """
    if value is None:
        return True
    if not isinstance(value, dict):
        return False
    allowed_keys = {"edits"}
    if any(k not in allowed_keys for k in value.keys()):
        return False
    edits = value.get("edits")
    if edits in (None, []):
        return True
    if not isinstance(edits, list):
        return False
    for edit in edits:
        if not isinstance(edit, dict):
            return False
        typ = str(edit.get("type") or "")
        if not typ.startswith("clear_thinking_"):
            return False
    return True


def parse_json_object(value: Any) -> dict[str, Any] | None:
    """Return a JSON object from dict or JSON-object string, otherwise None."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except Exception:
            return None
        if isinstance(parsed, dict):
            return parsed
    return None


def map_anthropic_service_tier_to_openai(value: Any, *, codex_oauth: bool = False) -> tuple[bool, str | None]:
    """Map Anthropic service_tier to OpenAI service_tier semantics.

    Returns (recognized, mapped_value).  `None` mapped_value means the request is
    valid but the best equivalent is omission (notably Codex `standard_only`,
    where cc-switch simply disables fast/priority mode).
    """
    if value is None:
        return True, None
    if not isinstance(value, str):
        return False, None
    tier = value.strip().lower()
    if not tier:
        return True, None
    if tier == "auto":
        return True, "priority" if codex_oauth else "auto"
    if tier == "standard_only":
        return True, None if codex_oauth else "default"
    if tier in _OPENAI_SERVICE_TIERS:
        if codex_oauth and tier == "default":
            return True, None
        return True, tier
    return False, None


def map_openai_service_tier_to_anthropic(value: Any) -> tuple[bool, str | None]:
    """Map OpenAI service_tier intent to Anthropic service_tier.

    Only the safely equivalent latency intents are mapped.  OpenAI `flex` and
    `priority` imply provider-specific scheduling/fast-lane semantics that
    Anthropic Messages does not expose here, so compatibility bridges strip them
    instead of blocking the core request.
    """
    if value is None:
        return True, None
    if not isinstance(value, str):
        return False, None
    tier = value.strip().lower()
    if not tier:
        return True, None
    if tier == "auto":
        return True, "auto"
    if tier in ("default", "standard_only"):
        return True, "standard_only"
    return False, None


# ─── 请求字段白名单 ──────────────────────────────────────────────
#
# The canonical allowlists live in ``src.providers.capabilities`` so native
# passthrough and cross-family bridge egress filtering share the same provider
# capability metadata.  Re-export the names here for older transform callers.


def filter_chat_passthrough(body: dict) -> dict:
    """同协议 /v1/chat/completions 透传：保留白名单字段。"""
    return {k: v for k, v in body.items() if k in CHAT_REQ_ALLOWED}


def filter_responses_passthrough(body: dict) -> dict:
    """同协议 /v1/responses 透传：保留白名单字段。"""
    return {k: v for k, v in body.items() if k in RESPONSES_REQ_ALLOWED}


def filter_anthropic_bridge_payload(payload: dict) -> dict:
    """OpenAI-family → Anthropic bridge 出口：只保留当前安全可表达字段。

    注意这不是源请求字段白名单。源请求里的 provider-specific hint 可以被
    translator/adapter 安全剥离；真正会丢内容/状态的部分由 matrix/translator
    的语义 guard 负责。
    """
    if not isinstance(payload, dict):
        return {}
    return {k: v for k, v in payload.items() if k in ANTHROPIC_BRIDGE_REQ_ALLOWED}


# ─── SSE 帧工具 ──────────────────────────────────────────────────


def sse_frame_chat(obj: dict) -> bytes:
    """构造 `data: {json}\\n\\n` 一帧。用于 translator / 错误收尾。"""
    payload = json.dumps(obj, ensure_ascii=False)
    return f"data: {payload}\n\n".encode("utf-8")


def sse_frame_responses(event: str, obj: dict) -> bytes:
    """构造 `event: <name>\\ndata: {json}\\n\\n` 一帧。"""
    payload = json.dumps(obj, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n".encode("utf-8")


def sse_done_chat() -> bytes:
    """Chat SSE 终止帧。"""
    return b"data: [DONE]\n\n"


# ─── usage 归一 ──────────────────────────────────────────────────
#
# 与 src/upstream.py 的 extract_usage_*_json 保持一致形状（4 键 anthropic 风味），
# 供 handler / translator 共用。

def extract_usage_chat(obj: Any) -> dict:
    if not isinstance(obj, dict):
        return _zero()
    u = obj.get("usage") or {}
    details = u.get("prompt_tokens_details") or {}
    return {
        "input_tokens": int(u.get("prompt_tokens", 0) or 0),
        "output_tokens": int(u.get("completion_tokens", 0) or 0),
        "cache_creation": 0,
        "cache_read": int(details.get("cached_tokens", 0) or 0),
    }


def extract_usage_responses(obj: Any) -> dict:
    if not isinstance(obj, dict):
        return _zero()
    u = obj.get("usage") or {}
    in_details = u.get("input_tokens_details") or {}
    return {
        "input_tokens": int(u.get("input_tokens", 0) or 0),
        "output_tokens": int(u.get("output_tokens", 0) or 0),
        "cache_creation": 0,
        "cache_read": int(in_details.get("cached_tokens", 0) or 0),
    }


def _zero() -> dict:
    return {"input_tokens": 0, "output_tokens": 0, "cache_creation": 0, "cache_read": 0}


# ─── reasoning bridge 配置 ───────────────────────────────────────
#
# 两种模式：
#   - "passthrough"（默认）：在 chat ↔ responses 之间双向映射 reasoning 文本
#     - chat 侧通过非官方字段 `message.reasoning_content`（DeepSeek 等生态）
#     - responses 侧通过 reasoning item 的 summary_text
#   - "drop"：丢弃 reasoning 文本（usage.reasoning_tokens 不受影响，仍透传）
#
# encrypted_content：本 proxy 不处理加密推理（chat 无对应字段）；同协议
# passthrough 路径会原样转发，跨变体路径由 guard 在 include 里拦截。


# ─── ResponseUsage builder ───────────────────────────────────────
#
# spec: ResponseUsage（schemas_registry: ResponseUsage）required:
#   - input_tokens, input_tokens_details, output_tokens, output_tokens_details, total_tokens
# spec: input_tokens_details required: cached_tokens
# spec: output_tokens_details required: reasoning_tokens
#
# 之前各 _usage_* 函数在 cached/reasoning 为 0 时省略整段 details，导致严格客户端
# 反序列化失败（02-bug-findings #9）。本函数统一构造，cached/reasoning 默认 0。

def build_response_usage(*, input_tokens: int = 0, output_tokens: int = 0,
                          cached_tokens: int = 0, reasoning_tokens: int = 0,
                          total_tokens: int | None = None) -> dict:
    """按 spec ResponseUsage 构造 usage 字典；所有 required 字段始终写入。"""
    in_tok = int(input_tokens or 0)
    out_tok = int(output_tokens or 0)
    return {
        "input_tokens": in_tok,
        # spec: ResponseUsage.input_tokens_details required
        "input_tokens_details": {"cached_tokens": int(cached_tokens or 0)},
        "output_tokens": out_tok,
        # spec: ResponseUsage.output_tokens_details required
        "output_tokens_details": {"reasoning_tokens": int(reasoning_tokens or 0)},
        "total_tokens": int(total_tokens if total_tokens is not None else (in_tok + out_tok)),
    }


def build_chat_usage(*, prompt_tokens: int = 0, completion_tokens: int = 0,
                     cached_tokens: int = 0, reasoning_tokens: int = 0,
                     total_tokens: int | None = None) -> dict:
    """构造 chat 侧 CompletionUsage，details 字段也始终写入。

    spec: CompletionUsage 不强制 details required，但 02-bug-findings #9
    要求四处统一为 0 也写 details，避免严格客户端因缺字段反序列化失败。
    """
    p_tok = int(prompt_tokens or 0)
    c_tok = int(completion_tokens or 0)
    return {
        "prompt_tokens": p_tok,
        "completion_tokens": c_tok,
        "total_tokens": int(total_tokens if total_tokens is not None else (p_tok + c_tok)),
        "prompt_tokens_details": {"cached_tokens": int(cached_tokens or 0)},
        "completion_tokens_details": {"reasoning_tokens": int(reasoning_tokens or 0)},
    }


# ─── Response skeleton builder ──────────────────────────────────
#
# spec: Response required: id, object, created_at, error, incomplete_details,
#   instructions, model, tools, output, parallel_tool_calls, metadata,
#   tool_choice, temperature, top_p
# 02-bug-findings #13: 之前 stream_c2r._response_skeleton 只塞 9 个字段。

def build_response_skeleton(*, resp_id: str, model: str, created_at: int,
                             status: str,
                             previous_response_id: str | None = None,
                             request_body: dict | None = None) -> dict:
    """构造 spec-compliant Response 骨架（response.created/in_progress 携带）。

    透传 request_body 中的 tools/tool_choice/temperature/top_p/metadata/
    parallel_tool_calls/instructions/reasoning/text/truncation/store/prompt
    等字段；缺失时使用 spec 推荐默认值。
    """
    rb = request_body or {}
    return {
        "id": resp_id,
        "object": "response",
        "created_at": created_at,
        "status": status,
        "error": None,
        "incomplete_details": None,
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


# ─── ResponseError code mapping ─────────────────────────────────
#
# spec: ResponseError.code enum (从 schemas_registry: ResponseError):
#   server_error, rate_limit_exceeded, invalid_prompt,
#   vector_store_timeout, invalid_image, invalid_image_format,
#   invalid_base64_image, invalid_image_url, image_too_large,
#   image_too_small, image_parse_error, image_content_policy_violation,
#   invalid_image_mode, image_file_too_large,
#   unsupported_image_media_type, empty_image_file,
#   failed_to_download_image, image_file_not_found
# 02-bug-findings #8: chat 上游的 error.type 是 invalid_request_error /
# api_error / rate_limit_error 等完全不同 enum，必须做映射，
# 否则 ResponseError.code 落到非 enum 值时严格客户端反序列化失败。

RESPONSE_ERROR_CODES: frozenset[str] = frozenset({
    "server_error", "rate_limit_exceeded", "invalid_prompt",
    "vector_store_timeout", "invalid_image", "invalid_image_format",
    "invalid_base64_image", "invalid_image_url", "image_too_large",
    "image_too_small", "image_parse_error",
    "image_content_policy_violation", "invalid_image_mode",
    "image_file_too_large", "unsupported_image_media_type",
    "empty_image_file", "failed_to_download_image", "image_file_not_found",
})

_CHAT_TYPE_TO_RESP_CODE: dict[str, str] = {
    "rate_limit_error": "rate_limit_exceeded",
    "rate_limit_exceeded": "rate_limit_exceeded",
    "invalid_request_error": "invalid_prompt",
    "tokens_exceeded_error": "invalid_prompt",
    "context_length_exceeded": "invalid_prompt",
    "permission_error": "server_error",
    "authentication_error": "server_error",
    "api_error": "server_error",
    "server_error": "server_error",
    "overloaded_error": "server_error",
    "internal_server_error": "server_error",
}


def map_response_error_code(code: str | None, type_: str | None) -> str:
    """把 chat 风格的 error.code/type 映射到 spec ResponseError.code enum。

    优先 code（若已在 enum 中），其次按 type 映射，最后兜底 server_error。
    """
    if isinstance(code, str) and code in RESPONSE_ERROR_CODES:
        return code
    if isinstance(type_, str):
        mapped = _CHAT_TYPE_TO_RESP_CODE.get(type_)
        if mapped:
            return mapped
    return "server_error"


def reasoning_bridge_mode() -> str:
    """返回当前 reasoning 桥接模式。未设/非法值均回落 'passthrough'。"""
    try:
        # 延迟 import 避免 common.py 成为 config 依赖图的叶节点时循环
        from ... import config as _config
        raw = ((_config.get().get("openai") or {}).get("reasoningBridge") or "passthrough")
    except Exception:
        raw = "passthrough"
    mode = str(raw).lower().strip()
    if mode not in ("passthrough", "drop"):
        return "passthrough"
    return mode


def reasoning_passthrough_enabled() -> bool:
    return reasoning_bridge_mode() == "passthrough"
