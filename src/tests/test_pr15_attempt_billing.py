from __future__ import annotations

import json
import math
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))
from src.tests import _isolation
_isolation.isolate()


def _import_modules():
    from src import config, log_db, model_pricing
    from src.telegram import ui
    return {"config": config, "log_db": log_db, "model_pricing": model_pricing, "ui": ui}


def _setup(m):
    m["log_db"].init()
    conn = m["log_db"]._get_conn()
    for table in ("upstream_attempt_usage", "retry_chain", "request_detail", "request_log"):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()


def _pending(ld, rid: str, model: str = "gpt-5.6-luna", *, fast: bool = False):
    ld.insert_pending(
        rid, "127.0.0.1", "k", model, False, 1, 0, {}, {},
        ingress_protocol="responses", fast_mode=fast,
    )


def test_strict_normalization_observes_zero_and_terminal_errors(m):
    mp = m["model_pricing"]
    explicit_zero = mp.normalize_response_billing(
        'event: response.failed\ndata: {"type":"response.failed","response":'
        '{"service_tier":"default","usage":{"input_tokens":0,"output_tokens":0,'
        '"cost_in_usd_ticks":"0"}}}\n\n'
    )
    assert explicit_zero.usage_observed is True
    assert explicit_zero.actual_cost_ticks == 0
    assert explicit_zero.service_tier == "default"
    arbitrary = 'metadata={"response":{"usage":{"cost_in_usd_ticks":999}}}'
    assert mp.normalize_response_billing(arbitrary).usage_observed is False
    assert mp.extract_actual_cost_ticks(arbitrary) is None
    wrapped_nonterminal = (
        'data: {"type":"response.output_item.done","data":{"usage":'
        '{"input_tokens":1,"cost_in_usd_ticks":777}}}\n\n'
    )
    wrapped = mp.normalize_response_billing(wrapped_nonterminal)
    assert wrapped.usage_observed is True
    assert wrapped.actual_cost_ticks is None


def test_nonfinite_values_isolate_only_bad_models(m):
    mp = m["model_pricing"]
    parsed = mp._parse_catalog({
        "bad-price": {"input_cost_per_token": float("inf"), "output_cost_per_token": 1e-6},
        "bad-multiplier": {
            "input_cost_per_token": 1e-6, "output_cost_per_token": 2e-6,
            "long_context_input_cost_multiplier": "Infinity",
        },
        "bad-threshold": {
            "input_cost_per_token": 1e-6, "output_cost_per_token": 2e-6,
            "long_context_input_token_threshold": "NaN",
        },
        "good": {"input_cost_per_token": 1e-6, "output_cost_per_token": 2e-6},
    })
    assert set(parsed) == {"good"}
    cfg = {"pricing": {"enabled": True, "overrides": {
        "bad": {"inputPerMillion": math.inf, "outputPerMillion": 1},
        "good": {"inputPerMillion": 1, "outputPerMillion": 2},
    }}}
    assert set(mp.settings(cfg).overrides) == {"good"}


def test_provider_qualified_bundled_grok_default(m):
    mp = m["model_pricing"]
    mp.reset_for_tests()
    mp.initialize()
    qualified = mp.provider_pricing_model("grok-4.5", "oauth:xai:user@example.com")
    assert qualified == "xai/grok-4.5"
    estimate = mp.estimate_cost(
        qualified, input_tokens=1_000_000, output_tokens=1_000_000,
        pricing_settings=mp.settings({"pricing": {"enabled": True}}),
    )
    assert estimate is not None
    assert estimate.total_ticks == 18 * mp.TICKS_PER_USD


