"""Protocol Runtime fake upstream integration tests.

Covers the Phase 8 fake-upstream matrix slice:
- Anthropic client -> OpenAI Chat HTTP upstream
- Anthropic client -> OpenAI Responses HTTP upstream
- OpenAI Chat client -> Anthropic HTTP upstream
- OpenAI Responses client -> Anthropic HTTP upstream
- OpenAI Responses WS client -> OpenAI Responses HTTP/SSE upstream
- HTTP Responses client -> OpenAI Responses WebSocket upstream
"""

from __future__ import annotations

# Test isolation: redirect config/state/logs before importing Parrot modules.
import os as _ap_os
import sys as _ap_sys

_ap_sys.path.insert(0, _ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.abspath(__file__)))))
from src.tests import _isolation

_isolation.isolate()

import asyncio
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest


def _import_modules():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if root not in sys.path:
        sys.path.insert(0, root)
    from src import (
        affinity,
        auth,
        config,
        cooldown,
        failover,
        log_db,
        scheduler,
        scorer,
        state_db,
        upstream,
    )
    from src.channel import api_channel, registry
    from src.openai import handler as openai_handler
    from src.openai import responses_ws
    from src.openai.channel.registration import register_factories

    register_factories()
    return {
        "affinity": affinity,
        "auth": auth,
        "config": config,
        "cooldown": cooldown,
        "failover": failover,
        "log_db": log_db,
        "scheduler": scheduler,
        "scorer": scorer,
        "state_db": state_db,
        "upstream": upstream,
        "registry": registry,
        "api_channel": api_channel,
        "openai_handler": openai_handler,
        "responses_ws": responses_ws,
    }


def _setup(m):
    m["state_db"].init()
    m["log_db"].init()
    m["state_db"].perf_delete()
    m["state_db"].error_delete()
    m["state_db"].affinity_delete()
    m["state_db"].client_affinity_delete()
    for mod_name in ("affinity", "cooldown", "scorer"):
        mod = m[mod_name]
        mod._initialized = False
    m["affinity"]._client_initialized = False
    m["affinity"].init()
    m["affinity"].client_init()
    m["cooldown"].init()
    m["scorer"].init()


class MockRouter:
    def __init__(self):
        self.handlers: dict[str, callable] = {}
        self.requests: list[httpx.Request] = []

    def register(self, base_url: str, handler):
        self.handlers[base_url.rstrip("/")] = handler

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url_str = str(request.url)
        for base, handler in self.handlers.items():
            if url_str.startswith(base):
                return handler(request)
        return httpx.Response(404, text="no mock")


class ChunkedByteStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]):
        self.chunks = list(chunks)

    async def __aiter__(self):
        for chunk in self.chunks:
            await asyncio.sleep(0)
            yield chunk


class TerminalThenHangByteStream(httpx.AsyncByteStream):
    """Yield complete Responses payload chunk(s) but deliberately never send EOF."""

    def __init__(self, payload: bytes | list[bytes]):
        self.payloads = [payload] if isinstance(payload, bytes) else list(payload)
        self.release = asyncio.Event()
        self.closed = asyncio.Event()

    async def __aiter__(self):
        for payload in self.payloads:
            yield payload
        await self.release.wait()

    async def aclose(self):
        self.closed.set()
        self.release.set()


def _json_request(req: httpx.Request) -> dict:
    return json.loads(req.content.decode("utf-8"))


def _make_openai_channel(name: str, base_url: str, *, protocol: str, alias: str, real: str, extra: dict | None = None):
    from src.openai.channel.api_channel import OpenAIApiChannel

    entry = {
        "name": name,
        "type": "api",
        "baseUrl": base_url,
        "apiKey": "sk-x",
        "protocol": protocol,
        "models": [{"real": real, "alias": alias}],
        "enabled": True,
    }
    if extra:
        entry.update(extra)
    return OpenAIApiChannel(entry)


def _make_anthropic_channel(m, name: str, base_url: str, *, alias: str, real: str):
    return m["api_channel"].ApiChannel({
        "name": name,
        "type": "api",
        "baseUrl": base_url,
        "apiKey": "sk-x",
        "models": [{"real": real, "alias": alias}],
        "cc_mimicry": False,
        "enabled": True,
    })


def _make_openai_oauth_channel(email: str = "fake-ws@example.com"):
    from src.channel.openai_oauth_channel import OpenAIOAuthChannel

    return OpenAIOAuthChannel({
        "email": email,
        "provider": "openai",
        "accountKey": f"openai:{email}",
        "accessToken": "tok",
        "refreshToken": "rt",
        "expiresAt": 32503680000,
        "models": ["gpt-5"],
    })


def _install_channels(m, channels):
    reg = m["registry"]
    with reg._lock:
        reg._channels = {ch.key: ch for ch in channels}


def _install_keys(m, keys: dict):
    def _mutate(cfg):
        cfg["apiKeys"] = keys
    m["config"].update(_mutate)


def _default_key(name="k", key="ccp-test"):
    return {name: {"key": key, "allowedModels": []}}


class FakeHeaders:
    def __init__(self, data):
        self._d = {k.lower(): v for k, v in data.items()}

    def get(self, k, default=None):
        return self._d.get(k.lower(), default)

    def items(self):
        return self._d.items()

    def keys(self):
        return self._d.keys()

    def __getitem__(self, k):
        return self._d[k.lower()]

    def __iter__(self):
        return iter(self._d.keys())

    def __len__(self):
        return len(self._d)


class FakeClient:
    def __init__(self, host="1.2.3.4"):
        self.host = host


class FakeRequest:
    def __init__(self, headers, body_bytes, client_ip="1.2.3.4"):
        self.headers = FakeHeaders(headers)
        self._body = body_bytes
        self.client = FakeClient(client_ip)

    async def body(self):
        return self._body


class FakeWebSocket:
    def __init__(self, first_obj: dict, *, extra_headers: dict | None = None):
        headers = {
            "Authorization": "Bearer sk-ws",
            "user-agent": "codex_cli_rs/0.125.0",
            "x-codex-turn-metadata": "turn-meta",
        }
        if extra_headers:
            headers.update(extra_headers)
        self.headers = FakeHeaders(headers)
        self.client = SimpleNamespace(host="1.2.3.4")
        self.application_state = None
        self._first_text = json.dumps(first_obj)
        self.sent_texts: list[str] = []
        self.close_calls: list[tuple[int, str]] = []
        self.accepted = False

    async def accept(self):
        from starlette.websockets import WebSocketState
        self.accepted = True
        self.application_state = WebSocketState.CONNECTED

    async def receive(self):
        if self._first_text is not None:
            text = self._first_text
            self._first_text = None
            return {"type": "websocket.receive", "text": text}
        return {"type": "websocket.disconnect", "code": 1000}

    async def send_text(self, text: str):
        self.sent_texts.append(text)

    async def send_bytes(self, data: bytes):
        self.sent_texts.append(data.decode("utf-8", errors="replace"))

    async def close(self, code: int = 1000, reason: str = ""):
        from starlette.websockets import WebSocketState
        self.close_calls.append((code, reason))
        self.application_state = WebSocketState.DISCONNECTED


class FakeOAuthResponseWs:
    def __init__(self, events: list[dict]):
        self.sent: list[str] = []
        self._events = [json.dumps(e) for e in events]
        self.closed = False
        self.response = SimpleNamespace(headers={})

    async def send(self, data: str | bytes, text: bool | None = None):
        self.sent.append(data.decode("utf-8") if isinstance(data, bytes) else data)
        await asyncio.sleep(0)

    async def recv(self):
        if self._events:
            return self._events.pop(0)
        import websockets
        raise websockets.ConnectionClosed(None, None)

    async def close(self, *args, **kwargs):
        self.closed = True
        return None


async def _call_openai_handler(m, router: MockRouter, ingress_protocol: str, body: dict):
    transport = httpx.MockTransport(router.handle)
    mock_client = httpx.AsyncClient(transport=transport, timeout=10.0)
    m["upstream"].set_client(mock_client)
    req = FakeRequest(
        {"Authorization": "Bearer ccp-test"},
        json.dumps(body).encode("utf-8"),
    )
    resp = await m["openai_handler"].handle(req, ingress_protocol=ingress_protocol)
    return resp, mock_client


async def _call_anthropic_core(m, router: MockRouter, body: dict):
    transport = httpx.MockTransport(router.handle)
    mock_client = httpx.AsyncClient(transport=transport, timeout=10.0)
    m["upstream"].set_client(mock_client)

    request_id = f"fake-{int(time.time() * 1000)}"
    start = time.time()
    await asyncio.to_thread(
        m["log_db"].insert_pending,
        request_id,
        "1.2.3.4",
        "ccp-test",
        body.get("model"),
        bool(body.get("stream", True)),
        len(body.get("messages") or []),
        len(body.get("tools") or []),
        {},
        body,
        ingress_protocol="anthropic",
    )
    route = m["scheduler"].schedule(
        body,
        api_key_name="ccp-test",
        client_ip="1.2.3.4",
        ingress_protocol="anthropic",
    )
    assert route, getattr(route, "guard_error", None)
    resp = await m["failover"].run_failover(
        route,
        body,
        request_id,
        "ccp-test",
        "1.2.3.4",
        is_stream=bool(body.get("stream", True)),
        start_time=start,
        ingress_protocol="anthropic",
    )
    return resp, mock_client, route


def _chat_response(text="openai chat ok"):
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl_fake",
            "object": "chat.completion",
            "created": 1,
            "model": "gpt-real",
            "choices": [{
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": text},
            }],
            "usage": {
                "prompt_tokens": 8,
                "completion_tokens": 3,
                "total_tokens": 11,
                "prompt_tokens_details": {"cached_tokens": 2},
            },
        },
        headers={"content-type": "application/json"},
    )


def _responses_response(text="openai responses ok"):
    return httpx.Response(
        200,
        json={
            "id": "resp_fake",
            "object": "response",
            "created_at": 1,
            "status": "completed",
            "model": "gpt-real",
            "output": [{
                "type": "message",
                "id": "msg_fake",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }],
            "output_text": text,
            "usage": {
                "input_tokens": 8,
                "output_tokens": 3,
                "total_tokens": 11,
                "input_tokens_details": {"cached_tokens": 2},
            },
        },
        headers={"content-type": "application/json"},
    )


def _responses_sse_event(event_type: str, payload: dict) -> bytes:
    return (
        f"event: {event_type}\n"
        f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"
    ).encode("utf-8")


def _responses_sse_response(text="ws over sse ok"):
    payload = b"".join([
        _responses_sse_event("response.created", {
            "type": "response.created",
            "sequence_number": 1,
            "response": {"id": "resp_sse", "status": "in_progress"},
        }),
        _responses_sse_event("response.output_item.added", {
            "type": "response.output_item.added",
            "sequence_number": 2,
            "output_index": 0,
            "item": {
                "type": "message", "id": "msg_sse", "role": "assistant",
                "status": "in_progress", "content": [],
            },
        }),
        _responses_sse_event("response.output_text.delta", {
            "type": "response.output_text.delta",
            "sequence_number": 3,
            "item_id": "msg_sse",
            "output_index": 0,
            "content_index": 0,
            "delta": text,
        }),
        _responses_sse_event("response.completed", {
            "type": "response.completed",
            "sequence_number": 4,
            "response": {
                "id": "resp_sse",
                "status": "completed",
                "output": [{
                    "type": "message", "id": "msg_sse", "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": text, "annotations": []}],
                }],
                "usage": {
                    "input_tokens": 7,
                    "output_tokens": 4,
                    "total_tokens": 11,
                    "input_tokens_details": {"cached_tokens": 2},
                },
            },
        }),
    ])
    return httpx.Response(200, content=payload, headers={"content-type": "text/event-stream"})


def _responses_sse_function_call_response():
    output = {
        "type": "function_call",
        "id": "fc_sse",
        "call_id": "call_sse",
        "name": "lookup",
        "arguments": '{"q":"ping"}',
        "status": "completed",
    }
    payload = b"".join([
        _responses_sse_event("response.created", {
            "type": "response.created",
            "sequence_number": 1,
            "response": {"id": "resp_sse_tool", "status": "in_progress"},
        }),
        _responses_sse_event("response.output_item.added", {
            "type": "response.output_item.added",
            "sequence_number": 2,
            "output_index": 0,
            "item": {**output, "arguments": "", "status": "in_progress"},
        }),
        _responses_sse_event("response.function_call_arguments.done", {
            "type": "response.function_call_arguments.done",
            "sequence_number": 3,
            "output_index": 0,
            "item_id": "fc_sse",
            "name": "lookup",
            "arguments": output["arguments"],
        }),
        _responses_sse_event("response.output_item.done", {
            "type": "response.output_item.done",
            "sequence_number": 4,
            "output_index": 0,
            "item": output,
        }),
        _responses_sse_event("response.completed", {
            "type": "response.completed",
            "sequence_number": 5,
            "response": {
                "id": "resp_sse_tool",
                "status": "completed",
                "output": [output],
                "usage": {
                    "input_tokens": 7,
                    "output_tokens": 4,
                    "total_tokens": 11,
                    "input_tokens_details": {"cached_tokens": 2},
                },
            },
        }),
    ])
    return httpx.Response(200, content=payload, headers={"content-type": "text/event-stream"})


def _chat_sse_response(text="openai chat stream ok"):
    payload = b"".join([
        b'data: {"id":"chatcmpl_sse","object":"chat.completion.chunk","created":1,"model":"gpt-real","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}\n\n',
        (
            'data: {"id":"chatcmpl_sse","object":"chat.completion.chunk","created":1,'
            '"model":"gpt-real","choices":[{"index":0,"delta":{"content":'
            f'{json.dumps(text, ensure_ascii=False)}'
            '},"finish_reason":null}]}\n\n'
        ).encode("utf-8"),
        b'data: {"id":"chatcmpl_sse","object":"chat.completion.chunk","created":1,"model":"gpt-real","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":8,"completion_tokens":3,"total_tokens":11,"prompt_tokens_details":{"cached_tokens":2}}}\n\n',
        b'data: [DONE]\n\n',
    ])
    return httpx.Response(200, content=payload, headers={"content-type": "text/event-stream"})


def _chat_sse_reasoning_function_call_response(reasoning: str, call_id: str):
    chunks = [
        {
            "id": "chatcmpl_reasoning_tool", "object": "chat.completion.chunk",
            "created": 1, "model": "deepseek-v4-flash",
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        },
        {
            "id": "chatcmpl_reasoning_tool", "object": "chat.completion.chunk",
            "created": 1, "model": "deepseek-v4-flash",
            "choices": [{"index": 0, "delta": {"reasoning_content": reasoning}, "finish_reason": None}],
        },
        {
            "id": "chatcmpl_reasoning_tool", "object": "chat.completion.chunk",
            "created": 1, "model": "deepseek-v4-flash",
            "choices": [{
                "index": 0,
                "delta": {"tool_calls": [{
                    "index": 0, "id": call_id, "type": "function",
                    "function": {"name": "lookup", "arguments": "{\"q\":\"ping\"}"},
                }]},
                "finish_reason": "tool_calls",
            }],
            "usage": {
                "prompt_tokens": 8, "completion_tokens": 6, "total_tokens": 14,
                "prompt_tokens_details": {"cached_tokens": 0},
            },
        },
    ]
    payload = b"".join(
        b"data: " + json.dumps(chunk, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n\n"
        for chunk in chunks
    ) + b"data: [DONE]\n\n"
    return httpx.Response(200, content=payload, headers={"content-type": "text/event-stream"})


def _responses_sse_reasoning_function_call_response(reasoning: str, call_id: str):
    reasoning_item = {
        "type": "reasoning", "id": "rs_replay", "status": "completed",
        "summary": [], "content": [{"type": "reasoning_text", "text": reasoning}],
    }
    function_item = {
        "type": "function_call", "id": "fc_replay", "call_id": call_id,
        "name": "lookup", "arguments": "{\"q\":\"ping\"}", "status": "completed",
    }
    payload = b"".join([
        _responses_sse_event("response.created", {
            "type": "response.created", "sequence_number": 1,
            "response": {"id": "resp_reasoning_tool", "status": "in_progress"},
        }),
        _responses_sse_event("response.output_item.added", {
            "type": "response.output_item.added", "sequence_number": 2,
            "output_index": 0,
            "item": {**reasoning_item, "status": "in_progress", "content": []},
        }),
        _responses_sse_event("response.reasoning_text.delta", {
            "type": "response.reasoning_text.delta", "sequence_number": 3,
            "output_index": 0, "content_index": 0, "item_id": "rs_replay",
            "delta": reasoning,
        }),
        _responses_sse_event("response.output_item.done", {
            "type": "response.output_item.done", "sequence_number": 4,
            "output_index": 0, "item": reasoning_item,
        }),
        _responses_sse_event("response.output_item.added", {
            "type": "response.output_item.added", "sequence_number": 5,
            "output_index": 1,
            "item": {**function_item, "status": "in_progress", "arguments": ""},
        }),
        _responses_sse_event("response.function_call_arguments.done", {
            "type": "response.function_call_arguments.done", "sequence_number": 6,
            "output_index": 1, "item_id": "fc_replay",
            "name": "lookup", "arguments": function_item["arguments"],
        }),
        _responses_sse_event("response.output_item.done", {
            "type": "response.output_item.done", "sequence_number": 7,
            "output_index": 1, "item": function_item,
        }),
        _responses_sse_event("response.completed", {
            "type": "response.completed", "sequence_number": 8,
            "response": {
                "id": "resp_reasoning_tool", "status": "completed",
                "model": "deepseek-v4-flash", "output": [reasoning_item, function_item],
                "usage": {
                    "input_tokens": 8, "output_tokens": 6, "total_tokens": 14,
                    "input_tokens_details": {"cached_tokens": 0},
                },
            },
        }),
    ])
    return httpx.Response(200, content=payload, headers={"content-type": "text/event-stream"})


def _anthropic_sse_response(text="anthropic stream ok"):
    def ev(name: str, payload: dict) -> bytes:
        return (
            f"event: {name}\n"
            f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"
        ).encode("utf-8")

    payload = b"".join([
        ev("message_start", {
            "type": "message_start",
            "message": {
                "id": "msg_stream",
                "type": "message",
                "role": "assistant",
                "model": "claude-real",
                "content": [],
                "usage": {
                    "input_tokens": 6,
                    "cache_creation_input_tokens": 1,
                    "cache_read_input_tokens": 2,
                    "output_tokens": 0,
                },
            },
        }),
        ev("content_block_start", {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        }),
        ev("content_block_delta", {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": text},
        }),
        ev("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ev("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 3},
        }),
        ev("message_stop", {"type": "message_stop"}),
    ])
    return httpx.Response(200, content=payload, headers={"content-type": "text/event-stream"})


async def _consume_streaming_to_string(resp) -> str:
    chunks = []
    async for chunk in resp.body_iterator:
        chunks.append(chunk.encode("utf-8") if isinstance(chunk, str) else chunk)
    return b"".join(chunks).decode("utf-8", errors="replace")


def _anthropic_response(text="anthropic ok"):
    return httpx.Response(
        200,
        json={
            "id": "msg_fake",
            "type": "message",
            "role": "assistant",
            "model": "claude-real",
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {
                "input_tokens": 6,
                "cache_creation_input_tokens": 1,
                "cache_read_input_tokens": 2,
                "output_tokens": 3,
            },
        },
        headers={"content-type": "application/json"},
    )


async def test_anthropic_native_official_fields_passthrough_fake_upstream(m):
    _setup(m)
    router = MockRouter()
    captured: dict[str, object] = {}

    def handler(req: httpx.Request):
        assert str(req.url) == "https://anthropic-native.example/v1/messages"
        payload = _json_request(req)
        captured["payload"] = payload
        return _anthropic_response("native anthropic pong")

    router.register("https://anthropic-native.example", handler)
    _install_channels(m, [
        _make_anthropic_channel(
            m,
            "anth-native",
            "https://anthropic-native.example",
            alias="sonnet",
            real="claude-real",
        ),
    ])

    body = {
        "model": "sonnet",
        "stream": False,
        "max_tokens": 32,
        "messages": [{"role": "user", "content": "ping"}],
        "service_tier": "auto",
        "container": "container_1",
        "mcp_servers": [{"type": "url", "url": "https://mcp.example/sse", "name": "docs"}],
        "openai_only_hint": {"drop": True},
    }
    resp, mc, route = await _call_anthropic_core(m, router, body)
    await mc.aclose()

    assert route.candidates[0][0].protocol == "anthropic"
    assert resp.status_code == 200
    payload = captured["payload"]
    assert payload["model"] == "claude-real"
    assert payload["service_tier"] == "auto"
    assert payload["container"] == "container_1"
    assert payload["mcp_servers"] == [{"type": "url", "url": "https://mcp.example/sse", "name": "docs"}]
    assert "openai_only_hint" not in payload
    assert json.loads(resp.body)["content"] == [{"type": "text", "text": "native anthropic pong"}]


