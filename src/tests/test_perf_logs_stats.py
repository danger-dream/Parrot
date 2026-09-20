from __future__ import annotations

import sqlite3
import threading
from datetime import datetime

import pytest

from src import log_db


def _memory_log_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(log_db._schema_sql())
    return conn


def _insert_request(
    conn: sqlite3.Connection,
    request_id: str,
    created_at: float,
    *,
    status: str = "success",
    api_key: str = "key-a",
    requested_model: str = "model-a",
    final_model: str | None = None,
    channel: str = "api:a",
    protocol: str = "chat",
    total_ms: int = 0,
    error: str | None = None,
) -> None:
    conn.execute(
        """INSERT INTO request_log(
               request_id, created_at, status, api_key_name, requested_model,
               final_model, final_channel_key, ingress_protocol, total_time_ms,
               error_message, input_tokens, output_tokens, usage_observed,
               actual_service_tier)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            request_id, created_at, status, api_key, requested_model,
            final_model, channel, protocol, total_ms, error, 10, 2, 1, "standard",
        ),
    )


def _reference_ids(rows: list[dict], *, sort: str, descending: bool) -> list[str]:
    # The pre-PERF-05 selector was stable over recent_logs(created_at DESC).
    ordered = sorted(rows, key=lambda row: (row["created_at"], row["id"]), reverse=True)
    if sort == "status":
        key = lambda row: str(row.get("status") or "")
    elif sort == "latency":
        key = lambda row: float(row.get("total_time_ms") or 0)
    elif sort == "model":
        key = lambda row: str(row.get("requested_model") or row.get("final_model") or "").casefold()
    else:
        key = lambda row: float(row.get("created_at") or 0)
    ordered.sort(key=key, reverse=descending)
    return [str(row["request_id"]) for row in ordered]


@pytest.mark.parametrize("sort", ["createdAt", "status", "latency", "model"])
@pytest.mark.parametrize("descending", [False, True])
def test_management_sql_page_matches_reference_sort_ties_and_random_pages(
    monkeypatch, sort, descending,
):
    conn = _memory_log_db()
    values = [
        ("r1", 100.0, "success", "key-a", "Straße", None, "api:a", "chat", 30, None),
        ("r2", 100.0, "error", "key-b", "STRASSE", None, "api:b", "responses", 30, "Needle"),
        ("r3", 101.0, "success", "key-a", "alpha", "omega", "api:a", "chat", 10, None),
        ("r4", 102.0, "cancelled", "key-c", "beta", None, "api:c", "anthropic", 40, "needle"),
        ("r5", 103.0, "success", "key-a", "alpha", None, "api:a", "chat", 10, None),
        ("r6", 104.0, "error", "key-a", "gamma", None, "api:a", "chat", 20, "other"),
    ]
    for value in values:
        _insert_request(
            conn, value[0], value[1], status=value[2], api_key=value[3],
            requested_model=value[4], final_model=value[5], channel=value[6],
            protocol=value[7], total_ms=value[8], error=value[9],
        )
    conn.commit()
    monkeypatch.setattr(log_db, "_get_conn", lambda: conn)

    raw = [dict(row) for row in conn.execute("SELECT * FROM request_log")]
    expected = _reference_ids(raw, sort=sort, descending=descending)
    actual: list[str] = []
    for page in (1, 2, 3):
        rows, total = log_db.management_logs_page(
            sort=sort, descending=descending, page=page, page_size=2,
        )
        assert total == len(expected)
        actual.extend(str(row["request_id"]) for row in rows)
    assert actual == expected

    filtered, total = log_db.management_logs_page(
        statuses=["success", "error"], api_keys=["key-a"],
        channel_keys=["api:a"], protocols=["chat"],
        started_at=101.0, ended_at=104.0,
        sort=sort, descending=descending, page=1, page_size=20,
    )
    expected_filtered = [
        row for row in raw
        if row["status"] in {"success", "error"}
        and row["api_key_name"] == "key-a"
        and row["final_channel_key"] == "api:a"
        and row["ingress_protocol"] == "chat"
        and 101.0 <= row["created_at"] <= 104.0
    ]
    assert total == len(expected_filtered)
    assert [row["request_id"] for row in filtered] == _reference_ids(
        expected_filtered, sort=sort, descending=descending,
    )


class _CursorProbe:
    def __init__(self, cursor: sqlite3.Cursor, fetched: list[int]):
        self._cursor = cursor
        self._fetched = fetched

    def fetchmany(self, size: int):
        rows = self._cursor.fetchmany(size)
        self._fetched.append(len(rows))
        return rows

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class _ConnectionProbe:
    def __init__(self, conn: sqlite3.Connection, fetched: list[int]):
        self._conn = conn
        self._fetched = fetched

    def execute(self, sql, parameters=()):
        cursor = self._conn.execute(sql, parameters)
        if " ORDER BY " in sql and "FROM request_log" in sql and "LIMIT" not in sql:
            return _CursorProbe(cursor, self._fetched)
        return cursor

    def create_collation(self, *args):
        return self._conn.create_collation(*args)


def test_log_detail_without_detail_row_preserves_response_body_none(monkeypatch):
    conn = _memory_log_db()
    _insert_request(conn, "missing-detail", 1.0)
    conn.commit()
    monkeypatch.setattr(log_db, "_get_conn", lambda: conn)

    detail = log_db.log_detail("missing-detail")

    assert detail["detail"] is None
    assert "response_body" in detail["log"]
    assert detail["log"]["response_body"] is None
    conn.close()


def test_management_text_search_is_one_bounded_stream_after_sql_filters(monkeypatch):
    conn = _memory_log_db()
    for index in range(1000):
        _insert_request(
            conn, f"r{index:04d}", float(index),
            status="success" if index % 2 else "error",
            api_key="target" if index % 5 == 0 else "other",
            requested_model="needle-model" if index % 60 == 15 else "model",
            protocol="chat" if index % 3 == 0 else "anthropic",
            total_ms=index % 17,
        )
    conn.commit()
    fetched: list[int] = []
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    monkeypatch.setattr(log_db, "_MANAGEMENT_TEXT_BATCH_SIZE", 17)
    monkeypatch.setattr(log_db, "_get_conn", lambda: _ConnectionProbe(conn, fetched))

    rows, total = log_db.management_logs_page(
        statuses=["success"], api_keys=["target"], protocols=["chat"],
        query="NEEDLE", sort="latency", descending=True, page=2, page_size=7,
    )

    assert total == 17
    assert len(rows) == 7
    assert fetched and max(fetched) <= 17
    row_selects = [sql for sql in statements if "FROM request_log" in sql and "ORDER BY" in sql]
    assert len(row_selects) == 1
    assert "status IN" in row_selects[0]
    assert "api_key_name IN" in row_selects[0]
    assert "ingress_protocol IN" in row_selects[0]
    assert "OFFSET" not in row_selects[0]


def test_structured_filters_and_filter_options_are_sql_aggregated(monkeypatch):
    conn = _memory_log_db()
    for index in range(1000):
        _insert_request(
            conn, f"r{index:04d}", float(index),
            status="success" if index % 3 else "error",
            api_key=f"key-{index % 5}",
            requested_model=f"model-{index % 7}",
            final_model=(f"model-{index % 7}" if index % 2 else f"final-{index % 4}"),
            channel=f"api:{index % 4}", protocol="chat" if index % 2 else "anthropic",
        )
    conn.commit()
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    monkeypatch.setattr(log_db, "_get_conn", lambda: conn)
    monkeypatch.setattr(
        log_db, "_management_text_matches",
        lambda *_args: (_ for _ in ()).throw(AssertionError("Python scan used")),
    )

    rows, total = log_db.management_logs_page(
        statuses=["success", "error"], protocols=["chat"],
        started_at=100.0, ended_at=900.0, sort="status",
        descending=False, page=2, page_size=50,
    )
    assert total == 400
    assert len(rows) == 50
    page_sql = [sql for sql in statements if "FROM request_log" in sql and "ORDER BY" in sql]
    assert len(page_sql) == 1
    assert "LIMIT 50 OFFSET 50" in page_sql[0]
    request_selects = [sql for sql in statements if "FROM request_log" in sql]
    assert len(request_selects) == 2  # exact count + one final random-page query

    statements.clear()
    options = log_db.management_log_filter_options()
    option_selects = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]
    assert len(option_selects) == 5
    assert all("GROUP BY" in sql for sql in option_selects)
    assert sum(item["count"] for item in options["statuses"]) == 1000
    assert {item["value"] for item in options["apiKeys"]} == {
        f"key-{index}" for index in range(5)
    }
    assert sum(len(options[field]) for field in options) == 22


def test_page_costs_match_single_row_reference_and_remove_n_plus_one(monkeypatch, tmp_path):
    conn = _memory_log_db()
    now = datetime.now(log_db._BJT).timestamp()
    rows: list[dict] = []
    for index in range(50):
        request_id = f"cost-{index:02d}"
        _insert_request(conn, request_id, now + index / 1000, requested_model="priced")
        source = ("actual", "estimated", "unpriced")[index % 3]
        conn.execute(
            """INSERT INTO upstream_attempt_usage(
                   retry_attempt_id, root_request_id, call_request_id, attempt_order,
                   channel_key, channel_type, model, outcome, usage_observed,
                   cost_source, cost_ticks, settled_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                index + 1, request_id, request_id, 1, "api:a", "api", "priced",
                "success", 1, source, (index + 1) * 100 if source != "unpriced" else None,
                now,
            ),
        )
    conn.commit()
    rows = [dict(row) for row in conn.execute("SELECT * FROM request_log ORDER BY id")]
    monkeypatch.setattr(log_db, "_log_dir", str(tmp_path))
    monkeypatch.setattr(log_db, "_get_conn", lambda: conn)

    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    reference = {row["request_id"]: log_db.cost_for_log(row) for row in rows}
    before = len([sql for sql in statements if sql.lstrip().upper().startswith(("SELECT", "PRAGMA"))])
    before_attempt_selects = len([
        sql for sql in statements
        if "FROM upstream_attempt_usage" in sql and "root_request_id=" in sql
    ])

    statements.clear()
    batched = log_db.costs_for_logs(rows)
    after = len([sql for sql in statements if sql.lstrip().upper().startswith(("SELECT", "PRAGMA"))])
    after_attempt_selects = len([
        sql for sql in statements
        if "FROM upstream_attempt_usage" in sql and "root_request_id IN" in sql
    ])

    assert batched == reference
    assert before_attempt_selects == 50
    assert after_attempt_selects == 1
    assert before == 300
    assert after == 6


