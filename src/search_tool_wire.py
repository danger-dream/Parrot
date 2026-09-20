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


def restore_object(obj, state):
    if not state or "from_wire" not in state:
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
    if not state or "from_wire" not in state:
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
    out = []
    for part in parts:
        lines = part.split(b"\n")
        data = b"\n".join(line[5:].lstrip() for line in lines if line.startswith(b"data:"))
        if data and data != b"[DONE]":
            obj = json.loads(data)
            restored = json.dumps(restore_object(obj, state), ensure_ascii=False, separators=(",", ":")).encode()
            lines = [line for line in lines if not line.startswith(b"data:")] + [b"data: " + restored]
        out.append(b"\n".join(lines) + b"\n\n")
    return b"".join(out)
