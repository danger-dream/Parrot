"""Anthropic Messages ↔ OpenAI Chat bridge.

Phase 8 first path: Anthropic ingress → OpenAI Chat upstream.  Supported now:
text messages, user image/document input, function tools, assistant tool_use,
user tool_result, and narrow streaming output translation.  Guarded for now:
thinking/redacted_thinking, unsupported block kinds, remote/stateful documents,
and non-function/built-in concepts.
"""

from __future__ import annotations

import json
from typing import Any

from . import common
from .guard import GuardError
from ... import local_web_tools
from ...protocols.usage import legacy_usage_from_openai_chat_json


def _fail(message: str, *, param: str | None = None, scope: str = "request") -> None:
    raise GuardError(400, "invalid_request_error", message, param=param, scope=scope)


def _blocks(content: Any) -> list[dict[str, Any]]:
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    _fail("message content must be a string or content block array", param="messages")
    return []


def _text_from_blocks(content: Any) -> str:
    parts: list[str] = []
    for block in _blocks(content):
        typ = block.get("type")
        if typ == "text":
            parts.append(str(block.get("text") or ""))
        elif typ in ("image", "document"):
            _fail(f"{typ} content is only supported in user messages when routing Anthropic to OpenAI Chat", param="messages")
        elif typ in ("thinking", "redacted_thinking"):
            _fail("thinking/redacted_thinking cannot be safely converted to OpenAI Chat yet", param="messages")
        else:
            _fail(f"unsupported Anthropic content block for Chat bridge: {typ!r}", param="messages")
    return "\n".join(p for p in parts if p)


def _tool_result_unsupported_label(block: dict[str, Any]) -> str | None:
    content = block.get("content")
    if content is None or isinstance(content, str):
        return None
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") in ("text", "tool_reference"):
                continue
            if isinstance(item, dict):
                return f"tool_result:{item.get('type') or 'object'}"
            return f"tool_result:{type(item).__name__}"
        return None
    return f"tool_result:{type(content).__name__}"


def _guard_tool_result_content(block: dict[str, Any]) -> None:
    unsupported = _tool_result_unsupported_label(block)
    if unsupported:
        _fail(
            f"Anthropic {unsupported} cannot be safely converted to OpenAI Chat tool output",
            param="messages",
        )


def _tool_use_input(block: dict[str, Any]) -> dict[str, Any]:
    value = block.get("input")
    if isinstance(value, dict):
        return value
    _fail("Anthropic tool_use.input must be an object when routing to OpenAI Chat", param="messages")
    return {}


def guard_request(body: dict, *, target_model: str | None = None) -> None:
    if not isinstance(body, dict):
        _fail("request body must be a JSON object")
    if (body.get("thinking") is not None or body.get("output_config") is not None) and not common.anthropic_thinking_is_disabled(body):
        if not common.anthropic_reasoning_config_is_mappable(body, target_model=target_model):
            _fail("Anthropic thinking/output_config cannot be mapped to OpenAI reasoning_effort", param="thinking")
    if body.get("context_management") is not None and not common.anthropic_context_management_is_ignorable(body.get("context_management")):
        _fail("Anthropic context_management is not supported on OpenAI Chat bridge yet", param="context_management")
    if body.get("container") is not None or body.get("mcp_servers") is not None:
        _fail("Anthropic container/MCP features are not supported on Chat bridge yet", param="tools")
    if body.get("service_tier") is not None:
        ok, _ = common.map_anthropic_service_tier_to_openai(body.get("service_tier"))
        if not ok:
            pass
    for msg in body.get("messages") or []:
        if not isinstance(msg, dict):
            _fail("messages must contain objects", param="messages")
        for block in _blocks(msg.get("content")):
            typ = block.get("type")
            if typ == "image" and msg.get("role") != "user":
                _fail("image content is only supported in user messages when routing Anthropic to OpenAI Chat", param="messages")
            if typ == "document" and msg.get("role") != "user":
                _fail("document content is only supported in user messages when routing Anthropic to OpenAI Chat", param="messages")
            if typ in ("thinking", "redacted_thinking"):
                _fail("thinking/redacted_thinking cannot be safely converted to OpenAI Chat yet", param="messages")
            if typ == "tool_result":
                _guard_tool_result_content(block)
            if typ == "tool_use":
                _tool_use_input(block)
            if typ not in ("text", "image", "document", "tool_use", "tool_result"):
                _fail(f"unsupported Anthropic content block for Chat bridge: {typ!r}", param="messages")

    for tool in body.get("tools") or []:
        if not isinstance(tool, dict):
            _fail("tools must contain objects", param="tools")
        # Anthropic function tools normally omit type. Web search/fetch server
        # tools are emulated by Parrot as local function tools backed by
        # AnySearch when routing to OpenAI-family upstreams.
        if tool.get("type") not in (None, "function") and not local_web_tools.is_anthropic_web_tool_type(tool.get("type")):
            _fail("Anthropic built-in/server tools are not supported on Chat bridge yet", param="tools")


