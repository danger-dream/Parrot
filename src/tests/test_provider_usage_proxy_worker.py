"""Provider-usage worker routing and signature-compatibility release tests."""
from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from src.tests import _isolation
_isolation.isolate()

from src import provider_usage as pu, state_db


def _channel(name: str, api_key: str = "secret"):
    return SimpleNamespace(
        provider_id="deepseek", provider_preset_id="standard",
        api_key=api_key, type="api", key=f"api:{name}",
        display_name=name, models=[],
    )


def _snapshot(source="deepseek"):
    return {"source": source, "balances": [], "windows": [], "counters": [],
            "notices": [], "partial": False}


@pytest.fixture(autouse=True)
def _clean_state():
    state_db.init()
    for row in state_db.provider_usage_load_all():
        state_db.provider_usage_delete(row["account_id"])
    with pu._GUARD:
        pu._INFLIGHT.clear()
        pu._RUNTIME.clear()
    yield


@pytest.mark.asyncio
async def test_schedule_job_worker_fetch_network_preserves_channel(monkeypatch):
    """The actual fetch API reaches network.async_client with the job channel."""
    routes = []

    class Response:
        def raise_for_status(self): pass
        def json(self): return {"is_available": True, "balance_infos": []}

    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return None
        async def get(self, *_args, **_kwargs): return Response()

    def async_client(**kwargs):
        routes.append((kwargs["proxy_purpose"], kwargs["proxy_channel"]))
        return Client()

    monkeypatch.setattr(pu.network, "async_client", async_client)
    monkeypatch.setattr(pu, "_still_live", lambda _aid: True)
    await pu.start()
    try:
        channels = [_channel("a", "key-a"), _channel("b", "key-b")]
        assert all(pu.schedule_refresh(ch) for ch in channels)
        for _ in range(200):
            if not pu._INFLIGHT:
                break
            await asyncio.sleep(.005)
        assert not pu._INFLIGHT
        assert sorted(routes) == [
            ("provider_usage", "api:a"), ("provider_usage", "api:b")]
    finally:
        await pu.stop()


@pytest.mark.asyncio
async def test_same_account_is_singleflight_and_first_channel_route_is_explicit(monkeypatch):
    calls = []
    gate = asyncio.Event()

    async def routed_fetch(spec, key, *, channel_key=""):
        calls.append((key, channel_key))
        await gate.wait()
        return _snapshot(spec.adapter)

    monkeypatch.setattr(pu, "fetch", routed_fetch)
    monkeypatch.setattr(pu, "_still_live", lambda _aid: True)
    await pu.start()
    try:
        first = _channel("first", "shared")
        alias = _channel("alias", "shared")
        assert pu.account_id(first) == pu.account_id(alias)
        assert pu.schedule_refresh(first)
        assert not pu.schedule_refresh(alias)
        await asyncio.sleep(0)
        gate.set()
        for _ in range(100):
            if not pu._INFLIGHT: break
            await asyncio.sleep(.005)
        assert calls == [("shared", "api:first")]
    finally:
        await pu.stop()


@pytest.mark.asyncio
async def test_legacy_signature_once_and_internal_typeerror_never_retried(monkeypatch):
    legacy_calls = []

    async def legacy_fetch(spec, key):
        legacy_calls.append(key)
        return _snapshot(spec.adapter)

    monkeypatch.setattr(pu, "fetch", legacy_fetch)
    monkeypatch.setattr(pu, "_still_live", lambda _aid: True)
    await pu.start()
    try:
        ch = _channel("legacy", "legacy-key")
        assert pu.schedule_refresh(ch)
        for _ in range(100):
            if not pu._INFLIGHT: break
            await asyncio.sleep(.005)
        assert legacy_calls == ["legacy-key"]
    finally:
        await pu.stop()

    body_calls = []

    async def broken_fetch(spec, key, *, channel_key=""):
        body_calls.append((key, channel_key))
        raise TypeError("unexpected keyword argument 'channel_key' from provider body")

    monkeypatch.setattr(pu, "fetch", broken_fetch)
    await pu.start()
    try:
        ch = _channel("broken", "broken-key")
        assert pu.schedule_refresh(ch)
        for _ in range(100):
            if not pu._INFLIGHT: break
            await asyncio.sleep(.005)
        assert body_calls == [("broken-key", "api:broken")]
        row = state_db.provider_usage_load(pu.account_id(ch))
        assert row is not None and row["last_error"] == "上游用量暂时获取失败"
    finally:
        await pu.stop()


@pytest.mark.asyncio
async def test_stop_cancels_inflight_and_clears_reservations(monkeypatch):
    started = asyncio.Event()

    async def blocked(spec, key, *, channel_key=""):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(pu, "fetch", blocked)
    ch = _channel("cancel", "cancel-key")
    await pu.start()
    assert pu.schedule_refresh(ch)
    await started.wait()
    await pu.stop()
    assert not pu._INFLIGHT
    assert pu._QUEUE is None
