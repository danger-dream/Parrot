"""Account generation/CAS lifecycle. Model keys never depend on OAuth JWT expiry."""
from __future__ import annotations

import copy
import json
import math
import time
import uuid
from datetime import datetime, timezone

from . import auth, common as c, signing


def ensure_device_id(account_key, expected_account):
    """Persist one device UUID per account, independent of token/signer refresh."""
    from ... import config, oauth_manager
    if expected_account.get("zcode_device_id"):
        return expected_account["zcode_device_id"]
    with config.serialized_updates():
        current = copy.deepcopy(oauth_manager.get_account(account_key))
        if not current or c.fingerprint(current) != c.fingerprint(expected_account):
            raise c.ZhipuError("device_id", "stale_generation")
        if current.get("zcode_device_id"):
            return current["zcode_device_id"]
        device_id = str(uuid.uuid4())
        result = oauth_manager.mutate_account_if_unchanged(
            account_key, current, lambda value: value.update(zcode_device_id=device_id))
        if result.get("status") != "updated":
            raise c.ZhipuError("device_id", "stale_generation")
        return device_id


def refresh_locked(account, account_key, force):
    from ... import oauth_manager
    if not force and account.get("model_key"):
        return account["model_key"]
    signing.forget(account_key)
    if account.get("credential_mode") == "api_key":
        return account["model_key"]  # No OAuth renewal exists for key mode.
    try:
        key = auth.resolve_model_key(account, account_key=account_key)
    except c.ZhipuError as exc:
        if exc.auth_error:
            oauth_manager.mutate_account_if_unchanged(account_key, account,
                lambda value: value.update(management_status="relogin_required"))
        raise
    patch = {"model_key": key, "management_status": "ready"}
    try:
        patch["entitlement"] = auth.entitlement(account, account_key=account_key)
    except c.ZhipuError as exc:
        if exc.auth_error:
            patch["management_status"] = "relogin_required"
    def repaired(value):
        value.update(patch)
        # Only the authentication incident repaired by this exact snapshot may
        # be lifted. A user/quota pause (including a concurrent one) is retained.
        if value.get("disabled_reason") == "auth_error":
            value.update(enabled=True, disabled_reason=None, disabled_until=None)
    result = oauth_manager.mutate_account_if_unchanged(account_key, account, repaired)
    if result.get("status") != "updated":
        raise c.ZhipuError("model_key", "stale_generation")
    return key


def enrich_account(account_key):
    """Best-effort reads after credential persistence, never a save prerequisite."""
    from ... import config, oauth_manager
    account = copy.deepcopy(oauth_manager.get_account(account_key))
    if not account or account.get("credential_mode") != "oauth":
        return account
    patch, failures = {}, []
    try:
        patch["entitlement"] = auth.entitlement(account, account_key=account_key)
    except c.ZhipuError as exc:
        failures.append(exc)
        if exc.auth_error:
            patch["management_status"] = "relogin_required"
    if not account.get("model_key"):
        if account.get("organization_id") and account.get("project_id"):
            try:
                patch["model_key"] = auth.resolve_model_key(account, account_key=account_key)
                patch.setdefault("management_status", "ready")
            except (c.ZhipuError, ValueError) as exc:
                failures.insert(0, exc)
                patch["management_status"] = ("relogin_required" if getattr(exc, "auth_error", False) else
                    "creation_confirmation_required" if getattr(exc, "kind", "") == "creation_confirmation_required" else "model_key_lookup_failed")
        else:
            patch.setdefault("management_status", "project_required")
    with config.serialized_updates():
        current = copy.deepcopy(oauth_manager.get_account(account_key))
        if current and c.fingerprint(current) == c.fingerprint(account):
            oauth_manager.mutate_account_if_unchanged(account_key, current, lambda value: value.update(patch))
    if failures:
        raise failures[0]  # Saved credentials/results survive; the caller can explain the failed step.
    return oauth_manager.get_account(account_key)


