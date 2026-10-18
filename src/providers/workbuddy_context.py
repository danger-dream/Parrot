"""Request-local correlation only; never infer sessions/turns from transcript text."""
from __future__ import annotations

import hashlib
import json
import secrets
import time
import uuid

from .. import cache_hints

KEY = "_parrot_workbuddy_context"
HINTS = "_parrot_workbuddy_client_ids"


def uuid7():
    return f"{(int(time.time() * 1000) << 80) | (7 << 76) | (secrets.randbits(12) << 64) | (2 << 62) | secrets.randbits(62):032x}"


def capture_headers(body, headers):
    body.pop(KEY, None)
    body.pop(HINTS, None)
    hints = {}
    for key, names in {"session": ("x-conversation-id", "session-id", "x-session-id", "x-claude-code-session-id"),
                       "turn": ("x-conversation-request-id", "x-query-id")}.items():
        value = next((headers.get(n) for n in names if headers.get(n)), None)
        if isinstance(value, str) and value.strip():
            hints[key] = value.strip()
    if hints:
        body[HINTS] = hints


def ensure_context(body, *, request_id="", api_key_name=""):
    out = dict(body)
    if isinstance(out.get(KEY), dict):
        return out
    hints = out.get(HINTS) or {}
    session = hints.get("session") or cache_hints.anthropic_session_id(body)
    # No stable session? Leave Conversation-ID absent rather than inventing a
    # durable relationship. Logical-request IDs still survive 401 replay.
    out[KEY] = {"session": session, "turn": hints.get("turn"), "owner": api_key_name or body.get("_api_key_name") or body.get("_parrot_api_key_name", ""),
                "logical": request_id or uuid.uuid4().hex, "message": uuid7(), "generated_turn": uuid7(),
                "ide_turn": uuid.uuid4().hex}
    return out


def _mapped(value, owner, kind):
    raw = hashlib.sha256(json.dumps(["parrot:workbuddy", owner, kind, value], ensure_ascii=False).encode()).digest()[:16]
    return uuid.UUID(bytes=raw, version=4)


def headers(body, account_key, *, ide=False, workbuddy_cli=False):
    ctx = ensure_context(body)[KEY]
    owner = [account_key, ctx["owner"]]
    if workbuddy_cli:
        return _workbuddy_cli_headers(ctx, owner)
    result = {}
    if ctx.get("session"):
        result["X-Conversation-ID"] = str(_mapped(ctx["session"], owner, "session"))
    turn = ctx.get("turn")
    if turn:
        # Preserve an explicit valid CLI UUIDv7; opaque caller IDs are mapped
        # deterministically (format is not a proven server requirement).
        try:
            parsed = uuid.UUID(turn)
        except (ValueError, TypeError, AttributeError):
            parsed = None
        turn = parsed.hex if parsed and parsed.version == 7 else _mapped(turn, owner, "turn").hex
    else:
        turn = ctx["ide_turn"] if ide else ctx["generated_turn"]
    message = uuid.uuid4().hex if ide else ctx["message"]
    result.update({"X-Conversation-Request-ID": turn, "X-Conversation-Message-ID": message,
                   "X-Request-ID": turn if ide else message})
    return result


def _cli_id(value, owner, kind):
    mapped = _mapped(value, owner, kind)
    try:
        parsed = uuid.UUID(value)
    except (ValueError, TypeError, AttributeError):
        parsed = None
    if parsed and parsed.version == 7:
        # Keep the real creation timestamp but scope randomness to the selected
        # account, including when failover reuses the logical request context.
        bits = (parsed.int >> 80 << 80) | (7 << 76) | (mapped.int & ((1 << 76) - 1))
        return uuid.UUID(int=bits).hex
    return mapped.hex


def _workbuddy_cli_headers(ctx, owner):
    # With no caller session, this logical request is the complete local session;
    # it survives an authorized 401 replay, not unrelated future requests.
    session = str(_mapped(ctx.get("session") or ctx["logical"], owner, "session"))
    turn = _cli_id(ctx.get("turn") or ctx["generated_turn"], owner, "turn")
    message = _cli_id(ctx["message"], owner, "message")
    # No upstream OTel context is installed for this profile. Official SDK
    # propagation is conditional; do not fabricate trace/B3 context here.
    return {"X-Conversation-ID": session, "X-Conversation-Request-ID": turn,
            "X-Root-Request-ID": turn, "X-Conversation-Message-ID": message, "X-Request-ID": message}
