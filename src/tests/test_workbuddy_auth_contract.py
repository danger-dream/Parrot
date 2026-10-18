"""Static auth-contract fixtures; isolated runner and in-memory HTTP transport only."""
from __future__ import annotations

import copy
from types import SimpleNamespace

import httpx
import pytest

from src import config, oauth_manager as om, oauth_refresh_failure
from src.oauth.workbuddy import auth, common, runtime
from src.tests.test_workbuddy_lifecycle import account
from src.tests.test_workbuddy_provider import credential


@pytest.fixture
def clock(monkeypatch):
    value = [1_900_000_000.0]
    monkeypatch.setattr(auth, "time", SimpleNamespace(time=lambda: value[0]))
    return value


@pytest.fixture
def wire(monkeypatch):
    before = copy.deepcopy(config.get())
    config.update(lambda c: c.setdefault("oauth", {}).update(mockMode=False))
    monkeypatch.delenv("DISABLE_OAUTH_NETWORK_CALLS", raising=False)
    monkeypatch.delenv("PARROT_NO_REFRESH", raising=False)
    state = {"requests": []}

    def transport(request):
        state["requests"].append(request)
        return state["handler"](request)

    def client(**kwargs):
        assert kwargs["follow_redirects"] is False
        return httpx.Client(transport=httpx.MockTransport(transport), cookies=kwargs.get("cookies"))

    monkeypatch.setattr(common.network, "sync_client", client)
    yield state
    config.update(lambda c: (c.clear(), c.update(before)))


def success(data):
    return httpx.Response(200, json={"code": 0, "data": data})


@pytest.mark.parametrize("realm,profile,host,domain,platform", [
    ("cn", "cli", "copilot.tencent.com", "www.codebuddy.cn", "CLI"),
    ("global", "ide", "www.codebuddy.ai", "www.codebuddy.ai", "ide"),
    ("global", "workbuddy-cli", "www.workbuddy.ai", "www.workbuddy.ai", "CLI"),
])
def test_three_login_stages_identity_pending_and_expiry_persistence(account, wire, clock, realm, profile, host, domain, platform):
    stages = {"token": 0, "account": 0}
    token_expiry, rt_expiry = clock[0] + 120, clock[0] + 7200

    def handler(request):
        assert request.url.host == host
        headers = request.headers
        path = request.url.path
        if path.endswith("/auth/state"):
            assert request.method == "POST" and request.content == b"{}"
            assert dict(request.url.params) == {"platform": platform}
            assert headers["x-domain"] == host
            for name in ("authorization", "user-id", "enterprise-id", "department-info"):
                assert headers["x-no-" + name] == "true"
            assert "authorization" not in headers and "x-refresh-token" not in headers
            return httpx.Response(200, json={"code": 0, "data": {
                "state": "fixture state/+", "authUrl": f"https://{domain}/login?state=fixture",
            }}, headers={"Set-Cookie": "fixture-session=flow; Path=/; Secure"})
        assert request.method == "GET" and request.content == b""
        assert dict(request.url.params) == {"state": "fixture state/+"}
        assert "fixture-session=flow" in headers["cookie"]
        if path.endswith("/auth/token"):
            stages["token"] += 1
            assert "authorization" not in headers and "x-domain" not in headers
            for name in ("authorization", "user-id", "enterprise-id", "department-info"):
                assert headers["x-no-" + name] == "true"
            if stages["token"] == 1:
                return httpx.Response(200, json={"code": 11217})
            return success({"accessToken": "fixture-login-at", "refreshToken": "fixture-login-rt",
                "expiresAt": token_expiry * 1000, "expiresIn": 9000,
                "refreshExpiresAt": rt_expiry * 1000, "refreshExpiresIn": 99999,
                "method": "fixture-method", "domain": domain})
        assert path.endswith("/login/account")
        stages["account"] += 1
        assert headers["authorization"] == "Bearer fixture-login-at"
        assert headers["x-domain"] == domain and "x-no-authorization" not in headers
        for name in ("user-id", "enterprise-id", "department-info"):
            assert headers["x-no-" + name] == "true" and "x-" + name not in headers
        if stages["account"] <= 2:
            return httpx.Response(200, json={"code": 12151})
        return success({"uid": "fixture-login-uid", "enterpriseId": "fixture-team",
                        "idSource": "fixture-source", "departmentFullName": "fixture-department"})

    wire["handler"] = handler
    payload = auth.start_login_sync(realm=realm, client_profile=profile)
    assert auth.poll_login_sync(payload) == {} and payload["status"] == "pending"
    assert auth.poll_login_sync(payload) == {} and payload["status"] == "identity_pending"
    clock[0] += 40
    assert auth.poll_login_sync(payload) == {} and payload["status"] == "identity_pending"
    entry = auth.poll_login_sync(payload)
    assert stages == {"token": 2, "account": 3}
    assert entry["expired"] == auth.utc_text(token_expiry)
    assert entry["refresh_expired"] == auth.utc_text(rt_expiry)
    assert entry["workbuddy_client_profile"] == profile
    assert "token" not in payload and payload["status"] == "ready"
    assert auth.poll_login_sync(payload) == entry
    om.add_account(entry)
    saved = om.get_account(om.get_account_key(entry))
    for field in ("expired", "refresh_expired", "auth_method", "id_source", "department_full_name"):
        assert saved[field] == entry[field]
    assert "cookies" not in saved and "auth" not in saved


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("as_text", [False, True])
def test_import_absolute_milliseconds_and_legacy_seconds(nested, as_text, clock):
    value = credential()
    value.update(expiresAt=clock[0] * 1000, refreshExpiresAt=(clock[0] + 500) * 1000,
                 expiresIn=99999, refreshExpiresIn=99999)
    if as_text:
        value["expiresAt"], value["refreshExpiresAt"] = str(value["expiresAt"]), str(value["refreshExpiresAt"])
    raw = {"auth": value, "account": value} if nested else value
    result = auth.normalize_credential(raw)
    assert result["expired"] == auth.utc_text(clock[0])
    assert result["refresh_expired"] == auth.utc_text(clock[0] + 500)
    for field in ("expired", "expires_at"):
        old = auth.normalize_credential(credential(**{field: clock[0]}))
        assert old["expired"] == auth.utc_text(clock[0])
    assert auth.normalize_credential(result)["refresh_expired"] == result["refresh_expired"]