async def test_anthropic_client_to_openai_chat_fake_upstream(m):
    _setup(m)
    router = MockRouter()

    def handler(req: httpx.Request):
        assert str(req.url) == "https://chat.example/v1/chat/completions"
        payload = _json_request(req)
        assert payload["model"] == "gpt-real"
        assert payload["messages"] == [{"role": "user", "content": "ping"}]
        return _chat_response("chat bridge pong")

    router.register("https://chat.example", handler)
    _install_channels(m, [
        _make_openai_channel("chat", "https://chat.example", protocol="openai-chat", alias="sonnet", real="gpt-real"),
    ])

    body = {"model": "sonnet", "stream": False, "max_tokens": 32, "messages": [{"role": "user", "content": "ping"}]}
    resp, mc, route = await _call_anthropic_core(m, router, body)
    await mc.aclose()

    assert route.candidates[0][0].protocol == "openai-chat"
    assert resp.status_code == 200
    out = json.loads(resp.body)
    assert out["type"] == "message"
    assert out["content"] == [{"type": "text", "text": "chat bridge pong"}]
    assert out["usage"]["input_tokens"] == 6
    assert out["usage"]["cache_read_input_tokens"] == 2


async def test_anthropic_client_document_to_openai_chat_fake_upstream(m):
    _setup(m)
    router = MockRouter()
    captured: dict[str, object] = {}

    def handler(req: httpx.Request):
        assert str(req.url) == "https://chat-document.example/v1/chat/completions"
        payload = _json_request(req)
        captured["payload"] = payload
        return _chat_response("chat document pong")

    router.register("https://chat-document.example", handler)
    _install_channels(m, [
        _make_openai_channel("chat-document", "https://chat-document.example", protocol="openai-chat", alias="sonnet", real="gpt-real"),
    ])

    body = {
        "model": "sonnet",
        "stream": False,
        "max_tokens": 32,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "read"},
            {
                "type": "document",
                "title": "brief.pdf",
                "source": {"type": "base64", "media_type": "application/pdf", "data": "JVBERi0xLjQ="},
            },
        ]}],
    }
    resp, mc, route = await _call_anthropic_core(m, router, body)
    await mc.aclose()

    assert route.candidates[0][0].protocol == "openai-chat"
    assert resp.status_code == 200, resp.body.decode()
    payload = captured["payload"]
    assert payload["messages"] == [{"role": "user", "content": [
        {"type": "text", "text": "read"},
        {"type": "file", "file": {
            "file_data": "JVBERi0xLjQ=",
            "filename": "brief.pdf",
        }},
    ]}]
    out = json.loads(resp.body)
    assert out["content"] == [{"type": "text", "text": "chat document pong"}]


async def test_non_stream_restore_runs_before_cross_protocol_response_translator(m, monkeypatch):
    """Provider restore must run before response translator parses upstream JSON."""
    _setup(m)
    router = MockRouter()

    def handler(req: httpx.Request):
        return httpx.Response(200, json={
            "id": "chatcmpl_restore",
            "object": "chat.completion",
            "created": 1,
            "model": "gpt-real",
            "choices": [{
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "fake_lookup", "arguments": '{"q":"x"}'},
                    }],
                },
            }],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
        }, headers={"content-type": "application/json"})

    calls: list[tuple[bytes, dict | None, dict | None]] = []

    async def fake_restore(channel, chunk, *, dynamic_map=None, translator_ctx=None):
        calls.append((chunk, dynamic_map, translator_ctx))
        assert translator_ctx["response_translator"] == "anthropic_to_chat"
        return chunk.replace(b"fake_lookup", b"lookup")

    monkeypatch.setattr(m["failover"].provider_registry, "restore_response_bytes", fake_restore)

    router.register("https://chat-restore.example", handler)
    _install_channels(m, [
        _make_openai_channel("chat-restore", "https://chat-restore.example", protocol="openai-chat", alias="sonnet", real="gpt-real"),
    ])

    body = {"model": "sonnet", "stream": False, "max_tokens": 32, "messages": [{"role": "user", "content": "hi"}]}
    resp, mc, route = await _call_anthropic_core(m, router, body)
    await mc.aclose()

    assert route.candidates[0][0].protocol == "openai-chat"
    assert resp.status_code == 200
    out = json.loads(resp.body)
    tool_use = [part for part in out["content"] if part.get("type") == "tool_use"][0]
    assert tool_use["name"] == "lookup"
    assert calls and b"fake_lookup" in calls[0][0]


async def test_anthropic_client_to_openai_responses_fake_upstream(m):
    _setup(m)
    router = MockRouter()

    def handler(req: httpx.Request):
        assert str(req.url) == "https://responses.example/v1/responses"
        payload = _json_request(req)
        assert payload["model"] == "gpt-real"
        assert payload["input"] == [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "ping"}]}]
        return _responses_response("responses bridge pong")

    router.register("https://responses.example", handler)
    _install_channels(m, [
        _make_openai_channel("responses", "https://responses.example", protocol="openai-responses", alias="sonnet", real="gpt-real"),
    ])

    body = {"model": "sonnet", "stream": False, "max_tokens": 32, "messages": [{"role": "user", "content": "ping"}]}
    resp, mc, route = await _call_anthropic_core(m, router, body)
    await mc.aclose()

    assert route.candidates[0][0].protocol == "openai-responses"
    assert resp.status_code == 200
    out = json.loads(resp.body)
    assert out["type"] == "message"
    assert out["content"] == [{"type": "text", "text": "responses bridge pong"}]
    assert out["usage"]["input_tokens"] == 6
    assert out["usage"]["cache_read_input_tokens"] == 2


async def test_anthropic_client_document_to_openai_responses_fake_upstream(m):
    _setup(m)
    router = MockRouter()
    captured: dict[str, object] = {}

    def handler(req: httpx.Request):
        assert str(req.url) == "https://responses-document.example/v1/responses"
        payload = _json_request(req)
        captured["payload"] = payload
        return _responses_response("responses document pong")

    router.register("https://responses-document.example", handler)
    _install_channels(m, [
        _make_openai_channel("responses-document", "https://responses-document.example", protocol="openai-responses", alias="sonnet", real="gpt-real"),
    ])

    body = {
        "model": "sonnet",
        "stream": False,
        "max_tokens": 32,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "read"},
            {
                "type": "document",
                "title": "brief.pdf",
                "source": {"type": "base64", "media_type": "application/pdf", "data": "JVBERi0xLjQ="},
            },
            {
                "type": "document",
                "title": "remote.pdf",
                "source": {"type": "url", "url": "https://example.com/remote.pdf"},
            },
        ]}],
    }
    resp, mc, route = await _call_anthropic_core(m, router, body)
    await mc.aclose()

    assert route.candidates[0][0].protocol == "openai-responses"
    assert resp.status_code == 200, resp.body.decode()
    payload = captured["payload"]
    assert payload["input"] == [{"type": "message", "role": "user", "content": [
        {"type": "input_text", "text": "read"},
        {"type": "input_file", "file_data": "JVBERi0xLjQ=", "filename": "brief.pdf"},
        {"type": "input_file", "file_url": "https://example.com/remote.pdf", "filename": "remote.pdf"},
    ]}]
    out = json.loads(resp.body)
    assert out["content"] == [{"type": "text", "text": "responses document pong"}]


async def test_anthropic_reasoning_and_service_tier_to_openai_responses_fake_upstream(m):
    _setup(m)
    router = MockRouter()

    def handler(req: httpx.Request):
        assert str(req.url) == "https://responses-reasoning.example/v1/responses"
        payload = _json_request(req)
        assert payload["model"] == "gpt-5"
        assert payload["reasoning"] == {"effort": "xhigh"}
        assert payload["service_tier"] == "auto"
        assert payload["input"] == [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "think"}]}]
        return _responses_response("reasoning bridge pong")

    router.register("https://responses-reasoning.example", handler)
    _install_channels(m, [
        _make_openai_channel(
            "responses-reasoning",
            "https://responses-reasoning.example",
            protocol="openai-responses",
            alias="sonnet",
            real="gpt-5",
        ),
    ])

    body = {
        "model": "sonnet",
        "stream": False,
        "max_tokens": 32,
        "messages": [{"role": "user", "content": "think"}],
        "thinking": {"type": "adaptive"},
        "service_tier": "auto",
    }
    resp, mc, route = await _call_anthropic_core(m, router, body)
    await mc.aclose()

    assert route.candidates[0][0].protocol == "openai-responses"
    assert resp.status_code == 200, resp.body.decode()
    out = json.loads(resp.body)
    assert out["content"] == [{"type": "text", "text": "reasoning bridge pong"}]


async def test_forced_fast_updates_http_log_without_mutating_raw_request(m):
    _setup(m)
    _install_keys(m, _default_key())
    router = MockRouter()

    def handler(req: httpx.Request):
        payload = _json_request(req)
        assert payload["service_tier"] == "priority"
        return _responses_response("forced fast")

    router.register("https://forced-fast.example", handler)
    _install_channels(m, [
        _make_openai_channel(
            "forced-fast",
            "https://forced-fast.example",
            protocol="openai-responses",
            alias="gpt-fast",
            real="gpt-fast-real",
            extra={"fastMode": "force", "fastModels": []},
        ),
    ])

    body = {"model": "gpt-fast", "stream": False, "input": "hello"}
    resp, mc = await _call_openai_handler(m, router, "responses", body)
    await mc.aclose()

    assert resp.status_code == 200, resp.body.decode()
    conn = m["log_db"]._get_conn()
    row = conn.execute(
        "SELECT request_id, fast_mode FROM request_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["fast_mode"] == 1
    detail = m["log_db"].log_detail(row["request_id"])["detail"]
    assert "service_tier" not in json.loads(detail["request_body"])


async def test_fast_log_follows_final_http_failover_candidate(m):
    _setup(m)
    _install_keys(m, _default_key())
    router = MockRouter()

    def forced_failure(req: httpx.Request):
        assert _json_request(req)["service_tier"] == "priority"
        return httpx.Response(500, json={
            "error": {"type": "api_error", "code": "temporary_failure", "message": "try another channel"},
        })

    def auto_success(req: httpx.Request):
        assert "service_tier" not in _json_request(req)
        return _responses_response("fallback without fast")

    router.register("https://forced-fast-fail.example", forced_failure)
    router.register("https://auto-fast-success.example", auto_success)
    _install_channels(m, [
        _make_openai_channel(
            "forced-fast-fail",
            "https://forced-fast-fail.example",
            protocol="openai-responses",
            alias="gpt-fast",
            real="gpt-fast-real",
            extra={"fastMode": "force", "fastModels": []},
        ),
        _make_openai_channel(
            "auto-fast-success",
            "https://auto-fast-success.example",
            protocol="openai-responses",
            alias="gpt-fast",
            real="gpt-fast-real",
        ),
    ])

    body = {"model": "gpt-fast", "stream": False, "input": "hello"}
    resp, mc = await _call_openai_handler(m, router, "responses", body)
    await mc.aclose()

    assert resp.status_code == 200, resp.body.decode()
    row = m["log_db"]._get_conn().execute(
        "SELECT fast_mode, final_channel_key FROM request_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["final_channel_key"] == "api:auto-fast-success"
    assert row["fast_mode"] == 0


async def test_candidate_scoped_reasoning_guard_falls_through_to_supported_model(m):
    _setup(m)
    router = MockRouter()

    def good_handler(req: httpx.Request):
        assert str(req.url) == "https://reasoning-good.example/v1/chat/completions"
        payload = _json_request(req)
        assert payload["model"] == "gpt-5"
        assert payload["reasoning_effort"] == "xhigh"
        return _chat_response("candidate guard fell through")

    router.register("https://reasoning-good.example", good_handler)
    _install_channels(m, [
        _make_openai_channel(
            "reasoning-bad",
            "https://reasoning-bad.example",
            protocol="openai-chat",
            alias="sonnet",
            real="gpt-4o",
        ),
        _make_openai_channel(
            "reasoning-good",
            "https://reasoning-good.example",
            protocol="openai-chat",
            alias="sonnet",
            real="gpt-5",
        ),
    ])

    body = {
        "model": "sonnet",
        "stream": False,
        "max_tokens": 32,
        "messages": [{"role": "user", "content": "think"}],
        "thinking": {"type": "adaptive"},
    }
    resp, mc, route = await _call_anthropic_core(m, router, body)
    await mc.aclose()

    assert [ch.key for ch, _ in route.candidates][:2] == ["api:reasoning-bad", "api:reasoning-good"]
    assert resp.status_code == 200, resp.body.decode()
    out = json.loads(resp.body)
    assert out["content"] == [{"type": "text", "text": "candidate guard fell through"}]
    assert len(router.requests) == 1
    assert str(router.requests[0].url) == "https://reasoning-good.example/v1/chat/completions"


async def test_anthropic_client_multiturn_tool_result_to_openai_chat_fake_upstream(m):
    _setup(m)
    router = MockRouter()

    def handler(req: httpx.Request):
        assert str(req.url) == "https://chat-history.example/v1/chat/completions"
        payload = _json_request(req)
        assert payload["model"] == "gpt-real"
        assert payload["messages"] == [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "need tool", "tool_calls": [{
                "id": "toolu_1",
                "type": "function",
                "function": {"name": "lookup", "arguments": '{"q":"x"}'},
            }]},
            {"role": "tool", "tool_call_id": "toolu_1", "content": "result x"},
            {"role": "user", "content": "continue"},
        ]
        return _chat_response("chat history pong")

    router.register("https://chat-history.example", handler)
    _install_channels(m, [
        _make_openai_channel("chat-history", "https://chat-history.example", protocol="openai-chat", alias="sonnet", real="gpt-real"),
    ])

    body = {
        "model": "sonnet", "stream": False, "max_tokens": 32,
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "need tool"},
                {"type": "tool_use", "id": "toolu_1", "name": "lookup", "input": {"q": "x"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": "result x"},
                {"type": "text", "text": "continue"},
            ]},
        ],
        "tools": [{"name": "lookup", "input_schema": {"type": "object"}}],
    }
    resp, mc, route = await _call_anthropic_core(m, router, body)
    await mc.aclose()

    assert route.candidates[0][0].protocol == "openai-chat"
    assert resp.status_code == 200
    assert json.loads(resp.body)["content"][0]["text"] == "chat history pong"


async def test_anthropic_client_multiturn_tool_result_to_openai_responses_fake_upstream(m):
    _setup(m)
    router = MockRouter()

    def handler(req: httpx.Request):
        assert str(req.url) == "https://responses-history.example/v1/responses"
        payload = _json_request(req)
        assert payload["model"] == "gpt-real"
        assert payload["input"] == [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "need tool"}]},
            {"type": "function_call", "id": "fc_toolu_1", "call_id": "toolu_1", "name": "lookup", "arguments": '{"q":"x"}', "status": "completed"},
            {"type": "function_call_output", "call_id": "toolu_1", "output": "result x"},
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "continue"}]},
        ]
        return _responses_response("responses history pong")

    router.register("https://responses-history.example", handler)
    _install_channels(m, [
        _make_openai_channel("responses-history", "https://responses-history.example", protocol="openai-responses", alias="sonnet", real="gpt-real"),
    ])

    body = {
        "model": "sonnet", "stream": False, "max_tokens": 32,
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "need tool"},
                {"type": "tool_use", "id": "toolu_1", "name": "lookup", "input": {"q": "x"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": "result x"},
                {"type": "text", "text": "continue"},
            ]},
        ],
        "tools": [{"name": "lookup", "input_schema": {"type": "object"}}],
    }
    resp, mc, route = await _call_anthropic_core(m, router, body)
    await mc.aclose()

    assert route.candidates[0][0].protocol == "openai-responses"
    assert resp.status_code == 200
    assert json.loads(resp.body)["content"][0]["text"] == "responses history pong"


async def test_anthropic_client_tool_result_attachments_to_openai_responses_fake_upstream(m):
    _setup(m)
    router = MockRouter()
    captured: dict[str, object] = {}

    def handler(req: httpx.Request):
        assert str(req.url) == "https://responses-tool-attachments.example/v1/responses"
        payload = _json_request(req)
        captured["payload"] = payload
        return _responses_response("anthropic tool attachments pong")

    router.register("https://responses-tool-attachments.example", handler)
    _install_channels(m, [
        _make_openai_channel(
            "responses-tool-attachments",
            "https://responses-tool-attachments.example",
            protocol="openai-responses",
            alias="sonnet",
            real="gpt-real",
        ),
    ])

    body = {
        "model": "sonnet",
        "stream": False,
        "max_tokens": 32,
        "messages": [{"role": "user", "content": [{
            "type": "tool_result",
            "tool_use_id": "toolu_1",
            "content": [
                {"type": "text", "text": "see attached"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}},
                {
                    "type": "document",
                    "title": "brief.pdf",
                    "source": {"type": "base64", "media_type": "application/pdf", "data": "JVBERi0xLjQ="},
                },
                {
                    "type": "document",
                    "title": "remote.pdf",
                    "context": "remote contract",
                    "source": {"type": "url", "url": "https://example.com/remote.pdf"},
                },
            ],
        }]}],
    }
    resp, mc, route = await _call_anthropic_core(m, router, body)
    await mc.aclose()

    assert route.candidates[0][0].protocol == "openai-responses"
    assert resp.status_code == 200
    payload = captured["payload"]
    assert payload["model"] == "gpt-real"
    assert payload["input"] == [{
        "type": "function_call_output",
        "call_id": "toolu_1",
        "output": [
            {"type": "input_text", "text": "see attached"},
            {"type": "input_image", "image_url": "data:image/png;base64,AAAA", "detail": "auto"},
            {"type": "input_file", "file_data": "JVBERi0xLjQ=", "filename": "brief.pdf"},
            {"type": "input_text", "text": "Document context: remote contract"},
            {"type": "input_file", "file_url": "https://example.com/remote.pdf", "filename": "remote.pdf"},
        ],
    }]
    assert json.loads(resp.body)["content"][0]["text"] == "anthropic tool attachments pong"


async def test_anthropic_client_to_openai_chat_stream_fake_upstream(m):
    _setup(m)
    router = MockRouter()

    def handler(req: httpx.Request):
        assert str(req.url) == "https://chat-sse.example/v1/chat/completions"
        payload = _json_request(req)
        assert payload["model"] == "gpt-real"
        assert payload["stream"] is True
        assert payload["messages"] == [{"role": "user", "content": "ping"}]
        return _chat_sse_response("chat stream pong")

    router.register("https://chat-sse.example", handler)
    _install_channels(m, [
        _make_openai_channel("chat-sse", "https://chat-sse.example", protocol="openai-chat", alias="sonnet", real="gpt-real"),
    ])

    body = {"model": "sonnet", "stream": True, "max_tokens": 32, "messages": [{"role": "user", "content": "ping"}]}
    resp, mc, route = await _call_anthropic_core(m, router, body)
    text = await _consume_streaming_to_string(resp)
    await mc.aclose()

    assert route.candidates[0][0].protocol == "openai-chat"
    assert resp.status_code == 200
    assert "event: message_start" in text
    assert '"type":"content_block_delta"' in text
    assert '"text":"chat stream pong"' in text
    assert '"stop_reason":"end_turn"' in text
    assert '"input_tokens":6' in text
    assert '"cache_read_input_tokens":2' in text


async def test_anthropic_client_to_openai_responses_stream_fake_upstream(m):
    _setup(m)
    router = MockRouter()

    def handler(req: httpx.Request):
        assert str(req.url) == "https://responses-sse.example/v1/responses"
        payload = _json_request(req)
        assert payload["model"] == "gpt-real"
        assert payload["stream"] is True
        assert payload["input"] == [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "ping"}]}]
        return _responses_sse_response("responses stream pong")

    router.register("https://responses-sse.example", handler)
    _install_channels(m, [
        _make_openai_channel("responses-sse", "https://responses-sse.example", protocol="openai-responses", alias="sonnet", real="gpt-real"),
    ])

    body = {"model": "sonnet", "stream": True, "max_tokens": 32, "messages": [{"role": "user", "content": "ping"}]}
    resp, mc, route = await _call_anthropic_core(m, router, body)
    text = await _consume_streaming_to_string(resp)
    await mc.aclose()

    assert route.candidates[0][0].protocol == "openai-responses"
    assert resp.status_code == 200
    assert "event: message_start" in text
    assert '"type":"content_block_delta"' in text
    assert '"text":"responses stream pong"' in text
    assert '"stop_reason":"end_turn"' in text


