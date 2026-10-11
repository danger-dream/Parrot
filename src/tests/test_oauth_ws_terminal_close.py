"""HTTP Responses terminal delivery must not wait for one-shot WS teardown."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from src.tests.test_openai_responses_ws import (
    FakeOAuthHttpWs,
    _call_failover_responses,
    _import_modules,
    _isolate_ws_config,
    _make_oauth_channel_for_failover,
    _setup,
)


class GatedCloseWs(FakeOAuthHttpWs):
    def __init__(self, events, order):
        super().__init__(events)
        self.order = order
        self.close_started = asyncio.Event()
        self.allow_close = asyncio.Event()
        self.close_calls = 0

    async def close(self, *args, **kwargs):
        self.close_calls += 1
        self.order.append('close_started')
        self.close_started.set()
        await self.allow_close.wait()
        self.closed = True
        self.order.append('close_finished')


def install(monkeypatch, m, ws):
    cfg = _setup(m)
    cfg.setdefault('openai', {})['responsesUpstreamWsForOAuth'] = True
    cfg['retry'] = {'transient': {'enabled': False}}
    ch = _make_oauth_channel_for_failover(m)

    async def token(*args, **kwargs):
        return 'test-token'

    async def connect(*args, **kwargs):
        return ws

    monkeypatch.setattr(m['failover'].oauth_manager, 'ensure_valid_token', token)
    monkeypatch.setattr(m['failover'], '_connect_oauth_responses_ws', connect)
    return ch


@pytest.mark.asyncio
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('first_batch_terminal', [False, True])
@pytest.mark.parametrize('terminal', ['completed', 'failed', 'incomplete'])
async def test_terminal_returns_and_releases_before_close(
    monkeypatch, m, stream, first_batch_terminal, terminal,
):
    order = []
    response = {'id': 'resp_close', 'status': terminal, 'output': [],
                'usage': {'input_tokens': 2, 'output_tokens': 1}}
    if terminal == 'failed':
        response['error'] = {'code': 'server_error', 'message': 'failed terminal'}
    if terminal == 'incomplete':
        response['incomplete_details'] = {'reason': 'max_output_tokens'}
    events = [] if first_batch_terminal else [
        {'type': 'response.created', 'response': {'id': 'resp_close'}},
        {'type': 'response.output_text.delta', 'output_index': 0,
         'content_index': 0, 'delta': 'ok'},
    ]
    events.append({'type': f'response.{terminal}', 'response': response})
    ws = GatedCloseWs(events, order)
    ch = install(monkeypatch, m, ws)
    real_release = m['concurrency'].release

    def release(*args, **kwargs):
        order.append('released')
        return real_release(*args, **kwargs)

    monkeypatch.setattr(m['concurrency'], 'release', release)

    async def consume():
        resp, rid = await _call_failover_responses(
            m, ch, {'model': 'test-model', 'stream': stream, 'input': 'hello'},
        )
        if stream:
            messages = []
            async def send(message):
                messages.append(message)
            await resp.stream_response(send)
            assert messages[-1]['more_body'] is False
            payload = b''.join(msg.get('body', b'') for msg in messages)
            assert f'response.{terminal}'.encode() in payload
        else:
            assert resp.status_code == (503 if terminal == 'failed' else 200)
        order.append('http_finished')
        return rid

    task = asyncio.create_task(consume())
    try:
        done, _ = await asyncio.wait({task}, timeout=1)
        assert task in done, 'HTTP delivery waited for upstream close'
        rid = task.result()
        await asyncio.wait_for(ws.close_started.wait(), timeout=1)
        assert not ws.closed
        assert order.count('released') == 1
        assert order.index('released') < order.index('close_started')
        assert m['failover']._oauth_ws_close_tasks
        log = m['log_db'].log_detail(rid)['log']
        assert log['status'] == ('error' if terminal == 'failed' else 'success')
    finally:
        ws.allow_close.set()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.gather(*m['failover']._oauth_ws_close_tasks)
    assert ws.closed and ws.close_calls == 1
    assert not m['failover']._oauth_ws_close_tasks
    assert order.index('http_finished') < order.index('close_finished')


@pytest.mark.asyncio
@pytest.mark.parametrize('stream', [False, True])
async def test_nonterminal_transport_error_still_waits_for_close(monkeypatch, m, stream):
    # Exhausting recv without a terminal raises ConnectionClosed.
    ws = GatedCloseWs([], [])
    ch = install(monkeypatch, m, ws)
    task = asyncio.create_task(_call_failover_responses(
        m, ch, {'model': 'test-model', 'stream': stream, 'input': 'hello'},
    ))
    try:
        await asyncio.wait_for(ws.close_started.wait(), timeout=1)
        assert not task.done()
        assert not m['failover']._oauth_ws_close_tasks
    finally:
        ws.allow_close.set()
        await task
    assert ws.close_calls == 1 and ws.closed


@pytest.mark.asyncio
async def test_stream_cancel_keeps_synchronous_close_owner(monkeypatch, m):
    waiting = asyncio.Event()
    class BlockedRecvWs(GatedCloseWs):
        async def recv(self):
            if self._events:
                return await super().recv()
            waiting.set()
            await asyncio.Event().wait()

    ws = BlockedRecvWs([{'type': 'response.output_text.delta', 'delta': 'ok'}], [])
    ch = install(monkeypatch, m, ws)
    resp, rid = await _call_failover_responses(
        m, ch, {'model': 'test-model', 'stream': True, 'input': 'hello'},
    )
    async def consume():
        async for _ in resp.body_iterator:
            pass
    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(waiting.wait(), timeout=1)
        task.cancel()
        await asyncio.wait_for(ws.close_started.wait(), timeout=1)
        assert not task.done()
        assert not m['failover']._oauth_ws_close_tasks
    finally:
        ws.allow_close.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert ws.closed and ws.close_calls == 1
    assert m['log_db'].log_detail(rid)['log']['status'] == 'cancelled'


@pytest.mark.asyncio
async def test_background_close_cancellation_drains_managed_ws_and_bridge(m):
    from src.transports.ws_runtime import ManagedWsConnection
    order = []
    ws = GatedCloseWs([], order)
    async def wait_closed():
        assert ws.closed
        order.append('wait_closed')
    ws.wait_closed = wait_closed
    async def close_bridge(**kwargs):
        order.append('bridge_closed')
    bridge = SimpleNamespace(aclose=close_bridge)
    managed = ManagedWsConnection(ws, bridge)
    m['failover']._close_completed_oauth_ws(managed)
    task, = m['failover']._oauth_ws_close_tasks
    try:
        await asyncio.wait_for(ws.close_started.wait(), timeout=1)
        task.cancel()
        # Cancellation must not interrupt the retained cleanup owner.
        await asyncio.sleep(0)
        assert not task.done() and not ws.closed
    finally:
        ws.allow_close.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert order == ['close_started', 'close_finished', 'wait_closed', 'bridge_closed']
    assert not m['failover']._oauth_ws_close_tasks


@pytest.mark.asyncio
async def test_disconnect_after_terminal_keeps_synchronous_close(monkeypatch, m):
    ws = GatedCloseWs([{'type': 'response.completed', 'response': {
        'id': 'resp_finished', 'status': 'completed', 'output': [],
        'usage': {'input_tokens': 1, 'output_tokens': 1},
    }}], [])
    ch = install(monkeypatch, m, ws)
    resp, rid = await _call_failover_responses(
        m, ch, {'model': 'test-model', 'stream': True, 'input': 'hello'},
    )
    assert b'response.completed' in await anext(resp.body_iterator)
    task = asyncio.create_task(resp.body_iterator.aclose())
    try:
        await asyncio.wait_for(ws.close_started.wait(), timeout=1)
        assert not task.done()
        assert not m['failover']._oauth_ws_close_tasks
    finally:
        ws.allow_close.set()
        await task
    assert ws.closed and ws.close_calls == 1
    # A client leaving after the terminal cannot rewrite settled success.
    assert m['log_db'].log_detail(rid)['log']['status'] == 'success'


@pytest.mark.asyncio
async def test_background_close_timeout_reclaims_real_ws_and_bridge(m):
    import websockets
    from src.tests.test_ss2022_duplex_bridge import _connector, _loopback_bridge_opener
    from src.transports.ws_runtime import connect_upstream_ws, WsProxyBytes

    async def handler(ws):
        original_recv_frame = ws.protocol.recv_frame
        def ignore_close(frame):
            if frame.opcode != 8:
                original_recv_frame(frame)
        ws.protocol.recv_frame = ignore_close
        await ws.send('ready')
        await ws.wait_closed()

    owners = []
    async with websockets.serve(handler, '127.0.0.1', 0) as server:
        port = server.sockets[0].getsockname()[1]
        opener = await _loopback_bridge_opener(port, owners)
        managed = await connect_upstream_ws(
            f'ws://127.0.0.1:{port}/responses', headers={}, connector=_connector(),
            proxy_bytes=WsProxyBytes(), open_timeout=1, open_socket_func=opener,
        )
        assert await managed.recv() == 'ready'
        # The production transport uses 10s; exercise the same library timer
        # with a shorter budget instead of waiting ten seconds in each test.
        managed._ws.close_timeout = 0.02
        m['failover']._close_completed_oauth_ws(managed)
        await asyncio.wait_for(asyncio.gather(*m['failover']._oauth_ws_close_tasks), 1)
        assert managed._ws.protocol.close_rcvd is None
        assert managed._ws.transport.is_closing()
    bridge, raw = owners[0]
    assert bridge.closed and raw.close_count == 1
    assert all(task.done() for task in bridge.pump_tasks)
    assert bridge._application_socket.fileno() == -1
    assert not m['failover']._oauth_ws_close_tasks


@pytest.mark.asyncio
async def test_background_close_observes_errors_without_logging_payload(m, capsys):
    async def close():
        raise RuntimeError('private upstream payload')
    m['failover']._close_completed_oauth_ws(SimpleNamespace(close=close))
    await asyncio.gather(*m['failover']._oauth_ws_close_tasks)
    assert not m['failover']._oauth_ws_close_tasks
    output = capsys.readouterr().out
    assert 'RuntimeError' in output
    assert 'private upstream payload' not in output
