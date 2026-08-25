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
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
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
    CURSOR_USAGE_EVENTS_URL,
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


def _event_nonnegative_int(value: Any) -> int:
    if value is None or isinstance(value, bool):
        return 0
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError, OverflowError):
        return 0


def _event_nonnegative_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not parsed.is_finite() or parsed < 0:
        return None
    return parsed


def _normalize_usage_event(raw: dict[str, Any]) -> dict[str, Any] | None:
    conversation_id = _profile_text(raw.get("conversationId"))
    model = _profile_text(raw.get("model"))
    timestamp_ms = _event_nonnegative_int(raw.get("timestamp"))
    if not conversation_id or not model or timestamp_ms <= 0:
        return None
    token_usage = raw.get("tokenUsage") if isinstance(raw.get("tokenUsage"), dict) else {}
    charged_cents = _event_nonnegative_decimal(raw.get("chargedCents"))
    is_chargeable = (
        raw.get("isChargeable") if isinstance(raw.get("isChargeable"), bool) else None
    )
    cost_ticks = (
        int(
            (charged_cents * Decimal(100_000_000)).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP,
            )
        )
        if charged_cents is not None
        else 0 if is_chargeable is False else None
    )
    event_identity = json.dumps(
        {
            "conversation_id": conversation_id,
            "timestamp_ms": timestamp_ms,
            "model": model,
            "kind": _profile_text(raw.get("kind")),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "event_key": hashlib.sha256(event_identity.encode("utf-8")).hexdigest(),
        "conversation_id": conversation_id,
        "timestamp_ms": timestamp_ms,
        "model": model,
        "kind": _profile_text(raw.get("kind")),
        "input_tokens": _event_nonnegative_int(token_usage.get("inputTokens")),
        "output_tokens": _event_nonnegative_int(token_usage.get("outputTokens")),
        "cache_creation_tokens": _event_nonnegative_int(token_usage.get("cacheWriteTokens")),
        "cache_read_tokens": _event_nonnegative_int(token_usage.get("cacheReadTokens")),
        "charged_cents": float(charged_cents) if charged_cents is not None else None,
        "cost_ticks": cost_ticks,
        "request_units": (
            float(value) if (value := _event_nonnegative_decimal(raw.get("requestsCosts"))) is not None
            else None
        ),
        "is_chargeable": is_chargeable,
        "is_headless": raw.get("isHeadless") if isinstance(raw.get("isHeadless"), bool) else None,
    }


def fetch_usage_events_sync(
    access_token: str,
    *,
    start_ms: int,
    end_ms: int,
    account_key: str = "",
    page_size: int = 1000,
    max_pages: int = 20,
) -> list[dict[str, Any]]:
    """Fetch normalized Cursor dashboard events for exact conversation reconciliation."""
    if _mock_mode_enabled():
        return []
    start = max(0, int(start_ms))
    end = max(start, int(end_ms))
    size = max(1, min(1000, int(page_size or 1000)))
    pages = max(1, min(200, int(max_pages or 20)))
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Cookie": web_session_cookie(access_token),
        "Origin": "https://cursor.com",
        "Referer": "https://cursor.com/dashboard/usage",
        "User-Agent": f"cursor-agent/{CURSOR_CLIENT_VERSION.removeprefix('cli-')}",
    }
    deduped: dict[str, dict[str, Any]] = {}
    try:
        with _http_client(account_key=account_key, timeout=30.0) as client:
            for page in range(1, pages + 1):
                response = client.post(
                    CURSOR_USAGE_EVENTS_URL,
                    headers=headers,
                    json={
                        "startDate": str(start),
                        "endDate": str(end),
                        "page": page,
                        "pageSize": size,
                    },
                )
                if response.status_code in {401, 403}:
                    raise CursorAuthError(
                        f"Cursor usage events 未授权 ({response.status_code})"
                    )
                if not response.is_success:
                    raise classify_cursor_failure(
                        f"Cursor usage events 返回 HTTP {response.status_code}",
                        http_status=response.status_code,
                    )
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise CursorError(
                        "Cursor usage events 不是有效 JSON",
                        code="usage_events_invalid",
                        status=502,
                    ) from exc
                if not isinstance(payload, dict):
                    raise CursorError(
                        "Cursor usage events 格式无效",
                        code="usage_events_invalid",
                        status=502,
                    )
                raw_events = payload.get("usageEventsDisplay")
                raw_events = raw_events if isinstance(raw_events, list) else []
                for raw in raw_events:
                    if not isinstance(raw, dict):
                        continue
                    normalized = _normalize_usage_event(raw)
                    if normalized is not None:
                        deduped[normalized["event_key"]] = normalized
                total = _event_nonnegative_int(payload.get("totalUsageEventsCount"))
                if not raw_events or len(raw_events) < size or len(deduped) >= total:
                    break
    except httpx.TimeoutException as exc:
        raise CursorTimeoutError("Cursor usage events 请求超时") from exc
    except CursorError:
        raise
    except httpx.HTTPError as exc:
        raise classify_cursor_failure("Cursor usage events 请求失败") from exc
    return sorted(deduped.values(), key=lambda event: int(event["timestamp_ms"]))


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
    "fetch_usage_events_sync",
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
