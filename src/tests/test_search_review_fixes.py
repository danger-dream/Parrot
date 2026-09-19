"""Focused regressions for controller-found execution/privacy/transport gaps."""
import asyncio
import copy
import json
from types import SimpleNamespace

import pytest
from fastapi.responses import JSONResponse, StreamingResponse
from src import search_tool_policy as policy, local_web_tools as web, search_service
from src.openai.transform.guard import GuardError


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    policy._REPLAY.clear()
    settings = {**search_service.DEFAULTS, "maxToolRounds": 4}
    monkeypatch.setattr(web, '_settings', lambda: settings)
    monkeypatch.setattr(web, 'max_tool_rounds', lambda: 4)
    yield settings
    policy._REPLAY.clear()


def req(protocol='responses'):
    tool = {'type':'function','name':'web_search','parameters':{'type':'object'}}
    if protocol == 'chat':tool = {'type':'function','function':{k:v for k,v in tool.items() if k!='type'}}
    if protocol == 'anthropic':tool = {'name':'web_search','input_schema':{'type':'object'}}
    return {'model':'m','tools':[tool], **({'input':[{'role':'user','content':'conversation A'}]} if protocol=='responses' else {'messages':[{'role':'user','content':'conversation A'}]})}


def answer(protocol='responses', calls=(), text='answer', usage=None):
    if protocol == 'responses':
        obj = {'id':'r-final' if not calls else 'r-calls','object':'response','status':'completed','output':[{'type':'function_call','call_id':i,'id':'fc-'+i,'name':n,'arguments':json.dumps(a)} for i,n,a in calls] or [{'type':'message','role':'assistant','content':[{'type':'output_text','text':text}]}]}
    elif protocol == 'chat':
        obj = {'id':'chat','object':'chat.completion','choices':[{'index':0,'message':{'role':'assistant','content':None if calls else text, **({'tool_calls':[{'id':i,'type':'function','function':{'name':n,'arguments':json.dumps(a)}} for i,n,a in calls]} if calls else {})},'finish_reason':'tool_calls' if calls else 'stop'}]}
    else:
        obj = {'id':'msg','type':'message','role':'assistant','content':[{'type':'tool_use','id':i,'name':n,'input':a} for i,n,a in calls] or [{'type':'text','text':text}],'stop_reason':'tool_use' if calls else 'end_turn'}
    if usage is not None:obj['usage']=copy.deepcopy(usage)
    return obj


@pytest.mark.parametrize('protocol', ['chat','responses','anthropic'])
def test_replay_full_prefix_collision_and_repeated_submission(protocol):
    visible=req(protocol);saved=copy.deepcopy(visible)
    client=answer(protocol,[('client-id','calculate',{'x':1})])
    policy._append(visible,client,[],protocol)
    policy._append(saved,answer(protocol,[('search-id','web_search',{'query':'docs'})]),[web.LocalToolResult('search-id','private search A')],protocol)
    policy._append(saved,client,[],protocol)
    policy._remember(saved,protocol,'shared',['client-id'],visible_body=visible)
    other=copy.deepcopy(saved);other_visible=copy.deepcopy(visible)
    policy._history(other,protocol)[0]['content']='conversation B private'
    policy._history(other_visible,protocol)[0]['content']='conversation B private'
    policy._remember(other,protocol,'shared',['client-id'],visible_body=other_visible)
    result=({'type':'function_call_output','call_id':'client-id','output':'client-result'} if protocol=='responses' else {'role':'tool','tool_call_id':'client-id','content':'client-result'} if protocol=='chat' else {'role':'user','content':[{'type':'tool_result','tool_use_id':'client-id','content':'client-result'}]})
    delta={'model':'m', 'input' if protocol=='responses' else 'messages':[result]}
    assert policy.restore_replay(delta,protocol,'shared') == delta  # ambiguous modern ID
    full=copy.deepcopy(visible);policy._history(full,protocol).append(result)
    expanded=policy.restore_replay(full,protocol,'shared')
    assert 'private search A' in json.dumps(expanded) and 'conversation B' not in json.dumps(expanded)
    assert policy.restore_replay(full,protocol,'shared')==expanded  # retry is stable
    assert policy.restore_replay(expanded,protocol,'shared')==expanded  # handler+run is idempotent
    conflict=copy.deepcopy(full);policy._history(conflict,protocol)[0]['content']='conversation C'
    assert policy.restore_replay(conflict,protocol,'shared')==conflict


