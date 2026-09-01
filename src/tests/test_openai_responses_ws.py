"""Responses WebSocket ingress tests.

Covers the Codex/OpenAI Responses WebSocket transport added on /v1/responses.
"""

from __future__ import annotations

import os as _ap_os, sys as _ap_sys
_ap_sys.path.insert(0, _ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.abspath(__file__)))))
from src.tests import _isolation
_isolation.isolate()

import asyncio
import base64
import json
import os
import socket
import sys
import time
from types import SimpleNamespace
from typing import Any

import pytest


def _valid_encrypted_content(seed: int = 1) -> str:
    payload = bytearray(1 + 8 + 16 + 16 + 32)
    payload[0] = 0x80
    for i in range(9, len(payload)):
        payload[i] = (seed + i) % 256
    return base64.urlsafe_b64encode(bytes(payload)).decode("ascii").rstrip("=")


def _import_modules():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if root not in sys.path:
        sys.path.insert(0, root)
    from src import (
        affinity, apikey_limiter, concurrency, config, cooldown, failover,
        fingerprint, log_db, scorer, state_db, upstream,
    )
    from src.channel import registry
    from src.openai.channel.registration import register_factories
    from src.openai.channel.api_channel import OpenAIApiChannel
    from src.channel.openai_oauth_channel import OpenAIOAuthChannel
    from src.openai import responses_ws, reasoning_replay
    register_factories()
    return {
        "affinity": affinity,
        "apikey_limiter": apikey_limiter,
        "concurrency": concurrency,
        "config": config,
        "cooldown": cooldown,
        "failover": failover,
        "fingerprint": fingerprint,
        "log_db": log_db,
        "scorer": scorer,
        "state_db": state_db,
        "upstream": upstream,
        "registry": registry,
        "OpenAIApiChannel": OpenAIApiChannel,
        "OpenAIOAuthChannel": OpenAIOAuthChannel,
        "responses_ws": responses_ws,
        "reasoning_replay": reasoning_replay,
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
    m["reasoning_replay"].clear()
    cfg = {
        "apiKeys": {
            "ws-key": {
                "key": "sk-ws",
                "allowedProtocols": ["responses"],
                "allowedModels": ["test-model"],
            }
        },
        "channels": [],
        "oauthAccounts": [],
        "network": {"routing": {"default": "direct"}},
        "timeouts": {"connect": 5, "firstByte": 5, "idle": 10, "total": 30},
        "concurrency": {"queueWaitSeconds": 1},
    }
    m["config"]._cache = cfg
    m["config"]._mtime = m["config"]._current_mtime()
    return cfg


class FakeHeaders:
    def __init__(self, data: dict[str, str]):
        self._d = {k.lower(): v for k, v in data.items()}

    def get(self, key: str, default=None):
        return self._d.get(key.lower(), default)

    def items(self):
        return self._d.items()

    def keys(self):
        return self._d.keys()

    def __iter__(self):
        return iter(self._d.keys())

    def __len__(self):
        return len(self._d)

    def __getitem__(self, key: str):
        return self._d[key.lower()]


class FakeWebSocket:
    def __init__(self, first_obj: dict[str, Any], *, extra_headers: dict[str, str] | None = None, extra_receive: list[dict[str, Any]] | None = None):
        h = {
            "Authorization": "Bearer sk-ws",
            "user-agent": "codex_cli_rs/0.125.0",
            "x-codex-turn-metadata": "turn-meta",
        }
        if extra_headers:
            h.update(extra_headers)
        self.headers = FakeHeaders(h)
        self.client = SimpleNamespace(host="1.2.3.4")
        self.application_state = None
        self._first_text = json.dumps(first_obj)
        self._extra_receive = list(extra_receive or [])
        self.sent_texts: list[str] = []
        self.close_calls: list[tuple[int, str]] = []
        self.accepted = False
        self._closed = asyncio.Event()
        self._relay_wait_cancelled = False
        self._terminal_seen = False

    async def accept(self):
        self.accepted = True
        # Starlette uses WebSocketState.CONNECTED; the handler only compares equality.
        from starlette.websockets import WebSocketState
        self.application_state = WebSocketState.CONNECTED

    async def receive(self):
        if self._first_text is not None:
            text = self._first_text
            self._first_text = None
            return {"type": "websocket.receive", "text": text}
        if self._extra_receive:
            return self._extra_receive.pop(0)
        if self._terminal_seen:
            self._terminal_seen = False
            return {"type": "websocket.disconnect", "code": 1000}
        if self._relay_wait_cancelled:
            self._relay_wait_cancelled = False
            return {"type": "websocket.disconnect", "code": 1000}
        # A real connected client blocks here; an immediate synthetic disconnect
        # races the owned per-frame timing task and can cancel a healthy upstream.
        try:
            await self._closed.wait()
        except asyncio.CancelledError:
            if self._terminal_seen:
                self._relay_wait_cancelled = True
            raise
        return {"type": "websocket.disconnect", "code": 1000}

    async def send_text(self, text: str):
        self.sent_texts.append(text)
        try:
            event_type = json.loads(text).get("type")
        except Exception:
            event_type = None
        if event_type in {
            "response.completed", "response.failed", "response.incomplete",
        }:
            self._terminal_seen = True

    async def send_bytes(self, data: bytes):
        self.sent_texts.append(data.decode("utf-8"))

    async def close(self, code: int = 1000, reason: str = ""):
        self.close_calls.append((code, reason))
        self._closed.set()
        from starlette.websockets import WebSocketState
        self.application_state = WebSocketState.DISCONNECTED


class SequentialFakeWebSocket(FakeWebSocket):
    """Send the next create only after the active receive task is cancelled."""

    def __init__(self, first_obj: dict[str, Any], *next_objs: dict[str, Any]):
        super().__init__(first_obj)
        self._next_texts = [json.dumps(obj) for obj in next_objs]

    async def send_text(self, text: str):
        await super().send_text(text)
        try:
            event_type = json.loads(text).get("type")
        except Exception:
            event_type = None
        if event_type in {
            "response.completed", "response.failed", "response.incomplete",
            "error",
        }:
            self._terminal_seen = True

    async def receive(self):
        if (
            self._first_text is None
            and self._next_texts
            and (self._relay_wait_cancelled or self._terminal_seen)
        ):
            self._relay_wait_cancelled = False
            self._terminal_seen = False
            text = self._next_texts.pop(0)
            return {"type": "websocket.receive", "text": text}
        if (
            self._first_text is None
            and not self._next_texts
            and self._terminal_seen
        ):
            self._terminal_seen = False
            return {"type": "websocket.disconnect", "code": 1000}
        return await super().receive()


class IdleBetweenTurnsFakeWebSocket(FakeWebSocket):
    """Keep the client socket open after one terminal response for inspection."""

    def __init__(self, first_obj: dict[str, Any]):
        super().__init__(first_obj)
        self.waiting_between_turns = asyncio.Event()
        self.disconnect_between_turns = asyncio.Event()

    async def receive(self):
        if self._first_text is not None:
            return await super().receive()
        if self._relay_wait_cancelled or self._terminal_seen:
            self._relay_wait_cancelled = False
            self._terminal_seen = False
            self.waiting_between_turns.set()
            await self.disconnect_between_turns.wait()
            return {"type": "websocket.disconnect", "code": 1000}
        return await super().receive()


class FakeUpstreamWebSocket:
    def __init__(self, events: list[dict[str, Any]]):
        self.sent: list[str] = []
        self._events = [json.dumps(e) for e in events]
        self.response = SimpleNamespace(headers={})

    async def send(self, data: str | bytes, text: bool | None = None):
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        self.sent.append(data)
        await asyncio.sleep(0)

    async def recv(self):
        if self._events:
            return self._events.pop(0)
        import websockets
        raise websockets.ConnectionClosed(None, None)

    async def close(self, *args, **kwargs):
        return None


class DisconnectBeforeVisibleWebSocket(FakeWebSocket):
    """Disconnect immediately after the initial response.create is consumed."""

    async def receive(self):
        if self._first_text is not None:
            return await super().receive()
        return {"type": "websocket.disconnect", "code": 1000}


class ClosingAfterEventsWebSocket(FakeUpstreamWebSocket):
    def __init__(self, events: list[dict[str, Any]], close_code: int):
        super().__init__(events)
        self.close_code = close_code
        self.recv_calls = 0

    async def recv(self):
        self.recv_calls += 1
        if self._events:
            return self._events.pop(0)
        from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK
        from websockets.frames import Close
        exc_type = ConnectionClosedOK if self.close_code in (1000, 1001) else ConnectionClosedError
        raise exc_type(Close(self.close_code, "upstream ended"), None)


class BlockingAfterEventsWebSocket(FakeUpstreamWebSocket):
    """Deliver finite frames, then signal that the consumer is blocked."""

    def __init__(self, events: list[dict[str, Any]]):
        super().__init__(events)
        self.waiting = asyncio.Event()
        self._release = asyncio.Event()
        self.closed = False

    async def recv(self):
        if self._events:
            return self._events.pop(0)
        self.waiting.set()
        await self._release.wait()
        import websockets
        raise websockets.ConnectionClosed(None, None)

    async def close(self, *args, **kwargs):
        self.closed = True
        self._release.set()




def _last_request_log(m):
    conn = m["log_db"]._get_conn()
    row = conn.execute("SELECT * FROM request_log ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def _response_sse_event(event_type: str, payload: dict[str, Any]) -> bytes:
    return (
        f"event: {event_type}\n"
        f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"
    ).encode("utf-8")


class FakeOAuthHttpWs:
    def __init__(self, events: list[dict[str, Any]]):
        self.sent: list[str] = []
        self._events = [json.dumps(e) for e in events]
        self.response = SimpleNamespace(headers={})
        self.closed = False

    async def send(self, data: str | bytes, text: bool | None = None):
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        self.sent.append(data)
        await asyncio.sleep(0)

    async def recv(self):
        if self._events:
            return self._events.pop(0)
        import websockets
        raise websockets.ConnectionClosed(None, None)

    async def close(self, *args, **kwargs):
        self.closed = True
        return None


class FakeOAuthHttpStreamWs:
    def __init__(self, events: list[dict[str, Any]]):
        self.sent: list[str] = []
        self._events = [json.dumps(e) for e in events]
        self.response = SimpleNamespace(headers={})
        self.closed = False

    async def send(self, data: str | bytes, text: bool | None = None):
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        self.sent.append(data)
        await asyncio.sleep(0)

    async def recv(self):
        if self._events:
            return self._events.pop(0)
        import websockets
        raise websockets.ConnectionClosed(None, None)

    async def close(self, *args, **kwargs):
        self.closed = True
        return None


class ClockedOAuthHttpStreamWs(FakeOAuthHttpStreamWs):
    def __init__(self, events: list[dict[str, Any]], delays: list[float], clock):
        super().__init__(events)
        self._delays = list(delays)
        self._clock = clock

    async def recv(self):
        if self._delays:
            self._clock.advance(self._delays.pop(0))
        return await super().recv()


def _make_oauth_channel_for_failover(m, *, name="oauth@example.com"):
    account = {
        "email": name, "provider": "openai",
        "access_token": "tok", "refresh_token": "rt",
        "expired": "2999-01-01T00:00:00Z",
        "models": ["test-model"],
    }
    m["config"]._cache.setdefault("oauthAccounts", [])[:] = [dict(account)]
    ch = m["OpenAIOAuthChannel"](account)
    with m["registry"]._lock:
        m["registry"]._channels = {ch.key: ch}
    return ch


async def _call_failover_responses(
    m, ch, body: dict[str, Any], *, fp_query=None, bound_channel_key=None,
):
    from src.scheduler import ScheduleResult
    request_id = f"http-ws-{len(body)}-{int(time.time()*1000000)}"
    start = time.time()
    await asyncio.to_thread(
        m["log_db"].insert_pending,
        request_id, "1.2.3.4", "ws-key", body.get("model"), bool(body.get("stream", False)),
        1, 0, {}, body, ingress_protocol="responses",
    )
    body.setdefault("_api_key_name", "ws-key")
    sr = ScheduleResult(
        candidates=[(ch, "test-model")], saturated=[],
        affinity_hit=bool(fp_query), fp_query=fp_query, client_key="client:1",
        bound_channel_key=bound_channel_key,
    )
    resp = await m["failover"].run_failover(
        sr, body, request_id, "ws-key", "1.2.3.4",
        is_stream=bool(body.get("stream", False)), start_time=start, ingress_protocol="responses",
    )
    return resp, request_id


def _retry_chain(m, request_id: str):
    conn = m["log_db"]._get_conn()
    return [dict(r) for r in conn.execute(
        "SELECT * FROM retry_chain WHERE request_id=? ORDER BY attempt_order", (request_id,)
    ).fetchall()]


def _proxy_chain(m, request_id: str):
    conn = m["log_db"]._get_conn()
    return [dict(r) for r in conn.execute(
        "SELECT * FROM proxy_chain WHERE request_id=? ORDER BY attempt_order", (request_id,)
    ).fetchall()]


def _attempt_usage(m, request_id: str):
    conn = m["log_db"]._get_conn()
    return [dict(r) for r in conn.execute(
        "SELECT * FROM upstream_attempt_usage "
        "WHERE root_request_id=? ORDER BY attempt_order",
        (request_id,),
    ).fetchall()]

def _make_channel(m, *, extra: dict[str, Any] | None = None):
    entry = {
        "name": "ws-upstream",
        "type": "api",
        "baseUrl": "https://up.example",
        "apiKey": "up-key",
        "protocol": "openai-responses",
        "models": [{"alias": "test-model", "real": "real-model"}],
        "enabled": True,
    }
    if extra:
        entry.update(extra)
    ch = m["OpenAIApiChannel"](entry)
    with m["registry"]._lock:
        m["registry"]._channels = {ch.key: ch}
    return ch


@pytest.mark.asyncio
async def test_responses_ws_routes_maps_model_and_relays(monkeypatch, m):
    cfg = _setup(m)
    external_alias = "grok-4.5"
    logical_model = "test-model"
    cfg["modelMapping"] = {"global": {external_alias: logical_model}}
    cfg["modelBindings"] = {
        "defaults": {
            logical_model: {"target": "openai/gpt-5.4", "source": "test"},
        },
        "scoped": {},
    }
    cfg["modelMetadata"] = {}
    from src import model_pricing
    model_pricing.reset_for_tests()
    model_pricing.initialize()
    assert model_pricing.canonical_official_model(external_alias) == "xai/grok-4.5"

    _make_channel(m)
    ws = FakeWebSocket({
        "type": "response.create",
        "model": external_alias,
        "input": "hello",
        "stream": True,
        "background": False,
    })
    events = [
        {"type": "response.created", "response": {"id": "resp_1"}},
        {"type": "response.in_progress", "response": {"id": "resp_1"}},
        {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": "ok"},
        {"type": "response.completed", "response": {
            "id": "resp_1",
            "output": [],
            "usage": {"input_tokens": 3, "output_tokens": 2,
                      "input_tokens_details": {"cached_tokens": 1}},
        }},
    ]
    fake_upstream = FakeUpstreamWebSocket(events)

    async def fake_connect(url, *, headers, connector, proxy_bytes, open_timeout, timing=None, round_timeouts=None):
        assert url == "wss://up.example/v1/responses"
        assert headers["Authorization"] == "Bearer up-key"
        assert headers["OpenAI-Beta"] == "responses_websockets=2026-02-06"
        assert headers["x-codex-turn-metadata"] == "turn-meta"
        return fake_upstream

    monkeypatch.setattr(m["responses_ws"], "_connect_upstream_ws", fake_connect)
    await m["responses_ws"].handle_responses_ws(ws)  # type: ignore[arg-type]

    upstream_first = json.loads(fake_upstream.sent[0])
    assert upstream_first["type"] == "response.create"
    assert upstream_first["model"] == "real-model"
    assert "background" not in upstream_first
    assert "prompt_cache_key" in upstream_first
    assert [json.loads(t)["type"] for t in ws.sent_texts] == [
        "response.created", "response.in_progress",
        "response.output_text.delta", "response.completed"
    ]
    row = _last_request_log(m)
    chain = _retry_chain(m, row["request_id"])
    assert row["requested_model"] == logical_model
    assert len(chain) == 1 and chain[0]["dispatched_at"] is not None
    assert chain[0]["client_visible_model"] == logical_model
    assert chain[0]["model"] == "real-model"
    assert chain[0]["binding_source"] == "metadata_default"
    assert chain[0]["binding_pricing_key"] == "openai/gpt-5.4"
    assert chain[0]["binding_pricing_key"] != "xai/grok-4.5"
    frozen = json.loads(chain[0]["binding_json"])
    assert frozen["dispatch"] == {
        "channel_key": "api:ws-upstream",
        "channel_type": "api",
        "client_visible_model": logical_model,
        "outbound_model_id": "real-model",
        "upstream_protocol": "openai-responses",
    }
    assert frozen["tariff"] is not None
    assert not ws.close_calls


@pytest.mark.parametrize(("upstream_code", "downstream_code"), [
    (1000, 1000), (1001, 1001), (1006, 1011),
])
@pytest.mark.asyncio
async def test_responses_ws_visible_then_lifecycle_close_terminates_once_and_releases_capacity(
    monkeypatch, m, upstream_code, downstream_code,
):
    cfg = _setup(m)
    cfg["apiKeyConcurrency"] = {
        "enabled": True,
        "defaultMaxConcurrent": 1,
        "defaultMaxQueue": 0,
        "defaultQueueWaitSeconds": 0,
    }
    cfg["concurrency"].update({
        "enabled": True, "defaultMaxConcurrent": 1, "queueWaitSeconds": 1,
    })
    ch = _make_channel(m, extra={"maxConcurrent": 1})
    ws = FakeWebSocket({
        "type": "response.create", "model": "test-model", "input": "hello", "stream": True,
    })
    fake_upstream = ClosingAfterEventsWebSocket([{
        "type": "response.output_text.delta",
        "output_index": 0,
        "content_index": 0,
        "delta": "partial",
    }], upstream_code)

    async def fake_connect(*args, **kwargs):
        return fake_upstream

    monkeypatch.setattr(m["responses_ws"], "_connect_upstream_ws", fake_connect)
    await asyncio.wait_for(
        m["responses_ws"].handle_responses_ws(ws),  # type: ignore[arg-type]
        timeout=1,
    )

    assert fake_upstream.recv_calls == 2
    assert [json.loads(text).get("delta") for text in ws.sent_texts] == ["partial"]
    assert ws.close_calls[-1][0] == downstream_code
    row = _last_request_log(m)
    assert row["status"] == "error"
    assert _attempt_usage(m, row["request_id"])[0]["outcome"] == "connection_lifecycle"
    assert m["cooldown"].get_state(ch.key, "real-model") is None
    assert m["scorer"].get_stats(ch.key, "real-model") is None
    assert m["apikey_limiter"].key_snapshot("ws-key")["in_flight"] == 0
    channel_rows = {
        item["channel_key"]: item for item in m["concurrency"].snapshot()
    }
    assert channel_rows[ch.key]["in_flight"] == 0


@pytest.mark.asyncio
async def test_responses_ws_forced_fast_updates_wire_and_log(monkeypatch, m):
    _setup(m)
    _make_channel(m, extra={"fastMode": "force", "fastModels": []})
    ws = FakeWebSocket({
        "type": "response.create",
        "model": "test-model",
        "input": "hello",
        "stream": True,
    })
    fake_upstream = FakeUpstreamWebSocket([
        {"type": "response.created", "response": {"id": "resp_fast"}},
        {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": "ok"},
        {"type": "response.completed", "response": {
            "id": "resp_fast",
            "output": [],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }},
    ])

    async def fake_connect(url, *, headers, connector, proxy_bytes, open_timeout, timing=None, round_timeouts=None):
        return fake_upstream

    monkeypatch.setattr(m["responses_ws"], "_connect_upstream_ws", fake_connect)
    await m["responses_ws"].handle_responses_ws(ws)  # type: ignore[arg-type]

    upstream_first = json.loads(fake_upstream.sent[0])
    assert upstream_first["service_tier"] == "priority"
    row = _last_request_log(m)
    assert row["status"] == "success"
    assert row["fast_mode"] == 1
    detail = m["log_db"].log_detail(row["request_id"])["detail"]
    assert "service_tier" not in json.loads(detail["request_body"])


@pytest.mark.asyncio
async def test_responses_ws_rejects_non_response_create_first_frame(m):
    _setup(m)
    _make_channel(m)
    ws = FakeWebSocket({"type": "response.processed", "response_id": "resp_1"})
    await m["responses_ws"].handle_responses_ws(ws)  # type: ignore[arg-type]
    assert ws.close_calls[-1][0] == 4400
    assert "response.create" in ws.close_calls[-1][1]


@pytest.mark.asyncio
async def test_responses_ws_rejects_background_true(m):
    _setup(m)
    _make_channel(m)
    ws = FakeWebSocket({
        "type": "response.create",
        "model": "test-model",
        "input": "hello",
        "background": True,
    })
    await m["responses_ws"].handle_responses_ws(ws)  # type: ignore[arg-type]
    assert ws.close_calls[-1][0] == 4400
    assert "background" in ws.close_calls[-1][1]


@pytest.mark.asyncio
async def test_responses_ws_blacklist_before_first_visible_fails_over(monkeypatch, m):
    cfg = _setup(m)
    cfg["contentBlacklist"] = {"default": ["blocked-token"], "byChannel": {}}
    ch_a = m["OpenAIApiChannel"]({
        "name": "bad", "type": "api", "baseUrl": "https://bad.example",
        "apiKey": "bad-key", "protocol": "openai-responses",
        "models": [{"alias": "test-model", "real": "real-model"}], "enabled": True,
    })
    ch_b = m["OpenAIApiChannel"]({
        "name": "good", "type": "api", "baseUrl": "https://good.example",
        "apiKey": "good-key", "protocol": "openai-responses",
        "models": [{"alias": "test-model", "real": "real-model"}], "enabled": True,
    })
    with m["registry"]._lock:
        m["registry"]._channels = {ch_a.key: ch_a, ch_b.key: ch_b}

    ws = FakeWebSocket({
        "type": "response.create", "model": "test-model",
        "input": "hello", "stream": True,
    })
    # No response.created: blacklist rejection is still pre-commit and may use
    # the next candidate. A created response has a separate no-replay test.
    bad = FakeUpstreamWebSocket([
        {"type": "response.output_item.done", "output_index": 0, "item": {
            "type": "message", "content": [{"type": "output_text", "text": "blocked-token"}],
        }},
    ])
    good = FakeUpstreamWebSocket([
        {"type": "response.created", "response": {"id": "good"}},
        {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": "ok"},
        {"type": "response.completed", "response": {"id": "good", "output": [], "usage": {}}},
    ])

    async def fake_connect(url, *, headers, connector, proxy_bytes, open_timeout, timing=None, round_timeouts=None):
        if url == "wss://bad.example/v1/responses":
            return bad
        if url == "wss://good.example/v1/responses":
            return good
        raise AssertionError(url)

    monkeypatch.setattr(m["responses_ws"], "_connect_upstream_ws", fake_connect)
    await m["responses_ws"].handle_responses_ws(ws)  # type: ignore[arg-type]

    assert json.loads(bad.sent[0])["model"] == "real-model"
    assert json.loads(good.sent[0])["model"] == "real-model"
    assert [json.loads(t)["response"]["id"] for t in ws.sent_texts if json.loads(t)["type"] == "response.created"] == ["good"]
    assert any(json.loads(t).get("delta") == "ok" for t in ws.sent_texts)
    assert not ws.close_calls


@pytest.mark.asyncio
async def test_responses_ws_previsible_client_disconnect_stops_all_candidate_dispatch(
    monkeypatch, m,
):
    _setup(m)
    channels = [m["OpenAIApiChannel"]({
        "name": f"disconnect-{idx}",
        "type": "api",
        "baseUrl": f"https://disconnect-{idx}.example",
        "apiKey": "up-key",
        "protocol": "openai-responses",
        "models": [{"alias": "test-model", "real": "real-model"}],
        "enabled": True,
    }) for idx in range(2)]
    ws = DisconnectBeforeVisibleWebSocket({
        "type": "response.create", "model": "test-model", "input": "hello", "stream": True,
    })
    await ws.accept()
    request_id = "responses-ws-client-disconnect-short-circuit"
    body = {"model": "test-model", "input": "hello", "stream": True}
    started_at = time.time()
    started_monotonic = time.monotonic()
    m["log_db"].insert_pending(
        request_id, "1.2.3.4", "ws-key", "test-model", True,
        1, 0, {}, body, ingress_protocol="responses_ws",
    )
    calls = []

    async def fake_try(_websocket, **kwargs):
        calls.append(kwargs["ch"].key)
        return m["responses_ws"]._WsAttemptResult(
            connected=True,
            outcome="client_disconnected",
            error_detail="client disconnected",
        )

    from src.scheduler import ScheduleResult
    route = ScheduleResult(
        candidates=[(ch, "real-model") for ch in channels],
        saturated=[], affinity_hit=False, fp_query=None, client_key="client:1",
    )
    monkeypatch.setattr(m["responses_ws"], "_try_ws_channel", fake_try)
    await m["responses_ws"]._run_ws_failover(
        ws,
        first_obj={"type": "response.create", **body},
        schedule_result=route,
        body=body,
        request_id=request_id,
        api_key_name="ws-key",
        client_ip="1.2.3.4",
        start_time=started_at,
        start_monotonic=started_monotonic,
        fp_query=None,
    )

    assert calls == [channels[0].key]
    assert _last_request_log(m)["status"] == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize("service_tier", ["ultrafast", "hyperspeed"])
async def test_responses_ws_oauth_reuses_codex_transform_and_session_headers(
    monkeypatch, m, service_tier,
):
    cfg = _setup(m)
    cfg["openaiOAuth"] = {
        "codexCliVersion": "0.150.1",
        "forceCodexCLI": True,
        "isolateSessionId": True,
    }
    ch = m["OpenAIOAuthChannel"]({
        "email": "u@example.com",
        "provider": "openai",
        "accountKey": "openai:u@example.com",
        "accessToken": "tok",
        "refreshToken": "rt",
        "expiresAt": 9999999999,
        "models": ["test-model"],
    })
    with m["registry"]._lock:
        m["registry"]._channels = {ch.key: ch}

    async def fake_token(account_key):
        return "tok"

    monkeypatch.setattr(m["responses_ws"].oauth_manager, "ensure_valid_token", fake_token)
    ws = FakeWebSocket({
        "type": "response.create",
        "model": "test-model",
        "input": [
            {"type": "compaction", "id": "cmp_downstream_in", "encrypted_content": "downstream-in-cipher"},
            {"type": "message", "role": "user", "content": "hello"},
        ],
        "stream": True,
        "temperature": 0.9,
        "service_tier": service_tier,
        "prompt_cache_key": "shared-anchor",
        "client_metadata": {"a": "b"},
    })
    fake_upstream = FakeUpstreamWebSocket([
        {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": "ok"},
        {"type": "response.completed", "response": {"id": "resp_o", "output": [
            {"type": "compaction", "id": "cmp_downstream_out", "encrypted_content": "downstream-out-cipher"},
        ], "usage": {}}},
    ])

    captured: dict[str, Any] = {}

    async def fake_connect(url, *, headers, connector, proxy_bytes, open_timeout, timing=None, round_timeouts=None):
        assert url == "wss://chatgpt.com/backend-api/codex/responses"
        assert headers["authorization"] == "Bearer tok"
        assert headers["OpenAI-Beta"] == "responses_websockets=2026-02-06"
        assert headers["x-codex-routing-hint"] == f"model=test-model;tier={service_tier}"
        assert headers["version"] == "0.150.1"
        assert headers["User-Agent"].startswith("codex_cli_rs/0.150.1 ")
        assert headers["session-id"] == headers["thread-id"]
        # Codex CLI only uses hyphenated session-id; underscore variants must not be sent.
        assert "session_id" not in headers
        assert "conversation_id" not in headers
        captured["headers"] = dict(headers)
        return fake_upstream

    monkeypatch.setattr(m["responses_ws"], "_connect_upstream_ws", fake_connect)
    await m["responses_ws"].handle_responses_ws(ws)  # type: ignore[arg-type]

    upstream_first = json.loads(fake_upstream.sent[0])
    assert upstream_first["type"] == "response.create"
    assert upstream_first["model"] == "test-model"
    assert upstream_first["store"] is False
    assert upstream_first["stream"] is True
    assert upstream_first["service_tier"] == service_tier
    assert upstream_first["input"] == [
        {"type": "compaction", "id": "cmp_downstream_in", "encrypted_content": "downstream-in-cipher"},
        {"type": "message", "role": "user", "content": "hello"},
    ]
    assert upstream_first["client_metadata"] == {"a": "b"}
    # Frame and handshake identities share the same isolated session anchor.
    assert upstream_first["prompt_cache_key"] == captured["headers"]["session-id"]
    assert upstream_first["prompt_cache_key"] != "shared-anchor"
    assert captured["headers"]["session-id"] != "shared-anchor"
    assert "temperature" not in upstream_first
    assert not ws.close_calls
    for value in (
        {"input": [{"type": "compaction", "id": "cmp_downstream_in", "encrypted_content": "downstream-in-cipher"}]},
        {"output": [{"type": "compaction", "id": "cmp_downstream_out", "encrypted_content": "downstream-out-cipher"}]},
    ):
        ref = m["responses_ws"].compaction_owner.complete_refs(value)[0]
        assert m["state_db"].compaction_owner_load(ref.compaction_id, ref.content_digest)


@pytest.mark.asyncio
async def test_responses_ws_accepts_explicit_session_headers(monkeypatch, m):
    _setup(m)
    _make_channel(m)
    ws = FakeWebSocket({
        "type": "response.create", "model": "test-model", "input": "hello", "stream": True,
    }, extra_headers={"session_id": "sid-1", "conversation_id": "tid-1"})
    fake_upstream = FakeUpstreamWebSocket([
        {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": "ok"},
        {"type": "response.completed", "response": {"id": "resp", "output": [], "usage": {}}},
    ])

    async def fake_connect(url, *, headers, connector, proxy_bytes, open_timeout, timing=None, round_timeouts=None):
        assert headers["session-id"] == "sid-1"
        assert headers["thread-id"] == "tid-1"
        return fake_upstream

    monkeypatch.setattr(m["responses_ws"], "_connect_upstream_ws", fake_connect)
    await m["responses_ws"].handle_responses_ws(ws)  # type: ignore[arg-type]
    assert not ws.close_calls


@pytest.mark.asyncio
async def test_responses_ws_forwards_terminal_only_completed_response(monkeypatch, m):
    _setup(m)
    _make_channel(m)
    ws = FakeWebSocket({
        "type": "response.create", "model": "test-model",
        "input": "hello", "stream": True,
    })
    fake_upstream = FakeUpstreamWebSocket([
        {"type": "response.created", "response": {"id": "terminal_only"}},
        {"type": "response.completed", "response": {
            "id": "terminal_only",
            "output": [{
                "type": "message",
                "content": [{"type": "output_text", "text": "done"}],
            }],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }},
    ])

    async def fake_connect(*args, **kwargs):
        return fake_upstream

    monkeypatch.setattr(m["responses_ws"], "_connect_upstream_ws", fake_connect)
    await m["responses_ws"].handle_responses_ws(ws)  # type: ignore[arg-type]

    assert [json.loads(text)["type"] for text in ws.sent_texts] == [
        "response.created", "response.completed",
    ]
    completed = json.loads(ws.sent_texts[-1])
    assert completed["response"]["output"][0]["content"][0]["text"] == "done"
    assert not ws.close_calls
    row = _last_request_log(m)
    assert row["status"] == "success"
    assert row["input_tokens"] == row["output_tokens"] == 1


@pytest.mark.asyncio
async def test_responses_ws_cancellation_preserves_partial_usage_and_body(monkeypatch, m):
    _setup(m)
    _make_channel(m)
    ws = FakeWebSocket({
        "type": "response.create", "model": "test-model",
        "input": "hello", "stream": True,
    })
    fake_upstream = BlockingAfterEventsWebSocket([{
        "type": "response.in_progress",
        "response": {
            "id": "cancel_partial",
            "usage": {
                "input_tokens": 5,
                "output_tokens": 1,
                "input_tokens_details": {"cached_tokens": 2},
            },
        },
    }])

    async def fake_connect(*args, **kwargs):
        return fake_upstream

    monkeypatch.setattr(m["responses_ws"], "_connect_upstream_ws", fake_connect)
    task = asyncio.create_task(
        m["responses_ws"].handle_responses_ws(ws)  # type: ignore[arg-type]
    )
    await asyncio.wait_for(fake_upstream.waiting.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    row = _last_request_log(m)
    assert row["status"] == "cancelled"
    assert row["usage_observed"] == 1
    detail = m["log_db"].log_detail(row["request_id"])
    assert "response.in_progress" in detail["detail"]["response_body"]
    attempts = _attempt_usage(m, row["request_id"])
    assert len(attempts) == 1
    assert attempts[0]["usage_observed"] == 1
    assert attempts[0]["input_tokens"] == 3
    assert attempts[0]["cache_read_tokens"] == 2
    assert attempts[0]["output_tokens"] == 1


@pytest.mark.asyncio
async def test_responses_ws_sse_error_body_cancellation_terminalizes_usage(
    monkeypatch, m,
):
    _setup(m)
    _make_channel(m, extra={"responsesWsUpstreamTransport": "sse"})
    ws = FakeWebSocket({
        "type": "response.create", "model": "test-model",
        "input": "hello", "stream": True,
    })
    waiting = asyncio.Event()
    closed = []
    partial_body = json.dumps({
        "error": {"code": "server_error", "message": "partial upstream error"},
        "usage": {
            "input_tokens": 5,
            "output_tokens": 1,
            "input_tokens_details": {"cached_tokens": 2},
        },
    }).encode()

    class BlockingErrorResponse:
        status_code = 503
        reason_phrase = "Service Unavailable"

        async def aiter_bytes(self):
            yield partial_body
            waiting.set()
            await asyncio.Event().wait()

    class FakeContext:
        async def __aexit__(self, *args):
            closed.append(True)

    async def fake_open(**kwargs):
        m["log_db"].mark_retry_attempt_dispatch(
            kwargs["retry_attempt_id"], {"service_tier": "default"},
        )
        return SimpleNamespace(
            error=None,
            response=BlockingErrorResponse(),
            connect_ms=7,
            timing=None,
            proxy_name=None,
            proxy_bytes={"up": 11, "down": len(partial_body)},
            proxy_client=None,
            proxy_attempt_id=None,
            round_timeouts=None,
            ctx=FakeContext(),
        )

    monkeypatch.setattr(
        m["responses_ws"], "open_response_with_proxy_chain", fake_open,
    )
    task = asyncio.create_task(
        m["responses_ws"].handle_responses_ws(ws)  # type: ignore[arg-type]
    )
    await asyncio.wait_for(waiting.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert closed == [True]
    row = _last_request_log(m)
    assert row["status"] == "cancelled"
    assert row["http_status"] == 499
    assert row["usage_observed"] == 1
    detail = m["log_db"].log_detail(row["request_id"])
    assert "partial upstream error" in detail["detail"]["response_body"]
    attempts = _attempt_usage(m, row["request_id"])
    assert len(attempts) == 1
    assert attempts[0]["outcome"] == "cancelled"
    assert attempts[0]["usage_observed"] == 1
    assert attempts[0]["input_tokens"] == 3
    assert attempts[0]["cache_read_tokens"] == 2
    assert attempts[0]["output_tokens"] == 1


@pytest.mark.asyncio
async def test_responses_ws_created_commits_without_becoming_visible_or_failing_over(monkeypatch, m):
    cfg = _setup(m)
    # This case isolates candidate failover; same-candidate transient retry has
    # dedicated coverage below.
    cfg["retry"] = {"transient": {"enabled": False}}
    bad = m["OpenAIApiChannel"]({
        "name": "bad-meta", "type": "api", "baseUrl": "https://bad-meta.example",
        "apiKey": "bad", "protocol": "openai-responses",
        "models": [{"alias": "test-model", "real": "real-model"}], "enabled": True,
    })
    good = m["OpenAIApiChannel"]({
        "name": "good-meta", "type": "api", "baseUrl": "https://good-meta.example",
        "apiKey": "good", "protocol": "openai-responses",
        "models": [{"alias": "test-model", "real": "real-model"}], "enabled": True,
    })
    with m["registry"]._lock:
        m["registry"]._channels = {bad.key: bad, good.key: good}
    ws = SequentialFakeWebSocket({
        "type": "response.create", "model": "test-model", "input": "hello", "stream": True,
    })
    bad_up = FakeUpstreamWebSocket([
        {"type": "response.created", "response": {"id": "bad"}},
        {"type": "error", "status": 503,
         "error": {"code": "server_error", "message": "boom"},
         "usage": {"input_tokens": 0, "output_tokens": 0}},
    ])
    good_up = FakeUpstreamWebSocket([
        {"type": "response.created", "response": {"id": "good"}},
        {"type": "response.output_item.added", "output_index": 0, "item": {"type": "message", "content": []}},
        {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": "ok"},
        {"type": "response.completed", "response": {"id": "good", "output": [], "usage": {}}},
    ])

    async def fake_connect(url, *, headers, connector, proxy_bytes, open_timeout, timing=None, round_timeouts=None):
        return bad_up if "bad-meta" in url else good_up

    monkeypatch.setattr(m["responses_ws"], "_connect_upstream_ws", fake_connect)
    await m["responses_ws"].handle_responses_ws(ws)  # type: ignore[arg-type]

    sent = [json.loads(t) for t in ws.sent_texts]
    assert [event["type"] for event in sent] == ["response.created", "error"]
    assert sent[0]["response"]["id"] == "bad"
    # response.created commits dispatch but isn't itself a visible output event.
    assert len(bad_up.sent) == 1
    assert good_up.sent == []
    assert not ws.close_calls
    row = _last_request_log(m)
    attempts = _attempt_usage(m, row["request_id"])
    assert [item["attempt_order"] for item in attempts] == [1]
    assert attempts[0]["usage_observed"] == 1
    assert attempts[0]["input_tokens"] == attempts[0]["output_tokens"] == 0


@pytest.mark.parametrize("visible_event", [
    {
        "type": "response.custom_tool_call_input.delta",
        "output_index": 0,
        "item_id": "ctc_opaque",
        "call_id": "opaque-call",
        "delta": "{\"cmd\":",
    },
    {
        "type": "response.output_item.done",
        "output_index": 0,
        "item": {
            "type": "function_call",
            "call_id": "opaque-call",
            "name": "shell",
            "arguments": "{}",
        },
    },
])
@pytest.mark.asyncio
async def test_responses_ws_tool_execution_events_are_visible_commit_boundaries(
    monkeypatch, m, visible_event,
):
    cfg = _setup(m)
    cfg["retry"] = {"transient": {"enabled": False}}
    channels = [m["OpenAIApiChannel"]({
        "name": f"tool-visible-{idx}",
        "type": "api",
        "baseUrl": f"https://tool-visible-{idx}.example",
        "apiKey": "up-key",
        "protocol": "openai-responses",
        "models": [{"alias": "test-model", "real": "real-model"}],
        "enabled": True,
    }) for idx in range(2)]
    first_upstream = FakeUpstreamWebSocket([
        visible_event,
        {"type": "error", "status": 503, "error": {
            "type": "api_error", "code": "server_error", "message": "after execution",
        }},
    ])
    connect_calls = []

    async def fake_connect(url, **kwargs):
        connect_calls.append(url)
        if len(connect_calls) > 1:
            raise AssertionError("visible tool execution must prevent candidate replay")
        return first_upstream

    ws = SequentialFakeWebSocket({
        "type": "response.create", "model": "test-model", "input": "hello", "stream": True,
    })
    await ws.accept()
    await ws.receive()  # handler normally consumes the first create before _run_ws_failover
    request_id = f"responses-ws-visible-{visible_event['type']}"
    body = {"model": "test-model", "input": "hello", "stream": True}
    started_at = time.time()
    started_monotonic = time.monotonic()
    m["log_db"].insert_pending(
        request_id, "1.2.3.4", "ws-key", "test-model", True,
        1, 0, {}, body, ingress_protocol="responses_ws",
    )
    from src.scheduler import ScheduleResult
    route = ScheduleResult(
        candidates=[(ch, "real-model") for ch in channels],
        saturated=[], affinity_hit=False, fp_query=None, client_key="client:1",
    )
    monkeypatch.setattr(m["responses_ws"], "_connect_upstream_ws", fake_connect)
    await m["responses_ws"]._run_ws_failover(
        ws,
        first_obj={"type": "response.create", **body},
        schedule_result=route,
        body=body,
        request_id=request_id,
        api_key_name="ws-key",
        client_ip="1.2.3.4",
        start_time=started_at,
        start_monotonic=started_monotonic,
        fp_query=None,
    )

    sent = [json.loads(text) for text in ws.sent_texts]
    assert sent[0] == visible_event
    assert sent[1]["type"] == "error"
    assert len(connect_calls) == 1


@pytest.mark.asyncio
async def test_responses_ws_transient_retries_same_candidate_and_honors_retry_after(monkeypatch, m):
    cfg = _setup(m)
    cfg["retry"] = {
        "transient": {
            "enabled": True,
            "maxExtraAttempts": 1,
            "backoffSeconds": [0],
            "errors": {"openaiServerError": True},
        }
    }
    ch = _make_channel(m)
    ws = FakeWebSocket({
        "type": "response.create", "model": "test-model", "input": "hello", "stream": True,
    })
    # Retry remains valid only before response.created / visible output.
    bad_up = FakeUpstreamWebSocket([
        {"type": "error", "status": 503, "error": {"code": "server_error", "message": "retry me"}},
    ])
    bad_up.response = SimpleNamespace(headers={"Retry-After": "4"})
    good_up = FakeUpstreamWebSocket([
        {"type": "response.created", "response": {"id": "good"}},
        {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": "ok"},
        {"type": "response.completed", "response": {"id": "good", "output": [], "usage": {}}},
    ])
    upstreams = [bad_up, good_up]
    connect_calls = []

    async def fake_connect(url, *, headers, connector, proxy_bytes, open_timeout, timing=None, round_timeouts=None):
        connect_calls.append(url)
        return upstreams.pop(0)

    observed_retry_after = []

    async def capture_wait(ordinal, retry_cfg, deadline_ts, *, retry_after_seconds=None):
        observed_retry_after.append(retry_after_seconds)
        return 0.0

    monkeypatch.setattr(m["responses_ws"], "_connect_upstream_ws", fake_connect)
    monkeypatch.setattr(m["responses_ws"], "_wait_for_transient_retry", capture_wait)
    await m["responses_ws"].handle_responses_ws(ws)  # type: ignore[arg-type]

    assert len(connect_calls) == 2
    assert observed_retry_after == [4.0]
    assert any(json.loads(text).get("delta") == "ok" for text in ws.sent_texts)
    assert not ws.close_calls
    row = _last_request_log(m)
    assert row["status"] == "success"
    assert row["retry_count"] == 1
    assert [item["outcome"] for item in _retry_chain(m, row["request_id"])] == [
        "upstream_error_json", "success",
    ]
    assert m["cooldown"].get_state(ch.key, "real-model") is None
    assert m["scorer"].get_stats(ch.key, "real-model")["total_requests"] == 1


@pytest.mark.asyncio
async def test_responses_ws_oauth_refresh_setting_can_disable_refresh(monkeypatch, m):
    cfg = _setup(m)
    cfg["retry"] = {
        "transient": {"enabled": False},
        "recovery": {"oauthRefresh": False},
    }
    oauth_ch = _make_oauth_channel_for_failover(m, name="no-refresh@example.com")
    api_ch = m["OpenAIApiChannel"]({
        "name": "after-auth", "type": "api", "baseUrl": "https://after-auth.example",
        "apiKey": "good", "protocol": "openai-responses",
        "models": [{"alias": "test-model", "real": "real-model"}], "enabled": True,
    })
    with m["registry"]._lock:
        m["registry"]._channels = {oauth_ch.key: oauth_ch, api_ch.key: api_ch}

    ws = FakeWebSocket({"type": "response.create", "model": "test-model", "input": "hello", "stream": True})
    await ws.accept()
    first_obj = {"type": "response.create", "model": "test-model", "input": "hello", "stream": True}
    body = {"model": "test-model", "input": "hello", "stream": True}
    request_id = "responses-ws-oauth-refresh-disabled"
    started_at = time.time()
    started_monotonic = time.monotonic()
    m["log_db"].insert_pending(
        request_id, "1.2.3.4", "ws-key", "test-model", True,
        1, 0, {}, body, ingress_protocol="responses_ws",
    )
    calls = []

    async def fake_try(_websocket, **kwargs):
        channel = kwargs["ch"]
        calls.append(channel.key)
        if channel.key == oauth_ch.key:
            return m["responses_ws"]._WsAttemptResult(
                outcome="http_auth_error", http_status=401, error_detail="HTTP 401",
            )
        m["log_db"].finish_success(
            request_id, channel.key, channel.type, kwargs["resolved_model"],
            retry_count=kwargs["retry_count_so_far"], http_status=101,
            upstream_protocol="openai-responses", upstream_transport="ws",
        )
        return m["responses_ws"]._WsAttemptResult(ok=True, outcome="success")

    async def must_not_refresh(_account_key):
        raise AssertionError("oauthRefresh=false must suppress force_refresh")

    from src.scheduler import ScheduleResult
    route = ScheduleResult(
        candidates=[(oauth_ch, "test-model"), (api_ch, "real-model")],
        saturated=[], affinity_hit=False, fp_query=None, client_key="client:1",
    )
    monkeypatch.setattr(m["responses_ws"], "_try_ws_channel", fake_try)
    monkeypatch.setattr(m["responses_ws"].oauth_manager, "force_refresh", must_not_refresh)
    accepted = await m["responses_ws"]._run_ws_failover(
        ws,
        first_obj=first_obj,
        schedule_result=route,
        body=body,
        request_id=request_id,
        api_key_name="ws-key",
        client_ip="1.2.3.4",
        start_time=started_at,
        start_monotonic=started_monotonic,
        fp_query=None,
    )

    assert accepted is True
    assert calls == [oauth_ch.key, api_ch.key]
    assert _last_request_log(m)["status"] == "success"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("statuses", "expected_status", "expected_close"),
    [
        ([401, 401], 401, 4401),
        ([401, 403], 403, 4403),
        ([403, 401], 403, 4403),
        ([402, 402], 402, 4400),
        ([429, 429], 429, 4429),
        ([401, 429], 503, 4500),
        ([500, 429], 503, 4500),
    ],
)
async def test_responses_ws_exhausted_candidates_use_finite_terminal_rule_and_safe_error(
    monkeypatch, m, statuses, expected_status, expected_close,
):
    cfg = _setup(m)
    cfg["retry"] = {
        "transient": {"enabled": False},
        "recovery": {"oauthRefresh": False},
    }
    channels = [m["OpenAIApiChannel"]({
        "name": f"terminal-{idx}-{status}",
        "type": "api",
        "baseUrl": f"https://terminal-{idx}.example",
        "apiKey": f"up-key-{idx}",
        "protocol": "openai-responses",
        "models": [{"alias": "test-model", "real": "real-model"}],
        "enabled": True,
    }) for idx, status in enumerate(statuses)]
    with m["registry"]._lock:
        m["registry"]._channels = {ch.key: ch for ch in channels}

    ws = FakeWebSocket({
        "type": "response.create", "model": "test-model", "input": "hello",
        "stream": True,
    })
    await ws.accept()
    request_id = f"responses-ws-terminal-{'-'.join(map(str, statuses))}"
    body = {"model": "test-model", "input": "hello", "stream": True}
    started_at = time.time()
    started_monotonic = time.monotonic()
    m["log_db"].insert_pending(
        request_id, "1.2.3.4", "ws-key", "test-model", True,
        1, 0, {}, body, ingress_protocol="responses_ws",
    )
    sensitive_detail = (
        "Bearer secret-bearer api_key=sk-sensitive access_token=access-secret "
        "refresh_token=refresh-secret credential=password "
        "email=user@example.com proxy authorization=proxy-secret"
    )

    async def fake_try(_websocket, **kwargs):
        idx = channels.index(kwargs["ch"])
        status = statuses[idx]
        outcome = "http_auth_error" if status in (401, 403) else "http_error"
        return m["responses_ws"]._WsAttemptResult(
            outcome=outcome, http_status=status,
            error_detail=sensitive_detail if idx == len(channels) - 1 else f"HTTP {status}",
        )

    from src.scheduler import ScheduleResult
    route = ScheduleResult(
        candidates=[(ch, "real-model") for ch in channels], saturated=[],
        affinity_hit=False, fp_query=None, client_key="client:1",
    )
    monkeypatch.setattr(m["responses_ws"], "_try_ws_channel", fake_try)
    accepted = await m["responses_ws"]._run_ws_failover(
        ws,
        first_obj={"type": "response.create", **body},
        schedule_result=route,
        body=body,
        request_id=request_id,
        api_key_name="ws-key",
        client_ip="1.2.3.4",
        start_time=started_at,
        start_monotonic=started_monotonic,
        fp_query=None,
    )

    assert accepted is True
    row = dict(m["log_db"]._get_conn().execute(
        "SELECT * FROM request_log WHERE request_id=?", (request_id,),
    ).fetchone())
    assert row["http_status"] == expected_status
    assert ws.close_calls[-1][0] == expected_close
    downstream = " ".join(ws.sent_texts + [ws.close_calls[-1][1]]).lower()
    for secret in (
        "secret-bearer", "sk-sensitive", "access-secret", "refresh-secret",
        "password", "user@example.com", "proxy-secret",
    ):
        assert secret not in downstream
    error_frames = [json.loads(text) for text in ws.sent_texts if json.loads(text).get("type") == "error"]
    assert len(error_frames) == 1
    frame = error_frames[0]
    assert frame["status"] == expected_status
    assert frame["message"] == ws.close_calls[-1][1]
    assert set(frame["error"]) >= {"type", "code", "message"}
    assert frame["error"]["message"] == frame["message"]
    parsed = m["responses_ws"]._parse_wrapped_ws_error(json.dumps(frame))
    assert parsed == {
        "status": expected_status,
        "code": frame["error"]["code"],
        "message": frame["error"]["message"],
    }
    attempts = _retry_chain(m, request_id)
    assert len(attempts) == len(statuses)
    assert attempts[-1]["error_detail"] == sensitive_detail


@pytest.mark.asyncio
async def test_responses_ws_records_quota_snapshot_from_upgrade_headers(monkeypatch, m):
    _setup(m)
    ch = m["OpenAIOAuthChannel"]({
        "email": "quota@example.com",
        "provider": "openai",
        "accountKey": "openai:quota@example.com",
        "accessToken": "tok",
        "refreshToken": "rt",
        "expiresAt": 9999999999,
        "models": ["test-model"],
    })
    with m["registry"]._lock:
        m["registry"]._channels = {ch.key: ch}

    async def fake_token(account_key):
        return "tok"

    recorded = {}
    def fake_record(channel, response):
        recorded["channel"] = channel.key
        recorded["headers"] = dict(response.headers)

    monkeypatch.setattr(m["responses_ws"].oauth_manager, "ensure_valid_token", fake_token)
    monkeypatch.setattr(m["failover"], "_maybe_record_codex_snapshot", fake_record)
    ws = FakeWebSocket({"type": "response.create", "model": "test-model", "input": "hello", "stream": True})
    fake_upstream = FakeUpstreamWebSocket([
        {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": "ok"},
        {"type": "response.completed", "response": {"id": "resp", "output": [], "usage": {}}},
    ])
    fake_upstream.response = SimpleNamespace(headers={"x-codex-primary-used-percent": "12"})

    async def fake_connect(url, *, headers, connector, proxy_bytes, open_timeout, timing=None, round_timeouts=None):
        return fake_upstream

    monkeypatch.setattr(m["responses_ws"], "_connect_upstream_ws", fake_connect)
    await m["responses_ws"].handle_responses_ws(ws)  # type: ignore[arg-type]

    assert recorded["channel"] == ch.key
    assert recorded["headers"]["x-codex-primary-used-percent"] == "12"
    assert not ws.close_calls



def test_sync_translated_body_to_ws_create_updates_input_and_instructions(m):
    _setup(m)
    obj = {
        "type": "response.create",
        "model": "test-model",
        "input": "原文",
        "instructions": "原系统",
        "stream": True,
        "background": False,
    }
    body = {
        "model": "real-model",
        "input": "translated input",
        "instructions": "translated instructions",
        "stream": True,
        "prompt_cache_key": "pc-translated",
    }
    m["responses_ws"]._sync_translated_body_to_ws_create(obj, body)
    assert obj["model"] == "real-model"
    assert obj["input"] == "translated input"
    assert obj["instructions"] == "translated instructions"
    assert obj["prompt_cache_key"] == "pc-translated"
    assert "background" not in obj

def test_map_ws_create_frame_applies_model_guard_and_codex_transform(m):
    _setup(m)
    ch = _make_channel(m)
    obj = {
        "type": "response.create",
        "model": "test-model",
        "input": "hello",
        "stream": True,
        "generate": False,
        "background": False,
        "unknown_provider_field": "drop",
        "_api_key_name": "internal",
    }
    body = m["responses_ws"]._request_body_from_ws_create(obj)
    m["config"]._cache["apiKeys"]["ws-key"]["allowedModels"] = ["test-model"]
    m["config"]._cache["modelMappings"] = {"openai-responses": {"test-model": "test-model"}}
    m["responses_ws"].model_mapping.apply_default(body, "openai-responses")
    m["responses_ws"].model_mapping.apply_mapping(body, "openai-responses")
    m["responses_ws"].guard_responses_ingress(body, store_enabled=True)
    body["stream"] = True
    m["responses_ws"]._sync_prompt_cache_key_to_ws_create(obj, body)

    mapped = m["responses_ws"]._map_ws_create_frame_for_upstream(obj, "real-model", channel=ch)
    assert mapped["type"] == "response.create"
    assert mapped["model"] == "real-model"
    assert mapped["generate"] is False
    assert "background" not in mapped
    assert "unknown_provider_field" not in mapped
    assert "_api_key_name" not in mapped

    oauth = m["OpenAIOAuthChannel"]({
        "email": "x@example.com", "provider": "openai",
        "accountKey": "openai:x@example.com", "accessToken": "tok",
        "refreshToken": "rt", "expiresAt": 9999999999,
        "models": ["test-model"],
    })
    codex_mapped = m["responses_ws"]._map_ws_create_frame_for_upstream({
        "type": "response.create", "model": "test-model", "input": "hello",
        "stream": True, "generate": False, "temperature": 1, "background": False,
        "unknown_provider_field": "drop", "_api_key_name": "internal",
    }, "test-model", channel=oauth)
    assert codex_mapped["store"] is False
    assert codex_mapped["stream"] is True
    assert codex_mapped["generate"] is False
    assert codex_mapped["input"] == [{"type": "message", "role": "user", "content": "hello"}]
    assert "temperature" not in codex_mapped
    assert "background" not in codex_mapped
    assert "unknown_provider_field" not in codex_mapped
    assert "_api_key_name" not in codex_mapped
    assert "ws_request_header_x_openai_internal_codex_responses_lite" not in codex_mapped.get("client_metadata", {})

    codex_lite_mapped = m["responses_ws"]._map_ws_create_frame_for_upstream({
        "type": "response.create", "model": "gpt-5.6-luna", "input": "hello",
        "stream": True, "client_metadata": {"a": "b"},
    }, "gpt-5.6-luna", channel=oauth)
    assert codex_lite_mapped["client_metadata"]["a"] == "b"
    assert codex_lite_mapped["client_metadata"]["ws_request_header_x_openai_internal_codex_responses_lite"] == "true"

    first_with_previous = m["responses_ws"]._map_ws_create_frame_for_upstream({
        "type": "response.create",
        "model": "test-model",
        "previous_response_id": "resp_before_connect",
        "input": [{"type": "message", "role": "user", "content": "delta-first"}],
        "stream": True,
    }, "test-model", channel=oauth)
    assert first_with_previous["previous_response_id"] == "resp_before_connect"
    assert first_with_previous["input"] == [
        {"type": "message", "role": "user", "content": "delta-first"}
    ]

    official_prefix = [
        {
            "type": "additional_tools",
            "id": "at_official",
            "role": "developer",
            "tools": [{"type": "function", "name": "shell", "parameters": {"type": "object"}}],
        },
        {
            "type": "message",
            "id": "msg_official",
            "role": "developer",
            "content": [{"type": "input_text", "text": "official instructions"}],
        },
        {"type": "message", "role": "user", "content": "full prompt"},
    ]
    official_warmup = m["responses_ws"]._map_ws_create_frame_for_upstream({
        "type": "response.create",
        "model": "gpt-5.6-luna",
        "input": official_prefix,
        "instructions": "",
        "parallel_tool_calls": False,
        "generate": False,
    }, "gpt-5.6-luna", channel=oauth)
    assert official_warmup["generate"] is False
    assert official_warmup["input"] == official_prefix
    assert sum(
        item.get("type") == "additional_tools"
        for item in official_warmup["input"] if isinstance(item, dict)
    ) == 1
    assert [
        item for item in official_warmup["input"]
        if isinstance(item, dict) and item.get("role") == "developer"
    ][1]["content"][0]["text"] == "official instructions"
    assert "You are a helpful coding assistant." not in json.dumps(official_warmup)

    lite_delta = [{
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "only the delta"}],
    }]
    official_incremental = m["responses_ws"]._map_ws_create_frame_for_upstream({
        "type": "response.create",
        "model": "gpt-5.6-luna",
        "previous_response_id": "resp_lite_warmup",
        "input": lite_delta,
        "instructions": "",
        "parallel_tool_calls": False,
    }, "gpt-5.6-luna", channel=oauth)
    assert official_incremental["previous_response_id"] == "resp_lite_warmup"
    assert official_incremental["input"] == lite_delta
    assert not any(
        isinstance(item, dict) and (
            item.get("type") == "additional_tools" or item.get("role") == "developer"
        )
        for item in official_incremental["input"]
    )


@pytest.mark.asyncio
async def test_responses_ws_writes_log_retry_usage_proxy_and_affinity(monkeypatch, m):
    cfg = _setup(m)
    cfg["openai"] = {"autoPromptCacheKey": {"enabled": False}}
    _make_channel(m)
    ws = FakeWebSocket({
        "type": "response.create", "model": "test-model",
        "input": "hello", "stream": True, "prompt_cache_key": "pc-key",
    })
    fake_upstream = FakeUpstreamWebSocket([
        {"type": "response.output_item.added", "output_index": 0, "item": {"type": "message", "content": []}},
        {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": "ok"},
        {"type": "response.completed", "response": {
            "id": "resp",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
            "usage": {"input_tokens": 5, "output_tokens": 3, "input_tokens_details": {"cached_tokens": 2}},
        }},
    ])

    async def fake_connect(url, *, headers, connector, proxy_bytes, open_timeout, timing=None, round_timeouts=None):
        proxy_bytes.count(up=11, down=17)
        return fake_upstream

    class DummyProxy:
        type = "socks5"
        stats = SimpleNamespace(total_attempts=0, last_attempt_ts=0, total_successes=0,
                                last_success_ts=0, last_latency_ms=0, total_failures=0, last_error=None)
    dummy_proxy = DummyProxy()
    monkeypatch.setattr(m["responses_ws"], "_connect_upstream_ws", fake_connect)
    monkeypatch.setattr(m["responses_ws"], "_pick_non_direct_proxy_name", lambda ch, model: "proxy-a")
    monkeypatch.setattr(m["responses_ws"], "_resolve_ws_route_chain", lambda ch, model: [("proxy-a", dummy_proxy)])
    await m["responses_ws"].handle_responses_ws(ws)  # type: ignore[arg-type]

    row = _last_request_log(m)
    assert row["status"] == "success"
    assert row["ingress_protocol"] == "responses_ws"
    assert row["upstream_protocol"] == "openai-responses"
    assert row["final_channel_key"] == "api:ws-upstream"
    assert row["final_model"] == "real-model"
    assert row["http_status"] == 101
    assert row["upstream_transport"] == "ws"
    assert row["input_tokens"] == 3  # input_tokens - cached_tokens
    assert row["output_tokens"] == 3
    assert row["cache_read_tokens"] == 2
    assert row["proxy_name"] == "proxy-a"
    assert row["proxy_bytes_up"] > 0
    assert row["proxy_bytes_down"] > 0
    assert row["final_round_id"]
    assert row["connect_time_ms"] is not None
    assert row["first_token_time_ms"] is not None
    assert row["idle_time_ms"] is not None
    assert row["total_time_ms"] is not None
    assert row["request_elapsed_ms"] is not None
    chain = _retry_chain(m, row["request_id"])
    assert len(chain) == 1
    assert chain[0]["outcome"] == "success"
    assert chain[0]["proxy_name"] == "proxy-a"
    assert chain[0]["bytes_up"] > 0
    assert chain[0]["bytes_down"] > 0
    assert chain[0]["final_round_id"] == row["final_round_id"]
    assert chain[0]["total_ms"] is not None
    assert chain[0]["attempt_elapsed_ms"] is not None
    rounds = _proxy_chain(m, row["request_id"])
    assert len(rounds) == 1
    assert rounds[0]["round_id"] == row["final_round_id"]
    assert rounds[0]["transport"] == "ws"
    assert rounds[0]["request_mode"] == "ws"
    assert rounds[0]["total_ms"] is not None
    assert rounds[0]["ended_at"] is not None
    assert row["fingerprint"] is None  # first-turn requests have no query affinity anchor
    fp_write = m["fingerprint"].fingerprint_write_responses(
        "ws-key",
        "1.2.3.4",
        [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hello"}]}],
        [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
    )
    aff = m["affinity"].get(fp_write)
    assert aff is not None
    assert aff["channel_key"] == "api:ws-upstream"
    assert aff["model"] == "real-model"


@pytest.mark.asyncio
async def test_responses_ws_proxy_chain_falls_back_before_first_event(monkeypatch, m):
    _setup(m)
    _make_channel(m)
    ws = FakeWebSocket({"type": "response.create", "model": "test-model", "input": "hello", "stream": True})
    fake_upstream = FakeUpstreamWebSocket([
        {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": "ok"},
        {"type": "response.completed", "response": {"id": "resp", "output": [], "usage": {}}},
    ])
    calls = []
    class DummyProxy:
        type = "socks5"
        url = "socks5://127.0.0.1:9999"
        stats = SimpleNamespace(total_attempts=0, last_attempt_ts=0, total_successes=0,
                                last_success_ts=0, last_latency_ms=0, total_failures=0, last_error=None)
    dummy = DummyProxy()

    async def fake_connect(url, *, headers, connector, proxy_bytes, open_timeout, timing=None, round_timeouts=None):
        calls.append(connector)
        if connector is dummy:
            raise OSError("proxy down")
        return fake_upstream

    monkeypatch.setattr(m["responses_ws"], "_resolve_ws_route_chain", lambda ch, model: [("p1", dummy), ("direct", None)])
    monkeypatch.setattr(m["responses_ws"], "_connect_upstream_ws", fake_connect)
    await m["responses_ws"].handle_responses_ws(ws)  # type: ignore[arg-type]

    assert calls == [dummy, None]
    assert dummy.stats.total_failures == 1
    row = _last_request_log(m)
    rounds = _proxy_chain(m, row["request_id"])
    assert len(rounds) == 2
    assert rounds[0]["round_id"] != rounds[1]["round_id"]
    assert all(item["ended_at"] is not None for item in rounds)
    assert rounds[-1]["round_id"] == row["final_round_id"]
    assert not ws.close_calls


@pytest.mark.asyncio
async def test_responses_ws_connect_failures_never_set_dispatch_marker(monkeypatch, m):
    _setup(m)
    _make_channel(m)
    ws = FakeWebSocket({
        "type": "response.create", "model": "test-model",
        "input": "hello", "stream": True,
    })

    class DummyProxy:
        type = "socks5"
        url = "socks5://127.0.0.1:9999"
        stats = SimpleNamespace(
            total_attempts=0, last_attempt_ts=0, total_successes=0,
            last_success_ts=0, last_latency_ms=0, total_failures=0,
            last_error=None,
        )

    dummy = DummyProxy()

    async def fail_connect(*args, **kwargs):
        raise OSError("route unavailable")

    monkeypatch.setattr(
        m["responses_ws"], "_resolve_ws_route_chain",
        lambda ch, model: [("p1", dummy), ("direct", None)],
    )
    monkeypatch.setattr(m["responses_ws"], "_connect_upstream_ws", fail_connect)
    await m["responses_ws"].handle_responses_ws(ws)  # type: ignore[arg-type]

    row = _last_request_log(m)
    chain = _retry_chain(m, row["request_id"])
    assert len(chain) == 1
    assert chain[0]["outcome"] == "connect_error"
    assert chain[0]["dispatched_at"] is None
    conn = m["log_db"]._get_conn()
    assert conn.execute(
        "SELECT COUNT(*) FROM upstream_attempt_usage WHERE root_request_id=?",
        (row["request_id"],),
    ).fetchone()[0] == 0


@pytest.mark.asyncio
async def test_responses_ws_does_not_replay_proxy_route_after_create_send_starts(
    monkeypatch, m,
):
    cfg = _setup(m)
    cfg["retry"] = {"transient": {"enabled": False}}
    _make_channel(m)
    ws = FakeWebSocket({
        "type": "response.create", "model": "test-model",
        "input": "hello", "stream": True,
    })

    class DummyProxy:
        type = "socks5"
        url = "socks5://127.0.0.1:9999"
        stats = SimpleNamespace(
            total_attempts=0, last_attempt_ts=0, total_successes=0,
            last_success_ts=0, last_latency_ms=0, total_failures=0,
            last_error=None,
        )

    class FailOnCreate:
        response = SimpleNamespace(headers={})

        async def send(self, *args, **kwargs):
            raise OSError("send outcome is uncertain")

        async def close(self, *args, **kwargs):
            return None

    dummy = DummyProxy()
    connect_calls = []

    async def fake_connect(*args, connector=None, **kwargs):
        connect_calls.append(connector)
        return FailOnCreate()

    monkeypatch.setattr(
        m["responses_ws"], "_resolve_ws_route_chain",
        lambda ch, model: [("p1", dummy), ("direct", None)],
    )
    monkeypatch.setattr(m["responses_ws"], "_connect_upstream_ws", fake_connect)
    await m["responses_ws"].handle_responses_ws(ws)  # type: ignore[arg-type]

    assert connect_calls == [dummy]
    row = _last_request_log(m)
    chain = _retry_chain(m, row["request_id"])
    assert len(chain) == 1 and chain[0]["dispatched_at"] is not None
    facts = m["log_db"]._get_conn().execute(
        "SELECT dispatch_state,cost_source FROM upstream_attempt_usage "
        "WHERE root_request_id=?", (row["request_id"],),
    ).fetchall()
    assert [tuple(item) for item in facts] == [("sent", "unpriced")]


@pytest.mark.asyncio
async def test_responses_ws_filters_non_responses_channels_without_scoring_failure(monkeypatch, m):
    _setup(m)
    bad = m["OpenAIApiChannel"]({
        "name": "chat-only", "type": "api", "baseUrl": "https://chat.example",
        "apiKey": "chat", "protocol": "openai-chat",
        "models": [{"alias": "test-model", "real": "real-model"}], "enabled": True,
    })
    good = m["OpenAIApiChannel"]({
        "name": "good-after-filter", "type": "api", "baseUrl": "https://good-after.example",
        "apiKey": "good", "protocol": "openai-responses",
        "models": [{"alias": "test-model", "real": "real-model"}], "enabled": True,
    })
    with m["registry"]._lock:
        m["registry"]._channels = {bad.key: bad, good.key: good}
    ws = FakeWebSocket({"type": "response.create", "model": "test-model", "input": "hello", "stream": True})
    fake_upstream = FakeUpstreamWebSocket([
        {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": "ok"},
        {"type": "response.completed", "response": {"id": "resp", "output": [], "usage": {}}},
    ])

    async def fake_connect(url, *, headers, connector, proxy_bytes, open_timeout, timing=None, round_timeouts=None):
        assert url == "wss://good-after.example/v1/responses"
        return fake_upstream

    monkeypatch.setattr(m["responses_ws"], "_connect_upstream_ws", fake_connect)
    await m["responses_ws"].handle_responses_ws(ws)  # type: ignore[arg-type]

    assert not ws.close_calls
    row = _last_request_log(m)
    assert row["status"] == "success"
    assert row["final_channel_key"] == good.key
    chain = _retry_chain(m, row["request_id"])
    assert len(chain) == 1
    assert chain[0]["channel_key"] == good.key
    assert m["cooldown"].get_state(bad.key, "real-model") is None


@pytest.mark.asyncio
async def test_responses_ws_stream_error_after_visible_logs_and_cools_down(monkeypatch, m):
    cfg = _setup(m)
    cfg["errorWindows"] = [1, 0]
    _make_channel(m)
    ws = FakeWebSocket({"type": "response.create", "model": "test-model", "input": "hello", "stream": True})
    fake_upstream = FakeUpstreamWebSocket([
        {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": "partial"},
        {"type": "response.failed", "response": {"id": "resp", "error": {"code": "boom", "message": "failed later"}, "usage": {}}},
    ])

    async def fake_connect(url, *, headers, connector, proxy_bytes, open_timeout, timing=None, round_timeouts=None):
        return fake_upstream

    monkeypatch.setattr(m["responses_ws"], "_connect_upstream_ws", fake_connect)
    await m["responses_ws"].handle_responses_ws(ws)  # type: ignore[arg-type]

    assert any(json.loads(t)["type"] == "response.output_text.delta" for t in ws.sent_texts)
    assert not ws.close_calls
    row = _last_request_log(m)
    assert row["status"] == "error"
    assert row["final_channel_key"] == "api:ws-upstream"
    assert row["http_status"] == 503
    assert "failed later" in row["error_message"]
    assert m["cooldown"].get_state("api:ws-upstream", "real-model") is not None




def test_responses_upstream_ws_config_default_off(m):
    cfg = _setup(m)
    assert m["failover"]._responses_upstream_ws_enabled(cfg) is False
    ch = _make_oauth_channel_for_failover(m)
    assert m["failover"]._should_use_responses_upstream_ws(ch, ingress_protocol="responses", cfg=cfg) is False
    cfg.setdefault("openai", {})["responsesUpstreamWsForOAuth"] = True
    assert m["failover"]._should_use_responses_upstream_ws(ch, ingress_protocol="responses", cfg=cfg) is True



@pytest.mark.asyncio
async def test_http_responses_oauth_ws_identity_confuse_first_frame_and_restores_response(monkeypatch, m):
    cfg = _setup(m)
    cfg.setdefault("openai", {})["responsesUpstreamWsForOAuth"] = True
    cfg.setdefault("oauth", {})["providers"] = {"openai": {"isolateSessionId": True, "forceCodexCLI": True}}
    ch = _make_oauth_channel_for_failover(m, name="identity@example.com")

    async def fake_token(account_key):
        return "tok"

    captured: dict[str, Any] = {}

    async def fake_connect(url, *, headers, connector, proxy_bytes, open_timeout, timing=None, round_timeouts=None):
        captured["headers"] = dict(headers)
        return fake_ws

    fake_ws = FakeOAuthHttpWs([])
    events_holder: list[str] = []

    async def dynamic_recv():
        if not events_holder:
            for _ in range(20):
                if fake_ws.sent:
                    break
                await asyncio.sleep(0)
            sent = json.loads(fake_ws.sent[0])
            cm = sent.get("client_metadata") or {}
            tm = json.loads(cm["x-codex-turn-metadata"])
            events_holder.extend(json.dumps(e) for e in [
                {"type": "response.created", "response": {"id": "resp_identity"}},
                {"type": "response.completed", "response": {
                    "id": "resp_identity",
                    "prompt_cache_key": sent["prompt_cache_key"],
                    "turn_id": tm["turn_id"],
                    "metadata": {"installation": cm["x-codex-installation-id"]},
                    "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                }},
            ])
        if events_holder:
            return events_holder.pop(0)
        import websockets
        raise websockets.ConnectionClosed(None, None)

    fake_ws.recv = dynamic_recv  # type: ignore[method-assign]
    monkeypatch.setattr(m["failover"].oauth_manager, "ensure_valid_token", fake_token)
    monkeypatch.setattr(m["failover"], "_connect_oauth_responses_ws", fake_connect)
    body = {
        "model": "test-model", "stream": False, "input": "hello",
        "prompt_cache_key": "shared-anchor",
        "client_metadata": {
            "x-codex-installation-id": "inst-real",
            "x-codex-window-id": "shared-anchor:0",
            "x-codex-turn-metadata": json.dumps({
                "prompt_cache_key": "shared-anchor",
                "turn_id": "turn-real",
                "window_id": "shared-anchor:0",
            }),
        },
    }
    resp, rid = await _call_failover_responses(m, ch, body)
    assert resp.status_code == 200
    sent = json.loads(fake_ws.sent[0])
    cm = sent["client_metadata"]
    tm = json.loads(cm["x-codex-turn-metadata"])
    assert sent["prompt_cache_key"] == captured["headers"]["session-id"]
    assert sent["prompt_cache_key"] != "shared-anchor"
    assert cm["x-codex-installation-id"] != "inst-real"
    assert cm["x-codex-window-id"] == f"{sent['prompt_cache_key']}:0"
    assert tm["prompt_cache_key"] == sent["prompt_cache_key"]
    assert tm["turn_id"] != "turn-real"
    assert tm["window_id"] == f"{sent['prompt_cache_key']}:0"
    assert "conversation_id" not in {k.lower(): v for k, v in captured["headers"].items()}

    obj = json.loads(resp.body)
    assert obj["prompt_cache_key"] == "shared-anchor"
    assert obj["turn_id"] == "turn-real"
    assert obj["metadata"]["installation"] == "inst-real"
    assert "shared-anchor" in (m["log_db"].log_detail(rid)["detail"].get("response_body") or "")


@pytest.mark.asyncio
async def test_responses_ws_oauth_pending_visible_identity_restored_before_downstream(monkeypatch, m):
    cfg = _setup(m)
    cfg["oauth"] = {"providers": {"openai": {"forceCodexCLI": True, "isolateSessionId": True}}}
    ch = m["OpenAIOAuthChannel"]({
        "email": "pending@example.com", "provider": "openai",
        "access_token": "tok", "refresh_token": "rt", "models": ["test-model"],
    })
    with m["registry"]._lock:
        m["registry"]._channels = {ch.key: ch}

    async def fake_token(account_key):
        return "tok"

    fake_upstream = FakeUpstreamWebSocket([])
    events_holder: list[str] = []

    async def dynamic_recv():
        if not events_holder:
            for _ in range(20):
                if fake_upstream.sent:
                    break
                await asyncio.sleep(0)
            sent = json.loads(fake_upstream.sent[0])
            tm = json.loads(sent["client_metadata"]["x-codex-turn-metadata"])
            events_holder.extend(json.dumps(e) for e in [
                {"type": "response.output_text.delta", "output_index": 0, "content_index": 0,
                 "delta": sent["prompt_cache_key"] + "|" + tm["turn_id"]},
                {"type": "response.completed", "response": {"id": "resp", "output": [], "usage": {}}},
            ])
        if events_holder:
            return events_holder.pop(0)
        import websockets
        raise websockets.ConnectionClosed(None, None)

    fake_upstream.recv = dynamic_recv  # type: ignore[method-assign]

    async def fake_connect(url, *, headers, connector, proxy_bytes, open_timeout, timing=None, round_timeouts=None):
        return fake_upstream

    monkeypatch.setattr(m["responses_ws"].oauth_manager, "ensure_valid_token", fake_token)
    monkeypatch.setattr(m["responses_ws"], "_connect_upstream_ws", fake_connect)
    ws = FakeWebSocket({
        "type": "response.create", "model": "test-model", "input": "hello", "stream": True,
        "prompt_cache_key": "raw-pck",
        "client_metadata": {"x-codex-turn-metadata": json.dumps({"prompt_cache_key": "raw-pck", "turn_id": "turn-raw"})},
    })
    await m["responses_ws"].handle_responses_ws(ws)  # type: ignore[arg-type]
    first_downstream = json.loads(ws.sent_texts[0])
    assert first_downstream["type"] == "response.output_text.delta"
    assert "raw-pck" in first_downstream["delta"]
    assert "turn-raw" in first_downstream["delta"]
    assert "003" not in first_downstream["delta"]  # guard against obvious isolated-session leakage
    assert not ws.close_calls

@pytest.mark.asyncio
async def test_http_responses_uses_oauth_ws_when_enabled_non_stream(monkeypatch, caplog, m):
    cfg = _setup(m)
    cfg.setdefault("openai", {})["responsesUpstreamWsForOAuth"] = True
    cfg.setdefault("oauth", {})["providers"] = {"openai": {"isolateSessionId": True, "forceCodexCLI": True}}
    ch = _make_oauth_channel_for_failover(m)

    async def fake_token(account_key):
        return "tok"

    fake_ws = FakeOAuthHttpWs([
        {"type": "response.created", "response": {"id": "resp_http_ws"}},
        {"type": "response.output_item.added", "output_index": 0, "item": {"type": "message", "content": []}},
        {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": "ok"},
        {"type": "response.completed", "response": {
            "id": "resp_http_ws",
            "output": [
                {"type": "compaction", "id": "cmp_ws_nonstream_out", "encrypted_content": "ws-nonstream-out-cipher"},
                {"type": "message", "content": [{"type": "output_text", "text": "ok"}]},
            ],
            "usage": {"input_tokens": 4, "output_tokens": 2, "input_tokens_details": {"cached_tokens": 1}},
        }},
    ])
    captured = {}

    async def fake_connect(url, *, headers, connector, proxy_bytes, open_timeout, timing=None, round_timeouts=None):
        captured["url"] = url
        captured["headers"] = dict(headers)
        return fake_ws

    monkeypatch.setattr(m["failover"].oauth_manager, "ensure_valid_token", fake_token)
    monkeypatch.setattr(m["failover"], "_connect_oauth_responses_ws", fake_connect)
    body = {"model": "test-model", "stream": False, "input": [
        {"type": "compaction", "id": "cmp_ws_nonstream_in", "encrypted_content": "ws-nonstream-in-cipher"},
        {"type": "message", "role": "user", "content": "hello"},
    ], "prompt_cache_key": "anchor"}
    resp, rid = await _call_failover_responses(
        m, ch, body, bound_channel_key=ch.key,
    )

    assert resp.status_code == 200
    assert json.loads(resp.body)["output"][1]["content"][0]["text"] == "ok"
    for value in (body, json.loads(resp.body)):
        ref = m["responses_ws"].compaction_owner.complete_refs(value)[0]
        assert m["state_db"].compaction_owner_load(ref.compaction_id, ref.content_digest)
    assert captured["url"] == "wss://chatgpt.com/backend-api/codex/responses"
    assert captured["headers"]["OpenAI-Beta"] == "responses_websockets=2026-02-06"
    assert captured["headers"]["session-id"] == captured["headers"]["thread-id"]
    sent = json.loads(fake_ws.sent[0])
    assert sent["type"] == "response.create"
    assert sent["store"] is False and sent["stream"] is True
    assert sent["input"] == body["input"]
    row = m["log_db"].log_detail(rid)["log"]
    assert row["status"] == "success"
    assert row["final_channel_key"] == ch.key
    assert row["http_status"] == 200
    assert row["upstream_protocol"] == "openai-responses"
    assert row["upstream_transport"] == "ws"
    assert row["input_tokens"] == 3
    assert row["cache_read_tokens"] == 1

    # Owner storage is auxiliary: DB failure cannot overturn this successful
    # non-stream response or its success log.
    monkeypatch.setattr(
        m["state_db"], "compaction_owner_upsert",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("owner db unavailable")),
    )
    fake_ws = FakeOAuthHttpWs([{"type": "response.completed", "response": {
        "id": "resp_db_failure", "output": [],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }}])
    failed_persist_body = {"model": "test-model", "stream": False, "input": [
        {"type": "compaction", "id": "cmp_db_failure_nonstream", "encrypted_content": "db-failure-nonstream"},
    ]}
    with caplog.at_level("WARNING", logger="src.openai.compaction_owner"):
        response, failure_rid = await _call_failover_responses(
            m, ch, failed_persist_body, bound_channel_key=ch.key,
        )
    assert response.status_code == 200
    assert m["log_db"].log_detail(failure_rid)["log"]["status"] == "success"
    assert any(
        r.message == "codex_compaction_owner_persist_failed"
        and r.path == "oauth_upstream_ws_non_stream"
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_http_responses_oauth_ws_stream_converts_frames_to_sse(monkeypatch, caplog, m):
    cfg = _setup(m)
    cfg.setdefault("openai", {})["responsesUpstreamWsForOAuth"] = True
    cfg.setdefault("oauth", {})["providers"] = {"openai": {"isolateSessionId": True, "forceCodexCLI": True}}
    ch = _make_oauth_channel_for_failover(m, name="stream@example.com")

    async def fake_token(account_key):
        return "tok"

    fake_ws = FakeOAuthHttpStreamWs([
        {"type": "response.created", "response": {"id": "resp_stream"}},
        {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": "ok"},
        {"type": "response.completed", "response": {
            "id": "resp_stream",
            "output": [
                {"type": "compaction", "id": "cmp_ws_stream_out", "encrypted_content": "ws-stream-out-cipher"},
                {"type": "message", "content": [{"type": "output_text", "text": "ok"}]},
            ],
            "usage": {"input_tokens": 2, "output_tokens": 1},
        }},
    ])

    async def fake_connect(url, *, headers, connector, proxy_bytes, open_timeout, timing=None, round_timeouts=None):
        return fake_ws

    monkeypatch.setattr(m["failover"].oauth_manager, "ensure_valid_token", fake_token)
    monkeypatch.setattr(m["failover"], "_connect_oauth_responses_ws", fake_connect)
    body = {"model": "test-model", "stream": True, "input": [
        {"type": "compaction", "id": "cmp_ws_stream_in", "encrypted_content": "ws-stream-in-cipher"},
        {"type": "message", "role": "user", "content": "hello"},
    ], "prompt_cache_key": "anchor"}
    stable_fp = "stable-oauth-ws-complete"
    old_owner = "oauth:old-owner"
    m["affinity"].upsert(stable_fp, old_owner, "test-model")
    resp, rid = await _call_failover_responses(
        m, ch, body, fp_query=stable_fp, bound_channel_key=ch.key,
    )
    assert resp.status_code == 200
    assert m["affinity"].get(stable_fp)["channel_key"] == old_owner
    text = b"".join([c async for c in resp.body_iterator]).decode("utf-8")
    assert "event: response.created" in text
    assert "event: response.output_text.delta" in text
    assert '"delta":"ok"' in text or '"delta": "ok"' in text
    assert "event: response.completed" in text
    assert fake_ws.closed is True
    assert m["affinity"].get(stable_fp)["channel_key"] == ch.key
    row = m["log_db"].log_detail(rid)["log"]
    assert row["status"] == "success"
    assert row["http_status"] == 200
    assert row["upstream_protocol"] == "openai-responses"
    assert row["upstream_transport"] == "ws"
    for value in (body, {"output": [{"type": "compaction", "id": "cmp_ws_stream_out", "encrypted_content": "ws-stream-out-cipher"}]}):
        ref = m["responses_ws"].compaction_owner.complete_refs(value)[0]
        assert m["state_db"].compaction_owner_load(ref.compaction_id, ref.content_digest)

    # Once stream bytes are delivered, owner DB failure remains observable but
    # cannot fabricate an error terminal or error log.
    monkeypatch.setattr(
        m["state_db"], "compaction_owner_upsert",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("owner db unavailable")),
    )
    fake_ws = FakeOAuthHttpStreamWs([{"type": "response.completed", "response": {
        "id": "resp_stream_db_failure", "output": [],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }}])
    failure_body = {"model": "test-model", "stream": True, "input": [
        {"type": "compaction", "id": "cmp_db_failure_stream", "encrypted_content": "db-failure-stream"},
    ]}
    with caplog.at_level("WARNING", logger="src.openai.compaction_owner"):
        response, failure_rid = await _call_failover_responses(
            m, ch, failure_body, bound_channel_key=ch.key,
        )
        stream_text = b"".join([chunk async for chunk in response.body_iterator]).decode()
    assert "response.completed" in stream_text
    assert m["log_db"].log_detail(failure_rid)["log"]["status"] == "success"
    assert any(
        r.message == "codex_compaction_owner_persist_failed"
        and r.path == "oauth_upstream_ws_stream_finalize"
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_http_responses_oauth_ws_truncation_does_not_rebind(monkeypatch, m):
    cfg = _setup(m)
    cfg.setdefault("openai", {})["responsesUpstreamWsForOAuth"] = True
    cfg.setdefault("oauth", {})["providers"] = {"openai": {"isolateSessionId": True, "forceCodexCLI": True}}
    ch = _make_oauth_channel_for_failover(m, name="truncated@example.com")

    async def fake_token(account_key):
        return "tok"

    fake_ws = FakeOAuthHttpStreamWs([
        {"type": "response.created", "response": {"id": "resp_truncated"}},
        {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": "partial"},
    ])

    async def fake_connect(url, *, headers, connector, proxy_bytes, open_timeout, timing=None, round_timeouts=None):
        return fake_ws

    monkeypatch.setattr(m["failover"].oauth_manager, "ensure_valid_token", fake_token)
    monkeypatch.setattr(m["failover"], "_connect_oauth_responses_ws", fake_connect)
    stable_fp = "stable-oauth-ws-truncated"
    old_owner = "oauth:old-owner"
    m["affinity"].upsert(stable_fp, old_owner, "test-model")
    body = {"model": "test-model", "stream": True, "input": "hello", "prompt_cache_key": "anchor"}
    resp, rid = await _call_failover_responses(
        m, ch, body, fp_query=stable_fp, bound_channel_key=old_owner,
    )
    assert resp.status_code == 200
    assert m["affinity"].get(stable_fp)["channel_key"] == old_owner

    text = b"".join([c async for c in resp.body_iterator]).decode("utf-8")
    assert "event: response.output_text.delta" in text
    assert "upstream websocket closed" in text
    assert m["affinity"].get(stable_fp)["channel_key"] == old_owner
    assert m["log_db"].log_detail(rid)["log"]["status"] == "error"


@pytest.mark.asyncio
async def test_http_responses_oauth_ws_invalid_replay_clears_scope_and_retries(monkeypatch, m):
    cfg = _setup(m)
    cfg.setdefault("openai", {})["responsesUpstreamWsForOAuth"] = True
    cfg.setdefault("oauth", {})["providers"] = {"openai": {"isolateSessionId": True, "forceCodexCLI": True}}
    ch = _make_oauth_channel_for_failover(m, name="replay-clear@example.com")
    rr = m["reasoning_replay"]
    encrypted_content = _valid_encrypted_content(11)
    rr.cache_items(
        "test-model",
        "prompt-cache:anchor",
        [{"type": "reasoning", "encrypted_content": encrypted_content}],
        account_key=ch.account_key,
    )

    async def fake_token(account_key):
        return "tok"

    bad_ws = FakeOAuthHttpWs([
        {"type": "error", "status": 400, "error": {
            "code": "invalid_encrypted_content",
            "message": (
                "The encrypted content gAAA... could not be verified. "
                "Reason: Encrypted content could not be decrypted or parsed."
            ),
        }},
    ])
    good_ws = FakeOAuthHttpWs([
        {"type": "response.created", "response": {"id": "resp_retry"}},
        {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": "ok"},
        {"type": "response.completed", "response": {
            "id": "resp_retry",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
            "usage": {"input_tokens": 2, "output_tokens": 1},
        }},
    ])
    attempts = [bad_ws, good_ws]

    async def fake_connect(url, *, headers, connector, proxy_bytes, open_timeout, timing=None, round_timeouts=None):
        return attempts.pop(0)

    monkeypatch.setattr(m["failover"].oauth_manager, "ensure_valid_token", fake_token)
    monkeypatch.setattr(m["failover"], "_connect_oauth_responses_ws", fake_connect)
    body = {"model": "test-model", "stream": False, "input": "continue", "prompt_cache_key": "anchor"}
    resp, rid = await _call_failover_responses(m, ch, body)

    assert resp.status_code == 200
    assert json.loads(resp.body)["output"][0]["content"][0]["text"] == "ok"
    assert rr.get("test-model", "prompt-cache:anchor", account_key=ch.account_key) == []
    assert attempts == []

    first_payload = json.loads(bad_ws.sent[0])
    second_payload = json.loads(good_ws.sent[0])
    assert first_payload["type"] == "response.create"
    first_input = first_payload["input"]
    assert [
        item.get("encrypted_content")
        for item in first_input
        if isinstance(item, dict) and item.get("type") == "reasoning"
    ] == [encrypted_content]
    assert [
        item
        for item in first_input
        if isinstance(item, dict)
        and item.get("type") == "message"
        and item.get("role") == "user"
    ] == [{"type": "message", "role": "user", "content": "continue"}]
    assert second_payload["type"] == "response.create"
    assert second_payload["input"] == [
        {"type": "message", "role": "user", "content": "continue"},
    ]

    detail = m["log_db"].log_detail(rid)
    assert detail["log"]["status"] == "success", detail["log"]
    assert detail["log"]["final_channel_key"] == ch.key
    assert [item["outcome"] for item in detail["retry_chain"]] == ["request_invalid", "success"]
    assert all(item["dispatched_at"] is not None for item in detail["retry_chain"])
    assert m["cooldown"].get_state(ch.key, "test-model") is None


@pytest.mark.asyncio
async def test_http_responses_oauth_ws_logs_first_packet_not_first_visible(monkeypatch, m):
    cfg = _setup(m)
    cfg.setdefault("openai", {})["responsesUpstreamWsForOAuth"] = True
    ch = _make_oauth_channel_for_failover(m, name="first-packet@example.com")

    async def fake_token(account_key):
        return "tok"

    class FakeClock:
        def __init__(self):
            self.value = 100.0

        def __call__(self):
            return self.value

        def advance(self, seconds):
            self.value += seconds

    clock = FakeClock()
    real_timing = m["failover"].WsAttemptTiming

    def timing_factory(**kwargs):
        return real_timing(clock=clock, wall_clock=clock, **kwargs)

    fake_ws = ClockedOAuthHttpStreamWs([
        {"type": "response.created", "response": {"id": "resp_first"}},
        {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": "ok"},
        {"type": "response.completed", "response": {
            "id": "resp_first",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
            "usage": {"input_tokens": 2, "output_tokens": 1},
        }},
    ], delays=[0.01, 0.15, 0.01], clock=clock)

    async def fake_connect(url, *, headers, connector, proxy_bytes, open_timeout, timing=None, round_timeouts=None):
        return fake_ws

    monkeypatch.setattr(m["failover"].oauth_manager, "ensure_valid_token", fake_token)
    monkeypatch.setattr(m["failover"], "WsAttemptTiming", timing_factory)
    monkeypatch.setattr(m["failover"], "_connect_oauth_responses_ws", fake_connect)
    body = {"model": "test-model", "stream": True, "input": "hello", "prompt_cache_key": "anchor"}
    resp, rid = await _call_failover_responses(m, ch, body)
    text = b"".join([c async for c in resp.body_iterator]).decode("utf-8")
    assert "event: response.created" in text
    assert "event: response.output_text.delta" in text
    first_ms = m["log_db"].log_detail(rid)["log"]["first_token_time_ms"]
    assert first_ms == 10


def test_failover_ss2022_ws_wrapper_close_waits_then_closes_owner_once(m):
    _setup(m)
    called = []

    class DummyWs:
        response = None
        async def close(self, *args, **kwargs):
            called.append("ws.close")
        async def wait_closed(self):
            called.append("ws.wait_closed")

    class DummyBridge:
        async def aclose(self, *, cause=None, direction="owner_close"):
            called.append(("bridge.aclose", cause, direction))

    async def run():
        wrapped = m["failover"]._ManagedWsConnection(DummyWs(), DummyBridge())
        await wrapped.close()
        await wrapped.close()

    asyncio.run(run())
    assert called == [
        "ws.close",
        "ws.wait_closed",
        ("bridge.aclose", None, "websocket_close"),
    ]


def test_failover_ss2022_cleanup_closes_socketpair_when_ws_connect_fails(monkeypatch, m):
    _setup(m)
    left, right = socket.socketpair()
    left.setblocking(False)
    right.setblocking(False)
    cleaned = []

    class FakeBridge:
        def handoff_socket(self):
            return left

        async def aclose(self, *, cause=None, direction="owner_close"):
            cleaned.append((type(cause).__name__, direction))
            right.close()

    async def fake_open(url, connector, proxy_bytes, *, timeout):
        return FakeBridge()

    async def fake_connect(*args, **kwargs):
        # websockets owns sock= once called; emulate that ownership with a real
        # asyncio transport and close/wait_closed before surfacing handshake failure.
        reader, writer = await asyncio.open_connection(sock=kwargs["sock"])
        del reader
        writer.close()
        await writer.wait_closed()
        raise RuntimeError("boom")

    monkeypatch.setattr(m["failover"], "_open_socket_via_ss2022", fake_open)
    monkeypatch.setattr(m["failover"].websockets, "connect", fake_connect)
    connector = m["failover"].SS2022Connector("dummy", "127.0.0.1", 1, "2022-blake3-aes-128-gcm", "AAAAAAAAAAAAAAAAAAAAAA")
    with pytest.raises(RuntimeError):
        timing = m["failover"].WsAttemptTiming(route_type="ss2022")
        asyncio.run(m["failover"]._connect_oauth_responses_ws(
            "wss://example.invalid/v1/responses",
            headers={}, connector=connector, proxy_bytes=m["failover"]._WsProxyBytes(), open_timeout=1,
            timing=timing,
            round_timeouts=m["failover"].RoundTimeouts(1, 1, 1, 2),
        ))
    assert cleaned == [("RuntimeError", "websocket_handshake_error")]
    assert left.fileno() == -1  # closed by asyncio transport, never by bridge
    assert right.fileno() == -1


@pytest.mark.asyncio
async def test_http_responses_oauth_ws_created_error_does_not_cross_candidate(monkeypatch, m):
    cfg = _setup(m)
    cfg.setdefault("openai", {})["responsesUpstreamWsForOAuth"] = True
    # This case isolates cross-transport candidate failover; transient same-candidate
    # retry behavior has dedicated coverage in test_protocol_fake_upstreams.py.
    cfg["retry"] = {"transient": {"enabled": False}}
    cfg.setdefault("oauth", {})["providers"] = {"openai": {"isolateSessionId": True, "forceCodexCLI": True}}
    oauth_ch = _make_oauth_channel_for_failover(m, name="failover@example.com")
    api_ch = m["OpenAIApiChannel"]({
        "name": "api-http", "type": "api", "baseUrl": "https://api.example",
        "apiKey": "api-key", "protocol": "openai-responses",
        "models": [{"alias": "test-model", "real": "real-model"}], "enabled": True,
    })
    with m["registry"]._lock:
        m["registry"]._channels = {oauth_ch.key: oauth_ch, api_ch.key: api_ch}

    async def fake_token(account_key):
        return "tok"

    bad_ws = FakeOAuthHttpWs([
        {"type": "response.created", "response": {"id": "bad"}},
        {"type": "error", "status": 503, "error": {"code": "server_error", "message": "boom"}},
    ])

    async def fake_connect(url, *, headers, connector, proxy_bytes, open_timeout, timing=None, round_timeouts=None):
        return bad_ws

    class MockStreamCtx:
        def __init__(self, resp): self.resp = resp
        async def __aenter__(self): return self.resp
        async def __aexit__(self, *args): await self.resp.aclose(); return False

    class MockClient:
        def stream(self, method, url, headers=None, content=None, timeout=None, extensions=None):
            # Accept the production HTTPcore trace extension without fabricating
            # connection timing in this protocol-failover fake.
            del extensions
            assert url == "https://api.example/v1/responses"
            resp = __import__("httpx").Response(200, content=(
                _response_sse_event("response.output_text.delta", {"type":"response.output_text.delta","output_index":0,"content_index":0,"delta":"ok"}) +
                _response_sse_event("response.completed", {"type":"response.completed","response":{"id":"resp_ok","output":[{"type":"message","content":[{"type":"output_text","text":"ok"}]}],"usage":{"input_tokens":1,"output_tokens":1}}})
            ), headers={"content-type":"text/event-stream"})
            return MockStreamCtx(resp)

    monkeypatch.setattr(m["failover"].oauth_manager, "ensure_valid_token", fake_token)
    monkeypatch.setattr(m["failover"], "_connect_oauth_responses_ws", fake_connect)
    monkeypatch.setattr(m["upstream"], "get_client", lambda: MockClient())

    from src.scheduler import ScheduleResult
    request_id = "http-ws-failover"
    start = time.time()
    body = {"model": "test-model", "stream": True, "input": "hello", "prompt_cache_key": "anchor"}
    await asyncio.to_thread(m["log_db"].insert_pending, request_id, "1.2.3.4", "ws-key", "test-model", True, 1, 0, {}, body, ingress_protocol="responses")
    sr = ScheduleResult(candidates=[(oauth_ch, "test-model"), (api_ch, "real-model")], saturated=[], affinity_hit=False, fp_query=None, client_key="client:1")
    resp = await m["failover"].run_failover(sr, body, request_id, "ws-key", "1.2.3.4", is_stream=True, start_time=start, ingress_protocol="responses")
    text = b"".join([c async for c in resp.body_iterator]).decode("utf-8")
    assert "event: response.created" in text
    assert "boom" in text
    assert "event: response.output_text.delta" not in text
    detail = m["log_db"].log_detail(request_id)
    assert detail["log"]["status"] == "error"
    assert detail["log"]["final_channel_key"] == oauth_ch.key
    outcomes = [r["outcome"] for r in detail["retry_chain"]]
    assert outcomes == ["upstream_error_json"]


@pytest.mark.asyncio
async def test_http_responses_oauth_ws_error_after_visible_does_not_failover(monkeypatch, m):
    cfg = _setup(m)
    cfg.setdefault("openai", {})["responsesUpstreamWsForOAuth"] = True
    cfg["errorWindows"] = [1, 0]
    cfg.setdefault("oauth", {})["providers"] = {"openai": {"isolateSessionId": True, "forceCodexCLI": True}}
    ch = _make_oauth_channel_for_failover(m, name="after-visible@example.com")

    async def fake_token(account_key):
        return "tok"

    fake_ws = FakeOAuthHttpStreamWs([
        {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": "partial"},
        {"type": "response.failed", "response": {"id": "resp", "error": {"code": "boom", "message": "failed later"}, "usage": {}}},
    ])

    async def fake_connect(url, *, headers, connector, proxy_bytes, open_timeout, timing=None, round_timeouts=None):
        return fake_ws

    monkeypatch.setattr(m["failover"].oauth_manager, "ensure_valid_token", fake_token)
    monkeypatch.setattr(m["failover"], "_connect_oauth_responses_ws", fake_connect)
    body = {"model": "test-model", "stream": True, "input": "hello", "prompt_cache_key": "anchor"}
    resp, rid = await _call_failover_responses(m, ch, body)
    text = b"".join([c async for c in resp.body_iterator]).decode("utf-8")
    assert "partial" in text
    assert "failed later" in text
    row = m["log_db"].log_detail(rid)["log"]
    assert row["status"] == "error"
    assert row["final_channel_key"] == ch.key
    assert row["upstream_protocol"] == "openai-responses"
    assert row["upstream_transport"] == "ws"
    assert "failed later" in row["error_message"]
    assert m["cooldown"].get_state(ch.key, "test-model") is not None


@pytest.mark.asyncio
async def test_http_responses_oauth_ws_consumes_codex_rate_limits_event(monkeypatch, m):
    cfg = _setup(m)
    cfg.setdefault("openai", {})["responsesUpstreamWsForOAuth"] = True
    ch = _make_oauth_channel_for_failover(m, name="quota@example.com")

    async def fake_token(account_key):
        return "tok"

    fake_ws = FakeOAuthHttpStreamWs([
        {
            "type": "codex.rate_limits",
            "primary_used_pct": 42.5,
            "primary_reset_sec": 60,
            "primary_window_min": 300,
        },
        {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": "ok"},
        {"type": "response.completed", "response": {
            "id": "resp_quota",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
            "usage": {"input_tokens": 2, "output_tokens": 1},
        }},
    ])

    async def fake_connect(url, *, headers, connector, proxy_bytes, open_timeout, timing=None, round_timeouts=None):
        return fake_ws

    monkeypatch.setattr(m["failover"].oauth_manager, "ensure_valid_token", fake_token)
    monkeypatch.setattr(m["failover"], "_connect_oauth_responses_ws", fake_connect)
    body = {"model": "test-model", "stream": True, "input": "hello", "prompt_cache_key": "anchor"}
    resp, rid = await _call_failover_responses(m, ch, body)
    text = b"".join([c async for c in resp.body_iterator]).decode("utf-8")

    assert "codex.rate_limits" not in text
    assert "event: response.output_text.delta" in text
    row = m["state_db"].quota_load(ch.account_key)
    assert row["codex_primary_used_pct"] == 42.5
    assert m["log_db"].log_detail(rid)["log"]["upstream_transport"] == "ws"


@pytest.mark.asyncio
async def test_responses_ws_pre_visible_context_error_preserves_explicit_zero_usage(
    monkeypatch, m,
):
    cfg = _setup(m)
    cfg["retry"] = {"transient": {"enabled": False}}
    _make_channel(m)
    ws = FakeWebSocket({
        "type": "response.create", "model": "test-model",
        "input": "hello", "stream": True,
    })
    fake_upstream = FakeUpstreamWebSocket([
        {"type": "response.created", "response": {"id": "resp_zero"}},
        {"type": "response.incomplete", "response": {
            "id": "resp_zero",
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [],
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }},
    ])

    async def fake_connect(*args, **kwargs):
        return fake_upstream

    monkeypatch.setattr(m["responses_ws"], "_connect_upstream_ws", fake_connect)
    await m["responses_ws"].handle_responses_ws(ws)  # type: ignore[arg-type]

    row = _last_request_log(m)
    assert row["status"] == "error"
    assert row["http_status"] == 400
    error_frame = next(
        json.loads(text) for text in ws.sent_texts
        if json.loads(text).get("type") == "error"
    )
    assert error_frame["status"] == 400
    assert error_frame["error"] == {
        "type": "invalid_request_error",
        "code": "context_length_exceeded",
        "message": error_frame["message"],
    }
    assert m["responses_ws"]._parse_wrapped_ws_error(
        json.dumps(error_frame)
    )["status"] == 400
    assert ws.close_calls[-1][0] == 4400
    detail = m["log_db"].log_detail(row["request_id"])
    assert "response.incomplete" in (detail["detail"].get("response_body") or "")
    attempts = _attempt_usage(m, row["request_id"])
    assert len(attempts) == 1
    assert attempts[0]["outcome"] == "request_invalid"
    assert attempts[0]["usage_observed"] == 1
    assert attempts[0]["input_tokens"] == attempts[0]["output_tokens"] == 0


@pytest.mark.parametrize("usage", [
    {"input_tokens": -1, "output_tokens": 2},
    {"input_tokens": "bad", "output_tokens": 2},
    {"input_tokens": 1 << 70, "output_tokens": 2},
])
@pytest.mark.asyncio
async def test_responses_ws_malformed_usage_is_not_logged_as_valid_tokens(
    monkeypatch, m, usage,
):
    _setup(m)
    _make_channel(m)
    ws = FakeWebSocket({
        "type": "response.create", "model": "test-model",
        "input": "hello", "stream": True,
    })
    fake_upstream = FakeUpstreamWebSocket([
        {"type": "response.output_text.delta", "output_index": 0,
         "content_index": 0, "delta": "ok"},
        {"type": "response.completed", "response": {
            "id": "resp_bad_usage", "output": [], "usage": usage,
        }},
    ])

    async def fake_connect(*args, **kwargs):
        return fake_upstream

    monkeypatch.setattr(m["responses_ws"], "_connect_upstream_ws", fake_connect)
    await m["responses_ws"].handle_responses_ws(ws)  # type: ignore[arg-type]

    row = _last_request_log(m)
    assert row["status"] == "success"
    assert row["usage_observed"] == 0
    assert row["input_tokens"] == row["output_tokens"] == 0
    attempts = _attempt_usage(m, row["request_id"])
    assert len(attempts) == 1
    assert attempts[0]["usage_observed"] == 0
    assert attempts[0]["cost_source"] == "unpriced"


@pytest.mark.asyncio
async def test_responses_ws_large_terminal_frame_keeps_billing_evidence(
    monkeypatch, m,
):
    cfg = _setup(m)
    ch = _make_channel(m)
    cfg["pricing"] = {
        "enabled": True,
        "channelProviders": {ch.key: "xai"},
    }
    cfg["modelBindings"] = {
        "defaults": {},
        "scoped": {
            ch.key: {
                "test-model": {
                    "target": "xai/grok-4.5",
                    "outboundModel": "real-model",
                    "source": "test",
                },
            },
        },
    }
    ws = FakeWebSocket({
        "type": "response.create", "model": "test-model",
        "input": "hello", "stream": True,
    })
    huge_text = "x" * 210_000
    fake_upstream = FakeUpstreamWebSocket([
        {"type": "response.output_text.delta", "output_index": 0,
         "content_index": 0, "delta": "ok"},
        {"type": "response.completed", "response": {
            "id": "resp_large_zero",
            # Billing fields deliberately precede a huge output so a naive tail
            # slice would remove them from the persisted response body.
            "service_tier": "priority",
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_in_usd_ticks": 4321,
            },
            "output": [{
                "type": "message",
                "content": [{"type": "output_text", "text": huge_text}],
            }],
        }},
    ])

    async def fake_connect(*args, **kwargs):
        return fake_upstream

    monkeypatch.setattr(m["responses_ws"], "_connect_upstream_ws", fake_connect)
    await m["responses_ws"].handle_responses_ws(ws)  # type: ignore[arg-type]

    row = _last_request_log(m)
    assert row["status"] == "success"
    assert row["usage_observed"] == 1
    assert row["actual_service_tier"] == "priority"
    detail_body = m["log_db"].log_detail(row["request_id"])["detail"]["response_body"]
    assert len(detail_body) == 200_000
    assert not detail_body.startswith("{")
    assert "_parrot_truncated_billing_evidence" in detail_body
    attempts = _attempt_usage(m, row["request_id"])
    assert len(attempts) == 1
    assert attempts[0]["usage_observed"] == 1
    assert attempts[0]["input_tokens"] == attempts[0]["output_tokens"] == 0
    assert attempts[0]["service_tier"] == "priority"
    assert attempts[0]["cost_source"] == "actual"
    assert attempts[0]["cost_ticks"] == 4321


@pytest.mark.asyncio
async def test_responses_ws_rejects_overlapping_create_without_stopping_active_turn(
    monkeypatch, m,
):
    _setup(m)
    _make_channel(m)
    second_create = {
        "type": "response.create", "model": "test-model",
        "input": "second", "stream": True,
    }
    ws = FakeWebSocket(
        {
            "type": "response.create", "model": "test-model",
            "input": "first", "stream": True,
        },
        extra_receive=[{
            "type": "websocket.receive", "text": json.dumps(second_create),
        }],
    )

    class BlockingUpstream:
        def __init__(self):
            self.sent: list[str] = []
            self.response = SimpleNamespace(headers={})
            self._recv_count = 0

        async def send(self, data, text=None):
            del text
            if isinstance(data, bytes):
                data = data.decode("utf-8")
            self.sent.append(data)

        async def recv(self):
            if self._recv_count == 0:
                self._recv_count += 1
                return json.dumps({
                    "type": "response.output_text.delta",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": "partial",
                })
            await asyncio.sleep(0.01)
            return json.dumps({
                "type": "response.completed",
                "response": {
                    "id": "first-completes", "output": [],
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            })

        async def close(self, *args, **kwargs):
            return None

    fake_upstream = BlockingUpstream()

    async def fake_connect(*args, **kwargs):
        return fake_upstream

    monkeypatch.setattr(m["responses_ws"], "_connect_upstream_ws", fake_connect)
    await m["responses_ws"].handle_responses_ws(ws)  # type: ignore[arg-type]

    assert len(fake_upstream.sent) == 1
    assert json.loads(fake_upstream.sent[0])["input"] == "first"
    assert any(
        json.loads(text).get("code") == "invalid_request_error"
        for text in ws.sent_texts
    )
    assert not ws.close_calls
    row = _last_request_log(m)
    assert row["status"] == "success"
    assert len(_retry_chain(m, row["request_id"])) == 1
    attempts = _attempt_usage(m, row["request_id"])
    assert len(attempts) == 1
    assert attempts[0]["outcome"] == "success"


@pytest.mark.asyncio
async def test_oauth_responses_ws_warmup_image_and_incremental_previous_id_are_wire_exact(
    monkeypatch, m,
):
    """Codex v2 warmup/full context is sent once; the next frame is delta-only."""
    cfg = _setup(m)
    cfg["retry"] = {"transient": {"enabled": False}}
    cfg["openai"] = {"store": {"enabled": False}}
    ch = _make_oauth_channel_for_failover(m, name="ws-v2@example.com")

    async def fake_token(_account_key):
        return "tok"

    data_url = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB"
    full_input = [{
        "type": "message",
        "role": "user",
        "content": [
            {"type": "input_text", "text": "inspect the image"},
            {"type": "input_image", "image_url": data_url},
        ],
    }]
    delta_input = [{
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "now answer briefly"}],
    }]
    ws = SequentialFakeWebSocket(
        {
            "type": "response.create",
            "model": "test-model",
            "input": full_input,
            "generate": False,
            "stream": True,
        },
        {
            "type": "response.create",
            "model": "test-model",
            "previous_response_id": "resp_warmup",
            "input": delta_input,
            "stream": True,
        },
    )
    fake_upstream = FakeUpstreamWebSocket([
        {"type": "response.incomplete", "response": {
            "id": "resp_warmup",
            "status": "incomplete",
            "incomplete_details": {"reason": "generate_false"},
            "output": [],
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }},
        {"type": "response.completed", "response": {
            "id": "resp_final",
            "output": [],
            "usage": {"input_tokens": 3, "output_tokens": 1},
        }},
    ])

    async def fake_connect(*args, **kwargs):
        return fake_upstream

    monkeypatch.setattr(m["responses_ws"].oauth_manager, "ensure_valid_token", fake_token)
    monkeypatch.setattr(m["responses_ws"], "_connect_upstream_ws", fake_connect)
    await m["responses_ws"].handle_responses_ws(ws)  # type: ignore[arg-type]

    assert len(fake_upstream.sent) == 2
    warmup_wire, delta_wire = map(json.loads, fake_upstream.sent)
    assert warmup_wire["generate"] is False
    assert warmup_wire["input"] == full_input
    assert delta_wire["previous_response_id"] == "resp_warmup"
    assert delta_wire["input"] == delta_input
    assert data_url in json.dumps(warmup_wire)
    assert data_url not in json.dumps(delta_wire)
    assert sum(frame.count(data_url) for frame in fake_upstream.sent) == 1
    assert m["cooldown"].get_state(ch.key, "test-model") is None


@pytest.mark.parametrize("policy_code", [
    "invalid_prompt", "bio_policy", "cyber_policy",
    "misalignment_policy_violation",
])
@pytest.mark.asyncio
async def test_native_response_failed_request_policy_is_health_neutral_and_not_replayed(
    monkeypatch, m, policy_code,
):
    cfg = _setup(m)
    cfg["retry"] = {"transient": {"enabled": False}}
    oauth_ch = _make_oauth_channel_for_failover(
        m, name=f"policy-{policy_code}@example.com",
    )
    api_ch = m["OpenAIApiChannel"]({
        "name": f"policy-fallback-{policy_code}",
        "type": "api",
        "baseUrl": "https://must-not-dispatch.example",
        "apiKey": "unused",
        "protocol": "openai-responses",
        "models": [{"alias": "test-model", "real": "real-model"}],
        "enabled": True,
    })
    with m["registry"]._lock:
        m["registry"]._channels = {oauth_ch.key: oauth_ch, api_ch.key: api_ch}

    async def fake_token(_account_key):
        return "tok"

    failed_event = {
        "type": "response.failed",
        "response": {
            "id": f"resp_{policy_code}",
            "status": "failed",
            "error": {
                "type": "invalid_request_error",
                "code": policy_code,
                "message": f"blocked by {policy_code}",
            },
            "usage": {"input_tokens": 2, "output_tokens": 0},
        },
    }
    upstream = FakeUpstreamWebSocket([failed_event])
    connect_calls = []

    async def fake_connect(url, **kwargs):
        connect_calls.append(url)
        if len(connect_calls) > 1:
            raise AssertionError("request/policy failure must not reach another candidate")
        return upstream

    ws = SequentialFakeWebSocket({
        "type": "response.create", "model": "test-model", "input": "hello", "stream": True,
    })
    await ws.accept()
    await ws.receive()  # handler normally consumes the first create before _run_ws_failover
    request_id = f"responses-ws-policy-{policy_code}"
    body = {"model": "test-model", "input": "hello", "stream": True}
    started_at = time.time()
    started_monotonic = time.monotonic()
    m["log_db"].insert_pending(
        request_id, "1.2.3.4", "ws-key", "test-model", True,
        1, 0, {}, body, ingress_protocol="responses_ws",
    )
    from src.scheduler import ScheduleResult
    route = ScheduleResult(
        candidates=[(oauth_ch, "test-model"), (api_ch, "real-model")],
        saturated=[], affinity_hit=False, fp_query=None, client_key="client:1",
    )
    monkeypatch.setattr(m["responses_ws"].oauth_manager, "ensure_valid_token", fake_token)
    monkeypatch.setattr(m["responses_ws"], "_connect_upstream_ws", fake_connect)
    await m["responses_ws"]._run_ws_failover(
        ws,
        first_obj={"type": "response.create", **body},
        schedule_result=route,
        body=body,
        request_id=request_id,
        api_key_name="ws-key",
        client_ip="1.2.3.4",
        start_time=started_at,
        start_monotonic=started_monotonic,
        fp_query=None,
    )

    assert len(connect_calls) == 1
    assert failed_event in [json.loads(text) for text in ws.sent_texts]
    row = _last_request_log(m)
    assert row["status"] == "error"
    assert row["http_status"] == 400
    assert _attempt_usage(m, request_id)[0]["outcome"] == "request_rejected"
    assert m["cooldown"].get_state(oauth_ch.key, "test-model") is None
    assert m["scorer"].get_stats(oauth_ch.key, "test-model") is None


@pytest.mark.parametrize("reason", ["max_output_tokens", "max_tokens", "generate_false"])
@pytest.mark.asyncio
async def test_native_oauth_responses_ws_relays_incomplete_but_settles_as_failure(
    monkeypatch, m, reason,
):
    _setup(m)
    ch = _make_oauth_channel_for_failover(m, name="incomplete@example.com")

    async def fake_token(_account_key):
        return "tok"

    incomplete_event = {
        "type": "response.incomplete",
        "response": {
            "id": "resp_max_output",
            "status": "incomplete",
            "incomplete_details": {"reason": reason},
            "output": [],
            "usage": {"input_tokens": 4, "output_tokens": 2},
        },
    }
    ws = FakeWebSocket({
        "type": "response.create",
        "model": "test-model",
        "input": "write a bounded answer",
        "stream": True,
    })
    fake_upstream = FakeUpstreamWebSocket([incomplete_event])

    async def fake_connect(*args, **kwargs):
        return fake_upstream

    success_artifacts = []
    monkeypatch.setattr(m["responses_ws"].oauth_manager, "ensure_valid_token", fake_token)
    monkeypatch.setattr(m["responses_ws"], "_connect_upstream_ws", fake_connect)
    monkeypatch.setattr(
        m["responses_ws"], "_write_responses_affinity",
        lambda **kwargs: success_artifacts.append(("affinity", kwargs)),
    )
    monkeypatch.setattr(
        m["responses_ws"].compaction_owner, "persist_observed_safe",
        lambda *args, **kwargs: success_artifacts.append(("compaction", kwargs)),
    )
    await m["responses_ws"].handle_responses_ws(ws)  # type: ignore[arg-type]

    downstream_events = [json.loads(text) for text in ws.sent_texts]
    assert incomplete_event in downstream_events
    assert all("context_length_exceeded" not in text for text in ws.sent_texts)
    row = _last_request_log(m)
    assert row["status"] == "error"
    assert row["http_status"] == 400
    assert row["usage_observed"] == 1
    attempts = _attempt_usage(m, row["request_id"])
    assert len(attempts) == 1
    assert attempts[0]["outcome"] == "response_incomplete"
    assert attempts[0]["usage_observed"] == 1
    assert attempts[0]["input_tokens"] == 4
    assert attempts[0]["output_tokens"] == 2
    assert success_artifacts == []
    assert m["cooldown"].get_state(ch.key, "test-model") is None
    assert m["scorer"].get_stats(ch.key, "test-model") is None
    assert not ws.close_calls


@pytest.mark.asyncio
async def test_responses_ws_accepts_sequential_creates_with_one_ledger_row_each(
    monkeypatch, m,
):
    _setup(m)
    _make_channel(m)
    conn = m["log_db"]._get_conn()
    request_id_before = conn.execute(
        "SELECT COALESCE(MAX(id), 0) FROM request_log"
    ).fetchone()[0]
    retry_id_before = conn.execute(
        "SELECT COALESCE(MAX(id), 0) FROM retry_chain"
    ).fetchone()[0]
    attempt_id_before = conn.execute(
        "SELECT COALESCE(MAX(id), 0) FROM upstream_attempt_usage"
    ).fetchone()[0]
    overall_before = m["log_db"].stats_summary(
        0, summary_top_limit=0
    )["overall"]
    ws = SequentialFakeWebSocket(
        {
            "type": "response.create", "model": "test-model",
            "input": "first", "stream": True,
        },
        {
            "type": "response.create", "model": "test-model",
            "previous_response_id": "resp_first",
            "input": "second", "stream": True,
        },
    )
    fake_upstream = FakeUpstreamWebSocket([
        {"type": "response.created", "response": {"id": "resp_first"}},
        {"type": "response.output_text.delta", "output_index": 0,
         "content_index": 0, "delta": "one"},
        {"type": "response.completed", "response": {
            "id": "resp_first", "output": [],
            "usage": {"input_tokens": 3, "output_tokens": 2},
        }},
        {"type": "response.created", "response": {"id": "resp_second"}},
        {"type": "response.output_text.delta", "output_index": 0,
         "content_index": 0, "delta": "two"},
        {"type": "response.completed", "response": {
            "id": "resp_second", "output": [],
            "usage": {"input_tokens": 5, "output_tokens": 4},
        }},
    ])

    async def fake_connect(*args, **kwargs):
        return fake_upstream

    monkeypatch.setattr(m["responses_ws"], "_connect_upstream_ws", fake_connect)
    await m["responses_ws"].handle_responses_ws(ws)  # type: ignore[arg-type]

    assert len(fake_upstream.sent) == 2
    first_wire, second_wire = map(json.loads, fake_upstream.sent)
    assert first_wire["model"] == second_wire["model"] == "real-model"
    assert second_wire["previous_response_id"] == "resp_first"
    rows = conn.execute(
        "SELECT * FROM request_log WHERE id>? ORDER BY id",
        (request_id_before,),
    ).fetchall()
    chain = conn.execute(
        "SELECT * FROM retry_chain WHERE id>? ORDER BY id",
        (retry_id_before,),
    ).fetchall()
    attempts = conn.execute(
        "SELECT * FROM upstream_attempt_usage WHERE id>? ORDER BY id",
        (attempt_id_before,),
    ).fetchall()
    assert len(rows) == len(chain) == len(attempts) == 2
    assert [row["status"] for row in rows] == ["success", "success"]
    assert [
        (row["input_tokens"], row["output_tokens"]) for row in rows
    ] == [(3, 2), (5, 4)]
    assert [a["outcome"] for a in attempts] == ["success", "success"]
    assert [a["upstream_protocol"] for a in attempts] == [
        "openai-responses", "openai-responses",
    ]
    assert [
        (a["input_tokens"], a["output_tokens"]) for a in attempts
    ] == [(3, 2), (5, 4)]
    overall = m["log_db"].stats_summary(0, summary_top_limit=0)["overall"]
    assert overall["total"] - overall_before["total"] == 2
    assert (
        overall["total_input_tokens"] - overall_before["total_input_tokens"],
        overall["total_output_tokens"] - overall_before["total_output_tokens"],
    ) == (8, 6)
    assert not ws.close_calls


@pytest.mark.asyncio
async def test_responses_ws_releases_capacity_while_waiting_between_turns(
    monkeypatch, m,
):
    cfg = _setup(m)
    cfg["apiKeyConcurrency"] = {
        "enabled": True,
        "defaultMaxConcurrent": 1,
        "defaultMaxQueue": 0,
        "defaultQueueWaitSeconds": 0,
    }
    cfg["concurrency"].update({
        "enabled": True,
        "defaultMaxConcurrent": 1,
        "queueWaitSeconds": 1,
    })
    ch = _make_channel(m, extra={"maxConcurrent": 1})
    ws = IdleBetweenTurnsFakeWebSocket({
        "type": "response.create", "model": "test-model",
        "input": "first", "stream": True,
    })
    fake_upstream = FakeUpstreamWebSocket([
        {"type": "response.completed", "response": {
            "id": "resp_first", "output": [],
            "usage": {"input_tokens": 3, "output_tokens": 2},
        }},
    ])

    async def fake_connect(*args, **kwargs):
        return fake_upstream

    monkeypatch.setattr(m["responses_ws"], "_connect_upstream_ws", fake_connect)
    handler_task = asyncio.create_task(
        m["responses_ws"].handle_responses_ws(ws)  # type: ignore[arg-type]
    )
    try:
        await asyncio.wait_for(ws.waiting_between_turns.wait(), timeout=2)
        assert not handler_task.done()
        assert m["apikey_limiter"].key_snapshot("ws-key")["in_flight"] == 0
        channel_rows = {
            row["channel_key"]: row
            for row in m["concurrency"].snapshot()
        }
        assert channel_rows[ch.key]["in_flight"] == 0
    finally:
        ws.disconnect_between_turns.set()
        await asyncio.wait_for(handler_task, timeout=2)


@pytest.mark.asyncio
async def test_responses_ws_translates_and_reacquires_capacity_for_each_turn(
    monkeypatch, m,
):
    _setup(m)
    ch = _make_channel(m)
    translated_inputs: list[str] = []
    api_key_acquires: list[str] = []
    channel_acquires: list[str] = []

    original_api_key_acquire = m["apikey_limiter"].acquire
    original_channel_try_acquire = m["concurrency"].try_acquire

    async def tracked_api_key_acquire(key_name, *args, **kwargs):
        api_key_acquires.append(str(key_name or ""))
        return await original_api_key_acquire(key_name, *args, **kwargs)

    async def tracked_channel_try_acquire(channel_key):
        channel_acquires.append(channel_key)
        return await original_channel_try_acquire(channel_key)

    async def fake_translate(body, *, ingress_protocol, route=None):
        assert ingress_protocol == "responses"
        translated_inputs.append(body["input"])
        translated = dict(body)
        translated["input"] = f"translated:{body['input']}"
        return translated

    monkeypatch.setattr(
        m["responses_ws"].apikey_limiter, "acquire", tracked_api_key_acquire,
    )
    monkeypatch.setattr(
        m["responses_ws"].concurrency, "try_acquire", tracked_channel_try_acquire,
    )
    monkeypatch.setattr(
        m["responses_ws"].translation, "translate_body", fake_translate,
    )

    ws = SequentialFakeWebSocket(
        {
            "type": "response.create", "model": "test-model",
            "input": "first", "stream": True,
        },
        {
            "type": "response.create", "model": "test-model",
            "input": "second", "stream": True,
        },
    )
    fake_upstream = FakeUpstreamWebSocket([
        {"type": "response.completed", "response": {
            "id": "resp_first", "output": [],
            "usage": {"input_tokens": 2, "output_tokens": 1},
        }},
        {"type": "response.completed", "response": {
            "id": "resp_second", "output": [],
            "usage": {"input_tokens": 3, "output_tokens": 1},
        }},
    ])

    async def fake_connect(*args, **kwargs):
        return fake_upstream

    monkeypatch.setattr(m["responses_ws"], "_connect_upstream_ws", fake_connect)
    await m["responses_ws"].handle_responses_ws(ws)  # type: ignore[arg-type]

    assert translated_inputs == ["first", "second"]
    assert [json.loads(payload)["input"] for payload in fake_upstream.sent] == [
        "translated:first", "translated:second",
    ]
    assert api_key_acquires == ["ws-key", "ws-key"]
    assert channel_acquires == [ch.key, ch.key]
    assert m["apikey_limiter"].key_snapshot("ws-key")["in_flight"] == 0
    channel_rows = {
        row["channel_key"]: row for row in m["concurrency"].snapshot()
    }
    assert channel_rows[ch.key]["in_flight"] == 0


@pytest.mark.asyncio
async def test_responses_ws_sequential_turn_can_change_to_supported_model(
    monkeypatch, m,
):
    cfg = _setup(m)
    cfg["apiKeys"]["ws-key"]["allowedModels"].append("other-model")
    _make_channel(m, extra={"models": [
        {"alias": "test-model", "real": "real-model"},
        {"alias": "other-model", "real": "other-real-model"},
    ]})
    conn = m["log_db"]._get_conn()
    request_id_before = conn.execute(
        "SELECT COALESCE(MAX(id), 0) FROM request_log"
    ).fetchone()[0]
    retry_id_before = conn.execute(
        "SELECT COALESCE(MAX(id), 0) FROM retry_chain"
    ).fetchone()[0]
    ws = SequentialFakeWebSocket(
        {
            "type": "response.create", "model": "test-model",
            "input": "first", "stream": True,
        },
        {
            "type": "response.create", "model": "other-model",
            "input": "second", "stream": True,
        },
    )
    fake_upstream = FakeUpstreamWebSocket([
        {"type": "response.completed", "response": {
            "id": "resp_first", "output": [],
            "usage": {"input_tokens": 2, "output_tokens": 1},
        }},
        {"type": "response.completed", "response": {
            "id": "resp_second", "output": [],
            "usage": {"input_tokens": 3, "output_tokens": 1},
        }},
    ])

    async def fake_connect(*args, **kwargs):
        return fake_upstream

    monkeypatch.setattr(m["responses_ws"], "_connect_upstream_ws", fake_connect)
    await m["responses_ws"].handle_responses_ws(ws)  # type: ignore[arg-type]

    assert [json.loads(payload)["model"] for payload in fake_upstream.sent] == [
        "real-model", "other-real-model",
    ]
    rows = conn.execute(
        "SELECT requested_model, final_model FROM request_log "
        "WHERE id>? ORDER BY id",
        (request_id_before,),
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("test-model", "real-model"),
        ("other-model", "other-real-model"),
    ]
    retry_rows = conn.execute(
        "SELECT model, client_visible_model FROM retry_chain "
        "WHERE id>? ORDER BY id",
        (retry_id_before,),
    ).fetchall()
    assert [tuple(row) for row in retry_rows] == [
        ("real-model", "test-model"),
        ("other-real-model", "other-model"),
    ]


@pytest.mark.asyncio
async def test_responses_ws_failed_turn_can_continue_sequentially(
    monkeypatch, m,
):
    _setup(m)
    _make_channel(m)
    conn = m["log_db"]._get_conn()
    request_id_before = conn.execute(
        "SELECT COALESCE(MAX(id), 0) FROM request_log"
    ).fetchone()[0]
    attempt_id_before = conn.execute(
        "SELECT COALESCE(MAX(id), 0) FROM upstream_attempt_usage"
    ).fetchone()[0]
    ws = SequentialFakeWebSocket(
        {
            "type": "response.create", "model": "test-model",
            "input": "first", "stream": True,
        },
        {
            "type": "response.create", "model": "test-model",
            "input": "recover", "stream": True,
        },
    )
    fake_upstream = FakeUpstreamWebSocket([
        {"type": "response.failed", "response": {
            "id": "resp_failed",
            "error": {"code": "server_error", "message": "turn failed"},
            "usage": {"input_tokens": 2, "output_tokens": 0},
        }},
        {"type": "response.completed", "response": {
            "id": "resp_recovered", "output": [],
            "usage": {"input_tokens": 3, "output_tokens": 1},
        }},
    ])

    async def fake_connect(*args, **kwargs):
        return fake_upstream

    monkeypatch.setattr(m["responses_ws"], "_connect_upstream_ws", fake_connect)
    await m["responses_ws"].handle_responses_ws(ws)  # type: ignore[arg-type]

    rows = conn.execute(
        "SELECT * FROM request_log WHERE id>? ORDER BY id",
        (request_id_before,),
    ).fetchall()
    attempts = conn.execute(
        "SELECT * FROM upstream_attempt_usage WHERE id>? ORDER BY id",
        (attempt_id_before,),
    ).fetchall()
    assert [row["status"] for row in rows] == ["error", "success"]
    assert len(fake_upstream.sent) == len(attempts) == 2
    assert [a["outcome"] for a in attempts] == [
        "stream_upstream_error", "success",
    ]
    assert [
        (a["input_tokens"], a["output_tokens"]) for a in attempts
    ] == [(2, 0), (3, 1)]
    assert not ws.close_calls


@pytest.mark.asyncio
async def test_responses_ws_close_1009_is_request_too_large_and_safe(m):
    _setup(m)
    from websockets.exceptions import ConnectionClosed
    from websockets.frames import Close
    from src.transports.ws_runtime import (
        read_until_first_responses_ws_visible_event,
    )

    class TooLargeUpstream(FakeUpstreamWebSocket):
        async def recv(self):
            raise ConnectionClosed(Close(1009, "secret upstream reason"), None)

    result = await read_until_first_responses_ws_visible_event(
        TooLargeUpstream([]),
        m["responses_ws"]._WsTracker(),
        channel_key="api:ws-upstream",
        deadline_ts=0,
        first_wait=5,
        idle_timeout=5,
    )

    assert result.outcome == "request_invalid"
    assert result.http_status == 413
    assert result.error_code == "message_too_big"
    assert result.error_detail == "upstream rejected request: message too big"
    assert "secret" not in result.error_detail


@pytest.mark.asyncio
async def test_responses_ws_postvisible_1009_sends_typed_error_and_closes(monkeypatch, m):
    _setup(m)
    _make_channel(m)
    ws = FakeWebSocket({
        "type": "response.create", "model": "test-model", "input": "hello", "stream": True,
    })
    upstream = ClosingAfterEventsWebSocket([{
        "type": "response.output_text.delta",
        "output_index": 0,
        "content_index": 0,
        "delta": "partial",
    }], 1009)

    async def fake_connect(*args, **kwargs):
        return upstream

    monkeypatch.setattr(m["responses_ws"], "_connect_upstream_ws", fake_connect)
    await asyncio.wait_for(
        m["responses_ws"].handle_responses_ws(ws),  # type: ignore[arg-type]
        timeout=1,
    )

    frames = [json.loads(text) for text in ws.sent_texts]
    assert frames[0]["type"] == "response.output_text.delta"
    error = frames[-1]
    assert error["type"] == "error"
    assert error["status"] == 413
    assert error["error"] == {
        "type": "invalid_request_error",
        "code": "message_too_big",
        "message": error["message"],
    }
    assert m["responses_ws"]._parse_wrapped_ws_error(json.dumps(error))["status"] == 413
    assert ws.close_calls[-1][0] == 4400
    assert upstream.recv_calls == 2


@pytest.mark.asyncio
async def test_responses_ws_previsible_metadata_buffer_is_bounded(m):
    _setup(m)
    from src.transports.ws_runtime import (
        read_until_first_responses_ws_visible_event,
    )

    upstream = FakeUpstreamWebSocket([
        {"type": "response.created", "response": {"id": f"r-{idx}"}}
        for idx in range(1_025)
    ])
    result = await read_until_first_responses_ws_visible_event(
        upstream,
        m["responses_ws"]._WsTracker(),
        channel_key="api:ws-upstream",
        deadline_ts=0,
        first_wait=5,
        idle_timeout=5,
    )

    assert result.outcome == "transport_error"
    assert result.http_status == 502
    assert result.error_detail == "pre-visible websocket buffer limit exceeded"
    assert result.pending == []


@pytest.mark.asyncio
async def test_http_responses_oauth_ws_non_stream_close_before_terminal_is_error(
    monkeypatch, m,
):
    cfg = _setup(m)
    cfg.setdefault("openai", {})["responsesUpstreamWsForOAuth"] = True
    ch = _make_oauth_channel_for_failover(m, name="non-stream-close@example.com")

    async def fake_token(account_key):
        return "tok"

    fake_ws = FakeOAuthHttpWs([
        {"type": "response.created", "response": {"id": "resp_truncated"}},
        {"type": "response.output_text.delta", "output_index": 0,
         "content_index": 0, "delta": "partial"},
    ])

    async def fake_connect(*args, **kwargs):
        return fake_ws

    monkeypatch.setattr(m["failover"].oauth_manager, "ensure_valid_token", fake_token)
    monkeypatch.setattr(m["failover"], "_connect_oauth_responses_ws", fake_connect)
    resp, rid = await _call_failover_responses(
        m, ch, {"model": "test-model", "stream": False, "input": "hello"},
    )

    assert resp.status_code == 502
    detail = m["log_db"].log_detail(rid)
    assert detail["log"]["status"] == "error"
    assert "partial" in (detail["detail"].get("response_body") or "")
    attempts = _attempt_usage(m, rid)
    assert len(attempts) == 1
    assert attempts[0]["outcome"] == "connection_lifecycle"
    assert attempts[0]["usage_observed"] == 0
    assert attempts[0]["cost_source"] == "unpriced"
    assert not m["cooldown"].is_blocked(ch.key, "test-model")
    assert m["scorer"].get_stats(ch.key, "test-model") is None


@pytest.mark.asyncio
async def test_http_responses_oauth_ws_non_stream_failed_returns_http_error(
    monkeypatch, m,
):
    cfg = _setup(m)
    cfg.setdefault("openai", {})["responsesUpstreamWsForOAuth"] = True
    cfg["retry"] = {"transient": {"enabled": False}}
    ch = _make_oauth_channel_for_failover(m, name="non-stream-failed@example.com")

    async def fake_token(account_key):
        return "tok"

    fake_ws = FakeOAuthHttpWs([{
        "type": "response.failed",
        "response": {
            "id": "resp_non_stream_failed",
            "error": {"code": "server_error", "message": "failed terminal"},
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    }])

    async def fake_connect(*args, **kwargs):
        return fake_ws

    monkeypatch.setattr(m["failover"].oauth_manager, "ensure_valid_token", fake_token)
    monkeypatch.setattr(m["failover"], "_connect_oauth_responses_ws", fake_connect)
    resp, rid = await _call_failover_responses(
        m, ch, {"model": "test-model", "stream": False, "input": "hello"},
    )

    assert resp is not None
    assert resp.status_code == 503
    detail = m["log_db"].log_detail(rid)
    assert detail["log"]["status"] == "error"
    assert "failed terminal" in detail["log"]["error_message"]
    assert "response.failed" in (detail["detail"].get("response_body") or "")
    attempts = _attempt_usage(m, rid)
    assert len(attempts) == 1
    assert attempts[0]["outcome"] == "stream_upstream_error"
    assert attempts[0]["usage_observed"] == 1
    assert attempts[0]["input_tokens"] == attempts[0]["output_tokens"] == 0


@pytest.mark.asyncio
async def test_http_responses_oauth_ws_success_without_usage_does_not_fabricate_zero(
    monkeypatch, m,
):
    cfg = _setup(m)
    cfg.setdefault("openai", {})["responsesUpstreamWsForOAuth"] = True
    ch = _make_oauth_channel_for_failover(m, name="missing-usage@example.com")

    async def fake_token(account_key):
        return "tok"

    fake_ws = FakeOAuthHttpWs([
        {"type": "response.output_text.delta", "output_index": 0,
         "content_index": 0, "delta": "ok"},
        {"type": "response.completed", "response": {
            "id": "resp_missing_usage", "output": [],
        }},
    ])

    async def fake_connect(*args, **kwargs):
        return fake_ws

    monkeypatch.setattr(m["failover"].oauth_manager, "ensure_valid_token", fake_token)
    monkeypatch.setattr(m["failover"], "_connect_oauth_responses_ws", fake_connect)
    resp, rid = await _call_failover_responses(
        m, ch, {"model": "test-model", "stream": False, "input": "hello"},
    )

    assert resp.status_code == 200
    assert "usage" not in json.loads(resp.body)
    detail = m["log_db"].log_detail(rid)
    assert detail["log"]["status"] == "success"
    assert detail["log"]["usage_observed"] == 0
    attempts = _attempt_usage(m, rid)
    assert len(attempts) == 1
    assert attempts[0]["usage_observed"] == 0
    assert attempts[0]["cost_source"] == "unpriced"


def test_oauth_ws_tracker_large_frame_keeps_actual_cost_evidence(m):
    tracker = m["failover"]._WsResponsesTracker()
    tracker.feed_text(json.dumps({
        "type": "response.failed",
        "response": {
            "service_tier": "priority",
            "usage": {
                "input_tokens": 7,
                "output_tokens": 2,
                "input_tokens_details": {"cached_tokens": 3},
                "cost_in_usd_ticks": 9876,
            },
            "output": [{
                "type": "message",
                "content": [{"type": "output_text", "text": "x" * 210_000}],
            }],
        },
    }))

    body = tracker.get_full_response()
    normalized = m["failover"].model_pricing.normalize_response_billing(body)
    assert len(body) == 200_000
    assert "_parrot_truncated_billing_evidence" in body
    assert normalized.usage_observed is True
    assert normalized.input_tokens == 4
    assert normalized.cache_read_tokens == 3
    assert normalized.output_tokens == 2
    assert normalized.service_tier == "priority"
    assert normalized.actual_cost_ticks == 9876


@pytest.mark.asyncio
async def test_http_responses_oauth_ws_large_terminal_keeps_observed_zero_usage(
    monkeypatch, m,
):
    cfg = _setup(m)
    cfg.setdefault("openai", {})["responsesUpstreamWsForOAuth"] = True
    ch = _make_oauth_channel_for_failover(m, name="large-zero@example.com")

    async def fake_token(account_key):
        return "tok"

    huge_text = "x" * 210_000
    fake_ws = FakeOAuthHttpWs([
        {"type": "response.output_text.delta", "output_index": 0,
         "content_index": 0, "delta": "ok"},
        {"type": "response.completed", "response": {
            "id": "resp_oauth_large_zero",
            "output": [{
                "type": "message",
                "content": [{"type": "output_text", "text": huge_text}],
            }],
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }},
    ])

    async def fake_connect(*args, **kwargs):
        return fake_ws

    monkeypatch.setattr(m["failover"].oauth_manager, "ensure_valid_token", fake_token)
    monkeypatch.setattr(m["failover"], "_connect_oauth_responses_ws", fake_connect)
    resp, rid = await _call_failover_responses(
        m, ch, {"model": "test-model", "stream": False, "input": "hello"},
    )

    assert resp.status_code == 200
    detail = m["log_db"].log_detail(rid)
    assert detail["log"]["status"] == "success"
    assert detail["log"]["usage_observed"] == 1
    assert len(detail["detail"]["response_body"]) == 200_000
    attempts = _attempt_usage(m, rid)
    assert len(attempts) == 1
    assert attempts[0]["usage_observed"] == 1
    assert attempts[0]["input_tokens"] == attempts[0]["output_tokens"] == 0


@pytest.mark.asyncio
async def test_http_responses_oauth_ws_cancellation_preserves_partial_usage_and_body(
    monkeypatch, m,
):
    cfg = _setup(m)
    cfg.setdefault("openai", {})["responsesUpstreamWsForOAuth"] = True
    ch = _make_oauth_channel_for_failover(m, name="cancel-partial@example.com")

    async def fake_token(account_key):
        return "tok"

    fake_ws = BlockingAfterEventsWebSocket([{
        "type": "response.in_progress",
        "response": {
            "id": "oauth_cancel_partial",
            "usage": {
                "input_tokens": 7,
                "output_tokens": 2,
                "input_tokens_details": {"cached_tokens": 3},
            },
        },
    }])

    async def fake_connect(*args, **kwargs):
        return fake_ws

    monkeypatch.setattr(m["failover"].oauth_manager, "ensure_valid_token", fake_token)
    monkeypatch.setattr(m["failover"], "_connect_oauth_responses_ws", fake_connect)
    task = asyncio.create_task(_call_failover_responses(
        m, ch, {"model": "test-model", "stream": False, "input": "hello"},
    ))
    await asyncio.wait_for(fake_ws.waiting.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    row = _last_request_log(m)
    assert row["status"] == "cancelled"
    assert row["usage_observed"] == 1
    detail = m["log_db"].log_detail(row["request_id"])
    assert "response.in_progress" in detail["detail"]["response_body"]
    attempts = _attempt_usage(m, row["request_id"])
    assert len(attempts) == 1
    assert attempts[0]["usage_observed"] == 1
    assert attempts[0]["input_tokens"] == 4
    assert attempts[0]["cache_read_tokens"] == 3
    assert attempts[0]["output_tokens"] == 2


@pytest.mark.asyncio
async def test_http_responses_oauth_ws_pre_visible_context_error_keeps_zero_usage(
    monkeypatch, m,
):
    cfg = _setup(m)
    cfg.setdefault("openai", {})["responsesUpstreamWsForOAuth"] = True
    cfg["retry"] = {"transient": {"enabled": False}}
    ch = _make_oauth_channel_for_failover(m, name="zero-context@example.com")

    async def fake_token(account_key):
        return "tok"

    fake_ws = FakeOAuthHttpWs([
        {"type": "response.created", "response": {"id": "resp_zero_context"}},
        {"type": "response.incomplete", "response": {
            "id": "resp_zero_context",
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [],
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }},
    ])

    async def fake_connect(*args, **kwargs):
        return fake_ws

    monkeypatch.setattr(m["failover"].oauth_manager, "ensure_valid_token", fake_token)
    monkeypatch.setattr(m["failover"], "_connect_oauth_responses_ws", fake_connect)
    resp, rid = await _call_failover_responses(
        m, ch, {"model": "test-model", "stream": False, "input": "hello"},
    )

    assert resp.status_code == 400
    detail = m["log_db"].log_detail(rid)
    assert detail["log"]["status"] == "error"
    assert "response.incomplete" in (detail["detail"].get("response_body") or "")
    attempts = _attempt_usage(m, rid)
    assert len(attempts) == 1
    assert attempts[0]["usage_observed"] == 1
    assert attempts[0]["input_tokens"] == attempts[0]["output_tokens"] == 0


def test_responses_ws_uses_remote_dns_for_socks5(m):
    _setup(m)
    assert m["responses_ws"]._socks5h_url("socks5://127.0.0.1:1080") == "socks5h://127.0.0.1:1080"
    assert m["responses_ws"]._socks5h_url("socks5h://127.0.0.1:1080") == "socks5h://127.0.0.1:1080"

@pytest.mark.asyncio
async def test_responses_ws_html403_skips_refresh_health_and_exhausts_with_safe_403(monkeypatch, m):
    cfg = _setup(m)
    monkeypatch.setitem(
        cfg, "retry",
        {"transient": {"enabled": False}, "recovery": {"oauthRefresh": True}},
    )
    channels = [
        _make_oauth_channel_for_failover(m, name=f"html403-{idx}@example.com")
        for idx in range(2)
    ]
    with m["registry"]._lock:
        m["registry"]._channels = {ch.key: ch for ch in channels}

    ws = FakeWebSocket({
        "type": "response.create", "model": "test-model", "input": "hello", "stream": True,
    })
    await ws.accept()
    request_id = "responses-ws-html403-protection"
    body = {"model": "test-model", "input": "hello", "stream": True}
    started_at = time.time()
    started_monotonic = time.monotonic()
    m["log_db"].insert_pending(
        request_id, "1.2.3.4", "ws-key", "test-model", True,
        1, 0, {}, body, ingress_protocol="responses_ws",
    )
    calls = []

    async def fake_try(_websocket, **kwargs):
        calls.append(kwargs["ch"].key)
        return m["responses_ws"]._WsAttemptResult(
            outcome="http_auth_error", http_status=403,
            error_detail="HTTP 403: <html>Bearer secret user@example.com</html>",
            openai_oauth_html_403=True,
        )

    async def must_not_refresh(_account_key):
        raise AssertionError("HTML403 must not force refresh")

    from src.scheduler import ScheduleResult
    route = ScheduleResult(
        candidates=[(ch, "real-model") for ch in channels], saturated=[],
        affinity_hit=False, fp_query=None, client_key="client:1",
    )
    monkeypatch.setattr(m["responses_ws"], "_try_ws_channel", fake_try)
    monkeypatch.setattr(m["responses_ws"].oauth_manager, "force_refresh", must_not_refresh)
    accepted = await m["responses_ws"]._run_ws_failover(
        ws, first_obj={"type": "response.create", **body}, schedule_result=route,
        body=body, request_id=request_id, api_key_name="ws-key", client_ip="1.2.3.4",
        start_time=started_at, start_monotonic=started_monotonic, fp_query=None,
    )

    assert accepted is True
    assert calls == [ch.key for ch in channels]
    row = dict(m["log_db"]._get_conn().execute(
        "SELECT * FROM request_log WHERE request_id=?", (request_id,),
    ).fetchone())
    assert row["http_status"] == 403
    assert row["retry_count"] == 2
    downstream = " ".join(ws.sent_texts + [ws.close_calls[-1][1]]).lower()
    assert "upstream candidates failed" in downstream
    assert "denied permission" not in downstream
    assert "<html" not in downstream and "secret" not in downstream and "example.com" not in downstream
    for ch in channels:
        assert m["cooldown"].get_state(ch.key, "real-model") is None
        assert m["scorer"].get_stats(ch.key, "real-model") is None