def test_missing_usage_unpriced_but_explicit_zero_costs_zero(m):
    _setup(m); ld = m["log_db"]
    _pending(ld, "missing")
    a1 = ld.record_retry_attempt("missing", 1, "api:A", "api", "gpt-5.6-luna", 1.0)
    ld.update_retry_attempt(a1, outcome="http_error", ended_at=2.0)
    ld.finish_error("missing", "boom", final_channel_key="api:A", final_model="gpt-5.6-luna")

    _pending(ld, "zero")
    a2 = ld.record_retry_attempt("zero", 1, "api:A", "api", "gpt-5.6-luna", 1.0)
    body = json.dumps({"usage": {"input_tokens": 0, "output_tokens": 0}})
    ld.finish_success("zero", "api:A", "api", "gpt-5.6-luna", response_body=body)
    ld.update_retry_attempt(a2, outcome="success", ended_at=2.0)

    rows = ld._get_conn().execute(
        "SELECT root_request_id, usage_observed, dispatch_state, cost_source, "
        "cost_ticks, pricing_snapshot_json, pricing_version "
        "FROM upstream_attempt_usage ORDER BY id"
    ).fetchall()
    assert tuple(rows[0])[:5] == ("missing", 0, "sent", "unpriced", None)
    assert tuple(rows[1])[:5] == ("zero", 1, "sent", "estimated", 0)
    assert rows[1]["pricing_snapshot_json"]
    assert rows[1]["pricing_version"].startswith("pricing-v1:")
    assert ld.cost_for_log(dict(ld._get_conn().execute(
        "SELECT * FROM request_log WHERE request_id='missing'"
    ).fetchone()))["unpriced_success"] == 1
    zero = ld.cost_for_log(dict(ld._get_conn().execute(
        "SELECT * FROM request_log WHERE request_id='zero'"
    ).fetchone()))
    assert zero["costed_success"] == 1 and zero["cost_ticks"] == 0


def test_actual_service_tier_overrides_fast_intent(m):
    _setup(m); ld = m["log_db"]; mp = m["model_pricing"]
    _pending(ld, "tier", fast=True)
    aid = ld.record_retry_attempt("tier", 1, "api:openai", "api", "gpt-5.6-luna", 1.0)
    body = json.dumps({
        "service_tier": "default",
        "usage": {"input_tokens": 100_000, "output_tokens": 100_000},
    })
    ld.finish_success(
        "tier", "api:openai", "api", "gpt-5.6-luna",
        input_tokens=100_000, output_tokens=100_000, response_body=body,
    )
    ld.update_retry_attempt(aid, outcome="success", ended_at=2.0)
    row = ld._get_conn().execute(
        "SELECT service_tier,cost_ticks FROM upstream_attempt_usage WHERE retry_attempt_id=?", (aid,)
    ).fetchone()
    req = ld._get_conn().execute(
        "SELECT actual_service_tier FROM request_log WHERE request_id='tier'"
    ).fetchone()
    assert row["service_tier"] == req["actual_service_tier"] == "default"
    standard = mp.estimate_cost("gpt-5.6-luna", input_tokens=100_000, output_tokens=100_000)
    fast = mp.estimate_cost("gpt-5.6-luna", input_tokens=100_000, output_tokens=100_000, priority=True)
    assert standard is not None and fast is not None
    assert row["cost_ticks"] == standard.total_ticks
    assert row["cost_ticks"] != fast.total_ticks


def test_outbound_tier_is_second_precedence_and_intent_is_not_billing_fact(m):
    _setup(m); ld = m["log_db"]; mp = m["model_pricing"]
    _pending(ld, "outbound", fast=False)
    aid = ld.record_retry_attempt(
        "outbound", 1, "api:openai", "api", "gpt-5.6-luna", 1.0,
    )
    ld.mark_retry_attempt_dispatch(aid, {"service_tier": "priority"})
    body = json.dumps({"usage": {"input_tokens": 100_000, "output_tokens": 100_000}})
    ld.finish_success(
        "outbound", "api:openai", "api", "gpt-5.6-luna",
        input_tokens=100_000, output_tokens=100_000, response_body=body,
    )
    ld.update_retry_attempt(aid, outcome="success", ended_at=2.0)
    row = ld._get_conn().execute(
        "SELECT service_tier,outbound_service_tier,cost_ticks "
        "FROM upstream_attempt_usage WHERE retry_attempt_id=?", (aid,),
    ).fetchone()
    fast = mp.estimate_cost(
        "gpt-5.6-luna", input_tokens=100_000, output_tokens=100_000,
        priority=True,
    )
    assert fast is not None
    assert row["service_tier"] == row["outbound_service_tier"] == "priority"
    assert row["cost_ticks"] == fast.total_ticks


