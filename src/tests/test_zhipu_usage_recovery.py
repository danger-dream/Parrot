"""Saved OAuth login -> refresh quota -> project/Key recovery via the actual TG entry."""
from __future__ import annotations

import threading
import pytest
from src import oauth_manager as om, oauth_errors, state_db
from src.management_control.oauth.menu_bridge import telegram_context
from src.oauth.zhipu import common
from src.telegram import states, ui
from src.telegram.menus import oauth_menu as menu, zhipu_oauth_menu as zh
from src.tests.test_zhipu_provider import account_env, credential
from src.tests.test_zhipu_onboarding import onboarding
from src.tests.test_zhipu_callback_login import callback_env, callback


@pytest.fixture
def display(monkeypatch):
    messages = []
    monkeypatch.setattr(ui, "answer_cb", lambda *a, **kw: None)
    monkeypatch.setattr(ui, "edit", lambda chat, mid, text, **kw: messages.append(text))
    monkeypatch.setattr(ui, "send", lambda chat, text, **kw: messages.append(text))
    monkeypatch.setattr(zh, "_show_initial_sync", lambda *a: None)
    monkeypatch.setattr(menu, "_edit_cached_detail", lambda *a, **kw: messages.append(kw.get("prefix", "")))
    return messages


def login(control):
    flow = control.start_zhipu_callback_login(telegram_context(42), site="bigmodel")
    return control.submit_zhipu_callback(telegram_context(42), flow.flow_id, flow.flow_secret, callback(flow)).account_id


def refresh(key):
    menu.on_refresh_usage(42, 500, "callback-id", ui.register_code(key))


@pytest.mark.parametrize("existing_key", [True, False])
def test_saved_login_refresh_initializes_default_personal_project_without_relogin_or_duplicate_creation(callback_env, display, monkeypatch, existing_key):
    control, calls, _ = callback_env
    choices = control.backend.zhipu_project_choices
    def unavailable(*a, **kw):
        raise common.ZhipuError("projects", "network")
    monkeypatch.setattr(control.backend, "zhipu_project_choices", unavailable)
    key = login(control)  # Persist a genuine partially initialized login.
    monkeypatch.setattr(control.backend, "zhipu_project_choices", choices)
    wire = common.request
    created = []
    def request(url, **kw):
        if url.endswith("/api_keys") and not existing_key:
            if kw.get("method", "GET") == "POST":
                created.append(True)
                return {"apiKey": "new-id", "name": "zcode-api-key"}
            return [{"apiKey": "new-id", "name": "zcode-api-key"}] if created else []
        return wire(url, **kw)
    monkeypatch.setattr(common, "request", request)
    assert not om.get_account(key).get("model_key")
    refresh(key)
    assert states.get_state(42) is None
    assert not any("请选择目标组织/项目" in text for text in display)
    saved = om.list_accounts()[0]
    assert saved["project_id"] == "project"
    key = om.get_account_key(saved)
    refresh(key)
    if existing_key:
        assert saved["model_key"] == "existing-id.existing-secret"
        assert any("✅ 额度已更新" in text for text in display)
        assert state_db.quota_load(key)
    else:
        assert saved["model_key"] == "new-id.existing-secret"
        assert created == [True]
        assert any("账户已就绪" in text for text in display)
        assert not any("创建并继续" in text for text in display)
    assert sum(url.endswith("/oauth/token") for url, _ in calls) == 1
    assert all(kw.get("method", "GET") == "GET" for url, kw in calls if "/api_keys" in url or "/reset/" in url)


def test_project_network_failure_keeps_login_and_refresh_can_retry(callback_env, display, monkeypatch):
    control, _, _ = callback_env
    wire = common.request
    def request(url, **kw):
        if url.endswith("getCustomerInfo"):
            raise common.ZhipuError("request", "network")
        return wire(url, **kw)
    monkeypatch.setattr(common, "request", request)
    key = login(control)
    refresh(key)
    assert om.get_account(key)["access_token"]
    assert any("登录已保存" in text for text in display)
    assert not om.get_account(key).get("model_key")
    monkeypatch.setattr(common, "request", wire)
    refresh(key)
    assert states.get_state(42) is None
    assert om.list_accounts()[0]["model_key"] == "existing-id.existing-secret"


def test_key_usage_io_is_background_and_leaving_menu_suppresses_late_result(callback_env, display, monkeypatch):
    control, _, _ = callback_env
    entry = credential("api_key")
    om.add_account(entry)
    key = om.get_account_key(entry)
    entered, release = threading.Event(), threading.Event()
    futures = []
    def fetch(*a, **kw):
        entered.set()
        assert release.wait(5)
        return {"usage": {"zhipu": {"windows": {"five_hour": {"utilization": 1}}}}}
    monkeypatch.setattr(control, "refresh_usage_now", fetch)
    def submit(run):
        future = zh._remote_executor.submit(run)
        futures.append(future)
        return future
    monkeypatch.setattr(zh, "_submit_remote", submit)
    refresh(key)
    try:
        assert entered.wait(2)
        # Navigation can run while the network operation is still waiting.
        zh.before_command(42, "/stats")
        assert states.get_state(42) is None and not release.is_set()
        before = list(display)
    finally:
        release.set()
        for future in futures:
            future.result(5)
    assert display == before


@pytest.mark.parametrize("kind,expected", [("project_required", "重试初始化"),
    ("creation_confirmation_required", "自动取得专用模型 Key"), ("network", "网络请求"), ("timeout", "超时")])
def test_zhipu_typed_errors_do_not_misreport_configuration_or_network_as_login_expiry(kind, expected):
    result = oauth_errors.describe_oauth_error(common.ZhipuError("request", kind), provider="zhipu", operation="fetch_usage")
    assert expected in result.title + result.reason + result.action
    assert not result.auth_error
    assert "如果持续失败，请重新登录" not in result.action
    assert "具体原因已记录到日志" not in result.reason


def test_default_personal_project_prefers_default_org_then_default_project():
    from src.oauth.zhipu.auth import default_personal_project
    def choice(org, project, org_name="", project_name="", scope="personal"):
        return {"organization_id": org, "project_id": project, "organization_name": org_name,
                "project_name": project_name, "plan_scope": scope}
    choices = [choice("team", "t", "默认机构", "默认项目", "team"),
               choice("first", "p", "other", "默认项目"),
               choice("default", "first", "默认机构", "other"),
               choice("default", "wanted", "默认机构", "默认项目")]
    assert default_personal_project(choices)["project_id"] == "wanted"
    assert default_personal_project(choices[:2])["organization_id"] == "first"
    with pytest.raises(common.ZhipuError, match="personal_project_missing"):
        default_personal_project(choices[:1])


def test_auto_setup_never_selects_team_and_keeps_login(callback_env, display, monkeypatch):
    control, _, _ = callback_env
    monkeypatch.setattr(control.backend, "zhipu_project_choices", lambda *a: [{
        "organization_id": "team", "project_id": "team-project", "plan_scope": "team"}])
    key = login(control)
    refresh(key)
    assert om.get_account(key)["plan_scope"] == "personal"
    assert not om.get_account(key).get("project_id")
    assert any("选择对应团队项目" in text for text in display)
