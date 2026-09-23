"""Shared controls, HTTP confirmation gates, TG callbacks and late OAuth results."""
from __future__ import annotations
import copy
import json
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from src import config, oauth_manager as om
from src.management_api.routers.oauth import router
from src.management_api.routers.oauth_support import get_oauth_control_dependency
from src.management_control.oauth import OAuthControl, OAuthProvider, CreateOAuthAccountCommand, JsonCredential
from src.management_control.errors import ManagementError
from src.oauth.zhipu import auth, actions, common
from src.tests.test_management_api_foundation import build_app, bearer, create_session
from src.tests.test_workbuddy_lifecycle import context
from src.tests.test_zhipu_provider import account_env, credential, quota, window


@pytest.fixture
def ctl(account_env, monkeypatch):
    clock = [datetime.now(timezone.utc)]
    control = OAuthControl(clock=lambda: clock[0])
    monkeypatch.setattr(control, "_post_save_account_effects", lambda *a, **k: {})
    def start(*, site):
        return {"site": site, "auth_url": "https://bigmodel.cn/login", "status": "pending", "interval": 1, "expires": time.time()+300}
    monkeypatch.setattr(control.backend, "zhipu_start_login", start)
    return control, clock


@pytest.fixture
def reset_env(ctl, monkeypatch):
    control, clock = ctl
    a = credential("oauth", organization_id="org", project_id="project")
    om.add_account(a)
    key = om.get_account_key(a)
    state = {"calls": [], "used": False, "unknown": False, "keys": [], "quota": quota(window(0))}
    expires = (time.time()+1000)*1000
    def wire(url, **kw):
        state["calls"].append((url, kw))
        if url.endswith("/status"):
            return {"available_five_hour_resets": [{"expire_at": expires}], "available_week_resets": [],
                "latest_five_hour_reset_history": {"used_at": time.time()*1000} if state["used"] else None,
                "latest_week_reset_history": None, "has_unread_history": state["used"]}
        if url.endswith("/use"):
            if state["unknown"]:
                raise common.ZhipuError("reset", "network")
            state["used"] = True
            return {"code": 0, "data": {"used": True}}
        if "/quota/limit" in url:
            return state["quota"]
        if url.endswith("/subscription/list"):
            return [{"productId": "coding-plan", "status": "VALID", "inCurrentPeriod": True}]
        if url.endswith("/mcp/usage"):
            return {"total_usage": {"used": 0, "limit": 10, "remaining": 10}}
        if url.endswith("/opportunity"):
            return {"code": 0, "data": {"granted": True}}
        if "/copy/" in url:
            return {"secretKey": "new-secret"}
        if url.endswith("/api_keys"):
            if kw.get("method") == "POST":
                state["keys"] = [{"apiKey": "new-id", "name": "zcode-api-key"}]
                return state["keys"][0]
            return state["keys"]
        pytest.fail("unexpected endpoint " + url)
    monkeypatch.setattr(common, "request", wire)
    return control, key, state, clock


def test_key_create_real_control_no_email_and_no_secret_public(ctl):
    control, _ = ctl
    entry = {"site": "zai", "credential_mode": "api_key", "model_key": "private.fixture"}
    result = control.create_account(context(), CreateOAuthAccountCommand(JsonCredential(OAuthProvider.ZHIPU, json.dumps(entry))))
    saved = om.get_account(result.account_id)
    assert "email" not in saved and "refresh_token" not in saved and saved["model_key"] == "private.fixture"
    detail = control.get_account(context(), result.account_id)
    assert detail.account.provider == OAuthProvider.ZHIPU and detail.credential_configured
    assert "private.fixture" not in str(detail)
    with pytest.raises(ManagementError):
        control.plan_zhipu_action(context(), result.account_id, "opportunity")


def test_login_select_explicit_project_and_cancel_late(ctl, monkeypatch):
    control, _ = ctl
    a = credential("oauth")
    choice = {"organization_id": "o", "project_id": "p", "plan_scope": "team"}
    entered, release = threading.Event(), threading.Event()
    def poll(payload):
        entered.set(); assert release.wait(3)
        payload.update(status="select_project", credential=a, choices=[choice])
    monkeypatch.setattr(control.backend, "zhipu_poll_login", poll)
    flow = control.start_login_flow(context(), OAuthProvider.ZHIPU, site="bigmodel")
    errors = []
    def run():
        try:
            control.poll_login_flow(context(), flow.flow_id, flow.flow_secret)
        except ManagementError as exc:
            errors.append(exc)
    thread = threading.Thread(target=run); thread.start(); assert entered.wait(3)
    control.cancel_login_flow(context(), flow.flow_id, flow.flow_secret)
    release.set(); thread.join(3)
    assert errors and not om.list_accounts()
    assert control.poll_login_flow(context(), flow.flow_id, flow.flow_secret).status == "cancelled"
    with pytest.raises(ManagementError):
        control.poll_login_flow(context("other"), flow.flow_id, flow.flow_secret)
    flow = control.start_login_flow(context(), OAuthProvider.ZHIPU, site="zai")
    assert control.poll_login_flow(context(), flow.flow_id, flow.flow_secret).status == "select_project"
    monkeypatch.setattr(control.backend, "zhipu_select_project", lambda entry, selected: dict(entry, **selected, entitlement="unassigned"))
    result = control.select_zhipu_project(context(), flow.flow_id, flow.flow_secret, organization_id="o", project_id="p")
    saved = om.get_account(result.account_id)
    assert saved["plan_scope"] == "team" and saved["entitlement"] == "unassigned"
    assert om.account_model_selection(saved)["effective_models"] == []


