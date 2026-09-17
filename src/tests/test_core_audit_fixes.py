"""CORE-01–13 correct-behavior regressions promoted from the independent audit."""
import copy
import json
import os
import time
from types import SimpleNamespace

import pytest
from src import config, model_metadata, model_pricing, model_mapping, token_counter, failover
from src.channel import registry
from src.management_control.models import (ModelCenterControl, ModelFilters, ModelKind, ModelSelection, ModelSelectionMode, ModelStateField, ModelStateTarget)
from src.management_control.mapping import MappingControl
from src.tests.test_management_mapping_support import domain_client

@pytest.fixture(autouse=True)
def clean_review(monkeypatch):
    assert os.environ['PARROT_TEST_NETWORK_GUARD'] == 'loopback-only'
    monkeypatch.setattr(config, '_reload_callbacks', [])
    config.update(lambda c: (c.clear(), c.update(copy.deepcopy(config.DEFAULT_CONFIG))))
    monkeypatch.setattr(registry, '_sync_state_db_with_channels', lambda: None)
    registry.rebuild_from_config()


def channel(name='A', model='demo', protocol='anthropic'):
    return {'name': name, 'protocol': protocol, 'enabled': True,
            'baseUrl': 'https://fixture.invalid', 'apiKey': 'review-fixture',
            'models': [{'real': model, 'alias': model}]}


def test_query_source_id_without_type_must_not_expand_scope(domain_client):
    client, runtime, admin, *_ = domain_client
    config.update(lambda c: c.update(channels=[channel('A'), channel('B', 'other')]))
    result = client.get('/api/management/v1/models?type=chat&sourceId=api%3Amissing', headers=admin)
    assert result.status_code == 422, result.text


def test_filter_selection_revision_freezes_label_matching():
    config.update(lambda c: c.update(oauthAccounts=[
        {'provider':'claude','email':'a@example.test','label':'focus','models':['model-a']},
        {'provider':'claude','email':'b@example.test','label':'other','models':['model-b']},
    ]))
    control = ModelCenterControl()
    filters = ModelFilters(kinds=(ModelKind.CHAT,), text='focus')
    initial = control.list_models(filters=filters)
    assert [i.model_id for i in initial.items] == ['model-a']
    def rename(c):
        c['oauthAccounts'][0]['label'] = 'other'
        c['oauthAccounts'][1]['label'] = 'focus'
    config.update(rename)
    from src.management_control.errors import ManagementError, ManagementErrorCode
    before = copy.deepcopy(config.get())
    with pytest.raises(ManagementError) as error:
        control.set_state(None, scope=None, selection=ModelSelection(ModelSelectionMode.FILTER, filters=filters), target=ModelStateTarget(ModelStateField.ENABLED, False), expected_revision=initial.revision)
    assert error.value.code is ManagementErrorCode.REVISION_CONFLICT
    assert config.get() == before


def test_cursor_tightening_must_reach_effective_budget():
    from src.tests.test_cursor_oauth_integration import _account
    config.update(lambda c: c.update(oauthAccounts=[_account()]))
    scope = 'oauth:cursor:cursor-user-1'
    model_metadata.patch_override_fields('claude-fable-5', scope_key=scope, outbound_model='claude-fable-5',
        set_fields={'contextWindow':200000, 'maxOutputTokens':10000, 'toolCall':False, 'reasoningEfforts':[]})
    binding = model_metadata.resolve_binding('claude-fable-5', scope_key=scope, outbound_model='claude-fable-5')
    assert binding.source_override['contextWindow'] == 200000
    budget = model_metadata.effective_request_budget('claude-fable-5', scope_key=scope, outbound_model='claude-fable-5', request_shape={'max_tokens':10000})
    assert budget.context_window == 200000, (dict(binding.metadata), budget)


def test_legacy_channel_sync_without_revision_finishes_successfully(domain_client, monkeypatch):
    client, runtime, admin, *_ = domain_client
    config.update(lambda c: c.update(channels=[channel(model='gpt-5.4')]))
    registry.rebuild_from_config()
    assert model_pricing.binding_snapshot('openai/gpt-5.4')
    workers = []
    monkeypatch.setattr(runtime.operations, 'submit', lambda op, worker: workers.append(worker))
    result = client.post('/api/management/v1/model-metadata/actions/sync', headers=admin,
        json={'scope':'channel','channelId':'api:A','refreshCatalog':False})
    assert result.status_code == 202, result.text
    workers[0]()
    terminal = client.get('/api/management/v1/operations/' + result.json()['data']['id'], headers=admin).json()['data']
    assert terminal['status'] == 'succeeded', terminal


