"""Actual Python LS transport + mocked DNS/native I/O; no sockets or credentials."""
from __future__ import annotations

import asyncio
import base64
import threading

import pytest

from src.tests.test_antigravity_oauth import _import_modules, _add_ag_account
from src.tests.test_antigravity_compatibility import clean, BASE, MODEL
from src.transports import go_tls, http_runtime


@pytest.fixture
async def native_route(m, monkeypatch):
    from src.tests.conftest import _ORIG_TO_THREAD
    # Exercise the real worker handoff rather than conftest's fast in-loop shim.
    monkeypatch.setattr(asyncio, "to_thread", _ORIG_TO_THREAD)
    key = _add_ag_account(m["oauth_manager"], models=[MODEL])
    channel = m["AntigravityOAuthChannel"](m["oauth_manager"].get_account(key))
    channel.tls_fingerprint = go_tls.PROFILE
    async def token(*a, **k): return "synthetic-token"
    monkeypatch.setattr(m["oauth_manager"], "ensure_channel_token", token)
    monkeypatch.setattr(http_runtime, "_resolve_http_route_chain", lambda *a, **k: ([("direct", None)], None))
    state = {"starts": [], "clients": [], "natives": [], "timings": [], "hold_dns": False,
             "hold_worker": False, "after_start": None, "early_headers": False, "pending": False, "native_error": None}
    dns_entered, dns_release, polled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    worker_entered, worker_release = threading.Event(), threading.Event()
    state.update(dns_entered=dns_entered, dns_release=dns_release, worker_entered=worker_entered,
                 worker_release=worker_release, polled=polled)
    actual_timing = http_runtime.HttpAttemptTiming
    def timing(*a, **kw):
        value = actual_timing(*a, **kw)
        state["timings"].append(value)
        return value
    monkeypatch.setattr(http_runtime, "HttpAttemptTiming", timing)
    async def resolver(host, timeout):
        dns_entered.set()
        if state["hold_dns"]: await dns_release.wait()
        return ["203.0.113.7"]
    monkeypatch.setattr(go_tls, "_resolve_peer", resolver)
    loop_thread = threading.get_ident()

    class Native:
        def __init__(self):
            self.cancelled = self.closed = False
            self.events = ([] if state["early_headers"] else [{"kind": "dispatch"}, {"kind": "uploaded"}]) + [
                {"kind": "headers", "status": 200, "headers_raw": [["content-type", "text/plain"]], "dispatched": True},
                {"kind": "data", "data": base64.b64encode(b"OK").decode(), "dispatched": True},
                {"kind": "eof", "dispatched": True},
            ]
            if state["native_error"] is not None:
                self.events = [state["native_error"]]
            state["natives"].append(self)
        def poll(self, timeout):
            polled.set()
            return {"kind": "pending"} if state["pending"] else self.events.pop(0)
        def cancel(self): self.cancelled = True
        def close(self):
            self.closed = True
            return {"kind": "eof", "dispatched": True}

    class Client:
        def __init__(self, **kw):
            self.closed = False
            state["clients"].append(self)
            if state["hold_worker"]:
                worker_entered.set()
                assert worker_release.wait(3), "worker release timed out"
        def start(self, method, url, **kw):
            assert threading.get_ident() != loop_thread
            # Admission to native is not proof of a physical send for billing.
            assert not state["timings"][-1].dispatch_started
            assert not state["timings"][-1].connection_complete
            state["starts"].append((method, url, kw))
            native = Native()
            if state["after_start"]: state["after_start"]()
            return native
        def close(self): self.closed = True
    monkeypatch.setattr(go_tls, "native_client_type", lambda: Client)

    async def open_request(stream=True):
        request = await channel.build_upstream_request({"model": MODEL, "input": "hello", "stream": stream}, MODEL)
        return await http_runtime.open_response_with_proxy_chain(
            channel=channel, resolved_model=MODEL, upstream_req=request,
            connect_timeout=3, first_byte_timeout=3, idle_timeout=3, total_timeout=6,
            response_mode="stream" if stream else "non_stream", request_id=None,
        )
    def change_endpoint():
        m["config"].update(lambda cfg: cfg["oauthAccounts"][0].update(request_base_url="https://new-endpoint.example/ag"))
    state.update(open=open_request, change_endpoint=change_endpoint)
    yield state
    dns_release.set()
    worker_release.set()


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("phase", ["dns", "worker"])
@pytest.mark.asyncio
async def test_go_tls_route_switch_before_native_handoff_never_starts(native_route, stream, phase):
    state = native_route
    state["hold_" + phase] = True
    task = asyncio.create_task(state["open"](stream))
    try:
        if phase == "dns": await asyncio.wait_for(state["dns_entered"].wait(), 2)
        else: assert await asyncio.to_thread(state["worker_entered"].wait, 2)
        state["change_endpoint"]()
    finally:
        state[phase + "_release"].set()
    opened = await task
    assert state["starts"] == [], "stale endpoint reached native client.start"
    assert opened.error is not None and opened.error.outcome == "candidate_guard"
    assert opened.error.http_status == 400
    assert not state["timings"][0].dispatch_started
    assert not state["timings"][0].connection_complete
    assert all(client.closed for client in state["clients"])


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("early_headers", [False, True])
@pytest.mark.parametrize("change", ["endpoint", "model"])
@pytest.mark.asyncio
async def test_go_tls_post_send_state_change_does_not_reject_response(m, native_route, stream, early_headers, change):
    state = native_route
    state["early_headers"] = early_headers
    state["after_start"] = (state["change_endpoint"] if change == "endpoint" else
                            lambda: m["config"].update(lambda cfg: cfg["modelCenter"].update(disabledModels=[MODEL])))
    opened = await state["open"](stream)
    assert opened.error is None, opened.error
    try:
        assert opened.response.status_code == 200
        assert await http_runtime._read_response_bytes(opened.response, opened.timing, opened.round_timeouts) == b"OK"
        assert opened.timing.dispatch_started and opened.timing.connection_complete
        assert len(state["starts"]) == 1
    finally:
        await http_runtime.close_response_context(opened.ctx)
        await http_runtime.close_proxy_client(opened.proxy_client)
    assert all(native.closed and native.cancelled for native in state["natives"])
    assert all(client.closed for client in state["clients"])


@pytest.mark.asyncio
async def test_go_tls_dispatched_error_remains_transport_failure_without_route_replay(native_route, monkeypatch):
    state = native_route
    state["after_start"] = state["change_endpoint"]
    state["native_error"] = {"kind": "error", "code": "connect", "message": "synthetic sent failure", "dispatched": True}
    monkeypatch.setattr(http_runtime, "_resolve_http_route_chain", lambda *a, **k: ([("first", None), ("second", None)], None))
    opened = await state["open"]()
    assert opened.error is not None and opened.error.outcome == "transport_error"
    assert len(state["starts"]) == 1 and state["timings"][0].dispatch_started
    assert all(native.closed and native.cancelled for native in state["natives"])
    assert all(client.closed for client in state["clients"])


@pytest.mark.asyncio
async def test_go_tls_cancel_after_handoff_keeps_terminal_send_observation(native_route):
    state = native_route
    state["pending"] = True
    state["after_start"] = state["change_endpoint"]
    task = asyncio.create_task(state["open"]())
    await asyncio.wait_for(state["polled"].wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert len(state["starts"]) == 1 and state["timings"][0].dispatch_started
    assert all(native.closed and native.cancelled for native in state["natives"])
    assert all(client.closed for client in state["clients"])
