"""Recovery moments for error rules: times written in error text, usage windows, period boundaries."""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import regex

from .error_rule_conditions import compile_user_regex, search_user_regex


_ABS_TIME_RE = re.compile(
    r"(20\d{2})[-/](\d{1,2})[-/](\d{1,2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?"
    r"\s*(Z|UTC\s*[+-]\s*\d{1,2}(?::?\d{2})?|GMT\s*[+-]\s*\d{1,2}(?::?\d{2})?|UTC|GMT|[+-]\d{2}:?\d{2})?",
    re.IGNORECASE,
)
_REL_TIME_RE = re.compile(
    r"(?:in|after|retry after|try again in|wait)\s+(\d+(?:\.\d+)?)\s*"
    r"(ms|milliseconds?|s|secs?|seconds?|m|mins?|minutes?|h|hrs?|hours?)\b"
    r"|(\d+(?:\.\d+)?)\s*(秒|分钟|分|小时)后",
    re.IGNORECASE,
)
_UNIT_SECONDS = {"ms": 0.001, "millisecond": 0.001, "milliseconds": 0.001, "s": 1, "sec": 1, "secs": 1,
                 "second": 1, "seconds": 1, "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
                 "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
                 "秒": 1, "分": 60, "分钟": 60, "小时": 3600}


def tz_from_spec(spec: str) -> timezone:
    spec = (spec or "+08:00").strip().upper()
    if spec in ("UTC", "Z", "GMT"):
        return timezone.utc
    m = re.fullmatch(r"(?:UTC|GMT)?\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?", spec)
    if not m:
        return timezone(timedelta(hours=8))
    delta = timedelta(hours=int(m.group(2)), minutes=int(m.group(3) or 0))
    return timezone(delta if m.group(1) == "+" else -delta)


def _absolute_ms(m: re.Match, default_tz: str) -> int | None:
    try:
        tz = tz_from_spec(m.group(7)) if m.group(7) else tz_from_spec(default_tz)
        moment = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)),
                          int(m.group(5)), int(m.group(6) or 0), tzinfo=tz)
    except ValueError:
        return None
    return int(moment.timestamp() * 1000)


def _relative_seconds(m: re.Match) -> float | None:
    number, unit = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
    try:
        return float(number) * _UNIT_SECONDS[unit.lower() if unit.isascii() else unit]
    except (KeyError, ValueError, AttributeError):
        return None


def time_from_text(text: str, *, now_ms: int, pattern: str = "", tz: str = "+08:00") -> int | None:
    """First recovery moment written in ``text`` (absolute or relative), as epoch ms."""
    source = text or ""
    if pattern:
        try:
            found = search_user_regex(compile_user_regex(pattern), source)
        except regex.error:
            return None
        if not found:
            return None
        source = found.group(1) if found.groups() and found.group(1) else found.group(0)
        if re.fullmatch(r"\s*\d+(?:\.\d+)?\s*", source):
            return now_ms + int(float(source) * 1000)
    m = _ABS_TIME_RE.search(source)
    if m:
        return _absolute_ms(m, tz)
    m = _REL_TIME_RE.search(source)
    if m:
        seconds = _relative_seconds(m)
        if seconds is not None:
            return now_ms + int(seconds * 1000)
    return None


def usage_window_end(channel: Any, model: str, now_ms: int) -> int | None:
    """End of the exhausted window from the provider usage snapshot, if Parrot has one."""
    if channel is None:
        return None
    try:
        from . import provider_usage
        snap = (provider_usage.cached(channel) or {}).get("snapshot") or {}
    except Exception:
        return None
    ends: list[int] = []
    for window in snap.get("windows") or []:
        if not isinstance(window, dict):
            continue
        exhausted = (window.get("used_percent") is not None and float(window["used_percent"]) >= 100) or (
            window.get("remaining") is not None and float(window["remaining"]) <= 0)
        if not exhausted:
            continue
        for key in ("reset_at", "end_at"):
            value = window.get(key)
            if not value:
                continue
            try:
                moment = int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000)
            except ValueError:
                continue
            if moment > now_ms:
                ends.append(moment)
                break
    # Every exhausted window must reset before the model is usable again.
    return max(ends) if ends else None


def next_boundary(now_ms: int, hours: int, tz: str = "+08:00") -> int:
    zone = tz_from_spec(tz)
    now = datetime.fromtimestamp(now_ms / 1000, zone)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    step = timedelta(hours=hours)
    elapsed = now - midnight
    k = math.floor(elapsed / step) + 1
    # Windows restart at local midnight: 20:00 + 5 h ends at 00:00, not 01:00.
    return int(min(midnight + k * step, midnight + timedelta(days=1)).timestamp() * 1000)
