"""Real control/provider boundaries with adversarial secrets and offline HTTP only."""
from __future__ import annotations

import asyncio
import base64
import copy
import json
import logging
import socket
import ssl

import httpcore
import httpx
import pytest

from src import config, oauth_manager as om, oauth_errors, safe_diagnostics as diag
from src.management_control import ManagementError, ManagementErrorCode, OperationStore
from src.management_control.oauth import OAuthProvider
from src.oauth import add_context, openai, xai, antigravity, cursor
from src.oauth.workbuddy import auth as wb, common
from src.oauth.zhipu import auth as zh
from src.proxy.connector import SS2022Connector
from src.tests.test_oauth_add_proxy import isolated
from src.tests.test_workbuddy_lifecycle import context

SECRET = "NEVER_LOG_password_cookie_state_token_account"
PRIVATE_URL = "https://user:" + SECRET + "@private.example/login?state=" + SECRET
EVENT = "management_failure "


@pytest.fixture(autouse=True)
def no_socket_or_secret_output(monkeypatch, capsys, caplog):
    def blocked(*args, **kwargs):
        raise AssertionError("diagnostic tests must use mock transports, never sockets")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    yield
    output = capsys.readouterr()
    assert SECRET not in caplog.text + output.out + output.err
    assert PRIVATE_URL not in caplog.text + output.out + output.err


@pytest.fixture
def wire(isolated, monkeypatch, caplog):
    config.update(lambda c: (c.setdefault("oauth", {}).update(mockMode=False),
        c["network"]["proxies"].update({"misaka-lax": {
            "type": "ss2022", "server": "private.example", "port": 443,
            "cipher": "2022-blake3-aes-128-gcm", "password": base64.b64encode(b"fixture-key-1234!").decode(),
        }})))
    monkeypatch.delenv("DISABLE_OAUTH_NETWORK_CALLS", raising=False)
    caplog.set_level(logging.WARNING)
    calls = []
    options = {"realm": "global"}
    def respond(request):
        host = "www.codebuddy.ai" if options["realm"] == "global" else "www.codebuddy.cn"
        return httpx.Response(200, json={"code": 0, "data": {
            "state": SECRET, "authUrl": "https://" + host + "/login?state=" + SECRET,
        }}, headers={"Set-Cookie": "flow=" + SECRET + "; Path=/; Secure"})
    options["respond"] = respond
    def factory(connector, **kwargs):
        def handle(request):
            calls.append((connector.name, request))
            return options["respond"](request)
        return httpx.Client(transport=httpx.MockTransport(handle), trust_env=False, **kwargs)
    monkeypatch.setattr(SS2022Connector, "create_sync_httpx_client", factory)
    return isolated, options, calls


def start(ctl, *, realm="global"):
    with add_context.bind(add_context.AddAttempt("misaka-lax", "workbuddy")):
        return ctl.start_login_flow(context(), OAuthProvider.WORKBUDDY, realm=realm)


def events(caplog):
    records = [r for r in caplog.records if r.getMessage().startswith(EVENT)]
    for record in records:
        assert record.exc_info is None and record.stack_info is None
        assert SECRET not in repr(record.__dict__) and PRIVATE_URL not in repr(record.__dict__)
        assert "private.example" not in repr(record.__dict__)
        assert len(record.getMessage()) < 2200
    return [json.loads(r.getMessage()[len(EVENT):]) for r in records]


def fails_once(wire, caplog):
    ctl, _, calls = wire
    before = copy.deepcopy(config.get())
    with pytest.raises(ManagementError) as caught:
        start(ctl)
    assert caught.value.code is ManagementErrorCode.UPSTREAM_ERROR
    assert len(calls) == 1 and calls[0][0] == "misaka-lax"
    assert not om.list_accounts() and config.get() == before
    data, = events(caplog)
    assert data["provider"] == "workbuddy" and data["operation"] == "login_init"
    assert data["realm"] == "global" and data["selected_target"] == "misaka-lax"
    assert data["outcome"] == "unknown"
    assert SECRET not in caught.value.message and "盲目" in caught.value.message
    return data


