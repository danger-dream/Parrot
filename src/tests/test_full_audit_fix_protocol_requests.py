"""Protocol fixes: real handler, isolated Store and outbound payload contracts."""
import copy, json
import pytest, httpx
from src.tests import _isolation
_isolation.isolate()
from src.tests.test_protocol_fake_upstreams import (
    _import_modules, _setup, _install_channels, _install_keys, _default_key,
    _make_openai_channel, _make_anthropic_channel, _call_openai_handler,
    _call_anthropic_core, MockRouter,
)
from src.openai.transform import chat_to_responses as c2r, responses_to_chat as r2c
from src.openai.transform import chat_to_anthropic as c2a, responses_to_anthropic as r2a
from src.openai.transform import codex_oauth_transform as codex
from src.protocols.matrix import DEFAULT_MATRIX, extract_request_features
from src.channel.api_channel import ApiChannel


def resp(status='completed', text='ok'):
    return {'id':'resp_audit','object':'response','created_at':1,'status':status,'error':None,
            'model':'target','output':[{'type':'message','id':'msg_audit','role':'assistant',
            'status':'completed' if status=='completed' else 'in_progress',
            'content':[{'type':'output_text','text':text,'annotations':[]}]}],
            'usage':{'input_tokens':4,'output_tokens':2,'total_tokens':6}}

@pytest.mark.asyncio
@pytest.mark.parametrize('status',['queued','in_progress','cancelled'])
@pytest.mark.parametrize('ingress',['chat','anthropic'])
async def test_nonterminal_is_reported_success(m, status, ingress):
    _setup(m); _install_keys(m, _default_key())
    ch=_make_openai_channel('audit','https://audit.invalid',protocol='openai-responses',alias='audit',real='target')
    _install_channels(m,[ch]); router=MockRouter()
    router.register('https://audit.invalid',lambda req:httpx.Response(200,json=resp(status,'partial')))
    body={'model':'audit','stream':False,'max_tokens':32,'messages':[{'role':'user','content':'hi'}]}
    if ingress=='chat': response, client=await _call_openai_handler(m,router,ingress,body)
    else: response, client, _=await _call_anthropic_core(m,router,body)
    await client.aclose(); out=json.loads(response.body)
    assert response.status_code>=400, out
    row=m['log_db']._get_conn().execute('SELECT status FROM request_log ORDER BY id DESC LIMIT 1').fetchone()
    assert row['status']=='error'

@pytest.mark.asyncio
async def test_chat_stop_lost_on_real_http_chain(m):
    _setup(m); _install_keys(m,_default_key())
    ch=_make_openai_channel('audit','https://audit.invalid',protocol='openai-responses',alias='audit',real='target')
    _install_channels(m,[ch]); router=MockRouter()
    router.register('https://audit.invalid',lambda req:httpx.Response(200,json=resp(text='before<STOP>after')))
    response,client=await _call_openai_handler(m,router,'chat',{'model':'audit','stream':False,
        'messages':[{'role':'user','content':'hi'}],'stop':['<STOP>']})
    await client.aclose(); out=json.loads(response.body)
    assert response.status_code==200
    assert 'stop' not in json.loads(router.requests[0].content)
    assert out['choices'][0]['message']['content']=='before'

@pytest.mark.asyncio
async def test_legacy_tool_request_lost_on_real_http_chain(m):
    _setup(m); _install_keys(m,_default_key())
    ch=_make_openai_channel('audit','https://audit.invalid',protocol='openai-responses',alias='audit',real='target')
    _install_channels(m,[ch]); router=MockRouter()
    router.register('https://audit.invalid',lambda req:httpx.Response(200,json=resp()))
    body={'model':'audit','stream':False,'functions':[{'name':'lookup','parameters':{'type':'object'}}],
          'function_call':{'name':'lookup'},'messages':[{'role':'user','content':'hi'},
          {'role':'assistant','content':None,'function_call':{'name':'lookup','arguments':'{}'}},
          {'role':'function','name':'lookup','content':'result'}]}
    response,client=await _call_openai_handler(m,router,'chat',body); await client.aclose()
    assert response.status_code==200
    sent=json.loads(router.requests[0].content)
    assert sent['tools'][0]['name']=='lookup' and sent['tool_choice']=={'type':'function','name':'lookup'}
    calls=[i for i in sent['input'] if i.get('type')=='function_call']
    results=[i for i in sent['input'] if i.get('type')=='function_call_output']
    assert len(calls)==len(results)==1 and calls[0]['call_id']==results[0]['call_id']

