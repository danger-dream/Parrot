"""Fetch Cursor plan/quota metadata.

Primary source is DashboardService/GetCurrentPeriodUsage (Connect JSON).
Stripe profile, plan info, and legacy /auth/usage are merged when present.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import httpx

from .constants import (
    CURSOR_PERIOD_USAGE_PATH,
    CURSOR_PLAN_INFO_PATH,
    CURSOR_STRIPE_PROFILE_URL,
    CURSOR_USAGE_URL,
    UNARY_RPC_TIMEOUT_S,
)
from .errors import CursorAuthError, CursorError, classify_cursor_failure
from .h2stream import cursor_http_headers


@dataclass(frozen=True)
class CursorPlanUsage:
    total_spend_cents: int | None = None
    included_spend_cents: int | None = None
    remaining_cents: int | None = None
    limit_cents: int | None = None
    auto_percent_used: float | None = None
    api_percent_used: float | None = None
    total_percent_used: float | None = None


@dataclass(frozen=True)
class CursorSpendLimit:
    individual_limit_cents: int | None = None
    individual_remaining_cents: int | None = None
    limit_type: str | None = None


@dataclass(frozen=True)
class CursorModelUsage:
    key: str
    num_requests: int = 0
    num_requests_total: int = 0
    num_tokens: int = 0
    max_request_usage: int | None = None
    max_token_usage: int | None = None


@dataclass
class CursorUsage:
    membership_type: str | None = None
    individual_membership_type: str | None = None
    subscription_status: str | None = None
    plan_name: str | None = None
    included_amount_cents: int | None = None
    billing_cycle_start: str | None = None
    billing_cycle_end: str | None = None
    plan_usage: CursorPlanUsage | None = None
    spend_limit: CursorSpendLimit | None = None
    models: tuple[CursorModelUsage, ...] = ()
    auto_bucket_models: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    is_team_member: bool = False
    is_yearly_plan: bool = False
    display_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["object"] = "cursor.usage"
        return payload


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip():
        try:
            return int(float(value))
        except ValueError:
            return None
    return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return None


def parse_period_usage(raw: dict[str, Any]) -> CursorUsage:
    plan_raw = raw.get("planUsage") if isinstance(raw.get("planUsage"), dict) else {}
    spend_raw = raw.get("spendLimitUsage") if isinstance(raw.get("spendLimitUsage"), dict) else {}
    plan_usage = CursorPlanUsage(
        total_spend_cents=_as_int(plan_raw.get("totalSpend")),
        included_spend_cents=_as_int(plan_raw.get("includedSpend")),
        remaining_cents=_as_int(plan_raw.get("remaining")),
        limit_cents=_as_int(plan_raw.get("limit")),
        auto_percent_used=_as_float(plan_raw.get("autoPercentUsed")),
        api_percent_used=_as_float(plan_raw.get("apiPercentUsed")),
        total_percent_used=_as_float(plan_raw.get("totalPercentUsed")),
    )
    spend_limit = CursorSpendLimit(
        individual_limit_cents=_as_int(spend_raw.get("individualLimit")),
        individual_remaining_cents=_as_int(spend_raw.get("individualRemaining")),
        limit_type=_as_str(spend_raw.get("limitType")),
    )
    has_plan = any(value is not None for value in plan_usage.__dict__.values())
    has_spend = any(value is not None for value in spend_limit.__dict__.values())
    return CursorUsage(
        billing_cycle_start=_as_str(raw.get("billingCycleStart")),
        billing_cycle_end=_as_str(raw.get("billingCycleEnd")),
        plan_usage=plan_usage if has_plan else None,
        spend_limit=spend_limit if has_spend else None,
        display_message=_as_str(raw.get("displayMessage")),
        auto_bucket_models=tuple(
            str(item).strip() for item in (raw.get("autoBucketModels") or [])
            if str(item).strip()
        ),
        sources=("dashboard.GetCurrentPeriodUsage",),
    )


def parse_plan_info(raw: dict[str, Any]) -> CursorUsage:
    info = raw.get("planInfo") if isinstance(raw.get("planInfo"), dict) else raw
    return CursorUsage(
        plan_name=_as_str(info.get("planName")),
        included_amount_cents=_as_int(info.get("includedAmountCents")),
        billing_cycle_end=_as_str(info.get("billingCycleEnd")),
        sources=("dashboard.GetPlanInfo",),
    )


def parse_stripe_profile(raw: dict[str, Any]) -> CursorUsage:
    return CursorUsage(
        membership_type=_as_str(raw.get("membershipType")),
        individual_membership_type=_as_str(raw.get("individualMembershipType")),
        subscription_status=_as_str(raw.get("subscriptionStatus")),
        is_team_member=bool(raw.get("isTeamMember")),
        is_yearly_plan=bool(raw.get("isYearlyPlan")),
        sources=("auth/full_stripe_profile",),
    )


def parse_auth_usage(raw: dict[str, Any]) -> CursorUsage:
    models: list[CursorModelUsage] = []
    start = _as_str(raw.get("startOfMonth"))
    for key, value in raw.items():
        if key == "startOfMonth" or not isinstance(value, dict):
            continue
        models.append(
            CursorModelUsage(
                key=str(key),
                num_requests=_as_int(value.get("numRequests")) or 0,
                num_requests_total=_as_int(value.get("numRequestsTotal")) or 0,
                num_tokens=_as_int(value.get("numTokens")) or 0,
                max_request_usage=_as_int(value.get("maxRequestUsage")),
                max_token_usage=_as_int(value.get("maxTokenUsage")),
            )
        )
    models.sort(key=lambda item: item.key)
    return CursorUsage(billing_cycle_start=start, models=tuple(models), sources=("auth/usage",))


def merge_usage(*parts: CursorUsage | None) -> CursorUsage:
    merged = CursorUsage()
    sources: list[str] = []
    for part in parts:
        if part is None:
            continue
        sources.extend(part.sources)
        for name in (
            "membership_type",
            "individual_membership_type",
            "subscription_status",
            "plan_name",
            "included_amount_cents",
            "billing_cycle_start",
            "billing_cycle_end",
            "plan_usage",
            "spend_limit",
            "display_message",
        ):
            value = getattr(part, name)
            if value not in (None, (), ""):
                setattr(merged, name, value)
        if part.models:
            merged.models = part.models
        if part.auto_bucket_models:
            merged.auto_bucket_models = part.auto_bucket_models
        if part.is_team_member:
            merged.is_team_member = True
        if part.is_yearly_plan:
            merged.is_yearly_plan = True
    merged.sources = tuple(dict.fromkeys(sources))
    return merged


def _request_json(
    client: httpx.Client,
    method: str,
    url: str,
    token: str,
    *,
    body: bytes | None = None,
) -> dict[str, Any] | None:
    headers = cursor_http_headers(token, content_type="application/json")
    response = client.request(method, url, headers=headers, content=body)
    if response.status_code in {401, 403}:
        raise CursorAuthError(f"usage endpoint unauthorized ({response.status_code})")
    if not response.is_success:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def fetch_cursor_usage(access_token: str, *, client: httpx.Client | None = None) -> CursorUsage:
    own = client is None
    http = client or httpx.Client(timeout=UNARY_RPC_TIMEOUT_S)
    origin = "https://api2.cursor.sh"
    try:
        period = _request_json(
            http,
            "POST",
            origin + CURSOR_PERIOD_USAGE_PATH,
            access_token,
            body=b"{}",
        )
        plan = _request_json(
            http,
            "POST",
            origin + CURSOR_PLAN_INFO_PATH,
            access_token,
            body=b"{}",
        )
        stripe = _request_json(http, "GET", CURSOR_STRIPE_PROFILE_URL, access_token)
        legacy = _request_json(http, "GET", CURSOR_USAGE_URL, access_token)
        merged = merge_usage(
            parse_period_usage(period) if period else None,
            parse_plan_info(plan) if plan else None,
            parse_stripe_profile(stripe) if stripe else None,
            parse_auth_usage(legacy) if legacy else None,
        )
        if not merged.sources:
            raise CursorError("failed to fetch Cursor usage", code="usage_unavailable", status=502)
        return merged
    except CursorError:
        raise
    except httpx.TimeoutException as exc:
        raise classify_cursor_failure(f"usage request timed out: {exc}") from exc
    except httpx.HTTPError as exc:
        raise classify_cursor_failure(f"usage request failed: {exc}") from exc
    finally:
        if own:
            http.close()
