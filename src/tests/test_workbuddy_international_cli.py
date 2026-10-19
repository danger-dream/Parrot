"""WorkBuddy international CLI: real login/control/catalog/channel with fixture I/O.
CLI constants: official @tencent-ai/codebuddy-code 2.168.2; compatible host:
workbuddy2api-hub 5d7200f. No live tokens or automatic activity/trial actions.
"""
from __future__ import annotations

import copy
import json
import uuid
from types import SimpleNamespace

import httpx
import pytest

from src import config, oauth_manager as om
from src.channel.workbuddy_oauth_channel import WorkBuddyOAuthChannel
from src.management_control import ManagementError
from src.management_control.oauth import OAuthControl, OAuthProvider
from src.oauth.workbuddy import auth, billing, catalog, common
from src.providers import workbuddy_context as correlation
from src.tests import test_protocol_fake_upstreams as fake
from src.tests.test_workbuddy_channel import env, request_body, ev
from src.tests.test_workbuddy_lifecycle import context, account
from src.tests.test_workbuddy_wire_upgrade import fixture_round, assistant_response, add_history
from src.tests.test_workbuddy_management_api import api
from src.tests.test_workbuddy_actions import action_env
from src.tests.test_workbuddy_telegram import tg, buttons, click
from src.telegram.menus import oauth_menu, workbuddy_oauth_menu as menu

PROFILE = "workbuddy-cli"
BASE = "https://www.workbuddy.ai"
QUOTA = {"error": {"data": {"code": 14018, "msg": "Credits exhausted. Please visit www.codebuddy.ai/profile/usage",
                              "requestId": "fixture-request"}}}


def directory():
    return {"models": [{"id": "glm-fixture", "name": "Fixture", "modelId": "ide-only-wire",
                        "supportsToolCall": True, "reasoning": {"supportedEfforts": ["low", "high"]}},
                       {"id": "denied"}, {"id": "ide-only"}],
            "agents": [{"name": "cli", "models": ["Fixture", "denied", "not-in-config"]},
                       {"name": "craft", "models": ["ide-only"]}],
            "availableModels": ["glm-fixture"]}