def fetch_reset_status(account, account_key):
    """Cache read-only card status independently from model-Key/quota readiness."""
    from ... import config, oauth_manager
    from . import actions
    def save(mutator):
        with config.serialized_updates():
            current = copy.deepcopy(oauth_manager.get_account(account_key))
            if current and c.fingerprint(current) == c.fingerprint(account):
                oauth_manager.mutate_account_if_unchanged(account_key, current, mutator)
    try:
        data = actions.status(account, account_key)
    except c.ZhipuError as exc:
        def failed(value):
            cached = copy.deepcopy(value.get("zhipu_reset_status") or {})
            cached["error"] = {"stage": exc.stage, "kind": exc.kind, "http_status": exc.status_code,
                               "code": exc.code, "timeout_phase": exc.timeout_phase, **c.error_facts(exc)}
            value["zhipu_reset_status"] = cached
        save(failed)
        raise
    save(lambda value: value.update(zhipu_reset_status={"data": data, "fetched_at": time.time() * 1000}))
    return data


def model_route_available(account):
    if not account.get("model_key"):
        return False
    if account.get("plan_scope") == "team":
        if not account.get("organization_id") or not account.get("project_id"):
            return False
        if account.get("credential_mode") == "oauth":
            return account.get("entitlement") == "available"
    # Unknown personal subscription metadata is not a model-Key rejection.
    return not (account.get("credential_mode") == "oauth" and
                account.get("entitlement") in {"unavailable", "expired", "unassigned"})


def bind_project(account_key, source, entry):
    """Bind an explicit project; preserve settings and retire only an empty login shell."""
    from ... import config, oauth_manager as om
    from ...oauth_ids import account_key as key_of
    from ...management_control.errors import ManagementError, ManagementErrorCode as Code
    entry = auth.normalize_credential(entry)
    target_key = key_of(entry)
    with config.serialized_updates():
        current = copy.deepcopy(om.get_account(account_key))
        if not current or c.fingerprint(current) != c.fingerprint(source):
            raise ManagementError(Code.REVISION_CONFLICT)
        target = copy.deepcopy(om.get_account(target_key))
        # Only credentials/scope from the selected snapshot may be applied;
        # concurrent display/model/enabled preferences remain authoritative.
        fields = {key: copy.deepcopy(entry[key]) for key in (*auth.ACCOUNT_FIELDS, "provider", "type", "access_token", "refresh_token", "email") if key in entry}
        shell = not current.get("organization_id") and not current.get("project_id") and not current.get("model_key")
        if target is not None:
            result = om.replace_exact_identity(target_key, fields, expected_account=target)
            if result.get("status") != "replaced":
                raise ManagementError(Code.REVISION_CONFLICT)
            if shell and account_key != target_key:
                om.delete_account_if_unchanged(account_key, current)
            return target_key, "replaced"
        if not shell:
            result = om.add_account_if_identity_absent(fields)
            if result.get("status") != "added":
                raise ManagementError(Code.IDENTITY_CONFLICT)
            return target_key, "created"
        # Empty saved login -> scoped account, using the existing transactional
        # config/runtime rename path (LB, model policies and generation included).
        before = copy.deepcopy(config.get())
        replacement = dict(current, **fields)
        replacement["label"] = current.get("label") or entry.get("label")
        def mutate(cfg):
            accounts = cfg.get("oauthAccounts", [])
            index = next(i for i, value in enumerate(accounts) if key_of(value) == account_key)
            accounts[index] = copy.deepcopy(replacement)
            om._rename_priority_orders_in_config(cfg, "oauth:" + account_key, "oauth:" + target_key, "anthropic")
        def rollback(cfg):
            cfg["oauthAccounts"] = copy.deepcopy(before.get("oauthAccounts", []))
            cfg["loadBalancing"] = copy.deepcopy(before.get("loadBalancing", {}))
        om._rename_runtime_oauth_identity(account_key, target_key, config_mutator=mutate, rollback_mutator=rollback,
                                          email=current.get("email"))
        return target_key, "updated"


def mark_model_auth_error(channel):
    from ... import config, oauth_manager
    with config.serialized_updates():
        current = oauth_manager.get_account(channel.account_key)
        if not current or c.fingerprint(current) != c.fingerprint(channel.account):
            return
        oauth_manager.mutate_account_if_unchanged(channel.account_key, current,
            lambda account: account.update(enabled=False, disabled_reason="auth_error", management_status="model_key_rejected"))


def number(value):
    if type(value) not in (float, int) or not math.isfinite(value):
        return None
    return value


