"""Unified image contract tests: real encoded pixels, isolated state, no network."""
import base64
import copy
import gzip
import io
import json
from types import SimpleNamespace
import httpx
import pytest
from PIL import Image
from fastapi import FastAPI
from src import config, auth, image_artifacts, image_catalog, model_metadata, oauth_manager
from src.openai import images_runtime as runtime, images_openai_compat as compat, images_simple
from src.channel import registry
from src.management_control.models import (ModelCenterControl, ModelFilters, ModelKind, ModelSelection,
    ModelSelectionMode, ModelSourceRef, ModelSourceType, ModelStateTarget, ModelStateField)


def png(color='red', size=(32,32), mode='RGB'):
    out=io.BytesIO(); Image.new(mode,size,color).save(out,format='PNG'); return out.getvalue()


def b64(raw=None): return base64.b64encode(raw or png()).decode()


def ref(raw=None): return 'data:image/png;base64,'+b64(raw)


@pytest.fixture
def setup(monkeypatch, tmp_path):
    cfg=copy.deepcopy(config.DEFAULT_CONFIG)
    cfg['images'].update({'toolModel':'gpt-image-2','enabled':True,'cachePath':str(tmp_path/'cache')})
    cfg['oauthAccounts']=[{'provider':'openai','email':'image@example.test','chatgpt_account_id':'test-workspace','enabled':True,'access_token':'test'},
        {'provider':'xai','email':'grok@example.test','enabled':True,'access_token':'test'},
        {'provider':'antigravity','email':'ag@example.test','project_id':'p','enabled':True,'access_token':'test','models':['gemini-chat']}]
    cfg['xaiOAuth']['imageModels']=['grok-imagine-image','grok-imagine-image-quality']
    cfg['modelMapping']={'global':{'paint':'gpt-image-2'}}
    monkeypatch.setattr(config,'get',lambda:cfg)
    monkeypatch.setattr(model_metadata,'resolve_binding',lambda *a,**kw:None)
    monkeypatch.setattr(auth,'validate',lambda headers:('test', [], None))
    monkeypatch.setattr(auth,'images_allowed',lambda key:True)
    monkeypatch.setattr(runtime.cooldown,'is_blocked',lambda *a:False)
    async def acquire(*a): return True
    monkeypatch.setattr(runtime.concurrency,'try_acquire',acquire)
    monkeypatch.setattr(runtime.concurrency,'release',lambda *a:None)
    async def log(*a,**kw): return None
    monkeypatch.setattr(runtime.imagine,'_start_media_log',log)
    monkeypatch.setattr(runtime.imagine,'_finish_media_log',log)
    calls=[]
    async def send(source,parsed,*,action,n,cfg):
        calls.append((source,copy.copy(parsed),action,n))
        return httpx.Response(200,json={'data':[{'b64_json':b64()} for _ in range(n)]})
    monkeypatch.setattr(runtime,'_send',send)
    image_artifacts._ASSETS.clear()
    app=FastAPI()
    app.add_api_route('/v1/images/generations',compat.handle_generations,methods=['POST'])
    app.add_api_route('/v1/images/edits',compat.handle_edits,methods=['POST'])
    app.add_api_route('/v1/images/generate',images_simple.handle_generate,methods=['POST'])
    app.add_api_route('/v1/images/edit',images_simple.handle_edit,methods=['POST'])
    app.add_api_route('/v1/images/assets/{token}',image_artifacts.download,methods=['GET'])
    return cfg,calls,app


async def post(setup, body=None, path='/v1/images/generations', **kwargs):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=setup[2]),base_url='http://testserver') as client:
        return await client.post(path,json=body,**kwargs)


@pytest.mark.asyncio
async def test_model_alias_n_and_actual_dimensions(setup):
    response=await post(setup,{'model':'paint','prompt':'a boat','n':2,'size':'48x32'})
    assert response.status_code==200, response.text
    body=response.json();assert body['model']=='gpt-image-2'
    assert len(body['data'])==2 and len(setup[1])==2
    assert all(x[3]==1 for x in setup[1])
    assert body['parrot']['generation_budget']==2 and 'usage' not in body
    im=Image.open(io.BytesIO(base64.b64decode(body['data'][0]['b64_json'])))
    assert im.size==(48,32)
    assert body['parrot']['warnings']