async def test_deepseek_chat_stream_reasoning_is_automatically_cached_and_replayed(m):
    from src.openai import deepseek_reasoning

    _setup(m)
    _install_keys(m, _default_key())
    deepseek_reasoning.clear()
    router = MockRouter()
    reasoning = "\n  exact chat replay reasoning  \n"
    call_id = "call_chat_auto_replay"

    def handler(req: httpx.Request):
        payload = _json_request(req)
        assert payload["model"] == "deepseek-v4-flash"
        assert payload["thinking"] == {"type": "enabled"}
        if len(router.requests) == 1:
            assert not any(message.get("tool_calls") for message in payload["messages"])
            return _chat_sse_reasoning_function_call_response(reasoning, call_id)

        assistant = next(
            message for message in payload["messages"]
            if any(call.get("id") == call_id for call in message.get("tool_calls") or [])
        )
        assert assistant["reasoning_content"] == reasoning
        return _chat_sse_response("chat replay complete")

    router.register("https://deepseek-chat-replay.example", handler)
    _install_channels(m, [
        _make_openai_channel(
            "DeepSeek Chat Replay", "https://deepseek-chat-replay.example",
            protocol="openai-chat", alias="deepseek-v4-flash", real="deepseek-v4-flash",
        ),
    ])
    tools = [{"name": "lookup", "input_schema": {"type": "object"}}]
    first_body = {
        "model": "deepseek-v4-flash", "stream": True, "max_tokens": 256,
        "thinking": {"type": "enabled", "budget_tokens": 4096},
        "tool_choice": {"type": "auto"}, "tools": tools,
        "messages": [{"role": "user", "content": "look it up"}],
    }
    first_response, first_client, _ = await _call_anthropic_core(m, router, first_body)
    first_text = await _consume_streaming_to_string(first_response)
    await first_client.aclose()
    assert call_id in first_text
    assert deepseek_reasoning.has_replay_for_tool_call(call_id, model="deepseek-v4-flash")

    await asyncio.sleep(0.002)
    second_body = {
        "model": "deepseek-v4-flash", "stream": True, "max_tokens": 256,
        "tool_choice": {"type": "auto"}, "tools": tools,
        "messages": [
            {"role": "user", "content": "look it up"},
            {"role": "assistant", "content": [{
                "type": "tool_use", "id": call_id, "name": "lookup", "input": {"q": "ping"},
            }]},
            {"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": call_id, "content": "result",
            }]},
        ],
    }
    second_response, second_client, _ = await _call_anthropic_core(m, router, second_body)
    second_text = await _consume_streaming_to_string(second_response)
    await second_client.aclose()

    assert second_response.status_code == 200
    assert "chat replay complete" in second_text
    assert len(router.requests) == 2


async def test_deepseek_responses_stream_reasoning_is_automatically_cached_and_replayed(m):
    from src.openai import deepseek_reasoning

    _setup(m)
    _install_keys(m, _default_key())
    deepseek_reasoning.clear()
    router = MockRouter()
    reasoning = "\n  exact responses replay reasoning  \n"
    call_id = "call_responses_auto_replay"

    def handler(req: httpx.Request):
        payload = _json_request(req)
        assert payload["model"] == "deepseek-v4-flash"
        assert payload["reasoning"] == {"effort": "high"}
        if len(router.requests) == 1:
            assert not any(item.get("type") == "function_call" for item in payload["input"])
            return _responses_sse_reasoning_function_call_response(reasoning, call_id)

        function_index = next(
            index for index, item in enumerate(payload["input"])
            if item.get("type") == "function_call" and item.get("call_id") == call_id
        )
        assert payload["input"][function_index - 1]["type"] == "reasoning"
        assert payload["input"][function_index - 1]["content"] == [
            {"type": "reasoning_text", "text": reasoning},
        ]
        return _responses_sse_response("responses replay complete")

    router.register("https://deepseek-responses-replay.example", handler)
    _install_channels(m, [
        _make_openai_channel(
            "DeepSeek Responses Replay", "https://deepseek-responses-replay.example",
            protocol="openai-responses", alias="deepseek-v4-flash", real="deepseek-v4-flash",
        ),
    ])
    tools = [{"name": "lookup", "input_schema": {"type": "object"}}]
    first_body = {
        "model": "deepseek-v4-flash", "stream": True, "max_tokens": 256,
        "thinking": {"type": "enabled", "budget_tokens": 4096},
        "tool_choice": {"type": "auto"}, "tools": tools,
        "messages": [{"role": "user", "content": "look it up"}],
    }
    first_response, first_client, _ = await _call_anthropic_core(m, router, first_body)
    first_text = await _consume_streaming_to_string(first_response)
    await first_client.aclose()
    assert call_id in first_text
    assert deepseek_reasoning.has_replay_for_tool_call(call_id, model="deepseek-v4-flash")

    await asyncio.sleep(0.002)
    second_body = {
        "model": "deepseek-v4-flash", "stream": True, "max_tokens": 256,
        "tool_choice": {"type": "auto"}, "tools": tools,
        "messages": [
            {"role": "user", "content": "look it up"},
            {"role": "assistant", "content": [{
                "type": "tool_use", "id": call_id, "name": "lookup", "input": {"q": "ping"},
            }]},
            {"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": call_id, "content": "result",
            }]},
        ],
    }
    second_response, second_client, _ = await _call_anthropic_core(m, router, second_body)
    second_text = await _consume_streaming_to_string(second_response)
    await second_client.aclose()

    assert second_response.status_code == 200
    assert "responses replay complete" in second_text
    assert len(router.requests) == 2


async def test_responses_terminal_event_finishes_without_waiting_for_http_eof(m):
    _setup(m)
    router = MockRouter()
    terminal_payload = _responses_sse_response("terminal without eof").content
    hanging = TerminalThenHangByteStream(terminal_payload)

    def handler(req: httpx.Request):
        return httpx.Response(
            200,
            stream=hanging,
            headers={"content-type": "text/event-stream"},
        )

    router.register("https://responses-no-eof.example", handler)
    _install_channels(m, [
        _make_openai_channel(
            "responses-no-eof",
            "https://responses-no-eof.example",
            protocol="openai-responses",
            alias="sonnet",
            real="gpt-real",
        ),
    ])

    body = {
        "model": "sonnet",
        "stream": True,
        "max_tokens": 32,
        "messages": [{"role": "user", "content": "ping"}],
    }
    resp, mc, _route = await _call_anthropic_core(m, router, body)
    text = await asyncio.wait_for(_consume_streaming_to_string(resp), timeout=1.0)
    await mc.aclose()

    assert "terminal without eof" in text
    assert hanging.closed.is_set()


