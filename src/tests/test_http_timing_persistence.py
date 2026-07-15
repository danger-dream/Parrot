"""No-network, temporary-SQLite tests for HTTP timing persistence/compatibility."""

from __future__ import annotations

import sqlite3
import time

import pytest

from src import log_db, scorer
from src.protocols.runtime import AttemptResult


NEW_TIMING_COLUMNS = {
    "request_log": {
        "request_upload_ms",
        "response_headers_wait_ms",
        "response_body_first_byte_wait_ms",
    },
    "retry_chain": {
        "request_upload_ms",
        "response_headers_wait_ms",
        "response_body_first_byte_wait_ms",
        "total_ms",
    },
    "proxy_chain": {
        "proxy_tcp_ms",
        "proxy_tunnel_ms",
        "target_tls_ms",
        "total_ms",
    },
}


def _columns(conn: sqlite3.Connection, table: str) -> dict[str, sqlite3.Row]:
    return {row[1]: row for row in conn.execute(f"PRAGMA table_info({table})")}


@pytest.fixture
def timing_db(monkeypatch, tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(log_db._schema_sql())
    log_db._ensure_migrations(conn)
    # New production writes route by creation-bound LogDbRef, so the in-memory
    # fixture must intercept both current-month and ref-bound connection APIs.
    monkeypatch.setattr(log_db, "_log_dir", str(tmp_path))
    monkeypatch.setattr(log_db, "_request_handles", {})
    monkeypatch.setattr(log_db, "_get_conn", lambda: conn)
    monkeypatch.setattr(log_db, "_get_conn_for_ref", lambda ref: conn)
    try:
        yield conn
    finally:
        conn.close()


def test_new_and_old_schema_add_only_nullable_timing_columns():
    fresh = sqlite3.connect(":memory:")
    fresh.row_factory = sqlite3.Row
    fresh.executescript(log_db._schema_sql())
    for table, names in NEW_TIMING_COLUMNS.items():
        cols = _columns(fresh, table)
        assert names.issubset(cols)
        assert all(cols[name][3] == 0 for name in names)  # PRAGMA notnull
    fresh.close()

    old = sqlite3.connect(":memory:")
    old.row_factory = sqlite3.Row
    old.executescript(
        """
        CREATE TABLE request_log (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          request_id TEXT UNIQUE NOT NULL,
          created_at REAL NOT NULL,
          connect_time_ms INTEGER
        );
        CREATE TABLE retry_chain (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          request_id TEXT NOT NULL,
          attempt_order INTEGER NOT NULL,
          channel_key TEXT NOT NULL,
          channel_type TEXT NOT NULL,
          model TEXT NOT NULL,
          started_at REAL NOT NULL,
          connect_ms INTEGER,
          first_byte_ms INTEGER,
          ended_at REAL,
          outcome TEXT,
          error_detail TEXT
        );
        CREATE TABLE proxy_chain (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          request_id TEXT NOT NULL,
          retry_attempt_id INTEGER,
          attempt_order INTEGER NOT NULL,
          proxy_name TEXT NOT NULL,
          started_at REAL NOT NULL,
          connect_ms INTEGER,
          ended_at REAL,
          outcome TEXT,
          error_detail TEXT
        );
        INSERT INTO request_log(request_id, created_at, connect_time_ms)
          VALUES ('legacy', 1, 90000);
        INSERT INTO retry_chain(
          request_id, attempt_order, channel_key, channel_type, model,
          started_at, connect_ms, outcome, error_detail
        ) VALUES (
          'legacy', 1, 'api:a', 'api', 'm', 1, 90000,
          'first_byte_timeout',
          'first byte timeout > 90s while waiting for response headers'
        );
        INSERT INTO proxy_chain(
          request_id, retry_attempt_id, attempt_order, proxy_name,
          started_at, connect_ms, outcome, error_detail
        ) VALUES (
          'legacy', 1, 1, 'p1', 1, 90000,
          'first_byte_timeout',
          'first byte timeout > 90s while waiting for response headers'
        );
        """
    )
    before = {
        "request": tuple(old.execute(
            "SELECT request_id, connect_time_ms FROM request_log"
        ).fetchone()),
        "retry": tuple(old.execute(
            "SELECT request_id, connect_ms, outcome, error_detail FROM retry_chain"
        ).fetchone()),
        "proxy": tuple(old.execute(
            "SELECT request_id, connect_ms, outcome, error_detail FROM proxy_chain"
        ).fetchone()),
    }

    log_db._ensure_migrations(old)

    for table, names in NEW_TIMING_COLUMNS.items():
        cols = _columns(old, table)
        assert names.issubset(cols)
        assert all(cols[name][3] == 0 for name in names)
    after = {
        "request": tuple(old.execute(
            "SELECT request_id, connect_time_ms FROM request_log"
        ).fetchone()),
        "retry": tuple(old.execute(
            "SELECT request_id, connect_ms, outcome, error_detail FROM retry_chain"
        ).fetchone()),
        "proxy": tuple(old.execute(
            "SELECT request_id, connect_ms, outcome, error_detail FROM proxy_chain"
        ).fetchone()),
    }
    assert after == before  # migration does not rewrite historical rows
    old.close()


def test_request_retry_proxy_timing_round_trip(timing_db):
    rid = "timing-round-trip"
    log_db.insert_pending(rid, "127.0.0.1", "k", "m", True, 1, 0, {}, {})
    retry_id = log_db.record_retry_attempt(rid, 1, "api:a", "api", "m", time.time())
    proxy_id = log_db.record_proxy_attempt(rid, retry_id, 1, "p1", time.time())

    log_db.update_retry_attempt(
        retry_id,
        connect_ms=12,
        first_byte_ms=123,
        request_upload_ms=4,
        response_headers_wait_ms=80,
        response_body_first_byte_wait_ms=27,
        total_ms=456,
        ended_at=time.time(),
        outcome="success",
        bytes_up=10,
        bytes_down=20,
    )
    log_db.update_proxy_attempt(
        proxy_id,
        connect_ms=12,
        proxy_tcp_ms=3,
        proxy_tunnel_ms=5,
        target_tls_ms=4,
        total_ms=456,
        ended_at=time.time(),
        outcome="connected",
        bytes_up=10,
        bytes_down=20,
    )
    log_db.finish_success(
        rid,
        "api:a",
        "api",
        "m",
        connect_ms=12,
        first_token_ms=123,
        total_ms=456,
        request_upload_ms=4,
        response_headers_wait_ms=80,
        response_body_first_byte_wait_ms=27,
        proxy_name="p1",
        proxy_bytes_up=10,
        proxy_bytes_down=20,
    )

    detail = log_db.log_detail(rid)
    assert {
        key: detail["log"][key]
        for key in (
            "connect_time_ms",
            "request_upload_ms",
            "response_headers_wait_ms",
            "response_body_first_byte_wait_ms",
            "first_token_time_ms",
            "total_time_ms",
        )
    } == {
        "connect_time_ms": 12,
        "request_upload_ms": 4,
        "response_headers_wait_ms": 80,
        "response_body_first_byte_wait_ms": 27,
        "first_token_time_ms": 123,
        "total_time_ms": 456,
    }
    assert {
        key: detail["retry_chain"][0][key]
        for key in (
            "connect_ms",
            "request_upload_ms",
            "response_headers_wait_ms",
            "response_body_first_byte_wait_ms",
            "first_byte_ms",
            "total_ms",
        )
    } == {
        "connect_ms": 12,
        "request_upload_ms": 4,
        "response_headers_wait_ms": 80,
        "response_body_first_byte_wait_ms": 27,
        "first_byte_ms": 123,
        "total_ms": 456,
    }
    assert {
        key: detail["proxy_chain"][0][key]
        for key in (
            "connect_ms",
            "proxy_tcp_ms",
            "proxy_tunnel_ms",
            "target_tls_ms",
            "total_ms",
        )
    } == {
        "connect_ms": 12,
        "proxy_tcp_ms": 3,
        "proxy_tunnel_ms": 5,
        "target_tls_ms": 4,
        "total_ms": 456,
    }


def _insert_header_timeout(
    rid: str,
    *,
    connect_ms: int,
    header_wait_ms: int | None,
    proxy_total_ms: int | None,
) -> None:
    detail = "first byte timeout > 90s while waiting for response headers"
    log_db.insert_pending(rid, "127.0.0.1", "k", "m", True, 1, 0, {}, {})
    retry_id = log_db.record_retry_attempt(rid, 1, "api:a", "api", "m", time.time())
    proxy_id = log_db.record_proxy_attempt(rid, retry_id, 1, "p1", time.time())
    log_db.update_retry_attempt(
        retry_id,
        connect_ms=connect_ms,
        response_headers_wait_ms=header_wait_ms,
        total_ms=proxy_total_ms,
        ended_at=time.time(),
        outcome="first_byte_timeout",
        error_detail=detail,
    )
    log_db.update_proxy_attempt(
        proxy_id,
        connect_ms=connect_ms,
        total_ms=proxy_total_ms,
        ended_at=time.time(),
        outcome="first_byte_timeout",
        error_detail=detail,
    )
    log_db.finish_error(
        rid,
        detail,
        final_channel_key="api:a",
        final_channel_type="api",
        final_model="m",
        connect_ms=connect_ms,
        total_ms=proxy_total_ms,
        response_headers_wait_ms=header_wait_ms,
        proxy_name="p1",
    )


def test_historical_pseudo_connect_is_null_in_detail_and_aggregates(
    timing_db, monkeypatch, tmp_path
):
    _insert_header_timeout(
        "legacy-header-timeout",
        connect_ms=90000,
        header_wait_ms=None,
        proxy_total_ms=None,
    )
    _insert_header_timeout(
        "measured-header-timeout",
        connect_ms=12,
        header_wait_ms=90000,
        proxy_total_ms=90012,
    )

    legacy = log_db.log_detail("legacy-header-timeout")
    assert legacy["log"]["connect_time_ms"] is None
    assert legacy["retry_chain"][0]["connect_ms"] is None
    assert legacy["proxy_chain"][0]["connect_ms"] is None
    # Read compatibility does not mutate the stored historical values.
    assert timing_db.execute(
        "SELECT connect_time_ms FROM request_log WHERE request_id=?",
        ("legacy-header-timeout",),
    ).fetchone()[0] == 90000

    measured = log_db.log_detail("measured-header-timeout")
    assert measured["log"]["connect_time_ms"] == 12
    assert measured["retry_chain"][0]["connect_ms"] == 12
    assert measured["proxy_chain"][0]["connect_ms"] == 12

    monkeypatch.setattr(log_db, "_log_dir", str(tmp_path))
    monkeypatch.setattr(
        log_db,
        "_iter_month_conns_all",
        lambda since: [(timing_db, lambda: None)],
    )
    p1 = next(row for row in log_db.proxy_stats() if row["proxy_name"] == "p1")
    assert p1["avg_connect_ms"] == 12

    # General summary also skips missing connect samples rather than coercing to 0.
    log_db.insert_pending("null-connect", "127.0.0.1", "k", "m", True, 1, 0, {}, {})
    log_db.finish_success("null-connect", "api:a", "api", "m", connect_ms=None)
    log_db.insert_pending("real-connect", "127.0.0.1", "k", "m", True, 1, 0, {}, {})
    log_db.finish_success("real-connect", "api:a", "api", "m", connect_ms=20)
    monkeypatch.setattr(log_db, "_iter_month_conns", lambda since: [(timing_db, lambda: None)])
    assert log_db.stats_summary(0)["overall"]["avg_connect_ms"] == 20


def test_scorer_input_drops_legacy_90s_without_dropping_real_slow_connect(monkeypatch):
    from src import failover

    detail = "first byte timeout > 90s while waiting for response headers"
    legacy = AttemptResult(
        outcome="first_byte_timeout",
        connect_ms=90000,
        response_headers_wait_ms=None,
        error_detail=detail,
    )
    measured = AttemptResult(
        outcome="first_byte_timeout",
        connect_ms=12,
        response_headers_wait_ms=90000,
        error_detail=detail,
    )
    legitimate_slow = AttemptResult(
        outcome="connect_timeout",
        connect_ms=90000,
        error_detail="connect timeout > 90s",
    )
    assert failover._scorer_connect_ms(legacy) is None
    assert failover._scorer_connect_ms(measured) == 12
    assert failover._scorer_connect_ms(legitimate_slow) == 90000

    key = ("api:a", "m")
    stats = {
        "total_requests": 1,
        "success_count": 1,
        "recent_requests": 1,
        "recent_success_count": 1,
        "avg_connect_ms": 50.0,
        "avg_first_byte_ms": 100.0,
        "avg_total_ms": 200.0,
        "last_updated": 0,
    }
    monkeypatch.setattr(scorer, "_stats", {key: stats})
    monkeypatch.setattr(
        scorer,
        "_params",
        lambda: {
            "emaAlpha": 0.25,
            "recentWindow": 50,
            "defaultScore": 3000.0,
            "errorPenaltyFactor": 8.0,
            "staleMinutes": 15.0,
            "staleFullDecayMinutes": 30.0,
            "explorationRate": 0.0,
        },
    )
    saved = []
    monkeypatch.setattr(scorer.state_db, "perf_save", lambda *args: saved.append(args))
    scorer.record_failure("api:a", "m", connect_ms=failover._scorer_connect_ms(legacy))
    assert scorer.get_stats("api:a", "m")["avg_connect_ms"] == 50.0
    assert saved and saved[-1][2]["avg_connect_ms"] == 50.0


def test_log_detail_renderer_shows_complete_stages_nulls_and_flow(timing_db):
    from src.telegram.menus import logs_menu

    error_detail = "first byte timeout > 90s while waiting for response headers"
    rendered = logs_menu._render_detail({
        "log": {
            "request_id": "current-header-timeout",
            "created_at": 1,
            "status": "error",
            "http_status": 504,
            "connect_time_ms": 12,
            "request_upload_ms": 4,
            "response_headers_wait_ms": 90000,
            "response_body_first_byte_wait_ms": None,
            "first_token_time_ms": None,
            "idle_time_ms": 7,
            "total_time_ms": 90012,
            "final_round_id": "round-current",
            "request_elapsed_ms": 90020,
            "upstream_transport": "http",
            "error_message": error_detail,
        },
        "retry_chain": [{
            "id": 11,
            "attempt_order": 1,
            "channel_key": "api:a",
            "model": "m",
            "outcome": "first_byte_timeout",
            "connect_ms": 12,
            "request_upload_ms": 4,
            "response_headers_wait_ms": 90000,
            "response_body_first_byte_wait_ms": None,
            "first_byte_ms": None,
            "idle_ms": 7,
            "total_ms": 90012,
            "final_round_id": "round-current",
            "attempt_elapsed_ms": 90018,
            "error_detail": error_detail,
        }],
        "proxy_chain": [{
            "retry_attempt_id": 11,
            "attempt_order": 1,
            "round_id": "round-current",
            "transport": "http",
            "request_mode": "http_non_stream",
            "proxy_name": "p1",
            "outcome": "first_byte_timeout",
            "connect_ms": 12,
            "proxy_tcp_ms": 3,
            "proxy_tunnel_ms": None,
            "target_tls_ms": 4,
            "idle_ms": 7,
            "total_ms": 90012,
            "bytes_up": 10,
            "bytes_down": 20,
            "error_detail": error_detail,
        }],
    })
    assert "最终轮次: <code>round-current</code>" in rendered
    assert "业务计时: 连接 12ms · 空闲 7ms · 总计 90.0s" in rendered
    assert "首字" not in rendered
    assert "请求全程（外层）: 90.0s" in rendered
    assert "可靠阶段（可能重叠，不相加）: 请求上传 4ms · 等待响应头 90.0s" in rendered
    assert "终止轮摘要: 连接 12ms · 空闲 7ms · 总计 90.0s" in rendered
    assert "渠道尝试全程（外层）: 90.0s" in rendered
    assert "轮次 1" in rendered and "ID <code>round-current</code>" in rendered
    assert "代理 TCP 3ms" in rendered
    assert "代理隧道" not in rendered
    assert "目标 TLS 4ms" in rendered
    assert "流量 30B" in rendered

    _insert_header_timeout(
        "legacy-render-timeout",
        connect_ms=90000,
        header_wait_ms=None,
        proxy_total_ms=None,
    )
    historical = logs_menu._render_detail(log_db.log_detail("legacy-render-timeout"))
    assert "业务计时: 无可靠样本" in historical
    assert "连接 90.0s" not in historical
    assert "首字" not in historical


def test_log_detail_pages_preserve_long_final_error_without_html_truncation(timing_db):
    from src.telegram.menus import logs_menu

    long_error = "错误开始<>&" + ("长内容&" * 1800) + "错误结束<>&"
    pages = logs_menu._render_detail_pages({
        "log": {
            "request_id": "long-error-detail",
            "created_at": 1,
            "status": "error",
            "error_message": long_error,
        },
        "retry_chain": [],
        "proxy_chain": [],
    })

    assert len(pages) > 1
    assert all(len(page) < 4096 for page in pages)
    rendered = "\n".join(pages)
    assert "已截断" not in rendered
    assert "<b>最终错误</b>" in rendered
    assert "错误开始&lt;&gt;&amp;" in rendered
    assert "错误结束&lt;&gt;&amp;" in rendered
    assert rendered.count("<i>") == rendered.count("</i>")
