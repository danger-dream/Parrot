"""Regression coverage for routing review R1-R8; all upstreams are local fakes."""
import asyncio
import copy
import json
import time
from types import SimpleNamespace
import pytest
from src.tests import test_openai_responses_ws as w
from src import config, model_state, scheduler, search_tool_policy, search_service
from src.openai import responses_ws, store
from src.openai.transform import responses_to_anthropic


def setup(monkeypatch):
    m = w._import_modules()
    cfg = w._setup(m)
    monkeypatch.setattr(config, '_reload_callbacks', [])
    search_tool_policy._REPLAY.clear()
    return m, cfg

@pytest.mark.parametrize('disable', ['global', 'source'])
async def test_ws_turn_rejects_disabled_model(monkeypatch, disable):
    m, cfg = setup(monkeypatch)
    cfg['apiKeys']['ws-key']['allowedModels'].append('other-model')
    ch = w._make_channel(m, extra={'models': [
        {'alias':'test-model','real':'real-model'},
        {'alias':'other-model','real':'other-real-model'}]})
    cfg['modelCenter'] = {'disabledModels':['other-model']} if disable == 'global' else {'apiSourceDisabledModels':{ch.key:['other-model']}}
    # Independent scheduling rejects exactly this model.
    rejected = scheduler.schedule({'model':'other-model','input':'hello'}, api_key_name='ws-key', client_ip='1.2.3.4', ingress_protocol='responses')
    assert not rejected
    ws = w.SequentialFakeWebSocket(
        {'type':'response.create','model':'test-model','input':'first'},
        {'type':'response.create','model':'other-model','input':'second'},
        {'type':'response.create','model':'test-model','input':'allowed-third'})
    upstream = w.FakeUpstreamWebSocket([
        {'type':'response.completed','response':{'id':'r-first','output':[],'usage':{'input_tokens':2,'output_tokens':1}}},
        {'type':'response.completed','response':{'id':'r-second','output':[],'usage':{'input_tokens':2,'output_tokens':1}}}])
    async def connect(*a, **kw): return upstream
    monkeypatch.setattr(responses_ws, '_connect_upstream_ws', connect)
    await asyncio.wait_for(responses_ws.handle_responses_ws(ws), 2)
    sent = [json.loads(s)['model'] for s in upstream.sent]
    print('DISABLE_BYPASS', disable, rejected.exclusions, sent)
    assert sent == ['real-model', 'real-model']
    assert any(json.loads(frame).get('type') == 'error' for frame in ws.sent_texts)

async def test_managed_ws_disconnect_cancels_work(monkeypatch):
    m, cfg = setup(monkeypatch)
    w._make_channel(m)
    monkeypatch.setattr(search_service, 'settings', lambda: {'functionMode':'managed','hostedMode':'managed'})
    started, cancelled = asyncio.Event(), asyncio.Event()
    async def blocked_run(*a, **kw):
        started.set()
        try: await asyncio.Event().wait()
        finally: cancelled.set()
    monkeypatch.setattr(search_tool_policy, 'run', blocked_run)
    class WS(w.DisconnectBeforeVisibleWebSocket):
        read_count = 0
        async def receive(self):
            self.read_count += 1
            return await super().receive()
    ws = WS({'type':'response.create','model':'test-model','input':'first','tools':[{'type':'web_search'}]})
    task = asyncio.create_task(responses_ws.handle_responses_ws(ws))
    await asyncio.wait_for(started.wait(), 2)
    await asyncio.wait_for(task, 2)
    assert ws.read_count >= 2 and cancelled.is_set()


