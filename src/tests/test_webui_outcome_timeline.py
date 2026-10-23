"""GET /stats/outcome-timeline: clock-aligned buckets, empty buckets, error / cancel counts."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from src import log_db
from src.management_control.observability.common import telegram_context
from src.management_control.observability.stats import StatsControl
from src.tests.webui_support import API, MonthlyLogStore, bearer, build_app, insert_request, key_session

CTX = telegram_context("timeline-test")


@pytest.fixture
def store(monkeypatch, tmp_path):
    value = MonthlyLogStore(monkeypatch, tmp_path)
    yield value
    value.close()


def test_hourly_buckets_end_with_the_current_hour(store):
    now = (int(time.time()) // 3600) * 3600 + 1800  # half past the hour
    conn = log_db._get_conn()
    rows = [
        ("a", now - 60, "api:one", "success"),
        ("b", now - 120, "api:one", "error"),
        ("c", now - 3600 - 60, "api:one", "cancelled"),
        ("d", now - 5 * 3600, "api:two", "success"),
        ("e", now - 30 * 3600, "api:one", "error"),  # outside the 24 h window
        ("f", now - 30, "api:one", "pending"),  # still running: not an outcome
    ]
    for rid, created, channel, status in rows:
        insert_request(conn, request_id=rid, created_at=created, final_channel_key=channel, status=status)
    conn.commit()
    data = StatsControl(now=lambda: now).outcome_timeline(CTX, dimension="channel", bucket_minutes=60, buckets=24)

    assert data["bucketSeconds"] == 3600
    assert data["from"].timestamp() == now - 1800 - 23 * 3600
    by_key = {item["key"]: item for item in data["items"]}
    one = by_key["api:one"]
    assert len(one["buckets"]) == 24
    assert one["buckets"][-1] == {"total": 2, "errorCount": 1, "cancelledCount": 0}
    assert one["buckets"][-2] == {"total": 1, "errorCount": 0, "cancelledCount": 1}
    assert (one["total"], one["errorCount"], one["cancelledCount"]) == (3, 1, 1)
    assert sum(b["total"] for b in one["buckets"][:-2]) == 0
    assert by_key["api:two"]["buckets"][-6]["total"] == 1


def test_endpoint_validates_and_serves_the_timeline(store, tmp_path):
    conn = log_db._get_conn()
    insert_request(conn, request_id="x", created_at=time.time() - 10, final_channel_key="api:one", status="success")
    conn.commit()
    app, _runtime, _ = build_app(tmp_path)
    with TestClient(app) as client:
        headers = bearer(key_session(client).json()["data"]["credential"])
        ok = client.get(f"{API}/stats/outcome-timeline?dimension=channel", headers=headers)
        assert ok.status_code == 200, ok.text
        data = ok.json()["data"]
        assert data["bucketSeconds"] == 3600
        assert [item["key"] for item in data["items"]] == ["api:one"]
        assert data["items"][0]["buckets"][-1]["total"] == 1
        bad = client.get(f"{API}/stats/outcome-timeline?dimension=channel&buckets=500", headers=headers)
        assert bad.status_code == 422
