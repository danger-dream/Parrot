"""Codex encrypted reasoning replay cache.

ChatGPT/Codex `store=false` can continue an opaque reasoning chain when the
previous assistant output's Responses items are replayed in the next request
input.  The replay state is scoped by a stable session anchor (prompt_cache_key,
Codex turn/window metadata, or session headers) and by model.

This module intentionally stores only the minimal Responses input item shapes
accepted by Codex:
- reasoning with encrypted_content
- function_call
- custom_tool_call

It does not try to decrypt or synthesize encrypted_content.  If upstream rejects
a cached encrypted_content, failover deletes the scope and may retry without EC.
"""

from __future__ import annotations

import copy
import base64
import json
import re
import threading
import time
from collections import OrderedDict
from typing import Any

_TTL_SECONDS = 60 * 60
_MAX_ENTRIES = 10_240
_EVICT_BATCH = 128

_lock = threading.RLock()
_entries: OrderedDict[str, tuple[float, list[dict[str, Any]]]] = OrderedDict()

_SESSION_ID_RE = re.compile(r"session[_-]?id['\"=:\s]+([A-Za-z0-9_.:-]{8,})", re.IGNORECASE)
_GPT_REASONING_SIGNATURE_RE = re.compile(r"^[A-Za-z0-9_\-=]+$")
_MAX_GPT_REASONING_SIGNATURE_LEN = 32 * 1024 * 1024


def _now() -> float:
    return time.time()


def _clean_str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _cache_key(
    model: str | None,
    session_key: str | None,
    account_key: str | None = None,
) -> str:
    model_s = _clean_str(model)
    session_s = _clean_str(session_key)
    account_s = _clean_str(account_key)
    if not model_s or not session_s:
        return ""
    # account_key is an ownership boundary: equal session/model values on two
    # OAuth workspaces must never share opaque encrypted reasoning.
    return "\x00".join(("codex-reasoning-replay", account_s, model_s, session_s))


def _clone_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return copy.deepcopy(items)


def _decode_base64url(raw: str) -> bytes | None:
    padded = raw + ("=" * ((4 - len(raw) % 4) % 4))
    try:
        return base64.urlsafe_b64decode(padded.encode("ascii"))
    except Exception:
        return None


def is_valid_gpt_reasoning_signature(raw: str) -> bool:
    """Validate the Fernet-like transport shape used by Codex encrypted_content.

    This mirrors CLIProxyAPI's signature.InspectGPTReasoningSignature: it is a
    shape check only, not decryptability proof.  Invalid values are not cached
    so replay cannot poison the next request with obviously bad EC.
    """
    sig = _clean_str(raw)
    if not sig or len(sig) > _MAX_GPT_REASONING_SIGNATURE_LEN:
        return False
    if not sig.startswith("gAAAA"):
        return False
    if _GPT_REASONING_SIGNATURE_RE.fullmatch(sig) is None:
        return False
    decoded = _decode_base64url(sig)
    if decoded is None or len(decoded) < 73:
        return False
    if decoded[0] != 0x80:
        return False
    ciphertext_len = len(decoded) - 1 - 8 - 16 - 32
    return ciphertext_len > 0 and ciphertext_len % 16 == 0


def _purge_expired(now: float | None = None) -> None:
    ts = _now() if now is None else now
    expired: list[str] = []
    for key, (created, _) in list(_entries.items()):
        if ts - created > _TTL_SECONDS:
            expired.append(key)
    for key in expired:
        _entries.pop(key, None)


def _evict_if_needed() -> None:
    if len(_entries) <= _MAX_ENTRIES:
        return
    count = min(_EVICT_BATCH, max(1, len(_entries) - _MAX_ENTRIES + 1))
    for _ in range(count):
        try:
            _entries.popitem(last=False)
        except KeyError:
            break


def clear() -> None:
    with _lock:
        _entries.clear()


def delete(
    model: str | None,
    session_key: str | None,
    account_key: str | None = None,
) -> None:
    key = _cache_key(model, session_key, account_key)
    if not key:
        return
    with _lock:
        _entries.pop(key, None)


