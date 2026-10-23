"""10-minute time-series buckets and GET /stats/recent-outcomes (per-key recent status bars)."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from src import log_db
from src.management_control.observability.common import telegram_context
from src.management_control.observability.stats import StatsControl
from src.tests.webui_support import API, MonthlyLogStore, bearer, build_app, insert_request, key_session


CTX = telegram_context("recent-outcomes-test")


@pytest.fixture
def store(monkeypatch, tmp_path):
    value = MonthlyLogStore(monkeypatch, tmp_path)
    yield value
    value.close()


def test_ten_minute_buckets_cover_a_day(store):
    now = time.time()
    conn = log_db._get_conn()
    # Inside the bucket that holds `now`, however close `now` is to a 10-minute boundary.
    current_bucket = now - now % 600
    insert_request(conn, request_id="a", created_at=current_bucket + (now - current_bucket) / 2)
    insert_request(conn, request_id="b", created_at=now - 700, status="error")
    conn.commit()
    data = StatsControl(now=lambda: now).timeseries(CTX, range_value="24h", bucket="10m")
    points = data["series"][0]["points"]
    assert data["bucket"] == "10m" and data["bucketSeconds"] == 600 and len(points) == 144
    assert points[-1]["total"] == 1 and points[-1]["successCount"] == 1
    assert sum(p["errorCount"] for p in points) == 1


def test_recent_outcomes_keep_last_n_per_key_oldest_first(store):
    now = time.time()
    conn = log_db._get_conn()
    for i, status in enumerate(["success", "error", "success", "success", "cancelled"]):
        insert_request(conn, request_id=f"x{i}", created_at=now - 100 + i, status=status,
                       final_channel_key="api:alpha", api_key_name="k1")
    insert_request(conn, request_id="y", created_at=now - 50, status="error", final_channel_key="oauth:b:1")
    insert_request(conn, request_id="pending", created_at=now - 10, status="pending", final_channel_key="api:alpha")
    insert_request(conn, request_id="old", created_at=now - 9 * 86400, final_channel_key="api:old")
    conn.commit()
    control = StatsControl(now=lambda: now)
    items = {item["key"]: item for item in control.recent_outcomes(CTX, dimension="channel", limit=3)}
    assert set(items) == {"api:alpha", "oauth:b:1"}
    assert items["api:alpha"]["outcomes"] == ["success", "success", "cancelled"]
    assert (items["api:alpha"]["successCount"], items["api:alpha"]["errorCount"]) == (2, 0)
    assert items["oauth:b:1"]["outcomes"] == ["error"]
    by_key = control.recent_outcomes(CTX, dimension="apiKey", limit=10)
    assert [item["key"] for item in by_key] == ["client", "k1"]


def test_recent_outcomes_api_contract(store, tmp_path):
    now = time.time()
    conn = log_db._get_conn()
    insert_request(conn, request_id="r1", created_at=now - 5, requested_model="m1")
    conn.commit()
    app, _runtime, _ = build_app(tmp_path)
    with TestClient(app) as client:
        headers = bearer(key_session(client).json()["data"]["credential"])
        ok = client.get(f"{API}/stats/recent-outcomes?dimension=model&limit=5", headers=headers)
        assert ok.status_code == 200, ok.text
        [item] = ok.json()["data"]
        assert set(item) == {"key", "outcomes", "successCount", "errorCount", "lastAt"}
        assert item["key"] == "m1" and item["outcomes"] == ["success"] and item["lastAt"].endswith("Z")
        series = client.get(f"{API}/stats/timeseries?range=24h&bucket=10m", headers=headers)
        assert series.status_code == 200 and series.json()["data"]["bucket"] == "10m"
        for query in ("dimension=user", "dimension=model&limit=0", "dimension=model&limit=61", "dimension=model&x=1"):
            assert client.get(f"{API}/stats/recent-outcomes?{query}", headers=headers).status_code == 422, query