def test_xai_error_actual_cost_and_strict_sse(m):
    _setup(m); ld = m["log_db"]
    _pending(ld, "xai-err", "grok-4.5")
    aid = ld.record_retry_attempt(
        "xai-err", 1, "oauth:xai:user@example.com", "oauth", "grok-4.5", 1.0
    )
    body = (
        'event: response.failed\n'
        'data: {"type":"response.failed","response":{"service_tier":"priority",'
        '"usage":{"cost_in_usd_ticks":321}}}\n\n'
    )
    ld.update_retry_attempt(aid, outcome="http_error", response_body=body, ended_at=2.0)
    ld.finish_error(
        "xai-err", "upstream failed", final_channel_key="oauth:xai:user@example.com",
        final_channel_type="oauth", final_model="grok-4.5", response_body=body,
    )
    row = ld._get_conn().execute(
        "SELECT * FROM upstream_attempt_usage WHERE retry_attempt_id=?", (aid,)
    ).fetchone()
    assert row["cost_source"] == "actual" and row["cost_ticks"] == 321
    assert row["pricing_model"] == "xai/grok-4.5"
    assert row["usage_observed"] == 0
    assert row["pricing_version"] == "upstream-actual-v1"


def test_transport_uncertainty_is_unpriced_and_finalize_is_idempotent(m):
    _setup(m); ld = m["log_db"]
    _pending(ld, "uncertain")
    aid = ld.record_retry_attempt("uncertain", 1, "api:A", "api", "gpt-5.6-luna", 1.0)
    assert ld.settle_retry_attempt(aid, outcome="transport_error", usage={
        "input_tokens": 0, "output_tokens": 0,
        "cache_creation": 0, "cache_read": 0,
    }) is True
    assert ld.settle_retry_attempt(aid, outcome="transport_error") is False
    row = ld._get_conn().execute(
        "SELECT dispatch_state,cost_source,cost_ticks FROM upstream_attempt_usage "
        "WHERE retry_attempt_id=?", (aid,),
    ).fetchone()
    assert tuple(row) == ("unknown", "unpriced", None)
    assert ld._get_conn().execute(
        "SELECT COUNT(*) FROM upstream_attempt_usage WHERE retry_attempt_id=?", (aid,),
    ).fetchone()[0] == 1


def test_pre_dispatch_guard_does_not_create_billing_fact(m):
    _setup(m); ld = m["log_db"]
    _pending(ld, "guard")
    aid = ld.record_retry_attempt("guard", 1, "api:A", "api", "gpt-5.6-luna", 1.0)
    ld.update_retry_attempt(aid, outcome="transform_error", ended_at=2.0)
    assert ld._get_conn().execute(
        "SELECT COUNT(*) FROM upstream_attempt_usage WHERE retry_attempt_id=?", (aid,),
    ).fetchone()[0] == 0


