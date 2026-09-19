"""Local search/fetch validation and protocol encoders backed by search_service."""

from __future__ import annotations

import asyncio
import copy
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import urlparse, urlunparse

from fastapi.responses import Response, StreamingResponse

from . import config, log_db

from .protocols import errors as protocol_errors


ANTHROPIC_WEB_SEARCH_TOOL_TYPES = frozenset({
    "web_search_20250305",
    "web_search_20260209",
    "web_search_20260318",
})
ANTHROPIC_WEB_FETCH_TOOL_TYPES = frozenset({
    "web_fetch_20250910",
    "web_fetch_20260209",
    "web_fetch_20260309",
    "web_fetch_20260318",
})
OPENAI_WEB_SEARCH_TOOL_TYPES = frozenset({
    "web_search",
    "web_search_preview",
    "web_search_preview_2025_03_11",
})
# Search never owns image or unrelated built-in tool removal.
OPENAI_DROP_TOOL_TYPES = frozenset()  # Search policy never removes image tools.
OPENAI_LOCAL_WEB_MARKER = "_parrot_openai_local_web_tools"
SUPPORTED_TOOL_NAMES = frozenset({"WebSearch", "WebFetch", "web_search", "web_fetch"})
_URL_RE = re.compile(r"https?://[^\s)\]>}\"']+")


@dataclass(frozen=True)
class LocalToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class LocalToolResult:
    tool_use_id: str
    content: str
    is_error: bool = False


def _settings() -> dict[str, Any]:
    from . import search_service
    return search_service.settings()


def enabled() -> bool:
    settings = _settings()
    return any(settings.get(key, "managed") == "managed" for key in ("functionMode", "hostedMode"))


def max_tool_rounds() -> int:
    try:
        return max(0, int((_settings().get("maxToolRounds", 50))))
    except Exception:
        return 50


def _max_results() -> int:
    try:
        return min(20, max(1, int(_settings().get("maxResults", 8))))
    except Exception:
        return 8


def _max_fetch_chars() -> int:
    try:
        return max(1000, int(_settings().get("maxFetchChars", 50000)))
    except Exception:
        return 50000


def _min_query_chars() -> int:
    try:
        return max(1, int(_settings().get("minQueryChars", 2)))
    except Exception:
        return 2


def _max_fetch_url_chars() -> int:
    try:
        return max(1, int(_settings().get("maxFetchUrlChars", 2048)))
    except Exception:
        return 2048


def _require_known_url_for_fetch() -> bool:
    return _settings().get("requireKnownUrlForFetch", True) is not False


def _max_concurrent_tool_calls() -> int:
    try:
        return max(0, int(_settings().get("maxConcurrentToolCalls", 0)))
    except Exception:
        return 0


def _content_size(text: str) -> tuple[int, int]:
    return len((text or "").encode("utf-8")), len(text or "")


def _estimate_result_count(tool_name: str, text: str) -> int:
    if not text:
        return 0
    if tool_name == "extract":
        return 1
    try:
        obj = json.loads(text)
    except Exception:
        obj = None
    if isinstance(obj, list):
        return len(obj)
    if isinstance(obj, dict):
        for key in ("results", "items", "data", "documents"):
            val = obj.get(key)
            if isinstance(val, list):
                return len(val)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    numbered = [line for line in lines if re.match(r"^(?:\d+[.)]|[-*]\s+\[|#{2,4}\s+)", line)]
    if numbered:
        return len(numbered)
    urls = set(_URL_RE.findall(text))
    return len(urls) if urls else 1


def _record_call_start(request_id: str | log_db.RequestLogHandle | None, round_no: int, call: LocalToolCall) -> log_db.RowLogHandle | None:
    if not request_id:
        return None
    try:
        return log_db.record_local_web_call(
            request_id,
            round_no,
            call.name,
            query=str(call.input.get("query") or "")[:4000] if call.name in ("WebSearch", "web_search") else None,
            url=str(call.input.get("url") or "")[:4000] if call.name in ("WebFetch", "web_fetch") else None,
        )
    except Exception:
        return None


def _record_call_finish(log_id: int | log_db.RowLogHandle | None, call: LocalToolCall, result: LocalToolResult) -> None:
    if log_id is None:
        return
    try:
        b, chars = _content_size(result.content)
        tool_name = "search" if call.name in ("WebSearch", "web_search") else "extract"
        log_db.finish_local_web_call(
            log_id,
            status="error" if result.is_error else "success",
            result_count=(0 if result.is_error else _estimate_result_count(tool_name, result.content)),
            content_bytes=b,
            content_chars=chars,
            error_message=(result.content if result.is_error else None),
        )
    except Exception:
        pass


def is_anthropic_web_tool_type(value: Any) -> bool:
    return isinstance(value, str) and (value in (ANTHROPIC_WEB_SEARCH_TOOL_TYPES | ANTHROPIC_WEB_FETCH_TOOL_TYPES) or bool(re.fullmatch(r"web_(search|fetch)_\d{8}", value)))


def is_supported_tool_name(value: Any) -> bool:
    return isinstance(value, str) and value in SUPPORTED_TOOL_NAMES


def request_declares_supported_tools(body: dict[str, Any] | None) -> bool:
    from .search_tool_policy import needs_loop
    return isinstance(body, dict) and needs_loop(body)