@pytest.fixture
async def cli(env, monkeypatch):
    m, _ = env
    monkeypatch.delenv("DISABLE_OAUTH_NETWORK_CALLS", raising=False)
    config.update(lambda c: c.update(oauthAccounts=[], oauth={**c.get("oauth", {}), "mockMode": False}))
    ide = auth.normalize_credential({"realm": "global", "workbuddy_client_profile": "ide",
        "uid": "same-global-uid", "domain": "www.codebuddy.ai", "access_token": "ide-private-at", "refresh_token": "ide-private-rt"})
    ide.update(models=["ide-only"], label="preserve IDE", disabledModels=["ide-only"])
    om.add_account(ide)
    ide_key = om.get_account_key(ide)
    old = copy.deepcopy(om.get_account(ide_key))
    requests = []

    def wire(req):
        requests.append(req)
        assert req.url.host == "www.workbuddy.ai"
        assert req.headers["user-agent"] == "CLI/2.168.2 CodeBuddy/2.168.2"
        assert req.headers["x-ide-type"] == "CLI"
        path = req.url.path
        if path.endswith("/auth/state"):
            assert req.url.params["platform"] == "CLI"
            assert req.headers["x-domain"] == "www.workbuddy.ai"
            assert "authorization" not in req.headers
            return httpx.Response(200, json={"code": 0, "data": {"state": "fixture-state", "authUrl": BASE + "/login"}},
                                  headers={"Set-Cookie": "fixture-session=flow; Path=/; Secure"})
        if path.endswith("/auth/token"):
            assert "fixture-session=flow" in req.headers.get("cookie", "")
            assert "authorization" not in req.headers and "x-domain" not in req.headers
            return httpx.Response(200, json={"code": 0, "data": {"accessToken": "cli-fixture-at", "refreshToken": "cli-fixture-rt",
                                                                "domain": "www.workbuddy.ai", "expiresIn": 7200}})
        assert req.headers["authorization"] == "Bearer cli-fixture-at"
        assert "ide-private" not in str(req.headers)
        if path.endswith("/login/account"):
            data = {"uid": "same-global-uid", "nickname": "international CLI"}
        elif path == "/v3/config":
            assert req.headers["connection"] == "close"
            data = directory()
        elif path == "/v2/billing/meter/get-user-resource":
            data = {"Response": {"Data": {"TotalCount": 0, "Accounts": None}}}
        else:
            pytest.fail("Unexpected endpoint (no trials/checkin/payment effects): " + path)
        return httpx.Response(200, json={"code": 0, "data": data})

    monkeypatch.setattr(common.network, "sync_client", lambda **kwargs:
        httpx.Client(transport=httpx.MockTransport(wire), cookies=kwargs.get("cookies")))
    # The real Node probe has its own test; a cold runner start can exceed its 2s budget.
    monkeypatch.setattr(common.subprocess, "check_output", lambda *a, **k: '["v24.14.1","linux","x64"]')
    common._workbuddy_cli_sdk_headers.cache_clear()
    ctl = OAuthControl()
    # Execute real discovery below instead of launching a UI background thread.
    monkeypatch.setattr(ctl, "_post_save_account_effects", lambda *a, **k: {})
    flow = ctl.start_login_flow(context(), OAuthProvider.WORKBUDDY, realm="global")
    assert "WorkBuddy 国际 CLI" in flow.instruction and "workbuddy.ai" in flow.instruction
    poll = ctl.poll_login_flow(context(), flow.flow_id, flow.flow_secret)
    assert poll.status == "completed" and poll.save_status == "created"
    assert poll.account_id == ide_key + ":workbuddy-cli"
    assert poll.account_preview["workbuddy_client_profile"] == PROFILE
    assert om.get_account(poll.account_id)["models"] == []
    assert om.get_account(ide_key) == old
    assert (await om._discover_account_models_once(poll.account_id, timeout_s=10))["action"] == "updated"
    saved = om.get_account(poll.account_id)
    assert saved["models"] == ["glm-fixture"]
    record = om.account_model_records(saved)[0]
    assert record["wireModel"] == "glm-fixture" and record["requestBodyGzip"] is True
    assert record["source"] == "workbuddy:cli"
    assert billing.fetch_personal_sync(saved, account_key=poll.account_id)["packages"] == []
    ch = WorkBuddyOAuthChannel(saved)
    fake._install_channels(m, [ch])
    yield SimpleNamespace(m=m, ch=ch, requests=requests, ide_key=ide_key, old=old, ctl=ctl)
    common._workbuddy_cli_sdk_headers.cache_clear()
    assert om.get_account(ide_key) == old


async def chat_call(cli, ingress, body, response):
    router = fake.MockRouter()
    def wire(req):
        assert str(req.url) == BASE + "/v2/chat/completions"
        assert req.headers["authorization"] == "Bearer cli-fixture-at"
        assert req.headers["x-private-data"] == "true"
        assert "x-agent-purpose" not in req.headers
        assert req.headers["x-agent-runtime"] == "other"
        assert req.headers["x-agent-type"] == "main"
        assert req.headers["x-ide-version"] == "2.168.2"
        assert "x-model-id" not in req.headers and "x-device-token" not in req.headers
        assert "content-encoding" not in req.headers and not req.content.startswith(b"\x1f\x8b")
        assert req.headers["x-root-request-id"] == req.headers["x-conversation-request-id"]
        assert req.headers["x-request-id"] == req.headers["x-conversation-message-id"]
        assert uuid.UUID(req.headers["x-conversation-id"])
        assert not {"traceparent", "tracestate", "baggage", "b3", "x-trace-id"} & req.headers.keys()
        assert not any(key.startswith("x-b3-") for key in req.headers)
        assert req.headers["x-stainless-lang"] == "js"
        assert req.headers["x-stainless-package-version"] == "6.25.0"
        assert req.headers["x-stainless-runtime"] == "node"
        assert req.headers["x-stainless-runtime-version"].startswith("v")
        assert req.headers["x-stainless-retry-count"] == "0"
        assert "x-stainless-timeout" not in req.headers
        payload = json.loads(req.content)
        assert payload["cache_retention"] == "long" and payload["model"] == "glm-fixture"
        assert not {"metadata", "conversation_id", "previous_response_id"} & payload.keys()
        return response(req, payload)
    router.register(BASE, wire)
    if ingress == "anthropic":
        result, client, _ = await fake._call_anthropic_core(cli.m, router, body)
    else:
        result, client = await fake._call_openai_handler(cli.m, router, ingress, body)
    try:
        text = await fake._consume_streaming_to_string(result) if hasattr(result, "body_iterator") else result.body.decode()
    finally:
        await client.aclose()
    return result, text, router.requests