def test_legacy_name_delta_never_recovers_and_matching_full_history_does():
    saved=req('chat');saved['messages'] += [{'role':'assistant','content':'private hidden'}, {'role':'assistant','function_call':{'name':'calculate','arguments':'{}'}}]
    visible=req('chat');visible['messages'].append(saved['messages'][-1])
    policy._remember(saved,'chat','k',['calculate'],visible_body=visible)
    delta={'model':'m','messages':[{'role':'function','name':'calculate','content':'1'}]}
    assert policy.restore_replay(delta,'chat','k')==delta
    full=copy.deepcopy(visible);full['messages']+=delta['messages']
    assert 'private hidden' in json.dumps(policy.restore_replay(full,'chat','k'))


@pytest.mark.parametrize('protocol', ['chat','responses','anthropic'])
@pytest.mark.parametrize('conflict', [False,True])
async def test_duplicate_ids_preflight_before_any_side_effect(protocol,conflict,monkeypatch):
    searches=[]
    async def execute(calls,**kw):
        searches.extend(calls);return [web.LocalToolResult(c.id,'result') for c in calls]
    monkeypatch.setattr(web,'execute_local_tool_calls',execute)
    rounds=[]
    async def invoke(body):
        rounds.append(copy.deepcopy(body))
        return JSONResponse(answer(protocol,[('same','web_search',{'query':'one'}),('same','web_search',{'query':'two' if conflict else 'one'})] if len(rounds)==1 else []))
    response=await policy.run(req(protocol),protocol,invoke)
    if conflict:
        assert response.status_code==502 and b'tool_call_id_conflict' in response.body
        assert not searches and len(rounds)==1
    else:
        assert response.status_code==200 and len(searches)==1
        refs=[ref for item in policy._history(rounds[1],protocol) for ref,_ in policy._result_refs(item)]
        assert refs==['same']


async def test_completed_history_id_cannot_trigger_second_effect(monkeypatch):
    body=req();policy._append(body,answer(calls=[('done','web_search',{'query':'docs'})]),[web.LocalToolResult('done','actual earlier result')],'responses')
    async def execute(*a,**kw):pytest.fail('completed ID executed again')
    monkeypatch.setattr(web,'execute_local_tool_calls',execute)
    async def invoke(_):return JSONResponse(answer(calls=[('done','web_search',{'query':'other'})]))
    response=await policy.run(body,'responses',invoke)
    assert response.status_code==502 and b'tool_call_id_conflict' in response.body


@pytest.mark.parametrize('protocol', ['chat','responses','anthropic'])
async def test_usage_nested_totals_stream_and_new_client_turn(protocol,monkeypatch):
    usage={'input_tokens':10,'output_tokens':4,'total_tokens':14,'input_tokens_details':{'cached_tokens':3},'output_tokens_details':{'reasoning_tokens':2},'cost':0.25}
    if protocol=='chat':usage={'prompt_tokens':10,'completion_tokens':4,'total_tokens':14,'prompt_tokens_details':{'cached_tokens':3},'completion_tokens_details':{'reasoning_tokens':2},'cost':0.25}
    if protocol=='anthropic':usage={'input_tokens':10,'output_tokens':4,'cache_read_input_tokens':3,'cache_creation_input_tokens':5,'cache_creation':{'ephemeral_5m_input_tokens':2,'ephemeral_1h_input_tokens':3},'server_tool_use':{'web_search_requests':1}}
    count=0
    async def execute(calls,**kw):return [web.LocalToolResult(c.id,'result') for c in calls]
    monkeypatch.setattr(web,'execute_local_tool_calls',execute)
    async def invoke(_):
        nonlocal count
        count+=1
        return JSONResponse(answer(protocol,[('search','web_search',{'query':'docs'})] if count==1 else [],usage=usage))
    obj=json.loads((await policy.run(req(protocol),protocol,invoke,api_key_name='k')).body)
    expected={};policy._sum_usage(expected,usage);policy._sum_usage(expected,usage)
    assert obj['usage']==expected
    count=0
    streaming_body={**req(protocol),'stream_options':{'include_usage':True}}
    raw=b''.join([c async for c in policy.stream(streaming_body,protocol,invoke).body_iterator])
    frames=[json.loads(line[5:]) for line in raw.splitlines() if line.startswith(b'data:') and line[5:].strip()!=b'[DONE]']
    if protocol=='anthropic':
        start=next(f['message']['usage'] for f in frames if f.get('type')=='message_start')
        assert start['cache_creation']==usage['cache_creation']
        assert start['server_tool_use']==usage['server_tool_use']
        final_usage=next(f['usage'] for f in frames if f.get('type')=='message_delta')
        assert final_usage['cache_creation']==expected['cache_creation']
        assert final_usage['server_tool_use']==expected['server_tool_use']
        assert next(f['usage']['output_tokens'] for f in frames if f.get('type')=='message_delta')==8
    else:
        final=next(f['response'] for f in frames if f.get('type')=='response.completed') if protocol=='responses' else next(f for f in frames if 'usage' in f)
        assert final['usage']==expected
    later=req(protocol)
    if protocol=='responses':later.update(previous_response_id='r-final',input='continue')
    assert json.loads((await policy.run(later,protocol,invoke,api_key_name='k')).body)['usage']==usage


