"""Codex realtime OAuth relay tests."""

from __future__ import annotations

import os as _ap_os
import sys as _ap_sys

_ap_sys.path.insert(
    0,
    _ap_os.path.dirname(
        _ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.abspath(__file__)))
    ),
)
from src.tests import _isolation

_isolation.isolate()

import asyncio
import json
import os
import sys
from typing import Any

import httpx
import pytest
from fastapi.responses import Response
from starlette.requests import Request
from starlette.websockets import WebSocketState
from websockets.exceptions import ConnectionClosed


_NO_CLIENT_FRAME = object()


class FakeHeaders:
    def __init__(self, values: dict[str, str]):
        self._values = {key.lower(): value for key, value in values.items()}

    def get(self, key: str, default=None):
        return self._values.get(key.lower(), default)

    def items(self):
        return self._values.items()


class FakeRealtimeWebSocket:
    def __init__(
        self,
        *,
        authorization: str | None = "Bearer sk-realtime",
        headers: dict[str, str] | None = None,
        query_params: dict[str, str] | None = None,
        query_string: bytes | None = None,
        client_frame: str | bytes | object = '{"type":"session.update"}',
    ):
        header_values = {
            "openai-alpha": "quicksilver=v1",
            "x-session-id": "rt-session",
            "x-codex-installation-id": "install-test",
        }
        if authorization is not None:
            header_values["authorization"] = authorization
        header_values.update(headers or {})
        self.headers = FakeHeaders(header_values)

        if query_params is None:
            query_params = {
                "intent": "quicksilver",
                "model": "gpt-realtime-1.5",
            }
        self.query_params = dict(query_params)
        if query_string is None:
            query_string = b"intent=quicksilver&model=gpt-realtime-1.5"
        self.scope = {"query_string": query_string}

        self.application_state = WebSocketState.CONNECTING
        self.accepted = False
        self.sent_texts: list[str] = []
        self.sent_bytes: list[bytes] = []
        self.close_calls: list[tuple[int, str]] = []
        self._client_frame = client_frame
        self._client_frame_sent = False
        self.server_frame_sent = asyncio.Event()

    async def accept(self):
        self.accepted = True
        self.application_state = WebSocketState.CONNECTED

    async def receive(self):
        if not self._client_frame_sent and self._client_frame is not _NO_CLIENT_FRAME:
            self._client_frame_sent = True
            if isinstance(self._client_frame, str):
                return {"type": "websocket.receive", "text": self._client_frame}
            return {"type": "websocket.receive", "bytes": self._client_frame}
        await self.server_frame_sent.wait()
        self.application_state = WebSocketState.DISCONNECTED
        return {"type": "websocket.disconnect", "code": 1000}

    async def send_text(self, text: str):
        self.sent_texts.append(text)
        self.server_frame_sent.set()

    async def send_bytes(self, data: bytes):
        self.sent_bytes.append(data)
        self.server_frame_sent.set()

    async def close(self, code: int = 1000, reason: str = ""):
        self.close_calls.append((code, reason))
        self.application_state = WebSocketState.DISCONNECTED
        self.server_frame_sent.set()


class FakeUpstreamWebSocket:
    def __init__(self, server_frame: str | bytes = '{"type":"session.updated"}'):
        self.server_frame = server_frame
        self.sent: list[str | bytes] = []
        self.client_frame_received = asyncio.Event()
        self._server_frame_returned = False
        self.closed = False
        self.close_code = 1000
        self.close_reason = ""

    async def send(self, data):
        self.sent.append(data)
        self.client_frame_received.set()

    async def recv(self):
        await self.client_frame_received.wait()
        if not self._server_frame_returned:
            self._server_frame_returned = True
            return self.server_frame
        await asyncio.Event().wait()

    async def close(self):
        self.closed = True


class ClosingUpstreamWebSocket:
    def __init__(self, code: int, reason: str):
        self.close_code = code
        self.close_reason = reason
        self.closed = False

    async def send(self, _data):
        raise AssertionError("no downstream frame should be sent in this test")

    async def recv(self):
        raise ConnectionClosed(None, None)

    async def close(self):
        self.closed = True


def _import_modules():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if root not in sys.path:
        sys.path.insert(0, root)
    import server
    from src import apikey_limiter, concurrency, config, drain, oauth_manager
    from src.channel import registry
    from src.channel import openai_oauth_channel
    from src.channel.openai_oauth_channel import OpenAIOAuthChannel
    from src.openai import realtime

    return {
        "apikey_limiter": apikey_limiter,
        "concurrency": concurrency,
        "config": config,
        "drain": drain,
        "oauth_manager": oauth_manager,
        "registry": registry,
        "openai_oauth_channel": openai_oauth_channel,
        "OpenAIOAuthChannel": OpenAIOAuthChannel,
        "realtime": realtime,
        "server": server,
    }


