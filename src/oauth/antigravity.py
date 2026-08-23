"""Antigravity / Google Code Assist OAuth provider.

Aligned with CPA ``internal/auth/antigravity`` (v7.2.140):

  - Google authorization-code flow, **no PKCE**
  - authorize: https://accounts.google.com/o/oauth2/v2/auth
  - token:     https://oauth2.googleapis.com/token
  - userinfo:  https://www.googleapis.com/oauth2/v2/userinfo?alt=json
  - project:   POST cloudcode-pa.googleapis.com/v1internal:loadCodeAssist
  - onboard:   POST daily-cloudcode-pa.googleapis.com/v1internal:onboardUser
  - credits:   same loadCodeAssist, ``paidTier.availableCredits`` / ``GOOGLE_ONE_AI``

Routing and Gemini↔Responses conversion live in
``src.channel.antigravity_oauth_channel`` and the Antigravity adapter.
Tests can enable mock mode via ``oauth.mockMode`` or
``DISABLE_OAUTH_NETWORK_CALLS=1``.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import time
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from .. import network


CLIENT_ID = "1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com"
CLIENT_SECRET = "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"
CALLBACK_PORT = 51121
REDIRECT_URI = f"http://localhost:{CALLBACK_PORT}/oauth-callback"
SCOPES = (
    "https://www.googleapis.com/auth/cloud-platform "
    "https://www.googleapis.com/auth/userinfo.email "
    "https://www.googleapis.com/auth/userinfo.profile "
    "https://www.googleapis.com/auth/cclog "
    "https://www.googleapis.com/auth/experimentsandconfigs"
)
AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
USERINFO_ENDPOINT = "https://www.googleapis.com/oauth2/v2/userinfo?alt=json"
API_ENDPOINT = "https://cloudcode-pa.googleapis.com"
DAILY_API_ENDPOINT = "https://daily-cloudcode-pa.googleapis.com"
API_VERSION = "v1internal"

# CPA floor is 2.9.1: Cloud Code rejects newer models below 2.9.0.
# Impersonate the official hub family UA, including CPA's hub platform.
DEFAULT_HUB_VERSION = "2.9.1"
DEFAULT_HUB_PLATFORM = "darwin/arm64"
DEFAULT_USER_AGENT = f"antigravity/hub/{DEFAULT_HUB_VERSION} {DEFAULT_HUB_PLATFORM}"
NODE_API_CLIENT_UA = "google-api-nodejs-client/10.3.0"
GOOG_API_CLIENT_UA = "gl-node/22.21.1"
CREDIT_TYPE = "GOOGLE_ONE_AI"

_TOKEN_HTTP_TIMEOUT = 120.0
_API_HTTP_TIMEOUT = 30.0
_ONBOARD_ATTEMPTS = 5
_ONBOARD_POLL_SECONDS = 2.0

DEFAULT_TEXT_MODELS = (
    "gemini-3.7-flash-high",
    "gemini-3.6-flash-high",
    "gemini-3-flash",
    "gemini-3-flash-agent",
    "gemini-pro-agent",
    "gemini-3.1-pro-low",
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash-low",
    "gemini-3.5-flash-extra-low",
    "claude-opus-4-6-thinking",
    "claude-sonnet-4-6",
    "gpt-oss-120b-medium",
)
DEFAULT_IMAGE_MODELS = (
    "gemini-3.1-flash-image",
)


def _mock_mode_enabled() -> bool:
    if os.environ.get("DISABLE_OAUTH_NETWORK_CALLS") == "1":
        return True
    from .. import config
    return bool(config.get().get("oauth", {}).get("mockMode", False))


def _ag_cfg() -> dict[str, Any]:
    try:
        from .. import config
        cfg = config.get().get("antigravityOAuth") or {}
    except Exception:
        return {}
    return cfg if isinstance(cfg, dict) else {}


def _cfg_str(*keys: str, default: str) -> str:
    cfg = _ag_cfg()
    for key in keys:
        value = cfg.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return default


def client_id() -> str:
    return _cfg_str("clientId", "client_id", default=CLIENT_ID)


def client_secret() -> str:
    return _cfg_str("clientSecret", "client_secret", default=CLIENT_SECRET)


def redirect_uri() -> str:
    return _cfg_str("redirectUri", "redirect_uri", default=REDIRECT_URI)


def scopes() -> str:
    cfg = _ag_cfg()
    value = cfg.get("scope", cfg.get("scopes"))
    if isinstance(value, list):
        joined = " ".join(str(x).strip() for x in value if str(x).strip())
        if joined:
            return joined
    if isinstance(value, str) and value.strip():
        return value.strip()
    return SCOPES


def auth_url() -> str:
    return _cfg_str("authorizationEndpoint", "authorizationUrl", "authorizeUrl",
                    default=AUTH_ENDPOINT)


def token_url() -> str:
    return _cfg_str("tokenEndpoint", "tokenUrl", default=TOKEN_ENDPOINT)


def userinfo_url() -> str:
    return _cfg_str("userinfoEndpoint", "userinfoUrl", default=USERINFO_ENDPOINT)


def api_base_url() -> str:
    return _cfg_str("apiBaseUrl", "api_base_url", "baseUrl", default=API_ENDPOINT).rstrip("/")


def daily_api_base_url() -> str:
    return _cfg_str(
        "dailyApiBaseUrl", "daily_api_base_url",
        default=DAILY_API_ENDPOINT,
    ).rstrip("/")


def request_user_agent() -> str:
    return _cfg_str("userAgent", "user_agent", default=DEFAULT_USER_AGENT)


def onboard_user_agent() -> str:
    configured = _cfg_str("onboardUserAgent", "onboard_user_agent", default="")
    if configured:
        return configured
    base = request_user_agent()
    if "google-api-nodejs-client/" in base.lower():
        return base
    return f"{base} {NODE_API_CLIENT_UA}"


def goog_api_client() -> str:
    return _cfg_str("googApiClient", "goog_api_client", default=GOOG_API_CLIENT_UA)


def default_models() -> list[str]:
    cfg = _ag_cfg()
    value = cfg.get("defaultModels")
    if isinstance(value, list):
        models = [str(x).strip() for x in value if str(x).strip()]
        if models:
            return models
    return list(DEFAULT_TEXT_MODELS)


def image_models() -> list[str]:
    cfg = _ag_cfg()
    value = cfg.get("imageModels")
    if isinstance(value, list):
        models = [str(x).strip() for x in value if str(x).strip()]
        if models:
            return models
    return list(DEFAULT_IMAGE_MODELS)


def request_api_base_url() -> str:
    """CPA generateContent default: daily first, prod reserved for loadCodeAssist."""
    return daily_api_base_url()


def generate_content_url() -> str:
    return f"{request_api_base_url()}/{API_VERSION}:generateContent"


def stream_generate_content_url() -> str:
    return f"{request_api_base_url()}/{API_VERSION}:streamGenerateContent?alt=sse"


def load_code_assist_url() -> str:
    return f"{api_base_url()}/{API_VERSION}:loadCodeAssist"


def onboard_user_url() -> str:
    return f"{daily_api_base_url()}/{API_VERSION}:onboardUser"


# ─── authorize / callback ───────────────────────────────────────


def build_login_url(
    state: str,
    *,
    redirect_uri: str | None = None,
    authorization_endpoint: str | None = None,
) -> str:
    endpoint = (authorization_endpoint or auth_url()).strip()
    params = {
        "access_type": "offline",
        "client_id": client_id(),
        "prompt": "consent",
        "redirect_uri": redirect_uri or redirect_uri_or_default(),
        "response_type": "code",
        "scope": scopes(),
        "state": state,
    }
    return f"{endpoint}?{urlencode(params)}"


def redirect_uri_or_default(value: str | None = None) -> str:
    raw = str(value or "").strip()
    return raw or redirect_uri()


def parse_callback_url(raw: str) -> dict[str, str]:
    """Extract ``code`` / ``state`` / ``error`` from a pasted localhost callback URL."""
    text = str(raw or "").strip()
    if not text:
        raise ValueError("empty Antigravity callback URL")
    parsed = urlparse(text)
    query = parse_qs(parsed.query, keep_blank_values=False)
    if not query and parsed.fragment:
        query = parse_qs(parsed.fragment, keep_blank_values=False)
    out = {
        "code": (query.get("code") or [""])[0].strip(),
        "state": (query.get("state") or [""])[0].strip(),
        "error": (query.get("error") or [""])[0].strip(),
        "error_description": (query.get("error_description") or [""])[0].strip(),
    }
    if out["error"]:
        detail = out["error_description"] or out["error"]
        raise ValueError(f"Antigravity OAuth denied: {detail}")
    if not out["code"]:
        raise ValueError("Antigravity callback URL is missing code")
    return out


# ─── token exchange / refresh ───────────────────────────────────


def _post_token_form(token_endpoint: str, data: dict) -> dict:
    resp = network.post_sync(
        token_endpoint,
        data=data,
        headers={
            "content-type": "application/x-www-form-urlencoded",
            "accept": "application/json",
            "user-agent": request_user_agent(),
        },
        timeout=_TOKEN_HTTP_TIMEOUT,
        proxy_purpose="oauth_antigravity",
    )
    resp.raise_for_status()
    payload = resp.json()
    return payload if isinstance(payload, dict) else {}


def _attach_identity_fields(
    data: dict,
    *,
    token_endpoint: str | None = None,
    redirect_uri: str | None = None,
    email: str | None = None,
    project_id: str | None = None,
) -> dict:
    out = dict(data or {})
    if email and not out.get("email"):
        out["email"] = email
    if project_id and not out.get("project_id"):
        out["project_id"] = project_id
    out.setdefault("base_url", api_base_url())
    out.setdefault("token_endpoint", token_endpoint or token_url())
    out.setdefault("redirect_uri", redirect_uri_or_default(redirect_uri))
    return out


def _mock_token_response(
    email: str | None = None,
    *,
    project_id: str | None = None,
) -> dict:
    return _attach_identity_fields(
        {
            "access_token": "mock-antigravity-access-" + secrets.token_hex(8),
            "refresh_token": "mock-antigravity-refresh-" + secrets.token_hex(8),
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": scopes(),
            "email": email or f"mock-ag-{secrets.token_hex(4)}@gmail.com",
            "project_id": project_id or f"mock-project-{secrets.token_hex(4)}",
        },
    )


def exchange_code_sync(
    code: str,
    *,
    redirect_uri: str | None = None,
    token_endpoint: str | None = None,
) -> dict:
    if _mock_mode_enabled():
        return _mock_token_response()
    endpoint = token_endpoint or token_url()
    data = _post_token_form(endpoint, {
        "grant_type": "authorization_code",
        "code": str(code or "").strip(),
        "redirect_uri": redirect_uri_or_default(redirect_uri),
        "client_id": client_id(),
        "client_secret": client_secret(),
    })
    return _attach_identity_fields(
        data,
        token_endpoint=endpoint,
        redirect_uri=redirect_uri_or_default(redirect_uri),
    )


async def exchange_code(
    code: str,
    *,
    redirect_uri: str | None = None,
    token_endpoint: str | None = None,
) -> dict:
    return await asyncio.to_thread(
        exchange_code_sync,
        code,
        redirect_uri=redirect_uri,
        token_endpoint=token_endpoint,
    )


def refresh_sync(
    refresh_token: str,
    *,
    token_endpoint: str | None = None,
    email: str | None = None,
    project_id: str | None = None,
) -> dict:
    if _mock_mode_enabled():
        return _mock_token_response(email, project_id=project_id)
    endpoint = token_endpoint or token_url()
    data = _post_token_form(endpoint, {
        "grant_type": "refresh_token",
        "client_id": client_id(),
        "client_secret": client_secret(),
        "refresh_token": refresh_token,
    })
    return _attach_identity_fields(
        data,
        token_endpoint=endpoint,
        email=email,
        project_id=project_id,
    )


async def refresh(
    refresh_token: str,
    *,
    token_endpoint: str | None = None,
    email: str | None = None,
    project_id: str | None = None,
) -> dict:
    return await asyncio.to_thread(
        refresh_sync,
        refresh_token,
        token_endpoint=token_endpoint,
        email=email,
        project_id=project_id,
    )


# ─── userinfo / project discovery ───────────────────────────────


def extract_project_id(data: Any) -> str:
    """CPA ``extractCloudaicompanionProject``: cloudaicompanionProject / projectId / project."""
    if not isinstance(data, dict):
        return ""
    for key in ("cloudaicompanionProject", "projectId", "project"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            nested = value.get("id")
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
    return ""


def default_tier_id(load_resp: dict | None) -> str:
    data = load_resp if isinstance(load_resp, dict) else {}
    tiers = data.get("allowedTiers")
    if isinstance(tiers, list):
        for raw in tiers:
            if not isinstance(raw, dict):
                continue
            if raw.get("isDefault") is True:
                tid = str(raw.get("id") or "").strip()
                if tid:
                    return tid
    current = data.get("currentTier")
    if isinstance(current, dict):
        tid = str(current.get("id") or "").strip()
        if tid:
            return tid
    return "free-tier"


def _auth_headers(access_token: str, *, user_agent: str | None = None,
                  extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "*/*",
        "Content-Type": "application/json",
        "User-Agent": user_agent or request_user_agent(),
    }
    if extra:
        headers.update(extra)
    return headers


def fetch_userinfo_sync(access_token: str) -> dict:
    if _mock_mode_enabled():
        return {"email": f"mock-ag-{secrets.token_hex(4)}@gmail.com"}
    token = str(access_token or "").strip()
    if not token:
        raise ValueError("antigravity userinfo: missing access token")
    resp = network.get_sync(
        userinfo_url(),
        headers=_auth_headers(token),
        timeout=_API_HTTP_TIMEOUT,
        proxy_purpose="oauth_antigravity",
    )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise ValueError("antigravity userinfo: unexpected response")
    email = str(data.get("email") or "").strip()
    if not email:
        raise ValueError("antigravity userinfo: response missing email")
    return {"email": email, "id": data.get("id"), "name": data.get("name")}


async def fetch_userinfo(access_token: str) -> dict:
    return await asyncio.to_thread(fetch_userinfo_sync, access_token)


def load_code_assist_sync(access_token: str) -> dict:
    if _mock_mode_enabled():
        return {
            "cloudaicompanionProject": f"mock-project-{secrets.token_hex(4)}",
            "paidTier": {
                "id": "mock-tier",
                "availableCredits": [{
                    "creditType": CREDIT_TYPE,
                    "creditAmount": "100",
                    "minimumCreditAmountForUsage": "1",
                }],
            },
        }
    token = str(access_token or "").strip()
    if not token:
        raise ValueError("antigravity loadCodeAssist: missing access token")
    resp = network.post_sync(
        load_code_assist_url(),
        json={"metadata": {"ideType": "ANTIGRAVITY"}},
        headers=_auth_headers(token),
        timeout=_API_HTTP_TIMEOUT,
        proxy_purpose="oauth_antigravity",
    )
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, dict) else {}


async def load_code_assist(access_token: str) -> dict:
    return await asyncio.to_thread(load_code_assist_sync, access_token)


def onboard_user_sync(access_token: str, tier_id: str | None = None) -> str:
    if _mock_mode_enabled():
        return f"mock-project-{secrets.token_hex(4)}"
    token = str(access_token or "").strip()
    if not token:
        raise ValueError("antigravity onboardUser: missing access token")
    body = {
        "tier_id": (tier_id or "free-tier").strip() or "free-tier",
        "metadata": {
            "ide_type": "ANTIGRAVITY",
            "ide_version": DEFAULT_HUB_VERSION,
            "ide_name": "antigravity",
        },
    }
    last_error = "onboard user did not complete"
    for _attempt in range(1, _ONBOARD_ATTEMPTS + 1):
        resp = network.post_sync(
            onboard_user_url(),
            json=body,
            headers=_auth_headers(
                token,
                user_agent=onboard_user_agent(),
                extra={"X-Goog-Api-Client": goog_api_client()},
            ),
            timeout=_API_HTTP_TIMEOUT,
            proxy_purpose="oauth_antigravity",
        )
        if resp.status_code != 200:
            preview = (resp.text or "").strip()[:200]
            raise RuntimeError(f"antigravity onboardUser http {resp.status_code}: {preview}")
        data = resp.json() if resp.content else {}
        if isinstance(data, dict) and data.get("done") is True:
            nested = data.get("response") if isinstance(data.get("response"), dict) else data
            project_id = extract_project_id(nested)
            if project_id:
                return project_id
            raise RuntimeError("antigravity onboardUser: no project_id in response")
        last_error = "antigravity onboardUser not done"
        time.sleep(_ONBOARD_POLL_SECONDS)
    raise RuntimeError(f"{last_error} after {_ONBOARD_ATTEMPTS} attempts")


async def onboard_user(access_token: str, tier_id: str | None = None) -> str:
    return await asyncio.to_thread(onboard_user_sync, access_token, tier_id)


def fetch_project_id_sync(access_token: str) -> str:
    load_resp = load_code_assist_sync(access_token)
    project_id = extract_project_id(load_resp)
    if project_id:
        return project_id
    project_id = onboard_user_sync(access_token, default_tier_id(load_resp))
    if not project_id:
        raise RuntimeError("antigravity: project id not found in loadCodeAssist or onboardUser")
    return project_id


async def fetch_project_id(access_token: str) -> str:
    return await asyncio.to_thread(fetch_project_id_sync, access_token)


def complete_login_sync(
    code: str,
    *,
    redirect_uri: str | None = None,
    token_endpoint: str | None = None,
) -> dict:
    """Exchange code, resolve email + project_id. Fail-closed without project_id."""
    tokens = exchange_code_sync(
        code, redirect_uri=redirect_uri, token_endpoint=token_endpoint,
    )
    access_token = str(tokens.get("access_token") or "").strip()
    if not access_token:
        raise RuntimeError("antigravity login: token response missing access_token")
    if _mock_mode_enabled() and tokens.get("email") and tokens.get("project_id"):
        return tokens
    info = fetch_userinfo_sync(access_token)
    email = str(info.get("email") or "").strip()
    if not email:
        raise RuntimeError("antigravity login: userinfo missing email")
    project_id = fetch_project_id_sync(access_token)
    if not project_id:
        raise RuntimeError("antigravity login: missing project_id")
    return _attach_identity_fields(
        tokens,
        token_endpoint=token_endpoint or token_url(),
        redirect_uri=redirect_uri,
        email=email,
        project_id=project_id,
    )


async def complete_login(
    code: str,
    *,
    redirect_uri: str | None = None,
    token_endpoint: str | None = None,
) -> dict:
    return await asyncio.to_thread(
        complete_login_sync,
        code,
        redirect_uri=redirect_uri,
        token_endpoint=token_endpoint,
    )


# ─── credits / usage ────────────────────────────────────────────


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def parse_credits(load_resp: Any) -> dict[str, Any]:
    """Parse loadCodeAssist credits.

    Plan rule (deliberately stricter than CPA): a missing/non-array
    ``availableCredits`` is *unknown*, not ``available=false``.
    """
    if not isinstance(load_resp, dict):
        return {"known": False, "quota_supported": True, "source": "loadCodeAssist"}
    paid = load_resp.get("paidTier")
    if not isinstance(paid, dict):
        paid = {}
    current_tier = load_resp.get("currentTier")
    current_tier_id = ""
    if isinstance(current_tier, dict):
        current_tier_id = str(current_tier.get("id") or "").strip()
    tier = str(paid.get("id") or current_tier_id or "").strip()
    credits = paid.get("availableCredits")
    if not isinstance(credits, list):
        return {
            "known": False,
            "quota_supported": True,
            "source": "loadCodeAssist",
            "tier": tier or None,
        }
    for item in credits:
        if not isinstance(item, dict):
            continue
        if str(item.get("creditType") or "").strip().upper() != CREDIT_TYPE:
            continue
        amount = _num(item.get("creditAmount"))
        minimum = _num(item.get("minimumCreditAmountForUsage"))
        if amount is None or minimum is None:
            continue
        return {
            "known": True,
            "quota_supported": True,
            "source": "loadCodeAssist",
            "tier": tier or None,
            "credit_type": CREDIT_TYPE,
            "credit_amount": amount,
            "minimum_credit_amount": minimum,
            "available": amount >= minimum,
        }
    return {
        "known": False,
        "quota_supported": True,
        "source": "loadCodeAssist",
        "tier": tier or None,
    }


def empty_usage() -> dict:
    return {
        "five_hour": {},
        "seven_day": {},
        "seven_day_sonnet": {},
        "seven_day_opus": {},
        "extra_usage": {"is_enabled": False},
        "antigravity": {
            "source": "unsupported",
            "quota_supported": True,
            "known": False,
        },
    }


def _usage_from_credits(credits: dict) -> dict:
    usage = empty_usage()
    usage["antigravity"] = {
        "source": credits.get("source") or "loadCodeAssist",
        "quota_supported": True,
        "known": bool(credits.get("known")),
        "tier": credits.get("tier"),
        "credit_type": credits.get("credit_type") or CREDIT_TYPE,
        "credit_amount": credits.get("credit_amount"),
        "minimum_credit_amount": credits.get("minimum_credit_amount"),
        "available": credits.get("available"),
    }
    return usage


def fetch_usage_sync(access_token: str) -> dict:
    if _mock_mode_enabled():
        return _usage_from_credits({
            "known": True,
            "source": "mock",
            "tier": "Mock",
            "credit_type": CREDIT_TYPE,
            "credit_amount": 100.0,
            "minimum_credit_amount": 1.0,
            "available": True,
        })
    load_resp = load_code_assist_sync(access_token)
    return _usage_from_credits(parse_credits(load_resp))


async def fetch_usage(access_token: str) -> dict:
    return await asyncio.to_thread(fetch_usage_sync, access_token)
