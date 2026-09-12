"""Real config-save -> reload -> HTTP request regression for issue #29."""
from __future__ import annotations

import asyncio
import copy
import json
import os
import threading
from pathlib import Path

import httpx
import pytest

from src.tests._isolation import isolate

isolate()

from src import config, drain, network, upstream
from src.telegram import ui
from src.tests import test_protocol_fake_upstreams as fake


@pytest.fixture
async def reload_env(monkeypatch, tmp_path):
    cfg = copy.deepcopy(config.get())
    cfg.update(
        oauthAccounts=[], channels=[],
        apiKeys={"fixture": {"key": "ccp-test", "enabled": True}},
        network={
            "dns": {"servers": ["8.8.8.8"], "timeoutSeconds": 3, "cacheTtlSeconds": 300},
            "socks5": {"enabled": False, "url": "socks5://127.0.0.1:1080"},
            "routing": {"default": "direct"}, "proxies": {}, "groups": {},
        },
        retry={"transient": {"enabled": False}, "recovery": {"oauthRefresh": True}},
    )
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg))
    monkeypatch.setattr(config, "CONFIG_PATH", str(path))
    monkeypatch.setattr(config, "_cache", None)
    monkeypatch.setattr(config, "_mtime", 0.0)
    monkeypatch.setattr(config, "_reload_callbacks", [])
    config.reload()
    monkeypatch.setattr(network, "_LAST_SIGNATURE", network._signature())
    config.on_reload(network.on_config_reload)
    # Stub only Telegram I/O, not config.update, network reset or HTTP lifecycle.
    monkeypatch.setattr(ui, "rebuild_session", lambda: None)
    monkeypatch.setattr(upstream, "_client_pool", upstream.SharedClientPool(upstream._new_client))
    drain.reset_for_tests()
    yield
    await upstream.close_client()
    drain.reset_for_tests()


CHANGES = [
    ("dns", "servers", ["223.5.5.5"]),
    ("dns", "timeoutSeconds", 4),
    ("dns", "cacheTtlSeconds", 301),
    ("socks5", "enabled", True),
    ("socks5", "url", "socks5://127.0.0.1:1081"),
]


@pytest.mark.parametrize("section,key,value", CHANGES)
async def test_network_signature_change_rebuilds_once_with_latest_settings(
    reload_env, monkeypatch, section, key, value,
):
    created = []
    factory = network.async_client

    def observe(**kwargs):
        client = factory(**kwargs)
        created.append((client, copy.deepcopy(config.get()["network"])))
        return client

    monkeypatch.setattr(network, "async_client", observe)
    old = upstream.create_client()
    config.update(lambda cfg: cfg["network"][section].update({key: value}))
    new = upstream.get_client()
    assert new is not old
    assert created[-1][1][section][key] == value
    assert upstream.get_client() is new
    config.reload()
    assert upstream.get_client() is new
    assert len(created) == 2


async def test_unchanged_dns_does_not_reset_client(reload_env):
    old = upstream.create_client()
    network.save_dns_servers(["8.8.8.8"])
    assert upstream.get_client() is old
    assert not old.is_closed


def _api_environment():
    m = fake._import_modules()
    fake._setup(m)
    from src.openai.channel.api_channel import OpenAIApiChannel

    ch = OpenAIApiChannel({
        "name": "reload-fixture", "type": "api", "enabled": True,
        "baseUrl": "https://fixture.example.test", "apiKey": "fixture-only",
        "protocol": "openai-chat",
        "models": [{"alias": "test-model", "real": "upstream-model"}],
    })
    fake._install_channels(m, [ch])
    return m