def test_relative_import_never_extends_lifetime_even_with_login_label(clock):
    for source in ("import", "login"):
        result = auth.normalize_credential(credential(expiresIn=3600, refreshExpiresIn=7200), source=source)
        assert result["expired"] == result["refresh_expired"] == ""
    assert auth.token_expiries({"expiresAt": 0, "expiresIn": 3600}, acquired_at=clock[0]) == {
        "expired": "1970-01-01T00:00:00Z"}


def test_login_relative_expiries_are_frozen_before_identity_poll(monkeypatch, clock):
    calls = []

    def request(account, path, **kwargs):
        calls.append(path)
        if "/auth/token?" in path:
            return {"accessToken": "fixture-at", "refreshToken": "fixture-rt", "expiresIn": 120,
                    "refreshExpiresIn": 7200}
        if len(calls) == 2:
            raise common.WorkBuddyError("request", code=12151)
        return {"uid": "fixture-uid"}

    monkeypatch.setattr(common, "request", request)
    flow = {"realm": "cn", "state": "fixture-state"}
    start = clock[0]
    assert auth.poll_login_sync(flow) == {}
    clock[0] += 100
    result = auth.poll_login_sync(flow)
    assert result["expired"] == auth.utc_text(start + 120)
    assert result["refresh_expired"] == auth.utc_text(start + 7200)
    assert len(calls) == 3