def _setup(m):
    cfg = {
        "apiKeys": {
            "realtime-key": {"key": "sk-realtime", "allowedModels": []},
        },
        "oauthAccounts": [],
        "channels": [],
        "channelSelection": "order",
        "openaiOAuth": {
            "codexCliVersion": "0.153.4",
            "codexProtocolProfile": "rust-v0.153.4",
            "codexUpstreamUrl": "https://chatgpt.com/backend-api/codex/responses",
            "forceCodexCLI": True,
        },
        "network": {"routing": {"default": "direct"}},
        "apiKeyConcurrency": {"enabled": False},
        "concurrency": {"enabled": False},
        "timeouts": {"connect": 1, "firstByte": 1},
    }
    m["config"]._cache = cfg
    m["config"]._mtime = m["config"]._current_mtime()
    m["concurrency"]._slots.clear()
    m["apikey_limiter"]._slots.clear()
    m["realtime"]._call_bindings.clear()
    m["drain"].reset_for_tests()
    with m["registry"]._lock:
        m["registry"]._channels = {}
    return cfg


def _install_oauth_channel(
    m,
    cfg,
    *,
    email: str = "realtime@example.test",
    workspace_id: str = "workspace-realtime",
):
    account = {
        "email": email,
        "provider": "openai",
        "workspace_id": workspace_id,
        "enabled": True,
        "access_token": "unused-in-test",
        "refresh_token": "unused-in-test",
        "expired": "2999-01-01T00:00:00Z",
        "models": [],
    }
    channel = m["OpenAIOAuthChannel"](account)
    cfg["oauthAccounts"].append(account)
    with m["registry"]._lock:
        m["registry"]._channels[channel.key] = channel
    return channel


def _request_for_call(
    body: bytes,
    *,
    authorization: str | None = "Bearer sk-realtime",
) -> Request:
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    headers = [
        (b"content-type", b"application/json"),
        (b"openai-alpha", b"quicksilver=v2"),
        (b"x-session-id", b"session-1"),
    ]
    if authorization is not None:
        headers.insert(0, (b"authorization", authorization.encode("ascii")))

    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "path": "/backend-api/codex/realtime/calls",
            "query_string": b"intent=quicksilver&architecture=avas",
            "headers": headers,
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 443),
        },
        receive,
    )


async def _run_asgi_websocket(app, path: str) -> list[dict[str, Any]]:
    incoming = [{"type": "websocket.connect"}]
    sent: list[dict[str, Any]] = []

    async def receive():
        if incoming:
            return incoming.pop(0)
        await asyncio.Event().wait()

    async def send(message):
        sent.append(message)

    await app(
        {
            "type": "websocket",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "scheme": "ws",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 12345),
            "root_path": "",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": [(b"host", b"testserver")],
            "subprotocols": [],
            "state": {},
        },
        receive,
        send,
    )
    return sent


def test_realtime_ws_and_call_use_separate_upstream_bases(m):
    cfg = _setup(m)
    cfg["openaiOAuth"]["codexUpstreamUrl"] = (
        "https://codex.example.test/custom/codex/responses"
    )

    assert m["realtime"]._realtime_ws_url(
        "/v1/realtime",
        "intent=quicksilver&model=gpt-realtime-1.5",
    ) == (
        "wss://api.openai.com/v1/realtime"
        "?intent=quicksilver&model=gpt-realtime-1.5"
    )
    assert m["realtime"]._realtime_call_url(
        "intent=quicksilver&architecture=avas"
    ) == (
        "https://codex.example.test/custom/codex/realtime/calls"
        "?intent=quicksilver&architecture=avas"
    )


@pytest.mark.asyncio
async def test_oauth_channel_build_realtime_headers_uses_existing_identity(monkeypatch, m):
    cfg = _setup(m)
    channel = _install_oauth_channel(m, cfg)
    token_requests: list[str] = []

    async def fake_ensure_valid_token(account_key: str):
        token_requests.append(account_key)
        return "oauth-access-token"

    monkeypatch.setattr(m["oauth_manager"], "ensure_valid_token", fake_ensure_valid_token)

    headers = await channel.build_realtime_headers()

    assert token_requests == [channel.account_key]
    assert headers["authorization"] == "Bearer oauth-access-token"
    assert headers["chatgpt-account-id"] == "workspace-realtime"
    assert headers["originator"] == "codex_cli_rs"
    assert headers["version"]
    from src.openai.codex_constants import codex_cli_user_agent
    assert headers["user-agent"] == codex_cli_user_agent(cfg["openaiOAuth"])
    assert headers["x-codex-installation-id"] == channel.codex_device_installation_id
    for response_only_header in ("host", "accept", "content-type", "openai-beta"):
        assert response_only_header not in headers