def test_nonstream_hosted_search_persisted_with_response(monkeypatch):
    m, cfg = setup(monkeypatch)
    saved = []
    monkeypatch.setattr(store, 'is_enabled', lambda: True)
    store.init()
    real_save = store.save
    def save(**kwargs):
        saved.append(copy.deepcopy(kwargs))
        return real_save(**kwargs)
    monkeypatch.setattr(store, 'save', save)
    message = {'id':'msg-native','type':'message','model':'claude-fixture','role':'assistant', 'stop_reason':'end_turn',
        'content':[{'type':'server_tool_use','id':'srv1','name':'web_search','input':{'query':'parrots'}},
                   {'type':'web_search_tool_result','tool_use_id':'srv1','content':[{'type':'web_search_result','url':'https://example.test','title':'Example','encrypted_content':'opaque'}]},
                   {'type':'text','text':'Found it.'}], 'usage':{'input_tokens':2,'output_tokens':1}}
    out = responses_to_anthropic.translate_response(message, model='test-model', api_key_name='ws-key', current_input_items=[{'role':'user','content':'search'}])
    print('VISIBLE_OUTPUT', [i['type'] for i in out['output']], 'STORED_OUTPUT', [i['type'] for i in saved[0]['output_items']])
    assert any(i['type']=='web_search_call' for i in out['output'])
    assert len(saved) == 1
    assert saved[0]['output_items'] == out['output']
    restored = store.expand_history(out['id'], api_key_name='ws-key')
    assert any(item.get('type') == 'web_search_call' for item in restored)

async def test_reconnect_restores_replay_before_scheduling(monkeypatch):
    m, cfg = setup(monkeypatch)
    monkeypatch.setattr(search_service, 'settings', lambda: {'functionMode':'managed','hostedMode':'managed'})
    # xAI has no native previous_response_id capability but managed replay is complete.
    from src.channel.xai_oauth_channel import XAIOAuthChannel
    account = {'provider':'xai','email':'fixture@example.test','subject':'fixture','models':['test-model']}
    ch = XAIOAuthChannel(account)
    monkeypatch.setattr(m['registry'], '_channels', {ch.key:ch})
    history = {'model':'test-model','input':[{'role':'user','content':'first'}, {'role':'assistant','content':'answer'}], 'tools':[{'type':'web_search'}]}
    search_tool_policy._remember(history, 'responses', 'ws-key', ['saved-response'])
    body = {'model':'test-model','input':'next','previous_response_id':'saved-response'}
    restored = search_tool_policy.restore_replay(body, 'responses', 'ws-key')
    assert 'previous_response_id' not in restored
    good = scheduler.schedule(restored, api_key_name='ws-key', client_ip='1.2.3.4', ingress_protocol='responses')
    assert good
    invoked = []
    async def unexpected(*a, **kw):
        invoked.append(kw['body'])
        return True
    monkeypatch.setattr(responses_ws, '_run_search_ws_session', unexpected)
    ws = w.FakeWebSocket({'type':'response.create', **body})
    await responses_ws.handle_responses_ws(ws)
    print('RECONNECT_CLOSE', ws.close_calls, 'RESTORED_ROUTES', len(good.candidates))
    assert len(invoked) == 1 and not ws.close_calls
    assert 'previous_response_id' not in invoked[0]
    assert invoked[0]['tools'] == history['tools']


def test_native_search_stream_allocates_fresh_text_blocks(monkeypatch):
    setup(monkeypatch)
    from src.openai.transform.stream_responses_to_anthropic import StreamTranslator
    t = StreamTranslator(model='m')
    events = [
        {'type':'response.output_text.delta','output_index':0,'delta':'Before.'},
        {'type':'response.output_item.done','output_index':1,'item':{'type':'web_search_call','id':'ws_1','status':'completed','action':{'type':'search','query':'q','sources':[]}}},
        {'type':'response.output_text.delta','output_index':2,'delta':'After.'},
        {'type':'response.completed','response':{'id':'r','status':'completed','output':[]}}]
    output = []
    for event in events:
        output.extend(t.feed(('event: '+event['type']+'\ndata: '+json.dumps(event)+'\n\n').encode()))
    output.extend(t.close())
    objects = [json.loads(line[6:]) for chunk in output for line in chunk.decode().splitlines() if line.startswith('data: ')]
    starts = [o['index'] for o in objects if o['type']=='content_block_start']
    print('STREAM_START_INDICES', starts)
    assert starts == [0,1,2,3]
    assert [o['index'] for o in objects if o['type']=='content_block_stop'] == starts
    content = t.get_downstream_anthropic_assistant()['content']
    assert [b['type'] for b in content] == ['text','server_tool_use','web_search_tool_result','text']
    assert content[0]['text'] == 'Before.' and content[-1]['text'] == 'After.'