@pytest.mark.parametrize("values,body,source,host", [
    ({}, b"{}", "plugin", "copilot.tencent.com"),
    ({"realm": "global", "domain": "www.codebuddy.ai", "workbuddy_client_profile": "ide"},
     b"{}", "plugin", "www.codebuddy.ai"),
    ({"realm": "global", "domain": "www.workbuddy.ai", "workbuddy_client_profile": "cli"},
     b"", "workbuddy", "www.workbuddy.ai"),
])
def test_refresh_profile_wire_is_single_request(wire, clock, values, body, source, host):
    original = credential(**values)

    def handler(request):
        assert request.url.host == host and request.url.path == "/v2/plugin/auth/token/refresh"
        assert request.method == "POST" and not request.url.query and request.content == body
        assert request.headers["x-refresh-token"] == "fixture-refresh"
        assert request.headers["x-auth-refresh-source"] == source and "authorization" not in request.headers
        if source == "plugin":
            assert request.headers["x-domain"] == original["domain"]
        else:
            assert request.headers["user-agent"] == "CLI/2.63.2 CodeBuddy/2.63.2"
            assert "x-domain" not in request.headers
        return success({"accessToken": "fixture-next-at", "expiresAt": (clock[0] + 123) * 1000,
                        "expiresIn": 99999, "refreshExpiresIn": 3600})

    wire["handler"] = handler
    result = auth.refresh_sync(original["refresh_token"], account=original)
    assert result["expired"] == auth.utc_text(clock[0] + 123)
    assert result["refresh_expired"] == auth.utc_text(clock[0] + 3600)
    assert "refresh_token" not in result and len(wire["requests"]) == 1
    assert original == credential(**values)


def test_refresh_relative_seconds_and_zero_are_not_milliseconds(wire, clock):
    wire["handler"] = lambda _: success({"accessToken": "fixture-at", "expiresIn": 0, "refreshExpiresIn": 7200})
    result = auth.refresh_sync("fixture-rt", account=credential())
    assert result["expired"] == auth.utc_text(clock[0])
    assert result["refresh_expired"] == auth.utc_text(clock[0] + 7200)


@pytest.mark.parametrize("rt,expected", [(None, "kept"), ("", "kept"), ("fixture-rt", "kept"), ("rotated-rt", "cleared")])
def test_refresh_rt_omission_and_rotation_do_not_guess_lifetime(account, wire, clock, rt, expected):
    old_expiry = auth.utc_text(clock[0] + 7200)
    config.update(lambda c: c["oauthAccounts"][0].update(refresh_expired=old_expiry))
    response = {"accessToken": "fixture-next-at", "expiresAt": (clock[0] + 3600) * 1000}
    if rt is not None:
        response["refreshToken"] = rt
    wire["handler"] = lambda _: success(response)
    assert om._refresh_sync_locked(account, True) == "fixture-next-at"
    saved = om.get_account(account)
    assert saved["refresh_token"] == (rt or "fixture-rt")
    assert saved["refresh_expired"] == (old_expiry if expected == "kept" else "")
    assert saved["expired"] == auth.utc_text(clock[0] + 3600)
    assert len(wire["requests"]) == 1


