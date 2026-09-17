"""Dedicated search-call log: independent lifecycle, accounting and aggregation.

The search log is its own top-level table, not a child of request_log, so these
tests never create a request parent. Billing reuses the production
``normalize_response_billing`` / ``estimate_cost`` path, and a missing usage
object must stay ``unpriced`` rather than becoming a fabricated zero cost.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from src import log_db, model_pricing

BJT = timezone(timedelta(hours=8))


@pytest.fixture
def search_log(tmp_path, monkeypatch):
    """Isolated monthly log DB; never touches the real logs directory."""
    monkeypatch.setattr(log_db, "_log_dir", str(tmp_path))
    monkeypatch.setattr(log_db, "_local", threading.local())
    monkeypatch.setattr(log_db, "_write_conn_registry", {})
    monkeypatch.setattr(log_db, "_request_handles", {})
    monkeypatch.setattr(log_db, "_retired_log_paths", set())
    yield tmp_path
    for connections in log_db._write_conn_registry.values():
        for conn in connections:
            conn.close()


def _rows(path):
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT * FROM search_call_log ORDER BY id")]


def test_table_exists_without_any_request_parent(search_log):
    handle = log_db.record_search_call(
        call_id="call-1", attempt_no=1, source_id="tavily", source_type="tavily",
        source_name="Tavily", operation="search", credential_kind="api_key",
        credential_label="Key #1", query="hello",
    )
    assert handle.table == "search_call_log"
    # No request_log row was ever created; the search log stands alone.
    conn = log_db._get_conn()
    assert "search_call_log" in log_db._existing_tables(conn)
    with sqlite3.connect(handle.db.path) as raw:
        assert raw.execute("SELECT COUNT(*) FROM request_log").fetchone()[0] == 0
    rows = _rows(handle.db.path)
    assert len(rows) == 1
    assert rows[0]["status"] == "running" and rows[0]["request_id"] is None


def test_settlement_records_tokens_and_estimates_cost(search_log):
    handle = log_db.record_search_call(
        call_id="c", attempt_no=1, source_id="openai", source_type="openai",
        operation="search", credential_kind="oauth",
        account_key="openai:a@example.test", model="gpt-5.5",
    )
    body = {"model": "gpt-5.5", "usage": {"input_tokens": 1200, "output_tokens": 340}}
    log_db.finish_search_call(
        handle, status="success", elapsed_ms=1234, result_count=9, content_chars=4321,
        response_body=body, model="gpt-5.5", provider="openai",
    )
    row = _rows(handle.db.path)[0]
    assert row["status"] == "success"
    assert row["input_tokens"] == 1200 and row["output_tokens"] == 340
    assert row["usage_observed"] == 1
    assert row["elapsed_ms"] == 1234 and row["result_count"] == 9
    assert row["cost_source"] in ("estimated", "unpriced")
    if row["cost_source"] == "estimated":
        assert row["cost_ticks"] and row["cost_ticks"] > 0
        assert row["pricing_model"]


def test_missing_usage_stays_unpriced_and_never_becomes_zero_cost(search_log):
    handle = log_db.record_search_call(
        call_id="c", attempt_no=1, source_id="xai", source_type="xai",
        operation="search", credential_kind="oauth", model="grok-4.6",
    )
    log_db.finish_search_call(
        handle, status="success", elapsed_ms=10, response_body={"output": []},
        model="grok-4.6", provider="xai",
    )
    row = _rows(handle.db.path)[0]
    assert row["usage_observed"] == 0
    assert row["cost_source"] == "unpriced"
    assert row["cost_ticks"] is None


def test_xai_actual_cost_is_preferred_over_estimate(search_log):
    handle = log_db.record_search_call(
        call_id="c", attempt_no=1, source_id="xai", source_type="xai",
        operation="search", credential_kind="oauth", model="grok-4.6",
    )
    body = {"usage": {"input_tokens": 10, "output_tokens": 5,
                      "cost_in_usd_ticks": 9876543210}}
    log_db.finish_search_call(handle, status="success", elapsed_ms=5,
                              response_body=body, model="grok-4.6", provider="xai")
    row = _rows(handle.db.path)[0]
    assert row["cost_source"] == "actual"
    assert row["cost_ticks"] == 9876543210


def test_failed_call_keeps_latency_and_error_code(search_log):
    handle = log_db.record_search_call(
        call_id="c", attempt_no=2, source_id="exa", source_type="exa", operation="extract",
    )
    log_db.finish_search_call(handle, status="error", error_code="search_timeout",
                              elapsed_ms=10000)
    row = _rows(handle.db.path)[0]
    assert row["status"] == "error" and row["error_code"] == "search_timeout"
    assert row["elapsed_ms"] == 10000 and row["usage_observed"] == 0
    assert row["cost_source"] == "unpriced"


def test_stats_aggregate_per_source_and_never_leak_query_text(search_log):
    for source, ok in (("tavily", True), ("tavily", True), ("exa", False)):
        handle = log_db.record_search_call(
            call_id="c", attempt_no=1, source_id=source, source_type=source,
            operation="search", credential_label="Key #1", query="secret query text",
        )
        log_db.finish_search_call(
            handle, status="success" if ok else "error", elapsed_ms=100,
            result_count=3, error_code=None if ok else "search_timeout",
        )
    stats = {row["source_id"]: row for row in log_db.search_call_stats(0)}
    assert stats["tavily"]["attempts"] == 2 and stats["tavily"]["success"] == 2
    assert stats["exa"]["failed"] == 1 and stats["exa"]["attempts"] == 1
    assert stats["tavily"]["elapsed_sum"] == 200 and stats["tavily"]["elapsed_n"] == 2
    assert "secret query text" not in repr(stats)


def test_entries_are_newest_first_and_filterable_by_source(search_log):
    for index, source in enumerate(("tavily", "exa", "tavily")):
        handle = log_db.record_search_call(
            call_id=f"c{index}", attempt_no=1, source_id=source, source_type=source,
            operation="search", started_at=1000 + index,
        )
        log_db.finish_search_call(handle, status="success", elapsed_ms=1)
    entries = log_db.search_call_entries(0)
    assert [e["call_id"] for e in entries] == ["c2", "c1", "c0"]
    only = log_db.search_call_entries(0, source_id="tavily")
    assert [e["call_id"] for e in only] == ["c2", "c0"]
    assert len(log_db.search_call_entries(0, limit=1)) == 1


def test_duplicate_settlement_does_not_invent_a_second_row(search_log):
    handle = log_db.record_search_call(
        call_id="c", attempt_no=1, source_id="brave", source_type="brave", operation="search",
    )
    log_db.finish_search_call(handle, status="success", elapsed_ms=5)
    log_db.finish_search_call(handle, status="success", elapsed_ms=5)
    rows = _rows(handle.db.path)
    assert len(rows) == 1


def test_backend_id_column_is_absent_from_the_legacy_table(search_log):
    """The dedicated log replaces local_web_log for new writes, not by mutating it."""
    conn = log_db._get_conn()
    local_columns = {row[1] for row in conn.execute("PRAGMA table_info(local_web_log)")}
    # The old table is intentionally left byte-identical for history; the new
    # source dimension lives only in search_call_log.
    assert "backend_id" not in local_columns
    assert "source_id" in {row[1] for row in conn.execute("PRAGMA table_info(search_call_log)")}


def test_cost_usd_conversion_matches_pricing_ticks(search_log):
    handle = log_db.record_search_call(
        call_id="c", attempt_no=1, source_id="xai", source_type="xai", operation="search",
    )
    log_db.finish_search_call(
        handle, status="success", elapsed_ms=1,
        response_body={"usage": {"input_tokens": 1, "output_tokens": 1,
                                 "cost_in_usd_ticks": model_pricing.TICKS_PER_USD}},
        provider="xai",
    )
    row = _rows(handle.db.path)[0]
    assert row["cost_ticks"] == model_pricing.TICKS_PER_USD
