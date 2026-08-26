"""Cursor account OAuth, model discovery, and quota normalization.

Cursor's browser flow is a PKCE login URL plus polling endpoint rather than a
normal callback pasted into Telegram.  The inference transport itself is the
reverse-engineered Cursor AgentService bridge in :mod:`src.cursor_bridge`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from .. import config, network
from ..cursor_bridge.auth import (
    CursorAuthParams,
    CursorAuthPending,
    CursorTokens,
    generate_auth_params,
    poll_cursor_auth_once,
    refresh_cursor_token,
    token_expiry_ms,
)
from ..cursor_bridge.catalog import build_catalog
from ..cursor_bridge.constants import (
    CURSOR_CLIENT_VERSION,
    CURSOR_WEB_PROFILE_URL,
)
from ..cursor_bridge.errors import (
    CursorAuthError,
    CursorError,
    CursorTimeoutError,
    classify_cursor_failure,
)
from ..cursor_bridge.models import CursorModel, list_cursor_models
from ..cursor_bridge.usage import CursorUsage, fetch_cursor_usage


def _mock_mode_enabled() -> bool:
    return bool(config.get().get("oauth", {}).get("mockMode", False))


def _http_client(*, account_key: str = "", timeout: float = 20.0) -> httpx.Client:
    return network.sync_client(
        timeout=timeout,
        proxy_purpose="oauth_cursor",
        proxy_channel=f"oauth:{account_key}" if account_key else "",
        http2=True,
    )


def generate_login() -> CursorAuthParams:
    return generate_auth_params()


def poll_login_once(login_uuid: str, verifier: str) -> CursorTokens:
    if _mock_mode_enabled():
        now = int(time.time() * 1000)
        return CursorTokens(
            access_token=_mock_jwt("cursor-mock-user", now + 3600_000),
            refresh_token="mock-cursor-refresh",
            expires_at_ms=now + 3300_000,
        )
    with _http_client(timeout=15.0) as client:
        return poll_cursor_auth_once(login_uuid, verifier, client=client)


def refresh_sync(refresh_token: str, *, account_key: str = "", **_kwargs) -> dict[str, Any]:
    if _mock_mode_enabled():
        now = int(time.time() * 1000)
        return {
            "access_token": _mock_jwt("cursor-mock-user", now + 3600_000),
            "refresh_token": refresh_token or "mock-cursor-refresh",
            "expires_in": 3300,
        }
    with _http_client(account_key=account_key, timeout=20.0) as client:
        tokens = refresh_cursor_token(refresh_token, client=client)
    expires_in = max(60, int((tokens.expires_at_ms - int(time.time() * 1000)) / 1000))
    return {
        "access_token": tokens.access_token,
        "refresh_token": tokens.refresh_token or refresh_token,
        "expires_in": expires_in,
    }


def decode_access_claims(token: str) -> dict[str, Any]:
    try:
        parts = str(token or "").split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def subject_from_access_token(token: str) -> str:
    return str(decode_access_claims(token).get("sub") or "").strip()


def account_label(subject: str) -> str:
    digest = hashlib.sha256(str(subject or "cursor").encode("utf-8")).hexdigest()[:10]
    return f"cursor-{digest}@local"


def web_session_cookie(access_token: str) -> str:
    """Build Cursor's first-party Web session cookie from a CLI access token."""
    subject = subject_from_access_token(access_token)
    user_id = subject.rsplit("|", 1)[-1].strip() if subject else ""
    if not user_id:
        raise CursorAuthError("Cursor access token 缺少 Web session user id")
    return f"WorkosCursorSessionToken={user_id}%3A%3A{access_token}"


def _profile_text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_profile(raw: dict[str, Any]) -> dict[str, Any]:
    profile = {
        "id": _profile_text(raw.get("id") or raw.get("userId")),
        "sub": _profile_text(raw.get("sub")),
        "email": _profile_text(raw.get("email")),
        "name": _profile_text(raw.get("name")),
        "email_verified": (
            raw.get("email_verified")
            if isinstance(raw.get("email_verified"), bool)
            else None
        ),
    }
    if not profile["id"] and not profile["email"]:
        raise CursorError("Cursor 账号资料缺少稳定用户 ID 和邮箱", code="profile_invalid", status=502)
    return profile


