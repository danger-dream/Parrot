from __future__ import annotations

import asyncio
import json
import math
import os as _os
import sqlite3
import sys as _sys
import time
from types import SimpleNamespace

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))
from src.tests import _isolation
_isolation.isolate()


def _import_modules():
    from src import config, failover, log_db, model_pricing
    from src.openai.channel.api_channel import OpenAIApiChannel
    from src.telegram import ui
    return {
        "config": config,
        "failover": failover,
        "log_db": log_db,
        "model_pricing": model_pricing,
        "OpenAIApiChannel": OpenAIApiChannel,
        "ui": ui,
    }


def _setup(m):
    pricing = dict(m["config"].DEFAULT_CONFIG["pricing"])
    pricing["channelProviders"] = {
        "api:a": "openai",
        "api:b": "openai",
        "api:c": "openai",
        "api:openai": "openai",
    }
    m["config"].get()["pricing"] = pricing
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
    assert wrapped.usage_observed is False
    assert wrapped.usage_invalid is True
    assert wrapped.actual_cost_ticks is None

    split_usage = mp.normalize_response_billing(
        'data: {"type":"message_start","message":{"usage":'
        '{"input_tokens":4,"cache_read_input_tokens":1}}}\n\n'
        'data: {"type":"message_delta","usage":{"output_tokens":2}}\n\n'
    )
    assert split_usage.usage_observed is True
    assert split_usage.usage_invalid is False
    assert (
        split_usage.input_tokens, split_usage.output_tokens,
        split_usage.cache_read_tokens,
    ) == (4, 2, 1)

    malformed_then_partial = mp.normalize_response_billing(
        'data: {"type":"message_start","message":{"usage":'
        '{"input_tokens":-1}}}\n\n'
        'data: {"type":"message_delta","usage":{"output_tokens":2}}\n\n'
    )
    assert malformed_then_partial.usage_observed is False
    assert malformed_then_partial.usage_invalid is True


def test_strict_normalization_rejects_corrupt_usage_without_zero_coercion(m):
    mp = m["model_pricing"]
    assert mp.normalize_response_billing({"usage": []}).usage_invalid is True
    for bad in (-1, 1.5, float("inf"), True, "not-a-number"):
        normalized = mp.normalize_response_billing({
            "usage": {"input_tokens": bad, "output_tokens": 1}
        })
        assert normalized.usage_observed is False
        assert normalized.usage_invalid is True

    impossible_cache = mp.normalize_response_billing({
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 1,
            "prompt_tokens_details": {"cached_tokens": 11},
        }
    })
    assert impossible_cache.usage_observed is False
    assert impossible_cache.usage_invalid is True

    malformed_cost = mp.normalize_response_billing({
        "type": "response.completed",
        "service_tier": 123,
        "usage": {
            "input_tokens": 1,
            "output_tokens": 1,
            "cost_in_usd_ticks": "bad",
        },
    })
    assert malformed_cost.usage_observed is False
    assert malformed_cost.usage_invalid is True
    assert malformed_cost.actual_cost_ticks is None

    # A later terminal usage object supersedes an earlier malformed event.
    recovered = mp.normalize_response_billing(
        'data: {"type":"response.output_text.delta","usage":'
        '{"input_tokens":-1}}\n\n'
        'data: {"type":"response.completed","response":{"usage":'
        '{"input_tokens":2,"output_tokens":3}}}\n\n'
    )
    assert recovered.usage_observed is True
    assert recovered.usage_invalid is False
    assert (recovered.input_tokens, recovered.output_tokens) == (2, 3)


def test_nonstream_restoration_failure_preserves_raw_billing_evidence(m, monkeypatch):
    from src.protocols import runtime as protocol_runtime

    raw = json.dumps({
        "type": "response.failed",
        "usage": {
            "input_tokens": 4,
            "output_tokens": 2,
            "cost_in_usd_ticks": 321,
        },
    }).encode()

    async def fail_restore(*args, **kwargs):
        raise RuntimeError("adapter exploded")

    monkeypatch.setattr(
        protocol_runtime.provider_registry, "restore_response_bytes", fail_restore,
    )
    prepared = asyncio.run(protocol_runtime.prepare_non_stream_response(
        SimpleNamespace(key="oauth:xai:test", protocol="openai-responses"),
        raw,
        dynamic_map=None,
        connect_ms=1,
        total_ms=2,
    ))

    assert prepared.error is not None
    assert prepared.error.outcome == "transform_error"
    assert prepared.error.full_response_text == raw.decode()
    billing = m["model_pricing"].normalize_response_billing(
        prepared.error.full_response_text,
    )
    assert billing.actual_cost_ticks == 321
    assert billing.input_tokens == 4
    assert billing.output_tokens == 2


