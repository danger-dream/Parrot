"""OpenAI Chat ↔ Anthropic Messages non-stream bridge.

Phase 8 second path: OpenAI Chat ingress → Anthropic upstream, non-stream only.
Supported now: text/image_url user content, file_data-backed documents,
function tools, legacy functions, assistant tool_calls, and tool messages.
Compatibility policy: preserve core messages/tool results; map known controls;
strip OpenAI/provider-specific request hints that Anthropic cannot represent.
Guard only content/history shapes that would otherwise corrupt the conversation
or tool chain (for example input audio or file_id/file_url without retrieval).
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from . import common
from .common import build_chat_usage
from .guard import GuardError
from ... import cache_hints
from ...protocols.usage import legacy_usage_from_anthropic_json


_TOOL_ID_BAD = re.compile(r"[^a-zA-Z0-9_-]")


def _fail(message: str, *, param: str | None = None) -> None:
    raise GuardError(400, "invalid_request_error", message, param=param)


def _sanitize_tool_use_id(value: Any) -> str:
    raw = str(value or "")
    sanitized = _TOOL_ID_BAD.sub("_", raw)
    return sanitized or f"toolu_{int(time.time() * 1000)}"


def guard_request(body: dict, *, allow_file_url_documents: bool = False) -> None:
    if not isinstance(body, dict):
        _fail("request body must be a JSON object")
    # Request control hints that have no Anthropic bridge field are stripped by
    # the output allowlist instead of guarded here.  Hard schema enforcement or
    # provider-specific policy must be modeled outside this generic fallback.
    # If both legacy function_call and tool_choice are present, prefer
    # tool_choice later and ignore the legacy hint.  This matches CPA-style
    # normalizers: unsupported/duplicate request controls should not block the
    # core message payload.
    # Response metadata / sampling hints with no Anthropic bridge field are
    # ignored: response_format/logprobs/top_logprobs, modalities/audio output
    # hints, penalties, seed, prediction, verbosity, logit_bias, store,
    # prompt-cache hints, unsupported service_tier values, etc.
    n = body.get("n")
    if isinstance(n, int) and n > 1:
        _fail("Chat n>1 requires multi-candidate aggregation and cannot be converted to one Anthropic message", param="n")
    modalities = body.get("modalities")
    if body.get("audio") is not None or (isinstance(modalities, list) and "audio" in modalities):
        _fail("Chat audio output cannot be converted to Anthropic Messages", param="modalities")

    for msg in body.get("messages") or []:
        if not isinstance(msg, dict):
            _fail("messages must contain objects", param="messages")
        role = msg.get("role")
        if role not in ("system", "developer", "user", "assistant", "tool"):
            _fail(f"unsupported Chat message role for Anthropic bridge: {role!r}", param="messages")
        if msg.get("audio") is not None:
            _fail("assistant audio references cannot be converted to Anthropic messages", param="messages")
        _guard_chat_content(
            msg.get("content"),
            role=role,
            allow_file_url_documents=allow_file_url_documents,
        )
        # Non-standard Chat reasoning_content is treated as provider-specific
        # history.  Without a signature/replay adapter the safe fallback is to
        # drop it, not reject the whole request.
        if msg.get("tool_calls") is not None and role != "assistant":
            _fail("tool_calls are only supported on assistant messages", param="messages")
        for tc in msg.get("tool_calls") or []:
            if not isinstance(tc, dict):
                _fail("tool_calls must contain objects on Chat→Anthropic bridge", param="messages")
            tc_type = tc.get("type")
            if tc_type == "function":
                continue
            if tc_type == "custom":
                custom = tc.get("custom") or {}
                if common.parse_json_object(custom.get("input")) is None:
                    _fail(
                        "custom tool_call input must be a JSON object for Chat→Anthropic bridge",
                        param="messages",
                    )
                continue
            _fail("only function or object-input custom tool_calls are supported on Chat→Anthropic bridge", param="messages")

    # Tool declarations are model capabilities, not conversation history.  Keep
    # function tools; skip OpenAI hosted/custom tool declarations that Anthropic
    # Messages cannot express through this bridge.

    for fn in body.get("functions") or []:
        if not isinstance(fn, dict) or not isinstance(fn.get("name"), str) or not fn.get("name"):
            continue

    # Unknown tool_choice/function_call forms are ignored by the conversion
    # helpers instead of turning the whole request into a 400.


def _guard_chat_content(content: Any, *, role: Any, allow_file_url_documents: bool = False) -> None:
    if content is None or isinstance(content, str):
        return
    if not isinstance(content, list):
        _fail("message content must be a string or content part array", param="messages")
    for part in content:
        if isinstance(part, str):
            continue
        if not isinstance(part, dict):
            _fail("message content parts must be objects", param="messages")
        typ = part.get("type")
        if typ == "text":
            continue
        if typ == "image_url":
            if role not in ("user", "tool"):
                _fail("image_url content is only supported in user/tool messages on Chat→Anthropic bridge", param="messages")
            continue
        if typ == "input_audio":
            _fail("input_audio content is not supported on Chat→Anthropic bridge", param="messages")
        if typ == "file":
            if role not in ("user", "tool"):
                _fail("file/document content is only supported in user/tool messages on Chat→Anthropic bridge", param="messages")
            _file_part_to_anthropic(part, allow_file_url_documents=allow_file_url_documents)
            continue
        _fail(f"unsupported Chat content part for Anthropic bridge: {typ!r}", param="messages")


def _image_url_to_anthropic(part: dict[str, Any]) -> dict[str, Any]:
    iu = part.get("image_url")
    if isinstance(iu, dict) and iu.get("file_id") is not None:
        _fail("file_id-backed images cannot be converted to Anthropic images without file retrieval", param="messages")
    url = iu.get("url") if isinstance(iu, dict) else iu
    url = str(url or "")
    if not url:
        _fail("image_url content part is missing url", param="messages")
    if url.startswith("data:"):
        header, sep, data = url.partition(",")
        if not sep or ";base64" not in header or not data:
            _fail("image_url data URL must be base64 encoded for Chat→Anthropic bridge", param="messages")
        media_type = header.split(";", 1)[0].removeprefix("data:") or "application/octet-stream"
        return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}
    return {"type": "image", "source": {"type": "url", "url": url}}


def _file_data_to_document(file_data: Any, *, filename: Any = None) -> dict[str, Any]:
    if not isinstance(file_data, str) or not file_data:
        _fail("file content requires non-empty file.file_data for Chat→Anthropic bridge", param="messages")

    media_type = "application/octet-stream"
    data = file_data
    if file_data.startswith("data:"):
        header, sep, encoded = file_data.partition(",")
        if not sep or ";base64" not in header:
            _fail("file.file_data data URL must be base64 encoded for Chat→Anthropic bridge", param="messages")
        media_type = header.split(";", 1)[0].removeprefix("data:") or media_type
        data = encoded
    if not data:
        _fail("file.file_data is missing base64 data for Chat→Anthropic bridge", param="messages")

    doc: dict[str, Any] = {
        "type": "document",
        "source": {"type": "base64", "media_type": media_type, "data": data},
    }
    if isinstance(filename, str) and filename:
        # Anthropic document blocks support an optional title. Preserve the
        # OpenAI filename here rather than silently dropping user-visible
        # document context.
        doc["title"] = filename
    return doc


def _file_url_to_document(file_url: Any, *, filename: Any = None) -> dict[str, Any]:
    if not isinstance(file_url, str) or not file_url:
        _fail("file content requires non-empty file.file_url for Chat→Anthropic bridge", param="messages")
    doc: dict[str, Any] = {
        "type": "document",
        "source": {"type": "url", "url": file_url},
    }
    if isinstance(filename, str) and filename:
        doc["title"] = filename
    return doc


def _file_part_to_anthropic(part: dict[str, Any], *, allow_file_url_documents: bool = False) -> dict[str, Any]:
    file_obj = part.get("file")
    if not isinstance(file_obj, dict):
        _fail("file content part must contain a file object for Chat→Anthropic bridge", param="messages")
    if file_obj.get("file_id") is not None:
        _fail("file_id-backed files cannot be converted to Anthropic documents without file retrieval", param="messages")
    file_url = file_obj.get("file_url")
    file_data = file_obj.get("file_data")
    if file_url is not None:
        if not allow_file_url_documents:
            _fail("file_url-backed files cannot be converted to Anthropic documents without remote fetch", param="messages")
        if isinstance(file_data, str) and file_data:
            _fail("file content cannot contain both file.file_url and file.file_data for Chat→Anthropic bridge", param="messages")
        return _file_url_to_document(file_url, filename=file_obj.get("filename"))
    return _file_data_to_document(file_data, filename=file_obj.get("filename"))


def _content_to_anthropic_blocks(
    content: Any,
    *,
    role: str,
    allow_file_url_documents: bool = False,
) -> list[dict[str, Any]]:
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        _fail("message content must be a string or content part array", param="messages")
    out: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, str):
            out.append({"type": "text", "text": part})
            continue
        if not isinstance(part, dict):
            _fail("message content parts must be objects", param="messages")
        typ = part.get("type")
        if typ == "text":
            out.append({"type": "text", "text": str(part.get("text") or "")})
        elif typ == "image_url":
            out.append(_image_url_to_anthropic(part))
        elif typ == "file":
            out.append(_file_part_to_anthropic(part, allow_file_url_documents=allow_file_url_documents))
        else:
            _fail(f"unsupported Chat content part for Anthropic bridge: {typ!r}", param="messages")
    return out


def _system_text_blocks(content: Any, *, allow_file_url_documents: bool = False) -> list[dict[str, str]]:
    blocks = _content_to_anthropic_blocks(
        content,
        role="system",
        allow_file_url_documents=allow_file_url_documents,
    )
    out: list[dict[str, str]] = []
    for block in blocks:
        if block.get("type") != "text":
            _fail("system/developer messages can only contain text on Chat→Anthropic bridge", param="messages")
        text = str(block.get("text") or "")
        if text:
            out.append({"type": "text", "text": text})
    return out


def _tool_result_content(
    content: Any,
    *,
    allow_file_url_documents: bool = False,
) -> str | list[dict[str, Any]]:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        blocks = _content_to_anthropic_blocks(
            content,
            role="tool",
            allow_file_url_documents=allow_file_url_documents,
        )
        if not blocks:
            return ""
        return blocks
    try:
        return json.dumps(content, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return str(content)


def _parse_tool_args(
    raw: Any,
    *,
    tool_name: str | None = None,
    optional_empty_string_fields_by_tool: dict[str, set[str]] | None = None,
) -> dict[str, Any]:
    if isinstance(raw, dict):
        return common.normalize_tool_input_optional_empty_strings(tool_name, raw, optional_empty_string_fields_by_tool)
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        value = json.loads(raw)
    except Exception:
        _fail("assistant tool_call function.arguments must be valid JSON object for Chat→Anthropic bridge", param="messages")
    if not isinstance(value, dict):
        _fail("assistant tool_call function.arguments must decode to a JSON object for Chat→Anthropic bridge", param="messages")
    return common.normalize_tool_input_optional_empty_strings(tool_name, value, optional_empty_string_fields_by_tool)


def _convert_messages(
    messages: Any,
    *,
    allow_file_url_documents: bool = False,
    optional_empty_string_fields_by_tool: dict[str, set[str]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], dict[str, str]]:
    out: list[dict[str, Any]] = []
    system: list[dict[str, str]] = []
    id_map: dict[str, str] = {}

    for msg in messages or []:
        if not isinstance(msg, dict):
            _fail("messages must contain objects", param="messages")
        role = str(msg.get("role") or "")
        if role in ("system", "developer"):
            system.extend(
                _system_text_blocks(
                    msg.get("content"),
                    allow_file_url_documents=allow_file_url_documents,
                )
            )
            continue

        if role == "tool":
            raw_id = str(msg.get("tool_call_id") or "")
            tool_use_id = id_map.get(raw_id) or _sanitize_tool_use_id(raw_id)
            id_map[raw_id] = tool_use_id
            out.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": _tool_result_content(
                        msg.get("content"),
                        allow_file_url_documents=allow_file_url_documents,
                    ),
                }],
            })
            continue

        if role not in ("user", "assistant"):
            _fail(f"unsupported Chat message role for Anthropic bridge: {role!r}", param="messages")

        content = _content_to_anthropic_blocks(
            msg.get("content"),
            role=role,
            allow_file_url_documents=allow_file_url_documents,
        )
        if role == "assistant":
            for tc in msg.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    _fail("tool_calls must contain objects on Chat→Anthropic bridge", param="messages")
                raw_id = str(tc.get("id") or "")
                tool_use_id = id_map.get(raw_id) or _sanitize_tool_use_id(raw_id)
                id_map[raw_id] = tool_use_id
                tc_type = tc.get("type")
                if tc_type == "custom":
                    custom = tc.get("custom") or {}
                    input_obj = common.parse_json_object(custom.get("input"))
                    if input_obj is None:
                        _fail(
                            "custom tool_call input must be a JSON object for Chat→Anthropic bridge",
                            param="messages",
                        )
                    content.append({
                        "type": "tool_use",
                        "id": tool_use_id,
                        "name": str(custom.get("name") or ""),
                        "input": input_obj,
                    })
                    continue
                if tc_type != "function":
                    _fail(
                        "only function or object-input custom tool_calls are supported on Chat→Anthropic bridge",
                        param="messages",
                    )
                fn = tc.get("function") or {}
                content.append({
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": str(fn.get("name") or ""),
                    "input": _parse_tool_args(
                        fn.get("arguments"),
                        tool_name=str(fn.get("name") or ""),
                        optional_empty_string_fields_by_tool=optional_empty_string_fields_by_tool,
                    ),
                })
        if not content:
            content = [{"type": "text", "text": ""}]
        out.append({"role": role, "content": content})

    if not out and system:
        out.append({"role": "user", "content": [{"type": "text", "text": ""}]})
    return out, system, id_map


def _convert_tools(tools: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for tool in tools or []:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        fn = tool.get("function") or {}
        name = str(fn.get("name") or "")
        if not name:
            continue
        anth_tool: dict[str, Any] = {
            "name": name,
            "input_schema": fn.get("parameters") if isinstance(fn.get("parameters"), dict) else {"type": "object"},
        }
        if fn.get("description") is not None:
            anth_tool["description"] = str(fn.get("description") or "")
        out.append(anth_tool)
    return out


def _convert_legacy_functions(functions: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for fn in functions or []:
        if not isinstance(fn, dict):
            continue
        name = str(fn.get("name") or "")
        if not name:
            continue
        anth_tool: dict[str, Any] = {
            "name": name,
            "input_schema": fn.get("parameters") if isinstance(fn.get("parameters"), dict) else {"type": "object"},
        }
        if fn.get("description") is not None:
            anth_tool["description"] = str(fn.get("description") or "")
        out.append(anth_tool)
    return out


def _tool_name_from_chat_tool(tool: dict[str, Any]) -> str:
    fn = tool.get("function") if isinstance(tool.get("function"), dict) else None
    if fn is not None and isinstance(fn.get("name"), str):
        return fn["name"]
    if isinstance(tool.get("name"), str):
        return tool["name"]
    return ""


def _apply_allowed_tools_choice(
    tools: list[dict[str, Any]],
    choice: dict[str, Any],
    *,
    parallel_tool_calls: Any = None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    nested = choice.get("allowed_tools") if isinstance(choice.get("allowed_tools"), dict) else {}
    raw_allowed = nested.get("tools") if isinstance(nested, dict) else None
    if not isinstance(raw_allowed, list) or not raw_allowed:
        _fail("tool_choice.allowed_tools must contain at least one function tool for Chat→Anthropic bridge", param="tool_choice")
    allowed_names: list[str] = []
    for tool in raw_allowed:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            _fail("tool_choice.allowed_tools only supports function tools on Chat→Anthropic bridge", param="tool_choice")
        name = _tool_name_from_chat_tool(tool)
        if name:
            allowed_names.append(name)
    if not allowed_names:
        _fail("tool_choice.allowed_tools must name at least one function tool for Chat→Anthropic bridge", param="tool_choice")

    by_name = {str(tool.get("name") or ""): tool for tool in tools}
    filtered = [by_name[name] for name in allowed_names if name in by_name]
    if not filtered:
        _fail("tool_choice.allowed_tools does not match any declared function tool for Chat→Anthropic bridge", param="tool_choice")
    mode = nested.get("mode") or "auto"
    if mode == "required":
        out = {"type": "any"}
    elif mode == "none":
        out = {"type": "none"}
    elif mode == "auto":
        out = {"type": "auto"}
    else:
        out = {"type": "auto"}
    if parallel_tool_calls is False:
        out["disable_parallel_tool_use"] = True
    return filtered, out


def _convert_tool_choice(choice: Any, *, parallel_tool_calls: Any = None) -> dict[str, Any] | None:
    out: dict[str, Any] | None = None
    if isinstance(choice, str):
        if choice == "auto":
            out = {"type": "auto"}
        elif choice == "none":
            out = {"type": "none"}
        elif choice == "required":
            out = {"type": "any"}
    elif isinstance(choice, dict):
        if choice.get("type") == "function":
            fn = choice.get("function") or {}
            out = {"type": "tool", "name": str(fn.get("name") or "")}
    elif parallel_tool_calls is False:
        out = {"type": "auto"}

    if out is not None and parallel_tool_calls is False:
        out = dict(out)
        out["disable_parallel_tool_use"] = True
    return out


def _convert_legacy_function_call(choice: Any, *, parallel_tool_calls: Any = None) -> dict[str, Any] | None:
    out: dict[str, Any] | None = None
    if isinstance(choice, str):
        if choice == "auto":
            out = {"type": "auto"}
        elif choice == "none":
            out = {"type": "none"}
    elif isinstance(choice, dict):
        name = str(choice.get("name") or "")
        if name:
            out = {"type": "tool", "name": name}

    if out is not None and parallel_tool_calls is False:
        out = dict(out)
        out["disable_parallel_tool_use"] = True
    return out


def translate_request(body: dict, *, allow_file_url_documents: bool = False) -> dict:
    guard_request(body, allow_file_url_documents=allow_file_url_documents)
    payload: dict[str, Any] = {"stream": bool(body.get("stream"))}

    messages, system_blocks, _ = _convert_messages(
        body.get("messages") or [],
        allow_file_url_documents=allow_file_url_documents,
        optional_empty_string_fields_by_tool=common.optional_empty_string_fields_by_tool_from_responses_tools(body.get("tools")),
    )
    payload["messages"] = messages
    if system_blocks:
        payload["system"] = system_blocks

    if body.get("max_completion_tokens") is not None:
        payload["max_tokens"] = body.get("max_completion_tokens")
    elif body.get("max_tokens") is not None:
        payload["max_tokens"] = body.get("max_tokens")

    if body.get("temperature") is not None:
        payload["temperature"] = body.get("temperature")
    if body.get("top_p") is not None:
        payload["top_p"] = body.get("top_p")
    if body.get("stop") is not None:
        stop = body.get("stop")
        payload["stop_sequences"] = stop if isinstance(stop, list) else [stop]

    _, service_tier = common.map_openai_service_tier_to_anthropic(body.get("service_tier"))
    if service_tier:
        payload["service_tier"] = service_tier
    if common.openai_service_tier_requests_anthropic_fast(body.get("service_tier")):
        payload["speed"] = "fast"

    tools = _convert_tools(body.get("tools") or [])
    tools.extend(_convert_legacy_functions(body.get("functions") or []))
    raw_tool_choice = body.get("tool_choice")
    tool_choice = None
    if isinstance(raw_tool_choice, dict) and raw_tool_choice.get("type") == "allowed_tools":
        tools, tool_choice = _apply_allowed_tools_choice(
            tools,
            raw_tool_choice,
            parallel_tool_calls=body.get("parallel_tool_calls"),
        )
    if tools:
        payload["tools"] = tools
    if tool_choice is None:
        if raw_tool_choice is not None:
            tool_choice = _convert_tool_choice(raw_tool_choice, parallel_tool_calls=body.get("parallel_tool_calls"))
        elif body.get("function_call") is not None:
            tool_choice = _convert_legacy_function_call(body.get("function_call"), parallel_tool_calls=body.get("parallel_tool_calls"))
        else:
            tool_choice = _convert_tool_choice(None, parallel_tool_calls=body.get("parallel_tool_calls"))
    if tool_choice is not None:
        tool_names = {str(tool.get("name") or "") for tool in tools}
        choice_type = tool_choice.get("type")
        if choice_type in ("any", "tool") and not tools:
            tool_choice = None
        elif choice_type == "tool" and tool_choice.get("name") not in tool_names:
            tool_choice = None
    if tool_choice is not None:
        payload["tool_choice"] = tool_choice

    metadata: dict[str, Any] = {}
    if isinstance(body.get("metadata"), dict):
        # Anthropic metadata is narrower than Chat metadata. Preserve user_id only.
        user_id = body["metadata"].get("user_id") or body["metadata"].get("user")
        if isinstance(user_id, str) and user_id:
            metadata["user_id"] = user_id[:256]
    if isinstance(body.get("user"), str) and body.get("user"):
        metadata.setdefault("user_id", str(body.get("user"))[:256])
    if isinstance(body.get("safety_identifier"), str) and body.get("safety_identifier"):
        metadata.setdefault("user_id", str(body.get("safety_identifier"))[:256])
    if metadata:
        payload["metadata"] = metadata

    cache_hints.apply_openai_cache_to_anthropic_payload(body, payload)

    return common.filter_anthropic_bridge_payload(payload)


def _stop_reason_to_finish_reason(stop_reason: str | None, *, has_tool_calls: bool = False) -> str:
    if stop_reason == "tool_use" or has_tool_calls:
        return "tool_calls"
    if stop_reason == "max_tokens":
        return "length"
    if stop_reason == "stop_sequence":
        return "stop"
    return "stop"


def _anthropic_usage_to_chat_usage(message: dict) -> dict:
    legacy = legacy_usage_from_anthropic_json(message)
    prompt_tokens = legacy["input_tokens"] + legacy["cache_creation"] + legacy["cache_read"]
    completion_tokens = legacy["output_tokens"]
    return build_chat_usage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cached_tokens=legacy["cache_read"],
        reasoning_tokens=0,
        total_tokens=prompt_tokens + completion_tokens,
    )


def translate_response(message: dict, *, model: str = "") -> dict:
    content_text: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for block in message.get("content") or []:
        if not isinstance(block, dict):
            continue
        typ = block.get("type")
        if typ == "text":
            text = block.get("text")
            if isinstance(text, str):
                content_text.append(text)
        elif typ == "tool_use":
            tool_calls.append({
                "id": str(block.get("id") or ""),
                "type": "function",
                "function": {
                    "name": str(block.get("name") or ""),
                    "arguments": json.dumps(
                        block.get("input") if isinstance(block.get("input"), dict) else {},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            })
        elif typ in ("thinking", "redacted_thinking"):
            # Do not leak Anthropic thinking into Chat messages until a deliberate
            # reasoning bridge with provider capability checks is enabled.
            continue

    chat_message: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(content_text) if content_text else None,
    }
    if tool_calls:
        chat_message["tool_calls"] = tool_calls

    created = int(time.time())
    msg_id = str(message.get("id") or "msg_anthropic_bridge")
    return {
        "id": f"chatcmpl-{msg_id.replace('msg_', '')}",
        "object": "chat.completion",
        "created": created,
        "model": model or str(message.get("model") or ""),
        "choices": [{
            "index": 0,
            "message": chat_message,
            "finish_reason": _stop_reason_to_finish_reason(message.get("stop_reason"), has_tool_calls=bool(tool_calls)),
            "logprobs": None,
        }],
        "usage": _anthropic_usage_to_chat_usage(message),
    }
