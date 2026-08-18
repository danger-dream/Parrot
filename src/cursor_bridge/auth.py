"""Cursor browser PKCE login and token refresh."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import uuid
from dataclasses import dataclass

import httpx

from .constants import CURSOR_LOGIN_URL, CURSOR_POLL_URL, CURSOR_REFRESH_URL
from .h2stream import cursor_http_headers


@dataclass(frozen=True)
class CursorAuthParams:
    verifier: str
    challenge: str
    uuid: str
    login_url: str


@dataclass(frozen=True)
class CursorTokens:
    access_token: str
    refresh_token: str
    expires_at_ms: int


class CursorAuthPending(RuntimeError):
    """Browser login has not completed for this PKCE session yet."""


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def generate_pkce() -> tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def generate_auth_params() -> CursorAuthParams:
    verifier, challenge = generate_pkce()
    login_uuid = str(uuid.uuid4())
    query = httpx.QueryParams(
        {"challenge": challenge, "uuid": login_uuid, "mode": "login", "redirectTarget": "cli"}
    )
    return CursorAuthParams(
        verifier=verifier,
        challenge=challenge,
        uuid=login_uuid,
        login_url=f"{CURSOR_LOGIN_URL}?{query}",
    )


def token_expiry_ms(token: str, now_ms: int | None = None) -> int:
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    try:
        parts = token.split(".")
        if len(parts) != 3 or not parts[1]:
            return now + 3600 * 1000
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        exp = payload.get("exp")
        if isinstance(exp, (int, float)):
            return int(exp * 1000) - 5 * 60 * 1000
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        pass
    return now + 3600 * 1000


def poll_cursor_auth_once(
    login_uuid: str,
    verifier: str,
    *,
    client: httpx.Client | None = None,
) -> CursorTokens:
    """Poll once; Telegram's “已登录” button owns retry pacing."""
    own_client = client is None
    http = client or httpx.Client(timeout=15.0)
    try:
        response = http.get(
            CURSOR_POLL_URL,
            params={"uuid": login_uuid, "verifier": verifier},
            headers=cursor_http_headers("", content_type=None),
        )
        if response.status_code == 404:
            raise CursorAuthPending("Cursor browser login is still pending")
        response.raise_for_status()
        data = response.json()
        access = data["accessToken"]
        refresh = data["refreshToken"]
        return CursorTokens(access, refresh, token_expiry_ms(access))
    finally:
        if own_client:
            http.close()


def poll_cursor_auth(
    login_uuid: str,
    verifier: str,
    *,
    timeout_s: float = 180.0,
    client: httpx.Client | None = None,
) -> CursorTokens:
    own_client = client is None
    http = client or httpx.Client(timeout=15.0)
    delay = 1.0
    errors = 0
    deadline = time.monotonic() + timeout_s
    try:
        while time.monotonic() < deadline:
            time.sleep(delay)
            try:
                return poll_cursor_auth_once(login_uuid, verifier, client=http)
            except CursorAuthPending:
                errors = 0
                delay = min(delay * 1.2, 10.0)
                continue
            except httpx.HTTPError:
                errors += 1
                if errors >= 3:
                    raise
        raise TimeoutError("Cursor authentication polling timeout")
    finally:
        if own_client:
            http.close()


def refresh_cursor_token(refresh_token: str, *, client: httpx.Client | None = None) -> CursorTokens:
    own_client = client is None
    http = client or httpx.Client(timeout=15.0)
    try:
        response = http.post(
            CURSOR_REFRESH_URL,
            headers=cursor_http_headers(refresh_token),
            content=b"{}",
        )
        if not response.is_success:
            raise RuntimeError(f"Cursor token refresh failed: {response.text}")
        data = response.json()
        access = data["accessToken"]
        refresh = data.get("refreshToken") or refresh_token
        return CursorTokens(access, refresh, token_expiry_ms(access))
    finally:
        if own_client:
            http.close()