def test_multi_attempt_and_compact_segments_aggregate_to_root(m):
    _setup(m); ld = m["log_db"]
    _pending(ld, "multi")
    a1 = ld.record_retry_attempt("multi", 1, "api:A", "api", "gpt-5.6-luna", 1.0)
    b1 = json.dumps({"usage": {"input_tokens": 100, "output_tokens": 10}})
    ld.update_retry_attempt(a1, outcome="http_error", response_body=b1, ended_at=2.0)
    a2 = ld.record_retry_attempt("multi", 2, "api:B", "api", "gpt-5.6-luna", 3.0)
    b2 = json.dumps({"usage": {"input_tokens": 200, "output_tokens": 20}})
    ld.finish_success(
        "multi", "api:B", "api", "gpt-5.6-luna",
        input_tokens=200, output_tokens=20, response_body=b2, retry_count=1,
    )
    ld.update_retry_attempt(a2, outcome="success", ended_at=4.0)

    # Compact map/reduce uses child call ids but must aggregate on the parent.
    _pending(ld, "compact", "gpt-5.6-sol")
    for order, suffix, inp in ((1, "1", 30), (2, "2", 40), (3, "reduce", 50)):
        aid = ld.record_retry_attempt(
            f"compact:compact:{suffix}", order, "api:C", "api", "gpt-5.6-sol", float(order)
        )
        body = json.dumps({"usage": {"input_tokens": inp, "output_tokens": 5}})
        ld.settle_retry_attempt(aid, outcome="success", response_body=body)
        ld.update_retry_attempt(aid, outcome="success", ended_at=float(order + 1))
    ld.finish_success("compact", "compact-rescue", "internal", "gpt-5.6-sol", response_body="{}")

    rows = ld._get_conn().execute(
        "SELECT root_request_id,COUNT(*) n,SUM(input_tokens) inp,SUM(output_tokens) outp "
        "FROM upstream_attempt_usage GROUP BY root_request_id ORDER BY root_request_id"
    ).fetchall()
    got = {r["root_request_id"]: (r["n"], r["inp"], r["outp"]) for r in rows}
    assert got["multi"] == (2, 300, 30)
    assert got["compact"] == (3, 120, 15)
    summary = ld.stats_summary(0, summary_top_limit=5)
    assert summary["overall"]["total_input_tokens"] == 420
    assert summary["overall"]["total_output_tokens"] == 45
    assert summary["overall"]["costed_success"] == 5
    multi_row = dict(ld._get_conn().execute(
        "SELECT * FROM request_log WHERE request_id='multi'"
    ).fetchone())
    assert ld.cost_for_log(multi_row)["costed_success"] == 2
    # Per-channel surfaces attribute each failover attempt to the channel that
    # actually received it, rather than charging both attempts to final B.
    a_cost = ld.tokens_for_channel("api:A", since_ts=0)
    b_cost = ld.tokens_for_channel("api:B", since_ts=0)
    a_row = ld._get_conn().execute(
        "SELECT cost_ticks FROM upstream_attempt_usage WHERE retry_attempt_id=?", (a1,)
    ).fetchone()
    b_row = ld._get_conn().execute(
        "SELECT cost_ticks FROM upstream_attempt_usage WHERE retry_attempt_id=?", (a2,)
    ).fetchone()
    assert a_cost["cost_ticks"] == a_row["cost_ticks"]
    assert b_cost["cost_ticks"] == b_row["cost_ticks"]


def test_token_surfaces_show_cost_without_cache_hit(m):
    _setup(m); ld = m["log_db"]; ui = m["ui"]
    _pending(ld, "display")
    aid = ld.record_retry_attempt("display", 1, "api:A", "api", "gpt-5.6-luna", 1.0)
    body = json.dumps({"usage": {"input_tokens": 100, "output_tokens": 10}})
    ld.finish_success(
        "display", "api:A", "api", "gpt-5.6-luna",
        input_tokens=100, output_tokens=10, cache_read_tokens=0, response_body=body,
    )
    ld.update_retry_attempt(aid, outcome="success", ended_at=2.0)
    row = dict(ld._get_conn().execute("SELECT * FROM request_log WHERE request_id='display'").fetchone())
    rendered = ui.fmt_log_entry_body(row)
    assert "Token:" in rendered and "💵" in rendered
    assert "未计价" not in rendered


