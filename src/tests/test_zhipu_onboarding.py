"""New accounts initialize quota, catalog and metadata without a refresh click."""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import Future

import pytest
from src import config, model_metadata, model_pricing, oauth_manager as om, state_db
from src.channel import registry
from src.management_control.errors import ManagementError
from src.management_control.oauth import OAuthControl, OAuthProvider, CreateOAuthAccountCommand, JsonCredential
from src.oauth.zhipu import common
from src.telegram import states, ui
from src.telegram.menus import oauth_menu as menu, zhipu_oauth_menu as zh
from src.tests.test_workbuddy_lifecycle import context
from src.tests.test_zhipu_provider import account_env, credential, quota, window


@pytest.fixture
def onboarding(account_env, monkeypatch):
    control = OAuthControl()
    monkeypatch.setattr(config, "_reload_callbacks", [])
    monkeypatch.setattr(registry, "_channels", {})
    registry.install_config_reload_hook()
    # account_env clears config, not the isolated suite's quota cache. Each
    # onboarding case must start without another case's same-identity snapshot.
    for row in state_db.quota_load_all():
        if row["account_key"].startswith("zhipu:"):
            state_db.quota_delete(row["account_key"])
    calls, downloads = [], []
    failure = {"quota": False, "models": False, "metadata": False}
    config.update(lambda c: c.update(
        pricing={**c.get("pricing", {}), "enabled": True, "autoUpdate": True},
        modelBindings={"defaults": {}, "scoped": {}},
        modelMetadataOverrides={"defaults": {"unrelated-model": {"contextWindow": 12345}}},
    ))
    model_pricing.initialize()
    monkeypatch.setattr(om, "mock_mode_enabled", lambda: False)
    monkeypatch.delenv("PARROT_NO_REFRESH", raising=False)

    def wire(url, **kwargs):
        assert kwargs.get("method", "GET") == "GET"
        calls.append(url)
        if "/quota/limit" in url:
            if failure["quota"]:
                raise common.ZhipuError("quota", "network")
            return quota(window(23), window(47, week=True))
        if url.endswith("/api/anthropic/v1/models"):
            if failure["models"]:
                raise common.ZhipuError("catalog", "network")
            return {"data": [{"id": "GLM-5.3"}, {"id": "GLM-5.3-Flash"}], "hasMore": False}
        if "client/configs" in url:
            return {"configs": {"builtin_provider_config_json": "https://cdn.example.test/models.json"}}
        if url == "https://cdn.example.test/models.json":
            return {"config": {"modelConfigRules": {"modelRules": [{"modelMatch": r"^glm-5\.3(-flash)?$", "config": {
                "properties": {"contextWindow": 1000000}, "optionSpecs": {"maxOutputTokens": {"max": 128000}}}}]}}}
        if url.endswith("/subscription/list"):
            return [{"productId": "coding-plan", "status": "VALID", "inCurrentPeriod": True}]
        if url.endswith("/mcp/usage"):
            return {"total_usage": {"used": 0, "limit": 10, "remaining": 10}}
        if url.endswith("/coding-plan/reset/status"):
            return {"available_five_hour_resets": [], "available_week_resets": [],
                    "latest_five_hour_reset_history": None, "latest_week_reset_history": None,
                    "has_unread_history": False}
        if url.endswith("/getCustomerInfo"):
            return {"organizations": []}
        pytest.fail("unexpected onboarding endpoint: " + url)

    def download():
        downloads.append(True)
        if failure["metadata"]:
            raise OSError("offline")
        return True

    monkeypatch.setattr(common, "request", wire)
    monkeypatch.setattr(model_pricing, "refresh_remote_catalog_sync", download)
    monkeypatch.setattr(zh, "control", control)
    monkeypatch.setattr(menu, "oauth_control", control)
    futures = []
    start_refresh = control.backend.start_account_model_refresh
    def start(account_id):
        future = start_refresh(account_id)
        futures.append(future)
        return future
    monkeypatch.setattr(control.backend, "start_account_model_refresh", start)
    yield control, calls, downloads, failure
    for future in futures:
        # Join all fixture-owned work before restoring patches. A project bind
        # may intentionally retire the old shell before its queued sync starts;
        # callers assert the live account's result, not success of retired work.
        future.exception(10)
    for thread in threading.enumerate():
        if thread.name == "oauth-model-sync-ui":
            thread.join(3)
    states.pop_state(42)