def test_cost_overflow_is_exact_for_batch_stats_groups_and_lifetime(
    monkeypatch, tmp_path,
):
    max_int64 = (1 << 63) - 1
    expected_ticks = 18_446_744_073_709_551_614
    current_month = datetime.now(log_db._BJT).strftime("%Y-%m")
    conn = _file_log_db(tmp_path / f"{current_month}.db")
    now = datetime.now(log_db._BJT).timestamp()
    _insert_request(
        conn, "overflow-cost", now, requested_model="priced",
        final_model="priced", channel="api:a", protocol="chat",
    )
    for retry_id in (1, 2):
        conn.execute(
            """INSERT INTO upstream_attempt_usage(
                   retry_attempt_id, root_request_id, call_request_id, attempt_order,
                   channel_key, channel_type, model, outcome, usage_observed,
                   service_tier, upstream_protocol, cost_source, cost_ticks, settled_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                retry_id, "overflow-cost", "overflow-cost", retry_id,
                "api:a", "api", "priced", "success", 1,
                "standard", "openai-chat", "actual", max_int64, now,
            ),
        )
    conn.commit()
    request_row = dict(conn.execute(
        "SELECT * FROM request_log WHERE request_id='overflow-cost'"
    ).fetchone())

    monkeypatch.setattr(log_db, "_log_dir", str(tmp_path))
    monkeypatch.setattr(log_db, "_get_conn", lambda: conn)
    monkeypatch.setattr(
        log_db, "_iter_month_conns", lambda _since: [(conn, lambda: None)],
    )
    monkeypatch.setattr(
        log_db, "_iter_month_conns_all", lambda _since: [(conn, lambda: None)],
    )
    monkeypatch.setattr(
        log_db.model_pricing, "settings",
        lambda *args, **kwargs: type("Settings", (), {"enabled": True})(),
    )
    monkeypatch.setattr(log_db, "_lifetime_pricing_signature", lambda: "stable")

    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        single = log_db.cost_for_log(request_row)
        batch = log_db.costs_for_logs([request_row])["overflow-cost"]
        grouped_summary = log_db.stats_summary(0)["overall"]
        overall_only_summary = log_db.stats_summary(
            0, summary_top_limit=0,
        )["overall"]
        model_stats = log_db.channel_model_stats("api:a", 0)[0]
        lifetime = log_db.stats_lifetime()
    finally:
        conn.set_trace_callback(None)
        conn.close()

    for result in (
        single, batch, grouped_summary, overall_only_summary, model_stats, lifetime,
    ):
        assert result["cost_ticks"] == expected_ticks
        assert result["actual_cost_ticks"] == expected_ticks
        assert result["costed_success"] == 2
        assert result["unpriced_success"] == 0
    assert any(
        "SUM(CASE WHEN" in sql.upper() and "COST_TICKS" in sql.upper()
        for sql in statements
    )
    assert any(log_db._EXACT_COST_TICKS_SUM_FUNCTION in sql for sql in statements)


def test_current_window_channel_stats_keep_retry_filter_as_outer_join(
    monkeypatch, tmp_path,
):
    """频道窗口不能按日期先扫全部 retry，否则会长期阻塞唯一统计线程。"""
    conn = _memory_log_db()
    now = datetime.now(log_db._BJT).timestamp()
    _insert_request(
        conn, "window-missing", now, requested_model="unknown",
        channel="oauth:claude:window-account",
    )
    conn.execute(
        """INSERT INTO retry_chain(
               request_id, attempt_order, channel_key, channel_type, model,
               started_at, dispatched_at)
           VALUES(?,?,?,?,?,?,?)""",
        (
            "window-missing", 1, "oauth:claude:window-account", "oauth",
            "unknown", now, now,
        ),
    )
    conn.commit()
    monkeypatch.setattr(log_db, "_log_dir", str(tmp_path))
    monkeypatch.setattr(
        log_db, "_iter_month_conns_all",
        lambda _since: [(conn, lambda: None)],
    )
    monkeypatch.setattr(
        log_db.model_pricing, "settings",
        lambda *args, **kwargs: type("Settings", (), {"enabled": True})(),
    )
    statements: list[str] = []
    conn.set_trace_callback(statements.append)

    result = log_db.tokens_for_channel(
        "oauth:claude:window-account", now - 3600,
    )

    missing_queries = [
        sql for sql in statements
        if "retry_chain r" in sql and "a.id IS NULL" in sql
    ]
    assert result["unpriced_success"] == 1
    assert len(missing_queries) == 1
    assert "FROM retry_chain r JOIN request_log" in missing_queries[0]
    assert "FROM request_log CROSS JOIN retry_chain" not in missing_queries[0]
    conn.close()


def test_batch_cost_preserves_missing_dispatch_and_legacy_fallback(monkeypatch, tmp_path):
    conn = _memory_log_db()
    now = datetime.now(log_db._BJT).timestamp()
    _insert_request(conn, "missing", now, requested_model="unknown")
    conn.execute(
        """INSERT INTO retry_chain(
               request_id, attempt_order, channel_key, channel_type, model,
               started_at, dispatched_at)
           VALUES(?,?,?,?,?,?,?)""",
        ("missing", 1, "api:a", "api", "unknown", now, now),
    )
    _insert_request(conn, "legacy-unpriced", now + 0.1, requested_model="unknown")
    conn.execute(
        "UPDATE request_log SET usage_observed=0 WHERE request_id='legacy-unpriced'"
    )
    _insert_request(
        conn, "legacy-xai", now + 0.2, status="error",
        requested_model="grok", channel="oauth:xai:account",
    )
    conn.commit()
    rows = [dict(row) for row in conn.execute(
        "SELECT * FROM request_log ORDER BY id"
    )]
    rows[-1]["response_body"] = '{"usage":{"cost_in_usd_ticks":123456}}'
    monkeypatch.setattr(log_db, "_log_dir", str(tmp_path))
    monkeypatch.setattr(log_db, "_get_conn", lambda: conn)
    monkeypatch.setattr(
        log_db.model_pricing, "settings",
        lambda *args, **kwargs: type("Settings", (), {"enabled": True})(),
    )

    reference = {row["request_id"]: log_db.cost_for_log(row) for row in rows}
    batched = log_db.costs_for_logs(rows)

    assert batched == reference
    assert batched["missing"]["unpriced_success"] == 1
    assert batched["legacy-unpriced"]["unpriced_success"] == 1
    assert batched["legacy-xai"]["actual_cost_ticks"] == 123456


def test_batch_cost_groups_request_ids_by_month(monkeypatch, tmp_path):
    current_month = datetime.now(log_db._BJT).strftime("%Y-%m")
    current = _file_log_db(tmp_path / f"{current_month}.db")
    historical = _file_log_db(tmp_path / "2000-01.db")
    current_ts = datetime.now(log_db._BJT).timestamp()
    historical_ts = datetime(2000, 1, 15, tzinfo=log_db._BJT).timestamp()
    rows: list[dict] = []
    for index, (conn, request_id, created_at) in enumerate((
        (current, "current-cost", current_ts),
        (historical, "historical-cost", historical_ts),
    )):
        _insert_request(conn, request_id, created_at, requested_model="priced")
        conn.execute(
            """INSERT INTO upstream_attempt_usage(
                   retry_attempt_id, root_request_id, call_request_id, attempt_order,
                   channel_key, channel_type, model, outcome, usage_observed,
                   cost_source, cost_ticks, settled_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                index + 1, request_id, request_id, 1, "api:a", "api", "priced",
                "success", 1, "actual", (index + 1) * 100, created_at,
            ),
        )
        conn.commit()
        rows.append(dict(conn.execute(
            "SELECT * FROM request_log WHERE request_id=?", (request_id,),
        ).fetchone()))
    historical.close()
    monkeypatch.setattr(log_db, "_log_dir", str(tmp_path))
    monkeypatch.setattr(log_db, "_get_conn", lambda: current)

    assert log_db.costs_for_logs(rows) == {
        "current-cost": {
            "cost_ticks": 100, "actual_cost_ticks": 100,
            "estimated_cost_ticks": 0, "actual_costed_success": 1,
            "estimated_costed_success": 0, "costed_success": 1,
            "unpriced_success": 0,
        },
        "historical-cost": {
            "cost_ticks": 200, "actual_cost_ticks": 200,
            "estimated_cost_ticks": 0, "actual_costed_success": 1,
            "estimated_costed_success": 0, "costed_success": 1,
            "unpriced_success": 0,
        },
    }
    current.close()