@pytest.mark.asyncio
async def test_xai_native_batch_b64_default(setup):
    response=await post(setup,{'model':'grok-imagine-image','prompt':'a boat','n':2})
    assert response.status_code==200
    assert len(setup[1])==1 and setup[1][0][3]==2
    assert 'b64_json' in response.json()['data'][0]


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation,model,status',[
    ('unknown','nonexistent',400),('missing',None,400),('disabled','gpt-image-2',403),
    ('source-disabled','gpt-image-2',503),('account-disabled','gpt-image-2',503),('denied','paint',403),
])
async def test_model_permissions_and_availability(setup,monkeypatch,mutation,model,status):
    cfg=setup[0]
    if mutation=='disabled': cfg['modelCenter']={'disabledModels':[model]}
    if mutation=='source-disabled': cfg['oauthAccounts'][0]['disabledModels']=[model]
    if mutation=='account-disabled': cfg['oauthAccounts'][0]['enabled']=False
    if mutation=='denied': monkeypatch.setattr(auth,'validate',lambda headers:('test',['other'],None))
    response=await post(setup,{'model':model,'prompt':'icon'})
    assert response.status_code==status, response.text
    assert not setup[1]


@pytest.mark.asyncio
async def test_hidden_is_still_callable(setup):
    setup[0]['modelCenter']={'hiddenModels':['gpt-image-2']}
    assert 'gpt-image-2' not in image_catalog.available_models()
    assert (await post(setup,{'model':'gpt-image-2','prompt':'icon'})).status_code==200


@pytest.mark.asyncio
async def test_downloadable_url_and_expiry(setup,monkeypatch):
    response=await post(setup,{'model':'gpt-image-2','prompt':'icon','response_format':'url'})
    assert response.status_code==200,response.text
    item=response.json()['data'][0]
    assert item['url'].startswith('http://testserver/v1/images/assets/')
    assert 'image@example' not in item['url'] and 'cache' not in item['url']
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=setup[2]),base_url='http://testserver') as client:
        result=await client.get(item['url']); assert result.status_code==200
        assert Image.open(io.BytesIO(result.content)).size==(32,32)
        assert (await client.get('/v1/images/assets/bad')).status_code==404
        monkeypatch.setattr(image_artifacts.time,'time',lambda:item['expires_at']+1)
        assert (await client.get(item['url'])).status_code==404


@pytest.mark.asyncio
async def test_real_transparency_not_invented(setup):
    response=await post(setup,{'model':'gpt-image-2','prompt':'icon','background':'transparent'})
    assert response.status_code==502
    assert 'transparent' in response.json()['error']['message']
    assert len(setup[1])==1 and not response.json()['data']


@pytest.mark.asyncio
@pytest.mark.parametrize('model',['gpt-image-2','gemini-3.1-flash-image'])
async def test_mask_multipart_preserves_opaque_pixels(setup,model):
    original=png('blue');im=Image.new('RGBA',(32,32),(255,255,255,255));im.paste((0,0,0,0),(16,0,32,32))
    mask=io.BytesIO();im.save(mask,format='PNG')
    response=await post(setup,path='/v1/images/edits',data={'model':model,'prompt':'paint red'},
        files=[('image[]',('one.png',original,'image/png')),('image[]',('two.png',png('green'),'image/png')),('mask',('mask.png',mask.getvalue(),'image/png'))])
    assert response.status_code==200,response.text
    result=Image.open(io.BytesIO(base64.b64decode(response.json()['data'][0]['b64_json']))).convert('RGB')
    assert result.getpixel((5,5))==(0,0,255) and result.getpixel((25,5))==(255,0,0)
    assert len(setup[1][0][1].input_images)==2


@pytest.mark.asyncio
async def test_legacy_wrapper_uses_same_model(setup):
    result=await post(setup,{'model':'grok-imagine-image','prompt':'icon'},path='/v1/images/generate')
    assert result.status_code==200 and result.json()['image_model']=='grok-imagine-image'
    result=await post(setup,{'model':'gpt-image-2','prompt':'icon','image_url':ref()},path='/v1/images/edit')
    assert result.status_code==200


@pytest.mark.asyncio
async def test_timeout_keeps_partial_without_retry(setup,monkeypatch):
    called=[]
    async def send(*a,**kw):
        called.append(1)
        if len(called)==2: raise httpx.ReadTimeout('unknown')
        return httpx.Response(200,json={'data':[{'b64_json':b64()}],'usage':{'actual':8}})
    monkeypatch.setattr(runtime,'_send',send)
    result=await post(setup,{'model':'gpt-image-2','prompt':'icon','n':3})
    assert result.status_code==504
    body=result.json();assert len(body['data'])==1 and len(called)==2
    assert 'usage' not in body and body['parrot']['usage_by_call']==[{'actual':8}, None]
    assert not body['parrot']['complete']


@pytest.mark.asyncio
async def test_native_count_mismatch_returns_partial(setup,monkeypatch):
    async def send(*a,**kw): return httpx.Response(200,json={'data':[{'b64_json':b64()}]})
    monkeypatch.setattr(runtime,'_send',send)
    result=await post(setup,{'model':'grok-imagine-image','prompt':'icon','n':2})
    assert result.status_code==502 and len(result.json()['data'])==1
    assert result.json()['parrot']['upstream_calls']==1