@pytest.mark.parametrize("ingress", ["chat", "responses", "anthropic"])
@pytest.mark.parametrize("stream", [False, True])
async def test_login_save_catalog_billing_and_three_round_chat(cli, ingress, stream):
    body = request_body(ingress, stream)
    if ingress == "responses":
        body["input"] = [{"role": "user", "content": "read a.py"}]
    history = []
    from src.providers import workbuddy_replay
    for turn in range(3):
        raw, expected = fixture_round(turn)
        def response(req, payload):
            assistants = [m for m in payload["messages"] if m["role"] == "assistant"]
            assert [workbuddy_replay.fields(m) for m in assistants] == history
            return httpx.Response(200, content=raw, headers={"content-type": "text/event-stream"})
        result, text, calls = await chat_call(cli, ingress, body, response)
        assert result.status_code == 200 and len(calls) == 1, text
        add_history(body, ingress, assistant_response(ingress, stream, text), turn)
        history.append(expected)
    assert [r.url.path for r in cli.requests] == ["/v2/plugin/auth/state", "/v2/plugin/auth/token",
        "/v2/plugin/login/account", "/v3/config", "/v2/billing/meter/get-user-resource"]


def test_correlation_is_account_scoped_retry_stable_and_request_local():
    body = correlation.ensure_context({}, request_id="logical", api_key_name="owner")
    a = correlation.headers(body, "account-a", workbuddy_cli=True)
    retry = correlation.headers(body, "account-a", workbuddy_cli=True)
    b = correlation.headers(body, "account-b", workbuddy_cli=True)
    later = correlation.headers(correlation.ensure_context({}), "account-a", workbuddy_cli=True)
    for key in ("X-Conversation-ID", "X-Conversation-Request-ID", "X-Conversation-Message-ID", "X-Root-Request-ID"):
        assert a[key] == retry[key] and a[key] != b[key] and a[key] != later[key]
    assert a == retry
    assert set(a) == {"X-Conversation-ID", "X-Conversation-Request-ID", "X-Conversation-Message-ID", "X-Root-Request-ID", "X-Request-ID"}
    assert uuid.UUID(a["X-Request-ID"]).version == 7
    for value in ("one", "two"):
        explicit = {}
        correlation.capture_headers(explicit, {"session-id": "caller-session", "x-query-id": "caller-turn"})
        explicit = correlation.ensure_context(explicit, request_id=value)
        headers = correlation.headers(explicit, "account-a", workbuddy_cli=True)
        if value == "one":
            first = headers
        else:
            assert headers["X-Conversation-ID"] == first["X-Conversation-ID"]
            assert headers["X-Conversation-Request-ID"] == first["X-Conversation-Request-ID"]


def test_sdk_headers_use_verified_local_node_metadata():
    import subprocess
    version, system, arch = json.loads(subprocess.check_output(["node", "-p",
        "JSON.stringify([process.version,process.platform,process.arch])"], text=True, timeout=2))
    headers = common._workbuddy_cli_sdk_headers()
    assert headers["x-stainless-runtime-version"] == version
    assert headers["x-stainless-os"] == {"linux": "Linux", "darwin": "MacOS", "win32": "Windows"}[system]
    assert headers["x-stainless-arch"] == {"x64": "x64", "arm64": "arm64"}[arch]
    assert headers["x-stainless-package-version"] == "6.25.0"
    assert "x-stainless-timeout" not in headers