async def test_output_clamp_preserves_thinking_budget(monkeypatch):
    m, cfg = setup(monkeypatch)
    from src import failover
    from src.channel.api_channel import ApiChannel
    ch = ApiChannel({'name':'think','baseUrl':'https://example.invalid','apiKey':'fixture','cc_mimicry':False,'models':[{'alias':'m','real':'claude-sonnet-4-20250514'}]})
    body = {'model':'m','messages':[{'role':'user','content':'hello'}], 'max_tokens':10000, 'thinking':{'type':'enabled','budget_tokens':8192}}
    monkeypatch.setattr(failover.model_metadata,'max_output_tokens',lambda *a, **kw:4096)
    clamped = failover._candidate_budget_body(ch, 'claude-sonnet-4-20250514', body)
    old_wire = json.loads((await ch.build_upstream_request(body,'claude-sonnet-4-20250514')).body)
    wire = json.loads((await ch.build_upstream_request(clamped,'claude-sonnet-4-20250514')).body)
    print('THINKING_CLAMP', old_wire['max_tokens'], wire['max_tokens'], wire['thinking'])
    assert old_wire['thinking']['budget_tokens'] < old_wire['max_tokens']
    assert wire['thinking']['budget_tokens'] == wire['max_tokens'] - 1
    assert body['thinking']['budget_tokens'] == 8192 and body['max_tokens'] == 10000

async def test_http_model_header_recorded(monkeypatch):
    import httpx
    from src import failover, log_db, upstream, model_reroute
    m, cfg = setup(monkeypatch)
    ch = w._make_channel(m)
    rid = 'header-proof'
    body = {'model':'test-model','input':'hi','stream':False}
    log_db.insert_pending(rid, '1.2.3.4', 'ws-key', 'test-model', False, 1,0,{},body, ingress_protocol='responses')
    seen = []
    def transport(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, headers={'openai-model':'rerouted-real-model'}, json={'id':'resp_header','object':'response','model':'real-model','status':'completed','output':[], 'usage':{'input_tokens':1,'output_tokens':1}})
    monkeypatch.setattr(upstream, '_client_pool', upstream.SharedClientPool(upstream._new_client))
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        upstream.set_client(client)
        response = await failover.run_failover(scheduler.ScheduleResult([(ch,'real-model')],None,False), body,rid,'ws-key','1.2.3.4',False,time.time(), ingress_protocol='responses')
    row = log_db._get_conn().execute('SELECT upstream_actual_model FROM request_log WHERE request_id=?',(rid,)).fetchone()
    expected = model_reroute.resolve_actual_model(outbound_model='real-model', signals=model_reroute.ResponseModelSignals(body_model='real-model'), http_header_model='rerouted-real-model')
    print('HEADER_OBSERVATION',response.status_code, seen[0]['model'], row['upstream_actual_model'], 'EXTRACTOR_EXPECTS',expected)
    assert response.status_code == 200 and row['upstream_actual_model'] == expected and expected == 'rerouted-real-model'

