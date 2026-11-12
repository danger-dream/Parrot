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
from ..transform.cc_mimicry import first_party_oauth_headers
from . import claude_usage_queries as queries

PROGRAMS = ("cedar_ember", "juniper_tide")
CEDAR_GRANT_KINDS = ("session", "weekly", "fable", "full")
GRANT_ID_RE = re.compile(r"^[a-z0-9_-]{1,40}$")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
ORG_UUID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
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


def grant_kind(grant: dict) -> str:
    """Classify a cedar_ember grant (aligned with CC v2.1.321 Ye())."""
    if not isinstance(grant, dict):
        return "full"
    if grant.get("gives_back_weekly") is True:
        return "fable"
    gtype = grant.get("type")
    if gtype in {"session", "weekly", "full"}:
        return gtype
    clears = [c for c in (grant.get("clears") or []) if isinstance(c, str)]
    has_five_hour = "five_hour" in clears
    has_other = any(c != "five_hour" for c in clears)
    if has_five_hour and not has_other:
        return "session"
    if has_other and not has_five_hour:
        return "weekly"
    return "full"


def _blocking_reason(grant: dict, blocking) -> str:
    gives_back = grant.get("gives_back_weekly") is True
    items = [x for x in blocking if isinstance(x, str)] if isinstance(blocking, list) else []
    if gives_back and "seven_day" in items:
        return "fable_no_weekly_usage"
    for item in items:
        if item == "five_hour":
            return "blocked_five_hour"
        if item == "seven_day":
            return "blocked_seven_day"
        if item == "seven_day_opus":
            return "blocked_opus"
        if item == "seven_day_sonnet":
            return "blocked_sonnet"
    return "blocked_other"


def grant_eligibility(status: dict, grant: dict) -> str | None:
    """Per-card conservative eligibility check preserving 321 granular reasons."""
    if not isinstance(status, dict):
        return "status_unknown"
    block = status.get("cedar_ember")
    if not isinstance(block, dict):
        return "status_unknown"
    if not isinstance(grant, dict):
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
    unavailable_reason = grant.get("unavailable_reason")
    blocking = grant.get("blocking")
    blocking_list = blocking if isinstance(blocking, list) else []
    if isinstance(unavailable_reason, str) and unavailable_reason.strip():
        reason = unavailable_reason.strip()
        if grant.get("gives_back_weekly") is True and (
            reason == "blocked_seven_day" or "seven_day" in blocking_list
        ):
            return "fable_no_weekly_usage"
        return reason
    if grant.get("use_requires_limit", True) and block.get("at_limit") is not True:
        return "not_limited"
    if blocking_list:
        return _blocking_reason(grant, blocking_list)
    if grant.get("usable_now") is not True:
        exhausted = [x for x in (block.get("exhausted") or []) if isinstance(x, str)]
        clears = [x for x in (grant.get("clears") or []) if isinstance(x, str)]
        gives_back = grant.get("gives_back_weekly") is True
        if block.get("at_limit") is True and exhausted and not any(
            w in clears or (w == "seven_day" and gives_back) for w in exhausted
        ):
            return "cannot_clear"
        return "unavailable"
    return None


def _cedar_priority_kinds(block: dict) -> list[str]:
    exhausted = {x for x in (block.get("exhausted") or []) if isinstance(x, str)}
    preferred: list[str] = []
    if "five_hour" in exhausted:
        preferred.append("session")
    if "seven_day_overage_included" in exhausted:
        preferred.append("fable")
    if "seven_day" in exhausted:
        preferred.append("weekly")
    rest = [k for k in CEDAR_GRANT_KINDS if k not in preferred]
    if "seven_day" in exhausted and "seven_day_overage_included" not in exhausted and "fable" in rest:
        rest = [k for k in rest if k != "fable"] + ["fable"]
    return preferred + rest


