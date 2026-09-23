"""Durable Key/claim actions and explicitly confirmed card consumption."""
from __future__ import annotations

import copy
import hashlib
import json
import time
import uuid
import threading
from contextlib import nullcontext

from . import auth, common as c

RESET_PATH = "/api/v1/coding-plan/reset/"


class ActionCancellation:
    """Serialize local cancellation with the durable pre-dispatch commitment."""
    def __init__(self):
        self.lock = threading.RLock()
        self.cancelled = False
        self.submitted = False

    def cancel(self):
        with self.lock:
            self.cancelled = True
            return not self.submitted

    def check(self):
        with self.lock:
            if self.cancelled and not self.submitted:
                raise c.ZhipuError("action", "cancelled")


def owner(account):
    return hashlib.sha256((c.identity(account) + ":" + str(account.get("generationId"))).encode()).hexdigest()


def status(account, account_key):
    data = c.request(c.PLATFORM_ORIGIN + RESET_PATH + "status", headers=c.reset_headers(account),
                     account_key=account_key, read_attempts=3, stage="reset_status")
    if not isinstance(data, dict):
        raise c.ZhipuError("reset", "invalid_data")
    result = {}
    for name in ("five_hour", "week"):
        field = "available_" + name + "_resets"
        values = data.get(field)
        if not isinstance(values, list):
            raise c.ZhipuError("reset", "invalid_data")
        result[field] = [{"expire_at": item["expire_at"]} for item in values if isinstance(item, dict)
                         and type(item.get("expire_at")) in (int, float) and item["expire_at"] > time.time() * 1000]
        field = "latest_" + name + "_reset_history"
        history = data.get(field)
        result[field] = {"used_at": history["used_at"]} if isinstance(history, dict) and type(history.get("used_at")) in (int, float) else None
    result["has_unread_history"] = data.get("has_unread_history") is True
    return result  # Never POST history/read.


def _known_unsent_creation(record):
    # Legacy error facts are sufficient only for the create POST's pre-HTTP
    # connect/pool timeout, never for a later GET or a reset operation.
    return bool(record and record.get("action") == "create_key" and not record.get("key_id") and
        record.get("status") == "unknown" and record.get("http_status") == 0 and
        record.get("error_kind") == "timeout" and record.get("error_stage") == "request" and
        record.get("timeout_phase") in {"connect", "pool"})


def inspect(account, account_key, action, *, reset_type=None, card_index=0):
    c.biz_headers(account)
    if (account.get("disabled_reason") not in {None, "quota"}
            or (not account.get("enabled", True) and account.get("disabled_reason") != "quota")):
        raise c.ZhipuError("action", "account_paused")
    if action == "create_key":
        if not account.get("organization_id") or not account.get("project_id"):
            raise c.ZhipuError("model_key", "project_required")
        from ... import state_db
        previous = next((row for row in reversed(state_db.zhipu_action_history(owner(account)))
                         if row.get("action") == "create_key"), None)
        resume_only = bool(previous and previous.get("status") in {"unknown", "key_pending"}
                           and not _known_unsent_creation(previous))
        return {"action": action, "organization_id": account["organization_id"],
                "project_id": account["project_id"], "resume_only": resume_only}
    if action not in {"opportunity", "use"}:
        raise c.ZhipuError("action", "unsupported")
    snapshot = status(account, account_key)
    value = {"action": action, "snapshot": snapshot}
    if action == "use":
        if reset_type not in {"FIVE_HOUR", "WEEK"} or type(card_index) is not int or card_index < 0:
            raise c.ZhipuError("reset", "invalid_card")
        name = "five_hour" if reset_type == "FIVE_HOUR" else "week"
        cards = snapshot["available_" + name + "_resets"]
        if card_index >= len(cards):
            raise c.ZhipuError("reset", "card_missing")
        value.update(reset_type=reset_type, card_index=card_index, expire_at=cards[card_index]["expire_at"],
                     history=(snapshot["latest_" + name + "_reset_history"] or {}).get("used_at"))
    return value


