"""Real server/Control/DB regression gates for the four post-delivery findings."""
from __future__ import annotations

import asyncio
import copy
import json
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

import server
from src import config, log_db, oauth_manager
from src.tests.test_log_db_round_handles import isolated_log_db
from src.tests.test_management_channels_api import _reset_channels

PREFIX = "/api/management/v1"


@pytest.fixture
def actual_management(tmp_path):
    before = copy.deepcopy(config.get())
    management_key = "pmk_" + "delivery-regression-only-" * 3

    def setup(current):
        current.update({
            "apiKeys": {}, "oauthAccounts": [], "channels": [],
            "modelBindings": {"defaults": {}, "scoped": {}},
            "network": copy.deepcopy(config.DEFAULT_CONFIG["network"]),
            "management": {
                **copy.deepcopy(config.DEFAULT_CONFIG["management"]),
                "managementKey": management_key,
                "stateDbPath": str(tmp_path / "management.db"),
            },
            "telegram": {"botToken": "", "adminIds": []},
        })

    config.update(setup)
    _reset_channels()
    runtime = server._initialize_management_runtime(server.app)
    assert runtime is not None
    # No lifespan: do not start external refresh, Telegram, or update workers.
    client = TestClient(server.app, raise_server_exceptions=False)
    try:
        session = client.post(PREFIX + "/auth/sessions", json={
            "grantType": "managementKey", "managementKey": management_key,
        })
        assert session.status_code == 201, session.text
        client.headers["Authorization"] = "Bearer " + session.json()["data"]["credential"]
        yield client, runtime
    finally:
        client.close()
        asyncio.run(server._close_management_runtime(server.app))
        _reset_channels()
        config.update(lambda current: (current.clear(), current.update(before)))


@pytest.mark.parametrize("name", [
    "region/team", "region/team/compatibility", "区域/percent%2F?",
])
def test_encoded_channel_id_roundtrip_and_subresources(actual_management, name):
    client, _runtime = actual_management
    created = client.post(PREFIX + "/channels", json={
        "mode": "manual", "name": name, "baseUrl": "https://example.test",
        "apiKey": "regression-only-channel-key", "protocol": "openai-chat",
        "models": [{"real": "provider/model", "alias": "provider/model"}],
    })
    assert created.status_code == 201, created.text
    initial = created.json()["data"]
    channel_id = initial["id"]
    path = PREFIX + "/channels/" + quote(channel_id, safe="")
    assert any(row["id"] == channel_id for row in client.get(PREFIX + "/channels").json()["data"])
    read = client.get(path)
    assert read.status_code == 200, read.text
    assert read.json()["data"]["id"] == channel_id
    compatibility = client.get(path + "/compatibility")
    assert compatibility.status_code == 200, compatibility.text
    changed_compatibility = client.patch(path + "/compatibility", json={
        "fast": {"mode": "force", "models": ["provider/model"]},
    })
    assert changed_compatibility.status_code == 200, changed_compatibility.text
    assert changed_compatibility.json()["data"]["fast"]["mode"] == "force"
    for action in ("clear-errors", "clear-affinity"):
        result = client.post(path + "/actions/" + action)
        assert result.status_code == 200, result.text
    changed = client.patch(path, json={"enabled": False})
    assert changed.status_code == 200, changed.text
    assert changed.json()["data"]["enabled"] is False
    # A successful PATCH invalidates the creation revision: do not weaken CAS
    # merely to make the original audit's sequential DELETE pass.
    stale = client.delete(path, headers={"If-Match": initial["revision"]})
    assert stale.status_code == 409, stale.text
    deleted = client.delete(path, headers={"If-Match": changed.json()["data"]["revision"]})
    assert deleted.status_code == 204, deleted.text
    assert not any(row.get("name") == name for row in config.get()["channels"])
    assert client.get(path).status_code == 404


def test_model_stats_preserves_slash_id(actual_management):
    client, _runtime = actual_management
    for index, model in enumerate(("audit-plain-model", "audit-provider/model")):
        handle = log_db.insert_pending(
            "delivery-model-" + str(index), "127.0.0.1", "audit-key", model,
            False, 1, 0, {}, {},
        )
        log_db.finish_success(handle, "api:audit", "api", model, input_tokens=1, output_tokens=1)
    summary = client.get(PREFIX + "/stats/breakdown", params={"dimension": "model", "pageSize": 200})
    assert summary.status_code == 200, summary.text
    assert {row["key"] for row in summary.json()["data"]} >= {"audit-plain-model", "audit-provider/model"}
    for model in ("audit-plain-model", "audit-provider/model"):
        detail = client.get(PREFIX + "/stats/models/" + quote(model, safe=""))
        assert detail.status_code == 200, detail.text


