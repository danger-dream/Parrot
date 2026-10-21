"""Correct-behavior regressions derived from the independent integration audit."""
import copy
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from types import SimpleNamespace

import httpcore
import httpx
import pytest
from pydantic import ValidationError

from src import config, network, state_db, oauth_manager as om
from src.oauth.qoder import auth, common, device_identity as di, device_profile, umid_protocol
from src.oauth import add_context
from src.management_control import ManagementError
from src.management_control.oauth import OAuthControl, OAuthProvider
from src.tests.test_workbuddy_lifecycle import context

@pytest.fixture
def isolated(monkeypatch):
    before = copy.deepcopy(config.get())
    config.update(lambda c: c.update(oauthAccounts=[], channels=[], network={"proxies": {
        "one": {"type": "socks5", "url": "socks5://127.0.0.1:18001"},
        "two": {"type": "socks5", "url": "socks5://127.0.0.1:18002"}},
        "routing": {"default": "one", "directFallback": True}}))
    state_db.init()
    di._pending.clear()
    ctl = OAuthControl()
    monkeypatch.setattr(ctl, "_post_save_account_effects", lambda *a, **k: {})
    yield ctl
    di._pending.clear()
    config.update(lambda c: (c.clear(), c.update(before)))

def account():
    p = device_profile.generate()
    return auth.normalize_credential({"realm": "global", "uid": p["machine_id"], "machine_id": p["machine_id"],
        "qoder_device_profile": p, "access_token": "fixture-at", "refresh_token": "fixture-rt"})

def confirm(ctl, f, **kw):
    return ctl.confirm_qoder_device(context(), f.flow_id, f.flow_secret, f.device_preview["revision"], **kw)

@pytest.mark.parametrize("phase", ["preparing", "confirmed"])
def test_api_poll_accepts_real_new_phases(tmp_path, phase):
    from src.tests.test_management_oauth_api import auth_client
    client, headers, _, ctl, _ = auth_client(tmp_path)
    root = "/api/management/v1/oauth/login-flows"
    try:
        r = client.post(root, headers=headers, json={"provider": "qoder", "loginMethod": "pat"})
        assert r.status_code == 201
        f = r.json()["data"]
        if phase == "confirmed":
            r = client.post(root + "/" + f["flowId"] + "/qoder-device/confirm", headers=headers,
                json={"flowSecret": f["flowSecret"], "revision": f["devicePreview"]["revision"]})
            assert r.status_code == 200
        r = client.post(root + "/" + f["flowId"] + "/poll", headers=headers, json={"flowSecret": f["flowSecret"]})
        assert r.status_code == 200 and r.json()["data"]["status"] == phase
    finally:
        client.__exit__(None, None, None)


def test_pat_retry_keeps_exchange_after_userinfo_failure(isolated, monkeypatch):
    ctl = isolated
    calls = []
    def request(url, **kw):
        calls.append(url.rsplit("/", 1)[-1])
        if url.endswith("exchange"):
            return 200, {"token": "fixture-issued", "refresh_token": "fixture-issued-r"}
        raise common.QoderError("userinfo", kind="network")
    monkeypatch.setattr(common, "request", request)
    f = ctl.start_login_flow(context(), OAuthProvider.QODER, login_method="pat")
    for _ in range(2):
        with pytest.raises(ManagementError) as e:
            confirm(ctl, f, pat="pt-offline-fixture")
        assert e.value.code.value == "UPSTREAM_ERROR"
    assert calls == ["exchange", "userinfo", "userinfo"]
    assert not config.get()["oauthAccounts"]
    assert ctl._flows.qoder.poll(context().actor.subject_id, f.flow_id, f.flow_secret).status == "identity_pending"


def test_environment_proxy_cannot_split_qoder_exit(isolated, monkeypatch):
    config.update(lambda c: c.update(network={"routing": {"default": "direct"}}))
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://audit-env-proxy.invalid:18888")
    destinations = []
    def connect(self, host, port, *args, **kwargs):
        destinations.append((host, port))
        raise httpcore.ConnectError("offline stop before socket")
    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", connect)
    with pytest.raises(httpx.ConnectError):
        with network.sync_client(proxy_purpose="oauth_qoder", proxy_provider="qoder", timeout=.01) as client:
            client.get("https://openapi.qoder.sh/api/v1/userinfo")
    with pytest.raises(httpcore.ConnectError):
        network.open_sync_stream("pum.m.alibaba.com", 443, timeout=.01, proxy_purpose="oauth_qoder", proxy_provider="qoder")
    assert destinations == [("openapi.qoder.sh", 443), ("pum.m.alibaba.com", 443)]


