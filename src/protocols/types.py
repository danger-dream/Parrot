"""Shared protocol runtime types.

The initial runtime migration only needs a typed representation of the legacy
failover toolkit.  It deliberately mirrors the old dict shape so existing
failover code can keep its behaviour while callers move to a registry boundary.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProtocolToolkit:
    """Legacy per-protocol helpers used by failover.

    This is the Phase 1 shell around the old ``_UPSTREAM_TOOLKIT`` mapping in
    ``failover.py``.  The fields are intentionally equivalent to the old keys:
    no conversion, scheduling, commit-boundary, or response-restore behaviour is
    changed by introducing this dataclass.
    """

    name: str
    stream_tracker: type
    stream_builder: type
    first_event_parser: Callable[[bytes], dict[str, Any] | None]
    extract_usage_json: Callable[[Any], dict[str, int]]
    is_upstream_error_json: Callable[[dict[str, Any]], bool]

    def as_legacy_dict(self) -> dict[str, Any]:
        """Return the exact dict shape expected by existing failover code."""
        return {
            "stream_tracker": self.stream_tracker,
            "stream_builder": self.stream_builder,
            "first_event_parser": self.first_event_parser,
            "extract_usage_json": self.extract_usage_json,
            "is_upstream_error_json": self.is_upstream_error_json,
        }
