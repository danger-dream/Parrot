"""Typed Cursor/Connect errors and retry classification."""

from __future__ import annotations

from typing import Any, Literal

RetryHint = Literal["blob_not_found", "resource_exhausted", "timeout", "unavailable"]


class CursorError(RuntimeError):
    """Base error surfaced to callers and the HTTP sidecar."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "upstream_error",
        status: int = 502,
        retry_hint: RetryHint | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.retry_hint = retry_hint
        self.retryable = retryable

    @property
    def error_type(self) -> str:
        if self.status == 401:
            return "authentication_error"
        if self.status == 429:
            return "rate_limit_error"
        if self.status == 400:
            return "invalid_request_error"
        return "server_error"

    def to_openai_error(self) -> dict[str, Any]:
        return {
            "error": {
                "message": str(self),
                "type": self.error_type,
                "code": self.code,
            }
        }


class CursorAuthError(CursorError):
    def __init__(self, message: str = "unauthorized") -> None:
        super().__init__(message, code="authentication_error", status=401)


class CursorRateLimitError(CursorError):
    def __init__(self, message: str = "resource exhausted") -> None:
        super().__init__(
            message,
            code="rate_limit_exceeded",
            status=429,
            retry_hint="resource_exhausted",
            retryable=True,
        )


class CursorOverloadError(CursorError):
    def __init__(self, message: str = "upstream unavailable") -> None:
        super().__init__(
            message,
            code="server_overloaded",
            status=503,
            retry_hint="unavailable",
            retryable=True,
        )


class CursorTimeoutError(CursorError):
    def __init__(self, message: str = "timeout") -> None:
        super().__init__(
            message,
            code="timeout",
            status=504,
            retry_hint="timeout",
            retryable=True,
        )


class CursorProtocolError(CursorError):
    pass


class CursorToolActivityError(CursorError):
    def __init__(self, message: str = "Unexpected tool activity while collecting a non-streaming response") -> None:
        super().__init__(message, code="unexpected_tool_activity", status=502)


def classify_cursor_failure(message: str, *, http_status: int | None = None) -> CursorError:
    """Map a Connect/H2/HTTP failure to a typed error with retry hint."""

    text = (message or "").strip() or "cursor error"
    lower = text.lower()

    if http_status in {401, 403} or any(
        token in lower
        for token in (
            "unauthenticated",
            "unauthorized",
            "permission_denied",
            "invalid token",
            "jwt expired",
            "authentication",
        )
    ):
        return CursorAuthError(text)

    if http_status == 429 or any(
        token in lower
        for token in (
            "resource_exhausted",
            "rate limit",
            "rate_limit",
            "too many requests",
            "quota exceeded",
        )
    ):
        return CursorRateLimitError(text)

    if "blob not found" in lower:
        return CursorProtocolError(
            text,
            code="blob_not_found",
            status=502,
            retry_hint="blob_not_found",
            retryable=True,
        )

    if http_status in {408, 504} or any(
        token in lower
        for token in (
            "inactivity timeout",
            "deadline_exceeded",
            "deadline exceeded",
            "timed out",
            "timeout",
        )
    ):
        return CursorTimeoutError(text)

    if http_status in {502, 503} or any(
        token in lower
        for token in (
            "unavailable",
            "overloaded",
            "overload",
            "http2 stream reset",
            "bridge connection lost",
            "connection reset",
            "connection lost",
            "goaway",
        )
    ):
        return CursorOverloadError(text)

    if http_status == 400 or "invalid_argument" in lower or "invalid chat completion" in lower:
        return CursorProtocolError(text, code="invalid_request", status=400)

    if http_status == 404 or "not_found" in lower:
        return CursorProtocolError(text, code="not_found", status=404)

    return CursorProtocolError(text)
