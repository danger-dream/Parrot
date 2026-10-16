"""Gemini generateContent ↔ OpenAI Responses codec for Antigravity.

Channel converts ingress Responses-like payloads into Cloud Code envelopes.
The adapter restores Gemini JSON / candidates SSE into standard Responses
JSON / SSE *before* Parrot's Responses toolkit sees the bytes.

Bare generateContent SSE uses incremental text, as in Google's public API.
Cloud Code's private stream contract is not established by our fixtures:
wrapped responses retain the old snapshot-aware compatibility path. Callers
with a verified contract can select delta or cumulative mode explicitly;
content prefixes never select the public protocol.
"""

from __future__ import annotations

import codecs
import copy
import hashlib
import json
import logging
import re
import time
import uuid
from typing import Any, Iterator

from ..oauth import antigravity as ag_provider
from . import antigravity_schema


_DATA_URL_RE = re.compile(r"^data:([^;,]+);base64,(.+)$", re.DOTALL)
SKIP_THOUGHT_SIGNATURE = "skip_thought_signature_validator"


def _gen_id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:24]}"


def _json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _as_json_str(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return "{}"
    try:
        return _json_dumps(value)
    except TypeError:
        return "{}"


def _parse_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {"_raw": raw}
        return parsed if isinstance(parsed, dict) else {"_raw": raw}
    return {}


def _part_thought_signature(part: dict) -> str:
    for key in ("thoughtSignature", "thought_signature", "encrypted_content"):
        value = part.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for nested_key in ("functionCall", "function_call", "functionResponse", "function_response"):
        nested = part.get(nested_key)
        if not isinstance(nested, dict):
            continue
        for key in ("thoughtSignature", "thought_signature"):
            value = nested.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _sanitize_thought_signatures(request: dict) -> None:
    """Only the first functionCall in a model turn may get the bypass."""
    contents = request.get("contents")
    if not isinstance(contents, list):
        return
    for content in contents:
        if not isinstance(content, dict) or content.get("role") != "model":
            continue
        parts = content.get("parts")
        if not isinstance(parts, list):
            continue
        first_function_call = True
        for part in parts:
            if not isinstance(part, dict):
                continue
            if part.get("functionResponse") or part.get("function_response"):
                part.pop("thoughtSignature", None)
                part.pop("thought_signature", None)
                continue
            if not (part.get("functionCall") or part.get("function_call")):
                continue
            if first_function_call:
                first_function_call = False
                if not _part_thought_signature(part):
                    part["thoughtSignature"] = SKIP_THOUGHT_SIGNATURE
                continue
            # Parallel siblings stay unsigned, matching native Gemini history.


def _sanitize_request_schemas(request: dict, *, model: str) -> None:
    require_placeholder = antigravity_schema.uses_antigravity_schema(model)
    preserve_json_schema = antigravity_schema.uses_json_schema(model)
    tools = request.get("tools")
    if isinstance(tools, list):
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            decls = tool.get("functionDeclarations") or tool.get("function_declarations")
            if not isinstance(decls, list):
                continue
            for decl in decls:
                if not isinstance(decl, dict):
                    continue
                for key in ("parameters", "parametersJsonSchema", "parameters_json_schema"):
                    if preserve_json_schema and key != "parameters":
                        continue  # do not flatten unions or discard JSON Schema constraints
                    schema = decl.get(key)
                    if isinstance(schema, dict):
                        decl[key] = antigravity_schema.clean_tool_schema(
                            schema, require_placeholder=require_placeholder,
                        )
    gen = request.get("generationConfig") or request.get("generation_config")
    if isinstance(gen, dict):
        for key in ("responseSchema", "responseJsonSchema", "response_schema", "response_json_schema"):
            if preserve_json_schema and key in ("responseJsonSchema", "response_json_schema"):
                continue
            schema = gen.get(key)
            if isinstance(schema, dict):
                gen[key] = antigravity_schema.clean_response_schema(schema)


def _delta_from_snapshot(previous: str, current: str) -> str:
    if current.startswith(previous):
        return current[len(previous):]
    return current


def _text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
            continue
        if not isinstance(item, dict):
            continue
        typ = str(item.get("type") or "")
        if typ in {"input_text", "output_text", "text", "summary_text"}:
            parts.append(str(item.get("text") or ""))
        elif typ == "refusal":
            parts.append(str(item.get("refusal") or item.get("text") or ""))
    return "".join(parts)


def _inline_data_from_url(url: str) -> dict[str, str] | None:
    text = str(url or "").strip()
    match = _DATA_URL_RE.match(text)
    if not match:
        return None
    return {"mimeType": match.group(1).strip() or "application/octet-stream", "data": match.group(2).strip()}


def _part_from_content_item(item: Any) -> dict[str, Any] | None:
    if isinstance(item, str):
        return {"text": item} if item else None
    if not isinstance(item, dict):
        return None
    typ = str(item.get("type") or "")
    if typ in {"input_text", "output_text", "text", "summary_text", ""}:
        text = str(item.get("text") or "")
        return {"text": text} if text else None
    if typ == "refusal":
        text = str(item.get("refusal") or item.get("text") or "")
        return {"text": text} if text else None
    if typ in {"input_image", "output_image", "image_url"}:
        image = item.get("image_url") if isinstance(item.get("image_url"), dict) else item
        url = ""
        if isinstance(image, dict):
            url = str(image.get("url") or image.get("image_url") or "")
        inline = _inline_data_from_url(url)
        if inline:
            return {"inlineData": inline}
    file_part = _file_part_from_content_item(item, typ)
    if file_part:
        return file_part
    if item.get("inlineData") or item.get("inline_data"):
        raw = item.get("inlineData") or item.get("inline_data")
        if isinstance(raw, dict) and raw.get("data"):
            return {
                "inlineData": {
                    "mimeType": str(raw.get("mimeType") or raw.get("mime_type") or "application/octet-stream"),
                    "data": str(raw.get("data") or ""),
                }
            }
    return None


def _mime_from_filename(name: str) -> str:
    lowered = str(name or "").strip().lower()
    if lowered.endswith(".pdf"):
        return "application/pdf"
    if lowered.endswith(".png"):
        return "image/png"
    if lowered.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if lowered.endswith(".webp"):
        return "image/webp"
    if lowered.endswith(".gif"):
        return "image/gif"
    if lowered.endswith(".txt"):
        return "text/plain"
    if lowered.endswith((".html", ".htm")):
        return "text/html"
    return "application/octet-stream"


def _file_part_from_content_item(item: dict, typ: str) -> dict[str, Any] | None:
    if typ not in {"input_file", "file"} and not item.get("file_data") and not item.get("file_url"):
        return None
    filename = str(item.get("filename") or item.get("name") or "")
    file_data = item.get("file_data") or item.get("fileData")
    if isinstance(file_data, str) and file_data.strip():
        data = file_data.strip()
        inline = _inline_data_from_url(data)
        if inline:
            return {"inlineData": inline}
        return {"inlineData": {"mimeType": _mime_from_filename(filename), "data": data}}
    file_url = item.get("file_url") or item.get("fileUrl")
    if isinstance(file_url, str) and file_url.strip():
        url = file_url.strip()
        inline = _inline_data_from_url(url)
        if inline:
            return {"inlineData": inline}
        return {"fileData": {"mimeType": _mime_from_filename(filename), "fileUri": url}}
    return None


def _tool_output_parts(output: Any) -> tuple[dict, list[dict]]:
    """Extract only protocol content parts, never image-shaped tool JSON data."""
    from ..openai.transform.guard import GuardError
    from . import remote_image

    images = []
    if isinstance(output, list) and any(
        isinstance(part, dict) and part.get("type") in {"input_image", "image_url", "input_file", "file"}
        for part in output
    ):
        texts = []
        for part in output:
            if isinstance(part, str):
                texts.append(part)
            elif isinstance(part, dict) and part.get("type") in {"input_text", "output_text", "text"}:
                texts.append(str(part.get("text") or ""))
            elif isinstance(part, dict) and part.get("type") in {"input_image", "image_url"}:
                image = _part_from_content_item(part)
                inline = (image or {}).get("inlineData")
                if not inline:
                    raise GuardError(400, "invalid_request_error", "Antigravity tool images require image data or a supported HTTPS URL", scope="candidate")
                remote_image.validate_inline_tool_image(inline)
                images.append(image)
            else:
                raise GuardError(400, "invalid_request_error", "Antigravity tool results support text and images, not opaque files or unknown content parts", scope="candidate")
        return {"result": "\n".join(texts)}, images
    if isinstance(output, str):
        try:
            response = json.loads(output)
        except json.JSONDecodeError:
            response = {"result": output}
    else:
        response = output
    return response if isinstance(response, dict) else {"result": response}, images


def _thinking_config(payload: dict) -> dict[str, Any] | None:
    from ..antigravity import argon
    model = str(payload.get("model") or "")
    if argon.is_argon(model):
        return argon.thinking_config(model, payload)
    reasoning = payload.get("reasoning")
    if not isinstance(reasoning, dict):
        return None
    effort = str(reasoning.get("effort") or "").strip().lower()
    if not effort or effort == "none":
        return None
    model = str(payload.get("model") or "")
    if "claude" in model.lower():
        return _claude_thinking_budget(payload, reasoning, effort)
    mapping = {
        "minimal": "minimal",
        "low": "low",
        "medium": "medium",
        "high": "high",
        "xhigh": "high",
    }
    level = mapping.get(effort)
    if not level:
        return None
    return {"thinkingLevel": level}


def _claude_thinking_budget(payload: dict, reasoning: dict, effort: str) -> dict[str, Any] | None:
    raw = reasoning.get("budget_tokens")
    try:
        budget = int(raw) if raw is not None else None
    except (TypeError, ValueError):
        budget = None
    if budget is None:
        budget = {
            "minimal": 1024,
            "low": 2048,
            "medium": 8192,
            "high": 16384,
            "xhigh": 32768,
        }.get(effort)
    if budget is None:
        return None
    max_out = payload.get("max_output_tokens")
    try:
        max_tokens = int(max_out) if max_out is not None else 64000
    except (TypeError, ValueError):
        max_tokens = 64000
    if max_tokens > 0 and budget >= max_tokens:
        budget = max_tokens - 1
    if budget < 1024:
        return None
    return {"thinkingBudget": min(budget, 64000)}


def _structured_output(payload: dict) -> tuple[str | None, dict | None]:
    text = payload.get("text")
    if not isinstance(text, dict):
        return None, None
    fmt = text.get("format")
    if not isinstance(fmt, dict):
        return None, None
    typ = str(fmt.get("type") or "").strip().lower()
    if typ in {"json_object", "json_schema"}:
        schema = fmt.get("schema") if isinstance(fmt.get("schema"), dict) else None
        if schema is None and isinstance(fmt.get("json_schema"), dict):
            schema = fmt["json_schema"].get("schema") if isinstance(fmt["json_schema"].get("schema"), dict) else None
        return "application/json", schema
    return None, None


def _tool_choice_config(tool_choice: Any, names: list[str]) -> dict[str, Any] | None:
    if tool_choice is None:
        return None
    if isinstance(tool_choice, str):
        lowered = tool_choice.strip().lower()
        if lowered in {"auto", "none", "required", "any"}:
            mode = {"auto": "AUTO", "none": "NONE", "required": "ANY", "any": "ANY"}[lowered]
            return {"mode": mode}
        return None
    if isinstance(tool_choice, dict):
        typ = str(tool_choice.get("type") or "")
        if typ in {"function", "allowed_tools"}:
            name = str((tool_choice.get("function") or {}).get("name") or tool_choice.get("name") or "")
            if name:
                return {"mode": "ANY", "allowedFunctionNames": [name]}
            if names:
                return {"mode": "ANY", "allowedFunctionNames": names}
        if typ == "none":
            return {"mode": "NONE"}
        if typ in {"auto", "required", "any"}:
            return {"mode": "ANY" if typ != "auto" else "AUTO"}
    return None


def prepare_responses_request(payload: dict, *, api_key_name: str = "") -> tuple[dict, Any]:
    """Resolve local Responses state and flatten ordinary namespace functions.

    Reuse the tenant-checked Store/reference resolver and reversible tool-name
    map rather than pretending Gemini has OpenAI's server-side state. This is
    a provider-boundary operation, before the target allowlist removes fields.
    """
    from ..openai.transform import guard, responses_to_chat, responses_to_anthropic as bridge

    def fail(message: str, param: str) -> None:
        raise guard.GuardError(400, "invalid_request_error", message, param=param, scope="candidate")

    for key in ("conversation", "background"):
        if payload.get(key):
            fail(f"Antigravity has no OpenAI {key} backend; use local previous_response_id or explicit input", key)
    prepared = copy.deepcopy(payload)
    items = responses_to_chat.resolve_input_items(payload, api_key_name=api_key_name)
    for item in items:
        if not isinstance(item, dict):
            fail("Gemini input items must be objects", "input")
        typ = item.get("type") or ("message" if item.get("role") else "")
        if typ not in {"message", "reasoning", "function_call", "function_call_output"}:
            fail(f"Antigravity cannot replay OpenAI input type {typ!r} as Gemini content", "input")
        if typ == "function_call" and not _complete_call(item):
            fail("Cannot replay malformed function_call arguments as Gemini Struct", "input")
        if typ == "message" and isinstance(item.get("content"), list):
            for part in item["content"]:
                if not isinstance(part, dict):
                    continue
                nested = part.get("file") if isinstance(part.get("file"), dict) else {}
                if part.get("file_id") or nested.get("file_id"):
                    fail("OpenAI file_id has no local Gemini file content; provide file_data or file_url", "input")
    tools = prepared.get("tools") or []
    # Only ordinary client functions are promised here. Hosted tools and
    # custom/grammar tools need their own execution/validation contract.
    for tool in tools:
        if not isinstance(tool, dict):
            fail("Gemini tools must be objects", "tools")
        typ = tool.get("type") or "function"
        if typ == "namespace":
            if any(not isinstance(child, dict) or child.get("type", "function") != "function"
                   for child in tool.get("tools") or []):
                fail("Antigravity namespace children must be ordinary function tools", "tools")
            if tool.get("description"):
                for child in tool.get("tools") or []:
                    child["description"] = str(tool["description"]) + "\n" + str(child.get("description") or "")
        elif typ == "custom":
            fail("Antigravity requires JSON-schema function tools; custom tool format conversion is not implemented", "tools")
        elif typ != "function":
            fail(f"Antigravity has no execution backend for OpenAI tool type {typ!r}", "tools")
    plan = bridge.NamespaceToolMap()
    prepared["tools"] = bridge._flatten_response_tools(tools, plan)
    prepared["tool_choice"] = bridge._map_tool_choice(prepared.get("tool_choice"), plan)
    choice = prepared["tool_choice"]
    if isinstance(choice, dict) and choice.get("type") == "allowed_tools":
        selected = {tool["name"] for tool in choice["tools"]}
        prepared["tools"] = [tool for tool in prepared["tools"] if tool["name"] in selected]
        mode = choice.get("mode", "auto")
        if mode not in {"auto", "required"}:
            fail("allowed_tools mode must be auto or required", "tool_choice")
        # Gemini's AUTO has no reliable subset restriction; expose exactly the
        # permitted declarations instead of silently widening allowed tools.
        prepared["tool_choice"] = mode
    prepared["input"] = bridge._map_namespaced_history(items, plan)
    prepared.pop("previous_response_id", None)
    return prepared, plan


def _restore_tool_identity(item: dict, plan: Any = None) -> dict:
    if plan is None:
        return item
    from ..openai.transform.responses_to_anthropic import restore_output_item
    return restore_output_item(item, plan)


def responses_to_gemini(payload: dict) -> dict[str, Any]:
    """Convert an internal Responses-like request into Gemini generateContent.

    Channel callers prepare once and retain the returned identity map for
    response restoration. Direct callers still must not silently lose local
    references or namespace declarations.
    """
    raw_items = payload.get("input")
    needs_prepare = payload.get("previous_response_id") or payload.get("conversation") or payload.get("background")
    needs_prepare = needs_prepare or any(
        isinstance(tool, dict) and tool.get("type") not in {None, "function"}
        for tool in payload.get("tools") or []
    )
    if isinstance(raw_items, list):
        needs_prepare = needs_prepare or any(
            isinstance(item, dict) and (item.get("namespace") or item.get("type") == "item_reference")
            for item in raw_items
        )
    if needs_prepare:
        payload, _ = prepare_responses_request(payload, api_key_name=str(payload.get("_api_key_name") or ""))
    contents: list[dict[str, Any]] = []
    system_parts: list[dict[str, str]] = []
    call_names: dict[str, str] = {}
    model = str(payload.get("model") or "")
    json_schema_fields = antigravity_schema.uses_json_schema(model)

    instructions = payload.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        system_parts.append({"text": instructions})

    raw_input = payload.get("input")
    items = ([{"type": "message", "role": "user", "content": raw_input}]
             if isinstance(raw_input, str)
             else list(raw_input) if isinstance(raw_input, list) else [])
    for item in items:
        if not isinstance(item, dict):
            continue
        typ = str(item.get("type") or "")
        role = str(item.get("role") or "")
        if not typ and role:
            typ = "message"

        if typ == "message":
            if role in {"system", "developer"}:
                text = _text_from_content(item.get("content"))
                if text:
                    system_parts.append({"text": text})
                continue
            parts: list[dict[str, Any]] = []
            content = item.get("content")
            if isinstance(content, list):
                for piece in content:
                    part = _part_from_content_item(piece)
                    if part:
                        parts.append(part)
            elif isinstance(content, str) and content:
                parts.append({"text": content})
            if not parts:
                continue
            gemini_role = "model" if role in {"assistant", "model"} else "user"
            contents.append({"role": gemini_role, "parts": parts})
            continue

        if typ == "reasoning":
            text = _text_from_content(item.get("summary") or item.get("content"))
            signature = str(item.get("encrypted_content") or item.get("thoughtSignature") or "").strip()
            if not text and not signature:
                continue
            part: dict[str, Any] = {"text": text, "thought": True}
            if signature:
                part["thoughtSignature"] = signature
            contents.append({"role": "model", "parts": [part]})
            continue

        if typ == "function_call":
            call_id = str(item.get("call_id") or item.get("id") or "")
            name = str(item.get("name") or "")
            if call_id and name:
                call_names[call_id] = name
            function_call: dict[str, Any] = {
                "name": name,
                "args": _parse_args(item.get("arguments")),
            }
            if call_id:
                function_call["id"] = call_id
            part = {"functionCall": function_call}
            signature = _part_thought_signature(item)
            if signature:
                part["thoughtSignature"] = signature
            contents.append({"role": "model", "parts": [part]})
            continue

        if typ == "function_call_output":
            call_id = str(item.get("call_id") or "")
            name = call_names.get(call_id) or str(item.get("name") or "tool")
            response, image_parts = _tool_output_parts(item.get("output"))
            function_response: dict[str, Any] = {
                "name": name,
                "response": response if isinstance(response, dict) else {"result": response},
            }
            if call_id:
                function_response["id"] = call_id
            contents.append({
                "role": "user",
                "parts": [{"functionResponse": function_response}, *image_parts],
            })
            continue

    tools_out: list[dict[str, Any]] = []
    declarations: list[dict[str, Any]] = []
    for tool in payload.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        typ = str(tool.get("type") or "")
        if typ and typ not in {"function"}:
            continue
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        if not isinstance(fn, dict):
            continue
        name = str(fn.get("name") or tool.get("name") or "").strip()
        if not name:
            continue
        decl: dict[str, Any] = {"name": name}
        if fn.get("description") or tool.get("description"):
            decl["description"] = str(fn.get("description") or tool.get("description") or "")
        params = fn.get("parameters") or tool.get("parameters")
        if isinstance(params, dict):
            if json_schema_fields:
                # parameters is protobuf Schema (one type enum); JSON Schema
                # unions, refs and branch-local constraints belong in this
                # mutually exclusive field instead. Keep caller data intact.
                decl["parametersJsonSchema"] = copy.deepcopy(params)
            else:
                decl["parameters"] = antigravity_schema.clean_tool_schema(
                    params,
                    require_placeholder=antigravity_schema.uses_antigravity_schema(model),
                )
        declarations.append(decl)
    if declarations:
        tools_out.append({"functionDeclarations": declarations})

    generation: dict[str, Any] = {}
    if payload.get("temperature") is not None:
        generation["temperature"] = payload["temperature"]
    if payload.get("top_p") is not None:
        generation["topP"] = payload["top_p"]
    if payload.get("max_output_tokens") is not None:
        generation["maxOutputTokens"] = payload["max_output_tokens"]
    thinking = _thinking_config(payload)
    if thinking:
        generation["thinkingConfig"] = thinking
    mime, schema = _structured_output(payload)
    if mime:
        generation["responseMimeType"] = mime
    if schema:
        if json_schema_fields:
            generation["responseJsonSchema"] = copy.deepcopy(schema)
        else:
            generation["responseSchema"] = antigravity_schema.clean_response_schema(schema)

    out: dict[str, Any] = {"contents": contents}
    if system_parts:
        out["systemInstruction"] = {"parts": system_parts}
    if tools_out:
        out["tools"] = tools_out
    if generation:
        out["generationConfig"] = generation
    choice = _tool_choice_config(payload.get("tool_choice"), [d["name"] for d in declarations])
    if choice:
        out["toolConfig"] = {"functionCallingConfig": choice}
    return out


def format_session_id(anchor: str) -> str:
    """Map a client conversation anchor to Antigravity's negative int64 sessionId."""
    text = str(anchor or "").strip()
    if not text:
        return ""
    digest = hashlib.sha256(f"parrot:antigravity:session\x00{text}".encode("utf-8")).digest()
    value = int.from_bytes(digest[:8], "big") & 0x7FFFFFFFFFFFFFFF
    return f"-{value}"


def _stable_session_id(gemini: dict) -> str:
    contents = gemini.get("contents")
    if isinstance(contents, list):
        for item in contents:
            if not isinstance(item, dict) or item.get("role") != "user":
                continue
            parts = item.get("parts")
            if not isinstance(parts, list) or not parts:
                continue
            first = parts[0]
            if isinstance(first, dict) and isinstance(first.get("text"), str) and first["text"]:
                digest = hashlib.sha256(first["text"].encode("utf-8")).digest()
                value = int.from_bytes(digest[:8], "big") & 0x7FFFFFFFFFFFFFFF
                return f"-{value}"
    return f"-{int.from_bytes(uuid.uuid4().bytes[:8], 'big') & 0x7FFFFFFFFFFFFFFF}"


def is_image_model(model: str) -> bool:
    return "image" in str(model or "").lower()


def wrap_cloud_code(
    gemini: dict,
    *,
    model: str,
    project_id: str,
    stream: bool = False,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Wrap a Gemini generateContent body in the Cloud Code envelope."""
    request = dict(gemini or {})
    request.pop("model", None)
    request.pop("safetySettings", None)
    request.pop("safety_settings", None)
    if not request.get("sessionId") and not is_image_model(model):
        request["sessionId"] = format_session_id(session_id) or _stable_session_id(request)

    image = is_image_model(model)
    claude = "claude" in str(model or "").lower()
    if claude:
        tool_config = request.setdefault("toolConfig", {})
        if isinstance(tool_config, dict):
            fcc = tool_config.setdefault("functionCallingConfig", {})
            if isinstance(fcc, dict) and not fcc.get("mode"):
                fcc["mode"] = "VALIDATED"
        gen = request.get("generationConfig")
        thinking = gen.get("thinkingConfig") if isinstance(gen, dict) else None
        if isinstance(thinking, dict) and thinking.get("thinkingBudget") is not None:
            if not isinstance(gen, dict):
                gen = {}
                request["generationConfig"] = gen
            if gen.get("maxOutputTokens") is None:
                gen["maxOutputTokens"] = 64000
    else:
        gen = request.get("generationConfig")
        if isinstance(gen, dict):
            gen.pop("maxOutputTokens", None)
            if not gen:
                request.pop("generationConfig", None)

    _sanitize_request_schemas(request, model=model)
    _sanitize_thought_signatures(request)

    envelope = {
        "project": project_id,
        "model": model,
        "userAgent": "antigravity",
        "requestType": "image_gen" if image else "agent",
        "requestId": (
            f"image_gen/{int(time.time() * 1000)}/{uuid.uuid4()}/12"
            if image else f"agent-{uuid.uuid4()}"
        ),
        "request": request,
    }
    _ = stream
    return envelope


def unwrap_cloud_code(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    inner = payload.get("response")
    if isinstance(inner, dict) and any(key in inner for key in (
        "candidates", "usageMetadata", "usage_metadata", "error", "promptFeedback", "prompt_feedback",
    )):
        return inner
    return payload


def _usage_from_gemini(meta: Any) -> dict[str, Any] | None:
    if not isinstance(meta, dict):
        return None
    prompt = int(meta.get("promptTokenCount") or 0)
    candidates = int(meta.get("candidatesTokenCount") or 0)
    cached = int(meta.get("cachedContentTokenCount") or 0)
    thoughts = int(meta.get("thoughtsTokenCount") or 0)
    output = candidates + thoughts
    # An explicit upstream total (even zero) is authoritative, not a checksum
    # to be overwritten when other provider accounting components differ.
    total = int(meta["totalTokenCount"]) if meta.get("totalTokenCount") is not None else prompt + output
    return {
        "input_tokens": prompt,
        "output_tokens": output,
        "total_tokens": total,
        "input_tokens_details": {"cached_tokens": cached},
        "output_tokens_details": {"reasoning_tokens": thoughts},
    }


def _finish_to_status(reason: str | None) -> tuple[str, dict | None]:
    value = str(reason or "").upper()
    if value == "STOP":
        return "completed", None
    if value in {"MAX_TOKENS", "LENGTH"}:
        return "incomplete", {"reason": "max_output_tokens"}
    if value in {
        "SAFETY", "BLOCKLIST", "PROHIBITED_CONTENT", "RECITATION", "SPII",
        "IMAGE_SAFETY", "IMAGE_PROHIBITED_CONTENT", "IMAGE_RECITATION", "ESCALATION",
    }:
        return "incomplete", {"reason": "content_filter"}
    if value in {
        "MALFORMED_FUNCTION_CALL", "UNEXPECTED_TOOL_CALL", "MISSING_THOUGHT_SIGNATURE",
        "MALFORMED_RESPONSE", "PUP_LIMITED_DISABLED",
    }:
        return "failed", None
    # Responses has no standard incomplete reason for EOF / unknown Gemini
    # reasons. Inventing one lets downstream bridges mistake it for normal
    # stop. Use an explicit failure, retaining recoverable output items.
    return "failed", None


def _terminal_details(reason: str | None, feedback: Any, error: Any = None):
    status, incomplete = _finish_to_status(reason)
    response_error = None
    if isinstance(feedback, dict):
        block = feedback.get("blockReason") or feedback.get("block_reason")
        if block and block != "BLOCK_REASON_UNSPECIFIED":
            status, incomplete = "incomplete", {"reason": "content_filter"}
    if error is not None:
        err = error if isinstance(error, dict) else {"message": str(error)}
        status, incomplete = "failed", None
        response_error = {
            "message": str(err.get("message") or "antigravity error"),
            "code": str(err.get("status") or err.get("code") or "api_error"),
        }
    elif status == "failed":
        response_error = {
            "code": "gemini_" + str(reason).lower() if reason else "upstream_stream_incomplete",
            "message": f"Gemini generation ended with {reason}" if reason else "Gemini response ended without finishReason",
        }
    return status, incomplete, response_error


def _complete_call(item: dict) -> bool:
    if not item.get("name"):
        return False
    try:
        return isinstance(json.loads(item.get("arguments") or ""), dict)
    except (ValueError, TypeError):
        return False


def _provider_metadata(reason=None, feedback=None, error=None, finish_message=None) -> dict:
    details = {}
    if reason:
        details["finishReason"] = reason
    if finish_message:
        details["finishMessage"] = finish_message
    if feedback is not None:
        details["promptFeedback"] = copy.deepcopy(feedback)
    if error is not None:
        details["error"] = copy.deepcopy(error)
    return {"gemini": details}


def _inline_image_part(part: dict) -> dict[str, Any] | None:
    inline = part.get("inlineData") or part.get("inline_data") or {}
    if not isinstance(inline, dict) or not inline.get("data"):
        return None
    mime = str(inline.get("mimeType") or inline.get("mime_type") or "image/png")
    if mime and not mime.startswith("image/"):
        return None
    return {
        "type": "output_image",
        "image_url": f"data:{mime};base64,{inline['data']}",
    }


def gemini_parts_to_output_items(parts: list[Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    text_buf: list[str] = []
    call_index = 0

    def flush_text() -> None:
        if not text_buf:
            return
        text = "".join(text_buf)
        text_buf.clear()
        items.append({
            "type": "message",
            "id": _gen_id("msg_"),
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        })

    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("functionCall") or part.get("function_call"):
            flush_text()
            call = part.get("functionCall") or part.get("function_call") or {}
            call_index += 1
            name = str(call.get("name") or "")
            args = call.get("args") if "args" in call else call.get("arguments")
            native_id = str(call.get("id") or "").strip()
            item = {
                "type": "function_call",
                "id": _gen_id("fc_"),
                "call_id": native_id or f"call_{call_index}",
                "name": name,
                "arguments": _as_json_str(args if args is not None else {}),
                "status": "completed",
            }
            signature = _part_thought_signature(part)
            if signature:
                item["encrypted_content"] = signature
            items.append(item)
            continue
        if part.get("thought") is True:
            flush_text()
            text = str(part.get("text") or "")
            signature = str(part.get("thoughtSignature") or part.get("thought_signature") or "")
            item = {
                "type": "reasoning",
                "id": _gen_id("rs_"),
                "summary": [{"type": "summary_text", "text": text}] if text else [],
                "status": "completed",
            }
            if signature:
                item["encrypted_content"] = signature
            items.append(item)
            continue
        if part.get("text"):
            text_buf.append(str(part.get("text") or ""))
            continue
        image = _inline_image_part(part)
        if image:
            flush_text()
            items.append({
                "type": "message",
                "id": _gen_id("msg_"),
                "role": "assistant",
                "status": "completed",
                "content": [image],
            })
    flush_text()
    return items


def gemini_to_responses(payload: dict, *, model: str, namespace_tool_map: Any = None) -> dict[str, Any]:
    data = unwrap_cloud_code(payload)
    candidate = {}
    candidates = data.get("candidates")
    if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict):
        candidate = candidates[0]
    content = candidate.get("content") if isinstance(candidate.get("content"), dict) else {}
    parts = content.get("parts") if isinstance(content.get("parts"), list) else []
    items = [_restore_tool_identity(item, namespace_tool_map) for item in gemini_parts_to_output_items(parts)]
    reason = candidate.get("finishReason") or candidate.get("finish_reason")
    feedback = data.get("promptFeedback", data.get("prompt_feedback"))
    error = data.get("error", payload.get("error"))
    invalid_calls = [it for it in items if it["type"] == "function_call" and not _complete_call(it)]
    if invalid_calls and reason == "STOP" and error is None:
        error = {"code": "gemini_malformed_function_call", "message": "Invalid Gemini function call arguments"}
    status, incomplete, response_error = _terminal_details(reason, feedback, error)
    for item in items:
        if item["type"] == "function_call":
            if item in invalid_calls or reason in {"MALFORMED_FUNCTION_CALL", "UNEXPECTED_TOOL_CALL"}:
                item["status"] = "incomplete"
        elif status != "completed":
            item["status"] = "incomplete"
    output_text = "".join(
        (it.get("content") or [{}])[0].get("text", "")
        for it in items
        if it.get("type") == "message" and it.get("content")
    )
    return {
        "id": _gen_id("resp_"),
        "object": "response",
        "created_at": int(time.time()),
        "status": status,
        "error": response_error,
        "incomplete_details": incomplete,
        "provider_metadata": _provider_metadata(reason, feedback, error, candidate.get("finishMessage")),
        "model": model,
        "output": items,
        "output_text": output_text,
        "usage": _usage_from_gemini(data.get("usageMetadata") or data.get("usage_metadata")),
    }


def _emit(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {_json_dumps(data)}\n\n".encode("utf-8")


class GeminiStreamToResponses:
    """Convert Gemini / Cloud Code candidates SSE into Responses SSE."""

    def __init__(self, *, model: str, request_body: dict | None = None, stream_mode: str = "auto",
                 namespace_tool_map: Any = None, local_store_context: dict | None = None):
        self.namespace_tool_map = namespace_tool_map
        self.local_store_context = local_store_context
        # auto is envelope-based, never content-based: public Gemini is delta;
        # private Cloud Code keeps its historical compatibility behaviour.
        if stream_mode not in {"auto", "delta", "cumulative", "cloud_code_legacy"}:
            raise ValueError(f"Unknown Gemini stream mode: {stream_mode}")
        self.stream_mode = stream_mode
        self.active_mode: str | None = None
        self.decoder = codecs.getincrementaldecoder("utf-8")()
        self.wire_format: str | None = None
        self.closed = False
        self.model = model
        self.request_body = request_body or {}
        self.resp_id = _gen_id("resp_")
        self.created_at = int(time.time())
        self.seq = 0
        self.output_index = 0
        self.created = False
        self.finished = False
        self.buffer = ""
        self.text_item: dict[str, Any] | None = None
        self.reasoning_item: dict[str, Any] | None = None
        self.fc_items: dict[int, dict[str, Any]] = {}
        self.closed_items: dict[int, dict[str, Any]] = {}
        self.seen_text = ""
        self.seen_thought = ""
        self.fc_ids: dict[str, int] = {}
        self.fc_slots: dict[int, int] = {}
        self.finish_reason: str | None = None
        self.finish_message: str | None = None
        self.prompt_feedback: dict | None = None
        self.upstream_error: Any = None
        self.item_status = "completed"
        self.usage_meta: dict[str, Any] = {}
        self.usage: dict[str, Any] | None = None

    def _next_seq(self) -> int:
        self.seq += 1
        return self.seq

    def _alloc_index(self) -> int:
        idx = self.output_index
        self.output_index += 1
        return idx

    def _skeleton(self, status: str) -> dict[str, Any]:
        from ..openai.transform.common import build_response_skeleton
        return build_response_skeleton(
            resp_id=self.resp_id,
            model=self.model,
            created_at=self.created_at,
            status=status,
            request_body=self.request_body,
        )

    def _ensure_created(self) -> Iterator[bytes]:
        if self.created:
            return
        self.created = True
        skeleton = self._skeleton("in_progress")
        yield _emit("response.created", {
            "type": "response.created",
            "sequence_number": self._next_seq(),
            "response": skeleton,
        })
        yield _emit("response.in_progress", {
            "type": "response.in_progress",
            "sequence_number": self._next_seq(),
            "response": skeleton,
        })

    def _wire_error(self, code: str, message: str) -> bytes:
        error = {"code": code, "message": message, "raw": self.buffer}
        if self.wire_format == "json":
            self.finished = True
            return _json_dumps(gemini_to_responses({"error": error}, model=self.model)).encode("utf-8")
        return b"".join(self._finalize(error=error))

    def feed(self, chunk: bytes) -> bytes:
        if not chunk or self.finished or self.closed:
            return b""
        try:
            self.buffer += self.decoder.decode(chunk)
        except UnicodeDecodeError as exc:
            return self._wire_error("invalid_upstream_utf8", str(exc))
        if self.wire_format is None and self.buffer.lstrip():
            self.wire_format = "json" if self.buffer.lstrip().startswith("{") else "sse"
        if self.wire_format == "json":
            try:
                payload = json.loads(self.buffer)
            except json.JSONDecodeError:
                return b""  # A JSON response may also span arbitrary network chunks.
            self.finished = True
            self.buffer = ""
            response = gemini_to_responses(payload, model=self.model, namespace_tool_map=self.namespace_tool_map)
            self._save_local_response(response)
            return _json_dumps(response).encode("utf-8")
        out = bytearray()
        while not self.finished:
            boundary = re.search(r"\r?\n\r?\n", self.buffer)
            if boundary is None:
                break
            block, self.buffer = self.buffer[:boundary.start()], self.buffer[boundary.end():]
            out.extend(b"".join(self._handle_block(block)))
        return bytes(out)

    def close(self) -> bytes:
        if self.closed or self.finished:
            return b""
        self.closed = True
        try:
            self.buffer += self.decoder.decode(b"", final=True)
        except UnicodeDecodeError as exc:
            return self._wire_error("invalid_upstream_utf8", str(exc))
        if self.wire_format == "json":
            return self._wire_error("invalid_upstream_json", "Truncated Gemini JSON response")
        leftover = self.buffer.strip()
        self.buffer = ""
        out = bytearray()
        if leftover:
            out.extend(b"".join(self._handle_block(leftover)))
        if not self.finished:
            out.extend(b"".join(self._finalize()))
        return bytes(out)

    def _handle_block(self, block: str) -> Iterator[bytes]:
        if self.finished:
            return
        data_lines: list[str] = []
        for raw_line in block.splitlines():
            line = raw_line.strip("\r")
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if not data_lines:
            return
        raw = "\n".join(data_lines).strip()
        if not raw or raw == "[DONE]":
            if raw == "[DONE]":
                yield from self._finalize()
            return
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            yield from self._finalize(error={
                "code": "invalid_upstream_json", "message": "Invalid Gemini SSE JSON", "raw": raw,
            })
            return
        if not isinstance(payload, dict):
            yield from self._finalize(error={
                "code": "invalid_upstream_json", "message": "Gemini SSE payload must be an object", "raw": raw,
            })
            return
        data = unwrap_cloud_code(payload)
        if self.active_mode is None:
            self.active_mode = self.stream_mode if self.stream_mode != "auto" else (
                "cloud_code_legacy" if data is not payload else "delta"
            )
        if data is not payload and payload.get("error") is not None:
            data = dict(data, error=payload["error"])
        try:
            yield from self._ingest_gemini(data)
        except ValueError as exc:
            yield from self._finalize(error={
                "code": "invalid_upstream_stream", "message": str(exc), "chunk": data,
            })

    def _ingest_gemini(self, data: dict) -> Iterator[bytes]:
        yield from self._ensure_created()
        meta = data.get("usageMetadata") or data.get("usage_metadata")
        if isinstance(meta, dict):
            self.usage_meta.update(meta)
            self.usage = _usage_from_gemini(self.usage_meta)
        if data.get("error") is not None:
            self.upstream_error = data["error"]
        feedback = data.get("promptFeedback", data.get("prompt_feedback"))
        if isinstance(feedback, dict):
            self.prompt_feedback = feedback
        candidates = data.get("candidates")
        candidate = candidates[0] if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict) else {}
        reason = candidate.get("finishReason") or candidate.get("finish_reason")
        if reason:
            self.finish_reason = str(reason)
        if candidate.get("finishMessage"):
            self.finish_message = str(candidate["finishMessage"])
        content = candidate.get("content") if isinstance(candidate.get("content"), dict) else {}
        parts = content.get("parts") if isinstance(content.get("parts"), list) else []
        fc_seen = 0
        for part in parts:
            if not isinstance(part, dict):
                continue
            if part.get("functionCall") or part.get("function_call"):
                yield from self._on_function_call(
                    fc_seen,
                    part.get("functionCall") or part.get("function_call") or {},
                    signature=_part_thought_signature(part),
                )
                fc_seen += 1
                continue
            if part.get("thought") is True:
                yield from self._on_thought(str(part.get("text") or ""), str(part.get("thoughtSignature") or ""))
                continue
            if part.get("text"):
                yield from self._on_text(str(part.get("text") or ""))
                continue
            image = _inline_image_part(part)
            if image:
                yield from self._on_image(image)
        blocked = (self.prompt_feedback or {}).get("blockReason") or (self.prompt_feedback or {}).get("block_reason")
        terminal_reason = self.finish_reason and self.finish_reason != "FINISH_REASON_UNSPECIFIED"
        if terminal_reason or self.upstream_error is not None or (blocked and blocked != "BLOCK_REASON_UNSPECIFIED"):
            yield from self._finalize()

    def _close_text(self) -> Iterator[bytes]:
        item = self.text_item
        if not item:
            return
        yield _emit("response.output_text.done", {
            "type": "response.output_text.done",
            "sequence_number": self._next_seq(),
            "item_id": item["id"],
            "output_index": item["output_index"],
            "content_index": 0,
            "text": item["text"],
            "logprobs": [],
        })
        part = {"type": "output_text", "text": item["text"], "annotations": []}
        yield _emit("response.content_part.done", {
            "type": "response.content_part.done",
            "sequence_number": self._next_seq(),
            "item_id": item["id"],
            "output_index": item["output_index"],
            "content_index": 0,
            "part": part,
        })
        completed = {
            "type": "message", "id": item["id"], "role": "assistant",
            "status": self.item_status, "content": [part],
        }
        yield _emit("response.output_item.done", {
            "type": "response.output_item.done",
            "sequence_number": self._next_seq(),
            "output_index": item["output_index"],
            "item": completed,
        })
        self.closed_items[item["output_index"]] = completed
        self.text_item = None

    def _close_reasoning(self) -> Iterator[bytes]:
        item = self.reasoning_item
        if not item:
            return
        if item["text"]:
            yield _emit("response.reasoning_summary_text.done", {
                "type": "response.reasoning_summary_text.done",
                "sequence_number": self._next_seq(),
                "item_id": item["id"],
                "output_index": item["output_index"],
                "summary_index": 0,
                "text": item["text"],
            })
            yield _emit("response.reasoning_summary_part.done", {
                "type": "response.reasoning_summary_part.done",
                "sequence_number": self._next_seq(),
                "item_id": item["id"],
                "output_index": item["output_index"],
                "summary_index": 0,
                "part": {"type": "summary_text", "text": item["text"]},
            })
        completed = {
            "type": "reasoning",
            "id": item["id"],
            "summary": [{"type": "summary_text", "text": item["text"]}] if item["text"] else [],
            "status": self.item_status,
        }
        if item.get("signature"):
            completed["encrypted_content"] = item["signature"]
        yield _emit("response.output_item.done", {
            "type": "response.output_item.done",
            "sequence_number": self._next_seq(),
            "output_index": item["output_index"],
            "item": completed,
        })
        self.closed_items[item["output_index"]] = completed
        self.reasoning_item = None

    def _close_function_calls(self) -> Iterator[bytes]:
        for idx in sorted(self.fc_items):
            item = self.fc_items[idx]
            # Struct args are whole snapshots, not JSON string deltas. Buffer
            # until closure so {"x":1} -> {"x":1,"y":2} needs no retraction.
            if item["arguments"]:
                yield _emit("response.function_call_arguments.delta", {
                    "type": "response.function_call_arguments.delta",
                    "sequence_number": self._next_seq(),
                    "item_id": item["id"], "output_index": item["output_index"],
                    "delta": item["arguments"],
                })
            complete = _complete_call(item) and self.finish_reason not in {
                "MALFORMED_FUNCTION_CALL", "UNEXPECTED_TOOL_CALL",
            }
            if complete:
                yield _emit("response.function_call_arguments.done", {
                    "type": "response.function_call_arguments.done",
                    "sequence_number": self._next_seq(),
                    "item_id": item["id"],
                    "output_index": item["output_index"],
                    "arguments": item["arguments"],
                })
            completed = {
                "type": "function_call",
                "id": item["id"],
                "call_id": item["call_id"],
                "name": item["name"],
                "arguments": item["arguments"],
                "status": "completed" if complete else "incomplete",
            }
            if item.get("signature"):
                completed["encrypted_content"] = item["signature"]
            completed = _restore_tool_identity(completed, self.namespace_tool_map)
            yield _emit("response.output_item.done", {
                "type": "response.output_item.done",
                "sequence_number": self._next_seq(),
                "output_index": item["output_index"],
                "item": completed,
            })
            self.closed_items[item["output_index"]] = completed
        self.fc_items.clear()

    def _text_delta(self, previous: str, current: str) -> str:
        if self.active_mode == "delta":
            return current
        if self.active_mode == "cumulative":
            if not current.startswith(previous):
                raise ValueError("Gemini cumulative text revised an already emitted prefix")
            return current[len(previous):]
        # Explicit legacy-only compatibility. No captured private contract is
        # available to disambiguate identical incremental chunks here.
        return _delta_from_snapshot(previous, current)

    def _on_text(self, text: str) -> Iterator[bytes]:
        delta = self._text_delta(self.seen_text, text)
        self.seen_text += delta
        if not delta:
            return
        yield from self._close_reasoning()
        if self.text_item is None:
            item = {
                "id": _gen_id("msg_"),
                "output_index": self._alloc_index(),
                "text": "",
            }
            self.text_item = item
            yield _emit("response.output_item.added", {
                "type": "response.output_item.added",
                "sequence_number": self._next_seq(),
                "output_index": item["output_index"],
                "item": {
                    "type": "message", "id": item["id"], "role": "assistant",
                    "status": "in_progress", "content": [],
                },
            })
            yield _emit("response.content_part.added", {
                "type": "response.content_part.added",
                "sequence_number": self._next_seq(),
                "item_id": item["id"],
                "output_index": item["output_index"],
                "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []},
            })
        self.text_item["text"] += delta
        yield _emit("response.output_text.delta", {
            "type": "response.output_text.delta",
            "sequence_number": self._next_seq(),
            "item_id": self.text_item["id"],
            "output_index": self.text_item["output_index"],
            "content_index": 0,
            "delta": delta,
            "logprobs": [],
        })

    def _on_image(self, image: dict[str, Any]) -> Iterator[bytes]:
        yield from self._close_reasoning()
        yield from self._close_text()
        item = {
            "type": "message",
            "id": _gen_id("msg_"),
            "role": "assistant",
            "status": "completed",
            "content": [image],
        }
        output_index = self._alloc_index()
        yield _emit("response.output_item.added", {
            "type": "response.output_item.added",
            "sequence_number": self._next_seq(),
            "output_index": output_index,
            "item": item,
        })
        yield _emit("response.output_item.done", {
            "type": "response.output_item.done",
            "sequence_number": self._next_seq(),
            "output_index": output_index,
            "item": item,
        })
        self.closed_items[output_index] = item

    def _on_thought(self, text: str, signature: str) -> Iterator[bytes]:
        delta = self._text_delta(self.seen_thought, text)
        self.seen_thought += delta
        if self.reasoning_item is None:
            item = {
                "id": _gen_id("rs_"),
                "output_index": self._alloc_index(),
                "text": "",
                "signature": signature,
            }
            self.reasoning_item = item
            yield _emit("response.output_item.added", {
                "type": "response.output_item.added",
                "sequence_number": self._next_seq(),
                "output_index": item["output_index"],
                "item": {"type": "reasoning", "id": item["id"], "summary": []},
            })
            yield _emit("response.reasoning_summary_part.added", {
                "type": "response.reasoning_summary_part.added",
                "sequence_number": self._next_seq(),
                "item_id": item["id"],
                "output_index": item["output_index"],
                "summary_index": 0,
                "part": {"type": "summary_text", "text": ""},
            })
        elif signature:
            self.reasoning_item["signature"] = signature
        if not delta:
            return
        self.reasoning_item["text"] += delta
        yield _emit("response.reasoning_summary_text.delta", {
            "type": "response.reasoning_summary_text.delta",
            "sequence_number": self._next_seq(),
            "item_id": self.reasoning_item["id"],
            "output_index": self.reasoning_item["output_index"],
            "summary_index": 0,
            "delta": delta,
        })

    def _on_function_call(self, slot: int, call: dict, *, signature: str = "") -> Iterator[bytes]:
        yield from self._close_text()
        yield from self._close_reasoning()
        name = str(call.get("name") or "")
        native_id = str(call.get("id") or "").strip()
        index = self.fc_ids.get(native_id) if native_id else None
        if index is None and not native_id and self.active_mode != "delta":
            # Only the private compatibility/cumulative branch has positional
            # snapshots. Names guard against per-packet index resets; explicit
            # native ids always win, even for parallel calls to the same tool.
            previous = self.fc_slots.get(slot)
            prior = self.fc_items.get(previous)
            if prior and not prior["native_id"] and (not name or prior["name"] == name):
                index = previous
        if index is None:
            index = len(self.fc_items)
        item = self.fc_items.get(index)
        if item is not None and name and item["name"] and name != item["name"]:
            raise ValueError(f"Gemini function call id {native_id!r} changed name")
        if item is None:
            item = {
                "id": _gen_id("fc_"),
                "call_id": native_id or _gen_id("call_"),
                "native_id": native_id,
                "output_index": self._alloc_index(),
                "name": name,
                "arguments": "",
                "signature": signature,
            }
            self.fc_items[index] = item
            if native_id:
                self.fc_ids[native_id] = index
            yield _emit("response.output_item.added", {
                "type": "response.output_item.added",
                "sequence_number": self._next_seq(),
                "output_index": item["output_index"],
                "item": _restore_tool_identity({
                    "type": "function_call",
                    "id": item["id"],
                    "call_id": item["call_id"],
                    "name": name,
                    "arguments": "",
                    "status": "in_progress",
                }, self.namespace_tool_map),
            })
        elif name and not item["name"]:
            item["name"] = name
        self.fc_slots[slot] = index
        if signature:
            item["signature"] = signature
        raw_args = call.get("args") if "args" in call else call.get("arguments")
        if raw_args is None:
            if not item["arguments"]:
                item["arguments"] = "{}"
        elif isinstance(raw_args, str):
            # String fragments are a private compatibility extension; the
            # public FunctionCall.args field is a complete protobuf Struct.
            previous = item["arguments"] if item.get("string_args") else ""
            item["arguments"] = previous + self._text_delta(previous, raw_args)
            item["string_args"] = True
        else:
            item["arguments"] = _as_json_str(raw_args)
            item["string_args"] = False

    def _finalize(self, error: Any = None) -> Iterator[bytes]:
        if self.finished:
            return
        self.finished = True
        if error is not None:
            self.upstream_error = error
        if self.finish_reason == "STOP" and self.upstream_error is None and any(
            not _complete_call(item) for item in self.fc_items.values()
        ):
            self.upstream_error = {
                "code": "gemini_malformed_function_call", "message": "Invalid Gemini function call arguments",
            }
        status, incomplete, response_error = _terminal_details(
            self.finish_reason, self.prompt_feedback, self.upstream_error,
        )
        self.item_status = "completed" if status == "completed" else "incomplete"
        yield from self._ensure_created()
        yield from self._close_text()
        yield from self._close_reasoning()
        yield from self._close_function_calls()
        output = [self.closed_items[idx] for idx in sorted(self.closed_items)]
        output_text = "".join(
            (it.get("content") or [{}])[0].get("text", "")
            for it in output
            if it.get("type") == "message" and it.get("content")
        )
        response = self._skeleton(status)
        response["output"] = output
        response["output_text"] = output_text
        response["usage"] = self.usage
        response["incomplete_details"] = incomplete
        response["error"] = response_error
        response["provider_metadata"] = _provider_metadata(
            self.finish_reason, self.prompt_feedback, self.upstream_error, self.finish_message,
        )
        if status == "failed":
            event = "response.failed"
        elif status == "incomplete":
            event = "response.incomplete"
        else:
            event = "response.completed"
        self._save_local_response(response)
        yield _emit(event, {
            "type": event,
            "sequence_number": self._next_seq(),
            "response": response,
        })

    def _save_local_response(self, response: dict) -> None:
        ctx = self.local_store_context
        if ctx is None:
            return
        from ..openai import store
        response["previous_response_id"] = ctx.get("parent_id")
        response["store"] = False
        if self.request_body.get("store") is False or not store.is_enabled():
            return
        if response.get("status") not in {"completed", "incomplete"}:
            return
        try:
            store.save(
                response["id"], ctx.get("parent_id"), api_key_name=ctx["api_key_name"],
                model=self.model, channel_key=ctx["channel_key"],
                input_items=ctx["current_input_items"], output_items=response["output"],
            )
            # save() intentionally no-ops before application Store init.
            # Verify the record rather than advertising a phantom anchor.
            store.lookup(response["id"], api_key_name=ctx["api_key_name"])
            response["store"] = True
        except Exception:
            # A useful generation is not discarded due to persistence trouble,
            # but never advertise a resumable response that was not stored.
            logging.getLogger(__name__).warning("Antigravity local response Store write failed", exc_info=True)
            response.setdefault("provider_metadata", {}).setdefault("gemini", {})["local_store"] = "unavailable"


def restore_antigravity_bytes(
    chunk: bytes,
    *,
    converter: GeminiStreamToResponses | None,
    flush: bool = False,
) -> bytes:
    if converter is None:
        text = (chunk or b"").lstrip()
        if text.startswith(b"{"):
            try:
                payload = json.loads(chunk.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                return chunk
            if isinstance(payload, dict):
                return json.dumps(
                    gemini_to_responses(payload, model="antigravity"),
                    ensure_ascii=False,
                ).encode("utf-8")
        return chunk
    out = converter.feed(chunk or b"")
    if flush:
        out += converter.close()
    return out


def default_api_url(stream: bool) -> str:
    return ag_provider.stream_generate_content_url() if stream else ag_provider.generate_content_url()
