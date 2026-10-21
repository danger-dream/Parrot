"""Qoder sign-in, refresh, usage and daily credits against scripted account pages."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import stat
import time
import uuid
from urllib.parse import parse_qs, urlparse

import pytest

from src import config, oauth_manager as om, state_db
from src.oauth.qoder import actions, auth, billing, common, runtime, device_profile, device_identity


class Pages:
    """Scripted Qoder answers by path; each entry is a (status, json) or a list consumed in order."""

    def __init__(self, routes):
        self.routes = {key: list(value) if isinstance(value, list) else value for key, value in routes.items()}
        self.calls = []

    def __call__(self, url, *, method="GET", headers=None, body=None, content=None, account_key="", timeout=20.0, stage="request", prepare_headers=None):
        if prepare_headers is not None:
            headers = prepare_headers()
        parsed = urlparse(url)
        self.calls.append({"host": parsed.netloc, "path": parsed.path, "query": parse_qs(parsed.query),
                           "method": method, "headers": dict(headers or {}), "body": body})
        answer = self.routes.get(parsed.path)
        if isinstance(answer, list):
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        if isinstance(answer, Exception):
            raise answer
        if answer is None:
            return 404, None
        return answer

    def paths(self):
        return [call["path"] for call in self.calls]


@pytest.fixture
def pages(monkeypatch):
    def install(routes):
        scripted = Pages(routes)
        monkeypatch.setattr(common, "request", scripted)
        return scripted
    return install


def test_login_url_is_the_desktop_device_flow_with_pkce():
    flow = auth.start_login_sync(profile=device_profile.generate(), realm="global")
    url = urlparse(flow["auth_url"])
    query = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert url.netloc == "qoder.com" and url.path == "/device/selectAccounts"
    assert query["client_id"] == "732aef47-9cf2-46a2-95fe-4cebb5d0d1fa" and query["redirect_uri"] == "qoder-app://"
    assert query["challenge"] == base64.urlsafe_b64encode(hashlib.sha256(flow["verifier"].encode()).digest()).rstrip(b"=").decode()
    assert query["nonce"] == flow["nonce"] and query["machine_id"] == flow["machine_id"]
    cn = {k: v[0] for k, v in parse_qs(urlparse(auth.start_login_sync(profile=device_profile.generate(), realm="cn")["auth_url"]).query).items()}
    assert urlparse(auth.start_login_sync(profile=device_profile.generate(), realm="cn")["auth_url"]).netloc == "qoder.cn"
    assert cn["client_id"] == "e883ade2-e6e3-4d6d-adf7-f92ceff5fdcb" and "redirect_uri" not in cn


def test_poll_waits_then_trades_the_device_token_for_a_job_token(pages):
    flow = auth.start_login_sync(profile=device_profile.generate(), realm="global")
    scripted = pages({
        "/api/v1/deviceToken/poll": [(400, {"message": "pending"}),
                                     (200, {"token": "dt", "refresh_token": "drt", "user_id": "u1", "user_name": "Q", "expires_in": 3600})],
        "/api/v1/me/jobToken": (200, {"token": "jt", "refresh_token": "jrt", "expires_in": 7200000}),
        "/api/v1/userinfo": (200, {"email": "q@example.com", "name": "Qoder User"}),
    })
    assert auth.poll_login_sync(flow) == {} and flow["status"] == "pending"
    entry = auth.poll_login_sync(flow)
    assert entry["access_token"] == "jt" and entry["refresh_token"] == "jrt"
    assert entry["device_token"] == "dt" and entry["device_refresh_token"] == "drt"
    assert entry["uid"] == "u1" and entry["email"] == "q@example.com" and entry["label"] == "q@example.com"
    assert entry["machine_id"] == flow["machine_id"] and entry["qoder_device_chat"] is False
    expired = om._parse_iso(entry["expired"]).timestamp()
    assert 7100 < expired - time.time() < 7300
    poll = scripted.calls[0]
    assert poll["query"]["verifier"] == [flow["verifier"]] and poll["query"]["nonce"] == [flow["nonce"]]
    job = next(call for call in scripted.calls if call["path"] == "/api/v1/me/jobToken")
    assert job["headers"]["Authorization"] == "Bearer dt" and job["body"] == {"clientId": common.SITES["global"]["client_id"]}
    assert auth.poll_login_sync(flow) == entry  # a ready flow answers again without asking Qoder


def test_a_device_sign_in_without_a_refresh_token_saves_and_lapses_at_refresh(pages):
    flow = auth.start_login_sync(profile=device_profile.generate(), realm="global")
    pages({"/api/v1/deviceToken/poll": (200, {"token": "dt", "user_id": "u1", "expires_at": "Tue, 21 Oct 2099 12:00:00 GMT"}),
           "/api/v1/me/jobToken": (200, {"token": "jt"})})
    entry = auth.poll_login_sync(flow)
    assert entry["access_token"] == "jt" and entry["refresh_token"] == "" and entry["device_refresh_token"] == ""
    with pytest.raises(common.QoderError) as raised:
        auth.refresh_sync(entry)
    assert raised.value.kind == "missing_refresh_token" and raised.value.auth_error
    assert auth.device_expiry({"expires_at": "Tue, 21 Oct 2099 12:00:00 GMT"}, 0) == "2099-10-21T12:00:00Z"


def test_a_failed_job_token_exchange_never_polls_the_device_again(pages):
    flow = auth.start_login_sync(profile=device_profile.generate(), realm="global")
    scripted = pages({
        "/api/v1/deviceToken/poll": (200, {"token": "dt", "refresh_token": "drt", "user_id": "u1"}),
        "/api/v1/me/jobToken": [common.QoderError("job_token", kind="network"), (200, {"token": "jt", "refresh_token": "jrt"})],
    })
    with pytest.raises(common.QoderError):
        auth.poll_login_sync(flow)
    assert auth.poll_login_sync(flow)["access_token"] == "jt"
    assert scripted.paths().count("/api/v1/deviceToken/poll") == 1


@pytest.mark.parametrize("status", [403, 404])
def test_qoder_cn_device_chat_requires_positive_model_access(pages, monkeypatch, status):
    from src.oauth.qoder import catalog
    verified = []
    monkeypatch.setattr(catalog, "fetch_models_sync", lambda a: verified.append(a) or [{"id": "m"}])
    flow = auth.start_login_sync(profile=device_profile.generate(), realm="cn")
    pages({"/api/v1/deviceToken/poll": (200, {"token": "dt", "refresh_token": "drt", "user_id": "u9", "expires_at": "2099-01-01T00:00:00Z"}),
           "/api/v1/me/jobToken": (status, {"code": str(status)})})
    entry = auth.poll_login_sync(flow)
    assert entry["qoder_device_chat"] is True and entry["access_token"] == entry["device_token"] == "dt"
    assert entry["refresh_token"] == "drt" and entry["expired"] == "2099-01-01T00:00:00Z"
    assert len(verified) == 1 and verified[0]["access_token"] == "dt"
    global_flow = auth.start_login_sync(profile=device_profile.generate(), realm="global")
    pages({"/api/v1/deviceToken/poll": (200, {"token": "dt", "refresh_token": "drt", "user_id": "u9"}),
           "/api/v1/me/jobToken": (403, {"code": "403"})})
    with pytest.raises(common.QoderError):
        auth.poll_login_sync(global_flow)


def test_pat_sign_in_exchanges_the_token_and_is_international_only(pages):
    pages({"/api/v1/jobToken/exchange": (200, {"token": "jt", "refresh_token": "jrt", "expire_time": "2099-01-01T00:00:00Z"}),
           "/api/v1/userinfo": (200, {"id": 42, "email": "p@example.com"})})
    entry = auth.pat_login_sync("pt-secret", profile=device_profile.generate())
    assert entry["uid"] == "42" and entry["qoder_auth_method"] == "pat" and entry["qoder_pat"] == "pt-secret"
    assert entry["expired"] == "2099-01-01T00:00:00Z" and entry["device_token"] == ""
    with pytest.raises(ValueError):
        auth.pat_login_sync("secret", profile=device_profile.generate())
    with pytest.raises(ValueError):
        auth.pat_login_sync("pt-secret", profile=device_profile.generate(), realm="cn")
    pages({"/api/v1/jobToken/exchange": (401, {"code": "401"})})
    with pytest.raises(common.QoderError) as raised:
        auth.pat_login_sync("pt-secret", profile=device_profile.generate())
    assert raised.value.auth_error


def account(**extra):
    return auth.normalize_credential({"realm": "global", "uid": "u-" + uuid.uuid4().hex, "access_token": "jt",
        "refresh_token": "jrt", "expired": "2000-01-01T00:00:00Z", "machine_id": "m", "device_token": "dt",
        "device_refresh_token": "drt", "qoder_device_profile": device_profile.generate("m"), **extra})


def test_refresh_spends_the_job_refresh_token_and_a_refusal_is_a_lapsed_sign_in(pages):
    scripted = pages({"/api/v1/jobToken/refresh": (200, {"token": "jt2", "refresh_token": "jrt2", "expires_in": 86400000})})
    fields = auth.refresh_sync(account())
    assert fields["access_token"] == "jt2" and fields["refresh_token"] == "jrt2"
    assert scripted.calls[0]["body"] == {"refresh_token": "jrt"}
    pages({"/api/v1/jobToken/refresh": (401, {"code": "401"})})
    with pytest.raises(common.QoderError) as raised:
        auth.refresh_sync(account())
    assert raised.value.kind == "refresh_rejected" and raised.value.auth_error
    pages({"/api/v1/jobToken/refresh": (502, None)})
    with pytest.raises(common.QoderError) as raised:
        auth.refresh_sync(account())
    assert not raised.value.auth_error and raised.value.retryable


def test_a_pat_account_exchanges_its_pat_again_when_the_refresh_lapses(pages):
    scripted = pages({"/api/v1/jobToken/refresh": (401, None), "/api/v1/jobToken/exchange": (200, {"token": "jt3", "refresh_token": "jrt3"})})
    pat = account(qoder_auth_method="pat", qoder_pat="pt-x")
    assert auth.refresh_sync(pat)["access_token"] == "jt3"
    assert scripted.paths() == ["/api/v1/jobToken/refresh", "/api/v1/jobToken/exchange"]


def test_device_chat_and_device_rotation(pages):
    pages({"/api/v1/deviceToken/refresh": (200, {"device_token": "dt2", "refresh_token": "drt2", "expires_in": 3600})})
    chat = auth.normalize_credential({"realm": "cn", "uid": "c1", "access_token": "dt", "refresh_token": "drt", "machine_id": "m",
                                      "device_token": "dt", "device_refresh_token": "drt", "qoder_device_chat": True})
    fields = auth.refresh_sync(chat)
    assert fields["access_token"] == fields["device_token"] == "dt2" and fields["refresh_token"] == fields["device_refresh_token"] == "drt2"
    rotated = auth.rotate_device_sync(account())
    assert rotated == {"device_token": "dt2", "device_refresh_token": "drt2"}
    pages({"/api/v1/deviceToken/refresh": (403, None)})
    with pytest.raises(common.QoderError) as raised:
        auth.rotate_device_sync(account())
    assert raised.value.kind == "device_rejected" and not raised.value.auth_error  # chat runs on the job token
    with pytest.raises(common.QoderError) as raised:
        auth.refresh_sync(chat)
    assert raised.value.auth_error


def test_parse_usage_reads_every_credit_pool():
    usage = billing.parse_usage({"displayMode": "qoder", "qoderUsage": {
        "userType": "Pro", "expiresAt": 1893456000000,
        "userQuota": {"total": 2000, "used": 500, "unit": "credits"},
        "add_on_quota": {"cap": 100, "remaining": 100},
        "dedicatedResourcePackages": [{"name": "Gift", "total": 50, "used": 60}, {"total": 0, "used": 0}]}})
    assert usage["plan"] == "Pro" and usage["until"] == "2030-01-01T00:00:00Z"
    assert [(p["kind"], p["used"], p["remaining"], p["used_percent"]) for p in usage["pools"]] == [
        ("user", 500, 1500, 25.0), ("add_on", 0, 100, 0.0), ("dedicated", 60, 0, 100.0)]
    assert billing.parse_usage({"displayMode": "enterprise"})["plan"] == "Enterprise"
    naive = billing.parse_usage({"displayMode": "qoder", "qoderUsage": {"expiresAt": "2030-01-01T00:00:00"}})
    assert "until" not in naive  # a time with no zone is not guessed in the server's zone
    with pytest.raises(common.QoderError):
        billing.parse_usage({"displayMode": "other"})


def test_runtime_info_is_found_where_the_cli_caches_it(tmp_path):
    cache = tmp_path / ".qoder" / ".bin"
    cache.mkdir(parents=True)
    old = cache / "runtime-info-linux-x64-aaa"
    new = cache / "runtime-info-linux-x64-bbb"
    for path in (old, new, cache / "runtime-info-linux-x64-ccc.tmp"):
        path.write_text("x")
    os.utime(old, (1, 1))
    assert billing.runtime_info_path(env={}, home=str(tmp_path), system="linux", arch="x86_64") == str(new)
    assert billing.runtime_info_path(env={"QODER_RUNTIME_INFO": str(old)}, home=str(tmp_path), system="linux") == str(old.resolve())
    with pytest.raises(common.QoderError) as raised:
        billing.runtime_info_path(env={}, home=str(tmp_path / "none"), system="linux", arch="x86_64")
    assert raised.value.kind == "runtime_info_missing"


def campaign(status, **extra):
    now = time.time()
    return {"campaignId": "c1", "actionType": "CLAIM_BENEFIT", "claimStatus": status,
            "startAt": now - 60, "endAt": now + 3600, "benefit": {"amount": 100}, **extra}


def test_checkin_claims_reports_done_and_needs_the_native_identity_internationally(pages, monkeypatch, tmp_path):
    cn = auth.normalize_credential({"realm": "cn", "uid": "c1", "access_token": "jt", "refresh_token": "jrt", "machine_id": "m",
                                    "device_token": "dt", "device_refresh_token": "drt"})
    scripted = pages({common.CAMPAIGNS_PATH: (200, {"campaigns": [campaign("CLAIMABLE")]}),
                      common.CAMPAIGNS_PATH + "/c1/claim": (200, {"data": {"status": "CLAIMED"}})})
    result = billing.checkin_sync(cn)
    assert result["status"] == "claimed" and result["credit"] == 100
    claim = scripted.calls[-1]
    assert claim["method"] == "POST" and claim["headers"]["Authorization"] == "Bearer dt" and claim["headers"]["Cosy-ClientType"] == "10"
    assert not any(key.startswith("Cosy-Machine") for call in scripted.calls for key in call["headers"])
    assert all(call["headers"]["Content-Type"] == "application/json" for call in scripted.calls)
    assert scripted.calls[0]["host"] == "openapi.qoder.com.cn"
    pages({common.CAMPAIGNS_PATH: (200, {"campaigns": [campaign("CLAIMED")]})})
    assert billing.checkin_sync(cn)["status"] == "already_done"
    pages({common.CAMPAIGNS_PATH: (200, {"campaigns": [campaign("UNCLAIMABLE")]})})
    assert billing.checkin_sync(cn)["status"] == "not_offered"
    pages({common.CAMPAIGNS_PATH: (200, {"campaigns": []})})
    assert billing.checkin_sync(cn)["status"] == "inactive"

    monkeypatch.setattr(device_identity, "headers", lambda account, **kw: {
        "Cosy-MachineToken": "issued-token", "Cosy-MachineType": "type", "Cosy-MachineCode": "code"})
    scripted = pages({common.CAMPAIGNS_PATH: (200, {"campaigns": [campaign("CLAIMED")]})})
    assert billing.checkin_sync(account())["status"] == "already_done"
    assert scripted.calls[0]["headers"]["Cosy-MachineToken"] == "issued-token"
    legacy = account()
    legacy.pop("qoder_device_profile")
    assert billing.checkin_supported(legacy) == (False, "device_initialization_required")
    assert billing.checkin_supported(cn) == (True, "")


@pytest.fixture
def stored(monkeypatch):
    from src.tests import test_protocol_fake_upstreams as fake
    m = fake._import_modules()
    fake._setup(m)
    before = copy.deepcopy(config.get())
    entry = account()
    config.update(lambda c: c.update(oauthAccounts=[entry], channels=[]))
    key = om.get_account_key(om.get_account(f"qoder:{auth.identity(entry)}"))
    yield key
    runtime.forget(key)
    config.update(lambda c: (c.clear(), c.update(before)))


def test_a_rotated_pair_whose_save_failed_is_saved_later_without_spending_again(stored, monkeypatch):
    spent = []

    def refresh(account, *, account_key=""):
        spent.append(account["refresh_token"])
        return {"access_token": "jt2", "refresh_token": "jrt2", "expired": "2099-01-01T00:00:00Z"}

    monkeypatch.setattr(auth, "refresh_sync", refresh)
    real_save = om._save_token_fields
    failures = iter([True])
    monkeypatch.setattr(om, "_save_token_fields", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")) if next(failures, False) else real_save(*a, **k))
    with pytest.raises(common.QoderError) as raised:
        om._refresh_sync_locked(stored, False)
    assert raised.value.kind == "save_failed_retry_save"
    assert om._refresh_sync_locked(stored, False) == "jt2"
    assert spent == ["jrt"]
    saved = om.get_account(stored)
    assert saved["access_token"] == "jt2" and saved["refresh_token"] == "jrt2"


def test_a_failed_early_renewal_keeps_serving_the_still_valid_token(stored, monkeypatch):
    calls = []

    def refresh(account, *, account_key=""):
        calls.append(1)
        raise common.QoderError("refresh", kind="network", transport_kind="connect")

    monkeypatch.setattr(auth, "refresh_sync", refresh)
    soon = auth.utc_text(time.time() + 300)  # inside the 10-minute renewal lead, not yet expired
    config.update(lambda c: c["oauthAccounts"][0].update(expired=soon))
    with pytest.raises(common.QoderError):
        om._refresh_sync_locked(stored, False)
    assert om._refresh_sync_locked(stored, False) == "jt"  # backoff: the valid token keeps serving
    assert calls == [1]
    config.update(lambda c: c["oauthAccounts"][0].update(expired="2000-01-01T00:00:00Z"))
    with pytest.raises(common.QoderError) as raised:
        om._refresh_sync_locked(stored, False)
    assert raised.value.kind == "backoff" and calls == [1]


def test_usage_rotates_a_refused_device_token_once_and_saves_it(stored, pages):
    pages({common.USAGE_PATH: [(401, None), (200, {"displayMode": "qoder", "qoderUsage": {"userQuota": {"total": 10, "used": 1}}})],
           "/api/v1/deviceToken/refresh": (200, {"token": "dt2", "refresh_token": "drt2"})})
    usage = billing.fetch_usage_sync(om.get_account(stored), account_key=stored)
    assert usage["qoder"]["pools"][0]["remaining"] == 9
    saved = om.get_account(stored)
    assert saved["device_token"] == "dt2" and saved["access_token"] == "jt"


def test_daily_checkin_is_due_once_a_day_and_rechecked_after_thirty_minutes(stored, pages, monkeypatch):
    monkeypatch.setattr(device_identity, "headers", lambda account, **kw: {"Cosy-MachineToken": "t", "Cosy-MachineType": "1"})
    monkeypatch.setattr(billing, "runtime_info_path", lambda **kw: "/bin/true")
    pages({common.CAMPAIGNS_PATH: [(200, {"campaigns": [campaign("CLAIMABLE")]}), (200, {"campaigns": [campaign("CLAIMED")]})],
           common.CAMPAIGNS_PATH + "/c1/claim": (200, {"status": "CLAIMED", "benefit": {"amount": 100}})})
    state_db.qoder_checkin_forget(stored)
    assert actions.due(stored)
    assert actions.auto_checkin_once() == {}  # the daily switch is off by default
    actions.set_auto(stored, True)
    assert actions.auto_checkin_once() == {stored: "claimed"}
    assert not actions.due(stored) and actions.auto_checkin_once() == {}
    assert actions.due(stored, time.time() + 31 * 60)
    again = actions.checkin_now(stored)
    assert again["status"] == "claimed" and again["credit"] == 100  # a later look keeps today's claim
    tomorrow = time.time() + 24 * 3600
    assert actions.due(stored, tomorrow)