def test_disabled_oauth_model_overrides_remain_manageable(domain_client):
    client, runtime, admin, *_ = domain_client
    config.update(lambda c: c.update(oauthAccounts=[{
        'provider':'claude','email':'disabled@example.test','models':['model-a'], 'disabledModels':['model-a']
    }]))
    registry.rebuild_from_config()
    listing = client.get('/api/management/v1/models?type=chat', headers=admin).json()
    assert any(i['modelId']=='model-a' for i in listing['data'])
    control = runtime.controls.mapping if getattr(runtime, 'controls', None) else MappingControl()
    revision = control._metadata_revision()
    result = client.patch('/api/management/v1/model-metadata/model-a/overrides', headers={**admin,'If-Match':revision},
        json={'scope':'oauth','accountId':'claude:disabled@example.test','set':{'contextWindow':100000}})
    assert result.status_code == 200, result.text


@pytest.mark.asyncio
@pytest.mark.parametrize("oversized", [True, False])
async def test_final_responses_instructions_count_before_real_channel_transport(monkeypatch, oversized):
    from src.openai.channel.registration import register_factories
    from src.protocols.runtime import AttemptResult
    register_factories()
    config.update(lambda c: c.update(channels=[channel(protocol='openai-responses')], modelMetadataOverrides={
        'defaults':{'demo':{'fields':{'contextWindow':100, 'maxInputTokens':100}}}, 'scoped':{}}))
    registry.rebuild_from_config()
    ch = registry.get_channel('api:A')
    body = {'model':'demo', '_client_visible_model':'demo', 'input':'hello', 'instructions':'this is long context ' * 1000}
    assert token_counter.count_text_tokens(body['instructions'], model='demo') > 100
    if not oversized:
        body['instructions'] = 'Be helpful'
    sent = []
    async def fake_transport(**kw):
        sent.append(json.loads(kw['upstream_req'].body))
        return SimpleNamespace(error=AttemptResult(outcome='transport_error', error_detail='review transport sentinel'))
    monkeypatch.setattr(failover,'open_response_with_proxy_chain',fake_transport)
    monkeypatch.setattr(failover.log_db,'update_pending_fast_mode_from_upstream',lambda *a, **k: None)
    now = time.time()
    result = await failover._try_channel(ch, 'demo', body, False, now+60, now, None, [], None,
        '127.0.0.1','review',0,0, ingress_protocol='responses', start_monotonic=time.monotonic(), attempt_start_monotonic=time.monotonic())
    # Input length is the upstream's authority now: even an oversized local
    # estimate is dispatched with instructions preserved.
    assert result.outcome == 'transport_error' and len(sent) == 1
    assert sent[0]['instructions'] == body['instructions']


def test_snapshot_explicitly_unpriced_must_not_reuse_active_catalog_price():
    snapshot = model_pricing.binding_snapshot('openai/gpt-5.4')
    assert snapshot and snapshot['tariff']
    snapshot['catalogRevision'] = 'candidate-price-unknown'
    snapshot['metadata']['cost'] = None
    snapshot['tariff'] = None
    config.update(lambda c: c.update(modelBindings={'defaults':{'demo':{'target':'openai/gpt-5.4','source':'auto','autoSnapshot':snapshot}},'scoped':{}}))
    assert model_metadata.get_metadata('demo')['cost'] is None
    priced = model_pricing.build_pricing_binding(channel_key='api:A',channel_type='api',upstream_protocol='openai-responses',outbound_model_id='demo',client_visible_model='demo')
    assert priced.tariff is None, priced


def test_new_alias_patch_rejects_account_real_model_collision():
    config.update(lambda c: c.update(oauthAccounts=[{'provider':'claude','email':'catalog@example.test','models':['real-a','real-b']}],
        modelMapping={'global':{'old':'real-b'}}))
    registry.rebuild_from_config()
    assert 'real-a' in registry.available_models()
    control = MappingControl()
    from src.management_control.errors import ManagementError, ManagementErrorCode
    with pytest.raises(ManagementError) as err:
        control.update_mapping(control.current_context(), 'old', new_alias='real-a', real_model='real-b', expected_revision=control._mapping_revision())
    assert err.value.code is ManagementErrorCode.RESOURCE_CONFLICT


def test_catalog_explicit_input_limit_reaches_new_budget(monkeypatch):
    # A fully specified controlled source record, not guessed metadata.
    raw = {'id':'limited','limit':{'context':1000,'input':100,'output':200},'cost':{'input':1,'output':2}}
    monkeypatch.setattr(model_pricing,'catalog_model',lambda target:copy.deepcopy(raw))
    config.update(lambda c: c.update(modelBindings={'defaults':{'demo':{'target':'fixture/limited','source':'manual'}},'scoped':{}}))
    budget = model_metadata.effective_request_budget('demo', request_shape={'max_tokens':20})
    assert budget.effective_input_budget == 100, budget


