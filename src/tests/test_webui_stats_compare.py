"""GET /stats/compare: comparison windows, combined filters, retention coverage, summary parity."""

from __future__ import annotations

import time
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from src import log_db
from src.management_control.errors import ManagementError
from src.management_control.observability.common import telegram_context
from src.management_control.observability.compare import compare_windows
from src.management_control.observability.stats import StatsControl
from src.tests.webui_support import (
    API, MonthlyLogStore, bearer, build_app, insert_attempt, insert_request, key_session,
)


CTX = telegram_context("compare-test")
BJT = log_db._BJT


@pytest.fixture
def store(monkeypatch, tmp_path):
    value = MonthlyLogStore(monkeypatch, tmp_path)
    yield value
    value.close()


def _ts(*args) -> float:
    return datetime(*args, tzinfo=BJT).timestamp()


def test_windows_cover_the_same_elapsed_time():
    now = _ts(2026, 3, 31, 10, 30)
    # Today: yesterday from midnight up to the same clock time.
    assert compare_windows(now, period="today", start=None, end=None) == (
        _ts(2026, 3, 31), now, _ts(2026, 3, 30), _ts(2026, 3, 30, 10, 30),
    )
    # Rolling ranges: the range right before.
    assert compare_windows(now, period="7d", start=None, end=None) == (
        now - 7 * 86400, now, now - 14 * 86400, now - 7 * 86400,
    )
    # Month: last month up to the same point, clipped at this month's start (February is shorter).
    start, end, previous_start, previous_end = compare_windows(now, period="month", start=None, end=None)
    assert (start, end, previous_start, previous_end) == (_ts(2026, 3, 1), now, _ts(2026, 2, 1), _ts(2026, 3, 1))
    mid = _ts(2026, 3, 10, 12)
    assert compare_windows(mid, period="month", start=None, end=None)[2:] == (_ts(2026, 2, 1), _ts(2026, 2, 10, 12))
    # Custom: the equal-length range before; a future end is clipped to now.
    assert compare_windows(now, period=None, start=now - 3600, end=now + 999) == (now - 3600, now, now - 7200, now - 3600)


@pytest.mark.parametrize("kwargs, field", [
    ({"period": "lifetime"}, "period"),
    ({"period": "today", "start": 1.0}, "period"),
    ({"period": None, "start": 1.0}, "from"),
    ({"period": None, "start": 100.0, "end": 50.0}, "to"),
])
def test_window_validation(kwargs, field):
    args = {"period": None, "start": None, "end": None, **kwargs}
    with pytest.raises(ManagementError) as caught:
        compare_windows(1_000_000.0, **args)
    assert caught.value.fields[0].path == field


def test_current_previous_and_combined_filters(store):
    now = time.time()
    day_start = datetime.fromtimestamp(now, BJT).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    elapsed = now - day_start
    conn = log_db._get_conn()
    rows = [
        # today
        ("t1", day_start + elapsed / 2, "model-a", "api:alpha", "success", 100),
        ("t2", day_start + elapsed / 2, "model-a", "api:beta", "success", 0),
        ("t3", day_start + elapsed / 3, "model-b", "api:alpha", "error", 0),
        # yesterday before the same clock time
        ("y1", day_start - 86400 + elapsed / 2, "model-a", "api:alpha", "success", 0),
        # yesterday after the same clock time: outside the previous window
        ("y2", now - 86400 + 1, "model-a", "api:alpha", "success", 0),
    ]
    for request_id, created, model, channel, status, cache_read in rows:
        insert_request(conn, request_id=request_id, created_at=created, requested_model=model,
                       final_channel_key=channel, status=status, input_tokens=10, output_tokens=5,
                       cache_read_tokens=cache_read)
    conn.commit()
    control = StatsControl(now=lambda: now)
    data = control.compare(CTX, period="today")
    assert (data["current"]["total"], data["current"]["errorCount"]) == (3, 1)
    assert data["current"]["cacheHitRequests"] == 1
    assert data["previous"]["total"] == 1
    assert sum(p["total"] for p in data["points"]) == 3
    both = control.compare(CTX, period="today", filters={"model": "model-a", "channel": "api:alpha"})
    assert (both["current"]["total"], both["previous"]["total"]) == (1, 1)
    model_only = control.compare(CTX, period="today", filters={"model": "model-a"})
    assert model_only["current"]["total"] == 2
    none = control.compare(CTX, period="today", filters={"model": "model-a", "apiKey": "other"})
    assert none["current"]["total"] == 0


