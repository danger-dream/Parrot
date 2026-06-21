"""Anthropic Messages ↔ OpenAI Responses bridge.

Phase 8 third path: Anthropic ingress → OpenAI Responses upstream.  Supported
now: text/image/document input, function tools, assistant tool_use, user
tool_result, and narrow streaming output translation.  Guarded for now:
thinking/reasoning, citation/stateful documents, built-in Anthropic tools, and
stateful Responses concepts.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from . import common
from .guard import GuardError
from ...protocols.usage import legacy_usage_from_openai_responses_json


def _gen_id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:24]}"


def _fail(message: str, *, param: str | None = None, scope: str = "request") -> None:
    raise GuardError(400, "invalid_request_error", message, param=param, scope=scope)


def _json_dumps(value: Any, *, sort_keys: bool = False) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=sort_keys)


def guard_request(body: dict) -> None:
    if not isinstance(body, dict):
        _fail("request body must be a JSON object")
    if body.get("thinking") is not None and not common.anthropic_thinking_is_disabled(body):
        if not common.anthropic_reasoning_config_is_mappable(body):
            _fail("Anthropic thinking cannot be mapped to Responses reasoning.effort", param="thinking")
    if body.get("output_config") is not None:
        if not common.anthropic_reasoning_config_is_mappable(body):
            _fail("Anthropic output_config.effort cannot be mapped to Responses reasoning.effort", param="output_config")
    if body.get("context_management") is not None and not common.anthropic_context_management_is_ignorable(body.get("context_management")):
        _fail("Anthropic context_management is not supported on Responses bridge yet", param="context_management")
    if body.get("container") is not None or body.get("mcp_servers") is not None:
        _fail("Anthropic built-in/container features are not supported on Responses bridge yet", param="tools")
    if body.get("service_tier") is not None:
        ok, _ = common.map_anthropic_service_tier_to_openai(body.get("service_tier"))
        if not ok:
            pass

    for msg in body.get("messages") or []:
        if not isinstance(msg, dict):
            _fail("messages must contain objects", param="messages")
        role = msg.get("role")
        if role not in ("system", "developer", "user", "assistant"):
            _fail(f"unsupported Anthropic message role for Responses bridge: {role!r}", param="messages")
        for block in _blocks(msg.get("content")):
            typ = block.get("type")
            if typ == "tool_result":
                _guard_tool_result_content(block)
                continue
            if typ == "tool_use":
                _tool_use_input(block)
                continue
            if typ in ("text", "image", "document", "tool_use"):
                continue
            if typ in ("thinking", "redacted_thinking"):
                _fail("thinking/redacted_thinking cannot be safely converted to Responses yet", param="messages")
            _fail(f"unsupported Anthropic content block for Responses bridge: {typ!r}", param="messages")

    for tool in body.get("tools") or []:
        if not isinstance(tool, dict):
            _fail("tools must contain objects", param="tools")
        # Anthropic function tools normally omit type. Built-ins carry a type
        # such as web_search_20250305; guard those until a hosted-tool bridge is
        # deliberately implemented.
        if tool.get("type") not in (None, "function"):
            _fail("Anthropic built-in tools are not supported on Responses bridge yet", param="tools")

    choice = body.get("tool_choice")
    if isinstance(choice, dict):
        typ = choice.get("type")
        if typ not in ("auto", "none", "any", "tool"):
            _fail(f"unsupported Anthropic tool_choice for Responses bridge: {typ!r}", param="tool_choice")


def _blocks(content: Any) -> list[dict[str, Any]]:
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    _fail("message content must be a string or content block array", param="messages")
    return []


def _tool_result_unsupported_label(block: dict[str, Any]) -> str | None:
    content = block.get("content")
    if content is None or isinstance(content, str):
        return None
    if isinstance(content, list):
        for item in content:
            if isinstance(item, str):
                continue
            if not isinstance(item, dict):
                return f"tool_result:{type(item).__name__}"
            typ = item.get("type")
            if typ in ("text", "image"):
                continue
            if typ == "document":
                doc_label = _tool_result_document_unsupported_label(item)
                if doc_label:
                    return f"tool_result:{doc_label}"
                continue
            if isinstance(item, dict):
                return f"tool_result:{typ or 'object'}"
            return f"tool_result:{type(item).__name__}"
        return None
    return f"tool_result:{type(content).__name__}"


def _tool_result_document_unsupported_label(block: dict[str, Any]) -> str | None:
    if _document_citations_enabled(block):
        return "document.citations"
    source = block.get("source")
    if not isinstance(source, dict):
        return "document.source"
    st = source.get("type")
    if st == "base64":
        data = source.get("data")
        return None if isinstance(data, str) and data else "document.base64"
    if st == "url":
        url = source.get("url")
        return None if isinstance(url, str) and url else "document.url"
    if st == "file":
        return "document.file"
    return f"document.{st or 'source'}"


def _guard_tool_result_content(block: dict[str, Any]) -> None:
    unsupported = _tool_result_unsupported_label(block)
    if unsupported:
        _fail(
            f"Anthropic {unsupported} cannot be safely converted to OpenAI Responses function_call_output",
            param="messages",
        )


def _tool_use_input(block: dict[str, Any]) -> dict[str, Any]:
    value = block.get("input")
    if isinstance(value, dict):
        return value
    _fail("Anthropic tool_use.input must be an object when routing to OpenAI Responses", param="messages")
    return {}


def _system_to_instructions(system: Any) -> str:
    if system is None:
        return ""
    if isinstance(system, str):
        return system
    parts: list[str] = []
    for block in _blocks(system):
        typ = block.get("type")
        if typ != "text":
            _fail("Anthropic system can only contain text on Responses bridge", param="system")
        text = str(block.get("text") or "")
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def _image_to_responses(block: dict[str, Any]) -> dict[str, Any]:
    source = block.get("source") or {}
    if not isinstance(source, dict):
        _fail("image source must be an object", param="messages")
    st = source.get("type")
    if st == "base64":
        media_type = str(source.get("media_type") or "application/octet-stream")
        data = str(source.get("data") or "")
        if not data:
            _fail("base64 image source is missing data", param="messages")
        return {"type": "input_image", "image_url": f"data:{media_type};base64,{data}", "detail": "auto"}
    if st == "url":
        url = str(source.get("url") or "")
        if not url:
            _fail("url image source is missing url", param="messages")
        return {"type": "input_image", "image_url": url, "detail": "auto"}
    _fail(f"unsupported Anthropic image source for Responses bridge: {st!r}", param="messages")
    return {}


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
        return [{"type": "input_text", "text": f"Document context: {context}"}]
    return []


def _document_to_responses_parts(block: dict[str, Any]) -> list[dict[str, Any]]:
    if _document_citations_enabled(block):
        _fail("document citations cannot be safely converted to OpenAI Responses file input", param="messages")
    source = block.get("source")
    if not isinstance(source, dict):
        _fail("document source must be an object for Responses bridge", param="messages")
    st = source.get("type")
    file_part: dict[str, Any] = {"type": "input_file"}
    if st == "base64":
        data = source.get("data")
        if not isinstance(data, str) or not data:
            _fail("base64 document source is missing data", param="messages")
        file_part["file_data"] = data
    elif st == "url":
        url = source.get("url")
        if not isinstance(url, str) or not url:
            _fail("url document source is missing url", param="messages")
        file_part["file_url"] = url
    elif st == "file":
        _fail("Anthropic file-backed documents cannot be converted to OpenAI Responses file input", param="messages")
    else:
        _fail(f"unsupported Anthropic document source for Responses bridge: {st!r}", param="messages")
    title = _document_title(block)
    if title:
        file_part["filename"] = title
    return _document_context_parts(block) + [file_part]


def _flush_message(items: list[dict[str, Any]], role: str, parts: list[dict[str, Any]]) -> None:
    if parts:
        items.append({"type": "message", "role": role, "content": list(parts)})
        parts.clear()


def _tool_result_output(block: dict[str, Any]) -> str | list[dict[str, Any]]:
    _guard_tool_result_content(block)
    content = block.get("content")
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        output_parts: list[dict[str, Any]] = []
        text_parts: list[str] = []
        has_attachment = False
        for item in content:
            if isinstance(item, str):
                text = item
                text_parts.append(text)
                output_parts.append({"type": "input_text", "text": text})
                continue
            typ = item.get("type") if isinstance(item, dict) else None
            if typ == "text":
                text = str(item.get("text") or "")
                text_parts.append(text)
                output_parts.append({"type": "input_text", "text": text})
            elif typ == "image":
                has_attachment = True
                output_parts.append(_image_to_responses(item))
            elif typ == "document":
                has_attachment = True
                output_parts.extend(_document_to_responses_parts(item))
            else:
                _fail(f"unsupported Anthropic tool_result content block for Responses bridge: {typ!r}", param="messages")
        if has_attachment:
            return output_parts
        return "".join(text_parts)
    return _json_dumps(content)


def _messages_to_input_items(messages: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            _fail("messages must contain objects", param="messages")
        role = str(msg.get("role") or "user")
        if role not in ("system", "developer", "user", "assistant"):
            _fail(f"unsupported Anthropic message role for Responses bridge: {role!r}", param="messages")
        parts: list[dict[str, Any]] = []
        for block in _blocks(msg.get("content")):
            typ = block.get("type")
            if typ == "text":
                content_type = "output_text" if role == "assistant" else "input_text"
                parts.append({"type": content_type, "text": str(block.get("text") or "")})
            elif typ == "image":
                if role != "user":
                    _fail("assistant image content is not supported on Responses bridge", param="messages")
                parts.append(_image_to_responses(block))
            elif typ == "document":
                if role != "user":
                    _fail("assistant document content is not supported on Responses bridge", param="messages")
                parts.extend(_document_to_responses_parts(block))
            elif typ == "tool_use":
                if role != "assistant":
                    _fail("tool_use content is only supported in assistant messages on Responses bridge", param="messages")
                _flush_message(items, role, parts)
                tool_input = _tool_use_input(block)
                call_id = str(block.get("id") or _gen_id("call_"))
                items.append({
                    "type": "function_call",
                    "id": f"fc_{call_id}",
                    "call_id": call_id,
                    "name": str(block.get("name") or ""),
                    "arguments": _json_dumps(tool_input, sort_keys=True),
                    "status": "completed",
                })
            elif typ == "tool_result":
                if role != "user":
                    _fail("tool_result content is only supported in user messages on Responses bridge", param="messages")
                _flush_message(items, role, parts)
                items.append({
                    "type": "function_call_output",
                    "call_id": str(block.get("tool_use_id") or ""),
                    "output": _tool_result_output(block),
                })
            elif typ in ("thinking", "redacted_thinking"):
                _fail("thinking/redacted_thinking cannot be safely converted to Responses yet", param="messages")
            else:
                _fail(f"unsupported Anthropic content block for Responses bridge: {typ!r}", param="messages")
        _flush_message(items, role, parts)
    return items


def _tools_to_responses(tools: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            _fail("tools must contain objects", param="tools")
        if tool.get("type") not in (None, "function"):
            _fail("Anthropic built-in tools are not supported on Responses bridge yet", param="tools")
        item: dict[str, Any] = {
            "type": "function",
            "name": str(tool.get("name") or ""),
            "parameters": tool.get("input_schema") if isinstance(tool.get("input_schema"), dict) else {"type": "object"},
        }
        if tool.get("description") is not None:
            item["description"] = str(tool.get("description") or "")
        out.append(item)
    return out


def _tool_choice_to_responses(choice: Any) -> Any:
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
        return {"type": "function", "name": str(choice.get("name") or "")}
    _fail(f"unsupported Anthropic tool_choice for Responses bridge: {typ!r}", param="tool_choice")
    return None


def _disable_parallel_tool_calls(choice: Any) -> bool:
    return isinstance(choice, dict) and choice.get("disable_parallel_tool_use") is True


def translate_request(
    body: dict,
    *,
    target_model: str | None = None,
    codex_oauth: bool = False,
) -> dict:
    guard_request(body)
    payload: dict[str, Any] = {
        "input": _messages_to_input_items(body.get("messages") or []),
        "stream": bool(body.get("stream")),
    }
    instructions = _system_to_instructions(body.get("system"))
    if instructions:
        payload["instructions"] = instructions
    if body.get("max_tokens") is not None:
        payload["max_output_tokens"] = body.get("max_tokens")
    if body.get("temperature") is not None:
        payload["temperature"] = body.get("temperature")
    if body.get("top_p") is not None:
        payload["top_p"] = body.get("top_p")
    if body.get("metadata") is not None:
        payload["metadata"] = body.get("metadata")
    effort = common.resolve_anthropic_reasoning_effort(body)
    if effort:
        if target_model and not common.supports_reasoning_effort(target_model):
            _fail(
                f"target OpenAI Responses model {target_model!r} does not support reasoning.effort",
                param="thinking",
                scope="candidate",
            )
        payload["reasoning"] = {"effort": effort}
    ok, service_tier = common.map_anthropic_service_tier_to_openai(
        body.get("service_tier"),
        codex_oauth=codex_oauth,
    )
    if service_tier:
        payload["service_tier"] = service_tier
    tools = _tools_to_responses(body.get("tools") or [])
    if tools:
        payload["tools"] = tools
    tool_choice = _tool_choice_to_responses(body.get("tool_choice"))
    if tool_choice is not None:
        payload["tool_choice"] = tool_choice
    if _disable_parallel_tool_calls(body.get("tool_choice")):
        payload["parallel_tool_calls"] = False
    return payload


def _parse_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        value = json.loads(raw)
    except Exception:
        return {"_raw": raw}
    return value if isinstance(value, dict) else {"_value": value}


def _gather_output_text(output: list[Any]) -> list[str]:
    parts: list[str] = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            if isinstance(content, dict) and content.get("type") == "output_text":
                text = content.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(content, dict) and content.get("type") == "refusal":
                refusal = content.get("refusal")
                if isinstance(refusal, str):
                    parts.append(refusal)
    return parts


def _response_stop_reason(resp: dict, *, has_tool_use: bool) -> str:
    status = resp.get("status")
    if status == "completed":
        return "tool_use" if has_tool_use else "end_turn"
    if status == "incomplete":
        reason = (resp.get("incomplete_details") or {}).get("reason")
        if reason in ("max_output_tokens", "max_tokens"):
            return "max_tokens"
        return "end_turn"
    if status in ("failed", "cancelled"):
        return "end_turn"
    return "tool_use" if has_tool_use else "end_turn"


def _anthropic_usage_from_responses(resp: dict) -> dict[str, int]:
    legacy = legacy_usage_from_openai_responses_json(resp)
    return {
        "input_tokens": legacy["input_tokens"],
        "output_tokens": legacy["output_tokens"],
        "cache_creation_input_tokens": legacy["cache_creation"],
        "cache_read_input_tokens": legacy["cache_read"],
    }


def translate_response(resp: dict, *, model: str = "") -> dict:
    output = resp.get("output") if isinstance(resp.get("output"), list) else []
    content: list[dict[str, Any]] = []

    text = resp.get("output_text")
    if isinstance(text, str) and text:
        content.append({"type": "text", "text": text})
    else:
        gathered = _gather_output_text(output)
        if gathered:
            content.append({"type": "text", "text": "".join(gathered)})

    for item in output:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "function_call":
            content.append({
                "type": "tool_use",
                "id": str(item.get("call_id") or item.get("id") or _gen_id("call_")),
                "name": str(item.get("name") or ""),
                "input": _parse_arguments(item.get("arguments")),
            })
        elif item.get("type") == "reasoning":
            # Do not expose reasoning summaries as Anthropic thinking until a
            # deliberate reasoning bridge + capability guard is enabled.
            continue

    has_tool_use = any(block.get("type") == "tool_use" for block in content)
    return {
        "id": str(resp.get("id") or _gen_id("msg_")),
        "type": "message",
        "role": "assistant",
        "model": model or str(resp.get("model") or ""),
        "content": content,
        "stop_reason": _response_stop_reason(resp, has_tool_use=has_tool_use),
        "stop_sequence": None,
        "usage": _anthropic_usage_from_responses(resp),
    }