async def test_http_requests_continue_after_real_dns_save(reload_env, monkeypatch):
    m = _api_environment()
    calls = []

    def wire(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={
            "id": "chatcmpl-fixture", "object": "chat.completion", "model": "upstream-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "fixture-ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    monkeypatch.setattr(network, "async_client", lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(wire), **kw))
    upstream.create_client()
    body = {"model": "test-model", "messages": [{"role": "user", "content": "hello"}], "stream": False}

    async def request():
        req = fake.FakeRequest({"Authorization": "Bearer ccp-test"}, json.dumps(body).encode())
        return await m["openai_handler"].handle(req, ingress_protocol="chat")

    first = await request()
    assert first.status_code == 200, first.body
    network.save_dns_servers(["223.5.5.5"])
    for _ in range(2):
        response = await request()
        assert response.status_code == 200, response.body
        assert b"fixture-ok" in response.body
    assert len(calls) == 3


async def test_external_file_reload_rebuilds_without_changing_live_config(reload_env):
    old = upstream.create_client()
    path = Path(config.path())
    value = json.loads(path.read_text())
    value["network"]["dns"]["servers"] = ["223.5.5.5"]
    previous_mtime = path.stat().st_mtime
    path.write_text(json.dumps(value))
    os.utime(path, (previous_mtime + 2, previous_mtime + 2))
    assert config.get()["network"]["dns"]["servers"] == ["223.5.5.5"]
    assert upstream.get_client() is not old


def _worker_save_dns():
    errors = []

    def update():
        try:
            network.save_dns_servers(["223.5.5.5"])
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=update)
    worker.start()
    worker.join(timeout=3)
    assert not worker.is_alive(), "config callback must not block on the owner event loop"
    assert errors == []


class ObservedClient(httpx.AsyncClient):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.closed_event = asyncio.Event()
        self.close_count = 0

    async def aclose(self):
        self.close_count += 1
        await super().aclose()
        self.closed_event.set()


@pytest.mark.parametrize("cancel_stream", [False, True])
async def test_existing_sse_survives_worker_save_and_retires_on_completion_or_cancel(
    reload_env, monkeypatch, cancel_stream,
):
    m = _api_environment()
    before_id = m["log_db"]._get_conn().execute("SELECT COALESCE(MAX(id), 0) FROM request_log").fetchone()[0]
    clients = []
    finish_stream = asyncio.Event()

    class HoldingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            events = fake._chat_sse_response("stream-still-alive").content.split(b"\n\n")
            for event in events[:2]:
                yield event + b"\n\n"
            # Keep real upstream work in flight; a fully buffered fake may
            # legitimately finish in the background before downstream consumes it.
            await finish_stream.wait()
            for event in events[2:]:
                if event:
                    yield event + b"\n\n"

    def wire(request):
        if json.loads(request.content).get("stream"):
            return httpx.Response(200, stream=HoldingStream(), headers={"content-type": "text/event-stream"})
        return fake._chat_response("new-generation-ok")

    def factory(**kw):
        client = ObservedClient(transport=httpx.MockTransport(wire), **kw)
        clients.append(client)
        return client

    monkeypatch.setattr(network, "async_client", factory)
    old = upstream.create_client()

    async def request(stream):
        body = {"model": "test-model", "messages": [{"role": "user", "content": "hello"}], "stream": stream}
        req = fake.FakeRequest({"Authorization": "Bearer ccp-test"}, json.dumps(body).encode())
        return await m["openai_handler"].handle(req, ingress_protocol="chat")

    response = await request(True)
    assert response.status_code == 200
    iterator = response.body_iterator
    first = await anext(iterator)
    _worker_save_dns()
    await asyncio.sleep(0)
    assert not old.is_closed
    new_response = await request(False)
    assert new_response.status_code == 200 and b"new-generation-ok" in new_response.body
    assert len(clients) == 2 and clients[1] is not old
    assert not old.is_closed
    if cancel_stream:
        await iterator.aclose()
    else:
        finish_stream.set()
        chunks = [first] + [chunk async for chunk in iterator]
        text = b"".join(chunk.encode() if isinstance(chunk, str) else chunk for chunk in chunks)
        assert b"stream-still-alive" in text and b"[DONE]" in text
    await asyncio.wait_for(old.closed_event.wait(), 1)
    assert old.close_count == 1
    assert not clients[1].is_closed
    rows = m["log_db"]._get_conn().execute("SELECT status FROM request_log WHERE id > ? ORDER BY id", (before_id,)).fetchall()
    assert [row["status"] for row in rows] == (["cancelled", "success"] if cancel_stream else ["success", "success"])


