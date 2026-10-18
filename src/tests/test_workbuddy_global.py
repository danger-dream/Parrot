"""International WorkBuddy profile: real wire/control/TG code, fixture transport only."""
from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from src import config, oauth_manager as om, state_db
from src.management_control.oauth import CompleteOAuthLoginCommand, OAuthControl, OAuthProvider
from src.oauth.workbuddy import auth, billing, catalog, common
from src.telegram import states
from src.telegram.menus import oauth_menu, workbuddy_oauth_menu as wb
from src.tests.test_workbuddy_actions import action_env
from src.tests.test_workbuddy_lifecycle import account, context
from src.tests.test_workbuddy_management_api import api
from src.tests.test_workbuddy_telegram import tg, buttons, click


@pytest.fixture
def wire(monkeypatch):
    before = copy.deepcopy(config.get())
    state_db.init()
    config.update(lambda c: c.update(oauthAccounts=[], channels=[], oauth={**c.get("oauth", {}), "mockMode": False}))
    monkeypatch.delenv("DISABLE_OAUTH_NETWORK_CALLS", raising=False)
    monkeypatch.delenv("PARROT_NO_REFRESH", raising=False)
    requests, contexts = [], []
    options = {"pending": False, "identity_failures": 0, "expires_in": 3600, "identity": {"uid": "global-fixture-uid", "nickname": "国际样本"}}
    def transport(request):
        requests.append(request)
        assert request.url.host == "www.codebuddy.ai"
        path = request.url.path
        if path.endswith("/auth/state"):
            assert request.url.params["platform"] == "ide"
            assert request.headers["x-no-authorization"] == "true"
            assert "authorization" not in request.headers and "x-refresh-token" not in request.headers
            return httpx.Response(200, json={"code": 0, "data": {"state": "fixture state/+", "authUrl": "https://www.codebuddy.ai/login?state=fixture"}},
                                  headers={"Set-Cookie": "fixture-session=flow; Path=/; Secure"})
        if path.endswith("/auth/token"):
            assert request.url.params["state"] == "fixture state/+"
            assert "fixture-session=flow" in request.headers.get("cookie", "")
            if options["pending"]:
                options["pending"] = False
                return httpx.Response(200, json={"code": 11217, "msg": "authorization pending"})
            return httpx.Response(200, json={"code": 0, "data": {
                "accessToken": "fixture-global-at", "refreshToken": "fixture-global-rt",
                "domain": "www.codebuddy.ai", "expiresIn": options["expires_in"],
            }})
        if path.endswith("/login/account"):
            assert request.headers["authorization"] == "Bearer fixture-global-at"
            assert "x-no-authorization" not in request.headers
            if options["identity_failures"]:
                options["identity_failures"] -= 1
                return httpx.Response(503)
            return httpx.Response(200, json={"code": 0, "data": options["identity"]})
        if path.endswith("/auth/token/refresh"):
            assert request.method == "POST" and json.loads(request.content) == {}
            assert request.headers["x-refresh-token"] == "fixture-global-rt"
            assert request.headers["x-auth-refresh-source"] == "plugin"
            assert request.headers["x-domain"] == "www.codebuddy.ai"
            assert "authorization" not in request.headers
            return httpx.Response(200, json={"code": 0, "data": {"accessToken": "fixture-refreshed-at"}})
        raise AssertionError(path)
    def client(**kwargs):
        contexts.append(kwargs)
        assert kwargs["follow_redirects"] is False
        return httpx.Client(transport=httpx.MockTransport(transport), cookies=kwargs.get("cookies"))
    monkeypatch.setattr(common.network, "sync_client", client)
    yield requests, options, contexts
    config.update(lambda c: (c.clear(), c.update(before)))