def is_openai_web_search_tool_type(value: Any) -> bool:
    return isinstance(value, str) and (value in OPENAI_WEB_SEARCH_TOOL_TYPES or value.startswith("web_search_"))


def is_openai_drop_tool_type(value: Any) -> bool:
    return isinstance(value, str) and value in OPENAI_DROP_TOOL_TYPES


def _openai_web_search_function_tool(source: dict[str, Any] | None = None) -> dict[str, Any]:
    source = source or {}
    params = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query", "minLength": _min_query_chars()},
            "allowed_domains": {"type": "array", "items": {"type": "string"}},
            "blocked_domains": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["query"],
        "additionalProperties": False,
    }
    tool = {
        "type": "function",
        "name": "web_search",
        "description": str(
            source.get("description")
            or "Search the web. Executed by Parrot search_service when managed."
        ),
        "parameters": params,
    }
    if source.get("strict") is True:
        tool["strict"] = True
    return tool


def _normalize_openai_local_web_tool(tool: Any) -> tuple[dict[str, Any] | None, bool, bool]:
    """Return (tool_or_none, changed, web_search_enabled)."""
    if not isinstance(tool, dict):
        return tool, False, False  # type: ignore[return-value]
    typ = str(tool.get("type") or "").strip()
    if is_openai_web_search_tool_type(typ):
        return _openai_web_search_function_tool(tool), True, True
    if is_openai_drop_tool_type(typ):
        return None, True, False
    return tool, False, False


def _normalize_openai_local_web_tool_choice(choice: Any, *, has_tools: bool) -> tuple[Any, bool]:
    if not isinstance(choice, dict):
        return choice, False
    typ = str(choice.get("type") or "").strip()
    if is_openai_web_search_tool_type(typ):
        return ({"type": "function", "name": "web_search"} if has_tools else "auto"), True
    if is_openai_drop_tool_type(typ):
        return "auto", True
    if typ == "allowed_tools":
        tools = choice.get("tools")
        if not isinstance(tools, list):
            return choice, False
        changed = False
        kept: list[Any] = []
        for item in tools:
            normalized, item_changed, _ = _normalize_openai_local_web_tool(item)
            changed = changed or item_changed
            if normalized is not None:
                kept.append(normalized)
        if not changed:
            return choice, False
        if not kept:
            return "auto", True
        out = dict(choice)
        out["tools"] = kept
        return out, True
    return choice, False


_XAI_WEB_SEARCH_TOOL_ALLOWED_FIELDS = frozenset({
    "type",
    "allowed_domains",
    "excluded_domains",
    "enable_image_understanding",
    "enable_image_search",
})


def _normalize_xai_native_web_search_tool(tool: Any) -> tuple[Any, bool]:
    # Keep constraints (including filters and external_web_access) verbatim.
    # A candidate may reject unsupported options; it must not weaken policy.
    if not isinstance(tool, dict) or not is_openai_web_search_tool_type(tool.get("type")):
        return tool, False
    out = dict(tool)
    out["type"] = "web_search"
    if "blocked_domains" in out and "excluded_domains" not in out:
        out["excluded_domains"] = out.pop("blocked_domains")
    return out, out != tool


def _normalize_xai_native_web_search_tool_choice(choice: Any) -> tuple[Any, bool]:
    if not isinstance(choice, dict):
        return choice, False
    typ = str(choice.get("type") or "").strip()
    if is_openai_web_search_tool_type(typ):
        return {"type": "web_search"}, typ != "web_search" or choice != {"type": "web_search"}
    if typ == "allowed_tools":
        tools = choice.get("tools")
        if not isinstance(tools, list):
            return choice, False
        changed = False
        normalized_tools: list[Any] = []
        for item in tools:
            normalized, item_changed = _normalize_xai_native_web_search_tool(item)
            changed = changed or item_changed
            normalized_tools.append(normalized)
        if not changed:
            return choice, False
        out = dict(choice)
        out["tools"] = normalized_tools
        return out, True
    return choice, False


def prepare_xai_responses_native_web_search_tools(body: dict[str, Any] | None) -> bool:
    """Keep web_search as an xAI-native hosted tool instead of local AnySearch.

    Returns True when the request declared a web_search tool and was normalized
    for xAI.  This is intentionally separate from
    :func:`prepare_openai_responses_local_web_tools` so scheduler/provider
    selection can choose by family.
    """
    if not isinstance(body, dict):
        return False
    tools = body.get("tools")
    if not isinstance(tools, list):
        return False
    declared = False
    changed = False
    normalized_tools: list[Any] = []
    for tool in tools:
        if isinstance(tool, dict) and is_openai_web_search_tool_type(tool.get("type")):
            declared = True
        normalized, item_changed = _normalize_xai_native_web_search_tool(tool)
        changed = changed or item_changed
        normalized_tools.append(normalized)
    if changed:
        body["tools"] = normalized_tools
    choice = body.get("tool_choice")
    if choice is not None:
        normalized_choice, choice_changed = _normalize_xai_native_web_search_tool_choice(choice)
        if choice_changed:
            body["tool_choice"] = normalized_choice
    if declared:
        body.pop(OPENAI_LOCAL_WEB_MARKER, None)
    return declared