async def test_chat_precommit_max_output_is_request_invalid_without_cooldown(m):
    """A pre-visible Responses incomplete event is request-scoped, not channel health."""
    _setup(m)
    _install_keys(m, _default_key())
    router = MockRouter()
    payload = _responses_sse_event("response.incomplete", {
        "type": "response.incomplete",
        "response": {
            "id": "resp_precommit_max_output",
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [],
        },
    })
    router.register(
        "https://responses-precommit-max-output.example",
        lambda req: httpx.Response(
            200, content=payload, headers={"content-type": "text/event-stream"},
        ),
    )
    _install_channels(m, [
        _make_openai_channel(
            "responses-precommit-max-output",
            "https://responses-precommit-max-output.example",
            protocol="openai-responses",
            alias="gpt-5",
            real="gpt-real",
        ),
    ])

    resp, mc = await _call_openai_handler(m, router, "chat", {
        "model": "gpt-5",
        "stream": True,
        "messages": [{"role": "user", "content": "ping"}],
    })
    await mc.aclose()

    assert resp.status_code == 400
    error = json.loads(resp.body)["error"]
    assert error["code"] == "context_length_exceeded"
    assert "max_output_tokens" in error["message"]
    latest = m["log_db"]._get_conn().execute(
        "SELECT status, http_status, error_message FROM request_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert latest is not None
    assert latest["status"] == "error"
    assert latest["http_status"] == 400
    assert "max_output_tokens" in latest["error_message"]
    assert m["cooldown"].get_state(
        "api:responses-precommit-max-output", "gpt-real",
    ) is None


async def test_stream_generator_aclose_records_cancelled_without_cooldown(m, monkeypatch):
    _setup(m)
    router = MockRouter()
    partial_payload = b"".join([
        _responses_sse_event("response.created", {
            "type": "response.created",
            "response": {"id": "resp_partial", "status": "in_progress"},
        }),
        _responses_sse_event("response.output_text.delta", {
            "type": "response.output_text.delta",
            "item_id": "msg_partial",
            "output_index": 0,
            "content_index": 0,
            "delta": "partial",
        }),
    ])
    hanging = TerminalThenHangByteStream(partial_payload)
    router.register(
        "https://responses-aclose.example",
        lambda req: httpx.Response(
            200,
            stream=hanging,
            headers={"content-type": "text/event-stream"},
        ),
    )
    _install_channels(m, [
        _make_openai_channel(
            "responses-aclose",
            "https://responses-aclose.example",
            protocol="openai-responses",
            alias="sonnet",
            real="gpt-real",
        ),
    ])

    body = {
        "model": "sonnet",
        "stream": True,
        "max_tokens": 32,
        "messages": [{"role": "user", "content": "ping"}],
    }
    resp, mc, _route = await _call_anthropic_core(m, router, body)
    finish_statuses: list[str] = []
    original_finish_error = m["log_db"].finish_error

    def recorded_finish_error(*args, **kwargs):
        finish_statuses.append(str(kwargs.get("status") or "error"))
        return original_finish_error(*args, **kwargs)

    monkeypatch.setattr(m["log_db"], "finish_error", recorded_finish_error)
    iterator = resp.body_iterator
    emitted = b""
    for _ in range(10):
        item = await asyncio.wait_for(anext(iterator), timeout=1.0)
        emitted += item.encode() if isinstance(item, str) else item
        if b"partial" in emitted:
            break
    assert b"partial" in emitted
    await iterator.aclose()
    await mc.aclose()

    latest = m["log_db"]._get_conn().execute(
        "SELECT status, error_message FROM request_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert finish_statuses == ["cancelled"]
    assert latest is not None and latest["status"] == "cancelled"
    assert latest["error_message"] == "client disconnected"
    assert hanging.closed.is_set()
    assert m["cooldown"].get_state("api:responses-aclose", "gpt-real") is None


async def test_client_disconnect_finishes_outer_log_before_retry_bookkeeping(m, monkeypatch):
    """An interruption after retry persistence must not leave request_log pending."""
    _setup(m)
    router = MockRouter()
    partial_payload = b"".join([
        _responses_sse_event("response.created", {
            "type": "response.created",
            "response": {"id": "resp_cancel_order", "status": "in_progress"},
        }),
        _responses_sse_event("response.output_text.delta", {
            "type": "response.output_text.delta",
            "item_id": "msg_cancel_order",
            "output_index": 0,
            "content_index": 0,
            "delta": "partial",
        }),
    ])
    hanging = TerminalThenHangByteStream(partial_payload)
    router.register(
        "https://responses-cancel-order.example",
        lambda req: httpx.Response(
            200, stream=hanging, headers={"content-type": "text/event-stream"},
        ),
    )
    _install_channels(m, [
        _make_openai_channel(
            "responses-cancel-order",
            "https://responses-cancel-order.example",
            protocol="openai-responses",
            alias="sonnet",
            real="gpt-real",
        ),
    ])

    body = {
        "model": "sonnet",
        "stream": True,
        "max_tokens": 32,
        "messages": [{"role": "user", "content": "ping"}],
    }
    resp, mc, _route = await _call_anthropic_core(m, router, body)
    iterator = resp.body_iterator
    emitted = b""
    for _ in range(10):
        item = await asyncio.wait_for(anext(iterator), timeout=1.0)
        emitted += item.encode() if isinstance(item, str) else item
        if b"partial" in emitted:
            break
    assert b"partial" in emitted

    original_to_thread = asyncio.to_thread
    interrupted = False

    async def interrupt_after_retry_write(func, /, *args, **kwargs):
        nonlocal interrupted
        result = await original_to_thread(func, *args, **kwargs)
        if func is m["log_db"].update_retry_attempt and not interrupted:
            interrupted = True
            raise RuntimeError("simulated interruption after retry persistence")
        return result

    monkeypatch.setattr(asyncio, "to_thread", interrupt_after_retry_write)
    await iterator.aclose()
    await mc.aclose()

    latest = m["log_db"]._get_conn().execute(
        "SELECT status, http_status, error_message FROM request_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert interrupted is True
    assert latest is not None
    assert latest["status"] == "cancelled"
    assert latest["http_status"] == 499
    assert latest["error_message"] == "client disconnected"


async def test_terminal_finalization_survives_consumer_cancellation_between_db_writes(m, monkeypatch):
    _setup(m)
    router = MockRouter()
    router.register(
        "https://responses-cancel.example",
        lambda req: _responses_sse_response("cancel after terminal"),
    )
    _install_channels(m, [
        _make_openai_channel(
            "responses-cancel",
            "https://responses-cancel.example",
            protocol="openai-responses",
            alias="sonnet",
            real="gpt-real",
        ),
    ])

    body = {
        "model": "sonnet",
        "stream": True,
        "max_tokens": 32,
        "messages": [{"role": "user", "content": "ping"}],
    }
    resp, mc, _route = await _call_anthropic_core(m, router, body)

    retry_write_entered = asyncio.Event()
    release_retry_write = asyncio.Event()
    finish_success_calls: list[str] = []
    original_to_thread = asyncio.to_thread
    original_finish_success = m["log_db"].finish_success

    async def controlled_to_thread(func, /, *args, **kwargs):
        if func is m["log_db"].update_retry_attempt:
            retry_write_entered.set()
            await release_retry_write.wait()
        return func(*args, **kwargs)

    def recorded_finish_success(request_id, *args, **kwargs):
        finish_success_calls.append(str(request_id))
        return original_finish_success(request_id, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", controlled_to_thread)
    monkeypatch.setattr(m["log_db"], "finish_success", recorded_finish_success)
    consumer = asyncio.create_task(_consume_streaming_to_string(resp))
    await asyncio.wait_for(retry_write_entered.wait(), timeout=1.0)
    consumer.cancel()
    await asyncio.sleep(0)
    assert not consumer.done()

    release_retry_write.set()
    with pytest.raises(asyncio.CancelledError):
        await consumer
    await mc.aclose()
    monkeypatch.setattr(asyncio, "to_thread", original_to_thread)

    assert len(finish_success_calls) == 1


async def test_anthropic_messages_stream_invalid_image_http_400_short_circuits(m):
    _setup(m)
    router = MockRouter()
    fallback_calls = {"count": 0}

    def invalid_image_handler(req: httpx.Request):
        assert str(req.url) == "https://invalid-image.example/v1/responses"
        return httpx.Response(400, json={
            "error": {
                "type": "invalid_request_error",
                "code": "invalid_value",
                "param": "input",
                "message": "Invalid image data: expected a base64-encoded image.",
            }
        })

    def fallback_handler(req: httpx.Request):
        fallback_calls["count"] += 1
        return _responses_sse_response("must not fail over")

    router.register("https://invalid-image.example", invalid_image_handler)
    router.register("https://invalid-image-fallback.example", fallback_handler)
    _install_channels(m, [
        _make_openai_channel(
            "invalid-image",
            "https://invalid-image.example",
            protocol="openai-responses",
            alias="sonnet",
            real="gpt-real",
        ),
        _make_openai_channel(
            "invalid-image-fallback",
            "https://invalid-image-fallback.example",
            protocol="openai-responses",
            alias="sonnet",
            real="gpt-real",
        ),
    ])

    body = {
        "model": "sonnet",
        "stream": True,
        "max_tokens": 32,
        "messages": [{"role": "user", "content": "invalid image request"}],
    }
    resp, mc, route = await _call_anthropic_core(m, router, body)
    await mc.aclose()

    assert route.candidates[0][0].protocol == "openai-responses"
    assert [str(req.url) for req in router.requests] == [
        "https://invalid-image.example/v1/responses",
    ]
    assert fallback_calls["count"] == 0
    assert resp.status_code == 400
    assert resp.headers["content-type"].startswith("application/json")
    assert b"event: error" not in resp.body
    payload = json.loads(resp.body)
    assert payload == {
        "type": "error",
        "error": {
            "type": "invalid_request_error",
            "message": "Invalid image data: expected a base64-encoded image.",
            "code": "invalid_value",
        },
    }

    conn = m["log_db"]._get_conn()
    request_row = dict(conn.execute(
        "SELECT request_id, http_status, retry_count FROM request_log ORDER BY id DESC LIMIT 1"
    ).fetchone())
    attempts = [dict(row) for row in conn.execute(
        "SELECT channel_key, outcome FROM retry_chain WHERE request_id = ? ORDER BY attempt_order",
        (request_row["request_id"],),
    ).fetchall()]
    assert request_row["http_status"] == 400
    assert request_row["retry_count"] == 0
    assert attempts == [{
        "channel_key": route.candidates[0][0].key,
        "outcome": "request_invalid",
    }]
    assert not m["cooldown"].is_blocked(route.candidates[0][0].key, "gpt-real")
    assert m["scorer"].get_stats(route.candidates[0][0].key, "gpt-real") is None


@pytest.mark.parametrize("status,error_type", [(429, "rate_limit_error"), (503, "server_error")])
async def test_anthropic_messages_stream_retryable_http_errors_still_fail_over_and_cool_down(
    m,
    status,
    error_type,
):
    _setup(m)
    router = MockRouter()
    bad_base = f"https://retryable-{status}.example"
    good_base = f"https://retryable-{status}-fallback.example"

    def bad_handler(req: httpx.Request):
        return httpx.Response(status, json={
            "error": {
                "type": error_type,
                "code": "temporarily_unavailable",
                "message": "retry later",
            }
        })

    router.register(bad_base, bad_handler)
    router.register(good_base, lambda req: _responses_sse_response("retry succeeded"))
    _install_channels(m, [
        _make_openai_channel(
            f"retryable-{status}", bad_base, protocol="openai-responses", alias="sonnet", real="gpt-real",
        ),
        _make_openai_channel(
            f"retryable-{status}-fallback",
            good_base,
            protocol="openai-responses",
            alias="sonnet",
            real="gpt-real",
        ),
    ])

    body = {
        "model": "sonnet",
        "stream": True,
        "max_tokens": 32,
        "messages": [{"role": "user", "content": "ping"}],
    }
    resp, mc, route = await _call_anthropic_core(m, router, body)
    text = await _consume_streaming_to_string(resp)
    await mc.aclose()

    assert resp.status_code == 200
    assert "retry succeeded" in text
    assert [str(req.url) for req in router.requests] == [
        f"{bad_base}/v1/responses",
        f"{good_base}/v1/responses",
    ]
    conn = m["log_db"]._get_conn()
    request_id = conn.execute(
        "SELECT request_id FROM request_log ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]
    outcomes = [row[0] for row in conn.execute(
        "SELECT outcome FROM retry_chain WHERE request_id = ? ORDER BY attempt_order",
        (request_id,),
    ).fetchall()]
    assert outcomes == ["http_error", "success"]
    assert m["cooldown"].is_blocked(route.candidates[0][0].key, "gpt-real")
    assert m["scorer"].get_stats(route.candidates[0][0].key, "gpt-real")["total_requests"] == 1


async def test_openai_previsible_overload_retries_same_channel_without_health_penalty(m, monkeypatch):
    """The exact CXP/OpenAI Responses SSE overload shape is retried pre-commit."""
    _setup(m)
    monkeypatch.setattr(m["failover"], "_overload_retry_delay_seconds", lambda _ordinal: 0.0)
    router = MockRouter()
    attempts = {"count": 0}
    overload_sse = b"".join([
        _responses_sse_event("response.created", {
            "type": "response.created",
            "response": {"id": "resp_overloaded", "status": "in_progress"},
        }),
        _responses_sse_event("error", {
            "type": "error",
            "error": {
                "type": "service_unavailable_error",
                "code": "server_is_overloaded",
                "message": "Our servers are currently overloaded. Please try again later.",
                "param": None,
            },
        }),
    ])

    def handler(req: httpx.Request):
        attempts["count"] += 1
        if attempts["count"] < 3:
            return httpx.Response(
                200, content=overload_sse, headers={"content-type": "text/event-stream"},
            )
        return _responses_sse_response("openai overload recovered")

    router.register("https://openai-overload.example", handler)
    _install_channels(m, [
        _make_openai_channel(
            "openai-overload",
            "https://openai-overload.example",
            protocol="openai-responses",
            alias="sonnet",
            real="gpt-real",
        ),
    ])

    body = {
        "model": "sonnet",
        "stream": True,
        "max_tokens": 32,
        "messages": [{"role": "user", "content": "ping"}],
    }
    resp, mc, route = await _call_anthropic_core(m, router, body)
    text = await _consume_streaming_to_string(resp)
    await mc.aclose()

    assert resp.status_code == 200
    assert "openai overload recovered" in text
    assert attempts["count"] == 3
    assert [str(req.url) for req in router.requests] == [
        "https://openai-overload.example/v1/responses",
    ] * 3
    conn = m["log_db"]._get_conn()
    request_row = dict(conn.execute(
        "SELECT request_id, retry_count FROM request_log ORDER BY id DESC LIMIT 1"
    ).fetchone())
    outcomes = [row[0] for row in conn.execute(
        "SELECT outcome FROM retry_chain WHERE request_id = ? ORDER BY attempt_order",
        (request_row["request_id"],),
    ).fetchall()]
    assert request_row["retry_count"] == 2
    assert outcomes == ["upstream_error_json", "upstream_error_json", "success"]
    assert m["cooldown"].get_state(route.candidates[0][0].key, "gpt-real") is None
    assert m["scorer"].get_stats(route.candidates[0][0].key, "gpt-real")["total_requests"] == 1


async def test_openai_server_error_retries_same_channel_without_health_penalty(m, monkeypatch):
    """Exact OpenAI server_error uses the configured shared transient budget."""
    _setup(m)
    monkeypatch.setattr(m["failover"], "_overload_retry_delay_seconds", lambda _ordinal: 0.0)
    router = MockRouter()
    attempts = {"count": 0}
    server_error_sse = b"".join([
        _responses_sse_event("response.created", {
            "type": "response.created",
            "response": {"id": "resp_server_error", "status": "in_progress"},
        }),
        _responses_sse_event("error", {
            "type": "error",
            "error": {
                "type": "server_error",
                "code": "server_error",
                "message": "An error occurred while processing your request. You can retry your request.",
            },
        }),
    ])

    def handler(req: httpx.Request):
        attempts["count"] += 1
        if attempts["count"] < 3:
            return httpx.Response(
                200, content=server_error_sse, headers={"content-type": "text/event-stream"},
            )
        return _responses_sse_response("openai server_error recovered")

    router.register("https://openai-server-error.example", handler)
    _install_channels(m, [
        _make_openai_channel(
            "openai-server-error",
            "https://openai-server-error.example",
            protocol="openai-responses",
            alias="sonnet",
            real="gpt-real",
        ),
    ])

    body = {
        "model": "sonnet",
        "stream": True,
        "max_tokens": 32,
        "messages": [{"role": "user", "content": "ping"}],
    }
    resp, mc, route = await _call_anthropic_core(m, router, body)
    text = await _consume_streaming_to_string(resp)
    await mc.aclose()

    assert resp.status_code == 200
    assert "openai server_error recovered" in text
    assert attempts["count"] == 3
    conn = m["log_db"]._get_conn()
    request_row = dict(conn.execute(
        "SELECT request_id, retry_count FROM request_log ORDER BY id DESC LIMIT 1"
    ).fetchone())
    outcomes = [row[0] for row in conn.execute(
        "SELECT outcome FROM retry_chain WHERE request_id = ? ORDER BY attempt_order",
        (request_row["request_id"],),
    ).fetchall()]
    assert request_row["retry_count"] == 2
    assert outcomes == ["upstream_error_json", "upstream_error_json", "success"]
    assert m["cooldown"].get_state(route.candidates[0][0].key, "gpt-real") is None
    assert m["scorer"].get_stats(route.candidates[0][0].key, "gpt-real")["total_requests"] == 1


async def test_http_transient_retry_prefers_bounded_retry_after_header(m, monkeypatch):
    _setup(m)
    router = MockRouter()
    calls = {"count": 0}
    observed = []

    async def capture_wait(ordinal, deadline_ts, *, retry_after_seconds=None):
        observed.append(retry_after_seconds)
        return 0.0

    monkeypatch.setattr(m["failover"], "_wait_for_overload_retry", capture_wait)

    def handler(req: httpx.Request):
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(
                503,
                json={
                    "error": {
                        "type": "server_error",
                        "code": "server_error",
                        "message": "retry after the requested delay",
                    }
                },
                headers={"Retry-After": "7"},
            )
        return _responses_sse_response("retry-after recovered")

    router.register("https://retry-after.example", handler)
    _install_channels(m, [
        _make_openai_channel(
            "retry-after", "https://retry-after.example",
            protocol="openai-responses", alias="sonnet", real="gpt-real",
        ),
    ])
    body = {
        "model": "sonnet", "stream": True, "max_tokens": 32,
        "messages": [{"role": "user", "content": "ping"}],
    }
    resp, mc, _route = await _call_anthropic_core(m, router, body)
    text = await _consume_streaming_to_string(resp)
    await mc.aclose()

    assert "retry-after recovered" in text
    assert calls["count"] == 2
    assert observed == [7.0]


async def test_transient_retry_limit_is_hot_configured_and_still_switches_candidate(m, monkeypatch):
    _setup(m)
    monkeypatch.setattr(m["failover"], "_overload_retry_delay_seconds", lambda _ordinal: 0.0)
    original_retry = json.loads(json.dumps(m["config"].get().get("retry") or {}))
    m["config"].update(
        lambda c: c.setdefault("retry", {}).setdefault("transient", {}).__setitem__(
            "maxExtraAttempts", 1,
        )
    )
    router = MockRouter()
    bad_calls = {"count": 0}

    def bad(req: httpx.Request):
        bad_calls["count"] += 1
        return httpx.Response(503, json={
            "error": {
                "type": "server_error",
                "code": "server_error",
                "message": "retryable exact server_error",
            }
        })

    try:
        router.register("https://configured-retry.example", bad)
        router.register(
            "https://configured-retry-fallback.example",
            lambda req: _responses_sse_response("configured fallback ok"),
        )
        _install_channels(m, [
            _make_openai_channel(
                "configured-retry", "https://configured-retry.example",
                protocol="openai-responses", alias="sonnet", real="gpt-real",
            ),
            _make_openai_channel(
                "configured-retry-fallback", "https://configured-retry-fallback.example",
                protocol="openai-responses", alias="sonnet", real="gpt-real",
            ),
        ])
        body = {
            "model": "sonnet", "stream": True, "max_tokens": 32,
            "messages": [{"role": "user", "content": "ping"}],
        }
        resp, mc, _route = await _call_anthropic_core(m, router, body)
        text = await _consume_streaming_to_string(resp)
        await mc.aclose()
        assert resp.status_code == 200
        assert "configured fallback ok" in text
        assert bad_calls["count"] == 2  # original attempt + configured one extra
        assert [str(req.url) for req in router.requests][-1] == (
            "https://configured-retry-fallback.example/v1/responses"
        )
    finally:
        m["config"].update(lambda c: c.__setitem__("retry", original_retry))


async def test_claude_overloaded_529_retries_same_channel_without_health_penalty(m, monkeypatch):
    """Anthropic's documented 529/overloaded_error follows the same bounded retry."""
    _setup(m)
    monkeypatch.setattr(m["failover"], "_overload_retry_delay_seconds", lambda _ordinal: 0.0)
    router = MockRouter()
    attempts = {"count": 0}

    def handler(req: httpx.Request):
        attempts["count"] += 1
        if attempts["count"] < 3:
            return httpx.Response(529, json={
                "type": "error",
                "error": {"type": "overloaded_error", "message": "Overloaded"},
            })
        return _anthropic_sse_response("claude overload recovered")

    router.register("https://claude-overload.example", handler)
    _install_channels(m, [
        _make_anthropic_channel(
            m,
            "claude-overload",
            "https://claude-overload.example",
            alias="sonnet",
            real="claude-real",
        ),
    ])

    body = {
        "model": "sonnet",
        "stream": True,
        "max_tokens": 32,
        "messages": [{"role": "user", "content": "ping"}],
    }
    resp, mc, route = await _call_anthropic_core(m, router, body)
    text = await _consume_streaming_to_string(resp)
    await mc.aclose()

    assert resp.status_code == 200
    assert "claude overload recovered" in text
    assert attempts["count"] == 3
    assert [str(req.url) for req in router.requests] == [
        "https://claude-overload.example/v1/messages",
    ] * 3
    conn = m["log_db"]._get_conn()
    request_row = dict(conn.execute(
        "SELECT request_id, retry_count FROM request_log ORDER BY id DESC LIMIT 1"
    ).fetchone())
    outcomes = [row[0] for row in conn.execute(
        "SELECT outcome FROM retry_chain WHERE request_id = ? ORDER BY attempt_order",
        (request_row["request_id"],),
    ).fetchall()]
    assert request_row["retry_count"] == 2
    assert outcomes == ["http_error", "http_error", "success"]
    assert m["cooldown"].get_state(route.candidates[0][0].key, "claude-real") is None
    assert m["scorer"].get_stats(route.candidates[0][0].key, "claude-real")["total_requests"] == 1


async def test_xai_direct_503_retries_same_channel_without_health_penalty(m, monkeypatch):
    """Direct api.x.ai 503 is the REST counterpart of xAI SDK UNAVAILABLE."""
    _setup(m)
    monkeypatch.setattr(m["failover"], "_overload_retry_delay_seconds", lambda _ordinal: 0.0)
    router = MockRouter()
    attempts = {"count": 0}

    def handler(req: httpx.Request):
        attempts["count"] += 1
        if attempts["count"] < 3:
            return httpx.Response(503, json={
                "error": {"type": "server_error", "message": "Service temporarily unavailable."},
            })
        return _responses_sse_response("xai overload recovered")

    router.register("https://api.x.ai", handler)
    _install_channels(m, [
        _make_openai_channel(
            "xai-overload",
            "https://api.x.ai",
            protocol="openai-responses",
            alias="grok",
            real="grok-real",
        ),
    ])

    body = {
        "model": "grok",
        "stream": True,
        "max_tokens": 32,
        "messages": [{"role": "user", "content": "ping"}],
    }
    resp, mc, route = await _call_anthropic_core(m, router, body)
    text = await _consume_streaming_to_string(resp)
    await mc.aclose()

    assert resp.status_code == 200
    assert "xai overload recovered" in text
    assert attempts["count"] == 3
    assert [str(req.url) for req in router.requests] == [
        "https://api.x.ai/v1/responses",
    ] * 3
    conn = m["log_db"]._get_conn()
    request_row = dict(conn.execute(
        "SELECT request_id, retry_count FROM request_log ORDER BY id DESC LIMIT 1"
    ).fetchone())
    outcomes = [row[0] for row in conn.execute(
        "SELECT outcome FROM retry_chain WHERE request_id = ? ORDER BY attempt_order",
        (request_row["request_id"],),
    ).fetchall()]
    assert request_row["retry_count"] == 2
    assert outcomes == ["http_error", "http_error", "success"]
    assert m["cooldown"].get_state(route.candidates[0][0].key, "grok-real") is None
    assert m["scorer"].get_stats(route.candidates[0][0].key, "grok-real")["total_requests"] == 1


def test_zhipu_quota_parser_rejects_generic_429_and_non_zhipu(m):
    parser = m["failover"].quota_errors.zhipu_1310_reset_ms
    reset = (datetime.now(timezone(timedelta(hours=8))) + timedelta(days=1)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    zhipu = SimpleNamespace(base_url="https://open.bigmodel.cn/api/anthropic")
    other = SimpleNamespace(base_url="https://other.example")
    generic = '{"error":{"code":"1302","message":"RPM limit"}}'
    exact = f'{{"error":{{"code":"1310","message":"限额将在 {reset} 重置"}}}}'
    assert parser(zhipu, http_status=429, error_detail=generic) is None
    assert parser(zhipu, http_status=503, error_detail=exact) is None
    assert parser(other, http_status=429, error_detail=exact) is None
    assert parser(zhipu, http_status=429, error_detail=exact) is not None


def test_zhipu_quota_cooldown_notification_is_explicit_and_links_channel(m, monkeypatch):
    calls = []
    monkeypatch.setattr(
        m["failover"].notifier,
        "throttled_notify_event_sync",
        lambda event, alert, text, **kwargs: calls.append((event, alert, text, kwargs)),
    )
    channel = SimpleNamespace(
        key="api:智谱 Max",
        type="api",
        display_name="智谱 Max",
    )
    reset_ms = int(datetime(2026, 7, 26, 10, 1, 10, tzinfo=timezone(timedelta(hours=8))).timestamp() * 1000)
    m["failover"]._notify_zhipu_quota_cooldown(channel, "glm-5.2", reset_ms)

    assert len(calls) == 1
    event, alert, text, kwargs = calls[0]
    assert event == "quota_cooldown"
    assert "api:智谱 Max:glm-5.2" in alert
    assert "周/月使用额度已达上限" in text
    assert "2026-07-26 10:01:10" in text
    assert "仅跳过" in text and "不是手动禁用" in text
    button = kwargs["reply_markup"]["inline_keyboard"][0][0]
    assert button["text"] == "📡 查看渠道详情"
    assert button["callback_data"].startswith("ch:view:")


async def test_zhipu_1310_cools_only_channel_model_until_reset_and_fails_over(m, monkeypatch):
    _setup(m)
    router = MockRouter()
    bjt = timezone(timedelta(hours=8))
    reset_dt = (datetime.now(bjt) + timedelta(days=2)).replace(microsecond=0)
    reset_text = reset_dt.strftime("%Y-%m-%d %H:%M:%S")
    reset_ms = int(reset_dt.timestamp() * 1000)
    notices = []
    monkeypatch.setattr(
        m["failover"],
        "_notify_zhipu_quota_cooldown",
        lambda ch, model, until: notices.append((ch.key, model, until)),
    )

    def zhipu_handler(req: httpx.Request):
        return httpx.Response(429, json={
            "type": "error",
            "error": {
                "type": "rate_limit_error",
                "code": "1310",
                "message": f"[1310][您已达到每周/每月使用上限，您的限额将在 {reset_text} 重置。][req-1]",
            },
            "request_id": "req-1",
        })

    router.register("https://open.bigmodel.cn", zhipu_handler)
    router.register("https://zhipu-fallback.example", lambda req: _anthropic_sse_response("fallback ok"))
    _install_channels(m, [
        _make_anthropic_channel(
            m, "智谱 Max", "https://open.bigmodel.cn/api/anthropic",
            alias="glm", real="glm-5.2",
        ),
        _make_anthropic_channel(
            m, "智谱老号", "https://zhipu-fallback.example",
            alias="glm", real="glm-5.2",
        ),
    ])

    body = {
        "model": "glm",
        "stream": True,
        "max_tokens": 32,
        "messages": [{"role": "user", "content": "ping"}],
    }
    resp, mc, route = await _call_anthropic_core(m, router, body)
    text = await _consume_streaming_to_string(resp)
    await mc.aclose()

    assert resp.status_code == 200
    assert "fallback ok" in text
    assert [str(req.url) for req in router.requests] == [
        "https://open.bigmodel.cn/api/anthropic/v1/messages",
        "https://zhipu-fallback.example/v1/messages",
    ]
    zhipu_channel = route.candidates[0][0]
    state = m["cooldown"].get_state(zhipu_channel.key, "glm-5.2")
    assert state is not None
    assert state["error_count"] == 1
    assert state["cooldown_until"] == reset_ms
    assert m["cooldown"].is_blocked(zhipu_channel.key, "glm-5.2")
    assert zhipu_channel.enabled is True
    assert notices == [(zhipu_channel.key, "glm-5.2", reset_ms)]
    assert m["scorer"].get_stats(zhipu_channel.key, "glm-5.2")["total_requests"] == 1


async def test_openai_overload_exhaustion_scores_and_cools_once(m, monkeypatch):
    """Only the terminal third overload may affect health/cooldown state."""
    _setup(m)
    monkeypatch.setattr(m["failover"], "_overload_retry_delay_seconds", lambda _ordinal: 0.0)
    router = MockRouter()
    overload_sse = b"".join([
        _responses_sse_event("response.created", {
            "type": "response.created",
            "response": {"id": "resp_overloaded_final", "status": "in_progress"},
        }),
        _responses_sse_event("error", {
            "type": "error",
            "error": {
                "type": "service_unavailable_error",
                "code": "server_is_overloaded",
                "message": "Our servers are currently overloaded. Please try again later.",
                "param": None,
            },
        }),
    ])
    router.register(
        "https://openai-overload-exhausted.example",
        lambda req: httpx.Response(
            200, content=overload_sse, headers={"content-type": "text/event-stream"},
        ),
    )
    _install_channels(m, [
        _make_openai_channel(
            "openai-overload-exhausted",
            "https://openai-overload-exhausted.example",
            protocol="openai-responses",
            alias="sonnet",
            real="gpt-real",
        ),
    ])

    body = {
        "model": "sonnet",
        "stream": True,
        "max_tokens": 32,
        "messages": [{"role": "user", "content": "ping"}],
    }
    resp, mc, route = await _call_anthropic_core(m, router, body)
    await mc.aclose()

    assert resp.status_code == 503
    assert len(router.requests) == 3
    conn = m["log_db"]._get_conn()
    request_row = dict(conn.execute(
        "SELECT request_id, retry_count FROM request_log ORDER BY id DESC LIMIT 1"
    ).fetchone())
    outcomes = [row[0] for row in conn.execute(
        "SELECT outcome FROM retry_chain WHERE request_id = ? ORDER BY attempt_order",
        (request_row["request_id"],),
    ).fetchall()]
    assert request_row["retry_count"] == 3
    assert outcomes == ["upstream_error_json", "upstream_error_json", "upstream_error_json"]
    state = m["cooldown"].get_state(route.candidates[0][0].key, "gpt-real")
    assert state is not None
    assert state["error_count"] == 1
    assert m["cooldown"].is_blocked(route.candidates[0][0].key, "gpt-real")
    assert m["scorer"].get_stats(route.candidates[0][0].key, "gpt-real")["total_requests"] == 1


async def test_anthropic_client_to_openai_responses_stream_pre_visible_error_fails_over(m):
    _setup(m)
    router = MockRouter()

    def bad_handler(req: httpx.Request):
        assert str(req.url) == "https://bad-responses-sse.example/v1/responses"
        return httpx.Response(
            200,
            content=b"".join([
                _responses_sse_event("response.created", {
                    "type": "response.created",
                    "sequence_number": 1,
                    "response": {"id": "resp_bad", "status": "in_progress"},
                }),
                _responses_sse_event("error", {
                    "type": "error",
                    "error": {"type": "server_error", "message": "pre-visible boom"},
                }),
            ]),
            headers={"content-type": "text/event-stream"},
        )

    def good_handler(req: httpx.Request):
        assert str(req.url) == "https://good-responses-sse.example/v1/responses"
        return _responses_sse_response("good after pre-visible error")

    router.register("https://bad-responses-sse.example", bad_handler)
    router.register("https://good-responses-sse.example", good_handler)
    _install_channels(m, [
        _make_openai_channel("bad-responses-sse", "https://bad-responses-sse.example", protocol="openai-responses", alias="sonnet", real="gpt-real"),
        _make_openai_channel("good-responses-sse", "https://good-responses-sse.example", protocol="openai-responses", alias="sonnet", real="gpt-real"),
    ])

    body = {"model": "sonnet", "stream": True, "max_tokens": 32, "messages": [{"role": "user", "content": "ping"}]}
    resp, mc, route = await _call_anthropic_core(m, router, body)
    text = await _consume_streaming_to_string(resp)
    await mc.aclose()

    assert resp.status_code == 200
    assert [str(req.url) for req in router.requests] == [
        "https://bad-responses-sse.example/v1/responses",
        "https://good-responses-sse.example/v1/responses",
    ]
    assert "pre-visible boom" not in text
    assert "good after pre-visible error" in text
    assert "event: message_stop" in text

    conn = m["log_db"]._get_conn()
    rid = conn.execute("SELECT request_id FROM request_log ORDER BY id DESC LIMIT 1").fetchone()[0]
    rows = [dict(r) for r in conn.execute(
        "SELECT channel_key, outcome FROM retry_chain WHERE request_id = ? ORDER BY attempt_order",
        (rid,),
    ).fetchall()]
    assert rows == [
        {"channel_key": route.candidates[0][0].key, "outcome": "upstream_error_json"},
        {"channel_key": route.candidates[1][0].key, "outcome": "success"},
    ]


async def test_anthropic_client_to_openai_responses_stream_post_visible_error_does_not_fail_over(m):
    _setup(m)
    router = MockRouter()

    def bad_handler(req: httpx.Request):
        assert str(req.url) == "https://bad-post-visible.example/v1/responses"
        return httpx.Response(
            200,
            content=b"".join([
                _responses_sse_event("response.output_text.delta", {
                    "type": "response.output_text.delta",
                    "sequence_number": 1,
                    "output_index": 0,
                    "content_index": 0,
                    "delta": "visible before error",
                }),
                _responses_sse_event("error", {
                    "type": "error",
                    "error": {"type": "server_error", "message": "post-visible boom"},
                }),
            ]),
            headers={"content-type": "text/event-stream"},
        )

    def good_handler(req: httpx.Request):
        assert str(req.url) == "https://good-post-visible.example/v1/responses"
        return _responses_sse_response("should not be used")

    router.register("https://bad-post-visible.example", bad_handler)
    router.register("https://good-post-visible.example", good_handler)
    _install_channels(m, [
        _make_openai_channel("bad-post-visible", "https://bad-post-visible.example", protocol="openai-responses", alias="sonnet", real="gpt-real"),
        _make_openai_channel("good-post-visible", "https://good-post-visible.example", protocol="openai-responses", alias="sonnet", real="gpt-real"),
    ])

    body = {"model": "sonnet", "stream": True, "max_tokens": 32, "messages": [{"role": "user", "content": "ping"}]}
    resp, mc, route = await _call_anthropic_core(m, router, body)
    text = await _consume_streaming_to_string(resp)
    await mc.aclose()

    assert resp.status_code == 200
    assert [str(req.url) for req in router.requests] == [
        "https://bad-post-visible.example/v1/responses",
    ]
    assert "visible before error" in text
    assert "post-visible boom" in text
    assert "should not be used" not in text

    conn = m["log_db"]._get_conn()
    rid = conn.execute("SELECT request_id FROM request_log ORDER BY id DESC LIMIT 1").fetchone()[0]
    row = dict(conn.execute(
        "SELECT status, final_channel_key, error_message FROM request_log WHERE request_id = ?",
        (rid,),
    ).fetchone())
    assert row["status"] == "error"
    assert row["final_channel_key"] == route.candidates[0][0].key
    assert "post-visible boom" in row["error_message"]


async def test_anthropic_client_to_openai_responses_stream_pre_commit_blacklist_fails_over(m):
    _setup(m)
    m["config"].update(lambda cfg: cfg.update({"contentBlacklist": {"default": ["blocked-token"], "byChannel": {}}}))
    router = MockRouter()

    def bad_handler(req: httpx.Request):
        assert str(req.url) == "https://bad-blacklist.example/v1/responses"
        return _responses_sse_response("blocked-token")

    def good_handler(req: httpx.Request):
        assert str(req.url) == "https://good-blacklist.example/v1/responses"
        return _responses_sse_response("clean after blacklist")

    router.register("https://bad-blacklist.example", bad_handler)
    router.register("https://good-blacklist.example", good_handler)
    _install_channels(m, [
        _make_openai_channel("bad-blacklist", "https://bad-blacklist.example", protocol="openai-responses", alias="sonnet", real="gpt-real"),
        _make_openai_channel("good-blacklist", "https://good-blacklist.example", protocol="openai-responses", alias="sonnet", real="gpt-real"),
    ])

    body = {"model": "sonnet", "stream": True, "max_tokens": 32, "messages": [{"role": "user", "content": "ping"}]}
    resp, mc, route = await _call_anthropic_core(m, router, body)
    text = await _consume_streaming_to_string(resp)
    await mc.aclose()

    assert resp.status_code == 200
    assert [str(req.url) for req in router.requests] == [
        "https://bad-blacklist.example/v1/responses",
        "https://good-blacklist.example/v1/responses",
    ]
    assert "blocked-token" not in text
    assert "clean after blacklist" in text

    conn = m["log_db"]._get_conn()
    rid = conn.execute("SELECT request_id FROM request_log ORDER BY id DESC LIMIT 1").fetchone()[0]
    rows = [dict(r) for r in conn.execute(
        "SELECT channel_key, outcome FROM retry_chain WHERE request_id = ? ORDER BY attempt_order",
        (rid,),
    ).fetchall()]
    assert rows == [
        {"channel_key": route.candidates[0][0].key, "outcome": "blacklist_hit"},
        {"channel_key": route.candidates[1][0].key, "outcome": "success"},
    ]


async def test_anthropic_client_to_openai_responses_stream_post_commit_blacklist_does_not_fail_over(m):
    _setup(m)
    m["config"].update(lambda cfg: cfg.update({"contentBlacklist": {"default": ["blocked-token"], "byChannel": {}}}))
    router = MockRouter()

    def bad_handler(req: httpx.Request):
        assert str(req.url) == "https://bad-post-blacklist.example/v1/responses"
        chunks = [
            _responses_sse_event("response.output_text.delta", {
                "type": "response.output_text.delta",
                "sequence_number": 1,
                "output_index": 0,
                "content_index": 0,
                "delta": "clean first",
            }),
            b"".join([
            _responses_sse_event("response.output_text.delta", {
                "type": "response.output_text.delta",
                "sequence_number": 2,
                "output_index": 0,
                "content_index": 0,
                "delta": "blocked-token",
            }),
            _responses_sse_event("response.completed", {
                "type": "response.completed",
                "sequence_number": 3,
                "response": {"id": "resp_post_bl", "status": "completed", "usage": {"input_tokens": 1, "output_tokens": 2}},
            }),
            ]),
        ]
        return httpx.Response(200, stream=ChunkedByteStream(chunks), headers={"content-type": "text/event-stream"})

    def good_handler(req: httpx.Request):
        assert str(req.url) == "https://good-post-blacklist.example/v1/responses"
        return _responses_sse_response("should not be used")

    router.register("https://bad-post-blacklist.example", bad_handler)
    router.register("https://good-post-blacklist.example", good_handler)
    _install_channels(m, [
        _make_openai_channel("bad-post-blacklist", "https://bad-post-blacklist.example", protocol="openai-responses", alias="sonnet", real="gpt-real"),
        _make_openai_channel("good-post-blacklist", "https://good-post-blacklist.example", protocol="openai-responses", alias="sonnet", real="gpt-real"),
    ])

    body = {"model": "sonnet", "stream": True, "max_tokens": 32, "messages": [{"role": "user", "content": "ping"}]}
    resp, mc, route = await _call_anthropic_core(m, router, body)
    text = await _consume_streaming_to_string(resp)
    await mc.aclose()

    assert resp.status_code == 200
    assert [str(req.url) for req in router.requests] == [
        "https://bad-post-blacklist.example/v1/responses",
    ]
    assert "clean first" in text
    assert '"text":"blocked-token"' not in text
    assert '"delta":{"type":"text_delta","text":"blocked-token"}' not in text
    assert "blacklist: blocked-token" in text
    assert "should not be used" not in text

    conn = m["log_db"]._get_conn()
    rid = conn.execute("SELECT request_id FROM request_log ORDER BY id DESC LIMIT 1").fetchone()[0]
    row = dict(conn.execute(
        "SELECT status, final_channel_key, error_message FROM request_log WHERE request_id = ?",
        (rid,),
    ).fetchone())
    assert row["status"] == "error"
    assert row["final_channel_key"] == route.candidates[0][0].key
    assert "blacklist: blocked-token" in row["error_message"]


async def test_chat_client_to_anthropic_fake_upstream(m):
    _setup(m)
    _install_keys(m, _default_key())
    router = MockRouter()

    def handler(req: httpx.Request):
        assert str(req.url) == "https://anthropic.example/v1/messages"
        payload = _json_request(req)
        assert payload["model"] == "claude-real"
        assert payload["messages"][0]["role"] == "user"
        assert payload["messages"][0]["content"][0]["type"] == "text"
        assert payload["messages"][0]["content"][0]["text"] == "ping"
        return _anthropic_response("chat client pong")

    router.register("https://anthropic.example", handler)
    _install_channels(m, [
        _make_anthropic_channel(m, "anth", "https://anthropic.example", alias="gpt-5", real="claude-real"),
    ])

    body = {"model": "gpt-5", "stream": False, "max_tokens": 32, "messages": [{"role": "user", "content": "ping"}]}
    resp, mc = await _call_openai_handler(m, router, "chat", body)
    await mc.aclose()

    assert resp.status_code == 200
    out = json.loads(resp.body)
    assert out["object"] == "chat.completion"
    assert out["choices"][0]["message"] == {"role": "assistant", "content": "chat client pong"}
    assert out["usage"]["prompt_tokens"] == 9
    assert out["usage"]["prompt_tokens_details"]["cached_tokens"] == 2


async def test_chat_client_assistant_refusal_to_openai_responses_fake_upstream(m):
    _setup(m)
    _install_keys(m, _default_key())
    router = MockRouter()
    captured: dict[str, object] = {}

    def handler(req: httpx.Request):
        assert str(req.url) == "https://responses-chat-refusal.example/v1/responses"
        payload = _json_request(req)
        captured["payload"] = payload
        return _responses_response("chat refusal pong")

    router.register("https://responses-chat-refusal.example", handler)
    _install_channels(m, [
        _make_openai_channel(
            "responses-chat-refusal",
            "https://responses-chat-refusal.example",
            protocol="openai-responses",
            alias="gpt-5",
            real="gpt-real",
        ),
    ])

    body = {
        "model": "gpt-5",
        "stream": False,
        "messages": [
            {"role": "user", "content": "classified request"},
            {"role": "assistant", "content": [
                {"type": "refusal", "refusal": "I can't help with that."},
            ]},
            {"role": "user", "content": "thanks"},
        ],
    }
    resp, mc = await _call_openai_handler(m, router, "chat", body)
    await mc.aclose()

    assert resp.status_code == 200
    assert json.loads(resp.body)["choices"][0]["message"]["content"] == "chat refusal pong"
    payload = captured["payload"]
    assert payload["model"] == "gpt-real"
    assert payload["input"] == [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "classified request"}]},
        {"type": "message", "role": "assistant", "content": [{"type": "refusal", "refusal": "I can't help with that."}]},
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "thanks"}]},
    ]


