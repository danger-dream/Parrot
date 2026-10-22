"""The browser session cookie (``sessionTransport=cookie``)."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import Request, Response

from .dependencies import SESSION_COOKIE_NAME, SESSION_COOKIE_PATH
from .origin_policy import https_request


def _secure(request: Request) -> bool:
    return https_request(request.url.scheme, request.headers.get("x-forwarded-proto"))


def set_session_cookie(
    request: Request, response: Response, credential: str, expires_at: datetime,
) -> None:
    """HttpOnly, SameSite=Strict, Path=/api/management; Secure over https.

    Max-Age is the remaining absolute session lifetime in whole seconds.
    """
    remaining = int((expires_at - datetime.now(timezone.utc)).total_seconds())
    response.set_cookie(
        SESSION_COOKIE_NAME,
        credential,
        max_age=max(0, remaining),
        path=SESSION_COOKIE_PATH,
        secure=_secure(request),
        httponly=True,
        samesite="strict",
    )


def clear_session_cookie(request: Request, response: Response) -> None:
    response.delete_cookie(
        SESSION_COOKIE_NAME,
        path=SESSION_COOKIE_PATH,
        secure=_secure(request),
        httponly=True,
        samesite="strict",
    )


def has_session_cookie(request: Request) -> bool:
    return SESSION_COOKIE_NAME in request.cookies
