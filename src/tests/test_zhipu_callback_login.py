"""TG pasted callback -> provider exchange -> project -> named, initialized account."""
from __future__ import annotations

import copy
import threading
import time
from urllib.parse import parse_qs, urlsplit, urlencode

import pytest
from src import oauth_manager as om, state_db
from src.management_control.errors import ManagementError
from src.management_control.oauth import OAuthProvider
from src.management_control.oauth.menu_bridge import telegram_context
from src.oauth.zhipu import auth, common
from src.telegram import states, ui
from src.telegram.menus import oauth_menu as menu, zhipu_oauth_menu as zh
from src.tests.test_zhipu_onboarding import onboarding
from src.tests.test_zhipu_provider import account_env


@pytest.fixture
def callback_env(onboarding, monkeypatch):
    control, calls, downloads, failures = onboarding
    wire = common.request
    observed = []
    profile = {"user_id": "callback-user", "name": "官方用户名", "email": "user@example.test"}
    def request(url, **kw):
        observed.append((url, kw))
        if url.endswith("/cli/init"):
            site = kw["body"]["provider"]
            host = "https://bigmodel.cn/login" if site == "bigmodel" else "https://chat.z.ai/api/oauth/authorize"
            query = {"state": "official-flow-state", "redirect" if site == "bigmodel" else "redirect_uri":
                     common.PLATFORM_ORIGIN + "/api/v1/oauth/cli/callback/" + site}
            return {"authorize_url": host + "?" + urlencode(query), "flow_id": site,
                    "expires_at": time.time() + 300, "poll_interval_sec": 1}
        if "/cli/poll/" in url:
            site = url.rsplit("/", 1)[-1]
            return {"status": "ready", "token": "platform-token", "user": copy.deepcopy(profile),
                    site: {"access_token": "oauth-token"}}
        if url.endswith("/oauth/token"):
            site = kw["body"]["provider"]
            assert kw["body"]["redirect_uri"] == auth.CALLBACK_URI
            return {"token": "platform-token", "user": copy.deepcopy(profile), site: {"access_token": "oauth-token"}}
        if url.endswith("/api/auth/z/login"):
            assert kw["body"] == {"token": "oauth-token"}
            return {"access_token": "business-token"}
        if url.endswith("/api/oauth/userinfo"):
            return {"sub": "callback-user", "preferred_username": "国际站用户名"}
        if url.endswith("getCustomerInfo"):
            return {"customerNumber": "callback-user", "nickName": "中国站昵称", "organizations": [
                {"organizationId": "org", "organizationName": "默认机构", "projects": [
                    {"projectId": "project", "projectName": "默认项目", "projectType": "1"}]}]}
        if url.endswith("/api_keys"):
            assert kw.get("method", "GET") == "GET", "login must never create a Key"
            return [{"apiKey": "existing-id", "name": "zcode-api-key"}]
        if "/api_keys/copy/" in url:
            return {"secretKey": "existing-secret"}
        return wire(url, **kw)
    monkeypatch.setattr(common, "request", request)
    yield control, observed, profile


def callback(flow, *, site="bigmodel"):
    state = parse_qs(urlsplit(flow.auth_url).query)["state"][0]
    return auth.CALLBACK_URI + "?" + urlencode({"authCode" if site == "bigmodel" else "code": "one-use-code", "state": state})


