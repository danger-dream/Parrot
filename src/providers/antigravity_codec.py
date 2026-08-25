"""Gemini generateContent ↔ OpenAI Responses codec for Antigravity.

Channel converts ingress Responses-like payloads into Cloud Code envelopes.
The adapter restores Gemini JSON / candidates SSE into standard Responses
JSON / SSE *before* Parrot's Responses toolkit sees the bytes.

Text deltas are snapshot-aware: if a later part starts with the previous
text, only the suffix is emitted (cumulative Gemini). Otherwise the new
text is treated as an incremental delta.
"""

from __future__ import annotations

import hashlib
import json
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
                    schema = decl.get(key)
                    if isinstance(schema, dict):
                        decl[key] = antigravity_schema.clean_tool_schema(
                            schema, require_placeholder=require_placeholder,
                        )
    gen = request.get("generationConfig") or request.get("generation_config")
    if isinstance(gen, dict):
        for key in ("responseSchema", "responseJsonSchema", "response_schema", "response_json_schema"):
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


def _thinking_config(payload: dict) -> dict[str, Any] | None:
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


def responses_to_gemini(payload: dict) -> dict[str, Any]:
    """Convert an internal Responses-like request into Gemini generateContent."""
    contents: list[dict[str, Any]] = []
    system_parts: list[dict[str, str]] = []
    call_names: dict[str, str] = {}

    instructions = payload.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        system_parts.append({"text": instructions})

    raw_input = payload.get("input")
    items = list(raw_input) if isinstance(raw_input, list) else []
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
            output = item.get("output")
            if isinstance(output, str):
                try:
                    response = json.loads(output)
                except json.JSONDecodeError:
                    response = {"result": output}
            elif isinstance(output, dict):
                response = output
            else:
                response = {"result": output}
            function_response: dict[str, Any] = {
                "name": name,
                "response": response if isinstance(response, dict) else {"result": response},
            }
            if call_id:
                function_response["id"] = call_id
            contents.append({
                "role": "user",
                "parts": [{"functionResponse": function_response}],
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
            decl["parameters"] = antigravity_schema.clean_tool_schema(
                params,
                require_placeholder=antigravity_schema.uses_antigravity_schema(
                    str(payload.get("model") or "")
                ),
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
    if isinstance(inner, dict) and ("candidates" in inner or "usageMetadata" in inner or "error" in inner):
        return inner
    return payload


def _usage_from_gemini(meta: Any) -> dict[str, Any] | None:
    if not isinstance(meta, dict):
        return None
    prompt = int(meta.get("promptTokenCount") or 0)
    candidates = int(meta.get("candidatesTokenCount") or 0)
    total = int(meta.get("totalTokenCount") or (prompt + candidates))
    cached = int(meta.get("cachedContentTokenCount") or 0)
    thoughts = int(meta.get("thoughtsTokenCount") or 0)
    return {
        "input_tokens": prompt,
        "output_tokens": candidates,
        "total_tokens": total,
        "input_tokens_details": {"cached_tokens": cached},
        "output_tokens_details": {"reasoning_tokens": thoughts},
    }


def _finish_to_status(reason: str | None) -> tuple[str, dict | None]:
    value = str(reason or "").upper()
    if value in {"MAX_TOKENS", "LENGTH"}:
        return "incomplete", {"reason": "max_output_tokens"}
    if value in {"SAFETY", "BLOCKLIST", "PROHIBITED_CONTENT", "RECITATION"}:
        return "incomplete", {"reason": "content_filter"}
    return "completed", None


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


def gemini_to_responses(payload: dict, *, model: str) -> dict[str, Any]:
    data = unwrap_cloud_code(payload)
    if data.get("error"):
        err = data["error"] if isinstance(data["error"], dict) else {"message": str(data["error"])}
        return {
            "id": _gen_id("resp_"),
            "object": "response",
            "created_at": int(time.time()),
            "status": "failed",
            "error": {
                "message": str(err.get("message") or "antigravity error"),
                "code": str(err.get("status") or err.get("code") or "api_error"),
            },
            "model": model,
            "output": [],
            "output_text": "",
            "usage": None,
        }
    candidate = {}
    candidates = data.get("candidates")
    if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict):
        candidate = candidates[0]
    content = candidate.get("content") if isinstance(candidate.get("content"), dict) else {}
    parts = content.get("parts") if isinstance(content.get("parts"), list) else []
    items = gemini_parts_to_output_items(parts)
    status, incomplete = _finish_to_status(candidate.get("finishReason") or candidate.get("finish_reason"))
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
        "error": None,
        "incomplete_details": incomplete,
        "model": model,
        "output": items,
        "output_text": output_text,
        "usage": _usage_from_gemini(data.get("usageMetadata") or data.get("usage_metadata")),
    }


def _emit(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {_json_dumps(data)}\n\n".encode("utf-8")


class GeminiStreamToResponses:
    """Convert Gemini / Cloud Code candidates SSE into Responses SSE."""

    def __init__(self, *, model: str, request_body: dict | None = None):
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
        self.closed_items: list[dict[str, Any]] = []
        self.seen_text = ""
        self.seen_thought = ""
        self.seen_fc_args: dict[int, str] = {}
        self.finish_reason: str | None = None
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

    def feed(self, chunk: bytes) -> bytes:
        if not chunk:
            return b""
        text = chunk.decode("utf-8", errors="replace")
        if text.lstrip().startswith("{") and "\ndata:" not in text and not self.buffer:
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict) and not self.created:
                converted = gemini_to_responses(payload, model=self.model)
                return json.dumps(converted, ensure_ascii=False).encode("utf-8")
        self.buffer += text
        out = bytearray()
        while True:
            split_at = self.buffer.find("\n\n")
            if split_at < 0:
                crlf = self.buffer.find("\r\n\r\n")
                if crlf < 0:
                    break
                block, self.buffer = self.buffer[:crlf], self.buffer[crlf + 4:]
            else:
                block, self.buffer = self.buffer[:split_at], self.buffer[split_at + 2:]
            out.extend(b"".join(self._handle_block(block)))
        return bytes(out)

    def close(self) -> bytes:
        leftover = self.buffer.strip()
        self.buffer = ""
        out = bytearray()
        if leftover:
            out.extend(b"".join(self._handle_block(leftover)))
        if not self.finished:
            out.extend(b"".join(self._finalize()))
        return bytes(out)

    def _handle_block(self, block: str) -> Iterator[bytes]:
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
            return
        if not isinstance(payload, dict):
            return
        if payload.get("error"):
            yield from self._ensure_created()
            yield from self._finalize(error=payload.get("error"))
            return
        data = unwrap_cloud_code(payload)
        yield from self._ingest_gemini(data)

    def _ingest_gemini(self, data: dict) -> Iterator[bytes]:
        yield from self._ensure_created()
        usage = _usage_from_gemini(data.get("usageMetadata") or data.get("usage_metadata"))
        if usage:
            self.usage = usage
        candidates = data.get("candidates")
        candidate = candidates[0] if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict) else {}
        reason = candidate.get("finishReason") or candidate.get("finish_reason")
        if reason:
            self.finish_reason = str(reason)
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
        if self.finish_reason:
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
            "status": "completed", "content": [part],
        }
        yield _emit("response.output_item.done", {
            "type": "response.output_item.done",
            "sequence_number": self._next_seq(),
            "output_index": item["output_index"],
            "item": completed,
        })
        self.closed_items.append(completed)
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
            "status": "completed",
        }
        if item.get("signature"):
            completed["encrypted_content"] = item["signature"]
        yield _emit("response.output_item.done", {
            "type": "response.output_item.done",
            "sequence_number": self._next_seq(),
            "output_index": item["output_index"],
            "item": completed,
        })
        self.closed_items.append(completed)
        self.reasoning_item = None

    def _close_function_calls(self) -> Iterator[bytes]:
        for idx in sorted(self.fc_items):
            item = self.fc_items[idx]
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
                "status": "completed",
            }
            if item.get("signature"):
                completed["encrypted_content"] = item["signature"]
            yield _emit("response.output_item.done", {
                "type": "response.output_item.done",
                "sequence_number": self._next_seq(),
                "output_index": item["output_index"],
                "item": completed,
            })
            self.closed_items.append(completed)
        self.fc_items.clear()

    def _on_text(self, text: str) -> Iterator[bytes]:
        delta = _delta_from_snapshot(self.seen_text, text)
        self.seen_text = text if text.startswith(self.seen_text) else (self.seen_text + delta)
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
        self.closed_items.append(item)

    def _on_thought(self, text: str, signature: str) -> Iterator[bytes]:
        delta = _delta_from_snapshot(self.seen_thought, text)
        self.seen_thought = text if text.startswith(self.seen_thought) else (self.seen_thought + delta)
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

    def _on_function_call(self, index: int, call: dict, *, signature: str = "") -> Iterator[bytes]:
        yield from self._close_text()
        yield from self._close_reasoning()
        name = str(call.get("name") or "")
        args = _as_json_str(call.get("args") if "args" in call else call.get("arguments") or {})
        item = self.fc_items.get(index)
        if item is None:
            native_id = str(call.get("id") or "").strip()
            item = {
                "id": _gen_id("fc_"),
                "call_id": native_id or f"call_{index + 1}",
                "output_index": self._alloc_index(),
                "name": name,
                "arguments": "",
                "signature": signature,
            }
            self.fc_items[index] = item
            self.seen_fc_args[index] = ""
            yield _emit("response.output_item.added", {
                "type": "response.output_item.added",
                "sequence_number": self._next_seq(),
                "output_index": item["output_index"],
                "item": {
                    "type": "function_call",
                    "id": item["id"],
                    "call_id": item["call_id"],
                    "name": name,
                    "arguments": "",
                    "status": "in_progress",
                },
            })
        elif name and not item["name"]:
            item["name"] = name
        prev = self.seen_fc_args.get(index, "")
        delta = _delta_from_snapshot(prev, args)
        self.seen_fc_args[index] = args if args.startswith(prev) else (prev + delta)
        if not delta:
            return
        item["arguments"] += delta
        yield _emit("response.function_call_arguments.delta", {
            "type": "response.function_call_arguments.delta",
            "sequence_number": self._next_seq(),
            "item_id": item["id"],
            "output_index": item["output_index"],
            "delta": delta,
        })

    def _finalize(self, error: Any = None) -> Iterator[bytes]:
        if self.finished:
            return
        self.finished = True
        yield from self._ensure_created()
        yield from self._close_text()
        yield from self._close_reasoning()
        yield from self._close_function_calls()
        status, incomplete = _finish_to_status(self.finish_reason)
        if error:
            status = "failed"
            incomplete = None
        output_text = "".join(
            (it.get("content") or [{}])[0].get("text", "")
            for it in self.closed_items
            if it.get("type") == "message" and it.get("content")
        )
        response = self._skeleton(status)
        response["output"] = list(self.closed_items)
        response["output_text"] = output_text
        response["usage"] = self.usage
        response["incomplete_details"] = incomplete
        if error:
            err = error if isinstance(error, dict) else {"message": str(error)}
            response["error"] = {
                "message": str(err.get("message") or "antigravity error"),
                "code": str(err.get("status") or err.get("code") or "api_error"),
            }
            event = "response.failed"
        elif status == "incomplete":
            event = "response.incomplete"
        else:
            event = "response.completed"
        yield _emit(event, {
            "type": event,
            "sequence_number": self._next_seq(),
            "response": response,
        })


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
