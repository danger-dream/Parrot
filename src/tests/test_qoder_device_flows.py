"""Real control/config binding, actor leases, API preparation and reauthorization."""
import copy
import json
import threading
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor

import pytest

from src import config, network, oauth_manager as om
from src.oauth import add_context
from src.oauth.qoder import auth, common, device_profile
from src.management_control import ManagementError
from src.management_control.oauth import OAuthControl, OAuthProvider
from src.tests.test_workbuddy_lifecycle import context
from src.tests.test_management_oauth_api import auth_client


@pytest.fixture
def setup(monkeypatch):
    before = copy.deepcopy(config.get())
    config.update(lambda c: c.update(oauthAccounts=[], channels=[], network={"proxies": {
        "selected": {"type": "socks5", "url": "socks5://127.0.0.1:18001"}},
        "routing": {"default": "direct", "directFallback": True}}))
    now = [datetime.now(timezone.utc)]
    ctl = OAuthControl(clock=lambda: now[0])
    monkeypatch.setattr(ctl, "_post_save_account_effects", lambda *a, **k: {})
    seen = []
    def request(url, **kw):
        chain = network._configured_proxy_chain_or_none(proxy_purpose="oauth_qoder", proxy_channel="oauth:qoder:login", proxy_model="")
        seen.append((url, [n for n, _ in chain], kw))
        if "deviceToken/poll" in url:
            return 200, {"token": "dt", "refresh_token": "dr", "user_id": "test-uid"}
        if "jobToken" in url:
            return 200, {"token": "jt", "refresh_token": "jr"}
        return 200, {"id": "test-uid", "email": "test@example.test"}
    monkeypatch.setattr(common, "request", request)
    yield ctl, seen, now
    config.update(lambda c: (c.clear(), c.update(before)))


def prepare(ctl, **kw):
    with add_context.bind(add_context.AddAttempt(target="selected", provider="qoder")):
        return ctl.start_login_flow(context(), OAuthProvider.QODER, **kw)


def confirm(ctl, flow, **kw):
    return ctl.confirm_qoder_device(context(), flow.flow_id, flow.flow_secret, flow.device_preview["revision"], **kw)


@pytest.mark.parametrize("realm,method", [("global", "device"), ("cn", "device"), ("global", "pat")])
def test_confirmed_profile_flows_to_real_account_save_and_proxy(setup, realm, method):
    ctl, seen, _ = setup
    draft = prepare(ctl, realm=realm, login_method=method)
    assert draft.auth_url is None and seen == []
    assert ctl.poll_login_flow(context(), draft.flow_id, draft.flow_secret).status == "preparing"
    assert seen == []
    started = confirm(ctl, draft)
    if method == "pat":
        assert seen == []
        started = confirm(ctl, draft, pat="pt-private")
    else:
        assert "machine_id=" in started.auth_url and "machine_token" not in started.auth_url
    poll = ctl.poll_login_flow(context(), draft.flow_id, draft.flow_secret)
    assert poll.status == "completed"
    saved = om.get_account(poll.account_id)
    assert saved["machine_id"] == saved["qoder_device_profile"]["machine_id"]
    assert device_profile.fingerprint(saved["qoder_device_profile"]) == draft.device_preview["revision"]
    assert config.get()["network"]["routing"]["accounts"]["oauth:" + poll.account_id] == "selected"
    assert all(target == ["selected"] for _, target, _ in seen)
    assert ctl.poll_login_flow(context(), draft.flow_id, draft.flow_secret).account_id == poll.account_id


def test_regeneration_old_buttons_actor_cancel_and_expiry(setup):
    ctl, seen, now = setup
    draft = prepare(ctl)
    with pytest.raises(ManagementError):
        ctl._flows.qoder.confirm("different-actor", draft.flow_id, draft.flow_secret, draft.device_preview["revision"])
    fresh = ctl.regenerate_qoder_device(context(), draft.flow_id, draft.flow_secret, draft.device_preview["revision"])
    assert fresh.device_preview["revision"] != draft.device_preview["revision"] and seen == []
    with pytest.raises(ManagementError):
        confirm(ctl, draft)
    confirm(ctl, fresh)
    with pytest.raises(ManagementError):
        ctl.regenerate_qoder_device(context(), fresh.flow_id, fresh.flow_secret, fresh.device_preview["revision"])
    ctl.cancel_login_flow(context(), fresh.flow_id, fresh.flow_secret)
    with pytest.raises(ManagementError):
        confirm(ctl, fresh)
    expired = prepare(ctl)
    now[0] += timedelta(seconds=901)
    assert ctl.poll_login_flow(context(), expired.flow_id, expired.flow_secret).status == "expired"
    with pytest.raises(ManagementError):
        confirm(ctl, expired)
    assert seen == []