def select_cedar_grant(status: dict, grant_id: str | None = None) -> tuple[dict | None, str | None]:
    """Select a usable cedar_ember grant or return the conservative rejection reason."""
    if not isinstance(status, dict):
        return None, "status_unknown"
    observation = (status.get("claude_reset_queries") or {}).get("cedar_ember") or {}
    if observation.get("state") in {"error", "cached"}:
        return None, "query_cooldown" if observation.get("retry_at") else "status_refresh_required"
    block = status.get("cedar_ember")
    if not isinstance(block, dict):
        return None, "status_unknown"
    if block.get("eligible") is not True:
        return None, str(block.get("ineligible_reason") or "ineligible")
    if _future(block.get("cooldown_until")):
        return None, "cooldown"

    grants = [g for g in (block.get("grants") or []) if isinstance(g, dict)]
    if grant_id is not None:
        if not isinstance(grant_id, str) or not GRANT_ID_RE.fullmatch(grant_id):
            return None, "unknown_grant"
        grant = next((g for g in grants if g.get("id") == grant_id), None)
        if not grant:
            return None, "unknown_grant"
        reason = grant_eligibility(status, grant)
        return (grant, None) if reason is None else (None, reason)

    next_id = block.get("next_grant_id")
    next_id_valid = isinstance(next_id, str) and bool(GRANT_ID_RE.fullmatch(next_id))
    next_grant = next((g for g in grants if g.get("id") == next_id), None) if next_id_valid else None
    if next_grant is not None and grant_eligibility(status, next_grant) is None:
        return next_grant, None

    for kind in _cedar_priority_kinds(block):
        for candidate in grants:
            cid = candidate.get("id")
            if not isinstance(cid, str) or not GRANT_ID_RE.fullmatch(cid):
                continue
            if grant_kind(candidate) == kind and grant_eligibility(status, candidate) is None:
                return candidate, None

    if not grants:
        return None, "no_grant"
    if next_grant is not None:
        return None, grant_eligibility(status, next_grant) or "unavailable"
    active_grant = next((g for g in grants if type(g.get("resets_left")) is int and g["resets_left"] > 0), None)
    if active_grant is not None:
        return None, grant_eligibility(status, active_grant) or "unavailable"
    if next_id_valid:
        return None, "unknown_grant"
    return None, grant_eligibility(status, grants[0]) or "unavailable"


def eligibility(status: dict, program: str, grant_id: str | None = None) -> str | None:
    """Conservative local gate; preserve server reasons, never infer entitlement."""
    if program not in PROGRAMS:
        return "unknown_program"
    if program == "cedar_ember":
        _, reason = select_cedar_grant(status, grant_id)
        return reason
    observation = (status.get("claude_reset_queries") or {}).get(program) or {}
    if observation.get("state") in {"error", "cached"}:
        return "query_cooldown" if observation.get("retry_at") else "status_refresh_required"
    block = status.get(program)
    if not isinstance(block, dict):
        return "status_unknown"
    if block.get("eligible") is not True:
        return str(block.get("ineligible_reason") or "ineligible")
    if block.get("arm") != "reset" or block.get("in_experiment") is not True:
        return "control"
    if block.get("available") is not True:
        return str(block.get("ineligible_reason") or "already_used")
    if _future(block.get("next_available_at")):
        return "cooldown"
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
        headers=first_party_oauth_headers(token),
        timeout=12, proxy_purpose="oauth_anthropic", proxy_channel=f"oauth:{account_key}",
    )
    response.raise_for_status()
    value = response.json()
    if not isinstance(value, dict):
        raise ValueError("reset_status_invalid_response")
    return value


def _observed_blocks(value, program):
    names = [program]
    blocks = {name: copy.deepcopy(value.get(name)) if isinstance(value.get(name), dict) else None for name in names}
    blocks["claude_reset_queries"] = {name: {
        "state": "known" if isinstance(value.get(name), dict) else "not_provided" if value.get(name) is None else "invalid_response",
        "fetched_at": int(time.time() * 1000),
    } for name in names}
    return blocks


