"""Parse Antigravity / Cloud Code 429 bodies.

Google puts RetryDelay and the real reason in ``error.details``, not in the
HTTP Retry-After header. Split:

- ``RATE_LIMIT_EXCEEDED`` + delay < 3s → same-account short retry
- longer/unknown delays and ``QUOTA_EXHAUSTED`` → exact account+model pause
- only structured ``INSUFFICIENT_G1_CREDITS_BALANCE`` proves account exhaustion.

Neither model metadata, generic quota wording nor a long delay proves scope.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any


RETRY_INFO_TYPE = "type.googleapis.com/google.rpc.RetryInfo"
ERROR_INFO_TYPE = "type.googleapis.com/google.rpc.ErrorInfo"
QUOTA_REASONS = frozenset({
    "QUOTA_EXHAUSTED",
    "INSUFFICIENT_G1_CREDITS_BALANCE",
})
_DURATION_RE = re.compile(
    r"^\s*(?P<value>-?\d+(?:\.\d+)?)(?P<unit>ns|us|µs|ms|s|m|h)?\s*$",
    re.IGNORECASE,
)
_UNIT_SECONDS = {
    "ns": 1e-9,
    "us": 1e-6,
    "µs": 1e-6,
    "ms": 1e-3,
    "s": 1.0,
    "m": 60.0,
    "h": 3600.0,
}


def parse_antigravity_429(raw: Any) -> dict[str, Any]:
    """Separate exhaustion from proven account scope; do not retain metadata."""
    payload = _as_object(raw)
    error = payload.get("error") if isinstance(payload.get("error"), dict) else payload
    if not isinstance(error, dict):
        error = {}
    details = error.get("details")
    reason = ""
    retry_after: float | None = None
    account_quota = False
    if isinstance(details, list):
        for item in details:
            if not isinstance(item, dict):
                continue
            typ = str(item.get("@type") or "")
            if typ == RETRY_INFO_TYPE and retry_after is None:
                retry_after = parse_google_duration(item.get("retryDelay", item.get("retry_delay")))
            elif typ == ERROR_INFO_TYPE:
                candidate = str(item.get("reason") or "").strip()
                if candidate:
                    reason = candidate
                    account_quota |= candidate.upper() == "INSUFFICIENT_G1_CREDITS_BALANCE"
                if retry_after is None:
                    meta = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
                    retry_after = parse_google_duration(
                        meta.get("quotaResetDelay") or meta.get("quota_reset_delay")
                    )
    if not reason:
        reason = str(error.get("status") or error.get("reason") or "").strip()
    text = json.dumps(payload, ensure_ascii=False).lower() if payload else str(raw or "").lower()
    quota = reason.upper() in QUOTA_REASONS or any(
        marker in text for marker in (
            "quota_exhausted",
            "quota exhausted",
            "insufficient_g1_credits_balance",
            "insufficient g1 credits",
        )
    )
    account_quota |= reason.upper() == "INSUFFICIENT_G1_CREDITS_BALANCE"
    return {
        "reason": reason,
        "retry_after": retry_after,
        "quota_exhausted": quota,
        "account_quota_exhausted": account_quota,
    }


def parse_google_duration(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            number = float(value)
        except OverflowError:
            return None
        return number if number >= 0 and math.isfinite(number) else None
    text = str(value).strip()
    if not text:
        return None
    match = _DURATION_RE.match(text)
    if not match:
        return None
    number = float(match.group("value"))
    if number < 0:
        return None
    unit = (match.group("unit") or "s").lower()
    scale = _UNIT_SECONDS.get(unit)
    if scale is None:
        return None
    seconds = number * scale
    return seconds if math.isfinite(seconds) else None


def _as_object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw or "")
    start = text.find("{")
    if start < 0:
        return {}
    try:
        parsed = json.loads(text[start:])
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}