@pytest.mark.parametrize("cls,kind", [
    (httpx.ConnectError, "connect"), (httpx.ConnectTimeout, "connect_timeout"),
    (httpx.ReadTimeout, "read_timeout"), (httpx.WriteTimeout, "write_timeout"),
    (httpx.PoolTimeout, "pool_timeout"), (httpx.ProxyError, "proxy"),
    (httpx.RemoteProtocolError, "protocol"), (httpx.ReadError, "read"),
    (httpx.WriteError, "write"), (ConnectionRefusedError, "connection_refused"),
    (ConnectionResetError, "connection_reset"), (TimeoutError, "timeout"),
])
def test_workbuddy_transport_categories_without_raw_text_or_replay(wire, caplog, cls, kind):
    def respond(request):
        raise cls(PRIVATE_URL)
    wire[1]["respond"] = respond
    data = fails_once(wire, caplog)
    assert data["stage"] == "state_request" and data["kind"] == kind
    assert data["http_status"] == 0 and data["request_state"] == "unknown"


@pytest.mark.parametrize("cause,kind,name", [
    (ssl.SSLCertVerificationError(1, PRIVATE_URL), "tls_certificate", "SSLCertVerificationError"),
    (ssl.SSLError(1, PRIVATE_URL), "tls", "SSLError"),
    (socket.gaierror(-2, PRIVATE_URL), "dns", "gaierror"),
    (httpcore.ReadTimeout(PRIVATE_URL), "read_timeout", "ReadTimeout"),
])
def test_wrapped_cause_uses_standard_safe_type_only(wire, caplog, cause, kind, name):
    def respond(request):
        try:
            raise cause
        except Exception as exc:
            raise httpx.ConnectError(PRIVATE_URL, request=request) from exc
    wire[1]["respond"] = respond
    data = fails_once(wire, caplog)
    assert data["kind"] == kind and data["exception_type"] == name


@pytest.mark.parametrize("status", [302, 403, 429, 503])
def test_http_rejection_never_logs_body_headers_or_location(wire, caplog, status):
    wire[1]["respond"] = lambda request: httpx.Response(status, text=SECRET,
        headers={"Location": PRIVATE_URL, "Set-Cookie": "flow=" + SECRET})
    data = fails_once(wire, caplog)
    assert data["kind"] == "http" and data["http_status"] == status
    assert data["business_code"] in {"absent", "unrecognized"}


@pytest.mark.parametrize("code,expected", [(11217, 11217), (12153, 12153), (98765432101234567890, "unrecognized"), (-1, "unrecognized")])
def test_only_allowlisted_business_codes_are_logged(wire, caplog, code, expected):
    wire[1]["respond"] = lambda request: httpx.Response(200, json={
        "code": code, "msg": PRIVATE_URL, "data": {"token": SECRET}})
    data = fails_once(wire, caplog)
    assert data["kind"] == "business" and data["business_code"] == expected
    assert data["http_status"] == 200


@pytest.mark.parametrize("value,kind", [
    (SECRET, "invalid_json"), ({"code": SECRET, "msg": PRIVATE_URL}, "invalid_envelope"),
    ({"code": True, "data": SECRET}, "invalid_envelope"), ([SECRET], "invalid_envelope"),
    ({"code": 0, "data": SECRET}, "invalid_data"),
])
def test_parse_failures_are_distinct_and_private(wire, caplog, value, kind):
    wire[1]["respond"] = lambda request: (httpx.Response(200, text=value) if isinstance(value, str)
                                         else httpx.Response(200, json=value))
    data = fails_once(wire, caplog)
    assert data["kind"] == kind and data["http_status"] == 200


@pytest.mark.parametrize("field,value,stage", [
    ("state", "", "state_validation"), ("state", SECRET * 100, "state_validation"),
    ("authUrl", PRIVATE_URL, "authorization_url_validation"),
    ("authUrl", "https://www.codebuddy.ai:" + SECRET + "/?state=" + SECRET, "authorization_url_validation"),
    ("authUrl", {"token": SECRET}, "authorization_url_validation"),
])
def test_state_and_url_validation_are_identifiable(wire, caplog, field, value, stage):
    wire[1]["respond"] = lambda request: httpx.Response(200, json={"code": 0, "data": {
        "state": SECRET, "authUrl": "https://www.codebuddy.ai/?state=" + SECRET, field: value}})
    data = fails_once(wire, caplog)
    assert data["stage"] == stage and data["kind"] == "validation"
    assert data["http_status"] == 200 and data["request_state"] == "response_received"


