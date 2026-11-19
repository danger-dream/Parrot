"""Per-candidate, structural xAI reserved function-name compatibility.

No cache/session keys are removed. State belongs to UpstreamRequest, never to a
Channel instance; SSE decoding also keeps split UTF-8/JSON frames request-local.
"""
from __future__ import annotations

import copy
import hashlib
import json

RESERVED = frozenset({
    "web_search", "WebSearch", "web_fetch", "WebFetch", "x_search", "XSearch", "tool_search",
})


def _names(value, result):
    if isinstance(value, list):
        for item in value:
            _names(item, result)
    elif isinstance(value, dict):
        typ = value.get("type")
        if typ in ("function", "function_call", "tool_use") and isinstance(value.get("name"), str):
            result.add(value["name"])
        if isinstance(value.get("function"), dict):
            name = value["function"].get("name")
            if isinstance(name, str):
                result.add(name)
        for key in ("tools", "tool_choice", "allowed_tools", "input", "messages", "tool_calls", "content"):
            if key in value and not isinstance(value[key], str):
                _names(value[key], result)


def _rewrite(value, mapping):
    if isinstance(value, list):
        return [_rewrite(item, mapping) for item in value]
    if not isinstance(value, dict):
        return value
    out = dict(value)
    typ = value.get("type")
    if typ in ("function", "function_call", "tool_use") and value.get("name") in mapping:
        out["name"] = mapping[value["name"]]
    if isinstance(value.get("function"), dict):
        fn = dict(value["function"])
        if fn.get("name") in mapping:
            fn["name"] = mapping[fn["name"]]
        out["function"] = fn
    # These are protocol containers, not arbitrary user arguments or result JSON.
    for key in ("tools", "tool_choice", "allowed_tools", "input", "messages", "tool_calls", "content", "output", "item", "response", "choices", "message", "delta", "content_block"):
        if typ in ("function_call_output", "custom_tool_call_output", "tool_result") and key in ("content", "output"):
            continue
        item = value.get(key)
        if isinstance(item, (dict, list)):
            out[key] = _rewrite(item, mapping)
    return out


def compile_xai(payload: dict, *, stream=True) -> tuple[dict, dict]:
    names = set()
    _names(payload, names)
    mapping = {}
    occupied = set(names)
    for name in sorted(names & RESERVED):
        stem = "parrot_fn_" + name + "_" + hashlib.sha256(name.encode()).hexdigest()[:8]
        alias, idx = stem, 0
        while alias in occupied:
            idx += 1
            alias = f"{stem}_{idx}"
        occupied.add(alias)
        mapping[name] = alias
    native = any(isinstance(t, dict) and t.get("type") == "web_search" for t in payload.get("tools") or [])
    state = {"to_wire": mapping, "from_wire": {v: k for k, v in mapping.items()}, "stream": stream, "buffer": b"", "hosted_search": native}
    return _rewrite(copy.deepcopy(payload), mapping), state


def _restore_namespace_items(value, plan):
    from .openai.transform import responses_to_anthropic as bridge
    if isinstance(value, list):
        return [_restore_namespace_items(item, plan) for item in value]
    if not isinstance(value, dict):
        return value
    typ = value.get("type")
    out = bridge.restore_output_item(value, plan) if typ in ("function_call", "custom_tool_call") else dict(value)
    if typ in ("response.function_call_arguments.done", "response.custom_tool_call_input.done") and isinstance(out.get("name"), str):
        ident = plan.identity_for_flat(out["name"])
        if ident is not None:
            out["name"] = ident.child_name
    for key in ("output", "item", "response"):
        if typ in ("function_call_output", "custom_tool_call_output") and key == "output":
            continue
        sub = out.get(key)
        if isinstance(sub, (dict, list)):
            out[key] = _restore_namespace_items(sub, plan)
    return out


