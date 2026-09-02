"""标准 Anthropic 请求转换（cc_mimicry=False 时使用）。

保留：
  - `system` 字段原样透传（不转 user+assistant 对）
  - cache_control 按 section 补缺（保留客户端断点，总数最多 4 个）

不做：
  - CC 伪装（cc_version / metadata / beta 头 / 工具名混淆 / CCH 签名）
"""

import json

from .. import cache_hints
from .cc_mimicry import (
    PARROT_WANTS_FAST_MODE_KEY,
    _normalize_messages_for_api,
    _strip_assistant_thinking_blocks,
    _strip_message_cache_control,
    _strip_tool_cache_control,
    apply_opus_adaptive_thinking,
)


def standard_transform(body: dict) -> dict:
    """把下游请求体转换为按 section 补齐 cache 断点的标准 Anthropic payload。

    入参 body 来自客户端；函数内不修改原对象。
    返回纯 dict（未序列化为 bytes）。
    """
    explicit_cache_control = cache_hints.has_anthropic_cache_control(body)
    messages = body.get("messages", [])
    messages = _normalize_messages_for_api(messages)
    messages = _strip_assistant_thinking_blocks(messages)
    if not explicit_cache_control:
        messages = _strip_message_cache_control(messages)

    payload: dict = {
        "model": body["model"],
        "messages": messages,
        "max_tokens": body.get("max_tokens", 4096),
        "stream": body.get("stream", False),
    }

    # system 字段先原样保留；最终按 section 补齐缺失的缓存断点。
    if "system" in body:
        payload["system"] = body["system"]

    top_cache = cache_hints.top_level_cache_control(body)
    if top_cache:
        payload["cache_control"] = top_cache

    # 可选字段透传
    for k in (
        "temperature", "top_p", "top_k", "stop_sequences",
        "thinking", "context_management", "output_config",
        "tool_choice", "metadata", "service_tier",
        "container", "mcp_servers",
    ):
        if k in body:
            payload[k] = body[k]

    # Claude Fast mode 必须同时带 body speed=fast 和 anthropic-beta。
    # Header 在 Channel 层补，这里只负责最终 Anthropic payload。
    if body.get(PARROT_WANTS_FAST_MODE_KEY) is True:
        payload["speed"] = "fast"
    elif "speed" in body:
        payload["speed"] = body["speed"]

    if body.get("tools"):
        tools = _strip_tool_cache_control(
            [dict(t) for t in body["tools"]],
            preserve_cache_control=explicit_cache_control,
        )
        payload["tools"] = tools

    cache_hints.apply_anthropic_block_cache_breakpoints(payload)
    cache_hints.promote_anthropic_cache_ttls_for_order(payload)
    apply_opus_adaptive_thinking(payload, body.get("model", ""))

    return payload


def serialize(payload: dict) -> bytes:
    """与 cc_mimicry.sign_body 保持相同的 JSON 序列化策略（紧凑、不转义 ASCII）。
    非 CC 伪装路径不做 CCH 签名，只做序列化。"""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
