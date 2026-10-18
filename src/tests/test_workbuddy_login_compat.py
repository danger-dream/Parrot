"""2026-09-08 decisions: real control/channel/ledger, fixture I/O only."""
from __future__ import annotations

import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from unittest.mock import Mock

import pytest

from src import config, oauth_manager as om
from src.channel.workbuddy_oauth_channel import WorkBuddyOAuthChannel, CLIENT_IDENTITY, add_client_identity
from src.management_control import ManagementError, ManagementErrorCode
from src.management_control.oauth import CompleteOAuthLoginCommand, OAuthProvider
from src.oauth.workbuddy import actions, billing, catalog, common
from src.telegram import menu_cache, states, ui
from src.telegram.menus import workbuddy_oauth_menu as wb
from src.tests.test_workbuddy_actions import action_env
from src.tests.test_workbuddy_channel import env as channel_env, wire_json
from src.tests.test_workbuddy_lifecycle import account, context, control
from src.tests.test_workbuddy_provider import credential
from src.tests.test_workbuddy_telegram import tg, click

_REAL_LOGIN_START = wb._start_login_worker


def ready_flow(ctl, key, monkeypatch, **changes):
    incoming = dict(om.get_account(key), **changes)
    poll = Mock(side_effect=lambda payload: payload.update(status="ready", entry=incoming))
    monkeypatch.setattr(ctl.backend, "workbuddy_poll_login", poll)
    return ctl.start_login_flow(context(), OAuthProvider.WORKBUDDY), poll


def test_cancellation_during_vendor_poll_prevents_auto_save(account, control, monkeypatch):
    ctl, _ = control
    entered, release = threading.Event(), threading.Event()
    def poll(payload):
        entered.set()
        assert release.wait(3)
        payload.update(status="ready", entry=dict(om.get_account(account), access_token="cancelled-at"))
    monkeypatch.setattr(ctl.backend, "workbuddy_poll_login", poll)
    flow = ctl.start_login_flow(context(), OAuthProvider.WORKBUDDY)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(ctl.poll_login_flow, context(), flow.flow_id, flow.flow_secret)
        try:
            assert entered.wait(2)
            ctl.cancel_login_flow(context(), flow.flow_id, flow.flow_secret)
        finally:
            release.set()
        with pytest.raises(ManagementError):
            future.result(timeout=3)
    assert ctl.poll_login_flow(context(), flow.flow_id, flow.flow_secret).status == "cancelled"
    assert om.get_account(account)["access_token"] == "fixture-at"


def test_auto_replace_cas_conflict_keeps_new_settings_and_retries_same_candidate(account, control, monkeypatch):
    ctl, _ = control
    flow, poll = ready_flow(ctl, account, monkeypatch, access_token="new-login-at")
    replace = ctl.backend.replace_exact_identity_conditional
    calls = []
    def conflict_once(*args):
        calls.append(1)
        if len(calls) == 1:
            config.update(lambda c: c["oauthAccounts"][0].update(label="并发新备注", maxConcurrent=11))
        return replace(*args)
    monkeypatch.setattr(ctl.backend, "replace_exact_identity_conditional", conflict_once)
    with pytest.raises(ManagementError) as caught:
        ctl.poll_login_flow(context(), flow.flow_id, flow.flow_secret)
    assert caught.value.code == ManagementErrorCode.REVISION_CONFLICT
    assert om.get_account(account)["access_token"] == "fixture-at"
    saved = ctl.poll_login_flow(context(), flow.flow_id, flow.flow_secret)
    assert saved.status == "completed" and saved.save_status == "replaced"
    assert om.get_account(account)["label"] == "并发新备注"
    assert om.get_account(account)["maxConcurrent"] == 11 and poll.call_count == 1


