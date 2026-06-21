"""Protocol toolkit registry.

This module is the first Protocol Runtime seam.  It centralizes the three
existing upstream protocol helper bundles that used to live inside
``failover.py``.  The registry is behaviour-preserving: helpers still come from
``src.upstream`` and expose the same semantics as before.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any

from .. import upstream
from .types import ProtocolToolkit


def is_anthropic_error_json(obj: dict[str, Any]) -> bool:
    """Anthropic non-stream error response detector."""
    return obj.get("type") == "error" or isinstance(obj.get("error"), dict)


def is_openai_error_json(obj: dict[str, Any]) -> bool:
    """OpenAI-family non-stream error response detector."""
    return isinstance(obj.get("error"), dict)


_TOOLKITS: dict[str, ProtocolToolkit] = {
    "anthropic": ProtocolToolkit(
        name="anthropic",
        stream_tracker=upstream.SSEUsageTracker,
        stream_builder=upstream.SSEAssistantBuilder,
        first_event_parser=upstream.parse_first_sse_event,
        extract_usage_json=upstream.extract_usage_from_json,
        is_upstream_error_json=is_anthropic_error_json,
    ),
    "openai-chat": ProtocolToolkit(
        name="openai-chat",
        stream_tracker=upstream.ChatSSEUsageTracker,
        stream_builder=upstream.ChatSSEAssistantBuilder,
        first_event_parser=upstream.parse_first_chat_sse_event,
        extract_usage_json=upstream.extract_usage_chat_json,
        is_upstream_error_json=is_openai_error_json,
    ),
    "openai-responses": ProtocolToolkit(
        name="openai-responses",
        stream_tracker=upstream.ResponsesSSEUsageTracker,
        stream_builder=upstream.ResponsesSSEAssistantBuilder,
        first_event_parser=upstream.parse_first_responses_sse_event,
        extract_usage_json=upstream.extract_usage_responses_json,
        is_upstream_error_json=is_openai_error_json,
    ),
}

TOOLKITS = MappingProxyType(_TOOLKITS)


def get_toolkit(protocol: str) -> ProtocolToolkit:
    """Return the registered toolkit for an upstream protocol.

    Raise ``KeyError`` for unknown protocols so callers fail loudly instead of
    silently falling back to the wrong parser.
    """
    return TOOLKITS[protocol]


def registered_protocols() -> tuple[str, ...]:
    """Return protocol names registered in deterministic order."""
    return tuple(_TOOLKITS.keys())