def _convert_system(system: Any) -> list[dict[str, Any]]:
    if system is None:
        return []
    text = _text_from_blocks(system)
    return [{"role": "system", "content": text}] if text else []


def _tool_result_text(block: dict[str, Any]) -> str:
    _guard_tool_result_content(block)
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "tool_reference":
                parts.append(local_web_tools.tool_reference_text(item))
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or ""))
        return "\n".join(p for p in parts if p)
    return json.dumps(content, ensure_ascii=False, separators=(",", ":")) if content is not None else ""


def _image_to_chat_part(block: dict[str, Any]) -> dict[str, Any]:
    source = block.get("source")
    if not isinstance(source, dict):
        _fail("image source must be an object", param="messages")
    st = source.get("type")
    if st == "base64":
        data = source.get("data")
        if not isinstance(data, str) or not data:
            _fail("base64 image source is missing data", param="messages")
        media_type = str(source.get("media_type") or "application/octet-stream")
        url = f"data:{media_type};base64,{data}"
    elif st == "url":
        url = source.get("url")
        if not isinstance(url, str) or not url:
            _fail("url image source is missing url", param="messages")
    else:
        _fail(f"unsupported Anthropic image source for Chat bridge: {st!r}", param="messages")
    return {"type": "image_url", "image_url": {"url": url}}


def _document_citations_enabled(block: dict[str, Any]) -> bool:
    citations = block.get("citations")
    if citations is None:
        return False
    if isinstance(citations, dict):
        return citations.get("enabled") is True
    return bool(citations)


def _document_title(block: dict[str, Any]) -> str | None:
    title = block.get("title")
    if isinstance(title, str) and title:
        return title
    return None


def _document_context_parts(block: dict[str, Any]) -> list[dict[str, Any]]:
    context = block.get("context")
    if isinstance(context, str) and context.strip():
        return [{"type": "text", "text": f"Document context: {context}"}]
    return []


def _document_to_chat_parts(block: dict[str, Any]) -> list[dict[str, Any]]:
    if _document_citations_enabled(block):
        _fail("document citations cannot be safely converted to OpenAI Chat file input", param="messages")
    source = block.get("source")
    if not isinstance(source, dict):
        _fail("document source must be an object for Chat bridge", param="messages")
    st = source.get("type")
    file_obj: dict[str, Any] = {}
    if st == "base64":
        data = source.get("data")
        if not isinstance(data, str) or not data:
            _fail("base64 document source is missing data", param="messages")
        file_obj["file_data"] = data
    elif st == "url":
        _fail("url document sources cannot be converted to OpenAI Chat file input without file retrieval", param="messages")
    elif st == "file":
        _fail("Anthropic file-backed documents cannot be converted to OpenAI Chat file input", param="messages")
    else:
        _fail(f"unsupported Anthropic document source for Chat bridge: {st!r}", param="messages")
    title = _document_title(block)
    if title:
        file_obj["filename"] = title
    return _document_context_parts(block) + [{"type": "file", "file": file_obj}]


def _chat_user_content(parts: list[dict[str, Any]]) -> str | list[dict[str, Any]]:
    if not parts:
        return ""
    if all(part.get("type") == "text" for part in parts):
        return "\n".join(str(part.get("text") or "") for part in parts if part.get("text"))
    return list(parts)