def test_failed_persistence_retries_save_not_vendor_login(account, control, monkeypatch):
    ctl, _ = control
    flow, poll = ready_flow(ctl, account, monkeypatch, uid="new-save-fixture")
    add = ctl.backend.add_account_if_absent
    calls = []
    def fail_once(entry):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("fixture disk failure")
        return add(entry)
    monkeypatch.setattr(ctl.backend, "add_account_if_absent", fail_once)
    with pytest.raises(OSError):
        ctl.poll_login_flow(context(), flow.flow_id, flow.flow_secret)
    assert len(om.list_accounts()) == 1
    result = ctl.poll_login_flow(context(), flow.flow_id, flow.flow_secret)
    assert result.status == "completed" and result.save_status == "created"
    assert len(calls) == 2 and poll.call_count == 1 and len(om.list_accounts()) == 2


def test_auto_save_survives_model_and_usage_sync_failures(account, control, monkeypatch):
    ctl, _ = control
    monkeypatch.setattr(ctl, "_post_save_account_effects", type(ctl)._post_save_account_effects.__get__(ctl))
    monkeypatch.setattr(ctl.backend, "start_account_model_refresh", Mock(side_effect=OSError("fixture model sync failure")))
    async def fail_usage(*args, **kwargs):
        raise OSError("fixture quota sync failure")
    monkeypatch.setattr(ctl.backend, "fetch_usage_snapshot", fail_usage)
    flow, poll = ready_flow(ctl, account, monkeypatch, access_token="saved-despite-sync-errors")
    result = ctl.poll_login_flow(context(), flow.flow_id, flow.flow_secret)
    assert result.status == "completed" and result.save_status == "replaced"
    assert om.get_account(account)["access_token"] == "saved-despite-sync-errors"
    assert ctl.poll_login_flow(context(), flow.flow_id, flow.flow_secret) == result and poll.call_count == 1


def test_completed_flow_is_private_idempotent_after_ttl_and_cannot_resurrect_deleted_account(account, control, monkeypatch):
    ctl, clock = control
    flow, poll = ready_flow(ctl, account, monkeypatch)
    plan = ctl._flows.workbuddy.store.inspect_parts(flow.flow_id, flow.flow_secret,
        actor_subject_id=context().actor.subject_id, kind="workbuddy-login")
    result = ctl.poll_login_flow(context(), flow.flow_id, flow.flow_secret)
    assert plan.payload == {}
    assert "fixture-at" not in repr(ctl._flows.workbuddy._known)
    assert "fixture-rt" not in repr(ctl._flows.workbuddy._known)
    assert flow.flow_secret not in repr(ctl._flows.workbuddy._known)
    with pytest.raises(ManagementError):
        ctl.poll_login_flow(context(), flow.flow_id, "wrong-secret")
    config.update(lambda c: c.update(oauthAccounts=[]))
    clock[0] += timedelta(seconds=301)
    assert ctl.poll_login_flow(context(), flow.flow_id, flow.flow_secret) == result
    saved = ctl.complete_login_flow(context(), flow.flow_id, flow.flow_secret, CompleteOAuthLoginCommand(completed=True))
    assert saved.account_id == result.account_id and not om.list_accounts() and poll.call_count == 1


@pytest.mark.parametrize("change_state", [False, True])
def test_real_login_worker_does_not_block_or_overwrite_other_views(tg, monkeypatch, change_state):
    key, ctl, _, output, *_ = tg
    entered, release = threading.Event(), threading.Event()
    threads = []
    def start(worker):
        thread = _REAL_LOGIN_START(worker)
        threads.append(thread)
        return thread
    monkeypatch.setattr(wb, "_start_login_worker", start)
    monkeypatch.setattr(ctl.backend, "workbuddy_start_login", lambda: {
        "status": "pending", "realm": "cn", "auth_url": "https://www.codebuddy.cn/login", "state": "fixture"})
    def poll(payload):
        entered.set()
        assert release.wait(3)
        payload.update(status="ready", entry=dict(om.get_account(key), access_token="async-fixture-at"))
    monkeypatch.setattr(ctl.backend, "workbuddy_poll_login", poll)
    try:
        wb.handle_callback(42, 900, "cb", "oa:wb:login")
        assert entered.wait(2)  # Handler already returned while vendor is blocked.
        menu_cache.begin_view(42, 900)
        ui.edit(42, 900, "另一页", reply_markup=ui.inline_kb([]))
        if change_state:
            states.set_state(42, "another_input", {"keep": True})
        release.set()
        threads[0].join(timeout=3)
        assert not threads[0].is_alive()
        assert output[-1][0] == "另一页" and om.get_account(key)["access_token"] == "async-fixture-at"
        if change_state:
            assert states.get_state(42)["action"] == "another_input"
        else:
            assert states.get_state(42) is None
        assert not wb._login_jobs
    finally:
        release.set()
        for thread in threads:
            thread.join(timeout=3)