async def test_1311_backoff_includes_queued_candidate(monkeypatch):
    from src import failover, log_db, cooldown, channel_state
    from src.protocols.runtime import AttemptResult
    from src.channel.api_channel import ApiChannel
    m, cfg = setup(monkeypatch)
    cfg['errorWindows'] = [1,3,5,10,15,0]
    detail = 'HTTP 429: {"error":{"code":"1311","message":"[1311][Your current subscription plan does not yet include access to X]"}}'
    async def result(*a, **kw): return AttemptResult(outcome='http_error',http_status=429,error_detail=detail,full_response_text=detail)
    monkeypatch.setattr(failover,'_try_channel', result)
    notices = []
    monkeypatch.setattr(failover,'_notify_zhipu_quota_cooldown', lambda *a, **kw:notices.append(kw))
    async def acquire(*a, **kw): return True
    async def queue(candidates, timeout): return candidates[0]
    monkeypatch.setattr(failover.concurrency,'try_acquire',acquire)
    monkeypatch.setattr(failover.concurrency,'acquire_from_candidates',queue)
    monkeypatch.setattr(failover.concurrency,'release',lambda *a:None)
    minutes = []
    for queued in (False, True):
        ch = ApiChannel({'name':'zhipu-proof-'+str(queued), 'baseUrl':'https://api.z.ai','models':[{'alias':'m','real':'m'}]})
        rid = '1311-proof-'+str(queued)
        body = {'model':'m','messages':[{'role':'user','content':'hi'}]}
        log_db.insert_pending(rid,'1.2.3.4','ws-key','m',False,1,0,{},body)
        route = scheduler.ScheduleResult([] if queued else [(ch,'m')],None,False,saturated=[(ch,'m')] if queued else [])
        now = time.time()*1000
        await failover.run_failover(route,body,rid,'ws-key','1.2.3.4',False,time.time())
        state = cooldown.get_state(channel_state.effect_key(ch),'m')
        minutes.append(round((state['cooldown_until']-now)/60000))
    print('PLAN_1311_COOLDOWN_MINUTES', minutes)
    assert minutes == [15,15]
    assert len(notices) == 2 and all(n['plan_excluded'] for n in notices)


@pytest.mark.parametrize('limit', [4096, 1025, 1024, 512])
@pytest.mark.parametrize('nested', [False, True])
def test_thinking_clamp_bounds_and_input_immutability(monkeypatch, limit, nested):
    from src import failover
    setup(monkeypatch)
    ch = SimpleNamespace(key='api:thinking', protocol='anthropic')
    monkeypatch.setattr(failover.model_metadata, 'max_output_tokens', lambda *a, **kw: limit)
    payload = {'model':'m', 'max_tokens':10000, 'thinking':{'type':'enabled','budget_tokens':8192}}
    body = {'model':'m', 'response':payload} if nested else payload
    original = copy.deepcopy(body)
    for clamp in (
        lambda: failover._candidate_budget_body(ch, 'm', body),
        lambda: failover._clamp_wire_output_limit(ch, 'm', body, body, None),
    ):
        out = clamp()
        wire = out['response'] if nested else out
        assert wire['max_tokens'] == limit
        if limit > 1024:
            assert 1024 <= wire['thinking']['budget_tokens'] < limit
        else:
            from src.openai.transform.guard import GuardError
            assert wire['thinking'] == original.get('response', original)['thinking']
            with pytest.raises(GuardError, match='Enabled thinking requires') as exc:
                failover._validate_wire_payload_budget(ch, 'm', body, out, None)
            assert exc.value.scope == 'candidate'
        assert body == original
    payload['thinking'] = {'type':'adaptive'}
    out = failover._candidate_budget_body(ch, 'm', body)
    assert (out['response'] if nested else out)['thinking'] == {'type':'adaptive'}


@pytest.mark.parametrize('disable', ['global','source'])
def test_final_wire_rechecks_model_disable(monkeypatch, disable):
    from src import failover
    from src.openai.transform.guard import GuardError
    _, cfg = setup(monkeypatch)
    ch = SimpleNamespace(key='api:race')
    cfg['modelCenter'] = {'disabledModels':['m']} if disable == 'global' else {'apiSourceDisabledModels':{ch.key:['m']}}
    with pytest.raises(GuardError, match='disabled'):
        failover._validate_wire_payload_budget(ch, 'upstream', {'model':'m'}, {'model':'upstream'}, None)