def test_duplicate_add_refused_relogin_and_refresh_keep_binding(setup):
    ctl, _, _ = setup
    first = prepare(ctl)
    confirm(ctl, first)
    saved = ctl.poll_login_flow(context(), first.flow_id, first.flow_secret)
    original = copy.deepcopy(om.get_account(saved.account_id))
    duplicate = prepare(ctl)
    confirm(ctl, duplicate)
    with pytest.raises(ManagementError) as error:
        ctl.poll_login_flow(context(), duplicate.flow_id, duplicate.flow_secret)
    assert error.value.code.value == "IDENTITY_CONFLICT"
    assert om.get_account(saved.account_id) == original
    relogin = ctl.start_login_flow(context(), OAuthProvider.QODER, relogin_account_id=saved.account_id)
    assert relogin.device_preview["revision"] == first.device_preview["revision"]
    assert relogin.device_preview["can_regenerate"] is False
    confirm(ctl, relogin)
    again = ctl.poll_login_flow(context(), relogin.flow_id, relogin.flow_secret)
    assert again.account_id == saved.account_id
    account = om.get_account(saved.account_id)
    refreshed = dict(account, **auth.refresh_sync(account, account_key=saved.account_id))
    assert refreshed["qoder_device_profile"] == original["qoder_device_profile"]
    assert refreshed["machine_id"] == original["machine_id"]


def test_legacy_initialization_preserves_machine_id_and_settings(setup):
    ctl, _, _ = setup
    old = auth.normalize_credential({"realm": "global", "uid": "test-uid", "machine_id": "legacy-machine",
                                     "access_token": "old", "refresh_token": "old-r"})
    old.update(label="my label", models=["old-model"], maxConcurrent=3)
    config.update(lambda c: c.update(oauthAccounts=[old]))
    key = om.get_account_key(old)
    draft = ctl.start_login_flow(context(), OAuthProvider.QODER, relogin_account_id=key)
    assert "qoder_device_profile" not in om.get_account(key)
    confirm(ctl, draft)
    ctl.poll_login_flow(context(), draft.flow_id, draft.flow_secret)
    saved = om.get_account(key)
    assert saved["machine_id"] == "legacy-machine" and saved["qoder_device_profile"]["machine_id"] == "legacy-machine"
    assert saved["label"] == "my label" and saved["maxConcurrent"] == 3


def test_concurrent_pat_confirmation_exchanges_once(setup, monkeypatch):
    ctl, _, _ = setup
    entered, finish = threading.Event(), threading.Event()
    calls = []
    original = ctl.backend.qoder_pat_login
    def exchange(*a, **kw):
        calls.append(1); entered.set()
        assert finish.wait(5)
        return original(*a, **kw)
    monkeypatch.setattr(ctl.backend, "qoder_pat_login", exchange)
    draft = prepare(ctl, login_method="pat")
    with ThreadPoolExecutor(max_workers=2) as pool:
        worker = pool.submit(confirm, ctl, draft, pat="pt-private")
        assert entered.wait(5)
        try:
            with pytest.raises(ManagementError):
                confirm(ctl, draft, pat="pt-private")
        finally:
            finish.set()
        worker.result()
    confirm(ctl, draft, pat="pt-private")
    assert calls == [1]


def test_rejected_pat_keeps_confirmed_profile_and_never_saves(setup, monkeypatch):
    ctl, _, _ = setup
    draft = prepare(ctl, login_method="pat")
    monkeypatch.setattr(common, "request", lambda *a, **kw: (401, {"code": "401"}))
    with pytest.raises(ManagementError) as refused:
        confirm(ctl, draft, pat="pt-refused")
    assert refused.value.code.value == "VALIDATION_FAILED"
    assert not config.get()["oauthAccounts"]
    with pytest.raises(ManagementError):
        ctl.regenerate_qoder_device(context(), draft.flow_id, draft.flow_secret, draft.device_preview["revision"])


def test_management_api_requires_prepare_then_confirm_and_redacts(tmp_path):
    client, headers, _, ctl, backend = auth_client(tmp_path)
    root = "/api/management/v1/oauth/login-flows"
    try:
        response = client.post(root, headers=headers, json={"provider": "qoder", "loginMethod": "pat"})
        assert response.status_code == 201, response.text
        draft = response.json()["data"]
        assert draft["authUrl"] is None and draft["devicePreview"]["phase"] == "preparing"
        args = {"flowSecret": draft["flowSecret"], "revision": draft["devicePreview"]["revision"]}
        path = root + "/" + draft["flowId"]
        confirmation = client.post(path + "/qoder-device/confirm", headers=headers, json=args)
        assert confirmation.status_code == 200, confirmation.text
        result = client.post(path + "/qoder-device/confirm", headers=headers, json={**args, "pat": "pt-private"})
        assert result.status_code == 200, result.text
        assert "pt-private" not in result.text and "blocks" not in result.text
        assert client.post(path + "/qoder-device/regenerate", headers=headers, json=args).status_code >= 400
        poll = client.post(path + "/poll", headers=headers, json={"flowSecret": draft["flowSecret"]})
        assert poll.status_code == 200 and poll.json()["data"]["status"] == "completed", poll.text
        assert "pt-private" not in poll.text and "blocks" not in poll.text
        assert client.post(root, headers=headers, json={"provider": "qoder", "pat": "pt-private"}).status_code >= 400
    finally:
        client.__exit__(None, None, None)
