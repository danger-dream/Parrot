"""Eight audited failure boundaries, exercised through real saved-account paths."""
from __future__ import annotations

import copy
import json
import threading
import time
from datetime import datetime, timezone

import pytest
from src import oauth_manager as om
from src.management_control.errors import ManagementError
from src.management_control.oauth import OAuthProvider, CreateOAuthAccountCommand, JsonCredential
from src.oauth.zhipu import auth, common, runtime
from src.telegram import bot, states, ui
from src.telegram.menus import zhipu_oauth_menu as zh, oauth_menu as menu
from src.management_control.oauth.menu_bridge import telegram_context
from src.tests.test_zhipu_provider import account_env, credential, quota, window
from src.tests.test_zhipu_onboarding import onboarding
from src.tests.test_zhipu_callback_login import callback_env, callback


def network_failure(*args, **kwargs):
    raise common.ZhipuError("request", "network")


def create(control, entry):
    result = control.create_account(telegram_context(42), CreateOAuthAccountCommand(
        JsonCredential(OAuthProvider.ZHIPU, json.dumps(entry))))
    future = result.post_save["model_sync_future"]
    if future is not None:
        future.result(10)
    else:
        assert result.post_save["initialization_pending"] and not om.get_account(result.account_id).get("model_key")
    return result


@pytest.mark.parametrize("site", ["bigmodel", "zai"])
def test_optional_name_failure_preserves_login_and_received_credentials(callback_env, monkeypatch, site):
    control, observed, profile = callback_env
    profile.pop("name"); profile.pop("email")
    wire = common.request
    def request(url, **kw):
        if url.endswith(("getCustomerInfo", "/api/oauth/userinfo")):
            return network_failure()
        return wire(url, **kw)
    monkeypatch.setattr(common, "request", request)
    flow = control.start_zhipu_callback_login(telegram_context(42), site=site)
    poll = control.submit_zhipu_callback(telegram_context(42), flow.flow_id, flow.flow_secret, callback(flow, site=site))
    assert poll.status == "completed"
    saved = om.get_account(poll.account_id)
    assert saved["subject"] == "callback-user" and saved["access_token"] and saved["zcode_token"]
    assert not saved.get("model_key")
    assert sum(url.endswith("/oauth/token") for url, _ in observed) == 1


def test_required_identity_lookup_retry_does_not_exchange_code_twice(callback_env, monkeypatch):
    control, observed, profile = callback_env
    profile.clear()
    wire = common.request
    attempts = []
    def request(url, **kw):
        if url.endswith("getCustomerInfo"):
            attempts.append(True)
            if len(attempts) == 1:
                return network_failure()
        return wire(url, **kw)
    monkeypatch.setattr(common, "request", request)
    flow = control.start_zhipu_callback_login(telegram_context(42), site="bigmodel")
    with pytest.raises(common.ZhipuError):
        control.submit_zhipu_callback(telegram_context(42), flow.flow_id, flow.flow_secret, callback(flow))
    poll = control.submit_zhipu_callback(telegram_context(42), flow.flow_id, flow.flow_secret, callback(flow))
    assert poll.status == "completed" and om.get_account(poll.account_id)
    assert sum(url.endswith("/oauth/token") for url, _ in observed) == 1


@pytest.mark.parametrize("has_key", [False, True])
def test_oauth_import_without_projects_survives_subscription_outage(onboarding, monkeypatch, has_key):
    control, _, _, _ = onboarding
    monkeypatch.setattr(auth, "entitlement", network_failure)
    result = create(control, credential("oauth", model_key="fixture.secret" if has_key else ""))
    saved = om.get_account(result.account_id)
    assert bool(saved.get("model_key")) == has_key
    assert saved["access_token"] and saved["zcode_token"]
    assert bool(om.account_model_selection(saved)["effective_models"]) == has_key