def test_sdk_deployment_identity_is_cached_not_randomized(monkeypatch):
    calls = []
    def probe(command, **kwargs):
        calls.append(command)
        assert command[:2] == ["node", "-p"] and kwargs["env"]["NODE_OPTIONS"] == ""
        return '["v24.14.1","linux","x64"]'
    common._workbuddy_cli_sdk_headers.cache_clear()
    monkeypatch.setattr(common.subprocess, "check_output", probe)
    try:
        first = common._workbuddy_cli_sdk_headers()
        assert first == common._workbuddy_cli_sdk_headers() and len(calls) == 1
        assert first["x-stainless-runtime-version"] == "v24.14.1"
    finally:
        common._workbuddy_cli_sdk_headers.cache_clear()


def test_missing_runtime_is_not_replaced_with_guessed_version(monkeypatch):
    def missing(*a, **kw):
        raise FileNotFoundError()
    common._workbuddy_cli_sdk_headers.cache_clear()
    monkeypatch.setattr(common.subprocess, "check_output", missing)
    try:
        with pytest.raises(common.WorkBuddyError, match="cli_runtime_unavailable"):
            common._workbuddy_cli_sdk_headers()
    finally:
        common._workbuddy_cli_sdk_headers.cache_clear()


@pytest.mark.parametrize("realm,profile", [("cn", "cli"), ("global", "ide"), ("global", "cli")])
def test_old_profiles_never_probe_or_add_sdk_identity(monkeypatch, realm, profile):
    monkeypatch.setattr(common, "_workbuddy_cli_sdk_headers", lambda: pytest.fail("old profile must not probe Node"))
    headers = common.headers({"realm": realm, "workbuddy_client_profile": profile, "uid": "fixture", "access_token": "fixture"})
    assert not any(key.lower().startswith("x-stainless-") for key in headers)
    assert "X-Agent-Purpose" not in headers


@pytest.mark.parametrize("gzip_flag", [None, False, True])
async def test_compat_endpoint_skips_gzip_without_mutating_catalog(cli, gzip_flag):
    def update(c):
        record = next(a for a in c["oauthAccounts"] if a.get("workbuddy_client_profile") == PROFILE)["account_model_catalog"]["models"][0]
        if gzip_flag is None:
            record.pop("requestBodyGzip", None)
        else:
            record["requestBodyGzip"] = gzip_flag
    config.update(update)
    before = copy.deepcopy(om.get_account(cli.ch.account_key)["account_model_catalog"])
    req = await cli.ch.build_upstream_request(request_body("chat", False), "glm-fixture", ingress_protocol="chat")
    assert "Content-Encoding" not in req.headers and json.loads(req.body)["model"] == "glm-fixture"
    assert om.get_account(cli.ch.account_key)["account_model_catalog"] == before


def test_enterprise_cli_overlay_and_no_hardcoded_fallback(monkeypatch):
    account = {"realm": "global", "workbuddy_client_profile": PROFILE, "enterprise_id": "team"}
    calls = []
    def request(account, path, **kw):
        calls.append(path)
        if path == "/v3/config":
            return directory()
        assert path == "/console/enterprises/team/config/models" and kw["allow_list"] is True
        return [{"id": "custom:team", "tags": ["chat"], "modelId": "must-not-use-ide-id"},
                {"id": "glm-fixture", "maxInputTokens": 12345, "tags": ["chat"]}]
    monkeypatch.setattr(common, "request", request)
    records = catalog.fetch_models_sync(account)
    assert [r["id"] for r in records] == ["custom:team", "glm-fixture"]
    assert records[0]["wireModel"] == "custom:team" and records[1]["maxInputTokens"] == 12345
    assert len(calls) == 2
    monkeypatch.setattr(common, "request", lambda *a, **k: {"models": [], "agents": [{"name": "cli", "models": []}]})
    with pytest.raises(common.WorkBuddyError):
        catalog.fetch_models_sync(dict(account, enterprise_id=""))


