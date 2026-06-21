"""OpenAI Responses ↔ Anthropic Messages non-stream bridge.

Phase 8 fourth path: Responses ingress → Anthropic upstream, non-stream only.
This module intentionally composes the already-tested Responses↔Chat and
Chat↔Anthropic translators instead of duplicating the whole mapping table.

Compatibility policy matches CPA/cc-switch style request normalizers: preserve
input/function-call/tool-result content; map known controls; strip unsupported
Responses request hints and tool declarations.  Guard only stateful history or
content parts that would be corrupted if ignored.
"""

from __future__ import annotations

import json
import copy
from typing import Any, Optional

from ... import cache_hints
from . import chat_to_anthropic, common, guard, responses_to_chat


def _fail(message: str, *, param: str | None = None) -> None:
    raise guard.GuardError(400, "invalid_request_error", message, param=param)


def _custom_tool_label(body: dict) -> str | None:
    # Custom tool declarations are capabilities and can be stripped.  Historical
    # custom_tool_call items are conversation/tool state and must not be dropped.
    for item in _current_input_items_for_guard(body):
        if not isinstance(item, dict):
            continue
        typ = item.get("type")
        if typ == "custom_tool_call":
            if common.parse_json_object(item.get("input")) is None:
                return "custom_tool_call.input"
        if typ == "custom_tool_call_output":
            try:
                _guard_function_call_output_content(item.get("output"))
            except guard.GuardError:
                raise
    return None


def _item_reference_unresolved_label(body: dict) -> str | None:
    instructions = body.get("instructions")
    if isinstance(instructions, list):
        for item in instructions:
            if isinstance(item, dict) and item.get("type") == "item_reference":
                return "item_reference"
    inp = body.get("input")
    items = inp if isinstance(inp, list) else []
    known_ids: set[str] = set()
    has_history_anchor = bool(body.get("previous_response_id"))
    for item in items:
        if not isinstance(item, dict):
            continue
        typ = item.get("type")
        if typ == "item_reference":
            ref_id = item.get("id")
            if not isinstance(ref_id, str) or not ref_id:
                return "item_reference"
            if ref_id in known_ids or has_history_anchor:
                continue
            return "item_reference"
        item_id = item.get("id")
        if isinstance(item_id, str) and item_id:
            known_ids.add(item_id)
    return None


def _stateful_input_item_label(body: dict) -> str | None:
    unresolved_ref = _item_reference_unresolved_label(body)
    if unresolved_ref:
        return unresolved_ref
    for item in _current_input_items_for_guard(body):
        if not isinstance(item, dict):
            continue
        typ = item.get("type")
        if typ == "reasoning" and isinstance(item.get("encrypted_content"), str) and item.get("encrypted_content"):
            return "reasoning.encrypted_content"
        if typ in {
            "web_search_call", "file_search_call", "computer_call",
            "image_generation_call", "code_interpreter_call",
            "mcp_call", "mcp_list_tools", "mcp_approval_request",
            "mcp_approval_response", "local_shell_call", "local_shell_call_output",
        }:
            return str(typ)
    return None


def _guard_input_file_part(part: dict, *, param: str = "input") -> None:
    if part.get("file_id") is not None:
        _fail("file_id-backed files cannot be converted to Anthropic documents without file retrieval", param=param)
    file_data = part.get("file_data")
    file_url = part.get("file_url")
    has_file_data = isinstance(file_data, str) and bool(file_data)
    has_file_url = isinstance(file_url, str) and bool(file_url)
    if file_data is not None and not has_file_data:
        _fail("Responses input_file.file_data must be a non-empty string for Anthropic document conversion", param=param)
    if file_url is not None and not has_file_url:
        _fail("Responses input_file.file_url must be a non-empty string for Anthropic document conversion", param=param)
    if has_file_data and has_file_url:
        _fail("Responses input_file cannot contain both file_data and file_url for Anthropic document conversion", param=param)
    if not has_file_data and not has_file_url:
        _fail("Responses input_file requires non-empty file_data or file_url for Anthropic document conversion", param=param)
    if has_file_data and file_data.startswith("data:"):
        header, sep, encoded = file_data.partition(",")
        if not sep or ";base64" not in header or not encoded:
            _fail("Responses input_file.file_data data URL must be base64 encoded", param=param)