async def test_chat_three_branches_mixed_external_and_finished_preserved(monkeypatch):
    body=req('chat');body['n']=3;searches=[];rounds=[]
    async def execute(calls,**kw):
        searches.extend(calls);return [web.LocalToolResult(c.id,'result '+c.id) for c in calls]
    monkeypatch.setattr(web,'execute_local_tool_calls',execute)
    async def invoke(current):
        rounds.append(copy.deepcopy(current))
        if len(rounds)==1:
            a=answer('chat',[('s0','web_search',{'query':'zero'}),('client0','calculate',{})])['choices'][0];a['index']=4
            b=answer('chat',text='finished B')['choices'][0];b['index']=7
            c=answer('chat',[('s2','web_search',{'query':'two'})])['choices'][0];c['index']=9
            return JSONResponse({'id':'initial','object':'chat.completion','choices':[a,b,c],'usage':{'prompt_tokens':10,'completion_tokens':6,'total_tokens':16}})
        assert current['n']==1 and 'client0' not in json.dumps(current) and 'finished B' not in json.dumps(current)
        assert 'result s2' in json.dumps(current)
        return JSONResponse(answer('chat',text='finished C',usage={'prompt_tokens':8,'completion_tokens':3,'total_tokens':11}))
    response=json.loads((await policy.run(body,'chat',invoke,api_key_name='k')).body)
    assert [c['index'] for c in response['choices']]==[4,7,9]
    assert [c['message'].get('content') for c in response['choices']]==[None,'finished B','finished C']
    assert [c[1] for c in policy._calls({'choices':[response['choices'][0]]},'chat')]==['client0']
    assert response['usage']=={'prompt_tokens':18,'completion_tokens':9,'total_tokens':27}
    assert len(rounds)==2 and len(searches)==2
    resumed=copy.deepcopy(body);resumed['n']=1
    resumed['messages'] += [response['choices'][0]['message'],{'role':'tool','tool_call_id':'client0','content':'client reply'}]
    restored=policy.restore_replay(resumed,'chat','k')
    assert 'result s0' in json.dumps(restored) and 'result s2' not in json.dumps(restored) and 'finished B' not in json.dumps(restored)


async def test_declared_limits_and_options_reach_service_once(monkeypatch):
    body=req('anthropic');body['tools']=[{'type':'web_search_20250305','name':'web_search','max_uses':1,'search_context_size':'high','user_location':{'type':'approximate','country':'GB'}}]
    calls=[];rounds=[]
    async def execute(batch,**kw):
        calls.extend(batch);return [web.LocalToolResult(c.id,'result') for c in batch]
    monkeypatch.setattr(web,'execute_local_tool_calls',execute)
    async def invoke(current):
        rounds.append(copy.deepcopy(current));name=current['tools'][0]['name']
        if len(rounds)<3:return JSONResponse(answer('anthropic',[(str(len(rounds)),name,{'query':'docs','max_uses':999,'search_context_size':'low','user_location':{'country':'US'}})]))
        assert 'max_uses_exceeded' in json.dumps(current)
        return JSONResponse(answer('anthropic'))
    response=await policy.run(body,'anthropic',invoke)
    assert response.status_code==200 and len(calls)==1 and len(rounds)==3
    assert calls[0].input['max_uses']==1 and calls[0].input['search_context_size']=='high' and calls[0].input['user_location']=={'type':'approximate','country':'GB'}


