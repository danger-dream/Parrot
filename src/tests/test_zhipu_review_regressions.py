"""Independent-review regressions: real controls/IO boundaries, isolated accounts."""
from __future__ import annotations

import asyncio
import copy
import threading
import time
from datetime import datetime, timezone

import pytest
from src import config, oauth_manager as om, cooldown, state_db
from src.oauth.zhipu import auth, actions, common, catalog
from src.tests.test_zhipu_provider import account_env, credential, window, quota
from src.tests.test_zhipu_management import ctl, reset_env
from src.tests.test_workbuddy_lifecycle import context


def _use(control, key):
    plan = control.plan_zhipu_action(context(), key, "use", reset_type="FIVE_HOUR")
    return control.execute_zhipu_action_now(context(), key, plan["plan_token"])


def _pause(key, windows=("five_hour",)):
    om.set_disabled_by_quota(key, None, observation={"source": "zhipu_quota", "windows": list(windows)})


def test_reset_success_refreshes_cache_and_restores_actual_channel(reset_env):
    from src.channel import registry
    control, key, state, _ = reset_env
    current = copy.deepcopy(om.get_account(key))
    om.mutate_account_if_unchanged(key, current, lambda a: a.update(models=["GLM-5.3"]))
    _pause(key)
    result = _use(control, key)
    assert result["status"] == "succeeded" and result["quota_status"] == "resumed"
    assert om.get_account(key)["enabled"] and om.get_account(key)["disabled_reason"] is None
    assert state_db.quota_load(key)
    registry.rebuild_from_config()
    channel = registry.get_channel("oauth:" + key)
    assert channel.enabled and channel.supports_model("GLM-5.3")
    assert len([url for url, _ in state["calls"] if url.endswith("/use")]) == 1


@pytest.mark.parametrize("case,expected", [("failure", "refresh_failed"), ("missing_week", "noop_unknown"),
    ("over", "still_over_quota"), ("user", "noop_user"), ("new_incident", "account_changed"), ("replaced", "account_changed")])
def test_reset_success_does_not_claim_unsafe_recovery(reset_env, monkeypatch, case, expected):
    control, key, state, _ = reset_env
    _pause(key, ("five_hour", "seven_day") if case == "missing_week" else ("five_hour",))
    if case == "over":
        state["quota"] = quota(window(100))
    original = common.request
    def wire(url, **kw):
        if "/quota/limit" in url:
            if case == "failure":
                raise common.ZhipuError("quota", "network")
            if case == "user":
                om.set_enabled(key, False)
            if case == "new_incident":
                _pause(key)
            if case == "replaced":
                current = copy.deepcopy(om.get_account(key))
                om.mutate_account_if_unchanged(key, current, lambda a: a.update(model_key="replacement.secret"))
        return original(url, **kw)
    monkeypatch.setattr(common, "request", wire)
    result = _use(control, key)
    assert result["status"] == "succeeded" and result["quota_status"] == expected
    assert not om.get_account(key)["enabled"]
    assert len([url for url, _ in state["calls"] if url.endswith("/use")]) == 1


@pytest.mark.parametrize("reason", ["auth_error", "user", "quota"])
def test_key_repair_only_restores_auth_incident(account_env, monkeypatch, reason):
    a = credential("oauth", organization_id="org", project_id="project", models=["GLM-5.3"],
        enabled=False, disabled_reason=reason, disabled_until="2099-01-01T00:00:00Z")
    om.add_account(a); key = om.get_account_key(a)
    monkeypatch.delenv("PARROT_NO_REFRESH", raising=False)
    monkeypatch.setattr(auth, "entitlement", lambda *a, **k: "available")
    monkeypatch.setattr(auth, "resolve_model_key", lambda *a, **k: "repaired.secret")
    assert asyncio.run(om.force_refresh(key)) == "repaired.secret"
    current = om.get_account(key)
    assert current["enabled"] is (reason == "auth_error")
    assert current["disabled_reason"] == (None if reason == "auth_error" else reason)
    if reason == "auth_error":
        assert current["disabled_until"] is None