@pytest.mark.parametrize("ticks", [2_500_000_000, 0, None])
def test_oauth_local_cost_uses_real_billing_ticks(actual_management, ticks):
    client, _runtime = actual_management
    suffix = str(ticks) if ticks is not None else "unknown"
    account = {
        "provider": "xai", "subject": "delivery-cost-" + suffix,
        "email": "audit@example.test", "enabled": True,
        "access_token": "audit-access-not-real", "refresh_token": "audit-refresh-not-real",
        "models": [],
    }
    model = "grok-4.5" if ticks is not None else "audit-no-pricing"
    binding = {model: {"target": "xai/grok-4.5", "source": "manual"}} if ticks is not None else {}
    config.update(lambda current: current.update({
        "oauthAccounts": [account], "logStoreBodies": True,
        "modelBindings": {"defaults": binding, "scoped": {}},
    }))
    account_id = oauth_manager.get_account_key(account)
    channel_key = "oauth:" + account_id
    handle = log_db.insert_pending("delivery-cost-" + suffix, "127.0.0.1", "audit-key", model, False, 1, 0, {}, {})
    attempt = log_db.record_retry_attempt(handle, 1, channel_key, "oauth", model, time.time(), upstream_protocol="openai-responses")
    log_db.mark_retry_attempt_dispatch(attempt, {"model": model})
    usage = {"input_tokens": 10, "output_tokens": 3}
    if ticks is not None:
        usage["cost_in_usd_ticks"] = ticks
    log_db.finish_success(handle, channel_key, "oauth", model, input_tokens=10, output_tokens=3,
                          response_body=json.dumps({"usage": usage}))
    backend = log_db.tokens_for_channel(channel_key, 0)
    assert backend["costed_success"] == (0 if ticks is None else 1)
    assert backend["cost_ticks"] == (ticks or 0)
    response = client.get(PREFIX + "/oauth/accounts/" + quote(account_id, safe=""))
    assert response.status_code == 200, response.text
    local = response.json()["data"]["localStats"]
    assert local["requestCount"] == 1
    assert local["inputTokens"] == 10 and local["outputTokens"] == 3
    assert local["costUsd"] == (None if ticks is None else ticks / 10_000_000_000)


@pytest.mark.parametrize("hour_bjt", [12, 3], ids=["month-start-eight-hours", "utc-still-previous-year"])
def test_oauth_local_stats_use_telegram_bjt_month(actual_management, isolated_log_db, monkeypatch, hour_bjt):
    client, runtime = actual_management
    bjt = timezone(timedelta(hours=8))
    month_start = datetime(2026, 1, 1, tzinfo=bjt)
    now = month_start + timedelta(hours=hour_bjt)
    monkeypatch.setattr(runtime.control_owner().oauth, "_clock", lambda: now.astimezone(timezone.utc))
    account = {
        "provider": "xai", "subject": f"month-boundary-{hour_bjt}",
        "email": "month@example.test", "enabled": True,
        "access_token": "month-regression-not-real", "models": [],
    }
    config.update(lambda current: current.update({"oauthAccounts": [account]}))
    account_id = oauth_manager.get_account_key(account)
    channel_key = "oauth:" + account_id
    expected = {"requestCount": 0, "inputTokens": 0, "outputTokens": 0}
    for index, seconds in enumerate((-1, 0, 3600, 9 * 3600)):
        created_at = month_start + timedelta(seconds=seconds)
        if created_at > now:
            continue
        handle = log_db.insert_pending(
            f"month-{hour_bjt}-{index}", "127.0.0.1", "month-key", "month-model",
            False, 1, 0, {}, {}, created_at=created_at.timestamp(),
        )
        log_db.finish_success(
            handle, channel_key, "oauth", "month-model",
            input_tokens=10 + index, output_tokens=3 + index,
        )
        if seconds >= 0:
            expected["requestCount"] += 1
            expected["inputTokens"] += 10 + index
            expected["outputTokens"] += 3 + index
    # The frozen TG rule is the first day at 00:00 UTC+8, not UTC midnight.
    telegram_stats = log_db.tokens_for_channel(channel_key, month_start.timestamp())
    assert telegram_stats["total"] == expected["requestCount"]
    assert telegram_stats["input"] == expected["inputTokens"]
    assert telegram_stats["output"] == expected["outputTokens"]
    response = client.get(PREFIX + "/oauth/accounts/" + quote(account_id, safe=""))
    assert response.status_code == 200, response.text
    local = response.json()["data"]["localStats"]
    assert {field: local[field] for field in expected} == expected