@pytest.mark.asyncio
async def test_legacy_nonstream_output_lost(m):
    _setup(m); _install_keys(m,_default_key())
    ch=_make_openai_channel('audit','https://audit.invalid',protocol='openai-chat',alias='audit',real='target')
    _install_channels(m,[ch]); router=MockRouter()
    wire={'id':'chatcmpl_audit','object':'chat.completion','created':1,'model':'target','choices':[{
        'index':0,'finish_reason':'function_call','message':{'role':'assistant','content':None,
        'function_call':{'name':'lookup','arguments':'{}'}}}]}
    router.register('https://audit.invalid',lambda req:httpx.Response(200,json=wire))
    response,client=await _call_openai_handler(m,router,'responses',{'model':'audit','stream':False,'input':'hi'})
    await client.aclose(); out=json.loads(response.body)
    assert response.status_code==200 and out['status']=='completed'
    assert out['output'][0]['type']=='function_call' and out['output'][0]['name']=='lookup'

@pytest.mark.asyncio
async def test_mimicry_tool_name_collision_and_wrong_restoration():
    ch=ApiChannel({'name':'audit','baseUrl':'https://audit.invalid','cc_mimicry':True})
    body={'model':'claude-sonnet-4-6','messages':[{'role':'user','content':'hi'}],
        'tools':[{'name':'cc_ses_lookup','input_schema':{'type':'object'}}]}
    request=await ch.build_upstream_request(body,body['model'])
    alias = json.loads(request.body)['tools'][0]['name']
    wire={'type':'message','content':[{'type':'tool_use','id':'t1','name':alias,'input':{}}]}
    out=json.loads(await ch.restore_response(json.dumps(wire).encode(),request.dynamic_tool_map))
    assert out['content'][0]['name']=='cc_ses_lookup'
    body['tools'].append({'name':'session_lookup','input_schema':{'type':'object'}})
    request=await ch.build_upstream_request(body,body['model'])
    assert len({t['name'] for t in json.loads(request.body)['tools']})==2
    assert json.loads(request.body)['tools'][0]['name'] == alias

@pytest.mark.asyncio
async def test_tool_call_id_sanitization_collision():
    ch=ApiChannel({'name':'audit','baseUrl':'https://audit.invalid','cc_mimicry':False})
    body={'model':'audit','messages':[{'role':'assistant','content':None,'tool_calls':[
        {'id':v,'type':'function','function':{'name':'lookup','arguments':'{}'}} for v in ['call.a','call_a']]},
        *[{'role':'tool','tool_call_id':v,'content':v} for v in ['call.a','call_a']]]}
    DEFAULT_MATRIX.plan('chat','anthropic',extract_request_features('chat',body))
    request=await ch.build_upstream_request(body,'claude-sonnet-4-6',ingress_protocol='chat')
    sent=json.loads(request.body)
    assert len({b['id'] for b in sent['messages'][0]['content']})==2
    assert [b['id'] for b in sent['messages'][0]['content']]==[b['tool_use_id'] for b in sent['messages'][1]['content']]

@pytest.mark.asyncio
async def test_refusal_history_is_erased():
    ch=ApiChannel({'name':'audit','baseUrl':'https://audit.invalid','cc_mimicry':False})
    body={'model':'audit','input':[{'role':'user','content':'first'},
        {'type':'message','role':'assistant','content':[{'type':'refusal','refusal':'CANNOT DO X'}]},
        {'role':'user','content':'why?'}]}
    DEFAULT_MATRIX.plan('responses','anthropic',extract_request_features('responses',body))
    sent=json.loads((await ch.build_upstream_request(body,'claude-sonnet-4-6',ingress_protocol='responses')).body)
    assert sent['messages'][1]['content'][0]['text']=='CANNOT DO X'