async def test_chat_client_logprobs_to_openai_responses_fake_upstream(m):
    _setup(m)
    _install_keys(m, _default_key())
    router = MockRouter()
    captured: dict[str, object] = {}

    def handler(req: httpx.Request):
        assert str(req.url) == "https://responses-chat-logprobs.example/v1/responses"
        payload = _json_request(req)
        captured["payload"] = payload
        return httpx.Response(200, json={
            "id": "resp_logprobs",
            "object": "response",
            "status": "completed",
            "created_at": 1,
            "model": "gpt-real",
            "output": [{
                "type": "message",
                "id": "msg_1",
                "role": "assistant",
                "content": [{
                    "type": "output_text",
                    "text": "pong",
                    "annotations": [],
                    "logprobs": [{
                        "token": "pong",
                        "bytes": [112, 111, 110, 103],
                        "logprob": -0.02,
                        "top_logprobs": [{"token": "pong", "bytes": [112, 111, 110, 103], "logprob": -0.02}],
                    }],
                }],
            }],
            "output_text": "pong",
            "usage": {"input_tokens": 4, "output_tokens": 1, "total_tokens": 5},
        })

    router.register("https://responses-chat-logprobs.example", handler)
    _install_channels(m, [
        _make_openai_channel(
            "responses-chat-logprobs",
            "https://responses-chat-logprobs.example",
            protocol="openai-responses",
            alias="gpt-5",
            real="gpt-real",
        ),
    ])

    body = {
        "model": "gpt-5",
        "stream": False,
        "messages": [{"role": "user", "content": "ping"}],
        "logprobs": True,
        "top_logprobs": 2,
    }
    resp, mc = await _call_openai_handler(m, router, "chat", body)
    await mc.aclose()

    assert resp.status_code == 200
    payload = captured["payload"]
    assert payload["include"] == ["message.output_text.logprobs"]
    assert payload["top_logprobs"] == 2
    out = json.loads(resp.body)
    assert out["choices"][0]["message"]["content"] == "pong"
    assert out["choices"][0]["logprobs"]["content"][0]["token"] == "pong"


async def test_chat_client_input_audio_to_openai_responses_fake_upstream(m):
    _setup(m)
    _install_keys(m, _default_key())
    router = MockRouter()
    captured: dict[str, object] = {}

    def handler(req: httpx.Request):
        assert str(req.url) == "https://responses-chat-audio.example/v1/responses"
        payload = _json_request(req)
        captured["payload"] = payload
        return _responses_response("chat audio pong")

    router.register("https://responses-chat-audio.example", handler)
    _install_channels(m, [
        _make_openai_channel(
            "responses-chat-audio",
            "https://responses-chat-audio.example",
            protocol="openai-responses",
            alias="gpt-5",
            real="gpt-real",
        ),
    ])

    body = {
        "model": "gpt-5",
        "stream": False,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "transcribe this"},
            {"type": "input_audio", "input_audio": {"data": "AAAA", "format": "wav"}},
        ]}],
    }
    resp, mc = await _call_openai_handler(m, router, "chat", body)
    await mc.aclose()

    assert resp.status_code == 200
    assert json.loads(resp.body)["choices"][0]["message"]["content"] == "chat audio pong"
    assert captured["payload"]["input"] == [{
        "type": "message",
        "role": "user",
        "content": [
            {"type": "input_text", "text": "transcribe this"},
            {"type": "input_audio", "input_audio": {"data": "AAAA", "format": "wav"}},
        ],
    }]


async def test_chat_client_tool_output_file_url_to_openai_responses_fake_upstream(m):
    _setup(m)
    _install_keys(m, _default_key())
    router = MockRouter()
    captured: dict[str, object] = {}

    def handler(req: httpx.Request):
        assert str(req.url) == "https://responses-chat-tool-file.example/v1/responses"
        payload = _json_request(req)
        captured["payload"] = payload
        return _responses_response("chat tool file url pong")

    router.register("https://responses-chat-tool-file.example", handler)
    _install_channels(m, [
        _make_openai_channel(
            "responses-chat-tool-file",
            "https://responses-chat-tool-file.example",
            protocol="openai-responses",
            alias="gpt-5",
            real="gpt-real",
        ),
    ])

    body = {
        "model": "gpt-5",
        "stream": False,
        "messages": [
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {"name": "inspect", "arguments": "{}"},
            }]},
            {"role": "tool", "tool_call_id": "call_1", "content": [
                {"type": "text", "text": "see attached"},
                {"type": "file", "file": {
                    "filename": "remote.pdf",
                    "file_url": "https://example.com/remote.pdf",
                }},
            ]},
        ],
        "tools": [{"type": "function", "function": {"name": "inspect", "parameters": {"type": "object"}}}],
    }
    resp, mc = await _call_openai_handler(m, router, "chat", body)
    await mc.aclose()

    assert resp.status_code == 200
    assert json.loads(resp.body)["choices"][0]["message"]["content"] == "chat tool file url pong"
    payload = captured["payload"]
    assert payload["model"] == "gpt-real"
    assert payload["input"] == [
        {
            "type": "function_call",
            "id": "fc_call_1",
            "call_id": "call_1",
            "name": "inspect",
            "arguments": "{}",
            "status": "completed",
        },
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": [
                {"type": "input_text", "text": "see attached"},
                {"type": "input_file", "file_url": "https://example.com/remote.pdf", "filename": "remote.pdf"},
            ],
        },
    ]


