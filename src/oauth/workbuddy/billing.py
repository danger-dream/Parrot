"""WorkBuddy credits and activity reads. Unknown/partial never means zero."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import common

MAX_PAGES = 50
PAGE_SIZE = 100
CN_TIMEZONE = ZoneInfo("Asia/Shanghai")


def _error(exc: Exception) -> dict:
    if isinstance(exc, common.WorkBuddyError):
        return {"code": exc.code, "http_status": exc.status_code, "kind": exc.kind,
                "retryable": exc.retryable, "auth_error": exc.auth_error}
    return {"kind": "invalid_data", "retryable": False}


def _time(value, realm: str) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            if realm != "cn":
                return None  # Global's unzoned billing dates are not yet verified.
            dt = dt.replace(tzinfo=CN_TIMEZONE)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, OverflowError):
        return None


def normalize_package(raw: dict, realm: str) -> dict:
    suffixes = ("Size", "Remain", "Used")
    prefix = "CycleCapacity" if any(
        "CycleCapacity" + suffix + precision in raw
        for suffix in suffixes for precision in ("", "Precise")
    ) else "Capacity"
    # The integer fields truncate independently (e.g. 412 + 87 != 500).
    # Prefer the matching precise field, including explicit zero; only an
    # ABSENT precise field may fall back to the legacy field. Keep precision
    # for reconciliation, aggregation and quota decisions; round only in UI.
    values = [common.number(raw.get(prefix + suffix + "Precise")
                            if prefix + suffix + "Precise" in raw
                            else raw.get(prefix + suffix)) for suffix in suffixes]
    capacity, remaining, used = values
    invalid_precise = any(prefix + suffix + "Precise" in raw and value is None
                          for suffix, value in zip(suffixes, values))
    reliable = remaining is not None and not invalid_precise
    if capacity is not None and remaining is not None:
        reliable = reliable and remaining <= capacity
        if used is None and reliable:
            used = capacity - remaining
        elif used is not None and abs(capacity - remaining - used) > 0.000001:
            reliable = False
    if capacity is not None and used is not None and used > capacity:
        reliable = False
    expiry = _time(raw.get("PackageEndTime") or raw.get("EndTime"), realm)
    if expiry is None:
        # The live billing API's deduction-validity endpoint is epoch milliseconds,
        # distinct from CycleEndTime (which can be a monthly allowance boundary).
        deduction_end = common.number(raw.get("DeductionEndTime"))
        if deduction_end is not None and deduction_end > 0:
            try:
                expiry = datetime.fromtimestamp(deduction_end / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            except (ValueError, OverflowError, OSError):
                pass
    return {"id": common.text(str(raw.get("AccountId") or raw.get("PackageId") or ""), "package id"),
            "name": common.text(raw.get("PackageName") or "未命名资源包", "package name", maximum=512),
            "scope": "personal", "unit": "credits", "capacity": capacity, "remaining": remaining,
            "used": used, "reliable": reliable, "basis": prefix,
            "cycle_start": _time(raw.get("CycleStartTime"), realm),
            "cycle_end": _time(raw.get("CycleEndTime"), realm),
            "expires_at": expiry}


def _totals(packages: list[dict], complete: bool) -> dict:
    def add(field):
        values = [item.get(field) for item in packages]
        return sum(values) if values and all(value is not None for value in values) else None
    capacity, remaining, used = (add(field) for field in ("capacity", "remaining", "used"))
    reliable = bool(packages) and complete and all(item.get("reliable") for item in packages)
    percent = used * 100 / capacity if reliable and capacity and used is not None else None
    return {"capacity": capacity, "remaining": remaining, "used": used, "used_percent": percent,
            "reliable": reliable, "scope": "personal", "unit": "credits"}


def fetch_personal_sync(account: dict, *, account_key: str = "", timeout: float = 30.0) -> dict:
    realm = common.realm_of(account)
    now = datetime.now(CN_TIMEZONE if realm == "cn" else timezone.utc)
    body = {"PageSize": PAGE_SIZE, "ProductCode": "p_tcaca", "Status": [0, 3],
            "PackageEndTimeRangeBegin": now.strftime("%Y-%m-%d %H:%M:%S"),
            "PackageEndTimeRangeEnd": (now + timedelta(days=365 * 101)).strftime("%Y-%m-%d %H:%M:%S")}
    end = time.monotonic() + timeout
    packages, errors, seen_pages = [], {}, set()
    count = None
    complete = False
    for page in range(1, MAX_PAGES + 1):
        try:
            remaining_time = end - time.monotonic()
            if remaining_time <= 0:
                raise common.WorkBuddyError("credits", kind="deadline")
            data = common.request(account, "/v2/billing/meter/get-user-resource",
                                  body=dict(body, PageNumber=page), account_key=account_key,
                                  timeout=min(15.0, remaining_time))
            response = data.get("Response")
            if not isinstance(response, dict) or not isinstance(response.get("Data"), dict) or response.get("Error"):
                raise common.WorkBuddyError("credits", kind="invalid_envelope")
            data = response["Data"]
            total = common.number(data.get("TotalCount"))
            rows = data.get("Accounts")
            # The international endpoint explicitly returns Accounts:null for
            # TotalCount:0. Missing/malformed/non-empty responses remain errors.
            if "Accounts" in data and rows is None and total == 0:
                rows = []
            if not isinstance(rows, list) or not all(isinstance(item, dict) for item in rows):
                raise common.WorkBuddyError("credits", kind="invalid_packages")
            if total is not None and (not isinstance(total, int) or (count is not None and count != total)):
                raise common.WorkBuddyError("credits", kind="unstable_pagination")
            if total is not None:
                count = total
            normalized = [normalize_package(item, realm) for item in rows]
            signature = repr(normalized)
            if rows and signature in seen_pages:
                raise common.WorkBuddyError("credits", kind="repeated_page")
            seen_pages.add(signature)
            packages.extend(normalized)
            if count is not None and len(packages) >= count:
                complete = len(packages) == count
                if not complete:
                    errors["pagination"] = {"kind": "count_mismatch"}
                break
            if len(rows) < PAGE_SIZE:
                complete = count is None or len(packages) == count
                if not complete:
                    errors["pagination"] = {"kind": "incomplete_pages"}
                break
        except (common.WorkBuddyError, ValueError) as exc:
            errors["credits"] = _error(exc)
            break
    else:
        errors["pagination"] = {"kind": "page_limit"}
    credits = _totals(packages, complete)
    return {"packages": packages, "credits": credits, "complete": complete,
            "status": "empty" if complete and not packages else "known" if credits["reliable"] else "partial" if packages else "unknown",
            "errors": errors}


def fetch_enterprise_sync(account: dict, *, account_key: str = "") -> dict:
    if common.realm_of(account) != "cn" or not account.get("enterprise_id"):
        raise ValueError("Unsupported WorkBuddy enterprise scope")
    data = common.request(account, "/billing/meter/get-enterprise-user-usage", body={}, kind="enterprise", account_key=account_key)
    used, capacity = common.number(data.get("credit")), common.number(data.get("limitNum"))
    reliable = used is not None and capacity is not None and capacity > 0
    remaining = max(0, capacity - used) if reliable else None
    return {"scope": "enterprise", "unit": "credits", "used": used, "capacity": capacity,
            "remaining": remaining, "used_percent": min(100, used * 100 / capacity) if reliable else None,
            "reliable": reliable, "cycle_start": _time(data.get("cycleStartTime"), "cn"),
            "cycle_end": _time(data.get("cycleEndTime"), "cn")}


def fetch_checkin_sync(account: dict, *, account_key: str = "") -> dict:
    if common.realm_of(account) != "cn":
        return {"supported": False}
    try:
        data = common.request(account, "/v2/billing/meter/checkin-activity-status", body={}, account_key=account_key)
    except common.WorkBuddyError as exc:
        if exc.status_code != 404:
            raise
        data = common.request(account, "/v2/billing/meter/checkin-status", body={}, account_key=account_key)
    result = {"supported": True, "observed_at": int(time.time() * 1000)}
    for target, keys in {
        "active": ("active", "Active"), "today_checked_in": ("todayCheckedIn", "today_checked_in"),
        "streak_days": ("streakDays", "streak_days"), "daily_credit": ("dailyCredit", "daily_credit"),
        "today_credit": ("todayCredit", "today_credit"), "total_credits": ("totalCredits", "total_credits"),
        "activity_name": ("activityName", "activity_name"),
    }.items():
        value = next((data[key] for key in keys if key in data), None)
        if target in {"active", "today_checked_in"}:
            result[target] = value if isinstance(value, bool) else None
        elif target == "activity_name":
            result[target] = common.text(value, "activity name", maximum=512) if isinstance(value, str) else None
        else:
            result[target] = common.number(value)
    return result


def fetch_usage_sync(access_token: str, *, account: dict, account_key: str = "") -> dict:
    account = dict(account, access_token=access_token)
    realm = common.realm_of(account)
    block = fetch_personal_sync(account, account_key=account_key)
    block.update(realm=realm, source="workbuddy:billing", fetched_at=int(time.time() * 1000),
                 scope="enterprise" if account.get("enterprise_id") else "personal", unit="credits")
    block["personal_credits"] = dict(block["credits"])
    if account.get("enterprise_id"):
        try:
            enterprise = fetch_enterprise_sync(account, account_key=account_key)
            block["enterprise_credits"] = enterprise
            block["credits"] = enterprise
            block["status"] = "known" if enterprise["reliable"] else "unknown"
            block["complete"] = enterprise["reliable"]
        except (common.WorkBuddyError, ValueError) as exc:
            block["errors"]["enterprise"] = _error(exc)
            block["credits"] = {"scope": "enterprise", "unit": "credits", "reliable": False,
                                "remaining": None, "used": None, "capacity": None, "used_percent": None}
            block["status"], block["complete"] = "unknown", False
    if realm == "cn":
        try:
            block["checkin"] = fetch_checkin_sync(account, account_key=account_key)
        except (common.WorkBuddyError, ValueError) as exc:
            block["checkin"] = {"supported": True, "active": None, "today_checked_in": None}
            block["errors"]["checkin"] = _error(exc)
    else:
        block["checkin"] = {"supported": False}
    block["trial"] = {"supported": realm == "global", "claim_state": "unknown"}
    try:
        payment = common.request(account, "/v2/billing/meter/get-payment-type", body={}, account_key=account_key, timeout=8.0)
        value = payment.get("paymentType")
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            block["payment_type"] = str(value)[:80]
    except (common.WorkBuddyError, ValueError) as exc:
        block["errors"]["payment_type"] = _error(exc)
    if block["credits"].get("reliable"):
        block["last_success_at"] = block["fetched_at"]
    return {"workbuddy": block}


def execute_action_sync(account: dict, action: str, *, account_key: str = "") -> dict:
    """Wire-only action. Call only after durable intent and confirmation checks."""
    realm = common.realm_of(account)
    if action == "checkin" and realm == "cn":
        path = "/v2/billing/meter/daily-checkin"
    elif action == "claim_trial" and realm == "global":
        path = "/billing/ide/trial"
    else:
        raise ValueError("WorkBuddy action is not supported for this region")
    try:
        data = common.request(account, path, body={}, account_key=account_key, allow_empty=True)
    except common.WorkBuddyError as exc:
        if action == "claim_trial" and exc.code == 14051:
            return {"status": "already_done", "code": exc.code}
        raise
    result = {"status": "succeeded"}
    # Only explicit response fields establish an award, never a balance delta.
    award = common.number(data.get("todayCredit", data.get("today_credit")))
    if award is not None:
        result["awarded_credits"] = award
    return result
