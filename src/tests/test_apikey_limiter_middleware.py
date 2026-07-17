"""Production HTTP middleware coverage for API-key body-limit responses."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import httpx
import pytest
from starlette.responses import Response
from starlette.requests import Request

import server as parrot_server
from src import apikey_limiter, drain


def _config(*, request_bytes: int = 100, events: int = 10,
            per_key: int = 100, process: int = 100,
            spool_threshold: int = 1024 * 1024,
            spool_per_key: int = 512 * 1024 * 1024,
            spool_process: int = 2 * 1024 * 1024 * 1024,
            spool_directory: str = "") -> dict:
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
            "queuedBodySpoolThresholdBytes": spool_threshold,
            "queuedBodySpoolDirectory": spool_directory,
            "defaultMaxQueuedBodySpoolBytesPerKey": spool_per_key,
            "maxQueuedBodySpoolBytes": spool_process,
        },
        "apiKeys": {"k": {"key": "secret", "enabled": True, "limits": {}}},
    }


@pytest.fixture(autouse=True)
def _reset_runtime(monkeypatch):
    drain.reset_for_tests()
    apikey_limiter._slots.clear()
    apikey_limiter._queued_body_bytes_by_key.clear()
    apikey_limiter._queued_body_bytes_total = 0
    apikey_limiter._queued_body_spool_bytes_by_key.clear()
    apikey_limiter._queued_body_spool_bytes_total = 0
    monkeypatch.setattr(parrot_server.auth, "validate", lambda _headers: ("k", [], None))
    monkeypatch.setattr(
        apikey_limiter, "QUEUED_BODY_EVENT_OVERHEAD_BYTES", 0, raising=False,
    )
    yield
    drain.reset_for_tests()
    apikey_limiter._slots.clear()
    apikey_limiter._queued_body_bytes_by_key.clear()
    apikey_limiter._queued_body_bytes_total = 0
    apikey_limiter._queued_body_spool_bytes_by_key.clear()
    apikey_limiter._queued_body_spool_bytes_total = 0


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


@pytest.mark.asyncio
@pytest.mark.parametrize("pressure", ["key", "process"])
async def test_production_middleware_disk_aggregate_pressure_returns_429_and_cleans(
    monkeypatch, tmp_path, pressure,
):
    cfg = _config(
        request_bytes=100,
        per_key=100,
        process=100,
        spool_threshold=1,
        spool_per_key=5 if pressure == "key" else 100,
        spool_process=5 if pressure == "process" else 100,
        spool_directory=str(tmp_path),
    )
    monkeypatch.setattr(apikey_limiter.config, "get", lambda: cfg)
    active = await apikey_limiter.acquire("k")
    try:
        response = await _post("/v1/responses", b"123456")
    finally:
        await active.release()

    assert response.status_code == 429
    assert response.headers["retry-after"] == "1"
    assert response.json()["error"]["code"] == "queued_body_capacity"
    assert apikey_limiter._queued_body_bytes_total == 0
    assert apikey_limiter._queued_body_spool_bytes_total == 0
    assert list(Path(tmp_path).iterdir()) == []


def _multipart_multi_image_body(total_bytes: int) -> bytes:
    boundary = b"parrot-spool-regression"
    prefix = (
        b"--" + boundary + b"\r\n"
        b'Content-Disposition: form-data; name="prompt"\r\n\r\nedit\r\n'
        b"--" + boundary + b"\r\n"
        b'Content-Disposition: form-data; name="images[]"; filename="one.png"\r\n'
        b"Content-Type: image/png\r\n\r\n"
    )
    between = (
        b"\r\n--" + boundary + b"\r\n"
        b'Content-Disposition: form-data; name="images[]"; filename="two.png"\r\n'
        b"Content-Type: image/png\r\n\r\n"
    )
    before_mask = (
        b"\r\n--" + boundary + b"\r\n"
        b'Content-Disposition: form-data; name="mask"; filename="mask.png"\r\n'
        b"Content-Type: image/png\r\n\r\n"
    )
    suffix = b"\r\n--" + boundary + b"--\r\n"
    first_size = 16 * 1024 * 1024
    mask = b"M" * 1024
    second_size = (
        total_bytes - len(prefix) - first_size - len(between)
        - len(before_mask) - len(mask) - len(suffix)
    )
    assert second_size > 0
    return (
        prefix + (b"A" * first_size) + between + (b"B" * second_size)
        + before_mask + mask + suffix
    )


@pytest.mark.asyncio
async def test_production_middleware_queued_33mib_multi_image_spools_and_replays_exactly(
    monkeypatch, tmp_path,
):
    """The real production middleware must treat queued/nonqueued edits alike."""
    cfg = _config(
        request_bytes=8 * 1024 * 1024,
        events=32,
        per_key=32 * 1024 * 1024,
        process=128 * 1024 * 1024,
        spool_threshold=1024 * 1024,
        spool_directory=str(tmp_path),
    )
    cfg["images"] = {"maxInputImageBytes": 20 * 1024 * 1024}
    monkeypatch.setattr(apikey_limiter.config, "get", lambda: cfg)

    from src.openai import images_openai_compat

    seen: list[bytes] = []

    async def exact_echo(request):
        body = await request.body()
        seen.append(body)
        return Response(
            content=hashlib.sha256(body).hexdigest(), media_type="text/plain"
        )

    monkeypatch.setattr(images_openai_compat, "handle_edits", exact_echo)
    body = _multipart_multi_image_body(33 * 1024 * 1024)
    expected_digest = hashlib.sha256(body).hexdigest()
    headers = {
        "authorization": "Bearer secret",
        "content-type": "multipart/form-data; boundary=parrot-spool-regression",
    }
    transport = httpx.ASGITransport(app=parrot_server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        nonqueued = await client.post("/v1/images/edits", headers=headers, content=body)
        assert nonqueued.status_code == 200
        assert nonqueued.text == expected_digest
        assert apikey_limiter._queued_body_spool_bytes_total == 0

        active = await apikey_limiter.acquire("k")
        queued_task = asyncio.create_task(
            client.post("/v1/images/edits", headers=headers, content=body)
        )

        async def body_spooled():
            while apikey_limiter._queued_body_spool_bytes_total != len(body):
                if queued_task.done():
                    await queued_task
                    pytest.fail("queued request completed before its body was spooled")
                await asyncio.sleep(0)

        await asyncio.wait_for(body_spooled(), timeout=10)
        assert apikey_limiter._queued_body_bytes_total < 1024 * 1024
        await active.release()
        queued = await asyncio.wait_for(queued_task, timeout=10)

    assert queued.status_code == 200
    assert queued.text == expected_digest
    assert seen == [body, body]
    assert apikey_limiter._queued_body_bytes_total == 0
    assert apikey_limiter._queued_body_bytes_by_key == {}
    assert apikey_limiter._queued_body_spool_bytes_total == 0
    assert apikey_limiter._queued_body_spool_bytes_by_key == {}
    assert list(Path(tmp_path).iterdir()) == []


@pytest.mark.asyncio
async def test_image_endpoint_absolute_cap_is_413_queued_and_nonqueued_and_never_spools(
    monkeypatch, tmp_path,
):
    cfg = _config(
        request_bytes=100,
        per_key=1024,
        process=4096,
        spool_threshold=16,
        spool_directory=str(tmp_path),
    )
    cfg["images"] = {"maxInputImageBytes": 64}
    monkeypatch.setattr(apikey_limiter.config, "get", lambda: cfg)
    limits = apikey_limiter._resolve_limits("k")
    request_for_limit = Request(
        {
            "type": "http", "method": "POST", "path": "/v1/images/edits",
            "headers": [(b"content-type", b"application/json")],
        }
    )
    endpoint_max = apikey_limiter._request_body_max_bytes(request_for_limit, limits)
    body = b"x" * (endpoint_max + 1)

    nonqueued = await _post("/v1/images/edits", body)
    assert nonqueued.status_code == 413
    active = await apikey_limiter.acquire("k")
    try:
        queued = await _post("/v1/images/edits", body)
    finally:
        await active.release()
    assert queued.status_code == 413
    assert nonqueued.json()["error"]["code"] == "request_too_large"
    assert queued.json()["error"]["code"] == "request_too_large"
    assert apikey_limiter._queued_body_bytes_total == 0
    assert apikey_limiter._queued_body_spool_bytes_total == 0
    assert list(Path(tmp_path).iterdir()) == []


@pytest.mark.asyncio
async def test_small_queued_body_stays_in_memory_without_disk_rollover(
    monkeypatch, tmp_path,
):
    cfg = _config(
        request_bytes=1024,
        per_key=4096,
        process=4096,
        spool_threshold=1024,
        spool_directory=str(tmp_path),
    )
    monkeypatch.setattr(apikey_limiter.config, "get", lambda: cfg)
    active = await apikey_limiter.acquire("k")
    request = asyncio.create_task(_post("/v1/responses", b"small-body"))

    async def retained_in_memory():
        while apikey_limiter._queued_body_bytes_total != len(b"small-body"):
            await asyncio.sleep(0)

    await asyncio.wait_for(retained_in_memory(), timeout=1)
    assert apikey_limiter._queued_body_spool_bytes_total == 0
    await active.release()
    await request
    assert apikey_limiter._queued_body_bytes_total == 0
    assert apikey_limiter._queued_body_spool_bytes_total == 0
    assert list(Path(tmp_path).iterdir()) == []