def test_http_pre_header_cancellation_terminalizes_root_and_retry(m, monkeypatch):
    _setup(m)
    ch = m["OpenAIApiChannel"]({
        "name": "a", "type": "api", "baseUrl": "https://cancel.test",
        "apiKey": "secret", "protocol": "openai-responses",
        "models": [{"alias": "test-model", "real": "gpt-5.6-luna"}],
        "enabled": True,
    })
    rid = "cancel-before-headers"
    body = {"model": "test-model", "input": "hello", "stream": False}
    _pending(m["log_db"], rid, "test-model")
    retry = m["log_db"].record_retry_attempt(
        rid, 1, ch.key, ch.type, "gpt-5.6-luna", time.time(),
    )
    entered = asyncio.Event()

    async def blocking_open(**kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(
        m["failover"], "open_response_with_proxy_chain", blocking_open,
    )

    async def run_cancel():
        task = asyncio.create_task(m["failover"]._try_channel(
            ch, "gpt-5.6-luna", body,
            False, 0.0, time.time(), None, [], "k", "127.0.0.1",
            rid, 0, 0,
            ingress_protocol="responses",
            retry_attempt_id=retry,
            start_monotonic=time.monotonic(),
            attempt_start_monotonic=time.monotonic(),
        ))
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return
        raise AssertionError("cancelled HTTP attempt did not propagate cancellation")

    asyncio.run(run_cancel())
    detail = m["log_db"].log_detail(rid)
    assert detail["log"]["status"] == "cancelled"
    assert detail["log"]["http_status"] == 499
    assert detail["retry_chain"][0]["outcome"] == "cancelled"
    assert m["log_db"]._get_conn().execute(
        "SELECT COUNT(*) FROM upstream_attempt_usage WHERE root_request_id=?",
        (rid,),
    ).fetchone()[0] == 0


def test_http_body_cancellation_preserves_complete_usage_chunk(m, monkeypatch):
    _setup(m)
    ch = m["OpenAIApiChannel"]({
        "name": "a", "type": "api", "baseUrl": "https://cancel.test",
        "apiKey": "secret", "protocol": "openai-responses",
        "models": [{"alias": "test-model", "real": "gpt-5.6-luna"}],
        "enabled": True,
    })
    rid = "cancel-after-body-chunk"
    body = {"model": "test-model", "input": "hello", "stream": False}
    _pending(m["log_db"], rid, "test-model")
    retry = m["log_db"].record_retry_attempt(
        rid, 1, ch.key, ch.type, "gpt-5.6-luna", time.time(),
    )
    waiting = asyncio.Event()
    closed = []
    response_body = json.dumps({
        "type": "response.completed",
        "response": {
            "service_tier": "default",
            "usage": {
                "input_tokens": 5,
                "output_tokens": 1,
                "input_tokens_details": {"cached_tokens": 2},
            },
        },
    }).encode()

    class BlockingResponse:
        status_code = 200
        headers = {"content-type": "application/json"}

        async def _body(self):
            yield response_body
            waiting.set()
            await asyncio.Event().wait()

        def aiter_bytes(self):
            return self._body()

    class FakeContext:
        async def __aexit__(self, *args):
            closed.append(True)

    async def fake_open(**kwargs):
        m["log_db"].mark_retry_attempt_dispatch(
            kwargs["retry_attempt_id"], {"service_tier": "default"},
        )
        return SimpleNamespace(
            error=None,
            response=BlockingResponse(),
            connect_ms=7,
            timing=None,
            proxy_name=None,
            proxy_bytes={"up": 9, "down": len(response_body)},
            proxy_client=None,
            proxy_attempt_id=None,
            round_timeouts=None,
            ctx=FakeContext(),
        )

    monkeypatch.setattr(
        m["failover"], "open_response_with_proxy_chain", fake_open,
    )

    async def run_cancel():
        task = asyncio.create_task(m["failover"]._try_channel(
            ch, "gpt-5.6-luna", body,
            False, 0.0, time.time(), None, [], "k", "127.0.0.1",
            rid, 0, 0,
            ingress_protocol="responses",
            retry_attempt_id=retry,
            start_monotonic=time.monotonic(),
            attempt_start_monotonic=time.monotonic(),
        ))
        await asyncio.wait_for(waiting.wait(), timeout=1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return
        raise AssertionError("cancelled HTTP attempt did not propagate cancellation")

    asyncio.run(run_cancel())
    assert closed == [True]
    detail = m["log_db"].log_detail(rid)
    assert detail["log"]["status"] == "cancelled"
    assert "response.completed" in detail["detail"]["response_body"]
    attempt = m["log_db"]._get_conn().execute(
        "SELECT * FROM upstream_attempt_usage WHERE root_request_id=?",
        (rid,),
    ).fetchone()
    assert attempt is not None
    assert attempt["outcome"] == "cancelled"
    assert attempt["usage_observed"] == 1
    assert attempt["input_tokens"] == 3
    assert attempt["cache_read_tokens"] == 2
    assert attempt["output_tokens"] == 1
    assert attempt["cost_source"] == "estimated"


def test_nonfinite_values_isolate_only_bad_models(m):
    mp = m["model_pricing"]
    api = {"provider": {"models": {
        "bad-price": {"cost": {"input": float("inf"), "output": 1}},
        "bad-fast": {
            "cost": {"input": 1, "output": 2},
            "experimental": {"modes": {"fast": {"cost": {"input": "Infinity"}}}},
        },
        "bad-threshold": {
            "cost": {"input": 1, "output": 2, "tiers": [
                {"input": 2, "output": 4,
                 "tier": {"type": "context", "size": "NaN"}},
            ]},
        },
        "good": {"cost": {"input": 1, "output": 2}},
    }}}
    models = {f"provider/{name}": {} for name in api["provider"]["models"]}
    parsed = mp._parse_catalog(api, models)
    assert set(parsed) == {"provider/good"}
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
    assert estimate.total_ticks == 16 * mp.TICKS_PER_USD


def test_missing_usage_unpriced_but_explicit_zero_costs_zero(m):
    _setup(m); ld = m["log_db"]
    _pending(ld, "missing")
    a1 = ld.record_retry_attempt("missing", 1, "api:A", "api", "gpt-5.6-luna", 1.0)
    ld.mark_retry_attempt_dispatch(a1, {})
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


def test_partial_attempt_ledger_keeps_missing_dispatched_attempt_unpriced(m):
    _setup(m); ld = m["log_db"]
    _pending(ld, "partial-ledger")
    settled = ld.record_retry_attempt(
        "partial-ledger", 1, "api:A", "api", "gpt-5.6-luna", 1.0,
    )
    ld.update_retry_attempt(
        settled,
        outcome="http_error",
        ended_at=2.0,
        response_body=json.dumps({"usage": {"input_tokens": 10, "output_tokens": 1}}),
    )
    crashed = ld.record_retry_attempt(
        "partial-ledger", 2, "api:B", "api", "gpt-5.6-sol", 3.0,
    )
    ld.mark_retry_attempt_dispatch(crashed, {"model": "gpt-5.6-sol"})
    # Simulate process death after dispatch and later stale-request recovery.
    ld._get_conn().execute(
        """UPDATE request_log
           SET status='success', final_channel_key='api:B', final_model='gpt-5.6-sol',
               input_tokens=20, output_tokens=2, usage_observed=1
           WHERE request_id='partial-ledger'"""
    )
    ld._get_conn().commit()

    request_row = dict(ld._get_conn().execute(
        "SELECT * FROM request_log WHERE request_id='partial-ledger'"
    ).fetchone())
    request_cost = ld.cost_for_log(request_row)
    assert request_cost["costed_success"] == 1
    assert request_cost["unpriced_success"] == 1
    assert request_cost["cost_ticks"] > 0

    summary = ld.stats_summary(0)["overall"]
    assert summary["costed_success"] == 1
    assert summary["unpriced_success"] == 1
    assert (summary["total_input_tokens"], summary["total_output_tokens"]) == (30, 3)
    first_channel = ld.tokens_for_channel("api:A", 0)
    final_channel = ld.tokens_for_channel("api:B", 0)
    assert first_channel["costed_success"] == 1
    assert (first_channel["input"], first_channel["output"]) == (10, 1)
    assert final_channel["unpriced_success"] == 1
    assert (final_channel["input"], final_channel["output"]) == (20, 2)
    by_key = ld.tokens_for_apikey("k", 0)
    assert (by_key["costed_success"], by_key["unpriced_success"]) == (1, 1)
    assert (by_key["input"], by_key["output"]) == (30, 3)
    channel_models = {
        row["final_model"]: row for row in ld.channel_model_stats("api:B", 0)
    }
    assert channel_models["gpt-5.6-sol"]["unpriced_success"] == 1
    assert (
        channel_models["gpt-5.6-sol"]["input"],
        channel_models["gpt-5.6-sol"]["output"],
    ) == (20, 2)
    apikey_models = {
        row["final_model"]: row for row in ld.apikey_model_stats("k", 0)
    }
    assert set(apikey_models) == {"gpt-5.6-luna", "gpt-5.6-sol"}
    assert apikey_models["gpt-5.6-luna"]["costed_success"] == 1
    assert (
        apikey_models["gpt-5.6-luna"]["input"],
        apikey_models["gpt-5.6-luna"]["output"],
    ) == (10, 1)
    assert apikey_models["gpt-5.6-sol"]["unpriced_success"] == 1
    assert (
        apikey_models["gpt-5.6-sol"]["input"],
        apikey_models["gpt-5.6-sol"]["output"],
    ) == (20, 2)


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
        "SELECT service_tier,cost_ticks FROM upstream_attempt_usage WHERE retry_attempt_id=?", (aid.row_id,)
    ).fetchone()
    req = ld._get_conn().execute(
        "SELECT actual_service_tier FROM request_log WHERE request_id='tier'"
    ).fetchone()
    assert row["service_tier"] == req["actual_service_tier"] == "default"
    standard = mp.estimate_cost(
        "openai/gpt-5.6-luna", input_tokens=100_000, output_tokens=100_000,
    )
    fast = mp.estimate_cost(
        "openai/gpt-5.6-luna", input_tokens=100_000, output_tokens=100_000,
        priority=True,
    )
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
        "FROM upstream_attempt_usage WHERE retry_attempt_id=?", (aid.row_id,),
    ).fetchone()
    fast = mp.estimate_cost(
        "openai/gpt-5.6-luna", input_tokens=100_000, output_tokens=100_000,
        priority=True,
    )
    assert fast is not None
    assert row["service_tier"] == row["outbound_service_tier"] == "priority"
    assert row["cost_ticks"] == fast.total_ticks