async def test_chat_client_file_data_to_anthropic_fake_upstream(m):
    _setup(m)
    _install_keys(m, _default_key())
    router = MockRouter()
    captured: dict[str, object] = {}

    def handler(req: httpx.Request):
        assert str(req.url) == "https://anthropic-file.example/v1/messages"
        payload = _json_request(req)
        captured["payload"] = payload
        return _anthropic_response("chat file pong")

    router.register("https://anthropic-file.example", handler)
    _install_channels(m, [
        _make_anthropic_channel(m, "anth-file", "https://anthropic-file.example", alias="gpt-5", real="claude-real"),
    ])

    body = {
        "model": "gpt-5",
        "stream": False,
        "max_tokens": 32,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "read"},
            {"type": "file", "file": {"filename": "case.pdf", "file_data": "data:application/pdf;base64,JVBERi0xLjQ="}},
        ]}],
    }
    resp, mc = await _call_openai_handler(m, router, "chat", body)
    await mc.aclose()

    assert resp.status_code == 200
    payload = captured["payload"]
    content = payload["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "read"}
    assert {k: content[1][k] for k in ("type", "source", "title")} == {
        "type": "document",
        "source": {"type": "base64", "media_type": "application/pdf", "data": "JVBERi0xLjQ="},
        "title": "case.pdf",
    }


async def test_chat_client_file_id_to_anthropic_is_guarded(m):
    _setup(m)
    _install_keys(m, _default_key())
    router = MockRouter()
    router.register("https://anthropic-file.example", lambda req: _anthropic_response("should not be called"))
    _install_channels(m, [
        _make_anthropic_channel(m, "anth-file", "https://anthropic-file.example", alias="gpt-5", real="claude-real"),
    ])

    body = {
        "model": "gpt-5",
        "stream": False,
        "messages": [{"role": "user", "content": [
            {"type": "file", "file": {"file_id": "file_1"}},
        ]}],
    }
    resp, mc = await _call_openai_handler(m, router, "chat", body)
    await mc.aclose()

    assert resp.status_code == 400
    assert "file" in resp.body.decode()


async def test_chat_client_explicit_prompt_cache_key_to_anthropic_is_stripped(m):
    _setup(m)
    _install_keys(m, _default_key())
    router = MockRouter()
    captured: dict[str, object] = {}

    def handler(req: httpx.Request):
        payload = _json_request(req)
        captured["payload"] = payload
        return _anthropic_response("prompt cache stripped")

    router.register("https://anthropic.example", handler)
    _install_channels(m, [
        _make_anthropic_channel(m, "anth", "https://anthropic.example", alias="gpt-5", real="claude-real"),
    ])

    body = {
        "model": "gpt-5",
        "stream": False,
        "messages": [{"role": "user", "content": "ping"}],
        "prompt_cache_key": "user-cache-key",
    }
    resp, mc = await _call_openai_handler(m, router, "chat", body)
    await mc.aclose()

    assert resp.status_code == 200
    assert "prompt_cache_key" not in captured["payload"]
    out = json.loads(resp.body)
    assert out["choices"][0]["message"]["content"] == "prompt cache stripped"


async def test_chat_client_allowed_tools_to_anthropic_filters_upstream_tools(m):
    _setup(m)
    _install_keys(m, _default_key())
    router = MockRouter()
    captured: dict[str, object] = {}

    def handler(req: httpx.Request):
        assert str(req.url) == "https://anthropic-chat-allowed.example/v1/messages"
        payload = _json_request(req)
        captured["payload"] = payload
        return _anthropic_response("chat allowed tools pong")

    router.register("https://anthropic-chat-allowed.example", handler)
    _install_channels(m, [
        _make_anthropic_channel(m, "anth-chat-allowed", "https://anthropic-chat-allowed.example", alias="gpt-5", real="claude-real"),
    ])

    body = {
        "model": "gpt-5",
        "stream": False,
        "max_tokens": 32,
        "messages": [{"role": "user", "content": "ping"}],
        "tools": [
            {"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}},
            {"type": "function", "function": {"name": "search", "parameters": {"type": "object"}}},
        ],
        "tool_choice": {
            "type": "allowed_tools",
            "allowed_tools": {
                "mode": "required",
                "tools": [{"type": "function", "function": {"name": "search"}}],
            },
        },
        "parallel_tool_calls": False,
    }
    resp, mc = await _call_openai_handler(m, router, "chat", body)
    await mc.aclose()

    assert resp.status_code == 200
    out = json.loads(resp.body)
    assert out["choices"][0]["message"]["content"] == "chat allowed tools pong"
    payload = captured["payload"]
    assert {k: payload["messages"][0]["content"][0][k] for k in ("type", "text")} == {"type": "text", "text": "ping"}
    assert len(payload["tools"]) == 1
    assert {k: payload["tools"][0][k] for k in ("name", "input_schema")} == {
        "name": "search",
        "input_schema": {"type": "object"},
    }
    assert payload["tool_choice"] == {"type": "any", "disable_parallel_tool_use": True}


async def test_responses_client_to_anthropic_fake_upstream(m):
    _setup(m)
    _install_keys(m, _default_key())
    router = MockRouter()

    def handler(req: httpx.Request):
        assert str(req.url) == "https://anthropic.example/v1/messages"
        payload = _json_request(req)
        assert payload["model"] == "claude-real"
        assert payload["messages"][0]["role"] == "user"
        assert payload["messages"][0]["content"][0]["type"] == "text"
        assert payload["messages"][0]["content"][0]["text"] == "ping"
        return _anthropic_response("responses client pong")

    router.register("https://anthropic.example", handler)
    _install_channels(m, [
        _make_anthropic_channel(m, "anth", "https://anthropic.example", alias="gpt-5", real="claude-real"),
    ])

    body = {"model": "gpt-5", "stream": False, "max_output_tokens": 32, "input": "ping"}
    resp, mc = await _call_openai_handler(m, router, "responses", body)
    await mc.aclose()

    assert resp.status_code == 200
    out = json.loads(resp.body)
    assert out["object"] == "response"
    assert out["output_text"] == "responses client pong"
    assert out["output"][0]["content"][0]["text"] == "responses client pong"
    assert out["usage"]["input_tokens"] == 9
    assert out["usage"]["input_tokens_details"]["cached_tokens"] == 2


async def test_non_stream_success_survives_locked_state_db_for_all_ingress(m, monkeypatch):
    _setup(m)
    _install_keys(m, _default_key())
    router = MockRouter()
    router.register("https://locked-chat.example", lambda req: _chat_response("anthropic ingress ok"))
    router.register("https://locked-chat-other.example", lambda req: _chat_response("wrong anthropic route"))
    router.register("https://locked-anth-chat.example", lambda req: _anthropic_response("chat ingress ok"))
    router.register("https://locked-anth-chat-other.example", lambda req: _anthropic_response("wrong chat route"))
    router.register("https://locked-anth-responses.example", lambda req: _anthropic_response("responses ingress ok"))
    router.register("https://locked-anth-responses-other.example", lambda req: _anthropic_response("wrong responses route"))

    fingerprints = {
        "anthropic": "fp-anthropic-locked",
        "chat": "fp-chat-locked",
        "responses": "fp-responses-locked",
    }
    monkeypatch.setattr(
        m["scheduler"].fingerprint, "fingerprint_query",
        lambda *_args, **_kwargs: fingerprints["anthropic"],
    )
    monkeypatch.setattr(
        m["openai_handler"].fingerprint, "fingerprint_query_chat",
        lambda *_args, **_kwargs: fingerprints["chat"],
    )
    monkeypatch.setattr(
        m["openai_handler"].fingerprint, "fingerprint_query_responses",
        lambda *_args, **_kwargs: fingerprints["responses"],
    )
    monkeypatch.setattr(
        m["failover"].fingerprint, "fingerprint_write", lambda *_args, **_kwargs: fingerprints["anthropic"],
    )
    monkeypatch.setattr(
        m["failover"].fingerprint, "fingerprint_write_chat", lambda *_args, **_kwargs: fingerprints["chat"],
    )
    monkeypatch.setattr(
        m["failover"].fingerprint, "fingerprint_write_responses", lambda *_args, **_kwargs: fingerprints["responses"],
    )
    m["affinity"]._entries.clear()
    m["affinity"]._client_entries.clear()
    m["affinity"].upsert(fingerprints["anthropic"], "api:locked-chat", "gpt-real")
    m["affinity"].upsert(fingerprints["chat"], "api:locked-anth-chat", "claude-real")
    m["affinity"].upsert(fingerprints["responses"], "api:locked-anth-responses", "claude-real")
    before = m["affinity"].snapshot()

    state_conn = m["state_db"]._get_conn()
    original_busy_timeout = int(state_conn.execute("PRAGMA busy_timeout").fetchone()[0])
    started = time.monotonic()
    locker = sqlite3.connect(m["state_db"]._db_path, timeout=0.05)
    locker.execute("BEGIN IMMEDIATE")
    try:
        _install_channels(m, [
            _make_openai_channel(
                "locked-chat-other", "https://locked-chat-other.example",
                protocol="openai-chat", alias="sonnet", real="gpt-real",
            ),
            _make_openai_channel(
                "locked-chat", "https://locked-chat.example",
                protocol="openai-chat", alias="sonnet", real="gpt-real",
            ),
        ])
        anth_body = {
            "model": "sonnet", "stream": False, "max_tokens": 32,
            "messages": [{"role": "user", "content": "ping"}],
        }
        anth_resp, anth_client, anth_route = await _call_anthropic_core(m, router, anth_body)
        await anth_client.aclose()
        assert anth_resp.status_code == 200
        assert anth_route.affinity_hit is True
        assert json.loads(anth_resp.body)["content"][0]["text"] == "anthropic ingress ok"

        _install_channels(m, [
            _make_anthropic_channel(
                m, "locked-anth-chat-other", "https://locked-anth-chat-other.example",
                alias="gpt-5", real="claude-real",
            ),
            _make_anthropic_channel(
                m, "locked-anth-chat", "https://locked-anth-chat.example",
                alias="gpt-5", real="claude-real",
            ),
        ])
        chat_body = {
            "model": "gpt-5", "stream": False,
            "messages": [{"role": "user", "content": "ping"}],
        }
        chat_resp, chat_client = await _call_openai_handler(m, router, "chat", chat_body)
        await chat_client.aclose()
        assert chat_resp.status_code == 200
        assert json.loads(chat_resp.body)["choices"][0]["message"]["content"] == "chat ingress ok"

        _install_channels(m, [
            _make_anthropic_channel(
                m, "locked-anth-responses-other", "https://locked-anth-responses-other.example",
                alias="gpt-5", real="claude-real",
            ),
            _make_anthropic_channel(
                m, "locked-anth-responses", "https://locked-anth-responses.example",
                alias="gpt-5", real="claude-real",
            ),
        ])
        responses_body = {
            "model": "gpt-5", "stream": False, "input": "ping",
        }
        responses_resp, responses_client = await _call_openai_handler(
            m, router, "responses", responses_body,
        )
        await responses_client.aclose()
        assert responses_resp.status_code == 200
        assert json.loads(responses_resp.body)["output_text"] == "responses ingress ok"
    finally:
        locker.rollback()
        locker.close()

    assert time.monotonic() - started < 2.5
    assert int(state_conn.execute("PRAGMA busy_timeout").fetchone()[0]) == original_busy_timeout

    # Existing affinity hits take the bound route, but a locked state DB keeps
    # the previous memory value rather than publishing process-only timestamps.
    assert m["affinity"].snapshot() == before
    assert m["affinity"].client_snapshot() == {}


async def test_responses_native_state_passthrough_fake_upstream(m):
    _setup(m)
    _install_keys(m, _default_key())
    router = MockRouter()
    captured: dict[str, object] = {}

    def handler(req: httpx.Request):
        assert str(req.url) == "https://responses-native-state.example/v1/responses"
        payload = _json_request(req)
        captured["payload"] = payload
        return _responses_response("native state pong")

    router.register("https://responses-native-state.example", handler)
    _install_channels(m, [
        _make_openai_channel(
            "responses-native-state",
            "https://responses-native-state.example",
            protocol="openai-responses",
            alias="gpt-5",
            real="gpt-real",
        ),
    ])

    body = {
        "model": "gpt-5",
        "stream": False,
        "conversation": "conv_1",
        "background": True,
        "input": "ping",
        "openai_chat_only_hint": {"drop": True},
    }
    resp, mc = await _call_openai_handler(m, router, "responses", body)
    await mc.aclose()

    assert resp.status_code == 200
    assert json.loads(resp.body)["output_text"] == "native state pong"
    payload = captured["payload"]
    assert payload["model"] == "gpt-real"
    assert payload["conversation"] == "conv_1"
    assert payload["background"] is True
    assert payload["input"] == "ping"
    assert "openai_chat_only_hint" not in payload


async def test_native_responses_response_is_saved_for_fallback_previous_response_id(m):
    _setup(m)
    _install_keys(m, _default_key())
    from src.openai import store as openai_store

    openai_store.init()
    openai_store._reset_for_test()
    router = MockRouter()
    captured: dict[str, object] = {}

    def native_handler(req: httpx.Request):
        assert str(req.url) == "https://responses-store-seed.example/v1/responses"
        payload = _json_request(req)
        captured["seed_payload"] = payload
        return httpx.Response(200, json={
            "id": "resp_native_seed",
            "object": "response",
            "created_at": 1,
            "status": "completed",
            "model": "gpt-real",
            "output": [{
                "type": "message",
                "id": "msg_native_seed",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "native seed ok", "annotations": []}],
            }],
            "output_text": "native seed ok",
            "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
        }, headers={"content-type": "application/json"})

    def chat_handler(req: httpx.Request):
        assert str(req.url) == "https://chat-store-follow.example/v1/chat/completions"
        payload = _json_request(req)
        captured["follow_payload"] = payload
        return _chat_response("chat follow ok")

    router.register("https://responses-store-seed.example", native_handler)
    router.register("https://chat-store-follow.example", chat_handler)

    _install_channels(m, [
        _make_openai_channel(
            "responses-store-seed",
            "https://responses-store-seed.example",
            protocol="openai-responses",
            alias="gpt-5",
            real="gpt-real",
        ),
    ])
    resp1, mc1 = await _call_openai_handler(m, router, "responses", {
        "model": "gpt-5",
        "stream": False,
        "input": "seed question",
    })
    await mc1.aclose()
    assert resp1.status_code == 200
    assert json.loads(resp1.body)["id"] == "resp_native_seed"

    rec = openai_store.lookup("resp_native_seed", api_key_name="k")
    assert rec.parent_id is None
    assert rec.channel_key == "api:responses-store-seed"
    assert rec.input_items == [{"type": "message", "role": "user", "content": [
        {"type": "input_text", "text": "seed question"},
    ]}]
    assert rec.output_items[0]["content"][0]["text"] == "native seed ok"

    router.requests.clear()
    _install_channels(m, [
        _make_openai_channel(
            "chat-store-follow",
            "https://chat-store-follow.example",
            protocol="openai-chat",
            alias="gpt-5",
            real="gpt-real",
        ),
    ])
    resp2, mc2 = await _call_openai_handler(m, router, "responses", {
        "model": "gpt-5",
        "stream": False,
        "previous_response_id": "resp_native_seed",
        "input": "follow up",
    })
    await mc2.aclose()

    assert resp2.status_code == 200, resp2.body.decode()
    assert json.loads(resp2.body)["output_text"] == "chat follow ok"
    assert captured["follow_payload"]["messages"] == [
        {"role": "user", "content": "seed question"},
        {"role": "assistant", "content": "native seed ok", "reasoning_content": ""},
        {"role": "user", "content": "follow up"},
    ]


async def test_native_responses_stream_response_is_saved_for_previous_response_id(m):
    _setup(m)
    _install_keys(m, _default_key())
    from src.openai import store as openai_store

    openai_store.init()
    openai_store._reset_for_test()
    router = MockRouter()

    def handler(req: httpx.Request):
        assert str(req.url) == "https://responses-stream-store.example/v1/responses"
        payload = _json_request(req)
        assert payload["stream"] is True
        assert payload["input"] == "stream seed"
        return _responses_sse_response("native stream stored")

    router.register("https://responses-stream-store.example", handler)
    _install_channels(m, [
        _make_openai_channel(
            "responses-stream-store",
            "https://responses-stream-store.example",
            protocol="openai-responses",
            alias="gpt-5",
            real="gpt-real",
        ),
    ])

    resp, mc = await _call_openai_handler(m, router, "responses", {
        "model": "gpt-5",
        "stream": True,
        "input": "stream seed",
    })
    text = await _consume_streaming_to_string(resp)
    await mc.aclose()

    assert resp.status_code == 200
    assert "event: response.completed" in text
    rec = openai_store.lookup("resp_sse", api_key_name="k")
    assert rec.parent_id is None
    assert rec.channel_key == "api:responses-stream-store"
    assert rec.input_items == [{"type": "message", "role": "user", "content": [
        {"type": "input_text", "text": "stream seed"},
    ]}]
    assert rec.output_items[0]["content"][0]["text"] == "native stream stored"


async def test_native_responses_function_call_stream_finishes_request_log(m):
    _setup(m)
    _install_keys(m, _default_key())
    router = MockRouter()
    expected_body = _responses_sse_function_call_response().content

    def handler(req: httpx.Request):
        assert str(req.url) == "https://responses-stream-tool.example/v1/responses"
        return httpx.Response(
            200,
            content=expected_body,
            headers={"content-type": "text/event-stream"},
        )

    router.register("https://responses-stream-tool.example", handler)
    _install_channels(m, [
        _make_openai_channel(
            "responses-stream-tool",
            "https://responses-stream-tool.example",
            protocol="openai-responses",
            alias="gpt-5",
            real="gpt-real",
        ),
    ])

    resp, mc = await _call_openai_handler(m, router, "responses", {
        "model": "gpt-5",
        "stream": True,
        "input": "call lookup",
        "tools": [{
            "type": "function",
            "name": "lookup",
            "description": "Lookup a value",
            "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
        }],
    })
    text = await _consume_streaming_to_string(resp)
    await mc.aclose()

    assert "event: response.completed" in text
    assert text.encode("utf-8") == expected_body
    row = m["log_db"]._get_conn().execute(
        """SELECT request_id, status, http_status, error_message,
                  input_tokens, output_tokens, cache_read_tokens, usage_observed
             FROM request_log ORDER BY id DESC LIMIT 1"""
    ).fetchone()
    assert row is not None
    assert row["status"] == "success"
    assert row["http_status"] == 200
    assert row["error_message"] is None
    assert (row["input_tokens"], row["output_tokens"], row["cache_read_tokens"]) == (5, 4, 2)
    assert row["usage_observed"] == 1

    conn = m["log_db"]._get_conn()
    retry = conn.execute(
        "SELECT id, outcome FROM retry_chain WHERE request_id=?",
        (row["request_id"],),
    ).fetchone()
    assert retry is not None
    assert retry["outcome"] == "success"
    settlement = conn.execute(
        """SELECT outcome, usage_observed, input_tokens, output_tokens,
                  cache_read_tokens, dispatch_state
             FROM upstream_attempt_usage WHERE retry_attempt_id=?""",
        (retry["id"],),
    ).fetchone()
    assert settlement is not None
    assert dict(settlement) == {
        "outcome": "success",
        "usage_observed": 1,
        "input_tokens": 5,
        "output_tokens": 4,
        "cache_read_tokens": 2,
        "dispatch_state": "sent",
    }
    detail = conn.execute(
        "SELECT response_body FROM request_detail WHERE request_id=?",
        (row["request_id"],),
    ).fetchone()
    assert detail is not None
    assert detail["response_body"].encode("utf-8") == expected_body