@pytest.mark.parametrize("site", ["bigmodel", "zai"])
def test_callback_flow_preserves_user_name_and_custom_rename(callback_env, site):
    control, observed, _ = callback_env
    ctx = telegram_context(42)
    flow = control.start_zhipu_callback_login(ctx, site=site)
    query = parse_qs(urlsplit(flow.auth_url).query)
    assert query["redirect" if site == "bigmodel" else "redirect_uri"] == [auth.CALLBACK_URI]
    poll = control.submit_zhipu_callback(ctx, flow.flow_id, flow.flow_secret, callback(flow, site=site))
    assert poll.status == "completed" and poll.account_preview["label"] == "官方用户名"
    assert om.get_account(poll.account_id)["model_key"] == "existing-id.existing-secret"
    assert om.get_account(poll.account_id)["project_id"] == "project"
    project_flow = control.start_zhipu_project_selection(ctx, poll.account_id)
    result = control.select_zhipu_project(ctx, project_flow.flow_id, project_flow.flow_secret, organization_id="org", project_id="project")
    result.post_save["model_sync_future"].result(10)
    saved = om.get_account(result.account_id)
    assert saved["label"] == "官方用户名" and saved["model_key"] == "existing-id.existing-secret"
    assert state_db.quota_load(result.account_id) and saved["models"]
    assert len([url for url, kw in observed if url.endswith("/oauth/token")]) == 1
    assert not any("/cli/poll/" in url for url, _ in observed)
    with pytest.raises(ManagementError):
        control.submit_zhipu_callback(ctx, flow.flow_id, flow.flow_secret, callback(flow, site=site))
    om.mutate_account_if_unchanged(result.account_id, copy.deepcopy(saved), lambda a: a.update(label="我的老号"))
    again = control.start_zhipu_callback_login(ctx, site=site)
    again_poll = control.submit_zhipu_callback(ctx, again.flow_id, again.flow_secret, callback(again, site=site))
    assert again_poll.status == "completed" and again_poll.account_id == result.account_id
    project_flow = control.start_zhipu_project_selection(ctx, again_poll.account_id)
    replaced = control.select_zhipu_project(ctx, project_flow.flow_id, project_flow.flow_secret, organization_id="org", project_id="project")
    replaced.post_save["model_sync_future"].result(10)
    assert replaced.account_id == result.account_id and om.get_account(result.account_id)["label"] == "我的老号"


@pytest.mark.parametrize("site,expected", [("bigmodel", "中国站昵称"), ("zai", "国际站用户名")])
def test_missing_backend_name_uses_official_profile(callback_env, site, expected):
    control, _, profile = callback_env
    profile.clear()
    flow = control.start_zhipu_callback_login(telegram_context(42), site=site)
    result = control.submit_zhipu_callback(telegram_context(42), flow.flow_id, flow.flow_secret, callback(flow, site=site))
    assert result.account_preview["label"] == expected


@pytest.mark.parametrize("change", ["state", "host", "duplicate", "no_code", "fragment"])
def test_invalid_callback_rejected_before_any_exchange(callback_env, change):
    control, observed, _ = callback_env
    flow = control.start_zhipu_callback_login(telegram_context(42), site="zai")
    url = callback(flow, site="zai")
    if change == "state": url = url.replace("state=", "state=wrong")
    elif change == "host": url = url.replace("127.0.0.1", "example.test")
    elif change == "duplicate": url += "&code=other"
    elif change == "no_code": url = url.replace("code=one-use-code", "unrelated=none")
    else: url += "#extra"
    with pytest.raises(ValueError, match="invalid_callback"):
        control.submit_zhipu_callback(telegram_context(42), flow.flow_id, flow.flow_secret, url)
    assert not observed


def test_callback_actor_cancel_and_lost_exchange_cannot_replay(callback_env, monkeypatch):
    control, observed, _ = callback_env
    ctx = telegram_context(42)
    flow = control.start_zhipu_callback_login(ctx, site="zai")
    with pytest.raises(ManagementError):
        control.submit_zhipu_callback(telegram_context(99), flow.flow_id, flow.flow_secret, callback(flow, site="zai"))
    assert not observed
    control.cancel_login_flow(ctx, flow.flow_id, flow.flow_secret)
    with pytest.raises(ManagementError):
        control.submit_zhipu_callback(ctx, flow.flow_id, flow.flow_secret, callback(flow, site="zai"))
    flow = control.start_zhipu_callback_login(ctx, site="zai")
    calls = []
    def lost(url, **kw):
        calls.append(url)
        raise common.ZhipuError("request", "network")
    monkeypatch.setattr(common, "request", lost)
    with pytest.raises(common.ZhipuError):
        control.submit_zhipu_callback(ctx, flow.flow_id, flow.flow_secret, callback(flow, site="zai"))
    with pytest.raises(ValueError, match="already_submitted"):
        control.submit_zhipu_callback(ctx, flow.flow_id, flow.flow_secret, callback(flow, site="zai"))
    assert len(calls) == 1