def test_unknown_or_unpriced_upstream_service_tier_is_not_standard_priced(m):
    _setup(m); ld = m["log_db"]
    _pending(ld, "unknown-tier")
    aid = ld.record_retry_attempt(
        "unknown-tier", 1, "api:openai", "api", "gpt-5.6-luna", 1.0,
    )
    ld.mark_retry_attempt_dispatch(aid, {"service_tier": "flex"})
    body = json.dumps({
        "service_tier": "flex",
        "usage": {"input_tokens": 100_000, "output_tokens": 100_000},
    })
    ld.finish_success(
        "unknown-tier", "api:openai", "api", "gpt-5.6-luna",
        input_tokens=100_000, output_tokens=100_000, response_body=body,
    )
    ld.update_retry_attempt(aid, outcome="success", ended_at=2.0)
    row = ld._get_conn().execute(
        "SELECT service_tier,cost_source,cost_ticks "
        "FROM upstream_attempt_usage WHERE retry_attempt_id=?", (aid.row_id,),
    ).fetchone()
    assert tuple(row) == ("flex", "unpriced", None)
    request = dict(ld._get_conn().execute(
        "SELECT * FROM request_log WHERE request_id='unknown-tier'"
    ).fetchone())
    assert ld.cost_for_log(request)["unpriced_success"] == 1


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
        "SELECT * FROM upstream_attempt_usage WHERE retry_attempt_id=?", (aid.row_id,)
    ).fetchone()
    assert row["cost_source"] == "actual" and row["cost_ticks"] == 321
    assert row["pricing_model"] == "xai/grok-4.5"
    assert row["usage_observed"] == 0
    assert row["pricing_version"] == "upstream-actual-v1"


