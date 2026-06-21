"""Short-lived DeepSeek V4 reasoning_content replay cache.

DeepSeek thinking mode returns ``message.reasoning_content`` alongside
``message.tool_calls``.  Its API requires that reasoning_content be passed back
with the assistant tool-call message in subsequent tool-result turns.  Anthropic
Messages has no equivalent public field, so Parrot keeps a tiny in-process
mapping keyed by tool_call_id and injects it only when sending back to DeepSeek.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

_TTL_SECONDS = 30 * 60
_lock = threading.Lock()
_by_tool_call_id: dict[str, tuple[float, str]] = {}


def _cleanup(now: float | None = None) -> None:
    now = now or time.time()
    stale = [k for k, (ts, _) in _by_tool_call_id.items() if now - ts > _TTL_SECONDS]
    for k in stale:
        _by_tool_call_id.pop(k, None)


def cache_from_chat_response(obj: Any) -> None:
    """Cache reasoning_content from one non-stream Chat completion response."""
    if not isinstance(obj, dict):
        return
    choices = obj.get("choices") if isinstance(obj.get("choices"), list) else []
    now = time.time()
    with _lock:
        _cleanup(now)
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            msg = choice.get("message") if isinstance(choice.get("message"), dict) else {}
            reasoning = msg.get("reasoning_content")
            if not isinstance(reasoning, str) or not reasoning:
                continue
            for tc in msg.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                call_id = str(tc.get("id") or "")
                if call_id:
                    _by_tool_call_id[call_id] = (now, reasoning)


def inject_into_chat_payload(payload: dict[str, Any]) -> int:
    """Inject cached reasoning_content into assistant messages with tool_calls.

    Returns the number of assistant messages patched.
    """
    if not isinstance(payload, dict):
        return 0
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return 0
    now = time.time()
    patched = 0
    with _lock:
        _cleanup(now)
        for msg in messages:
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            if isinstance(msg.get("reasoning_content"), str) and msg.get("reasoning_content"):
                continue
            for tc in msg.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                call_id = str(tc.get("id") or "")
                entry = _by_tool_call_id.get(call_id)
                if entry and entry[1]:
                    msg["reasoning_content"] = entry[1]
                    patched += 1
                    break
    return patched


def _debug_size() -> int:
    with _lock:
        _cleanup()
        return len(_by_tool_call_id)