@pytest.mark.asyncio
async def test_realtime_ws_injects_oauth_and_relays_text_frames(monkeypatch, m):
    cfg = _setup(m)
    channel = _install_oauth_channel(m, cfg)
    ws = FakeRealtimeWebSocket()
    upstream = FakeUpstreamWebSocket()

    async def fake_headers():
        return {
            "authorization": "Bearer oauth-access-token",
            "chatgpt-account-id": "workspace-realtime",
            "originator": "codex_cli_rs",
            "version": "test-version",
        }

    async def fake_connect(url, *, headers, channel: object, model):
        assert url == (
            "wss://api.openai.com/v1/realtime"
            "?intent=quicksilver&model=gpt-realtime-1.5"
        )
        assert headers["authorization"] == "Bearer oauth-access-token"
        assert headers["chatgpt-account-id"] == "workspace-realtime"
        assert headers["openai-alpha"] == "quicksilver=v1"
        assert headers["x-session-id"] == "rt-session"
        assert headers["x-codex-installation-id"] == channel.codex_device_installation_id
        assert headers["x-codex-installation-id"] != "install-test"
        assert model == "gpt-realtime-1.5"
        return upstream

    monkeypatch.setattr(channel, "build_realtime_headers", fake_headers)
    monkeypatch.setattr(m["realtime"], "_connect_realtime_upstream", fake_connect)

    await m["realtime"].handle_realtime_ws(ws, path="/v1/realtime")

    assert ws.accepted
    assert upstream.sent == ['{"type":"session.update"}']
    assert ws.sent_texts == ['{"type":"session.updated"}']
    assert ws.sent_bytes == []
    assert upstream.closed


@pytest.mark.asyncio
async def test_realtime_live_v3_relays_binary_frames_without_changes(monkeypatch, m):
    cfg = _setup(m)
    channel = _install_oauth_channel(m, cfg)
    client_frame = b"\x00\xffclient-audio"
    server_frame = b"\x80\x01server-audio"
    ws = FakeRealtimeWebSocket(
        headers={"openai-alpha": "quicksilver=v2"},
        client_frame=client_frame,
    )
    upstream = FakeUpstreamWebSocket(server_frame)

    async def fake_headers():
        return {
            "authorization": "Bearer oauth-v3",
            "chatgpt-account-id": "workspace-realtime",
        }

    async def fake_connect(url, *, headers, channel: object, model):
        assert url == (
            "wss://api.openai.com/v1/live"
            "?intent=quicksilver&model=gpt-realtime-1.5"
        )
        assert headers["authorization"] == "Bearer oauth-v3"
        assert headers["openai-alpha"] == "quicksilver=v2"
        assert model == "gpt-realtime-1.5"
        return upstream

    monkeypatch.setattr(channel, "build_realtime_headers", fake_headers)
    monkeypatch.setattr(m["realtime"], "_connect_realtime_upstream", fake_connect)

    await m["realtime"].handle_realtime_ws(ws, path="/v1/live")

    assert ws.accepted
    assert upstream.sent == [client_frame]
    assert ws.sent_bytes == [server_frame]
    assert ws.sent_texts == []
    assert upstream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("sideband_kind", ["v1_query", "v3_path"])
