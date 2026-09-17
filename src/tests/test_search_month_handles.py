"""Real temporary monthly SQLite + real failover, without external network.

Only the provider HTTP transport and search executor are simulated. Attempt,
proxy, usage and request finalization use the production log_db implementation.
"""
from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from src import log_db, local_web_tools as web, search_tool_policy as policy
from src.tests import test_protocol_fake_upstreams as fx

BJT = timezone(timedelta(hours=8))
JAN = datetime(2026, 1, 31, 23, 59, 59, tzinfo=BJT).timestamp()
FEB = datetime(2026, 2, 1, 0, 0, 1, tzinfo=BJT).timestamp()


def rows(path, table, request_id):
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        request_column = 'root_request_id' if table == 'upstream_attempt_usage' else 'request_id'
        return [dict(r) for r in conn.execute(
            f'SELECT * FROM {table} WHERE {request_column}=? ORDER BY id', (request_id,))]


def output(protocol, call_id=None, *, index=0, text='final'):
    if protocol == 'chat':
        message = {'role': 'assistant', 'content': None if call_id else text}
        if call_id:
            message['tool_calls'] = [{'id': call_id, 'type': 'function', 'function': {
                'name': 'web_search', 'arguments': '{"query":"Python docs"}'}}]
        return {'object': 'chat.completion', 'id': 'chat-' + (call_id or text),
                'model': 'test-model', 'choices': [{'index': index, 'message': message,
                'finish_reason': 'tool_calls' if call_id else 'stop'}],
                'usage': {'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 5}}
    item = ({'type': 'function_call', 'id': 'fc-' + call_id, 'call_id': call_id,
             'name': 'web_search', 'arguments': '{"query":"Python docs"}'} if call_id else
            {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': text}]})
    return {'object': 'response', 'id': 'resp-' + (call_id or text), 'status': 'completed',
            'model': 'test-model', 'output': [item],
            'usage': {'input_tokens': 3, 'output_tokens': 2, 'total_tokens': 5}}


@pytest.fixture
def monthly(tmp_path, monkeypatch):
    m = fx._import_modules()
    fx._setup(m)
    monkeypatch.setattr(log_db, '_log_dir', str(tmp_path))
    monkeypatch.setattr(log_db, '_local', threading.local())
    monkeypatch.setattr(log_db, '_write_conn_registry', {})
    monkeypatch.setattr(log_db, '_request_handles', {})
    clock = [JAN]
    original = log_db._db_ref_for_timestamp
    monkeypatch.setattr(log_db, '_db_ref_for_timestamp', lambda timestamp=None: original(clock[0] if timestamp is None else timestamp))
    monkeypatch.setattr(web, '_settings', lambda: {'functionMode': 'managed', 'hostedMode': 'managed'})
    monkeypatch.setattr(web, 'max_tool_rounds', lambda: 3)
    m['config'].get()['retry'] = {'transient': {'enabled': False}, 'recovery': {'oauthRefresh': False}}
    policy._REPLAY.clear()
    yield SimpleNamespace(m=m, root=tmp_path, clock=clock)
    policy._REPLAY.clear()
    for connections in log_db._write_conn_registry.values():
        for conn in connections:
            conn.close()


def setup_route(env, protocol, queued):
    ch = fx._make_openai_channel('month', 'https://month.example', protocol='openai-' + protocol,
                                 alias='test-model', real='test-model')
    fx._install_channels(env.m, [ch])
    tool = {'type': 'function', 'name': 'web_search', 'parameters': {'type': 'object'}}
    if protocol == 'chat':
        body = {'model': 'test-model', 'n': 2, 'tools': [{'type': 'function', 'function': {
            k: v for k, v in tool.items() if k != 'type'}}], 'messages': [{'role': 'user', 'content': 'search'}]}
    else:
        body = {'model': 'test-model', 'tools': [tool], 'input': 'search'}
    route = env.m['scheduler'].schedule(body, api_key_name='test-key', client_ip='127.0.0.1', ingress_protocol=protocol)
    assert route.candidates
    if queued:
        route.saturated, route.candidates = route.candidates, []
    # Give the new month its own row 1. The old request must not update it or
    # write follow-up rows into that database merely because wall time changed.
    sentinel = log_db.insert_pending('feb-sentinel', '127.0.0.1', 'test-key', 'test-model', False,
                                     1, 0, {}, {}, created_at=FEB)
    retry = log_db.record_retry_attempt(sentinel, 1, 'api:sentinel', 'api', 'test-model', FEB)
    log_db.finish_success(sentinel, 'api:sentinel', 'api', 'test-model', input_tokens=99, output_tokens=1)
    request_id = 'jan-request'
    request = log_db.insert_pending(request_id, '127.0.0.1', 'test-key', 'test-model', False,
                                    1, 1, {}, body, created_at=JAN, ingress_protocol=protocol)
    return body, route, request, sentinel, retry


@pytest.mark.parametrize('protocol', ['responses', 'chat'])
@pytest.mark.parametrize('queued', [False, True])
async def test_managed_real_failover_all_rounds_and_branches_stay_in_origin_month(monthly, monkeypatch, protocol, queued):
    body, route, request, sentinel, sentinel_retry = setup_route(monthly, protocol, queued)
    untouched = rows(sentinel.db.path, 'request_log', sentinel.request_id)
    handles, invocations = [], []
    network_round = 0

    def model(req):
        nonlocal network_round
        network_round += 1
        if network_round == 1:
            obj = output(protocol, 'search-0')
            if protocol == 'chat':
                obj['choices'] += output(protocol, 'search-1', index=1)['choices']
        elif protocol == 'chat' and network_round == 2:
            obj = output(protocol, 'search-0-again')
        else:
            obj = output(protocol, text='final-' + str(network_round))
        return httpx.Response(200, json=obj)

    async def execute(call, *, request_id=None, round_no=0):
        # Keep the real tool wrapper and SQLite logs; mock only external search.
        # Successful model rounds must not retain a global binding during tools.
        assert request_id == request.request_id
        assert request.request_id not in log_db._request_handles
        assert monthly.clock[0] == FEB
        return web.LocalToolResult(call.id, 'actual simulated search result')

    async def invoke(current):
        invocations.append(copy.deepcopy(current))
        assert log_db._request_handles[request.request_id].db == request.db
        response = await monthly.m['failover'].run_failover(
            route, current, request.request_id, 'test-key', '127.0.0.1', False, time.time(),
            ingress_protocol=protocol, start_monotonic=time.monotonic())
        handle = response._parrot_search_attempt_handle
        assert isinstance(handle, log_db.RowLogHandle) and handle.table == 'retry_chain'
        assert handle.db == request.db
        assert request.request_id not in log_db._request_handles
        handles.append(handle)
        monthly.clock[0] = FEB
        return response

    monkeypatch.setattr(web, 'execute_local_tool_call', execute)
    client = httpx.AsyncClient(transport=httpx.MockTransport(model))
    monthly.m['upstream'].set_client(client)
    try:
        response = await policy.run(body, protocol, invoke, request_id=request.request_id, api_key_name='test-key')
    finally:
        await client.aclose()
    expected = 4 if protocol == 'chat' else 2
    assert response.status_code == 200 and len(handles) == expected
    assert handles[0].row_id == sentinel_retry.row_id == 1
    assert len(rows(request.db.path, 'retry_chain', request.request_id)) == expected
    assert len(rows(request.db.path, 'proxy_chain', request.request_id)) == expected
    assert len(rows(request.db.path, 'upstream_attempt_usage', request.request_id)) == expected
    tool_logs = rows(request.db.path, 'local_web_log', request.request_id)
    assert len(tool_logs) == expected - 1
    assert all(r['status'] == 'success' for r in tool_logs)
    assert rows(sentinel.db.path, 'local_web_log', request.request_id) == []
    assert all(r['outcome'] == 'success' for r in rows(request.db.path, 'retry_chain', request.request_id))
    assert rows(request.db.path, 'request_log', request.request_id)[0]['status'] == 'success'
    assert rows(sentinel.db.path, 'request_log', request.request_id) == []
    assert rows(sentinel.db.path, 'retry_chain', request.request_id) == []
    assert rows(sentinel.db.path, 'request_log', sentinel.request_id) == untouched
    assert request.request_id not in log_db._request_handles
    assert b'_parrot_search_attempt_handle' not in response.body
    if protocol == 'chat':
        assert all(b['n'] == 1 for b in invocations[1:])
        assert [c['index'] for c in json.loads(response.body)['choices']] == [0, 1]


@pytest.mark.parametrize('outcome', ['http_error', 'cancelled'])
async def test_real_continuation_error_and_cancel_finalize_original_month(monthly, monkeypatch, outcome):
    body, route, request, sentinel, _ = setup_route(monthly, 'responses', False)
    entered = asyncio.Event()
    count = 0

    async def model(req):
        nonlocal count
        count += 1
        if count == 1:
            return httpx.Response(200, json=output('responses', 'search'))
        assert log_db._request_handles[request.request_id].db == request.db
        entered.set()
        if outcome == 'cancelled':
            await asyncio.Event().wait()
        return httpx.Response(404, json={'error': {'message': 'model unavailable'}})

    async def execute(calls, **kw):
        assert request.request_id not in log_db._request_handles
        return [web.LocalToolResult(c.id, 'result') for c in calls]

    async def invoke(current):
        response = await monthly.m['failover'].run_failover(
            route, current, request.request_id, 'test-key', '127.0.0.1', False, time.time(),
            ingress_protocol='responses', start_monotonic=time.monotonic())
        monthly.clock[0] = FEB
        return response

    monkeypatch.setattr(web, 'execute_local_tool_calls', execute)
    client = httpx.AsyncClient(transport=httpx.MockTransport(model))
    monthly.m['upstream'].set_client(client)
    task = asyncio.create_task(policy.run(body, 'responses', invoke, request_id=request.request_id, api_key_name='test-key'))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if outcome == 'cancelled':
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert (await task).status_code >= 400
    finally:
        await client.aclose()
    chain = rows(request.db.path, 'retry_chain', request.request_id)
    assert len(chain) == 2 and chain[-1]['outcome'] != 'success'
    assert rows(request.db.path, 'request_log', request.request_id)[0]['status'] == ('cancelled' if outcome == 'cancelled' else 'error')
    assert rows(sentinel.db.path, 'retry_chain', request.request_id) == []
    assert request.request_id not in log_db._request_handles


@pytest.mark.parametrize('outcome', ['invoke_raises', 'invoke_cancelled', 'tool_cancelled', 'round_limit', 'no_search', 'mixed_external'])
async def test_retained_binding_lives_only_inside_a_real_next_invoke(monthly, monkeypatch, outcome):
    body, route, request, sentinel, _ = setup_route(monthly, 'responses', False)
    entered = asyncio.Event()
    count = 0
    retained_handles = []
    original_retain = log_db.retain_request_handle

    def retain(*args):
        # Observe the real method, never substitute a fabricated call-count result.
        handle = original_retain(*args)
        retained_handles.append(handle)
        return handle

    async def model(req):
        obj = output('responses', None if outcome == 'no_search' else 'search')
        if outcome == 'mixed_external':
            obj['output'].append({'type': 'function_call', 'id': 'fc-client', 'call_id': 'client', 'name': 'calculate', 'arguments': '{}'})
        return httpx.Response(200, json=obj)

    if outcome == 'mixed_external':
        body['tools'].append({'type': 'function', 'name': 'calculate', 'parameters': {'type': 'object'}})

    async def execute(calls, **kw):
        assert request.request_id not in log_db._request_handles
        if outcome == 'tool_cancelled':
            entered.set()
            await asyncio.Event().wait()
        return [web.LocalToolResult(c.id, 'result') for c in calls]

    async def invoke(current):
        nonlocal count
        count += 1
        if count == 2:
            assert log_db._request_handles[request.request_id].db == request.db
            entered.set()
            if outcome == 'invoke_raises':
                raise RuntimeError('failure before failover acquired another attempt')
            await asyncio.Event().wait()
        response = await monthly.m['failover'].run_failover(
            route, current, request.request_id, 'test-key', '127.0.0.1', False, time.time(),
            ingress_protocol='responses', start_monotonic=time.monotonic())
        monthly.clock[0] = FEB
        return response

    monkeypatch.setattr(log_db, 'retain_request_handle', retain)
    monkeypatch.setattr(web, 'execute_local_tool_calls', execute)
    if outcome == 'round_limit':
        monkeypatch.setattr(web, 'max_tool_rounds', lambda: 0)
    client = httpx.AsyncClient(transport=httpx.MockTransport(model))
    monthly.m['upstream'].set_client(client)
    task = asyncio.create_task(policy.run(body, 'responses', invoke, request_id=request.request_id, api_key_name='test-key'))
    try:
        if outcome in ('round_limit', 'no_search', 'mixed_external'):
            assert (await task).status_code == (400 if outcome == 'round_limit' else 200)
        else:
            await asyncio.wait_for(entered.wait(), 2)
            if outcome == 'invoke_raises':
                with pytest.raises(RuntimeError, match='failure before failover'):
                    await task
            else:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
    finally:
        await client.aclose()
    assert len(retained_handles) == (1 if outcome.startswith('invoke_') else 0)
    assert all(h.db == request.db for h in retained_handles)
    assert len(rows(request.db.path, 'retry_chain', request.request_id)) == 1
    assert rows(sentinel.db.path, 'retry_chain', request.request_id) == []
    assert request.request_id not in log_db._request_handles
