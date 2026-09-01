"""OpenAI 两套对话接口的通用工具：字段白名单、usage 归一、SSE 帧工具。

所有函数为纯函数、无 I/O；调用方按需组合。
"""

from __future__ import annotations

import json
import re
from typing import Any

from ... import config
from ...providers.capabilities import (
    ANTHROPIC_BRIDGE_REQ_ALLOWED,
    ANTHROPIC_MESSAGES_REQ_ALLOWED,
    CHAT_REQ_ALLOWED,
    RESPONSES_REQ_ALLOWED,
)


# ─── Cross-family capability helpers ─────────────────────────────

_OPENAI_REASONING_EFFORTS: frozenset[str] = frozenset({"low", "medium", "high", "xhigh"})
_BIGMODEL_REASONING_EFFORTS: frozenset[str] = frozenset({
    "none", "minimal", "low", "medium", "high", "xhigh", "max",
})
# Explicit cross-family compatibility only. Native OpenAI/Codex paths treat
# catalog-advertised service-tier IDs as opaque safe tokens and never use this set.
_ANTHROPIC_TO_OPENAI_COMPAT_SERVICE_TIERS: frozenset[str] = frozenset({
    "auto", "default", "flex", "priority", "ultrafast",
})
_ANTHROPIC_FAST_MODE_BETA = "fast-mode-2026-02-01"
_PARROT_WANTS_FAST_MODE_KEY = "_parrot_wants_fast_mode"
_GLM_VERSION_RE = re.compile(r"^glm-(\d+)(?:\.(\d+))?")


def _protocol_bridge_cfg() -> dict[str, Any]:
    root = config.get().get("protocolBridge") or {}
    return root if isinstance(root, dict) else {}


def _anthropic_to_openai_cfg() -> dict[str, Any]:
    root = _protocol_bridge_cfg().get("anthropicToOpenAI") or {}
    return root if isinstance(root, dict) else {}


def _reasoning_cfg() -> dict[str, Any]:
    root = _anthropic_to_openai_cfg().get("reasoning") or {}
    return root if isinstance(root, dict) else {}


def _valid_effort(value: Any, default: str) -> str:
    effort = str(value or "").strip().lower()
    return effort if effort in _OPENAI_REASONING_EFFORTS else default


def _glm_version(model: str | None) -> tuple[int, int | None] | None:
    name = str(model or "").strip().lower()
    m = _GLM_VERSION_RE.match(name)
    if not m:
        return None
    major = int(m.group(1))
    minor = int(m.group(2)) if m.group(2) is not None else None
    return major, minor


def supports_bigmodel_thinking(model: str | None) -> bool:
    """Return whether a GLM model supports BigModel chat `thinking`.

    BigModel documents thinking support for GLM-5 family and GLM-4.5/4.6/4.7
    families. Keep this model-name based so generic OpenAI-compatible channels
    do not receive BigModel-only fields by accident.
    """
    ver = _glm_version(model)
    if ver is None:
        return False
    major, minor = ver
    if major >= 5:
        return True
    return major == 4 and minor is not None and minor >= 5


def supports_bigmodel_reasoning_effort(model: str | None) -> bool:
    """Return whether BigModel accepts top-level `reasoning_effort`.

    Per BigModel docs, `reasoning_effort` is supported by GLM-5.2 and newer.
    Older GLM thinking models still accept `thinking`, but not this knob.
    """
    ver = _glm_version(model)
    if ver is None:
        return False
    major, minor = ver
    if major > 5:
        return True
    return major == 5 and minor is not None and minor >= 2


def disable_parallel_tool_calls_for_local_web() -> bool:
    return _anthropic_to_openai_cfg().get("disableParallelToolCallsForLocalWeb", True) is not False


def _tier_mapping(section: str) -> dict[str, Any]:
    root = (_protocol_bridge_cfg().get("serviceTier") or {})
    if not isinstance(root, dict):
        return {}
    mapping = root.get(section) or {}
    return mapping if isinstance(mapping, dict) else {}


def _parse_beta_header(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, (list, tuple, set)):
        out: list[str] = []
        for item in value:
            out.extend(_parse_beta_header(item))
        return out
    return [x.strip() for x in str(value).split(",") if x.strip()]


