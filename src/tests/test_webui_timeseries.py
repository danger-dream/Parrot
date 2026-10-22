"""GET /stats/timeseries: windows, zero fill, grouping, cost parity, cross-month."""

from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from src import log_db
from src.management_control.errors import ManagementError
from src.management_control.observability.common import telegram_context
from src.management_control.observability.stats import StatsControl
from src.management_control.observability.timeseries import window, TimeseriesBucket, TimeseriesRange
from src.tests.webui_support import (
    API, MonthlyLogStore, bearer, build_app, insert_attempt, insert_request, key_session,
)


CTX = telegram_context("timeseries-test")


@pytest.fixture
def store(monkeypatch, tmp_path):
    value = MonthlyLogStore(monkeypatch, tmp_path)
    yield value
    value.close()


def _iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _sum(series, name):
    return sum(point[name] for item in series for point in item["points"])


def test_window_alignment_and_default_buckets():
    now = 1_760_000_123.0
    start, end, bucket_seconds, count = window(now, TimeseriesRange.ONE_HOUR, TimeseriesBucket.MINUTE)
    assert (bucket_seconds, count) == (60, 60)
    assert start <= now - 3600 + 60 and end > now and (end - start) == 3600
    start, end, bucket_seconds, count = window(now, TimeseriesRange.THIRTY_DAYS, TimeseriesBucket.DAY)
    assert (bucket_seconds, count) == (86400, 30)
    # Day buckets start at Beijing midnight (16:00 UTC), like today/month stats.
    assert datetime.fromtimestamp(start, timezone.utc).hour == 16
    assert end - 86400 <= now < end


def test_zero_filled_continuous_points_and_validation(store):
    now = time.time()
    conn = log_db._get_conn()
    insert_request(conn, request_id="r1", created_at=now - 130, input_tokens=10,
                   output_tokens=20, first_token_time_ms=100, total_time_ms=400)
    insert_request(conn, request_id="r2", created_at=now - 125, status="error",
                   total_time_ms=50)
    insert_request(conn, request_id="r3", created_at=now - 7200)  # outside 1h
    conn.commit()
    control = StatsControl(now=lambda: now)
    data = control.timeseries(CTX, range_value="1h")
    assert (data["range"], data["bucket"], data["bucketSeconds"]) == ("1h", "minute", 60)
    assert data["timezone"] == "Asia/Shanghai"
    (series,) = data["series"]
    assert series["key"] == "total"
    points = series["points"]
    assert len(points) == 60
    assert [p["t"] for p in points] == sorted(p["t"] for p in points)
    assert all((b["t"] - a["t"]).total_seconds() == 60 for a, b in zip(points, points[1:]))
    assert points[0]["t"] == data["from"]
    assert sum(p["total"] for p in points) == 2
    busy = [p for p in points if p["total"]]
    assert len(busy) == 1 and busy[0]["successCount"] == 1 and busy[0]["errorCount"] == 1
    assert busy[0]["inputTokens"] == 10 and busy[0]["outputTokens"] == 20
    assert busy[0]["averageFirstTokenMilliseconds"] == 100
    assert busy[0]["averageTotalMilliseconds"] == 400  # successful requests only
    empty = next(p for p in points if not p["total"])
    assert empty["averageFirstTokenMilliseconds"] is None
    assert empty["averageTotalMilliseconds"] is None
    assert empty["costTicks"] == 0 and empty["inputTokens"] == 0

    defaults = {"24h": ("hour", 24), "7d": ("hour", 168), "30d": ("day", 30)}
    for range_value, (bucket, count) in defaults.items():
        value = control.timeseries(CTX, range_value=range_value)
        assert value["bucket"] == bucket
        assert len(value["series"][0]["points"]) == count
    assert len(control.timeseries(CTX, range_value="30d", bucket="hour")["series"][0]["points"]) == 720
    for kwargs in (
        {"range_value": "30d", "bucket": "minute"},
        {"range_value": "24h", "bucket": "minute"},
        {"range_value": "1h", "limit": 13},
        {"range_value": "1h", "limit": 0},
    ):
        with pytest.raises(ManagementError) as exc:
            control.timeseries(CTX, **kwargs)
        assert exc.value.code.value == "VALIDATION_FAILED"