async def test_webrtc_sideband_reuses_bound_oauth_channel(
    monkeypatch, m, sideband_kind,
):
    cfg = _setup(m)
    bound_channel = _install_oauth_channel(
        m,
        cfg,
        email="bound@example.test",
        workspace_id="workspace-bound",
    )
    other_channel = _install_oauth_channel(
        m,
        cfg,
        email="other@example.test",
        workspace_id="workspace-other",
    )
    call_id = f"rtc_{sideband_kind}"
    await m["realtime"]._store_call_binding(
        call_id,
        channel=bound_channel,
        api_key_name="realtime-key",
        model="gpt-realtime-bound",
    )

    if sideband_kind == "v1_query":
        path = "/v1/realtime"
        live_call_id = None
        ws = FakeRealtimeWebSocket(
            query_params={"call_id": call_id},
            query_string=f"call_id={call_id}".encode("ascii"),
        )
        expected_url = (
            f"wss://api.openai.com/v1/realtime?call_id={call_id}"
        )
    else:
        path = f"/v1/live/{call_id}"
        live_call_id = call_id
        ws = FakeRealtimeWebSocket(
            headers={"openai-alpha": "quicksilver=v2"},
            query_params={},
            query_string=b"",
        )
        expected_url = f"wss://api.openai.com/v1/live/{call_id}"

    upstream = FakeUpstreamWebSocket()
    header_calls: list[str] = []

    async def bound_headers():
        header_calls.append("bound")
        return {
            "authorization": "Bearer oauth-bound",
            "chatgpt-account-id": "workspace-bound",
        }

    async def other_headers():
        header_calls.append("other")
        return {"authorization": "Bearer oauth-other"}

    async def fake_connect(url, *, headers, channel: object, model):
        assert url == expected_url
        assert channel is bound_channel
        assert headers["authorization"] == "Bearer oauth-bound"
        assert model == "gpt-realtime-bound"
        return upstream

    monkeypatch.setattr(bound_channel, "build_realtime_headers", bound_headers)
    monkeypatch.setattr(other_channel, "build_realtime_headers", other_headers)
    monkeypatch.setattr(m["realtime"], "_connect_realtime_upstream", fake_connect)

    await m["realtime"].handle_realtime_ws(
        ws,
        path=path,
        live_call_id=live_call_id,
    )

    assert ws.accepted
    assert header_calls == ["bound"]
    assert upstream.sent == ['{"type":"session.update"}']
    assert upstream.closed


@pytest.mark.asyncio
async def test_upstream_close_code_and_reason_reach_downstream(monkeypatch, m):
    cfg = _setup(m)
    channel = _install_oauth_channel(m, cfg)
    ws = FakeRealtimeWebSocket(client_frame=_NO_CLIENT_FRAME)
    upstream = ClosingUpstreamWebSocket(4008, "realtime session complete")

    async def fake_headers():
        return {"authorization": "Bearer oauth-access-token"}

    async def fake_connect(_url, *, headers, channel: object, model):
        assert headers["authorization"] == "Bearer oauth-access-token"
        return upstream

    monkeypatch.setattr(channel, "build_realtime_headers", fake_headers)
    monkeypatch.setattr(m["realtime"], "_connect_realtime_upstream", fake_connect)

    await m["realtime"].handle_realtime_ws(ws, path="/v1/realtime")

    assert ws.accepted
    assert ws.close_calls == [(4008, "realtime session complete")]
    assert upstream.closed


@pytest.mark.asyncio
async def test_realtime_ws_rejects_invalid_api_key_before_upstream(monkeypatch, m):
    _setup(m)
    ws = FakeRealtimeWebSocket(authorization="Bearer not-a-parrot-key")

    async def must_not_connect(*_args, **_kwargs):
        raise AssertionError("invalid API key must not reach the upstream connector")

    monkeypatch.setattr(m["realtime"], "_connect_realtime_upstream", must_not_connect)

    await m["realtime"].handle_realtime_ws(ws, path="/v1/realtime")

    assert not ws.accepted
    assert ws.close_calls == [(4401, "Invalid API key")]


@pytest.mark.asyncio
async def test_realtime_ws_rejects_disallowed_model_before_upstream(monkeypatch, m):
    cfg = _setup(m)
    cfg["apiKeys"]["realtime-key"]["allowedModels"] = ["allowed-realtime-model"]
    ws = FakeRealtimeWebSocket()

    async def must_not_connect(*_args, **_kwargs):
        raise AssertionError("disallowed model must not reach the upstream connector")

    monkeypatch.setattr(m["realtime"], "_connect_realtime_upstream", must_not_connect)

    await m["realtime"].handle_realtime_ws(ws, path="/v1/realtime")

    assert not ws.accepted
    assert ws.close_calls == [(4403, "model is not allowed for this API key")]