@pytest.mark.parametrize("outcome", ["cancel", "expired"])
def test_login_worker_stops_without_saving_on_cancel_or_expiry(tg, monkeypatch, outcome):
    key, ctl, ledger, output, _, _, clock = tg
    monkeypatch.setattr(ctl.backend, "workbuddy_start_login", lambda: {
        "status": "pending", "realm": "cn", "auth_url": "https://www.codebuddy.cn/login", "state": "fixture"})
    poll = Mock(side_effect=AssertionError("expired/cancelled login must not poll vendor"))
    monkeypatch.setattr(ctl.backend, "workbuddy_poll_login", poll)
    wb.handle_callback(42, 900, "cb", "oa:wb:login")
    if outcome == "cancel":
        click(tg, "取消")
    else:
        clock[0] += timedelta(seconds=301)
    ledger["login_workers"].pop(0)()
    assert states.get_state(42) is None and not wb._login_jobs and poll.call_count == 0
    assert om.get_account(key)["access_token"] == "fixture-at"
    if outcome == "expired":
        assert "过期" in output[-1][0]


def test_login_continues_after_vendor_error_and_telegram_edit_error(tg, monkeypatch):
    key, ctl, ledger, output, _, _, clock = tg
    monkeypatch.setattr(ctl.backend, "workbuddy_start_login", lambda: {
        "status": "pending", "realm": "cn", "auth_url": "https://www.codebuddy.cn/login", "state": "fixture"})
    calls = []
    def poll(payload):
        calls.append(1)
        if len(calls) == 1:
            raise common.WorkBuddyError("request", status=503)
        payload.update(status="ready", entry=dict(om.get_account(key), access_token="retry-fixture-at"))
    monkeypatch.setattr(ctl.backend, "workbuddy_poll_login", poll)
    wb.handle_callback(42, 900, "cb", "oa:wb:login")
    job = wb._login_jobs[42]
    monkeypatch.setattr(job["wake"], "wait", lambda timeout: clock.__setitem__(0, clock[0] + timedelta(seconds=5)))
    edit, edits = ui.edit, []
    def fail_edit_once(*args, **kwargs):
        edits.append(1)
        if len(edits) == 1:
            raise OSError("fixture Telegram delivery failure")
        return edit(*args, **kwargs)
    monkeypatch.setattr(ui, "edit", fail_edit_once)
    ledger["login_workers"].pop(0)()
    assert len(calls) == 2 and "授权已更新" in output[-1][0]
    assert om.get_account(key)["access_token"] == "retry-fixture-at" and not wb._login_jobs


