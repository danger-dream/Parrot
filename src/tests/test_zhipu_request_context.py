"""Account-owned devices and bounded, content-independent ZCode attribution."""
from __future__ import annotations

import copy
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from src import config, oauth_manager as om
from src.oauth.zhipu import auth, request_context as rc, runtime
from src.tests.test_zhipu_provider import account_env, credential


def test_three_accounts_have_distinct_persisted_devices_after_reload_and_relogin(account_env, monkeypatch):
    entries = [credential("oauth", subject=f"user-{i}") for i in range(3)]
    keys, devices = [], []
    for entry in entries:
        om.add_account(entry)
        key = om.get_account_key(entry)
        keys.append(key)
        devices.append(runtime.ensure_device_id(key, om.get_account(key)))
    assert len(set(devices)) == 3
    assert all(uuid.UUID(device).version == 4 for device in devices)
    saved = json.loads(Path(config.path()).read_text())
    assert [a["zcode_device_id"] for a in saved["oauthAccounts"]] == devices

    config.reload()
    for entry, key, device in zip(entries, keys, devices):
        # Login returns no device ID; the account's own ID must survive.
        om.add_account({**entry, "access_token": "renewed-biz", "zcode_token": "renewed-platform"})
        assert runtime.ensure_device_id(key, om.get_account(key)) == device
        monkeypatch.setattr(auth, "resolve_model_key", lambda *a, **k: "rotated.secret")
        monkeypatch.setattr(auth, "entitlement", lambda *a, **k: "available")
        runtime.refresh_locked(copy.deepcopy(om.get_account(key)), key, True)
        assert runtime.ensure_device_id(key, om.get_account(key)) == device
    config.reload()
    assert [om.get_account(key)["zcode_device_id"] for key in keys] == devices


def test_concurrent_first_requests_share_one_persisted_device(account_env):
    entry = credential("oauth")
    om.add_account(entry)
    key = om.get_account_key(entry)
    snapshot = copy.deepcopy(om.get_account(key))
    with ThreadPoolExecutor(max_workers=6) as pool:
        devices = list(pool.map(lambda _: runtime.ensure_device_id(key, snapshot), range(12)))
    assert len(set(devices)) == 1
    assert om.get_account(key)["zcode_device_id"] == devices[0]


def test_import_keeps_device_and_blank_login_does_not_reset_it():
    device = str(uuid.uuid4())
    assert credential("oauth", zcode_device_id=device)["zcode_device_id"] == device
    assert "zcode_device_id" not in credential("oauth", zcode_device_id="")
    with pytest.raises(ValueError):
        credential("oauth", zcode_device_id="not-a-uuid")


def test_context_retry_stability_and_no_cross_request_inference():
    body = {"messages": [{"role": "user", "content": "same prompt"}],
            "_parrot_api_key_name": "tenant", "_parrot_client_ip": "192.0.2.1"}
    first = rc.ensure_request_context(body)
    assert rc.ensure_request_context(first) == first
    second = rc.ensure_request_context(body)
    for kind in ("session", "query", "trace"):
        field = "_parrot_zcode_" + kind
        assert first[field] != second[field]
        uuid.UUID(first[field])
    assert "_parrot_zcode_session" not in body


@pytest.mark.parametrize("header", ["session-id", "x-session-id", "x-claude-code-session-id"])
def test_explicit_session_header_is_stable_but_query_is_per_logical_request(header):
    session = str(uuid.uuid4())
    body = {"_parrot_zcode_session": "untrusted", "_parrot_zcode_trace": "untrusted"}
    rc.capture_headers(body, {header: "sess_" + session})
    first, second = rc.ensure_request_context(body), rc.ensure_request_context(body)
    assert first["_parrot_zcode_session"] == second["_parrot_zcode_session"] == session
    assert first["_parrot_zcode_query"] != second["_parrot_zcode_query"]
    assert first["_parrot_zcode_trace"] != "untrusted"


def test_explicit_query_v7_and_opaque_ids_are_not_message_heuristics():
    query = "01931a2b-3c4d-7e8f-9012-345678901234"
    body = {}
    rc.capture_headers(body, {"session-id": "session-with-private-name", "x-query-id": "query_" + query})
    first = rc.ensure_request_context(body, api_key_name="tenant-a")
    second = rc.ensure_request_context(body, api_key_name="tenant-a")
    other = rc.ensure_request_context(body, api_key_name="tenant-b")
    assert first["_parrot_zcode_query"] == query
    assert first["_parrot_zcode_session"] == second["_parrot_zcode_session"]
    assert first["_parrot_zcode_session"] != other["_parrot_zcode_session"]
    assert "private" not in first["_parrot_zcode_session"]


def test_metadata_session_is_reused_and_existing_user_id_untouched():
    session = str(uuid.uuid4())
    value = json.dumps({"session_id": session, "device_id": "caller-device", "account_uuid": ""})
    body = {"metadata": {"user_id": value, "custom": "preserve"}}
    context = rc.ensure_request_context(body)
    assert context["_parrot_zcode_session"] == session
    rc.add_metadata(body, device_id="account-device", session_id=session)
    assert body == {"metadata": {"user_id": value, "custom": "preserve"}}