def _guard_input_image_part(part: dict, *, param: str = "input") -> None:
    if part.get("file_id") is not None:
        _fail("file_id-backed images cannot be converted to Anthropic images without file retrieval", param=param)
    url = part.get("image_url")
    if not isinstance(url, str) or not url:
        _fail("Responses input_image requires non-empty image_url for Anthropic image conversion", param=param)
    if url.startswith("data:"):
        header, sep, data = url.partition(",")
        if not sep or ";base64" not in header or not data:
            _fail("Responses input_image.image_url data URL must be base64 encoded", param=param)


def _guard_function_call_output_content(output: Any) -> None:
    if output is None or isinstance(output, str):
        return
    if not isinstance(output, list):
        return
    for part in output:
        if isinstance(part, str):
            continue
        if not isinstance(part, dict):
            _fail("Responses function_call_output output parts must be objects", param="input")
        typ = part.get("type")
        if typ in ("input_text", "output_text", "text"):
            continue
        if typ == "input_image":
            _guard_input_image_part(part, param="input")
            continue
        if typ == "input_file":
            _guard_input_file_part(part, param="input")
            continue
        if typ == "input_audio":
            _fail("audio tool output is not supported on Responses→Anthropic bridge yet", param="input")
        _fail(
            f"Responses function_call_output output part {typ!r} cannot be safely converted to Anthropic tool_result yet",
            param="input",
        )


def guard_request(body: dict, *, store_enabled: bool = True) -> None:
    if not isinstance(body, dict):
        _fail("request body must be a JSON object")
    # Request control hints such as background, reasoning/text.format/cache
    # fields, and unsupported service_tier values are stripped/fallback by the
    # bridge output allowlist. They do not change conversation content/tool
    # semantics for this target.
    include = body.get("include")
    if isinstance(include, list) and "reasoning.encrypted_content" in include:
        _fail(
            "Responses include reasoning.encrypted_content cannot be produced by Anthropic fallback",
            param="include",
        )
    # `conversation` is different: it names server-side state that this bridge
    # cannot load or replay, so align direct translator calls with the real
    # Responses ingress guard and reject non-null values instead of pretending
    # the conversation context was applied.
    if body.get("conversation"):
        _fail("conversation resource is not supported on Responses→Anthropic bridge", param="conversation")
    if body.get("background") is True:
        _fail("background async response is not supported on Responses→Anthropic bridge", param="background")
    custom_label = _custom_tool_label(body)
    if custom_label:
        _fail(
            f"Responses {custom_label} cannot be safely converted to Anthropic tool history yet",
            param="input",
        )
    stateful_label = _stateful_input_item_label(body)
    if stateful_label:
        _fail(
            f"Responses {stateful_label} history item cannot be safely converted to Anthropic Messages",
            param="input",
        )

    # Do not reuse the stricter Responses→Chat guard here: it is intentionally
    # designed for a strict OpenAI Chat upstream.  For Anthropic fallback we let
    # responses_to_chat skip/strip Responses-only state items, while
    # previous_response_id Store errors are still raised by _resolve_input().
    _ = store_enabled

    for item in _current_input_items_for_guard(body):
        if not isinstance(item, dict):
            continue
        typ = item.get("type")
        role = item.get("role")
        if typ == "reasoning":
            continue
        if typ == "function_call_output":
            _guard_function_call_output_content(item.get("output"))
        if typ == "input_image" and role != "user":
            _fail("Responses input_image is only supported in user messages on Anthropic bridge", param="input")
        if typ in ("input_file", "file"):
            _guard_input_file_part(item, param="input")
            _fail("Responses input_file is only supported inside user message content on Anthropic bridge", param="input")
        content = item.get("content")
        parts = content if isinstance(content, list) else []
        for part in parts:
            if not isinstance(part, dict):
                continue
            pt = part.get("type")
            if pt == "input_image" and role != "user":
                _fail("Responses input_image is only supported in user messages on Anthropic bridge", param="input")
            if pt == "input_image":
                _guard_input_image_part(part, param="input")
            if pt in ("input_file", "file"):
                _guard_input_file_part(part, param="input")
            if pt == "input_audio":
                _fail("audio input is not supported on Responses→Anthropic bridge yet", param="input")


