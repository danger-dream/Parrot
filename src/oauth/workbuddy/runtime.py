"""WorkBuddy lifecycle helpers, called under Parrot's account refresh lock."""
from __future__ import annotations

import copy
import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import auth, common


@dataclass
class _RefreshState:
    fingerprint: str
    retry_at: float = 0
    unknown_valid_until: float = 0
    pending_until: float = 0
    pending: dict | None = field(default=None, repr=False)
    failed: bool = False
    candidate_expired: bool = False


_refresh_states: dict[str, _RefreshState] = {}


def credential_fingerprint(account: dict) -> str:
    raw = json.dumps([auth.identity(account), account.get("generationId"), account.get("access_token"), account.get("refresh_token")], ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def forget(account_key: str) -> None:
    _refresh_states.pop(account_key, None)


def refresh_due(account: dict, account_key: str, threshold: int = 300) -> bool:
    state = _refresh_states.get(account_key)
    if state and state.fingerprint == credential_fingerprint(account):
        if state.candidate_expired:
            # Retry only the local terminal-state write if storage was unavailable.
            return account.get("enabled", True) and account.get("disabled_reason") not in {"user", "auth_error"}
        if state.pending is not None:
            return True
        if max(state.retry_at, state.unknown_valid_until) > time.monotonic():
            return False
    try:
        expiry = datetime.fromisoformat(str(account.get("expired") or "").replace("Z", "+00:00"))
        return expiry.timestamp() - time.time() < threshold
    except (ValueError, TypeError, OverflowError):
        return True


def _candidate_lost(account, account_key, state):
    from ... import oauth_manager, oauth_refresh_failure
    from ...notifications import reporting
    state.pending = None
    state.candidate_expired = True
    error = common.WorkBuddyError("refresh", kind="save_candidate_expired_relogin")
    error.oauth_refresh_observation = oauth_manager.refresh_observation(account)
    observation = reporting.observation(account_key)
    display, effect = oauth_refresh_failure.apply(account_key, error, provider="workbuddy")
    if effect is not None and not account.get("workbuddy_relogin_required"):
        try:
            reporting.refresh_failed(account_key, display, effect, source="credential_save", observation=observation)
        except Exception:
            pass  # Notification failure cannot turn terminal credential loss into success.
    raise error


def refresh_locked(account: dict, account_key: str, force: bool, *, reuse_current: bool = False) -> str:
    from ... import config, oauth_manager
    common.require_refresh()
    fingerprint = credential_fingerprint(account)
    expected_state_key = oauth_manager.account_state_key(account)
    state = _refresh_states.get(account_key)
    if state is None or state.fingerprint != fingerprint:
        state = _RefreshState(fingerprint)
        _refresh_states[account_key] = state
    now = time.monotonic()
    if account.get("workbuddy_relogin_required") or state.candidate_expired or (state.pending is not None and state.pending_until <= now):
        _candidate_lost(account, account_key, state)
    if state.pending is None and reuse_current:
        return account["access_token"]
    if state.pending is None and not force:
        if state.unknown_valid_until > now:
            return account["access_token"]
        if state.retry_at > now:
            raise common.WorkBuddyError("refresh", kind="backoff")
        if not refresh_due(account, account_key):
            return account["access_token"]
    if state.pending is None:
        try:
            result = auth.refresh_sync(account["refresh_token"], account=account, account_key=account_key)
        except Exception:
            state.retry_at, state.failed = now + 60, True
            raise
        fields = {"access_token": result["access_token"], "expired": "", "last_refresh": auth.utc_text(time.time())}
        if result.get("refresh_token"):
            fields["refresh_token"] = result["refresh_token"]
        if result.get("domain"):
            fields["domain"] = result["domain"]
        seconds = common.number(result.get("expires_in"))
        if seconds is not None and seconds > 0:
            fields["expired"] = auth.utc_text(time.time() + seconds)
        state.pending, state.pending_until = fields, now + 600
    if state.pending_until <= time.monotonic():
        # Do not reuse the pre-rotation RT after losing the recoverable candidate.
        _candidate_lost(account, account_key, state)
    fields = state.pending
    try:
        with config.serialized_updates():
            current = oauth_manager.get_account(account_key)
            if not current or credential_fingerprint(current) != fingerprint:
                forget(account_key)
                raise common.WorkBuddyError("refresh", kind="stale_generation")
            if not oauth_manager._save_token_fields(account_key, fields, expected_state_key=expected_state_key):
                forget(account_key)
                raise common.WorkBuddyError("refresh", kind="stale_generation")
    except common.WorkBuddyError:
        raise
    except Exception:
        raise common.WorkBuddyError("refresh", kind="save_failed_retry_save") from None
    saved = dict(account, **fields)
    state.fingerprint = credential_fingerprint(saved)
    state.pending = None
    state.failed = False
    state.retry_at = 0
    state.unknown_valid_until = time.monotonic() + 300 if not fields["expired"] else 0
    return fields["access_token"]


def preserve_snapshot(account_key: str, usage: dict, old_row: dict | None) -> dict:
    """Retain the last successful display value, never its eligibility as fresh."""
    block = usage.get("workbuddy")
    if not isinstance(block, dict):
        return usage
    try:
        old = json.loads((old_row or {}).get("raw_data") or "{}").get("workbuddy") or {}
    except (ValueError, TypeError):
        old = {}
    if old.get("realm") != block.get("realm") or old.get("scope") != block.get("scope"):
        return usage
    result = copy.deepcopy(usage)
    block = result["workbuddy"]
    empty = block.get("status") == "empty" and block.get("complete") is True
    if not empty and not block.get("credits", {}).get("reliable") and old.get("last_success_at"):
        block["last_success_at"] = old["last_success_at"]
        block["last_success_credits"] = copy.deepcopy(old.get("last_success_credits") or old.get("credits") or {})
        block["last_success_packages"] = copy.deepcopy(old.get("last_success_packages") or old.get("packages") or [])
    if empty or block.get("credits", {}).get("reliable"):
        block.pop("last_success_credits", None)
        block.pop("last_success_packages", None)
    if "checkin" in block.get("errors", {}) and old.get("checkin"):
        block["last_success_checkin"] = copy.deepcopy(old.get("last_success_checkin") or old["checkin"])
    return result


def _evaluate_credits_locked(account_key: str, account: dict, usage: dict, *, fresh: bool, expected_generation) -> dict:
    from ... import oauth_manager
    block = usage.get("workbuddy") or {}
    credits = block.get("credits") or {}
    result = {"action": "noop_unknown", "utils": [None] * 6, "any_over": False,
              "hit_windows": [], "disabled_until": account.get("disabled_until")}
    reason = account.get("disabled_reason")
    if reason in {"user", "auth_error"}:
        result["action"] = "noop_" + reason
        return result
    observed = common.number(block.get("fetched_at"))
    current = time.time() * 1000
    if (not block.get("complete") or not credits.get("reliable") or observed is None
            or observed > current + 60000 or current - observed > 15 * 60 * 1000
            or block.get("realm") != common.realm_of(account)
            or credits.get("scope") != ("enterprise" if account.get("enterprise_id") else "personal")):
        return result
    remaining = common.number(credits.get("remaining"))
    if remaining is None:
        return result
    if remaining == 0:
        result.update(any_over=True, hit_windows=["积分"])
        if reason == "quota":
            result["action"] = "still_over_quota"
        else:
            decision = oauth_manager.set_disabled_by_quota(account_key, None)
            result["action"] = "disabled" if (decision or {}).get("state") == "disabled" else "disable_failed"
        return result
    if reason == "quota":
        if not fresh or expected_generation is None:
            return result
        decision = oauth_manager.set_enabled(account_key, True, reason=None,
            expected_disabled_reason="quota", expected_quota_observation_generation=expected_generation)
        result["action"] = "resumed" if (decision or {}).get("state") == "enabled" else "resume_failed"
    else:
        result["action"] = "kept_enabled"
    return result


def evaluate_credits(account_key: str, account: dict, usage: dict, *, fresh: bool, expected_generation) -> dict:
    from ... import config, oauth_manager
    with config.serialized_updates():
        current = oauth_manager.get_account(account_key)
        fingerprint = (usage.get("workbuddy") or {}).get("credential_fingerprint")
        if not current or fingerprint != credential_fingerprint(current):
            return {"action": "noop_stale", "utils": [None] * 6, "any_over": False,
                    "hit_windows": [], "disabled_until": (current or {}).get("disabled_until")}
        return _evaluate_credits_locked(account_key, current, usage, fresh=fresh, expected_generation=expected_generation)


_CREDIT_FIELDS = {"scope", "unit", "capacity", "remaining", "used", "used_percent", "reliable", "cycle_start", "cycle_end"}
_PACKAGE_FIELDS = _CREDIT_FIELDS | {"id", "name", "basis", "expires_at"}
_CHECKIN_FIELDS = {"supported", "observed_at", "active", "today_checked_in", "streak_days", "daily_credit", "today_credit", "total_credits", "activity_name"}


def public_snapshot(account: dict | None, row: dict | None) -> dict:
    account = account or {}
    try:
        block = json.loads((row or {}).get("raw_data") or "{}").get("workbuddy") or {}
    except (ValueError, TypeError, AttributeError):
        block = {}
    if not isinstance(block, dict):
        block = {}
    def subset(value, fields):
        if not isinstance(value, dict):
            return {}
        return {key: copy.deepcopy(value[key]) for key in fields if key in value and isinstance(value[key], (str, int, float, bool, type(None)))}
    result = subset(block, {"realm", "scope", "unit", "source", "status", "complete", "fetched_at", "last_success_at", "payment_type"})
    for key in ("credits", "personal_credits", "enterprise_credits", "last_success_credits"):
        result[key] = subset(block.get(key), _CREDIT_FIELDS)
    for key in ("packages", "last_success_packages"):
        values = block.get(key)
        result[key] = [subset(value, _PACKAGE_FIELDS) for value in values[:5000] if isinstance(value, dict)] if isinstance(values, list) else []
    for key in ("checkin", "last_success_checkin"):
        result[key] = subset(block.get(key), _CHECKIN_FIELDS)
    result["trial"] = subset(block.get("trial"), {"supported", "claim_state", "observed_at"})
    errors = block.get("errors")
    result["errors"] = {key: subset(value, {"code", "http_status", "kind", "retryable", "auth_error"})
                        for key, value in (errors or {}).items() if key in {"credits", "pagination", "enterprise", "checkin", "payment_type"}} if isinstance(errors, dict) else {}
    result.update({key: account.get(key) for key in ("realm", "uid", "enterprise_id", "nickname", "workbuddy_client_profile", "workbuddy_identity_source")})
    result["auto_checkin"] = account.get("workbuddy_auto_checkin") is True
    result["effects_enabled"] = common.effects_allowed()
    return result
