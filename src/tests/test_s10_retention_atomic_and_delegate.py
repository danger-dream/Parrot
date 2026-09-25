from __future__ import annotations

import copy
import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from src import config, log_db
from src.management_control.observability import RetentionControl
from src.management_control.observability.common import telegram_context
from src.management_control.system.telegram_retention import TelegramRetentionAdapter
from src.tests.management_observability_support import build_client, fake_controls


_BJT = timezone(timedelta(hours=8))


@pytest.fixture
def real_retention_store(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    initial = {
        "logDir": str(log_dir),
        "logRetention": {"mode": "days", "days": 30},
        "logStoreBodies": True,
    }
    config_path.write_text(json.dumps(initial), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", str(config_path))
    monkeypatch.setattr(config, "_cache", copy.deepcopy(initial))
    monkeypatch.setattr(config, "_mtime", os.path.getmtime(config_path))

    registry: dict[str, list[sqlite3.Connection]] = {}
    monkeypatch.setattr(log_db, "_log_dir", str(log_dir))
    monkeypatch.setattr(log_db, "_initialized", True)
    monkeypatch.setattr(log_db, "_local", threading.local())
    monkeypatch.setattr(log_db, "_write_lock", threading.RLock())
    monkeypatch.setattr(log_db, "_retention_lock", threading.Lock())
    monkeypatch.setattr(log_db, "_retention_auto_lock", threading.Lock())
    monkeypatch.setattr(log_db, "_last_retention_cleanup_key", None)
    monkeypatch.setattr(log_db, "_write_conn_registry", registry)
    monkeypatch.setattr(log_db, "_write_conn_registry_lock", threading.Lock())
    monkeypatch.setattr(log_db, "_retired_log_paths", set())
    monkeypatch.setattr(log_db, "_request_handles", {})

    yield {"config": config_path, "logs": log_dir}

    for connections in registry.values():
        for connection in connections:
            try:
                connection.close()
            except sqlite3.Error:
                pass
    for connection in getattr(log_db._local, "write_conns", {}).values():
        try:
            connection.close()
        except sqlite3.Error:
            pass


def _create_month(log_dir, month: str, rows: list[tuple[str, float]]) -> str:
    path = log_dir / f"{month}.db"
    connection = sqlite3.connect(path)
    try:
        connection.executescript(log_db._schema_sql())
        connection.executemany(
            "INSERT INTO request_log(request_id, created_at, status) VALUES(?, ?, 'success')",
            rows,
        )
        connection.commit()
    finally:
        connection.close()
    return str(path)


def _real_client(tmp_path, control: RetentionControl):
    controls = fake_controls()
    controls.retention = control
    runtime_dir = tmp_path / "management"
    runtime_dir.mkdir()
    return build_client(runtime_dir, controls_value=controls)


def test_asgi_mixed_patch_is_one_atomic_write_with_cas_and_failure_rollback(
    real_retention_store,
    monkeypatch,
    tmp_path,
):
    control = RetentionControl(log_db=log_db, config=config)
    client, runtime, _, auth = _real_client(tmp_path, control)
    original_write = config._write_atomic
    writes: list[dict] = []

    def record_write(candidate):
        writes.append(copy.deepcopy(candidate))
        original_write(candidate)

    monkeypatch.setattr(config, "_write_atomic", record_write)
    before = client.get("/api/management/v1/logs/retention", headers=auth)
    assert before.status_code == 200, before.text
    old_revision = before.json()["data"]["revision"]

    changed = client.patch(
        "/api/management/v1/logs/retention",
        json={"mode": "days", "days": 60, "logStoreBodies": False},
        headers={**auth, "If-Match": old_revision},
    )
    assert changed.status_code == 200, changed.text
    assert len(writes) == 1
    assert writes[0]["logRetention"] == {"mode": "days", "days": 60}
    assert writes[0]["logStoreBodies"] is False
    assert config.get()["logRetention"] == {"mode": "days", "days": 60}
    assert config.get()["logStoreBodies"] is False
    persisted = json.loads(real_retention_store["config"].read_text(encoding="utf-8"))
    assert persisted["logRetention"] == {"mode": "days", "days": 60}
    assert persisted["logStoreBodies"] is False

    stale = client.patch(
        "/api/management/v1/logs/retention",
        json={"mode": "forever"},
        headers={**auth, "If-Match": old_revision},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "REVISION_CONFLICT"
    assert len(writes) == 1

    revision = changed.json()["data"]["revision"]
    dangerous = client.patch(
        "/api/management/v1/logs/retention",
        json={"mode": "days", "days": 10, "logStoreBodies": True},
        headers={**auth, "If-Match": revision},
    )
    assert dangerous.status_code == 400
    assert dangerous.json()["error"]["code"] == "CONFIRMATION_REQUIRED"
    assert config.get()["logRetention"] == {"mode": "days", "days": 60}
    assert config.get()["logStoreBodies"] is False
    assert len(writes) == 1

    cache_before_failure = copy.deepcopy(config.get())
    disk_before_failure = real_retention_store["config"].read_bytes()

    def fail_write(_candidate):
        raise OSError("injected-persist-failure")

    monkeypatch.setattr(config, "_write_atomic", fail_write)
    failed = client.patch(
        "/api/management/v1/logs/retention",
        json={"mode": "forever", "logStoreBodies": True},
        headers={**auth, "If-Match": revision},
    )
    assert failed.status_code == 503, failed.text
    assert failed.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    assert config.get() == cache_before_failure
    assert real_retention_store["config"].read_bytes() == disk_before_failure
    runtime.operations.close(timeout_seconds=2)
    client.close()


def test_retention_current_rows_counts_all_strict_months_via_real_asgi(
    real_retention_store,
    tmp_path,
):
    old = datetime.now(tz=_BJT) - timedelta(days=120)
    newer = datetime.now(tz=_BJT) - timedelta(days=70)
    _create_month(
        real_retention_store["logs"],
        old.strftime("%Y-%m"),
        [("old-a", old.timestamp()), ("old-b", (old + timedelta(days=1)).timestamp())],
    )
    _create_month(
        real_retention_store["logs"],
        newer.strftime("%Y-%m"),
        [("newer-a", newer.timestamp())],
    )
    # Valid SQLite but non-month filenames remain outside the Management count.
    _create_month(real_retention_store["logs"], "not-a-month", [("ignored", old.timestamp())])

    control = RetentionControl(log_db=log_db, config=config)
    client, runtime, _, auth = _real_client(tmp_path, control)
    response = client.get("/api/management/v1/logs/retention", headers=auth)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["currentData"] == {"rows": 3}
    runtime.operations.close(timeout_seconds=2)
    client.close()


def test_plan_id_round_trip_commit_and_cancel_with_real_control_and_log_db(
    real_retention_store,
    tmp_path,
):
    config.update(
        lambda cfg: cfg.__setitem__("logRetention", {"mode": "forever", "days": None}),
    )
    old = datetime.now(tz=_BJT) - timedelta(days=500)
    old_path = _create_month(
        real_retention_store["logs"], old.strftime("%Y-%m"), [("expired", old.timestamp())],
    )
    control = RetentionControl(log_db=log_db, config=config)
    client, runtime, _, auth = _real_client(tmp_path, control)
    schemas = client.get("/openapi.json").json()["components"]["schemas"]
    properties = schemas["RetentionPlanData"]["properties"]
    assert "planId" in properties and "id" not in properties

    prepared = client.post(
        "/api/management/v1/logs/retention/plans", json={"days": 30}, headers=auth,
    )
    assert prepared.status_code == 201, prepared.text
    plan = prepared.json()["data"]
    assert plan["planId"].startswith("plan_")
    assert "id" not in plan

    cancellable = client.post(
        "/api/management/v1/logs/retention/plans", json={"days": 31}, headers=auth,
    ).json()["data"]
    cancelled = client.delete(
        f"/api/management/v1/logs/retention/plans/{cancellable['planId']}", headers=auth,
    )
    assert cancelled.status_code == 204
    cancelled_commit = client.post(
        f"/api/management/v1/logs/retention/plans/{cancellable['planId']}/commit",
        headers={**auth, "If-Match": cancellable["revision"]},
    )
    assert cancelled_commit.status_code == 409

    committed = client.post(
        f"/api/management/v1/logs/retention/plans/{plan['planId']}/commit",
        headers={**auth, "If-Match": plan["revision"]},
    )
    assert committed.status_code == 202, committed.text
    operation_id = committed.json()["data"]["id"]
    # close() cancels queued work; it is not a completion barrier. Wait for
    # this operation before shutdown, including when its worker hasn't started.
    with runtime.operations._lock:
        future = runtime.operations._futures.get(operation_id)
    if future is not None:
        future.result(timeout=5)
    runtime.operations.close(timeout_seconds=5)
    with runtime.operations._lock:
        operation = runtime.operations._items[operation_id]
    assert operation.status.value == "succeeded", operation.error
    assert operation.result["deletedRows"] == 1
    assert operation.result["policyActivated"] is True
    assert config.get()["logRetention"] == {"mode": "days", "days": 30}
    assert not os.path.exists(old_path)
    client.close()


def test_real_retention_control_is_the_sync_telegram_delegate(
    real_retention_store,
):
    config.update(
        lambda cfg: cfg.__setitem__("logRetention", {"mode": "forever", "days": None}),
    )
    old = datetime.now(tz=_BJT) - timedelta(days=500)
    old_path = _create_month(
        real_retention_store["logs"], old.strftime("%Y-%m"), [("tg-expired", old.timestamp())],
    )
    control = RetentionControl(log_db=log_db, config=config)
    adapter = TelegramRetentionAdapter(control)
    context = telegram_context("retention-sync-delegate")
    assert adapter.control is control

    adapter.set_log_store_bodies(context, False)
    assert config.get()["logStoreBodies"] is False
    plan = adapter.create_plan(context, 30)
    progress: list[str] = []
    result = adapter.commit_plan(
        context,
        plan,
        progress=lambda event: progress.append(str(event.get("phase"))),
    )
    assert result["ok"] is True
    assert progress[0] == "item_start" and progress[-1] == "item_done"
    assert config.get()["logRetention"] == {"mode": "days", "days": 30}
    assert not os.path.exists(old_path)
    # Completion is synchronous: policy/data effects exist before the call returns.
    assert adapter.set_forever(context)["ok"] is True
    assert config.get()["logRetention"] == {"mode": "forever", "days": None}