async def enrich_usage(token, account_key, usage, *, expected):
    """Attach cached opt-in blocks without extra GETs or replacing spend/windows.

    Explicit status queries retain opt-in discovery; ordinary polling must not
    multiply every successful usage read into three upstream requests.
    """
    if not isinstance(usage, dict):
        return usage
    result = copy.deepcopy(usage)
    persisted_raw = None
    # Ordinary reads never fan out into opt-in GETs. Explicit status reads
    # populate this cache; only their status blocks (never spend/windows) attach.
    for program in PROGRAMS:
        if isinstance(result.get(program), dict):
            continue
        value, stamp, until = queries.peek(expected, token, program)
        if value is not None:
            blocks = _observed_blocks(value, program)
            blocks["claude_reset_queries"][program].update(
                state="cached", fetched_at=int(stamp * 1000),
                retry_at=int(until * 1000) if until > time.time() else None)
            if isinstance(blocks.get(program), dict):
                result[program] = blocks[program]
            result.setdefault("claude_reset_queries", {}).update(blocks["claude_reset_queries"])
            continue
        # Plain /api/oauth/usage includes `"cedar_ember": null, "juniper_tide": null`.
        # Preserve any previously persisted card block from state_db so ordinary
        # quota polling never wipes out cards after token rotation or restart.
        if persisted_raw is None:
            try:
                row = state_db.quota_load(account_key) or {}
                parsed = json.loads(row.get("raw_data") or "{}")
                persisted_raw = parsed if isinstance(parsed, dict) else {}
            except (TypeError, ValueError):
                persisted_raw = {}
        if isinstance(persisted_raw.get(program), dict):
            result[program] = copy.deepcopy(persisted_raw[program])
        persisted_queries = persisted_raw.get("claude_reset_queries")
        if isinstance(persisted_queries, dict) and isinstance(persisted_queries.get(program), dict):
            result.setdefault("claude_reset_queries", {}).setdefault(
                program, copy.deepcopy(persisted_queries[program])
            )
    _current(account_key, expected)
    return result


async def status(account_key: str, program: str = "cedar_ember", *, expected=None, fresh=False) -> dict:
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
    async def read():
        def fetch():
            _current(key, expected)
            return _get_status_sync(token, key, program)
        return await asyncio.to_thread(queries.read, expected, token, program, fetch, fresh=fresh)

    try:
        try:
            value, stamp, cached = await read()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 401:
                raise
            _, current_account = _current(key, expected)
            current_token = current_account.get("access_token")
            token = current_token if current_token and current_token != token else await manager.force_refresh(key, expected_state_key=expected)
            value, stamp, cached = await read()
        blocks = _observed_blocks(value, program)
        blocks["claude_reset_queries"][program].update(
            fetched_at=int(stamp * 1000), cached=cached)
    except Exception as exc:
        if fresh:
            raise  # No stale eligibility may authorize a consuming POST.
        value, stamp, until = queries.peek(expected, token, program)
        blocks = _observed_blocks(value, program) if value is not None else {}
        code = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else 429 if isinstance(exc, queries.CoolingDown) else None
        blocks["claude_reset_queries"] = {program: {
            "state": "error", "http_status": code,
            "fetched_at": int(stamp * 1000) if stamp else None,
            "retry_at": int(until * 1000) if until > time.time() else None,
        }}
    key, account = _current(key, expected)
    if account.get("access_token") != token:
        raise ValueError("stale_credentials")
    # Old accounts did not store organization.uuid. Discover it from profile,
    # never account.uuid, and do not use token-save's auth-error recovery side effect.
    if not account.get("claude_organization_uuid") and blocks["claude_reset_queries"][program]["state"] != "error":
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
    with manager.account_generation_guard(expected):
        key, account = _current(key, expected)
        if account.get("access_token") != token:
            raise ValueError("stale_credentials")
        # Do NOT flatten skip_spend or overwrite fresh window evidence.
        state_db.quota_patch_claude_reset_status(key, blocks, expected_state_key=expected)
    return {**blocks, "organization_uuid": account.get("claude_organization_uuid") or "",
            "generation": expected}


