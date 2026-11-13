"""Local admission/quietness regression tests; no upstream calls or restarts."""
import asyncio
import json
import threading
from types import SimpleNamespace

import pytest
from starlette.websockets import WebSocketState

from src import drain
import server
from src.openai import responses_ws
from src.mcp import server as mcp_server


@pytest.fixture(autouse=True)
def reset():
    drain.reset_for_tests()
    yield
    drain.reset_for_tests()


def test_reservation_release_and_real_shutdown():
    token = drain.try_reserve_update(0)
    assert token is not None
    assert not drain.is_draining() and not drain.shutdown_requested()
    assert drain.try_reserve_update(0) is None
    drain.release_update(object())
    with pytest.raises(drain.AdmissionClosed):
        drain.enter_work()
    drain.release_update(token)
    work = drain.enter_work()
    assert drain.try_reserve_update(0) is None
    work.close()
    work.close()
    assert drain.active_work_count() == 0
    assert drain.try_reserve_update(180) is None
    token = drain.try_reserve_update(0)
    drain.begin("SIGTERM")
    drain.release_update(token)
    assert drain.shutdown_requested()
    with pytest.raises(drain.AdmissionClosed):
        drain.enter_work()


def test_work_and_reservation_race_are_mutually_exclusive():
    for _ in range(50):
        drain.reset_for_tests()
        barrier = threading.Barrier(2)
        result = {}

        def reserve():
            barrier.wait()
            result["token"] = drain.try_reserve_update(0)

        thread = threading.Thread(target=reserve)
        thread.start()
        barrier.wait()
        try:
            work = drain.enter_work()
        except drain.AdmissionClosed:
            work = None
        thread.join()
        assert (work is None) != (result["token"] is None)
        if work:
            work.close()
        drain.release_update(result["token"])


@pytest.mark.parametrize("path", ["/v1/responses", "/v1/chat/completions", "/images/generations", "/images/edits", "/backend-api/codex/realtime/calls", "/v1/videos"])
def test_http_real_middleware_rejects_and_reopens(path):
    async def scenario():
        calls = []
        async def app(scope, receive, send):
            calls.append(path)
            assert drain.active_work_count() == 1
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})
        async def receive():
            return {"type": "http.disconnect"}
        messages = []
        async def send(message):
            messages.append(message)
        middleware = server._DrainHttpMiddleware(app)
        scope = {"type": "http", "method": "POST", "path": path, "headers": []}
        token = drain.try_reserve_update(0)
        await middleware(scope, receive, send)
        assert messages[0]["status"] == 503 and calls == []
        drain.release_update(token)  # failed update rollback reopens admission
        await middleware(scope, receive, send)
        assert calls == [path]
        assert drain.active_count() == drain.active_work_count() == 0
    asyncio.run(scenario())


def test_non_work_connections_do_not_reset_quietness():
    async def scenario():
        leases = [await drain.enter(label) for label in (
            "ws /v1/responses", "http GET /v1/models", "http POST /mcp", "http GET /management")]
        assert drain.active_count() == 4 and drain.active_work_count() == 0
        token = drain.try_reserve_update(180)
        assert token is not None
        for lease in leases:
            await lease.aclose()
        drain.release_update(token)
        assert drain.is_idle_for_update(180)
        lease = await drain.enter("ws /v1/realtime")
        assert drain.try_reserve_update(0) is None
        await lease.aclose()
    asyncio.run(scenario())


def test_ws_existing_connection_round_queue_and_finalization(monkeypatch):
    async def scenario():
        events = []
        socket = SimpleNamespace(headers={}, client=None, scope={}, application_state=WebSocketState.CONNECTED)
        async def send_text(text):
            events.append(json.loads(text))
        socket.send_text = send_text
        conn = responses_ws._ResponsesWsConnection(socket)
        finishing = asyncio.Event()
        finish = asyncio.Event()
        idle = asyncio.Event()
        async def runner(lane):
            await lane.receive_create()
            finishing.set()
            await finish.wait()  # upstream/log persistence finalizer still owns work
            idle.set()
            await lane.receive_create()  # actual between-round idle boundary
        monkeypatch.setattr(responses_ws, "_handle_responses_ws_lane", runner)
        await conn.dispatch(json.dumps({"type": "response.create", "model": "test"}))
        assert drain.active_work_count() == 1  # counted before worker/slot admission
        await finishing.wait()
        assert drain.try_reserve_update(0) is None
        finish.set()
        await idle.wait()
        await asyncio.sleep(0)
        assert drain.active_work_count() == 0
        token = drain.try_reserve_update(0)
        assert token is not None
        await conn.dispatch(json.dumps({"type": "response.create", "model": "test"}))
        assert events[-1]["error"]["code"] == "server_restarting"
        assert drain.active_work_count() == 0
        drain.release_update(token)
        await conn.dispatch(json.dumps({"type": "response.create", "model": "test"}))
        tasks = conn._cancel_lanes()
        await asyncio.gather(*tasks, return_exceptions=True)
        assert drain.active_work_count() == 0
    asyncio.run(scenario())