@pytest.mark.parametrize("failure", ["subscription", "key", "both"])
def test_scope_is_saved_before_enrichment_and_reads_fail_independently(callback_env, monkeypatch, failure):
    control, _, _ = callback_env
    flow = control.start_zhipu_callback_login(telegram_context(42), site="bigmodel")
    subscription, key = auth.entitlement, auth.resolve_model_key
    called = []
    def inspect_saved(account, *, account_key=""):
        current = om.get_account(account_key)
        assert current and current["project_id"] == "project"
        called.append("subscription")
        return network_failure() if failure in {"subscription", "both"} else subscription(account, account_key=account_key)
    def read_key(account, *, account_key="", **kw):
        assert om.get_account(account_key)["project_id"] == "project"
        called.append("key")
        return network_failure() if failure in {"key", "both"} else key(account, account_key=account_key, **kw)
    monkeypatch.setattr(auth, "entitlement", inspect_saved)
    monkeypatch.setattr(auth, "resolve_model_key", read_key)
    control.submit_zhipu_callback(telegram_context(42), flow.flow_id, flow.flow_secret, callback(flow))
    result = control._flows.zhipu.completed_result(telegram_context(42).actor.subject_id, flow.flow_id, flow.flow_secret)
    future = result.post_save["model_sync_future"]
    if future is not None:
        future.result(10)
    else:
        assert result.post_save["initialization_pending"] and not om.get_account(result.account_id).get("model_key")
    saved = om.get_account(result.account_id)
    assert saved["access_token"] and saved["project_id"] == "project"
    assert len(om.list_accounts()) == 1
    assert "key" in called and "subscription" in called
    assert bool(saved.get("model_key")) == (failure == "subscription")
    if failure != "subscription":
        assert saved["management_status"] == "model_key_lookup_failed"


async def test_unknown_subscription_keeps_personal_routes_and_ui_consistent(onboarding, monkeypatch):
    control, _, _, _ = onboarding
    result = create(control, credential("oauth"))
    saved = copy.deepcopy(om.get_account(result.account_id))
    monkeypatch.setattr(auth, "entitlement", lambda *a, **kw: "unknown")
    runtime.fetch_usage_sync(saved, result.account_id)
    current = om.get_account(result.account_id)
    assert current["entitlement"] == "unknown" and current["enabled"]
    assert om.account_model_selection(current)["effective_models"]
    assert menu._status_icon(current) == "✅" and menu._filter_account(current, "available")
    from src.channel.zhipu_oauth_channel import ZhipuOAuthChannel
    request = await ZhipuOAuthChannel(current).build_upstream_request(
        {"messages": [{"role": "user", "content": "fixture"}], "max_tokens": 16}, "GLM-5.3")
    assert request.headers["x-api-key"] == current["model_key"]
    team = dict(current, plan_scope="team", organization_id="org", project_id="project")
    assert not runtime.model_route_available(team)
    team["entitlement"] = "available"
    assert runtime.model_route_available(team)
    for field in ("organization_id", "project_id"):
        assert not runtime.model_route_available(dict(team, **{field: ""}))


@pytest.mark.parametrize("reason", [None, "auth_error", "user", "quota"])
def test_key_repair_survives_subscription_outage_without_lifting_other_pauses(account_env, monkeypatch, reason):
    entry = credential("oauth", organization_id="org", project_id="project", enabled=reason is None, disabled_reason=reason)
    om.add_account(entry); key = om.get_account_key(entry)
    monkeypatch.setattr(auth, "entitlement", network_failure)
    monkeypatch.setattr(auth, "resolve_model_key", lambda *a, **kw: "repaired.fixture")
    assert runtime.refresh_locked(copy.deepcopy(om.get_account(key)), key, True) == "repaired.fixture"
    saved = om.get_account(key)
    assert saved["enabled"] == (reason in (None, "auth_error"))
    assert saved.get("disabled_reason") == (None if reason == "auth_error" else reason)


