"""OBS-01..06: audit probes converted to correct-behaviour, local-only regressions."""
from __future__ import annotations

import json
import sqlite3
import threading
import weakref
from datetime import datetime, timedelta, timezone

import pytest

from src import config, log_db, model_pricing, model_reroute

BJT = timezone(timedelta(hours=8))


def ts(day):
    return datetime.fromisoformat(day).replace(tzinfo=BJT).timestamp()


@pytest.fixture
def logs(tmp_path, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime(2026, 9, 19, tzinfo=BJT)
            return value.astimezone(tz) if tz else value.replace(tzinfo=None)

    monkeypatch.setattr(log_db, "datetime", Clock)
    monkeypatch.setattr(log_db, "_log_dir", str(tmp_path))
    monkeypatch.setattr(log_db, "_local", threading.local())
    monkeypatch.setattr(log_db, "_write_conn_registry", {})
    monkeypatch.setattr(log_db, "_request_handles", {})
    monkeypatch.setattr(log_db, "_active_call_handles", weakref.WeakValueDictionary())
    monkeypatch.setattr(log_db, "_retired_log_paths", set())
    monkeypatch.setattr(log_db, "_last_retention_cleanup_key", None)
    monkeypatch.setattr(model_reroute, "on_log_observation", lambda payload: None)
    monkeypatch.setitem(config.get(), "logRetention", {"mode": "forever", "days": None})
    monkeypatch.setitem(config.get(), "logStoreBodies", True)
    yield tmp_path
    for conns in log_db._write_conn_registry.values():
        for conn in conns:
            conn.close()


def root(day, rid="root"):
    handle = log_db.insert_pending(rid, "127.0.0.1", "client-key", "public-alias", False,
                                   1, 0, {}, {}, created_at=ts(day))
    log_db.finish_success(handle, "api:fixture", "api", "fixture-model")
    return handle


def calls(day, name="call", *, finish=True):
    search = log_db.record_search_call(call_id=name, source_id="t", source_type="tavily", started_at=ts(day))
    mcp = log_db.record_mcp_call(call_id=name, tool_name="web_search", started_at=ts(day))
    log_db.save_mcp_call_detail(mcp, {"query": name}, created_at=ts("2026-09-19"))
    if finish:
        log_db.finish_search_call(search, status="success")
        log_db.finish_mcp_call(mcp, status="success")
    return search, mcp


def plan():
    return log_db.plan_retention(30, now_ts=ts("2026-09-19"))


def test_retention_preserves_unexpired_independent_rows_and_bodies(logs):
    old = root("2026-08-01")
    calls("2026-08-31", "keep")
    p = plan()
    assert p["items"][0]["action"] == "trim_and_vacuum"
    result = log_db.apply_retention_plan(p)
    assert result["ok"] and result["deleted_requests"] == 1
    assert (logs / "2026-08.db").exists()
    conn = log_db._get_conn_for_ref(old.db)
    assert conn.execute("SELECT count(*) FROM request_log").fetchone()[0] == 0
    assert log_db.search_call_entries(0)[0]["call_id"] == "keep"
    assert log_db.mcp_call_entry("keep") is not None
    assert json.loads(log_db.mcp_call_detail("keep")["result_body"]) == {"query": "keep"}


def test_independent_only_month_is_cleaned_including_late_detail(logs):
    calls("2026-01-01", "old")
    p = plan()
    assert p["items"][0]["expired_requests"] == 0
    assert p["items"][0]["expired_independent_rows"] == 3
    assert p["items"][0]["action"] == "delete_file"
    result = log_db.apply_retention_plan(p)
    assert result["ok"] and result["deleted_independent_rows"] == 3
    assert not (logs / "2026-01.db").exists()


def test_boundary_month_trims_each_call_by_its_own_date(logs):
    calls("2026-08-01", "old")
    calls("2026-08-31", "new")
    result = log_db.apply_retention_plan(plan())
    assert result["ok"] and result["deleted_independent_rows"] == 3
    assert log_db.mcp_call_entry("old") is None
    assert log_db.mcp_call_detail("old") is None
    assert log_db.mcp_call_entry("new") is not None
    assert log_db.mcp_call_detail("new") is not None
    assert [r["call_id"] for r in log_db.search_call_entries(0)] == ["new"]


def test_retention_confirmation_revalidates_new_independent_rows(logs):
    calls("2026-08-01", "old")
    p = plan()
    calls("2026-08-31", "new")
    result = log_db.apply_retention_plan(p)
    assert not result["ok"] and "重新扫描" in result["reason"]
    assert log_db.mcp_call_entry("new") is not None


@pytest.mark.parametrize("boundary", [False, True])
def test_live_call_handles_survive_retention_then_release(logs, boundary):
    active = calls("2026-08-01", "active", finish=False)
    if boundary:
        calls("2026-08-31", "new")
        calls("2026-08-02", "old")
    result = log_db.apply_retention_plan(plan())
    assert result["ok"] is boundary
    assert log_db.mcp_call_entry("active")["status"] == "running"
    assert log_db.mcp_call_detail("active") is not None
    log_db.finish_search_call(active[0], status="success")
    log_db.finish_mcp_call(active[1], status="success")
    result = log_db.apply_retention_plan(plan())
    assert result["ok"]
    assert log_db.mcp_call_entry("active") is None


def test_orphan_mcp_details_use_their_own_date(logs):
    conn = log_db._get_conn_for_ref(log_db._db_ref_for_timestamp(ts("2026-08-01")))
    conn.executemany("INSERT INTO mcp_call_detail VALUES (?,?,?)", [
        ("old", ts("2026-08-01"), "old"), ("new", ts("2026-08-31"), "new")])
    conn.commit()
    result = log_db.apply_retention_plan(plan())
    assert result["ok"] and result["deleted_independent_rows"] == 1
    assert log_db.mcp_call_detail("old") is None
    assert log_db.mcp_call_detail("new")["result_body"] == "new"


def test_historical_db_without_independent_tables_still_cleans(logs):
    handle = root("2026-08-01")
    conn = log_db._get_conn_for_ref(handle.db)
    for table in ("search_call_log", "mcp_call_log", "mcp_call_detail"):
        conn.execute(f"DROP TABLE {table}")
    conn.commit()
    assert log_db.apply_retention_plan(plan())["ok"]
    assert not (logs / "2026-08.db").exists()


@pytest.mark.parametrize("split, expected", [((30000, 50000), 10375000000), ((80000, 0), 8500000000), (None, None)])
def test_search_cache_ttl_split_is_priced_without_guessing(logs, monkeypatch, split, expected):
    model = "claude-opus-4-6"
    monkeypatch.setitem(config.get(), "modelBindings", {
        "defaults": {model: {"target": "anthropic/" + model, "source": "test"}}, "scoped": {}})
    usage = {"input_tokens": 20000, "output_tokens": 10000,
             "cache_creation_input_tokens": 80000, "cache_read_input_tokens": 0}
    if split is not None:
        usage["cache_creation"] = {"ephemeral_5m_input_tokens": split[0], "ephemeral_1h_input_tokens": split[1]}
    handle = log_db.record_search_call(call_id="cached", source_id="claude", source_type="anthropic",
                                      account_key="claude:fixture", credential_kind="oauth", model=model)
    log_db.finish_search_call(handle, status="success", model=model, provider="anthropic",
                              response_body={"type": "message", "model": model, "usage": usage})
    row = log_db.search_call_entries(0)[0]
    assert row["cost_ticks"] == expected
    assert row["cost_source"] == ("estimated" if expected is not None else "unpriced")
    assert row["cache_creation_tokens"] == 80000


@pytest.mark.parametrize("error", [False, True])
@pytest.mark.parametrize("bodies", [False, True])
def test_independent_observation_survives_clipping_and_body_switch(logs, monkeypatch, error, bodies):
    monkeypatch.setitem(config.get(), "logStoreBodies", bodies)
    handle = log_db.insert_pending("signal", "127.0.0.1", "k", "gpt-5.6-sol", True, 1, 0, {}, {})
    raw = json.dumps({"type": "response.completed", "response": {
        "model": "gpt-5.6-terra", "output": [{"text": "x" * 300000}],
        "usage": {"input_tokens": 1, "output_tokens": 1}},
        "safety_buffering": {"reasons": ["policy"]}})
    signals = model_reroute.extract_response_signals(raw)
    clipped = model_pricing.preserve_billing_evidence_tail(raw, usage={"input_tokens":1, "output_tokens":1},
        usage_observed=True, service_tier=None, actual_cost_ticks=None, event_type="response.completed")
    assert len(clipped) == 200000
    def no_reparse(_body):
        raise AssertionError("independent facts must not reparse cropped logs")
    monkeypatch.setattr(model_reroute, "extract_response_signals", no_reparse)
    kwargs = dict(final_channel_key="oauth:openai:fixture", final_channel_type="oauth", final_model="gpt-5.6-sol",
                  upstream_protocol="openai-responses", response_body=clipped, response_signals=signals)
    if error:
        log_db.finish_error(handle, "failed", **kwargs)
    else:
        log_db.finish_success(handle, **kwargs)
    conn = log_db._get_conn_for_ref(handle.db)
    row = conn.execute("SELECT * FROM request_log WHERE request_id='signal'").fetchone()
    assert row["upstream_actual_model"] == "gpt-5.6-terra"
    assert json.loads(row["safety_review"]) == {"reasons": ["policy"]}
    detail = conn.execute("SELECT response_body FROM request_detail WHERE request_id='signal'").fetchone()[0]
    assert (detail is not None) is bodies


@pytest.mark.parametrize("embedded,http,expected", [("embedded", "http", "embedded"), (None, "http", "http"), (None, None, "body")])
def test_model_observation_conflict_and_api_precedence(logs, embedded, http, expected):
    signals = model_reroute.ResponseModelSignals(header_model=embedded, body_model="body")
    actual, _, conflict = log_db._upstream_observation("", "sent", channel_key="oauth:openai:fixture",
        response_signals=signals, http_header_model=http)
    assert actual == (None if embedded or http else expected)
    assert bool(conflict) == bool(embedded or http)
    api_actual, _, api_conflict = log_db._upstream_observation("", "sent", channel_key="api:fixture",
        channel_type="api", upstream_protocol="openai-responses", response_signals=signals, http_header_model=http)
    assert api_actual == expected and api_conflict is None
    assert log_db._upstream_observation("", "sent", channel_key="oauth:claude:fixture",
        response_signals=model_reroute.ResponseModelSignals(body_model="body"), http_header_model=http) == (None, None, None)


@pytest.mark.parametrize("error", [False, True])
def test_cross_month_observation_notice_keeps_original_request_context(logs, monkeypatch, error):
    handle = log_db.insert_pending("rollover", "127.0.0.1", "client-key", "public-alias", False,
                                   1, 0, {}, {}, created_at=ts("2026-08-31T23:59:59"))
    received = []
    monkeypatch.setattr(model_reroute, "on_log_observation", received.append)
    kwargs = dict(final_channel_key="oauth:openai:fixture", final_channel_type="oauth", final_model="sent",
                  response_signals=model_reroute.ResponseModelSignals(body_model="actual"))
    if error:
        log_db.finish_error(handle, "error", **kwargs)
    else:
        log_db.finish_success(handle, **kwargs)
    assert received[0]["api_key_name"] == "client-key"
    assert received[0]["requested_model"] == "public-alias"


@pytest.mark.parametrize("table,timestamp,entry", [
    ("search_call_log", "started_at", log_db.search_call_entries),
    ("mcp_call_log", "created_at", log_db.mcp_call_entries),
])
def test_first_page_hydrates_only_requested_rows(logs, table, timestamp, entry):
    conn = log_db._get_conn()
    if table == "search_call_log":
        conn.executemany("INSERT INTO search_call_log(call_id,source_id,source_type,started_at,query) VALUES (?, 't','tavily',?,?)",
                         [(str(i), ts("2026-09-01") + i, "q" * 4000) for i in range(5000)])
    else:
        conn.executemany("INSERT INTO mcp_call_log(call_id,tool_name,created_at,params_json) VALUES (?, 'image_generate',?,?)",
                         [(str(i), ts("2026-09-01") + i, "{}") for i in range(5000)])
    conn.commit()
    hydrated = []
    statements = []
    def factory(cursor, values):
        if any(col[0] in {"query", "params_json"} for col in cursor.description):
            hydrated.append(1)
        return sqlite3.Row(cursor, values)
    conn.row_factory = factory
    conn.set_trace_callback(statements.append)
    page = entry(0, limit=1)
    assert len(page) == 1 and page[0]["call_id"] == "4999"
    assert len(hydrated) == 1
    assert any(f"SELECT id, {timestamp}" in sql and "LIMIT 1" in sql for sql in statements)
    assert not conn.in_transaction


def test_cross_month_paging_ties_and_filters_are_stable(logs):
    for day in ("2026-08-31", "2026-09-01"):
        for index in range(4):
            h = log_db.record_mcp_call(call_id=f"{day}-{index}", tool_name="web_search", api_key_name="k" if index % 2 else "other", started_at=ts(day))
            log_db.finish_mcp_call(h, status="success")
    rows = log_db.mcp_call_entries(0, api_key_name="k", tool_name="web_search", offset=1, limit=2)
    assert [r["call_id"] for r in rows] == ["2026-09-01-1", "2026-08-31-3"]
    assert log_db.mcp_call_entries(0, offset=100, limit=2) == []


def test_exact_call_lookup_is_not_limited_to_recent_200(logs, monkeypatch):
    old = log_db.record_mcp_call(call_id="wanted", tool_name="web_search", started_at=ts("2026-08-31"))
    log_db.finish_mcp_call(old, status="success")
    conn = log_db._get_conn()
    conn.executemany("INSERT INTO mcp_call_log(call_id,tool_name,created_at) VALUES (?, 'web_search', ?)",
                     [(f"new-{i}", ts("2026-09-01") + i) for i in range(501)])
    conn.commit()
    monkeypatch.setattr(log_db, "mcp_call_entries", lambda *a, **kw: pytest.fail("must not load a list"))
    assert log_db.mcp_call_entry("wanted")["status"] == "success"
    assert log_db.mcp_call_entry("wanted", since_ts=ts("2026-09-01")) is None
    assert log_db.mcp_call_entry("missing") is None


@pytest.mark.parametrize("prompt", ["x" * 9000, '中文\\\"\n' * 3000])
def test_long_mcp_parameters_remain_valid_and_preserve_short_siblings(logs, prompt):
    params = {"prompt": prompt, "model": "gpt-image-2", "n": 2}
    log_db.record_mcp_call(call_id="long", tool_name="image_generate", params=params)
    row = log_db.mcp_call_entry("long")
    assert len(row["params_json"]) <= 8000
    saved = json.loads(row["params_json"])
    assert saved["model"] == "gpt-image-2" and saved["n"] == 2
    assert "truncated" in saved["prompt"]
    assert params["prompt"] == prompt


def test_huge_mcp_container_is_a_marked_valid_preview(logs):
    saved = log_db._mcp_params_json({"items": list(range(10000))})
    assert len(saved) <= 8000
    assert json.loads(saved)["_parrot_truncated"] is True
    original = {"prompt": "small", "n": 1}
    assert log_db._mcp_params_json(original) == json.dumps(original, ensure_ascii=False)


def test_retention_failure_rolls_back_parent_and_independent_rows(logs):
    old = root("2026-08-01")
    calls("2026-08-01", "old")
    calls("2026-08-31", "new")
    conn = log_db._get_conn_for_ref(old.db)
    conn.execute("CREATE TRIGGER prevent_mcp_delete BEFORE DELETE ON mcp_call_log "
                 "BEGIN SELECT RAISE(ABORT, 'fixture deletion failure'); END")
    conn.commit()
    result = log_db.apply_retention_plan(plan())
    assert not result["ok"]
    assert conn.execute("SELECT count(*) FROM request_log").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM search_call_log").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM mcp_call_detail").fetchone()[0] == 2


