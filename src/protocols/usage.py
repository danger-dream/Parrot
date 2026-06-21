"""Usage normalization helpers for Protocol Runtime.

Parrot historically passes usage around as the Anthropic-flavoured four-key dict:
``input_tokens`` / ``output_tokens`` / ``cache_creation`` / ``cache_read``.  The
runtime IR uses clearer names instead: cache writes and reads are explicit.

This module introduces the new representation while keeping boundary functions
that convert back to the legacy dict, so log_db and existing UI semantics remain
unchanged during the migration.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def _to_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    raw: dict | None = None

    def to_legacy_dict(self) -> dict[str, int]:
        """Return the legacy four-key shape expected by log_db/failover."""
        return {
            "input_tokens": int(self.input_tokens or 0),
            "output_tokens": int(self.output_tokens or 0),
            "cache_creation": int(self.cache_write_tokens or 0),
            "cache_read": int(self.cache_read_tokens or 0),
        }


class UsageAccumulator:
    """Mutable usage accumulator with protocol-specific update helpers."""

    def __init__(self) -> None:
        self.usage = Usage()

    def legacy_dict(self) -> dict[str, int]:
        return self.usage.to_legacy_dict()

    def set_from_anthropic_json_usage(self, usage_obj: Any) -> None:
        u = usage_obj if isinstance(usage_obj, dict) else {}
        self.usage = Usage(
            input_tokens=_to_int(u.get("input_tokens")),
            output_tokens=_to_int(u.get("output_tokens")),
            cache_write_tokens=_to_int(u.get("cache_creation_input_tokens")),
            cache_read_tokens=_to_int(u.get("cache_read_input_tokens")),
            raw=dict(u),
        )

    def update_from_anthropic_message_start(self, usage_obj: Any) -> None:
        u = usage_obj if isinstance(usage_obj, dict) else {}
        self.usage.input_tokens = _to_int(u.get("input_tokens"))
        self.usage.cache_write_tokens = _to_int(u.get("cache_creation_input_tokens"))
        self.usage.cache_read_tokens = _to_int(u.get("cache_read_input_tokens"))
        self.usage.raw = dict(u)

    def update_from_anthropic_message_delta(self, usage_obj: Any) -> None:
        u = usage_obj if isinstance(usage_obj, dict) else {}
        if "output_tokens" in u:
            self.usage.output_tokens = max(self.usage.output_tokens, _to_int(u.get("output_tokens")))
        # Some Anthropic-compatible upstreams send input_tokens in message_delta
        # while message_start had 0; others send zero-filled prompt/cache fields
        # with output-only deltas. Preserve the largest cumulative value so a
        # later zero cannot erase message_start accounting.
        if "input_tokens" in u:
            self.usage.input_tokens = max(self.usage.input_tokens, _to_int(u.get("input_tokens")))
        if "cache_creation_input_tokens" in u:
            self.usage.cache_write_tokens = max(
                self.usage.cache_write_tokens,
                _to_int(u.get("cache_creation_input_tokens")),
            )
        if "cache_read_input_tokens" in u:
            self.usage.cache_read_tokens = max(
                self.usage.cache_read_tokens,
                _to_int(u.get("cache_read_input_tokens")),
            )
        if u:
            self.usage.raw = dict(u)

    def set_from_openai_chat_usage(self, usage_obj: Any) -> None:
        u = usage_obj if isinstance(usage_obj, dict) else {}
        details = u.get("prompt_tokens_details") or {}
        completion_details = u.get("completion_tokens_details") or {}
        prompt_total = _to_int(u.get("prompt_tokens"))
        cached = _to_int(details.get("cached_tokens")) if isinstance(details, dict) else 0
        reasoning = _to_int(completion_details.get("reasoning_tokens")) if isinstance(completion_details, dict) else 0
        self.usage = Usage(
            input_tokens=max(0, prompt_total - cached),
            output_tokens=_to_int(u.get("completion_tokens")),
            cache_read_tokens=cached,
            cache_write_tokens=0,
            reasoning_tokens=reasoning,
            raw=dict(u),
        )

    def set_from_openai_responses_usage(self, usage_obj: Any) -> None:
        u = usage_obj if isinstance(usage_obj, dict) else {}
        details = u.get("input_tokens_details") or {}
        output_details = u.get("output_tokens_details") or {}
        prompt_total = _to_int(u.get("input_tokens"))
        cached = _to_int(details.get("cached_tokens")) if isinstance(details, dict) else 0
        reasoning = _to_int(output_details.get("reasoning_tokens")) if isinstance(output_details, dict) else 0
        self.usage = Usage(
            input_tokens=max(0, prompt_total - cached),
            output_tokens=_to_int(u.get("output_tokens")),
            cache_read_tokens=cached,
            cache_write_tokens=0,
            reasoning_tokens=reasoning,
            raw=dict(u),
        )


def zero_legacy_usage() -> dict[str, int]:
    return Usage().to_legacy_dict()


def legacy_usage_from_anthropic_json(obj: Any) -> dict[str, int]:
    acc = UsageAccumulator()
    if isinstance(obj, dict):
        acc.set_from_anthropic_json_usage(obj.get("usage") or {})
    return acc.legacy_dict()


def legacy_usage_from_openai_chat_json(obj: Any) -> dict[str, int]:
    acc = UsageAccumulator()
    if isinstance(obj, dict):
        acc.set_from_openai_chat_usage(obj.get("usage") or {})
    return acc.legacy_dict()


def legacy_usage_from_openai_responses_json(obj: Any) -> dict[str, int]:
    acc = UsageAccumulator()
    if isinstance(obj, dict):
        acc.set_from_openai_responses_usage(obj.get("usage") or {})
    return acc.legacy_dict()
