"""Add-session routing: real menu/control/writer and offline transport boundaries."""
from __future__ import annotations

import asyncio
import copy
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import httpx
import pytest

from src import config, network, oauth_manager as om, state_db
from src.oauth import add_context, antigravity, xai
from src.oauth.workbuddy import auth as wb_auth
from src.oauth.zhipu import auth as zh_auth, runtime as zh_runtime
from src.management_control.oauth import OAuthControl, OAuthProvider, CompleteOAuthLoginCommand
from src.management_control.oauth.account_mutations import OAuthReplaceRequired
from src.proxy import manager as pm
from src.proxy.connector import ProxyConnectError
from src.telegram import states, ui
from src.telegram.menus import oauth_menu as menu, oauth_add_session as sessions
from src.telegram.menus import workbuddy_oauth_menu as wb, zhipu_oauth_menu as zh
from src.tests.test_oauth_overwrite_confirmation import _entry
from src.tests.test_workbuddy_lifecycle import context
from src.tests.test_zhipu_provider import credential


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    before = copy.deepcopy(config.get())
    state_db.init()
    states.clear_all()
    config.update(lambda c: c.update(oauthAccounts=[], channels=[], loadBalancing={},
        network={"proxies": {"one": {"type": "socks5", "url": "socks5://127.0.0.1:18001"},
                             "two": {"type": "socks5", "url": "socks5://127.0.0.1:18002"}},
                 "groups": {"group": ["one", "two"]},
                 "routing": {"default": "two", "providers": {"openai": "two"}, "accounts": {}}}))
    for name in ("send", "edit", "send_result", "answer_cb"):
        monkeypatch.setattr(ui, name, Mock(return_value={"ok": True, "result": {}}))
    ctl = OAuthControl()
    monkeypatch.setattr(menu, "oauth_control", ctl)
    monkeypatch.setattr(wb, "oauth_control", ctl)
    monkeypatch.setattr(zh, "control", ctl)
    monkeypatch.setattr(ctl, "_post_save_account_effects", Mock(return_value={}))
    monkeypatch.setattr(menu, "_foreground_account_model_sync", Mock(return_value={"post_save": {}}))
    monkeypatch.setattr(wb, "_start_login_worker", Mock())
    monkeypatch.setattr(zh, "_submit_remote", lambda run: run())
    monkeypatch.setattr(zh, "_start_login_wait", Mock())
    yield ctl
    states.clear_all()
    for job in wb._login_jobs.values():
        job["stop"].set(); job["wake"].set()
    wb._login_jobs.clear()
    config.update(lambda c: (c.clear(), c.update(before)))


def route(purpose="oauth_openai", channel=""):
    chain = network._configured_proxy_chain_or_none(proxy_purpose=purpose, proxy_channel=channel, proxy_model="")
    return [name for name, _ in chain] if chain is not None else None


def selected(chat=42, target="one"):
    sessions.get(chat).target = target


def rows():
    return ui.edit.call_args.kwargs["reply_markup"]["inline_keyboard"]


def test_exact_brand_layout_and_every_old_entry_reachable():
    menu.on_add_menu(42, 1, "cb")
    assert [[b["text"] for b in row] for row in rows()] == [
        ["Anthropic", "OpenAI", "Grok"], ["Cursor", "Antigravity", "WorkBuddy"], ["智谱"],
        ["⚙ 设置代理", "🏠 返回主菜单", "◀ 返回列表"]]
    assert "本次新增代理：继承现有规则" in ui.edit.call_args.args[2]
    for row in rows()[:3]:
        assert all(b.get("icon_custom_emoji_id") for b in row)
    expected = {"claude": {"oa:login", "oa:set_json"},
        "openai": {"oa:login:openai", "oa:set_rt:openai", "oa:import:sub2api", "oa:import:cpa"},
        "xai": {"oa:login:xai", "oa:set_rt:xai"},
        "workbuddy": {"oa:wb:login", "oa:wb:login:global"}}
    for provider, callbacks in expected.items():
        assert menu.handle_callback(42, 1, "cb", "oa:add:" + provider)
        assert {b["callback_data"] for row in rows() for b in row} == callbacks | {"oa:add"}
    menu.handle_callback(42, 1, "cb", "oa:zh:add")
    assert {b["callback_data"] for row in rows() for b in row} >= {
        "oa:zh:login:bigmodel", "oa:zh:login:zai", "oa:zh:key:bigmodel", "oa:zh:key:zai", "oa:zh:import"}


