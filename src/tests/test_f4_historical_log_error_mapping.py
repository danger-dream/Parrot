from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src import log_db
from src.management_control.observability import LogsControl, RetentionControl, StatsControl
from src.tests.management_observability_support import build_client, fake_controls


@pytest.fixture
def isolated_log_store(monkeypatch, tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setattr(log_db, "_log_dir", str(log_dir))
    monkeypatch.setattr(log_db, "_initialized", True)
    monkeypatch.setattr(log_db, "_local", threading.local())
    monkeypatch.setattr(log_db, "_write_conn_registry", {})
    monkeypatch.setattr(log_db, "_retired_log_paths", set())

    log_db._get_conn()
    current_start = datetime.now(log_db._BJT).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0,
    )
    historical_month = (current_start - timedelta(seconds=1)).strftime("%Y-%m")
    bad_path = log_dir / f"{historical_month}.db"
    conn = sqlite3.connect(bad_path)
    try:
        conn.execute("CREATE TABLE unrelated(value TEXT)")
        conn.commit()
    finally:
        conn.close()

    yield bad_path

    for connections in list(log_db._write_conn_registry.values()):
        for conn in connections:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def test_strict_corrupt_history_is_retryable_503_for_every_management_log_reader(
    isolated_log_store, tmp_path,
):
    controls = fake_controls()
    controls.logs = LogsControl(log_db=log_db)
    controls.stats = StatsControl(log_db=log_db)
    controls.retention = RetentionControl(
        log_db=log_db,
        config=SimpleNamespace(get=lambda: {
            "logRetention": {"mode": "forever"},
            "logStoreBodies": True,
        }),
    )
    management_dir = tmp_path / "management"
    management_dir.mkdir()
    raising_client, runtime, _installed, auth = build_client(
        management_dir, controls_value=controls,
    )
    client = TestClient(raising_client.app, raise_server_exceptions=False)
    requests = (
        ("/api/management/v1/logs", {}),
        ("/api/management/v1/logs/filter-options", {}),
        ("/api/management/v1/logs/unresolved-history-id", {}),
        ("/api/management/v1/logs/unresolved-history-id/body", {"kind": "request"}),
        (
            "/api/management/v1/logs/unresolved-history-id/body/items/item_1",
            {"kind": "request"},
        ),
        ("/api/management/v1/logs/unresolved-history-id/raw-body", {"kind": "request"}),
        ("/api/management/v1/stats/recent-calls", {}),
        ("/api/management/v1/logs/retention", {}),
    )
    try:
        responses = [
            (path, client.get(path, params=params, headers=auth))
            for path, params in requests
        ]
        for path, response in responses:
            assert response.status_code == 503, (path, response.status_code, response.text)
            assert response.headers["content-type"].startswith("application/json")
            error = response.json()["error"]
            message = error.pop("message")
            assert "未分类异常" in message
            assert "服务器诊断" in message
            assert error == {
                "code": "DEPENDENCY_UNAVAILABLE",
                "fields": [],
                "retryable": True,
                "requestId": "request-p4-test",
                "operationId": None,
            }
            assert str(isolated_log_store) not in response.text
            assert "request_log" not in response.text
            assert "no such table" not in response.text

        isolated_log_store.unlink()
        missing_detail = client.get(
            "/api/management/v1/logs/normal-missing-id", headers=auth,
        )
        missing_body = client.get(
            "/api/management/v1/logs/normal-missing-id/raw-body",
            params={"kind": "request"}, headers=auth,
        )
        assert missing_detail.status_code == 404
        assert missing_detail.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert missing_body.status_code == 404
        assert missing_body.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
    finally:
        client.close()
        raising_client.close()
        runtime.close()