def test_global_device_flow_pending_identity_retry_persistence_and_refresh(wire, monkeypatch):
    requests, options, _ = wire
    options.update(pending=True, identity_failures=1)
    clock = [datetime.now(timezone.utc)]
    ctl = OAuthControl(clock=lambda: clock[0])
    monkeypatch.setattr(ctl, "_post_save_account_effects", lambda *a, **k: {})
    ctx = context()
    flow = ctl.start_login_flow(ctx, OAuthProvider.WORKBUDDY, realm="global", client_profile="ide")
    assert "国际区" in flow.instruction and flow.auth_url.startswith("https://www.codebuddy.ai/")
    assert ctl.poll_login_flow(ctx, flow.flow_id, flow.flow_secret).status == "pending"
    clock[0] += timedelta(seconds=3)
    assert ctl.poll_login_flow(ctx, flow.flow_id, flow.flow_secret).status == "identity_pending"
    clock[0] += timedelta(seconds=3)
    preview = ctl.poll_login_flow(ctx, flow.flow_id, flow.flow_secret)
    assert preview.status == "completed" and preview.account_preview["realm"] == "global"
    assert preview.save_status == "created" and om.get_account(preview.account_id) is not None
    assert "fixture-global-at" not in str(preview) and "fixture-global-rt" not in str(preview)
    result = ctl.complete_login_flow(ctx, flow.flow_id, flow.flow_secret, CompleteOAuthLoginCommand(completed=True))
    saved = om.get_account(result.account_id)
    assert saved["realm"] == "global" and saved["workbuddy_client_profile"] == "ide"
    assert saved["expired"] and saved["access_token"] == "fixture-global-at"
    assert not saved.get("cookies") and "fixture-session" not in json.dumps(saved)
    assert sum(r.url.path.endswith("/auth/token") for r in requests) == 2
    assert sum(r.url.path.endswith("/login/account") for r in requests) == 2
    assert om._refresh_sync_locked(result.account_id, True) == "fixture-refreshed-at"
    assert om.get_account(result.account_id)["refresh_token"] == "fixture-global-rt"
    assert om.get_account(result.account_id)["expired"] == ""  # unknown, not guessed 24h


def test_global_unknown_expiry_and_missing_identity_do_not_become_login(wire):
    _, options, _ = wire
    options.update(expires_in=None, identity={"nickname": "Not an identity"})
    flow = auth.start_login_sync(realm="global", client_profile="ide")
    with pytest.raises(ValueError, match="uid"):
        auth.poll_login_sync(flow)
    assert flow["status"] == "identity_pending" and "entry" not in flow
    options["identity"] = {"uid": "global-fixture"}
    assert auth.poll_login_sync(flow)["expired"] == ""


@pytest.mark.parametrize("realm,url", [
    ("global", "https://www.codebuddy.cn/login"), ("cn", "https://www.codebuddy.ai/login"),
    ("global", "https://www.codebuddy.ai.evil.test/login"), ("global", "http://www.codebuddy.ai/login"),
    ("global", "https://attacker@www.codebuddy.ai/login"), ("global", "https://www.codebuddy.ai:444/login"),
])
def test_authorization_url_is_region_bound(realm, url):
    with pytest.raises(ValueError):
        common.safe_auth_url(url, realm=realm)


@pytest.mark.parametrize("realm,profile", [("cn", "ide"), ("global", "cli"), ("unknown", None)])
def test_login_rejects_wrong_profile_before_network(realm, profile, monkeypatch):
    monkeypatch.setattr(common, "request", lambda *a, **k: pytest.fail("must not contact upstream"))
    with pytest.raises(ValueError):
        auth.start_login_sync(realm=realm, client_profile=profile)


def test_global_profile_headers_and_legacy_profile_are_distinct():
    account = {"realm": "global", "uid": "u", "access_token": "fixture-at", "refresh_token": "fixture-rt"}
    assert common.api_base_url(account) == "https://www.workbuddy.ai"
    assert common.headers(account)["User-Agent"] == common.LEGACY_USER_AGENT
    account["workbuddy_client_profile"] = "ide"
    assert common.api_base_url(account) == common.billing_base_url(account) == "https://www.codebuddy.ai"
    headers = common.headers(account)
    assert headers["User-Agent"] == common.GLOBAL_CHAT_USER_AGENT
    assert headers["X-IDE-Name"] == "CodeBuddyIDE" and "x-codebuddy-request" not in headers
    assert headers["Authorization"] == "Bearer fixture-at" and "X-No-Authorization" not in headers
    for kind in ("anonymous", "refresh", "account", "billing"):
        assert common.headers(account, kind)["User-Agent"] == common.GLOBAL_AUTH_USER_AGENT
    with pytest.raises(ValueError, match="mismatch"):
        common.headers(dict(account, domain="www.codebuddy.cn"))