def test_picker_never_writes_public_rules_navigation_exit_and_ttl(monkeypatch):
    before = copy.deepcopy(config.get())
    menu.on_add_menu(42, 1, "cb")
    menu.handle_callback(42, 1, "cb", "oa:add:proxy")
    buttons = {b["text"]: b["callback_data"] for row in rows() for b in row}
    assert len(buttons) == 6  # inherit, direct, two nodes, group, back
    choose = next(v for k, v in buttons.items() if "one" in k)
    menu.handle_callback(42, 1, "cb", choose)
    assert "本次新增代理：one" in ui.edit.call_args.args[2]
    menu.on_set_rt_openai_start(42, 1, "cb")
    old = sessions.get(42).attempt
    assert states.get_state(42)["data"]["_oauth_add_attempt"] == old.nonce
    menu.on_add_openai(42, 1, "cb")
    assert states.get_state(42) is None and old.cancelled
    menu.on_add_menu(42, 1, "cb")
    assert sessions.get(42).target == "one"
    for exit_callback in ("menu:main", "menu:oauth"):
        sessions.before_callback(42, exit_callback)
        menu.on_add_menu(42, 1, "cb")
        assert sessions.get(42).target is None
        selected()
    sessions._sessions[42].touched -= 601
    sessions.cleanup()
    menu.handle_callback(42, 1, "cb", choose)  # old picker is not a fresh add
    assert sessions.get(42, create=False) is None
    menu.on_login_xai_start(42, 1, "cb")
    assert states.get_state(42) is None  # expired old entry cannot silently inherit
    menu.on_add_menu(42, 1, "cb")
    assert sessions.get(42).target is None
    assert config.get() == before


def test_two_chat_attempt_snapshots_thread_async_and_telegram_isolation(monkeypatch):
    selected(42, "one"); selected(43, "two")
    menu.on_set_rt_openai_start(42, 1, "cb")
    menu.on_set_rt_openai_start(43, 1, "cb")
    attempts = [sessions.get(chat).attempt for chat in (42, 43)]
    barrier = threading.Barrier(2)
    def worker(attempt):
        with add_context.bind(attempt):
            barrier.wait(timeout=3)
            assert route("telegram") == ["two"]
            async def probe():
                await asyncio.sleep(0)
                return route()
            return asyncio.run(probe())
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(worker, attempts)) == [["one"], ["two"]]
    assert add_context.current() is None and route() == ["two"]
    # A newer choice cannot mutate the old attempt's route, or permit its save.
    sessions.get(42).target = "group"
    assert attempts[0].target == "one"
    menu.on_set_rt_openai_start(42, 1, "cb")
    with add_context.bind(attempts[0]), pytest.raises(ValueError):
        om.add_account_if_identity_absent(_entry("claude"))
    assert config.get()["oauthAccounts"] == []


def test_fail_closed_and_explicit_direct_fallback_policy():
    with add_context.bind(add_context.AddAttempt("gone")):
        with pytest.raises(ProxyConnectError):
            route()
        config.update(lambda c: c["network"]["routing"].update(directFallback=True))
        assert route() == ["direct"]
    with add_context.bind(add_context.AddAttempt("group")):
        assert route() == ["one", "two", "direct"]
    config.update(lambda c: c.update(network={}))
    with add_context.bind(add_context.AddAttempt("direct")):
        assert route() == ["direct"]
    with add_context.bind(add_context.AddAttempt(None)):
        assert route() is None
    expired = add_context.AddAttempt("one", expires_at=time.time() - 1)
    with add_context.bind(expired), pytest.raises(ValueError):
        route()


@pytest.mark.parametrize("provider", ["claude", "openai", "xai", "cursor", "antigravity", "workbuddy", "zhipu"])
def test_route_published_atomically_before_init_and_default_relogin_unchanged(isolated, provider, monkeypatch):
    entry = (wb_auth.normalize_credential(dict(realm="cn", uid="fixture-user", access_token="at", refresh_token="rt"))
             if provider == "workbuddy" else credential() if provider == "zhipu" else _entry(provider))
    if provider == "antigravity": entry["project_id"] = "project"
    key = om.get_account_key(entry)
    observations = []
    def observe(cfg):
        if any(om.get_account_key(a) == key for a in cfg["oauthAccounts"]):
            # Even BEFORE the manager's reload callback: network.init reads the
            # newly published config generation and cannot see an old route.
            observations.append(route("oauth_" + provider, "oauth:" + key))
    monkeypatch.setattr(config, "_reload_callbacks", [observe])
    monkeypatch.setattr(isolated, "_post_save_account_effects", lambda *a, **k: observe(config.get()) or {})
    attempt = add_context.AddAttempt("group", provider)
    with add_context.bind(attempt):
        result = isolated.add_account_entry(context(), entry)
    assert result["status"] == "added" and observations and all(v == ["one", "two"] for v in observations)
    assert config.get()["network"]["routing"]["accounts"] == {"oauth:" + key: "group"}
    assert config.get()["network"]["routing"]["providers"] == {"openai": "two"}
    attempt.cancel()
    with add_context.bind(attempt):
        assert route("oauth_" + provider, "oauth:" + key) == ["one", "two"]
    incoming = dict(om.get_account(key), access_token="new-at")
    with add_context.bind(None):
        assert om.replace_exact_identity(key, incoming)["status"] == "replaced"
    assert route("oauth_" + provider, "oauth:" + key) == ["one", "two"]


