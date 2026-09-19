"""Search ownership and transport-independent execution, before candidate translation.

Only declarations authorize execution. Compiled functions never cross the downstream
boundary. Mixed rounds retain their actual transcript, scoped to the API key/model,
so a client result can resume without rerunning the managed side of the round.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from fastapi.responses import JSONResponse, Response, StreamingResponse

from . import local_web_tools as web
from .openai.transform.guard import GuardError

ROUND_KEY = "_parrot_search_round"
PLAN_KEY = "_parrot_search_plan"


def kind(tool: Any) -> str | None:
    if not isinstance(tool, dict):
        return None
    typ = str(tool.get("type") or "")
    if typ in web.OPENAI_WEB_SEARCH_TOOL_TYPES or typ == "web_fetch" or typ.startswith(("web_search_", "web_fetch_")):
        return "hosted"
    fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
    if typ in ("", "function") and fn.get("name") in web.SUPPORTED_TOOL_NAMES:
        return "function"
    return None


def declarations(body: dict):
    for tool in body.get("tools") or []:
        if isinstance(tool, dict) and tool.get("type") == "namespace":
            for child in tool.get("tools") or []:
                yield child, str(tool.get("name") or "")
        else:
            yield tool, ""
    for fn in body.get("functions") or []:
        yield {"type": "function", "function": fn}, ""


def validate(body: dict) -> None:
    # Anthropic ingress invokes this before its protocol guard. Do not let
    # malformed collections become a Python iteration error (HTTP 500).
    for field in ("tools", "functions"):
        if body.get(field) is not None and not isinstance(body[field], list):
            raise GuardError(400, "invalid_request_error", f"{field} must be an array", param=field)
    for tool in body.get("tools") or []:
        if isinstance(tool, dict) and tool.get("type") == "namespace":
            if tool.get("tools") is not None and not isinstance(tool["tools"], list):
                raise GuardError(400, "invalid_request_error", "namespace tools must be an array", param="tools")
    settings = web._settings()
    for tool, _ in declarations(body):
        category = kind(tool)
        if category and settings.get(category + "Mode", "managed") == "managed":
            source = tool.get("function") if isinstance(tool.get("function"), dict) else tool
            try:
                _validate_domain_options(source)
            except ValueError as exc:
                raise GuardError(400, "invalid_request_error", str(exc), param="tools") from None
            for key in ("max_uses", "max_content_tokens"):
                if key in source and (isinstance(source[key], bool) or not isinstance(source[key], int) or source[key] < (1 if key == "max_content_tokens" else 0)):
                    raise GuardError(400, "invalid_request_error", f"{key} must be a valid non-negative integer budget", param="tools")
        if category and settings.get(category + "Mode", "managed") == "disabled":
            raise GuardError(400, "invalid_request_error", f"search_{category}_disabled: {category} search tools are disabled by Parrot policy", param="tools")


def needs_loop(body: dict) -> bool:
    if body.get(ROUND_KEY):
        return False
    settings = web._settings()
    return any(kind(t) and settings.get(kind(t) + "Mode", "managed") == "managed" for t, _ in declarations(body))


@dataclass
class ManagedTool:
    name: str
    operation: str
    category: str
    source: dict
    namespace: str = ""


def _schema(operation: str) -> dict:
    if operation == "web_fetch":
        return {"type": "object", "properties": {"url": {"type": "string"}, "prompt": {"type": "string"}}, "required": ["url"]}
    return web._openai_web_search_function_tool()["parameters"]


def compile_request(body: dict, protocol: str) -> tuple[dict, dict[tuple[str, str], ManagedTool]]:
    """Compile only managed declarations in their original ingress protocol.

    This is provider-neutral. Each failover candidate subsequently translates
    this fresh canonical request and applies its own wire compatibility map.
    """
    validate(body)
    out = dict(body)
    tools = copy.deepcopy(body.get("tools") or [])
    settings = web._settings()
    reserved = {str((t.get("function") or t).get("name") or "") for t, _ in declarations(body) if isinstance(t, dict)}
    plan: dict[tuple[str, str], ManagedTool] = {}
    choices = {}

    def convert(tool, namespace=""):
        category = kind(tool)
        if not category or settings.get(category + "Mode", "managed") != "managed":
            return tool
        source = copy.deepcopy(tool)
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        operation = "web_fetch" if (str(tool.get("type") or "") == "web_fetch" or str(tool.get("type") or "").startswith("web_fetch_")) or fn.get("name") in ("WebFetch", "web_fetch") else "web_search"
        name = str(fn.get("name") or operation)
        if category == "hosted":
            stem = "parrot_hosted_" + operation
            name = stem
            suffix = 0
            while name in reserved:
                suffix += 1
                name = stem + "_" + str(suffix)
            reserved.add(name)
            choices[(str(tool.get("type")), str(tool.get("name") or ""))] = name
            description = tool.get("description") or ("Search the web using Parrot." if operation == "web_search" else "Fetch a known URL using Parrot.")
            if protocol == "anthropic":
                tool = {"name": name, "description": description, "input_schema": _schema(operation)}
            elif protocol == "chat":
                tool = {"type": "function", "function": {"name": name, "description": description, "parameters": _schema(operation)}}
            else:
                tool = {"type": "function", "name": name, "description": description, "parameters": _schema(operation)}
        plan[(namespace, name)] = ManagedTool(name, operation, category, source, namespace)
        return tool

    for idx, tool in enumerate(tools):
        if isinstance(tool, dict) and tool.get("type") == "namespace":
            tool["tools"] = [convert(c, str(tool.get("name") or "")) for c in tool.get("tools") or []]
        else:
            tools[idx] = convert(tool)
    for fn in body.get("functions") or []:
        convert({"type": "function", "function": fn})
    if "tools" in body:
        out["tools"] = tools

    def choice_map(value):
        if not isinstance(value, dict):
            return value
        result = copy.deepcopy(value)
        target = choices.get((str(value.get("type")), str(value.get("name") or "")))
        if not target and kind(value) == "hosted":
            operation = "web_fetch" if str(value.get("type")).startswith("web_fetch") else "web_search"
            target = next((tool.name for tool in plan.values() if tool.category == "hosted" and tool.operation == operation), None)
        if protocol == "anthropic" and value.get("type") == "tool":
            target = next((n for (_, old), n in choices.items() if old == value.get("name")), None)
        if target:
            return {"type": "tool", "name": target} if protocol == "anthropic" else ({"type": "function", "function": {"name": target}} if protocol == "chat" else {"type": "function", "name": target})
        if isinstance(value.get("tools"), list):
            result["tools"] = [choice_map(t) for t in value["tools"]]
        return result

    if "tool_choice" in body:
        out["tool_choice"] = choice_map(body["tool_choice"])
    out[ROUND_KEY] = True
    out["_parrot_search_original_tools"] = copy.deepcopy(body.get("tools"))
    return out, plan


def _validate_domain_options(options: dict) -> None:
    filters = options.get("filters")
    if filters is not None and not isinstance(filters, dict):
        raise ValueError("filters must be an object")
    for values in (options, filters or {}):
        for field in ("allowed_domains", "blocked_domains", "excluded_domains"):
            domains = values.get(field)
            if domains is not None and (not isinstance(domains, list) or
                                        any(not isinstance(domain, str) for domain in domains)):
                raise ValueError(f"{field} must be an array of strings")


def constraints(tool: ManagedTool, args: dict) -> dict:
    source = tool.source.get("function") if isinstance(tool.source.get("function"), dict) else tool.source
    _validate_domain_options(source)
    _validate_domain_options(args)
    options = dict(source.get("filters") or {})
    for key in ("allowed_domains", "blocked_domains", "excluded_domains", "external_web_access", "freshness", "language", "country", "max_results", "search_context_size", "user_location", "max_uses", "max_content_tokens"):
        if key in source:
            options[key] = source[key]
    # Definition constraints always dominate model-generated arguments.
    merged = web._merge_tool_options(args, options)
    blocked = list(dict.fromkeys([*(options.get("blocked_domains") or []), *(options.get("excluded_domains") or []), *(args.get("blocked_domains") or []), *(args.get("excluded_domains") or [])]))
    if blocked:
        merged["blocked_domains"] = blocked
    if options.get("external_web_access") is False or options.get("external_web_access") in ("cached", "indexed"):
        merged["external_web_access"] = options["external_web_access"]
    for key in ("freshness", "language", "country", "max_results", "search_context_size", "user_location", "max_uses", "max_content_tokens"):
        if key in options:
            merged[key] = copy.deepcopy(options[key])
    return merged


def _calls(obj: dict, protocol: str):
    if protocol == "responses":
        for item in obj.get("output") or []:
            if isinstance(item, dict) and item.get("type") == "function_call":
                yield item, str(item.get("call_id") or item.get("id") or ""), str(item.get("name") or ""), item.get("arguments"), str(item.get("namespace") or "")
    elif protocol == "anthropic":
        for item in obj.get("content") or []:
            if isinstance(item, dict) and item.get("type") == "tool_use":
                yield item, str(item.get("id") or ""), str(item.get("name") or ""), item.get("input"), ""
    else:
        for choice in obj.get("choices") or []:
            msg = choice.get("message") or {}
            for item in msg.get("tool_calls") or []:
                fn = item.get("function") or {}
                yield item, str(item.get("id") or ""), str(fn.get("name") or ""), fn.get("arguments"), ""
            if isinstance(msg.get("function_call"), dict):
                fn = msg["function_call"]
                yield fn, str(fn.get("name") or ""), str(fn.get("name") or ""), fn.get("arguments"), ""


def _append(body: dict, obj: dict, results: list[web.LocalToolResult], protocol: str) -> None:
    if protocol == "responses":
        inp = body.get("input")
        if isinstance(inp, str):
            inp = [{"role": "user", "content": inp}]
        body["input"] = copy.deepcopy(inp or []) + copy.deepcopy(obj.get("output") or []) + [
            {"type": "function_call_output", "call_id": r.tool_use_id, "output": r.content} for r in results]
    elif protocol == "anthropic":
        body["messages"] = copy.deepcopy(body.get("messages") or [])
        web.append_tool_results_to_body(body, obj, results)
    else:
        body["messages"] = copy.deepcopy(body.get("messages") or [])
        # Each continuation is a single branch, never a bag of alternatives.
        if len(obj.get("choices") or []) > 1:
            raise ValueError("cannot append independent Chat choices to one history")
        for choice in obj.get("choices") or []:
            msg = copy.deepcopy(choice.get("message") or {})
            body["messages"].append(msg)
            ids = {str(c.get("id") or "") for c in msg.get("tool_calls") or []}
            legacy = msg.get("function_call")
            for r in results:
                if r.tool_use_id in ids:
                    body["messages"].append({"role": "tool", "tool_call_id": r.tool_use_id, "content": r.content})
                elif isinstance(legacy, dict) and legacy.get("name") == r.tool_use_id:
                    body["messages"].append({"role": "function", "name": r.tool_use_id, "content": r.content})
    # Release only a requirement actually satisfied by the managed calls;
    # allowed-tool sets and unrelated forced client choices remain constraints.
    choice = body.get("tool_choice")
    ids = {r.tool_use_id for r in results}
    names = {c[2] for c in _calls(obj, protocol) if c[1] in ids}
    if results and choice == "required":
        body["tool_choice"] = "auto"
    elif results and isinstance(choice, dict):
        selected = (choice.get("function") or choice).get("name")
        if choice.get("type") == "any" or selected in names:
            body["tool_choice"] = ({**choice, "type": "auto"} if protocol == "anthropic" else "auto")
            if isinstance(body["tool_choice"], dict):
                body["tool_choice"].pop("name", None)
        elif choice.get("type") == "allowed_tools":
            choice = copy.deepcopy(choice)
            target = choice.get("allowed_tools") if isinstance(choice.get("allowed_tools"), dict) else choice
            if target.get("mode") == "required":
                target["mode"] = "auto"
            body["tool_choice"] = choice
    legacy = body.get("function_call")
    if results and (legacy == "auto" or isinstance(legacy, dict) and legacy.get("name") in names):
        body["function_call"] = "auto"


# Reference lookup never authorizes replacing a conflicting supplied transcript.
_REPLAY: OrderedDict[tuple, tuple[float, dict, list]] = OrderedDict()
_REPLAY_LIMIT = 256
_REPLAY_TTL = 1800


def _scope(body, protocol, api_key_name):
    return (api_key_name or "", str(body.get("model") or ""), protocol)


def _history(body, protocol):
    items = body.get("input" if protocol == "responses" else "messages") or []
    return [{"role": "user", "content": items}] if isinstance(items, str) else items


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _remember(body, protocol, api_key_name, references, *, visible_body=None):
    now = time.monotonic()
    visible = copy.deepcopy(_history(visible_body if visible_body is not None else body, protocol))
    saved = copy.deepcopy(body)
    # Colliding references, even with different hidden results for identical
    # visible prompts, must remain ambiguous rather than last-writer-wins.
    identity = _digest([visible, _history(saved, protocol)])
    for reference in references:
        if reference:
            key = (*_scope(body, protocol, api_key_name), reference, identity)
            _REPLAY[key] = (now, saved, visible)
            _REPLAY.move_to_end(key)
    while _REPLAY and (len(_REPLAY) > _REPLAY_LIMIT or now - next(iter(_REPLAY.values()))[0] > _REPLAY_TTL):
        _REPLAY.popitem(last=False)


def _result_refs(item):
    if not isinstance(item, dict):
        return []
    if item.get("type") == "function_call_output":
        return [(str(item.get("call_id") or ""), False)]
    if item.get("role") == "tool":
        return [(str(item.get("tool_call_id") or ""), False)]
    if item.get("role") == "function":
        return [(str(item.get("name") or ""), True)]
    blocks = item.get("content")
    return [(str(b.get("tool_use_id") or ""), False) for b in blocks
            if isinstance(b, dict) and b.get("type") == "tool_result"] if isinstance(blocks, list) else []


def _split_history_prefix(items, prefix, protocol):
    if items[:len(prefix)] == prefix:
        return items[len(prefix):]
    # Anthropic merges adjacent user turns; a client result can extend the
    # final internally-owned result turn without changing its existing blocks.
    if protocol == "anthropic" and prefix and len(items) >= len(prefix) and items[:len(prefix)-1] == prefix[:-1]:
        old, new = prefix[-1], items[len(prefix)-1]
        if isinstance(old, dict) and isinstance(new, dict) and old.get("role") == new.get("role") == "user" and isinstance(old.get("content"), list) and isinstance(new.get("content"), list) and new["content"][:len(old["content"])] == old["content"]:
            return [{**new, "content": new["content"][len(old["content"]):]}, *items[len(prefix):]]
    return None


def _visible_body(body, protocol, api_key_name):
    """Inverse projection after ingress already expanded replay, without a
    client-spoofable private marker or including hidden rounds in future keys.
    """
    items = _history(body, protocol)
    best = None
    for key, (when, saved, visible) in list(_REPLAY.items()):
        if key[:3] != _scope(body, protocol, api_key_name) or time.monotonic() - when > _REPLAY_TTL:
            continue
        prefix = _history(saved, protocol)
        tail = _split_history_prefix(items, prefix, protocol)
        if tail is not None and (best is None or len(prefix) > best[0]):
            best = (len(prefix), visible, tail)
    if best is None:
        return copy.deepcopy(body)
    out = copy.deepcopy(body)
    out["input" if protocol == "responses" else "messages"] = copy.deepcopy(best[1] + best[2])
    return out


def restore_replay(body: dict, protocol: str, api_key_name: str | None) -> dict:
    """Match full visible history; never use a legacy name without that proof.

    Tool IDs (modern or legacy) are not session credentials: both require a
    matching full transcript. Only Responses' explicit previous_response_id can
    select an unambiguous delta snapshot. Retries never mutate that snapshot.
    """
    field = "input" if protocol == "responses" else "messages"
    items = _history(body, protocol)
    previous = str(body.get("previous_response_id") or "") if protocol == "responses" else ""
    refs = {ref: legacy for item in items for ref, legacy in _result_refs(item) if ref}
    if previous:
        refs[previous] = False
    matches = {}
    for key, record in list(_REPLAY.items()):
        if key[:3] != _scope(body, protocol, api_key_name) or key[3] not in refs:
            continue
        if time.monotonic() - record[0] > _REPLAY_TTL:
            continue
        _, saved, visible = record
        reference, history = key[3], _history(saved, protocol)
        expanded_tail = _split_history_prefix(items, history, protocol) if history else None
        if expanded_tail is not None:
            if any(reference == r for item in expanded_tail for r, _ in _result_refs(item)):
                return body
        visible_tail = _split_history_prefix(items, visible, protocol) if visible else None
        if visible_tail is not None:
            tail = visible_tail
            if reference != previous and not any(reference == r for item in tail for r, _ in _result_refs(item)):
                continue
            strength = len(visible)
        elif reference == previous:
            if any(isinstance(i, dict) and (i.get("role") == "assistant" or i.get("type") == "function_call") for i in items):
                continue
            tail, strength = items, 0
        else:
            continue
        matches[key[4]] = (strength, saved, tail)
    if not matches:
        return body
    strongest = max(m[0] for m in matches.values())
    matches = [m for m in matches.values() if m[0] == strongest]
    if len(matches) != 1:
        return body
    _, saved, tail = matches[0]
    out = dict(body)
    original_tools = saved.get("_parrot_search_original_tools", saved.get("tools"))
    if "tools" not in body and original_tools is not None:
        out["tools"] = copy.deepcopy(original_tools)
    if "functions" not in body and "functions" in saved:
        out["functions"] = copy.deepcopy(saved["functions"])
    history, tail = copy.deepcopy(_history(saved, protocol)), copy.deepcopy(tail)
    if protocol == "anthropic" and history and tail and history[-1].get("role") == "user" and tail[0].get("role") == "user" and isinstance(history[-1].get("content"), list) and isinstance(tail[0].get("content"), list):
        history[-1]["content"].extend(tail.pop(0)["content"])
    out[field] = history + tail
    out.pop("previous_response_id", None)
    return out


def _hide_calls(obj: dict, protocol: str, ids: set[str]) -> dict:
    out = copy.deepcopy(obj)
    if protocol == "responses":
        out["output"] = [b for b in out.get("output") or [] if not (b.get("type") == "function_call" and str(b.get("call_id") or b.get("id")) in ids)]
    elif protocol == "anthropic":
        out["content"] = [b for b in out.get("content") or [] if not (b.get("type") == "tool_use" and str(b.get("id")) in ids)]
    else:
        for c in out.get("choices") or []:
            m = c.get("message") or {}
            m["tool_calls"] = [b for b in m.get("tool_calls") or [] if str(b.get("id")) not in ids]
            if isinstance(m.get("function_call"), dict) and m["function_call"].get("name") in ids:
                m.pop("function_call")
    return out


def advance_route(route, response):
    """Continue the successful candidate; do not replenish failed predecessors."""
    winner = getattr(response, "_parrot_search_candidate", None)
    if winner is None:
        return route
    out = copy.copy(route)
    candidates = list(route.candidates)
    try:
        index = candidates.index(winner)
        out.candidates = candidates[index:]
    except ValueError:
        out.candidates = [winner]
    out.saturated = [pair for pair in route.saturated if pair != winner]
    out.bound_channel_key = winner[0].key
    return out


def _sum_usage(total: dict, usage: dict) -> None:
    """Add independent round counters, including nested cache/reasoning/cost.

    Only complete model responses enter here, not cumulative SSE snapshots.
    Replay snapshots contain no accumulated usage from earlier client requests.
    """
    for key, value in usage.items():
        if isinstance(value, dict):
            if not isinstance(total.get(key), dict):
                total[key] = {}
            _sum_usage(total[key], value)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            prior = total.get(key)
            total[key] = (prior if isinstance(prior, (int, float)) and not isinstance(prior, bool) else 0) + value
        elif key not in total or value is not None:
            total[key] = copy.deepcopy(value)


def _restore_hosted_metadata(obj: dict, plan: dict) -> None:
    """Project only compiled tool declarations/references back to public names.

    Do not rewrite arbitrary output text, user functions or provider metadata.
    Compiled hosted aliases are unique across namespaces by construction.
    """
    hosted = {tool.name: tool.source for tool in plan.values() if tool.category == "hosted"}
    if not hosted:
        return

    def restore(value, *, choice=False):
        if isinstance(value, list):
            return [restore(item, choice=choice) for item in value]
        if not isinstance(value, dict):
            return value
        if value.get("type") == "function" and value.get("name") in hosted:
            source = hosted[value["name"]]
            if not choice:
                return copy.deepcopy(source)
            return {**{k: copy.deepcopy(v) for k, v in value.items() if k not in ("type", "name")},
                    "type": source["type"]}
        out = dict(value)
        for key in ("tools", "allowed_tools"):
            if key in out:
                out[key] = restore(out[key], choice=choice)
        return out

    for field in ("tools", "tool_choice"):
        if field in obj:
            obj[field] = restore(obj[field], choice=field == "tool_choice")


def _json_response(obj, response):
    return JSONResponse(obj, status_code=response.status_code, headers={
        k: v for k, v in response.headers.items() if k.lower() not in ("content-length", "content-type")})


def _tool_error(message, code="invalid_search_tool_call"):
    return JSONResponse({"error": {"type": "api_error", "code": code, "message": message}}, status_code=502)


def _unique_calls(obj, protocol):
    """Validate the whole batch before any side effect; coalesce duplicates."""
    seen, duplicates = {}, set()
    for entry in _calls(obj, protocol):
        _, call_id, name, args, namespace = entry
        try:
            args = json.loads(args) if isinstance(args, str) else args
        except ValueError:
            pass
        identity = (namespace, name, _digest(args))
        if call_id in seen:
            if seen[call_id] != identity:
                raise ValueError("tool_call_id_conflict: one call ID has different names or arguments")
            duplicates.add(call_id)
        seen[call_id] = identity
    if not duplicates:
        return obj
    out = copy.deepcopy(obj)
    emitted = set()
    def keep(item, key):
        ident = str(item.get(key) or item.get("id") or "")
        if ident not in duplicates:
            return True
        if ident in emitted:
            return False
        emitted.add(ident)
        return True
    if protocol == "responses":
        out["output"] = [i for i in out.get("output") or [] if i.get("type") != "function_call" or keep(i, "call_id")]
    elif protocol == "anthropic":
        out["content"] = [i for i in out.get("content") or [] if i.get("type") != "tool_use" or keep(i, "id")]
    else:
        for choice in out.get("choices") or []:
            msg = choice.get("message") or {}
            if "tool_calls" in msg:
                msg["tool_calls"] = [i for i in msg["tool_calls"] if keep(i, "id")]
    return out


async def run(body: dict, protocol: str, invoke, *, request_id=None, api_key_name=None, _stream=None) -> Response:
    body = restore_replay(body, protocol, api_key_name)
    visible_base = _visible_body(body, protocol, api_key_name)
    current, plan = compile_request(body, protocol)
    current["stream"] = _stream is not None
    if _stream is not None:
        _stream.plan = plan
        # Internal Chat rounds must report usage, independently of whether the
        # client asked for a downstream usage frame.
        if protocol == "chat":
            current["stream_options"] = {**(current.get("stream_options") or {}), "include_usage": True}
    else:
        current.pop("stream_options", None)
    usage, usage_seen = {}, False
    if _stream is not None:
        _stream.usage = usage
    uses: dict[tuple[str, str], int] = {}  # one request budget, including all Chat branches
    first_attempt_handle = None  # one concrete monthly DB for every branch/round

    async def model_round(request_body, branch_index=None):
        nonlocal usage_seen, first_attempt_handle
        # finish_success releases the request binding after every model round.
        # Rebind only for a genuine next invoke, not while executing tools or
        # returning a terminal/mixed response. retain is memory-only and runs
        # synchronously so cancellation cannot leave a detached worker rebinding.
        retained = None
        if request_id and first_attempt_handle is not None:
            retained = web.log_db.retain_request_handle(request_id, first_attempt_handle)
        try:
            response = await invoke(request_body)
            attempt_handle = getattr(response, "_parrot_search_attempt_handle", None)
            if first_attempt_handle is None and isinstance(attempt_handle, web.log_db.RowLogHandle):
                first_attempt_handle = attempt_handle
            if _stream is not None and response.status_code < 400:
                response = await _stream.consume(response, branch_index)
        finally:
            if retained is not None:
                # Normal failover finalization already removes this mapping.
                # Also clean our own binding when invoke raises/cancels before
                # its finalizer; never remove a replacement owned by another call.
                with web.log_db._write_lock:
                    if web.log_db._request_handles.get(retained.request_id) is retained:
                        web.log_db._request_handles.pop(retained.request_id, None)
        if response.status_code >= 400:
            return response, None
        try:
            obj = json.loads(response.body)
        except (ValueError, AttributeError):
            return _tool_error("managed search expected a complete model response"), None
        if isinstance(obj.get("usage"), dict):
            usage_seen = True
            _sum_usage(usage, obj["usage"])
        return response, obj

    async def branch(request_body, response, obj, branch_index=None):
        # Each Chat alternative owns an independent history and execution state.
        executed, executed_ids = {}, {}
        completed_ids = {ref for item in _history(request_body, protocol) for ref, legacy in _result_refs(item) if not legacy}
        for round_no in range(web.max_tool_rounds() + 1):
            try:
                obj = _unique_calls(obj, protocol)
            except ValueError as exc:
                return _tool_error(str(exc), "tool_call_id_conflict"), None
            calls = list(_calls(obj, protocol))
            owned = [(entry, plan[(entry[4], entry[2])]) for entry in calls if (entry[4], entry[2]) in plan]
            if not owned:
                final = copy.deepcopy(request_body)
                _append(final, obj, [], protocol)
                if _stream is not None:
                    obj = await _stream.finish_branch(obj, branch_index)
                visible = copy.deepcopy(visible_base)
                _append(visible, obj, [], protocol)
                refs = [c[1] for c in calls]
                if protocol == "responses":
                    refs.append(str(obj.get("id") or ""))
                _remember(final, protocol, api_key_name, refs, visible_body=visible)
                return response, obj
            if round_no >= web.max_tool_rounds():
                return JSONResponse({"error": {"type": "invalid_request_error", "code": "search_tool_round_limit", "message": "Parrot managed search exceeded maxToolRounds"}}, status_code=400), None
            results, pending = [], []
            for entry, tool in owned:
                _, call_id, _, raw_args, namespace = entry
                if not call_id:
                    return _tool_error("managed search call is missing its result ID"), None
                try:
                    args = raw_args if isinstance(raw_args, dict) else json.loads(raw_args or "{}")
                    if not isinstance(args, dict):
                        raise ValueError()
                except (ValueError, TypeError):
                    results.append(web.LocalToolResult(call_id, "invalid_input: tool arguments must be a JSON object", True))
                    continue
                try:
                    merged = constraints(tool, args)
                except ValueError as exc:
                    results.append(web.LocalToolResult(call_id, f"invalid_input: {exc}", True))
                    continue
                if tool.operation == "web_fetch":
                    merged["_known_urls"] = web.known_urls_from_body(request_body)
                identity = (call_id, namespace, tool.name, _digest({k: v for k, v in merged.items() if not k.startswith("_")}))
                legacy = protocol == "chat" and entry[0].get("name") == call_id and "type" not in entry[0]
                if not legacy and call_id in completed_ids:
                    return _tool_error("tool_call_id_conflict: call ID already has a completed result in this history", "tool_call_id_conflict"), None
                if not legacy and call_id in executed_ids and executed_ids[call_id] != identity:
                    return _tool_error("tool_call_id_conflict: a completed call ID cannot be reused with different arguments", "tool_call_id_conflict"), None
                if identity in executed:
                    results.append(executed[identity])
                else:
                    budget_key = (namespace, tool.name)
                    source = tool.source.get("function") or tool.source
                    limit = source.get("max_uses")
                    if limit is not None and uses.get(budget_key, 0) >= limit:
                        results.append(web.LocalToolResult(call_id, "max_uses_exceeded: declared per-request search tool budget exhausted", True))
                        continue
                    uses[budget_key] = uses.get(budget_key, 0) + 1
                    pending.append((identity, web.LocalToolCall(call_id, tool.operation, merged)))
            tool_request = request_id
            if request_id and first_attempt_handle is not None:
                tool_request = web.log_db.RequestLogHandle(
                    request_id=first_attempt_handle.request_id, db=first_attempt_handle.db,
                )
            new_results = await web.execute_local_tool_calls([p[1] for p in pending], request_id=tool_request, round_no=round_no + 1)
            for (identity, _), result in zip(pending, new_results):
                executed[identity] = result
                executed_ids[result.tool_use_id] = identity
                results.append(result)
            _append(request_body, obj, results, protocol)
            completed_ids.update(ref for item in _history(request_body, protocol) for ref, legacy in _result_refs(item) if not legacy)
            managed_ids = {c[0][1] for c in owned}
            external = [c for c in calls if c[1] not in managed_ids]
            if external:
                visible_obj = _hide_calls(obj, protocol, managed_ids)
                if _stream is not None:
                    visible_obj = await _stream.finish_branch(visible_obj, branch_index)
                visible = copy.deepcopy(visible_base)
                _append(visible, visible_obj, [], protocol)
                refs = [c[1] for c in external]
                if protocol == "responses":
                    refs.append(str(visible_obj.get("id") or ""))
                _remember(request_body, protocol, api_key_name, refs, visible_body=visible)
                return response, visible_obj
            response, obj = await model_round(request_body, branch_index)
            if obj is None:
                return response, None
        raise AssertionError("unreachable search loop")

    def finish_request(response=None, *, status="error", http_status=500, message=None):
        # Before the first successful model round, failover owns finalization.
        # Afterwards the logical request can fail/cancel without another model
        # invoke. Pin its original month and update only the request outcome;
        # individual model rounds must keep their successful billing facts.
        if not request_id or first_attempt_handle is None:
            return
        try:
            text = None
            if response is not None:
                http_status = response.status_code
                status = "error" if http_status >= 400 else "success"
                text = response.body.decode("utf-8")
                if status == "error":
                    obj = json.loads(text)
                    error = obj.get("error") if isinstance(obj, dict) else None
                    message = str(error.get("message") or error.get("code") or "managed search failed") if isinstance(error, dict) else "managed search failed"
            handle = web.log_db.RequestLogHandle(
                request_id=first_attempt_handle.request_id, db=first_attempt_handle.db)
            web.log_db.finish_managed_search_request(
                handle, status=status, http_status=http_status,
                error_message=message, response_body=text)
        except Exception:
            # Logging must neither replace a response nor swallow cancellation.
            pass

    async def complete():
        response, first = await model_round(current)
        if first is None:
            return response
        if protocol == "chat":
            # n applies only to the initial generation, never to branch continuations.
            final = copy.deepcopy(first)
            final["choices"] = []
            initial_response = response
            initial_choices = first.get("choices") or []
            for choice in initial_choices:
                index = choice.get("index", 0)
                one = {**first, "choices": [copy.deepcopy(choice)]}
                branch_body = copy.deepcopy(current)
                branch_body["n"] = 1
                response, result = await branch(branch_body, initial_response, one, index)
                if result is None:
                    return response
                returned = result.get("choices") or []
                if len(returned) != 1:
                    return _tool_error("a Chat branch continuation must return exactly one choice")
                if len(initial_choices) == 1:
                    final = {**result, "choices": []}
                completed = copy.deepcopy(returned[0])
                completed["index"] = index
                final["choices"].append(completed)
        else:
            response, final = await branch(current, response, first)
            if final is None:
                return response
        if usage_seen:
            final["usage"] = usage
        if protocol == "responses":
            _restore_hosted_metadata(final, plan)
        if _stream is not None:
            await _stream.commit_tools()
        return _json_response(final, response)

    try:
        response = await complete()
    except asyncio.CancelledError:
        finish_request(status="cancelled", http_status=499, message="managed search cancelled")
        raise
    except Exception:
        finish_request(message="managed search failed")
        raise
    finish_request(response)
    return response


async def _chat_sse(obj):
    base = {k: obj[k] for k in ("id", "created", "model", "system_fingerprint") if k in obj}
    base["object"] = "chat.completion.chunk"
    for choice in obj.get("choices") or []:
        msg = copy.deepcopy(choice.get("message") or {})
        for idx, call in enumerate(msg.get("tool_calls") or []):
            call["index"] = idx
        yield b"data: " + json.dumps({**base, "choices": [{"index": choice.get("index", 0), "delta": msg, "finish_reason": None}]}).encode() + b"\n\n"
        yield b"data: " + json.dumps({**base, "choices": [{"index": choice.get("index", 0), "delta": {}, "finish_reason": choice.get("finish_reason")}]}).encode() + b"\n\n"
    if "usage" in obj:
        yield b"data: " + json.dumps({**base, "choices": [], "usage": obj["usage"]}).encode() + b"\n\n"
    yield b"data: [DONE]\n\n"


def stream(body: dict, protocol: str, invoke, *, request_id=None, api_key_name=None) -> StreamingResponse:
    from .search_tool_stream import stream as stream_rounds
    return stream_rounds(body, protocol, invoke, request_id=request_id, api_key_name=api_key_name)
