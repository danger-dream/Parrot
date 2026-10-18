"""WorkBuddy wire contracts; isolated fixtures only, no live credentials/network."""
from __future__ import annotations

import copy
import json

import httpx
import pytest

from src.oauth import normalize_provider
from src.oauth import workbuddy
from src.oauth.workbuddy import auth, billing, catalog, common
from src.oauth_ids import account_key


def credential(**values):
    return {"provider": "workbuddy", "realm": "cn", "uid": "uid-a", "enterprise_id": "",
            "access_token": "fixture-access", "refresh_token": "fixture-refresh", "domain": "www.codebuddy.cn", **values}


@pytest.mark.parametrize("realm,domain,api,bill", [
    ("cn", "www.codebuddy.cn", "https://copilot.tencent.com", "https://www.codebuddy.cn"),
    ("global", "www.workbuddy.ai", "https://www.workbuddy.ai", "https://www.workbuddy.ai"),
])
def test_region_and_headers(realm, domain, api, bill):
    account = credential(realm=realm, domain=domain, enterprise_id="tenant")
    assert common.api_base_url(account) == api
    assert common.billing_base_url(account) == bill
    chat = common.headers(account)
    assert chat["Authorization"] == "Bearer fixture-access"
    assert "X-Refresh-Token" not in chat
    assert chat["X-Enterprise-Id"] == "tenant"
    refresh = common.headers(account, "refresh")
    assert refresh["X-Refresh-Token"] == "fixture-refresh"
    assert refresh["X-Auth-Refresh-Source"] == ("workbuddy" if realm == "global" else "plugin")
    assert "Authorization" not in refresh
    assert common.headers(account, "enterprise")["X-Client-Platform"] == "web"


@pytest.mark.parametrize("override", [
    {"realm": "global"}, {"domain": "evil.example"}, {"realm": "unknown"},
    {"domain": "https://www.codebuddy.cn"}, {"workbuddy_client_profile": "app"},
])
def test_reject_cross_region_and_untrusted_profile(override):
    with pytest.raises(ValueError):
        common.headers(credential(**override))


def test_identity_no_email_and_scope_collisions():
    base = auth.normalize_credential(credential())
    assert base["email"] == ""
    assert account_key(base) == "workbuddy:cn:uid-a:p"
    variants = [base, dict(base, enterprise_id="personal"), dict(base, enterprise_id="p"),
                dict(base, uid="uid-a:p"), dict(base, realm="global", domain="www.workbuddy.ai")]
    assert len({account_key(item) for item in variants}) == len(variants)
    assert normalize_provider("workbuddy") == "workbuddy"


@pytest.mark.parametrize("shape", ["nested", "camel", "snake"])
def test_stored_credential_shapes_and_unknown_expiry_remain_compatible(shape):
    raw = {"accessToken": "fixture-at", "refreshToken": "fixture-rt", "domain": "www.codebuddy.cn", "expiresIn": 3600}
    user = {"uid": "id", "enterpriseId": "team", "nickname": "演示账号"}
    value = {"auth": raw, "account": user} if shape == "nested" else dict(raw, **user)
    if shape == "snake":
        value = credential(nickname="演示账号")
    entry = auth.normalize_credential(value)
    assert entry["expired"] == ""
    assert entry["label"] == "演示账号"
    assert entry["workbuddy_identity_source"] == "import"  # Preserve existing imported-account metadata.


@pytest.mark.parametrize("expiry", ["2026-09-07 18:30:00", "bad", True, {}, -1000.0])
def test_invalid_expiry_does_not_invent_lifetime(expiry):
    if expiry == -1000.0:
        assert auth.normalize_credential(credential(expired=expiry))["expired"] == ""
    else:
        with pytest.raises(ValueError):
            auth.normalize_credential(credential(expired=expiry))


def test_poll_retains_token_after_identity_failure(monkeypatch):
    calls = []
    def wire(account, path, **kwargs):
        calls.append(path)
        if "/auth/token?" in path:
            return {"accessToken": "fixture-at", "refreshToken": "fixture-rt", "expiresIn": 3600, "domain": "www.codebuddy.cn"}
        if len(calls) == 2:
            raise common.WorkBuddyError("identity", status=503)
        return {"uid": "uid-a", "nickname": "A"}
    monkeypatch.setattr(common, "request", wire)
    payload = {"realm": "cn", "state": "fixture-state"}
    with pytest.raises(common.WorkBuddyError):
        auth.poll_login_sync(payload)
    assert payload["status"] == "identity_pending"
    entry = auth.poll_login_sync(payload)
    assert entry["uid"] == "uid-a" and entry["expired"]
    assert sum("/auth/token?" in path for path in calls) == 1
    assert payload["status"] == "ready"
    assert auth.poll_login_sync(payload) == entry
    assert len(calls) == 3