def test_grouping_top_limit_and_other(store):
    now = time.time()
    conn = log_db._get_conn()
    counter = 0
    for model, count in (("m-five", 5), ("m-four", 4), ("m-three", 3), ("m-one", 1)):
        for _ in range(count):
            counter += 1
            insert_request(conn, request_id=f"g{counter}", created_at=now - 60 * counter,
                           requested_model=model, api_key_name=f"key-{model}",
                           final_channel_key=f"api:{model}")
    conn.commit()
    control = StatsControl(now=lambda: now)
    data = control.timeseries(CTX, range_value="24h", group="model", limit=2)
    keys = [item["key"] for item in data["series"]]
    assert keys == ["m-five", "m-four", "__other__"]
    totals = {item["key"]: sum(p["total"] for p in item["points"]) for item in data["series"]}
    assert totals == {"m-five": 5, "m-four": 4, "__other__": 4}
    assert all(len(item["points"]) == 24 for item in data["series"])
    assert data["series"][-1]["label"] == "其他"

    wide = control.timeseries(CTX, range_value="24h", group="model", limit=12)
    assert [item["key"] for item in wide["series"]] == ["m-five", "m-four", "m-three", "m-one"]
    by_key = control.timeseries(CTX, range_value="24h", group="apiKey", limit=1)
    assert [item["key"] for item in by_key["series"]] == ["key-m-five", "__other__"]
    by_channel = control.timeseries(CTX, range_value="24h", group="channel", limit=6)
    assert by_channel["series"][0]["key"] == "api:m-five"
    assert _sum(by_channel["series"], "total") == 13


def test_tokens_and_cost_match_summary_rules(store):
    now = time.time()
    conn = log_db._get_conn()
    # Attempt-ledger requests: the ledger owns both tokens and cost.
    insert_request(conn, request_id="a1", created_at=now - 300, input_tokens=1, output_tokens=1)
    insert_attempt(conn, request_id="a1", retry_attempt_id=1, cost_source="actual",
                   cost_ticks=7_000, input_tokens=100, output_tokens=50)
    insert_request(conn, request_id="a2", created_at=now - 200, final_channel_key="api:beta")
    insert_attempt(conn, request_id="a2", retry_attempt_id=2, channel_key="api:beta",
                   cost_source="estimated", cost_ticks=3_000, input_tokens=10, output_tokens=5)
    insert_attempt(conn, request_id="a2", retry_attempt_id=3, channel_key="api:alpha",
                   cost_source="unpriced", cost_ticks=0, input_tokens=2, output_tokens=0)
    # Legacy request without ledger rows and an error.
    insert_request(conn, request_id="l1", created_at=now - 100, input_tokens=40, output_tokens=8)
    insert_request(conn, request_id="e1", created_at=now - 90, status="error")
    conn.commit()
    control = StatsControl(now=lambda: now)
    series = control.timeseries(CTX, range_value="24h")["series"]
    data = control.timeseries(CTX, range_value="24h")
    since = _iso(data["from"].isoformat()).timestamp()
    summary = log_db.stats_summary(since_ts=since, family=None, summary_top_limit=0, include_cost=True)["overall"]
    assert _sum(series, "total") == summary["total"] == 4
    assert _sum(series, "successCount") == summary["success_count"] == 3
    assert _sum(series, "errorCount") == summary["error_count"] == 1
    assert _sum(series, "inputTokens") == summary["total_input_tokens"]
    assert _sum(series, "outputTokens") == summary["total_output_tokens"]
    assert _sum(series, "costTicks") == summary["cost_ticks"]
    assert _sum(series, "costTicks") >= 10_000  # actual + estimated ledger ticks

    by_channel = {
        item["key"]: item
        for item in control.timeseries(CTX, range_value="24h", group="channel")["series"]
    }
    # Attempt cost is attributed to the attempt's channel, like by_channel summaries.
    assert sum(p["costTicks"] for p in by_channel["api:beta"]["points"]) == 3_000


