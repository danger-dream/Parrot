"""Shared WorkBuddy action ownership, durable intent, reconciliation and scheduling.

No network POST is replayed merely because a caller timed out or restarted.
Token material lives only in the account/refresh boundary, never the ledger.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import threading
import time
import uuid
from datetime import datetime

from ... import config, notifier, oauth_manager, state_db
from . import billing, common, runtime

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
_next_auto_at: dict[str, float] = {}
_auto_attempts: dict[str, tuple[str, int]] = {}
_cleanup_after = 0.0


def _lock(key):
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def owner(account_key: str) -> str:
    return hashlib.sha256(account_key.encode()).hexdigest()


def business_date() -> str:
    return datetime.now(billing.CN_TIMEZONE).strftime("%Y-%m-%d")


def action_key(account_key: str, action: str, day: str) -> str:
    return hashlib.sha256((account_key + "|" + action + "|" + day).encode()).hexdigest()


def current_account(account_key: str, action: str) -> dict:
    account = oauth_manager.get_account(account_key)
    if not account or oauth_manager.provider_of(account) != "workbuddy":
        raise common.WorkBuddyError("action", kind="account_missing")
    realm = common.realm_of(account)
    if (action, realm) not in {("checkin", "cn"), ("claim_trial", "global")}:
        raise common.WorkBuddyError("action", kind="unsupported_region")
    if account.get("disabled_reason") not in (None, "quota") or (not account.get("enabled", True) and account.get("disabled_reason") != "quota"):
        raise common.WorkBuddyError("action", kind="account_paused")
    return copy.deepcopy(account)


def public_record(record: dict) -> dict:
    fields = {"id", "action", "business_date", "source", "attempt", "status", "phase", "code",
              "http_status", "awarded_credits", "balance_updated", "created_at", "updated_at", "reconciled"}
    return {key: copy.deepcopy(value) for key, value in record.items() if key in fields}


def history(account_key: str, *, limit: int | None = 50) -> list[dict]:
    return [public_record(record) for record in state_db.workbuddy_action_history(owner(account_key), limit=limit)]


def inspect(account_key: str, action: str, *, allow_unknown: bool = False, free_trial_confirmed: bool = False) -> dict:
    account = current_account(account_key, action)
    day = business_date() if action == "checkin" else "lifetime"
    record = state_db.workbuddy_action_load(action_key(account_key, action, day))
    if action == "claim_trial" and not free_trial_confirmed:
        raise common.WorkBuddyError("action", kind="free_trial_terms_confirmation_required")
    status = None
    if action == "checkin":
        try:
            status = billing.fetch_checkin_sync(account, account_key=account_key)
        except Exception:
            if not allow_unknown:
                raise common.WorkBuddyError("checkin", kind="status_unknown") from None
        if status and status.get("active") is False:
            raise common.WorkBuddyError("checkin", kind="activity_inactive")
        if not allow_unknown and (not status or status.get("active") is not True or status.get("today_checked_in") is None):
            raise common.WorkBuddyError("checkin", kind="status_unknown")
    return {"action": action, "business_date": day,
            "observed_status": status, "effects_enabled": common.effects_allowed(),
            "prior_result": public_record(record) if record else None}


def _reconcile(account_key, account, key, record):
    # A leftover pending is evidence of possible dispatch, not permission to replay.
    state = "unknown"
    if record["action"] == "checkin" and record["business_date"] == business_date():
        try:
            status = billing.fetch_checkin_sync(account, account_key=account_key)
            if status.get("today_checked_in") is True:
                state = "already_done"
        except Exception:
            pass
    saved = state_db.workbuddy_action_finish(key, record["attempt_id"],
        {"status": state, "phase": "reconciled", "reconciled": state == "already_done"})
    return public_record(saved)


def reconcile_pending(account_key: str) -> list[dict]:
    """Explicit status refresh: query unresolved intents only, never dispatch POSTs."""
    with _lock(account_key):
        account = oauth_manager.get_account(account_key)
        if not account or oauth_manager.provider_of(account) != "workbuddy":
            raise common.WorkBuddyError("action", kind="account_missing")
        results = []
        for record in state_db.workbuddy_action_history(owner(account_key), limit=None):
            if record.get("status") in {"pending", "unknown"}:
                key = action_key(account_key, record["action"], record["business_date"])
                results.append(_reconcile(account_key, account, key, record))
        return results


def execute(account_key: str, action: str, *, actor: str, source: str = "manual",
            expected_day: str | None = None, expected_generation: str | None = None,
            allow_unknown: bool = False, free_trial_confirmed: bool = False,
            retry_failed: bool = False, before_submit=None) -> dict:
    """Call after an explicit actor-bound confirmation, or persisted auto opt-in."""
    with _lock(account_key):
        account = current_account(account_key, action)
        if source == "auto" and (action != "checkin" or account.get("workbuddy_auto_checkin") is not True):
            raise common.WorkBuddyError("action", kind="auto_not_enabled")
        day = business_date() if action == "checkin" else "lifetime"
        if expected_day and expected_day != day:
            raise common.WorkBuddyError("action", kind="business_date_changed")
        generation = runtime.credential_fingerprint(account)
        if expected_generation and expected_generation != generation:
            raise common.WorkBuddyError("action", kind="stale_generation")
        key = action_key(account_key, action, day)
        old = state_db.workbuddy_action_load(key)
        if old and old.get("status") in {"succeeded", "already_done"}:
            return public_record(old)
        if old and old.get("status") in {"pending", "unknown"}:
            return _reconcile(account_key, account, key, old)
        if source == "auto":
            # A fresh evening opportunity may retry a *certain* rejection from
            # before 21:05. Pending/unknown returned above only reconcile. The
            # durable attempt timestamp also prevents retries after a restart.
            now_bjt = datetime.now(billing.CN_TIMEZONE)
            evening = now_bjt.replace(hour=21, minute=5, second=0, microsecond=0)
            previous_at = common.number((old or {}).get("updated_at"))
            retry_failed = bool(old and old.get("status") in {"failed", "rejected"}
                                and now_bjt >= evening and previous_at is not None
                                and previous_at < evening.timestamp() * 1000)
            allow_unknown = False
        if old and not retry_failed:
            return public_record(old)
        if action == "claim_trial" and not free_trial_confirmed:
            raise common.WorkBuddyError("action", kind="free_trial_terms_confirmation_required")
        access_token = asyncio.run(oauth_manager.ensure_valid_token(account_key))
        account = current_account(account_key, action)
        if account["access_token"] != access_token:
            raise common.WorkBuddyError("action", kind="stale_generation")
        observation = inspect(account_key, action, allow_unknown=allow_unknown, free_trial_confirmed=free_trial_confirmed)
        checked = (observation.get("observed_status") or {}).get("today_checked_in") is True
        generation = runtime.credential_fingerprint(account)
        # Cancelling before this boundary prevents durable intent and the POST.
        if before_submit is not None:
            before_submit()
        latest = current_account(account_key, action)
        if runtime.credential_fingerprint(latest) != generation or (source == "auto" and latest.get("workbuddy_auto_checkin") is not True):
            raise common.WorkBuddyError("action", kind="stale_generation")
        if action == "checkin" and day != business_date():
            raise common.WorkBuddyError("action", kind="business_date_changed")
        now = int(time.time() * 1000)
        candidate = {"owner": owner(account_key), "action": action, "business_date": day,
            "attempt_id": uuid.uuid4().hex, "actor": actor, "source": source,
            "credential_generation": generation, "confirmed_at": now, "created_at": now, "updated_at": now}
        intent = state_db.workbuddy_action_begin(key, candidate, retry_failed=retry_failed)
        if not intent["created"]:
            return public_record(intent["record"])
        record = intent["record"]
        # This call is never reached if prepare/write/verify/install failed.
        try:
            result = {"status": "already_done"} if checked else billing.execute_action_sync(account, action, account_key=account_key)
        except common.WorkBuddyError as exc:
            certain_rejection = exc.code is not None or 400 <= exc.status_code < 500
            result = {"status": "rejected" if certain_rejection else "unknown",
                      "code": exc.code, "http_status": exc.status_code}
        except Exception:
            result = {"status": "unknown"}
        result["phase"] = "response_received" if result["status"] != "unknown" else "response_unknown"
        # A post-send persistence error must propagate, leaving durable pending.
        saved = state_db.workbuddy_action_finish(key, record["attempt_id"], result)
        out = public_record(saved)
        if result["status"] in {"succeeded", "already_done"}:
            out["balance_updated"] = False
            try:
                if runtime.credential_fingerprint(current_account(account_key, action)) == generation:
                    usage = asyncio.run(oauth_manager.fetch_usage_snapshot(account_key))
                    if runtime.credential_fingerprint(current_account(account_key, action)) == generation:
                        state_db.quota_save(account_key, oauth_manager.flatten_usage(usage), email=oauth_manager.account_key_to_email(account_key))
                        toggled = oauth_manager.evaluate_and_toggle_by_usage(account_key, usage, fresh=True)
                        out["balance_updated"] = (usage.get("workbuddy") or {}).get("status") == "known"
                        out["quota_action"] = toggled.get("action")
            except Exception:
                pass
            state_db.workbuddy_action_finish(key, record["attempt_id"],
                {"status": saved["status"], "balance_updated": out["balance_updated"]})
        return out


def _notify_auto_result(key: str, result: dict, *, observation=None) -> None:
    from ... import notifications
    from ...notifications import reporting
    from ...notifications.events import Effect
    subject = observation[0] if observation else notifications.subjects.account(key)
    if notifications.subjects.current(subject) is None:
        return  # No delayed result may target a replacement account.
    if result.get("status") in {"succeeded", "already_done"}:
        notifications.resolve_silently("account.action.attention", subject,
            scope="operation", scope_ref="checkin", dimension=business_date(), version=observation[1] if observation else 0)
    if result.get("quota_action") == "resumed":
        reporting.quota(key, recovered=True, operation="签到后积分重新可用", source="workbuddy_checkin", observation=observation)
    elif result.get("status") not in {"succeeded", "already_done", "paused"}:
        unknown = result.get("status") in {"pending", "unknown"}
        notifications.account_event("account.action.attention", key,
            {"operation": "自动签到", "result": str(result.get("status") or "unknown"), "business_date": business_date(), "error_code": "result_unknown" if unknown else "action_not_completed"},
            Effect("unknown" if unknown else "failed", "action_unknown" if unknown else "action_failed"),
            source="workbuddy_checkin", scope="operation", scope_ref="checkin", dimension=business_date(), observation=observation)


def auto_checkin_once() -> dict:
    global _cleanup_after
    now = datetime.now(billing.CN_TIMEZONE)
    if (now.hour, now.minute) < (9, 5):
        return {}
    accounts = [a for a in oauth_manager.list_accounts() if oauth_manager.provider_of(a) == "workbuddy"]
    if not accounts:
        return {}
    results = {}
    for account in accounts:
        if account.get("realm") != "cn" or account.get("workbuddy_auto_checkin") is not True:
            continue
        key = oauth_manager.get_account_key(account)
        if account.get("disabled_reason") not in (None, "quota"):
            continue
        day = now.strftime("%Y-%m-%d")
        window = day + (":evening" if (now.hour, now.minute) >= (21, 5) else ":morning")
        previous_window, count = _auto_attempts.get(key, (window, 0))
        if previous_window == window and _next_auto_at.get(key, 0) > time.monotonic():
            continue
        count = count if previous_window == window else 0
        if count >= 3:
            continue
        _auto_attempts[key] = (window, count + 1)
        _next_auto_at[key] = time.monotonic() + 15 * 60
        from ...notifications import subjects, store
        subject = subjects.account(account)
        try:
            observation = (subject, store.next_version(subject))
        except Exception:
            observation = (subject, 0)
        try:
            with oauth_manager.exact_operation_target(key, oauth_manager.account_state_key(account)):
                results[key] = execute(key, "checkin", actor="scheduler:workbuddy", source="auto")
        except common.WorkBuddyError as exc:
            results[key] = {"status": "paused" if exc.kind == "account_paused" else "not_submitted", "code": exc.kind}
        except Exception:
            results[key] = {"status": "unknown", "code": "persistence_or_runtime_failure"}
        try:
            _notify_auto_result(key, results[key], observation=observation)
        except Exception:
            pass  # A notification failure never changes a recorded action.
    if time.monotonic() >= _cleanup_after:
        # Keep 90 days of determined check-in outcomes. Trial ownership and all
        # unresolved intents are retained regardless of age.
        _cleanup_after = time.monotonic() + 86400
        state_db.workbuddy_action_cleanup(int(time.time() * 1000) - 90 * 86400000)
        live = {oauth_manager.get_account_key(a) for a in oauth_manager.list_accounts() if oauth_manager.provider_of(a) == "workbuddy"}
        for key in set(_next_auto_at) - live:
            _next_auto_at.pop(key, None)
            _auto_attempts.pop(key, None)
    return results