def request_declares_openai_web_search_tools(body: dict[str, Any] | None) -> bool:
    """Whether a Responses request declares OpenAI/xAI web_search hosted tools."""
    if not isinstance(body, dict):
        return False
    tools = body.get("tools")
    if isinstance(tools, list):
        for tool in tools:
            if isinstance(tool, dict) and is_openai_web_search_tool_type(tool.get("type")):
                return True
    choice = body.get("tool_choice")
    if isinstance(choice, dict):
        typ = str(choice.get("type") or "").strip()
        if is_openai_web_search_tool_type(typ):
            return True
        if typ == "allowed_tools":
            for item in choice.get("tools") or []:
                if isinstance(item, dict) and is_openai_web_search_tool_type(item.get("type")):
                    return True
    return False


def prepare_openai_responses_local_web_tools(body: dict[str, Any] | None) -> bool:
    """Compatibility helper; execution preparation belongs to the shared runner."""
    from .search_tool_policy import validate, needs_loop
    if not isinstance(body, dict):
        return False
    validate(body)
    return needs_loop(body)


def openai_responses_local_web_active(body: dict[str, Any] | None) -> bool:
    return request_declares_supported_tools(body)


def openai_response_assistant_message(response_obj: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(response_obj, dict):
        return None
    output = response_obj.get("output")
    if not isinstance(output, list):
        return None
    return {"role": "assistant", "content": output}


def _tool_use_to_responses_function_call(block: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(block, dict):
        return None
    typ = block.get("type")
    if typ == "function_call":
        name = str(block.get("name") or "")
        if name not in SUPPORTED_TOOL_NAMES:
            return None
        item = dict(block)
        item.setdefault("call_id", item.get("id") or f"call_{uuid.uuid4().hex[:24]}")
        item.setdefault("status", "completed")
        return item
    if typ != "tool_use":
        return None
    name = str(block.get("name") or "")
    if name not in SUPPORTED_TOOL_NAMES:
        return None
    call_id = str(block.get("id") or f"call_{uuid.uuid4().hex[:24]}")
    raw_input = block.get("input") if isinstance(block.get("input"), dict) else {}
    return {
        "type": "function_call",
        "id": f"fc_{uuid.uuid4().hex[:24]}",
        "call_id": call_id,
        "name": name,
        "arguments": json.dumps(raw_input, ensure_ascii=False, separators=(",", ":")),
        "status": "completed",
    }


def append_openai_tool_results_to_body(
    body: dict[str, Any],
    assistant_message: dict[str, Any],
    results: list[LocalToolResult],
) -> None:
    inp = body.get("input")
    if isinstance(inp, str):
        body["input"] = [{"type": "message", "role": "user", "content": inp}]
    elif not isinstance(inp, list):
        body["input"] = []
    input_items = body["input"]

    assistant_message = normalize_assistant_message_for_local_tools(assistant_message) or {}
    known_result_ids = {r.tool_use_id for r in results}
    for block in assistant_message.get("content") or []:
        if not isinstance(block, dict):
            continue
        call_item = _tool_use_to_responses_function_call(block)
        if call_item is None:
            continue
        if str(call_item.get("call_id") or "") in known_result_ids:
            input_items.append(call_item)

    for result in results:
        item = {
            "type": "function_call_output",
            "call_id": result.tool_use_id,
            "output": result.content,
        }
        if result.is_error:
            item["status"] = "failed"
        input_items.append(item)

    choice = body.get("tool_choice")
    if isinstance(choice, dict) and choice.get("type") == "function" and choice.get("name") in SUPPORTED_TOOL_NAMES:
        body["tool_choice"] = "auto"


def remove_openai_supported_tools_from_body(body: dict[str, Any]) -> int:
    tools = body.get("tools")
    if not isinstance(tools, list):
        return 0
    kept: list[Any] = []
    removed = 0
    for tool in tools:
        if isinstance(tool, dict) and tool.get("type") == "function" and tool.get("name") in SUPPORTED_TOOL_NAMES:
            removed += 1
            continue
        kept.append(tool)
    if removed:
        if kept:
            body["tools"] = kept
        else:
            body.pop("tools", None)
        choice = body.get("tool_choice")
        if isinstance(choice, dict) and choice.get("type") == "function" and choice.get("name") in SUPPORTED_TOOL_NAMES:
            body["tool_choice"] = "auto"
    return removed


def _iter_content_blocks(message: dict[str, Any] | None) -> Iterable[dict[str, Any]]:
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if isinstance(content, list):
        return (b for b in content if isinstance(b, dict))
    return []


def _tool_definition_options(tools: Any) -> dict[str, dict[str, Any]]:
    options: dict[str, dict[str, Any]] = {}
    if not isinstance(tools, list):
        return options
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        name = str(tool.get("name") or "")
        if not name or not (is_supported_tool_name(name) or is_anthropic_web_tool_type(tool.get("type"))):
            continue
        item: dict[str, Any] = {}
        for key in ("allowed_domains", "blocked_domains"):
            value = tool.get(key)
            if isinstance(value, list):
                cleaned = [str(v).strip() for v in value if str(v).strip()]
                if cleaned:
                    item[key] = cleaned
        if item:
            options[name] = item
    return options


def _merge_tool_options(tool_input: dict[str, Any], options: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(tool_input)
    if not options:
        return merged
    allowed = options.get("allowed_domains")
    if isinstance(allowed, list) and allowed:
        # Tool-definition allowed domains are a policy constraint; model-supplied
        # values are ignored unless the definition omitted the constraint.
        merged["allowed_domains"] = list(allowed)
    blocked = []
    if isinstance(options.get("blocked_domains"), list):
        blocked.extend(str(v) for v in options.get("blocked_domains") if str(v).strip())
    if isinstance(merged.get("blocked_domains"), list):
        blocked.extend(str(v) for v in merged.get("blocked_domains") if str(v).strip())
    if blocked:
        # Preserve order while de-duping.
        merged["blocked_domains"] = list(dict.fromkeys(blocked))
    return merged


def _normalize_url_for_policy(url: str) -> str:
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return url.strip()
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return url.strip()
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/") or "/"
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", parsed.query, ""))


def _collect_urls(value: Any, out: set[str]) -> None:
    if isinstance(value, str):
        for match in _URL_RE.findall(value):
            out.add(_normalize_url_for_policy(match.rstrip(".,;:")))
        return
    if isinstance(value, list):
        for item in value:
            _collect_urls(item, out)
        return
    if isinstance(value, dict):
        for key in ("url", "file_url", "image_url"):
            item = value.get(key)
            if isinstance(item, str) and item.startswith(("http://", "https://")):
                out.add(_normalize_url_for_policy(item))
        for item in value.values():
            _collect_urls(item, out)


def known_urls_from_body(body: Any) -> list[str]:
    urls: set[str] = set()
    _collect_urls(body, urls)
    return sorted(urls)


def _responses_function_call_to_tool_use(block: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(block, dict) or block.get("type") != "function_call":
        return None
    name = str(block.get("name") or "")
    if name not in SUPPORTED_TOOL_NAMES:
        return None
    raw_args = block.get("arguments")
    args: dict[str, Any] = {}
    if isinstance(raw_args, dict):
        args = raw_args
    elif isinstance(raw_args, str) and raw_args.strip():
        try:
            parsed = json.loads(raw_args)
            if isinstance(parsed, dict):
                args = parsed
        except Exception:
            args = {}
    call_id = str(block.get("call_id") or block.get("id") or f"call_{uuid.uuid4().hex[:24]}")
    return {"type": "tool_use", "id": call_id, "name": name, "input": args}


def normalize_assistant_message_for_local_tools(
    assistant_message: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Return an Anthropic-shaped assistant message for local web tool handling.

    Normal Anthropic upstreams already return ``content[].type=tool_use``.  OpenAI
    Responses upstreams return ``output[].type=function_call``; when local web
    emulation is active those function calls must be intercepted by Parrot too,
    otherwise raw WebSearch/WebFetch calls leak back to Claude Code.
    """
    if not isinstance(assistant_message, dict):
        return assistant_message
    content = assistant_message.get("content")
    if not isinstance(content, list):
        return assistant_message
    changed = False
    normalized: list[Any] = []
    for block in content:
        if isinstance(block, dict):
            tool_use = _responses_function_call_to_tool_use(block)
            if tool_use is not None:
                normalized.append(tool_use)
                changed = True
                continue
        normalized.append(block)
    if not changed:
        return assistant_message
    out = dict(assistant_message)
    out["content"] = normalized
    return out


def extract_local_tool_calls(
    assistant_message: dict[str, Any] | None,
    tools: Any = None,
    *,
    conversation_body: Any = None,
) -> list[LocalToolCall]:
    assistant_message = normalize_assistant_message_for_local_tools(assistant_message)
    calls: list[LocalToolCall] = []
    tool_options = _tool_definition_options(tools)
    known_urls = known_urls_from_body(conversation_body) if conversation_body is not None else []
    for block in _iter_content_blocks(assistant_message):
        if block.get("type") != "tool_use":
            continue
        name = str(block.get("name") or "")
        if name not in SUPPORTED_TOOL_NAMES:
            continue
        raw_input = block.get("input")
        tool_input = raw_input if isinstance(raw_input, dict) else {}
        tool_input = _merge_tool_options(tool_input, tool_options.get(name))
        if name in ("WebFetch", "web_fetch") and conversation_body is not None:
            tool_input["_known_urls"] = known_urls
        call_id = str(block.get("id") or f"call_{uuid.uuid4().hex[:24]}")
        calls.append(LocalToolCall(id=call_id, name=name, input=tool_input))
    return calls


def tool_use_count(assistant_message: dict[str, Any] | None) -> int:
    assistant_message = normalize_assistant_message_for_local_tools(assistant_message)
    return sum(
        1
        for block in _iter_content_blocks(assistant_message)
        if block.get("type") in ("tool_use", "function_call")
    )


def remove_supported_tools_from_body(body: dict[str, Any]) -> int:
    """Disable local web tools for the next model turn.

    Used after the local web loop exhausts its budget: we still append tool_result
    entries for the just-requested calls so the transcript remains valid, but we
    remove WebSearch/WebFetch definitions so the model is forced to answer from
    accumulated evidence instead of requesting more web calls forever.
    """

    tools = body.get("tools")
    if not isinstance(tools, list):
        return 0
    kept = []
    removed = 0
    for tool in tools:
        if isinstance(tool, dict) and (is_supported_tool_name(tool.get("name")) or is_anthropic_web_tool_type(tool.get("type"))):
            removed += 1
            continue
        kept.append(tool)
    if removed:
        body["tools"] = kept
        choice = body.get("tool_choice")
        if isinstance(choice, dict) and choice.get("type") == "tool" and is_supported_tool_name(choice.get("name")):
            body["tool_choice"] = {"type": "auto"}
    return removed


def round_limit_results(calls: list[LocalToolCall], max_rounds: int) -> list[LocalToolResult]:
    content = (
        "local_web_tool_round_limit_reached: Parrot has already executed "
        f"{max_rounds} local WebSearch/WebFetch round(s) for this request. "
        "No more web calls will be executed in this request; answer using the "
        "search/fetch results already present in the conversation."
    )
    return [LocalToolResult(c.id, content, is_error=True) for c in calls]


async def _call_search_service(tool_name: str, arguments: dict[str, Any], *, request_id=None,
                               round_no: int = 0) -> str:
    from . import search_service
    operation = search_service.search if tool_name == "search" else search_service.extract
    result = await operation(arguments, request_id=request_id, origin="managed_round",
                             round_no=round_no)
    return json.dumps(_model_visible_search_result(result, tool_name),
                      ensure_ascii=False, separators=(",", ":"))


# Only these are the tool result the model is entitled to see. Source identity,
# per-attempt telemetry, token usage and raw upstream bodies are Parrot's own
# accounting facts and must never enter the conversation.
_MODEL_VISIBLE_SEARCH_FIELDS = (
    "query", "url", "results", "content", "answer", "truncated", "warnings",
)


def _model_visible_search_result(result: dict, tool_name: str) -> dict:
    if not isinstance(result, dict):
        return {"content": str(result)}
    visible = {key: result[key] for key in _MODEL_VISIBLE_SEARCH_FIELDS if key in result}
    if "content_budget" in result:
        # Parrot's own adaptation note; it explains truncation to the model.
        visible["content_budget"] = result["content_budget"]
    if tool_name == "extract":
        visible.pop("results", None)
    return visible


def _valid_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _bound_content(text: str, limit: Any) -> str:
    """Conservative, inspectable content budget without guessing a tokenizer.

    At most one UTF-8 byte of fetched text/snippets is returned per requested
    token. Metadata/URLs stay intact, JSON stays valid, and truncation is explicit.
    """
    if limit is None:
        return text
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("max_content_tokens must be a positive integer")
    obj = json.loads(text)
    remaining, truncated = limit, False
    def visit(value):
        nonlocal remaining, truncated
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ("content", "snippet", "text", "answer") and isinstance(item, str):
                    raw = item.encode("utf-8")
                    clipped = raw[:remaining].decode("utf-8", errors="ignore")
                    remaining -= len(clipped.encode("utf-8"))
                    truncated |= len(raw) > len(clipped.encode("utf-8"))
                    value[key] = clipped
                elif isinstance(item, (list, dict)):
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(obj)
    obj["content_budget"] = {"requested_max_content_tokens": limit, "enforced_utf8_bytes": limit, "method": "conservative_utf8_byte_cap"}
    if truncated:
        obj["truncated"] = True
    return json.dumps(obj, ensure_ascii=False)


async def _execute_web_search(call: LocalToolCall, *, request_id=None, round_no: int = 0) -> LocalToolResult:
    from . import search_service
    query = str(call.input.get("query") or call.input.get("q") or "").strip()
    if len(query) < _min_query_chars():
        return LocalToolResult(call.id, f"invalid_input: search query is empty or too short (min {_min_query_chars()} characters)", is_error=True)
    arguments = {k: v for k, v in call.input.items() if not k.startswith("_")}
    arguments["query"] = query
    arguments.setdefault("max_results", _max_results())
    try:
        text = await _call_search_service("search", arguments, request_id=request_id, round_no=round_no)
    except search_service.SearchError as exc:
        return LocalToolResult(call.id, f"{exc.code}: {exc.message}", is_error=True)
    except Exception:
        return LocalToolResult(call.id, "search_failed: search service failed", is_error=True)
    return LocalToolResult(call.id, _bound_content(text, call.input.get("max_content_tokens")))


async def _execute_web_fetch(call: LocalToolCall, *, request_id=None, round_no: int = 0) -> LocalToolResult:
    from . import search_service
    url = str(call.input.get("url") or "").strip()
    prompt = str(call.input.get("prompt") or "").strip()
    if not _valid_url(url):
        return LocalToolResult(call.id, "invalid_input: URL must be an http(s) URL", is_error=True)
    max_url_chars = _max_fetch_url_chars()
    if len(url) > max_url_chars:
        return LocalToolResult(call.id, f"url_too_long: URL exceeds {max_url_chars} characters", is_error=True)
    known_urls = call.input.get("_known_urls")
    if _require_known_url_for_fetch() and isinstance(known_urls, list):
        normalized = _normalize_url_for_policy(url)
        if normalized not in {_normalize_url_for_policy(str(u)) for u in known_urls}:
            return LocalToolResult(
                call.id,
                "url_not_allowed: WebFetch can only fetch URLs that already appeared in the conversation or prior search/fetch results",
                is_error=True,
            )
    arguments = {k: v for k, v in call.input.items() if not k.startswith("_")}
    arguments["url"] = url
    try:
        text = await _call_search_service("extract", arguments, request_id=request_id, round_no=round_no)
    except search_service.SearchError as exc:
        return LocalToolResult(call.id, f"{exc.code}: {exc.message}", is_error=True)
    except Exception:
        return LocalToolResult(call.id, "extract_failed: search service failed", is_error=True)
    # Truncate only the content value, never corrupt the structured JSON result.
    result = json.loads(text)
    if isinstance(result.get("content"), str) and len(result["content"]) > _max_fetch_chars():
        result["content"] = result["content"][:_max_fetch_chars()]
        result["truncated"] = True
    return LocalToolResult(call.id, _bound_content(json.dumps(result, ensure_ascii=False), call.input.get("max_content_tokens")))


async def execute_local_tool_call(call: LocalToolCall, *, request_id=None,
                                  round_no: int = 0) -> LocalToolResult:
    budget = call.input.get("max_content_tokens")
    if budget is not None and (isinstance(budget, bool) or not isinstance(budget, int) or budget < 1):
        return LocalToolResult(call.id, "invalid_input: max_content_tokens must be a positive integer", is_error=True)
    if call.name in ("WebSearch", "web_search"):
        return await _execute_web_search(call, request_id=request_id, round_no=round_no)
    if call.name in ("WebFetch", "web_fetch"):
        return await _execute_web_fetch(call, request_id=request_id, round_no=round_no)
    return LocalToolResult(call.id, f"unsupported local tool: {call.name}", is_error=True)


async def execute_local_tool_calls(
    calls: list[LocalToolCall],
    *,
    request_id: str | log_db.RequestLogHandle | None = None,
    round_no: int = 0,
) -> list[LocalToolResult]:
    # A concrete handle pins tool logs without retaining a global mapping after
    # model finalization. Only the public request ID goes to search adapters.
    service_request_id = request_id.request_id if isinstance(request_id, log_db.RequestLogHandle) else request_id
    # Keep order stable; run concurrently because web search/fetch is external I/O.
    async def _run(call: LocalToolCall) -> LocalToolResult:
        log_id = _record_call_start(request_id, round_no, call)
        try:
            result = await execute_local_tool_call(call, request_id=service_request_id, round_no=round_no)
        except asyncio.CancelledError:
            _record_call_finish(log_id, call, LocalToolResult(
                call.id, "cancelled: local web tool execution cancelled", is_error=True))
            raise
        _record_call_finish(log_id, call, result)
        return result

    concurrency = _max_concurrent_tool_calls()
    if concurrency > 0:
        sem = asyncio.Semaphore(concurrency)

        async def _run_limited(call: LocalToolCall) -> LocalToolResult:
            async with sem:
                return await _run(call)

        return list(await asyncio.gather(*(_run_limited(c) for c in calls)))
    return list(await asyncio.gather(*(_run(c) for c in calls)))


def append_tool_results_to_body(
    body: dict[str, Any],
    assistant_message: dict[str, Any],
    results: list[LocalToolResult],
) -> None:
    messages = body.setdefault("messages", [])
    if not isinstance(messages, list):
        body["messages"] = messages = []
    # Store the assistant tool_use turn exactly in Anthropic shape, then provide
    # user tool_result blocks.  Existing cross-protocol translators already know
    # how to turn this pair into OpenAI Chat/Responses function call history.
    messages.append({
        "role": "assistant",
        "content": list(assistant_message.get("content") or []),
    })
    messages.append({
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": r.tool_use_id,
                "content": r.content,
                "is_error": bool(r.is_error),
            }
            for r in results
        ],
    })
    choice = body.get("tool_choice")
    if isinstance(choice, dict) and choice.get("type") == "tool" and is_supported_tool_name(choice.get("name")):
        # A forced local web tool should only force the first call.  After Parrot
        # has appended the result, let the model answer instead of looping on the
        # same forced tool_choice until maxToolRounds.
        body["tool_choice"] = {"type": "auto"}


def _sse(event: str, payload: dict[str, Any]) -> bytes:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {data}\n\n".encode("utf-8")


def _message_usage(message: dict[str, Any]) -> dict[str, Any]:
    usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
    return {
        **copy.deepcopy(usage),
        "input_tokens": int(usage.get("input_tokens") or 0),
        "cache_creation_input_tokens": int(usage.get("cache_creation_input_tokens") or 0),
        "cache_read_input_tokens": int(usage.get("cache_read_input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
    }


async def _iter_anthropic_message_sse(message: dict[str, Any]):
    usage = _message_usage(message)
    start_message = {
        "id": message.get("id") or f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": message.get("model") or "parrot-local-web-tools",
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {**usage, "output_tokens": 0},
    }
    yield _sse("message_start", {"type": "message_start", "message": start_message})
    idx = 0
    for block in message.get("content") or []:
        if not isinstance(block, dict):
            continue
        typ = block.get("type")
        if typ == "text":
            yield _sse("content_block_start", {
                "type": "content_block_start",
                "index": idx,
                "content_block": {"type": "text", "text": ""},
            })
            text = str(block.get("text") or "")
            if text:
                yield _sse("content_block_delta", {
                    "type": "content_block_delta",
                    "index": idx,
                    "delta": {"type": "text_delta", "text": text},
                })
            yield _sse("content_block_stop", {"type": "content_block_stop", "index": idx})
            idx += 1
        elif typ == "tool_use":
            tool_id = str(block.get("id") or f"call_{uuid.uuid4().hex[:24]}")
            name = str(block.get("name") or "tool")
            tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
            yield _sse("content_block_start", {
                "type": "content_block_start",
                "index": idx,
                "content_block": {"type": "tool_use", "id": tool_id, "name": name, "input": {}},
            })
            if tool_input:
                yield _sse("content_block_delta", {
                    "type": "content_block_delta",
                    "index": idx,
                    "delta": {"type": "input_json_delta", "partial_json": json.dumps(tool_input, ensure_ascii=False)},
                })
            yield _sse("content_block_stop", {"type": "content_block_stop", "index": idx})
            idx += 1
        else:
            yield _sse("content_block_start", {"type": "content_block_start", "index": idx, "content_block": block})
            yield _sse("content_block_stop", {"type": "content_block_stop", "index": idx})
            idx += 1
    yield _sse("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": message.get("stop_reason") or "end_turn", "stop_sequence": message.get("stop_sequence")},
        "usage": {"output_tokens": usage.get("output_tokens", 0)},
    })
    yield _sse("message_stop", {"type": "message_stop"})


def _anthropic_error_payload_from_response(response: Response, body: bytes) -> dict[str, Any]:
    message = "upstream error"
    err_type = "api_error"
    code: str | None = None
    try:
        obj = json.loads(body.decode("utf-8")) if body else {}
    except Exception:
        obj = {}
    if isinstance(obj, dict):
        err = obj.get("error") if isinstance(obj.get("error"), dict) else obj
        if isinstance(err, dict):
            message = str(err.get("message") or message)
            err_type = str(err.get("type") or err.get("error_type") or err_type)
            raw_code = err.get("code")
            code = str(raw_code) if raw_code is not None else None
        elif obj.get("message"):
            message = str(obj.get("message"))
    if protocol_errors.is_context_length_code_or_message(code, message):
        err_type = "invalid_request_error"
        code = protocol_errors.CONTEXT_LENGTH_EXCEEDED_CODE
        message = protocol_errors.context_length_error_message_for_claude_code(message)
    error = {"type": err_type, "message": message}
    if code is not None:
        error["code"] = code
    return {"type": "error", "error": error}


async def _iter_response_as_anthropic_sse(response: Response):
    if isinstance(response, StreamingResponse):
        async for chunk in response.body_iterator:
            yield chunk
        return

    status = int(getattr(response, "status_code", 200) or 200)
    body = getattr(response, "body", b"")
    if status >= 400:
        yield _sse("error", _anthropic_error_payload_from_response(response, body))
        return

    try:
        obj = json.loads(body.decode("utf-8")) if body else None
    except Exception:
        obj = None
    if isinstance(obj, dict) and obj.get("type") == "message" and obj.get("role") == "assistant":
        async for chunk in _iter_anthropic_message_sse(obj):
            yield chunk
        return

    if body:
        yield body


def maybe_wrap_anthropic_json_response_as_sse(response: Response) -> Response:
    """Return an Anthropic SSE response when a streaming request was handled
    internally as non-streaming.

    If the response is not a successful Anthropic message JSON, leave it as-is.
    """
    status = int(getattr(response, "status_code", 200) or 200)
    if status >= 400:
        return response
    body = getattr(response, "body", b"")
    if not body:
        return response
    try:
        obj = json.loads(body.decode("utf-8"))
    except Exception:
        return response
    if not isinstance(obj, dict) or obj.get("type") != "message" or obj.get("role") != "assistant":
        return response
    headers = dict(getattr(response, "headers", {}) or {})
    headers.pop("content-length", None)
    headers.pop("content-type", None)
    return StreamingResponse(_iter_anthropic_message_sse(obj), media_type="text/event-stream", headers=headers)


def stream_anthropic_response_task_with_pings(
    task: "asyncio.Task[Response]",
    *,
    ping_interval_seconds: float = 5.0,
) -> StreamingResponse:
    """Stream Anthropic pings while a server-side local tool loop runs.

    Claude Code expects a streaming request to receive SSE traffic while the
    model/tool loop is active.  Local WebSearch/WebFetch emulation may require
    several internal non-streaming upstream turns; without keepalive events the
    client retries the request even though Parrot eventually finishes it.
    """

    async def _iter():
        interval = max(0.5, float(ping_interval_seconds or 5.0))
        while not task.done():
            yield _sse("ping", {"type": "ping"})
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=interval)
            except asyncio.TimeoutError:
                continue
        try:
            response = await task
        except Exception as exc:
            yield _sse("error", {
                "type": "error",
                "error": {"type": "api_error", "message": f"local web tool loop failed: {exc}"},
            })
            return
        async for chunk in _iter_response_as_anthropic_sse(response):
            yield chunk

    return StreamingResponse(_iter(), media_type="text/event-stream")


def _responses_error_payload_from_response(response: Response, body: bytes) -> dict[str, Any]:
    try:
        obj = json.loads(body.decode("utf-8")) if body else {}
    except Exception:
        obj = {}
    err = obj.get("error") if isinstance(obj, dict) and isinstance(obj.get("error"), dict) else {}
    message = str(err.get("message") or (obj.get("message") if isinstance(obj, dict) else "") or "upstream error")
    return {
        "type": "response.failed",
        "response": {
            "id": f"resp_{uuid.uuid4().hex[:24]}",
            "object": "response",
            "created_at": int(__import__("time").time()),
            "status": "failed",
            "error": {"type": str(err.get("type") or "api_error"), "message": message},
            "output": [],
        },
    }


async def _iter_openai_response_sse(response_obj: dict[str, Any]):
    response = dict(response_obj)
    response.setdefault("id", f"resp_{uuid.uuid4().hex[:24]}")
    response.setdefault("object", "response")
    response.setdefault("created_at", int(__import__("time").time()))
    output = response.get("output") if isinstance(response.get("output"), list) else []

    created = dict(response)
    created["status"] = "in_progress"
    created["output"] = []
    yield _sse("response.created", {"type": "response.created", "response": created})
    yield _sse("response.in_progress", {"type": "response.in_progress", "response": created})

    for idx, item in enumerate(output):
        if not isinstance(item, dict):
            continue
        yield _sse("response.output_item.added", {
            "type": "response.output_item.added",
            "output_index": idx,
            "item": item,
        })
        if item.get("type") == "message":
            content = item.get("content") if isinstance(item.get("content"), list) else []
            for cidx, part in enumerate(content):
                if not isinstance(part, dict):
                    continue
                yield _sse("response.content_part.added", {
                    "type": "response.content_part.added",
                    "item_id": item.get("id"),
                    "output_index": idx,
                    "content_index": cidx,
                    "part": part,
                })
                if part.get("type") == "output_text":
                    text = str(part.get("text") or "")
                    if text:
                        yield _sse("response.output_text.delta", {
                            "type": "response.output_text.delta",
                            "item_id": item.get("id"),
                            "output_index": idx,
                            "content_index": cidx,
                            "delta": text,
                        })
                    yield _sse("response.output_text.done", {
                        "type": "response.output_text.done",
                        "item_id": item.get("id"),
                        "output_index": idx,
                        "content_index": cidx,
                        "text": text,
                    })
                yield _sse("response.content_part.done", {
                    "type": "response.content_part.done",
                    "item_id": item.get("id"),
                    "output_index": idx,
                    "content_index": cidx,
                    "part": part,
                })
        yield _sse("response.output_item.done", {
            "type": "response.output_item.done",
            "output_index": idx,
            "item": item,
        })

    response["status"] = response.get("status") or "completed"
    yield _sse("response.completed", {"type": "response.completed", "response": response})


def maybe_wrap_responses_json_response_as_sse(response: Response) -> Response:
    status = int(getattr(response, "status_code", 200) or 200)
    if status >= 400:
        return response
    body = getattr(response, "body", b"")
    if not body:
        return response
    try:
        obj = json.loads(body.decode("utf-8"))
    except Exception:
        return response
    if not isinstance(obj, dict) or obj.get("object") != "response":
        return response
    headers = dict(getattr(response, "headers", {}) or {})
    headers.pop("content-length", None)
    headers.pop("content-type", None)
    return StreamingResponse(_iter_openai_response_sse(obj), media_type="text/event-stream", headers=headers)


def stream_responses_response_task_with_pings(
    task: "asyncio.Task[Response]",
    *,
    ping_interval_seconds: float = 5.0,
) -> StreamingResponse:
    async def _iter():
        interval = max(0.5, float(ping_interval_seconds or 5.0))
        while not task.done():
            yield _sse("response.ping", {"type": "response.ping"})
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=interval)
            except asyncio.TimeoutError:
                continue
        try:
            response = await task
        except Exception as exc:
            yield _sse("response.failed", {
                "type": "response.failed",
                "response": {
                    "id": f"resp_{uuid.uuid4().hex[:24]}",
                    "object": "response",
                    "created_at": int(__import__("time").time()),
                    "status": "failed",
                    "error": {"type": "api_error", "message": f"local web tool loop failed: {exc}"},
                    "output": [],
                },
            })
            return
        status = int(getattr(response, "status_code", 200) or 200)
        body = getattr(response, "body", b"")
        if status >= 400:
            yield _sse("response.failed", _responses_error_payload_from_response(response, body))
            return
        try:
            obj = json.loads(body.decode("utf-8")) if body else None
        except Exception:
            obj = None
        if isinstance(obj, dict) and obj.get("object") == "response":
            async for chunk in _iter_openai_response_sse(obj):
                yield chunk
            return
        if body:
            yield body

    return StreamingResponse(_iter(), media_type="text/event-stream")


def tool_reference_text(item: dict[str, Any]) -> str:
    name = str(item.get("tool_name") or item.get("name") or "").strip()
    if name:
        return f"Tool reference: {name}"
    return "Tool reference"
