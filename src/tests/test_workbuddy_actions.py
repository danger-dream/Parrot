"""Durable WorkBuddy actions: persistence boundaries, confirmation and restart."""
from __future__ import annotations

import copy
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from src import config, oauth_manager as om, state_db
from src.management_control import ManagementError
from src.management_control.oauth import OAuthControl
from src.management_control.operations import OperationStore, OperationStatus
from src.oauth.workbuddy import actions, auth, billing, common, runtime
from src.state_store import StateStore
from src.tests.test_workbuddy_lifecycle import account, context


@pytest.fixture
def action_env(account, monkeypatch, tmp_path):
    state = {"store": StateStore(str(tmp_path / "runtime.json"), str(tmp_path / "durable.json")), "calls": 0, "checked": False}
    state["store"].start()
    state["notifications"] = []
    # The notifier now returns whether enqueue succeeded; this fake accepts it.
    monkeypatch.setattr(actions.notifier, "notify_event", lambda event, text, **k: state["notifications"].append((event, text)) or True)
    monkeypatch.setattr(actions, "_cleanup_after", 0.0)
    actions._auto_attempts.clear()
    monkeypatch.setattr(state_db, "get_store", lambda: state["store"])
    config.update(lambda c: c["oauthAccounts"][0].update(expired=auth.utc_text(time.time() + 7200)))
    monkeypatch.setattr(billing, "fetch_checkin_sync", lambda *a, **k: {"active": True, "today_checked_in": state["checked"]})
    def perform(*a, **k):
        state["calls"] += 1
        return {"status": "succeeded", "awarded_credits": 3}
    monkeypatch.setattr(billing, "execute_action_sync", perform)
    async def usage(*a, **k):
        return {"workbuddy": {"status": "known", "complete": True, "realm": "cn", "fetched_at": time.time()*1000,
            "credential_fingerprint": runtime.credential_fingerprint(om.get_account(account)),
            "credits": {"scope": "personal", "reliable": True, "capacity": 10, "remaining": 3}}}
    monkeypatch.setattr(om, "fetch_usage_snapshot", usage)
    actions._next_auto_at.clear()
    yield account, state
    state["store"].close()
    actions._next_auto_at.clear()


def execute(key, **kwargs):
    return actions.execute(key, "checkin", actor="fixture-admin", **kwargs)


def test_one_post_across_concurrent_clients_and_reimport(action_env):
    key, state = action_env
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: execute(key), range(4)))
    assert state["calls"] == 1
    assert {r["id"] for r in results} == {results[0]["id"]}
    assert all(r["status"] == "succeeded" and r["awarded_credits"] == 3 for r in results)
    incoming = dict(om.get_account(key), access_token="replacement-at", refresh_token="replacement-rt")
    om.replace_exact_identity(key, incoming)
    assert execute(key)["status"] == "succeeded" and state["calls"] == 1
    raw = json.dumps(state["store"].items("workbuddy_actions"))
    assert "fixture-at" not in raw and "replacement-rt" not in raw


def test_intent_write_failure_prevents_post(action_env, monkeypatch):
    key, state = action_env
    def fail(*a, **k):
        raise OSError("fixture disk failure")
    monkeypatch.setattr(state["store"], "write_snapshot", fail)
    with pytest.raises(OSError):
        execute(key)
    assert state["calls"] == 0
    assert actions.history(key) == []