def get(
    model: str | None,
    session_key: str | None,
    account_key: str | None = None,
) -> list[dict[str, Any]]:
    key = _cache_key(model, session_key, account_key)
    if not key:
        return []
    with _lock:
        _purge_expired()
        entry = _entries.get(key)
        if not entry:
            return []
        _, items = entry
        # Sliding TTL: touch on read, same as CLIProxyAPI.
        _entries[key] = (_now(), items)
        _entries.move_to_end(key)
        return _clone_items(items)


def _normalize_reasoning(item: dict[str, Any]) -> dict[str, Any] | None:
    enc = item.get("encrypted_content")
    if not isinstance(enc, str) or not enc or enc != enc.strip():
        return None
    if not is_valid_gpt_reasoning_signature(enc):
        return None
    return {"type": "reasoning", "summary": [], "content": None, "encrypted_content": enc}


def _normalize_function_call(item: dict[str, Any]) -> dict[str, Any] | None:
    call_id = _clean_str(item.get("call_id"))
    name = _clean_str(item.get("name"))
    arguments = item.get("arguments")
    if not call_id or not name or not isinstance(arguments, str):
        return None
    return {"type": "function_call", "call_id": call_id, "name": name, "arguments": arguments}


def _normalize_custom_tool_call(item: dict[str, Any]) -> dict[str, Any] | None:
    call_id = _clean_str(item.get("call_id"))
    name = _clean_str(item.get("name"))
    if not call_id or not name or "input" not in item:
        return None
    out: dict[str, Any] = {
        "type": "custom_tool_call",
        "status": _clean_str(item.get("status")) or "completed",
        "call_id": call_id,
        "name": name,
        "input": copy.deepcopy(item.get("input")),
    }
    return out