def test_rotated_candidate_preserves_both_absolute_expiries_across_save_retry(account, wire, clock, monkeypatch):
    start = clock[0]
    wire["handler"] = lambda _: success({"accessToken": "rotated-at", "refreshToken": "rotated-rt",
        "expiresAt": (start + 1000) * 1000, "expiresIn": 99999,
        "refreshExpiresAt": (start + 2000) * 1000, "refreshExpiresIn": 99999})
    save = om._save_token_fields
    attempts = []

    def save_once(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("fixture save failure")
        return save(*args, **kwargs)

    monkeypatch.setattr(om, "_save_token_fields", save_once)
    with pytest.raises(common.WorkBuddyError, match="save_failed_retry_save"):
        om._refresh_sync_locked(account, True)
    assert om.get_account(account)["refresh_token"] == "fixture-rt"
    clock[0] += 300
    assert om._refresh_sync_locked(account, True) == "rotated-at"
    assert len(wire["requests"]) == 1 and len(attempts) == 2
    saved = om.get_account(account)
    assert saved["expired"] == auth.utc_text(start + 1000)
    assert saved["refresh_expired"] == auth.utc_text(start + 2000)
    assert saved["refresh_token"] == "rotated-rt"


@pytest.mark.parametrize("values,kind", [
    ({"refresh_token": ""}, "missing_refresh_token"),
    ({"refresh_expired": "2020-01-01T00:00:00Z"}, "refresh_token_expired"),
])
def test_missing_or_known_expired_rt_never_requests_or_erases_credentials(account, monkeypatch, values, kind):
    config.update(lambda c: c["oauthAccounts"][0].update(**values))
    old = copy.deepcopy(om.get_account(account))
    monkeypatch.setattr(common, "request", lambda *a, **k: pytest.fail("must not send unusable RT"))
    with pytest.raises(common.WorkBuddyError, match=kind):
        om._refresh_sync_locked(account, False)
    with pytest.raises(common.WorkBuddyError, match="backoff"):
        om._refresh_sync_locked(account, False)
    assert om.get_account(account) == old


def test_login_missing_rt_keeps_partial_token_without_repeat_exchange(monkeypatch):
    calls = []
    monkeypatch.setattr(common, "request", lambda *a, **k: (calls.append(1) or {"accessToken": "fixture-at"}))
    flow = {"realm": "cn", "state": "fixture-state"}
    for _ in range(2):
        with pytest.raises(ValueError, match="refresh_token"):
            auth.poll_login_sync(flow)
    assert len(calls) == 1 and flow["status"] == "identity_pending"
    assert flow["token"]["accessToken"] == "fixture-at" and "entry" not in flow


@pytest.mark.parametrize("status,code,auth_error", [(401, None, True), (403, None, False),
    (200, 12153, True), (200, 98765, False), (503, None, False)])
def test_refresh_error_classification_keeps_existing_protection(account, wire, status, code, auth_error):
    wire["handler"] = lambda _: httpx.Response(status, json={"code": code} if code else {})
    with pytest.raises(common.WorkBuddyError) as error:
        om._refresh_sync_locked(account, False)
    assert error.value.auth_error is auth_error
    oauth_refresh_failure.apply(account, error.value, provider="workbuddy")
    saved = om.get_account(account)
    assert saved["enabled"] is (not auth_error)
    assert saved["refresh_token"] == "fixture-rt" and saved["access_token"] == "fixture-at"
    with pytest.raises(common.WorkBuddyError, match="backoff"):
        om._refresh_sync_locked(account, False)
    assert len(wire["requests"]) == 1


@pytest.mark.parametrize("metadata", [
    {"expiresAt": True, "expiresIn": 3600, "refreshExpiresAt": "invalid", "refreshExpiresIn": 7200},
    {"expiresAt": 2**53, "refreshExpiresAt": 2**53},
    {"expiresIn": 2**53, "refreshExpiresIn": 2**53, "method": {"invalid": "metadata"}},
])
def test_invalid_expiry_metadata_cannot_discard_rotated_credentials(account, wire, metadata):
    wire["handler"] = lambda _: success({"accessToken": "rotated-at", "refreshToken": "rotated-rt", **metadata})
    assert om._refresh_sync_locked(account, True) == "rotated-at"
    saved = om.get_account(account)
    assert saved["refresh_token"] == "rotated-rt"
    assert saved["expired"] == saved["refresh_expired"] == ""
    assert om._refresh_sync_locked(account, False) == "rotated-at"
    assert len(wire["requests"]) == 1


def test_healthy_at_is_usable_without_renewable_rt(account, monkeypatch):
    config.update(lambda c: c["oauthAccounts"][0].update(refresh_token="", expired="2099-01-01T00:00:00Z"))
    old = copy.deepcopy(om.get_account(account))
    monkeypatch.setattr(common, "request", lambda *a, **k: pytest.fail("must not refresh healthy AT"))
    assert om._refresh_sync_locked(account, False) == "fixture-at"
    assert om.get_account(account) == old


def test_pending_save_is_account_isolated(account, monkeypatch):
    other = dict(om.get_account(account), realm="global", domain="www.workbuddy.ai")
    om.add_account(other)
    other_key = om.get_account_key(other)
    current = om.get_account(account)
    runtime._refresh_states[account] = runtime._RefreshState(runtime.credential_fingerprint(current),
        pending={"access_token": "unsaved-cn-at", "refresh_token": "unsaved-cn-rt", "expired": ""},
        pending_until=runtime.time.monotonic() + 600)
    calls = []
    monkeypatch.setattr(auth, "refresh_sync", lambda *a, **k: (calls.append(k["account_key"]) or {
        "access_token": "global-at", "refresh_token": "global-rt"}))
    assert om._refresh_sync_locked(other_key, True) == "global-at"
    assert om._refresh_sync_locked(account, True) == "unsaved-cn-at"
    assert calls == [other_key]
    assert om.get_account(other_key)["refresh_token"] == "global-rt"
    assert om.get_account(account)["refresh_token"] == "unsaved-cn-rt"
