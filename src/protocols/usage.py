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
import math
from typing import Any


_MAX_USAGE_INTEGER = (1 << 63) - 1


def _to_int(value: Any) -> int:
    """Legacy permissive conversion retained for non-ledger compatibility helpers."""
    try:
        return int(value or 0)
    except Exception:
        return 0


def _strict_token(value: Any) -> int | None:
    """Parse an upstream token count without coercing corruption to zero."""
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and (not math.isfinite(value) or not value.is_integer()):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed < 0 or parsed > _MAX_USAGE_INTEGER:
        return None
    return parsed


def _strict_fields(usage_obj: Any, names: tuple[str, ...]) -> tuple[dict[str, int], bool, bool]:
    """Return parsed present fields, whether usage was observed, and validity."""
    if not isinstance(usage_obj, dict):
        return {}, False, usage_obj is None
    present = {name: usage_obj[name] for name in names if name in usage_obj}
    if not present:
        return {}, False, True
    parsed: dict[str, int] = {}
    for name, value in present.items():
        token = _strict_token(value)
        if token is None:
            return {}, False, False
        parsed[name] = token
    return parsed, True, True


CACHE_CREATION_5M_KEY = "cache_creation_5m"
CACHE_CREATION_1H_KEY = "cache_creation_1h"


def anthropic_cache_creation_split(usage_obj: Any) -> tuple[int, int] | None:
    """Return Anthropic's exact 5m/1h cache-write split when present and valid.

    ``cache_creation_input_tokens`` remains the aggregate compatibility field.
    Current Anthropic responses additionally expose a nested ``cache_creation``
    object.  Compatible/older providers may omit it; that absence is not invalid
    usage, but it cannot prove an exact TTL tariff split.
    """
    if not isinstance(usage_obj, dict):
        return None
    details = usage_obj.get("cache_creation")
    if not isinstance(details, dict):
        return None
    if not {
        "ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens",
    }.issubset(details):
        return None
    five_minute = _strict_token(details.get("ephemeral_5m_input_tokens"))
    one_hour = _strict_token(details.get("ephemeral_1h_input_tokens"))
    if five_minute is None or one_hour is None:
        return None
    if "cache_creation_input_tokens" in usage_obj:
        aggregate = _strict_token(usage_obj.get("cache_creation_input_tokens"))
        if aggregate is None or five_minute + one_hour != aggregate:
            return None
    return five_minute, one_hour


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cache_write_5m_tokens: int | None = None
    cache_write_1h_tokens: int | None = None
    reasoning_tokens: int = 0
    raw: dict | None = None

    def to_legacy_dict(self) -> dict[str, int]:
        """Return the legacy shape plus an exact Anthropic TTL split when known."""
        result = {
            "input_tokens": int(self.input_tokens or 0),
            "output_tokens": int(self.output_tokens or 0),
            "cache_creation": int(self.cache_write_tokens or 0),
            "cache_read": int(self.cache_read_tokens or 0),
        }
        if self.cache_write_5m_tokens is not None and self.cache_write_1h_tokens is not None:
            result[CACHE_CREATION_5M_KEY] = int(self.cache_write_5m_tokens)
            result[CACHE_CREATION_1H_KEY] = int(self.cache_write_1h_tokens)
        return result