def test_previous_window_before_the_oldest_log_is_not_complete(store):
    now = time.time()
    conn = log_db._get_conn()
    insert_request(conn, request_id="only", created_at=now - 3600)
    conn.commit()
    control = StatsControl(now=lambda: now)
    covered = control.compare(CTX, period=None, start=now - 1800, end=now)
    assert covered["previousComplete"] is True
    uncovered = control.compare(CTX, period=None, start=now - 2 * 3600, end=now)
    assert uncovered["previousComplete"] is False
    assert uncovered["current"]["total"] == 1


def test_matches_summary_for_the_same_window(store):
    now = time.time()
    conn = log_db._get_conn()
    day_start = datetime.fromtimestamp(now, BJT).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    for index in range(12):
        created = day_start + (now - day_start) * (index + 0.5) / 12
        request_id = f"s-{index}"
        insert_request(conn, request_id=request_id, created_at=created, requested_model=f"model-{index % 3}",
                       final_channel_key=f"api:channel-{index % 2}", status="error" if index == 5 else "success",
                       input_tokens=100, output_tokens=40, cache_read_tokens=10 * index,
                       first_token_time_ms=200, total_time_ms=1200 + 100 * index, affinity_hit=index % 3 == 0)
        if index % 2 == 0:
            insert_attempt(conn, request_id=request_id, retry_attempt_id=index + 1, channel_key=f"api:channel-{index % 2}",
                           model=f"model-{index % 3}", cost_source="actual", cost_ticks=1000 + index,
                           input_tokens=100, output_tokens=40)
    conn.commit()
    control = StatsControl(now=lambda: now)
    overall = control.summary(CTX, "today")["overall"]
    current = control.compare(CTX, period="today")["current"]
    for name in ("total", "successCount", "errorCount", "inputTokens", "outputTokens", "cacheReadTokens",
                 "costTicks", "unpricedSuccess", "cacheHitRequests", "affinityHits"):
        assert current[name] == overall[name], name
    assert current["averageTokensPerSecond"] == pytest.approx(overall["averageTokensPerSecond"])


def test_compare_api_contract(store, tmp_path):
    now = time.time()
    conn = log_db._get_conn()
    insert_request(conn, request_id="api-1", created_at=now - 30)
    conn.commit()
    app, _runtime, _ = build_app(tmp_path)
    with TestClient(app) as client:
        headers = bearer(key_session(client).json()["data"]["credential"])
        ok = client.get(f"{API}/stats/compare?period=today&model=model-a", headers=headers)
        assert ok.status_code == 200, ok.text
        data = ok.json()["data"]
        assert set(data) == {
            "from", "to", "previousFrom", "previousTo", "previousComplete", "bucketSeconds",
            "current", "previous", "points", "revision",
        }
        assert data["current"]["total"] == 1
        custom = client.get(
            f"{API}/stats/compare",
            params={"from": (datetime.now(BJT) - timedelta(hours=1)).isoformat(), "to": datetime.now(BJT).isoformat()},
            headers=headers,
        )
        assert custom.status_code == 200, custom.text
        for query, field in (("period=lifetime", "period"), ("", "from"), ("period=today&extra=1", "extra")):
            bad = client.get(f"{API}/stats/compare?{query}", headers=headers)
            assert bad.status_code == 422, query
            assert field in {item["path"].split(".")[-1] for item in bad.json()["error"]["fields"]}
        assert client.get(f"{API}/stats/compare?period=today").status_code == 401


def test_hour_cache_composes_the_same_totals_as_one_direct_query(store):
    from src import log_timeseries

    now = time.time()
    conn = log_db._get_conn()
    for index in range(80):
        created = now - 30 * 3600 + index * 1300 + (index % 7) * 61
        insert_request(conn, request_id=f"h-{index}", created_at=created, requested_model=f"model-{index % 3}",
                       final_channel_key=f"api:channel-{index % 2}", status="error" if index % 9 == 0 else "success",
                       input_tokens=100 + index, output_tokens=40, cache_read_tokens=5 * index)
    conn.commit()
    control = StatsControl(now=lambda: now)
    start, end = now - 29 * 3600 - 777, now - 2 * 3600 - 99
    direct = log_timeseries.bucketed_stats(start=start, bucket_seconds=int(end - start) + 1, count=1, group=("model",),
                                           end=end)
    expected = {key: point for (_index, key), point in direct.items()}["model-1"]
    for _ in range(2):  # the second pass reads the cached closed hours
        current = control.compare(CTX, start=start, end=end, filters={"model": "model-1"})["current"]
        assert (current["total"], current["errorCount"], current["inputTokens"], current["cacheReadTokens"]) == (
            expected["total"], expected["error_count"], expected["input_tokens"], expected["cache_read_tokens"],
        )
    assert control._compare_hours._hours