def execute(account_key, account, observation, *, actor, cancellation=None, on_stage=None):
    from ... import config, oauth_manager, state_db
    action = observation["action"]
    # One durable key per concrete card (history distinguishes a later card
    # with the same expiry); opportunity/create are scoped to this incarnation.
    scope = {k: observation.get(k) for k in ("action", "reset_type", "card_index", "expire_at", "history")}
    key = hashlib.sha256((owner(account) + json.dumps(scope, sort_keys=True)).encode()).hexdigest()
    with config.serialized_updates():
        if oauth_manager.get_account(account_key) != account:
            raise c.ZhipuError("action", "stale_generation")
        old = state_db.zhipu_action_load(key)
        now = time.time() * 1000
        # A failed read before dispatch is safe to retry on a new initialization
        # or confirmed attempt. An uncertain create may only look up/copy an existing
        # key; an uncertain consumption must never be resubmitted.
        # Older create records recorded the dispatch intent before a connect
        # timeout. That specific error is pre-HTTP; never apply this migration to
        # read/write timeouts, reset operations or a failure while copying a Key.
        old_unsent = _known_unsent_creation(old)
        reconcile_key = bool(old and action == "create_key" and
            old.get("status") in {"unknown", "key_pending"} and not old_unsent)
        if old and action != "opportunity" and old.get("status") != "not_submitted" and not reconcile_key and not old_unsent:
            return public(old)
        if old and action == "opportunity" and now < old.get("next_try_at", float("inf")):
            return public(old)
        intent = {"owner": owner(account), "action": action, "actor": actor, "status": "pending",
                  "idempotency_key": old["idempotency_key"] if old and (old.get("retry_same_key") or reconcile_key
                      or (action == "opportunity" and old.get("status") in {"pending", "unknown"})) else str(uuid.uuid4()),
                  "submitted": reconcile_key,
                  "created_at": now, "next_try_at": now + 600000, "scope": scope}
        if reconcile_key and old.get("key_id"):
            intent["key_id"] = old["key_id"]
        if not state_db.zhipu_action_save(key, intent, expected=old):
            raise c.ZhipuError("action", "in_flight")
    result = dict(intent)
    if cancellation is not None:
        with cancellation.lock:
            cancellation.submitted = bool(intent["submitted"])

    def progress(stage):
        if cancellation is not None:
            cancellation.check()
        if on_stage is not None:
            try:
                on_stage(stage)
            except Exception:
                pass  # A TG rendering failure must not change remote effects.

    def remember_key(key_id, created):
        nonlocal intent
        # Persist only the remote identifier, never its secret. A failed copy or
        # local credential save resumes by reading this Key, not by creating one.
        updated = dict(intent, key_id=key_id)
        result["key_id"] = key_id
        if not state_db.zhipu_action_save(key, updated, expected=intent):
            raise c.ZhipuError("key_copy", "save_failed_result_unknown")
        intent = updated

    def commit_dispatch():
        nonlocal intent
        # Linearization point: mutations before this durable dispatch commit
        # cancel the operation; mutations after it observe an already in-flight
        # operation, which can only be reconciled. Do not hold the global config
        # lock across network IO.
        with cancellation.lock if cancellation is not None else nullcontext():
            if cancellation is not None:
                cancellation.check()
            with config.serialized_updates():
                if oauth_manager.get_account(account_key) != account:
                    raise c.ZhipuError("action", "stale_generation")
                committed = dict(intent, submitted=True)
                if not state_db.zhipu_action_save(key, committed, expected=intent):
                    raise c.ZhipuError("action", "in_flight")
                intent = committed
                result["submitted"] = True
                if cancellation is not None:
                    cancellation.submitted = True
        progress("submitted")

    try:
        if action == "create_key":
            model_key = auth.resolve_model_key(account, account_key=account_key,
                create=not reconcile_key, before_create=commit_dispatch, on_stage=progress,
                known_key_id=intent.get("key_id"), on_key=remember_key)
            if cancellation is not None:
                cancellation.check()
            saved = oauth_manager.mutate_account_if_unchanged(account_key, account,
                lambda current: current.update(model_key=model_key, management_status="ready"))
            result["status"] = "succeeded" if saved.get("status") == "updated" else "created_but_account_changed"
        else:
            progress("checking")
            if action == "use":
                latest = inspect(account, account_key, action, reset_type=observation["reset_type"], card_index=observation["card_index"])
                if any(latest.get(k) != observation.get(k) for k in ("expire_at", "history")):
                    raise c.ZhipuError("reset", "card_changed")
            body = {"idempotency_key": intent["idempotency_key"]}
            if action == "use":
                body["reset_type"] = observation["reset_type"]
            commit_dispatch()
            data = c.request(c.PLATFORM_ORIGIN + RESET_PATH + action, method="POST", body=body,
                             headers=c.reset_headers(account), account_key=account_key, envelope=False,
                             stage="reset_" + action)
            if not isinstance(data, dict):
                raise c.ZhipuError("reset", "invalid_data")
            code = data.get("code")
            result["code"] = code if type(code) is int else None
            if action == "opportunity" and code == 3301:
                result.update(status="not_granted", next_try_at=max(now + 600000, (data.get("data") or {}).get("next_try_at") or 0))
            elif code != 0:
                result.update(status="unknown" if code == 2007 else "rejected", retry_same_key=code == 2007,
                              next_try_at=now + (300000 if code == 2007 else 600000))
            elif action == "opportunity":
                result["status"] = "succeeded" if (data.get("data") or {}).get("granted") is True else "not_granted"
            elif (data.get("data") or {}).get("used") is True:
                result["status"] = "unknown"
                name = "five_hour" if observation["reset_type"] == "FIVE_HOUR" else "week"
                for delay in (0, .25, .75, 1.5):
                    if delay:
                        time.sleep(delay)
                    latest = status(account, account_key)
                    used = (latest["latest_" + name + "_reset_history"] or {}).get("used_at")
                    if used and used != observation.get("history"):
                        result.update(status="succeeded", used_at=used)
                        break
            else:
                result["status"] = "unknown"
    except c.ZhipuError as exc:
        if (not reconcile_key and exc.request_not_sent and
                exc.stage == ("key_create" if action == "create_key" else "reset_" + action)):
            result["submitted"] = False
        result.update(status="key_pending" if action == "create_key" and result.get("key_id") else
                      "not_submitted" if not result["submitted"] else
                      "rejected" if exc.auth_error or exc.status_code == 429 or exc.kind in {"card_changed", "card_missing", "disabled"} else "unknown",
                      http_status=exc.status_code, code=exc.code, error_kind=exc.kind,
                      error_stage=exc.stage, timeout_phase=exc.timeout_phase, **c.error_facts(exc))
        if action == "opportunity":
            transient = exc.kind in {"network", "timeout"} or exc.code == 2007
            result.update(retry_same_key=transient, next_try_at=now + (300000 if transient else 600000))
    except Exception:
        result["status"] = ("key_pending" if action == "create_key" and result.get("key_id") else
                            "unknown" if result["submitted"] else "not_submitted")
        result.update(error_stage="key_save" if result.get("key_id") else "action", error_kind="save_failed")
    if action == "use" and result["status"] == "succeeded":
        from .runtime import refresh_after_reset
        result["quota_status"] = refresh_after_reset(account_key, account)
    result["updated_at"] = time.time() * 1000
    if not state_db.zhipu_action_save(key, result, expected=intent):
        raise c.ZhipuError("action", "save_failed_result_unknown")
    if action == "opportunity" and result["status"] == "succeeded":
        # Only a newly completed grant reaches here. Cached/cooldown results
        # return above and must not resend success notifications.
        from .runtime import fetch_reset_status
        from ... import notifier
        from ...telegram.menus.zhipu_oauth_menu import reset_grant_notification
        after = None
        try:
            current = copy.deepcopy(oauth_manager.get_account(account_key))
            if current and c.fingerprint(current) == c.fingerprint(account):
                after = fetch_reset_status(current, account_key)
        except Exception:
            pass  # A granted card remains granted if the follow-up read fails.
        text, keyboard = reset_grant_notification(account_key, account, observation.get("snapshot"), after)
        notifier.notify(text, reply_markup=keyboard)
    return public(result)


def auto_claim(account_key, account, snapshot):
    """Background opt-in policy for OAuth: attempt grants, never consume cards."""
    if (account.get("credential_mode") != "oauth"
            or account.get("disabled_reason") not in {None, "quota"}
            or (not account.get("enabled", True) and account.get("disabled_reason") != "quota")):
        return None
    c.biz_headers(account)
    # Holding both types already leaves nothing to grant. Holding just one type
    # must not prevent the service from granting the other.
    if all(snapshot.get("available_" + name + "_resets") for name in ("five_hour", "week")):
        return None
    return execute(account_key, account, {"action": "opportunity", "snapshot": snapshot},
                   actor="automatic:reset-claim")


def public(record):
    return {k: copy.deepcopy(v) for k, v in record.items() if k in {"action", "status", "created_at", "updated_at", "next_try_at", "used_at", "http_status", "code", "quota_status", "error_kind", "error_stage", "timeout_phase", "request_not_sent", "network_phase", "target_host", "proxy_route", "fallback_used"}}


def history(account):
    from ... import state_db
    return [public(row) for row in state_db.zhipu_action_history(owner(account))]