@pytest.mark.parametrize('control', ['disconnect', 'cancel'])
async def test_search_ws_control_cancels_real_http_round_and_releases_capacity(monkeypatch, control):
    import httpx
    from src import upstream
    m, cfg = setup(monkeypatch)
    ch = w._make_channel(m)
    monkeypatch.setattr(search_service, 'settings', lambda: {'functionMode':'managed','hostedMode':'managed'})
    waiting, closed = asyncio.Event(), asyncio.Event()
    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            waiting.set()
            await asyncio.Event().wait()
            yield b''
        async def aclose(self):
            closed.set()
    class WS(w.FakeWebSocket):
        async def receive(self):
            if self._first_text is not None:
                return await super().receive()
            await waiting.wait()
            if control == 'disconnect':
                return {'type':'websocket.disconnect','code':1000}
            return {'type':'websocket.receive','text':json.dumps({'type':'response.cancel'})}
    ws = WS({'type':'response.create','model':'test-model','input':'hi','tools':[{'type':'web_search'}]})
    monkeypatch.setattr(upstream, '_client_pool', upstream.SharedClientPool(upstream._new_client))
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, headers={'openai-model':'cancel-model'}, stream=SlowStream()))) as client:
        upstream.set_client(client)
        await asyncio.wait_for(responses_ws.handle_responses_ws(ws), 3)
    assert closed.is_set()
    assert m['apikey_limiter'].key_snapshot('ws-key')['in_flight'] == 0
    channels = {r['channel_key']:r for r in m['concurrency'].snapshot()}
    assert channels[ch.key]['in_flight'] == 0
    assert w._last_request_log(m)['status'] == 'cancelled'
    assert w._last_request_log(m)['upstream_actual_model'] == 'cancel-model'
    if control == 'cancel':
        assert ws.close_calls == [(1000,'response cancelled')]


async def test_search_ws_sequential_terminal_handoff(monkeypatch):
    from starlette.responses import JSONResponse
    m, cfg = setup(monkeypatch)
    w._make_channel(m)
    monkeypatch.setattr(search_service, 'settings', lambda: {'functionMode':'managed','hostedMode':'managed'})
    inputs = []
    async def run(body, *args, **kwargs):
        inputs.append(body['input'])
        return JSONResponse({'id':'r'+str(len(inputs)), 'object':'response', 'status':'completed', 'output':[]})
    monkeypatch.setattr(search_tool_policy, 'run', run)
    ws = w.SequentialFakeWebSocket(*[
        {'type':'response.create','model':'test-model','input':text,'tools':[{'type':'web_search'}]}
        for text in ('first','second')])
    await asyncio.wait_for(responses_ws.handle_responses_ws(ws), 3)
    assert inputs == ['first','second']
    assert [json.loads(f)['type'] for f in ws.sent_texts].count('response.completed') == 2
    assert not any(json.loads(f)['type']=='error' for f in ws.sent_texts)
    assert m['apikey_limiter'].key_snapshot('ws-key')['in_flight'] == 0


def _observation_events(terminal):
    response = {'id':'observed', 'model':'rerouted-model', 'status':terminal,
                'output':[{'type':'message','role':'assistant','content':[{'type':'output_text','text':'x'*300000}]}],
                'usage':{'input_tokens':3,'output_tokens':2}}
    if terminal == 'failed':
        response['error'] = {'code':'server_error','message':'fixture failure'}
    events = [
        {'type':'response.metadata','metadata':{'type':'safety_buffering','reasons':['prefix-review']}},
        {'type':'response.in_progress','response':{'id':'observed','model':'rerouted-model'}},
        {'type':'response.output_text.delta','output_index':0,'content_index':0,'delta':'x'*300000},
    ]
    if terminal != 'cancelled':
        events.append({'type':'response.'+terminal,'response':response,
                       'safety_buffering':{'reasons':['terminal-review']}})
    return events