def test_legacy_xai_error_response_without_actual_cost_is_explicitly_unpriced(m):
    _setup(m); ld = m["log_db"]
    channel = "oauth:xai:user@example.com"
    _pending(ld, "xai-legacy-no-cost", "grok-4.5")
    body = json.dumps({
        "type": "response.failed",
        "usage": {"input_tokens": 10, "output_tokens": 2},
    })
    ld.finish_error(
        "xai-legacy-no-cost", "failed",
        final_channel_key=channel,
        final_channel_type="oauth",
        final_model="grok-4.5",
        response_body=body,
    )

    account = ld.xai_cost_for_channel(channel, since_ts=0)
    summary = ld.stats_summary(0)["overall"]
    assert account["costed_success"] == 0
    assert account["unpriced_success"] == 1
    assert summary["unpriced_success"] == 1


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
        "WHERE retry_attempt_id=?", (aid.row_id,),
    ).fetchone()
    assert tuple(row) == ("unknown", "unpriced", None)
    assert ld._get_conn().execute(
        "SELECT COUNT(*) FROM upstream_attempt_usage WHERE retry_attempt_id=?", (aid.row_id,),
    ).fetchone()[0] == 1


def test_explicit_observed_flag_cannot_turn_malformed_usage_into_zero_cost(m):
    _setup(m); ld = m["log_db"]
    _pending(ld, "bad-usage")
    aid = ld.record_retry_attempt(
        "bad-usage", 1, "api:A", "api", "gpt-5.6-luna", 1.0,
    )
    ld.mark_retry_attempt_dispatch(aid, {})
    assert ld.settle_retry_attempt(
        aid,
        outcome="success",
        usage={"input_tokens": "broken", "output_tokens": 0},
        usage_observed=True,
    ) is True
    row = ld._get_conn().execute(
        "SELECT usage_observed,cost_source,cost_ticks "
        "FROM upstream_attempt_usage WHERE retry_attempt_id=?", (aid.row_id,),
    ).fetchone()
    assert tuple(row) == (0, "unpriced", None)


def test_partial_usage_body_cannot_be_completed_by_default_zero_runtime_dict(m):
    _setup(m); ld = m["log_db"]
    _pending(ld, "partial-usage-body")
    aid = ld.record_retry_attempt(
        "partial-usage-body", 1, "api:A", "api", "gpt-5.6-luna", 1.0,
    )
    ld.mark_retry_attempt_dispatch(aid, {})
    ld.update_retry_attempt(
        aid,
        outcome="upstream_error_json",
        response_body=json.dumps({"usage": {"output_tokens": 7}}),
        usage={
            "input_tokens": 0, "output_tokens": 7,
            "cache_creation": 0, "cache_read": 0,
        },
        usage_observed=True,
        ended_at=2.0,
    )
    row = ld._get_conn().execute(
        "SELECT usage_observed,input_tokens,output_tokens,cost_source,cost_ticks "
        "FROM upstream_attempt_usage WHERE retry_attempt_id=?", (aid.row_id,),
    ).fetchone()
    assert tuple(row) == (0, 0, 0, "unpriced", None)


def test_pre_dispatch_guard_does_not_create_billing_fact(m):
    _setup(m); ld = m["log_db"]
    _pending(ld, "guard")
    aid = ld.record_retry_attempt("guard", 1, "api:A", "api", "gpt-5.6-luna", 1.0)
    ld.update_retry_attempt(aid, outcome="transform_error", ended_at=2.0)
    ld.finish_error(
        "guard", "transform failed", final_channel_key="api:A",
        final_channel_type="api", final_model="gpt-5.6-luna",
    )
    assert ld._get_conn().execute(
        "SELECT COUNT(*) FROM upstream_attempt_usage WHERE retry_attempt_id=?", (aid.row_id,),
    ).fetchone()[0] == 0

    _pending(ld, "cancelled-after-wire")
    sent = ld.record_retry_attempt(
        "cancelled-after-wire", 1, "api:A", "api", "gpt-5.6-luna", 1.0,
    )
    ld.update_retry_attempt(
        sent, outcome="cancelled", bytes_up=64, ended_at=2.0,
    )
    sent_row = ld._get_conn().execute(
        "SELECT dispatch_state,cost_source FROM upstream_attempt_usage "
        "WHERE retry_attempt_id=?", (sent.row_id,),
    ).fetchone()
    assert tuple(sent_row) == ("sent", "unpriced")


def test_response_transform_failure_after_dispatch_preserves_billing_fact(m):
    _setup(m); ld = m["log_db"]
    _pending(ld, "post-dispatch-transform")
    aid = ld.record_retry_attempt(
        "post-dispatch-transform", 1, "api:A", "api", "gpt-5.6-luna", 1.0,
    )
    ld.mark_retry_attempt_dispatch(aid, {})
    ld.update_retry_attempt(
        aid,
        outcome="transform_error",
        ended_at=2.0,
        response_body=json.dumps({
            "usage": {"input_tokens": 5, "output_tokens": 2},
        }),
    )
    row = ld._get_conn().execute(
        "SELECT dispatch_state,usage_observed,cost_source,cost_ticks "
        "FROM upstream_attempt_usage WHERE retry_attempt_id=?", (aid.row_id,),
    ).fetchone()
    assert row["dispatch_state"] == "sent"
    assert row["usage_observed"] == 1
    assert row["cost_source"] == "estimated"
    assert row["cost_ticks"] > 0


def test_known_connect_failure_before_dispatch_is_not_an_unpriced_bill(m):
    _setup(m); ld = m["log_db"]
    _pending(ld, "connect-failed")
    aid = ld.record_retry_attempt(
        "connect-failed", 1, "api:A", "api", "gpt-5.6-luna", 1.0,
    )
    ld.update_retry_attempt(aid, outcome="connect_error", ended_at=2.0)
    ld.finish_error(
        "connect-failed", "connect failed", final_channel_key="api:A",
        final_channel_type="api", final_model="gpt-5.6-luna",
    )
    assert ld._get_conn().execute(
        "SELECT COUNT(*) FROM upstream_attempt_usage WHERE retry_attempt_id=?",
        (aid.row_id,),
    ).fetchone()[0] == 0