def test_global_dynamic_catalog_uses_only_authenticated_client_models(monkeypatch):
    account = {"realm": "global", "workbuddy_client_profile": "ide"}
    monkeypatch.setattr(common, "request", lambda *a, **k: {
        "models": [{"id": "auto"}, {"id": "disabled", "disabled": True}, {"id": "not-advertised"}],
        "agents": [{"name": "craft", "models": ["auto", "disabled"]}],
    })
    assert [r["id"] for r in catalog.fetch_models_sync(account)] == ["auto"]


def test_global_api_selects_profile_and_keeps_secrets_private(api, monkeypatch):
    call, key, _, ctl, clock, _ = api
    calls = []
    def start(**kwargs):
        calls.append(kwargs)
        return {"realm": "global", "workbuddy_client_profile": "ide", "state": "fixture-state",
                "status": "pending", "auth_url": "https://www.codebuddy.ai/login"}
    monkeypatch.setattr(ctl.backend, "workbuddy_start_login", start)
    response = call("POST", "/oauth/login-flows", {"provider": "workbuddy", "realm": "global", "clientProfile": "ide"})
    assert response.status_code == 201, response.text
    assert calls == [{"realm": "global", "client_profile": "ide"}]
    flow = response.json()["data"]
    entry = dict(om.get_account(key), realm="global", domain="www.codebuddy.ai", workbuddy_client_profile="ide")
    monkeypatch.setattr(ctl.backend, "workbuddy_poll_login", lambda p: p.update(status="ready", entry=entry))
    path = "/oauth/login-flows/" + flow["flowId"]
    ready = call("POST", path + "/poll", {"flowSecret": flow["flowSecret"]})
    assert ready.status_code == 200 and "fixture-at" not in ready.text and "fixture-rt" not in ready.text
    assert call("POST", path + "/complete", {"flowSecret": flow["flowSecret"], "completed": True}).status_code == 200
    assert any(a.get("realm") == "global" and a.get("workbuddy_client_profile") == "ide" for a in om.list_accounts())


def test_global_tg_automatic_login_and_saved_identity_use_international_region(tg, monkeypatch):
    key, ctl, _, output, _, _, _ = tg
    monkeypatch.setattr(ctl.backend, "workbuddy_start_login", lambda **kwargs: {
        "realm": kwargs["realm"], "workbuddy_client_profile": kwargs["client_profile"],
        "auth_url": "https://www.codebuddy.ai/login", "state": "fixture-state", "status": "pending"})
    oauth_menu.handle_callback(42, 900, "cb", "oa:add")
    click(tg, "WorkBuddy")
    assert any(b.get("callback_data") == "oa:wb:login:global" for b in buttons(output[-1][1]))
    click(tg, "CodeBuddy IDE")
    assert "国际区登录" in output[-1][0] and "Google / GitHub" in output[-1][0]
    entry = dict(om.get_account(key), realm="global", domain="www.codebuddy.ai", workbuddy_client_profile="ide")
    monkeypatch.setattr(ctl.backend, "workbuddy_poll_login", lambda p: p.update(status="ready", entry=entry))
    tg[2]["login_workers"].pop(0)()  # Authorization finishes without another click.
    assert "授权已保存" in output[-1][0]
    assert "区域: 国际区" in output[-1][0] and "中国区" not in output[-1][0]
    assert "fixture-at" not in output[-1][0] and "fixture-rt" not in output[-1][0]
    assert states.get_state(42) is None
    assert any(a.get("realm") == "global" for a in om.list_accounts())