def anthropic_request_wants_openai_priority(body: dict | None) -> bool:
    """Anthropic Fast mode maps to OpenAI service_tier=priority on bridges."""
    if not isinstance(body, dict):
        return False
    if body.get(_PARROT_WANTS_FAST_MODE_KEY) is True:
        return True
    if str(body.get("speed") or "").strip().lower() == "fast":
        return True
    for key in ("betas", "anthropic_beta", "anthropic-beta", "anthropic_betas", "_parrot_downstream_betas"):
        if _ANTHROPIC_FAST_MODE_BETA in set(_parse_beta_header(body.get(key))):
            return True
    return False


def openai_service_tier_requests_anthropic_fast(value: Any) -> bool:
    """OpenAI priority is the safe latency-equivalent of Anthropic Fast mode."""
    return isinstance(value, str) and value.strip().lower() == "priority"


def supports_reasoning_effort(model: str | None) -> bool:
    """Return whether an OpenAI-family model is expected to accept reasoning_effort.

    Mirrors cc-switch's current rule: OpenAI o-series and GPT-5+ models support
    the effort field.  Keep this deliberately conservative so non-reasoning
    OpenAI-compatible backends do not receive unknown fields.
    """
    name = str(model or "").strip().lower()
    if not name:
        return False
    if supports_bigmodel_reasoning_effort(name):
        return True
    if name.startswith("deepseek-v4"):
        return True
    if name.startswith("o") and len(name) > 1 and name[1].isdigit():
        return True
    if not name.startswith("gpt-"):
        return False
    rest = name[4:]
    return bool(rest and rest[0].isdigit() and rest[0] >= "5")


def resolve_anthropic_reasoning_effort(body: dict[str, Any] | None, *, target_model: str | None = None) -> str | None:
    """Map Anthropic thinking/output_config effort to OpenAI reasoning effort.

    Explicit ``output_config.effort`` is protocol data and is preserved without
    consulting the target model.  Provider/model capability compatibility is a
    later request-boundary responsibility.  Existing inferred defaults remain:
    adaptive uses the configured default (xhigh), while enabled budgets map to
    low/medium/high thresholds.

    ``target_model`` remains accepted for API compatibility but intentionally
    does not influence this protocol translation.
    """
    if not isinstance(body, dict):
        return None
    cfg = _reasoning_cfg()
    output_config = body.get("output_config")
    if isinstance(output_config, dict):
        raw_effort = output_config.get("effort")
        effort = str(raw_effort).strip().lower() if isinstance(raw_effort, str) else ""
        if effort in _BIGMODEL_REASONING_EFFORTS:
            return effort

    thinking = body.get("thinking")
    if not isinstance(thinking, dict):
        return None
    typ = str(thinking.get("type") or "").strip().lower()
    if typ == "adaptive":
        return _valid_effort(cfg.get("adaptiveEffort"), "xhigh")
    if typ != "enabled":
        return None
    budget_raw = thinking.get("budget_tokens")
    try:
        budget = int(budget_raw) if budget_raw is not None else None
    except (TypeError, ValueError):
        budget = None
    if budget is None:
        return _valid_effort(cfg.get("defaultEnabledEffort"), "high")
    thresholds = cfg.get("budgetThresholds")
    if isinstance(thresholds, list):
        for item in thresholds:
            if not isinstance(item, dict):
                continue
            effort = _valid_effort(item.get("effort"), "")
            if not effort:
                continue
            if "lt" not in item:
                return effort
            try:
                if budget < int(float(str(item.get("lt")).replace(",", "").strip())):
                    return effort
            except Exception:
                continue
    if budget < 4_000:
        return "low"
    if budget < 16_000:
        return "medium"
    return "high"


def anthropic_thinking_budget_tokens(body: dict[str, Any] | None) -> int | None:
    """Return the raw Anthropic thinking.budget_tokens when it is a valid int."""
    if not isinstance(body, dict):
        return None
    thinking = body.get("thinking")
    if not isinstance(thinking, dict):
        return None
    raw = thinking.get("budget_tokens")
    try:
        budget = int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None
    return budget if budget is not None and budget >= 0 else None


def anthropic_reasoning_config_is_mappable(body: dict[str, Any] | None, *, target_model: str | None = None) -> bool:
    """Whether top-level Anthropic reasoning controls can be represented.

    Historical message `thinking` / `redacted_thinking` blocks are handled by
    the individual translators because those require stateful replay semantics;
    this helper only covers request-level controls.
    """
    if not isinstance(body, dict):
        return False
    has_reasoning_control = body.get("thinking") is not None or body.get("output_config") is not None
    return bool(has_reasoning_control and resolve_anthropic_reasoning_effort(body, target_model=target_model))


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