@pytest.mark.asyncio
async def test_forced_hosted_tool_silently_removed():
    ch=ApiChannel({'name':'audit','baseUrl':'https://audit.invalid','cc_mimicry':False})
    body={'model':'audit','input':'find data','tools':[{'type':'file_search','vector_store_ids':['vs_audit']}],
          'tool_choice':{'type':'file_search'}}
    from src.protocols.matrix import ProtocolGuardError
    with pytest.raises(ProtocolGuardError): DEFAULT_MATRIX.plan('responses','anthropic',extract_request_features('responses',body))
    with pytest.raises(GuardError) as caught: await ch.build_upstream_request(body,'claude-sonnet-4-6',ingress_protocol='responses')
    assert caught.value.scope=='candidate'


def test_codex_namespaced_choice_broadens():
    body={'model':'gpt-5','input':'use db lookup', 'tools':[{'type':'namespace','name':'db','tools':[
        {'type':'function','name':'lookup','parameters':{'type':'object'}},
        {'type':'function','name':'other','parameters':{'type':'object'}}]}],
        'tool_choice':{'type':'function','namespace':'db','name':'lookup'}}
    from src.protocols.matrix import ChannelCapabilities
    DEFAULT_MATRIX.plan('responses','openai-responses',extract_request_features('responses',body),
        ChannelCapabilities(protocol='openai-responses',native_state=frozenset({'namespace'})))
    out=codex.apply_codex_oauth_transform(copy.deepcopy(body),use_responses_lite=False)
    assert out['tool_choice']==body['tool_choice'] and len(out['tools'][0]['tools'])==2


def test_responses_developer_promoted_to_system():
    body={'model':'gpt-5','instructions':[{'role':'developer','content':'developer instruction'}],
          'input':[{'role':'developer','content':'history instruction'},{'role':'user','content':'hi'}]}
    DEFAULT_MATRIX.plan('responses','openai-chat',extract_request_features('responses',body))
    out=r2c.translate_request(body)
    assert [m['role'] for m in out['messages']]==['developer','developer','user']

import copy, json
import pytest, httpx
from src.tests import _isolation
_isolation.isolate()
from src.tests.test_protocol_fake_upstreams import (
    _import_modules, _setup, _install_channels, _install_keys, _default_key,
    _make_openai_channel, _make_anthropic_channel, _call_openai_handler, MockRouter,
)
from src.channel.api_channel import ApiChannel
from src.openai.channel.api_channel import OpenAIApiChannel
from src.openai.transform import chat_to_responses as c2r, responses_to_chat as r2c
from src.openai.transform import chat_to_anthropic as c2a, responses_to_anthropic as r2a
from src.openai.transform import anthropic_to_chat as a2c, anthropic_to_responses as a2r
from src.openai.transform.guard import GuardError
from src.protocols.matrix import DEFAULT_MATRIX, extract_request_features

@pytest.mark.asyncio
async def test_mimicry_omit_thinking_mutates_source():
    ch=ApiChannel({'name':'audit','baseUrl':'https://audit.invalid','cc_mimicry':True,'omitThinking':True})
    body={'model':'claude-sonnet-4-6','messages':[{'role':'user','content':'hi'}],
          'thinking':{'type':'adaptive'},'context_management':{'edits':[
              {'type':'clear_thinking_20251015','keep':'all'},
              {'type':'clear_tool_uses_20250919','trigger':{'type':'input_tokens','value':100000}}]}}
    old=copy.deepcopy(body)
    await ch.build_upstream_request(body,body['model'])
    assert len(old['context_management']['edits'])==2
    assert body==old
    standard=ApiChannel({'name':'next','baseUrl':'https://next.invalid','cc_mimicry':False})
    sent=json.loads((await standard.build_upstream_request(body,body['model'])).body)
    assert sent['context_management']['edits']==body['context_management']['edits']

