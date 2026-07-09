"""xAI / Grok OAuth provider.

This module mirrors the OAuth flow implemented by CLIProxyAPI's native xAI
support:
  - issuer/discovery: https://auth.x.ai/.well-known/openid-configuration
  - public client id: b1a00492-073a-47ea-816f-4c329264a828
  - scopes: openid profile email offline_access grok-cli:access api:access
  - PKCE: base64url_no_pad(96 random bytes), S256 challenge
  - token exchange/refresh: application/x-www-form-urlencoded
  - API base URL: https://api.x.ai/v1

Defaults can be overridden via config.xaiOAuth (issuer/discoveryUrl/clientId/scope/redirectUri/apiBaseUrl).

The module is intentionally provider-only: routing and request conversion live in
``src.channel.xai_oauth_channel``.  Tests can enable mock mode via
``oauth.mockMode`` or ``DISABLE_OAUTH_NETWORK_CALLS=1`` to avoid real OAuth
network calls.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import time
from typing import Any
from urllib.parse import urlencode, urlparse

from .. import network


ISSUER = "https://auth.x.ai"
DISCOVERY_URL = ISSUER + "/.well-known/openid-configuration"
AUTHORIZE_URL = ISSUER + "/oauth/authorize"
TOKEN_URL = ISSUER + "/oauth/token"
CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
SCOPES = "openid profile email offline_access grok-cli:access api:access"
REDIRECT_URI = "http://127.0.0.1:56121/callback"
DEFAULT_API_BASE_URL = "https://api.x.ai/v1"

_TOKEN_HTTP_TIMEOUT = 120.0
_DISCOVERY_TIMEOUT = 15.0
_USER_AGENT = "parrot/xai-oauth-adapter"


def _mock_mode_enabled() -> bool:
    if os.environ.get("DISABLE_OAUTH_NETWORK_CALLS") == "1":
        return True
    from .. import config  # 延迟导入，避免循环依赖
    return bool(config.get().get("oauth", {}).get("mockMode", False))


def _xai_cfg() -> dict[str, Any]:
    try:
        from .. import config  # 延迟导入，避免循环依赖
        cfg = config.get().get("xaiOAuth") or {}
    except Exception:
        return {}
    return cfg if isinstance(cfg, dict) else {}


def _cfg_str(*keys: str, default: str) -> str:
    cfg = _xai_cfg()
    for key in keys:
        value = cfg.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return default


def _issuer() -> str:
    return _cfg_str("issuer", default=ISSUER).rstrip("/")


def _discovery_url() -> str:
    return _cfg_str(
        "discoveryUrl", "discovery_url",
        default=_issuer() + "/.well-known/openid-configuration",
    )


def _authorization_url() -> str:
    return _cfg_str(
        "authorizationEndpoint", "authorizationUrl", "authorizeUrl",
        default=_issuer() + "/oauth/authorize",
    )


def _token_url() -> str:
    return _cfg_str("tokenEndpoint", "tokenUrl", default=_issuer() + "/oauth/token")


def _client_id() -> str:
    return _cfg_str("clientId", "client_id", default=CLIENT_ID)


def _scopes() -> str:
    cfg = _xai_cfg()
    value = cfg.get("scope", cfg.get("scopes"))
    if isinstance(value, list):
        joined = " ".join(str(x).strip() for x in value if str(x).strip())
        if joined:
            return joined
    if isinstance(value, str) and value.strip():
        return value.strip()
    return SCOPES


def _redirect_uri() -> str:
    return _cfg_str("redirectUri", "redirect_uri", default=REDIRECT_URI)


def api_base_url() -> str:
    return _cfg_str("apiBaseUrl", "api_base_url", "baseUrl", default=DEFAULT_API_BASE_URL).rstrip("/")


def authorization_url() -> str:
    return _authorization_url()


def token_url() -> str:
    return _token_url()


def redirect_uri() -> str:
    return _redirect_uri()


def client_id() -> str:
    return _client_id()


def scopes() -> str:
    return _scopes()


def _b64_json(obj: dict[str, Any]) -> str:
    raw = json.dumps(obj, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _mock_id_token(email: str | None = None, *, subject: str | None = None) -> str:
    if not email:
        email = f"mock-xai-{secrets.token_hex(4)}@local"
    if not subject:
        subject = f"mock-xai-sub-{secrets.token_hex(6)}"
    now = int(time.time())
    return ".".join([
        _b64_json({"alg": "none", "typ": "JWT"}),
        _b64_json({
            "iss": _issuer(),
            "aud": _client_id(),
            "sub": subject,
            "email": email,
            "email_verified": True,
            "iat": now,
            "exp": now + 3600,
        }),
        "",
    ])


def _mock_token_response(email: str | None = None, *, subject: str | None = None) -> dict:
    id_token = _mock_id_token(email, subject=subject)
    info = extract_user_info(decode_id_token(id_token))
    return {
        "access_token": "mock-xai-access-" + secrets.token_hex(8),
        "refresh_token": "mock-xai-refresh-" + secrets.token_hex(8),
        "id_token": id_token,
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": _scopes(),
        "email": info.get("email", ""),
        "subject": info.get("subject", ""),
        "sub": info.get("subject", ""),
        "base_url": api_base_url(),
        "token_endpoint": _token_url(),
        "redirect_uri": _redirect_uri(),
    }


# ─── OAuth endpoint / discovery ──────────────────────────────────


def validate_oauth_endpoint(raw_url: str, field: str = "endpoint") -> str:
    raw = str(raw_url or "").strip()
    if not raw:
        raise ValueError(f"xAI discovery {field} is empty")
    parsed = urlparse(raw)
    if parsed.scheme != "https":
        raise ValueError(f"xAI discovery {field} must use https: {raw!r}")
    host = (parsed.hostname or "").lower().strip()
    if host != "x.ai" and not host.endswith(".x.ai"):
        raise ValueError(f"xAI discovery {field} host {host!r} is not on x.ai")
    return raw


def discover_sync() -> dict:
    """Resolve xAI OAuth endpoints through OIDC discovery.

    Mock mode returns the documented default endpoints without network access.
    """
    if _mock_mode_enabled():
        return {"authorization_endpoint": _authorization_url(), "token_endpoint": _token_url()}
    resp = network.get_sync(
        _discovery_url(),
        headers={"accept": "application/json", "user-agent": _USER_AGENT},
        timeout=_DISCOVERY_TIMEOUT,
        proxy_purpose="oauth_xai",
    )
    resp.raise_for_status()
    data = resp.json()
    auth = validate_oauth_endpoint(data.get("authorization_endpoint", ""), "authorization_endpoint")
    token = validate_oauth_endpoint(data.get("token_endpoint", ""), "token_endpoint")
    return {"authorization_endpoint": auth, "token_endpoint": token}


async def discover() -> dict:
    return await asyncio.to_thread(discover_sync)


# ─── PKCE / authorize URL ────────────────────────────────────────


def pkce_generate() -> tuple[str, str]:
    """Return ``(code_verifier, code_challenge)`` for xAI OAuth.

    CLIProxyAPI uses 96 random bytes for the verifier before base64url-no-pad,
    which produces a 128 character verifier.
    """
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(96)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def build_login_url(
    code_challenge: str,
    state: str,
    *,
    nonce: str | None = None,
    redirect_uri: str | None = None,
    authorization_endpoint: str | None = None,
) -> str:
    endpoint = validate_oauth_endpoint(
        authorization_endpoint or _authorization_url(),
        "authorization_endpoint",
    )
    nonce = nonce or secrets.token_urlsafe(32)
    params = {
        "response_type": "code",
        "client_id": _client_id(),
        "redirect_uri": redirect_uri or _redirect_uri(),
        "scope": _scopes(),
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
        "nonce": nonce,
        "plan": "generic",
        "referrer": "cli-proxy-api",
    }
    return f"{endpoint}?{urlencode(params)}"


# ─── token exchange / refresh ────────────────────────────────────


def _post_token_form(token_endpoint: str, data: dict) -> dict:
    resp = network.post_sync(
        validate_oauth_endpoint(token_endpoint or _token_url(), "token_endpoint"),
        data=data,
        headers={
            "content-type": "application/x-www-form-urlencoded",
            "accept": "application/json",
            "user-agent": _USER_AGENT,
        },
        timeout=_TOKEN_HTTP_TIMEOUT,
        proxy_purpose="oauth_xai",
    )
    resp.raise_for_status()
    return resp.json()


def _attach_identity_fields(data: dict, *, token_endpoint: str | None = None,
                            redirect_uri: str | None = None,
                            email: str | None = None,
                            subject: str | None = None) -> dict:
    out = dict(data or {})
    if out.get("id_token"):
        try:
            info = extract_user_info(decode_id_token(out["id_token"]))
            if info.get("email"):
                out["email"] = info["email"]
            if info.get("subject"):
                out["subject"] = info["subject"]
                out["sub"] = info["subject"]
        except Exception:
            pass
    if email and not out.get("email"):
        out["email"] = email
    if subject and not out.get("subject"):
        out["subject"] = subject
        out["sub"] = subject
    out.setdefault("base_url", api_base_url())
    out.setdefault("token_endpoint", token_endpoint or _token_url())
    out.setdefault("redirect_uri", redirect_uri or _redirect_uri())
    return out


def exchange_code_sync(
    code: str,
    code_verifier: str,
    *,
    redirect_uri: str | None = None,
    token_endpoint: str | None = None,
) -> dict:
    if _mock_mode_enabled():
        return _mock_token_response()
    endpoint = token_endpoint or _token_url()
    data = _post_token_form(endpoint, {
        "grant_type": "authorization_code",
        "code": str(code or "").strip(),
        "redirect_uri": redirect_uri or _redirect_uri(),
        "client_id": _client_id(),
        "code_verifier": code_verifier,
    })
    return _attach_identity_fields(
        data, token_endpoint=endpoint, redirect_uri=redirect_uri or _redirect_uri(),
    )


async def exchange_code(
    code: str,
    code_verifier: str,
    *,
    redirect_uri: str | None = None,
    token_endpoint: str | None = None,
) -> dict:
    return await asyncio.to_thread(
        exchange_code_sync,
        code,
        code_verifier,
        redirect_uri=redirect_uri,
        token_endpoint=token_endpoint,
    )


def refresh_sync(
    refresh_token: str,
    *,
    token_endpoint: str | None = None,
    email: str | None = None,
    subject: str | None = None,
) -> dict:
    if _mock_mode_enabled():
        return _mock_token_response(email, subject=subject)
    endpoint = token_endpoint or _token_url()
    data = _post_token_form(endpoint, {
        "grant_type": "refresh_token",
        "client_id": _client_id(),
        "refresh_token": refresh_token,
    })
    return _attach_identity_fields(
        data, token_endpoint=endpoint, email=email, subject=subject,
    )


async def refresh(
    refresh_token: str,
    *,
    token_endpoint: str | None = None,
    email: str | None = None,
    subject: str | None = None,
) -> dict:
    return await asyncio.to_thread(
        refresh_sync,
        refresh_token,
        token_endpoint=token_endpoint,
        email=email,
        subject=subject,
    )


# ─── id_token helpers ────────────────────────────────────────────


class IDTokenError(ValueError):
    pass


def decode_id_token(id_token: str, *, verify_exp: bool = False,
                    skew_seconds: int = 120) -> dict:
    if not id_token or id_token.count(".") < 2:
        raise IDTokenError(f"invalid JWT: got {id_token!r}")
    parts = id_token.split(".")
    if len(parts) != 3:
        raise IDTokenError(f"invalid JWT: expected 3 parts, got {len(parts)}")
    payload_b64 = parts[1]
    padding = (-len(payload_b64)) % 4
    if padding:
        payload_b64 += "=" * padding
    try:
        raw = base64.urlsafe_b64decode(payload_b64)
    except Exception as exc:
        raise IDTokenError(f"decode base64: {exc}") from exc
    try:
        claims = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise IDTokenError(f"parse JSON: {exc}") from exc
    if verify_exp:
        exp = claims.get("exp")
        if isinstance(exp, int) and exp > 0 and time.time() > exp + skew_seconds:
            raise IDTokenError(f"id_token expired (exp={exp})")
    return claims


def extract_user_info(id_token_claims: dict) -> dict:
    return {
        "email": str(id_token_claims.get("email") or ""),
        "subject": str(id_token_claims.get("sub") or ""),
        "email_verified": bool(id_token_claims.get("email_verified")),
    }


def empty_usage() -> dict:
    """Return an Anthropic-shaped usage payload with xAI marked unsupported.

    xAI OAuth currently has no confirmed zero-cost quota endpoint equivalent to
    Claude `/oauth/usage` or OpenAI/Codex WHAM.  Returning no quota signal keeps
    Parrot from auto-disabling or auto-resuming accounts based on guessed data.
    """
    return {
        "five_hour": {},
        "seven_day": {},
        "seven_day_sonnet": {},
        "seven_day_opus": {},
        "extra_usage": {"is_enabled": False},
        "xai": {"source": "unsupported", "quota_supported": False},
    }
