"""Protocol Runtime error classification.

This is the Phase 4 entry point.  It centralizes classification data while still
exposing compatibility helpers for existing failover/logging code.  The legacy
``src.errors`` module remains responsible for encoding Anthropic/OpenAI error
payloads; this module decides what an upstream/transport error *means*.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from .. import errors as legacy_errors

ErrorCategory = Literal[
    "auth",
    "permission",
    "rate_limit",
    "quota",
    "invalid_request",
    "context_length",
    "request_too_large",
    "timeout",
    "upstream",
    "overloaded",
    "not_found",
    "transport",
    "blacklist",
    "client_cancelled",
]


@dataclass(frozen=True)
class NormalizedError:
    category: ErrorCategory
    http_status: int
    upstream_code: str | None
    message: str
    retryable_before_commit: bool
    retryable_after_commit: bool
    should_cooldown: bool
    should_score_failure: bool
    should_refresh_oauth: bool
    request_fixup: str | None
    raw: Any = None

    @property
    def anthropic_error_type(self) -> str:
        return legacy_errors.classify_http_status(self.http_status)

    @property
    def openai_error_type(self) -> str:
        return legacy_errors.classify_http_status_openai(self.http_status)


def category_from_http_status(status: int) -> ErrorCategory:
    if status == 401:
        return "auth"
    if status == 403:
        return "permission"
    if status == 404:
        return "not_found"
    if status in (408, 504):
        return "timeout"
    if status == 413:
        return "request_too_large"
    if status == 429:
        return "rate_limit"
    if status == 529:
        return "overloaded"
    if status >= 500:
        return "upstream"
    if status >= 400:
        return "invalid_request"
    return "upstream"


def normalize_http_status(status: int, *, message: str | None = None, raw: Any = None) -> NormalizedError:
    category = category_from_http_status(status)
    retryable_before_commit = category in {"rate_limit", "timeout", "upstream", "overloaded"}
    should_refresh_oauth = status in (401, 403)
    # Preserve current Parrot behaviour: 400/request-invalid-like outcomes are
    # not channel cooldowns; transient upstream/timeout/rate-limit classes are.
    should_cooldown = category in {"rate_limit", "timeout", "upstream", "overloaded"}
    return NormalizedError(
        category=category,
        http_status=int(status),
        upstream_code=None,
        message=message or f"upstream HTTP {status}",
        retryable_before_commit=retryable_before_commit,
        retryable_after_commit=False,
        should_cooldown=should_cooldown,
        should_score_failure=True,
        should_refresh_oauth=should_refresh_oauth,
        request_fixup=None,
        raw=raw,
    )


def legacy_anthropic_error_type_for_http_status(status: int) -> str:
    return normalize_http_status(status).anthropic_error_type


def legacy_openai_error_type_for_http_status(status: int) -> str:
    return normalize_http_status(status).openai_error_type


def classify_attempt_outcome(outcome: str, http_status: int | None) -> NormalizedError:
    if http_status is not None:
        return normalize_http_status(http_status, message=outcome, raw={"outcome": outcome})
    if outcome in ("connect_timeout", "first_byte_timeout", "idle_timeout", "total_timeout"):
        return NormalizedError(
            category="timeout",
            http_status=504,
            upstream_code=None,
            message=outcome,
            retryable_before_commit=True,
            retryable_after_commit=False,
            should_cooldown=True,
            should_score_failure=True,
            should_refresh_oauth=False,
            request_fixup=None,
            raw={"outcome": outcome},
        )
    if outcome == "transform_error":
        return NormalizedError(
            category="invalid_request",
            http_status=400,
            upstream_code=None,
            message=outcome,
            retryable_before_commit=False,
            retryable_after_commit=False,
            should_cooldown=False,
            should_score_failure=True,
            should_refresh_oauth=False,
            request_fixup=None,
            raw={"outcome": outcome},
        )
    return NormalizedError(
        category="upstream",
        http_status=500,
        upstream_code=None,
        message=outcome,
        retryable_before_commit=True,
        retryable_after_commit=False,
        should_cooldown=True,
        should_score_failure=True,
        should_refresh_oauth=False,
        request_fixup=None,
        raw={"outcome": outcome},
    )


def extract_error_info(payload: Any, fallback: str = "upstream stream error") -> tuple[str | None, str]:
    """Return ``(error_code, readable_message)`` for JSON/SSE/WS error payloads.

    Covers current wrappers used by Anthropic, OpenAI Chat, OpenAI Responses and
    Responses WS.  This function is intentionally formatting-compatible with the
    old ``upstream._format_stream_error_info`` helper.
    """
    err = payload
    if isinstance(payload, dict):
        if isinstance(payload.get("error"), dict):
            err = payload["error"]
        elif isinstance(payload.get("response"), dict) and isinstance(payload["response"].get("error"), dict):
            err = payload["response"]["error"]

    if isinstance(err, dict):
        code = (
            err.get("code")
            or err.get("type")
            or err.get("error_type")
            or err.get("status")
        )
        message = err.get("message") or err.get("reason") or fallback
        if code and str(code) not in str(message):
            return str(code), f"{code}: {message}"
        return str(code) if code else None, str(message)

    if isinstance(payload, dict):
        top_type = payload.get("type")
        code = (
            payload.get("code")
            or payload.get("error_type")
            or (top_type if top_type != "error" else None)
            or payload.get("status")
        )
        message = payload.get("message") or payload.get("reason") or fallback
        if code and str(code) not in str(message):
            return str(code), f"{code}: {message}"
        return str(code) if code else None, str(message)

    return None, fallback
