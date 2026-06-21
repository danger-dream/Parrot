"""WebSocket transport frame helpers."""

from __future__ import annotations

import json


def frame_size(data: str | bytes) -> int:
    if isinstance(data, bytes):
        return len(data)
    return len(data.encode("utf-8", errors="replace"))


def event_type(data: str | bytes) -> str:
    """Extract a JSON WS frame's ``type`` without interpreting the protocol."""
    if not isinstance(data, str):
        return ""
    try:
        obj = json.loads(data)
    except Exception:
        return ""
    return str(obj.get("type") or "") if isinstance(obj, dict) else ""