async def test_native_responses_function_call_is_logged_before_terminal_event_is_yielded(m):
    _setup(m)
    _install_keys(m, _default_key())
    router = MockRouter()
    router.register(
        "https://responses-stream-tool-terminal.example",
        lambda req: _responses_sse_function_call_response(),
    )
    _install_channels(m, [
        _make_openai_channel(
            "responses-stream-tool-terminal",
            "https://responses-stream-tool-terminal.example",
            protocol="openai-responses",
            alias="gpt-5",
            real="gpt-real",
        ),
    ])

    resp, mc = await _call_openai_handler(m, router, "responses", {
        "model": "gpt-5",
        "stream": True,
        "input": "call lookup",
        "tools": [{
            "type": "function",
            "name": "lookup",
            "parameters": {"type": "object", "properties": {}},
        }],
    })
    iterator = resp.body_iterator
    emitted = b""
    for _ in range(20):
        chunk = await asyncio.wait_for(anext(iterator), timeout=1.0)
        emitted += chunk.encode("utf-8") if isinstance(chunk, str) else chunk
        if b"event: response.completed" in emitted:
            break

    assert b"event: response.completed" in emitted
    row = m["log_db"]._get_conn().execute(
        "SELECT status, http_status FROM request_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    assert dict(row) == {"status": "success", "http_status": 200}

    await iterator.aclose()
    await mc.aclose()


async def test_native_responses_function_call_releases_upstream_before_terminal_yield(m):
    _setup(m)
    _install_keys(m, _default_key())
    router = MockRouter()
    hanging = TerminalThenHangByteStream(_responses_sse_function_call_response().content)
    router.register(
        "https://responses-stream-tool-release.example",
        lambda req: httpx.Response(
            200,
            stream=hanging,
            headers={"content-type": "text/event-stream"},
        ),
    )
    _install_channels(m, [
        _make_openai_channel(
            "responses-stream-tool-release",
            "https://responses-stream-tool-release.example",
            protocol="openai-responses",
            alias="gpt-5",
            real="gpt-real",
            extra={"maxConcurrent": 1},
        ),
    ])

    resp, mc = await _call_openai_handler(m, router, "responses", {
        "model": "gpt-5",
        "stream": True,
        "input": "call lookup",
        "tools": [{
            "type": "function",
            "name": "lookup",
            "parameters": {"type": "object", "properties": {}},
        }],
    })
    channel_key = "api:responses-stream-tool-release"
    channel_rows = {
        row["channel_key"]: row
        for row in m["failover"].concurrency.snapshot()
    }
    assert channel_rows[channel_key]["in_flight"] == 1

    iterator = resp.body_iterator
    emitted = b""
    for _ in range(20):
        chunk = await asyncio.wait_for(anext(iterator), timeout=1.0)
        emitted += chunk.encode("utf-8") if isinstance(chunk, str) else chunk
        if b"event: response.completed" in emitted:
            break

    assert b"event: response.completed" in emitted
    assert hanging.closed.is_set()
    channel_rows = {
        row["channel_key"]: row
        for row in m["failover"].concurrency.snapshot()
    }
    assert channel_rows[channel_key]["in_flight"] == 0

    await iterator.aclose()
    await mc.aclose()


@pytest.mark.parametrize("terminal_kind", ["failed", "incomplete"])
async def test_native_responses_error_terminal_finalizes_before_yield(m, terminal_kind):
    _setup(m)
    _install_keys(m, _default_key())
    router = MockRouter()
    channel_name = f"responses-stream-{terminal_kind}-terminal"
    base_url = f"https://{channel_name}.example"
    response_id = f"resp_{terminal_kind}_terminal"
    visible_prefix = b"".join([
        _responses_sse_event("response.created", {
            "type": "response.created",
            "sequence_number": 1,
            "response": {"id": response_id, "status": "in_progress"},
        }),
        _responses_sse_event("response.output_item.added", {
            "type": "response.output_item.added",
            "sequence_number": 2,
            "output_index": 0,
            "item": {
                "type": "message",
                "id": f"msg_{terminal_kind}",
                "role": "assistant",
                "status": "in_progress",
                "content": [],
            },
        }),
        _responses_sse_event("response.output_text.delta", {
            "type": "response.output_text.delta",
            "sequence_number": 3,
            "item_id": f"msg_{terminal_kind}",
            "output_index": 0,
            "content_index": 0,
            "delta": "partial output",
        }),
    ])
    if terminal_kind == "failed":
        terminal_payload = _responses_sse_event("response.failed", {
            "type": "response.failed",
            "sequence_number": 4,
            "response": {
                "id": response_id,
                "status": "failed",
                "error": {"code": "server_error", "message": "generation failed"},
                "output": [],
                "usage": {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
            },
        })
        expected_output = b"event: response.failed"
        expected_http_status = 200
        expected_error = "generation failed"
        expected_retry_outcome = "stream_upstream_error"
    else:
        terminal_payload = _responses_sse_event("response.incomplete", {
            "type": "response.incomplete",
            "sequence_number": 4,
            "response": {
                "id": response_id,
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output": [],
                "usage": {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
            },
        })
        expected_output = b"context_length_exceeded"
        expected_http_status = 400
        expected_error = "max_output_tokens"
        expected_retry_outcome = "request_invalid"

    hanging = TerminalThenHangByteStream([visible_prefix, terminal_payload])
    router.register(
        base_url,
        lambda req: httpx.Response(
            200,
            stream=hanging,
            headers={"content-type": "text/event-stream"},
        ),
    )
    _install_channels(m, [
        _make_openai_channel(
            channel_name,
            base_url,
            protocol="openai-responses",
            alias="gpt-5",
            real="gpt-real",
            extra={"maxConcurrent": 1},
        ),
    ])

    resp, mc = await _call_openai_handler(m, router, "responses", {
        "model": "gpt-5",
        "stream": True,
        "input": "produce a terminal error",
    })
    channel_key = f"api:{channel_name}"
    channel_rows = {
        row["channel_key"]: row
        for row in m["failover"].concurrency.snapshot()
    }
    assert channel_rows[channel_key]["in_flight"] == 1

    iterator = resp.body_iterator
    emitted = b""
    for _ in range(20):
        chunk = await asyncio.wait_for(anext(iterator), timeout=1.0)
        emitted += chunk.encode("utf-8") if isinstance(chunk, str) else chunk
        if expected_output in emitted:
            break

    assert expected_output in emitted
    row = m["log_db"]._get_conn().execute(
        """SELECT request_id, status, http_status, error_message
             FROM request_log ORDER BY id DESC LIMIT 1"""
    ).fetchone()
    assert row is not None
    assert row["status"] == "error"
    assert row["http_status"] == expected_http_status
    assert expected_error in row["error_message"]
    retry = m["log_db"]._get_conn().execute(
        "SELECT outcome FROM retry_chain WHERE request_id=?",
        (row["request_id"],),
    ).fetchone()
    assert retry is not None
    assert retry["outcome"] == expected_retry_outcome
    assert hanging.closed.is_set()
    channel_rows = {
        item["channel_key"]: item
        for item in m["failover"].concurrency.snapshot()
    }
    assert channel_rows[channel_key]["in_flight"] == 0

    await iterator.aclose()
    await mc.aclose()


@pytest.mark.parametrize("terminal_kind", ["completed", "failed", "incomplete"])
async def test_queued_responses_terminal_releases_slot_before_yield(m, terminal_kind):
    _setup(m)
    _install_keys(m, _default_key())
    previous_concurrency = dict(m["config"].get().get("concurrency") or {})

    def _enable_queue(cfg):
        concurrency_cfg = cfg.setdefault("concurrency", {})
        concurrency_cfg["enabled"] = True
        concurrency_cfg["defaultMaxConcurrent"] = 1
        concurrency_cfg["queueWaitSeconds"] = 2

    m["config"].update(_enable_queue)
    router = MockRouter()
    channel_name = f"responses-queued-{terminal_kind}"
    channel_key = f"api:{channel_name}"
    base_url = f"https://{channel_name}.example"
    response_id = f"resp_queued_{terminal_kind}"
    message_id = f"msg_queued_{terminal_kind}"
    visible_prefix = b"".join([
        _responses_sse_event("response.created", {
            "type": "response.created",
            "sequence_number": 1,
            "response": {"id": response_id, "status": "in_progress"},
        }),
        _responses_sse_event("response.output_item.added", {
            "type": "response.output_item.added",
            "sequence_number": 2,
            "output_index": 0,
            "item": {
                "type": "message",
                "id": message_id,
                "role": "assistant",
                "status": "in_progress",
                "content": [],
            },
        }),
        _responses_sse_event("response.output_text.delta", {
            "type": "response.output_text.delta",
            "sequence_number": 3,
            "item_id": message_id,
            "output_index": 0,
            "content_index": 0,
            "delta": "queued partial output",
        }),
    ])
    if terminal_kind == "completed":
        terminal_payload = _responses_sse_event("response.completed", {
            "type": "response.completed",
            "sequence_number": 4,
            "response": {
                "id": response_id,
                "status": "completed",
                "output": [{
                    "type": "message",
                    "id": message_id,
                    "role": "assistant",
                    "status": "completed",
                    "content": [{
                        "type": "output_text",
                        "text": "queued partial output",
                        "annotations": [],
                    }],
                }],
                "usage": {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
            },
        })
        expected_output = b"event: response.completed"
        expected_status = "success"
        expected_http_status = 200
        expected_error = None
        expected_retry_outcome = "success"
    elif terminal_kind == "failed":
        terminal_payload = _responses_sse_event("response.failed", {
            "type": "response.failed",
            "sequence_number": 4,
            "response": {
                "id": response_id,
                "status": "failed",
                "error": {"code": "server_error", "message": "queued generation failed"},
                "output": [],
                "usage": {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
            },
        })
        expected_output = b"event: response.failed"
        expected_status = "error"
        expected_http_status = 200
        expected_error = "queued generation failed"
        expected_retry_outcome = "stream_upstream_error"
    else:
        terminal_payload = _responses_sse_event("response.incomplete", {
            "type": "response.incomplete",
            "sequence_number": 4,
            "response": {
                "id": response_id,
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output": [],
                "usage": {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
            },
        })
        expected_output = b"context_length_exceeded"
        expected_status = "error"
        expected_http_status = 400
        expected_error = "max_output_tokens"
        expected_retry_outcome = "request_invalid"

    hanging = TerminalThenHangByteStream([visible_prefix, terminal_payload])
    router.register(
        base_url,
        lambda req: httpx.Response(
            200,
            stream=hanging,
            headers={"content-type": "text/event-stream"},
        ),
    )
    _install_channels(m, [
        _make_openai_channel(
            channel_name,
            base_url,
            protocol="openai-responses",
            alias="gpt-5",
            real="gpt-real",
            extra={"maxConcurrent": 1},
        ),
    ])

    concurrency = m["failover"].concurrency
    assert await concurrency.try_acquire(channel_key) is True
    queued_call = asyncio.create_task(_call_openai_handler(m, router, "responses", {
        "model": "gpt-5",
        "stream": True,
        "input": "wait for a queued terminal response",
    }))

    queued_row = None
    for _ in range(100):
        queued_row = next(
            (row for row in concurrency.snapshot() if row["channel_key"] == channel_key),
            None,
        )
        if queued_row is not None and queued_row["waiting"] == 1:
            break
        await asyncio.sleep(0.01)
    assert queued_row is not None
    assert queued_row["in_flight"] == 1
    assert queued_row["waiting"] == 1

    concurrency.release(channel_key)
    resp, mc = await asyncio.wait_for(queued_call, timeout=2.0)
    acquired_row = next(
        row for row in concurrency.snapshot() if row["channel_key"] == channel_key
    )
    assert acquired_row["in_flight"] == 1
    assert acquired_row["waiting"] == 0

    iterator = resp.body_iterator
    emitted = b""
    for _ in range(20):
        chunk = await asyncio.wait_for(anext(iterator), timeout=1.0)
        emitted += chunk.encode("utf-8") if isinstance(chunk, str) else chunk
        if expected_output in emitted:
            break

    assert expected_output in emitted
    row = m["log_db"]._get_conn().execute(
        """SELECT request_id, status, http_status, error_message
             FROM request_log ORDER BY id DESC LIMIT 1"""
    ).fetchone()
    assert row is not None
    assert row["status"] == expected_status
    assert row["http_status"] == expected_http_status
    if expected_error is None:
        assert row["error_message"] is None
    else:
        assert expected_error in row["error_message"]
    retry = m["log_db"]._get_conn().execute(
        "SELECT outcome FROM retry_chain WHERE request_id=?",
        (row["request_id"],),
    ).fetchone()
    assert retry is not None
    assert retry["outcome"] == expected_retry_outcome
    assert hanging.closed.is_set()
    terminal_row = next(
        item for item in concurrency.snapshot() if item["channel_key"] == channel_key
    )
    assert terminal_row["in_flight"] == 0

    await iterator.aclose()
    await mc.aclose()

    def _restore_concurrency(cfg):
        cfg["concurrency"] = previous_concurrency

    m["config"].update(_restore_concurrency)


async def test_responses_client_text_instruction_items_to_openai_chat_fake_upstream(m):
    _setup(m)
    _install_keys(m, _default_key())
    router = MockRouter()
    captured: dict[str, object] = {}

    def handler(req: httpx.Request):
        assert str(req.url) == "https://chat-instructions.example/v1/chat/completions"
        payload = _json_request(req)
        captured["payload"] = payload
        return _chat_response("responses instructions chat pong")

    router.register("https://chat-instructions.example", handler)
    _install_channels(m, [
        _make_openai_channel(
            "chat-instructions",
            "https://chat-instructions.example",
            protocol="openai-chat",
            alias="gpt-5",
            real="gpt-real",
        ),
    ])

    body = {
        "model": "gpt-5",
        "stream": False,
        "instructions": [
            {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "follow policy"}]},
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "background"}]},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "noted"}]},
        ],
        "input": "ping",
    }
    resp, mc = await _call_openai_handler(m, router, "responses", body)
    await mc.aclose()

    assert resp.status_code == 200
    assert json.loads(resp.body)["output_text"] == "responses instructions chat pong"
    assert captured["payload"]["messages"] == [
        {"role": "system", "content": "follow policy"},
        {"role": "user", "content": "background"},
        {"role": "assistant", "content": "noted"},
        {"role": "user", "content": "ping"},
    ]


async def test_responses_client_local_item_reference_to_openai_chat_fake_upstream(m):
    _setup(m)
    _install_keys(m, _default_key())
    router = MockRouter()
    captured: dict[str, object] = {}

    def handler(req: httpx.Request):
        assert str(req.url) == "https://chat-local-ref.example/v1/chat/completions"
        payload = _json_request(req)
        captured["payload"] = payload
        return _chat_response("local ref chat pong")

    router.register("https://chat-local-ref.example", handler)
    _install_channels(m, [
        _make_openai_channel(
            "chat-local-ref",
            "https://chat-local-ref.example",
            protocol="openai-chat",
            alias="gpt-5",
            real="gpt-real",
        ),
    ])

    body = {
        "model": "gpt-5",
        "stream": False,
        "input": [
            {"type": "message", "id": "msg_1", "role": "user", "content": [
                {"type": "input_text", "text": "remember this"},
            ]},
            {"type": "item_reference", "id": "msg_1"},
        ],
    }
    resp, mc = await _call_openai_handler(m, router, "responses", body)
    await mc.aclose()

    assert resp.status_code == 200
    assert json.loads(resp.body)["output_text"] == "local ref chat pong"
    assert captured["payload"]["messages"] == [
        {"role": "user", "content": "remember this"},
        {"role": "user", "content": "remember this"},
    ]


async def test_responses_client_input_audio_to_openai_chat_fake_upstream(m):
    _setup(m)
    _install_keys(m, _default_key())
    router = MockRouter()
    captured: dict[str, object] = {}

    def handler(req: httpx.Request):
        assert str(req.url) == "https://chat-audio.example/v1/chat/completions"
        payload = _json_request(req)
        captured["payload"] = payload
        return _chat_response("responses audio pong")

    router.register("https://chat-audio.example", handler)
    _install_channels(m, [
        _make_openai_channel(
            "chat-audio",
            "https://chat-audio.example",
            protocol="openai-chat",
            alias="gpt-5",
            real="gpt-real",
        ),
    ])

    body = {
        "model": "gpt-5",
        "stream": False,
        "input": [{"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "transcribe"},
            {"type": "input_audio", "input_audio": {"data": "BBBB", "format": "mp3"}},
        ]}],
    }
    resp, mc = await _call_openai_handler(m, router, "responses", body)
    await mc.aclose()

    assert resp.status_code == 200
    assert json.loads(resp.body)["output_text"] == "responses audio pong"
    assert captured["payload"]["messages"] == [{
        "role": "user",
        "content": [
            {"type": "text", "text": "transcribe"},
            {"type": "input_audio", "input_audio": {"data": "BBBB", "format": "mp3"}},
        ],
    }]


async def test_responses_client_logprobs_to_openai_chat_fake_upstream(m):
    _setup(m)
    _install_keys(m, _default_key())
    router = MockRouter()
    captured: dict[str, object] = {}

    def handler(req: httpx.Request):
        assert str(req.url) == "https://chat-logprobs.example/v1/chat/completions"
        payload = _json_request(req)
        captured["payload"] = payload
        return httpx.Response(200, json={
            "id": "chatcmpl-logprobs",
            "object": "chat.completion",
            "created": 1,
            "model": "gpt-real",
            "choices": [{
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "pong"},
                "logprobs": {
                    "content": [{
                        "token": "pong",
                        "bytes": [112, 111, 110, 103],
                        "logprob": -0.02,
                        "top_logprobs": [{"token": "pong", "bytes": [112, 111, 110, 103], "logprob": -0.02}],
                    }],
                    "refusal": None,
                },
            }],
            "usage": {"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5},
        })

    router.register("https://chat-logprobs.example", handler)
    _install_channels(m, [
        _make_openai_channel(
            "chat-logprobs",
            "https://chat-logprobs.example",
            protocol="openai-chat",
            alias="gpt-5",
            real="gpt-real",
        ),
    ])

    body = {
        "model": "gpt-5",
        "stream": False,
        "input": "ping",
        "include": ["message.output_text.logprobs"],
        "top_logprobs": 2,
    }
    resp, mc = await _call_openai_handler(m, router, "responses", body)
    await mc.aclose()

    assert resp.status_code == 200
    assert captured["payload"]["logprobs"] is True
    assert captured["payload"]["top_logprobs"] == 2
    out = json.loads(resp.body)
    assert out["output_text"] == "pong"
    assert out["output"][0]["content"][0]["logprobs"][0]["token"] == "pong"


async def test_responses_client_text_instruction_items_to_anthropic_fake_upstream(m):
    _setup(m)
    _install_keys(m, _default_key())
    router = MockRouter()
    captured: dict[str, object] = {}

    def handler(req: httpx.Request):
        assert str(req.url) == "https://anthropic-instructions.example/v1/messages"
        payload = _json_request(req)
        captured["payload"] = payload
        return _anthropic_response("responses instructions anthropic pong")

    router.register("https://anthropic-instructions.example", handler)
    _install_channels(m, [
        _make_anthropic_channel(
            m,
            "anth-instructions",
            "https://anthropic-instructions.example",
            alias="gpt-5",
            real="claude-real",
        ),
    ])

    body = {
        "model": "gpt-5",
        "stream": False,
        "instructions": [
            {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "follow policy"}]},
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "background"}]},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "noted"}]},
        ],
        "input": "ping",
    }
    resp, mc = await _call_openai_handler(m, router, "responses", body)
    await mc.aclose()

    assert resp.status_code == 200
    assert json.loads(resp.body)["output_text"] == "responses instructions anthropic pong"
    payload = captured["payload"]
    assert [{k: block[k] for k in ("type", "text")} for block in payload["system"]] == [
        {"type": "text", "text": "follow policy"},
    ]
    assert [
        {
            "role": msg["role"],
            "content": [{k: block[k] for k in ("type", "text")} for block in msg["content"]],
        }
        for msg in payload["messages"]
    ] == [
        {"role": "user", "content": [{"type": "text", "text": "background"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "noted"}]},
        {"role": "user", "content": [{"type": "text", "text": "ping"}]},
    ]


async def test_responses_client_local_item_reference_to_anthropic_fake_upstream(m):
    _setup(m)
    _install_keys(m, _default_key())
    router = MockRouter()
    captured: dict[str, object] = {}

    def handler(req: httpx.Request):
        assert str(req.url) == "https://anthropic-local-ref.example/v1/messages"
        payload = _json_request(req)
        captured["payload"] = payload
        return _anthropic_response("local ref anthropic pong")

    router.register("https://anthropic-local-ref.example", handler)
    _install_channels(m, [
        _make_anthropic_channel(
            m,
            "anth-local-ref",
            "https://anthropic-local-ref.example",
            alias="gpt-5",
            real="claude-real",
        ),
    ])

    body = {
        "model": "gpt-5",
        "stream": False,
        "max_output_tokens": 32,
        "input": [
            {"type": "message", "id": "msg_1", "role": "user", "content": [
                {"type": "input_text", "text": "remember this"},
            ]},
            {"type": "item_reference", "id": "msg_1"},
        ],
    }
    resp, mc = await _call_openai_handler(m, router, "responses", body)
    await mc.aclose()

    assert resp.status_code == 200
    assert json.loads(resp.body)["output_text"] == "local ref anthropic pong"
    msgs = captured["payload"]["messages"]
    assert [msg["role"] for msg in msgs] == ["user"]
    assert [{k: part[k] for k in ("type", "text")} for part in msgs[0]["content"]] == [
        {"type": "text", "text": "remember this"},
        {"type": "text", "text": "remember this"},
    ]


async def test_responses_client_file_documents_to_anthropic_fake_upstream(m):
    _setup(m)
    _install_keys(m, _default_key())
    router = MockRouter()
    captured: dict[str, object] = {}

    def handler(req: httpx.Request):
        assert str(req.url) == "https://anthropic-resp-file.example/v1/messages"
        payload = _json_request(req)
        captured["payload"] = payload
        return _anthropic_response("responses file pong")

    router.register("https://anthropic-resp-file.example", handler)
    _install_channels(m, [
        _make_anthropic_channel(m, "anth-resp-file", "https://anthropic-resp-file.example", alias="gpt-5", real="claude-real"),
    ])

    body = {
        "model": "gpt-5",
        "stream": False,
        "max_output_tokens": 32,
        "input": [{"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "read"},
            {"type": "input_file", "filename": "brief.pdf", "file_data": "data:application/pdf;base64,JVBERi0xLjQ="},
            {"type": "input_file", "filename": "remote.pdf", "file_url": "https://example.com/remote.pdf"},
        ]}],
    }
    resp, mc = await _call_openai_handler(m, router, "responses", body)
    await mc.aclose()

    assert resp.status_code == 200
    payload = captured["payload"]
    content = payload["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "read"}
    assert {k: content[1][k] for k in ("type", "source", "title")} == {
        "type": "document",
        "source": {"type": "base64", "media_type": "application/pdf", "data": "JVBERi0xLjQ="},
        "title": "brief.pdf",
    }
    assert {k: content[2][k] for k in ("type", "source", "title")} == {
        "type": "document",
        "source": {"type": "url", "url": "https://example.com/remote.pdf"},
        "title": "remote.pdf",
    }


async def test_responses_client_explicit_prompt_cache_key_to_anthropic_is_stripped(m):
    _setup(m)
    _install_keys(m, _default_key())
    router = MockRouter()
    captured: dict[str, object] = {}

    def handler(req: httpx.Request):
        payload = _json_request(req)
        captured["payload"] = payload
        return _anthropic_response("responses prompt cache stripped")

    router.register("https://anthropic.example", handler)
    _install_channels(m, [
        _make_anthropic_channel(m, "anth", "https://anthropic.example", alias="gpt-5", real="claude-real"),
    ])

    body = {
        "model": "gpt-5",
        "stream": False,
        "input": "ping",
        "prompt_cache_key": "user-cache-key",
    }
    resp, mc = await _call_openai_handler(m, router, "responses", body)
    await mc.aclose()

    assert resp.status_code == 200
    assert "prompt_cache_key" not in captured["payload"]
    out = json.loads(resp.body)
    assert out["output_text"] == "responses prompt cache stripped"


async def test_responses_client_allowed_tools_to_anthropic_filters_upstream_tools(m):
    _setup(m)
    _install_keys(m, _default_key())
    router = MockRouter()
    captured: dict[str, object] = {}

    def handler(req: httpx.Request):
        assert str(req.url) == "https://anthropic-allowed.example/v1/messages"
        payload = _json_request(req)
        captured["payload"] = payload
        return _anthropic_response("responses allowed tools pong")

    router.register("https://anthropic-allowed.example", handler)
    _install_channels(m, [
        _make_anthropic_channel(m, "anth-allowed", "https://anthropic-allowed.example", alias="gpt-5", real="claude-real"),
    ])

    body = {
        "model": "gpt-5",
        "stream": False,
        "max_output_tokens": 32,
        "input": "ping",
        "tools": [
            {"type": "function", "name": "lookup", "parameters": {"type": "object"}},
            {"type": "function", "name": "search", "parameters": {"type": "object"}},
        ],
        "tool_choice": {
            "type": "allowed_tools",
            "mode": "required",
            "tools": [{"type": "function", "name": "search"}],
        },
        "parallel_tool_calls": False,
    }
    resp, mc = await _call_openai_handler(m, router, "responses", body)
    await mc.aclose()

    assert resp.status_code == 200
    out = json.loads(resp.body)
    assert out["output_text"] == "responses allowed tools pong"
    payload = captured["payload"]
    assert payload["model"] == "claude-real"
    assert {k: payload["messages"][0]["content"][0][k] for k in ("type", "text")} == {"type": "text", "text": "ping"}
    assert len(payload["tools"]) == 1
    assert {k: payload["tools"][0][k] for k in ("name", "input_schema")} == {
        "name": "search",
        "input_schema": {"type": "object"},
    }
    assert payload["tool_choice"] == {"type": "any", "disable_parallel_tool_use": True}


async def test_chat_client_multiturn_tool_result_to_anthropic_fake_upstream(m):
    _setup(m)
    _install_keys(m, _default_key())
    router = MockRouter()

    def handler(req: httpx.Request):
        assert str(req.url) == "https://anthropic-history.example/v1/messages"
        payload = _json_request(req)
        assert payload["model"] == "claude-real"
        msgs = payload["messages"]
        assert [msg["role"] for msg in msgs] == ["user", "assistant", "user"]
        assert msgs[0]["content"][0] == {"type": "text", "text": "hi"}
        assert msgs[1]["content"] == [
            {"type": "text", "text": "need tool"},
            {"type": "tool_use", "id": "call_1", "name": "lookup", "input": {"q": "x"}},
        ]
        assert {k: msgs[2]["content"][0][k] for k in ("type", "tool_use_id", "content")} == {
            "type": "tool_result", "tool_use_id": "call_1", "content": "result x",
        }
        assert {k: msgs[2]["content"][1][k] for k in ("type", "text")} == {"type": "text", "text": "continue"}
        return _anthropic_response("chat history client pong")

    router.register("https://anthropic-history.example", handler)
    _install_channels(m, [
        _make_anthropic_channel(m, "anth-history", "https://anthropic-history.example", alias="gpt-5", real="claude-real"),
    ])

    body = {
        "model": "gpt-5", "stream": False, "max_tokens": 32,
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "need tool", "tool_calls": [{
                "id": "call_1", "type": "function", "function": {"name": "lookup", "arguments": '{"q":"x"}'},
            }]},
            {"role": "tool", "tool_call_id": "call_1", "content": "result x"},
            {"role": "user", "content": "continue"},
        ],
        "tools": [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}],
    }
    resp, mc = await _call_openai_handler(m, router, "chat", body)
    await mc.aclose()

    assert resp.status_code == 200
    out = json.loads(resp.body)
    assert out["choices"][0]["message"]["content"] == "chat history client pong"