def test_failed_write_cancel_and_duplicate_confirmation_never_misroute(monkeypatch):
    entry = _entry("xai"); om.add_account(entry)
    key = om.get_account_key(entry)
    config.update(lambda c: c["network"]["routing"]["accounts"].update({key: "two", "oauth:" + key: "two"}))
    selected(); attempt = sessions.begin(42, "xai")
    incoming = dict(entry, access_token="new-at")
    with add_context.bind(attempt):
        assert menu._persist_new_or_stage_overwrite(42, incoming, source="test") == "staged"
    assert route("oauth_xai", "oauth:" + key) == ["two"]
    nonce = states.get_state(42)["data"]["nonce"]
    menu.on_oauth_overwrite_confirm(43, 1, "cb", nonce)
    assert route("oauth_xai", "oauth:" + key) == ["two"]
    menu.on_oauth_overwrite_cancel(42, 1, "cb", nonce)
    assert om.get_account(key)["access_token"] == entry["access_token"]
    with add_context.bind(attempt):
        menu._persist_new_or_stage_overwrite(42, incoming, source="test")
    nonce = states.get_state(42)["data"]["nonce"]
    before = copy.deepcopy(config.get())
    write = config._write_atomic
    monkeypatch.setattr(config, "_write_atomic", Mock(side_effect=OSError("disk")))
    with pytest.raises(OSError): menu.on_oauth_overwrite_confirm(42, 1, "cb", nonce)
    assert config.get() == before
    monkeypatch.setattr(config, "_write_atomic", write)
    with add_context.bind(attempt): menu._persist_new_or_stage_overwrite(42, incoming, source="test")
    menu.on_oauth_overwrite_confirm(42, 1, "cb", states.get_state(42)["data"]["nonce"])
    assert om.get_account(key)["access_token"] == "new-at"
    assert config.get()["network"]["routing"]["accounts"] == {key: "one", "oauth:" + key: "one"}


@pytest.mark.parametrize("provider", [OAuthProvider.WORKBUDDY, OAuthProvider.ZHIPU])
def test_device_flow_keeps_proxy_across_threads_and_requires_duplicate_confirmation(isolated, monkeypatch, provider):
    ctl = isolated; seen = []
    entry = wb_auth.normalize_credential(dict(realm="cn", uid="dup", access_token="at", refresh_token="rt")) if provider is OAuthProvider.WORKBUDDY else credential("oauth", subject="device-user")
    om.add_account(entry)
    def start(**kwargs):
        seen.append(route("oauth_" + provider.value))
        payload = {"auth_url": "https://example.test/login", "status": "pending", "site": "bigmodel", "interval": 1}
        if provider is OAuthProvider.WORKBUDDY:
            # Real WorkBuddy start_login_sync includes its selected route.
            payload.update(realm="cn", workbuddy_client_profile="cli")
        return payload
    def poll(payload):
        seen.append(route("oauth_" + provider.value))
        payload.update(status="ready", entry=dict(entry, access_token="new-at"))
    monkeypatch.setattr(ctl.backend, provider.value + "_start_login", start)
    monkeypatch.setattr(ctl.backend, provider.value + "_poll_login", poll)
    with add_context.bind(add_context.AddAttempt("one", provider.value)):
        flow = ctl.start_login_flow(context(), provider, **({"site": "bigmodel"} if provider is OAuthProvider.ZHIPU else {}))
    with ThreadPoolExecutor(max_workers=1) as pool:
        with pytest.raises(OAuthReplaceRequired) as exc:
            pool.submit(ctl.poll_login_flow, context(), flow.flow_id, flow.flow_secret).result()
    key = om.get_account_key(entry)
    assert config.get()["network"]["routing"]["accounts"] == {}
    assert om.get_account(key)["access_token"] == ("at" if provider is OAuthProvider.WORKBUDDY else "fixture-biz")
    ctl.complete_login_flow(context(), flow.flow_id, flow.flow_secret,
        CompleteOAuthLoginCommand(completed=True, replace_plan_token=exc.value.plan_token))
    assert config.get()["network"]["routing"]["accounts"] == {"oauth:" + key: "one"}
    assert seen == [["one"], ["one"]]


