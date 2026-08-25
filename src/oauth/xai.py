"""xAI / Grok OAuth provider.

This module implements the xAI public OAuth flow:
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
from concurrent.futures import ThreadPoolExecutor, as_completed
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
DEFAULT_CLI_PROXY_BASE_URL = "https://cli-chat-proxy.grok.com/v1"
DEFAULT_CLI_CLIENT_VERSION = "0.2.93"

_TOKEN_HTTP_TIMEOUT = 120.0
_DISCOVERY_TIMEOUT = 15.0
_CLI_HTTP_TIMEOUT = 15.0
_CLI_OPTIONAL_HTTP_TIMEOUT = 8.0
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


def cli_proxy_base_url() -> str:
    return _cfg_str(
        "cliProxyBaseUrl", "cli_proxy_base_url",
        default=DEFAULT_CLI_PROXY_BASE_URL,
    ).rstrip("/")


def cli_client_version() -> str:
    return _cfg_str(
        "cliClientVersion", "cli_client_version",
        default=DEFAULT_CLI_CLIENT_VERSION,
    )


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

    Use 96 random bytes before base64url-no-pad, producing a 128-character
    verifier within the PKCE length limit.
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
        "referrer": "parrot",
    }
    return f"{endpoint}?{urlencode(params)}"


# ─── token exchange / refresh ────────────────────────────────────


def _post_token_form(token_endpoint: str, data: dict, *, proxy_channel: str = "") -> dict:
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
        proxy_channel=proxy_channel,
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
    account_key: str = "",
) -> dict:
    if _mock_mode_enabled():
        return _mock_token_response(email, subject=subject)
    endpoint = token_endpoint or _token_url()
    data = _post_token_form(endpoint, {
        "grant_type": "refresh_token",
        "client_id": _client_id(),
        "refresh_token": refresh_token,
    }, proxy_channel=f"oauth:{account_key}" if account_key else "")
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


# ─── Grok CLI billing / usage helpers ───────────────────────────


def _validate_cli_proxy_base_url(raw_url: str) -> str:
    raw = str(raw_url or "").strip().rstrip("/")
    if not raw:
        raise ValueError("xAI CLI proxy base URL is empty")
    parsed = urlparse(raw)
    if parsed.scheme != "https":
        raise ValueError(f"xAI CLI proxy base URL must use https: {raw!r}")
    host = (parsed.hostname or "").lower().strip()
    if host != "cli-chat-proxy.grok.com":
        raise ValueError(f"xAI CLI proxy base URL host {host!r} is not cli-chat-proxy.grok.com")
    return raw


def _cli_proxy_url(path_and_query: str) -> str:
    path = str(path_and_query or "").strip()
    if not path.startswith("/"):
        path = "/" + path
    return _validate_cli_proxy_base_url(cli_proxy_base_url()) + path


def _num_val(obj: Any) -> float | None:
    if isinstance(obj, dict):
        obj = obj.get("val")
    if obj is None:
        return None
    try:
        return float(obj)
    except (TypeError, ValueError):
        return None


def _clean_num(v: float | None) -> int | float | None:
    if v is None:
        return None
    if abs(v - int(v)) < 1e-9:
        return int(v)
    return v


def _cli_get_json_sync(access_token: str, path_and_query: str,
                       *, timeout: float | None = None,
                       account_key: str = "") -> dict:
    request_kwargs = {
        "headers": {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "User-Agent": f"grok-cli/{cli_client_version()}",
            "x-grok-client-version": cli_client_version(),
        },
        "timeout": _CLI_HTTP_TIMEOUT if timeout is None else timeout,
        "proxy_purpose": "oauth_xai",
    }
    if account_key:
        request_kwargs["proxy_channel"] = f"oauth:{account_key}"
    resp = network.get_sync(_cli_proxy_url(path_and_query), **request_kwargs)
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, dict) else {}


def _optional_cli_get_json_sync(access_token: str, path_and_query: str,
                                *, account_key: str = "") -> tuple[dict | None, str | None]:
    try:
        if account_key:
            return _cli_get_json_sync(
                access_token, path_and_query, timeout=_CLI_OPTIONAL_HTTP_TIMEOUT,
                account_key=account_key,
            ), None
        return _cli_get_json_sync(
            access_token, path_and_query, timeout=_CLI_OPTIONAL_HTTP_TIMEOUT,
        ), None
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _history_items(raw_history: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_history, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw_history:
        if not isinstance(item, dict):
            continue
        cyc = item.get("billingCycle") or item.get("billing_cycle") or {}
        included = _clean_num(_num_val(item.get("includedUsed") or item.get("included_used")))
        ondemand = _clean_num(_num_val(item.get("onDemandUsed") or item.get("on_demand_used")))
        total = _clean_num(_num_val(item.get("totalUsed") or item.get("total_used")))
        out.append({
            "year": cyc.get("year") if isinstance(cyc, dict) else None,
            "month": cyc.get("month") if isinstance(cyc, dict) else None,
            "included_used": included,
            "on_demand_used": ondemand,
            "total_used": total,
        })
    return out


def _mock_cli_billing_usage() -> dict:
    usage = empty_usage()
    usage["xai"] = {
        "source": "mock",
        "quota_supported": True,
        "billing": {
            "period_type": None,
            "used_percent": None,
            "remaining_percent": None,
            "period_start": None,
            "period_end": None,
        },
        "user": {"has_grok_code_access": True, "subscription_tier": "Mock"},
        "settings": {"allow_access": True, "subscription_tier_display": "Mock"},
    }
    return usage


def fetch_cli_billing_usage_sync(access_token: str, *, account_key: str = "") -> dict:
    """Fetch Grok CLI official billing/subscription snapshot.

    The subscription quota signal comes from ``/v1/billing?format=credits``.
    ``config.creditUsagePercent`` and ``config.currentPeriod`` are normalized
    under ``xai.billing``; a weekly period is also exposed as the shared
    ``seven_day`` window for quota monitoring.  Deprecated ``monthlyLimit`` /
    ``used`` fields from auto-topup are not treated as subscription quota.

    A successful credits payload that includes the current period but omits
    ``creditUsagePercent`` means unused quota (0% used / 100% remaining).
    Credits fetch failures must not be interpreted as 0%.
    """
    if _mock_mode_enabled():
        return _mock_cli_billing_usage()

    if account_key:
        auto = _cli_get_json_sync(
            access_token, "/billing?format=auto-topup", account_key=account_key,
        )
    else:
        auto = _cli_get_json_sync(access_token, "/billing?format=auto-topup")
    optional_paths = {
        "credits": "/billing?format=credits",
        "user": "/user?include=subscription",
        "settings": "/settings",
    }
    optional_results: dict[str, tuple[dict | None, str | None]] = {}
    with ThreadPoolExecutor(max_workers=len(optional_paths)) as pool:
        futures = {
            (
                pool.submit(
                    _optional_cli_get_json_sync, access_token, path,
                    account_key=account_key,
                )
                if account_key else
                pool.submit(_optional_cli_get_json_sync, access_token, path)
            ): name
            for name, path in optional_paths.items()
        }
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                optional_results[name] = fut.result()
            except Exception as exc:
                optional_results[name] = (None, f"{type(exc).__name__}: {exc}")

    credits, credits_err = optional_results.get("credits", (None, "not fetched"))
    user, user_err = optional_results.get("user", (None, "not fetched"))
    settings, settings_err = optional_results.get("settings", (None, "not fetched"))

    auto_cfg = auto.get("config") if isinstance(auto.get("config"), dict) else {}
    credits_cfg = credits.get("config") if isinstance(credits, dict) and isinstance(credits.get("config"), dict) else {}

    # auto-topup has separate payment-cap semantics.  Its legacy monthlyLimit /
    # used fields are intentionally not used as the subscription quota signal.
    auto_top_up_limit = _num_val(auto_cfg.get("monthlyLimit") or auto_cfg.get("monthly_limit"))
    auto_top_up_used = _num_val(
        auto_cfg.get("used")
        or auto_cfg.get("totalUsed")
        or auto_cfg.get("total_used")
        or auto_cfg.get("includedUsed")
        or auto_cfg.get("included_used")
    )

    current_period = credits_cfg.get("currentPeriod") if isinstance(credits_cfg.get("currentPeriod"), dict) else {}
    period_type = str(current_period.get("type") or "").strip() or None
    period_start = current_period.get("start")
    period_end = current_period.get("end")
    used_percent = _num_val(
        credits_cfg.get("creditUsagePercent")
        if credits_cfg.get("creditUsagePercent") is not None
        else credits_cfg.get("credit_usage_percent")
    )
    if used_percent is not None:
        used_percent = max(0.0, min(100.0, used_percent))
    elif credits_err is None and current_period:
        # Successful credits response with a billing window but no usage
        # fields: the pool is unused, not unknown.
        used_percent = 0.0
    remaining_percent = None if used_percent is None else 100.0 - used_percent

    on_demand_cap = _num_val(auto_cfg.get("onDemandCap") or auto_cfg.get("on_demand_cap"))
    if on_demand_cap is None:
        on_demand_cap = _num_val(credits_cfg.get("onDemandCap") or credits_cfg.get("on_demand_cap"))
    on_demand_used = _num_val(credits_cfg.get("onDemandUsed") or credits_cfg.get("on_demand_used"))
    prepaid_balance = _num_val(credits_cfg.get("prepaidBalance") or credits_cfg.get("prepaid_balance"))

    user = user if isinstance(user, dict) else {}
    settings = settings if isinstance(settings, dict) else {}
    team_blocked = user.get("teamBlockedReasons") if isinstance(user.get("teamBlockedReasons"), list) else []
    blocked_reason = user.get("userBlockedReason")

    xai: dict[str, Any] = {
        "source": "cli-chat-proxy",
        "quota_supported": True,
        "billing": {
            "period_type": period_type,
            "used_percent": used_percent,
            "remaining_percent": remaining_percent,
            "period_start": period_start,
            "period_end": period_end,
            "on_demand_cap": _clean_num(on_demand_cap),
            "on_demand_used": _clean_num(on_demand_used),
            "prepaid_balance": _clean_num(prepaid_balance),
            "is_unified_billing_user": credits_cfg.get("isUnifiedBillingUser"),
            "top_up_method": credits_cfg.get("topUpMethod"),
            "auto_top_up": {
                "monthly_limit": _clean_num(auto_top_up_limit),
                "used": _clean_num(auto_top_up_used),
            },
            "history": _history_items(auto_cfg.get("history")),
        },
        "user": {
            "user_id": user.get("userId"),
            "principal_type": user.get("principalType"),
            "principal_id": user.get("principalId"),
            "has_grok_code_access": user.get("hasGrokCodeAccess"),
            "subscription_tier": user.get("subscriptionTier"),
            "blocked": bool(blocked_reason or team_blocked),
            "user_blocked_reason": blocked_reason,
            "team_blocked_reasons": team_blocked,
        },
        "settings": {
            "allow_access": settings.get("allow_access"),
            "subscription_tier_display": settings.get("subscription_tier_display"),
            "default_model": settings.get("default_model"),
            "on_demand_enabled": settings.get("on_demand_enabled"),
            "usage_billing_redirect_url": settings.get("usage_billing_redirect_url"),
            "compaction_mode": settings.get("compaction_mode"),
            "flush_soft_threshold_tokens": settings.get("flush_soft_threshold_tokens"),
        },
    }
    errors = {
        k: v for k, v in {
            "credits": credits_err,
            "user": user_err,
            "settings": settings_err,
        }.items() if v
    }
    if errors:
        xai["errors"] = errors

    weekly = {}
    if period_type == "USAGE_PERIOD_TYPE_WEEKLY" and used_percent is not None:
        weekly = {"utilization": used_percent, "resets_at": period_end}

    return {
        "five_hour": {},
        "seven_day": weekly,
        "seven_day_sonnet": {},
        "seven_day_opus": {},
        "openai": {"thirty_day": {}},
        "extra_usage": {"is_enabled": bool(on_demand_cap and on_demand_cap > 0)},
        "xai": xai,
    }


async def fetch_cli_billing_usage(access_token: str, *, account_key: str = "") -> dict:
    return await asyncio.to_thread(
        fetch_cli_billing_usage_sync, access_token, account_key=account_key,
    )


def empty_usage() -> dict:
    """Return an Anthropic-shaped usage payload with xAI marked unsupported.

    Kept for backward compatibility and tests.  Runtime xAI billing now uses
    ``fetch_cli_billing_usage``; this empty shape is still useful when callers
    intentionally need a no-signal placeholder.
    """
    return {
        "five_hour": {},
        "seven_day": {},
        "seven_day_sonnet": {},
        "seven_day_opus": {},
        "extra_usage": {"is_enabled": False},
        "xai": {"source": "unsupported", "quota_supported": False},
    }
