"""End-to-end: one managed search writes a sourced, settled search-call row.

Only the provider HTTP transport is simulated. Attempt handling, logging and
billing use the production search_service/log_db implementation.
"""
from __future__ import annotations

import asyncio
import sqlite3
import threading

import httpx
import pytest

from src import log_db, search_service


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(log_db, "_log_dir", str(tmp_path))
    monkeypatch.setattr(log_db, "_local", threading.local())
    monkeypatch.setattr(log_db, "_write_conn_registry", {})
    monkeypatch.setattr(log_db, "_request_handles", {})
    monkeypatch.setattr(log_db, "_retired_log_registry" if hasattr(log_db, "_retired_log_registry") else "_retired_log_paths", set())
    yield tmp_path
    for connections in log_db._write_conn_registry.values():
        for conn in connections:
            conn.close()


def _settings(backends):
    return {"functionMode": "managed", "hostedMode": "managed", "maxAttempts": 3,
            "timeoutSeconds": 10, "maxResults": 5, "maxToolRounds": 10,
            "maxFetchChars": 1000, "minQueryChars": 2, "maxFetchUrlChars": 2048,
            "requireKnownUrlForFetch": True, "maxConcurrentToolCalls": 0,
            "language": "", "country": "", "freshness": "", "backends": backends}


def _tavily_backend():
    return {"id": "tavily", "type": "tavily", "name": "Tavily", "enabled": True,
            "apiKeys": ["k1", "k2"], "endpoint": "https://api.tavily.com", "model": "",
            "accountIds": [], "allowDisabledAccounts": False}


def _rows(path):
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT * FROM search_call_log ORDER BY id")]


def test_successful_http_search_writes_one_sourced_row(env, monkeypatch):
    monkeypatch.setattr(search_service, "settings", lambda: _settings([_tavily_backend()]))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [
            {"title": "A", "url": "https://a.test", "content": "text-a"},
            {"title": "B", "url": "https://b.test", "content": "text-b"},
        ]})

    async def run():
        transport = httpx.MockTransport(handler)
        real = httpx.AsyncClient

        class Patched(real):
            def __init__(self, *args, **kwargs):
                kwargs["transport"] = transport
                super().__init__(*args, **kwargs)

        import src.network as network
        monkeypatch.setattr(network, "async_client", lambda **kw: Patched())
        return await search_service.search({"query": "hello", "max_results": 5},
                                           origin="managed_round", round_no=1)

    result = asyncio.run(run())
    assert result["provider"] == "tavily"
    # Private billing evidence is stripped from the public result.
    assert "_billing_body" not in result and "_upstream_model" not in result
    rows = _rows(next(iter(log_db._write_conn_registry)))
    assert len(rows) == 1
    row = rows[0]
    assert row["source_id"] == "tavily" and row["source_type"] == "tavily"
    assert row["origin"] == "managed_round" and row["round_no"] == 1
    assert row["status"] == "success" and row["result_count"] == 2
    assert row["credential_kind"] == "api_key" and row["credential_label"] == "Key #1"
    assert row["query"] == "hello"
    assert row["elapsed_ms"] is not None


def test_failover_records_one_row_per_real_attempt(env, monkeypatch):
    backend = _tavily_backend()
    monkeypatch.setattr(search_service, "settings", lambda: _settings([backend]))
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, json={"error": "upstream down"})
        return httpx.Response(200, json={"results": [{"title": "A", "url": "https://a.test"}]})

    async def run():
        transport = httpx.MockTransport(handler)
        real = httpx.AsyncClient

        class Patched(real):
            def __init__(self, *args, **kwargs):
                kwargs["transport"] = transport
                super().__init__(*args, **kwargs)

        import src.network as network
        monkeypatch.setattr(network, "async_client", lambda **kw: Patched())
        return await search_service.search({"query": "hello", "max_results": 5})

    asyncio.run(run())
    rows = _rows(next(iter(log_db._write_conn_registry)))
    # Two credentials were tried; each real upstream call owns a row.
    assert len(rows) == 2
    assert rows[0]["credential_label"] == "Key #1" and rows[0]["status"] == "error"
    assert rows[1]["credential_label"] == "Key #2" and rows[1]["status"] == "success"
    assert rows[0]["attempt_no"] == 1 and rows[1]["attempt_no"] == 2


def test_no_configured_source_still_logs_nothing_and_raises(env, monkeypatch):
    backend = _tavily_backend()
    backend["apiKeys"] = []
    monkeypatch.setattr(search_service, "settings", lambda: _settings([backend]))
    with pytest.raises(search_service.SearchError):
        asyncio.run(search_service.search({"query": "hello"}))
    # Nothing was dispatched, so nothing may be recorded as an attempt.
    assert all(not log_db.search_call_entries(0) for _ in [0])


def test_management_probe_is_recorded_with_its_own_origin(env, monkeypatch):
    monkeypatch.setattr(search_service, "settings", lambda: _settings([_tavily_backend()]))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [{"title": "A", "url": "https://a.test"}]})

    async def run():
        transport = httpx.MockTransport(handler)
        real = httpx.AsyncClient

        class Patched(real):
            def __init__(self, *args, **kwargs):
                kwargs["transport"] = transport
                super().__init__(*args, **kwargs)

        import src.network as network
        monkeypatch.setattr(network, "async_client", lambda **kw: Patched())
        return await search_service.search({"query": "probe"}, backend_id="tavily",
                                           origin="management_test")

    asyncio.run(run())
    rows = _rows(next(iter(log_db._write_conn_registry)))
    assert len(rows) == 1 and rows[0]["origin"] == "management_test"