def test_zhipu_shell_route_follows_initial_project_rename(monkeypatch):
    shell = credential("oauth", subject="new-shell", model_key="", organization_id="", project_id="")
    attempt = add_context.AddAttempt("one", "zhipu")
    with add_context.bind(attempt): om.add_account_if_identity_absent(shell)
    key = om.get_account_key(shell)
    saved = copy.deepcopy(om.get_account(key))
    scoped = dict(saved, organization_id="org", project_id="project")
    with add_context.bind(attempt): new_key, _ = zh_runtime.bind_project(key, saved, scoped)
    routes = config.get()["network"]["routing"]["accounts"]
    assert routes == {"oauth:" + new_key: "one"}
    assert route("oauth_zhipu", "oauth:" + new_key) == ["one"]


@pytest.fixture
def wire(monkeypatch):
    """No provider facade mocks: capture selected clients at the HTTP boundary."""
    import base64
    from src.proxy.connector import SOCKS5Connector, DirectConnector
    real_sync, real_async = httpx.Client, httpx.AsyncClient
    calls = []
    state = {"provider": "", "site": "bigmodel", "realm": "cn", "failure": False}
    claims = {"email": "wire@example.test", "sub": "wire-user", "exp": 4102444800,
              "https://api.openai.com/auth": {"chatgpt_account_id": "wire-workspace", "chatgpt_plan_type": "plus"}}
    token = "e30." + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=") + ".signature"
    tokens = {"access_token": token, "refresh_token": "wire-refresh", "id_token": token, "expires_in": 3600}
    def handler(proxy, request):
        calls.append((proxy, request.method, str(request.url)))
        if state["failure"]:
            raise httpx.ConnectError("offline proxy failure", request=request)
        path = request.url.path
        if ".well-known" in path:
            value = {"authorization_endpoint": "https://auth.x.ai/authorize", "token_endpoint": "https://auth.x.ai/token"}
        elif state["provider"] == "workbuddy":
            if path.endswith("/auth/state"):
                host = "www.codebuddy.cn" if state["realm"] == "cn" else "www.codebuddy.ai"
                value = {"state": "remote-state", "authUrl": "https://" + host + "/login"}
            elif path.endswith("/auth/token"):
                value = {"accessToken": token, "refreshToken": "wire-refresh"}
            elif path.endswith("/login/account"):
                value = {"uid": "wire-user", "nickname": "Wire"}
            else: value = {}
            value = {"code": 0, "data": value}
        elif state["provider"] == "zhipu":
            if path.endswith("/cli/init"):
                host = "bigmodel.cn" if state["site"] == "bigmodel" else "chat.z.ai"
                value = {"flow_id": "remote-flow", "authorize_url": "https://" + host + "/login",
                         "expires_at": time.time() + 300, "poll_interval_sec": 1}
            elif "/cli/poll/" in path:
                value = {"status": "ready", state["site"]: {"access_token": token}, "token": "platform-token"}
            elif path.endswith("/api/auth/z/login"): value = {"access_token": "business-token"}
            elif path.endswith("/userinfo"): value = {"sub": "wire-user", "name": "Wire"}
            elif path.endswith("/api_keys"):
                value = [{"apiKey": "wire-key", "name": "zcode-api-key"}]
            elif path.endswith("/access_tokens"):
                from src.tests.zhipu_pat_fixtures import envelope
                return httpx.Response(200, json=envelope("wire-key"))
            else: value = {"customerNumber": "wire-user", "customerName": "Wire", "organizations": [
                {"organizationId": "wire-org", "projects": [{"projectId": "wire-project", "projectType": 1}]}]}
            value = {"code": 0, "data": value}
        elif path.endswith("/auth/poll"):
            value = {"accessToken": token, "refreshToken": "wire-refresh"}
        elif path.endswith("/api/auth/me"):
            value = {"email": "wire@example.test", "name": "Wire", "id": "wire-user"}
        elif "userinfo" in path: value = {"email": "wire@example.test"}
        elif "loadCodeAssist" in path: value = {"cloudaicompanionProject": "wire-project"}
        elif path.endswith("/profile"): value = {"account": {"email": "wire@example.test"}}
        elif "accounts/check" in path: value = {"accounts": {}}
        elif "token" in path: value = tokens
        else: value = {}
        return httpx.Response(200, json=value)
    def sync_factory(**kwargs):
        proxy = kwargs.pop("proxy", None)
        return real_sync(transport=httpx.MockTransport(lambda req: handler(proxy, req)), **kwargs)
    def async_factory(connector, **kwargs):
        kwargs.pop("byte_counter", None)
        proxy = getattr(connector, "url", None)
        return real_async(transport=httpx.MockTransport(lambda req: handler(proxy, req)), **kwargs)
    monkeypatch.setattr(network.httpx, "Client", sync_factory)
    monkeypatch.setattr(SOCKS5Connector, "create_httpx_client", async_factory)
    monkeypatch.setattr(DirectConnector, "create_httpx_client", async_factory)
    monkeypatch.delenv("DISABLE_OAUTH_NETWORK_CALLS", raising=False)
    monkeypatch.delenv("PARROT_NO_REFRESH", raising=False)
    config.update(lambda c: (c["oauth"].update(mockMode=False), c.setdefault("antigravityOAuth", {}).update(tlsFingerprint={"enabled": False})))
    return state, calls


