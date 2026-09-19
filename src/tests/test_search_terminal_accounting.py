"""S1-S3: logical search finalization and reported Exa fees.

Real failover/search execution and isolated monthly SQLite; only HTTP transports
are fake. These regressions supersede the read-only audit probes.
"""
from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
import time

import httpx
import pytest

from src import log_db, local_web_tools as web, search_service as service
from src.management_control.search import SearchControl
from src.tests import test_protocol_fake_upstreams as fx
from src.tests.search_stream_fixtures import wire, decode, text_delta
from src.tests.test_search_management import ctx  # noqa: F401
from src.tests.test_search_service_accounting import env  # noqa: F401
from src.tests.test_search_month_handles import (  # noqa: F401
    monthly, setup_route, output, rows, FEB,
)


def model_response(req, obj, protocol):
    if json.loads(req.content).get("stream"):
        return httpx.Response(200, content=wire(obj, protocol), headers={"content-type": "text/event-stream"})
    return httpx.Response(200, json=obj)


def logged_response(request):
    with sqlite3.connect(request.db.path) as conn:
        return conn.execute("SELECT response_body FROM request_detail WHERE request_id=?",
                            (request.request_id,)).fetchone()[0]


def search_cfg(kind="tavily"):
    return {**service.DEFAULTS, "maxAttempts": 1, "backends": [
        {**service.default_backend(kind), "apiKeys": ["isolated-key"]}]}


def priced_route(monthly, protocol, monkeypatch):
    monkeypatch.setitem(monthly.m["config"].get(), "pricing", {"enabled": True})
    monkeypatch.setitem(monthly.m["config"].get(), "modelBindings", {
        "defaults": {"test-model": {"target": "openai/gpt-5.6-luna", "source": "test"}},
        "scoped": {},
    })
    body, _, request, sentinel, retry = setup_route(monthly, protocol, False)
    channel = fx._make_openai_channel("month", "https://api.openai.com/v1",
        protocol="openai-" + protocol, alias="test-model", real="gpt-5.6-luna")
    fx._install_channels(monthly.m, [channel])
    route = monthly.m["scheduler"].schedule(body, api_key_name="test-key",
        client_ip="127.0.0.1", ingress_protocol=protocol)
    assert route.candidates
    return body, route, request, sentinel, retry


def invoke_request(monthly, body, route, request, *, streaming=False, protocol="responses"):
    return monthly.m["failover"].run_failover(
        route, body, request.request_id, "test-key", "127.0.0.1", streaming,
        time.time(), ingress_protocol=protocol, start_monotonic=time.monotonic())


@pytest.mark.parametrize("streaming", [False, True])
async def test_cancel_during_real_search_settles_all_log_layers(monthly, monkeypatch, streaming):
    body, route, request, sentinel, _ = priced_route(monthly, "responses", monkeypatch)
    untouched = rows(sentinel.db.path, "request_log", sentinel.request_id)
    entered, cancelled = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(service, "settings", lambda: search_cfg())
    monkeypatch.setattr(log_db, "_store_log_bodies", lambda: True)

    async def search_http(req):
        monthly.clock[0] = FEB
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(service.network, "async_client", lambda **kw: httpx.AsyncClient(
        transport=httpx.MockTransport(search_http)))
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: model_response(req, output("responses", "s1"), "responses")))
    monthly.m["upstream"].set_client(client)
    operation = invoke_request(monthly, body, route, request, streaming=streaming)
    if streaming:
        response = await operation
        iterator = response.body_iterator
        await anext(iterator)
    else:
        task = asyncio.create_task(operation)
    try:
        await asyncio.wait_for(entered.wait(), 3)
        # A completed model round was legitimately paid before search was cancelled.
        before = {table: rows(request.db.path, table, request.request_id)
                  for table in ("retry_chain", "proxy_chain", "upstream_attempt_usage")}
        assert len(before["upstream_attempt_usage"]) == 1
        assert before["upstream_attempt_usage"][0]["input_tokens"] == 3
        assert before["upstream_attempt_usage"][0]["cost_source"] == "estimated"
        assert before["upstream_attempt_usage"][0]["cost_ticks"] > 0
        if streaming:
            await iterator.aclose()
        else:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    finally:
        if streaming:
            await iterator.aclose()
        elif not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await client.aclose()
    assert cancelled.is_set()
    root = rows(request.db.path, "request_log", request.request_id)[0]
    local = rows(request.db.path, "local_web_log", request.request_id)[0]
    source = rows(request.db.path, "search_call_log", request.request_id)[0]
    assert (root["status"], root["http_status"]) == ("cancelled", 499)
    assert root["finished_at"] is not None and "cancelled" in root["error_message"]
    assert root["input_tokens"] == 3 and root["output_tokens"] == 2
    assert root["final_channel_key"] == "api:month"
    assert local["status"] == "error" and local["ended_at"] is not None
    assert "cancelled" in local["error_message"]
    assert (source["status"], source["error_code"]) == ("error", "cancelled")
    assert logged_response(request) is None
    for table, entries in before.items():
        assert rows(request.db.path, table, request.request_id) == entries
    assert request.request_id not in log_db._request_handles
    assert rows(sentinel.db.path, "request_log", sentinel.request_id) == untouched
    assert rows(sentinel.db.path, "request_log", request.request_id) == []