def test_backward_compatible_schema_migration_keeps_old_rows_readable(m):
    import sqlite3
    ld = m["log_db"]
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE request_log (
          id INTEGER PRIMARY KEY, request_id TEXT UNIQUE NOT NULL,
          created_at REAL NOT NULL, requested_model TEXT, status TEXT,
          input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
          cache_creation_tokens INTEGER DEFAULT 0, cache_read_tokens INTEGER DEFAULT 0
        );
        INSERT INTO request_log(request_id,created_at,requested_model,status,input_tokens)
        VALUES('legacy',1,'legacy-model','success',7);
        CREATE TABLE retry_chain (
          id INTEGER PRIMARY KEY, request_id TEXT, attempt_order INTEGER,
          channel_key TEXT, channel_type TEXT, model TEXT, started_at REAL,
          connect_ms INTEGER, first_byte_ms INTEGER, ended_at REAL,
          outcome TEXT, error_detail TEXT
        );
    """)
    ld._ensure_migrations(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(request_log)")}
    assert "actual_service_tier" in cols and "fast_mode" in cols
    assert conn.execute("SELECT input_tokens FROM request_log WHERE request_id='legacy'").fetchone()[0] == 7
    assert conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='upstream_attempt_usage'"
    ).fetchone() is not None


def test_transformed_outbound_priority_is_persisted_and_response_overrides_it(m):
    _setup(m); ld = m["log_db"]; mp = m["model_pricing"]
    _pending(ld, "outbound-tier")
    aid = ld.record_retry_attempt(
        "outbound-tier", 1, "oauth:openai:user@example.com", "oauth",
        "gpt-5.6-luna", 1.0,
    )
    # This is the post-transform payload: Anthropic auto on Codex becomes
    # OpenAI priority even though the downstream fast_mode flag was false.
    ld.mark_retry_attempt_dispatch(aid, {"service_tier": "priority"})
    body = json.dumps({"usage": {"input_tokens": 100_000, "output_tokens": 100_000}})
    ld.finish_success(
        "outbound-tier", "oauth:openai:user@example.com", "oauth", "gpt-5.6-luna",
        input_tokens=100_000, output_tokens=100_000, response_body=body,
    )
    row = ld._get_conn().execute(
        "SELECT * FROM upstream_attempt_usage WHERE retry_attempt_id=?", (aid,)
    ).fetchone()
    request_row = ld._get_conn().execute(
        "SELECT actual_service_tier FROM request_log WHERE request_id='outbound-tier'"
    ).fetchone()
    expected = mp.estimate_cost(
        "gpt-5.6-luna", input_tokens=100_000, output_tokens=100_000, priority=True,
    )
    assert expected is not None
    assert row["outbound_service_tier"] == row["service_tier"] == "priority"
    # request_log keeps response-observed tier only; the immutable attempt row
    # separately proves the exact outbound tier used for this estimate.
    assert request_row["actual_service_tier"] is None
    assert row["cost_ticks"] == expected.total_ticks

    _pending(ld, "response-tier", fast=True)
    aid2 = ld.record_retry_attempt(
        "response-tier", 1, "api:openai", "api", "gpt-5.6-luna", 1.0,
    )
    ld.mark_retry_attempt_dispatch(aid2, {"service_tier": "priority"})
    default_body = json.dumps({
        "service_tier": "default",
        "usage": {"input_tokens": 100_000, "output_tokens": 100_000},
    })
    ld.finish_success(
        "response-tier", "api:openai", "api", "gpt-5.6-luna",
        input_tokens=100_000, output_tokens=100_000, response_body=default_body,
    )
    row2 = ld._get_conn().execute(
        "SELECT * FROM upstream_attempt_usage WHERE retry_attempt_id=?", (aid2,)
    ).fetchone()
    standard = mp.estimate_cost(
        "gpt-5.6-luna", input_tokens=100_000, output_tokens=100_000,
    )
    assert standard is not None
    assert row2["outbound_service_tier"] == "priority"
    assert row2["service_tier"] == "default"
    assert row2["cost_ticks"] == standard.total_ticks


def test_partial_attempt_schema_migration_is_idempotent(m):
    import sqlite3
    ld = m["log_db"]
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE request_log (
          id INTEGER PRIMARY KEY, request_id TEXT UNIQUE NOT NULL,
          created_at REAL NOT NULL, status TEXT,
          input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
          cache_creation_tokens INTEGER DEFAULT 0, cache_read_tokens INTEGER DEFAULT 0
        );
        CREATE TABLE retry_chain (
          id INTEGER PRIMARY KEY, request_id TEXT, attempt_order INTEGER,
          channel_key TEXT, channel_type TEXT, model TEXT, started_at REAL
        );
        CREATE TABLE upstream_attempt_usage (
          id INTEGER PRIMARY KEY, retry_attempt_id INTEGER, root_request_id TEXT
        );
    """)
    ld._ensure_migrations(conn)
    ld._ensure_migrations(conn)
    request_cols = {r[1] for r in conn.execute("PRAGMA table_info(request_log)")}
    attempt_cols = {r[1] for r in conn.execute("PRAGMA table_info(upstream_attempt_usage)")}
    retry_cols = {r[1] for r in conn.execute("PRAGMA table_info(retry_chain)")}
    assert {"usage_observed", "actual_service_tier"} <= request_cols
    assert {
        "call_request_id", "usage_observed", "service_tier",
        "outbound_service_tier", "dispatch_state", "pricing_snapshot_json",
        "pricing_version", "cost_source", "cost_ticks", "settled_at",
    } <= attempt_cols
    assert "outbound_service_tier" in retry_cols


