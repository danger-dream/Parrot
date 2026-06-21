"""Transport-layer metadata helpers.

Phase 6 starts by separating transport metadata (status/headers/content-type) from
protocol parsing.  Sending/streaming still lives in failover for now; this module
only provides behaviour-preserving wrappers around response metadata handling.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

_FORWARD_HEADER_NAMES = ("content-type", "x-request-id", "request-id")


@dataclass(frozen=True)
class TransportMetadata:
    status_code: int | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    content_type: str | None = None

    def forward_headers(self) -> dict[str, str]:
        return pick_forward_headers(self.headers)


def _header_get(headers: Mapping[str, str], name: str) -> str | None:
    try:
        val = headers.get(name)  # httpx.Headers is case-insensitive.
    except Exception:
        val = None
    if val is not None:
        return val
    lname = name.lower()
    for k, v in headers.items():
        if str(k).lower() == lname:
            return v
    return None


def pick_forward_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Return the upstream headers Parrot forwards downstream today."""
    out: dict[str, str] = {}
    for name in _FORWARD_HEADER_NAMES:
        val = _header_get(headers, name)
        if val is not None:
            out[name] = val
    return out


def metadata_from_response(resp) -> TransportMetadata:
    headers = getattr(resp, "headers", {}) or {}
    status = getattr(resp, "status_code", None)
    try:
        status_i = int(status) if status is not None else None
    except Exception:
        status_i = None
    return TransportMetadata(
        status_code=status_i,
        headers=headers,
        content_type=_header_get(headers, "content-type"),
    )