def test_content_budget_utf8_is_bounded_and_json_explicit():
    result=json.loads(web._bound_content(json.dumps({'content':'你好world','results':[{'url':'https://example.com','snippet':'extra'}]}),7))
    assert len(result['content'].encode())+len(result['results'][0]['snippet'].encode())<=7
    assert result['truncated'] and result['content_budget']['method']=='conservative_utf8_byte_cap'
    assert result['results'][0]['url']=='https://example.com'


@pytest.mark.parametrize('bad', [-1,True,'3',None])
def test_invalid_declared_budget_is_visible(bad):
    body=req('anthropic');body['tools'][0]['max_content_tokens']=bad
    with pytest.raises(GuardError,match='max_content_tokens'):policy.validate(body)


async def test_native_candidate_specific_wire_filters_and_skipping(monkeypatch,setup):
    from src.tests import test_protocol_fake_upstreams as fx
    from src.channel.xai_oauth_channel import XAIOAuthChannel
    from src import oauth_manager
    from src.search_native_tools import SOURCE_KEY
    setup['hostedMode']='passthrough'
    m=fx._import_modules();fx._setup(m)
    xai=XAIOAuthChannel({'provider':'xai','email':'isolated@invalid','models':['m']})
    chat=fx._make_openai_channel('chat','https://chat.example',protocol='openai-chat',alias='m',real='m')
    openai=fx._make_openai_channel('responses','https://responses.example',protocol='openai-responses',alias='m',real='m')
    fx._install_channels(m,[chat,openai,xai])
    async def token(_):return 'isolated-token'
    monkeypatch.setattr(oauth_manager,'ensure_channel_token',token)
    body=req('anthropic');body['tools']=[{'type':'web_search_20250305','name':'web_search','allowed_domains':['docs.python.org'],'blocked_domains':['spam.invalid']}]
    route=m['scheduler'].schedule(body,api_key_name='k',client_ip='1.2.3.4',ingress_protocol='anthropic')
    assert [c.key for c,_ in route.candidates]==[xai.key]
    request=await xai.build_upstream_request(body,'m',ingress_protocol='anthropic')
    wire=json.loads(request.body)
    assert wire['tools']==[{'type':'web_search','filters':{'allowed_domains':['docs.python.org'],'excluded_domains':['spam.invalid']}}]
    assert SOURCE_KEY not in wire and 'allowed_domains' not in wire['tools'][0]
    body['tools'][0]={'type':'web_search_20250305','name':'web_search','user_location':{'type':'approximate','country':'GB'}}
    route=m['scheduler'].schedule(body,api_key_name='k',client_ip='1.2.3.4',ingress_protocol='anthropic')
    assert [c.key for c,_ in route.candidates]==[openai.key]
    wire=json.loads((await openai.build_upstream_request(body,'m',ingress_protocol='anthropic')).body)
    assert wire['tools'][0]['user_location']=={'type':'approximate','country':'GB'} and SOURCE_KEY not in wire
    body['tools'][0]['max_uses']=1
    assert not m['scheduler'].schedule(body,api_key_name='k',client_ip='1.2.3.4',ingress_protocol='anthropic')


async def test_responses_to_anthropic_wire_no_openai_unknowns(monkeypatch,setup):
    from src.tests import test_protocol_fake_upstreams as fx
    setup['hostedMode']='passthrough'
    m=fx._import_modules();fx._setup(m)
    ch=fx._make_anthropic_channel(m,'anth','https://anth.example',alias='m',real='m')
    body=req();body['tools']=[{'type':'web_search','filters':{'allowed_domains':['docs.python.org']},'external_web_access':True,'user_location':{'type':'approximate','country':'GB'}}]
    wire=json.loads((await ch.build_upstream_request(body,'m',ingress_protocol='responses')).body)
    # Existing automatic Anthropic cache policy remains independent of search.
    assert wire['tools'][0].pop('cache_control')=={'type':'ephemeral','ttl':'1h'}
    assert wire['tools']==[{'type':'web_search_20250305','name':'web_search','allowed_domains':['docs.python.org'],'user_location':{'type':'approximate','country':'GB'}}]
    body['tools'][0]['external_web_access']=False
    with pytest.raises(GuardError,match='offline/cached'):await ch.build_upstream_request(body,'m',ingress_protocol='responses')