@pytest.mark.parametrize('mode', ['native', 'http-json', 'http-stream'])
@pytest.mark.parametrize('terminal', ['completed', 'failed', 'cancelled'])
async def test_ws_observation_survives_display_truncation_end_to_end(monkeypatch, mode, terminal):
    m, cfg = setup(monkeypatch)
    cfg.setdefault('openai', {})['responsesUpstreamWsForOAuth'] = True
    cfg['retry'] = {'transient':{'enabled':False}}
    ch = w._make_oauth_channel_for_failover(m, name='signals@example.test')
    async def token(*a, **kw): return 'fixture'
    monkeypatch.setattr(m['failover'].oauth_manager, 'ensure_valid_token', token)
    events = _observation_events(terminal)
    upstream = w.BlockingAfterEventsWebSocket(events) if terminal == 'cancelled' else w.FakeOAuthHttpWs(events)
    async def connect(*a, **kw): return upstream
    monkeypatch.setattr(responses_ws, '_connect_upstream_ws', connect)
    monkeypatch.setattr(m['failover'], '_connect_oauth_responses_ws', connect)
    async def call():
        if mode == 'native':
            ws = w.FakeWebSocket({'type':'response.create','model':'test-model','input':'hi'})
            await responses_ws.handle_responses_ws(ws)
        else:
            response, rid = await w._call_failover_responses(m, ch, {'model':'test-model','input':'hi','stream':mode=='http-stream'})
            if hasattr(response, 'body_iterator'):
                async for chunk in response.body_iterator:
                    pass
    task = asyncio.create_task(call())
    if terminal == 'cancelled':
        await asyncio.wait_for(upstream.waiting.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await asyncio.wait_for(task, 3)
    row = w._last_request_log(m)
    assert row['status'] == {'completed':'success','failed':'error','cancelled':'cancelled'}[terminal]
    assert row['upstream_actual_model'] == 'rerouted-model'
    review = json.loads(row['safety_review'])
    assert 'prefix-review' in review['reasons']
    if terminal != 'cancelled':
        assert 'terminal-review' in review['reasons']
    detail = m['log_db'].log_detail(row['request_id'])
    assert len(detail['detail']['response_body']) == 200000


@pytest.mark.parametrize('mode', ['json','stream','aggregate','ws-sse'])
@pytest.mark.parametrize('embedded', [False, True])
async def test_http_header_observation_priority_all_response_paths(monkeypatch, mode, embedded):
    import httpx
    from src import failover, upstream, log_db
    m, cfg = setup(monkeypatch)
    ch = w._make_channel(m)
    if mode == 'aggregate':
        ch.upstream_stream_only = True
    if mode == 'ws-sse':
        ch.responses_ws_upstream_transport = 'sse'
    obj = {'id':'header-priority','object':'response','model':'body-model','status':'completed',
           'output':[], 'usage':{'input_tokens':2,'output_tokens':1}}
    if embedded:
        obj['headers'] = {'OpenAI-Model':'embedded-model'}
    terminal = {'type':'response.completed','response':obj}
    events = [{'type':'response.output_text.delta','output_index':0,'content_index':0,'delta':'ok'},terminal]
    def transport(request):
        if mode == 'json':
            return httpx.Response(200, headers={'x-openai-model':'header-model'}, json=obj)
        content = ''.join('event: '+e['type']+'\ndata: '+json.dumps(e)+'\n\n' for e in events)
        return httpx.Response(200, headers={'openai-model':'header-model','content-type':'text/event-stream'}, content=content)
    monkeypatch.setattr(upstream, '_client_pool', upstream.SharedClientPool(upstream._new_client))
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        upstream.set_client(client)
        if mode == 'ws-sse':
            await responses_ws.handle_responses_ws(w.FakeWebSocket({'type':'response.create','model':'test-model','input':'hi'}))
            row = w._last_request_log(m)
        else:
            rid = 'http-header-'+mode+'-'+str(embedded)
            body = {'model':'test-model','input':'hi','stream':mode=='stream'}
            log_db.insert_pending(rid,'1.2.3.4','ws-key','test-model',body['stream'],1,0,{},body,ingress_protocol='responses')
            response = await failover.run_failover(scheduler.ScheduleResult([(ch,'real-model')],None,False),body,rid,'ws-key','1.2.3.4',body['stream'],time.time(),ingress_protocol='responses')
            if hasattr(response, 'body_iterator'):
                async for _ in response.body_iterator:
                    pass
            row = log_db.log_detail(rid)['log']
    assert row['status'] == 'success'
    assert row['upstream_actual_model'] == ('embedded-model' if embedded else 'header-model')


@pytest.mark.parametrize('mode', ['http','ws-sse'])
async def test_http_error_header_observation(monkeypatch, mode):
    import httpx
    from src import failover, upstream, log_db
    m, cfg = setup(monkeypatch)
    ch = w._make_channel(m)
    cfg['retry'] = {'transient':{'enabled':False}}
    if mode == 'ws-sse':
        ch.responses_ws_upstream_transport = 'sse'
    monkeypatch.setattr(upstream, '_client_pool', upstream.SharedClientPool(upstream._new_client))
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(400,headers={'openai-model':'rejected-model'},json={'error':{'message':'fixture rejection'}}))) as client:
        upstream.set_client(client)
        if mode == 'ws-sse':
            await responses_ws.handle_responses_ws(w.FakeWebSocket({'type':'response.create','model':'test-model','input':'hi'}))
            row = w._last_request_log(m)
        else:
            rid = 'header-error'
            body = {'model':'test-model','input':'hi'}
            log_db.insert_pending(rid,'1.2.3.4','ws-key','test-model',False,1,0,{},body,ingress_protocol='responses')
            await failover.run_failover(scheduler.ScheduleResult([(ch,'real-model')],None,False),body,rid,'ws-key','1.2.3.4',False,time.time(),ingress_protocol='responses')
            row = log_db.log_detail(rid)['log']
    assert row['status'] == 'error'
    assert row['upstream_actual_model'] == 'rejected-model'