def _current_input_items_for_guard(body: dict) -> list:
    items: list = []
    instructions = body.get("instructions")
    if isinstance(instructions, list):
        items.extend(instructions)
    cur = body.get("input")
    if isinstance(cur, str):
        items.append({"type": "message", "role": "user", "content": [{"type": "input_text", "text": cur}]})
        return items
    if isinstance(cur, list):
        items.extend(cur)
    return items


def resolve_current_input_items(body: dict) -> list:
    return responses_to_chat.resolve_current_input_items(body)


def _function_output_part_to_chat_tool_part(part: dict[str, Any]) -> dict[str, Any]:
    typ = part.get("type")
    if typ in ("input_text", "output_text", "text"):
        return {"type": "text", "text": str(part.get("text") or "")}
    if typ == "input_image":
        _guard_input_image_part(part, param="input")
        image_url: dict[str, Any] = {"url": part.get("image_url") or ""}
        if part.get("detail"):
            image_url["detail"] = part.get("detail")
        return {"type": "image_url", "image_url": image_url}
    if typ == "input_file":
        _guard_input_file_part(part, param="input")
        file_obj: dict[str, Any] = {}
        if part.get("file_data") is not None:
            file_obj["file_data"] = part.get("file_data")
        if part.get("file_url") is not None:
            file_obj["file_url"] = part.get("file_url")
        if part.get("filename"):
            file_obj["filename"] = part.get("filename")
        return {"type": "file", "file": file_obj}
    if typ == "input_audio":
        _fail("audio tool output is not supported on Responses→Anthropic bridge yet", param="input")
    _fail(
        f"Responses function_call_output output part {typ!r} cannot be safely converted to Anthropic tool_result yet",
        param="input",
    )
    return {"type": "text", "text": ""}


def _function_call_output_attachment_content(output: Any) -> tuple[Any, bool]:
    if not isinstance(output, list):
        return output, False
    has_attachment = False
    content: list[dict[str, Any]] = []
    for part in output:
        if isinstance(part, str):
            content.append({"type": "text", "text": part})
            continue
        if not isinstance(part, dict):
            _fail("Responses function_call_output output parts must be objects", param="input")
        typ = part.get("type")
        if typ in ("input_image", "input_file"):
            has_attachment = True
        content.append(_function_output_part_to_chat_tool_part(part))
    return content, has_attachment


def _function_call_output_attachment_replacements(input_items: list) -> list[tuple[str, Any]]:
    replacements: list[tuple[str, Any]] = []
    for item in input_items:
        if not isinstance(item, dict) or item.get("type") != "function_call_output":
            continue
        content, has_attachment = _function_call_output_attachment_content(item.get("output"))
        if has_attachment:
            replacements.append((str(item.get("call_id") or ""), content))
    return replacements