def test_zero_cache_split_does_not_require_new_pricing_binding(logs, monkeypatch):
    model = "claude-opus-4-6"
    monkeypatch.setitem(config.get(), "modelBindings", {"defaults": {}, "scoped": {}})
    monkeypatch.setitem(config.get()["pricing"], "aliases", {model: "anthropic/" + model})
    estimate = model_pricing.estimate_cost(model, input_tokens=10, output_tokens=2)
    assert estimate is not None
    handle = log_db.record_search_call(call_id="no-write", source_id="claude", source_type="anthropic")
    body = {"type": "message", "usage": {"input_tokens": 10, "output_tokens": 2,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
            "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0}}}
    log_db.finish_search_call(handle, status="success", model=model, provider="anthropic", response_body=body)
    assert log_db.search_call_entries(0)[0]["cost_ticks"] == estimate.total_ticks


def test_pagination_closes_historical_connections_after_early_page_exit(logs, monkeypatch):
    calls("2026-08-31", "old")
    calls("2026-09-01", "new")
    opened = []
    real = log_db._open_readonly
    def capture(path):
        conn = real(path)
        opened.append(conn)
        return conn
    monkeypatch.setattr(log_db, "_open_readonly", capture)
    assert log_db.mcp_call_entries(0, limit=1)[0]["call_id"] == "new"
    assert opened
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")
    assert log_db._get_conn().execute("SELECT 1").fetchone()[0] == 1


def test_corrupt_independent_schema_fails_retention_closed(logs):
    old = root("2026-08-01")
    conn = log_db._get_conn_for_ref(old.db)
    conn.execute("DROP TABLE search_call_log")
    conn.execute("CREATE TABLE search_call_log (id INTEGER)")
    conn.commit()
    p = plan()
    assert p["errors"] and not p["preflight"]["ok"]
    assert not log_db.apply_retention_plan(p)["ok"]
    assert (logs / "2026-08.db").exists()