@pytest.mark.asyncio
async def test_realtime_call_keeps_backend_body_and_binds_sideband_account(monkeypatch, m):
    cfg = _setup(m)
    channel = _install_oauth_channel(m, cfg)
    body = json.dumps(
        {
            "sdp": "v=offer\\r\\n",
            "session": {"type": "quicksilver", "model": "gpt-realtime-1.5"},
        },
        separators=(",", ":"),
    ).encode("utf-8")

    async def fake_headers():
        return {
            "authorization": "Bearer oauth-access-token",
            "chatgpt-account-id": "workspace-realtime",
            "originator": "codex_cli_rs",
        }

    async def fake_post(url, *, headers, body: bytes, channel: object, model):
        assert url == (
            "https://chatgpt.com/backend-api/codex/realtime/calls"
            "?intent=quicksilver&architecture=avas"
        )
        assert headers["authorization"] == "Bearer oauth-access-token"
        assert headers["chatgpt-account-id"] == "workspace-realtime"
        assert headers["openai-alpha"] == "quicksilver=v2"
        assert headers["content-type"] == "application/json"
        assert body == request_body
        assert model == "gpt-realtime-1.5"
        return httpx.Response(
            200,
            content=b"v=answer\\r\\n",
            headers={
                "content-type": "application/sdp",
                "location": "/v1/realtime/calls/calls/rtc_test_42",
                "set-cookie": "must-not-leak=1",
            },
        )

    request_body = body
    monkeypatch.setattr(channel, "build_realtime_headers", fake_headers)
    monkeypatch.setattr(m["realtime"], "_post_realtime_call", fake_post)

    response = await m["realtime"].handle_realtime_call(_request_for_call(body))

    assert response.status_code == 200
    assert response.body == b"v=answer\\r\\n"
    assert response.headers["location"] == "/v1/realtime/calls/calls/rtc_test_42"
    assert "set-cookie" not in response.headers

    bound_channel, binding, error = await m["realtime"]._resolve_bound_channel(
        "rtc_test_42",
        api_key_name="realtime-key",
    )
    assert error == ""
    assert bound_channel is channel
    assert binding is not None
    assert binding.model == "gpt-realtime-1.5"


@pytest.mark.asyncio
async def test_realtime_call_rejects_auth_and_model_before_upstream(monkeypatch, m):
    cfg = _setup(m)
    body = json.dumps(
        {"sdp": "offer", "session": {"model": "blocked-realtime-model"}}
    ).encode("utf-8")

    async def must_not_post(*_args, **_kwargs):
        raise AssertionError("rejected call must not reach upstream")

    monkeypatch.setattr(m["realtime"], "_post_realtime_call", must_not_post)

    unauthorized = await m["realtime"].handle_realtime_call(
        _request_for_call(body, authorization=None)
    )
    assert unauthorized.status_code == 401
    assert json.loads(unauthorized.body)["error"]["message"] == "Missing API key"

    cfg["apiKeys"]["realtime-key"]["allowedModels"] = ["allowed-realtime-model"]
    disallowed = await m["realtime"].handle_realtime_call(_request_for_call(body))
    assert disallowed.status_code == 403
    assert json.loads(disallowed.body)["error"]["message"] == (
        "model is not allowed for this API key"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route_path", "expected_path", "expected_call_id"),
    [
        ("/v1/realtime", "/v1/realtime", None),
        ("/v1/live", "/v1/live", None),
        ("/v1/live/rtc_route_test", "/v1/live/rtc_route_test", "rtc_route_test"),
    ],
)
async def test_fastapi_websocket_routes_invoke_realtime_handler(
    monkeypatch,
    m,
    route_path,
    expected_path,
    expected_call_id,
):
    _setup(m)
    calls: list[tuple[str, str | None]] = []

    async def fake_handle(websocket, *, path, live_call_id=None):
        calls.append((path, live_call_id))
        await websocket.accept()
        await websocket.close(code=1000, reason="route-ok")

    monkeypatch.setattr(m["realtime"], "handle_realtime_ws", fake_handle)

    sent = await _run_asgi_websocket(m["server"].app, route_path)

    assert calls == [(expected_path, expected_call_id)]
    assert [message["type"] for message in sent] == [
        "websocket.accept",
        "websocket.close",
    ]
    assert sent[-1]["code"] == 1000
    assert sent[-1]["reason"] == "route-ok"


@pytest.mark.asyncio
async def test_fastapi_realtime_call_route_invokes_http_handler(monkeypatch, m):
    _setup(m)
    received: list[tuple[str, bytes]] = []

    async def fake_handle(request):
        received.append((request.url.path, await request.body()))
        return Response(content=b"route-answer", status_code=202, media_type="application/sdp")

    monkeypatch.setattr(m["realtime"], "handle_realtime_call", fake_handle)
    transport = httpx.ASGITransport(app=m["server"].app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/backend-api/codex/realtime/calls?intent=quicksilver",
            headers={
                "authorization": "Bearer sk-realtime",
                "content-type": "application/json",
            },
            content=b'{"sdp":"route-offer"}',
        )

    assert response.status_code == 202
    assert response.content == b"route-answer"
    assert received == [
        (
            "/backend-api/codex/realtime/calls",
            b'{"sdp":"route-offer"}',
        )
    ]
