from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from src import config
from src.transports import fingerprint


class FakeResponse:
    def __init__(self, *, status=200, headers=None, chunks=(b"ok",), version=3):
        self.status_code = status
        self.headers = httpx.Headers(headers or {})
        self.http_version = version
        self.reason = "OK"
        self._chunks = chunks
        self.quit_now = asyncio.Event()
        self.astream_task = None

    async def aiter_content(self):
        for chunk in self._chunks:
            yield chunk


class FakeSession:
    response = FakeResponse()
    error = None
    block = False
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.calls = []
        self.closed = False
        self.__class__.instances.append(self)

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.block:
            await asyncio.Event().wait()
        if self.error is not None:
            raise self.error
        return self.response

    async def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def fake_backend(monkeypatch):
    FakeSession.instances = []
    FakeSession.error = None
    FakeSession.block = False
    FakeSession.response = FakeResponse()
    monkeypatch.setattr(fingerprint, "_AsyncSession", FakeSession)


def test_default_is_disabled_and_route_boundary_is_explicit():
    setting = config.DEFAULT_CONFIG["antigravityOAuth"]["tlsFingerprint"]
    assert setting == {"enabled": False, "profile": "chrome131"}
    assert fingerprint.supports_route("direct")
    assert fingerprint.supports_route("socks5")
    assert fingerprint.supports_route("ss2022")
    assert fingerprint.normalize_proxy_url("socks5://127.0.0.1:1080") == "socks5h://127.0.0.1:1080"


def test_timeout_conversion_and_profile_validation():
    assert fingerprint.curl_timeout_from_extensions(
        {"timeout": {"connect": 1.25, "read": 9}}
    ) == (1.25, 9.0)
    assert fingerprint.validate_profile("chrome131") == "chrome131"
    with pytest.raises(ValueError):
        fingerprint.validate_profile("bad profile")


@pytest.mark.asyncio
async def test_success_preserves_redirect_boundary_decoding_dispatch_and_close():
    FakeSession.response = FakeResponse(
        status=302,
        headers={"location": "/next", "content-encoding": "gzip", "content-length": "99"},
        chunks=(b"decoded",),
    )
    events = []
    handoffs = []
    byte_counts = []

    async def trace(name, info):
        events.append(name)

    transport = fingerprint.ImpersonatedTransport(
        "chrome131", proxy="socks5://127.0.0.1:1080",
        byte_counter=lambda up, down: byte_counts.append((up, down)),
    )
    request = httpx.Request(
        "POST", "https://example.invalid/start", content=b"payload",
        extensions={
            "timeout": {"connect": 2.0, "read": 8.0},
            "trace": trace,
            "parrot_transport_lifecycle": {
                "mark_connection_complete": lambda: handoffs.append(True),
            },
        },
    )
    response = await transport.handle_async_request(request)
    assert response.status_code == 302
    assert response.headers["location"] == "/next"
    assert "content-encoding" not in response.headers
    assert "content-length" not in response.headers
    assert b"".join([chunk async for chunk in response.aiter_bytes()]) == b"decoded"
    assert handoffs == [True]
    assert events == ["http2.send_request_headers.started"]
    assert byte_counts == [(7, 0), (0, 7)]

    session = FakeSession.instances[0]
    assert session.kwargs["impersonate"] == "chrome131"
    assert session.kwargs["proxy"] == "socks5h://127.0.0.1:1080"
    call = session.calls[0][2]
    assert call["allow_redirects"] is False
    assert call["timeout"] == (2.0, 8.0)
    assert call["accept_encoding"] == "gzip, deflate, br"
    await response.aclose()
    assert session.closed


@pytest.mark.asyncio
async def test_timeout_is_mapped_and_ambiguous_attempt_is_marked_dispatched(monkeypatch):
    class CurlTimeout(Exception):
        pass

    monkeypatch.setattr(fingerprint, "_curl_errors", SimpleNamespace(Timeout=CurlTimeout))
    FakeSession.error = CurlTimeout("timed out")
    events = []

    async def trace(name, info):
        events.append(name)

    transport = fingerprint.ImpersonatedTransport("chrome131")
    request = httpx.Request("POST", "https://example.invalid", content=b"x", extensions={"trace": trace})
    with pytest.raises(httpx.TimeoutException):
        await transport.handle_async_request(request)
    assert events == ["http2.send_request_headers.started"]


@pytest.mark.asyncio
async def test_known_connect_failure_stays_pre_dispatch_and_maps_to_httpx(monkeypatch):
    class CurlConnectionError(Exception):
        def __init__(self, message, code):
            super().__init__(message)
            self.code = code

    codes = SimpleNamespace(COULDNT_CONNECT=7)
    monkeypatch.setattr(
        fingerprint, "_curl_errors",
        SimpleNamespace(ConnectionError=CurlConnectionError),
    )
    monkeypatch.setattr(fingerprint, "_CurlECode", codes)
    FakeSession.error = CurlConnectionError("connect failed", 7)
    events = []

    async def trace(name, info):
        events.append(name)

    transport = fingerprint.ImpersonatedTransport("chrome131")
    request = httpx.Request("POST", "https://example.invalid", extensions={"trace": trace})
    with pytest.raises(httpx.ConnectError):
        await transport.handle_async_request(request)
    assert events == []


@pytest.mark.asyncio
async def test_cancellation_closes_handle_route_owner_and_marks_attempt_dispatched():
    FakeSession.block = True
    events = []

    class RouteOwner:
        closed = False

        async def aclose(self):
            self.closed = True

    owner = RouteOwner()

    async def trace(name, info):
        events.append(name)

    transport = fingerprint.ImpersonatedTransport("chrome131", route_owner=owner)
    request = httpx.Request("POST", "https://example.invalid", extensions={"trace": trace})
    task = asyncio.create_task(transport.handle_async_request(request))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert events == ["http2.send_request_headers.started"]
    assert FakeSession.instances[0].closed
    assert owner.closed


@pytest.mark.asyncio
async def test_route_owner_auth_safe_failure_and_close_order(monkeypatch):
    class CurlProxyFailure(Exception):
        pass

    class RouteOwner:
        def __init__(self):
            self.closed_after_curl = []

        def safe_before_dispatch(self):
            return True

        async def aclose(self):
            self.closed_after_curl.append(FakeSession.instances[0].closed)

    owner = RouteOwner()
    monkeypatch.setattr(
        fingerprint, "_curl_errors",
        SimpleNamespace(RequestException=CurlProxyFailure),
    )
    FakeSession.error = CurlProxyFailure("CONNECT rejected")
    events = []

    async def trace(name, info):
        events.append(name)

    transport = fingerprint.ImpersonatedTransport(
        "chrome131",
        proxy="http://127.0.0.1:12345",
        proxy_auth=("one-time-user", "one-time-password"),
        route_owner=owner,
    )
    request = httpx.Request(
        "POST", "https://example.invalid", extensions={"trace": trace},
    )
    with pytest.raises(httpx.ProtocolError):
        await transport.handle_async_request(request)
    assert events == []
    assert FakeSession.instances[0].kwargs["proxy_auth"] == (
        "one-time-user", "one-time-password",
    )
    await transport.aclose()
    assert owner.closed_after_curl == [True]