@pytest.mark.parametrize("status", [0, 429, 500])
def test_transient_poll_error_retries_same_flow_and_saves(callback_env, monkeypatch, status):
    control, observed, _ = callback_env
    wire = common.request
    attempts = []
    def request(url, **kw):
        if "/cli/poll/" in url:
            attempts.append(url)
            if len(attempts) == 1:
                raise common.ZhipuError("request", "network" if not status else "upstream", status=status)
        return wire(url, **kw)
    monkeypatch.setattr(common, "request", request)
    now = [time.time()]
    monkeypatch.setattr(zh.time, "time", lambda: now[0])
    clock = lambda: datetime.fromtimestamp(now[0], timezone.utc)
    control._flows.zhipu.clock = clock
    control._flows.zhipu.store._clock = clock
    monkeypatch.setattr(zh.time, "sleep", lambda delay: now.__setitem__(0, now[0] + delay))
    monkeypatch.setattr(ui, "edit", lambda *a, **kw: None)
    monkeypatch.setattr(ui, "send", lambda *a, **kw: None)
    monkeypatch.setattr(ui, "answer_cb", lambda *a, **kw: None)
    monkeypatch.setattr(zh, "_show_initial_sync", lambda *a: None)
    monkeypatch.setattr(zh, "_start_login_wait", lambda chat, mid, data: zh._wait_for_login(chat, mid, data, data["flow"]))
    zh.handle_callback(42, 500, "cb", "oa:zh:login:bigmodel")
    assert len(attempts) == 2 and len(set(attempts)) == 1
    assert len(om.list_accounts()) == 1 and states.get_state(42) is None
    assert sum(url.endswith("/cli/init") for url, _ in observed) == 1


def test_remote_read_does_not_block_stats_or_overwrite_new_menu(callback_env, monkeypatch):
    control, _, _ = callback_env
    entry = credential("oauth"); om.add_account(entry); key = om.get_account_key(entry)
    entered, release, rendered = threading.Event(), threading.Event(), threading.Event()
    messages, futures = [], []
    def submit(run):
        future = zh._remote_executor.submit(run); futures.append(future); return future
    monkeypatch.setattr(zh, "_submit_remote", submit)
    def slow_read(*a, **kw):
        entered.set(); assert release.wait(5)
        return {"reset": {"available_five_hour_resets": [], "available_week_resets": []}, "actions": []}
    monkeypatch.setattr(control, "get_zhipu", slow_read)
    monkeypatch.setattr(ui, "is_admin", lambda *a: True)
    monkeypatch.setattr(ui, "answer_cb", lambda *a, **kw: None)
    monkeypatch.setattr(ui, "edit", lambda *a, **kw: messages.append(a))
    monkeypatch.setattr(bot.stats_menu, "send_new", lambda *a: rendered.set())
    bot._handle_update({"callback_query": {"id": "cb", "data": "oa:zh:reset:" + ui.register_code(key),
        "message": {"chat": {"id": 42}, "message_id": 500}}})
    try:
        assert entered.wait(3)
        bot._handle_update({"message": {"chat": {"id": 42}, "text": "/stats"}})
        assert rendered.is_set() and not release.is_set()
        before = list(messages)
    finally:
        release.set()
        for future in futures:
            future.result(5)
    assert messages == before and states.get_state(42) is None


def test_project_binding_rejects_other_actor_and_changed_credentials(callback_env, monkeypatch):
    control, _, _ = callback_env
    result = create(control, credential("oauth", model_key=""))
    projects = control.start_zhipu_project_selection(telegram_context(42), result.account_id)
    with pytest.raises(ManagementError):
        control.select_zhipu_project(telegram_context(99), projects.flow_id, projects.flow_secret, organization_id="org", project_id="project")
    saved = copy.deepcopy(om.get_account(result.account_id))
    om.mutate_account_if_unchanged(result.account_id, saved, lambda a: a.update(access_token="replaced-token"))
    with pytest.raises(ManagementError):
        control.select_zhipu_project(telegram_context(42), projects.flow_id, projects.flow_secret, organization_id="org", project_id="project")
    assert len(om.list_accounts()) == 1 and om.get_account(result.account_id)["access_token"] == "replaced-token"


