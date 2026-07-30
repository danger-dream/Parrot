"""No-network/temp-SQLite contracts for month-bound log handles and stats.

Execution is deferred until the import-before-src isolation bootstrap is active.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from src import log_db, scorer
from src.telegram.menus import proxy_menu, system_menu


_BJT = timezone(timedelta(hours=8))


@pytest.fixture
def isolated_log_db(tmp_path, monkeypatch):
    local = threading.local()
    monkeypatch.setattr(log_db, "_log_dir", str(tmp_path))
    monkeypatch.setattr(log_db, "_local", local)
    log_db._request_handles.clear()
    yield tmp_path
    for conn in getattr(local, "write_conns", {}).values():
        conn.close()
    log_db._request_handles.clear()


def _insert(request_id: str, created_at: float):
    return log_db.insert_pending(
        request_id,
        "127.0.0.1",
        "test-key",
        "model",
        True,
        1,
        0,
        {},
        {"model": "model"},
        created_at=created_at,
    )


def _row(path, table: str, row_id: int):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return dict(conn.execute(f"SELECT * FROM {table} WHERE id=?", (row_id,)).fetchone())
    finally:
        conn.close()


def _request(path, request_id: str):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return dict(conn.execute("SELECT * FROM request_log WHERE request_id=?", (request_id,)).fetchone())
    finally:
        conn.close()


def test_request_retry_and_round_handles_survive_month_rollover_and_id_collision(isolated_log_db):
    jan_ts = datetime(2026, 1, 31, 23, 59, 59, tzinfo=_BJT).timestamp()
    feb_ts = datetime(2026, 2, 1, 0, 0, 1, tzinfo=_BJT).timestamp()
    jan = _insert("jan-request", jan_ts)
    feb = _insert("feb-request", feb_ts)

    jan_retry = log_db.record_retry_attempt(jan, 1, "api:jan", "api", "m", jan_ts)
    feb_retry = log_db.record_retry_attempt(feb, 1, "api:feb", "api", "m", feb_ts)
    assert jan_retry.row_id == feb_retry.row_id == 1
    assert jan_retry.db != feb_retry.db

    jan_round = log_db.record_proxy_attempt(
        jan, jan_retry, 1, "direct", jan_ts,
        round_id="round-jan", transport="http", request_mode="http_stream",
    )
    feb_round = log_db.record_proxy_attempt(
        feb, feb_retry, 1, "direct", feb_ts,
        round_id="round-feb", transport="ws", request_mode="ws",
    )
    assert jan_round.row_id == feb_round.row_id == 1

    log_db.update_proxy_attempt(
        jan_round, connect_ms=10, first_byte_ms=20, idle_ms=3, total_ms=40,
        ended_at=jan_ts + 1, outcome="success",
    )
    log_db.update_proxy_attempt(
        feb_round, connect_ms=110, first_byte_ms=120, idle_ms=13, total_ms=140,
        ended_at=feb_ts + 1, outcome="idle_timeout",
    )
    log_db.update_retry_attempt(
        jan_retry, final_round_id="round-jan", connect_ms=10, first_byte_ms=20,
        idle_ms=3, total_ms=40, attempt_elapsed_ms=55,
        ended_at=jan_ts + 1, outcome="success",
    )
    log_db.update_retry_attempt(
        feb_retry, final_round_id="round-feb", connect_ms=110, first_byte_ms=120,
        idle_ms=13, total_ms=140, attempt_elapsed_ms=155,
        ended_at=feb_ts + 1, outcome="idle_timeout",
    )
    log_db.finish_success(
        jan, "api:jan", "api", "m", connect_ms=10, first_token_ms=20,
        idle_ms=3, total_ms=40, final_round_id="round-jan", request_elapsed_ms=70,
    )
    log_db.finish_error(
        feb, "idle timeout", final_channel_key="api:feb", final_channel_type="api",
        final_model="m", connect_ms=110, first_token_ms=120, idle_ms=13,
        total_ms=140, final_round_id="round-feb", request_elapsed_ms=170,
    )

    jan_req = _request(jan.db.path, jan.request_id)
    feb_req = _request(feb.db.path, feb.request_id)
    assert (jan_req["status"], jan_req["connect_time_ms"], jan_req["final_round_id"]) == (
        "success", 10, "round-jan",
    )
    assert (feb_req["status"], feb_req["connect_time_ms"], feb_req["final_round_id"]) == (
        "error", 110, "round-feb",
    )
    assert _row(jan.db.path, "retry_chain", 1)["attempt_elapsed_ms"] == 55
    assert _row(feb.db.path, "retry_chain", 1)["attempt_elapsed_ms"] == 155
    assert _row(jan.db.path, "proxy_chain", 1)["round_id"] == "round-jan"
    assert _row(feb.db.path, "proxy_chain", 1)["round_id"] == "round-feb"


def test_intermediate_local_tool_success_rebinds_followup_round_to_original_month(
    isolated_log_db,
):
    jan_ts = datetime(2026, 1, 31, 23, 59, 59, tzinfo=_BJT).timestamp()
    request = _insert("local-loop", jan_ts)
    first = log_db.record_retry_attempt(
        request, 1, "api:channel", "api", "model", jan_ts,
    )
    body = '{"usage":{"input_tokens":10,"output_tokens":2}}'
    log_db.finish_success(
        request, "api:channel", "api", "model",
        input_tokens=10, output_tokens=2, response_body=body,
    )
    assert request.request_id not in log_db._request_handles

    rebound = log_db.retain_request_handle(request.request_id, first)
    second = log_db.record_retry_attempt(
        request.request_id, 2, "api:channel", "api", "model", jan_ts + 2,
    )
    assert rebound.db == request.db
    assert second.db == request.db
    assert _row(request.db.path, "retry_chain", second.row_id)["attempt_order"] == 2


def test_cleanup_stale_pending_preserves_known_client_disconnect_semantics(isolated_log_db):
    created = time.time() - 1900
    disconnected = _insert("stale-client-disconnect", created)
    retry = log_db.record_retry_attempt(
        disconnected, 1, "api:channel", "api", "model", created,
    )
    log_db.update_retry_attempt(
        retry,
        ended_at=created + 1,
        outcome="client_disconnected",
        error_detail="client disconnected",
    )

    proxy_only = _insert("stale-proxy-client-disconnect", created)
    open_retry = log_db.record_retry_attempt(
        proxy_only, 1, "api:channel", "api", "model", created,
    )
    proxy_round = log_db.record_proxy_attempt(
        proxy_only,
        open_retry,
        1,
        "direct",
        created,
        round_id="stale-proxy-round",
        transport="http",
        request_mode="http_stream",
    )
    log_db.update_proxy_attempt(
        proxy_round,
        ended_at=created + 1,
        outcome="client_disconnected",
        error_detail="client disconnected",
    )
    orphan = _insert("stale-orphan", created)

    assert log_db.cleanup_stale_pending(1800) == 3

    disconnected_row = _request(disconnected.db.path, disconnected.request_id)
    proxy_only_row = _request(proxy_only.db.path, proxy_only.request_id)
    orphan_row = _request(orphan.db.path, orphan.request_id)
    assert (
        disconnected_row["status"],
        disconnected_row["http_status"],
        disconnected_row["error_message"],
    ) == ("cancelled", 499, "client disconnected")
    assert (
        proxy_only_row["status"],
        proxy_only_row["http_status"],
        proxy_only_row["error_message"],
    ) == ("cancelled", 499, "client disconnected")
    assert orphan_row["status"] == "error"
    assert orphan_row["error_message"] == "process crashed (stale pending)"


def test_cleanup_stale_pending_scans_request_creation_month_after_rollover(
    isolated_log_db,
):
    old_ts = datetime(2026, 1, 31, 23, 59, 0, tzinfo=_BJT).timestamp()
    old = _insert("stale-old-month", old_ts)
    retry = log_db.record_retry_attempt(
        old, 1, "api:channel", "api", "model", old_ts,
    )
    log_db.update_retry_attempt(
        retry,
        ended_at=old_ts + 1,
        outcome="client_disconnected",
        error_detail="client disconnected",
    )

    assert log_db.cleanup_stale_pending(1800) == 1
    row = _request(old.db.path, old.request_id)
    assert (row["status"], row["http_status"], row["error_message"]) == (
        "cancelled", 499, "client disconnected",
    )
    assert old.request_id not in log_db._request_handles


def test_historical_open_is_read_only_and_migration_is_explicit(isolated_log_db):
    path = isolated_log_db / "2024-01.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE request_log (
             id INTEGER PRIMARY KEY AUTOINCREMENT,
             request_id TEXT UNIQUE NOT NULL,
             created_at REAL NOT NULL,
             status TEXT,
             api_key_name TEXT,
             requested_model TEXT,
             final_channel_key TEXT
           )"""
    )
    conn.commit()
    before = {r[1] for r in conn.execute("PRAGMA table_info(request_log)").fetchall()}
    conn.close()

    ro = log_db._get_conn_for_month("2024-01")
    assert ro is not None
    assert ro.execute("PRAGMA query_only").fetchone()[0] == 1
    projected = log_db._compatible_recent_cols(ro)
    assert "NULL AS idle_time_ms" in projected
    assert "NULL AS final_round_id" in projected
    assert "0 AS local_web_count" in projected
    ro.close()

    check = sqlite3.connect(path)
    after_read = {r[1] for r in check.execute("PRAGMA table_info(request_log)").fetchall()}
    check.close()
    assert after_read == before

    log_db.migrate_month_schema("2024-01")
    log_db.migrate_month_schema("2024-01")  # idempotent
    migrated = sqlite3.connect(path)
    cols = {r[1] for r in migrated.execute("PRAGMA table_info(request_log)").fetchall()}
    migrated.close()
    assert {"idle_time_ms", "final_round_id", "request_elapsed_ms"} <= cols