def test_plan_actor_revision_generation_card_and_no_implicit_effect(reset_env):
    control, key, state, _ = reset_env
    control.get_zhipu(context(), key, reset_status=True)
    assert all(kw.get("method", "GET") == "GET" for _,kw in state["calls"])
    plan = control.plan_zhipu_action(context(), key, "use", reset_type="FIVE_HOUR")
    with pytest.raises(ManagementError):
        control.execute_zhipu_action_now(context("other"), key, plan["plan_token"])
    current = copy.deepcopy(om.get_account(key))
    om.mutate_account_if_unchanged(key, current, lambda a: a.update(maxConcurrent=7))
    with pytest.raises(ManagementError):
        control.execute_zhipu_action_now(context(), key, plan["plan_token"])
    assert all(kw.get("method", "GET") == "GET" for _,kw in state["calls"])
    plan = control.plan_zhipu_action(context(), key, "use", reset_type="FIVE_HOUR")
    assert plan["organization_id"] == "org" and plan["expire_at"]
    result = control.execute_zhipu_action_now(context(), key, plan["plan_token"])
    assert result["status"] == "succeeded"
    with pytest.raises(ManagementError):
        control.execute_zhipu_action_now(context(), key, plan["plan_token"])
    assert len([url for url,_ in state["calls"] if url.endswith("/use")]) == 1
    assert not any("history/read" in url for url,_ in state["calls"])


def test_uncertain_use_is_durable_and_never_resubmitted(reset_env):
    control, key, state, _ = reset_env
    state["unknown"] = True
    plan = control.plan_zhipu_action(context(), key, "use", reset_type="FIVE_HOUR")
    assert control.execute_zhipu_action_now(context(), key, plan["plan_token"])["status"] == "unknown"
    plan = control.plan_zhipu_action(context(), key, "use", reset_type="FIVE_HOUR")
    assert control.execute_zhipu_action_now(context(), key, plan["plan_token"])["status"] == "unknown"
    assert len([url for url,_ in state["calls"] if url.endswith("/use")]) == 1


def test_confirm_creation_only_after_org_project_bound_plan(reset_env):
    control, key, state, _ = reset_env
    current = copy.deepcopy(om.get_account(key))
    om.mutate_account_if_unchanged(key, current, lambda a: a.pop("model_key", None))
    plan = control.plan_zhipu_action(context(), key, "create_key")
    assert not any(kw.get("method") == "POST" for _,kw in state["calls"])
    result = control.execute_zhipu_action_now(context(), key, plan["plan_token"])
    assert result["status"] == "succeeded" and om.get_account(key)["model_key"] == "new-id.new-secret"
    assert len([url for url,kw in state["calls"] if kw.get("method") == "POST"]) == 1
    assert "new-secret" not in str(control.get_zhipu(context(), key))


def test_http_and_tg_confirmation_real_shared_controls(reset_env, tmp_path, monkeypatch):
    control, key, state, _ = reset_env
    app, _, _ = build_app(tmp_path); app.include_router(router, prefix="/api/management/v1")
    app.dependency_overrides[get_oauth_control_dependency] = lambda: control
    base = "/api/management/v1/oauth/accounts/" + key + "/zhipu"
    with TestClient(app) as client:
        headers = bearer(create_session(client))
        response = client.post(base + "/actions/execute", headers=headers, json={"planToken": "invalid.plan"})
        assert response.status_code == 400 and not state["calls"]
        response = client.post(base + "/action-plans", headers=headers, json={"action": "opportunity"})
        assert response.status_code == 200, response.text
        plan = response.json()["data"]
        assert response.headers["cache-control"] == "no-store"
        assert all(kw.get("method", "GET") == "GET" for _,kw in state["calls"])
        response = client.post(base + "/actions/execute", headers=headers, json={"planToken": plan["planToken"]})
        assert response.status_code == 200 and response.json()["data"]["status"] == "succeeded", response.text
    from src.telegram.menus import oauth_menu, zhipu_oauth_menu as menu
    from src.telegram import ui, states
    rendered = []
    monkeypatch.setattr(menu, "control", control)
    monkeypatch.setattr(ui, "answer_cb", lambda *a,**k: None)
    monkeypatch.setattr(ui, "edit", lambda *a, **k: rendered.append((a,k)))
    short = ui.register_code(key)
    assert oauth_menu.handle_callback(1, 2, "cb", "oa:zh:plan:" + short + ":use:FIVE_HOUR:0")
    current = states.get_state(1)
    assert current["action"] == "oa_zh_confirm" and "org / project" in rendered[-1][0][2]
    assert not state["used"]
    assert oauth_menu.handle_callback(1, 2, "cb", "oa:zh:confirm:" + current["data"]["nonce"])
    assert state["used"] and "✅" in rendered[-1][0][2]
    states.pop_state(1)