def compile_responses_namespace(payload: dict, *, stream: bool = False) -> tuple[dict, dict | None]:
    tools = payload.get("tools") if isinstance(payload.get("tools"), list) else []
    raw_input = payload.get("input") if isinstance(payload.get("input"), list) else []
    choice = payload.get("tool_choice")
    choice_has_ns = isinstance(choice, dict) and (
        bool(choice.get("namespace"))
        or choice.get("type") == "namespace"
        or (choice.get("type") == "allowed_tools" and any(
            isinstance(t, dict) and (t.get("namespace") or t.get("type") == "namespace")
            for t in (choice.get("tools") or [])
        ))
    )
    has_ns = (
        any(isinstance(t, dict) and t.get("type") == "namespace" for t in tools)
        or any(isinstance(it, dict) and it.get("type") in ("function_call", "custom_tool_call") and it.get("namespace") for it in raw_input)
        or choice_has_ns
    )
    if not has_ns:
        return payload, None

    from .openai.transform import guard, responses_to_anthropic as bridge

    def fail(msg: str, param: str = "tools") -> None:
        raise guard.GuardError(400, "invalid_request_error", msg, param=param, scope="candidate")

    prepared = copy.deepcopy(payload)
    plan = bridge.NamespaceToolMap()
    try:
        for tool in prepared.get("tools") or []:
            if not isinstance(tool, dict):
                continue
            typ = tool.get("type")
            if typ in (None, "function", "custom"):
                name = str(tool.get("name") or "")
                if not name:
                    fail("Responses tools require a non-empty name")
                plan.reserve_direct("custom" if typ == "custom" else "function", name)

        if isinstance(prepared.get("tools"), list):
            flattened: list[dict] = []
            for tool in prepared["tools"]:
                if not isinstance(tool, dict) or tool.get("type") != "namespace":
                    flattened.append(tool)
                    continue
                ns, children = str(tool.get("name") or ""), tool.get("tools")
                if not ns or not isinstance(children, list):
                    fail("Responses namespace tools require a non-empty name and tools array")
                ns_desc = str(tool.get("description") or "")
                seen: set[tuple[str, str, str]] = set()
                for child in children:
                    if not isinstance(child, dict):
                        fail("Responses namespace children must be tool objects")
                    kind = str(child.get("type") or "function")
                    if kind not in ("function", "custom"):
                        fail("Responses namespace children must be function or custom tools")
                    cname = str(child.get("name") or "")
                    if not cname:
                        fail("Responses namespace children require a non-empty name")
                    if (kind, ns, cname) in seen or bridge.ToolWireIdentity(kind, ns, cname) in plan.by_identity:
                        fail(f"duplicate Responses namespace tool {ns}.{cname}")
                    seen.add((kind, ns, cname))
                    c = copy.deepcopy(child)
                    c["type"] = kind
                    c["name"] = plan.flat_name(kind, ns, cname)
                    if ns_desc:
                        c["description"] = ns_desc + "\n" + str(c.get("description") or "")
                    flattened.append(c)
            prepared["tools"] = flattened

        if isinstance(prepared.get("input"), list):
            prepared["input"] = bridge._map_namespaced_history(prepared["input"], plan)

        if choice_has_ns:
            orig_choice_type = choice.get("type") if isinstance(choice, dict) else None
            if orig_choice_type == "namespace":
                fail("Responses tool_choice type 'namespace' must identify individual tools", param="tool_choice")
            mapped_choice = bridge._map_tool_choice(prepared.get("tool_choice"), plan)
            if orig_choice_type == "custom" and isinstance(mapped_choice, dict) and mapped_choice.get("type") == "function":
                mapped_choice = {**mapped_choice, "type": "custom"}
            elif isinstance(mapped_choice, dict) and mapped_choice.get("type") == "allowed_tools":
                selected = {t["name"] for t in mapped_choice.get("tools") or [] if isinstance(t, dict) and t.get("name")}
                prepared["tools"] = [t for t in (prepared.get("tools") or []) if not isinstance(t, dict) or t.get("name") in selected]
                mode = mapped_choice.get("mode", "auto")
                if mode not in ("auto", "required"):
                    fail("allowed_tools mode must be auto or required", param="tool_choice")
                mapped_choice = mode
            prepared["tool_choice"] = mapped_choice
    except guard.GuardError as exc:
        raise guard.GuardError(exc.status, exc.err_type, exc.message, param=exc.param, scope="candidate") from exc

    needles = tuple(
        flat.encode("utf-8")
        for flat, ident in plan.by_flat_name.items()
        if ident.namespace is not None
    )
    return prepared, {"namespace_plan": plan, "namespace_needles": needles, "stream": bool(stream), "buffer": b""}


def restore_object(obj, state):
    if not state:
        return obj
    if state.get("namespace_plan") is not None:
        obj = _restore_namespace_items(obj, state["namespace_plan"])
    if "from_wire" not in state:
        return obj
    if state["from_wire"] and not state.get("hosted_search"):
        def unexpected(value):
            if isinstance(value, list):
                return any(unexpected(item) for item in value)
            if isinstance(value, dict):
                typ = str(value.get("type") or "")
                if typ == "web_search_call" or typ.startswith("response.web_search_call."):
                    return True
                return any(unexpected(value[k]) for k in ("output", "response", "item") if k in value)
            return False
        if unexpected(obj):
            raise ValueError("xai_reserved_tool_misclassified: received hosted web_search_call without a hosted declaration")
    return _rewrite(obj, state["from_wire"])


def restore_bytes(chunk: bytes, state: dict | None) -> bytes:
    if not state or ("from_wire" not in state and "namespace_plan" not in state):
        return chunk
    buf = state.get("buffer", b"") + chunk
    if not state.get("stream") or buf.lstrip().startswith(b"{"):
        try:
            obj = json.loads(buf)
        except (ValueError, UnicodeDecodeError):
            state["buffer"] = buf
            return b""
        state["buffer"] = b""
        return json.dumps(restore_object(obj, state), ensure_ascii=False, separators=(",", ":")).encode()
    buf = buf.replace(b"\r\n", b"\n")
    parts = buf.split(b"\n\n")
    state["buffer"] = parts.pop()
    needles = state.get("namespace_needles") if "from_wire" not in state else None
    out = []
    for part in parts:
        if needles is not None and not any(n in part for n in needles):
            out.append(part + b"\n\n")
            continue
        lines = part.split(b"\n")
        data = b"\n".join(line[5:].lstrip() for line in lines if line.startswith(b"data:"))
        if data and data != b"[DONE]":
            if "from_wire" not in state:
                try:
                    obj = json.loads(data)
                except (ValueError, UnicodeDecodeError):
                    out.append(part + b"\n\n")
                    continue
            else:
                obj = json.loads(data)
            restored = json.dumps(restore_object(obj, state), ensure_ascii=False, separators=(",", ":")).encode()
            new_lines = []
            replaced = False
            for line in lines:
                if line.startswith(b"data:"):
                    if not replaced:
                        new_lines.append(b"data: " + restored)
                        replaced = True
                else:
                    new_lines.append(line)
            lines = new_lines
        out.append(b"\n".join(lines) + b"\n\n")
    return b"".join(out)


def flush_bytes(state: dict | None) -> bytes:
    if not state or not state.get("buffer"):
        return b""
    if state.get("stream") and not state["buffer"].lstrip().startswith(b"{"):
        tail = restore_bytes(b"\n\n", state)
        rem = state.get("buffer", b"")
        state["buffer"] = b""
        return tail + rem
    rem = state.get("buffer", b"")
    state["buffer"] = b""
    return rem