def test_service_tiers_native_record_can_be_read_by_metadata_api(domain_client):
    from fastapi.testclient import TestClient
    client, runtime, admin, *_ = domain_client
    # Production discovery shape also asserted in test_oauth_account_models.py.
    config.update(lambda c: c.update(oauthAccounts=[{
        'provider':'openai','email':'tiers@example.test','workspace_id':'ws','models':['tier-model'],
        'account_model_catalog':{'models':[{'id':'tier-model','contextWindow':200000,
            'serviceTiers':[{'id':'priority','name':'Fast'}]}]}
    }]))
    registry.rebuild_from_config()
    with TestClient(client.app, raise_server_exceptions=False) as public_client:
        result = public_client.get('/api/management/v1/model-metadata/tier-model?scopeId=openai%3Atiers%40example.test%3Aws', headers=admin)
    assert result.status_code == 200, (result.status_code, result.text)


def test_nonfinite_price_must_be_rejected_before_persistence(domain_client):
    from fastapi.testclient import TestClient
    client, runtime, admin, *_ = domain_client
    model_metadata.set_binding('demo', 'openai/gpt-5.4')
    revision = MappingControl._metadata_revision()
    with TestClient(client.app, raise_server_exceptions=False) as public_client:
        result = public_client.patch('/api/management/v1/model-metadata/demo/overrides',
            headers={**admin,'If-Match':revision,'Content-Type':'application/json'},
            content='{"scope":"global","set":{"cost":{"input":1e309}}}')
    fields = model_metadata.get_override_fields('demo')[0]
    price_error = None
    try:
        model_pricing.build_pricing_binding(channel_key='api:A',channel_type='api',upstream_protocol='openai-responses',outbound_model_id='demo',client_visible_model='demo')
    except ValueError as exc:
        price_error = str(exc)
    assert result.status_code == 422 and fields == {}, (result.status_code, fields, price_error)


def test_full_sync_cannot_overwrite_concurrent_manual_binding(domain_client, monkeypatch):
    import threading
    client, runtime, admin, *_ = domain_client
    config.update(lambda c: c.update(channels=[channel(model='gpt-5.4')]))
    registry.rebuild_from_config()
    model_metadata.set_binding('gpt-5.4','openai/gpt-5.4',source='auto')
    revision = MappingControl._metadata_revision()
    workers = []
    monkeypatch.setattr(runtime.operations,'submit',lambda op, worker:workers.append(worker))
    reached, release = threading.Event(), threading.Event()
    original = model_pricing.canonical_official_model
    def pause(name):
        if name == 'gpt-5.4':
            reached.set()
            assert release.wait(10)
        return original(name)
    monkeypatch.setattr(model_pricing,'canonical_official_model',pause)
    result = client.post('/api/management/v1/model-metadata/actions/sync', headers={**admin,'If-Match':revision},
        json={'mode':'full','refreshCatalog':False})
    assert result.status_code == 202, result.text
    thread = threading.Thread(target=workers[0])
    thread.start()
    try:
        assert reached.wait(10)
        model_metadata.set_binding('gpt-5.4','xai/grok-4.5',source='manual')
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive()
    terminal = client.get('/api/management/v1/operations/'+result.json()['data']['id'],headers=admin).json()['data']
    saved = config.get()['modelBindings']['defaults']['gpt-5.4']
    assert saved['source'] == 'manual' and saved['target'] == 'xai/grok-4.5', (terminal, saved)
    assert terminal['status'] == 'failed'
    assert terminal['error']['code'] == 'REVISION_CONFLICT'
    assert terminal['error']['retryable'] is False


def test_oauth_state_reload_failure_is_not_ordinary_success(domain_client, monkeypatch):
    from src import scheduler
    client, runtime, admin, *_ = domain_client
    config.update(lambda c:c.update(oauthAccounts=[{
        'provider':'claude','email':'reload@example.test','models':['model-a'],'enabled':True}]))
    registry.rebuild_from_config()
    def fail_reload(cfg):
        raise RuntimeError('review simulated registry reload failure')
    monkeypatch.setattr(config,'_reload_callbacks',[fail_reload])
    listing = client.get('/api/management/v1/models?type=chat',headers=admin).json()
    result = client.patch('/api/management/v1/models/actions/state', headers={**admin,'If-Match':listing['meta']['revision']},
        json={'scope':{'type':'oauth','id':'claude:reload@example.test'}, 'selection':{'mode':'ids','modelIds':['model-a']},'target':{'enabled':False}})
    route = scheduler.schedule({'model':'model-a','messages':[]},'review','127.0.0.1')
    assert result.status_code == 503, result.text
    error = result.json()['error']
    assert error['retryable'] is False
    assert error['fields'][0]['code'] == 'SAVED_RELOAD_UNCONFIRMED'
    assert config.get()['oauthAccounts'][0]['disabledModels'] == ['model-a']
    assert not route.candidates
