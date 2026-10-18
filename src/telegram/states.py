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
    from ..oauth.add_context import current
    # The selection lives separately; input states only retain the immutable
    # attempt binding so a later callback cannot borrow a newer selection.
    value = data if data is not None else {}
    attempt = current() if action.startswith("oa_") and not value.get("relogin") else None
    if attempt is not None:
        with attempt.lock:
            attempt.check()
            value["_oauth_add_attempt"] = attempt.nonce
            with _lock:
                _states[chat_id] = {"action": action, "data": value, "ts": time.time()}
    else:
        with _lock:
            _states[chat_id] = {"action": action, "data": value, "ts": time.time()}


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


def pop_state_if_current(chat_id: int, data: dict) -> Optional[dict]:
    """A background result may only clear the exact input state it owns."""
    with _lock:
        if (_states.get(chat_id) or {}).get("data") is data:
            return _states.pop(chat_id)
        return None


def cleanup() -> int:
    """清理过期状态，返回清理条数。"""
    from .menus import oauth_add_session
    oauth_add_session.cleanup()
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
    from .menus import oauth_add_session
    oauth_add_session.clear_all()
    with _lock:
        _states.clear()