def test_model_center_image_sources_include_antigravity(setup):
    control=ModelCenterControl()
    views=control.list_models(filters=ModelFilters(kinds=(ModelKind.IMAGE,))).items
    ids={v.model_id for v in views}
    assert {'gpt-image-2','gpt-image-2.5','grok-imagine-image','grok-imagine-image-quality','gemini-3.1-flash-image'} <= ids
    gpt=next(v for v in views if v.model_id=='gpt-image-2')
    assert gpt.aliases==('paint',) and gpt.sources and gpt.available_in()
    gemini=next(v for v in views if v.model_id=='gemini-3.1-flash-image')
    assert any(source.provider=='antigravity' for source in gemini.sources)


@pytest.mark.asyncio
async def test_antigravity_generation_uses_common_contract(setup):
    response=await post(setup,{'model':'gemini-3.1-flash-image','prompt':'icon','n':2,'size':'48x32'})
    assert response.status_code==200,response.text
    body=response.json()
    assert len(body['data'])==2 and body['parrot']['upstream_calls']==2
    ag_calls=[call for call in setup[1] if call[0].provider=='antigravity']
    assert len(ag_calls)==2 and all(call[3]==1 for call in ag_calls)


@pytest.mark.asyncio
async def test_antigravity_429_switches_account_without_disabling_chat(setup,monkeypatch):
    setup[0]['oauthAccounts'].append({
        'provider':'antigravity','email':'ag-second@example.test','project_id':'p2',
        'enabled':True,'access_token':'test-2','models':['gemini-chat'],
    })
    attempted=[];cooled=[]
    monkeypatch.setattr(runtime.cooldown,'record_error',lambda *args,**kwargs:cooled.append(args))
    async def send(source,parsed,*,action,n,cfg):
        attempted.append(source)
        if len(attempted)==1:
            return httpx.Response(429,json={'error':{'message':'image quota'}})
        return httpx.Response(200,json={'data':[{'b64_json':b64()}]})
    monkeypatch.setattr(runtime,'_send',send)
    result=await post(setup,{'model':'gemini-3.1-flash-image','prompt':'icon'})
    assert result.status_code==200,result.text
    assert len(attempted)==2 and attempted[0].key!=attempted[1].key
    assert cooled and cooled[0][1]=='gemini-3.1-flash-image'
    assert all(account['enabled'] for account in setup[0]['oauthAccounts'] if account['provider']=='antigravity')


@pytest.mark.asyncio
async def test_antigravity_edit_uses_common_contract(setup):
    response=await post(setup,{'model':'gemini-3.1-flash-image','prompt':'edit','image':ref()},path='/v1/images/edits')
    assert response.status_code==200,response.text
    assert len(setup[1])==1 and setup[1][0][2]=='edit'
    assert setup[1][0][1].input_images[0].startswith('data:image/png;base64,')