def fetch_profile_sync(
    access_token: str, *, account_key: str = "", timeout: float = 20.0,
) -> dict[str, Any]:
    """Fetch Cursor Web account identity (email/name) using the CLI OAuth token."""
    if _mock_mode_enabled():
        subject = subject_from_access_token(access_token) or "cursor-mock-user"
        return {
            "id": subject,
            "sub": subject,
            "email": "cursor-mock@example.test",
            "name": "Cursor Mock",
            "email_verified": True,
        }

    headers = {
        "Accept": "application/json",
        "Cookie": web_session_cookie(access_token),
        "User-Agent": f"cursor-agent/{CURSOR_CLIENT_VERSION.removeprefix('cli-')}",
    }
    try:
        with _http_client(account_key=account_key, timeout=max(0.001, float(timeout))) as client:
            response = client.get(CURSOR_WEB_PROFILE_URL, headers=headers)
    except httpx.TimeoutException as exc:
        raise CursorTimeoutError("Cursor 账号资料请求超时") from exc
    except httpx.HTTPError as exc:
        raise classify_cursor_failure("Cursor 账号资料请求失败") from exc
    if response.status_code in {401, 403}:
        raise CursorAuthError(f"Cursor 账号资料未授权 ({response.status_code})")
    if not response.is_success:
        raise classify_cursor_failure(
            f"Cursor 账号资料接口返回 HTTP {response.status_code}",
            http_status=response.status_code,
        )
    try:
        raw = response.json()
    except ValueError as exc:
        raise CursorError(
            "Cursor 账号资料不是有效 JSON", code="profile_invalid", status=502,
        ) from exc
    if not isinstance(raw, dict):
        raise CursorError("Cursor 账号资料格式无效", code="profile_invalid", status=502)
    return _normalize_profile(raw)