def create(control, mode="api_key", site="bigmodel"):
    return control.create_account(context(), CreateOAuthAccountCommand(
        JsonCredential(OAuthProvider.ZHIPU, json.dumps(credential(mode, site)))))


def assert_initialized(result, calls, downloads):
    outcome = result.post_save["model_sync_future"].result(timeout=10)
    assert result.post_save["usage_error"] is None
    assert outcome["action"] == "updated"
    assert outcome["metadata_sync"]["status"] == "succeeded"
    assert outcome["metadata_sync"]["scanned"] == 2
    key = result.account_id
    saved = om.get_account(key)
    assert saved["models"] == ["GLM-5.3", "GLM-5.3-Flash"]
    assert om.account_model_selection(key)["effective_models"] == saved["models"]
    row = state_db.quota_load(key)
    usage = json.loads(row["raw_data"])
    assert usage["five_hour"]["utilization"] == 23
    assert usage["seven_day"]["utilization"] == 47
    metadata = model_metadata.get_metadata("GLM-5.3", scope_key="oauth:" + key)
    assert metadata["contextWindow"] == 1000000
    assert metadata["maxOutputTokens"] == 128000
    assert len([url for url in calls if "/quota/limit" in url]) == 1
    assert len([url for url in calls if "client/configs" in url]) == 1
    assert len([url for url in calls if url.endswith("/api/anthropic/v1/models")]) == 1
    assert downloads == [True]
    assert config.get()["modelMetadataOverrides"] == {"defaults": {"unrelated-model": {"contextWindow": 12345}}}


@pytest.mark.parametrize("mode", ["api_key", "oauth"])
@pytest.mark.parametrize("site", ["bigmodel", "zai"])
def test_json_create_initializes_all_three_stages_once(onboarding, mode, site):
    control, calls, downloads, _ = onboarding
    assert_initialized(create(control, mode, site), calls, downloads)


def test_oauth_project_completion_returns_same_initialization_work(onboarding, monkeypatch):
    control, calls, downloads, _ = onboarding
    choice = {"organization_id": "org", "project_id": "project", "plan_scope": "personal"}
    monkeypatch.setattr(control.backend, "zhipu_start_login", lambda **kw: {
        "site": "bigmodel", "status": "pending", "auth_url": "https://bigmodel.cn/login",
        "expires": time.time() + 300, "interval": 1})
    monkeypatch.setattr(control.backend, "zhipu_poll_login", lambda payload: payload.update(
        status="select_project", credential=credential("oauth"), choices=[choice]))
    monkeypatch.setattr(control.backend, "zhipu_select_project", lambda entry, selected: dict(entry, **selected))
    flow = control.start_login_flow(context(), OAuthProvider.ZHIPU, site="bigmodel")
    assert control.poll_login_flow(context(), flow.flow_id, flow.flow_secret).status == "select_project"
    result = control.select_zhipu_project(context(), flow.flow_id, flow.flow_secret,
        organization_id="org", project_id="project")
    assert_initialized(result, calls, downloads)


def test_confirmed_reimport_initializes_once_not_during_conflict(onboarding):
    control, calls, downloads, _ = onboarding
    first = create(control)
    first.post_save["model_sync_future"].result(timeout=10)
    calls.clear(); downloads.clear()
    command = CreateOAuthAccountCommand(JsonCredential(OAuthProvider.ZHIPU, json.dumps(credential())))
    with pytest.raises(ManagementError) as conflict:
        control.create_account(context(), command)
    assert not calls and not downloads
    result = control.create_account(context(), CreateOAuthAccountCommand(command.credential,
        replace_plan_token=conflict.value.plan_token))
    assert result.status == "replaced" and result.account_id == first.account_id
    assert_initialized(result, calls, downloads)


