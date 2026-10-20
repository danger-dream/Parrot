"""Anthropic thinking carriers and the rejected-history compatibility fallback."""
from __future__ import annotations

import copy
import hashlib
import threading
import time
from collections import OrderedDict


SIGNATURE_PREFIX = "claude#"
REDACTED_PREFIX = "claude-redacted-thinking:"
DROP_HISTORY_KEY = "_parrot_drop_thinking_history"

_REJECTED_MAX = 20_000
_REJECTED_TTL_S = 24 * 3600
_rejected: OrderedDict[str, float] = OrderedDict()
_rejected_lock = threading.Lock()


def is_replayable(block: dict) -> bool:
    if block.get("type") == "thinking":
        return isinstance(block.get("thinking"), str) and isinstance(block.get("signature"), str) and bool(block["signature"])
    return block.get("type") == "redacted_thinking" and isinstance(block.get("data"), str) and bool(block["data"])


def is_native_carrier(value: object) -> bool:
    return isinstance(value, str) and value.startswith((SIGNATURE_PREFIX, REDACTED_PREFIX))


def without_native_items(payload: dict) -> dict:
    items = payload.get("input")
    if not isinstance(items, list):
        return payload
    kept = []
    removed_ids: set[str] = set()
    retained_ids: set[str] = set()
    for item in items:
        native = (isinstance(item, dict) and item.get("type") == "reasoning"
                  and is_native_carrier(item.get("encrypted_content")))
        if isinstance(item, dict) and item.get("type") != "item_reference":
            item_id = item.get("id")
            if isinstance(item_id, str) and item_id:
                (removed_ids if native else retained_ids).add(item_id)
        if not native:
            kept.append(item)
    # Only references whose known target was removed belong to this cleanup.
    # Unknown references (and IDs still supplied by another item) stay untouched.
    removed_ids -= retained_ids
    return {**payload, "input": [item for item in kept if not (
        isinstance(item, dict) and item.get("type") == "item_reference"
        and isinstance(item.get("id"), str) and item["id"] in removed_ids
    )]}


def encode(block: dict) -> str | None:
    if not is_replayable(block):
        return None
    if block["type"] == "redacted_thinking":
        return REDACTED_PREFIX + block["data"]
    return SIGNATURE_PREFIX + block["signature"]


def decode(item: dict) -> dict | None:
    carrier = item.get("encrypted_content")
    if not isinstance(carrier, str):
        return None
    if carrier.startswith(REDACTED_PREFIX):
        data = carrier[len(REDACTED_PREFIX):]
        return {"type": "redacted_thinking", "data": data} if data else None
    # An untagged OpenAI/Gemini ciphertext is not an Anthropic signature.
    if not carrier.startswith(SIGNATURE_PREFIX):
        return None
    signature = carrier[len(SIGNATURE_PREFIX):]
    if not signature:
        return None
    text = ""
    for field, kind in (("summary", "summary_text"), ("content", "reasoning_text")):
        parts = item.get(field)
        if isinstance(parts, list):
            text = "\n\n".join(part["text"] for part in parts if isinstance(part, dict)
                               and part.get("type") == kind and isinstance(part.get("text"), str))
        if text:
            break
    return {"type": "thinking", "thinking": text, "signature": signature}


def _block_key(block: dict) -> str | None:
    if block.get("type") == "thinking":
        value = block.get("signature")
    elif block.get("type") == "redacted_thinking":
        value = block.get("data")
    else:
        return None
    if not isinstance(value, str) or not value:
        return None
    return hashlib.sha256(f"{block['type']}\0{value}".encode()).hexdigest()


def thinking_block_keys(payload) -> frozenset[str]:
    """Digests of the replayed assistant thinking blocks in an Anthropic payload."""
    keys = set()
    for message in (payload.get("messages") if isinstance(payload, dict) else None) or []:
        content = message.get("content") if isinstance(message, dict) else None
        if message.get("role") == "assistant" and isinstance(content, list):
            for block in content:
                key = _block_key(block) if isinstance(block, dict) else None
                if key:
                    keys.add(key)
    return frozenset(keys)


def remember_rejected_history(channel_key: str, keys) -> int:
    """Record thinking blocks the channel rejected as history.

    Later requests to that channel drop exactly these blocks and keep thinking
    generated afterwards, instead of paying a rejected round trip every turn.
    """
    if not channel_key or not keys:
        return 0
    expires = time.monotonic() + _REJECTED_TTL_S
    with _rejected_lock:
        for key in keys:
            scoped = f"{channel_key}\0{key}"
            _rejected[scoped] = expires
            _rejected.move_to_end(scoped)
        while len(_rejected) > _REJECTED_MAX:
            _rejected.popitem(last=False)
    return len(keys)