def test_anthropic_outbound_speed_fast_freezes_models_dev_fast_tariff(m):
    _setup(m); ld = m["log_db"]
    _pending(ld, "claude-fast", "claude-opus-4-6")
    aid = ld.record_retry_attempt(
        "claude-fast", 1, "oauth:anthropic:user@example.com", "oauth",
        "claude-opus-4-6", 1.0,
    )
    ld.mark_retry_attempt_dispatch(aid, json.dumps({"speed": "fast"}))
    body = json.dumps({"usage": {"input_tokens": 100, "output_tokens": 10}})
    ld.finish_success(
        "claude-fast", "oauth:anthropic:user@example.com", "oauth",
        "claude-opus-4-6", input_tokens=100, output_tokens=10,
        response_body=body,
    )
    ld.update_retry_attempt(
        aid, outcome="success", response_body=body, ended_at=2.0,
    )
    row = ld._get_conn().execute(
        """SELECT service_tier, outbound_service_tier, dispatch_state,
                  cost_source, cost_ticks
           FROM upstream_attempt_usage WHERE retry_attempt_id=?""",
        (aid.row_id,),
    ).fetchone()
    expected = m["model_pricing"].estimate_cost(
        "anthropic/claude-opus-4-6",
        input_tokens=100,
        output_tokens=10,
        priority=True,
        pricing_settings=m["model_pricing"].settings({"pricing": {"enabled": True}}),
    )
    assert expected is not None
    assert tuple(row[:4]) == ("fast", "fast", "sent", "estimated")
    assert row["cost_ticks"] == expected.total_ticks


def test_anthropic_response_service_tier_does_not_override_fast_speed(m):
    _setup(m); ld = m["log_db"]; mp = m["model_pricing"]
    channel = "oauth:anthropic:user@example.com"
    _pending(ld, "claude-fast-tier", "claude-opus-5")
    aid = ld.record_retry_attempt(
        "claude-fast-tier", 1, channel, "oauth", "claude-opus-5", 1.0,
        upstream_protocol="anthropic",
    )
    # These fields are orthogonal in Anthropic's request schema.  speed=fast
    # selects the premium token tariff; service_tier=auto must not erase it.
    ld.mark_retry_attempt_dispatch(
        aid, {"service_tier": "auto", "speed": "fast"},
    )
    body = json.dumps({
        "service_tier": "standard",
        "usage": {"input_tokens": 100_000, "output_tokens": 100_000},
    })
    ld.finish_success(
        "claude-fast-tier", channel, "oauth", "claude-opus-5",
        input_tokens=100_000, output_tokens=100_000,
        response_body=body, upstream_protocol="anthropic",
    )
    row = ld._get_conn().execute(
        "SELECT service_tier,outbound_service_tier,upstream_protocol,"
        "cost_source,cost_ticks FROM upstream_attempt_usage "
        "WHERE retry_attempt_id=?", (aid.row_id,),
    ).fetchone()
    expected = mp.estimate_cost(
        "anthropic/claude-opus-5",
        input_tokens=100_000, output_tokens=100_000, priority=True,
    )
    standard = mp.estimate_cost(
        "anthropic/claude-opus-5",
        input_tokens=100_000, output_tokens=100_000, priority=False,
    )
    assert expected is not None and standard is not None
    assert ld._outbound_service_tier(
        {"service_tier": "auto", "speed": "fast"},
        upstream_protocol="anthropic",
    ) == "fast"
    assert ld._outbound_service_tier(
        {"speed": "fast"}
    ) is None
    assert ld._outbound_service_tier(
        {"service_tier": "auto", "speed": "fast"},
        upstream_protocol="openai-responses",
    ) == "auto"
    assert tuple(row[:4]) == (
        "standard", "fast", "anthropic", "estimated",
    )
    assert row["cost_ticks"] == expected.total_ticks
    assert row["cost_ticks"] != standard.total_ticks


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
    ld.finish_success(
        "compact", "compact-rescue", "internal", "gpt-5.6-sol",
        input_tokens=50, output_tokens=5,
        response_body=json.dumps({
            "usage": {"input_tokens": 50, "output_tokens": 5},
        }),
        usage_observed=True,
    )

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
        "SELECT cost_ticks FROM upstream_attempt_usage WHERE retry_attempt_id=?", (a1.row_id,)
    ).fetchone()
    b_row = ld._get_conn().execute(
        "SELECT cost_ticks FROM upstream_attempt_usage WHERE retry_attempt_id=?", (a2.row_id,)
    ).fetchone()
    assert a_cost["cost_ticks"] == a_row["cost_ticks"]
    assert b_cost["cost_ticks"] == b_row["cost_ticks"]