def _schema_allows_string(schema: Any) -> bool:
    if not isinstance(schema, dict):
        return False
    typ = schema.get("type")
    if typ == "string":
        return True
    if isinstance(typ, list) and "string" in typ:
        return True
    for key in ("anyOf", "oneOf"):
        variants = schema.get(key)
        if isinstance(variants, list) and any(_schema_allows_string(v) for v in variants):
            return True
    return False


def optional_empty_string_fields_from_tool_schema(schema: Any) -> set[str]:
    """Return optional string fields where provider "" should mean omitted."""
    if not isinstance(schema, dict):
        return set()
    props = schema.get("properties")
    if not isinstance(props, dict):
        return set()
    required_raw = schema.get("required")
    required = {str(x) for x in required_raw} if isinstance(required_raw, list) else set()
    return {
        str(name)
        for name, prop_schema in props.items()
        if str(name) not in required and _schema_allows_string(prop_schema)
    }


def optional_empty_string_fields_by_tool_from_anthropic_tools(tools: Any) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        name = str(tool.get("name") or "")
        if not name:
            continue
        fields = optional_empty_string_fields_from_tool_schema(tool.get("input_schema"))
        if fields:
            out[name] = fields
    return out


def optional_empty_string_fields_by_tool_from_responses_tools(tools: Any) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        name = str(tool.get("name") or "")
        if not name:
            continue
        fields = optional_empty_string_fields_from_tool_schema(tool.get("parameters"))
        if fields:
            out[name] = fields
    return out


def normalize_tool_input_optional_empty_strings(
    tool_name: str | None,
    value: Any,
    optional_empty_string_fields_by_tool: dict[str, set[str]] | None = None,
) -> dict[str, Any]:
    """Normalize provider-emitted tool args using the original tool schema.

    OpenAI-family function calling may emit optional string arguments as
    ``""``.  For Claude/Anthropic-style tools, an omitted optional field and an
    explicit empty string are not equivalent; the latter can be invalid.  Remove
    only fields that are optional strings according to that tool's schema.
    Required fields and non-string fields are left untouched.
    """
    out = dict(value) if isinstance(value, dict) else {}
    fields = (optional_empty_string_fields_by_tool or {}).get(str(tool_name or ""), set())
    for field in list(fields):
        if out.get(field) == "":
            out.pop(field, None)
    return out


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
    mapping = _tier_mapping("anthropicToCodex" if codex_oauth else "anthropicToOpenAI")
    if tier in mapping:
        mapped = mapping.get(tier)
        return True, str(mapped) if mapped is not None else None
    if tier == "auto":
        return True, "priority" if codex_oauth else "auto"
    if tier == "standard_only":
        return True, None if codex_oauth else "default"
    if tier in _ANTHROPIC_TO_OPENAI_COMPAT_SERVICE_TIERS:
        if codex_oauth and tier == "default":
            return True, None
        return True, tier
    return False, None


def map_openai_service_tier_to_anthropic(value: Any) -> tuple[bool, str | None]:
    """Map OpenAI service_tier intent to Anthropic service_tier.

    Only the safely equivalent service_tier values are mapped here.  OpenAI
    `priority` is handled separately by
    `openai_service_tier_requests_anthropic_fast()` because Claude Fast mode is
    `speed=fast` + `anthropic-beta`, not Anthropic `service_tier`.  OpenAI
    `flex` still has no safe Anthropic equivalent and is stripped.
    """
    if value is None:
        return True, None
    if not isinstance(value, str):
        return False, None
    tier = value.strip().lower()
    if not tier:
        return True, None
    mapping = _tier_mapping("openaiToAnthropic")
    if tier in mapping:
        mapped = mapping.get(tier)
        return True, str(mapped) if mapped is not None else None
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


def normalize_chat_reasoning_alias(body: Any) -> None:
    """Canonicalize assistant ``reasoning`` strings at Chat ingress in place."""
    if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
        return
    for message in body["messages"]:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        canonical = message.get("reasoning_content")
        alias = message.get("reasoning")
        if not isinstance(canonical, str) and isinstance(alias, str):
            message["reasoning_content"] = alias
        message.pop("reasoning", None)


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