class _FetchAllProbeCursor:
    def __init__(self, cursor: sqlite3.Cursor, sql: str, loaded: list[tuple[str, int]]):
        self._cursor = cursor
        self._sql = sql
        self._loaded = loaded

    def fetchall(self):
        rows = self._cursor.fetchall()
        self._loaded.append((self._sql, len(rows)))
        return rows

    def __iter__(self):
        return iter(self._cursor)

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class _FetchAllProbeConnection:
    def __init__(self, conn: sqlite3.Connection, loaded: list[tuple[str, int]]):
        self._conn = conn
        self._loaded = loaded

    def execute(self, sql, parameters=()):
        return _FetchAllProbeCursor(
            self._conn.execute(sql, parameters), sql, self._loaded,
        )


def test_lifetime_attempt_costs_are_sql_grouped_to_three_result_rows(monkeypatch):
    conn = _memory_log_db()
    for index in range(600):
        request_id = f"attempt-{index:04d}"
        _insert_request(conn, request_id, float(index), requested_model="priced")
        source = ("actual", "estimated", "unpriced")[index % 3]
        conn.execute(
            """INSERT INTO upstream_attempt_usage(
                   retry_attempt_id, root_request_id, call_request_id, attempt_order,
                   channel_key, channel_type, model, outcome, usage_observed,
                   input_tokens, output_tokens, cost_source, cost_ticks, settled_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                index + 1, request_id, request_id, 1, "api:a", "api", "priced",
                "success", 1, 10, 2, source,
                index + 1 if source != "unpriced" else None, float(index),
            ),
        )
    conn.commit()
    loaded: list[tuple[str, int]] = []
    monkeypatch.setattr(
        log_db.model_pricing, "settings",
        lambda *args, **kwargs: type("Settings", (), {"enabled": True})(),
    )

    result = log_db._aggregate_lifetime_month(_FetchAllProbeConnection(conn, loaded))

    attempt_cost_loads = [
        count for sql, count in loaded
        if "FROM upstream_attempt_usage a" in sql and "GROUP BY a.cost_source" in sql
    ]
    assert attempt_cost_loads == [3]
    attempt_cost_sql = [
        sql for sql, _count in loaded
        if "FROM upstream_attempt_usage a" in sql and "GROUP BY a.cost_source" in sql
    ]
    assert len(attempt_cost_sql) == 1
    assert "SUM(CASE WHEN a.cost_ticks>0" in attempt_cost_sql[0]
    assert log_db._EXACT_COST_TICKS_SUM_FUNCTION not in attempt_cost_sql[0]
    assert result["costed_success"] == 400
    assert result["unpriced_success"] == 200
    assert all("SELECT a.*" not in sql for sql, _count in loaded)


def _file_log_db(path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(log_db._schema_sql())
    return conn


def test_lifetime_sealed_cache_current_fresh_file_invalidation_and_singleflight(
    monkeypatch, tmp_path,
):
    current_month = datetime.now(log_db._BJT).strftime("%Y-%m")
    current_path = tmp_path / f"{current_month}.db"
    sealed_path = tmp_path / "2000-01.db"
    current = _file_log_db(current_path)
    sealed = _file_log_db(sealed_path)
    _insert_request(current, "current-1", datetime.now(log_db._BJT).timestamp())
    _insert_request(sealed, "sealed-1", 1.0)
    current.commit()
    sealed.commit()
    sealed.close()

    monkeypatch.setattr(log_db, "_log_dir", str(tmp_path))
    monkeypatch.setattr(log_db, "_get_conn", lambda: current)
    monkeypatch.setattr(
        log_db.model_pricing, "settings",
        lambda *args, **kwargs: type("Settings", (), {"enabled": False})(),
    )
    log_db._reset_lifetime_cache_for_tests()

    original = log_db._aggregate_lifetime_month
    calls = {"current": 0, "sealed": 0}

    def counted(conn):
        db_path = str(conn.execute("PRAGMA database_list").fetchone()[2])
        calls["current" if db_path == str(current_path) else "sealed"] += 1
        return original(conn)

    monkeypatch.setattr(log_db, "_aggregate_lifetime_month", counted)
    first = log_db.stats_lifetime()
    second = log_db.stats_lifetime()
    assert first == second
    assert first["total"] == 2
    assert calls == {"current": 2, "sealed": 1}

    _insert_request(current, "current-2", datetime.now(log_db._BJT).timestamp())
    current.commit()
    fresh = log_db.stats_lifetime()
    assert fresh["total"] == 3
    assert calls == {"current": 3, "sealed": 1}

    changed = _file_log_db(sealed_path)
    _insert_request(changed, "sealed-2", 2.0)
    changed.commit()
    changed.close()
    invalidated = log_db.stats_lifetime()
    assert invalidated["total"] == 4
    assert calls == {"current": 4, "sealed": 2}

    log_db._reset_lifetime_cache_for_tests()
    calls.update(current=0, sealed=0)
    results: list[dict] = []
    errors: list[BaseException] = []

    def run():
        try:
            results.append(log_db.stats_lifetime())
        except BaseException as exc:  # pragma: no cover - diagnostic capture
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert len(results) == 4
    assert all(result == invalidated for result in results)
    assert calls == {"current": 4, "sealed": 1}
    current.close()