def test_pending_remains_reusable(monkeypatch):
    monkeypatch.setattr(common, "request", lambda *a, **k: None)
    flow = {"realm": "cn", "state": "fixture"}
    assert auth.poll_login_sync(flow) == {}
    assert auth.poll_login_sync(flow) == {}
    assert flow["status"] == "pending"


def test_refresh_exact_wire_and_missing_fields(monkeypatch):
    monkeypatch.delenv("PARROT_NO_REFRESH", raising=False)
    calls = []
    def wire(account, path, **kwargs):
        calls.append((copy.deepcopy(account), path, kwargs))
        return {"accessToken": "new-fixture-access"}
    monkeypatch.setattr(common, "request", wire)
    result = auth.refresh_sync("fixture-refresh", account=credential(), account_key="workbuddy:cn:uid-a:p")
    assert result == {"access_token": "new-fixture-access"}
    assert calls[0][1] == "/v2/plugin/auth/token/refresh"
    assert calls[0][2]["kind"] == "refresh" and calls[0][2]["body"] == {}


def test_development_protection_prevents_token_refresh_only(monkeypatch):
    monkeypatch.setenv("PARROT_NO_REFRESH", "1")
    monkeypatch.setattr(common, "request", lambda *a, **k: pytest.fail("must not refresh"))
    with pytest.raises(common.WorkBuddyError, match="disabled"):
        auth.refresh_sync("fixture-rt", account=credential())


@pytest.mark.parametrize("action,realm,domain,path", [
    ("checkin", "cn", "www.codebuddy.cn", "/v2/billing/meter/daily-checkin"),
    ("claim_trial", "global", "www.workbuddy.ai", "/billing/ide/trial"),
])
def test_no_token_refresh_still_allows_activity_requests(monkeypatch, action, realm, domain, path):
    monkeypatch.setenv("PARROT_NO_REFRESH", "1")
    calls = []
    def wire(account, actual_path, **kwargs):
        calls.append(actual_path)
        assert account["access_token"] == "fixture-access"
        assert kwargs.get("kind", "billing") == "billing"
        return {"credit": 0} if action == "checkin" else {}
    monkeypatch.setattr(common, "request", wire)
    assert billing.execute_action_sync(credential(realm=realm, domain=domain), action)["status"] == "succeeded"
    assert calls == [path] and common.effects_allowed() is True and common.refresh_allowed() is False


def _page(rows, total=None):
    data = {"Accounts": rows}
    if total is not None:
        data["TotalCount"] = total
    return {"Response": {"Data": data}}


def _package(index=0, **fields):
    return {"AccountId": str(index), "PackageName": "fixture", "CapacitySize": 10,
            "CapacityRemain": 6, "CapacityUsed": 4, **fields}


def test_complete_pagination_and_no_total_dosage_consumption(monkeypatch):
    calls = []
    def wire(account, path, **kwargs):
        page = kwargs["body"]["PageNumber"]
        calls.append(page)
        data = _page([_package(i) for i in (range(100) if page == 1 else range(100, 102))], 102)
        data["Response"]["Data"]["TotalDosage"] = 900000
        return data
    monkeypatch.setattr(common, "request", wire)
    result = billing.fetch_personal_sync(credential())
    assert calls == [1, 2]
    assert result["complete"] and result["credits"]["reliable"]
    assert result["credits"]["capacity"] == 1020
    assert result["credits"]["used"] == 408
    assert result["credits"]["remaining"] == 612


def test_partial_pagination_cannot_stop_account(monkeypatch):
    def wire(account, path, **kwargs):
        if kwargs["body"]["PageNumber"] == 1:
            return _page([_package(i, CapacityRemain=0, CapacityUsed=10) for i in range(100)], 101)
        raise common.WorkBuddyError("request", status=503)
    monkeypatch.setattr(common, "request", wire)
    result = billing.fetch_personal_sync(credential())
    assert result["credits"]["remaining"] == 0
    assert not result["complete"] and not result["credits"]["reliable"]
    assert result["status"] == "partial"


@pytest.mark.parametrize("fields", [
    {"CapacityRemain": None}, {"CapacityRemain": -1}, {"CapacityRemain": float("nan")},
    {"CapacityRemain": 11}, {"CapacityUsed": 9},
    {"CycleCapacitySize": 0, "CycleCapacityRemain": None},
])
def test_unreliable_values_never_known_zero(fields):
    package = billing.normalize_package(_package(**fields), "cn")
    assert not package["reliable"]