@pytest.mark.parametrize("provider,variant", [(p, "") for p in ("claude", "openai", "xai", "cursor", "antigravity")] +
    [("workbuddy", "cn"), ("workbuddy", "global"), ("zhipu", "bigmodel"), ("zhipu", "zai")])
def test_actual_tg_login_first_request_through_save_uses_selected_proxy(isolated, wire, provider, variant):
    state, calls = wire
    state.update(provider=provider, realm=variant if provider == "workbuddy" else "cn",
                 site=variant if provider == "zhipu" else "bigmodel")
    selected()
    callback = {"claude": "oa:login", "workbuddy": "oa:wb:login" + (":global" if variant == "global" else ""),
                "zhipu": "oa:zh:login:" + variant}.get(provider, "oa:login:" + provider)
    assert menu.handle_callback(42, 1, "cb", callback)
    assert len(calls) == (1 if provider in {"xai", "workbuddy", "zhipu"} else 0)
    data = states.get_state(42)["data"]
    if provider in {"workbuddy", "zhipu"}:
        # A different worker has no ambient add context. The flow owns it.
        with ThreadPoolExecutor(max_workers=1) as pool:
            poll = pool.submit(isolated.poll_login_flow, menu._management_context(42), data["flow_id"], data["flow_secret"]).result()
        assert poll.status == "completed"
    elif provider == "cursor": menu.on_login_cursor_done(42, 1, "cb")
    else:
        fn = {"claude": menu.on_login_code_input, "openai": menu.on_login_openai_code_input,
              "xai": menu.on_login_xai_code_input, "antigravity": menu.on_login_antigravity_code_input}[provider]
        callback_text = ("code#" + data["state"] if provider == "claude" else
                         data["redirect_uri"] + "?code=code&state=" + data["state"] if provider == "antigravity" else
                         "code=code&state=" + data["state"])
        fn(42, callback_text)
    accounts = om.list_accounts()
    assert len(accounts) == 1, (calls, ui.send_result.call_args_list)
    assert all(proxy == "socks5://127.0.0.1:18001" for proxy, _, _ in calls), calls
    paths = [url for _, _, url in calls]
    for part in {"claude": ["token", "profile"], "openai": ["token", "accounts/check"],
                 "xai": [".well-known", "token"], "cursor": ["auth/poll", "api/auth/me", "GetCurrentPeriodUsage"],
                 "antigravity": ["token", "userinfo", "loadCodeAssist"], "workbuddy": ["auth/state", "auth/token", "login/account"],
                 "zhipu": ["cli/init", "cli/poll", "userinfo" if variant == "zai" else "getCustomerInfo"]}[provider]:
        assert any(part in path for path in paths), (part, paths)
    key = om.get_account_key(accounts[0])
    assert config.get()["network"]["routing"]["accounts"] == {"oauth:" + key: "one"}


@pytest.mark.parametrize("provider", ["openai", "xai"])
def test_refresh_token_add_and_failed_retry_keep_selection(wire, provider):
    state, calls = wire; state["provider"] = provider
    selected()
    menu.handle_callback(42, 1, "cb", "oa:set_rt:" + provider)
    fn = menu.on_set_rt_openai_input if provider == "openai" else menu.on_set_rt_xai_input
    state["failure"] = True
    fn(42, "wire-refresh-token-long-enough")
    assert not om.list_accounts() and not config.get()["network"]["routing"]["accounts"]
    menu.handle_callback(42, 1, "cb", "oa:add:" + provider)
    assert sessions.get(42).target == "one"
    menu.handle_callback(42, 1, "cb", "oa:set_rt:" + provider)
    state["failure"] = False
    fn(42, "wire-refresh-token-long-enough")
    assert len(om.list_accounts()) == 1
    assert calls and all(p == "socks5://127.0.0.1:18001" for p, _, _ in calls)