def test_xai_cost_surfaces_share_actual_estimated_and_unpriced_state(m):
    _setup(m); ld = m["log_db"]; ui = m["ui"]
    channel = "oauth:xai:user@example.com"
    _pending(ld, "xai-consistent", "grok-4.5")
    aid = ld.record_retry_attempt("xai-consistent", 1, channel, "oauth", "grok-4.5", 1.0)
    body = json.dumps({
        "usage": {"input_tokens": 10, "output_tokens": 2, "cost_in_usd_ticks": 321}
    })
    ld.update_retry_attempt(aid, outcome="http_error", response_body=body, ended_at=2.0)
    ld.finish_error(
        "xai-consistent", "failed", final_channel_key=channel,
        final_channel_type="oauth", final_model="grok-4.5", response_body=body,
    )
    request_row = dict(ld._get_conn().execute(
        "SELECT * FROM request_log WHERE request_id='xai-consistent'"
    ).fetchone())
    per_log = ld.cost_for_log(request_row)
    per_account = ld.xai_cost_for_channel(channel, since_ts=0)
    global_cost = ld.stats_summary(0, summary_top_limit=5)["overall"]
    assert per_log["cost_ticks"] == per_account["cost_ticks"] == global_cost["cost_ticks"] == 321
    assert ui.fmt_cost(per_log) == ui.fmt_cost(per_account) == ui.fmt_cost(global_cost)


def test_unknown_xai_model_is_unpriced_not_zero(m):
    _setup(m); ld = m["log_db"]; ui = m["ui"]
    channel = "oauth:xai:user@example.com"
    _pending(ld, "xai-unknown", "grok-does-not-exist")
    aid = ld.record_retry_attempt(
        "xai-unknown", 1, channel, "oauth", "grok-does-not-exist", 1.0,
    )
    body = json.dumps({"usage": {"input_tokens": 10, "output_tokens": 2}})
    ld.update_retry_attempt(aid, outcome="http_error", response_body=body, ended_at=2.0)
    ld.finish_error(
        "xai-unknown", "failed", final_channel_key=channel,
        final_channel_type="oauth", final_model="grok-does-not-exist",
        response_body=body,
    )
    account = ld.xai_cost_for_channel(channel, since_ts=0)
    assert account["costed_success"] == 0
    assert account["unpriced_success"] == 1
    assert ui.fmt_cost(account) == "未计价（1 次）"