def test_binding_preserves_concurrent_local_preferences(callback_env):
    control, _, _ = callback_env
    result = create(control, credential("oauth", model_key=""))
    projects = control.start_zhipu_project_selection(telegram_context(42), result.account_id)
    before = copy.deepcopy(om.get_account(result.account_id))
    om.mutate_account_if_unchanged(result.account_id, before, lambda a: a.update(
        label="自定义名称", enabled=False, disabled_reason="user", maxConcurrent=3, disabledModels=["GLM-5.3"]))
    bound = control.select_zhipu_project(telegram_context(42), projects.flow_id, projects.flow_secret,
                                          organization_id="org", project_id="project")
    bound.post_save["model_sync_future"].result(10)
    saved = om.get_account(bound.account_id)
    assert len(om.list_accounts()) == 1 and saved["label"] == "自定义名称"
    assert not saved["enabled"] and saved["disabled_reason"] == "user"
    assert saved["maxConcurrent"] == 3 and saved["disabledModels"] == ["GLM-5.3"]


def test_binding_existing_target_preserves_target_settings_and_removes_empty_shell(callback_env):
    control, _, _ = callback_env
    target = create(control, credential("oauth", organization_id="org", project_id="project", label="已有项目", enabled=False, disabled_reason="user"))
    shell = create(control, credential("oauth", model_key=""))
    projects = control.start_zhipu_project_selection(telegram_context(42), shell.account_id)
    bound = control.select_zhipu_project(telegram_context(42), projects.flow_id, projects.flow_secret,
                                          organization_id="org", project_id="project")
    bound.post_save["model_sync_future"].result(10)
    assert bound.account_id == target.account_id and len(om.list_accounts()) == 1
    saved = om.get_account(target.account_id)
    assert saved["label"] == "已有项目" and saved["disabled_reason"] == "user" and saved["model_key"] == "fixture.secret"


def test_saved_account_project_flow_http_round_trip_and_empty_projects(callback_env, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from src.management_api.routers.oauth import router
    from src.management_api.routers.oauth_support import get_oauth_control_dependency
    from src.tests.test_management_api_foundation import build_app, bearer, create_session
    control, _, _ = callback_env
    saved = create(control, credential("oauth", model_key=""))
    app, _, _ = build_app(tmp_path)
    app.include_router(router, prefix="/api/management/v1")
    app.dependency_overrides[get_oauth_control_dependency] = lambda: control
    base = "/api/management/v1/oauth"
    with TestClient(app) as client:
        headers = bearer(create_session(client))
        start = client.post(base + "/accounts/" + saved.account_id + "/zhipu/project-flows", headers=headers)
        assert start.status_code == 200, start.text
        assert start.headers["cache-control"] == "no-store"
        flow = start.json()["data"]
        polled = client.post(base + "/login-flows/" + flow["flowId"] + "/poll", headers=headers, json={"flowSecret": flow["flowSecret"]})
        assert polled.status_code == 200 and polled.json()["data"]["status"] == "select_project", polled.text
        assert all(secret not in polled.text for secret in ("fixture-biz", "fixture-platform"))
        bound = client.post(base + "/login-flows/" + flow["flowId"] + "/zhipu/project", headers=headers,
            json={"flowSecret": flow["flowSecret"], "organizationId": "org", "projectId": "project"})
        assert bound.status_code == 200, bound.text
        key = bound.json()["data"]["accountId"]
        assert om.get_account(key)["model_key"] == "existing-id.existing-secret"
        monkeypatch.setattr(control.backend, "zhipu_project_choices", lambda *a: [])
        start = client.post(base + "/accounts/" + key + "/zhipu/project-flows", headers=headers)
        assert start.status_code == 200
        flow = start.json()["data"]
        polled = client.post(base + "/login-flows/" + flow["flowId"] + "/poll", headers=headers, json={"flowSecret": flow["flowSecret"]})
        assert polled.json()["data"]["accountPreview"]["choices"] == []
        assert om.get_account(key)["model_key"] == "existing-id.existing-secret"