@pytest.mark.parametrize("kind", ["cpa", "sub2api"])
def test_file_import_preview_confirmation_cancel_and_saved_route(wire, monkeypatch, kind):
    state, calls = wire; state["provider"] = "openai"
    payload = {"email": "wire@example.test", "refresh_token": "wire-refresh-token-long-enough"}
    if kind == "sub2api":
        payload = {"accounts": [{"platform": "openai", "type": "oauth", "credentials": payload}]}
    def download(*args, **kwargs):
        assert route("telegram") == ["two"]
        return json.dumps(payload).encode(), "fixture.json"
    monkeypatch.setattr(ui, "download_file", download)
    monkeypatch.setattr(menu, "_render_openai_import_result", Mock())
    initialized = []
    def initialize(key, **kwargs):
        initialized.append((key, route("oauth_openai", "oauth:" + key)))
        return {}
    monkeypatch.setattr(menu, "_fetch_and_save_usage_sync", initialize)
    def preview():
        menu.on_import_openai_start(42, 1, "cb", kind)
        menu.on_import_openai_document_input(42, {"document": {"file_id": "file", "file_name": "fixture.json"}})
        assert states.get_state(42)["action"] == "oa_openai_import_confirm"
    selected()
    preview()
    assert not om.list_accounts() and not calls
    menu.on_import_openai_exec(42, 1, "cb")
    key = om.get_account_key(om.list_accounts()[0])
    assert initialized == [(key, ["one"])]
    config.update(lambda c: c["network"]["routing"]["accounts"].update({"oauth:" + key: "two"}))
    preview()
    before = copy.deepcopy(config.get())
    menu.on_import_openai_exec(42, 1, "cb")
    assert states.get_state(42)["action"] == "oa_openai_import_overwrite_confirm"
    assert config.get() == before and len(initialized) == 1
    nonce = states.get_state(42)["data"]["nonce"]
    menu.on_import_openai_overwrite(42, 1, "cb", nonce, confirm=False)
    assert config.get() == before
    preview(); menu.on_import_openai_exec(42, 1, "cb")
    menu.on_import_openai_overwrite(42, 1, "cb", states.get_state(42)["data"]["nonce"], confirm=True)
    assert initialized == [(key, ["one"]), (key, ["one"])]
    assert all(p == "socks5://127.0.0.1:18001" for p, _, _ in calls)


def test_batch_routes_only_successful_accounts_before_initialization(monkeypatch):
    first, duplicate = _entry("openai"), dict(_entry("openai"), email="other@example.test", workspace_id="ws-2", chatgpt_account_id="ws-2")
    # Let each workspace get its own installation identity.
    duplicate.pop("codexDeviceInstallationId", None)
    om.add_account(duplicate)
    duplicate_key = om.get_account_key(duplicate)
    config.update(lambda c: c["network"]["routing"]["accounts"].update({"oauth:" + duplicate_key: "two"}))
    staged = {"new": [{"entry": first, "account_key": om.get_account_key(first)}],
              "duplicate": [{"entry": dict(duplicate, access_token="new-at"), "account_key": duplicate_key}],
              "failed": [("bad@example.test", "token failed")]}
    observed = []
    monkeypatch.setattr(menu, "_fetch_and_save_usage_sync", lambda key, **kw: observed.append(route("oauth_openai", "oauth:" + key)))
    with add_context.bind(add_context.AddAttempt("group", "openai")):
        result = menu._commit_staged_openai_import(staged, chat_id=42)
    assert len(result["added"]) == len(result["replaced"]) == len(result["failed"]) == 1
    assert observed == [["one", "two"], ["one", "two"]]
    assert set(config.get()["network"]["routing"]["accounts"].values()) == {"group"}
    assert len(config.get()["network"]["routing"]["accounts"]) == 2


def test_expired_input_and_late_duplicate_cannot_publish_or_replace_new_input():
    selected(); menu.on_set_json_start(42, 1, "cb")
    old = sessions.get(42).attempt
    states._states[42]["ts"] -= 601
    menu.on_set_json_input(42, json.dumps(_entry("claude")))
    assert not om.list_accounts()
    menu.on_set_rt_openai_start(42, 1, "cb")
    current = states.get_state(42)
    with add_context.bind(old), pytest.raises(ValueError):
        states.set_state(42, "oa_oauth_overwrite_confirm", {"nonce": "late"})
    assert states.get_state(42) == current
    sessions.get(42).attempt.expires_at -= 601
    menu.on_set_rt_openai_input(42, "long-token-which-must-not-be-used")
    assert not om.list_accounts() and not config.get()["network"]["routing"]["accounts"]


