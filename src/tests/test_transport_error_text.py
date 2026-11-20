"""Transport failures stay diagnosable, and a body-upload disconnect stays quiet."""
import httpx
import pytest
from starlette.requests import ClientDisconnect

import server
from src.protocols.runtime import exception_text
from src.transports import http_runtime


def test_exception_text_names_messageless_exceptions():
    assert exception_text(httpx.ReadError("")) == "ReadError"
    assert exception_text(httpx.RemoteProtocolError("peer closed")) == "peer closed"


@pytest.mark.asyncio
async def test_stream_transport_error_without_message_keeps_exception_type():
    class Broken:
        async def __anext__(self):
            raise httpx.RemoteProtocolError("")

    class Tracker:
        def feed(self, _chunk):
            return None

    step = await http_runtime.read_next_stream_step(
        aiter=Broken(), channel=None, dynamic_map=None, tracker=Tracker(), builder=Tracker(),
        stream_translator=None, deadline_ts=0, start_time=0, idle_timeout=1,
    )
    assert step.kind == "error"
    assert step.message == "stream transport error: RemoteProtocolError"


@pytest.mark.asyncio
async def test_disconnect_while_reading_request_body_is_not_an_asgi_error(capsys):
    async def app(scope, receive, send):
        raise ClientDisconnect()

    sent = []

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/v1/chat/completions", "headers": []}
    await server._DrainHttpMiddleware(app)(scope, receive, send)

    assert sent == []
    assert "client disconnected before the request body was complete" in capsys.readouterr().out