def test_cross_protocol_attempts_keep_family_and_request_dimensions_separate(m):
    _setup(m); ld = m["log_db"]; ui = m["ui"]
    _pending(ld, "cross-family", "gpt-5.6-sol")
    anthropic_channel = "oauth:anthropic:user@example.com"
    first = ld.record_retry_attempt(
        "cross-family", 1, anthropic_channel, "oauth", "claude-opus-5", 1.0,
        upstream_protocol="anthropic",
    )
    ld.mark_retry_attempt_dispatch(first, {})
    ld.update_retry_attempt(
        first, outcome="http_error", ended_at=2.0,
        response_body=json.dumps({
            "usage": {"input_tokens": 100, "output_tokens": 10},
        }),
    )
    second = ld.record_retry_attempt(
        "cross-family", 2, "api:B", "api", "gpt-5.6-sol", 3.0,
        upstream_protocol="openai-responses",
    )
    ld.mark_retry_attempt_dispatch(second, {})
    final_body = json.dumps({
        "usage": {"input_tokens": 200, "output_tokens": 20},
    })
    ld.finish_success(
        "cross-family", "api:B", "api", "gpt-5.6-sol",
        input_tokens=200, output_tokens=20, response_body=final_body,
        retry_count=1, upstream_protocol="openai-responses",
    )

    anthropic = ld.stats_summary(
        0, family="anthropic", summary_top_limit=20,
    )["overall"]
    openai = ld.stats_summary(
        0, family="openai", summary_top_limit=20,
    )["overall"]
    assert (anthropic["total"], anthropic["total_input_tokens"],
            anthropic["total_output_tokens"], anthropic["costed_success"]) == (
        0, 100, 10, 1,
    )
    assert (openai["total"], openai["total_input_tokens"],
            openai["total_output_tokens"], openai["costed_success"]) == (
        1, 200, 20, 1,
    )

    # Downstream request counts remain final-route facts.  Billing text makes
    # the one concrete upstream call explicit instead of showing a bare
    # "0 requests + positive cost" contradiction.
    first_channel = ld.tokens_for_channel(anthropic_channel, 0)
    assert first_channel["total"] == 0
    assert first_channel["costed_success"] == 1
    assert "上游 1 次" in ui.fmt_cost(first_channel)
    assert "上游 1 次" in ui.fmt_cost(anthropic)
    from src.telegram.menus import stats_menu
    model_channels = ld.channels_by_requested_model(0)
    channel_facts = {
        (item["key"], item["upstream_protocol"])
        for item in model_channels["gpt-5.6-sol"]
    }
    assert channel_facts == {
        (anthropic_channel, "anthropic"),
        ("api:B", "openai-responses"),
    }
    family_block = stats_menu._section_family(
        "anthropic",
        ld.stats_summary(0, family="anthropic", summary_top_limit=20),
        model_channels=model_channels,
    )
    assert "请求 0" in family_block
    assert "上游 1 次" in family_block
    assert "<code>B</code>" not in family_block
    request_row = dict(ld._get_conn().execute(
        "SELECT * FROM request_log WHERE request_id='cross-family'"
    ).fetchone())
    assert "上游 2 次" in ui.fmt_cost_from_row(request_row)


def test_attempt_model_token_and_cost_breakdowns_use_the_same_attempt_keys(m):
    _setup(m); ld = m["log_db"]
    _pending(ld, "model-split", "gpt-5.6-luna")
    first = ld.record_retry_attempt(
        "model-split", 1, "api:A", "api", "gpt-5.6-luna", 1.0,
    )
    ld.update_retry_attempt(
        first, outcome="http_error", ended_at=2.0,
        response_body=json.dumps({"usage": {"input_tokens": 10, "output_tokens": 1}}),
    )
    second = ld.record_retry_attempt(
        "model-split", 2, "api:B", "api", "gpt-5.6-sol", 3.0,
    )
    final_body = json.dumps({"usage": {"input_tokens": 20, "output_tokens": 2}})
    ld.finish_success(
        "model-split", "api:B", "api", "gpt-5.6-sol",
        input_tokens=20, output_tokens=2, response_body=final_body, retry_count=1,
    )
    ld.update_retry_attempt(second, outcome="success", ended_at=4.0)

    channel_a = {row["final_model"]: row for row in ld.channel_model_stats("api:A", 0)}
    channel_b = {row["final_model"]: row for row in ld.channel_model_stats("api:B", 0)}
    by_apikey = {row["final_model"]: row for row in ld.apikey_model_stats("k", 0)}
    assert (channel_a["gpt-5.6-luna"]["input"], channel_a["gpt-5.6-luna"]["output"]) == (10, 1)
    assert channel_a["gpt-5.6-luna"]["costed_success"] == 1
    assert (channel_b["gpt-5.6-sol"]["input"], channel_b["gpt-5.6-sol"]["output"]) == (20, 2)
    assert channel_b["gpt-5.6-sol"]["costed_success"] == 1
    # Token and cost dimensions use the same concrete attempt model even when
    # the final successful route resolved to a different model.
    assert set(by_apikey) == {"gpt-5.6-luna", "gpt-5.6-sol"}
    assert (by_apikey["gpt-5.6-luna"]["input"], by_apikey["gpt-5.6-luna"]["output"]) == (10, 1)
    assert by_apikey["gpt-5.6-luna"]["costed_success"] == 1
    assert (by_apikey["gpt-5.6-sol"]["input"], by_apikey["gpt-5.6-sol"]["output"]) == (20, 2)
    assert by_apikey["gpt-5.6-sol"]["costed_success"] == 1


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
        "SELECT * FROM upstream_attempt_usage WHERE retry_attempt_id=?", (aid.row_id,)
    ).fetchone()
    request_row = ld._get_conn().execute(
        "SELECT actual_service_tier FROM request_log WHERE request_id='outbound-tier'"
    ).fetchone()
    expected = mp.estimate_cost(
        "openai/gpt-5.6-luna", input_tokens=100_000,
        output_tokens=100_000, priority=True,
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
        "SELECT * FROM upstream_attempt_usage WHERE retry_attempt_id=?", (aid2.row_id,)
    ).fetchone()
    standard = mp.estimate_cost(
        "openai/gpt-5.6-luna", input_tokens=100_000, output_tokens=100_000,
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
        "upstream_protocol", "pricing_version", "cost_source", "cost_ticks",
        "settled_at",
    } <= attempt_cols
    assert {"outbound_service_tier", "upstream_protocol"} <= retry_cols


