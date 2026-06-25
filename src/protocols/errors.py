"""Protocol Runtime error classification.

This is the Phase 4 entry point.  It centralizes classification data while still
exposing compatibility helpers for existing failover/logging code.  The legacy
``src.errors`` module remains responsible for encoding Anthropic/OpenAI error
payloads; this module decides what an upstream/transport error *means*.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
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


CONTEXT_LENGTH_EXCEEDED_CODE = "context_length_exceeded"
RESPONSES_MAX_OUTPUT_INCOMPLETE_REASONS = frozenset({"max_output_tokens", "max_tokens"})


def responses_incomplete_reason(payload: Any) -> str | None:
    """Return ``response.incomplete`` reason from a Responses payload/event.

    OpenAI Responses may report output-budget exhaustion as a terminal
    ``response.incomplete`` event instead of a normal error.  Many downstream
    clients do not understand that terminal event, so Parrot normalizes the
    unambiguous ``max_output_tokens`` case into a context-length style error.
    """
    if not isinstance(payload, dict):
        return None
    resp = payload.get("response") if isinstance(payload.get("response"), dict) else payload
    if not isinstance(resp, dict):
        return None
    details = resp.get("incomplete_details")
    reason = details.get("reason") if isinstance(details, dict) else None
    if reason is None and payload.get("type") == "response.incomplete":
        reason = resp.get("status")
    if reason is None and resp.get("status") == "incomplete":
        details = resp.get("incomplete_details")
        reason = details.get("reason") if isinstance(details, dict) else None
    return str(reason) if reason is not None else None


def is_responses_max_output_incomplete(payload: Any, event_name: str | None = None) -> bool:
    """Whether a Responses terminal incomplete means no usable output budget.

    This intentionally does not inspect token counts or model limits: the
    explicit upstream terminal reason is enough and is stable across models.
    """
    if not isinstance(payload, dict):
        return False
    typ = str(payload.get("type") or event_name or "")
    resp = payload.get("response") if isinstance(payload.get("response"), dict) else payload
    status = resp.get("status") if isinstance(resp, dict) else None
    if typ != "response.incomplete" and status != "incomplete":
        return False
    reason = responses_incomplete_reason(payload)
    return str(reason or "") in RESPONSES_MAX_OUTPUT_INCOMPLETE_REASONS


_CONTEXT_LENGTH_ERROR_CODES = frozenset({
    CONTEXT_LENGTH_EXCEEDED_CODE,
    "context_too_large",
    "context_window_exceeded",
    "model_context_window_exceeded",
    "request_too_large",
})
_CONTEXT_LENGTH_MESSAGE_MARKERS = (
    "context_length_exceeded",
    "context_too_large",
    "context_window_exceeded",
    "model_context_window_exceeded",
    "request_too_large",
    "context length exceeded",
    "maximum context length",
    "context window",
    "prompt is too long",
    "prompt too long",
    "too many tokens",
    "model token limit",
    "input exceeds the context window",
    "input exceeds the maximum number of tokens",
    "input is too long",
    "request size exceeds model context window",
)
_TOKEN_GT_RE = re.compile(r"(\d[\d,]*)\s*tokens?\s*>\s*(\d[\d,]*)", re.IGNORECASE)


def is_context_length_code_or_message(code: Any = None, message: Any = None) -> bool:
    """Return True when an upstream error means the prompt exceeds context.

    This intentionally lives in the protocol error module (rather than the
    failover runtime) so translators can normalize terminal SSE/WS events
    before they become downstream Anthropic errors.
    """
    code_s = str(code or "").strip().lower()
    msg_s = str(message or "").strip().lower()
    if code_s in _CONTEXT_LENGTH_ERROR_CODES:
        return True
    text = f"{code_s} {msg_s}"
    if not text.strip():
        return False
    if ("rate_limit" in text or "rate limit" in text) and "context" not in text:
        return False
    return any(marker in text for marker in _CONTEXT_LENGTH_MESSAGE_MARKERS)


def context_length_error_message_for_claude_code(
    message: Any = None,
    *,
    actual_tokens: int | None = None,
    max_tokens: int | None = None,
) -> str:
    """Format context overflow so Claude Code triggers reactive compact.

    Old Claude Code does not recover from OpenAI-style
    ``context_length_exceeded`` alone. Its recovery path looks for the human
    phrase ``Prompt is too long`` in the Anthropic SDK error message, and can
    optionally parse ``N tokens > M``. Preserve the upstream detail after the
    prefix so humans and logs still see the original provider reason.
    """
    detail = str(message or "").strip()
    if "prompt is too long" in detail.lower():
        return detail

    if actual_tokens is not None and max_tokens is not None:
        prefix = f"Prompt is too long: {int(actual_tokens)} tokens > {int(max_tokens)} maximum."
    elif actual_tokens is not None:
        prefix = f"Prompt is too long: {int(actual_tokens)} tokens exceed the model context window."
    else:
        m = _TOKEN_GT_RE.search(detail)
        if m:
            actual = m.group(1).replace(",", "")
            limit = m.group(2).replace(",", "")
            prefix = f"Prompt is too long: {actual} tokens > {limit} maximum."
        else:
            prefix = "Prompt is too long:"

    if not detail:
        detail = "input exceeds the model context window."
    if detail.lower().startswith("prompt is too long"):
        return detail
    return f"{prefix} {detail}"


def responses_max_output_context_error_message(reason: str | None = None) -> str:
    reason = str(reason or "max_output_tokens")
    detail = (
        f"{CONTEXT_LENGTH_EXCEEDED_CODE}: upstream Responses ended incomplete "
        f"because incomplete_details.reason={reason}; reduce input context or reserved output tokens."
    )
    return context_length_error_message_for_claude_code(detail)


def _format_error_info(code: Any, message: Any, fallback: str) -> tuple[str | None, str]:
    code_s = str(code) if code else None
    msg_s = str(message or fallback)
    if code_s and code_s not in msg_s:
        msg_s = f"{code_s}: {msg_s}"
    if is_context_length_code_or_message(code_s, msg_s):
        msg_s = context_length_error_message_for_claude_code(msg_s)
        code_s = code_s or CONTEXT_LENGTH_EXCEEDED_CODE
    return code_s, msg_s


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
    if is_responses_max_output_incomplete(payload):
        return (
            CONTEXT_LENGTH_EXCEEDED_CODE,
            responses_max_output_context_error_message(responses_incomplete_reason(payload)),
        )

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
        return _format_error_info(code, message, fallback)

    if isinstance(payload, dict):
        top_type = payload.get("type")
        code = (
            payload.get("code")
            or payload.get("error_type")
            or (top_type if top_type != "error" else None)
            or payload.get("status")
        )
        message = payload.get("message") or payload.get("reason") or fallback
        return _format_error_info(code, message, fallback)

    return None, fallback