@pytest.mark.parametrize("generated", [False, True])
def test_audit_db_failure_does_not_hide_committed_key_secret(actual_management, generated, caplog):
    client, runtime = actual_management
    original = "audit-original-123456"
    created = client.post(PREFIX + "/api-keys", json={
        "mode": "custom", "name": "audit-key", "customSecret": original,
    })
    assert created.status_code == 201, created.text
    revision = client.get(PREFIX + "/api-keys/audit-key").json()["data"]["revision"]
    if generated:
        plan = client.post(PREFIX + "/api-keys/audit-key/actions/generate-replacement-plan")
        assert plan.status_code == 200, plan.text
        plan_data = plan.json()["data"]
    with runtime.state_store._lock:
        runtime.state_store._conn.execute("PRAGMA query_only=ON")
    try:
        if generated:
            changed = client.post(PREFIX + "/api-keys/audit-key/actions/generate-replacement", json={
                "planId": plan_data["planId"], "planToken": plan_data["planToken"],
            })
        else:
            changed = client.put(PREFIX + "/api-keys/audit-key/secret", headers={"If-Match": revision},
                                 json={"customSecret": "audit-replacement-654321"})
        assert changed.status_code == 200, changed.text
        secret = changed.json()["data"]["secret"]
        assert secret != original
        assert config.get()["apiKeys"]["audit-key"]["key"] == secret
        # Round 3: the detail read intentionally returns the saved secret to a
        # secrets-capable session; logs must still never contain it.
        assert client.get(PREFIX + "/api-keys/audit-key").json()["data"]["secret"] == secret
        assert "Management audit write failed (OperationalError)" in caplog.text
        assert secret not in caplog.text and original not in caplog.text
    finally:
        with runtime.state_store._lock:
            runtime.state_store._conn.execute("PRAGMA query_only=OFF")


def test_config_failure_does_not_publish_rotated_secret(actual_management, monkeypatch):
    client, _runtime = actual_management
    original = "audit-original-123456"
    created = client.post(PREFIX + "/api-keys", json={"mode": "custom", "name": "audit-key", "customSecret": original})
    assert created.status_code == 201
    revision = client.get(PREFIX + "/api-keys/audit-key").json()["data"]["revision"]

    def fail_write(_candidate):
        raise OSError("test config write failure")

    with monkeypatch.context() as patch:
        patch.setattr(config, "_write_atomic", fail_write)
        response = client.put(PREFIX + "/api-keys/audit-key/secret", headers={"If-Match": revision},
                              json={"customSecret": "audit-replacement-654321"})
    assert response.status_code >= 400
    assert config.get()["apiKeys"]["audit-key"]["key"] == original


def test_quota_paths_use_one_candidate_owner_with_one_combined_write(actual_management, monkeypatch):
    client, runtime = actual_management
    owner = runtime.control_owner()
    shared = owner.system.settings
    assert owner.oauth.backend._settings_control is shared
    applied = []
    writes = []
    original_factory = shared.quota_monitor_mutator
    original_update = config.update

    def observe_factory(patch):
        mutate = original_factory(patch)

        def apply(candidate):
            applied.append((shared, dict(patch)))
            return mutate(candidate)
        return apply

    def observe_write(mutator, **kwargs):
        writes.append(1)
        return original_update(mutator, **kwargs)

    monkeypatch.setattr(shared, "quota_monitor_mutator", observe_factory)
    monkeypatch.setattr(config, "update", observe_write)
    system = client.patch(PREFIX + "/settings/quota-monitor", json={"thresholdPercent": 73})
    assert system.status_code == 200, system.text
    assert applied == [(shared, {"thresholdPercent": 73})]
    assert len(writes) == 1
    assert client.get(PREFIX + "/oauth/settings").json()["data"]["quotaMonitor"]["thresholdPercent"] == 73
    old_oauth = client.get(PREFIX + "/oauth/settings").json()["data"]
    oauth = client.patch(PREFIX + "/oauth/settings", headers={"If-Match": old_oauth["revision"]}, json={
        "quotaMonitor": {"thresholdPercent": 74, "intervalSeconds": 120}, "cchMode": "dynamic",
    })
    assert oauth.status_code == 200, oauth.text
    assert len(applied) == 2 and applied[-1][0] is shared
    assert len(writes) == 2  # quota + CCH remain one atomic config publication.
    stored = config.get()
    assert stored["quotaMonitor"]["disableThresholdPercent"] == stored["quotaMonitor"]["resumeThresholdPercent"] == 74
    assert stored["quotaMonitor"]["intervalSeconds"] == 120
    assert stored["cchMode"] == "dynamic"
    assert client.get(PREFIX + "/settings/quota-monitor").json()["data"]["thresholdPercent"] == 74
    for path, body, revision in (
        ("/settings/quota-monitor", {"thresholdPercent": 72}, system.json()["data"]["revision"]),
        ("/oauth/settings", {"quotaMonitor": {"thresholdPercent": 72}}, old_oauth["revision"]),
    ):
        before = copy.deepcopy(config.get())
        rejected = client.patch(PREFIX + path, headers={"If-Match": revision}, json=body)
        assert rejected.status_code == 409, rejected.text
        assert config.get() == before