def test_key_repair_cannot_override_concurrent_manual_disable(account_env, monkeypatch):
    a = credential("oauth", enabled=False, disabled_reason="auth_error")
    om.add_account(a); key = om.get_account_key(a)
    monkeypatch.delenv("PARROT_NO_REFRESH", raising=False)
    monkeypatch.setattr(auth, "entitlement", lambda *a, **k: "available")
    def resolve(*a, **k):
        om.set_enabled(key, False)
        return "repaired.secret"
    monkeypatch.setattr(auth, "resolve_model_key", resolve)
    with pytest.raises(common.ZhipuError, match="stale_generation"):
        asyncio.run(om.force_refresh(key))
    assert om.get_account(key)["disabled_reason"] == "user"


def test_http_reset_exposes_quota_recovery_status(reset_env, tmp_path):
    from fastapi.testclient import TestClient
    from src.management_api.routers.oauth import router
    from src.management_api.routers.oauth_support import get_oauth_control_dependency
    from src.tests.test_management_api_foundation import build_app, bearer, create_session
    control, key, _, _ = reset_env
    _pause(key)
    app, _, _ = build_app(tmp_path)
    app.include_router(router, prefix="/api/management/v1")
    app.dependency_overrides[get_oauth_control_dependency] = lambda: control
    base = "/api/management/v1/oauth/accounts/" + key + "/zhipu"
    with TestClient(app) as client:
        headers = bearer(create_session(client))
        response = client.post(base + "/action-plans", headers=headers,
            json={"action": "use", "resetType": "FIVE_HOUR"})
        assert response.status_code == 200, response.text
        response = client.post(base + "/actions/execute", headers=headers,
            json={"planToken": response.json()["data"]["planToken"]})
        assert response.status_code == 200, response.text
        assert response.json()["data"]["quotaStatus"] == "resumed"


def test_tg_distinguishes_consumption_from_failed_quota_refresh(reset_env, monkeypatch):
    from src.telegram.menus import zhipu_oauth_menu as menu
    from src.telegram import ui, states
    control, key, state, _ = reset_env
    original = common.request
    def wire(url, **kw):
        if "/quota/limit" in url:
            raise common.ZhipuError("quota", "network")
        return original(url, **kw)
    monkeypatch.setattr(common, "request", wire)
    monkeypatch.setattr(menu, "control", control)
    rendered = []
    monkeypatch.setattr(ui, "answer_cb", lambda *a, **k: None)
    monkeypatch.setattr(ui, "edit", lambda *a, **k: rendered.append(a[2]))
    short = ui.register_code(key)
    assert menu.handle_callback(1, 2, "cb", "oa:zh:plan:" + short + ":use:FIVE_HOUR:0")
    nonce = states.get_state(1)["data"]["nonce"]
    assert menu.handle_callback(1, 2, "cb", "oa:zh:confirm:" + nonce)
    assert state["used"] and "重置卡已使用" in rendered[-1]
    assert "恢复尚未确认" in rendered[-1] and "不要再次消费" in rendered[-1]
    states.pop_state(1)


def test_pre_creation_read_failure_allows_new_confirmation(reset_env, monkeypatch):
    control, key, state, _ = reset_env
    original = common.request
    def failing(url, **kw):
        if url.endswith("/api_keys") and kw.get("method", "GET") == "GET":
            raise common.ZhipuError("request", "network")
        return original(url, **kw)
    monkeypatch.setattr(common, "request", failing)
    plan = control.plan_zhipu_action(context(), key, "create_key")
    assert control.execute_zhipu_action_now(context(), key, plan["plan_token"])["status"] == "not_submitted"
    assert not state["keys"]
    monkeypatch.setattr(common, "request", original)
    plan = control.plan_zhipu_action(context(), key, "create_key")
    assert control.execute_zhipu_action_now(context(), key, plan["plan_token"])["status"] == "succeeded"
    assert len([kw for _, kw in state["calls"] if kw.get("method") == "POST"]) == 1


@pytest.mark.parametrize("created", [False, True])
def test_uncertain_creation_only_reconciles_by_reading(reset_env, monkeypatch, created):
    control, key, state, _ = reset_env
    original = common.request
    posts = []
    def failed_response(url, **kw):
        if kw.get("method") == "POST":
            posts.append(url)
            if created:
                original(url, **kw)
            raise common.ZhipuError("request", "network")
        return original(url, **kw)
    monkeypatch.setattr(common, "request", failed_response)
    plan = control.plan_zhipu_action(context(), key, "create_key")
    assert control.execute_zhipu_action_now(context(), key, plan["plan_token"])["status"] == "unknown"
    plan = control.plan_zhipu_action(context(), key, "create_key")
    result = control.execute_zhipu_action_now(context(), key, plan["plan_token"])
    assert result["status"] == ("succeeded" if created else "unknown")
    assert len(posts) == 1
    if created:
        assert om.get_account(key)["model_key"] == "new-id.new-secret"