def test_cross_month_reads_previous_month_database(store):
    now = time.time()
    start, _end, _size, _count = window(now, TimeseriesRange.THIRTY_DAYS, TimeseriesBucket.DAY)
    previous_last_hour = store.current_start.timestamp() - 3600
    if previous_last_hour < start:
        pytest.skip("30-day window does not reach the previous month today")
    old = store.month_conn(store.previous_month_start())
    insert_request(old, request_id="old-1", created_at=previous_last_hour, output_tokens=9)
    old.commit()
    old.close()
    current = log_db._get_conn()
    insert_request(current, request_id="new-1", created_at=now - 60)
    current.commit()
    data = StatsControl(now=lambda: now).timeseries(CTX, range_value="30d")
    points = data["series"][0]["points"]
    assert sum(p["total"] for p in points) == 2
    old_point = next(p for p in points if p["outputTokens"] == 9)
    assert old_point["t"].timestamp() <= previous_last_hour < old_point["t"].timestamp() + 86400


def test_timeseries_api_contract(store, tmp_path):
    now = time.time()
    conn = log_db._get_conn()
    insert_request(conn, request_id="api-1", created_at=now - 30)
    conn.commit()
    app, _runtime, _ = build_app(tmp_path)
    with TestClient(app) as client:
        headers = bearer(key_session(client).json()["data"]["credential"])
        ok = client.get(f"{API}/stats/timeseries?range=1h&groupBy=model&limit=3", headers=headers)
        assert ok.status_code == 200, ok.text
        data = ok.json()["data"]
        assert set(data) == {
            "range", "bucket", "bucketSeconds", "from", "to", "timezone", "series", "revision",
        }
        assert data["from"].endswith("Z") and data["to"].endswith("Z")
        point = data["series"][0]["points"][0]
        assert set(point) == {
            "t", "total", "successCount", "errorCount", "inputTokens", "outputTokens",
            "cacheReadTokens", "cacheCreationTokens", "costTicks",
            "averageFirstTokenMilliseconds", "averageTotalMilliseconds",
        }
        for query, field in (
            ("range=30d&bucket=minute", "bucket"),
            ("", "range"),
            ("range=2h", "range"),
            ("range=1h&groupBy=user", "groupBy"),
            ("range=1h&limit=13", "limit"),
            ("range=1h&extra=1", "extra"),
        ):
            bad = client.get(f"{API}/stats/timeseries?{query}", headers=headers)
            assert bad.status_code == 422, query
            assert bad.json()["error"]["code"] == "VALIDATION_FAILED"
            assert field in {item["path"].split(".")[-1] for item in bad.json()["error"]["fields"]}
        assert client.get(f"{API}/stats/timeseries?range=1h").status_code == 401


def test_thirty_day_production_volume_is_fast(store):
    now = time.time()
    conn = log_db._get_conn()
    span = max(3600.0, now - store.current_start.timestamp())
    rows = 80_000
    models = [f"model-{index}" for index in range(15)]
    channels = [f"api:channel-{index}" for index in range(10)]
    with conn:
        for index in range(rows):
            created = now - (index / rows) * span
            request_id = f"perf-{index}"
            model = models[index % len(models)]
            channel = channels[index % len(channels)]
            insert_request(
                conn, request_id=request_id, created_at=created, requested_model=model,
                final_channel_key=channel, api_key_name=f"key-{index % 7}",
                status="error" if index % 20 == 0 else "success",
                input_tokens=100, output_tokens=50, first_token_time_ms=300,
                total_time_ms=900,
            )
            if index % 2 == 0:
                insert_attempt(conn, request_id=request_id, retry_attempt_id=index + 1,
                               channel_key=channel, model=model, cost_source="actual",
                               cost_ticks=1000, input_tokens=100, output_tokens=50)
    control = StatsControl(now=lambda: now)
    timings = {}
    for group in ("none", "model"):
        started = time.perf_counter()
        data = control.timeseries(CTX, range_value="30d", group=group)
        timings[group] = time.perf_counter() - started
        assert _sum(data["series"], "total") == rows
    print(f"timeseries 30d timings: {timings}")
    # The contract target is < 1 s; allow headroom for loaded parallel CI workers.
    assert max(timings.values()) < 2.0, timings
