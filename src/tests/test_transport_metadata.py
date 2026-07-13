"""Transport metadata tests for Protocol Runtime Phase 6."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from src.transports import metadata_from_response, pick_forward_headers


def test_pick_forward_headers_preserves_existing_allowlist_and_lowercase_keys():
    headers = {
        "Content-Type": "text/event-stream",
        "X-Request-Id": "abc",
        "request-id": "def",
        "authorization": "secret",
    }

    assert pick_forward_headers(headers) == {
        "content-type": "text/event-stream",
        "x-request-id": "abc",
        "request-id": "def",
    }


def test_metadata_from_response_extracts_status_headers_and_content_type():
    resp = SimpleNamespace(
        status_code="200",
        headers={"Content-Type": "application/json", "x-request-id": "rid"},
    )

    meta = metadata_from_response(resp)

    assert meta.status_code == 200
    assert meta.content_type == "application/json"
    assert meta.forward_headers() == {
        "content-type": "application/json",
        "x-request-id": "rid",
    }


def test_open_stream_uses_existing_httpx_stream_parameters():
    from src.transports import HttpStreamRequest, open_stream

    class FakeClient:
        def __init__(self):
            self.kw = None

        def stream(self, method, url, **kwargs):
            self.kw = {"method": method, "url": url, **kwargs}
            return "ctx"

    client = FakeClient()
    req = HttpStreamRequest(
        method="POST",
        url="https://upstream.test/v1/messages",
        headers={"h": "v"},
        content=b"{}",
        connect_timeout=1.5,
        read_timeout=9.0,
        write_timeout=30.0,
        pool_timeout=1.5,
    )

    assert open_stream(client, req) == "ctx"
    assert client.kw["method"] == "POST"
    assert client.kw["url"] == "https://upstream.test/v1/messages"
    assert client.kw["headers"] == {"h": "v"}
    assert client.kw["content"] == b"{}"
    timeout = client.kw["timeout"]
    assert timeout.connect == 1.5
    assert timeout.read == 9.0
    assert timeout.write == 30.0
    assert timeout.pool == 1.5


def test_websocket_frame_helpers_are_protocol_agnostic():
    from src.transports import ws_event_type, ws_frame_size

    assert ws_frame_size(b"abc") == 3
    assert ws_frame_size("小夕") == len("小夕".encode("utf-8"))
    assert ws_event_type('{"type":"response.output_text.delta"}') == "response.output_text.delta"
    assert ws_event_type('{bad json') == ""
    assert ws_event_type(b'{"type":"binary"}') == ""


def test_http_response_headers_are_capped_by_first_byte_timeout(monkeypatch):
    from src.transports import http_runtime

    class SlowContext:
        def __init__(self):
            self.exited = False

        async def __aenter__(self):
            await asyncio.sleep(0.2)
            return SimpleNamespace(status_code=200, headers={})

        async def __aexit__(self, exc_type, exc, tb):
            self.exited = True

    ctx = SlowContext()
    monkeypatch.setattr(
        http_runtime,
        "_resolve_http_route_chain",
        lambda channel, model: ([('direct', None)], None),
    )
    monkeypatch.setattr(http_runtime.upstream, "get_client", lambda: object())
    monkeypatch.setattr(http_runtime, "open_stream", lambda client, request: ctx)

    result = asyncio.run(http_runtime.open_response_with_proxy_chain(
        channel=SimpleNamespace(),
        resolved_model="model",
        upstream_req=SimpleNamespace(
            method="POST", url="https://upstream.test", headers={}, body=b"{}",
        ),
        deadline_ts=time.time() + 5,
        connect_timeout=1,
        first_byte_timeout=0.02,
        request_id="header-timeout",
        retry_attempt_id=None,
    ))

    assert not result.ok
    assert result.error.outcome == "first_byte_timeout"
    assert "waiting for response headers" in result.error.error_detail
    assert result.error.connect_ms is not None
    assert ctx.exited is True


def test_http_response_header_wait_preserves_nearer_total_deadline(monkeypatch):
    from src.transports import http_runtime

    class SlowContext:
        async def __aenter__(self):
            await asyncio.sleep(0.2)
            return SimpleNamespace(status_code=200, headers={})

        async def __aexit__(self, exc_type, exc, tb):
            return None

    monkeypatch.setattr(
        http_runtime,
        "_resolve_http_route_chain",
        lambda channel, model: ([('direct', None)], None),
    )
    monkeypatch.setattr(http_runtime.upstream, "get_client", lambda: object())
    monkeypatch.setattr(http_runtime, "open_stream", lambda client, request: SlowContext())

    result = asyncio.run(http_runtime.open_response_with_proxy_chain(
        channel=SimpleNamespace(),
        resolved_model="model",
        upstream_req=SimpleNamespace(
            method="POST", url="https://upstream.test", headers={}, body=b"{}",
        ),
        deadline_ts=time.time() + 0.02,
        connect_timeout=1,
        first_byte_timeout=1,
        request_id="total-timeout",
        retry_attempt_id=None,
    ))

    assert not result.ok
    assert result.error.outcome == "total_timeout"


def test_http_response_headers_can_open_before_first_byte_timeout(monkeypatch):
    from src.transports import http_runtime

    response = SimpleNamespace(status_code=200, headers={})

    class FastContext:
        async def __aenter__(self):
            await asyncio.sleep(0)
            return response

        async def __aexit__(self, exc_type, exc, tb):
            return None

    ctx = FastContext()
    monkeypatch.setattr(
        http_runtime,
        "_resolve_http_route_chain",
        lambda channel, model: ([('direct', None)], None),
    )
    monkeypatch.setattr(http_runtime.upstream, "get_client", lambda: object())
    monkeypatch.setattr(http_runtime, "open_stream", lambda client, request: ctx)

    result = asyncio.run(http_runtime.open_response_with_proxy_chain(
        channel=SimpleNamespace(),
        resolved_model="model",
        upstream_req=SimpleNamespace(
            method="POST", url="https://upstream.test", headers={}, body=b"{}",
        ),
        deadline_ts=time.time() + 5,
        connect_timeout=1,
        first_byte_timeout=1,
        request_id="header-success",
        retry_attempt_id=None,
    ))

    assert result.ok
    assert result.ctx is ctx
    assert result.response is response