def _convert_messages(messages: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            _fail("messages must contain objects", param="messages")
        role = msg.get("role")
        blocks = _blocks(msg.get("content"))
        if role in ("system", "developer"):
            text = _text_from_blocks(msg.get("content"))
            if text:
                out.append({"role": role, "content": text})
        elif role == "user":
            content_parts: list[dict[str, Any]] = []
            def flush_content() -> None:
                if content_parts:
                    out.append({"role": "user", "content": _chat_user_content(content_parts)})
                    content_parts.clear()
            for block in blocks:
                typ = block.get("type")
                if typ == "text":
                    text = str(block.get("text") or "")
                    if text:
                        content_parts.append({"type": "text", "text": text})
                elif typ == "image":
                    content_parts.append(_image_to_chat_part(block))
                elif typ == "document":
                    content_parts.extend(_document_to_chat_parts(block))
                elif typ == "tool_result":
                    flush_content()
                    out.append({
                        "role": "tool",
                        "tool_call_id": str(block.get("tool_use_id") or ""),
                        "content": _tool_result_text(block),
                    })
                else:
                    _fail(f"unsupported user block for Chat bridge: {typ!r}", param="messages")
            flush_content()
        elif role == "assistant":
            text_parts: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            for block in blocks:
                typ = block.get("type")
                if typ == "text":
                    text = str(block.get("text") or "")
                    if text:
                        text_parts.append(text)
                elif typ == "tool_use":
                    tool_input = _tool_use_input(block)
                    tool_calls.append({
                        "id": str(block.get("id") or ""),
                        "type": "function",
                        "function": {
                            "name": str(block.get("name") or ""),
                            "arguments": json.dumps(tool_input, ensure_ascii=False, separators=(",", ":")),
                        },
                    })
                else:
                    _fail(f"unsupported assistant block for Chat bridge: {typ!r}", param="messages")
            item: dict[str, Any] = {"role": "assistant", "content": "\n".join(text_parts) if text_parts else None}
            if tool_calls:
                item["tool_calls"] = tool_calls
            out.append(item)
        else:
            _fail(f"unsupported Anthropic message role for Chat bridge: {role!r}", param="messages")
    return out


def _web_tool_schema(kind: str) -> dict[str, Any]:
    if kind == "search":
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query", "minLength": 2},
                "allowed_domains": {"type": "array", "items": {"type": "string"}},
                "blocked_domains": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["query"],
            "additionalProperties": False,
        }
    return {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "HTTP(S) URL to fetch"},
            "prompt": {"type": "string", "description": "Optional instruction for how to use the fetched content"},
        },
        "required": ["url"],
        "additionalProperties": False,
    }


def _server_tool_to_chat(tool: dict[str, Any]) -> dict[str, Any] | None:
    typ = tool.get("type")
    name = str(tool.get("name") or "")
    if typ in local_web_tools.ANTHROPIC_WEB_SEARCH_TOOL_TYPES:
        return {
            "type": "function",
            "function": {
                "name": name or "web_search",
                "description": "Search the web. Executed locally by Parrot through AnySearch when needed.",
                "parameters": _web_tool_schema("search"),
            },
        }
    if typ in local_web_tools.ANTHROPIC_WEB_FETCH_TOOL_TYPES:
        return {
            "type": "function",
            "function": {
                "name": name or "web_fetch",
                "description": "Fetch a URL and return extracted page content. Executed locally by Parrot through AnySearch when needed.",
                "parameters": _web_tool_schema("fetch"),
            },
        }
    return None