def _post_sync(token, key, org, body):
    if _manager().mock_mode_enabled():
        return {"result": "unavailable", "reason": "mock_mode"}
    if not isinstance(org, str) or not ORG_UUID_RE.fullmatch(org):
        raise ValueError("organization_invalid")
    program = body.get("program") if isinstance(body, dict) else None
    if program == "cedar_ember":
        gid = body.get("grant_id")
        rid = body.get("request_id")
        if (tuple(body) != ("program", "grant_id", "request_id")
                or not isinstance(gid, str) or not GRANT_ID_RE.fullmatch(gid)
                or not isinstance(rid, str) or not REQUEST_ID_RE.fullmatch(rid)):
            raise ValueError("malformed_cedar_claim")
    elif program == "juniper_tide":
        if tuple(body) != ("program",):
            raise ValueError("malformed_juniper_claim")
    else:
        raise ValueError("unknown_program")
    response = network.post_sync(
        f"https://api.anthropic.com/api/organizations/{quote(org, safe='')}/reset_rate_limits",
        json=body, headers=first_party_oauth_headers(token),
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


async def _post_with_auth_recovery(token, key, org, body, expected):
    """Only an explicit 401 proves the first POST did not execute."""
    try:
        return await asyncio.to_thread(_post_sync, token, key, org, body)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 401:
            raise
        manager = _manager()
        key, account = _current(key, expected)
        current_token = account.get("access_token")
        if current_token and current_token != token:
            token = current_token
        else:
            token = await manager.force_refresh(key, expected_state_key=expected)
        key, account = _current(key, expected)
        if account.get("disabled_reason") in {"user", "auth_error"}:
            return {"result": "ineligible", "reason": account["disabled_reason"]}
        if account.get("claude_organization_uuid") != org:
            return {"result": "ineligible", "reason": "organization_missing_or_changed"}
        # Same body/request_id, once only. A timeout or a second 401 propagates.
        return await asyncio.to_thread(_post_sync, token, key, org, body)


def _fable_window(flat: dict | None, usage: dict | None) -> tuple[float | None, str | None]:
    if isinstance(flat, dict) and flat.get("fable_util") is not None and not isinstance(flat.get("fable_util"), bool):
        try:
            val = float(flat["fable_util"])
        except (TypeError, ValueError):
            val = None
        if val is not None and math.isfinite(val):
            return max(0.0, min(100.0, val)), flat.get("fable_reset")
    if isinstance(usage, dict):
        block = usage.get("seven_day_overage_included")
        if (
            isinstance(block, dict)
            and block.get("is_active") is not False
            and block.get("utilization") is not None
            and not isinstance(block.get("utilization"), bool)
        ):
            try:
                val = float(block["utilization"])
            except (TypeError, ValueError):
                val = None
            if val is not None and math.isfinite(val):
                return max(0.0, min(100.0, val)), block.get("resets_at")
    return None, None


def _clear_recovered_quota_cooldowns(key, account, previous, flat, usage=None):
    """Clear only old rate-limit errors tied to a recovered account/Fable deadline.

    Caller holds the account generation lock and has compared pre-dispatch
    evidence, so an in-flight new restriction cannot be erased here.
    """
    from .. import cooldown
    manager = _manager()
    if account.get("disabled_reason") not in {"quota", None}:
        return []
    try:
        threshold = float((config.get().get("quotaMonitor") or {}).get("disableThresholdPercent", 95))
    except (TypeError, ValueError):
        threshold = 95.0
    windows = ("five_hour", "seven_day", "thirty_day", "sonnet", "opus")
    if any(flat.get(f"{w}_util") is not None and flat[f"{w}_util"] >= threshold for w in windows):
        return []
    try:
        previous_raw = json.loads(previous.get("raw_data") or "{}")
    except (TypeError, ValueError):
        previous_raw = {}
    prev_fable_util, prev_fable_reset = _fable_window(previous, previous_raw if isinstance(previous_raw, dict) else None)
    curr_fable_util, curr_fable_reset = _fable_window(flat, usage)

    account_deadlines = set()
    if account.get("disabled_reason") == "quota":
        account_deadlines.add(_date(account.get("disabled_until")))
        account_deadlines.update(
            _date(previous.get(f"{w}_reset")) for w in windows
            if previous.get(f"{w}_util") is not None and previous[f"{w}_util"] >= threshold
            and flat.get(f"{w}_util") is not None
        )
    account_deadlines = {int(stamp * 1000) for stamp in account_deadlines if stamp is not None}

    fable_deadlines = set()
    if curr_fable_util is not None and curr_fable_util < threshold:
        if prev_fable_util is not None and prev_fable_util >= threshold:
            fable_deadlines.add(_date(prev_fable_reset))
        fable_deadlines.add(_date(curr_fable_reset))
    fable_deadlines = {int(stamp * 1000) for stamp in fable_deadlines if stamp is not None}

    if not account_deadlines and not fable_deadlines:
        return []

    fable_models = set(manager.claude_fable_models(account))
    cleared = []
    for row in state_db.error_load_all():
        if row.get("channel_key") != f"oauth:{key}":
            continue
        model = row.get("model")
        until = row.get("cooldown_until")
        if model in fable_models:
            if curr_fable_util is None or curr_fable_util >= threshold:
                continue
            if until not in (account_deadlines | fable_deadlines):
                continue
        else:
            if until not in account_deadlines:
                continue
        message = str(row.get("last_error_message") or "")
        if not message.startswith("HTTP 429:"):
            continue
        try:
            error = json.loads(message[len("HTTP 429:"):]).get("error")
        except (ValueError, AttributeError):
            continue
        if not isinstance(error, dict) or error.get("type") != "rate_limit_error":
            continue
        cooldown.clear(f"oauth:{key}", model=model, notify_recovered=False)
        cleared.append(model)
    return cleared


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


def _patch_redeemed_status_in_db(key: str, program: str, grant_id: str | None,
                                  response: dict, expected: str) -> None:
    """Mirror CC 2.1.321 Rr()/Br()/Xr() post-claim state updates into state_db."""
    if not isinstance(response, dict):
        return
    res_kind = response.get("result")
    if res_kind not in {"reset", "already_used", "cooldown", "ineligible"}:
        return
    try:
        row = state_db.quota_load(key) or {}
        raw = json.loads(row.get("raw_data") or "{}")
    except (TypeError, ValueError):
        return
    if not isinstance(raw, dict):
        return
    block = raw.get(program)
    if not isinstance(block, dict):
        return
    updated = copy.deepcopy(block)
    if program == "cedar_ember":
        if isinstance(response.get("weekly_resets_at"), str):
            updated["weekly_resets_at"] = response["weekly_resets_at"]
        if isinstance(response.get("cooldown_until"), str):
            updated["cooldown_until"] = response["cooldown_until"]
        if res_kind in {"reset", "already_used"} and isinstance(grant_id, str) and grant_id:
            resp_left = response.get("resets_left")
            grants = updated.get("grants")
            if isinstance(grants, list):
                for g in grants:
                    if not isinstance(g, dict) or g.get("id") != grant_id:
                        continue
                    old_left = g.get("resets_left")
                    if type(resp_left) is int:
                        new_left = max(0, resp_left)
                    elif res_kind == "already_used":
                        new_left = 0
                    elif type(old_left) is int:
                        new_left = max(0, old_left - 1)
                    else:
                        new_left = 0
                    g["resets_left"] = new_left
                    if new_left <= 0:
                        g["usable_now"] = False
                        if updated.get("next_grant_id") == grant_id:
                            updated["next_grant_id"] = None
    elif program == "juniper_tide":
        if isinstance(response.get("weekly_resets_at"), str):
            updated["weekly_resets_at"] = response["weekly_resets_at"]
        if isinstance(response.get("next_available_at"), str):
            updated["next_available_at"] = response["next_available_at"]
        if res_kind in {"reset", "already_used"}:
            updated["available"] = False
        elif res_kind == "ineligible":
            updated["eligible"] = False
            updated["available"] = False
    state_db.quota_patch_claude_reset_status(key, {program: updated}, expected_state_key=expected)


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
    used_cached_status = False
    try:
        try:
            fresh = await status(key, program, expected=expected, fresh=True)
        except ValueError as exc:
            if str(exc) != "stale_credentials":
                raise
            fresh = await status(key, program, expected=expected, fresh=True)
    except (httpx.TransportError, queries.CoolingDown, httpx.HTTPStatusError) as exc:
        if isinstance(exc, httpx.HTTPStatusError):
            code = exc.response.status_code if exc.response is not None else 0
            if code != 429 and code < 500:
                raise
        _, current_account = _current(key, expected)
        current_token = current_account.get("access_token") or ""
        cached_val, cached_stamp, _ = queries.peek(expected, current_token, program)
        now = time.time()
        if cached_val is None or not cached_stamp or now - cached_stamp >= queries.TTL:
            raise
        blocks = _observed_blocks(cached_val, program)
        blocks["claude_reset_queries"][program].update(
            fetched_at=int(cached_stamp * 1000), cached=True,
        )
        fresh = {
            **blocks,
            "organization_uuid": current_account.get("claude_organization_uuid") or "",
            "generation": expected,
        }
        used_cached_status = True
    key, account = _current(key, expected)
    if not org or account.get("claude_organization_uuid") != org:
        return {"result": "ineligible", "reason": "organization_missing_or_changed"}
    old = state_db.claude_reset_operation_load(journal_key) or {}
    if old.get("operation_id") == operation_id and old.get("status") == "settled":
        return {**old["response"], "replayed": True}
    block = fresh.get(program) or {}
    pending = old.get("status") in {"pending", "unknown"}
    # A newly confirmed, independently identified grant is not a replay of the
    # old uncertain claim. Keep same-grant ambiguity/600s idempotency unchanged.
    if (pending and program == "cedar_ember" and isinstance(grant_id, str)
            and GRANT_ID_RE.fullmatch(grant_id) and grant_id != old.get("grant_id")
            and eligibility(fresh, program, grant_id) is None):
        pending = False
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
    if program == "cedar_ember":
        if not isinstance(grant_id, str) or not GRANT_ID_RE.fullmatch(grant_id):
            return {"result": "ineligible", "reason": "no_grant", "status": fresh}
        selected_grant, reason = select_cedar_grant(fresh, grant_id)
        if reason or selected_grant is None:
            return {"result": "ineligible", "reason": reason or "unknown_grant", "status": fresh}
    else:
        reason = eligibility(fresh, program)
        if reason:
            return {"result": "ineligible", "reason": reason, "status": fresh}
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
    # If we did not already fall back to a fresh (<60s) panel cache due to
    # /api/oauth/usage rate-limiting, check cooldown before dispatching.
    if not used_cached_status:
        queries.check_cooldown(expected)
    # Durable before dispatch: after a crash we cannot claim "not sent".
    state_db.claude_reset_operation_save(journal_key, record)
    try:
        response = await _post_with_auth_recovery(token, key, org, body, expected)
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
        # Authentication recovery was bounded to one explicit 401 above.
        # Other failures never automatically replay a possibly executed POST.
    except Exception as exc:
        response = {"result": "unconfirmed", "reason": "reset_unconfirmed",
                    "error_type": type(exc).__name__}
    queries.invalidate(expected)  # Never use pre-consumption quota to resume routing.
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
    with manager.account_generation_guard(expected) as current:
        if current:
            _patch_redeemed_status_in_db(key, program, grant_id, response, expected)
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
        try:
            previous_raw = json.loads(previous.get("raw_data") or "{}")
        except (TypeError, ValueError):
            previous_raw = {}
        prev_fable_util, _ = _fable_window(previous, previous_raw if isinstance(previous_raw, dict) else None)
        curr_fable_util, _ = _fable_window(flat, usage)
        required = {"five_hour_util", "seven_day_util"}
        required.update(k for k in ("sonnet_util", "opus_util") if previous.get(k) is not None)
        known = all(isinstance(flat.get(k), (int, float)) and math.isfinite(flat[k]) for k in required)
        if prev_fable_util is not None and curr_fable_util is None:
            known = False
        out["usage"] = usage
        if not known:
            # A partial read cannot erase the prior evidence required to
            # recover. Still show any new card status without touching limits.
            state_db.quota_patch_claude_reset_status(key, usage, expected_state_key=expected)
            out["quota_action"] = {"action": "quota_unknown_keep_disabled"}
        else:
            try:
                cleared = _clear_recovered_quota_cooldowns(key, account, previous, flat, usage=usage)
            except Exception as exc:
                out["quota_action"] = {"action": "resume_failed", "error_code": "runtime_state_clear_failed"}
                out["state_error"] = type(exc).__name__
                return out
            state_db.quota_save(key, flat, expected_state_key=expected)
            _patch_redeemed_status_in_db(key, program, grant_id, response, expected)
            out["quota_action"] = manager.evaluate_and_toggle_by_usage(key, usage, fresh=True, expected_state_key=expected)
            out["cleared_models"] = cleared
        # No blanket clear: Fable and unrelated/new restrictions retain their
        # own recovery rules; only confirmed old account-quota errors are gone.
    return out