@pytest.mark.asyncio
async def test_antigravity_remote_edit_timeout_happens_before_paid_post(setup,monkeypatch):
    from src.antigravity import images as ag_images
    async def timeout(_reference):
        raise httpx.ReadTimeout('input timed out')
    monkeypatch.setattr(ag_images.image_artifacts,'reference_bytes',timeout)
    response=await post(setup,{
        'model':'gemini-3.1-flash-image','prompt':'edit',
        'image':'https://public.example.test/reference.png',
    },path='/v1/images/edits')
    assert response.status_code==504
    assert response.json()['error']['type']=='image_input_timeout'
    assert not setup[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("transport_error", [
    httpx.ConnectError("connect failed"),
    httpx.ReadError("read failed"),
    httpx.RemoteProtocolError("protocol failed"),
])
async def test_antigravity_transport_preprocessing_failure_does_not_block_xai(
    setup, monkeypatch, transport_error,
):
    from src.antigravity import images as ag_images
    setup[0]['image_models'] = {
        'xai': ['shared-transport-edit-model'],
        'antigravity': ['shared-transport-edit-model'],
    }

    async def unusable_for_ag(_reference):
        raise transport_error

    monkeypatch.setattr(ag_images.image_artifacts, 'reference_bytes', unusable_for_ag)
    reference = 'https://public.example.test/reference.png'
    response = await post(setup, {
        'model': 'shared-transport-edit-model', 'prompt': 'edit', 'image': reference,
    }, path='/v1/images/edits')
    assert response.status_code == 200, response.text
    assert len(setup[1]) == 1 and setup[1][0][0].provider == 'xai'
    assert setup[1][0][1].input_images == [reference]


@pytest.mark.asyncio
async def test_antigravity_only_transport_preprocessing_failure_is_safe_502(setup,monkeypatch):
    from src.antigravity import images as ag_images

    async def connect_failed(_reference):
        raise httpx.ConnectError('private endpoint detail')

    monkeypatch.setattr(ag_images.image_artifacts, 'reference_bytes', connect_failed)
    response = await post(setup, {
        'model': 'gemini-3.1-flash-image', 'prompt': 'edit',
        'image': 'https://public.example.test/private-reference.png',
    }, path='/v1/images/edits')
    assert response.status_code == 502
    assert response.json()['error'] == {
        'type': 'image_input_error',
        'message': 'reference image or mask download failed; no generation was attempted',
    }
    assert 'private' not in response.text
    assert not setup[1]


@pytest.mark.asyncio
async def test_antigravity_reference_preprocessing_does_not_change_xai_candidate(setup,monkeypatch):
    from src.antigravity import images as ag_images
    setup[0]['image_models']={
        'xai':['shared-edit-model'],
        'antigravity':['shared-edit-model'],
    }
    async def unusable_for_ag(_reference):
        raise ValueError('AG cannot inline this reference')
    monkeypatch.setattr(ag_images.image_artifacts,'reference_bytes',unusable_for_ag)
    reference='https://public.example.test/reference.png'
    response=await post(setup,{
        'model':'shared-edit-model','prompt':'edit','image':reference,
    },path='/v1/images/edits')
    assert response.status_code==200,response.text
    assert len(setup[1])==1 and setup[1][0][0].provider=='xai'
    assert setup[1][0][1].input_images==[reference]
    assert any('Antigravity' in warning for warning in response.json()['parrot']['warnings'])


@pytest.mark.asyncio
async def test_antigravity_high_quality_uses_generation_contract(setup):
    response=await post(setup,{'model':'gemini-3.1-flash-image','prompt':'icon','quality':'high'})
    assert response.status_code==200,response.text
    assert len(setup[1])==1 and setup[1][0][1].native_options['quality']=='high'


@pytest.mark.asyncio
async def test_antigravity_verified_opaque_output_keeps_transparency_rejected(setup):
    response=await post(setup,{'model':'gemini-3.1-flash-image','prompt':'icon','background':'transparent'})
    assert response.status_code==400
    assert 'verified opaque JPEG output without alpha' in response.json()['error']['message']
    assert not setup[1]


def test_api_mapping_classification_and_url(setup):
    from src.antigravity import images as ag_images
    cfg=setup[0]
    cfg['channels']=[{'name':'pixels','providerId':'antigravity','protocol':'openai-chat','baseUrl':'https://api.example/v1','models':[{'real':'gpt-image-1','alias':'private-painter'}]}]
    row=next(s for s in image_catalog.sources() if s.model=='private-painter')
    assert row.upstream=='gpt-image-1' and row.key=='api:pixels'
    assert not ag_images.is_source(row)
    assert runtime._api_url(SimpleNamespace(base_url='https://api.example/v1',api_path=None),'edit')=='https://api.example/v1/images/edits'
    assert runtime._api_url(SimpleNamespace(base_url='https://api.example',api_path='/custom/chat/completions'),'generate')=='https://api.example/custom/images/generations'


@pytest.mark.asyncio
@pytest.mark.parametrize('compressed', [False, True])
@pytest.mark.parametrize('status', [200, 400])
async def test_antigravity_decoded_response_headers_for_success_and_error(
    setup, monkeypatch, compressed, status,
):
    from src.antigravity import images as ag_images
    source = next(s for s in image_catalog.sources() if s.provider == 'antigravity')
    account = next(a for a in setup[0]['oauthAccounts'] if a['provider'] == 'antigravity')
    channel = SimpleNamespace(
        state_key=source.state_key,
        base_url='https://daily-cloudcode-pa.googleapis.com',
        project_id='project-x',
        tls_fingerprint=None,
        supports_media_model=lambda kind, model: kind == 'image' and model == source.upstream,
    )

    async def headers():
        return {'authorization': 'Bearer private-test-token', 'content-type': 'application/json'}

    channel.build_media_headers = headers
    monkeypatch.setattr(registry, 'get_channel', lambda key: channel)
    monkeypatch.setattr(image_catalog, 'current_oauth_account', lambda selected: (selected.key[6:], account))
    upstream = (
        {'response': {'candidates': [{'content': {'parts': [
            {'inlineData': {'mimeType': 'image/png', 'data': b64()}},
        ]}}]}}
        if status == 200 else
        {'error': {'message': 'ordinary rejection'}}
    )
    plain = json.dumps(upstream).encode()
    wire = gzip.compress(plain) if compressed else plain
    response_headers = {'content-type': 'application/json'}
    if compressed:
        response_headers['content-encoding'] = 'gzip'

    def transport(req):
        return httpx.Response(status, content=wire, headers=response_headers)

    monkeypatch.setattr(
        ag_images.network, 'async_client',
        lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(transport)),
    )
    parsed = compat._ParsedRequest(model=source.model, prompt='boat')
    result = await ag_images.request(
        source, parsed, prompt='boat', n=1, action='generate', cfg={},
    )
    assert result.status_code == status
    assert 'content-encoding' not in result.headers
    assert int(result.headers['content-length']) == len(result.content)
    if status == 200:
        assert result.json()['data'][0]['b64_json'] == b64()
    else:
        assert result.json() == upstream