@pytest.mark.parametrize("realm,profile,domain", [
    ("cn", "cli", "www.codebuddy.cn"), ("global", "cli", "www.workbuddy.ai"),
    ("global", "ide", "www.codebuddy.ai"),
])
@pytest.mark.parametrize("kind", ["chat", "account", "anonymous", "refresh", "billing", "enterprise"])
def test_client_headers_align_version_and_keep_credentials_in_their_scope(realm, profile, domain, kind):
    entry = credential(realm=realm, workbuddy_client_profile=profile, domain=domain)
    headers = common.headers(entry, kind)
    name = "CodeBuddyIDE" if profile == "ide" else "CLI"
    version = "4.12.1" if profile == "ide" else "2.63.2" if realm == "global" else "2.164.0"
    assert headers["X-IDE-Type"] == headers["X-IDE-Name"] == name
    assert headers["X-IDE-Version"] == version and version in headers["User-Agent"]
    assert ("X-Refresh-Token" in headers) is (kind == "refresh")
    assert ("Authorization" in headers) is (kind not in {"refresh", "anonymous"})
    if kind == "chat":
        assert "x-codebuddy-request" not in headers
        assert headers.get("X-Agent-Intent") == "craft"


@pytest.mark.parametrize("typed", [False, True])
@pytest.mark.parametrize("realm", ["cn", "global"])
async def test_identity_preserves_chat_roles_content_order_and_effort(channel_env, typed, realm):
    _, ch = channel_env
    if realm == "global":
        entry = dict(ch.account, realm="global", domain="www.codebuddy.ai", workbuddy_client_profile="ide")
        config.update(lambda c: c.update(oauthAccounts=[entry]))
        ch = WorkBuddyOAuthChannel(entry)
    def content(text):
        return [{"type": "text", "text": text}] if typed else text
    original = [
        {"role": "system", "content": content("原系统：你是业务助手，保留全部规则。")},
        {"role": "developer", "content": content("原开发者：只返回 JSON。")},
        {"role": "user", "content": content("业务问题")},
        {"role": "assistant", "content": content("已有回复")},
        {"role": "user", "content": content("继续")},
    ]
    body = {"model": "glm-fixture", "messages": copy.deepcopy(original), "reasoning_effort": "low"}
    snapshot = copy.deepcopy(body)
    result = await ch.build_upstream_request(body, "glm-fixture", ingress_protocol="chat")
    payload = wire_json(result.body)
    assert payload["messages"][0] == {"role": "system", "content": CLIENT_IDENTITY}
    assert payload["messages"][1:] == original
    assert body == snapshot and payload["reasoning_effort"] == "low"
    assert payload["stream"] is True and "X-Refresh-Token" not in result.headers
    assert ("www.codebuddy.ai" if realm == "global" else "copilot.tencent.com") in result.url
    before = copy.deepcopy(payload)
    add_client_identity(payload)
    assert payload == before


@pytest.mark.parametrize("ingress", ["anthropic", "responses"])
async def test_identity_preserves_translated_business_instructions(channel_env, ingress):
    _, ch = channel_env
    if ingress == "anthropic":
        body = {"model": "glm-fixture", "system": "业务系统指令", "max_tokens": 64,
                "messages": [{"role": "user", "content": "业务问题"}]}
    else:
        body = {"model": "glm-fixture", "instructions": "业务系统指令", "input": "业务问题"}
    result = await ch.build_upstream_request(body, "glm-fixture", ingress_protocol=ingress)
    payload = wire_json(result.body)
    assert payload["messages"][0] == {"role": "system", "content": CLIENT_IDENTITY}
    tail = payload["messages"][1:]
    assert "业务系统指令" in json.dumps(tail, ensure_ascii=False)
    assert "业务问题" in json.dumps(tail, ensure_ascii=False)
    assert "reasoning_effort" not in payload


def catalog_data(agent="cli"):
    return {"models": [{"id": "fallback", "reasoning": {"supportedEfforts": ["low", "high"]}},
                       {"id": "unadvertised"}],
            "agents": [{"name": agent, "models": ["fallback"]}]}