@pytest.mark.parametrize("action", ["use", "create_key"])
@pytest.mark.parametrize("change", ["disable", "delete", "replace"])
def test_lifecycle_change_during_preflight_prevents_dispatch(reset_env, monkeypatch, action, change):
    control, key, state, _ = reset_env
    plan = control.plan_zhipu_action(context(), key, action, reset_type="FIVE_HOUR")
    entered, release = threading.Event(), threading.Event()
    original = common.request
    def wire(url, **kw):
        if url.endswith("/status" if action == "use" else "/api_keys") and kw.get("method", "GET") == "GET":
            value = original(url, **kw)
            entered.set()
            assert release.wait(5)
            return value
        return original(url, **kw)
    monkeypatch.setattr(common, "request", wire)
    results, errors = [], []
    def run():
        try:
            results.append(control.execute_zhipu_action_now(context(), key, plan["plan_token"]))
        except BaseException as exc:
            errors.append(exc)
    thread = threading.Thread(target=run); thread.start()
    try:
        assert entered.wait(5)
        if change == "disable":
            om.set_enabled(key, False)
        elif change == "delete":
            om.delete_account(key)
        else:
            current = copy.deepcopy(om.get_account(key))
            om.mutate_account_if_unchanged(key, current, lambda a: a.update(access_token="replacement-biz"))
    finally:
        release.set(); thread.join(5)
    assert not thread.is_alive() and not errors
    assert results[0]["status"] == "not_submitted"
    assert not state["used"] and not state["keys"]
    assert not any(kw.get("method") == "POST" for _, kw in state["calls"])


@pytest.mark.parametrize("values,reason", [({"entitlement": "unassigned"}, "entitlement_unassigned"),
    ({"model_key": ""}, "model_key_missing"), ({}, None)])
def test_model_center_eligibility_matches_channel(account_env, values, reason):
    from src.management_control.models.control import ModelCenterControl
    from src.channel.zhipu_oauth_channel import ZhipuOAuthChannel
    a = credential("oauth", models=["GLM-5.3"], **values)
    om.add_account(a); key = om.get_account_key(a)
    saved = om.get_account(key)
    views = ModelCenterControl()._chat_views(config.get())
    source = next(source for view in views for source in view.sources if source.id == key)
    assert source.effective_routable is (reason is None)
    assert source.unavailable_reason == reason
    assert bool(ZhipuOAuthChannel(saved).supports_model("GLM-5.3")) is source.effective_routable