def _convert_tools(tools: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            _fail("tools must contain objects", param="tools")
        server_tool = _server_tool_to_chat(tool)
        if server_tool is not None:
            out.append(server_tool)
            continue
        out.append({
            "type": "function",
            "function": {
                "name": str(tool.get("name") or ""),
                "description": str(tool.get("description") or ""),
                "parameters": tool.get("input_schema") if isinstance(tool.get("input_schema"), dict) else {"type": "object"},
            },
        })
    return out


def _convert_tool_choice(choice: Any) -> Any:
    if not isinstance(choice, dict):
        return None
    typ = choice.get("type")
    if typ == "auto":
        return "auto"
    if typ == "none":
        return "none"
    if typ == "any":
        return "required"
    if typ == "tool":
        return {"type": "function", "function": {"name": str(choice.get("name") or "")}}
    _fail(f"unsupported Anthropic tool_choice for Chat bridge: {typ!r}", param="tool_choice")
    return None


def _disable_parallel_tool_calls(choice: Any) -> bool:
    return isinstance(choice, dict) and choice.get("disable_parallel_tool_use") is True


def translate_request(body: dict, *, target_model: str | None = None) -> dict:
    guard_request(body, target_model=target_model)
    payload: dict[str, Any] = {
        "messages": _convert_system(body.get("system")) + _convert_messages(body.get("messages") or []),
        "stream": bool(body.get("stream")),
    }
    if body.get("max_output_tokens") is not None:
        payload["max_tokens"] = body.get("max_output_tokens")
    elif body.get("max_tokens") is not None:
        payload["max_tokens"] = body.get("max_tokens")
    if body.get("stop_sequences"):
        stop = body.get("stop_sequences")
        payload["stop"] = stop if isinstance(stop, list) else [stop]
    for key in ("temperature", "top_p", "metadata"):
        if body.get(key) is not None:
            payload[key] = body.get(key)
    effort = common.resolve_anthropic_reasoning_effort(body, target_model=target_model)
    if effort:
        if target_model and not common.supports_reasoning_effort(target_model):
            _fail(
                f"target OpenAI Chat model {target_model!r} does not support reasoning_effort",
                param="thinking",
                scope="candidate",
            )
        payload["reasoning_effort"] = effort
    ok, service_tier = common.map_anthropic_service_tier_to_openai(body.get("service_tier"))
    if service_tier:
        payload["service_tier"] = service_tier
    if common.anthropic_request_wants_openai_priority(body):
        payload["service_tier"] = "priority"
    tools = _convert_tools(body.get("tools") or [])
    if tools:
        payload["tools"] = tools
    tool_choice = _convert_tool_choice(body.get("tool_choice"))
    if tool_choice is not None:
        payload["tool_choice"] = tool_choice
    if _disable_parallel_tool_calls(body.get("tool_choice")) or (
        common.disable_parallel_tool_calls_for_local_web()
        and local_web_tools.request_declares_supported_tools(body)
    ):
        # Local web tools are executed inside Parrot.  Disable parallel tool
        # calls so the upstream model does not mix Parrot-handled WebSearch /
        # WebFetch calls with client-handled Claude Code tools in one turn.
        payload["parallel_tool_calls"] = False
    return payload


def _parse_tool_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        obj = json.loads(raw)
    except Exception:
        return {"_raw": raw}
    return obj if isinstance(obj, dict) else {"_value": obj}


def _stop_reason(finish_reason: str | None) -> str:
    if finish_reason == "tool_calls":
        return "tool_use"
    if finish_reason == "length":
        return "max_tokens"
    return "end_turn"


def translate_response(obj: dict, *, model: str = "") -> dict:
    choices = obj.get("choices") or []
    choice = choices[0] if choices else {}
    msg = (choice or {}).get("message") or {}
    content_blocks: list[dict[str, Any]] = []
    content = msg.get("content")
    if isinstance(content, str) and content:
        content_blocks.append({"type": "text", "text": content})
    refusal = msg.get("refusal")
    if isinstance(refusal, str) and refusal:
        content_blocks.append({"type": "text", "text": refusal})
    for tc in msg.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        content_blocks.append({
            "type": "tool_use",
            "id": str(tc.get("id") or ""),
            "name": str(fn.get("name") or ""),
            "input": _parse_tool_arguments(fn.get("arguments")),
        })
    legacy_usage = legacy_usage_from_openai_chat_json(obj)
    usage = {
        "input_tokens": legacy_usage["input_tokens"],
        "output_tokens": legacy_usage["output_tokens"],
        "cache_creation_input_tokens": legacy_usage["cache_creation"],
        "cache_read_input_tokens": legacy_usage["cache_read"],
    }
    return {
        "id": str(obj.get("id") or "msg_chat_bridge"),
        "type": "message",
        "role": "assistant",
        "model": model or str(obj.get("model") or ""),
        "content": content_blocks,
        "stop_reason": _stop_reason(choice.get("finish_reason")),
        "stop_sequence": None,
        "usage": usage,
    }