def test_explicit_cycle_zero_is_not_replaced_by_lifetime():
    result = billing.normalize_package(_package(CycleCapacitySize=0, CycleCapacityRemain=0, CycleCapacityUsed=0), "cn")
    assert result["remaining"] == 0 and result["capacity"] == 0 and result["reliable"]


def test_empty_packages_are_unknown(monkeypatch):
    monkeypatch.setattr(common, "request", lambda *a, **k: _page([], 0))
    result = billing.fetch_personal_sync(credential())
    assert result["credits"]["remaining"] is None
    assert not result["credits"]["reliable"]


def test_repeated_page_terminates(monkeypatch):
    monkeypatch.setattr(common, "request", lambda *a, **k: _page([_package(i) for i in range(100)], 1000))
    result = billing.fetch_personal_sync(credential())
    assert len(result["packages"]) == 100 and not result["complete"]


def test_enterprise_403_does_not_claim_personal_healthy(monkeypatch):
    def wire(account, path, **kwargs):
        if path.endswith("get-user-resource"):
            return _page([_package()], 1)
        if path.endswith("get-enterprise-user-usage"):
            raise common.WorkBuddyError("enterprise", status=403)
        return {}
    monkeypatch.setattr(common, "request", wire)
    block = billing.fetch_usage_sync("fixture-access", account=credential(enterprise_id="team"))["workbuddy"]
    assert block["personal_credits"]["remaining"] == 6
    assert block["credits"]["remaining"] is None and not block["credits"]["reliable"]
    assert block["errors"]["enterprise"]["http_status"] == 403


def test_checkin_unknown_not_false(monkeypatch):
    monkeypatch.setattr(common, "request", lambda *a, **k: {})
    result = billing.fetch_checkin_sync(credential())
    assert result["active"] is None and result["today_checked_in"] is None


def test_catalog_intersection_capabilities_and_account_scope(monkeypatch):
    def wire(account, path, **kwargs):
        assert path == "/v3/config"
        return {"models": [{"id": "a", "maxInputTokens": 1000, "maxOutputTokens": 200,
                            "reasoning": {"supportedEfforts": ["low", "high"]}},
                           {"id": "b", "disabled": True}, {"id": "other"}],
                "agents": [{"name": "cli", "models": ["a", "b"]}]}
    monkeypatch.setattr(common, "request", wire)
    result = catalog.fetch_models_sync(credential())
    assert [item["id"] for item in result] == ["a"]
    assert result[0]["maxInputTokens"] == 1000 and "contextWindow" not in result[0]
    assert result[0]["reasoningEfforts"] == ["low", "high"]


def test_wire_cookie_proxy_and_error_redaction(monkeypatch):
    from src import config
    monkeypatch.delenv("DISABLE_OAUTH_NETWORK_CALLS", raising=False)
    monkeypatch.setattr(config, "get", lambda: {"oauth": {"mockMode": False}})
    received = []
    def transport(request):
        received.append(request)
        return httpx.Response(200, json={"code": 12153, "msg": "secret fixture-access fixture-refresh", "data": {}}, headers={"Set-Cookie": "session=fixture; Path=/"})
    contexts = []
    def client(**kwargs):
        contexts.append(kwargs)
        return httpx.Client(transport=httpx.MockTransport(transport), cookies=kwargs.get("cookies"))
    monkeypatch.setattr(common.network, "sync_client", client)
    cookies = {}
    with pytest.raises(common.WorkBuddyError) as caught:
        common.request(credential(), "/fixture", account_key="workbuddy:cn:uid-a:p", cookies=cookies)
    assert caught.value.auth_error
    assert "fixture-access" not in str(caught.value) and "fixture-refresh" not in str(caught.value)
    assert contexts[0]["proxy_channel"] == "oauth:workbuddy:cn:uid-a:p"
    assert contexts[0]["follow_redirects"] is False
    assert cookies["session"] == "fixture"


@pytest.mark.parametrize("payload,expected", [
    ({"code": 1, "msg": "login ing"}, "pending"),
    ({"code": 1, "msg": "permission denied"}, "error"),
    ({"code": 0, "data": None}, "pending"),
    ({"data": {}}, "error"),
])
def test_wire_pending_is_not_any_business_error(monkeypatch, payload, expected):
    from src import config
    monkeypatch.delenv("DISABLE_OAUTH_NETWORK_CALLS", raising=False)
    monkeypatch.setattr(config, "get", lambda: {"oauth": {"mockMode": False}})
    monkeypatch.setattr(common.network, "sync_client", lambda **k: httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=payload))))
    if expected == "pending":
        assert common.request({"realm": "cn"}, "/fixture", kind="anonymous", pending=True) is None
    else:
        with pytest.raises(common.WorkBuddyError):
            common.request({"realm": "cn"}, "/fixture", kind="anonymous", pending=True)
