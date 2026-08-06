"""Short-lived DeepSeek reasoning replay across protocol bridges.

DeepSeek thinking mode requires the reasoning produced for an assistant tool call
to be passed back on every subsequent tool-result request.  Anthropic Messages
and the two OpenAI surfaces encode that state differently:

- Chat: ``assistant.reasoning_content`` beside ``tool_calls``
- Responses: a ``reasoning`` item before the matching ``function_call``
- Anthropic: ``thinking`` beside ``tool_use``

Parrot's Anthropic bridges intentionally do not expose provider reasoning as a
synthetic Anthropic thinking block.  Instead, this module keeps the exact
upstream reasoning for a short period, keyed by the stable tool-call id, and
replays it only when that same id appears in a later request.  It never invents
reasoning, substitutes visible answer text, or fills an empty placeholder.
"""

from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

_TTL_SECONDS = 30 * 60
_MAX_ENTRIES = 10_240
_EVICT_BATCH = 128
_lock = threading.RLock()


@dataclass
class _ReplayEntry:
    created_at: float
    reasoning_text: str
    responses_items: list[dict[str, Any]]


# (normalized model, tool_call_id) -> replay entry.  An empty model is retained
# as a compatibility fallback for callers that cannot supply the resolved model.
# Ordered insertion/access order provides bounded LRU eviction under bursts.
_by_tool_call_id: OrderedDict[tuple[str, str], _ReplayEntry] = OrderedDict()