async def test_health_distinguishes_pending_failed_and_stopped_without_constructing(reload_env, monkeypatch):
    _api_environment()
    import server

    calls = []
    fail = [False]

    def factory(**kw):
        calls.append(1)
        if fail[0]:
            raise ValueError("private proxy construction detail")
        return httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200)), **kw)

    monkeypatch.setattr(network, "async_client", factory)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        not_started = await client.get("/health")
        assert not_started.status_code == 503 and not_started.json()["status"] == "error"
        upstream.create_client()
        network.save_dns_servers(["223.5.5.5"])
        pending = await client.get("/health")
        assert pending.status_code == 200 and pending.json()["status"] == "ok"
        assert pending.json()["upstream_client"] == {"state": "rebuild_pending", "ready": True}
        assert len(calls) == 1  # Health never creates the pending client.
        fail[0] = True
        with pytest.raises(ValueError):
            upstream.get_client()
        failed = await client.get("/health")
        assert failed.status_code == 503 and failed.json()["status"] == "error"
        assert failed.json()["upstream_client"]["state"] == "construction_failed"
        assert "private" not in failed.text and len(calls) == 2
        fail[0] = False
        upstream.get_client()
        healthy = await client.get("/health")
        assert healthy.status_code == 200 and healthy.json()["status"] == "ok"
        await upstream.close_client()
        stopped = await client.get("/health")
        assert stopped.status_code == 503
        assert stopped.json()["upstream_client"]["state"] == "stopped"
        drain.begin("fixture-shutdown")
        draining = await client.get("/health")
        assert draining.status_code == 200 and draining.json()["status"] == "draining"
        assert len(calls) == 3


async def test_real_tcp_stream_remains_open_across_network_reload(reload_env):
    from src.transports.http import HttpStreamRequest
    from src.transports.http_runtime import _SharedStreamContext

    continue_old = asyncio.Event()
    old_finished = asyncio.Event()
    connections = []

    async def serve(reader, writer):
        connections.append(writer)
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            if b"/old " in headers:
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 6\r\nConnection: close\r\n\r\none")
                await writer.drain()
                await continue_old.wait()
                writer.write(b"two")
            else:
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\nConnection: close\r\n\r\nnew")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            old_finished.set()

    async with await asyncio.start_server(serve, "127.0.0.1", 0) as server_socket:
        port = server_socket.sockets[0].getsockname()[1]
        old = upstream.create_client()

        def request(path):
            return HttpStreamRequest("GET", f"http://127.0.0.1:{port}/{path}", {}, b"", 2, 2)

        try:
            async with _SharedStreamContext(request("old")) as response:
                chunks = response.aiter_bytes()
                assert await anext(chunks) == b"one"
                _worker_save_dns()
                async with _SharedStreamContext(request("new")) as new_response:
                    assert await new_response.aread() == b"new"
                assert not old.is_closed
                continue_old.set()
                assert b"".join([chunk async for chunk in chunks]) == b"two"
            await upstream.close_client()
            assert old.is_closed
        finally:
            continue_old.set()
            for writer in connections:
                writer.close()
            await asyncio.wait_for(old_finished.wait(), 2)