def test_busy_model_is_not_misclassified_as_balance_exhaustion():
    from src.protocols import runtime
    result = runtime.AttemptResult(outcome="upstream_http_error", http_status=429,
        full_response_text=json.dumps({"error": {"data": {"code": 14003, "msg": "Model busy"}}}))
    assert runtime.bounded_account_quota_error(result, SimpleNamespace(provider="workbuddy")) is None


def test_free_cli_model_not_replaced_by_unadvertised_sg_variant(monkeypatch):
    data = {"models": [{"id": "deepseek-v4.1-flash", "credits": "x0.00"},
                       {"id": "deepseek-v4.1-flash-sg", "credits": "x0.03"}],
            "agents": [{"name": "cli", "models": ["deepseek-v4.1-flash"]}], "productFeatures": {}}
    monkeypatch.setattr(common, "request", lambda *a, **k: data)
    records = catalog.fetch_models_sync({"realm": "global", "workbuddy_client_profile": PROFILE})
    assert [r["id"] for r in records] == ["deepseek-v4.1-flash"]
    assert records[0]["wireModel"] == "deepseek-v4.1-flash" and records[0]["requestBodyGzip"] is True


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("ingress", ["chat", "responses", "anthropic"])
async def test_nested_14018_stops_pool_without_refresh(cli, monkeypatch, stream, ingress):
    second = dict(cli.ch.account, uid="second-fixture")
    om.add_account(second)
    fake._install_channels(cli.m, [cli.ch, WorkBuddyOAuthChannel(second)])
    async def forbidden(*args, **kwargs):
        pytest.fail("quota must not refresh credentials")
    monkeypatch.setattr(om, "force_refresh", forbidden)
    result, text, calls = await chat_call(cli, ingress, request_body(ingress, stream), lambda req, payload:
        httpx.Response(429, json=QUOTA))
    assert len(calls) == 1 and result.status_code == 402, text
    assert "14018" in text and "balance_exhausted" in text
    assert "www.codebuddy.ai/profile/usage" not in text


async def test_401_replay_keeps_local_session_and_is_bounded(cli, monkeypatch):
    monkeypatch.delenv("PARROT_NO_REFRESH", raising=False)
    config.update(lambda c: c.update(retry={"recovery": {"oauthRefresh": True}}))
    refreshes = []
    async def refresh(*a, **k):
        refreshes.append(k)
        return "cli-fixture-at"
    monkeypatch.setattr(om, "force_refresh", refresh)
    result, text, calls = await chat_call(cli, "chat", request_body("chat", False), lambda req, payload:
        httpx.Response(401, json={"error": {"code": "invalid_token"}}))
    assert result.status_code != 200 and len(calls) == 2 and len(refreshes) == 1
    for key in ("x-conversation-id", "x-conversation-request-id", "x-request-id", "x-root-request-id"):
        assert calls[0].headers[key] == calls[1].headers[key]
    # Official maxRetries:0: each outer OAuth recovery call begins at SDK count 0.
    assert [call.headers["x-stainless-retry-count"] for call in calls] == ["0", "0"]
    assert {k: v for k, v in calls[0].headers.items() if k.startswith("x-stainless-")} == {
        k: v for k, v in calls[1].headers.items() if k.startswith("x-stainless-")}


@pytest.mark.parametrize("ingress", ["chat", "responses", "anthropic"])
async def test_nested_quota_after_output_never_replays(cli, ingress):
    raw = ev({"choices": [{"delta": {"content": "already emitted"}}]}) + ev(QUOTA)
    result, text, calls = await chat_call(cli, ingress, request_body(ingress, True), lambda req, payload:
        httpx.Response(200, content=raw, headers={"content-type": "text/event-stream"}))
    assert len(calls) == 1 and "already emitted" in text and '"error"' in text
    assert "14018" in text


