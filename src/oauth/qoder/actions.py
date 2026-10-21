"""Qoder daily credits: manual claims and the per-account daily schedule.

The campaign answers CLAIMED for a claim already made, so a repeated claim
is harmless; the lock only keeps one account's claims from racing.
"""
from __future__ import annotations

import copy
import threading
import time
from datetime import datetime, timedelta, timezone

from ... import oauth_manager, state_db
from ...safe_diagnostics import report
from . import billing, common

CN_TIMEZONE = timezone(timedelta(hours=8))
RECHECK_SECONDS = 30 * 60
SETTLED = {"claimed", "already_done"}

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def business_date(now: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if now is None else now, CN_TIMEZONE).strftime("%Y-%m-%d")


def record(account_key: str) -> dict:
    return copy.deepcopy(state_db.qoder_checkin_load(account_key) or {})


def due(account_key: str, now: float | None = None) -> bool:
    """A new Beijing day, a campaign that ended, or a look older than 30 minutes."""
    now = time.time() if now is None else now
    last = record(account_key)
    if not last or last.get("business_date") != business_date(now):
        return True
    until = common.number(last.get("until"))
    if last.get("status") in SETTLED and until and until <= now:
        return True
    return now - float(last.get("at") or 0) >= RECHECK_SECONDS


def _account(account_key: str) -> dict:
    account = oauth_manager.get_account(account_key)
    if not account or oauth_manager.provider_of(account) != "qoder":
        raise common.QoderError("checkin", kind="account_missing")
    if account.get("qoder_relogin_required") or account.get("disabled_reason") in {"user", "auth_error"}:
        raise common.QoderError("checkin", kind="account_paused")
    return copy.deepcopy(account)


def checkin_now(account_key: str, *, source: str = "manual") -> dict:
    with _lock(account_key):
        now = time.time()
        row = {"business_date": business_date(now), "at": now, "source": source}
        try:
            account = _account(account_key)
            row["realm"] = common.realm_of(account)
            if (account.get("qoder_device_chat") or account.get("qoder_auth_method") == "pat") and common.refresh_allowed():
                # The device token is the chat token here; renew it before it is used.
                oauth_manager._refresh_sync_locked(account_key, False, expected_state_key=oauth_manager.account_state_key(account))
                account = _account(account_key)
            supported, why = billing.checkin_supported(account)
            if not supported:
                row.update(status="unsupported", error_kind=why)
            else:
                result = billing.checkin_sync(account, account_key=account_key)
                row.update(status=result["status"])
                for key in ("credit", "until"):
                    if result.get(key):
                        row[key] = result[key]
        except common.QoderError as exc:
            row.update(status="failed", error_kind=exc.kind, error_status=exc.status_code or None)
        except Exception as exc:
            report(exc, provider="qoder", operation="checkin", stage="execute")
            row.update(status="failed", error_kind="internal")
        previous = record(account_key)
        if (row["status"] == "already_done" and previous.get("status") == "claimed"
                and previous.get("business_date") == row["business_date"]):
            # A later look at a claim made today keeps what that claim gave.
            row.update(status="claimed", credit=row.get("credit") or previous.get("credit"))
        row = {key: value for key, value in row.items() if value is not None}
        state_db.qoder_checkin_save(account_key, row)
        return row


def set_auto(account_key: str, enabled: bool) -> dict:
    current = oauth_manager.get_account(account_key)
    if not current or oauth_manager.provider_of(current) != "qoder":
        raise common.QoderError("checkin", kind="account_missing")

    def mark(value):
        value["qoder_auto_checkin"] = bool(enabled)

    return oauth_manager.mutate_account_if_unchanged(account_key, current, mark)


def auto_checkin_once(now: float | None = None) -> dict[str, str]:
    out: dict[str, str] = {}
    for account in oauth_manager.list_accounts():
        if oauth_manager.provider_of(account) != "qoder" or account.get("qoder_auto_checkin") is not True:
            continue
        if account.get("qoder_relogin_required") or account.get("disabled_reason") in {"user", "auth_error"}:
            continue
        key = oauth_manager.get_account_key(account)
        if not due(key, now):
            continue
        try:
            out[key] = checkin_now(key, source="auto")["status"]
        except Exception as exc:
            report(exc, provider="qoder", operation="checkin", stage="auto")
            out[key] = "failed"
    return out