@pytest.mark.parametrize("realm", ["cn", "global"])
def test_successful_start_keeps_region_selection_and_private_flow_data(wire, caplog, realm):
    ctl, options, calls = wire; options["realm"] = realm
    before = copy.deepcopy(config.get())
    flow = start(ctl, realm=realm)
    assert SECRET in flow.auth_url and not events(caplog)
    assert len(calls) == 1 and calls[0][0] == "misaka-lax"
    request = calls[0][1]
    assert request.url.host == ("copilot.tencent.com" if realm == "cn" else "www.codebuddy.ai")
    assert request.url.params["platform"] == ("CLI" if realm == "cn" else "ide")
    assert request.method == "POST" and request.content == b"{}"
    assert config.get() == before and not om.list_accounts()


@pytest.mark.parametrize("provider,call,stage", [
    ("openai", lambda: openai.exchange_code_sync("code", "verifier"), "token_exchange"),
    ("openai", lambda: openai.refresh_sync("refresh"), "token_refresh"),
    ("xai", lambda: xai.discover_sync(), "discovery"),
    ("xai", lambda: xai.refresh_sync("refresh"), "token_refresh"),
    ("claude", lambda: om.exchange_code("code", "verifier", "state"), "token_exchange"),
])
def test_other_providers_preserve_http_diagnostics_and_safe_display(monkeypatch, caplog, provider, call, stage):
    def response(*args, **kwargs):
        req = httpx.Request("POST", PRIVATE_URL, headers={"Authorization": SECRET})
        return httpx.Response(403, json={"error": {"code": "invalid_grant", "message": SECRET}}, request=req)
    config.update(lambda c: c.setdefault("oauth", {}).update(mockMode=False))
    monkeypatch.setattr(om.network, "post_sync", response)
    monkeypatch.setattr(om.network, "get_sync", response)
    with pytest.raises(httpx.HTTPStatusError) as caught:
        call()
    data, = events(caplog)
    assert data["provider"] == provider and data["stage"] == stage and data["http_status"] == 403
    assert data["business_code"] == "invalid_grant"
    display = oauth_errors.format_oauth_error_html(caught.value, provider=provider)
    assert SECRET not in display and "403" in display and "盲目" in display
    assert len(events(caplog)) == 1


def test_local_antigravity_callback_error_does_not_leak_upstream_description(caplog):
    with pytest.raises(ValueError) as caught:
        antigravity.parse_callback_url("https://localhost/?error=access_denied&error_description=" + SECRET)
    data, = events(caplog)
    assert data["provider"] == "antigravity" and data["stage"] == "callback_parse"
    assert SECRET not in oauth_errors.format_oauth_error_html(caught.value, provider="antigravity")


def test_cursor_pending_is_intentional_not_failure(monkeypatch, caplog):
    monkeypatch.setattr(cursor, "_mock_mode_enabled", lambda: False)
    client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(404)), trust_env=False)
    monkeypatch.setattr(cursor, "_http_client", lambda **kwargs: client)
    with pytest.raises(cursor.CursorAuthPending):
        cursor.poll_login_once("uuid", "verifier")
    assert not events(caplog)


def test_shared_management_wrapper_and_operation_store_keep_safe_cause(caplog):
    store = OperationStore()
    op = store.create(context(), kind="translation.test", cancellable=False)
    store.mark_running(op.id)
    try:
        raise httpx.ReadTimeout(PRIVATE_URL)
    except Exception:
        error = ManagementError(ManagementErrorCode.UPSTREAM_ERROR, retryable=True)
        store.fail(op.id, code=error.code, message=SECRET, retryable=error.retryable)
    result = store.get(context(), op.id)
    assert result.error.code is ManagementErrorCode.UPSTREAM_ERROR and result.error.retryable
    assert "超时" in result.error.message and SECRET not in result.error.message
    assert "超时" in error.message and len(events(caplog)) == 1
    store.close()


def test_parallel_async_scopes_do_not_cross(caplog):
    async def one(provider):
        with diag.scope(provider=provider, operation="refresh", stage="token_refresh"):
            await asyncio.sleep(0)
            try:
                raise httpx.ReadError(SECRET)
            except Exception as exc:
                return diag.report(exc).provider
    async def run():
        return await asyncio.gather(one("openai"), one("workbuddy"))
    assert asyncio.run(run()) == ["openai", "workbuddy"]
    assert {e["provider"] for e in events(caplog)} == {"openai", "workbuddy"}