def _normalize_custom_tool_history(input_items: list) -> list:
    """Map safe Responses custom tool history to function-call history.

    Anthropic Messages has `tool_use.input` as an object.  A custom tool call
    with an object (or JSON object string) can be represented without losing
    data; arbitrary raw strings cannot, so they remain guarded.
    """
    out: list[Any] = []
    for item in input_items:
        if not isinstance(item, dict):
            out.append(item)
            continue
        typ = item.get("type")
        if typ == "custom_tool_call":
            input_obj = common.parse_json_object(item.get("input"))
            if input_obj is None:
                _fail(
                    "Responses custom_tool_call.input must be a JSON object to convert to Anthropic tool_use",
                    param="input",
                )
            normalized = copy.deepcopy(item)
            normalized["type"] = "function_call"
            normalized["arguments"] = json.dumps(input_obj, ensure_ascii=False, separators=(",", ":"))
            normalized.pop("input", None)
            out.append(normalized)
            continue
        if typ == "custom_tool_call_output":
            normalized = copy.deepcopy(item)
            normalized["type"] = "function_call_output"
            out.append(normalized)
            continue
        out.append(item)
    return out


def _preserve_function_call_output_attachments(chat_payload: dict, input_items: list) -> None:
    replacements = _function_call_output_attachment_replacements(input_items)
    if not replacements:
        return

    by_call_id: dict[str, list[Any]] = {}
    for call_id, content in replacements:
        by_call_id.setdefault(call_id, []).append(content)

    replaced = 0
    messages = chat_payload.get("messages")
    if isinstance(messages, list):
        for msg in reversed(messages):
            if not isinstance(msg, dict) or msg.get("role") != "tool":
                continue
            call_id = str(msg.get("tool_call_id") or "")
            queue = by_call_id.get(call_id)
            if not queue:
                continue
            msg["content"] = queue.pop()
            replaced += 1

    if replaced != len(replacements):
        _fail("Responses function_call_output attachments could not be preserved on Anthropic bridge", param="input")


def translate_request(body: dict, *, api_key_name: str = "", store_enabled: bool = True) -> dict:
    guard_request(body, store_enabled=store_enabled)
    bridge_body = dict(body)
    cache_control = cache_hints.anthropic_cache_control_from_openai(body)
    # Do not let the intermediate Responses→Chat payload reintroduce cache hints
    # as if they were user-supplied Chat fields; translate them once after
    # composition instead.
    bridge_body.pop("prompt_cache_key", None)
    bridge_body.pop("prompt_cache_retention", None)
    if isinstance(bridge_body.get("tools"), list):
        # Keep only function tools for the existing Responses→Chat→Anthropic
        # composition.  Hosted/custom tool declarations are capability hints;
        # dropping them is preferable to rejecting the request.
        bridge_body["tools"] = [
            tool for tool in bridge_body["tools"]
            if isinstance(tool, dict) and tool.get("type") in (None, "function")
        ]
    choice = bridge_body.get("tool_choice")
    if isinstance(choice, dict) and choice.get("type") not in (None, "function", "allowed_tools"):
        bridge_body.pop("tool_choice", None)
    input_items = responses_to_chat.resolve_input_items(bridge_body, api_key_name=api_key_name)
    input_items = _normalize_custom_tool_history(input_items)
    chat_payload = responses_to_chat.translate_request_from_input_items(bridge_body, input_items)
    _preserve_function_call_output_attachments(chat_payload, input_items)
    # chat_to_anthropic runs its own guard too; this is intentional because it
    # catches fields introduced by the Responses→Chat mapping (response_format,
    # reasoning_effort, etc.) before anything reaches Anthropic upstream.
    payload = chat_to_anthropic.translate_request(chat_payload, allow_file_url_documents=True)
    if cache_control and not payload.get("cache_control"):
        payload["cache_control"] = cache_control
    return common.filter_anthropic_bridge_payload(payload)


def translate_response(
    message: dict,
    *,
    model: str = "",
    previous_response_id: Optional[str] = None,
    api_key_name: Optional[str] = None,
    channel_key: Optional[str] = None,
    current_input_items: Optional[list] = None,
) -> dict:
    chat_obj = chat_to_anthropic.translate_response(message, model=model)
    return responses_to_chat.translate_response(
        chat_obj,
        model=model,
        previous_response_id=previous_response_id,
        api_key_name=api_key_name,
        channel_key=channel_key,
        current_input_items=current_input_items,
    )