def test_incompatible_historical_stats_raise_instead_of_silent_month_skip(isolated_log_db):
    path = isolated_log_db / "2024-02.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE request_log (request_id TEXT, created_at REAL)")
    conn.commit()
    conn.close()
    since = datetime(2024, 2, 1, tzinfo=_BJT).timestamp()
    with pytest.raises(log_db.HistoricalLogError):
        log_db.proxy_stats(limit=10, since_ts=since)


def test_proxy_stats_expose_metric_specific_sums_and_counts(isolated_log_db):
    now = time.time()
    h1 = _insert("p1-null-connect", now)
    log_db.finish_success(h1, "api:a", "api", "m", connect_ms=None, first_token_ms=4,
                          idle_ms=10, total_ms=100, proxy_name="p1")
    h2 = _insert("p1-real-connect", now + 0.1)
    log_db.finish_success(h2, "api:a", "api", "m", connect_ms=20, first_token_ms=None,
                          total_ms=300, proxy_name="p1")
    h3 = _insert("p2-real-connect", now + 0.2)
    log_db.finish_success(h3, "api:b", "api", "m", connect_ms=100, first_token_ms=40,
                          idle_ms=50, total_ms=500, proxy_name="p2")

    rows = {row["proxy_name"]: row for row in log_db.proxy_stats(limit=10, since_ts=now - 1)}
    p1 = rows["p1"]
    assert p1["requests"] == 2
    assert (p1["connect_sum_ms"], p1["connect_sample_count"], p1["avg_connect_ms"]) == (20, 1, 20)
    assert (p1["first_byte_sum_ms"], p1["first_byte_sample_count"], p1["avg_first_byte_ms"]) == (4, 1, 4)
    assert (p1["idle_sum_ms"], p1["idle_sample_count"], p1["avg_idle_ms"]) == (10, 1, 10)
    assert (p1["total_sum_ms"], p1["total_sample_count"], p1["avg_total_ms"]) == (400, 2, 200)

    merged = proxy_menu._merge_group_stats(["p1", "p2"], rows)
    assert merged["requests"] == 3
    assert merged["connect_sample_count"] == 2
    assert merged["avg_connect_ms"] == 60  # (20 + 100) / 2, not weighted by 3 requests
    assert merged["first_byte_sample_count"] == 2
    assert merged["avg_first_byte_ms"] == 22
    assert merged["idle_sample_count"] == 2
    assert merged["avg_idle_ms"] == 30
    assert merged["total_sample_count"] == 3
    assert merged["avg_total_ms"] == 300

    system_merged = system_menu._merge_proxy_stats_for_system(["p1", "p2"], rows)
    for key in (
        "connect_sample_count", "avg_connect_ms",
        "first_byte_sample_count", "avg_first_byte_ms",
        "idle_sample_count", "avg_idle_ms",
        "total_sample_count", "avg_total_ms",
    ):
        assert system_merged[key] == merged[key]