def _clean(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _model_key(model: Any) -> str:
    return _clean(model).lower()


def _cleanup(now: float | None = None) -> None:
    current = now or time.time()
    stale = [
        key for key, entry in _by_tool_call_id.items()
        if current - entry.created_at > _TTL_SECONDS
    ]
    for key in stale:
        _by_tool_call_id.pop(key, None)


def _evict_if_needed() -> None:
    if len(_by_tool_call_id) <= _MAX_ENTRIES:
        return
    overflow = len(_by_tool_call_id) - _MAX_ENTRIES
    count = min(len(_by_tool_call_id), max(overflow, _EVICT_BATCH))
    for _ in range(count):
        try:
            _by_tool_call_id.popitem(last=False)
        except KeyError:
            break


def clear() -> None:
    """Clear all cached DeepSeek replay state (tests/debug only)."""
    with _lock:
        _by_tool_call_id.clear()


def _reasoning_text_from_item(item: Any) -> str:
    if not isinstance(item, dict) or item.get("type") != "reasoning":
        return ""
    parts: list[str] = []
    for part in item.get("content") or []:
        if not isinstance(part, dict) or part.get("type") != "reasoning_text":
            continue
        text = part.get("text")
        if isinstance(text, str) and text:
            parts.append(text)
    if parts:
        return "".join(parts)
    for part in item.get("summary") or []:
        if not isinstance(part, dict) or part.get("type") != "summary_text":
            continue
        text = part.get("text")
        if isinstance(text, str) and text:
            parts.append(text)
    return "".join(parts)


def _stable_reasoning_item(model: str, call_ids: list[str], reasoning_text: str) -> dict[str, Any]:
    """Build the Responses replay shape for reasoning originating from Chat.

    DeepSeek's native Responses output uses ``content[].reasoning_text``.  Keep
    that exact semantic shape and give the synthesized bridge item a stable id
    so repeated full-history requests do not create a different logical item.
    """
    seed = "\x00".join([_model_key(model), *call_ids, reasoning_text]).encode("utf-8")
    item_id = "rs_" + hashlib.sha256(seed).hexdigest()[:24]
    return {
        "type": "reasoning",
        "id": item_id,
        "status": "completed",
        "summary": [],
        "content": [{"type": "reasoning_text", "text": reasoning_text}],
    }


def _store(
    *,
    model: str | None,
    call_ids: list[str],
    reasoning_text: str,
    responses_items: list[dict[str, Any]],
    known_no_reasoning: bool = False,
) -> int:
    text = reasoning_text if isinstance(reasoning_text, str) else ""
    items = responses_items if isinstance(responses_items, list) else []
    ids = list(dict.fromkeys(_clean(call_id) for call_id in call_ids if _clean(call_id)))
    if not ids:
        return 0
    if known_no_reasoning:
        # A terminal upstream response can legitimately emit a function call
        # without any reasoning item/content.  Remember that observed absence so
        # the candidate guard can distinguish it from state lost after restart,
        # expiry, truncation, or an older Parrot version.  Injection remains a
        # no-op; no empty placeholder is fabricated.
        text = ""
        items = []
    elif not text.strip() or not items:
        return 0
    now = time.time()
    entry = _ReplayEntry(
        created_at=now,
        reasoning_text=text,
        responses_items=copy.deepcopy(items),
    )
    mk = _model_key(model)
    with _lock:
        _cleanup(now)
        for call_id in ids:
            key = (mk, call_id)
            _by_tool_call_id[key] = copy.deepcopy(entry)
            _by_tool_call_id.move_to_end(key)
        _evict_if_needed()
    return len(ids)


def cache_from_chat_assistant(message: Any, *, model: str | None = None) -> int:
    """Cache one reconstructed Chat assistant tool-call state.

    One Chat assistant message has a single ``reasoning_content`` value shared by
    all tool calls it emitted, including parallel calls.  A successful terminal
    tool call with no reasoning is recorded as known absence, not replayed as an
    empty placeholder.
    """
    if not isinstance(message, dict):
        return 0
    call_ids = [
        _clean(tool_call.get("id"))
        for tool_call in (message.get("tool_calls") or [])
        if isinstance(tool_call, dict)
    ]
    call_ids = [call_id for call_id in call_ids if call_id]
    if not call_ids:
        return 0
    raw_reasoning = message.get("reasoning_content")
    if not isinstance(raw_reasoning, str) or not raw_reasoning.strip():
        return _store(
            model=model,
            call_ids=call_ids,
            reasoning_text="",
            responses_items=[],
            known_no_reasoning=True,
        )
    reasoning_text = raw_reasoning
    item = _stable_reasoning_item(_clean(model), call_ids, reasoning_text)
    return _store(
        model=model,
        call_ids=call_ids,
        reasoning_text=reasoning_text,
        responses_items=[item],
    )


def cache_from_chat_response(obj: Any, *, model: str | None = None) -> int:
    """Cache reasoning from one non-stream or reconstructed Chat completion."""
    if not isinstance(obj, dict):
        return 0
    resolved_model = _clean(model) or _clean(obj.get("model"))
    cached = 0
    for choice in obj.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        cached += cache_from_chat_assistant(message, model=resolved_model)
    return cached


def cache_from_responses_response(obj: Any, *, model: str | None = None) -> int:
    """Cache native Responses reasoning items by their following function calls.

    The nearest preceding reasoning item(s) belong to the following tool-call
    group.  Preserve those items byte-for-byte at the JSON value level so a later
    Responses request can replay the provider's original ``reasoning_text``.
    """
    if not isinstance(obj, dict):
        return 0
    output = obj.get("output")
    if not isinstance(output, list):
        return 0
    resolved_model = _clean(model) or _clean(obj.get("model"))
    pending_reasoning: list[dict[str, Any]] = []
    cached = 0
    for item in output:
        if not isinstance(item, dict):
            continue
        typ = item.get("type")
        if typ == "reasoning":
            text = _reasoning_text_from_item(item)
            pending_reasoning = [copy.deepcopy(item)] if text else []
            continue
        if typ != "function_call":
            continue
        call_id = _clean(item.get("call_id") or item.get("id"))
        if not call_id:
            continue
        reasoning_text = "".join(_reasoning_text_from_item(r) for r in pending_reasoning)
        cached += _store(
            model=resolved_model,
            call_ids=[call_id],
            reasoning_text=reasoning_text,
            responses_items=pending_reasoning,
            known_no_reasoning=not bool(reasoning_text.strip()),
        )
    return cached


def _get(call_id: Any, model: str | None = None) -> _ReplayEntry | None:
    cid = _clean(call_id)
    if not cid:
        return None
    mk = _model_key(model)
    with _lock:
        _cleanup()
        key = (mk, cid)
        entry = _by_tool_call_id.get(key)
        if entry is None and mk:
            key = ("", cid)
            entry = _by_tool_call_id.get(key)
        if entry is not None:
            _by_tool_call_id.move_to_end(key)
        return copy.deepcopy(entry) if entry is not None else None


def inject_into_chat_payload(payload: dict[str, Any], *, model: str | None = None) -> int:
    """Restore exact reasoning_content on matching assistant tool-call messages."""
    if not isinstance(payload, dict):
        return 0
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return 0
    patched = 0
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        if _clean(message.get("reasoning_content")):
            continue
        entries: list[_ReplayEntry] = []
        for tool_call in message.get("tool_calls") or []:
            if not isinstance(tool_call, dict):
                continue
            entry = _get(tool_call.get("id"), model)
            if entry is not None:
                entries.append(entry)
        if not entries:
            continue
        # Parallel calls from one assistant response must share one reasoning
        # value.  Conflicting cache entries indicate an unsafe association; do
        # not choose one arbitrarily.
        texts = {entry.reasoning_text for entry in entries if entry.reasoning_text}
        if len(texts) != 1:
            continue
        message["reasoning_content"] = next(iter(texts))
        patched += 1
    return patched


def _item_fingerprint(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    try:
        raw = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except Exception:
        return ""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def inject_into_responses_payload(payload: dict[str, Any], *, model: str | None = None) -> int:
    """Insert cached reasoning items immediately before matching function calls."""
    if not isinstance(payload, dict):
        return 0
    input_items = payload.get("input")
    if not isinstance(input_items, list):
        return 0

    existing = {
        _item_fingerprint(item)
        for item in input_items
        if isinstance(item, dict) and item.get("type") == "reasoning"
    }
    output: list[Any] = []
    inserted = 0
    for item in input_items:
        if isinstance(item, dict) and item.get("type") == "function_call":
            entry = _get(item.get("call_id") or item.get("id"), model)
            if entry is not None:
                for reasoning_item in entry.responses_items:
                    fingerprint = _item_fingerprint(reasoning_item)
                    if not fingerprint or fingerprint in existing:
                        continue
                    output.append(copy.deepcopy(reasoning_item))
                    existing.add(fingerprint)
                    inserted += 1
        output.append(item)
    if inserted:
        payload["input"] = output
    return inserted


def has_replay_for_tool_call(call_id: str, *, model: str | None = None) -> bool:
    entry = _get(call_id, model)
    return bool(entry and entry.reasoning_text.strip() and entry.responses_items)


def has_observed_tool_call_state(call_id: str, *, model: str | None = None) -> bool:
    """Return whether a terminal response established replay or known absence."""
    return _get(call_id, model) is not None


def missing_chat_tool_call_ids(
    payload: dict[str, Any], *, model: str | None = None,
) -> list[str]:
    """Return active Chat tool calls whose terminal reasoning state is unknown."""
    if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
        return []
    messages = payload["messages"]
    # Chat tool results use role=tool.  Only an ordinary role=user message starts
    # a new DeepSeek reasoning turn; all assistant tool calls after it belong to
    # the active chain and require their original reasoning_content.
    chain_start = 0
    for index, message in enumerate(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            chain_start = index + 1
    missing: list[str] = []
    for message in messages[chain_start:]:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list) or not tool_calls:
            continue
        if _clean(message.get("reasoning_content")):
            continue
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                continue
            call_id = _clean(tool_call.get("id"))
            if call_id and _get(call_id, model) is None:
                missing.append(call_id)
    return list(dict.fromkeys(missing))


def missing_responses_tool_call_ids(
    payload: dict[str, Any], *, model: str | None = None,
) -> list[str]:
    """Return historical Responses function calls lacking exact replay state.

    Anthropic→Responses currently cannot carry provider-native reasoning items in
    the request itself, so each function call in the active reasoning turn must be
    backed by this replay cache before a strict DeepSeek route is safe.  The caller
    can reject only that candidate and continue failover without fabricating state.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("input"), list):
        return []
    input_items = payload["input"]
    # DeepSeek validates reasoning continuity for the active tool chain: all
    # function calls after the latest ordinary user message.  Older completed
    # turns may legitimately have been non-thinking and are accepted without a
    # reasoning item; rejecting them would strand mixed-mode conversations.
    chain_start = 0
    for index, item in enumerate(input_items):
        if (
            isinstance(item, dict)
            and item.get("type") == "message"
            and item.get("role") == "user"
        ):
            chain_start = index + 1
    missing: list[str] = []
    for item in input_items[chain_start:]:
        if not isinstance(item, dict) or item.get("type") != "function_call":
            continue
        call_id = _clean(item.get("call_id") or item.get("id"))
        if call_id and _get(call_id, model) is None:
            missing.append(call_id)
    return list(dict.fromkeys(missing))


def _debug_size() -> int:
    with _lock:
        _cleanup()
        return len(_by_tool_call_id)