def test_migrated_attempt_without_call_identity_does_not_double_count_tokens(m):
    _setup(m); ld = m["log_db"]
    _pending(ld, "legacy-attempt-identity")
    ld._get_conn().execute(
        """UPDATE request_log
           SET status='success', final_channel_key='api:B',
               final_channel_type='api', final_model='gpt-5.6-sol',
               input_tokens=20, output_tokens=2, usage_observed=1
           WHERE request_id='legacy-attempt-identity'"""
    )
    ld._get_conn().execute(
        """INSERT INTO upstream_attempt_usage
           (retry_attempt_id,root_request_id,call_request_id,attempt_order,
            channel_key,channel_type,model,outcome,usage_observed,input_tokens,
            output_tokens,cache_creation_tokens,cache_read_tokens,
            dispatch_state,cost_source,settled_at)
           VALUES (999,'legacy-attempt-identity','',1,'api:A','api',
                   'gpt-5.6-luna','success',1,10,1,0,0,'sent','unpriced',1)"""
    )
    ld._get_conn().commit()

    overall = ld.stats_summary(0, summary_top_limit=0)["overall"]
    assert (overall["total_input_tokens"], overall["total_output_tokens"]) == (20, 2)


def test_real_month_migration_repairs_partial_attempt_schema_before_indexes(
    m, tmp_path, monkeypatch,
):
    import sqlite3
    ld = m["log_db"]
    monkeypatch.setattr(ld, "_log_dir", str(tmp_path))
    path = tmp_path / "2026-06.db"
    conn = sqlite3.connect(path)
    conn.executescript(ld._schema_sql())
    conn.executescript("""
        DROP INDEX idx_attempt_usage_root;
        DROP INDEX idx_attempt_usage_channel;
        DROP INDEX idx_attempt_usage_model;
        DROP TABLE upstream_attempt_usage;
        CREATE TABLE upstream_attempt_usage (
          id INTEGER PRIMARY KEY,
          retry_attempt_id INTEGER,
          root_request_id TEXT
        );
    """)
    conn.commit()
    conn.close()

    ld.migrate_month_schema("2026-06")
    ld.migrate_month_schema("2026-06")
    conn = sqlite3.connect(path)
    try:
        cols = {row[1] for row in conn.execute(
            "PRAGMA table_info(upstream_attempt_usage)"
        )}
        indexes = {row[1] for row in conn.execute(
            "PRAGMA index_list(upstream_attempt_usage)"
        )}
    finally:
        conn.close()
    assert {"channel_key", "model", "upstream_protocol", "cost_ticks"} <= cols
    assert {
        "idx_attempt_usage_root", "idx_attempt_usage_channel",
        "idx_attempt_usage_model", "idx_attempt_usage_retry",
    } <= indexes


def test_family_stats_skip_read_only_pre_protocol_month_without_crashing(m):
    _setup(m); ld = m["log_db"]
    _pending(ld, "pre-protocol-month")
    aid = ld.record_retry_attempt(
        "pre-protocol-month", 1, "api:A", "api", "gpt-5.6-luna", 1.0,
        upstream_protocol="openai-responses",
    )
    ld.mark_retry_attempt_dispatch(aid, {})
    ld.update_retry_attempt(
        aid, outcome="success", ended_at=2.0,
        response_body=json.dumps({
            "service_tier": "default",
            "usage": {"input_tokens": 4, "output_tokens": 2},
        }),
    )
    ld.finish_success(
        "pre-protocol-month", "api:A", "api", "gpt-5.6-luna",
        input_tokens=4, output_tokens=2, usage_observed=True,
        upstream_protocol="openai-responses",
    )

    current = ld._get_conn()
    historical_path = _os.path.join(ld._log_dir, "1999-01.db")
    historical = sqlite3.connect(historical_path)
    current.backup(historical)
    historical.execute("ALTER TABLE request_log DROP COLUMN upstream_protocol")
    historical.execute("ALTER TABLE retry_chain DROP COLUMN upstream_protocol")
    historical.execute(
        "ALTER TABLE upstream_attempt_usage DROP COLUMN upstream_protocol"
    )
    historical.commit()
    historical.close()
    for table in (
        "upstream_attempt_usage", "retry_chain", "request_detail", "request_log",
    ):
        current.execute(f"DELETE FROM {table}")
    current.commit()

    try:
        result = ld.stats_summary(0, family="openai", summary_top_limit=20)
        assert result["overall"]["total"] == 0
        assert result["overall"]["costed_success"] == 0
        assert result["overall"]["unpriced_success"] == 0
    finally:
        _os.unlink(historical_path)