def test_scorer_legacy_connect_ema_is_neutralized_and_migration_requires_apply(monkeypatch):
    neutral = 3000.0
    row = {"avg_connect_ms": 12.0}
    assert scorer._loaded_connect_ema(row, stored_version=None, neutral=neutral) == neutral
    assert scorer._loaded_connect_ema(
        row, stored_version=scorer.TIMING_SEMANTICS_VERSION, neutral=neutral,
    ) == 12.0

    saved = []
    meta = {}
    monkeypatch.setattr(scorer, "_stats", {})
    monkeypatch.setattr(scorer, "_loaded_timing_semantics_version", None)
    rows = [{
        "channel_key": "api:a", "model": "m",
        "total_requests": 9, "success_count": 8,
        "avg_connect_ms": 12.0, "avg_first_byte_ms": 34.0,
        "avg_total_ms": 56.0, "last_updated": 1,
    }]
    monkeypatch.setattr(scorer.state_db, "perf_load_all", lambda: list(rows))
    monkeypatch.setattr(scorer.state_db, "schema_meta_get", lambda key: meta.get(key))
    monkeypatch.setattr(scorer.state_db, "perf_save", lambda ck, model, stats: saved.append((ck, model, dict(stats))))
    monkeypatch.setattr(scorer.state_db, "schema_meta_set", lambda key, value: meta.__setitem__(key, value))
    monkeypatch.setattr(scorer, "_params", lambda: {
        "defaultScore": neutral, "recentWindow": 50, "emaAlpha": 0.25,
        "errorPenaltyFactor": 8, "staleMinutes": 15,
        "staleFullDecayMinutes": 30, "explorationRate": 0.2,
    })

    preview = scorer.timing_semantics_migration(apply=False)
    assert preview == {
        "current_version": None,
        "target_version": scorer.TIMING_SEMANTICS_VERSION,
        "rows": 1,
        "applied": False,
    }
    assert saved == [] and meta == {}

    applied = scorer.timing_semantics_migration(apply=True)
    assert applied["applied"] is True
    assert saved[0][2]["avg_connect_ms"] == neutral
    assert meta[scorer.TIMING_SEMANTICS_META_KEY] == scorer.TIMING_SEMANTICS_VERSION