@pytest.mark.parametrize('limit', [512, 1024])
@pytest.mark.parametrize('has_fallback', [False, True])
async def test_small_cap_enabled_thinking_candidate_fails_over(monkeypatch, limit, has_fallback):
    import httpx
    from src import failover, upstream, log_db
    from src.channel.api_channel import ApiChannel
    setup(monkeypatch)
    channels = [ApiChannel({
        'name': name, 'baseUrl': 'https://'+name+'.invalid', 'apiKey':'fixture',
        'cc_mimicry':False, 'models':[{'alias':'m','real':'claude-sonnet-4-20250514'}],
    }) for name in ('small-thinking-cap', 'large-thinking-cap')]
    monkeypatch.setattr(failover.model_metadata, 'max_output_tokens',
                        lambda *a, **kw: limit if kw['scope_key'] == channels[0].key else 4096)
    body = {'model':'m','messages':[{'role':'user','content':'hello'}],
            'max_tokens':10000, 'thinking':{'type':'enabled','budget_tokens':8192}}
    original = copy.deepcopy(body)
    rid = f'thinking-cap-{limit}-{has_fallback}'
    log_db.insert_pending(rid, '1.2.3.4', 'ws-key', 'm', False, 1, 0, {}, body)
    sent = []
    def transport(request):
        sent.append((request.url.host, json.loads(request.content)))
        return httpx.Response(200, json={
            'id':'thinking-ok','type':'message','model':'claude-sonnet-4-20250514',
            'role':'assistant','content':[{'type':'text','text':'ok'}],
            'stop_reason':'end_turn','usage':{'input_tokens':2,'output_tokens':1},
        })
    monkeypatch.setattr(upstream, '_client_pool', upstream.SharedClientPool(upstream._new_client))
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        upstream.set_client(client)
        route = scheduler.ScheduleResult(
            [(ch, 'claude-sonnet-4-20250514') for ch in channels[:2 if has_fallback else 1]], None, False,
        )
        response = await failover.run_failover(route, body, rid, 'ws-key', '1.2.3.4', False, time.time())
    assert body['thinking'] == original['thinking'] and body['max_tokens'] == original['max_tokens']
    attempts = [dict(row) for row in log_db._get_conn().execute(
        'SELECT * FROM retry_chain WHERE request_id=? ORDER BY attempt_order', (rid,),
    )]
    assert attempts[0]['outcome'] == 'candidate_guard'
    assert 'Enabled thinking requires' in attempts[0]['error_detail']
    if has_fallback:
        assert response.status_code == 200
        assert len(sent) == 1 and sent[0][0] == 'large-thinking-cap.invalid'
        assert sent[0][1]['max_tokens'] == 4096
        assert sent[0][1]['thinking'] == {'type':'enabled','budget_tokens':4095}
    else:
        assert response.status_code == 400 and not sent
        assert b'Enabled thinking requires' in response.body