async def test_chat_client_safe_custom_tool_history_to_anthropic_fake_upstream(m):
    _setup(m)
    _install_keys(m, _default_key())
    router = MockRouter()

    def handler(req: httpx.Request):
        assert str(req.url) == "https://anthropic-chat-custom-history.example/v1/messages"
        payload = _json_request(req)
        assert payload["model"] == "claude-real"
        msgs = payload["messages"]
        assert [msg["role"] for msg in msgs] == ["assistant", "user"]
        assert msgs[0]["content"] == [
            {"type": "tool_use", "id": "call_1", "name": "shell", "input": {"cmd": "pwd"}},
        ]
        assert {k: msgs[1]["content"][0][k] for k in ("type", "tool_use_id", "content")} == {
            "type": "tool_result", "tool_use_id": "call_1", "content": "ok",
        }
        assert {k: msgs[1]["content"][1][k] for k in ("type", "text")} == {"type": "text", "text": "continue"}
        return _anthropic_response("chat custom history pong")

    router.register("https://anthropic-chat-custom-history.example", handler)
    _install_channels(m, [
        _make_anthropic_channel(
            m,
            "anth-chat-custom-history",
            "https://anthropic-chat-custom-history.example",
            alias="gpt-5",
            real="claude-real",
        ),
    ])

    body = {
        "model": "gpt-5", "stream": False, "max_tokens": 32,
        "messages": [
            {"role": "assistant", "tool_calls": [{
                "id": "call_1", "type": "custom", "custom": {"name": "shell", "input": {"cmd": "pwd"}},
            }]},
            {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
            {"role": "user", "content": "continue"},
        ],
        "tools": [{"type": "custom", "custom": {"name": "shell"}}],
    }
    resp, mc = await _call_openai_handler(m, router, "chat", body)
    await mc.aclose()

    assert resp.status_code == 200
    out = json.loads(resp.body)
    assert out["choices"][0]["message"]["content"] == "chat custom history pong"


async def test_responses_client_multiturn_tool_result_to_anthropic_fake_upstream(m):
    _setup(m)
    _install_keys(m, _default_key())
    router = MockRouter()

    def handler(req: httpx.Request):
        assert str(req.url) == "https://anthropic-history.example/v1/messages"
        payload = _json_request(req)
        assert payload["model"] == "claude-real"
        msgs = payload["messages"]
        assert [msg["role"] for msg in msgs] == ["user", "assistant", "user"]
        assert msgs[0]["content"][0] == {"type": "text", "text": "hi"}
        assert msgs[1]["content"] == [{"type": "tool_use", "id": "call_1", "name": "lookup", "input": {"q": "x"}}]
        assert {k: msgs[2]["content"][0][k] for k in ("type", "tool_use_id", "content")} == {
            "type": "tool_result", "tool_use_id": "call_1", "content": "result x",
        }
        assert {k: msgs[2]["content"][1][k] for k in ("type", "text")} == {"type": "text", "text": "continue"}
        return _anthropic_response("responses history client pong")

    router.register("https://anthropic-history.example", handler)
    _install_channels(m, [
        _make_anthropic_channel(m, "anth-history", "https://anthropic-history.example", alias="gpt-5", real="claude-real"),
    ])

    body = {
        "model": "gpt-5", "stream": False, "max_output_tokens": 32,
        "input": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            {"type": "function_call", "call_id": "call_1", "name": "lookup", "arguments": '{"q":"x"}'},
            {"type": "function_call_output", "call_id": "call_1", "output": "result x"},
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "continue"}]},
        ],
        "tools": [{"type": "function", "name": "lookup", "parameters": {"type": "object"}}],
    }
    resp, mc = await _call_openai_handler(m, router, "responses", body)
    await mc.aclose()

    assert resp.status_code == 200
    out = json.loads(resp.body)
    assert out["output_text"] == "responses history client pong"


async def test_responses_client_tool_result_attachments_to_anthropic_fake_upstream(m):
    _setup(m)
    _install_keys(m, _default_key())
    router = MockRouter()
    captured: dict[str, object] = {}

    def handler(req: httpx.Request):
        assert str(req.url) == "https://anthropic-tool-attachments.example/v1/messages"
        payload = _json_request(req)
        captured["payload"] = payload
        return _anthropic_response("responses tool attachments pong")

    router.register("https://anthropic-tool-attachments.example", handler)
    _install_channels(m, [
        _make_anthropic_channel(
            m,
            "anth-tool-attachments",
            "https://anthropic-tool-attachments.example",
            alias="gpt-5",
            real="claude-real",
        ),
    ])

    body = {
        "model": "gpt-5",
        "stream": False,
        "max_output_tokens": 32,
        "input": [
            {"type": "function_call", "call_id": "call_1", "name": "inspect", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call_1", "output": [
                {"type": "input_text", "text": "see attached"},
                {"type": "input_image", "image_url": "data:image/png;base64,AAAA", "detail": "high"},
                {"type": "input_file", "filename": "brief.pdf", "file_data": "data:application/pdf;base64,JVBERi0xLjQ="},
                {"type": "input_file", "filename": "remote.pdf", "file_url": "https://example.com/remote.pdf"},
            ]},
        ],
        "tools": [{"type": "function", "name": "inspect", "parameters": {"type": "object"}}],
    }
    resp, mc = await _call_openai_handler(m, router, "responses", body)
    await mc.aclose()

    assert resp.status_code == 200
    out = json.loads(resp.body)
    assert out["output_text"] == "responses tool attachments pong"
    payload = captured["payload"]
    assert payload["model"] == "claude-real"
    msgs = payload["messages"]
    assert [msg["role"] for msg in msgs] == ["assistant", "user"]
    assert msgs[0]["content"] == [{"type": "tool_use", "id": "call_1", "name": "inspect", "input": {}}]
    result = msgs[1]["content"][0]
    assert result["type"] == "tool_result"
    assert result["tool_use_id"] == "call_1"
    assert result["content"] == [
        {"type": "text", "text": "see attached"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}},
        {
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": "JVBERi0xLjQ="},
            "title": "brief.pdf",
        },
        {
            "type": "document",
            "source": {"type": "url", "url": "https://example.com/remote.pdf"},
            "title": "remote.pdf",
        },
    ]


async def test_responses_client_safe_custom_tool_history_to_anthropic_fake_upstream(m):
    _setup(m)
    _install_keys(m, _default_key())
    router = MockRouter()
    captured: dict[str, object] = {}

    def handler(req: httpx.Request):
        assert str(req.url) == "https://anthropic-custom-history.example/v1/messages"
        payload = _json_request(req)
        captured["payload"] = payload
        return _anthropic_response("custom history pong")

    router.register("https://anthropic-custom-history.example", handler)
    _install_channels(m, [
        _make_anthropic_channel(
            m,
            "anth-custom-history",
            "https://anthropic-custom-history.example",
            alias="gpt-5",
            real="claude-real",
        ),
    ])

    body = {
        "model": "gpt-5",
        "stream": False,
        "max_output_tokens": 32,
        "input": [
            {"type": "custom_tool_call", "call_id": "call_1", "name": "shell", "input": {"cmd": "pwd"}},
            {"type": "custom_tool_call_output", "call_id": "call_1", "output": "ok"},
        ],
    }
    resp, mc = await _call_openai_handler(m, router, "responses", body)
    await mc.aclose()

    assert resp.status_code == 200
    assert json.loads(resp.body)["output_text"] == "custom history pong"
    payload = captured["payload"]
    assert "tools" not in payload
    assert payload["cache_control"] == {"type": "ephemeral"}
    assert payload["messages"] == [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "call_1", "name": "shell", "input": {"cmd": "pwd"}},
        ]},
        {"role": "user", "content": [{
            "type": "tool_result",
            "tool_use_id": "call_1",
            "content": "ok",
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
        }]},
    ]


async def test_responses_client_previous_response_tool_result_attachments_to_anthropic_fake_upstream(m):
    _setup(m)
    _install_keys(m, _default_key())
    from src.openai import store as openai_store

    openai_store.init()
    openai_store._reset_for_test()
    openai_store.save(
        "resp_call",
        None,
        api_key_name="k",
        model="gpt-5",
        channel_key="api:anth-history-attachments",
        input_items=[],
        output_items=[{"type": "function_call", "call_id": "call_1", "name": "inspect", "arguments": "{}"}],
    )
    openai_store.save(
        "resp_tool",
        "resp_call",
        api_key_name="k",
        model="gpt-5",
        channel_key="api:anth-history-attachments",
        input_items=[{"type": "function_call_output", "call_id": "call_1", "output": [
            {"type": "input_text", "text": "from stored history"},
            {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
            {"type": "input_file", "filename": "brief.pdf", "file_data": "data:application/pdf;base64,JVBERi0xLjQ="},
            {"type": "input_file", "filename": "remote.pdf", "file_url": "https://example.com/remote.pdf"},
        ]}],
        output_items=[],
    )

    router = MockRouter()
    captured: dict[str, object] = {}

    def handler(req: httpx.Request):
        assert str(req.url) == "https://anthropic-history-attachments.example/v1/messages"
        payload = _json_request(req)
        captured["payload"] = payload
        return _anthropic_response("responses stored tool attachments pong")

    router.register("https://anthropic-history-attachments.example", handler)
    _install_channels(m, [
        _make_anthropic_channel(
            m,
            "anth-history-attachments",
            "https://anthropic-history-attachments.example",
            alias="gpt-5",
            real="claude-real",
        ),
    ])

    body = {
        "model": "gpt-5",
        "stream": False,
        "previous_response_id": "resp_tool",
        "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "continue"}]}],
        "tools": [{"type": "function", "name": "inspect", "parameters": {"type": "object"}}],
    }
    resp, mc = await _call_openai_handler(m, router, "responses", body)
    await mc.aclose()

    assert resp.status_code == 200
    assert json.loads(resp.body)["output_text"] == "responses stored tool attachments pong"
    msgs = captured["payload"]["messages"]
    assert [msg["role"] for msg in msgs] == ["assistant", "user"]
    assert msgs[0]["content"] == [{"type": "tool_use", "id": "call_1", "name": "inspect", "input": {}}]
    assert msgs[1]["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "call_1",
        "content": [
            {"type": "text", "text": "from stored history"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}},
            {
                "type": "document",
                "source": {"type": "base64", "media_type": "application/pdf", "data": "JVBERi0xLjQ="},
                "title": "brief.pdf",
            },
            {
                "type": "document",
                "source": {"type": "url", "url": "https://example.com/remote.pdf"},
                "title": "remote.pdf",
            },
        ],
    }
    assert {k: msgs[1]["content"][1][k] for k in ("type", "text")} == {"type": "text", "text": "continue"}


async def test_chat_client_to_anthropic_stream_fake_upstream(m):
    _setup(m)
    _install_keys(m, _default_key())
    router = MockRouter()

    def handler(req: httpx.Request):
        assert str(req.url) == "https://anthropic-sse.example/v1/messages"
        payload = _json_request(req)
        assert payload["model"] == "claude-real"
        assert payload["stream"] is True
        assert payload["messages"][0]["content"][0]["text"] == "ping"
        return _anthropic_sse_response("chat stream via anthropic")

    router.register("https://anthropic-sse.example", handler)
    _install_channels(m, [
        _make_anthropic_channel(m, "anth-sse", "https://anthropic-sse.example", alias="gpt-5", real="claude-real"),
    ])

    body = {
        "model": "gpt-5",
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_tokens": 32,
        "messages": [{"role": "user", "content": "ping"}],
    }
    resp, mc = await _call_openai_handler(m, router, "chat", body)
    text = await _consume_streaming_to_string(resp)
    await mc.aclose()

    assert resp.status_code == 200
    assert "data: [DONE]" in text
    assert '"object":"chat.completion.chunk"' in text
    assert '"content":"chat stream via anthropic"' in text
    assert '"finish_reason":"stop"' in text
    assert '"prompt_tokens":9' in text
    assert '"cached_tokens":2' in text


async def test_responses_client_to_anthropic_stream_fake_upstream(m):
    _setup(m)
    _install_keys(m, _default_key())
    router = MockRouter()

    def handler(req: httpx.Request):
        assert str(req.url) == "https://anthropic-sse.example/v1/messages"
        payload = _json_request(req)
        assert payload["model"] == "claude-real"
        assert payload["stream"] is True
        assert payload["messages"][0]["content"][0]["text"] == "ping"
        return _anthropic_sse_response("responses stream via anthropic")

    router.register("https://anthropic-sse.example", handler)
    _install_channels(m, [
        _make_anthropic_channel(m, "anth-sse", "https://anthropic-sse.example", alias="gpt-5", real="claude-real"),
    ])

    body = {"model": "gpt-5", "stream": True, "max_output_tokens": 32, "input": "ping"}
    resp, mc = await _call_openai_handler(m, router, "responses", body)
    text = await _consume_streaming_to_string(resp)
    await mc.aclose()

    assert resp.status_code == 200
    assert "event: response.created" in text
    assert "event: response.output_text.delta" in text
    assert '"delta":"responses stream via anthropic"' in text
    assert "event: response.completed" in text
    assert '"input_tokens":9' in text
    assert '"cached_tokens":2' in text


async def test_responses_ws_client_to_http_sse_fake_upstream(m):
    _setup(m)
    _install_keys(m, {
        "ws-key": {
            "key": "sk-ws",
            "allowedProtocols": ["responses"],
            "allowedModels": ["test-model"],
        }
    })
    from src.openai import store as openai_store

    openai_store.init()
    openai_store._reset_for_test()
    m["config"].update(lambda cfg: cfg.update({
        "network": {"routing": {"default": "direct"}},
        "timeouts": {"connect": 5, "firstByte": 5, "idle": 10, "total": 30},
        "concurrency": {"queueWaitSeconds": 1},
        "oauthAccounts": [],
    }))
    router = MockRouter()

    def handler(req: httpx.Request):
        assert str(req.url) == "https://responses-sse.example/v1/responses"
        payload = _json_request(req)
        assert payload["model"] == "real-model"
        assert payload["stream"] is True
        assert payload["input"] == "ping"
        return _responses_sse_response("ws bridge pong")

    router.register("https://responses-sse.example", handler)
    transport = httpx.MockTransport(router.handle)
    mock_client = httpx.AsyncClient(transport=transport, timeout=10.0)
    m["upstream"].set_client(mock_client)

    _install_channels(m, [
        _make_openai_channel(
            "responses-sse", "https://responses-sse.example",
            protocol="openai-responses", alias="test-model", real="real-model",
            extra={"responsesWsUpstreamTransport": "sse"},
        ),
    ])

    ws = FakeWebSocket({
        "type": "response.create",
        "model": "test-model",
        "input": "ping",
        "stream": True,
    })
    await m["responses_ws"].handle_responses_ws(ws)  # type: ignore[arg-type]
    await mock_client.aclose()

    sent = [json.loads(t) for t in ws.sent_texts]
    assert [e["type"] for e in sent] == [
        "response.created",
        "response.output_item.added",
        "response.output_text.delta",
        "response.completed",
    ]
    assert sent[2]["delta"] == "ws bridge pong"
    assert ws.close_calls[-1][0] == 1000

    conn = m["log_db"]._get_conn()
    row = conn.execute("SELECT upstream_transport, input_tokens, cache_read_tokens, output_tokens FROM request_log ORDER BY id DESC LIMIT 1").fetchone()
    assert dict(row) == {
        "upstream_transport": "sse",
        "input_tokens": 5,
        "cache_read_tokens": 2,
        "output_tokens": 4,
    }
    rec = openai_store.lookup("resp_sse", api_key_name="ws-key")
    assert rec.channel_key == "api:responses-sse"
    assert rec.input_items == [{"type": "message", "role": "user", "content": [
        {"type": "input_text", "text": "ping"},
    ]}]
    assert rec.output_items[0]["content"][0]["text"] == "ws bridge pong"


async def test_http_responses_client_to_ws_fake_upstream(monkeypatch, m):
    _setup(m)
    _install_keys(m, _default_key())
    m["config"].update(lambda cfg: cfg.update({
        "network": {"routing": {"default": "direct"}},
        "timeouts": {"connect": 5, "firstByte": 5, "idle": 10, "total": 30},
        "concurrency": {"queueWaitSeconds": 1},
        "openai": {"responsesUpstreamWsForOAuth": True},
        "oauth": {"providers": {"openai": {"isolateSessionId": True, "forceCodexCLI": True}}},
        # This test installs a fake in-memory OAuth channel directly in the
        # registry; the persisted/configured OAuth account list must remain off.
        "oauthAccounts": [],
    }))
    _install_channels(m, [_make_openai_oauth_channel("fake-http-ws@example.com")])

    async def fake_token(account_key):
        assert account_key.startswith("openai:fake-http-ws@example.com")
        return "tok"

    fake_ws = FakeOAuthResponseWs([
        {"type": "response.created", "response": {"id": "resp_http_ws"}},
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"type": "message", "id": "msg_http_ws", "role": "assistant", "content": []},
        },
        {
            "type": "response.output_text.delta",
            "output_index": 0,
            "content_index": 0,
            "item_id": "msg_http_ws",
            "delta": "http responses over ws pong",
        },
        {"type": "response.completed", "response": {
            "id": "resp_http_ws",
            "status": "completed",
            "output": [{
                "type": "message", "id": "msg_http_ws", "role": "assistant",
                "content": [{"type": "output_text", "text": "http responses over ws pong"}],
            }],
            "usage": {
                "input_tokens": 9,
                "output_tokens": 5,
                "total_tokens": 14,
                "input_tokens_details": {"cached_tokens": 3},
            },
        }},
    ])
    captured: dict[str, object] = {}

    async def fake_connect(url, *, headers, connector, proxy_bytes, open_timeout, timing=None, round_timeouts=None):
        captured["url"] = url
        captured["headers"] = dict(headers)
        captured["connector"] = connector
        captured["open_timeout"] = open_timeout
        return fake_ws

    monkeypatch.setattr(m["failover"].oauth_manager, "ensure_valid_token", fake_token)
    monkeypatch.setattr(m["failover"], "_connect_oauth_responses_ws", fake_connect)

    router = MockRouter()
    body = {"model": "gpt-5", "stream": False, "input": "ping", "prompt_cache_key": "anchor"}
    resp, mc = await _call_openai_handler(m, router, "responses", body)
    await mc.aclose()

    assert resp.status_code == 200
    out = json.loads(resp.body)
    assert out["object"] == "response"
    assert out["output"][0]["content"][0]["text"] == "http responses over ws pong"
    assert out["usage"]["input_tokens"] == 9
    assert out["usage"]["input_tokens_details"]["cached_tokens"] == 3
    assert captured["url"] == "wss://chatgpt.com/backend-api/codex/responses"
    assert captured["headers"]["OpenAI-Beta"] == "responses_websockets=2026-02-06"
    assert captured["connector"] is None

    sent = json.loads(fake_ws.sent[0])
    assert sent["type"] == "response.create"
    assert sent["model"] == "gpt-5"
    assert sent["store"] is False
    assert sent["stream"] is True
    assert sent["input"] == [{"type": "message", "role": "user", "content": "ping"}]
    assert fake_ws.closed is True

    conn = m["log_db"]._get_conn()
    row = conn.execute(
        "SELECT status, upstream_protocol, upstream_transport, http_status, "
        "input_tokens, cache_read_tokens, output_tokens "
        "FROM request_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert dict(row) == {
        "status": "success",
        "upstream_protocol": "openai-responses",
        "upstream_transport": "ws",
        "http_status": 200,
        "input_tokens": 6,
        "cache_read_tokens": 3,
        "output_tokens": 5,
    }