@pytest.mark.parametrize("profile", [None, PROFILE, "ide"])
def test_management_api_selects_actual_route(api, monkeypatch, profile):
    call, _, _, ctl, *_ = api
    calls = []
    def start(*, realm, client_profile):
        calls.append((realm, client_profile))
        host = "www.codebuddy.ai" if client_profile == "ide" else "www.workbuddy.ai"
        return {"realm": realm, "workbuddy_client_profile": client_profile, "state": "fixture",
                "auth_url": "https://" + host + "/login", "status": "pending"}
    monkeypatch.setattr(ctl.backend, "workbuddy_start_login", start)
    body = {"provider": "workbuddy", "realm": "global"}
    if profile:
        body["clientProfile"] = profile
    response = call("POST", "/oauth/login-flows", body)
    if profile == "ide":
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "UNSUPPORTED_VALUE"
        assert calls == []
        return
    assert response.status_code == 201, response.text
    assert calls == [("global", profile or PROFILE)]
    assert "WorkBuddy 国际 CLI" in response.text
    data = response.json()["data"]
    call("POST", "/oauth/login-flows/" + data["flowId"] + "/cancel", {"flowSecret": data["flowSecret"]})


def test_tg_only_offers_workbuddy_international_and_rejects_stale_ide_button(tg, monkeypatch):
    calls = []
    def start(**kwargs):
        calls.append(kwargs)
        return {**kwargs, "workbuddy_client_profile": kwargs["client_profile"], "state": "fixture",
                "auth_url": BASE + "/login", "status": "pending"}
    monkeypatch.setattr(tg[1].backend, "workbuddy_start_login", start)
    oauth_menu.on_add_workbuddy(42, 900, "cb")
    callbacks = {b.get("callback_data") for b in buttons(tg[3][-1][1])}
    assert {"oa:wb:login", "oa:wb:login:global", "oa:add"} == callbacks
    assert menu.handle_callback(42, 900, "cb", "oa:wb:login:global:ide")
    assert calls == [] and "旧 CodeBuddy IDE 新增入口已停用" in tg[3][-1][0]
    oauth_menu.on_add_workbuddy(42, 900, "cb")
    click(tg, "国际区")
    assert calls == [{"realm": "global", "client_profile": PROFILE}]
    assert "WorkBuddy 国际 CLI · workbuddy.ai" in tg[3][-1][0]
    menu.discard_state(42)


def test_policy_advertises_one_international_route():
    policy = OAuthControl().workbuddy_policy(context())
    assert policy["supported_client_profiles_by_realm"] == {"cn": ["cli"], "global": [PROFILE]}
    assert list(policy["client_routes"]) == [PROFILE]


def test_account_list_shows_region_without_client_protocol(tg):
    account = {"provider": "workbuddy", "realm": "global", "workbuddy_client_profile": PROFILE,
               "uid": "fixture", "models": ["hy3"]}
    assert menu.provider_line(account) == "🏷️ 账户: 个人 · 国际区 · 模型 1"
    assert "WorkBuddy 国际 CLI · workbuddy.ai" in menu.provider_line(account, detail=True)


@pytest.mark.parametrize("profile", [PROFILE, "ide", "cli"])
def test_relogin_never_silently_changes_route(account, monkeypatch, profile):
    config.update(lambda c: c["oauthAccounts"][0].update(realm="global", domain="www.workbuddy.ai" if profile != "ide" else "www.codebuddy.ai",
        workbuddy_client_profile=profile, disabled_reason="auth_error", enabled=False))
    saved = om.list_accounts()[0]
    key = om.get_account_key(saved)
    ctl = OAuthControl()
    calls = []
    def start(**kw):
        calls.append(kw)
        return {"realm": "global", "workbuddy_client_profile": kw["client_profile"], "state": "fixture", "auth_url": BASE + "/login"}
    monkeypatch.setattr(ctl.backend, "workbuddy_start_login", start)
    if profile == "cli":
        with pytest.raises(ManagementError):
            ctl.start_login_flow(context(), OAuthProvider.WORKBUDDY, relogin_account_id=key)
        assert not calls and om.get_account(key) == saved
    else:
        flow = ctl.start_login_flow(context(), OAuthProvider.WORKBUDDY, relogin_account_id=key)
        assert calls == [{"realm": "global", "client_profile": profile}]
        ctl.cancel_login_flow(context(), flow.flow_id, flow.flow_secret)