async def test_official_capability_rules_survive_catalog_sync(account_env, monkeypatch):
    directory = {"config": {"providerConfigRules": {"templateRules": [{"templateId": "bigmodel-api", "config": {
        "builtinModelIds": ["GLM-5.2", "GLM-5.3-Flash", "unknown-model"]}}]}, "modelConfigRules": {
        "modelRules": [
            {"modelMatch": ".*glm.*", "config": {"properties": {"contextWindow": 200000, "inputFormat": {
                "supportsText": True, "supportsImage": False}}, "optionSpecs": {"maxOutputTokens": {"max": 64000}}}},
            {"modelMatch": r".*glm-5\.2.*", "config": {"properties": {"contextWindow": 1000000},
                "optionSpecs": {"maxOutputTokens": {"max": 128000}, "reasoningLevel": {"values": ["disabled", "high", "max"]}}}},
            {"modelMatch": r".*glm-5\.3-flash.*", "config": {"properties": {"inputFormat": {
                "supportsImage": True, "supportsVideo": True, "supportsPdf": True}}}}],
        "modelApiRules": [
            {"modelMatch": r".*glm-5\.2.*", "apiTypeMatch": "openai-chat-completions", "config": {
                "optionSpecs": {"maxOutputTokens": {"max": 1}}}},
            {"modelMatch": r".*glm-5\.3-flash.*", "apiTypeMatch": "anthropic-messages", "config": {
                "optionSpecs": {"reasoningLevel": {"values": ["low", "high", "max"], "map": "not executed"}}}}]}}}
    monkeypatch.setattr(catalog.platform, "system", lambda: "Linux")
    monkeypatch.setattr(catalog.platform, "machine", lambda: "x86_64")
    def public_directory(url, **kwargs):
        if url.endswith("/api/anthropic/v1/models"):
            return {"data": [{"id": model} for model in ("GLM-5.2", "GLM-5.3-Flash", "unknown-model")], "hasMore": False}
        if "client/configs" in url:
            from urllib.parse import parse_qs, urlsplit
            assert parse_qs(urlsplit(url).query)["platform"] == ["linux-x86_64"]
            return {"configs": {"builtin_provider_config_json": "https://cdn.example.test/models.json"}}
        return directory
    monkeypatch.setattr(common, "request", public_directory)
    a = credential(); om.add_account(a); key = om.get_account_key(a)
    monkeypatch.setattr(om, "mock_mode_enabled", lambda: False)
    await om.refresh_account_models(key)
    records = {row["id"]: row for row in om.account_model_records(key)}
    assert records["GLM-5.2"]["contextWindow"] == 1000000
    assert records["GLM-5.2"]["maxOutputTokens"] == 128000
    assert records["GLM-5.3-Flash"]["inputModalities"] == ["text", "image", "video", "pdf"]
    assert records["GLM-5.3-Flash"]["reasoningEfforts"] == ["low", "high", "max"]
    assert records["unknown-model"] == {"id": "unknown-model", "name": "unknown-model"}


def test_consumption_requires_destructive_at_plan_and_execution(reset_env):
    from src.management_auth.principal import ManagementPrincipal, AuthMethod, Capability
    from src.management_control.context import ManagementContext
    from src.management_control.errors import ManagementError
    control, key, state, _ = reset_env
    administrator = context()
    limited = ManagementContext("limited", ManagementPrincipal.with_capabilities(
        subject_id=administrator.actor.subject_id, auth_method=AuthMethod.MANAGEMENT_KEY,
        capabilities=[Capability.READ, Capability.WRITE], issued_at=datetime.now(timezone.utc)))
    with pytest.raises(ManagementError):
        control.plan_zhipu_action(limited, key, "use", reset_type="FIVE_HOUR")
    plan = control.plan_zhipu_action(administrator, key, "use", reset_type="FIVE_HOUR")
    with pytest.raises(ManagementError):
        control.execute_zhipu_action_now(limited, key, plan["plan_token"])
    assert not state["used"]
    # Denial must not consume the administrator's valid confirmation plan.
    assert control.execute_zhipu_action_now(administrator, key, plan["plan_token"])["status"] == "succeeded"


async def test_production_quota_recovery_holds_cooldown_lock(account_env, monkeypatch):
    """Correct the review's same-thread reentrant injection with a real thread."""
    a = credential(models=["GLM-5.3"]); om.add_account(a); key = om.get_account_key(a)
    cooldown.init()
    cooldown.record_error("oauth:" + key, "GLM-5.3", '{"error":{"code":"1310","message":"quota"}}',
        cooldown_until=int((time.time() + 500) * 1000))
    data = [quota(window(100))]
    monkeypatch.setattr(common, "request", lambda *a, **k: data[0])
    om.evaluate_and_toggle_by_usage(key, await om.fetch_usage(key))
    data[0] = quota(window(0)); usage = await om.fetch_usage(key)
    original = cooldown.get_state
    acquired = []
    def read(channel, model):
        value = original(channel, model)
        def competing_writer():
            got = cooldown._lock.acquire(blocking=False)
            acquired.append(got)
            if got:
                cooldown._lock.release()
        thread = threading.Thread(target=competing_writer)
        thread.start(); thread.join(5)
        assert not thread.is_alive()
        return value
    monkeypatch.setattr(cooldown, "get_state", read)
    assert om.evaluate_and_toggle_by_usage(key, usage)["action"] == "resumed"
    assert acquired == [False]  # Real concurrent mutation is excluded throughout read/clear.
    cooldown.record_error("oauth:" + key, "GLM-5.3", "new restriction", cooldown_until=int((time.time() + 200) * 1000))
    assert original("oauth:" + key, "GLM-5.3")["last_error_message"] == "new restriction"