def test_real_relogin_does_not_consume_add_selection(wire):
    state, calls = wire; state["provider"] = "openai"
    old = dict(_entry("openai"), email="wire@example.test", workspace_id="wire-workspace", chatgpt_account_id="wire-workspace",
               disabled_reason="auth_error")
    old.pop("codexDeviceInstallationId", None)
    om.add_account(old)
    key = om.get_account_key(old)
    config.update(lambda c: c["network"]["routing"]["accounts"].update({"oauth:" + key: "group"}))
    selected(target="one")
    menu.on_relogin(42, 1, "cb", ui.register_code(key))
    data = states.get_state(42)["data"]
    assert "_oauth_add_attempt" not in data and sessions.get(42, create=False) is None
    menu.on_login_openai_code_input(42, "code=code&state=" + data["state"])
    assert calls and all(p == "socks5://127.0.0.1:18002" for p, _, _ in calls)
    nonce = states.get_state(42)["data"]["nonce"]
    menu.on_oauth_overwrite_confirm(42, 1, "cb", nonce)
    assert config.get()["network"]["routing"]["accounts"] == {"oauth:" + key: "group"}


def test_async_tasks_and_real_to_thread_do_not_cross(monkeypatch):
    from src.tests.conftest import _ORIG_TO_THREAD
    async def run():
        async def one(target):
            with add_context.bind(add_context.AddAttempt(target)):
                await asyncio.sleep(0)
                return await _ORIG_TO_THREAD(route)
        return await asyncio.gather(one("one"), one("two"))
    assert asyncio.run(run()) == [["one"], ["two"]]
    assert add_context.current() is None


def test_antigravity_go_tls_path_keeps_pre_account_scope(monkeypatch):
    from src.transports import go_tls
    calls = []
    def client(**kwargs):
        calls.append(kwargs["proxy"])
        def response(req):
            value = ({"access_token": "wire-at", "refresh_token": "wire-rt"} if "token" in req.url.path else
                     {"email": "wire@example.test"} if "userinfo" in req.url.path else
                     {"cloudaicompanionProject": "wire-project"})
            return httpx.Response(200, json=value)
        return httpx.AsyncClient(transport=httpx.MockTransport(response))
    monkeypatch.setattr(go_tls, "create_client", client)
    monkeypatch.setattr(antigravity, "_mock_mode_enabled", lambda: False)
    config.update(lambda c: c.setdefault("antigravityOAuth", {}).update(tlsFingerprint={"enabled": True}))
    with add_context.bind(add_context.AddAttempt("one", "antigravity")):
        assert antigravity.complete_login_sync("code", code_verifier="verifier")["project_id"] == "wire-project"
    assert calls == ["socks5://127.0.0.1:18001"] * 3


@pytest.mark.parametrize("site", ["bigmodel", "zai"])
def test_zhipu_post_save_project_and_pat_use_persisted_route_after_exit(wire, site):
    state, calls = wire; state.update(provider="zhipu", site=site)
    entry = credential("oauth", site=site, subject="post-save-" + site, model_key="",
                       organization_id="wire-org", project_id="wire-project")
    attempt = add_context.AddAttempt("one", "zhipu")
    with add_context.bind(attempt): om.add_account_if_identity_absent(entry)
    attempt.cancel()  # Exit does not revert the already saved account route.
    key = om.get_account_key(entry)
    entry = copy.deepcopy(om.get_account(key))
    with add_context.bind(None):
        assert zh_auth.project_choices(entry, account_key=key)[0]["project_id"] == "wire-project"
        assert zh_auth.resolve_model_key(entry, account_key=key, force=True)
    assert any("api_keys" in url for _, _, url in calls)
    assert any("access_tokens" in url for _, _, url in calls)
    assert all(p == "socks5://127.0.0.1:18001" for p, _, _ in calls)