def fake_umid_stream(monkeypatch, sent, *, fail_one=False):
    from src.proxy.connector import SOCKS5Connector
    class Stream:
        def __init__(self, name):
            self.name = name
            self.chunks = [b"HTTP/1.0 200 OK\r\n\r\n{}", b""]
        def sendall(self, raw):
            assert raw.startswith(b"POST /repPc.json?")
            sent.append(self.name)
        def recv(self, n): return self.chunks.pop(0)
        def settimeout(self, n): pass
        def close(self): pass
    def opening(self, host, port, **kw):
        assert (host, port) == ("pum.m.alibaba.com", 443)
        if fail_one and self.name == "one":
            raise OSError("offline first proxy failure")
        return Stream(self.name)
    monkeypatch.setattr(SOCKS5Connector, "open_sync_stream", opening)
    monkeypatch.setattr(common, "_network_disabled", lambda: False)
    monkeypatch.setattr(umid_protocol, "parse_response", lambda body: "ticket-via-" + sent[-1])


def test_reload_preserves_selected_connector_and_cache(isolated, monkeypatch):
    a = account()
    sent = []
    fake_umid_stream(monkeypatch, sent)
    original = di._fetch
    def fetch_after_reload(*args):
        config.update(lambda c: c["network"]["routing"].update(default="two"))
        return original(*args)
    monkeypatch.setattr(di, "_fetch", fetch_after_reload)
    from src.oauth.qoder import routes
    with routes.bind(routes.chain("account-a")[0]):
        key_one = di._binding(a, "account-a")[0]
    assert di.headers(a, account_key="account-a")["Cosy-MachineToken"] == "ticket-via-one"
    assert state_db.qoder_identity_load(key_one)["token"] == "ticket-via-one"
    config.update(lambda c: c["network"]["routing"].update(default="one"))
    assert di.headers(a, account_key="account-a")["Cosy-MachineToken"] == "ticket-via-one"
    assert sent == ["one"]


def test_group_fallback_prepares_effective_connector_identity(isolated, monkeypatch):
    from src.proxy import manager
    from src.transports.http_runtime import _resolve_http_route_chain
    a = account()
    config.update(lambda c: (c["network"].update(groups={"exit-group": ["one", "two"]}),
        c["network"]["routing"].update(default="exit-group")))
    sent = []
    fake_umid_stream(monkeypatch, sent, fail_one=True)
    # Identity is prepared inside each business physical attempt, never independently.
    # Full model path; preserve real route resolution/client choice, replace only HTTP I/O.
    from src.oauth.qoder import catalog
    original_client = httpx.Client
    http_sent = []
    def offline_client(**opts):
        proxy = opts.pop("proxy", None)
        name = "one" if str(proxy).endswith(":18001") else "two"
        def send(req):
            http_sent.append((name, req.headers["Cosy-MachineToken"]))
            return httpx.Response(200, json={"app": [{"key": "offline-model", "enable": True}]})
        return original_client(transport=httpx.MockTransport(send), **opts)
    monkeypatch.setattr(network.httpx, "Client", offline_client)
    assert catalog.fetch_models_sync(a, account_key="account-a")[0]["id"] == "offline-model"
    assert http_sent == [("two", "ticket-via-two")]
    assert sent == ["two"]



def test_cancel_inflight_pat_prevents_save(isolated, monkeypatch):
    ctl = isolated
    entered, release = threading.Event(), threading.Event()
    a = account()
    def login(pat, *, realm, profile, pending=None):
        entered.set()
        assert release.wait(5)
        return dict(a, machine_id=profile["machine_id"], qoder_device_profile=profile)
    monkeypatch.setattr(ctl.backend, "qoder_pat_login", login)
    f = ctl.start_login_flow(context(), OAuthProvider.QODER, login_method="pat")
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(confirm, ctl, f, pat="pt-fixture")
        assert entered.wait(5)
        ctl.cancel_login_flow(context(), f.flow_id, f.flow_secret)
        release.set()
        with pytest.raises(ManagementError): pending.result()
    assert ctl.poll_login_flow(context(), f.flow_id, f.flow_secret).status == "cancelled"
    assert not config.get()["oauthAccounts"]


def test_binding_persists_across_state_restart_and_same_profile_other_owner(isolated, monkeypatch):
    a = account()
    issued = []
    monkeypatch.setattr(di, "_fetch", lambda *args: issued.append(args[-1]) or "ticket-" + str(len(issued)))
    first = di.headers(a, account_key="a")
    state_db.close()
    state_db.init()
    assert di.headers(a, account_key="a") == first
    assert di.headers(a, account_key="b")["Cosy-MachineToken"] != first["Cosy-MachineToken"]
    assert issued == ["a", "b"]