@pytest.mark.asyncio
async def test_codex_real_builder_choice_and_unconfigured_defaults(monkeypatch):
    monkeypatch.setenv('DISABLE_OAUTH_NETWORK_CALLS','1')
    from src.tests import test_openai_oauth_channel as helpers
    m=helpers._import_modules(); helpers._setup(m); helpers._add_openai_acc(m)
    ch=m['OpenAIOAuthChannel'](m['oauth_manager'].get_account('openai:o@openai.test:acct-123'))
    body={'model':'gpt-5.1','input':'use db lookup',
          'reasoning':{'summary':'none'},'text':{'format':{'type':'text'}},
          'tools':[{'type':'namespace','name':'db','tools':[
              {'type':'function','name':'lookup','parameters':{'type':'object'}},
              {'type':'function','name':'other','parameters':{'type':'object'}}]}],
          'tool_choice':{'type':'function','namespace':'db','name':'lookup'}}
    old=copy.deepcopy(body)
    req=await ch.build_upstream_request(body,'gpt-5.1',ingress_protocol='responses')
    sent=json.loads(req.body)
    assert sent['tool_choice']==old['tool_choice']
    assert old['reasoning']==body['reasoning'] and old['text']==body['text']

@pytest.mark.asyncio
async def test_codex_bare_system_not_normalized(monkeypatch):
    monkeypatch.setenv('DISABLE_OAUTH_NETWORK_CALLS','1')
    from src.tests import test_openai_oauth_channel as helpers
    m=helpers._import_modules(); helpers._setup(m); helpers._add_openai_acc(m)
    ch=m['OpenAIOAuthChannel'](m['oauth_manager'].get_account('openai:o@openai.test:acct-123'))
    body={'model':'gpt-5.1','input':[{'role':'system','content':'ONLY USE Z'}, {'role':'user','content':'hi'}]}
    sent=json.loads((await ch.build_upstream_request(copy.deepcopy(body),'gpt-5.1',ingress_protocol='responses')).body)
    assert all(x.get('role')!='system' for x in sent['input']) and 'ONLY USE Z' in sent['instructions']
    body['input'][0]['type']='message'
    sent2=json.loads((await ch.build_upstream_request(body,'gpt-5.1',ingress_protocol='responses')).body)
    assert all(x.get('role')!='system' for x in sent2['input']) and 'ONLY USE Z' in sent2['instructions']

@pytest.mark.asyncio
@pytest.mark.parametrize('target',['openai-chat','anthropic'])
async def test_store_expanded_builtin_history_silently_dropped(m,target):
    _setup(m); _install_keys(m,_default_key())
    from src.openai import store
    store.init()  # mirror application startup, using isolated DATA_DIR
    assert store.is_enabled()
    ch=_make_openai_channel('origin','https://origin.invalid',protocol='openai-responses',alias='audit',real='target')
    _install_channels(m,[ch]); router=MockRouter()
    source={'id':'resp_history_'+target,'object':'response','status':'completed','model':'target',
        'output':[{'type':'code_interpreter_call','id':'ci_audit','status':'completed','container_id':'cn_audit',
                   'code':'print(731)','outputs':[{'type':'logs','logs':'731'}]},
                  {'type':'message','role':'assistant','id':'msg_audit','status':'completed',
                   'content':[{'type':'output_text','text':'computed','annotations':[]}]}]}
    router.register('https://origin.invalid',lambda req:httpx.Response(200,json=source))
    response,client=await _call_openai_handler(m,router,'responses',{'model':'audit','input':'calculate','stream':False})
    await client.aclose(); assert response.status_code==200
    if target=='openai-chat':
        newch=_make_openai_channel('fallback','https://fallback.invalid',protocol=target,alias='audit',real='target')
        wire={'id':'chatcmpl_audit','object':'chat.completion','model':'target','choices':[{'index':0,
              'finish_reason':'stop','message':{'role':'assistant','content':'ok'}}]}
    else:
        newch=_make_anthropic_channel(m,'fallback','https://fallback.invalid',alias='audit',real='claude-sonnet-4-6')
        wire={'id':'msg_audit','type':'message','role':'assistant','model':'claude-sonnet-4-6',
              'stop_reason':'end_turn','content':[{'type':'text','text':'ok'}],'usage':{'input_tokens':2,'output_tokens':1}}
    _install_channels(m,[newch]); router2=MockRouter()
    router2.register('https://fallback.invalid',lambda req:httpx.Response(200,json=wire))
    response,client=await _call_openai_handler(m,router2,'responses',{'model':'audit','input':'use the result',
          'previous_response_id':source['id'],'stream':False})
    await client.aclose(); assert response.status_code==200, response.body
    sent=json.loads(router2.requests[0].content)
    assert 'computed' in json.dumps(sent) and 'calculate' in json.dumps(sent)
    assert '731' in json.dumps(sent) and 'code_interpreter_call' in json.dumps(sent)