@pytest.mark.parametrize('policy', ['omit', 'adaptive', 'opus-adaptive'])
async def test_small_cap_thinking_respects_final_channel_policy(monkeypatch, policy):
    from src import failover
    from src.channel.api_channel import ApiChannel
    setup(monkeypatch)
    model = 'claude-opus-4-7' if policy == 'opus-adaptive' else 'claude-sonnet-4-20250514'
    ch = ApiChannel({'name':'thinking-policy','baseUrl':'https://example.invalid','apiKey':'fixture',
                     'cc_mimicry':False, 'omitThinking':policy == 'omit',
                     'models':[{'alias':'m','real':model}]})
    thinking = {'type':'adaptive'} if policy == 'adaptive' else {'type':'enabled','budget_tokens':8192}
    body = {'model':'m','messages':[{'role':'user','content':'hi'}], 'max_tokens':10000,'thinking':thinking}
    original = copy.deepcopy(body)
    monkeypatch.setattr(failover.model_metadata, 'max_output_tokens', lambda *a, **kw:1024)
    candidate = failover._candidate_budget_body(ch, model, body)
    req = await ch.build_upstream_request(candidate, model)
    wire = json.loads(req.body)
    failover._validate_wire_payload_budget(ch, model, candidate, req.body, req.dispatch_metadata)
    assert wire['max_tokens'] == 1024
    if policy == 'omit':
        assert 'thinking' not in wire
    else:
        assert wire['thinking'] == {'type':'adaptive'}
    assert body == original


@pytest.mark.parametrize('protocol', ['anthropic', 'openai-chat'])
def test_final_thinking_floor_is_anthropic_only(monkeypatch, protocol):
    from src import failover
    from src.openai.transform.guard import GuardError
    setup(monkeypatch)
    ch = SimpleNamespace(key='api:thinking-protocol', protocol=protocol)
    # The same keys do not imply the same protocol: DeepSeek Chat deliberately
    # emits no budget_tokens, and its small output limit remains valid.
    payload = {'model':'deepseek-v4-flash', 'max_tokens':256, 'thinking':{'type':'enabled'}}
    original = copy.deepcopy(payload)
    if protocol == 'anthropic':
        with pytest.raises(GuardError, match='Enabled thinking requires') as exc:
            failover._validate_wire_payload_budget(ch, payload['model'], payload, payload, None)
        assert exc.value.scope == 'candidate'
    else:
        failover._validate_wire_payload_budget(ch, payload['model'], payload, payload, None)
    assert payload == original