@pytest.mark.asyncio
async def test_antigravity_fingerprint_uses_shared_route_runtime_and_closes(setup,monkeypatch):
    import src.transports as transports
    from src.antigravity import images as ag_images

    source = next(s for s in image_catalog.sources() if s.provider == 'antigravity')
    account = next(a for a in setup[0]['oauthAccounts'] if a['provider'] == 'antigravity')
    channel = SimpleNamespace(
        state_key=source.state_key,
        base_url='https://daily-cloudcode-pa.googleapis.com',
        project_id='project-x',
        tls_fingerprint='chrome131',
        supports_media_model=lambda kind, model: kind == 'image' and model == source.upstream,
    )

    async def headers():
        return {'authorization': 'Bearer private-test-token', 'content-type': 'application/json'}

    channel.build_media_headers = headers
    monkeypatch.setattr(registry, 'get_channel', lambda key: channel)
    monkeypatch.setattr(image_catalog, 'current_oauth_account', lambda selected: (selected.key[6:], account))
    monkeypatch.setattr(
        ag_images.network, 'async_client',
        lambda **kw: (_ for _ in ()).throw(AssertionError('fingerprinted image must not use generic client')),
    )
    closed = []

    class Context:
        async def __aexit__(self, *_args):
            closed.append('response')

    class Client:
        async def aclose(self):
            closed.append('client')

    upstream = {'response': {'candidates': [{'content': {'parts': [
        {'inlineData': {'mimeType': 'image/png', 'data': b64()}},
    ]}}]}}
    request_obj = httpx.Request('POST', 'https://daily-cloudcode-pa.googleapis.com/v1internal:generateContent')
    response_obj = httpx.Response(200, json=upstream, request=request_obj)
    captured = {}

    async def open_response(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            ok=True, error=None, response=response_obj, ctx=Context(),
            proxy_client=Client(), timing=None, round_timeouts=None,
            proxy_attempt_id=None, proxy_bytes={}, terminal_persistence_scheduled=False,
        )

    monkeypatch.setattr(transports, 'open_response_with_proxy_chain', open_response)
    parsed = compat._ParsedRequest(model=source.model, prompt='boat')
    result = await ag_images.request(
        source, parsed, prompt='boat', n=1, action='generate',
        cfg={'requestTimeoutSeconds': 91},
    )
    assert result.status_code == 200
    assert captured['channel'] is channel
    assert captured['proxy_purpose'] == 'oauth_antigravity'
    assert captured['request_id'] is None
    assert captured['response_mode'] == 'non_stream'
    assert captured['total_timeout'] == 91
    assert captured['upstream_req'].url.endswith('/v1internal:generateContent')
    assert json.loads(captured['upstream_req'].body)['requestType'] == 'image_gen'
    assert closed == ['response', 'client']


@pytest.mark.asyncio
async def test_antigravity_fingerprint_body_error_closes_response_and_client(monkeypatch):
    import src.transports as transports
    from src.antigravity import images as ag_images

    closed = []

    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise httpx.ReadError('body failed')
            yield b''

    class Context:
        async def __aexit__(self, *_args):
            closed.append('response')

    class Client:
        async def aclose(self):
            closed.append('client')

    request_obj = httpx.Request('POST', 'https://daily-cloudcode-pa.googleapis.com/v1internal:generateContent')
    response_obj = httpx.Response(200, stream=BrokenStream(), request=request_obj)

    async def open_response(**_kwargs):
        return SimpleNamespace(
            ok=True, error=None, response=response_obj, ctx=Context(),
            proxy_client=Client(), timing=None, round_timeouts=None,
            proxy_attempt_id=None, proxy_bytes={}, terminal_persistence_scheduled=False,
        )

    monkeypatch.setattr(transports, 'open_response_with_proxy_chain', open_response)
    with pytest.raises(httpx.ReadError, match='body failed'):
        await ag_images._fingerprinted_post(
            SimpleNamespace(),
            model='gemini-3.1-flash-image',
            url=str(request_obj.url),
            headers={},
            body=b'{}',
            timeout_seconds=90,
        )
    assert closed == ['response', 'client']