async def test_ws_passthrough_forwards_first_real_event_before_terminal(monkeypatch,setup):
    from src.openai import responses_ws
    from src import failover
    setup['functionMode']='passthrough'
    first_sent=asyncio.Event();seen=[]
    class Socket:
        async def send_text(self,text):
            seen.append(json.loads(text));first_sent.set()
        async def receive(self):
            # Stay connected while the active-turn control reader watches for
            # cancellation; the mocked next-turn receiver ends the session.
            await asyncio.Event().wait()
    class Lease:
        async def release(self):seen.append({'released':True})
    async def source():
        raw=web._sse('response.created',{'type':'response.created','response':{'id':'r','status':'in_progress'}})
        yield raw[:11];yield raw[11:]
        await asyncio.wait_for(first_sent.wait(),0.2)
        yield web._sse('response.completed',answer())
    async def invoke(route,body,*a,**kw):
        assert body['stream'] is True
        return StreamingResponse(source())
    async def next_turn(*a,**kw):return None
    async def forbidden(*a,**kw):pytest.fail('passthrough entered managed buffering')
    monkeypatch.setattr(failover,'run_failover',invoke)
    monkeypatch.setattr(policy,'run',forbidden)
    monkeypatch.setattr(responses_ws,'_receive_next_response_create',next_turn)
    await responses_ws._run_search_ws_session(Socket(),body=req(),schedule_result=SimpleNamespace(),request_id='r',api_key_name='k',client_ip='ip',start_time=0,start_monotonic=0,allowed_models=None,api_key_lease=Lease())
    assert seen[0]['type']=='response.created' and seen[-1]=={'released':True}


@pytest.mark.parametrize('protocol',['chat','responses','anthropic'])
async def test_two_mixed_client_turns_keep_visible_prefix_after_ingress_expansion(protocol,monkeypatch):
    body=req(protocol);visible=copy.deepcopy(body)
    async def execute(calls,**kw):return [web.LocalToolResult(c.id,'hidden '+c.id) for c in calls]
    monkeypatch.setattr(web,'execute_local_tool_calls',execute)
    for turn in (1,2):
        async def invoke(current):
            if turn==2:
                assert 'hidden search1' in json.dumps(current)
                assert 'client result1' in json.dumps(current)
            return JSONResponse(answer(protocol,[(f'search{turn}','web_search',{'query':'docs'}),(f'client{turn}','calculate',{})]))
        # The actual OpenAI handler expands once before run(), which expands again.
        expanded=policy.restore_replay(body,protocol,'k')
        response=json.loads((await policy.run(expanded,protocol,invoke,api_key_name='k')).body)
        policy._append(visible,response,[],protocol)
        result=({'type':'function_call_output','call_id':f'client{turn}','output':f'client result{turn}'} if protocol=='responses' else {'role':'tool','tool_call_id':f'client{turn}','content':f'client result{turn}'} if protocol=='chat' else {'role':'user','content':[{'type':'tool_result','tool_use_id':f'client{turn}','content':f'client result{turn}'}]})
        policy._history(visible,protocol).append(result)
        body=copy.deepcopy(visible)
    final=policy.restore_replay(body,protocol,'k')
    text=json.dumps(final)
    assert text.count('hidden search1')==text.count('hidden search2')==1
    assert 'client result1' in text and 'client result2' in text


async def test_max_uses_is_shared_across_chat_choices(monkeypatch):
    body=req('chat');body['n']=2;body['tools'][0]['function']['max_uses']=1
    searches=[];count=0
    async def execute(calls,**kw):
        searches.extend(calls);return [web.LocalToolResult(c.id,'result') for c in calls]
    monkeypatch.setattr(web,'execute_local_tool_calls',execute)
    async def invoke(current):
        nonlocal count
        count+=1
        if count==1:
            one=answer('chat',[('first','web_search',{'query':'one'})]);two=answer('chat',[('second','web_search',{'query':'two'})])['choices'][0];two['index']=1
            one['choices'].append(two);return JSONResponse(one)
        if count==3:assert 'max_uses_exceeded' in json.dumps(current)
        return JSONResponse(answer('chat'))
    result=await policy.run(body,'chat',invoke)
    assert result.status_code==200 and len(searches)==1 and count==3