@pytest.mark.parametrize("site", ["bigmodel", "zai"])
def test_tg_key_paste_initializes_and_reports_without_any_refresh_click(onboarding, monkeypatch, site):
    _, calls, downloads, _ = onboarding
    messages, initialized = [], threading.Event()
    monkeypatch.setattr(ui, "answer_cb", lambda *a, **kw: None)
    def send(chat, text, **kwargs):
        messages.append(text)
        return {"result": {"message_id": 901}}
    def edit(chat, message_id, text, **kwargs):
        messages.append(text)
        if "账户初始化结果" in text:
            initialized.set()
    monkeypatch.setattr(ui, "send", send)
    monkeypatch.setattr(ui, "edit", edit)
    assert menu.handle_callback(42, 900, "cb", "oa:zh:key:" + site)
    assert menu.handle_text_state(42, "oa_zh_input", "fixture.secret")
    state = states.get_state(42)
    assert state["action"] == "oa_zh_name"
    assert initialized.wait(10)
    summary = next(text for text in messages if "账户初始化结果" in text)
    assert "已自动获取额度用量" in summary and "已同步目录" in summary and "当前可用" in summary and "元数据已同步" in summary
    assert "正在同步" not in summary
    assert state_db.quota_load(state["data"]["key"])
    assert len([url for url in calls if "/quota/limit" in url]) == 1
    assert len([url for url in calls if "client/configs" in url]) == 1
    assert len([url for url in calls if url.endswith("/api/anthropic/v1/models")]) == 1
    assert downloads == [True]


@pytest.mark.parametrize("failed_stage", ["quota", "models", "metadata"])
def test_independent_stage_failure_preserves_saved_account_and_other_stages(onboarding, failed_stage):
    control, calls, downloads, failures = onboarding
    failures[failed_stage] = True
    result = create(control)
    outcome = result.post_save["model_sync_future"].result(timeout=10)
    assert om.get_account(result.account_id)
    assert bool(result.post_save["usage_error"]) == (failed_stage == "quota")
    assert bool(state_db.quota_load(result.account_id)) == (failed_stage != "quota")
    assert bool(om.get_account(result.account_id).get("models")) == (failed_stage != "models")
    if failed_stage == "models":
        assert not downloads and "metadata_sync" not in outcome
    else:
        assert outcome["metadata_sync"]["status"] == ("partial_failed" if failed_stage == "metadata" else "succeeded")


def test_auto_metadata_disabled_is_preserved(onboarding):
    control, _, downloads, _ = onboarding
    config.update(lambda c: c["pricing"].update(autoUpdate=False))
    result = create(control)
    outcome = result.post_save["model_sync_future"].result(timeout=10)
    assert outcome["metadata_sync"]["status"] == "skipped" and not downloads
    assert result.post_save["usage_error"] is None and om.get_account(result.account_id)["models"]


@pytest.mark.parametrize("worker_timeout", [False, True])
def test_tg_pending_message_updates_on_same_future_completion(onboarding, monkeypatch, worker_timeout):
    control, _, _, _ = onboarding
    entry = credential(); om.add_account(entry); key = om.get_account_key(entry)
    future = Future()
    messages, waiting = [], threading.Event()
    monkeypatch.setattr(control, "model_sync_foreground_timeout_seconds", lambda: 0)
    monkeypatch.setattr(ui, "send", lambda *a, **k: {"result": {"message_id": 901}})
    def edit(chat, message_id, text, **kwargs):
        assert message_id == 901
        messages.append(text)
        waiting.set()
    monkeypatch.setattr(ui, "edit", edit)
    menu._foreground_account_model_sync(42, key, provider="zhipu", label="test", post_save={
        "model_sync_future": future, "usage": {"zhipu": {"windows": {"five_hour": {"utilization": 0}}}}})
    assert waiting.wait(3)
    assert "初始化仍在后台进行" in messages[-1] and "请同步上游模型" not in messages[-1]
    if worker_timeout:
        future.set_exception(TimeoutError())
    else:
        future.set_result({"action": "updated", "models": 2, "metadata_sync": {"status": "succeeded"}})
    # A callback registered just after set_result still runs on that same Future.
    finished = threading.Event()
    future.add_done_callback(lambda _: finished.set())
    assert finished.wait(3)
    # Join only the UI worker associated with this local test to avoid teardown races.
    for thread in threading.enumerate():
        if thread.name == "oauth-model-sync-ui":
            thread.join(3)
    assert len(messages) == 2 and "账户初始化结果" in messages[-1]
    assert ("模型同步失败" if worker_timeout else "元数据已同步") in messages[-1]