@pytest.mark.parametrize("reason", ["user", "quota", "auth_error", None])
def test_relogin_preserves_local_fields_and_rejects_stale_cas(isolated, reason):
    old = account()
    old.update(label="local", models=["old-model"], disabledModels=["old-model"], maxConcurrent=7,
        account_model_catalog={"models": [{"id": "old-model"}]}, qoder_auto_checkin=True,
        enabled=reason is None, disabled_reason=reason, disabled_until=123 if reason else None,
        tags=["local-tag"], extra_setting={"keep": True})
    config.update(lambda c: c.update(oauthAccounts=[old]))
    key = om.get_account_key(old)
    before = copy.deepcopy(om.get_account(key))
    incoming = auth.normalize_credential(dict(before, access_token="new-at", label="login-default"), source="login")
    result = om.replace_exact_identity(key, incoming, expected_account=before, authenticated_login=True)
    assert result["status"] == "replaced"
    saved = om.get_account(key)
    for field in ("label", "models", "disabledModels", "maxConcurrent", "account_model_catalog", "qoder_auto_checkin", "tags", "extra_setting", "qoder_device_profile"):
        assert saved[field] == before[field]
    assert saved["enabled"] is (reason in (None, "auth_error"))
    assert om.replace_exact_identity(key, incoming, expected_account=before, authenticated_login=True)["status"] == "revision_conflict"


def test_relogin_preserves_legacy_socks5(isolated, monkeypatch):
    ctl = isolated
    a = account()
    config.update(lambda c: c.update(oauthAccounts=[a], network={"socks5": {
        "enabled": True, "url": "socks5://127.0.0.1:18001"}, "routing": {"default": "direct"}}))
    key = om.get_account_key(a)
    destinations = []
    def connect(self, host, port, *args, **kwargs):
        destinations.append((host, port))
        raise httpcore.ConnectError("offline stop before socket")
    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", connect)
    def probe():
        with pytest.raises(httpx.ConnectError):
            with network.sync_client(proxy_purpose="oauth_qoder", proxy_provider="qoder", proxy_channel="oauth:" + key) as client:
                client.get("https://openapi.qoder.sh/api/v1/userinfo")
    probe()
    f = ctl.start_login_flow(context(), OAuthProvider.QODER, relogin_account_id=key)
    with ctl._flows.qoder.lease(context().actor.subject_id, f.flow_id, f.flow_secret):
        probe()
    assert destinations == [("127.0.0.1", 18001), ("127.0.0.1", 18001)]


def test_secret_capability_actor_and_flow_secret_are_independent(isolated):
    from src.management_auth.principal import ManagementPrincipal, AuthMethod, Capability
    from src.management_control.context import ManagementContext
    ctl = isolated
    a = ctl.start_login_flow(context(), OAuthProvider.QODER)
    b = ctl.start_login_flow(context(), OAuthProvider.QODER)
    reader = ManagementContext("offline", ManagementPrincipal.with_capabilities(
        subject_id=context().actor.subject_id, auth_method=AuthMethod.MANAGEMENT_KEY,
        capabilities=[Capability.READ, Capability.WRITE], issued_at=datetime.now(timezone.utc)))
    for ctx, secret in [(reader, a.flow_secret), (context("other-actor"), a.flow_secret), (context(), b.flow_secret)]:
        with pytest.raises(ManagementError):
            ctl.confirm_qoder_device(ctx, a.flow_id, secret, a.device_preview["revision"])
    assert ctl.poll_login_flow(context(), a.flow_id, a.flow_secret).status == "preparing"


def test_umid_save_failure_restart_retains_intent_not_unsaved_ticket(isolated, monkeypatch):
    a = account()
    issued = []
    monkeypatch.setattr(di, "_fetch", lambda *args: issued.append(1) or "issued-fixture")
    real_save = state_db.qoder_identity_save
    def save(key, value):
        if "token" in value:
            raise OSError("offline disk failure")
        return real_save(key, value)
    monkeypatch.setattr(state_db, "qoder_identity_save", save)
    with pytest.raises(common.QoderError, match="identity_store_unavailable"):
        di.headers(a, account_key="a")
    # Emulate process loss of volatile pending state, retaining actual durable snapshot.
    di._pending.clear()
    state_db.close()
    state_db.init()
    monkeypatch.setattr(state_db, "qoder_identity_save", real_save)
    with pytest.raises(common.QoderError, match="umid_backoff"):
        di.headers(a, account_key="a")
    assert issued == [1]


def test_old_durable_snapshot_upgrades_with_empty_qoder_domains(tmp_path):
    import json
    import stat
    from src.state_store import StateStore
    runtime, durable = tmp_path / "runtime.json", tmp_path / "durable.json"
    payload = {"workbuddy_actions": {"old": {"status": "keep"}}}
    durable.write_text(json.dumps(StateStore._envelope("durable", 1, payload)))
    s = StateStore(str(runtime), str(durable))
    try:
        s.start()
        assert s.get("workbuddy_actions", "old") == {"status": "keep"}
        assert s.get("qoder_identities", "absent") is None
        s._mutate("qoder_identities", lambda d: d.update(offline={"token": "fixture"}), strict=True)
        assert stat.S_IMODE(durable.stat().st_mode) == 0o600
    finally:
        s.close()


