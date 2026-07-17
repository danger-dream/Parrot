"""Production HTTP middleware coverage for API-key body-limit responses."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from starlette.responses import JSONResponse, Response
from starlette.requests import Request

import server as parrot_server
from src import apikey_limiter, drain


def _config(*, request_bytes: int = 100, events: int = 10,
            per_key: int = 100, process: int = 100,
            spool_threshold: int = 1024 * 1024,
            spool_per_key: int = 512 * 1024 * 1024,
            spool_process: int = 2 * 1024 * 1024 * 1024) -> dict:
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
            "defaultMaxQueuedBodySpoolBytesPerKey": spool_per_key,
            "maxQueuedBodySpoolBytes": spool_process,
        },
        "apiKeys": {"k": {"key": "secret", "enabled": True, "limits": {}}},
    }


@pytest.fixture(autouse=True)
def _reset_runtime(monkeypatch, tmp_path):
    drain.reset_for_tests()
    apikey_limiter._slots.clear()
    apikey_limiter._queued_body_bytes_by_key.clear()
    apikey_limiter._queued_body_bytes_total = 0
    apikey_limiter._queued_body_spool_bytes_by_key.clear()
    apikey_limiter._queued_body_spool_bytes_total = 0
    monkeypatch.setattr(parrot_server.auth, "validate", lambda _headers: ("k", [], None))
    monkeypatch.setattr(parrot_server.auth, "images_allowed", lambda _key: True)
    monkeypatch.setattr(apikey_limiter.config, "DATA_DIR", str(tmp_path))
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


def _assert_spool_empty(tmp_path) -> None:
    spool_dir = Path(tmp_path) / "queued-body-spool"
    assert not spool_dir.exists() or list(spool_dir.iterdir()) == []


async def _post(path: str, content, *, headers: dict[str, str] | None = None) -> httpx.Response:
    request_headers = {
        "authorization": "Bearer secret",
        "content-type": "application/json",
    }
    request_headers.update(headers or {})
    transport = httpx.ASGITransport(app=parrot_server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(path, headers=request_headers, content=content)


def _core_echo_app() -> FastAPI:
    echo_app = FastAPI()
    echo_app.add_middleware(parrot_server._DrainHttpMiddleware)

    @echo_app.post("/v1/messages")
    @echo_app.post("/v1/chat/completions")
    @echo_app.post("/v1/responses")
    async def echo(request: Request):
        body = await request.body()
        return JSONResponse({
            "bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
        })

    return echo_app


class _ChunkedBody(httpx.AsyncByteStream):
    def __init__(self, body: bytes):
        self.body = body

    async def __aiter__(self):
        cut = max(1, len(self.body) // 2)
        yield self.body[:cut]
        await asyncio.sleep(0)
        yield self.body[cut:]


def _wire_body(body: bytes, mode: str):
    if mode == "content_length":
        return body, {}
    headers = {"content-length": "1"} if mode == "false_low" else {}
    return _ChunkedBody(body), headers


async def _post_to(
    app, path: str, content, *, headers: dict[str, str] | None = None,
) -> httpx.Response:
    request_headers = {
        "authorization": "Bearer secret",
        "content-type": "application/json",
    }
    request_headers.update(headers or {})
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(path, headers=request_headers, content=content)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path", ["/v1/messages", "/v1/chat/completions", "/v1/responses"],
)
@pytest.mark.parametrize("mode", ["content_length", "chunked", "false_low"])
async def test_nonqueued_core_over_8mib_is_not_replay_limited(
    monkeypatch, path, mode,
):
    replay_cap = 8 * 1024 * 1024
    cfg = _config(
        request_bytes=replay_cap,
        events=1,
        per_key=32 * 1024 * 1024,
        process=128 * 1024 * 1024,
    )
    monkeypatch.setattr(apikey_limiter.config, "get", lambda: cfg)
    body = b"x" * (replay_cap + 1)
    content, headers = _wire_body(body, mode)
    response = await _post_to(_core_echo_app(), path, content, headers=headers)
    assert response.status_code == 200
    assert response.json() == {
        "bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
    }
    assert response.headers["x-parrot-apiKey-in-flight"] == "1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path", ["/v1/messages", "/v1/chat/completions", "/v1/responses"],
)
async def test_nonqueued_core_event_count_is_not_replay_limited(monkeypatch, path):
    cfg = _config(request_bytes=100, events=1)
    monkeypatch.setattr(apikey_limiter.config, "get", lambda: cfg)
    body = b"ab"
    response = await _post_to(_core_echo_app(), path, _ChunkedBody(body))
    assert response.status_code == 200
    assert response.json()["sha256"] == hashlib.sha256(body).hexdigest()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path", ["/v1/messages", "/v1/chat/completions", "/v1/responses"],
)
@pytest.mark.parametrize("mode", ["content_length", "chunked", "false_low"])
async def test_queued_core_over_replay_cap_returns_stable_413(
    monkeypatch, path, mode,
):
    cfg = _config(request_bytes=8, events=10, per_key=100, process=100)
    monkeypatch.setattr(apikey_limiter.config, "get", lambda: cfg)
    body = b"x" * 9
    content, headers = _wire_body(body, mode)
    active = await apikey_limiter.acquire("k")
    try:
        response = await _post_to(_core_echo_app(), path, content, headers=headers)
    finally:
        await active.release()
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"
    assert "retry-after" not in response.headers


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
    _assert_spool_empty(tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path", ["/v1/responses", "/v1/images/edit", "/v1/images/edits"],
)
async def test_production_middleware_spool_os_error_is_stable_and_sanitized(
    monkeypatch, tmp_path, capsys, path,
):
    cfg = _config(
        request_bytes=100,
        per_key=100,
        process=100,
        spool_threshold=1,
    )
    monkeypatch.setattr(apikey_limiter.config, "get", lambda: cfg)
    secret_path = "/srv/parrot/private/queued-body-spool/tenant-secret"

    def fail_open(_self, _limits):
        raise PermissionError(13, "permission denied", secret_path)

    monkeypatch.setattr(
        apikey_limiter._BodyPreservingReceive, "_open_spool", fail_open,
    )
    active = await apikey_limiter.acquire("k")
    try:
        response = await _post(path, b"123456")
    finally:
        await active.release()

    assert response.status_code == 503
    payload = response.json()
    assert payload["error"]["code"] == "queued_body_spool_unavailable"
    assert payload["error"]["message"] == (
        "queued request body spool is temporarily unavailable"
    )
    assert secret_path not in response.text
    assert "permission denied" not in response.text.lower()
    server_log = capsys.readouterr().out
    assert "type=PermissionError" in server_log
    assert "errno=13" in server_log
    assert secret_path not in server_log
    assert apikey_limiter._queued_body_bytes_total == 0
    assert apikey_limiter._queued_body_spool_bytes_total == 0


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
    _assert_spool_empty(tmp_path)


def _image_endpoint_max(path: str) -> int:
    request = Request({
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": [(b"content-type", b"application/json")],
    })
    return apikey_limiter._request_body_max_bytes(
        request, apikey_limiter._resolve_limits("k"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/v1/images/edit", "/v1/images/edits"])
@pytest.mark.parametrize("queued", [False, True])
@pytest.mark.parametrize("mode", ["content_length", "chunked", "false_low"])
async def test_image_json_body_limit_is_stable_413_for_both_parsers(
    monkeypatch, tmp_path, path, queued, mode,
):
    cfg = _config(
        request_bytes=100,
        events=10,
        per_key=1024,
        process=4096,
        spool_threshold=16,
    )
    cfg["images"] = {"maxInputImageBytes": 64}
    monkeypatch.setattr(apikey_limiter.config, "get", lambda: cfg)
    body = b"x" * (_image_endpoint_max(path) + 1)
    content, headers = _wire_body(body, mode)

    active = await apikey_limiter.acquire("k") if queued else None
    try:
        response = await _post(path, content, headers=headers)
    finally:
        if active is not None:
            await active.release()

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"
    assert response.json()["error"]["type"] == "invalid_request_error"
    assert "retry-after" not in response.headers
    assert apikey_limiter._queued_body_bytes_total == 0
    assert apikey_limiter._queued_body_spool_bytes_total == 0
    _assert_spool_empty(tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/v1/images/edit", "/v1/images/edits"])
@pytest.mark.parametrize("queued", [False, True])
async def test_image_json_event_limit_is_stable_413_for_both_parsers(
    monkeypatch, path, queued,
):
    cfg = _config(request_bytes=100, events=1, per_key=100, process=100)
    cfg["images"] = {"maxInputImageBytes": 64}
    monkeypatch.setattr(apikey_limiter.config, "get", lambda: cfg)
    active = await apikey_limiter.acquire("k") if queued else None
    try:
        response = await _post(path, _ChunkedBody(b"{}"))
    finally:
        if active is not None:
            await active.release()

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"
    assert "body events" in response.json()["error"]["message"]
    assert apikey_limiter._queued_body_bytes_total == 0
    assert apikey_limiter._queued_body_spool_bytes_total == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/v1/images/edit", "/v1/images/edits"])
@pytest.mark.parametrize("pressure", ["key", "process"])
async def test_image_json_aggregate_pressure_is_429_only_while_queued(
    monkeypatch, path, pressure,
):
    cfg = _config(
        request_bytes=100,
        events=10,
        per_key=1 if pressure == "key" else 100,
        process=1 if pressure == "process" else 100,
        spool_threshold=100,
    )
    cfg["images"] = {"maxInputImageBytes": 64}
    monkeypatch.setattr(apikey_limiter.config, "get", lambda: cfg)

    nonqueued = await _post(path, b"{}")
    assert nonqueued.status_code != 429

    active = await apikey_limiter.acquire("k")
    try:
        queued = await _post(path, b"{}")
    finally:
        await active.release()
    assert queued.status_code == 429
    assert queued.headers["retry-after"] == "1"
    assert queued.json()["error"]["code"] == "queued_body_capacity"
    assert queued.json()["error"]["type"] == "rate_limit_exceeded"
    assert apikey_limiter._queued_body_bytes_total == 0
    assert apikey_limiter._queued_body_spool_bytes_total == 0


@pytest.mark.asyncio
async def test_small_queued_body_stays_in_memory_without_disk_rollover(
    monkeypatch, tmp_path,
):
    cfg = _config(
        request_bytes=1024,
        per_key=4096,
        process=4096,
        spool_threshold=1024,
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
    _assert_spool_empty(tmp_path)