@pytest.mark.asyncio
async def test_send_antigravity_envelope_and_normalized_response(setup,monkeypatch):
    from src.antigravity import images as ag_images
    source=next(s for s in image_catalog.sources() if s.provider=='antigravity')
    account=next(a for a in setup[0]['oauthAccounts'] if a['provider']=='antigravity')
    channel=SimpleNamespace(
        state_key=source.state_key, base_url='https://daily-cloudcode-pa.googleapis.com',
        project_id='project-x', supports_media_model=lambda kind,model:kind=='image' and model==source.upstream,
    )
    async def headers(): return {'authorization':'Bearer private-test-token','content-type':'application/json'}
    channel.build_media_headers=headers
    monkeypatch.setattr(registry,'get_channel',lambda key:channel)
    monkeypatch.setattr(image_catalog,'current_oauth_account',lambda selected:(selected.key[6:],account))
    seen=[]
    upstream={'response':{'candidates':[{'content':{'parts':[{'inlineData':{'mimeType':'image/png','data':b64()}}]}}],
        'usageMetadata':{'promptTokenCount':5,'candidatesTokenCount':7,'totalTokenCount':12}}}
    def transport(req):
        seen.append(req);return httpx.Response(200,json=upstream)
    monkeypatch.setattr(ag_images.network,'async_client',lambda **kw:httpx.AsyncClient(transport=httpx.MockTransport(transport)))
    parsed=compat._ParsedRequest(model=source.model,prompt='boat',size='48x32',native_options={'quality':'standard'})
    result=await ag_images.request(source,parsed,prompt='boat',n=1,action='generate',cfg={})
    assert result.status_code==200
    body=result.json();assert body['data'][0]['b64_json']==b64() and body['usage']['total_tokens']==12
    wire=json.loads(seen[0].content)
    assert wire['model']=='gemini-3.1-flash-image' and wire['requestType']=='image_gen'
    assert wire['request']['generationConfig']['responseModalities']==['IMAGE']
    assert wire['request']['generationConfig']['imageConfig']=={'aspectRatio':'3:2','imageSize':'1K'}
    assert 'private-test-token' not in seen[0].content.decode()


def test_antigravity_2k_and_edit_envelopes(setup):
    from src.antigravity import images as ag_images
    high=compat._ParsedRequest(model='gemini-3.1-flash-image',prompt='icon',native_options={'quality':'high'})
    generated=ag_images.build_request(high,prompt='icon',n=1,action='generate')
    assert generated['generationConfig']['imageConfig']=={'imageSize':'2K'}
    mask=ref(png((0,0,0,0),mode='RGBA'))
    edited=compat._ParsedRequest(model='gemini-3.1-flash-image',prompt='edit',input_images=[ref()],mask_url=mask)
    request=ag_images.build_request(edited,prompt='edit',n=1,action='edit')
    parts=request['contents'][0]['parts']
    assert parts[0]=={'text':'edit'} and parts[1]=={'text':'Reference image 1:'}
    assert parts[2]['inlineData']['mimeType']=='image/png'
    assert base64.b64decode(parts[2]['inlineData']['data'])==png()
    assert parts[3]['text'].startswith('Edit mask for reference image 1:')
    assert parts[4]['inlineData']['mimeType']=='image/png'
    assert base64.b64decode(parts[4]['inlineData']['data'])==png((0,0,0,0),mode='RGBA')


@pytest.mark.asyncio
async def test_send_codex_exact_endpoint_and_prompt(setup,monkeypatch):
    # Exercise the real adapter, not the fake send installed by the ingress fixture.
    from importlib import reload
    real_send=reload(runtime)._send
    source=next(s for s in image_catalog.sources() if s.provider=='openai')
    setup[0]['oauthAccounts'][0].update(
        workspace_backend_origin='https://gov.chatgpt.com', account_routing_override='us_cr')
    monkeypatch.setattr(oauth_manager,'get_account',lambda key:setup[0]['oauthAccounts'][0])
    async def token(*a, expected_state_key=None):
        assert expected_state_key == source.state_key
        return 'private-test-token'
    monkeypatch.setattr(oauth_manager,'ensure_valid_token',token)
    monkeypatch.setattr(runtime,'codex_responses_url',lambda cfg:'https://chatgpt.com/backend-api/codex/responses')
    monkeypatch.setattr(images_simple,'_build_headers',lambda *a:{'Authorization':'Bearer private-test-token'})
    seen=[]
    def transport(req):
        seen.append(req);return httpx.Response(200,json={'data':[{'b64_json':b64()}]})
    monkeypatch.setattr(runtime.network,'async_client',lambda **kw:httpx.AsyncClient(transport=httpx.MockTransport(transport)))
    parsed=compat._ParsedRequest(model='gpt-image-2',prompt='boat',size='48x32',native_options={'background':'transparent'})
    response=await real_send(source,parsed,action='generate',n=1,cfg={})
    assert response.status_code==200 and str(seen[0].url).endswith('/codex/images/generations')
    assert seen[0].url.host == 'gov.chatgpt.com'
    assert seen[0].headers['x-openai-account-routing-override'] == 'us_cr'
    body=json.loads(seen[0].content); assert body['model']=='gpt-image-2' and body['n']==1
    assert 'alpha=0' in body['prompt'] and '48 by 32' in body['prompt']
    assert 'tools' not in body