async def test_fetch_definition_content_budget_applied_to_actual_service_result(monkeypatch):
    body=req('anthropic');body['tools']=[{'type':'web_fetch_20250910','name':'web_fetch','max_content_tokens':9}]
    url='https://docs.python.org/3/';body['messages'][0]['content']='Read '+url
    queries=[];rounds=[]
    async def extract(args,**kw):
        queries.append(args);return {'url':url,'content':'abcdef你好 world'*50}
    monkeypatch.setattr(search_service,'extract',extract)
    async def invoke(current):
        rounds.append(copy.deepcopy(current))
        if len(rounds)==1:return JSONResponse(answer('anthropic',[('fetch',current['tools'][0]['name'],{'url':url,'max_content_tokens':9000})]))
        blocks=current['messages'][-1]['content'];data=json.loads(blocks[0]['content'])
        assert data['content']=='abcdef你' and data['truncated']
        assert data['content_budget']['enforced_utf8_bytes']==9
        return JSONResponse(answer('anthropic'))
    response=await policy.run(body,'anthropic',invoke)
    assert response.status_code==200 and len(queries)==1
    assert queries[0]['max_content_tokens']==9


async def test_native_ws_passthrough_never_uses_managed_executor(monkeypatch,setup):
    from src.tests import test_openai_responses_ws as fx
    setup['functionMode']='passthrough'
    m=fx._import_modules();fx._setup(m);fx._make_channel(m)
    first={'type':'response.create','model':'test-model','input':'hello','tools':[{'type':'function','name':'web_search','parameters':{'type':'object'}}]}
    ws=fx.SequentialFakeWebSocket(first,{**first,'previous_response_id':'r1','input':'next'})
    upstream=fx.FakeUpstreamWebSocket([
        {'type':'response.created','response':{'id':'r1'}},
        {'type':'response.completed','response':{'id':'r1','output':[],'usage':{'input_tokens':1,'output_tokens':1}}},
        {'type':'response.created','response':{'id':'r2'}},
        {'type':'response.completed','response':{'id':'r2','output':[],'usage':{'input_tokens':2,'output_tokens':2}}},
    ])
    async def connect(*a,**kw):return upstream
    async def forbidden(*a,**kw):pytest.fail('native passthrough entered managed run')
    monkeypatch.setattr(m['responses_ws'],'_connect_upstream_ws',connect)
    monkeypatch.setattr(policy,'run',forbidden)
    await m['responses_ws'].handle_responses_ws(ws)
    assert len(upstream.sent)==2
    assert all(json.loads(frame)['tools'][0]['name']=='web_search' for frame in upstream.sent)


async def test_ws_passthrough_cancel_before_headers_releases_lease(monkeypatch,setup):
    from src.openai import responses_ws
    from src import failover
    setup['functionMode']='passthrough';entered=asyncio.Event();released=[]
    class Lease:
        async def release(self):released.append(True)
    async def invoke(*a,**kw):entered.set();await asyncio.Event().wait()
    monkeypatch.setattr(failover,'run_failover',invoke)
    task=asyncio.create_task(responses_ws._run_search_ws_session(object(),body=req(),schedule_result=SimpleNamespace(),request_id='r',api_key_name='k',client_ip='ip',start_time=0,start_monotonic=0,allowed_models=None,api_key_lease=Lease()))
    await entered.wait();task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert released==[True]


@pytest.mark.parametrize('protocol',['chat','responses','anthropic'])
def test_even_unique_modern_call_id_is_not_a_session_credential(protocol):
    saved=req(protocol);policy._append(saved,answer(protocol,[('predictable-id','calculate',{})]),[],protocol)
    policy._remember(saved,protocol,'shared',['predictable-id'])
    item=({'type':'function_call_output','call_id':'predictable-id','output':'result'} if protocol=='responses' else {'role':'tool','tool_call_id':'predictable-id','content':'result'} if protocol=='chat' else {'role':'user','content':[{'type':'tool_result','tool_use_id':'predictable-id','content':'result'}]})
    delta={'model':'m','input' if protocol=='responses' else 'messages':[item]}
    assert policy.restore_replay(delta,protocol,'shared')==delta