class UsageAccumulator:
    """Mutable strict usage accumulator with an independent observation fact."""

    def __init__(self) -> None:
        self.usage = Usage()
        self.usage_observed = False
        self.usage_invalid = False
        self._input_observed = False
        self._output_observed = False

    def legacy_dict(self) -> dict[str, int]:
        return self.usage.to_legacy_dict()

    def _reject(self) -> None:
        self.usage_invalid = True
        self.usage_observed = False

    def _refresh_observed(self) -> None:
        self.usage_observed = bool(
            not self.usage_invalid and self._input_observed and self._output_observed
        )

    def set_from_anthropic_json_usage(self, usage_obj: Any) -> None:
        names = (
            "input_tokens", "output_tokens", "cache_creation_input_tokens",
            "cache_read_input_tokens",
        )
        parsed, observed, valid = _strict_fields(usage_obj, names)
        if not valid:
            self._reject()
            return
        if not observed:
            return
        u = usage_obj
        split = anthropic_cache_creation_split(usage_obj)
        self.usage = Usage(
            input_tokens=parsed.get("input_tokens", 0),
            output_tokens=parsed.get("output_tokens", 0),
            cache_write_tokens=parsed.get("cache_creation_input_tokens", 0),
            cache_read_tokens=parsed.get("cache_read_input_tokens", 0),
            cache_write_5m_tokens=(split[0] if split is not None else None),
            cache_write_1h_tokens=(split[1] if split is not None else None),
            raw=dict(u),
        )
        self._input_observed = "input_tokens" in parsed
        self._output_observed = "output_tokens" in parsed
        self._refresh_observed()

    def update_from_anthropic_message_start(self, usage_obj: Any) -> None:
        names = (
            "input_tokens", "output_tokens", "cache_creation_input_tokens",
            "cache_read_input_tokens",
        )
        parsed, observed, valid = _strict_fields(usage_obj, names)
        if not valid:
            self._reject()
            return
        if not observed:
            return
        self.usage.input_tokens = parsed.get("input_tokens", 0)
        self.usage.output_tokens = parsed.get("output_tokens", self.usage.output_tokens)
        self.usage.cache_write_tokens = parsed.get("cache_creation_input_tokens", 0)
        self.usage.cache_read_tokens = parsed.get("cache_read_input_tokens", 0)
        split = anthropic_cache_creation_split(usage_obj)
        self.usage.cache_write_5m_tokens = split[0] if split is not None else None
        self.usage.cache_write_1h_tokens = split[1] if split is not None else None
        self.usage.raw = dict(usage_obj)
        self._input_observed = self._input_observed or "input_tokens" in parsed
        self._output_observed = self._output_observed or "output_tokens" in parsed
        self._refresh_observed()

    def update_from_anthropic_message_delta(self, usage_obj: Any) -> None:
        names = (
            "input_tokens", "output_tokens", "cache_creation_input_tokens",
            "cache_read_input_tokens",
        )
        parsed, observed, valid = _strict_fields(usage_obj, names)
        if not valid:
            self._reject()
            return
        if not observed:
            return
        if "output_tokens" in parsed:
            self.usage.output_tokens = max(self.usage.output_tokens, parsed["output_tokens"])
        # Compatible providers sometimes repeat prompt/cache counters as zero in
        # message_delta. Preserve the cumulative maximum from message_start.
        if "input_tokens" in parsed:
            self.usage.input_tokens = max(self.usage.input_tokens, parsed["input_tokens"])
        if "cache_creation_input_tokens" in parsed:
            self.usage.cache_write_tokens = max(
                self.usage.cache_write_tokens, parsed["cache_creation_input_tokens"],
            )
        if "cache_read_input_tokens" in parsed:
            self.usage.cache_read_tokens = max(
                self.usage.cache_read_tokens, parsed["cache_read_input_tokens"],
            )
        split = anthropic_cache_creation_split(usage_obj)
        if split is not None:
            current_5m = self.usage.cache_write_5m_tokens or 0
            current_1h = self.usage.cache_write_1h_tokens or 0
            self.usage.cache_write_5m_tokens = max(current_5m, split[0])
            self.usage.cache_write_1h_tokens = max(current_1h, split[1])
        self.usage.raw = dict(usage_obj)
        self._input_observed = self._input_observed or "input_tokens" in parsed
        self._output_observed = self._output_observed or "output_tokens" in parsed
        self._refresh_observed()

    def _set_from_openai(self, usage_obj: Any, *, prompt_name: str, output_name: str,
                         details_name: str, output_details_name: str) -> None:
        names = (prompt_name, output_name)
        parsed, observed, valid = _strict_fields(usage_obj, names)
        if not valid:
            self._reject()
            return
        if not isinstance(usage_obj, dict):
            self._reject()
            return
        details = usage_obj.get(details_name, {})
        output_details = usage_obj.get(output_details_name, {})
        if details is None:
            details = {}
        if output_details is None:
            output_details = {}
        if not isinstance(details, dict) or not isinstance(output_details, dict):
            self._reject()
            return
        cached = 0
        if "cached_tokens" in details:
            parsed_cached = _strict_token(details.get("cached_tokens"))
            if parsed_cached is None:
                self._reject()
                return
            cached = parsed_cached
        reasoning = 0
        if "reasoning_tokens" in output_details:
            parsed_reasoning = _strict_token(output_details.get("reasoning_tokens"))
            if parsed_reasoning is None:
                self._reject()
                return
            reasoning = parsed_reasoning
        if not observed:
            return
        if prompt_name not in parsed or output_name not in parsed:
            self._reject()
            return
        prompt_total = parsed.get(prompt_name, 0)
        if cached > prompt_total:
            self._reject()
            return
        self.usage = Usage(
            input_tokens=prompt_total - cached,
            output_tokens=parsed.get(output_name, 0),
            cache_read_tokens=cached,
            cache_write_tokens=0,
            reasoning_tokens=reasoning,
            raw=dict(usage_obj),
        )
        self._input_observed = True
        self._output_observed = True
        self._refresh_observed()

    def set_from_openai_chat_usage(self, usage_obj: Any) -> None:
        self._set_from_openai(
            usage_obj,
            prompt_name="prompt_tokens", output_name="completion_tokens",
            details_name="prompt_tokens_details",
            output_details_name="completion_tokens_details",
        )

    def set_from_openai_responses_usage(self, usage_obj: Any) -> None:
        self._set_from_openai(
            usage_obj,
            prompt_name="input_tokens", output_name="output_tokens",
            details_name="input_tokens_details",
            output_details_name="output_tokens_details",
        )


