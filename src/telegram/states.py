"""用户输入状态机（TG Bot 用）。

状态结构：`{"action": str, "data": dict, "ts": float}`。
超过 TTL（默认 600s）未续期则被 cleanup 清除。

同一 chat_id 同时只能有一条状态（新状态覆盖旧）。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Optional

_DEFAULT_TTL = 600

_lock = threading.Lock()
_states: dict[int, dict[str, Any]] = {}


def set_state(chat_id: int, action: str, data: Optional[dict] = None) -> None:
    with _lock:
        _states[chat_id] = {"action": action, "data": data or {}, "ts": time.time()}


def get_state(chat_id: int) -> Optional[dict]:
    with _lock:
        s = _states.get(chat_id)
        if s is None:
            return None
        if time.time() - s["ts"] > _DEFAULT_TTL:
            _states.pop(chat_id, None)
            return None
        return dict(s)


def pop_state(chat_id: int) -> Optional[dict]:
    with _lock:
        return _states.pop(chat_id, None)


def cleanup() -> int:
    """清理过期状态，返回清理条数。"""
    now = time.time()
    with _lock:
        expired = [cid for cid, s in _states.items() if now - s["ts"] > _DEFAULT_TTL]
        for cid in expired:
            _states.pop(cid, None)
    return len(expired)


def size() -> int:
    with _lock:
        return len(_states)


def clear_all() -> None:
    with _lock:
        _states.clear()
