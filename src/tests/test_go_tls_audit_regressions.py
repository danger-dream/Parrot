"""Release-audit regressions: loopback h2 or local fault injection only."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import gzip
import socket
import threading
import time
from types import SimpleNamespace
import zlib

import h2.config
import h2.connection
import h2.events
import httpx
import pytest

from src.transports import go_tls, http_runtime
from src.transports.timing import HttpAttemptTiming, RoundTimeouts
from src.tests.test_go_tls_transport import tls_files, h2_server


@pytest.fixture(autouse=True)
def real_workers(monkeypatch):
    from src.tests.conftest import _ORIG_TO_THREAD
    monkeypatch.setattr(asyncio, "to_thread", _ORIG_TO_THREAD)


@asynccontextmanager
async def local_server(tls_files, *, early=False, stall_upload=False, body=b"ok", headers=()):
    state = {"active": set(), "headers": [], "body": bytearray()}

    async def accept(reader, writer):
        state["active"].add(asyncio.current_task())
        h = h2.connection.H2Connection(config=h2.config.H2Configuration(client_side=False))
        outgoing = {}

        def reply(sid):
            h.send_headers(sid, [(b":status", b"413" if early else b"200"), *headers])
            outgoing[sid] = 0

        try:
            h.initiate_connection()
            writer.write(h.data_to_send())
            await writer.drain()
            while data := await reader.read(65536):
                for event in h.receive_data(data):
                    if isinstance(event, h2.events.RequestReceived):
                        state["headers"].append(event.headers)
                        if early:
                            reply(event.stream_id)
                    elif isinstance(event, h2.events.DataReceived):
                        state["body"].extend(event.data)
                        if not early and not stall_upload:
                            h.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                    elif isinstance(event, h2.events.StreamEnded) and not early and not stall_upload:
                        reply(event.stream_id)
                for sid, pos in list(outgoing.items()):
                    while pos < len(body):
                        n = min(h.local_flow_control_window(sid), h.max_outbound_frame_size, len(body) - pos)
                        if n <= 0:
                            break
                        h.send_data(sid, body[pos:pos+n], end_stream=pos+n == len(body))
                        pos += n
                    outgoing[sid] = pos
                    if pos == len(body):
                        if not body:
                            h.end_stream(sid)
                        del outgoing[sid]
                writer.write(h.data_to_send())
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, asyncio.CancelledError):
                pass
            state["active"].discard(asyncio.current_task())

    server = await asyncio.start_server(accept, "127.0.0.1", 0, ssl=tls_files[0])
    state["url"] = f"https://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    try:
        yield state
    finally:
        server.close()
        await server.wait_closed()
        tasks = tuple(state["active"])
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_early_rejection_completes_nonstream_connection_without_fake_upload(tls_files):
    timing = HttpAttemptTiming(response_mode="non_stream")
    events = []

    async def trace(name, info):
        events.append(name)
        await timing.trace(name, info)

    async with local_server(tls_files, early=True, body=b"too large") as server:
        async with go_tls.create_client(timeout=httpx.Timeout(2), ca_pem=tls_files[1]) as client:
            async with client.stream("POST", server["url"], content=b"x" * (2 << 20), extensions={
                "trace": trace,
                "parrot_transport_lifecycle": {"mark_connection_complete": timing.mark_connection_complete},
            }) as response:
                assert response.status_code == 413
                assert timing.connection_complete and timing.dispatch_started
                assert "http2.send_request_body.complete" not in events
                assert await http_runtime._read_response_bytes(
                    response, timing, RoundTimeouts.from_config({"total": 2}),
                ) == b"too large"
        assert len(server["body"]) < 2 << 20


@pytest.mark.asyncio
async def test_actual_image_consumer_preserves_early_provider_error(tls_files, monkeypatch):
    from src.antigravity.images import _fingerprinted_post
    monkeypatch.setattr(http_runtime, "_resolve_http_route_chain", lambda *a, **kw: ([("direct", None)], None))
    monkeypatch.setattr(http_runtime, "create_impersonated_client", lambda profile, **kw: go_tls.create_client(ca_pem=tls_files[1], **kw))
    async with local_server(tls_files, early=True, body=b"too large") as server:
        status, _, body, _ = await _fingerprinted_post(
            SimpleNamespace(tls_fingerprint=go_tls.PROFILE), model="fixture-image",
            url=server["url"], headers={}, body=b"x" * (2 << 20), timeout_seconds=2,
        )
        assert (status, body) == (413, b"too large")


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["identity", "gzip", "deflate", "raw-deflate"])
async def test_raw_compression_counter_multivalue_and_charset(tls_files, encoding):
    plain = b"caf\xe9-" * 14000
    raw = {
        "identity": plain, "gzip": gzip.compress(plain),
        "deflate": zlib.compress(plain), "raw-deflate": zlib.compress(plain)[2:-4],
    }[encoding]
    coding = "deflate" if encoding == "raw-deflate" else encoding
    headers = [(b"content-encoding", coding.encode()), (b"content-length", str(len(raw)).encode()),
               (b"content-type", b"text/plain; charset=iso-8859-1"),
               (b"set-cookie", b"a=1"), (b"set-cookie", b"b=2")]
    counts = []
    async with local_server(tls_files, body=raw, headers=headers) as server:
        async with go_tls.create_client(timeout=httpx.Timeout(2), ca_pem=tls_files[1], byte_counter=lambda u, d: counts.append((u, d))) as client:
            response = await client.post(server["url"], data={"label": "汉字", "scope": "a b"})
        assert response.content == plain and response.text.startswith("café-")
        assert response.headers.get_list("set-cookie") == ["a=1", "b=2"]
        assert response.num_bytes_downloaded == sum(d for _, d in counts) == len(raw)
        assert bytes(server["body"]) == b"label=%E6%B1%89%E5%AD%97&scope=a+b"
        assert sum(u for u, _ in counts) == len(server["body"])


@pytest.mark.asyncio
async def test_large_body_start_runs_off_loop_and_content_is_lossless(tls_files, monkeypatch):
    from parrot_go_tls_client import Client
    start_threads, interval, beats = [], [], []

    class ObservedClient(Client):
        def start(self, *args, **kwargs):
            start_threads.append(threading.get_ident())
            interval.append(time.monotonic())
            try:
                return super().start(*args, **kwargs)
            finally:
                interval.append(time.monotonic())

    async def heartbeat():
        while True:
            beats.append(time.monotonic())
            await asyncio.sleep(.001)

    monkeypatch.setattr(go_tls, "native_client_type", lambda: ObservedClient)
    body = b"0123456789abcdef" * (1 << 20)
    pulse = asyncio.create_task(heartbeat())
    try:
        async with local_server(tls_files, body=body) as server:
            async with go_tls.create_client(timeout=httpx.Timeout(5), ca_pem=tls_files[1]) as client:
                response = await client.post(server["url"], content=body)
            assert response.content == body == bytes(server["body"])
            assert start_threads and start_threads[0] != threading.get_ident()
            # Real FFI marshalling must not monopolize the loop for all of start.
            # Avoid a machine-specific millisecond threshold on native CI jobs.
            assert any(interval[0] < tick < interval[1] for tick in beats)
    finally:
        pulse.cancel()
        await asyncio.gather(pulse, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("close_transport", [False, True])
async def test_cancel_during_native_start_owns_result_before_route_close(monkeypatch, close_transport):
    started, release = threading.Event(), threading.Event()
    calls, traces = [], []

    class Native:
        def poll(self, wait):
            return {"kind": "pending"}
        def cancel(self):
            calls.append("cancel")
        def close(self):
            calls.append("request_close")
            return {"kind": "error", "dispatched": True}

    class Client:
        def __init__(self, **kwargs):
            pass
        def start(self, *args, **kwargs):
            started.set()
            assert release.wait(2)
            return Native()
        def close(self):
            calls.append("client_close")

    class Owner:
        async def aclose(self):
            calls.append("route_close")

    monkeypatch.setattr(go_tls, "native_client_type", lambda: Client)
    transport = go_tls.GoTLSTransport(route_owner=Owner())
    async with httpx.AsyncClient(transport=transport) as client:
        task = asyncio.create_task(client.post("https://127.0.0.1/", content=b"payload", extensions={"trace": lambda name, info: traces.append(name)}))
        while not started.is_set():
            await asyncio.sleep(.001)
        closer = asyncio.create_task(transport.aclose()) if close_transport else None
        task.cancel()
        await asyncio.sleep(.01)
        task.cancel()
        assert not task.done() and "route_close" not in calls
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        if closer:
            await closer
    assert calls.index("request_close") < calls.index("client_close") < calls.index("route_close")
    assert calls.count("request_close") == calls.count("client_close") == calls.count("route_close") == 1
    assert "http2.send_request_headers.started" in traces
    assert not transport._streams and not transport._starts


@pytest.mark.asyncio
async def test_ss_lease_setup_failure_continues_sync_route_chain(tls_files, monkeypatch):
    from src.proxy.connector import SS2022Connector
    ss = SS2022Connector("fixture", "127.0.0.1", 9, "2022-blake3-aes-128-gcm", "AAAAAAAAAAAAAAAAAAAAAA==")
    monkeypatch.setattr(go_tls.network, "_configured_proxy_chain_or_none", lambda **kw: [("ss", ss), ("direct", SimpleNamespace(type="direct"))])
    original = go_tls.create_client
    monkeypatch.setattr(go_tls, "create_client", lambda **kw: original(ca_pem=tls_files[1], **kw))
    async with local_server(tls_files) as server:
        async def bind_failure(*args, **kwargs):
            raise OSError("local listener unavailable")
        monkeypatch.setattr(asyncio, "start_server", bind_failure)
        response = await asyncio.to_thread(go_tls.request_sync, "GET", server["url"], timeout=2)
        assert response.content == b"ok" and len(server["headers"]) == 1


@pytest.mark.asyncio
async def test_decode_error_still_releases_native_stream(tls_files):
    async with local_server(tls_files, body=b"bad gzip", headers=[(b"content-encoding", b"gzip")]) as server:
        async with go_tls.create_client(timeout=httpx.Timeout(2), ca_pem=tls_files[1]) as client:
            with pytest.raises(httpx.DecodingError):
                await client.get(server["url"])
            assert not client._transport._streams


@pytest.mark.asyncio
async def test_none_zero_timeouts_not_hidden_by_optional_brotli(tls_files):
    async with h2_server(tls_files) as server:
        async with go_tls.create_client(timeout=httpx.Timeout(2), ca_pem=tls_files[1]) as client:
            assert (await client.get(server["url"], timeout=None)).content == b"local-body"
            with pytest.raises(httpx.ConnectTimeout):
                await client.get(server["url"], timeout=0)
            with pytest.raises(httpx.ReadTimeout):
                await client.get(server["url"] + "/read-hang", timeout=httpx.Timeout(2, read=0))

@pytest.mark.asyncio
async def test_obs_text_headers_are_lossless_in_both_directions(tls_files):
    headers = [(b"x-name", b"caf\xe9"), (b"x-name", b"raw\xff"),
               (b"set-cookie", b"a=\xe9"), (b"set-cookie", b"b=\xff")]
    async with local_server(tls_files, headers=headers) as server:
        async with go_tls.create_client(timeout=httpx.Timeout(2), ca_pem=tls_files[1]) as client:
            response = await client.get(server["url"], headers=headers[:2])
        assert [(k, v) for k, v in server["headers"][0] if k == b"x-name"] == headers[:2]
        assert [(k.lower(), v) for k, v in response.headers.raw if k.lower() == b"x-name"] == headers[:2]
        assert [(k.lower(), v) for k, v in response.headers.raw if k.lower() == b"set-cookie"] == headers[2:]


@pytest.mark.asyncio
async def test_transport_close_cancels_dns_before_route_release(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    order = []

    async def resolver(host, timeout):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            await release.wait()
            order.append("dns_closed")

    class Owner:
        async def aclose(self):
            order.append("route_closed")

    monkeypatch.setattr(go_tls, "native_client_type", lambda: lambda **kw: pytest.fail("must not start after close"))
    transport = go_tls.GoTLSTransport(resolver=resolver, route_owner=Owner())
    async with httpx.AsyncClient(transport=transport) as client:
        request = asyncio.create_task(client.get("https://cancel-dns.invalid"))
        await entered.wait()
        close = asyncio.create_task(transport.aclose())
        await asyncio.sleep(.01)
        request.cancel()
        close.cancel()
        await asyncio.sleep(.01)
        assert not order
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await request
        with pytest.raises(asyncio.CancelledError):
            await close
    assert order == ["dns_closed", "route_closed"]
    assert not transport._resolutions and not transport._streams

@pytest.mark.asyncio
async def test_cancel_queued_start_never_constructs_native_client(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    original = asyncio.to_thread

    async def queued(func, *args, **kwargs):
        if func.__name__ == "start_native":
            entered.set()
            await release.wait()
        return await original(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", queued)
    monkeypatch.setattr(go_tls, "native_client_type", lambda: lambda **kw: pytest.fail("cancelled queued start must not construct client"))
    transport = go_tls.GoTLSTransport()
    async with httpx.AsyncClient(transport=transport) as client:
        request = asyncio.create_task(client.get("https://127.0.0.1/"))
        await entered.wait()
        request.cancel()
        await asyncio.sleep(.01)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await request
    assert not transport._streams and not transport._starts

@pytest.mark.asyncio
async def test_close_terminal_prevents_false_poll_event_route_replay(monkeypatch):
    from src.channel.base import UpstreamRequest
    removed, release = threading.Event(), threading.Event()
    polling, false_seen = asyncio.Event(), asyncio.Event()
    transports = []

    class Native:
        def poll(self, wait):
            polling.set()
            if removed.is_set():
                false_seen.set()
                return {"kind": "error", "code": "invalid_request", "dispatched": False}
            return {"kind": "pending"}
        def cancel(self):
            pass
        def close(self):
            removed.set()
            assert release.wait(2)
            return {"dispatched": True}

    class Client:
        def __init__(self, **kw):
            pass
        def start(self, *a, **kw):
            return Native()
        def close(self):
            pass

    def connector():
        return SimpleNamespace(type="direct", stats=SimpleNamespace(total_attempts=0, total_failures=0, total_successes=0), record_bytes=lambda *a: None)

    first, second = connector(), connector()
    monkeypatch.setattr(go_tls, "native_client_type", lambda: Client)
    monkeypatch.setattr(http_runtime, "_resolve_http_route_chain", lambda *a, **kw: ([("first", first), ("second", second)], None))

    def factory(profile, **kwargs):
        client = go_tls.create_client(**kwargs)
        transports.append(client._transport)
        return client

    monkeypatch.setattr(http_runtime, "create_impersonated_client", factory)
    task = asyncio.create_task(http_runtime.open_response_with_proxy_chain(
        channel=SimpleNamespace(tls_fingerprint=go_tls.PROFILE), resolved_model="fixture",
        upstream_req=UpstreamRequest(url="https://127.0.0.1/", headers={}, body=b"payload"),
        connect_timeout=1, first_byte_timeout=1, idle_timeout=1, total_timeout=3,
        response_mode="stream", request_id=None,
    ))
    await asyncio.wait_for(polling.wait(), 1)
    close = asyncio.create_task(transports[0].aclose())
    await asyncio.wait_for(false_seen.wait(), 1)
    assert not task.done()
    release.set()
    opened = await task
    await close
    assert opened.error is not None and second.stats.total_attempts == 0
    assert len(transports) == 1 and not transports[0]._streams


@pytest.mark.asyncio
async def test_sync_total_after_send_is_not_connect_failover(tls_files, monkeypatch):
    monkeypatch.setattr(go_tls.network, "_configured_proxy_chain_or_none", lambda **kw: [
        ("first", SimpleNamespace(type="direct")), ("second", SimpleNamespace(type="direct")),
    ])
    original = go_tls.create_client
    monkeypatch.setattr(go_tls, "create_client", lambda **kw: original(ca_pem=tls_files[1], **kw))
    async with h2_server(tls_files) as server:
        with pytest.raises(httpx.TimeoutException) as failure:
            await asyncio.to_thread(go_tls.request_sync, "POST", server["url"] + "/headers-hang", json={}, timeout=.15)
        assert failure.value.parrot_dispatched
        assert failure.value.parrot_native_code == "total_timeout"
        assert len(server["calls"]) == 1
        await asyncio.wait_for(server["closed"].wait(), 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("level", [False, True])
async def test_native_repeated_and_anyio_cancel_waits_route_close(tls_files, monkeypatch, level):
    import anyio
    from parrot_go_tls_client import Client
    calls = []

    class ObservedClient(Client):
        def close(self):
            super().close()
            calls.append("native_closed")

    class Owner:
        async def aclose(self):
            calls.append("route_begin")
            await asyncio.sleep(.04)
            calls.append("route_done")

    monkeypatch.setattr(go_tls, "native_client_type", lambda: ObservedClient)
    async with h2_server(tls_files) as server:
        transport = go_tls.GoTLSTransport(ca_pem=tls_files[1], route_owner=Owner())
        async with httpx.AsyncClient(transport=transport, timeout=2) as client:
            if level:
                with anyio.CancelScope() as scope:
                    async def cancel_scope():
                        await server["request"].wait()
                        scope.cancel()
                    cancel = asyncio.create_task(cancel_scope())
                    await client.get(server["url"] + "/headers-hang")
                await cancel
            else:
                request = asyncio.create_task(client.get(server["url"] + "/headers-hang"))
                await server["request"].wait()
                for _ in range(4):
                    request.cancel()
                    await asyncio.sleep(.005)
                with pytest.raises(asyncio.CancelledError):
                    await request
            assert calls == ["native_closed", "route_begin", "route_done"]
            assert not transport._streams
            await asyncio.wait_for(server["closed"].wait(), 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["dns", "library", "start"])
async def test_setup_failures_close_route_owner(phase, monkeypatch, tmp_path):
    calls = []

    class Owner:
        async def aclose(self):
            calls.append("route_closed")

    class BadStartClient:
        def __init__(self, **kw):
            pass
        def start(self, *a, **kw):
            raise RuntimeError("local start fixture failure")
        def close(self):
            calls.append("client_closed")

    async def resolve(host, timeout):
        if phase == "dns":
            raise OSError("local DNS fixture failure")
        return ["127.0.0.1"]

    if phase == "start":
        monkeypatch.setattr(go_tls, "native_client_type", lambda: BadStartClient)
    transport = go_tls.GoTLSTransport(resolver=resolve, route_owner=Owner(), lib_path=str(tmp_path / "absent.so"))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises((httpx.ConnectError, OSError, RuntimeError)):
            await client.get("https://127.0.0.1/")
    assert calls[-1] == "route_closed"
    if phase == "start":
        assert calls == ["client_closed", "route_closed"]
    assert not transport._streams and not transport._starts and not transport._resolutions


def _runtime_fixture(monkeypatch, tls_files, url, *, body=b"{}", mode="stream", chain=None):
    from src.channel.base import UpstreamRequest
    def direct():
        return SimpleNamespace(type="direct", stats=SimpleNamespace(total_attempts=0, total_failures=0, total_successes=0), record_bytes=lambda *args: None)
    first, second = direct(), direct()
    routes = chain or [("first", first), ("second", second)]
    monkeypatch.setattr(http_runtime, "_resolve_http_route_chain", lambda *a, **kw: (routes, None))
    monkeypatch.setattr(http_runtime, "create_impersonated_client", lambda profile, **kw: go_tls.create_client(ca_pem=tls_files[1], **kw))
    kwargs = dict(channel=SimpleNamespace(tls_fingerprint=go_tls.PROFILE), resolved_model="fixture",
                  upstream_req=UpstreamRequest(url=url, headers={}, body=body),
                  connect_timeout=.5, first_byte_timeout=.06, idle_timeout=.06, total_timeout=.4,
                  response_mode=mode, request_id=None)
    return kwargs, second


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,outcome", [("stream", "first_byte_timeout"), ("non_stream", "total_timeout")])
async def test_real_runtime_headers_timeout_does_not_replay(tls_files, monkeypatch, mode, outcome):
    async with h2_server(tls_files) as server:
        kwargs, second = _runtime_fixture(monkeypatch, tls_files, server["url"] + "/headers-hang", mode=mode)
        opened = await http_runtime.open_response_with_proxy_chain(**kwargs)
        assert opened.error.outcome == outcome
        assert second.stats.total_attempts == 0 and len(server["calls"]) == 1


@pytest.mark.asyncio
async def test_real_native_write_and_business_connection_timeout(tls_files, monkeypatch):
    async with local_server(tls_files, stall_upload=True) as server:
        async with go_tls.create_client(timeout=httpx.Timeout(1, write=.06), ca_pem=tls_files[1]) as client:
            with pytest.raises(httpx.WriteTimeout) as failure:
                await client.post(server["url"], content=b"x" * (2 << 20))
            assert failure.value.parrot_dispatched
    async with local_server(tls_files, stall_upload=True) as server:
        kwargs, second = _runtime_fixture(monkeypatch, tls_files, server["url"], body=b"x" * (2 << 20))
        kwargs["connect_timeout"] = .1
        opened = await http_runtime.open_response_with_proxy_chain(**kwargs)
        assert opened.error.outcome == "connection_timeout"
        assert second.stats.total_attempts == 0 and len(server["headers"]) == 1


@pytest.mark.asyncio
async def test_real_runtime_body_idle_timeout(tls_files, monkeypatch):
    from src.transports.timing import BusinessTimeoutError
    async with h2_server(tls_files) as server:
        kwargs, second = _runtime_fixture(monkeypatch, tls_files, server["url"] + "/stream")
        opened = await http_runtime.open_response_with_proxy_chain(**kwargs)
        assert opened.ok
        try:
            iterator = opened.response.aiter_bytes()
            assert await http_runtime.next_nonempty_http_chunk(iterator, opened.timing, opened.round_timeouts) == b"data: first\n\n"
            with pytest.raises(BusinessTimeoutError) as failure:
                await http_runtime.next_nonempty_http_chunk(iterator, opened.timing, opened.round_timeouts)
            assert failure.value.outcome == "idle_timeout" and second.stats.total_attempts == 0
        finally:
            await http_runtime.close_response_context(opened.ctx)
            await http_runtime.close_proxy_client(opened.proxy_client)
        await asyncio.wait_for(server["closed"].wait(), 1)


@pytest.mark.asyncio
async def test_real_runtime_connect_failure_can_use_next_route(tls_files, monkeypatch):
    # Keep an unlistened socket bound so the refused port cannot be reassigned.
    with socket.socket() as refused:
        refused.bind(("127.0.0.1", 0))
        bad = SimpleNamespace(type="socks5", url=f"socks5://127.0.0.1:{refused.getsockname()[1]}",
                              stats=SimpleNamespace(total_attempts=0, total_failures=0, total_successes=0), record_bytes=lambda *a: None)
        good = SimpleNamespace(type="direct", stats=SimpleNamespace(total_attempts=0, total_failures=0, total_successes=0), record_bytes=lambda *a: None)
        async with h2_server(tls_files) as server:
            kwargs, _ = _runtime_fixture(monkeypatch, tls_files, server["url"], chain=[("bad", bad), ("good", good)])
            opened = await http_runtime.open_response_with_proxy_chain(**kwargs)
            assert opened.ok and bad.stats.total_failures == good.stats.total_attempts == 1
            try:
                assert await http_runtime._read_response_bytes(opened.response, opened.timing, opened.round_timeouts) == b"local-body"
            finally:
                await http_runtime.close_response_context(opened.ctx)
                await http_runtime.close_proxy_client(opened.proxy_client)
