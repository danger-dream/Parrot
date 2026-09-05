"""Narrow structured downstream↔upstream Codex identity response mapping.

The authoritative request snapshot normally means no response rewrite is needed.
When a native client requires reversible identifiers, mappings are applied only to
known JSON/SSE identity fields.  Ordinary assistant text is never byte-replaced.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

_IDENTITY_FIELDS = frozenset({
    "installation_id",
    "session_id",
    "thread_id",
    "turn_id",
    "window_id",
    "context_window_id",
    "prompt_cache_key",
    "x-codex-installation-id",
    "x-codex-window-id",
    "x-codex-turn-state",
})


@dataclass(frozen=True)
class IdentityReplacement:
    downstream: str
    upstream: str


@dataclass
class ProtocolIdentityMap:
    """Request-local reversible mappings; no identifiers are derived here."""

    replacements: list[IdentityReplacement] = field(default_factory=list)

    @property
    def enabled(self) -> bool:
        return bool(self.replacements)

    def register(self, downstream: Any, upstream: Any) -> None:
        original = str(downstream or "").strip()
        projected = str(upstream or "").strip()
        if not original or not projected or original == projected:
            return
        replacement = IdentityReplacement(original, projected)
        if replacement not in self.replacements:
            self.replacements.append(replacement)

    def downstream_value(self, upstream: str) -> str:
        for replacement in self.replacements:
            if replacement.upstream == upstream:
                return replacement.downstream
        return upstream

    def upstream_value(self, downstream: str) -> str:
        for replacement in self.replacements:
            if replacement.downstream == downstream:
                return replacement.upstream
        return downstream

    @classmethod
    def from_request(cls, payload: Any, snapshot: Any) -> "ProtocolIdentityMap":
        """Build reversible mappings from explicit downstream identity fields.

        The mapping is response-only and field-aware. Raw values are never used to
        derive an upstream identifier and are never searched in assistant text.
        """
        state = cls()
        if not isinstance(payload, dict) or snapshot is None:
            return state

        projected = {
            "installation_id": getattr(snapshot, "installation_id", ""),
            "session_id": getattr(snapshot, "session_id", ""),
            "thread_id": getattr(snapshot, "thread_id", ""),
            "turn_id": getattr(snapshot, "turn_id", ""),
            "window_id": getattr(snapshot, "window_id", ""),
            "context_window_id": getattr(snapshot, "context_window_id", ""),
            "prompt_cache_key": getattr(snapshot, "prompt_cache_key", ""),
            "x-codex-installation-id": getattr(snapshot, "installation_id", ""),
            "x-codex-window-id": getattr(snapshot, "window_id", ""),
        }

        def register_object(value: Any) -> None:
            if not isinstance(value, dict):
                return
            for field_name, upstream in projected.items():
                if field_name in value:
                    state.register(value.get(field_name), upstream)
            raw_turn_metadata = value.get("x-codex-turn-metadata")
            if isinstance(raw_turn_metadata, str):
                try:
                    parsed = json.loads(raw_turn_metadata)
                except (TypeError, ValueError):
                    parsed = None
                register_object(parsed)

        register_object(payload)
        register_object(payload.get("client_metadata"))
        native = payload.get("_codex_native_identity")
        if isinstance(native, dict):
            register_object(native.get("client_metadata"))
            headers = native.get("headers")
            if isinstance(headers, dict):
                lowered = {str(key).lower(): value for key, value in headers.items()}
                header_projection = {
                    "session-id": projected["session_id"],
                    "session_id": projected["session_id"],
                    "thread-id": projected["thread_id"],
                    "x-client-request-id": projected["thread_id"],
                    "x-codex-window-id": projected["window_id"],
                    "x-codex-installation-id": projected["installation_id"],
                }
                for name, upstream in header_projection.items():
                    if name in lowered:
                        state.register(lowered[name], upstream)
                raw_turn_metadata = lowered.get("x-codex-turn-metadata")
                if isinstance(raw_turn_metadata, str):
                    try:
                        register_object(json.loads(raw_turn_metadata))
                    except (TypeError, ValueError):
                        pass
        return state


def _map_json(value: Any, state: ProtocolIdentityMap, *, expose: bool, field_name: str = "") -> Any:
    if isinstance(value, dict):
        return {
            key: _map_json(child, state, expose=expose, field_name=str(key))
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [
            _map_json(child, state, expose=expose, field_name=field_name)
            for child in value
        ]
    if isinstance(value, str) and field_name in _IDENTITY_FIELDS:
        return state.downstream_value(value) if expose else state.upstream_value(value)
    return value


def _map_json_text(text: str, state: ProtocolIdentityMap, *, expose: bool) -> str | None:
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return None
    mapped = _map_json(value, state, expose=expose)
    return json.dumps(mapped, ensure_ascii=False, separators=(",", ":"))


def _map_payload(data: bytes, state: ProtocolIdentityMap, *, expose: bool) -> bytes:
    if not state.enabled or not data:
        return data
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data

    direct = _map_json_text(text, state, expose=expose)
    if direct is not None:
        return direct.encode("utf-8")

    # Preserve SSE/NDJSON framing. Only parse each explicit JSON payload; a data
    # line containing ordinary text or a UUID-looking assistant delta is untouched.
    changed = False
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        ending = ""
        content = line
        if content.endswith("\r\n"):
            content, ending = content[:-2], "\r\n"
        elif content.endswith("\n"):
            content, ending = content[:-1], "\n"
        prefix = ""
        candidate = content
        if content.startswith("data:"):
            prefix = content[:5]
            candidate = content[5:]
            leading = candidate[: len(candidate) - len(candidate.lstrip())]
            prefix += leading
            candidate = candidate.lstrip()
        mapped = _map_json_text(candidate, state, expose=expose)
        if mapped is None:
            out.append(line)
            continue
        out.append(prefix + mapped + ending)
        changed = changed or mapped != candidate
    return "".join(out).encode("utf-8") if changed else data


def expose_response_payload(data: bytes, state: ProtocolIdentityMap) -> bytes:
    return _map_payload(data, state, expose=True)


def map_response_payload_for_upstream(data: bytes, state: ProtocolIdentityMap) -> bytes:
    return _map_payload(data, state, expose=False)