@pytest.mark.asyncio
async def test_deepseek_forced_tool_is_candidate_incompatibility_but_marked_request():
    body={'model':'audit','messages':[{'role':'user','content':'lookup'}],
          'tools':[{'name':'lookup','input_schema':{'type':'object'}}],'tool_choice':{'type':'tool','name':'lookup'}}
    deep=OpenAIApiChannel({'name':'deepseek','protocol':'openai-chat','baseUrl':'https://deep.invalid'})
    with pytest.raises(GuardError) as e:
        await deep.build_upstream_request(copy.deepcopy(body),'deepseek-v4',ingress_protocol='anthropic')
    assert e.value.scope=='candidate'
    normal=OpenAIApiChannel({'name':'normal','protocol':'openai-chat','baseUrl':'https://normal.invalid'})
    req=await normal.build_upstream_request(copy.deepcopy(body),'gpt-4.1',ingress_protocol='anthropic')
    assert json.loads(req.body)['tool_choice']=={'type':'function','function':{'name':'lookup'}}

@pytest.mark.asyncio
async def test_explicit_system_cache_ttl_lost_in_mimicry():
    ch=ApiChannel({'name':'audit','baseUrl':'https://audit.invalid','cc_mimicry':True})
    body={'model':'claude-sonnet-4-6','system':[{'type':'text','text':'cached policy',
          'cache_control':{'type':'ephemeral','ttl':'1h'}}], 'messages':[{'role':'user','content':'hi'}]}
    old=copy.deepcopy(body)
    sent=json.loads((await ch.build_upstream_request(body,body['model'])).body)
    assert 'cached policy' in json.dumps(sent) and '1h' in json.dumps(sent)
    assert body==old

@pytest.mark.parametrize('fn,body',[
    (c2r.translate_request,{'model':'m','messages':[{'role':'system','content':'S'},{'role':'user','content':'U'}],
                          'tools':[{'type':'function','function':{'name':'f','parameters':{'type':'object'}}}]}),
    (r2c.translate_request,{'model':'m','instructions':'S','input':'U','tools':[{'type':'function','name':'f','parameters':{'type':'object'}}]}),
    (a2c.translate_request,{'model':'m','system':'S','messages':[{'role':'user','content':'U'}], 'tools':[{'name':'f','input_schema':{'type':'object'}}]}),
    (a2r.translate_request,{'model':'m','system':'S','messages':[{'role':'user','content':'U'}], 'tools':[{'name':'f','input_schema':{'type':'object'}}]}),
    (c2a.translate_request,{'model':'m','messages':[{'role':'system','content':'S'},{'role':'user','content':'U'}],
                          'tools':[{'type':'function','function':{'name':'f','parameters':{'type':'object'}}}]}),
    (r2a.translate_request,{'model':'m','instructions':'S','input':'U','tools':[{'type':'function','name':'f','parameters':{'type':'object'}}]}),
])
def test_six_direction_normal_requests_do_not_mutate(fn,body):
    old=copy.deepcopy(body); out=fn(body)
    assert body==old and ('S' in json.dumps(out)) and ('U' in json.dumps(out))


def test_tool_arguments_recover_only_bounded_object_wrappers():
    from src.openai.transform.tool_arguments import parse_tool_arguments, ToolArgumentsError
    assert parse_tool_arguments('```json\n{"path":"a",}\n```')=={'path':'a'}
    for raw in ('{"path":','{"path":,}','[1]','{"path":NaN}'):
        with pytest.raises(ToolArgumentsError): parse_tool_arguments(raw)

import copy, json
import pytest, httpx
from src.tests import _isolation
_isolation.isolate()
from src.tests.test_protocol_fake_upstreams import (
    _import_modules, _setup, _install_channels, _install_keys, _default_key,
    _make_openai_channel, _make_anthropic_channel, _call_openai_handler,
    _call_anthropic_core, MockRouter,
)

