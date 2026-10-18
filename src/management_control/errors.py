"""Stable transport-neutral errors for management controls."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable
import sys

from src import safe_diagnostics


class ManagementErrorCode(str, Enum):
    INVALID_REQUEST = "INVALID_REQUEST"
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
    INVALID_OPERATION_STATE = "INVALID_OPERATION_STATE"
    SESSION_REQUIRED = "SESSION_REQUIRED"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
    CAPABILITY_DENIED = "CAPABILITY_DENIED"
    ORIGIN_DENIED = "ORIGIN_DENIED"
    RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"
    OPERATION_NOT_FOUND = "OPERATION_NOT_FOUND"
    RESOURCE_CONFLICT = "RESOURCE_CONFLICT"
    IDENTITY_CONFLICT = "IDENTITY_CONFLICT"
    REVISION_CONFLICT = "REVISION_CONFLICT"
    STATE_CONFLICT = "STATE_CONFLICT"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    UNSUPPORTED_VALUE = "UNSUPPORTED_VALUE"
    RATE_LIMITED = "RATE_LIMITED"
    OPERATION_ALREADY_RUNNING = "OPERATION_ALREADY_RUNNING"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    SERVICE_NOT_READY = "SERVICE_NOT_READY"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    UPSTREAM_TIMEOUT = "UPSTREAM_TIMEOUT"


_DEFAULT_MESSAGES: dict[ManagementErrorCode, str] = {
    ManagementErrorCode.AUTHENTICATION_FAILED: "Authentication failed",
    ManagementErrorCode.CAPABILITY_DENIED: "Required management capability is not granted",
    ManagementErrorCode.DEPENDENCY_UNAVAILABLE: "A required dependency is unavailable",
    ManagementErrorCode.INVALID_OPERATION_STATE: "The operation is not in a valid state",
    ManagementErrorCode.INVALID_REQUEST: "The request is invalid",
    ManagementErrorCode.OPERATION_ALREADY_RUNNING: "Operation capacity is currently full",
    ManagementErrorCode.OPERATION_NOT_FOUND: "Management operation was not found",
    ManagementErrorCode.ORIGIN_DENIED: "The browser origin is not allowed",
    ManagementErrorCode.RATE_LIMITED: "Too many authentication attempts",
    ManagementErrorCode.RESOURCE_NOT_FOUND: "The requested resource was not found",
    ManagementErrorCode.SERVICE_NOT_READY: "Management service is not ready",
    ManagementErrorCode.SESSION_EXPIRED: "Management session has expired",
    ManagementErrorCode.SESSION_REQUIRED: "A valid management session is required",
    ManagementErrorCode.STATE_CONFLICT: "The resource state has changed",
    ManagementErrorCode.VALIDATION_FAILED: "Request validation failed",
}


@dataclass(frozen=True, slots=True)
class ErrorField:
    path: str
    code: str
    message: str


class ManagementError(Exception):
    """Public control failure whose code, fields and retryability are stable."""

    def __init__(
        self,
        code: ManagementErrorCode | str,
        message: str | None = None,
        *,
        fields: Iterable[ErrorField] = (),
        retryable: bool = False,
        operation_id: str | None = None,
    ) -> None:
        try:
            stable_code = code if isinstance(code, ManagementErrorCode) else ManagementErrorCode(code)
        except ValueError as exc:
            raise ValueError(f"unknown management error code: {code!r}") from exc
        self.code = stable_code
        self.message = message or _DEFAULT_MESSAGES.get(stable_code, stable_code.value)
        cause = sys.exception()
        if cause is not None and stable_code.value in safe_diagnostics._GENERIC_CODES:
            diagnostic = safe_diagnostics.report(cause)
            self._safe_diagnostic = diagnostic
            self.message = safe_diagnostics.message(diagnostic) + safe_diagnostics.guidance(diagnostic)
        self.fields = tuple(fields)
        self.retryable = bool(retryable)
        self.operation_id = operation_id
        super().__init__(self.message)