def test_inflight_authorization_cancelled_before_commit_cannot_save(isolated, monkeypatch):
    started, release = threading.Event(), threading.Event()
    selected(); menu.on_set_rt_xai_start(42, 1, "cb")
    def exchange(rt):
        assert route("oauth_xai") == ["one"]
        started.set(); assert release.wait(3)
        return {"access_token": "at", "refresh_token": "rt", "email": "cancel@example.test", "subject": "cancel-user"}
    # Keep real facade, input and save orchestration around the blocked I/O.
    monkeypatch.setattr(xai, "refresh_sync", exchange)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(menu.on_set_rt_xai_input, 42, "refresh-token-long-enough")
        assert started.wait(3)
        sessions.before_callback(42, "menu:main")
        release.set(); future.result()
    assert not om.list_accounts() and not config.get()["network"]["routing"]["accounts"]


def test_zhipu_rename_failure_restores_proxy_rule(monkeypatch):
    shell = credential("oauth", subject="rollback-shell", model_key="", organization_id="", project_id="")
    attempt = add_context.AddAttempt("one", "zhipu")
    with add_context.bind(attempt): om.add_account_if_identity_absent(shell)
    key = om.get_account_key(shell)
    saved = copy.deepcopy(om.get_account(key)); before = copy.deepcopy(config.get())
    monkeypatch.setattr(state_db, "rename_runtime_channel_state", Mock(side_effect=OSError("offline rename failure")))
    with add_context.bind(attempt), pytest.raises(OSError):
        zh_runtime.bind_project(key, saved, dict(saved, organization_id="org", project_id="project"))
    assert config.get()["oauthAccounts"] == before["oauthAccounts"]
    assert config.get()["network"] == before["network"]


@pytest.mark.parametrize("target", [None, "direct"])
def test_inherit_adds_no_exclusive_rule_and_direct_is_durable(target):
    entry = _entry("claude")
    with add_context.bind(add_context.AddAttempt(target)):
        result = om.add_account_if_identity_absent(entry)
    key = result["account_key"]
    assert config.get()["network"]["routing"]["accounts"] == ({} if target is None else {"oauth:" + key: "direct"})
    assert route("oauth_anthropic", "oauth:" + key) == (["two"] if target is None else ["direct"])


def test_atomic_batch_failure_rolls_back_routes_and_success_publishes_together():
    entries = [dict(_entry("claude"), email=f"batch-{n}@example.test") for n in range(2)]
    batch = [{"candidate_id": str(n), "entry": entry} for n, entry in enumerate(entries)]
    before = copy.deepcopy(config.get())
    broken = copy.deepcopy(batch); broken[1]["entry"]["refresh_token"] = ""
    with add_context.bind(add_context.AddAttempt("group")):
        with pytest.raises(ValueError):
            om.commit_import_batch_if_unchanged([], broken, {"0": "add", "1": "add"})
        assert config.get() == before
        result = om.commit_import_batch_if_unchanged([], batch, {"0": "add", "1": "add"})
    assert result["status"] == "committed"
    assert config.get()["network"]["routing"]["accounts"] == {"oauth:" + key: "group" for key in result["added"]}


@pytest.mark.parametrize("provider", ["workbuddy", "zhipu"])
@pytest.mark.parametrize("confirm", [False, True])
def test_device_duplicate_ui_requires_real_confirmation(isolated, wire, monkeypatch, provider, confirm):
    state, calls = wire; state["provider"] = provider
    entry = (wb_auth.normalize_credential(dict(realm="cn", uid="wire-user", access_token="old-at", refresh_token="old-rt"))
             if provider == "workbuddy" else credential("oauth", subject="wire-user"))
    om.add_account(entry); key = om.get_account_key(entry)
    config.update(lambda c: c["network"]["routing"]["accounts"].update({"oauth:" + key: "two"}))
    before = copy.deepcopy(config.get())
    monkeypatch.setattr(zh, "_initialize_account", Mock())
    selected()
    menu.handle_callback(42, 1, "cb", "oa:wb:login" if provider == "workbuddy" else "oa:zh:login:bigmodel")
    data = states.get_state(42)["data"]
    if provider == "workbuddy": wb._start_login_worker.call_args.args[0]()
    else: zh._wait_for_login(42, 1, data, data["flow"])
    assert "确认覆盖" in repr(rows()) and config.get() == before
    callback = ("oa:wb:" if provider == "workbuddy" else "oa:zh:") + ("replace:" if confirm else "cancel:") + data["nonce"]
    menu.handle_callback(42, 1, "cb", callback)
    if provider == "workbuddy" and confirm: wb._start_login_worker.call_args.args[0]()
    if confirm:
        assert config.get()["network"]["routing"]["accounts"] == {"oauth:" + key: "one"}
        assert om.get_account(key)["access_token"] != entry["access_token"]
    else:
        assert config.get() == before and sessions.get(42).target == "one"
    assert calls and all(p == "socks5://127.0.0.1:18001" for p, _, _ in calls)