@pytest.mark.parametrize("protocol", ["responses", "chat"])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("outcome,status", [("limit", 400), ("conflict", 502), ("success", 200), ("upstream_error", 404)])
async def test_logical_terminal_result_preserves_each_model_settlement(
        monthly, monkeypatch, protocol, streaming, outcome, status):
    body, route, request, sentinel, _ = priced_route(monthly, protocol, monkeypatch)
    if protocol == "chat":
        body["n"] = 1
    untouched = rows(sentinel.db.path, "request_log", sentinel.request_id)
    monkeypatch.setattr(service, "settings", lambda: search_cfg())
    monkeypatch.setattr(web, "max_tool_rounds", lambda: 1 if outcome == "limit" else 3)
    monkeypatch.setattr(log_db, "_store_log_bodies", lambda: True)
    calls, first_billing = [], []

    def model_http(req):
        calls.append(req)
        if len(calls) == 1:
            monthly.clock[0] = FEB
            return model_response(req, output(protocol, "s1"), protocol)
        first_billing.extend(copy.deepcopy(rows(request.db.path, "upstream_attempt_usage", request.request_id)))
        if outcome == "upstream_error":
            return httpx.Response(404, json={"error": {"message": "model unavailable"}})
        return model_response(req, output(protocol,
            "s2" if outcome == "limit" else ("s1" if outcome == "conflict" else None)), protocol)

    monkeypatch.setattr(service.network, "async_client", lambda **kw: httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"results": []}))))
    client = httpx.AsyncClient(transport=httpx.MockTransport(model_http))
    monthly.m["upstream"].set_client(client)
    try:
        response = await invoke_request(monthly, body, route, request, streaming=streaming, protocol=protocol)
        if streaming:
            raw = b"".join([chunk async for chunk in response.body_iterator])
        else:
            assert response.status_code == status
            raw = response.body
    finally:
        await client.aclose()
    if outcome == "limit":
        assert b"maxToolRounds" in raw
    elif outcome == "conflict":
        assert b"tool_call_id_conflict" in raw
    elif outcome == "success":
        assert ("".join(text_delta(frame, protocol) for frame in decode(raw)) == "final") if streaming else b"final" in raw
    else:
        assert b"error" in raw
    assert len(calls) == 2
    root = rows(request.db.path, "request_log", request.request_id)[0]
    assert (root["status"], root["http_status"]) == ("success" if status == 200 else "error", status)
    billing = rows(request.db.path, "upstream_attempt_usage", request.request_id)
    assert len(billing) == 2 and billing[:1] == first_billing
    assert billing[0]["outcome"] == "success"
    assert billing[0]["cost_source"] == "estimated" and billing[0]["cost_ticks"] > 0
    assert (billing[0]["input_tokens"], billing[0]["output_tokens"]) == (3, 2)
    if outcome != "upstream_error":
        assert [(r["outcome"], r["input_tokens"], r["output_tokens"]) for r in billing] == [("success", 3, 2)] * 2
    visible = json.loads(logged_response(request))
    assert ("error" in visible) == (status >= 400)
    if outcome == "success":
        assert visible["usage"]["prompt_tokens" if protocol == "chat" else "input_tokens"] == 6
    assert request.request_id not in log_db._request_handles
    assert rows(sentinel.db.path, "request_log", sentinel.request_id) == untouched
    assert rows(sentinel.db.path, "request_log", request.request_id) == []