def normalize_item(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    typ = _clean_str(item.get("type"))
    if typ == "reasoning":
        return _normalize_reasoning(item)
    if typ == "function_call":
        return _normalize_function_call(item)
    if typ == "custom_tool_call":
        return _normalize_custom_tool_call(item)
    return None


def normalize_items(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    out: list[dict[str, Any]] = []
    for item in items:
        normalized = normalize_item(item)
        if normalized is not None:
            out.append(normalized)
    return out


def cache_items(
    model: str | None,
    session_key: str | None,
    items: Any,
    account_key: str | None = None,
) -> bool:
    key = _cache_key(model, session_key, account_key)
    if not key:
        return False
    normalized = normalize_items(items)
    if not normalized:
        delete(model, session_key, account_key)
        return False
    with _lock:
        _purge_expired()
        _entries[key] = (_now(), _clone_items(normalized))
        _entries.move_to_end(key)
        _evict_if_needed()
    return True


def session_key_from_turn_metadata(raw: Any) -> str:
    text = _clean_str(raw)
    if not text:
        return ""
    try:
        obj = json.loads(text)
    except Exception:
        return ""
    if not isinstance(obj, dict):
        return ""
    pck = _clean_str(obj.get("prompt_cache_key"))
    if pck:
        return "prompt-cache:" + pck
    window_id = _clean_str(obj.get("window_id"))
    if window_id:
        return "window:" + window_id
    return ""


def session_key_from_metadata(metadata: Any) -> str:
    if not isinstance(metadata, dict):
        return ""
    user_id = _clean_str(metadata.get("user_id") or metadata.get("user"))
    if not user_id:
        return ""
    if user_id.startswith("{"):
        try:
            obj = json.loads(user_id)
        except Exception:
            obj = None
        if isinstance(obj, dict):
            session_id = _clean_str(obj.get("session_id") or obj.get("sessionId"))
            if session_id:
                return "claude:" + session_id
    match = _SESSION_ID_RE.search(user_id)
    if match:
        return "claude:" + match.group(1)
    # Plain metadata.user/user_id is usually an end-user identifier, not a
    # conversation/session boundary.  Do not use it for replay: it could leak
    # encrypted reasoning across unrelated conversations from the same user.
    return ""


def session_key_from_payload(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    pck = _clean_str(payload.get("prompt_cache_key"))
    if pck:
        return "prompt-cache:" + pck
    client_metadata = payload.get("client_metadata")
    if isinstance(client_metadata, dict):
        window_id = _clean_str(client_metadata.get("x-codex-window-id"))
        if window_id:
            return "window:" + window_id
        key = session_key_from_turn_metadata(client_metadata.get("x-codex-turn-metadata"))
        if key:
            return key
    key = session_key_from_metadata(payload.get("metadata"))
    if key:
        return key
    return ""


def _header_get(headers: Any, name: str) -> str:
    if not isinstance(headers, dict):
        return ""
    wanted = name.lower()
    for key, value in headers.items():
        if str(key).lower() == wanted:
            return _clean_str(value)
    return ""


def session_key_from_headers(headers: Any) -> str:
    turn = _header_get(headers, "x-codex-turn-metadata")
    if turn:
        key = session_key_from_turn_metadata(turn)
        if key:
            return key
    window_id = _header_get(headers, "x-codex-window-id")
    if window_id:
        return "window:" + window_id
    for name in ("session_id", "session-id", "Session_id", "Session-Id"):
        value = _header_get(headers, name)
        if value:
            return "session-id:" + value
    conv = _header_get(headers, "conversation_id") or _header_get(headers, "conversation-id")
    if conv:
        return "conversation_id:" + conv
    return ""


def scope_from_payload(
    model: str | None,
    payload: Any,
    headers: Any = None,
    *,
    account_key: str | None = None,
) -> dict[str, str] | None:
    model_s = _clean_str(model)
    if not model_s:
        return None
    session_key = session_key_from_payload(payload) or session_key_from_headers(headers)
    if not session_key:
        return None
    scope = {"model": model_s, "session_key": session_key}
    clean_account_key = _clean_str(account_key)
    if clean_account_key:
        scope["account_key"] = clean_account_key
    return scope


def _has_input_reasoning(input_items: list[Any]) -> bool:
    for item in input_items:
        if isinstance(item, dict) and item.get("type") == "reasoning":
            enc = item.get("encrypted_content")
            if isinstance(enc, str) and enc.strip():
                return True
    return False


def _comparable_call_ids(call_id: Any) -> set[str]:
    cid = _clean_str(call_id)
    if not cid:
        return set()
    out = {cid}
    if cid.startswith("call_"):
        out.add("fc" + cid[len("call_"):])
        out.add("fc_" + cid[len("call_"):])
    elif cid.startswith("fc_"):
        out.add("call_" + cid[len("fc_"):])
    elif cid.startswith("fc") and len(cid) > 2:
        out.add("call_" + cid[2:])
    return {x for x in out if x}


def _tool_call_keys(item: dict[str, Any]) -> set[str]:
    typ = _clean_str(item.get("type"))
    if typ not in ("function_call", "custom_tool_call"):
        return set()
    return {f"{typ}:{cid}" for cid in _comparable_call_ids(item.get("call_id"))}


def _output_call_id_map(input_items: list[Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in input_items:
        if not isinstance(item, dict):
            continue
        if item.get("type") not in ("function_call_output", "custom_tool_call_output"):
            continue
        call_id = _clean_str(item.get("call_id"))
        if not call_id:
            continue
        for candidate in _comparable_call_ids(call_id):
            out[candidate] = call_id
    return out


def _filter_replay_for_input(input_items: list[Any], replay_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    has_reasoning = _has_input_reasoning(input_items)
    existing_calls: set[str] = set()
    existing_outputs: set[str] = set()
    for item in input_items:
        if not isinstance(item, dict):
            continue
        existing_calls.update(_tool_call_keys(item))
        if item.get("type") in ("function_call_output", "custom_tool_call_output"):
            existing_outputs.update(_comparable_call_ids(item.get("call_id")))

    filtered: list[dict[str, Any]] = []
    for item in replay_items:
        typ = item.get("type")
        if typ == "reasoning":
            if has_reasoning:
                continue
            filtered.append(copy.deepcopy(item))
            continue
        if typ in ("function_call", "custom_tool_call"):
            keys = _tool_call_keys(item)
            if not keys or keys.intersection(existing_calls):
                continue
            if not _comparable_call_ids(item.get("call_id")).intersection(existing_outputs):
                continue
            existing_calls.update(keys)
            filtered.append(copy.deepcopy(item))
    return filtered


def _insert_index(input_items: list[Any], replay_items: list[dict[str, Any]]) -> int:
    replay_call_ids: set[str] = set()
    for item in replay_items:
        if item.get("type") in ("function_call", "custom_tool_call"):
            replay_call_ids.update(_comparable_call_ids(item.get("call_id")))
    if replay_call_ids:
        for idx, item in enumerate(input_items):
            if not isinstance(item, dict):
                continue
            if item.get("type") not in ("function_call_output", "custom_tool_call_output"):
                continue
            call_id = _clean_str(item.get("call_id"))
            if not call_id or call_id in replay_call_ids:
                return idx
    for idx in range(len(input_items) - 1, -1, -1):
        item = input_items[idx]
        if isinstance(item, dict) and item.get("type") == "message" and item.get("role") == "assistant":
            return idx
    for idx, item in enumerate(input_items):
        if not (isinstance(item, dict) and item.get("type") == "message" and item.get("role") in ("developer", "system")):
            return idx
    return len(input_items)


def _align_tool_call_ids(input_items: list[Any], replay_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output_map = _output_call_id_map(input_items)
    if not output_map:
        return replay_items
    out: list[dict[str, Any]] = []
    for item in replay_items:
        if item.get("type") not in ("function_call", "custom_tool_call"):
            out.append(item)
            continue
        replacement = ""
        for candidate in _comparable_call_ids(item.get("call_id")):
            if output_map.get(candidate):
                replacement = output_map[candidate]
                break
        if replacement and replacement != item.get("call_id"):
            new_item = dict(item)
            new_item["call_id"] = replacement
            out.append(new_item)
        else:
            out.append(item)
    return out


def inject_replay_items(payload: dict[str, Any], scope: dict[str, str] | None) -> int:
    """Inject cached replay items into a Responses payload in place.

    Returns the number of replay items inserted.  No-op when scope/input/cache is
    missing or when the current input already carries equivalent state.
    """
    if not isinstance(payload, dict) or not isinstance(scope, dict):
        return 0
    input_items = payload.get("input")
    if not isinstance(input_items, list):
        return 0
    replay_items = get(
        scope.get("model"), scope.get("session_key"), scope.get("account_key"),
    )
    if not replay_items:
        return 0
    filtered = _filter_replay_for_input(input_items, replay_items)
    if not filtered:
        return 0
    filtered = _align_tool_call_ids(input_items, filtered)
    idx = _insert_index(input_items, filtered)
    payload["input"] = list(input_items[:idx]) + filtered + list(input_items[idx:])
    return len(filtered)


def cache_from_response(
    model: str | None,
    session_key: str | None,
    response_obj: Any,
    account_key: str | None = None,
) -> bool:
    if not isinstance(response_obj, dict):
        return False
    return cache_items(
        model, session_key, response_obj.get("output"), account_key,
    )


def cache_from_translator_ctx(translator_ctx: Any, response_obj: Any) -> bool:
    if not isinstance(translator_ctx, dict):
        return False
    scope = translator_ctx.get("codex_reasoning_replay")
    if not isinstance(scope, dict):
        return False
    return cache_from_response(
        scope.get("model"), scope.get("session_key"), response_obj,
        scope.get("account_key"),
    )


def delete_from_translator_ctx(translator_ctx: Any) -> bool:
    if not isinstance(translator_ctx, dict):
        return False
    scope = translator_ctx.get("codex_reasoning_replay")
    if not isinstance(scope, dict):
        return False
    delete(
        scope.get("model"), scope.get("session_key"), scope.get("account_key"),
    )
    return True