@pytest.mark.parametrize("site", ["bigmodel", "zai"])
def test_actual_tg_login_automatically_initializes_in_one_message(callback_env, monkeypatch, site):
    control, observed, _ = callback_env
    messages, initialized = [], threading.Event()
    monkeypatch.setattr(ui, "answer_cb", lambda *a, **k: None)
    def send(chat, text, **kwargs):
        pytest.fail("login must not emit an extra initialization card: " + text)
    def edit(chat, mid, text, **kwargs):
        assert mid == 500, "all progress and the final detail replace the login card"
        messages.append((text, kwargs))
        if "账户已就绪" in text:
            initialized.set()
    monkeypatch.setattr(ui, "send", send)
    monkeypatch.setattr(ui, "edit", edit)
    assert menu.handle_callback(42, 500, "cb", "oa:zh:login:" + site)
    assert initialized.wait(10), "\n---\n".join(text for text, _ in messages)
    for thread in threading.enumerate():
        if thread.name == "zhipu-oauth-login":
            thread.join(3)
    assert any("无需粘贴回调地址" in text for text, _ in messages)
    assert states.get_state(42) is None
    saved = om.list_accounts()[0]
    assert saved["model_key"] == "existing-id.existing-secret"
    assert saved["entitlement"] == "available"
    assert len(om.account_model_selection(saved)["effective_models"]) == 2
    assert state_db.quota_load(om.get_account_key(saved))
    auth_url = next(button["url"] for _, kwargs in messages for row in kwargs.get("reply_markup", {}).get("inline_keyboard", []) for button in row if "url" in button)
    query = parse_qs(urlsplit(auth_url).query)
    assert query["state"] == ["official-flow-state"]
    assert query["redirect" if site == "bigmodel" else "redirect_uri"] == [
        common.PLATFORM_ORIGIN + "/api/v1/oauth/cli/callback/" + site]
    assert "官方用户名" in messages[-1][0]
    assert not any("请选择目标组织/项目" in text or "账户初始化结果" in text for text, _ in messages)
    assert "模型目录: 2 个 · 已启用 2 · 禁用 0" in messages[-1][0]
    assert all(kw.get("method", "GET") == "GET" for url, kw in observed if "/api_keys" in url)
    assert sum("client/configs" in url for url, _ in observed) == 1
    assert sum("/api_keys/copy/" in url for url, _ in observed) == 1
    assert om.list_accounts()[0]["label"] == "官方用户名"
    assert any("/cli/poll/" in url for url, _ in observed)
    assert not any(url.endswith("/oauth/token") for url, _ in observed)