def normalize_usage(data):
    windows, raw = {}, []
    for row in (data or {}).get("limits") or []:
        if not isinstance(row, dict):
            continue
        # Preserve documented numeric values independently; usage/currentValue
        # are NOT interpreted as used/total and number describes the window.
        item = {key: row[key] for key in ("type", "unit", "number", "usage", "currentValue", "remaining", "percentage", "nextResetTime")
                if key in row and isinstance(row[key], (str, int, float, type(None)))}
        raw.append(item)
        name = None
        if row.get("type") in {"TOKENS_LIMIT", "CREDIT_LIMIT"}:
            if row.get("unit") == 3 and row.get("number") == 5:
                name = "five_hour"
            elif row.get("unit") == 6:
                name = "seven_day"
        pct = number(row.get("percentage"))
        if name and pct is not None and 0 <= pct <= 100:
            reset = number(row.get("nextResetTime"))
            windows[name] = {"utilization": pct, "resets_at": datetime.fromtimestamp(reset / 1000, timezone.utc).isoformat() if reset and 0 < reset < 1e14 else None}
    return windows, raw


def fetch_usage_sync(account, account_key):
    from ... import oauth_manager
    observed = time.time() * 1000
    query = "?type=2" if account.get("plan_scope") == "team" else ""
    headers = {"authorization": account.get("model_key") or ""}
    if query:
        headers.update(c.scope_headers(account))
    data = c.request(c.MODEL_ORIGINS[c.site_of(account)] + "/api/monitor/usage/quota/limit" + query,
                     headers=headers, account_key=account_key, read_attempts=3, stage="quota")
    if not isinstance(data, dict):
        raise c.ZhipuError("quota", "unknown_or_unsupported")
    windows, raw = normalize_usage(data)
    mcp, errors = {}, {}
    if account.get("credential_mode") == "oauth":
        patch = {}
        try:
            patch = {"entitlement": auth.entitlement(account, account_key=account_key), "management_status": "ready"}
        except c.ZhipuError as exc:
            errors["subscription"] = {"kind": exc.kind, "http_status": exc.status_code, "code": exc.code}
            if exc.auth_error:
                patch = {"management_status": "relogin_required"}
        try:
            mcp_data = c.request(c.PLATFORM_ORIGIN + "/api/v1/mcp/usage", account_key=account_key,
                headers={**c.scope_headers(account), "Authorization": "Bearer " + account["zcode_token"],
                         "X-Bigmodel-Authorization": "Bearer " + account["access_token"]},
                read_attempts=3, stage="mcp")
            if isinstance(mcp_data, dict) and isinstance(mcp_data.get("total_usage"), dict):
                mcp = {key: number(mcp_data["total_usage"].get(key)) for key in ("used", "limit", "remaining")}
                mcp["next_refresh_at"] = number(mcp_data.get("next_refresh_at"))
        except c.ZhipuError as exc:
            errors["mcp"] = {"kind": exc.kind, "http_status": exc.status_code, "code": exc.code}
            if exc.auth_error:
                patch["management_status"] = "relogin_required"
        if patch:
            oauth_manager.mutate_account_if_unchanged(account_key, account, lambda value: value.update(patch))
    return {**windows, "zhipu": {"windows": windows, "limits": raw, "fetched_at": observed,
            "status": "known" if windows else "unknown", "scope": c.identity(account), "mcp": mcp, "errors": errors,
            "fingerprint": c.fingerprint(account), "quota_generation": oauth_manager._quota_observation_generation(account)}}