@pytest.mark.parametrize("operation", ["search", "extract"])
@pytest.mark.parametrize("origin", ["managed_round", "mcp"])
async def test_exa_observed_dollar_cost_is_settled_and_visible_to_management(env, ctx, monkeypatch, operation, origin):
    monkeypatch.setattr(service, "settings", lambda: search_cfg("exa"))
    monkeypatch.setattr(service.network, "async_client", lambda **kw: httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json={
            "results": [{"title": "Docs", "url": "https://example.com/docs", "text": "docs"}],
            "costDollars": {"total": 0.01, "search": {"neural": 0.005}, "contents": {"text": 0.005}}
        }))))
    result = await getattr(service, operation)({"query": "Python docs", "url": "https://example.com/docs"}, origin=origin)
    assert result["usage"]["total"] == 0.01
    assert "_billing_body" not in result
    assert "usage" not in web._model_visible_search_result(result, operation)
    row = log_db.search_call_entries(0)[0]
    assert (row["cost_source"], row["cost_ticks"]) == ("actual", 100_000_000)
    assert row["usage_observed"] == 1 and row["origin"] == origin
    assert row["input_tokens"] == row["output_tokens"] == 0
    control = SearchControl()
    entry = control.logs(ctx, since_ts=0)[0]
    assert (entry["costSource"], entry["costUsd"]) == ("actual", 0.01)
    assert control.stats(ctx, since_ts=0)[0]["costUsd"] == 0.01


@pytest.mark.parametrize("total,expected", [(0, 0), (0.00000000015, 2), (None, None), (-1, None), (True, None), ("0.01", None)])
async def test_exa_only_explicit_valid_total_is_a_known_cost(env, monkeypatch, total, expected):
    monkeypatch.setattr(service, "settings", lambda: search_cfg("exa"))
    payload = {"results": [], "costDollars": {"total": total, "search": {"neural": 0.005}}}
    monkeypatch.setattr(service.network, "async_client", lambda **kw: httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=payload))))
    await service.search({"query": "Python docs"})
    row = log_db.search_call_entries(0)[0]
    assert row["status"] == "success"
    assert row["cost_ticks"] == expected
    assert row["cost_source"] == ("actual" if expected is not None else "unpriced")
    assert row["usage_observed"] == (1 if expected is not None else 0)


async def test_exa_paid_parse_failure_is_settled_once_per_attempt(env, monkeypatch):
    cfg = search_cfg("exa")
    cfg["maxAttempts"] = 2
    monkeypatch.setattr(service, "settings", lambda: cfg)
    calls = []

    def upstream(req):
        calls.append(req)
        body = {"costDollars": {"total": len(calls) * 0.01}}
        if len(calls) == 2:
            body["results"] = []
        return httpx.Response(200, json=body)

    monkeypatch.setattr(service.network, "async_client", lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    result = await service.search({"query": "Python docs"})
    entries = sorted(log_db.search_call_entries(0), key=lambda row: row["attempt_no"])
    assert [r["status"] for r in entries] == ["error", "success"]
    assert [r["cost_ticks"] for r in entries] == [100_000_000, 200_000_000]
    assert all(r["cost_source"] == "actual" for r in entries)
    assert "_billing_body" not in result
    assert log_db.search_call_stats(0)[0]["cost_ticks"] == 300_000_000


async def test_exa_paid_domain_refusal_settles_without_exposing_private_body(env, monkeypatch):
    monkeypatch.setattr(service, "settings", lambda: search_cfg("exa"))
    monkeypatch.setattr(service.network, "async_client", lambda **kw: httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json={
            "costDollars": {"total": 0.01}, "private_detail": "not-public",
            "results": [{"url": "https://blocked.example.com/page", "text": "page"}],
        }))))
    with pytest.raises(service.SearchError) as exc:
        await service.extract({"url": "https://example.com/page", "blocked_domains": ["blocked.example.com"]})
    assert exc.value.code == "url_not_allowed"
    assert not any(key.startswith("_billing") for key in vars(exc.value))
    assert "not-public" not in str(exc.value)
    entry = log_db.search_call_entries(0)[0]
    assert (entry["status"], entry["cost_source"], entry["cost_ticks"]) == ("error", "actual", 100_000_000)


@pytest.mark.parametrize("total", [float("nan"), float("inf"), 1e30])
def test_exa_invalid_numeric_cost_does_not_break_settlement(env, total):
    handle = log_db.record_search_call(call_id="cost", source_id="exa", source_type="exa")
    log_db.finish_search_call(handle, status="success", provider="exa",
                             response_body={"costDollars": {"total": total}})
    row = log_db.search_call_entries(0)[0]
    assert row["status"] == "success" and row["cost_ticks"] is None
    assert row["cost_source"] == "unpriced"
