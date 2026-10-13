"""Stateless Anthropic thinking carriers and request-local compatibility fallback."""
from __future__ import annotations

import copy


SIGNATURE_PREFIX = "claude#"
REDACTED_PREFIX = "claude-redacted-thinking:"
DROP_HISTORY_KEY = "_parrot_drop_thinking_history"


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


def drop_thinking_history(payload: dict, *, allow_unsigned_thinking: bool = False) -> None:
    """Drop unavailable history without forcing adaptive models to stop thinking."""
    messages = []
    for message in payload.get("messages") or []:
        content = message.get("content")
        if message.get("role") == "assistant" and isinstance(content, list):
            kept = [block for block in content if not isinstance(block, dict)
                    or block.get("type") not in ("thinking", "redacted_thinking")]
            if not kept:
                continue
            message = {**message, "content": kept}
        messages.append(message)
    payload["messages"] = messages
    # Only legacy manual thinking requires a thinking-first assistant turn.
    if allow_unsigned_thinking or (payload.get("thinking") or {}).get("type") != "enabled":
        return
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