def test_unknown_exception_name_and_mutable_fields_cannot_escape(caplog):
    hostile = type(SECRET, (Exception,), {})(PRIVATE_URL)
    hostile.kind = {"password": SECRET}; hostile.stage = [SECRET]; hostile.code = SECRET
    result = diag.report(hostile, provider="workbuddy", stage="login_init")
    data, = events(caplog)
    assert data["exception_type"] == "Exception" and data["business_code"] == "unrecognized"
    assert SECRET not in diag.message(result)


@pytest.mark.parametrize("provider,call,stage", [
    ("antigravity", lambda: antigravity.exchange_code_sync("code", code_verifier="verifier"), "token_exchange"),
    ("antigravity", lambda: antigravity.refresh_sync("refresh"), "token_refresh"),
    ("antigravity", lambda: antigravity.fetch_userinfo_sync("access"), "profile_fetch"),
    ("cursor", lambda: cursor.poll_login_once("uuid", "verifier"), "token_poll"),
    ("cursor", lambda: cursor.refresh_sync("refresh"), "token_refresh"),
    ("cursor", lambda: cursor.fetch_profile_sync(cursor._mock_jwt("auth0|fixture", 9999999999999)), "profile_fetch"),
    ("zhipu", lambda: zh.start_login_sync(site="bigmodel"), "login_init"),
    ("zhipu", lambda: zh.project_choices({"site": "bigmodel", "credential_mode": "oauth", "access_token": "access"}), "projects"),
])
@pytest.mark.parametrize("mode,kind", [("timeout", "read_timeout"), ("http", "http"), ("json", "invalid_json")])
def test_remaining_provider_boundaries_with_sensitive_responses(monkeypatch, caplog, provider, call, stage, mode, kind):
    config.update(lambda c: c.setdefault("oauth", {}).update(mockMode=False))
    monkeypatch.delenv("DISABLE_OAUTH_NETWORK_CALLS", raising=False)
    monkeypatch.setattr(zh.c, "require_network", lambda: None)
    def respond(request):
        if mode == "timeout":
            raise httpx.ReadTimeout(PRIVATE_URL, request=request)
        return httpx.Response(403 if mode == "http" else 200, text=SECRET,
                              headers={"Set-Cookie": SECRET})
    def factory(**kwargs):
        return httpx.Client(transport=httpx.MockTransport(respond), trust_env=False)
    monkeypatch.setattr(cursor, "_http_client", factory)
    monkeypatch.setattr(zh.c.network, "sync_client", factory)
    monkeypatch.setattr(zh.c.network, "async_client", lambda **kwargs: httpx.AsyncClient(
        transport=httpx.MockTransport(respond), trust_env=False))
    def request_sync(method, url, **kwargs):
        with factory() as client:
            return client.request(method, url)
    monkeypatch.setattr(antigravity, "request_sync", request_sync)
    with pytest.raises(Exception) as caught:
        call()
    # Inner optional-read functions annotate but do not log until their caller
    # decides the result is a failure; this must still preserve their stage.
    result = diag.report(caught.value)
    assert result.provider == provider and result.stage == stage
    assert result.kind == kind
    assert SECRET not in oauth_errors.format_oauth_error_html(caught.value, provider=provider)
    assert len(events(caplog)) == 1


@pytest.mark.parametrize("provider", [OAuthProvider.OPENAI, OAuthProvider.CLAUDE, OAuthProvider.XAI, OAuthProvider.ZHIPU])
def test_json_credential_import_errors_keep_parse_stage_and_code(isolated, caplog, provider):
    from src.management_control.oauth import JsonCredential
    with pytest.raises(ManagementError) as caught:
        isolated._flows.credential_entry(JsonCredential(provider=provider, payload='{ "password": "' + SECRET))
    assert caught.value.code is ManagementErrorCode.VALIDATION_FAILED
    assert "解析" in caught.value.message and SECRET not in caught.value.message
    data, = events(caplog)
    assert data["provider"] == provider.value and data["stage"] == "import_parse"
    assert data["kind"] == "invalid_json"