def _known_rejected(channel_key: str, keys) -> set[str]:
    now = time.monotonic()
    found = set()
    with _rejected_lock:
        for key in keys:
            scoped = f"{channel_key}\0{key}"
            expires = _rejected.get(scoped)
            if expires is None:
                continue
            if expires <= now:
                del _rejected[scoped]
            else:
                found.add(key)
    return found


def clear_rejected_history() -> None:
    with _rejected_lock:
        _rejected.clear()


def drop_known_rejected_thinking(payload: dict, channel_key: str, *, allow_unsigned_thinking: bool = False) -> int:
    """Drop only thinking blocks this channel already rejected; keep the rest verbatim."""
    if not channel_key:
        return 0
    rejected = _known_rejected(channel_key, thinking_block_keys(payload))
    if not rejected:
        return 0
    return _drop_thinking(payload, rejected, allow_unsigned_thinking=allow_unsigned_thinking)


def drop_thinking_history(payload: dict, *, allow_unsigned_thinking: bool = False) -> None:
    """Drop unavailable history without forcing adaptive models to stop thinking."""
    _drop_thinking(payload, None, allow_unsigned_thinking=allow_unsigned_thinking)


def _drop_thinking(payload: dict, only: set[str] | None, *, allow_unsigned_thinking: bool) -> int:
    removed = 0
    messages = []
    for message in payload.get("messages") or []:
        content = message.get("content")
        if message.get("role") == "assistant" and isinstance(content, list):
            kept = []
            for block in content:
                if (isinstance(block, dict) and block.get("type") in ("thinking", "redacted_thinking")
                        and (only is None or _block_key(block) in only)):
                    removed += 1
                    continue
                kept.append(block)
            if len(kept) != len(content):
                if not kept:
                    continue
                message = {**message, "content": kept}
        messages.append(message)
    payload["messages"] = messages
    # Only legacy manual thinking requires a thinking-first assistant turn.
    if allow_unsigned_thinking or (payload.get("thinking") or {}).get("type") != "enabled":
        return removed
    if only is not None and not _active_tool_turn_lost_thinking(messages):
        return removed
    payload.pop("thinking", None)
    management = payload.get("context_management")
    if isinstance(management, dict) and isinstance(management.get("edits"), list):
        edits = [edit for edit in management["edits"] if not isinstance(edit, dict)
                 or not str(edit.get("type", "")).startswith("clear_thinking_")]
        management = {**management, "edits": edits}
        if edits or len(management) > 1:
            payload["context_management"] = management
        else:
            payload.pop("context_management", None)
    return removed


def _active_tool_turn_lost_thinking(messages: list) -> bool:
    """Manual thinking requires a tool-continuation assistant to start with thinking."""
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        blocks = message.get("content") if isinstance(message.get("content"), list) else []
        calls = {b.get("id") for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"}
        results = {b.get("tool_use_id") for following in messages[index + 1:] if isinstance(following, dict)
                   for b in (following.get("content") if isinstance(following.get("content"), list) else [])
                   if isinstance(b, dict) and b.get("type") == "tool_result"}
        if not calls & results:
            return False
        first = blocks[0] if blocks else None
        return not (isinstance(first, dict) and first.get("type") in ("thinking", "redacted_thinking"))
    return False


def retry_request(body: dict) -> dict:
    retried = copy.deepcopy(body)
    retried[DROP_HISTORY_KEY] = True
    return retried


def is_thinking_rejection(status: int | None, message: str) -> bool:
    if status != 400:
        return False
    text = str(message or "").lower()
    return (
        ("thinking" in text and "signature" in text and any(word in text for word in ("invalid", "missing", "required", "not valid")))
        or ("thinking" in text and "must start" in text)
        or ("thinking" in text and "cannot be modified" in text)
        or ("thinking" in text and "expected" in text and ("found `text`" in text or "found `tool_use`" in text))
        or ("thinking" in text and "prefix" in text and ("match" in text or "bound" in text))
    )


def is_block_rejection(status: int | None, message: str) -> bool:
    """Whether the rejection blames replayed blocks rather than turn structure.

    Structural errors (a tool turn missing its leading thinking, wrong block
    order) say nothing about the blocks that were sent, so they are retried but
    never remembered.
    """
    if not is_thinking_rejection(status, message):
        return False
    text = str(message or "").lower()
    return not ("must start" in text or ("expected" in text and "found `" in text))