@pytest.mark.parametrize("status", [404, 405])
def test_catalog_falls_back_only_to_same_authenticated_profile(monkeypatch, status):
    entry = credential(realm="global", domain="www.workbuddy.ai", workbuddy_client_profile="cli")
    calls = []
    def request(actual, path, **kwargs):
        assert actual is entry and 0 < kwargs["timeout"] <= 7
        assert {key: value for key, value in kwargs.items() if key != "timeout"} == {"method": "GET", "kind": "account", "account_key": "fixture-key"}
        calls.append(path)
        if len(calls) == 1:
            raise common.WorkBuddyError("request", status=status)
        return catalog_data()
    monkeypatch.setattr(common, "request", request)
    records = catalog.fetch_models_sync(entry, account_key="fixture-key", timeout=7)
    assert calls == ["/console/enterprises/personal/models", "/v3/config"]
    assert [r["id"] for r in records] == ["fallback"]
    assert records[0]["reasoningEfforts"] == ["low", "high"]


@pytest.mark.parametrize("error", [common.WorkBuddyError("request", status=s) for s in (401, 403, 429, 500)] + [
    common.WorkBuddyError("request", code=12153), common.WorkBuddyError("request", kind="network")])
def test_catalog_does_not_fallback_on_auth_rate_limit_or_transient_errors(monkeypatch, error):
    request = Mock(side_effect=error)
    monkeypatch.setattr(common, "request", request)
    with pytest.raises(common.WorkBuddyError):
        catalog.fetch_models_sync(credential())
    assert request.call_count == 1


@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("data", [{"enterpriseId": "fixture", "productFeatures": {}},
    {"models": [{"id": "other"}], "agents": [{"name": "cli", "models": ["unknown"]}]}])
def test_empty_or_invalid_catalog_never_becomes_success(monkeypatch, fallback, data):
    request = Mock(side_effect=[common.WorkBuddyError("request", status=404), data] if fallback else [data])
    monkeypatch.setattr(common, "request", request)
    with pytest.raises(common.WorkBuddyError):
        catalog.fetch_models_sync(credential(realm="global", domain="www.workbuddy.ai", workbuddy_client_profile="cli"))
    assert request.call_count == (2 if fallback else 1)


def test_successful_primary_catalog_is_not_merged(monkeypatch):
    request = Mock(return_value=catalog_data())
    monkeypatch.setattr(common, "request", request)
    assert [r["id"] for r in catalog.fetch_models_sync(credential())] == ["fallback"]
    assert request.call_count == 1


@pytest.mark.parametrize("failure", ["server500", "empty", "server503"])
async def test_model_sync_failure_preserves_last_good_catalog_and_selection(account, monkeypatch, failure):
    monkeypatch.setenv("PARROT_NO_REFRESH", "1")
    monkeypatch.delenv("DISABLE_OAUTH_NETWORK_CALLS", raising=False)
    old_catalog = {"schema": 1, "models": [{"id": "last-good", "reasoningEfforts": ["low", "high"]}]}
    config.update(lambda c: (c.setdefault("oauth", {}).update(mockMode=False),
        c["oauthAccounts"][0].update(models=["last-good"], disabledModels=["last-good"],
            account_model_catalog=old_catalog, last_model_sync_at="2026-09-07T00:00:00Z")))
    replies = [{"productFeatures": {}}] if failure == "empty" else [
        common.WorkBuddyError("request", status=500 if failure == "server500" else 503)]
    request = Mock(side_effect=replies)
    monkeypatch.setattr(common, "request", request)
    result = await om._discover_account_models_once(account, timeout_s=10)
    assert result["action"] == "error", result
    saved = om.get_account(account)
    assert saved["models"] == saved["disabledModels"] == ["last-good"]
    assert saved["account_model_catalog"] == old_catalog and saved["last_model_sync_at"] == "2026-09-07T00:00:00Z"
    assert saved["last_model_sync_error"] and request.call_count == len(replies)


def frozen_schedule(monkeypatch):
    now = [datetime(2026, 9, 8, 9, 5, tzinfo=billing.CN_TIMEZONE)]
    class Clock:
        @classmethod
        def now(cls, tz):
            return now[0]
    monkeypatch.setattr(actions, "datetime", Clock)
    monkeypatch.setattr(actions.time, "time", lambda: now[0].timestamp())
    monkeypatch.setenv("PARROT_NO_REFRESH", "1")
    config.update(lambda c: c["oauthAccounts"][0].update(workbuddy_auto_checkin=True))
    return now