@pytest.mark.parametrize("outcome", ["cancel", "replace", "error", "expired", "failed"])
def test_automatic_wait_ends_or_reports_instead_of_leaving_a_stale_card(callback_env, monkeypatch, outcome):
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    control, _, _ = callback_env
    flow = control.start_login_flow(telegram_context(42), OAuthProvider.ZHIPU, site="bigmodel")
    data = zh._state(42, "oa_zh_login", flow_id=flow.flow_id, flow_secret=flow.flow_secret)
    messages = []
    monkeypatch.setattr(ui, "edit", lambda chat, mid, text, **kw: messages.append(text))
    if outcome == "expired":
        flow = SimpleNamespace(flow_id=flow.flow_id, flow_secret=flow.flow_secret,
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    def poll(*args, **kwargs):
        if outcome == "error":
            raise common.ZhipuError("poll", "network")
        return SimpleNamespace(status="failed" if outcome == "failed" else "pending")
    monkeypatch.setattr(control, "poll_login_flow", poll)
    def after_pending(_):
        if outcome == "error":
            return  # Retryable read errors keep the original live flow.
        if outcome == "cancel":
            zh.discard_state(42)
        else:
            states.set_state(42, "unrelated_new_menu", {"keep": True})
    monkeypatch.setattr(zh.time, "sleep", after_pending)
    zh._wait_for_login(42, 500, data, flow)
    if outcome in {"cancel", "replace"}:
        assert not messages
        if outcome == "replace":
            assert states.get_state(42)["action"] == "unrelated_new_menu"
    elif outcome == "error":
        assert len(messages) == 4 and states.get_state(42)["data"] is data
        assert "故障编号" in messages[-1] and "流程已保留" in messages[-1]
    else:
        assert len(messages) == 1 and states.get_state(42) is None
        assert "过期" in messages[0]


@pytest.mark.parametrize("failure", ["network_once", "business_once", "business_persistent", "network_persistent", "auth_rejected"])
def test_project_lookup_failure_reuses_credentials_without_reauthorizing(callback_env, monkeypatch, failure):
    from datetime import datetime, timezone
    control, observed, _ = callback_env
    wire = common.request
    now = [time.time()]
    monkeypatch.setattr(zh.time, "time", lambda: now[0])
    monkeypatch.setattr(zh.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    control._flows.zhipu.clock = lambda: datetime.fromtimestamp(now[0], timezone.utc)
    control._flows.zhipu.store._clock = control._flows.zhipu.clock
    lookups, recovering, messages = [], [False], []
    def request(url, **kw):
        if url.endswith("getCustomerInfo"):
            assert len(om.list_accounts()) == 1 and om.list_accounts()[0].get("access_token")
            lookups.append(url)
            if not recovering[0] and (len(lookups) == 1 or failure.endswith("persistent")):
                if failure == "auth_rejected":
                    raise common.ZhipuError("response", "business", status=200, code="401")
                if failure.startswith("business"):
                    raise common.ZhipuError("response", "business", status=200, code="1234")
                raise common.ZhipuError("request", "network")
        return wire(url, **kw)
    monkeypatch.setattr(common, "request", request)
    monkeypatch.setattr(ui, "answer_cb", lambda *a, **k: None)
    monkeypatch.setattr(ui, "edit", lambda chat, mid, text, **kw: messages.append((text, kw)))
    # Deterministic worker scheduling; use the real flow, provider and TG handlers.
    monkeypatch.setattr(zh, "_start_login_wait", lambda chat, mid, data:
        zh._wait_for_login(chat, mid, data, data["flow"]))
    assert menu.handle_callback(42, 500, "cb", "oa:zh:login:bigmodel")
    assert states.get_state(42) is None and len(om.list_accounts()) == 1
    key = om.get_account_key(om.list_accounts()[0])
    assert len(lookups) == 1 and om.get_account(key), "default project setup runs after saving login"
    assert "登录已保存" in messages[-1][0]
    assert not om.get_account(key).get("model_key")
    short = ui.register_code(key)
    recovering[0] = True
    now[0] += 360  # Original login TTL no longer owns the saved credentials.
    assert menu.handle_callback(42, 500, "retry", "oa:zh:initialize:" + short)
    assert "账户已就绪" in messages[-1][0]
    assert om.list_accounts()[0]["model_key"] == "existing-id.existing-secret"
    assert "官方用户名" in messages[-1][0]
    assert sum("/cli/poll/" in url for url, _ in observed) == 1
    assert sum(url.endswith("/cli/init") for url, _ in observed) == 1
    assert not any(url.endswith("/oauth/token") for url, _ in observed)
