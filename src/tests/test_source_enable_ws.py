"""Native Responses WS and Codex HTTP-fallback continuation regressions."""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from src import model_mapping, model_state
from src.tests import test_openai_responses_ws as ws_test
from src.tests._config_isolation import isolated_config


@pytest.fixture
def ws_runtime(monkeypatch, isolated_config):
    m = ws_test._import_modules()
    monkeypatch.setattr(m['registry'], '_channels', {})
    cfg = ws_test._setup(m)
    cfg['channelSelection'] = 'order'
    cfg['modelMapping'] = {'global': {'friendly': 'test-model'}}
    one = ws_test._make_channel(m)
    two = ws_test._make_channel(m, extra={'name': 'other'})
    m['registry']._channels = {two.key: two, one.key: one}
    names = ['friendly', 'test-model', 'real-model']
    model_state.set_global_enabled_in_config(cfg, names, False)
    model_state.set_source_enable_override_in_config(cfg, one.key, names, True)
    cfg['modelCenter']['hiddenModels'] = names[:]  # discovery only, not execution
    return m, cfg, one, two


@pytest.mark.asyncio
@pytest.mark.parametrize('revoke', [None, 'friendly', 'test-model', 'real-model'])
async def test_native_ws_selection_reuse_and_live_revoke(ws_runtime, monkeypatch, revoke):
    m, cfg, one, two = ws_runtime
    class Session(ws_test.SequentialFakeWebSocket):
        async def send_text(self, text):
            await super().send_text(text)
            event = json.loads(text)
            if revoke and event.get('type') == 'response.completed' and event['response']['id'] == 'first':
                model_state.set_global_enabled_in_config(cfg, [revoke], False)
    ws = Session(
        {'type': 'response.create', 'model': 'friendly', 'input': 'first'},
        {'type': 'response.create', 'model': 'friendly', 'input': 'second'},
    )
    upstream = ws_test.FakeUpstreamWebSocket([
        {'type': 'response.completed', 'response': {'id': name, 'output': [], 'usage': {'input_tokens': 1, 'output_tokens': 1}}}
        for name in ('first', 'second')
    ])
    connects = []
    async def connect(*args, **kwargs):
        connects.append(kwargs)
        return upstream
    monkeypatch.setattr(m['responses_ws'], '_connect_upstream_ws', connect)
    await asyncio.wait_for(m['responses_ws'].handle_responses_ws(ws), timeout=10)
    assert len(connects) == 1
    assert len(upstream.sent) == (1 if revoke else 2)
    assert all(json.loads(frame)['model'] == 'real-model' for frame in upstream.sent)
    assert all('_parrot_model_alias' not in json.loads(frame) for frame in upstream.sent)
    row = ws_test._last_request_log(m)
    assert row['final_channel_key'] == one.key
    if revoke:
        assert any(json.loads(frame).get('type') == 'error' for frame in ws.sent_texts)
    # A fresh/reconnected request cannot reuse the now-cleared exception either.
    if revoke:
        reconnect = ws_test.FakeWebSocket({'type': 'response.create', 'model': 'friendly', 'input': 'reconnect'})
        await asyncio.wait_for(m['responses_ws'].handle_responses_ws(reconnect), timeout=10)
        assert len(connects) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('revoke', [None, 'friendly', 'test-model', 'real-model'])
async def test_codex_sse_continuation_uses_same_source_checks(ws_runtime, monkeypatch, revoke):
    m, cfg, one, _two = ws_runtime
    runtime = m['responses_ws']
    body = {'model': 'friendly', 'input': 'first'}
    model_mapping.apply_mapping(body, 'openai-responses')
    next_body = {'model': 'friendly', 'input': 'second'}
    model_mapping.apply_mapping(next_body, 'openai-responses')
    queued = [(None, next_body, None), None]
    sent = []
    async def turn(websocket, **kwargs):
        sent.append(kwargs['body'])
        if revoke:
            model_state.set_global_enabled_in_config(cfg, [revoke], False)
        return runtime._WsAttemptResult(ok=True, outcome='success', request_finalized=True)
    async def receive(*a, **kw): return queued.pop(0)
    async def capacity(*a, **kw): return True
    monkeypatch.setattr(runtime, '_run_codex_sse_turn', turn)
    monkeypatch.setattr(runtime, '_receive_next_response_create', receive)
    ws = ws_test.FakeWebSocket({})
    started, mono = time.time(), time.monotonic()
    await runtime._run_codex_sse_fallback_session(ws, ch=one, resolved_model='real-model', body=body,
        allowed_models=None, deadline_ts=started+30, start_time=started, start_monotonic=mono,
        request_id='source-sse', retry_count_so_far=0, affinity_hit=0, api_key_name='ws-key',
        client_ip='127.0.0.1', fp_query=None, client_key=None, retry_attempt_id=None,
        attempt_start_monotonic=mono, turn_capacity=SimpleNamespace(
            release=capacity, acquire_api_key=capacity, acquire_channel=capacity))
    assert len(sent) == (1 if revoke else 2)
    assert bool(ws.sent_texts) == bool(revoke)
