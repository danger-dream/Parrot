import asyncio
import gc

import httpx
import pytest
from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import Response

import server as parrot_server
from src import drain, memory_reclaim


@pytest.fixture(autouse=True)
def reclaim_state(monkeypatch):
    drain.reset_for_tests()
    monkeypatch.setattr(memory_reclaim, "_pending_bytes", 0)
    monkeypatch.setattr(memory_reclaim, "_last_activity", 0.0)
    monkeypatch.setattr(memory_reclaim, "_stats", {
        "collections": 0, "trims": 0, "refreezes": 0, "last_collect_ms": 0.0, "max_collect_ms": 0.0,
    })
    yield
    drain.reset_for_tests()


def test_importing_server_keeps_interpreter_defaults():
    # Only `python server.py` enables reclamation; tests import the app.
    assert memory_reclaim.is_enabled() is False


def test_configure_can_be_disabled(monkeypatch):
    monkeypatch.setattr(memory_reclaim, "_enabled", False)
    monkeypatch.setenv("PARROT_MEMORY_RECLAIM", "0")
    memory_reclaim.configure()
    assert memory_reclaim.is_enabled() is False


def test_middleware_records_request_and_response_payload(monkeypatch):
    noted = []
    monkeypatch.setattr(memory_reclaim, "note", noted.append)
    app = FastAPI()
    app.add_middleware(parrot_server._DrainHttpMiddleware)

    @app.post("/v1/echo")
    async def echo(request: Request):
        return Response(await request.body())

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
            response = await client.post("/v1/echo", content=b"x" * 5000)
            assert response.content == b"x" * 5000

    asyncio.run(scenario())
    assert noted == [10000]


class _Cycle:
    def __init__(self, payload):
        self.payload = payload
        self.me = self


def test_loop_collects_completed_payload_and_trims(monkeypatch):
    trims = []
    monkeypatch.setattr(memory_reclaim, "_TICK_S", 0.01)
    monkeypatch.setattr(memory_reclaim, "_MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(memory_reclaim, "_libc", type("Libc", (), {"malloc_trim": staticmethod(trims.append)})())

    async def scenario():
        gc.disable()
        try:
            _Cycle(bytearray(64))
            assert any(isinstance(o, _Cycle) for o in gc.get_objects())
            memory_reclaim.note(memory_reclaim._COLLECT_BYTES)
            task = asyncio.create_task(memory_reclaim.reclaim_loop())
            for _ in range(200):
                await asyncio.sleep(0.01)
                if memory_reclaim.stats()["collections"]:
                    break
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        finally:
            gc.enable()

    asyncio.run(scenario())
    assert memory_reclaim.stats()["collections"] == 1
    assert memory_reclaim.stats()["pending_bytes"] == 0
    assert trims == [0]
    assert not any(isinstance(o, _Cycle) for o in gc.get_objects())


def test_loop_waits_below_threshold_while_traffic_continues(monkeypatch):
    monkeypatch.setattr(memory_reclaim, "_TICK_S", 0.01)
    monkeypatch.setattr(memory_reclaim, "_libc", None)

    async def scenario():
        task = asyncio.create_task(memory_reclaim.reclaim_loop())
        for _ in range(20):
            memory_reclaim.note(memory_reclaim._QUIET_COLLECT_BYTES)
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert memory_reclaim.stats()["collections"] == 0


def test_configure_without_glibc_keeps_gc_reclaim(monkeypatch):
    monkeypatch.setattr(memory_reclaim, "_enabled", False)
    monkeypatch.setattr(memory_reclaim, "_libc", None)
    monkeypatch.delenv("PARROT_MEMORY_RECLAIM", raising=False)

    def no_glibc(name):
        raise OSError("unrecognized configuration name")

    monkeypatch.setattr(memory_reclaim.os, "confstr", no_glibc)
    memory_reclaim.configure()
    assert memory_reclaim.is_enabled() is True
    assert memory_reclaim._libc is None