async def test_metadata_refresh_keeps_one_client_across_both_downloads(reload_env, monkeypatch):
    from src import model_pricing

    monkeypatch.setattr(network, "async_client", lambda **kw: ObservedClient(transport=httpx.MockTransport(lambda _: httpx.Response(200)), **kw))
    old = upstream.create_client()
    seen = []

    async def download(client, url, budget):
        seen.append(client)
        assert client is old and not old.is_closed
        if len(seen) == 1:
            _worker_save_dns()
            await asyncio.sleep(0)
            assert not old.is_closed
        return b"{}"

    monkeypatch.setattr(model_pricing, "_download_catalog_bounded", download)
    # Preserve real catalog validation; deliberately empty fixtures fail
    # after both downloads, which must still release their single shared lease.
    with pytest.raises(ValueError, match="catalog contains no token-priced models"):
        await model_pricing.refresh_once(force=True)
    assert seen == [old, old]
    await asyncio.wait_for(old.closed_event.wait(), 1)
    assert upstream.get_client() is not old


@pytest.mark.parametrize("stage", ["build", "pre_headers", "cancel"])
async def test_http_open_failure_or_cancel_releases_shared_lease(reload_env, monkeypatch, stage):
    from src.transports import http_runtime
    from types import SimpleNamespace

    _api_environment()
    entered = asyncio.Event()

    async def wire(request):
        entered.set()
        if stage == "cancel":
            await asyncio.Event().wait()
        raise httpx.ConnectError("fixture-only connection failure")

    monkeypatch.setattr(network, "async_client", lambda **kw: ObservedClient(transport=httpx.MockTransport(wire), **kw))
    old = upstream.create_client()
    if stage == "build":
        def fail_build(client, request):
            raise ValueError("fixture-only stream construction failure")
        monkeypatch.setattr(http_runtime, "open_stream", fail_build)
    task = asyncio.create_task(http_runtime.open_response_with_proxy_chain(
        channel=SimpleNamespace(), resolved_model="fixture-model",
        upstream_req=SimpleNamespace(method="POST", url="https://fixture.example.test", headers={}, body=b"{}"),
        connect_timeout=2, first_byte_timeout=2, idle_timeout=2, total_timeout=5,
        response_mode="stream", request_id="fixture-lease-cleanup",
    ))
    if stage == "cancel":
        await asyncio.wait_for(entered.wait(), 1)
        network.save_dns_servers(["223.5.5.5"])
        assert not old.is_closed
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        result = await task
        assert not result.ok
        network.save_dns_servers(["223.5.5.5"])
    await asyncio.wait_for(old.closed_event.wait(), 1)
    assert old.close_count == 1
    assert upstream.get_client() is not old


async def test_failed_rebuild_does_not_cool_healthy_channel_and_next_request_recovers(reload_env, monkeypatch, capsys):
    m = _api_environment()
    broken = [False]

    def factory(**kw):
        if broken[0]:
            raise ValueError("socks5://fixture-user:private-secret@proxy.invalid:1080")
        return httpx.AsyncClient(transport=httpx.MockTransport(lambda _: fake._chat_response("recovered")), **kw)

    monkeypatch.setattr(network, "async_client", factory)
    upstream.create_client()
    network.save_dns_servers(["223.5.5.5"])
    broken[0] = True

    async def request():
        body = {"model": "test-model", "messages": [{"role": "user", "content": "hello"}], "stream": False}
        req = fake.FakeRequest({"Authorization": "Bearer ccp-test"}, json.dumps(body).encode())
        return await m["openai_handler"].handle(req, ingress_protocol="chat")

    response = await request()
    assert response.status_code == 500  # Existing local-runtime error response contract.
    assert b"shared upstream HTTP client unavailable" in response.body
    output = capsys.readouterr()
    assert "private-secret" not in response.body.decode() + output.err + output.out
    assert m["cooldown"].get_state("api:reload-fixture", "upstream-model") is None
    assert upstream.client_health()["state"] == "construction_failed"
    broken[0] = False
    recovered = await request()
    assert recovered.status_code == 200 and b"recovered" in recovered.body
    assert upstream.client_health() == {"state": "ready", "ready": True}