def test_stats_many_model_channel_pairs_do_not_build_unbounded_sql_cases(
    m, monkeypatch,
):
    _setup(m); ld = m["log_db"]; mp = m["model_pricing"]
    monkeypatch.setattr(mp, "long_context_threshold", lambda *args, **kwargs: 1)
    monkeypatch.setattr(
        mp, "has_ambiguous_cache_write_ttl", lambda *args, **kwargs: True,
    )
    rows = [
        (
            f"many-pairs-{i}", 1.0, "k", f"model-{i}", f"api:channel-{i}",
            "api", f"model-{i}", "success", 2, 1, 1, 0, 1,
        )
        for i in range(1_100)
    ]
    conn = ld._get_conn()
    conn.executemany(
        """INSERT INTO request_log
           (request_id,created_at,api_key_name,requested_model,
            final_channel_key,final_channel_type,final_model,status,
            input_tokens,output_tokens,cache_creation_tokens,cache_read_tokens,
            usage_observed)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    conn.commit()

    overall = ld.stats_summary(0, summary_top_limit=0)["overall"]
    assert overall["total"] == 1_100
    assert overall["costed_success"] == 0
    assert overall["unpriced_success"] == 1_100


def test_legacy_fast_and_failed_rows_fail_closed_instead_of_showing_zero(m):
    _setup(m); ld = m["log_db"]; ui = m["ui"]; mp = m["model_pricing"]
    _pending(ld, "legacy-fast-unknown", fast=True)
    fast_body = json.dumps({
        "usage": {"input_tokens": 100_000, "output_tokens": 100_000},
    })
    ld.finish_success(
        "legacy-fast-unknown", "api:openai", "api", "gpt-5.6-luna",
        input_tokens=100_000, output_tokens=100_000,
        response_body=fast_body, usage_observed=True,
        upstream_protocol="openai-responses",
    )
    _pending(ld, "legacy-failed", "gpt-5.6-luna")
    ld.finish_error(
        "legacy-failed", "upstream failed after dispatch",
        final_channel_key="api:A", final_channel_type="api",
        final_model="gpt-5.6-luna", upstream_protocol="openai-responses",
    )
    _pending(ld, "legacy-anthropic-capacity", "claude-opus-5")
    anthropic_body = json.dumps({
        "service_tier": "priority",
        "usage": {"input_tokens": 100_000, "output_tokens": 100_000},
    })
    ld.finish_success(
        "legacy-anthropic-capacity", "oauth:anthropic:user@example.com",
        "oauth", "claude-opus-5", input_tokens=100_000,
        output_tokens=100_000, response_body=anthropic_body,
        usage_observed=True, upstream_protocol="anthropic",
    )
    _pending(ld, "legacy-unknown-priority", "gpt-5.6-luna")
    unknown_body = json.dumps({
        "service_tier": "priority",
        "usage": {"input_tokens": 100_000, "output_tokens": 100_000},
    })
    ld.finish_success(
        "legacy-unknown-priority", "api:openai", "api", "gpt-5.6-luna",
        input_tokens=100_000, output_tokens=100_000,
        response_body=unknown_body, usage_observed=True,
    )

    fast_row = dict(ld._get_conn().execute(
        "SELECT * FROM request_log WHERE request_id='legacy-fast-unknown'"
    ).fetchone())
    failed_row = dict(ld._get_conn().execute(
        "SELECT * FROM request_log WHERE request_id='legacy-failed'"
    ).fetchone())
    anthropic_row = dict(ld._get_conn().execute(
        "SELECT * FROM request_log "
        "WHERE request_id='legacy-anthropic-capacity'"
    ).fetchone())
    unknown_row = dict(ld._get_conn().execute(
        "SELECT * FROM request_log "
        "WHERE request_id='legacy-unknown-priority'"
    ).fetchone())
    fast_cost = ld.cost_for_log(fast_row)
    failed_cost = ld.cost_for_log(failed_row)
    anthropic_cost = ld.cost_for_log(anthropic_row)
    unknown_cost = ld.cost_for_log(unknown_row)
    assert (fast_cost["costed_success"], fast_cost["unpriced_success"]) == (0, 1)
    assert (failed_cost["costed_success"], failed_cost["unpriced_success"]) == (0, 1)
    assert ui.fmt_cost(fast_cost) == "未计价（1 次）"
    assert ui.fmt_cost(failed_cost) == "未计价（1 次）"
    standard = mp.estimate_cost(
        "anthropic/claude-opus-5", input_tokens=100_000,
        output_tokens=100_000, priority=False,
    )
    fast = mp.estimate_cost(
        "anthropic/claude-opus-5", input_tokens=100_000,
        output_tokens=100_000, priority=True,
    )
    assert standard is not None and fast is not None
    assert anthropic_cost["cost_ticks"] == standard.total_ticks
    assert anthropic_cost["cost_ticks"] != fast.total_ticks
    assert (unknown_cost["costed_success"], unknown_cost["unpriced_success"]) == (0, 1)

    overall = ld.stats_summary(0, summary_top_limit=0)["overall"]
    assert overall["costed_success"] == 1
    assert overall["unpriced_success"] == 3


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
    assert "计费: 💵 实际 $0.00" in ui.fmt_log_entry_body(request_row)
    from src.telegram.menus import logs_menu
    assert "<b>计费</b>\n💵 实际 $0.00" in logs_menu._render_detail(
        ld.log_detail("xai-consistent")
    )


def test_xai_actual_cost_requires_provider_qualified_route(m):
    _setup(m); ld = m["log_db"]
    pricing = dict(m["config"].get()["pricing"])
    pricing["channelProviders"] = dict(pricing.get("channelProviders") or {})
    pricing["channelProviders"]["api:vendor"] = "xai"
    m["config"].get()["pricing"] = pricing

    _pending(ld, "xai-mapped", "grok-4.5")
    mapped = ld.record_retry_attempt(
        "xai-mapped", 1, "api:vendor", "api", "grok-4.5", 1.0,
    )
    mapped_body = json.dumps({
        "usage": {"input_tokens": 10, "output_tokens": 2,
                  "cost_in_usd_ticks": 321}
    })
    ld.mark_retry_attempt_dispatch(mapped, {})
    ld.update_retry_attempt(
        mapped, outcome="http_error", response_body=mapped_body, ended_at=2.0,
    )
    mapped_row = ld._get_conn().execute(
        "SELECT pricing_model,cost_source,cost_ticks FROM upstream_attempt_usage "
        "WHERE retry_attempt_id=?", (mapped.row_id,),
    ).fetchone()
    assert tuple(mapped_row) == ("xai/grok-4.5", "actual", 321)

    _pending(ld, "xai-name-only", "grok-4.5")
    name_only = ld.record_retry_attempt(
        "xai-name-only", 1, "api:xai:unproven", "api", "grok-4.5", 1.0,
    )
    ld.mark_retry_attempt_dispatch(name_only, {})
    ld.update_retry_attempt(
        name_only, outcome="http_error", response_body=mapped_body, ended_at=2.0,
    )
    name_row = ld._get_conn().execute(
        "SELECT pricing_model,cost_source,cost_ticks FROM upstream_attempt_usage "
        "WHERE retry_attempt_id=?", (name_only.row_id,),
    ).fetchone()
    assert name_row["pricing_model"] == "grok-4.5"
    assert name_row["cost_source"] == "unpriced"
    assert name_row["cost_ticks"] is None


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
