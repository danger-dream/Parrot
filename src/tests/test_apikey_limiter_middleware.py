"""Production HTTP middleware coverage for API-key body-limit responses."""

from __future__ import annotations

import asyncio

import httpx
import pytest

import server as parrot_server
from src import apikey_limiter, drain


def _config(*, request_bytes: int = 100, events: int = 10,
            per_key: int = 100, process: int = 100) -> dict:
    return {
        "apiKeyConcurrency": {
            "enabled": True,
            "defaultMaxConcurrent": 1,
            "defaultMaxQueue": 2,
            "defaultQueueWaitSeconds": 2,
            "defaultMaxRequestBodyBytes": request_bytes,
            "defaultMaxRequestBodyEvents": events,
            "defaultMaxQueuedBodyBytesPerKey": per_key,
            "maxQueuedBodyBytes": process,
        },
        "apiKeys": {"k": {"key": "secret", "enabled": True, "limits": {}}},
    }


@pytest.fixture(autouse=True)
def _reset_runtime(monkeypatch):
    drain.reset_for_tests()
    apikey_limiter._slots.clear()
    apikey_limiter._queued_body_bytes_by_key.clear()
    apikey_limiter._queued_body_bytes_total = 0
    monkeypatch.setattr(parrot_server.auth, "validate", lambda _headers: ("k", [], None))
    monkeypatch.setattr(
        apikey_limiter, "QUEUED_BODY_EVENT_OVERHEAD_BYTES", 0, raising=False,
    )
    yield
    drain.reset_for_tests()
    apikey_limiter._slots.clear()
    apikey_limiter._queued_body_bytes_by_key.clear()
    apikey_limiter._queued_body_bytes_total = 0


async def _post(path: str, content) -> httpx.Response:
    transport = httpx.ASGITransport(app=parrot_server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            path,
            headers={"authorization": "Bearer secret", "content-type": "application/json"},
            content=content,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "expected_type"),
    [
        ("/v1/messages", "request_too_large"),
        ("/v1/responses", "invalid_request_error"),
    ],
)
async def test_production_middleware_request_bytes_returns_413_schema(
    monkeypatch, path, expected_type,
):
    monkeypatch.setattr(apikey_limiter.config, "get", lambda: _config(request_bytes=5))
    response = await _post(path, b"123456")
    assert response.status_code == 413
    payload = response.json()
    if path == "/v1/messages":
        assert payload["type"] == "error"
        assert payload["error"]["type"] == expected_type
    else:
        assert payload["error"]["type"] == expected_type
    assert payload["error"]["code"] == "request_too_large"
    assert "retry-after" not in response.headers


class _TwoChunks(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"a"
        await asyncio.sleep(0)
        yield b"b"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/v1/messages", "/v1/responses"])
async def test_production_middleware_event_count_returns_413(monkeypatch, path):
    monkeypatch.setattr(
        apikey_limiter.config, "get", lambda: _config(request_bytes=100, events=1),
    )
    response = await _post(path, _TwoChunks())
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"
    assert "body events" in response.json()["error"]["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "pressure", "expected_type"),
    [
        ("/v1/messages", "key", "rate_limit_error"),
        ("/v1/messages", "process", "rate_limit_error"),
        ("/v1/responses", "key", "rate_limit_exceeded"),
        ("/v1/responses", "process", "rate_limit_exceeded"),
    ],
)
async def test_production_middleware_aggregate_pressure_returns_429_retry_after(
    monkeypatch, path, pressure, expected_type,
):
    cfg = _config(
        request_bytes=100,
        per_key=5 if pressure == "key" else 100,
        process=5 if pressure == "process" else 100,
    )
    monkeypatch.setattr(apikey_limiter.config, "get", lambda: cfg)
    active = await apikey_limiter.acquire("k")
    try:
        response = await _post(path, b"123456")
    finally:
        await active.release()

    assert response.status_code == 429
    assert response.headers["retry-after"] == "1"
    payload = response.json()
    assert payload["error"]["type"] == expected_type
    assert payload["error"]["code"] == "queued_body_capacity"