@pytest.mark.asyncio
@pytest.mark.parametrize('response_format',['','   '])
async def test_empty_response_format_preserves_b64_default(setup,response_format):
    result=await post(setup,{'model':'gpt-image-2','prompt':'icon','response_format':response_format})
    assert result.status_code==200 and 'b64_json' in result.json()['data'][0]


@pytest.mark.asyncio
async def test_safe_rejection_failover_and_retry_after(setup,monkeypatch):
    cfg=setup[0]
    extra=copy.deepcopy(cfg['oauthAccounts'][0]); extra.update(email='other@example.test',chatgpt_account_id='other')
    cfg['oauthAccounts'].append(extra)
    called=[]
    async def send(source,*a,**kw):
        called.append(source.key)
        return httpx.Response(429,headers={'retry-after':'42'},json={'error':{'message':'quota'}})
    monkeypatch.setattr(runtime,'_send',send)
    result=await post(setup,{'model':'gpt-image-2','prompt':'icon','n':2})
    assert result.status_code==429 and result.headers['retry-after']=='42'
    assert len(called)==len(set(called))==2
    assert result.json()['parrot']['upstream_calls']==2


@pytest.mark.asyncio
async def test_antigravity_upstream_error_redacts_project_identity(setup,monkeypatch):
    account=next(item for item in setup[0]['oauthAccounts'] if item['provider']=='antigravity')
    account['project_id']='project-secret-42'
    async def send(*args,**kwargs):
        return httpx.Response(403,json={'error':{'message':'denied project-secret-42 for ag@example.test'}})
    monkeypatch.setattr(runtime,'_send',send)
    response=await post(setup,{'model':'gemini-3.1-flash-image','prompt':'icon'})
    message=response.json()['error']['message']
    assert response.status_code==403
    assert 'project-secret-42' not in message and 'ag@example.test' not in message
    assert '[private]' in message


@pytest.mark.asyncio
async def test_success_after_safe_rejection(setup,monkeypatch):
    cfg=setup[0]
    extra=copy.deepcopy(cfg['oauthAccounts'][0]);extra.update(email='other@example.test',chatgpt_account_id='other');cfg['oauthAccounts'].append(extra)
    called=[]
    async def send(source,*a,**kw):
        called.append(source.key)
        return httpx.Response(403,json={}) if len(called)==1 else httpx.Response(200,json={'data':[{'b64_json':b64()}]})
    monkeypatch.setattr(runtime,'_send',send)
    result=await post(setup,{'model':'gpt-image-2','prompt':'icon','n':2})
    assert result.status_code==200
    assert len(called)==3 and called[0]!=called[1]==called[2]
    assert len(result.json()['data'])==2


@pytest.mark.asyncio
async def test_cancel_during_generation_releases_slot_and_logs_once(setup,monkeypatch):
    import asyncio
    reached=asyncio.Event();finished=[];released=[];posts=[]
    async def send(*a,**kw):
        posts.append(1);reached.set();await asyncio.Event().wait()
    async def finish(log_id,**kw):finished.append(kw)
    monkeypatch.setattr(runtime,'_send',send)
    monkeypatch.setattr(runtime.imagine,'_finish_media_log',finish)
    monkeypatch.setattr(runtime.concurrency,'release',lambda key:released.append(key))
    task=asyncio.create_task(post(setup,{'model':'gpt-image-2','prompt':'icon','n':2}))
    await reached.wait();task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert len(posts)==len(released)==1
    assert len(finished)==1 and finished[0]['status']=='cancelled'


@pytest.mark.asyncio
async def test_api_edits_json_input_is_multipart_upstream(setup,monkeypatch):
    from importlib import reload
    real_send=reload(runtime)._send
    channel=SimpleNamespace(base_url='https://api.example/v1',api_path=None,api_key='private-platform-key')
    setup[0]['channels'] = [dict(name='fixture', protocol='openai-chat', enabled=True, models=[dict(real='gpt-image-1', alias='paint-api', kind='image')])]
    source = next(row for row in image_catalog.sources() if row.key == 'api:fixture')
    channel_state_key = source.state_key
    monkeypatch.setattr(registry,'get_channel',lambda key:channel)
    seen=[]
    def transport(req):
        seen.append(req);return httpx.Response(200,json={'data':[{'b64_json':b64()}]})
    monkeypatch.setattr(runtime.network,'async_client',lambda **kw:httpx.AsyncClient(transport=httpx.MockTransport(transport)))
    parsed=compat._ParsedRequest(model='paint-api',prompt='edit',input_images=[ref(),ref(png('blue'))],mask_url=ref(png((0,0,0,0),mode='RGBA')))
    channel.state_key = channel_state_key
    result=await real_send(source,parsed,action='edit',n=1,cfg={})
    assert result.status_code==200
    req=seen[0];assert str(req.url)=='https://api.example/v1/images/edits'
    assert req.headers['content-type'].startswith('multipart/form-data;')
    assert req.content.count(b'name="image[]"')==2 and b'name="mask"' in req.content
    assert b'gpt-image-1' in req.content and b'private-platform-key' not in req.content


