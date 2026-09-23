"""Manual Claude quota resets (v2.1.280 cedar_ember / juniper_tide).

No automatic claims. Management one-shot confirmations call redeem(); status
reads never consume. A non-secret durable journal protects ambiguous dispatches
across restarts. Juniper has NO upstream idempotency key and is never replayed.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

from .. import config, network, state_db
from ..transform.cc_mimicry import CLI_USER_AGENT

PROGRAMS = ("cedar_ember", "juniper_tide")
_locks: dict[str, threading.Lock] = {}
_lock_guard = threading.Lock()


def _manager():
    from .. import oauth_manager
    return oauth_manager


def _date(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _future(value):
    stamp = _date(value)
    return stamp is not None and stamp > time.time()


def eligibility(status: dict, program: str, grant_id: str | None = None) -> str | None:
    """Conservative local gate; preserve server reasons, never infer entitlement."""
    if program not in PROGRAMS:
        return "unknown_program"
    block = status.get(program)
    if not isinstance(block, dict):
        return "status_unknown"
    if block.get("eligible") is not True:
        return str(block.get("ineligible_reason") or "ineligible")
    if program == "juniper_tide":
        if block.get("arm") != "reset" or block.get("in_experiment") is not True:
            return "control"
        if block.get("available") is not True:
            return str(block.get("ineligible_reason") or "already_used")
        if _future(block.get("next_available_at")):
            return "cooldown"
        return None
    if _future(block.get("cooldown_until")):
        return "cooldown"
    next_id = block.get("next_grant_id")
    if not isinstance(next_id, str) or not re.fullmatch(r"[a-z0-9_-]{1,40}", next_id):
        return "no_grant"
    if grant_id is not None and grant_id != next_id:
        return "not_next_grant"
    grant = next((g for g in block.get("grants", []) if isinstance(g, dict) and g.get("id") == next_id), None)
    if not grant:
        return "unknown_grant"
    if grant.get("paused"):
        return "paused"
    end = _date(grant.get("ends_at"))
    if end is not None and end <= time.time():
        return "expired"
    if _future(grant.get("starts_at")):
        return "not_started"
    remaining = grant.get("resets_left")
    if type(remaining) is not int or remaining <= 0:
        return "already_used"
    if grant.get("use_requires_limit", True) and block.get("at_limit") is not True:
        return "not_limited"
    if grant.get("blocking"):
        return "blocking"
    if grant.get("usable_now") is not True:
        return "unavailable"
    return None


def _current(account_key, expected):
    manager = _manager()
    with manager.account_generation_guard(expected) as current:
        if not current:
            raise ValueError("stale_generation")
        from .. import channel_state
        key = channel_state.resolve(expected).removeprefix("oauth:")
        account = manager.get_account(key)
        if not account or manager.provider_of(account) != "claude":
            raise ValueError("not_claude")
        return key, copy.deepcopy(account)


def _get_status_sync(token, account_key, program):
    manager = _manager()
    if manager.mock_mode_enabled():
        return manager._mock_usage()
    query = "cedar_ember" if program == "cedar_ember" else "at_wall"
    response = network.get_sync(
        manager.OAUTH_USAGE_URL + f"?{query}=1&skip_spend=1",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "anthropic-beta": "oauth-2025-04-20", "User-Agent": CLI_USER_AGENT},
        timeout=5, proxy_purpose="oauth_anthropic", proxy_channel=f"oauth:{account_key}",
    )
    response.raise_for_status()
    value = response.json()
    if not isinstance(value, dict) or not any(key in value for key in PROGRAMS):
        raise ValueError("reset_status_unavailable")
    return value


async def status(account_key: str, program: str = "cedar_ember", *, expected=None) -> dict:
    if program not in PROGRAMS:
        raise ValueError("unknown_program")
    manager = _manager()
    account = manager.get_account(account_key)
    if not account or manager.provider_of(account) != "claude":
        raise ValueError("not_claude")
    expected = expected or manager.account_state_key(account)
    key, account = _current(account_key, expected)
    if not account.get("generationId"):
        # Legacy entries may only have a process-local generation. Pin that
        # SAME incarnation before a durable intent can ever be dispatched.
        from .. import channel_state
        with manager.account_generation_guard(expected) as current:
            if not current:
                raise ValueError("stale_generation")
            def persist_generation(cfg):
                for row in cfg.get("oauthAccounts", []):
                    if manager.get_account_key(row) == key:
                        row["generationId"] = channel_state.generation_id(expected)
            config.update(persist_generation)
    token = await manager.ensure_valid_token(key, expected_state_key=expected)
    # Old accounts did not store organization.uuid. Discover it from profile,
    # never account.uuid, and do not use token-save's auth-error recovery side effect.
    if not account.get("claude_organization_uuid"):
        profile = await asyncio.to_thread(manager._profile_sync, token, account_key=key)
        organization = (profile.get("organization") or {}).get("uuid")
        with manager.account_generation_guard(expected) as current:
            if not current:
                raise ValueError("stale_generation")
            key, account = _current(key, expected)
            if organization:
                def save(cfg):
                    for row in cfg.get("oauthAccounts", []):
                        if manager.get_account_key(row) == key:
                            row["claude_organization_uuid"] = str(organization)
                config.update(save)
    try:
        value = await asyncio.to_thread(_get_status_sync, token, key, program)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 401:
            raise
        token = await manager.force_refresh(key, expected_state_key=expected)
        value = await asyncio.to_thread(_get_status_sync, token, key, program)
    key, account = _current(key, expected)
    blocks = {name: copy.deepcopy(value[name]) for name in PROGRAMS if name in value}
    # Do NOT flatten this skip_spend response or overwrite fresh window evidence.
    state_db.quota_patch_claude_reset_status(key, blocks, expected_state_key=expected)
    return {**blocks, "organization_uuid": account.get("claude_organization_uuid") or "",
            "generation": expected}


def _post_sync(token, key, org, body):
    if _manager().mock_mode_enabled():
        return {"result": "unavailable", "reason": "mock_mode"}
    response = network.post_sync(
        f"https://api.anthropic.com/api/organizations/{quote(org, safe='')}/reset_rate_limits",
        json=body, headers={"Authorization": f"Bearer {token}",
                            "Content-Type": "application/json", "User-Agent": CLI_USER_AGENT},
        timeout=25, proxy_purpose="oauth_anthropic", proxy_channel=f"oauth:{key}",
    )
    response.raise_for_status()
    value = response.json()
    if not isinstance(value, dict) or value.get("result") not in {
        "reset", "already_used", "not_limited", "cooldown", "ineligible", "unavailable",
    }:
        raise ValueError("reset_unconfirmed")
    return {k: copy.deepcopy(v) for k, v in value.items() if k in {
        "result", "reason", "resets_left", "cleared", "weekly_resets_at",
        "cooldown_until", "next_available_at",
    }}


def _evidence(key, account):
    row = state_db.quota_load(key) or {}
    model_errors = sorted((item for item in state_db.error_load_all()
                           if item.get("channel_key") == f"oauth:{key}"),
                          key=lambda item: str(item.get("model")))
    return (account.get("disabled_reason"), account.get("disabled_until"),
            _manager()._quota_observation_generation(account), row.get("last_passive_update_at"),
            json.dumps(model_errors, sort_keys=True))


def _journal_key(expected, program, organization_uuid):
    return hashlib.sha256(f"{expected}:{program}:{organization_uuid}".encode()).hexdigest()


async def redeem(account_key: str, program: str, *, expected: str,
                 organization_uuid: str, grant_id: str | None, operation_id: str) -> dict:
    """Only called by the management execute-plan gate, never by refresh jobs."""
    if program not in PROGRAMS:
        raise ValueError("unknown_program")
    manager = _manager()
    key, account = _current(account_key, expected)
    if account.get("disabled_reason") in {"user", "auth_error"}:
        return {"result": "ineligible", "reason": account["disabled_reason"]}
    journal_key = _journal_key(expected, program, organization_uuid)
    with _lock_guard:
        lock = _locks.setdefault(journal_key, threading.Lock())
    if not lock.acquire(blocking=False):
        return {"result": "pending", "reason": "operation_in_progress"}
    try:
        return await _redeem_locked(key, program, expected, organization_uuid, grant_id, operation_id, journal_key)
    finally:
        lock.release()


async def _redeem_locked(key, program, expected, org, grant_id, operation_id, journal_key):
    manager = _manager()
    fresh = await status(key, program, expected=expected)
    key, account = _current(key, expected)
    if not org or account.get("claude_organization_uuid") != org:
        return {"result": "ineligible", "reason": "organization_missing_or_changed"}
    old = state_db.claude_reset_operation_load(journal_key) or {}
    if old.get("operation_id") == operation_id and old.get("status") == "settled":
        return {**old["response"], "replayed": True}
    block = fresh.get(program) or {}
    pending = old.get("status") in {"pending", "unknown"}
    retry = False
    if pending:
        grant = next((g for g in block.get("grants", []) if isinstance(g, dict)
                      and g.get("id") == old.get("grant_id")), {})
        remaining = grant.get("resets_left")
        spent_observed = (program == "cedar_ember" and type(remaining) is int
                          and type(old.get("grant_remaining")) is int
                          and remaining < old["grant_remaining"])
        spent_observed = spent_observed or (program == "juniper_tide"
                                           and block.get("available") is False
                                           and block.get("next_available_at") is not None)
        if spent_observed:
            # A status change is evidence that the allowance is no longer
            # available, not proof that OUR timed-out request consumed it.
            observed = {"result": "unconfirmed", "reason": "allowance_changed_not_attributed"}
            state_db.claude_reset_operation_save(journal_key, {**old, "status": "settled", "response": observed})
            return {**observed, "status": fresh,
                    "quota_action": {"action": "unconfirmed_keep_disabled"}}
        if program == "cedar_ember":
            retry = (old.get("grant_id") == grant_id and
                     0 <= time.time() - old["created_at"] < 600)
        else:
            # An ambiguous Juniper dispatch cannot be retried, even if a read
            # still says available. Only a demonstrably new weekly period can
            # permit a new explicitly confirmed operation.
            previous_end = _date(old.get("weekly_resets_at"))
            new_end = _date(block.get("weekly_resets_at"))
            if previous_end and new_end and previous_end <= time.time() < new_end:
                pending = False
        if pending and not retry:
            return {"result": "unconfirmed", "reason": "pending_requires_reconciliation",
                    "status": fresh, "previous_response": old.get("response")}
    reason = eligibility(fresh, program, grant_id)
    if reason:
        return {"result": "ineligible", "reason": reason, "status": fresh}
    if program == "cedar_ember" and grant_id != block.get("next_grant_id"):
        return {"result": "ineligible", "reason": "not_next_grant"}
    record = old if retry else {
        "program": program, "operation_id": operation_id, "created_at": time.time(),
        "grant_id": grant_id, "request_id": str(uuid.uuid4()) if program == "cedar_ember" else None,
        "weekly_resets_at": block.get("weekly_resets_at"),
        "grant_remaining": next((g.get("resets_left") for g in block.get("grants", [])
                                  if isinstance(g, dict) and g.get("id") == grant_id), None),
    }
    record = {**record, "status": "pending"}
    token = await manager.ensure_valid_token(key, expected_state_key=expected)
    key, account = _current(key, expected)
    if account.get("disabled_reason") in {"user", "auth_error"}:
        return {"result": "ineligible", "reason": account["disabled_reason"]}
    if account.get("claude_organization_uuid") != org:
        return {"result": "ineligible", "reason": "organization_missing_or_changed"}
    before = _evidence(key, account)
    body = {"program": program}
    if program == "cedar_ember":
        body.update(grant_id=grant_id, request_id=record["request_id"])
    # Durable before dispatch: after a crash we cannot claim "not sent".
    state_db.claude_reset_operation_save(journal_key, record)
    try:
        response = await asyncio.to_thread(_post_sync, token, key, org, body)
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        response = {"result": "rate_limited" if code == 429 else "auth_error" if code in (401, 403) else "unconfirmed",
                    "reason": f"http_{code}"}
        try:
            detail = exc.response.json()
            if isinstance(detail, dict):
                for name in ("reason", "cooldown_until", "next_available_at"):
                    if isinstance(detail.get(name), str):
                        response[name] = detail[name]
        except ValueError:
            pass
        # No automatic POST retries (especially Juniper). A new product
        # confirmation is required even after an authentication failure.
    except Exception as exc:
        response = {"result": "unconfirmed", "reason": "reset_unconfirmed",
                    "error_type": type(exc).__name__}
    uncertain = response.get("result") == "unconfirmed" or response.get("reason") in {"stamp_indeterminate", "reset_unconfirmed"}
    record.update(status="unknown" if uncertain else "settled", response=response)
    state_db.claude_reset_operation_save(journal_key, record)
    out = {**response, "program": program, "request_id": record.get("request_id")}
    if uncertain:
        try:
            out["status"] = await status(key, program, expected=expected)
        except Exception:
            out["status_error"] = "refresh_failed"
        out["quota_action"] = {"action": "unconfirmed_keep_disabled"}
        return out
    if response.get("result") not in {"reset", "already_used"}:
        return out
    # Success is not proof of recovery. Read ordinary usage WITH spend, and
    # compare the pre-dispatch restriction epoch before any local mutation.
    try:
        key, account = _current(key, expected)
    except ValueError:
        out["quota_action"] = {"action": "noop_stale"}
        return out
    try:
        usage = await manager.fetch_usage(key)
    except Exception:
        out["quota_action"] = {"action": "refresh_failed_keep_disabled"}
        return out
    with manager.account_generation_guard(expected) as current:
        if not current:
            out["quota_action"] = {"action": "noop_stale"}
            return out
        key, account = _current(key, expected)
        if _evidence(key, account) != before:
            out["quota_action"] = {"action": "new_observation_keep_disabled"}
            return out
        if not isinstance(usage, dict):
            out["quota_action"] = {"action": "quota_unknown_keep_disabled"}
            return out
        flat = manager.flatten_usage(usage)
        previous = state_db.quota_load(key) or {}
        required = {"five_hour_util", "seven_day_util"}
        required.update(k for k in ("sonnet_util", "opus_util", "fable_util") if previous.get(k) is not None)
        known = all(isinstance(flat.get(k), (int, float)) and math.isfinite(flat[k]) for k in required)
        out["usage"] = usage
        if not known:
            # A partial read cannot erase the prior evidence required to
            # recover. Still show any new card status without touching limits.
            state_db.quota_patch_claude_reset_status(key, usage, expected_state_key=expected)
            out["quota_action"] = {"action": "quota_unknown_keep_disabled"}
        else:
            state_db.quota_save(key, flat, expected_state_key=expected)
            out["quota_action"] = manager.evaluate_and_toggle_by_usage(key, usage, fresh=True, expected_state_key=expected)
        # Never blanket-clear model cooldowns. Existing scoped evaluators own
        # their own recovery; unrelated/new restrictions remain authoritative.
    return out