def test_unknown_business_strings_and_nested_errors_never_transmit(caplog):
    for code in (SECRET, PRIVATE_URL, "a" * 20000, {"token": SECRET}, [SECRET], True, 123456789012345):
        response = httpx.Response(400, json={"error": {"code": code, "message": SECRET}},
                                  request=httpx.Request("POST", PRIVATE_URL))
        error = httpx.HTTPStatusError(SECRET, request=response.request, response=response)
        assert diag.report(error, provider="openai", stage="token_refresh").business_code == "unrecognized"
    assert len(events(caplog)) == 7


def test_failure_collector_keeps_result_fallback_but_safe_operation_reason(caplog):
    store = OperationStore()
    op = store.create(context(), kind="translation.test", cancellable=False)
    store.mark_running(op.id)
    with diag.collect_failures() as outer:
        with diag.collect_failures():
            diag.report(httpx.ReadTimeout(PRIVATE_URL), provider="translation", stage="request")
    assert len(outer) == 1
    store.fail(op.id, code=ManagementErrorCode.UPSTREAM_ERROR, message=SECRET, diagnostic=outer[0], retryable=True)
    result = store.get(context(), op.id)
    assert result.error.code is ManagementErrorCode.UPSTREAM_ERROR and result.error.retryable
    assert "等待响应超时" in result.error.message and SECRET not in result.error.message
    assert len(events(caplog)) == 1
    store.close()


def test_model_discovery_preserves_inner_http_and_parse_failures(caplog):
    from src.models_discovery import discover_models, ModelsDiscoveryError
    for response, kind in ((httpx.Response(429, text=SECRET), "http"), (httpx.Response(200, text=SECRET), "invalid_json")):
        def factory(**kwargs):
            return httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response), trust_env=False)
        with pytest.raises(ModelsDiscoveryError) as caught:
            asyncio.run(discover_models(PRIVATE_URL, SECRET, client_factory=factory))
        assert diag.find(caught.value).kind == kind
    assert len(events(caplog)) == 2


def test_proxy_probe_safe_result_and_stats_without_any_network(monkeypatch, caplog):
    from src.proxy.connector import DirectConnector
    from src.management_control.proxy.control import ProxyControl
    connector = DirectConnector()
    def factory(**kwargs):
        def fail(request):
            raise httpx.ConnectTimeout(PRIVATE_URL)
        return httpx.AsyncClient(transport=httpx.MockTransport(fail), trust_env=False)
    monkeypatch.setattr(connector, "create_httpx_client", factory)
    result = asyncio.run(connector.test_connectivity())
    assert not result["ok"] and result["diagnostic_kind"] == "connect_timeout"
    public = ProxyControl._public_probe_item(result)
    assert "连接超时" in public["error"]["message"]
    assert SECRET not in repr(result) + connector.stats.last_error
    assert len(events(caplog)) == 1


def test_logging_or_metadata_failure_never_replaces_original_error(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError(SECRET)
    monkeypatch.setattr(diag._logger, "warning", fail)
    original = httpx.ReadTimeout(PRIVATE_URL)
    assert diag.report(original, provider="workbuddy").kind == "read_timeout"
    monkeypatch.setattr(diag, "capture", fail)
    error = common.WorkBuddyError("request", kind="network")
    assert diag.inherit(error, original) is error
    assert error.kind == "network" and error.retryable


def test_tg_receipt_rejects_malicious_metadata_and_unknown_class(capsys):
    from src.telegram.error_reporting import report
    from src.oauth.zhipu.common import ZhipuError
    error = ZhipuError("projects", "business", status=200, code="123456789123",
                       target_host=PRIVATE_URL, proxy_route=PRIVATE_URL, network_phase=PRIVATE_URL)
    text = report(error, operation="组织/项目查询")
    output = capsys.readouterr().out
    assert SECRET not in text + output and PRIVATE_URL not in text + output
    assert '"business_code": "unrecognized"' in output


def test_optional_xai_metadata_error_is_safe_and_does_not_fail_required_usage(monkeypatch, caplog):
    def fail(*args, **kwargs):
        raise httpx.ReadTimeout(PRIVATE_URL)
    monkeypatch.setattr(xai, "_cli_get_json_sync", fail)
    value, reason = xai._optional_cli_get_json_sync("access", "/optional")
    assert value is None and "等待响应超时" in reason and SECRET not in reason
    assert len(events(caplog)) == 1
