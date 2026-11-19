"""Parse Antigravity / Cloud Code 429 bodies.

Google puts RetryDelay and the real reason in ``error.details``, not in the
HTTP Retry-After header. Split:

- ``RATE_LIMIT_EXCEEDED`` + delay < 3s → same-account short retry
- longer/unknown delays and ``QUOTA_EXHAUSTED`` → exact account+model pause
- only structured ``INSUFFICIENT_G1_CREDITS_BALANCE`` proves account exhaustion.

Neither model metadata, generic quota wording nor a long delay proves scope.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
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


def _identifier(value: Any, *, domain: bool = False) -> str:
    # Diagnostics never copy messages, URLs or opaque metadata.
    pattern = r"[a-zA-Z0-9.-]{1,128}" if domain else r"[A-Z][A-Z0-9_]{0,127}"
    return value if isinstance(value, str) and re.fullmatch(pattern, value) else ""


def parse_antigravity_error(raw: Any, *, http_status: int | None = None) -> dict[str, Any]:
    """Classify only structured evidence; permission denial alone has no pool scope.

    VALIDATION_REQUIRED is an account challenge, not a request-wide policy ban.
    INVALID_ARGUMENT/OUT_OF_RANGE are protocol request errors. No policy-ban
    reason is inferred from prose, model names, identity text or RetryInfo.
    """
    payload = _as_object(raw)
    error = payload.get("error") if isinstance(payload.get("error"), dict) else payload
    status = _identifier(error.get("status"))
    # Numeric Google error codes describe HTTP categories, not request scope.
    code = error.get("code") if type(error.get("code")) is int else None
    infos = []
    retry_info = None
    quota_delay = None
    for item in error.get("details", []) if isinstance(error.get("details"), list) else []:
        if not isinstance(item, dict):
            continue
        if item.get("@type") == RETRY_INFO_TYPE and retry_info is None:
            retry_info = parse_google_duration(item.get("retryDelay", item.get("retry_delay")))
        elif item.get("@type") == ERROR_INFO_TYPE:
            infos.append((_identifier(item.get("reason")), _identifier(item.get("domain"), domain=True)))
            meta = item.get("metadata")
            if quota_delay is None and isinstance(meta, dict):
                quota_delay = parse_google_duration(meta.get("quotaResetDelay") or meta.get("quota_reset_delay"))
    reasons = {reason for reason, _ in infos}
    if status == "VALIDATION_REQUIRED" or "VALIDATION_REQUIRED" in reasons:
        kind = "validation_required"
        selected = "VALIDATION_REQUIRED"
    elif status in {"INVALID_ARGUMENT", "OUT_OF_RANGE"}:
        kind, selected = "request_rejected", status
    elif status in {"PERMISSION_DENIED", "UNAUTHENTICATED"} or http_status in {401, 403} or code in {401, 403}:
        kind, selected = "account_rejected", status
    elif "INSUFFICIENT_G1_CREDITS_BALANCE" in reasons:
        kind = "account_quota_exhausted"
        selected = "INSUFFICIENT_G1_CREDITS_BALANCE"
    elif "QUOTA_EXHAUSTED" in reasons:
        kind, selected = "quota_exhausted", "QUOTA_EXHAUSTED"
    elif "RATE_LIMIT_EXCEEDED" in reasons:
        kind, selected = "rate_limit", "RATE_LIMIT_EXCEEDED"
    else:
        kind, selected = "unknown", next((r for r, _ in infos if r), "")
    reason, domain = next(((r, d) for r, d in infos if r == selected), (selected, ""))
    # In contradictory envelopes the protocol rejection wins even over a
    # structured quota reason. Keep the original identities in diagnostics only.
    diagnostic = {
        "http_status": http_status, "status": status,
        "error_info": [{"reason": r, "domain": d} for r, d in infos if r or d],
        "retry_info_seconds": retry_info,
    }
    return {
        "reason": reason, "domain": domain, "status": status,
        "classification": kind, "diagnostic": diagnostic,
        "retry_after": retry_info if retry_info is not None else quota_delay,
        "quota_exhausted": kind in {"quota_exhausted", "account_quota_exhausted"},
        "account_quota_exhausted": kind == "account_quota_exhausted",
        "stop_failover": kind == "request_rejected",
    }


@dataclass(frozen=True)
class GoogleErrorObservation:
    """In-process decoder observation; never reconstructed from client JSON."""

    error: Any

    @classmethod
    def capture(cls, error: Any) -> GoogleErrorObservation:
        return cls(copy.deepcopy(error))

    def signal(self) -> dict[str, Any]:
        return parse_antigravity_error({"error": self.error}, http_status=200)


# Presentation only: these identities must never drive account/quota actions.
LOCAL_ERROR_MESSAGES = {
    "upstream_stream_incomplete": "Antigravity stream ended without a completion reason",
    "stream_truncated": "Antigravity stream ended before a terminal event",
    "timeout": "Antigravity upstream timed out; completion is unknown",
    "transport_error": "Antigravity upstream connection failed; completion is unknown",
    "invalid_upstream_json": "Antigravity returned invalid JSON",
    "invalid_upstream_utf8": "Antigravity returned invalid UTF-8",
    "invalid_upstream_stream": "Antigravity returned an invalid stream",
    "transform_error": "Antigravity response could not be decoded",
    "request_transform_error": "Parrot could not prepare the Antigravity request; check model availability and request compatibility",
    "guard_error": "Parrot could not dispatch this Antigravity request; check model availability and thinking-tier compatibility",
    "candidate_guard": "Parrot could not dispatch this Antigravity model; check model availability and thinking-tier compatibility",
    "antigravity_thinking_unsupported": "Antigravity model does not support the requested thinking tier or reasoning effort",
    "antigravity_model_unavailable": "Antigravity model is unavailable or changed before dispatch",
    "antigravity_unsupported_input": "Antigravity does not support one or more input items or server-side state fields in this request",
    "antigravity_unsupported_tool": "Antigravity does not support one or more tool definitions or tool_choice modes in this request",
    "antigravity_unsupported_tool_result": "Antigravity tool results only support text and supported images",
    "antigravity_invalid_function_call": "Antigravity cannot replay a function_call without a valid function name",
    "antigravity_remote_image_failed": "Parrot could not download the remote input image for Antigravity",
    "antigravity_invalid_image": "Antigravity rejected the input image URL or format",
    "request_invalid": "Parrot could not process this Antigravity request",
    "no_images": "Antigravity returned no images; generation was not repeated",
    "image_result_error": "Antigravity image result could not be processed; generation was not repeated",
}


def present_error(signal: dict[str, Any], *, local_code: str | None = None) -> dict[str, Any]:
    """Enrich safe diagnostics without changing retry/health classification."""
    result = dict(signal)
    diagnostic = {k: v for k, v in signal['diagnostic'].items() if v is not None and v != '' and v != []}
    if local_code in LOCAL_ERROR_MESSAGES:
        diagnostic['local_code'] = local_code
    if 'error_info' in diagnostic:
        diagnostic['error_info'] = [{k: v for k, v in item.items() if v} for item in diagnostic['error_info']]
    code = diagnostic.get('generation_code') or diagnostic.get('local_code') or signal['reason'] or signal['status']
    if not code:
        status = diagnostic.get('http_status')
        code = f'upstream_http_{status}' if status and status >= 400 else 'antigravity_upstream_error'
    diagnostic['category'] = (
        'generation_error' if diagnostic.get('generation_code') else
        diagnostic.get('local_code') or
        (signal['classification'] if signal['classification'] != 'unknown' else 'upstream_error')
    )
    result['public_code'] = code
    result['diagnostic'] = diagnostic
    return result


def safe_error_message(signal: dict[str, Any]) -> str:
    signal = present_error(signal)
    code = signal['public_code']
    messages = {
        'request_rejected': 'Antigravity rejected the request parameters',
        'validation_required': 'Antigravity account requires verification',
        'account_rejected': 'Antigravity account authentication or permission failed',
        'quota_exhausted': 'Antigravity model quota is exhausted',
        'account_quota_exhausted': 'Antigravity account credits are exhausted',
        'rate_limit': 'Antigravity rate limit was reached',
    }
    message = LOCAL_ERROR_MESSAGES.get(code) or messages.get(signal['classification'])
    if not message:
        message = ('Antigravity generation failed' if signal['diagnostic'].get('generation_code')
                   else 'Antigravity upstream returned an error')
    return message + ': ' + json.dumps({'code': code, **signal['diagnostic']}, ensure_ascii=False)


class ImageResponseError(ValueError):
    """Trusted adapter observation, not exception prose or client metadata."""

    def __init__(self, signal):
        self.signal = present_error(signal)
        super().__init__(safe_error_message(self.signal))


def parse_antigravity_429(raw: Any) -> dict[str, Any]:
    """Compatibility entry point shared by text/image quota and retry policy."""
    return parse_antigravity_error(raw, http_status=429)


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
