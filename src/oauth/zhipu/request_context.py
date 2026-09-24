"""ZCode attribution only: no Agent-loop inference or model-parameter rewriting."""
from __future__ import annotations

import json
import uuid

from ... import cache_hints

_PREFIX = "_parrot_zcode_"
MID_SYSTEM_BETA = "mid-conversation-system-2026-04-07"


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def capture_headers(body, headers):
    """Only HTTP headers may supply private client hints; discard body spoofs."""
    for key in list(body):
        if isinstance(key, str) and key.startswith(_PREFIX):
            body.pop(key)
    session = (_text(headers.get("session-id")) or _text(headers.get("x-session-id"))
               or _text(headers.get("x-claude-code-session-id")))
    query = _text(headers.get("x-query-id"))
    if session:
        body[_PREFIX + "client_session"] = session
    if query:
        body[_PREFIX + "client_query"] = query


def _wire_id(value, *, kind, owner):
    for prefix in (("sess_", "subagent_agent_") if kind == "session" else ("query_",)):
        if value.startswith(prefix):
            value = value[len(prefix):]
    try:
        return str(uuid.UUID(value))
    except ValueError:
        # Explicit opaque IDs remain stable without exposing tenant names or
        # arbitrary client strings in upstream headers. No transcript guessing.
        return str(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(
            ["parrot:zhipu", kind, owner, value], ensure_ascii=False)))


def ensure_request_context(body, *, api_key_name=""):
    """One downstream logical request owns the fallback IDs, including retries."""
    out = dict(body)
    owner = api_key_name or _text(body.get("_api_key_name")) or _text(body.get("_parrot_api_key_name"))
    if not out.get(_PREFIX + "session"):
        session = (_text(body.get(_PREFIX + "client_session"))
                   or cache_hints.anthropic_session_id(body))
        out[_PREFIX + "session"] = (_wire_id(session, kind="session", owner=owner)
                                      if session else str(uuid.uuid4()))
    if not out.get(_PREFIX + "query"):
        query = _text(body.get(_PREFIX + "client_query"))
        out[_PREFIX + "query"] = (_wire_id(query, kind="query", owner=owner)
                                    if query else str(uuid.uuid4()))
    out.setdefault(_PREFIX + "trace", str(uuid.uuid4()))
    return out


def add_metadata(payload, *, device_id, session_id):
    """Fill missing ZCode telemetry, never replace a caller's explicit user_id."""
    metadata = payload.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        return
    metadata = dict(metadata or {})
    if "user_id" not in metadata:
        metadata["user_id"] = json.dumps({
            "device_id": device_id, "account_uuid": "", "session_id": session_id,
        }, separators=(",", ":"))
        payload["metadata"] = metadata


def needs_mid_system_beta(payload):
    # Top-level system is standard Anthropic; only the extended messages form
    # requires this beta. Do not introduce or reorder any system messages.
    return any(isinstance(message, dict) and message.get("role") == "system"
               for message in payload.get("messages") or [])
