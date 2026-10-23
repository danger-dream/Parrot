"""Path-scoped browser Origin policy that is stricter than inference CORS."""

from __future__ import annotations

import re
import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src.management_control import ManagementError, ManagementErrorCode

from .error_mapping import error_response, is_management_path
from .origin_policy import origin_allowed
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_ALLOWED_METHODS = {"GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"}
_ALLOWED_HEADERS = {
    "authorization",
    "content-type",
    "idempotency-key",
    "if-match",
    "x-request-id",
}
_ALLOWED_METHODS_HEADER = b"GET, POST, PATCH, PUT, DELETE, OPTIONS"
_ALLOWED_HEADERS_HEADER = b"Authorization, Content-Type, Idempotency-Key, If-Match, X-Request-Id"


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers") or ():
        if key.lower() == name:
            return value.decode("latin-1")
    return None


def _request_id(scope: Scope) -> str:
    state = scope.setdefault("state", {})
    supplied = _header(scope, b"x-request-id") or ""
    request_id = supplied if _REQUEST_ID_RE.fullmatch(supplied) else str(uuid.uuid4())
    state["management_request_id"] = request_id
    return request_id


class ManagementOriginMiddleware:
    """Reject unapproved browser origins before broad application CORS can answer."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not is_management_path(str(scope.get("path") or "")):
            await self.app(scope, receive, send)
            return
        request_id = _request_id(scope)
        origin = _header(scope, b"origin")
        allowed = frozenset()
        app = scope.get("app")
        runtime = getattr(getattr(app, "state", None), "management_runtime", None)
        if runtime is not None:
            allowed = frozenset(getattr(runtime, "allowed_origins", ()))
        if origin is not None and not origin_allowed(
            origin,
            allowed=allowed,
            host=_header(scope, b"host"),
            forwarded_host=_header(scope, b"x-forwarded-host"),
        ):
            response = error_response(
                ManagementError(ManagementErrorCode.ORIGIN_DENIED),
                request_id=request_id,
            )
            await response(scope, receive, send)
            return
        if origin is not None and scope.get("method", "").upper() == "OPTIONS":
            requested_method = (_header(scope, b"access-control-request-method") or "").upper()
            requested_headers = {
                item.strip().lower()
                for item in (_header(scope, b"access-control-request-headers") or "").split(",")
                if item.strip()
            }
            if requested_method not in _ALLOWED_METHODS or not requested_headers <= _ALLOWED_HEADERS:
                response = error_response(
                    ManagementError(ManagementErrorCode.ORIGIN_DENIED),
                    request_id=request_id,
                )
                await response(scope, receive, send)
                return
            headers = [
                (b"access-control-allow-origin", origin.encode("latin-1")),
                (b"access-control-allow-methods", _ALLOWED_METHODS_HEADER),
                (b"access-control-allow-headers", _ALLOWED_HEADERS_HEADER),
                (b"access-control-max-age", b"600"),
                (b"vary", b"Origin"),
                (b"cache-control", b"no-store"),
                (b"x-request-id", request_id.encode("ascii")),
            ]
            await send({"type": "http.response.start", "status": 204, "headers": headers})
            await send({"type": "http.response.body", "body": b""})
            return

        async def secure_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (key, value)
                    for key, value in message.get("headers") or []
                    if key.lower()
                    not in {
                        b"access-control-allow-origin",
                        b"access-control-allow-credentials",
                        b"x-request-id",
                    }
                ]
                headers.append((b"x-request-id", request_id.encode("ascii")))
                if origin is not None:
                    headers.extend(
                        [
                            (b"access-control-allow-origin", origin.encode("latin-1")),
                            (b"vary", b"Origin"),
                        ]
                    )
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, secure_send)
