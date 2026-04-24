"""OpenAI 两套对话接口的通用工具：字段白名单、usage 归一、SSE 帧工具。

所有函数为纯函数、无 I/O；调用方按需组合。
"""

from __future__ import annotations

import json
from typing import Any


# ─── 请求字段白名单 ──────────────────────────────────────────────
#
# 透传路径用：从下游请求体里只拷这些键给上游，把 proxy 内部字段（如 _api_key_name）
# 和上游不认的字段（如 previous_response_id 出现在 chat 上游时）过滤掉。
# 与官方文档对齐；新字段出现时在此处追加（MS-8 验收再扫一遍）。

CHAT_REQ_ALLOWED: frozenset[str] = frozenset({
    "model", "messages", "stream", "stream_options",
    "temperature", "top_p", "n",
    "max_completion_tokens", "max_tokens", "stop",
    "frequency_penalty", "presence_penalty",
    "logprobs", "top_logprobs", "logit_bias",
    "tools", "tool_choice", "parallel_tool_calls",
    # 已弃用但 openai-python SDK 仍保留的 legacy 字段；
    # 老客户端直接透传以保持语义，跨变体翻译不处理（反正 deprecated）
    "functions", "function_call",
    "response_format", "modalities", "audio",
    "store", "metadata", "seed", "prediction",
    "reasoning_effort", "verbosity", "web_search_options",
    "service_tier", "user", "safety_identifier",
    "prompt_cache_key", "prompt_cache_retention",
})


RESPONSES_REQ_ALLOWED: frozenset[str] = frozenset({
    "model", "input", "stream", "stream_options", "instructions",
    "previous_response_id", "conversation", "context_management",
    "include", "temperature", "top_p", "top_logprobs",
    "max_output_tokens", "max_tool_calls",
    "tools", "tool_choice", "parallel_tool_calls",
    "text", "reasoning", "truncation",
    "store", "metadata", "prompt", "background",
    "service_tier", "user", "safety_identifier",
    "prompt_cache_key", "prompt_cache_retention",
})


def filter_chat_passthrough(body: dict) -> dict:
    """同协议 /v1/chat/completions 透传：保留白名单字段。"""
    return {k: v for k, v in body.items() if k in CHAT_REQ_ALLOWED}


def filter_responses_passthrough(body: dict) -> dict:
    """同协议 /v1/responses 透传：保留白名单字段。"""
    return {k: v for k, v in body.items() if k in RESPONSES_REQ_ALLOWED}


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