def test_evening_has_independent_budget_after_three_morning_query_failures(action_env, monkeypatch):
    key, state = action_env
    now = frozen_schedule(monkeypatch)
    query = Mock(side_effect=common.WorkBuddyError("request", status=503))
    monkeypatch.setattr(billing, "fetch_checkin_sync", query)
    for _ in range(3):
        actions._next_auto_at.clear()
        assert actions.auto_checkin_once()[key]["status"] == "not_submitted"
        now[0] += timedelta(minutes=15)
    actions._next_auto_at.clear()
    assert actions.auto_checkin_once() == {} and state["calls"] == 0 and query.call_count == 3
    now[0] = now[0].replace(hour=21, minute=5)
    actions._next_auto_at[key] = float("inf")  # The previous round cannot delay 21:05.
    monkeypatch.setattr(billing, "fetch_checkin_sync", lambda *a, **k: {"active": True, "today_checked_in": False})
    assert actions.auto_checkin_once()[key]["status"] == "succeeded" and state["calls"] == 1
    assert actions._auto_attempts[key][1] == 1


@pytest.mark.parametrize("morning,evening_checked,expected,calls", [
    ("succeeded", False, "succeeded", 1),
    ("unknown", False, "unknown", 1),
    ("unknown", True, "already_done", 1),
    ("rejected", False, "succeeded", 2),
    ("rejected", True, "already_done", 1),
])
def test_evening_reconciles_uncertainty_and_only_retries_certain_rejection(action_env, monkeypatch, morning, evening_checked, expected, calls):
    key, state = action_env
    now = frozen_schedule(monkeypatch)
    def perform(*a, **k):
        state["calls"] += 1
        return {"status": morning if state["calls"] == 1 else "succeeded"}
    monkeypatch.setattr(billing, "execute_action_sync", perform)
    assert actions.auto_checkin_once()[key]["status"] == morning
    now[0] = now[0].replace(hour=21, minute=5)
    state["checked"] = evening_checked
    assert actions.auto_checkin_once()[key]["status"] == expected and state["calls"] == calls
    # Scheduler restart doesn't grant a new POST for the same durable outcome.
    actions._auto_attempts.clear()
    actions._next_auto_at.clear()
    assert actions.auto_checkin_once()[key]["status"] == expected and state["calls"] == calls


def test_evening_rejection_cannot_be_retried_again_after_restart(action_env, monkeypatch):
    key, state = action_env
    now = frozen_schedule(monkeypatch)
    def reject(*a, **k):
        state["calls"] += 1
        return {"status": "rejected"}
    monkeypatch.setattr(billing, "execute_action_sync", reject)
    assert actions.auto_checkin_once()[key]["status"] == "rejected"
    now[0] = now[0].replace(hour=21, minute=5)
    assert actions.auto_checkin_once()[key]["status"] == "rejected" and state["calls"] == 2
    actions._auto_attempts.clear()
    actions._next_auto_at.clear()
    now[0] += timedelta(minutes=20)
    assert actions.auto_checkin_once()[key]["status"] == "rejected" and state["calls"] == 2


@pytest.mark.parametrize("status", [{"active": False, "today_checked_in": False},
    {"active": True, "today_checked_in": None}, {"active": None, "today_checked_in": False}])
def test_evening_never_submits_on_inactive_or_unknown_status(action_env, monkeypatch, status):
    key, state = action_env
    now = frozen_schedule(monkeypatch)
    now[0] = now[0].replace(hour=21, minute=5)
    monkeypatch.setattr(billing, "fetch_checkin_sync", lambda *a, **k: status)
    assert actions.auto_checkin_once()[key]["status"] == "not_submitted"
    assert state["calls"] == 0 and not actions.history(key)
