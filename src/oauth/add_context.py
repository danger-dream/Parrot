"""Request-local routing for an unpublished OAuth addition.

No temporary config rules or invented account IDs. ContextVars cross async tasks
and asyncio.to_thread; adapters must explicitly carry the scope across other
worker/flow boundaries. Only successful account writers persist the selection.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
import threading
import time
import secrets


@dataclass(eq=False)
class AddAttempt:
    target: str | None = None
    provider: str = ""
    nonce: str = field(default_factory=lambda: secrets.token_hex(12))
    expires_at: float = field(default_factory=lambda: time.time() + 600)
    cancelled: bool = False
    published: set[str] = field(default_factory=set, repr=False)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def check(self):
        if self.cancelled or time.time() >= self.expires_at:
            raise ValueError("新增会话已取消或过期，请重新开始")

    def cancel(self):
        with self.lock:
            self.cancelled = True


_current: ContextVar[AddAttempt | None] = ContextVar("oauth_add_attempt", default=None)


def current() -> AddAttempt | None:
    return _current.get()


@contextmanager
def bind(attempt: AddAttempt | None):
    token = _current.set(attempt)
    try:
        yield
    finally:
        _current.reset(token)


def request_target(purpose: str, channel: str = "") -> str | None:
    attempt = current()
    # Telegram delivery/file download and unrelated metadata are NOT OAuth IO.
    if attempt is None or not purpose.startswith("oauth_"):
        return None
    if channel in attempt.published:
        return None  # published accounts now use their atomically saved rule
    attempt.check()
    return attempt.target


def publication_guard(fn):
    """Cancellation and local config publication have one linearization point."""
    @wraps(fn)
    def wrapped(*args, **kwargs):
        attempt = current()
        if attempt is None:
            return fn(*args, **kwargs)
        with attempt.lock:
            attempt.check()
            result = fn(*args, **kwargs)
            if isinstance(result, dict):
                if result.get("status") in {"added", "replaced"}:
                    attempt.published.add("oauth:" + result["account_key"])
                elif result.get("status") == "committed":
                    attempt.published.update("oauth:" + key for key in result["added"] + result["replaced"])
            return result
    return wrapped


def persist_account_route(cfg: dict, account_key: str) -> None:
    """Mutate the SAME unpublished candidate as the successful account save."""
    attempt = current()
    if attempt is None:
        return
    attempt.check()
    if attempt.target is None:
        return  # inherit never adds/removes an account's existing custom rule
    net = cfg.setdefault("network", {})
    target = attempt.target
    if target != "direct" and target not in (net.get("proxies") or {}) and target not in (net.get("groups") or {}):
        raise ValueError("所选代理已不存在，请返回新增页重新选择")
    routes = net.setdefault("routing", {}).setdefault("accounts", {})
    routes["oauth:" + account_key] = target
    # Existing canonical keys outrank the historical TG channel-key spelling.
    if account_key in routes:
        routes[account_key] = target