def expiry_iso(token: str) -> str:
    # token_expiry_ms already includes the bridge's five-minute safety margin.
    return datetime.fromtimestamp(token_expiry_ms(token) / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _mock_jwt(subject: str, expires_at_ms: int) -> str:
    def enc(value: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(
            json.dumps(value, separators=(",", ":")).encode("utf-8")
        ).decode("ascii").rstrip("=")
    return f"{enc({'alg': 'none'})}.{enc({'sub': subject, 'exp': expires_at_ms // 1000})}.mock"


def _mock_models() -> list[CursorModel]:
    return [
        CursorModel(
            id="claude-fable-5",
            name="Claude Fable 5",
            reasoning=True,
            context_window=300_000,
            context_window_max_mode=1_000_000,
            max_tokens=64_000,
            supports_images=True,
            supports_max_mode=True,
            supports_agent=True,
            legacy_slugs=(
                "claude-fable-5-low",
                "claude-fable-5-medium",
                "claude-fable-5-thinking-medium",
                "claude-fable-5-thinking-high",
                "claude-fable-5-thinking-max",
            ),
            default_on=True,
        ),
        CursorModel(
            id="composer-2.5",
            name="Composer 2.5",
            reasoning=True,
            context_window=200_000,
            max_tokens=64_000,
            supports_images=False,
            supports_max_mode=True,
            supports_agent=True,
            legacy_slugs=("composer-2.5-fast",),
            default_on=True,
        ),
    ]


def fetch_models_sync(access_token: str, *, timeout: float | None = None,
                      account_key: str = "") -> list[CursorModel]:
    if _mock_mode_enabled():
        return _mock_models()
    return list_cursor_models(
        access_token,
        include_hidden=False,
        use_model_parameters=True,
        timeout_s=timeout,
        account_key=account_key,
        channel_key=f"oauth:{account_key}" if account_key else "",
    )


def fetch_model_catalog_sync(access_token: str, *, timeout: float | None = None,
                             account_key: str = "") -> dict[str, Any]:
    return build_catalog(fetch_models_sync(
        access_token, timeout=timeout, account_key=account_key,
    ))


def _normalize_iso(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    try:
        numeric = float(text)
        if numeric > 10_000_000_000:
            numeric /= 1000.0
        if numeric > 1_000_000_000:
            return datetime.fromtimestamp(numeric, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OSError, OverflowError):
        pass
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_usage(usage: CursorUsage | dict[str, Any]) -> dict[str, Any]:
    raw = usage.to_dict() if isinstance(usage, CursorUsage) else dict(usage)
    plan = raw.get("plan_usage") if isinstance(raw.get("plan_usage"), dict) else {}
    spend = raw.get("spend_limit") if isinstance(raw.get("spend_limit"), dict) else {}

    total_cents = _number(plan.get("total_spend_cents"))
    limit_cents = _number(plan.get("limit_cents"))
    remaining_cents = _number(plan.get("remaining_cents"))
    if total_cents is None and limit_cents is not None and remaining_cents is not None:
        total_cents = max(0.0, limit_cents - remaining_cents)
    total_util = (
        max(0.0, min(100.0, total_cents / limit_cents * 100.0))
        if total_cents is not None and limit_cents and limit_cents > 0 else None
    )
    auto_util = _number(plan.get("auto_percent_used"))
    api_util = _number(plan.get("api_percent_used"))
    cycle_start = _normalize_iso(raw.get("billing_cycle_start"))
    cycle_end = _normalize_iso(raw.get("billing_cycle_end"))

    individual_limit = _number(spend.get("individual_limit_cents"))
    individual_remaining = _number(spend.get("individual_remaining_cents"))
    extra_used = (
        max(0.0, individual_limit - individual_remaining)
        if individual_limit is not None and individual_remaining is not None else 0.0
    )
    extra_util = (
        max(0.0, min(100.0, extra_used / individual_limit * 100.0))
        if individual_limit and individual_limit > 0 else None
    )

    cursor = {
        "source": "cursor_dashboard",
        "quota_supported": True,
        "membership_type": raw.get("membership_type"),
        "individual_membership_type": raw.get("individual_membership_type"),
        "subscription_status": raw.get("subscription_status"),
        "plan_name": raw.get("plan_name"),
        "included_amount_cents": raw.get("included_amount_cents"),
        "billing_cycle_start": cycle_start,
        "billing_cycle_end": cycle_end,
        "total_spend_cents": total_cents,
        "included_spend_cents": plan.get("included_spend_cents"),
        "remaining_cents": remaining_cents,
        "limit_cents": limit_cents,
        "total_utilization": total_util,
        "auto_percent_used": auto_util,
        "api_percent_used": api_util,
        # Preserve Cursor's own field for display/debug only.  It is not used for
        # disable decisions because upstream has emitted inconsistent values.
        "reported_total_percent_used": plan.get("total_percent_used"),
        "display_message": raw.get("display_message"),
        "spend_limit": {
            "limit_cents": individual_limit,
            "remaining_cents": individual_remaining,
            "limit_type": spend.get("limit_type"),
        },
        "sources": list(raw.get("sources") or []),
        "models": list(raw.get("models") or []),
        "auto_bucket_models": list(raw.get("auto_bucket_models") or []),
    }
    # Existing quota cache has a generic monthly slot under openai.thirty_day.
    # Cursor-specific evaluation bypasses whole-account disable and uses the
    # detailed ``cursor`` block to cool only the affected model pool.
    return {
        "five_hour": {},
        "seven_day": {},
        "openai": {
            "thirty_day": {
                "utilization": total_util,
                "resets_at": cycle_end,
            }
        },
        "extra_usage": {
            "is_enabled": bool(individual_limit and individual_limit > 0),
            "used_credits": int(extra_used),
            "monthly_limit": int(individual_limit or 0),
            "utilization": extra_util,
        },
        "cursor": cursor,
    }


def _mock_usage() -> dict[str, Any]:
    end = datetime.fromtimestamp(time.time() + 30 * 86400, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "five_hour": {},
        "seven_day": {},
        "openai": {"thirty_day": {"utilization": 1.0, "resets_at": end}},
        "extra_usage": {"is_enabled": False, "used_credits": 0, "monthly_limit": 0, "utilization": None},
        "cursor": {
            "source": "mock",
            "quota_supported": True,
            "plan_name": "Mock",
            "subscription_status": "active",
            "billing_cycle_end": end,
            "total_spend_cents": 100,
            "remaining_cents": 9900,
            "limit_cents": 10000,
            "total_utilization": 1.0,
            "auto_percent_used": 1.0,
            "api_percent_used": 1.0,
            "auto_bucket_models": ["composer-2.5"],
        },
    }


def fetch_usage_sync(access_token: str, *, account_key: str = "") -> dict[str, Any]:
    if _mock_mode_enabled():
        return _mock_usage()
    with _http_client(account_key=account_key, timeout=25.0) as client:
        usage = fetch_cursor_usage(access_token, client=client)
    return normalize_usage(usage)


async def fetch_usage(access_token: str, *, account_key: str = "") -> dict[str, Any]:
    import asyncio
    return await asyncio.to_thread(fetch_usage_sync, access_token, account_key=account_key)


__all__ = [
    "CursorAuthPending",
    "CursorAuthParams",
    "CursorTokens",
    "account_label",
    "decode_access_claims",
    "expiry_iso",
    "fetch_model_catalog_sync",
    "fetch_profile_sync",
    "fetch_models_sync",
    "fetch_usage",
    "fetch_usage_sync",
    "generate_login",
    "normalize_usage",
    "poll_login_once",
    "refresh_sync",
    "subject_from_access_token",
    "web_session_cookie",
]