@pytest.mark.asyncio
async def test_forced_file_search_http_becomes_untooled_success(m):
    _setup(m); _install_keys(m,_default_key())
    ch=_make_anthropic_channel(m,'anth','https://anth.invalid',alias='audit',real='claude-sonnet-4-6')
    _install_channels(m,[ch]); router=MockRouter()
    router.register('https://anth.invalid', lambda request: httpx.Response(200,json={
        'type':'message','id':'msg_audit','role':'assistant','model':'claude-sonnet-4-6','stop_reason':'end_turn',
        'content':[{'type':'text','text':'ordinary answer'}],'usage':{'input_tokens':2,'output_tokens':1}}))
    response,client=await _call_openai_handler(m,router,'responses',{'model':'audit','stream':False,
        'input':'search my files','tools':[{'type':'file_search','vector_store_ids':['vs_audit']}],
        'tool_choice':{'type':'file_search'}})
    await client.aclose(); out=json.loads(response.body)
    assert response.status_code>=400 and not router.requests

@pytest.mark.asyncio
async def test_deepseek_guard_prevents_valid_later_candidate(m,monkeypatch):
    _setup(m)
    deep=_make_openai_channel('deepseek','https://deep.invalid',protocol='openai-chat',alias='audit',real='deepseek-v4')
    good=_make_openai_channel('good','https://good.invalid',protocol='openai-chat',alias='audit',real='gpt-4.1')
    _install_channels(m,[deep,good])
    body={'model':'audit','stream':False,'max_tokens':64,'messages':[{'role':'user','content':'lookup'}],
          'tools':[{'name':'lookup','input_schema':{'type':'object'}}],'tool_choice':{'type':'tool','name':'lookup'}}
    route=m['scheduler'].schedule(body,api_key_name='ccp-test',client_ip='1.2.3.4',ingress_protocol='anthropic')
    assert len(route.candidates)==2
    route.candidates.sort(key=lambda pair:pair[0].key!='api:deepseek')
    monkeypatch.setattr(m['scheduler'],'schedule',lambda *a,**kw:route)
    router=MockRouter()
    router.register('https://good.invalid',lambda request:httpx.Response(200,json={'id':'chatcmpl_ok','choices':[
        {'index':0,'finish_reason':'stop','message':{'role':'assistant','content':'ok'}}]}))
    response,client,_=await _call_anthropic_core(m,router,body)
    await client.aclose()
    assert response.status_code==200 and len(router.requests)==1
    assert router.requests[0].url.host=='good.invalid'

@pytest.mark.asyncio
async def test_missing_chat_finish_is_completed_on_http_chain(m):
    _setup(m); _install_keys(m,_default_key())
    ch=_make_openai_channel('chat','https://chat.invalid',protocol='openai-chat',alias='audit',real='target')
    _install_channels(m,[ch]); router=MockRouter()
    router.register('https://chat.invalid',lambda request:httpx.Response(200,json={'id':'chatcmpl_partial',
        'object':'chat.completion','choices':[{'index':0,'finish_reason':None,
        'message':{'role':'assistant','content':'partial'}}]}))
    response,client=await _call_openai_handler(m,router,'responses',{'model':'audit','stream':False,'input':'hi'})
    await client.aclose(); out=json.loads(response.body)
    assert response.status_code>=400
    assert m['log_db']._get_conn().execute('select status from request_log order by id desc limit 1').fetchone()[0]=='error'

@pytest.mark.asyncio
async def test_anthropic_pause_turn_is_completed_on_http_chain(m):
    _setup(m); _install_keys(m,_default_key())
    ch=_make_anthropic_channel(m,'anth','https://anth.invalid',alias='audit',real='claude-sonnet-4-6')
    _install_channels(m,[ch]); router=MockRouter()
    router.register('https://anth.invalid',lambda request:httpx.Response(200,json={
        'type':'message','id':'msg_audit','role':'assistant','model':'claude-sonnet-4-6','stop_reason':'pause_turn',
        'content':[{'type':'text','text':'working'}],'usage':{'input_tokens':2,'output_tokens':1}}))
    response,client=await _call_openai_handler(m,router,'responses',{'model':'audit','stream':False,'input':'hi'})
    await client.aclose(); out=json.loads(response.body)
    assert response.status_code==200 and out['status']=='incomplete' and out['incomplete_details']=={'reason':'pause_turn'}