def test_image_global_source_and_visibility_state_mutations(setup,monkeypatch):
    from contextlib import nullcontext
    cfg=setup[0]
    monkeypatch.setattr(config,'serialized_updates',lambda:nullcontext())
    monkeypatch.setattr(config,'observe_reload_failures',lambda:nullcontext([]))
    monkeypatch.setattr(config,'update',lambda mutator,**kw:mutator(cfg))
    control=ModelCenterControl()
    selection=ModelSelection(ModelSelectionMode.IDS,('gpt-image-2',))
    view=next(v for v in control.list_models().items if v.model_id=='gpt-image-2')
    scope=ModelSourceRef(ModelSourceType.OAUTH,view.sources[0].id)
    control.set_state(None,scope=scope,selection=selection,target=ModelStateTarget(ModelStateField.ENABLED,False),expected_revision=view.revision)
    assert not next(s for s in image_catalog.sources() if s.model=='gpt-image-2').source_enabled
    assert cfg['oauthAccounts'][0]['enabled'] is True
    view=next(v for v in control.list_models().items if v.model_id=='gpt-image-2')
    control.set_state(None,scope=None,selection=selection,target=ModelStateTarget(ModelStateField.VISIBLE,False),expected_revision=view.revision)
    assert 'gpt-image-2' not in image_catalog.available_models()
    assert 'gpt-image-2' in cfg['modelCenter']['hiddenModels']

@pytest.mark.asyncio
async def test_model_discovery_lists_antigravity_image_not_chat_model(setup,monkeypatch):
    import server
    from starlette.requests import Request
    monkeypatch.setattr(registry,'available_models',lambda:[])
    request=Request({'type':'http','method':'GET','path':'/v1/models','headers':[]})
    ids={item['id'] for item in (await server.list_models(request))['data']}
    assert 'gemini-3.1-flash-image' in ids
    assert 'gemini-chat' not in ids


@pytest.mark.asyncio
async def test_model_discovery_image_alias_permissions_and_disabled_source(setup,monkeypatch):
    import server
    from starlette.requests import Request
    monkeypatch.setattr(registry,'available_models',lambda:[])
    monkeypatch.setattr(auth,'validate',lambda headers:('test',['paint'],None))
    request=Request({'type':'http','method':'GET','path':'/v1/models','headers':[]})
    result=await server.list_models(request)
    assert [item['id'] for item in result['data']]==['paint']
    setup[0]['oauthAccounts'][0]['enabled']=False
    assert (await server.list_models(request))['data']==[]
    # app.routes 在不同 FastAPI 版本下可能包含没有 .path 的内部条目
    # （例如 _IncludedRouter），与既有视频路由测试保持同样的取法。
    paths={getattr(route,'path','') for route in server.app.routes}
    assert {'/images/generations','/images/edits','/v1/images/assets/{token}'}<=paths


@pytest.mark.asyncio
async def test_multipart_generation_mask_is_not_silently_discarded(setup):
    result=await post(setup,data={'model':'gpt-image-2','prompt':'icon'},files={'mask':('mask.png',png((0,0,0,0),mode='RGBA'),'image/png')})
    assert result.status_code==400 and not setup[1]


@pytest.mark.asyncio
async def test_fidelity_is_explicit_prompt_adaptation_not_unsupported_legacy_field(setup,monkeypatch):
    result=await post(setup,{'model':'gpt-image-2','prompt':'edit','image':ref(),'input_fidelity':'high'},path='/v1/images/edits')
    assert result.status_code==200
    assert 'not a native fidelity control' in result.json()['parrot']['warnings'][0]
    assert 'high fidelity' in runtime._prompt(setup[1][0][1])


@pytest.mark.asyncio
async def test_url_batch_storage_eviction_cannot_claim_complete(setup):
    setup[0]['images']['cacheMaxBytes']=len(png())+20
    result=await post(setup,{'model':'gpt-image-2','prompt':'icon','response_format':'url','n':2})
    assert result.status_code==507, result.text
    body=result.json();assert not body['parrot']['complete'] and body['parrot']['upstream_calls']==2
    assert all(image_artifacts.url_available(item['url']) for item in body['data'])