def zero_legacy_usage() -> dict[str, int]:
    return Usage().to_legacy_dict()


def legacy_usage_from_anthropic_json(obj: Any) -> dict[str, int]:
    """Permissive compatibility extraction; ledger trackers are strict above."""
    usage = obj.get("usage") if isinstance(obj, dict) else None
    usage = usage if isinstance(usage, dict) else {}
    result = {
        "input_tokens": _to_int(usage.get("input_tokens")),
        "output_tokens": _to_int(usage.get("output_tokens")),
        "cache_creation": _to_int(usage.get("cache_creation_input_tokens")),
        "cache_read": _to_int(usage.get("cache_read_input_tokens")),
    }
    split = anthropic_cache_creation_split(usage)
    if split is not None:
        result[CACHE_CREATION_5M_KEY] = split[0]
        result[CACHE_CREATION_1H_KEY] = split[1]
    return result


def legacy_usage_from_openai_chat_json(obj: Any) -> dict[str, int]:
    usage = obj.get("usage") if isinstance(obj, dict) else None
    usage = usage if isinstance(usage, dict) else {}
    details = usage.get("prompt_tokens_details") or {}
    cached = _to_int(details.get("cached_tokens")) if isinstance(details, dict) else 0
    prompt = _to_int(usage.get("prompt_tokens"))
    return {
        "input_tokens": max(0, prompt - cached),
        "output_tokens": _to_int(usage.get("completion_tokens")),
        "cache_creation": 0,
        "cache_read": cached,
    }


def legacy_usage_from_openai_responses_json(obj: Any) -> dict[str, int]:
    usage = obj.get("usage") if isinstance(obj, dict) else None
    usage = usage if isinstance(usage, dict) else {}
    details = usage.get("input_tokens_details") or {}
    cached = _to_int(details.get("cached_tokens")) if isinstance(details, dict) else 0
    prompt = _to_int(usage.get("input_tokens"))
    return {
        "input_tokens": max(0, prompt - cached),
        "output_tokens": _to_int(usage.get("output_tokens")),
        "cache_creation": 0,
        "cache_read": cached,
    }