def test_post_result_save_failure_survives_restart_and_only_reconciles(action_env, monkeypatch, tmp_path):
    key, state = action_env
    finish = state_db.workbuddy_action_finish
    calls = []
    def fail_first(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("fixture result persistence failure")
        return finish(*a, **k)
    monkeypatch.setattr(state_db, "workbuddy_action_finish", fail_first)
    with pytest.raises(OSError):
        execute(key)
    assert state["calls"] == 1 and actions.history(key)[0]["status"] == "pending"
    state["store"].close()
    state["store"] = StateStore(str(tmp_path / "runtime.json"), str(tmp_path / "durable.json"))
    state["store"].start()
    assert execute(key)["status"] == "unknown" and state["calls"] == 1
    state["checked"] = True
    result = execute(key)
    assert result["status"] == "already_done" and result["reconciled"]
    assert "awarded_credits" not in result and state["calls"] == 1


def test_network_timeout_unknown_is_not_retry_permission(action_env, monkeypatch):
    key, state = action_env
    def timeout(*a, **k):
        state["calls"] += 1
        raise common.WorkBuddyError("checkin", kind="network")
    monkeypatch.setattr(billing, "execute_action_sync", timeout)
    assert execute(key)["status"] == "unknown"
    assert execute(key, retry_failed=True)["status"] == "unknown"
    assert state["calls"] == 1


def test_certain_rejection_requires_new_explicit_retry(action_env, monkeypatch):
    key, state = action_env
    def reject(*a, **k):
        state["calls"] += 1
        raise common.WorkBuddyError("checkin", code=10001)
    monkeypatch.setattr(billing, "execute_action_sync", reject)
    assert execute(key)["status"] == "rejected"
    assert execute(key)["status"] == "rejected" and state["calls"] == 1
    assert execute(key, retry_failed=True)["attempt"] == 2 and state["calls"] == 2


def test_post_success_balance_failure_not_action_failure(action_env, monkeypatch):
    key, state = action_env
    async def fail(*a, **k):
        raise RuntimeError("fixture quota failure")
    monkeypatch.setattr(om, "fetch_usage_snapshot", fail)
    result = execute(key)
    assert result["status"] == "succeeded" and result["balance_updated"] is False
    assert result["awarded_credits"] == 3
    execute(key)
    assert state["calls"] == 1


def test_unknown_checkin_status_needs_separate_ack(action_env, monkeypatch):
    key, state = action_env
    monkeypatch.setattr(billing, "fetch_checkin_sync", lambda *a, **k: {"active": None, "today_checked_in": None})
    with pytest.raises(common.WorkBuddyError, match="status_unknown"):
        execute(key)
    assert state["calls"] == 0
    assert execute(key, allow_unknown=True)["status"] == "succeeded"


def test_checked_in_read_does_not_send_post_or_invent_reward(action_env):
    key, state = action_env
    state["checked"] = True
    result = execute(key)
    assert result["status"] == "already_done" and "awarded_credits" not in result
    assert state["calls"] == 0


def test_trial_terms_and_global_only_and_lifetime_identity(action_env):
    key, state = action_env
    with pytest.raises(common.WorkBuddyError, match="unsupported_region"):
        actions.execute(key, "claim_trial", actor="fixture")
    account = dict(om.get_account(key), realm="global", domain="www.workbuddy.ai")
    config.update(lambda c: c.update(oauthAccounts=[account]))
    key = om.get_account_key(account)
    with pytest.raises(common.WorkBuddyError, match="free_trial_terms_confirmation_required"):
        actions.execute(key, "claim_trial", actor="fixture")
    assert state["calls"] == 0
    result = actions.execute(key, "claim_trial", actor="fixture", free_trial_confirmed=True)
    assert result["business_date"] == "lifetime" and state["calls"] == 1
    assert actions.execute(key, "claim_trial", actor="fixture", free_trial_confirmed=True)["status"] == "succeeded"
    assert state["calls"] == 1


@pytest.mark.parametrize("change", ["date", "generation", "user", "delete"])
def test_pre_submit_revalidation_blocks_stale_actions(action_env, monkeypatch, change):
    key, state = action_env
    kwargs = {}
    if change == "date":
        kwargs["expected_day"] = "2000-01-01"
    elif change == "generation":
        kwargs["expected_generation"] = "old-generation"
    elif change == "user":
        om.set_enabled(key, False, reason="user")
    elif change == "delete":
        config.update(lambda c: c.update(oauthAccounts=[]))
    with pytest.raises(common.WorkBuddyError):
        execute(key, **kwargs)
    assert state["calls"] == 0 and not actions.history(key)


def test_automatic_optin_time_business_day_and_quota_recovery(action_env, monkeypatch):
    monkeypatch.setenv("PARROT_NO_REFRESH", "1")
    key, state = action_env
    fixed = datetime(2026, 9, 7, 9, 4, tzinfo=billing.CN_TIMEZONE)
    class Clock:
        @classmethod
        def now(cls, tz):
            return fixed
    monkeypatch.setattr(actions, "datetime", Clock)
    assert actions.auto_checkin_once() == {}
    fixed = fixed.replace(minute=5)
    assert actions.auto_checkin_once() == {}  # default off
    config.update(lambda c: c["oauthAccounts"][0].update(workbuddy_auto_checkin=True))
    om.set_disabled_by_quota(key, None)
    assert actions.auto_checkin_once()[key]["status"] == "succeeded"
    assert om.get_account(key)["enabled"]
    actions._next_auto_at.clear()
    actions.auto_checkin_once()
    assert state["calls"] == 1
    fixed += timedelta(days=1)
    actions._next_auto_at.clear()
    assert actions.auto_checkin_once()[key]["status"] == "succeeded" and state["calls"] == 2
    om.set_enabled(key, False, reason="user")
    actions._next_auto_at.clear()
    assert actions.auto_checkin_once() == {}


def test_control_plans_actor_bound_one_shot_settings_cas(action_env):
    key, state = action_env
    ctl = OAuthControl()
    assert ctl.get_workbuddy(context(), key)["actions"] == []
    plan = ctl.plan_workbuddy_action(context(), key, "checkin")
    with pytest.raises(ManagementError):
        ctl.execute_workbuddy_action_now(context("other"), key, plan["plan_token"])
    assert state["calls"] == 0
    result = ctl.execute_workbuddy_action_now(context(), key, plan["plan_token"])
    assert result["status"] == "succeeded"
    with pytest.raises(ManagementError):
        ctl.execute_workbuddy_action_now(context(), key, plan["plan_token"])
    with pytest.raises(ManagementError):
        ctl.update_workbuddy_settings(context(), key, auto_checkin=True, expected_revision="old")
    settings = ctl.update_workbuddy_settings(context(), key, auto_checkin=True)
    assert settings["scheduled_time"] == "09:05" and om.get_account(key)["workbuddy_auto_checkin"] is True


def test_operation_cancel_before_post_and_seal_afterwards(action_env):
    key, state = action_env
    work = []
    class Deferred:
        def submit(self, fn):
            work.append(fn)
    ctl = OAuthControl(executor=Deferred())
    store = OperationStore()
    try:
        plan = ctl.plan_workbuddy_action(context(), key, "checkin")
        operation = ctl.execute_workbuddy_action(context(), key, plan["plan_token"], store)
        store.cancel(context(), operation.id)
        work.pop()()
        assert state["calls"] == 0 and store.get(context(), operation.id).status is OperationStatus.CANCELLED
        op = store.create(context(), kind="fixture", cancellable=True)
        store.mark_running(op.id)
        store.seal_cancellation(op.id)
        with pytest.raises(ManagementError):
            store.cancel(context(), op.id)
        store.succeed(op.id, {"status": "unknown"})
    finally:
        store.close()


def test_cleanup_keeps_unknown_and_trial_facts(action_env):
    key, state = action_env
    execute(key)
    assert state_db.workbuddy_action_cleanup(int(time.time() * 1000) + 1) == 1
    for action, day, status in [("checkin", "old-day", "unknown"), ("claim_trial", "lifetime", "succeeded")]:
        k = actions.action_key(key, action, day)
        state_db.workbuddy_action_begin(k, {"owner": actions.owner(key), "action": action, "business_date": day,
            "attempt_id": k, "created_at": 1, "updated_at": 1})
        state_db.workbuddy_action_finish(k, k, {"status": status})
    assert state_db.workbuddy_action_cleanup(int(time.time()*1000) + 1000) == 0
    assert len(actions.history(key)) == 2


def test_auto_retry_bound_failure_notice_and_silent_success(action_env, monkeypatch):
    key, state = action_env
    fixed = datetime(2026, 9, 7, 9, 5, tzinfo=billing.CN_TIMEZONE)
    class Clock:
        @classmethod
        def now(cls, tz):
            return fixed
    monkeypatch.setattr(actions, "datetime", Clock)
    config.update(lambda c: c["oauthAccounts"][0].update(workbuddy_auto_checkin=True))
    queries = []
    def unknown(*a, **k):
        queries.append(1)
        return {"active": None, "today_checked_in": None}
    monkeypatch.setattr(billing, "fetch_checkin_sync", unknown)
    for _ in range(6):
        actions._next_auto_at.clear()
        actions.auto_checkin_once()
    assert len(queries) == 3 and state["calls"] == 0
    assert len(state["notifications"]) == 1 and state["notifications"][0][0] == "workbuddy_action_failed"
    assert "fixture-at" not in repr(state["notifications"]) and "fixture-rt" not in repr(state["notifications"])
    fixed += timedelta(days=1)
    monkeypatch.setattr(billing, "fetch_checkin_sync", lambda *a, **k: {"active": True, "today_checked_in": False})
    actions._next_auto_at.clear()
    assert actions.auto_checkin_once()[key]["status"] == "succeeded"
    assert state["calls"] == 1 and len(state["notifications"]) == 1  # no daily success spam


def test_auto_quota_recovery_notice_and_scheduled_retention(action_env, monkeypatch):
    key, state = action_env
    fixed = datetime(2026, 9, 7, 9, 5, tzinfo=billing.CN_TIMEZONE)
    class Clock:
        @classmethod
        def now(cls, tz):
            return fixed
    monkeypatch.setattr(actions, "datetime", Clock)
    with monkeypatch.context() as scoped:
        scoped.setattr(state_db, "now_ms", lambda: 1)
        for action, day, status in [("checkin", "old-success", "succeeded"), ("checkin", "old-unknown", "unknown"), ("claim_trial", "lifetime", "succeeded")]:
            k = actions.action_key(key, action, day)
            state_db.workbuddy_action_begin(k, {"owner": actions.owner(key), "action": action, "business_date": day,
                "attempt_id": k, "created_at": 1, "updated_at": 1})
            state_db.workbuddy_action_finish(k, k, {"status": status})
    config.update(lambda c: c["oauthAccounts"][0].update(workbuddy_auto_checkin=True))
    om.set_disabled_by_quota(key, None)
    assert actions.auto_checkin_once()[key]["quota_action"] == "resumed"
    assert state["notifications"][0][0] == "quota_resumed"
    records = actions.history(key)
    assert {r["business_date"] for r in records} == {"old-unknown", "lifetime", "2026-09-07"}
    assert state["calls"] == 1
