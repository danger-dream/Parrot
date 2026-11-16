"""Bounded, tenant/account-scoped association for top-level Hub requests."""
from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field

_CONTEXT_KEY = "_parrot_antigravity_context"
TTL_SECONDS = 3600
MAX_SESSIONS = 4096
MAX_RESPONSES = 8192
_guard = threading.RLock()


def _digest(*parts: str) -> str:
    return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()


@dataclass
class _Session:
    owner: str
    key: str
    model: str
    cascade: str = field(default_factory=lambda: str(uuid.uuid4()))
    trajectory: str = field(default_factory=lambda: str(uuid.uuid4()))
    round: int = -1
    step: int = 0
    next_events: int = 1
    touched: float = field(default_factory=time.monotonic)


@dataclass
class _Round:
    session: _Session
    index: int


@dataclass
class RequestContext:
    anchor: str = ""
    nonce: str = field(default_factory=lambda: str(uuid.uuid4()))
    rounds: dict[tuple[str, str], _Round] = field(default_factory=dict)

    def __deepcopy__(self, memo):
        return self  # Retries of one logical request share round allocation.


@dataclass(frozen=True)
class RequestIdentity:
    owner: str
    session_key: str
    cascade: str
    trajectory: str
    round: int
    step: int
    epoch_ms: int
    model_enum: str

    @property
    def request_id(self) -> str:
        return f"agent/{self.cascade}/{self.epoch_ms}/{self.trajectory}/{self.step}"

    def labels(self) -> dict[str, str]:
        if not self.model_enum:
            return {}
        return {
            "cascade_id": self.cascade,
            "root_cascade_id": self.cascade,
            "trajectory_id": self.trajectory,
            "request_id": f"{self.trajectory}-{self.round}",
            "last_step_index": str(self.step - 1),
            "model_enum": self.model_enum,
            # Observed top-level Hub values, not inferred model classifications.
            "used_claude": "false",
            "used_claude_conservative": "false",
            "used_non_gemini_model": "true",
        }


_sessions: OrderedDict[str, _Session] = OrderedDict()
_responses: OrderedDict[str, tuple[str, str, float]] = OrderedDict()


def _session_anchor(body: dict) -> str:
    from ..cache_hints import anthropic_session_id
    session = anthropic_session_id(body)
    if session:
        return session
    if body.get("previous_response_id"):
        return ""  # The channel validates Store ownership before using this link.
    injected = body.get("_internal_injected_fields")
    if isinstance(injected, list) and "prompt_cache_key" in injected:
        return ""
    key = body.get("prompt_cache_key")
    return key.strip() if isinstance(key, str) else ""


def ensure_context(body: dict) -> dict:
    if isinstance(body.get(_CONTEXT_KEY), RequestContext):
        return body
    return {**body, _CONTEXT_KEY: RequestContext(anchor=_session_anchor(body))}


def context_of(body: dict) -> RequestContext:
    value = body.get(_CONTEXT_KEY)
    return value if isinstance(value, RequestContext) else RequestContext(anchor=_session_anchor(body))


def _prune(now: float) -> None:
    while _sessions:
        first = next(iter(_sessions))
        if len(_sessions) <= MAX_SESSIONS and now - _sessions[first].touched < TTL_SECONDS:
            break
        _sessions.popitem(last=False)
    while _responses:
        first = next(iter(_responses))
        if len(_responses) <= MAX_RESPONSES and now - _responses[first][2] < TTL_SECONDS:
            break
        _responses.popitem(last=False)


def next_attempt(
    context: RequestContext, *, principal: str, account: str, base_url: str,
    model: str, anchor: str = "", previous_response_id: str = "", model_enum: str = "",
) -> RequestIdentity:
    owner = _digest(principal, account, base_url)
    now = time.monotonic()
    with _guard:
        _prune(now)
        round_key = (owner, model)
        current = context.rounds.get(round_key)
        if current is None:
            # A missing tenant/anchor never merges callers by their opening text.
            key = _digest(owner, "anchor", anchor) if principal and anchor else _digest(owner, "request", context.nonce)
            session = None
            if principal and previous_response_id and not anchor:
                linked = _responses.get(_digest(owner, previous_response_id))
                if linked:
                    candidate = _sessions.get(linked[0])
                    if candidate and candidate.cascade == linked[1] and candidate.model == model:
                        session = candidate
                        key = candidate.key
            if session is None:
                session = _sessions.get(key)
            if session is None or session.model != model:
                session = _Session(owner=owner, key=key, model=model)
                _sessions[key] = session
            if session.round >= 0:
                session.step += session.next_events
            session.next_events = 1
            session.round += 1
            current = _Round(session, session.round)
            context.rounds[round_key] = current
        session = current.session
        session.step += 1
        session.touched = now
        if _sessions.get(session.key) is session:
            _sessions.move_to_end(session.key)
        identity = RequestIdentity(
            owner, session.key, session.cascade, session.trajectory,
            current.index, session.step, int(time.time() * 1000), model_enum,
        )
        _prune(now)
        return identity


def complete_response(identity: RequestIdentity, response: dict) -> None:
    if response.get("status") not in {"completed", "incomplete"}:
        return
    events = sum(item.get("type") == "function_call" for item in response.get("output", []) if isinstance(item, dict))
    with _guard:
        session = _sessions.get(identity.session_key)
        if session is not None and session.cascade == identity.cascade and session.round == identity.round:
            session.next_events = max(1, events)


def remember_response(response_id: str, identity: RequestIdentity) -> None:
    now = time.monotonic()
    with _guard:
        _prune(now)
        session = _sessions.get(identity.session_key)
        if session is None or session.cascade != identity.cascade:
            return
        key = _digest(identity.owner, response_id)
        _responses[key] = (session.key, session.cascade, now)
        _responses.move_to_end(key)
        _prune(now)


def clear_for_tests() -> None:
    with _guard:
        _sessions.clear()
        _responses.clear()
