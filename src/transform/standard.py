"""标准 Anthropic 请求转换（cc_mimicry=False 时使用）。

保留：
  - `system` 字段原样透传（不转 user+assistant 对）
  - cache_control 统一管理（剥离客户端 + 代理打 4 个 1h ephemeral 断点）

不做：
  - CC 伪装（cc_version / metadata / beta 头 / 工具名混淆 / CCH 签名）
"""

import json

from .cc_mimicry import (
    _strip_message_cache_control,
    _strip_tool_cache_control,
    add_cache_breakpoints,
)


def standard_transform(body: dict) -> dict:
    """把下游请求体转换为"标准 Anthropic 但打了 cache 断点"的 payload。

    入参 body 来自客户端；函数内不修改原对象。
    返回纯 dict（未序列化为 bytes）。
    """
    messages = body.get("messages", [])
    messages = _strip_message_cache_control(messages)
    messages = add_cache_breakpoints(messages)

    payload: dict = {
        "model": body["model"],
        "messages": messages,
        "max_tokens": body.get("max_tokens", 4096),
        "stream": body.get("stream", True),
    }

    # system 字段：原样保留；若是 list，末 block 打 ephemeral 1h 断点
    if "system" in body:
        user_system = body["system"]
        if isinstance(user_system, list) and user_system:
            sys_blocks = [dict(b) if isinstance(b, dict) else b for b in user_system]
            if isinstance(sys_blocks[-1], dict):
                sys_blocks[-1] = {
                    **sys_blocks[-1],
                    "cache_control": {"type": "ephemeral", "ttl": "1h"},
                }
            payload["system"] = sys_blocks
        else:
            # 字符串或空 list，原样
            payload["system"] = user_system

    # 可选字段透传
    for k in (
        "temperature", "top_p", "top_k", "stop_sequences",
        "thinking", "context_management", "output_config",
        "tool_choice", "metadata",
    ):
        if k in body:
            payload[k] = body[k]

    if body.get("tools"):
        tools = _strip_tool_cache_control([dict(t) for t in body["tools"]])
        tools[-1] = {
            **tools[-1],
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
        }
        payload["tools"] = tools

    return payload


def serialize(payload: dict) -> bytes:
    """与 cc_mimicry.sign_body 保持相同的 JSON 序列化策略（紧凑、不转义 ASCII）。
    非 CC 伪装路径不做 CCH 签名，只做序列化。"""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