def evaluate(account_key, account, usage, *, fresh, threshold):
    from ... import config, oauth_manager, cooldown, quota_errors
    result = {"action": "noop_unknown", "utils": oauth_manager.extract_utils_percent(usage),
              "any_over": False, "hit_windows": [], "disabled_until": account.get("disabled_until")}
    block = usage.get("zhipu") or {}
    windows = block.get("windows") or {}
    observed = number(block.get("fetched_at"))
    if not fresh or observed is None or not 0 <= time.time() * 1000 - observed <= 900000 or not windows:
        return result
    with config.serialized_updates():
        current = oauth_manager.get_account(account_key)
        if (not current or block.get("fingerprint") != c.fingerprint(current)
                or block.get("scope") != c.identity(current)
                or block.get("quota_generation") != oauth_manager._quota_observation_generation(current)):
            return dict(result, action="noop_stale")
        reason = current.get("disabled_reason")
        if reason not in (None, "quota"):
            return dict(result, action="noop_" + str(reason))
        hit = [name for name, win in windows.items() if win["utilization"] >= threshold]
        if hit:
            previous = current.get("quota_observation") or {}
            known = set(previous.get("windows") or []) if previous.get("source") == "zhipu_quota" else set()
            reset = max((windows[name].get("resets_at") or "" for name in hit), default="") or None
            captured = list(previous.get("cooldowns") or []) if previous.get("source") == "zhipu_quota" else []
            for item in cooldown.active_entries():
                if item.get("channel_key") == "oauth:" + account_key and quota_errors.is_zhipu_1310_message(item.get("last_error_message")):
                    model = item.get("model")
                    if model:
                        captured = [saved for saved in captured if saved["model"] != model]
                        captured.append({"model": model, "state": cooldown.get_state("oauth:" + account_key, model)})
            decision = oauth_manager.set_disabled_by_quota(account_key, reset,
                observation={"source": "zhipu_quota", "windows": sorted(known | set(hit)), "fetched_at": observed, "cooldowns": captured})
            return dict(result, action="disabled" if decision.get("state") == "disabled" else "still_over_quota",
                        any_over=True, hit_windows=hit, disabled_until=decision.get("disabled_until"))
        if reason == "quota":
            previous = current.get("quota_observation") or {}
            required = previous.get("windows") or []
            if previous.get("source") != "zhipu_quota" or not required or any(name not in windows for name in required):
                return result
            # Only quota-owned cooldowns captured in this observation may be
            # removed. Ordinary model/auth/concurrency restrictions stay intact.
            for saved in previous.get("cooldowns") or []:
                state = cooldown.get_state("oauth:" + account_key, saved["model"])
                if state == saved.get("state"):
                    cooldown.clear("oauth:" + account_key, saved["model"], notify_recovered=False)
            decision = oauth_manager.set_enabled(account_key, True, expected_disabled_reason="quota",
                expected_quota_observation_generation=block["quota_generation"])
            return dict(result, action="resumed" if decision.get("state") == "enabled" else "noop_stale")
        return dict(result, action="kept_enabled")


def refresh_after_reset(account_key, account):
    """Refresh quota after confirmed consumption; never retry the consumption.

    Capture the current quota generation before IO and commit only to that
    generation. A concurrent replacement, new quota incident or user pause
    must not be undone by this recovery.
    """
    from ... import config, oauth_manager, state_db
    try:
        current = copy.deepcopy(oauth_manager.get_account(account_key))
        if not current or c.fingerprint(current) != c.fingerprint(account):
            return "account_changed"
        state_key = oauth_manager.account_state_key(current)
        usage = fetch_usage_sync(current, account_key)
        with oauth_manager.account_generation_guard(state_key) as live:
            latest = oauth_manager.get_account(account_key)
            if (not live or not latest or c.fingerprint(latest) != c.fingerprint(current)
                    or oauth_manager._quota_observation_generation(latest)
                    != oauth_manager._quota_observation_generation(current)):
                return "account_changed"
            state_db.quota_save(account_key, oauth_manager.flatten_usage(usage),
                                email=oauth_manager.account_key_to_email(account_key))
            return oauth_manager.evaluate_and_toggle_by_usage(
                account_key, usage, expected_state_key=state_key)["action"]
    except Exception:
        # Consumption succeeded independently of this read/cache/recovery step.
        return "refresh_failed"


def public_snapshot(account, row):
    try:
        block = json.loads((row or {}).get("raw_data") or "{}").get("zhipu") or {}
    except (ValueError, TypeError):
        block = {}
    return {**{key: copy.deepcopy(block.get(key)) for key in ("windows", "limits", "fetched_at", "status", "mcp", "errors")},
            **{key: account.get(key) for key in ("site", "credential_mode", "plan_scope", "organization_id", "project_id", "entitlement", "management_status")},
            "reset": copy.deepcopy(account.get("zhipu_reset_status") or {}),
            "model_key_configured": bool(account.get("model_key")), "oauth_renewal_supported": False}