def test_refresh_save_failure_process_loss_retries_save_only(isolated, monkeypatch):
    from src.oauth.qoder import runtime
    a = account()
    config.update(lambda c: c.update(oauthAccounts=[a]))
    key = om.get_account_key(a)
    spent = []
    def refresh(current, *, account_key):
        spent.append(current["refresh_token"])
        return {"access_token": "rotated-at", "refresh_token": "rotated-rt", "expired": "2099-01-01T00:00:00Z"}
    monkeypatch.setattr(auth, "refresh_sync", refresh)
    real_save = om._save_token_fields
    monkeypatch.setattr(om, "_save_token_fields", lambda *a, **k: (_ for _ in ()).throw(OSError("offline disk failure")))
    with pytest.raises(common.QoderError, match="save_failed_retry_save"):
        om._refresh_sync_locked(key, True)
    assert runtime._refresh_states[key].pending["refresh_token"] == "rotated-rt"
    runtime.forget(key)  # Drop process state, reopen private store, keep old config.
    state_db.close()
    state_db.init()
    monkeypatch.setattr(om, "_save_token_fields", real_save)
    om._refresh_sync_locked(key, True)
    assert spent == ["fixture-rt"]
    assert om.get_account(key)["refresh_token"] == "rotated-rt"
    runtime.forget(key)

# Capture the real function before the chat env fixture supplies a simple fake.
_real_headers = di.headers
from src.tests.test_qoder_channel import env, call, request_body, frames


async def test_chat_member_fallback_and_reload_share_physical_identity(env, monkeypatch):
    from src.oauth.qoder import routes
    from src.proxy.connector import SOCKS5Connector
    from src.tests import test_protocol_fake_upstreams as fake
    monkeypatch.setattr(di, 'headers', _real_headers)
    config.update(lambda c: c.update(network={'proxies': {
        'one': {'type': 'socks5', 'url': 'socks5://127.0.0.1:18001'},
        'two': {'type': 'socks5', 'url': 'socks5://127.0.0.1:18002'}},
        'groups': {'g': ['one', 'two']}, 'routing': {'default': 'g'}}))
    issued, sent = [], []
    def fetch(*a):
        selected = routes.current()
        issued.append((selected.name, selected.connector.config_dict()))
        if selected.name == 'one':
            raise common.QoderError('device_identity', kind='umid_unavailable')
        config.update(lambda c: c['network']['routing'].update(default='direct'))
        assert routes.current() is selected
        return 'ticket-' + selected.name
    monkeypatch.setattr(di, '_fetch', fetch)
    def client(connector, **kw):
        def send(req):
            sent.append((connector.name, req.headers['Cosy-MachineToken']))
            return httpx.Response(200, stream=fake.ChunkedByteStream(frames()), headers={'content-type': 'text/event-stream'})
        return httpx.AsyncClient(transport=httpx.MockTransport(send))
    monkeypatch.setattr(SOCKS5Connector, 'create_httpx_client', client)
    response, text, _ = await call(env, 'chat', request_body('chat', False), lambda req: pytest.fail('no shared direct client'))
    assert response.status_code == 200, text
    assert [name for name, _ in issued] == ['one', 'two']
    assert sent == [('two', 'ticket-two')]


def test_refresh_state_is_projected_through_strict_api_and_tg(isolated):
    from src.management_api.schemas.oauth_qoder import QoderViewData
    from src.management_control.oauth.contracts import public_value
    from src.oauth.qoder import runtime
    from src.telegram.menus.qoder_oauth_menu import usage_block
    a = account()
    config.update(lambda c: c.update(oauthAccounts=[a]))
    key = om.get_account_key(a)
    state_db.qoder_refresh_save(runtime._journal_key(a, key, 'job'), {'phase': 'unknown'})
    snapshot = isolated.backend.qoder_snapshot(key)
    view = QoderViewData(**public_value(snapshot, camel_case_keys=True))
    assert view.refreshStatus['job'] == 'unknown'
    assert '禁止重放' in usage_block(key, detail=True, snapshot=snapshot)


def test_qoder_ss2022_sync_client_retains_connector_owned_trust_env(isolated):
    from src.oauth.qoder import routes
    from src.proxy.connector import SS2022Connector
    connector = SS2022Connector('fixture', 'proxy.invalid', 8388, '2022-blake3-aes-128-gcm', 'fixture-password')
    with routes.bind(routes.Route('fixture', connector)):
        with network.sync_client(proxy_provider='qoder', proxy_purpose='oauth_qoder') as client:
            assert client._trust_env is False  # construction/close only; no request or socket