def test_mcp_tool_admission_and_cancel_finalizers(monkeypatch):
    async def scenario():
        entered = asyncio.Event()
        finalized = []
        async def perform(ctx, params):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                await asyncio.sleep(0)
                assert drain.active_work_count() == 1
                finalized.append(True)
        monkeypatch.setattr(mcp_server, "_on_call_tool", perform)
        token = drain.try_reserve_update(0)
        result = await mcp_server.on_call_tool(None, None)
        assert result.is_error and not entered.is_set()
        drain.release_update(token)
        task = asyncio.create_task(mcp_server.on_call_tool(None, None))
        await entered.wait()
        assert drain.try_reserve_update(0) is None
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert finalized == [True] and drain.active_work_count() == 0
    asyncio.run(scenario())


def test_ws_slot_wait_and_disconnect_release_all_queued_work(monkeypatch):
    async def scenario():
        socket = SimpleNamespace(headers={}, client=None, scope={}, application_state=WebSocketState.CONNECTED)
        async def send_text(text):
            pass
        socket.send_text = send_text
        conn = responses_ws._ResponsesWsConnection(socket)
        conn.slots = asyncio.Semaphore(0)
        async def runner(lane):
            await lane.receive_create()
            pytest.fail("No execution slot is available")
        monkeypatch.setattr(responses_ws, "_handle_responses_ws_lane", runner)
        for _ in range(3):
            await conn.dispatch('{"type":"response.create","model":"test"}')
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert drain.active_work_count() == 3
        assert drain.try_reserve_update(0) is None
        await asyncio.gather(*conn._cancel_lanes(), return_exceptions=True)
        assert drain.active_work_count() == 0
    asyncio.run(scenario())


@pytest.mark.parametrize("route,args", [
    (server.proxy_realtime_websocket, ()),
    (server.proxy_realtime_live_websocket, ()),
    (server.proxy_realtime_live_sideband_websocket, ("call",)),
])
def test_realtime_routes_reject_update_reservation(route, args):
    async def scenario():
        closed = []
        async def close(**kwargs):
            closed.append(kwargs)
        token = drain.try_reserve_update(0)
        await route(SimpleNamespace(close=close), *args)
        assert closed[0]["code"] == 1013
        assert drain.active_count() == drain.active_work_count() == 0
        drain.release_update(token)
    asyncio.run(scenario())


def test_quiet_window_begins_after_last_work_finishes(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(drain.time, "monotonic", lambda: clock[0])
    work = drain.enter_work()
    clock[0] = 1000.0
    assert drain.try_reserve_update(180) is None
    work.close()
    clock[0] = 1179.0
    assert drain.try_reserve_update(180) is None
    clock[0] = 1180.0
    assert drain.try_reserve_update(180) is not None

def test_ws_direct_enqueue_owns_admission_and_clear_preserves_active_turn(monkeypatch):
    async def scenario():
        socket = SimpleNamespace(headers={}, client=None, scope={})
        conn = responses_ws._ResponsesWsConnection(socket)
        lane = responses_ws._ResponsesWsLane(conn, None)
        frame = {"type": "response.create", "model": "test"}
        token = drain.try_reserve_update(0)
        with pytest.raises(drain.AdmissionClosed):
            lane.creates.put_nowait(frame)
        assert lane.creates.empty() and not lane.ready.is_set()
        assert lane.queued_bytes == conn.queued_bytes == drain.active_work_count() == 0
        drain.release_update(token)
        await lane.creates.put(frame)
        lane.creates.put_nowait(frame)
        assert drain.active_work_count() == 2
        await lane.receive_create()
        lane.creates.clear()
        assert drain.active_work_count() == 1  # clearing pending frames cannot release the active turn
        assert lane.queued_bytes == conn.queued_bytes == 0
        lane.release_slot()
        assert drain.active_work_count() == 0
        # Byte-budget rejection never acquires a work lease.
        monkeypatch.setattr(responses_ws, "_WS_LANE_QUEUE_BYTES", 1)
        with pytest.raises(asyncio.QueueFull):
            lane.creates.put_nowait(frame)
        assert drain.active_work_count() == 0
        assert drain.try_reserve_update(0) is not None
    asyncio.run(scenario())


def test_ws_create_queue_capacity_rejection_and_clear_release_exactly_once():
    async def scenario():
        conn = responses_ws._ResponsesWsConnection(SimpleNamespace(headers={}, client=None, scope={}))
        lane = responses_ws._ResponsesWsLane(conn, None)
        frame = {"type": "response.create", "model": "test"}
        for _ in range(lane.creates.maxsize):
            lane.creates.put_nowait(frame)
        assert drain.active_work_count() == lane.creates.maxsize
        with pytest.raises(asyncio.QueueFull):
            lane.creates.put_nowait(frame)
        assert drain.active_work_count() == lane.creates.maxsize
        lane.creates.clear()
        lane.creates.clear()
        assert drain.active_work_count() == lane.queued_bytes == conn.queued_bytes == 0
        drain.begin("shutdown")
        with pytest.raises(drain.AdmissionClosed):
            lane.creates.put_nowait(frame)
    asyncio.run(scenario())
