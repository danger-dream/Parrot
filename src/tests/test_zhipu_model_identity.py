"""Provider-owned identity across discovery, preferences and request failover."""
from __future__ import annotations

import copy
import json
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from src import auth, config, cooldown, load_balancing, model_metadata, model_names, model_state, oauth_manager as om, state_db
from src.channel.api_channel import ApiChannel
from src.channel.zhipu_oauth_channel import ZhipuOAuthChannel
from src.management_control.models import ModelCenterControl
from src.tests.test_zhipu_channel import env, call, body, fake  # noqa: F401
from src.tests.test_zhipu_model_discovery import install
from src.tests.test_zhipu_provider import account_env, credential  # noqa: F401


MODELS = ['GLM-5.3', 'GLM-5.3-Flash', 'GLM-Next-Test']


@pytest.mark.parametrize('model', MODELS)
def test_legacy_catalog_identity_and_disabled_aliases(account_env, model):
    account = credential(models=[model, model.lower()], account_model_catalog={'models': [
        {'id': model, 'name': model, 'contextWindow': 1000000},
        {'id': model.lower(), 'name': model},
    ]})
    before = copy.deepcopy(account)
    channel = ZhipuOAuthChannel(account)
    assert channel.list_client_models() == [model.lower()]
    for spelling in (model, model.lower(), model.swapcase()):
        assert channel.supports_model(spelling) == model.lower()
    records = om.account_model_records(account)
    assert len(records) == 1 and records[0]['upstreamId'] == model
    assert account == before
    for spelling in (model, model.lower()):
        account['disabledModels'] = [spelling]
        config.update(lambda c: c.update(oauthAccounts=[account]))
        assert om.account_model_selection(account)['effective_models'] == []
        assert not model_state.is_source_enabled(channel.key, model)
        assert not model_state.is_source_enabled(channel.key, model.lower())
        assert ZhipuOAuthChannel(account).supports_model(model) is None
        om.set_account_model_disabled(channel.account_key, model.swapcase(), False)
        assert om.account_disabled_models(channel.account_key) == set()


def test_center_groups_accounts_without_rewriting_saved_data(account_env):
    first = credential(models=MODELS)
    second = credential(model_key='second.secret', models=[m.lower() for m in MODELS])
    config.update(lambda c: c.update(oauthAccounts=[first, second], channels=[], modelCenter={}))
    before = copy.deepcopy(config.get())
    rows = ModelCenterControl().list_models().items
    for model in MODELS:
        matches = [r for r in rows if r.model_id.casefold() == model.casefold()]
        assert len(matches) == 1 and matches[0].model_id == model.lower()
        assert len(matches[0].sources) == 2
    assert config.get() == before


@pytest.mark.parametrize('model', MODELS)
async def test_sync_keeps_identity_preferences_and_wire_id(account_env, monkeypatch, model):
    monkeypatch.setattr(om, 'mock_mode_enabled', lambda: False)
    account = credential(models=[model], account_model_catalog={'models': [{'id': model, 'name': model}]})
    om.add_account(account)
    key = om.get_account_key(account)
    scope = 'oauth:' + key
    config.update(lambda c: c.update(
        modelCenter={'disabledModels': [model], 'hiddenModels': [model]},
        modelMetadataOverrides={'defaults': {model: {'fields': {'maxOutputTokens': 1234}}},
            'scoped': {scope: {model: {'outboundModel': model, 'fields': {'contextWindow': 64000}}}}},
    ))
    before = copy.deepcopy(config.get())
    install(monkeypatch, [{'id': model.lower(), 'display_name': model}])
    result = await om.refresh_account_models(key)
    assert result['action'] == 'updated'
    assert result['added'] == [] and result['removed'] == []
    saved = om.get_account(key)
    assert saved['models'] == [model.lower()]
    assert saved['account_model_catalog']['models'][0]['upstreamId'] == model.lower()
    assert model_state.is_global_enabled(model.lower())
    assert model_state.is_discovery_visible(model.lower())
    assert not model_state.is_global_enabled(model.lower(), source_key=scope)
    assert not model_state.is_discovery_visible(model.lower(), source_key=scope)
    assert model_state.known_model(model)
    for spelling in (model, model.lower(), model.swapcase()):
        common, source = model_metadata.get_override_fields(spelling, scope_key=scope, outbound_model=model.lower())
        assert common == {'maxOutputTokens': 1234}
        assert source == {'contextWindow': 64000}
    for field in ('modelCenter', 'modelMetadataOverrides'):
        assert config.get()[field] == before[field]
    config.update(lambda c: model_state.set_global_enabled_in_config(c, [model], True))
    assert model_state.is_global_enabled(model)
    request = await ZhipuOAuthChannel(saved).build_upstream_request(
        {'model': model, 'messages': [{'role': 'user', 'content': 'test'}]}, model)
    assert json.loads(request.body)['model'] == model.lower()


def test_metadata_edits_merge_old_keys_without_resurrecting_removed_fields(account_env):
    account = credential(models=['GLM-5.3'])
    scope = 'oauth:' + om.get_account_key(account)
    config.update(lambda c: c.update(oauthAccounts=[account], modelMetadataOverrides={
        'defaults': {'GLM-5.3': {'fields': {'maxOutputTokens': 1234}}},
        'scoped': {scope: {'GLM-5.3': {'outboundModel': 'GLM-5.3', 'fields': {'cost.input': 1}},
                            'glm-5.3': {'outboundModel': 'glm-5.3', 'fields': {'cost.output': 2}}}},
    }))
    assert model_metadata.patch_override_fields('gLm-5.3', scope_key=scope, outbound_model='glm-5.3',
        set_fields={'cost.output': 3}, unset_fields=['cost.input'])
    assert model_metadata.get_override_fields('GLM-5.3', scope_key=scope, outbound_model='GLM-5.3') == (
        {'maxOutputTokens': 1234}, {'cost.output': 3})
    assert set(config.get()['modelMetadataOverrides']['scoped'][scope]) == {'glm-5.3'}
    assert model_metadata.delete_override_layer('GLM-5.3', scope_key=scope)
    assert not model_metadata.delete_override_layer('glm-5.3')
    assert model_metadata.get_override_fields('GLM-5.3')[0] == {'maxOutputTokens': 1234}
    assert model_metadata.delete_override_layer('GLM-5.3')
    assert model_metadata.get_override_fields('GLM-5.3', scope_key=scope) == ({}, {})


@pytest.mark.parametrize('model', MODELS[:2])
@pytest.mark.parametrize('ingress', ['anthropic', 'chat', 'responses'])
@pytest.mark.parametrize('uppercase', [True, False])
async def test_quota_failover_across_case_variants(env, monkeypatch, model, ingress, uppercase):
    first_model, second_model = (model, model.lower()) if uppercase else (model.lower(), model)
    first = credential(model_key='first.secret', models=[first_model])
    second = credential(model_key='second.secret', models=[second_model])
    config.update(lambda c: c.update(oauthAccounts=[first, second], channelSelection='order', modelCenter={}))
    channels = [ZhipuOAuthChannel(first), ZhipuOAuthChannel(second)]
    fake._install_channels(env[0], channels)
    monkeypatch.setattr(env[0]['failover'], '_notify_zhipu_quota_cooldown', lambda *a, **k: None)
    attempts = []
    reset = (datetime.now(timezone(timedelta(hours=8))) + timedelta(days=2)).strftime('%Y-%m-%d %H:%M:%S')
    def wire(request):
        token = request.headers['x-api-key']
        attempts.append(token)
        assert json.loads(request.content)['model'] == (first_model if token == 'first.secret' else second_model)
        if token == 'first.secret':
            return httpx.Response(429, json={'type': 'error', 'error': {'type': 'rate_limit_error',
                'code': '1310', 'message': f'限额将在 {reset} 重置'}})
        return fake._anthropic_response('old account served')
    value = body(ingress)
    value['model'] = first_model
    response, text, _ = await call(env, ingress, value, wire)
    assert response.status_code == 200 and 'old account served' in text
    assert attempts == ['first.secret', 'second.secret']
    for spelling in (model, model.lower()):
        assert cooldown.is_blocked(channels[0].key, spelling)
        assert not cooldown.is_blocked(channels[1].key, spelling)
    attempts.clear()
    value['model'] = second_model
    response, text, _ = await call(env, ingress, value, wire)
    assert response.status_code == 200
    assert attempts == ['second.secret']


def test_legacy_cooldown_survives_reload_and_case_change(account_env):
    account = credential(models=MODELS)
    scope = 'oauth:' + om.get_account_key(account)
    until = int(time.time() * 1000) + 600000
    cooldown.clear(scope, notify_recovered=False)
    state_db.error_save(scope, 'GLM-5.3-Flash', 3, until, 'legacy quota')
    cooldown._initialized = False
    cooldown.init()
    assert cooldown.is_blocked(scope, 'glm-5.3-flash')
    assert not cooldown.clear_on_success(scope, 'glm-5.3-flash')
    cooldown.record_error(scope, 'glm-5.3-flash', 'still blocked')
    assert cooldown.get_state(scope, 'GLM-5.3-Flash')['cooldown_until'] == until
    assert len([r for r in cooldown.snapshot() if r['channel_key'] == scope]) == 1
    cooldown.clear(scope, 'glm-5.3-flash', notify_recovered=False)
    assert not cooldown.is_blocked(scope, 'GLM-5.3-Flash')
    assert not [r for r in state_db.error_load_all() if r['channel_key'] == scope]


def test_model_priority_uses_same_identity_and_keeps_legacy_config(account_env):
    first = credential(models=['GLM-5.3-Flash'])
    second = credential(model_key='second.secret', models=['glm-5.3-flash'])
    channels = [ZhipuOAuthChannel(first), ZhipuOAuthChannel(second)]
    order = [channel.key for channel in reversed(channels)]
    config.update(lambda c: c.update(oauthAccounts=[first, second], loadBalancing={
        'channelPriorityOrder': [channel.key for channel in channels],
        'modelPriorityOrders': {'GLM-5.3-Flash': order},
    }))
    for spelling in ('GLM-5.3-Flash', 'glm-5.3-flash'):
        assert load_balancing.has_model_priority(spelling) == (spelling == 'GLM-5.3-Flash')
        assert load_balancing.effective_order_for_model(spelling, channels) == order
    assert config.get()['loadBalancing']['modelPriorityOrders'] == {'GLM-5.3-Flash': order}
    load_balancing.save_model_order('gLm-5.3-fLaSh', list(reversed(order)))
    assert set(config.get()['loadBalancing']['modelPriorityOrders']) == {'GLM-5.3-Flash', 'gLm-5.3-fLaSh'}
    assert load_balancing.clear_model_orders(['GLM-5.3-Flash']) == 1
    assert set(config.get()['loadBalancing']['modelPriorityOrders']) == {'gLm-5.3-fLaSh'}


def test_binding_lookup_and_list_share_legacy_identity(account_env, monkeypatch):
    account = credential(models=['GLM-5.3-Flash'])
    scope = 'oauth:' + om.get_account_key(account)
    entry = {'target': 'zhipu/glm-5.3-flash', 'source': 'manual', 'outboundModel': 'GLM-5.3-Flash',
             'autoSnapshot': {'catalogRevision': 'fixture', 'metadata': {'contextWindow': 800000}}}
    config.update(lambda c: c.update(oauthAccounts=[account], modelBindings={
        'defaults': {}, 'scoped': {scope: {'GLM-5.3-Flash': entry}},
    }, modelMetadataOverrides={}))
    for spelling in ('GLM-5.3-Flash', 'glm-5.3-flash'):
        binding = model_metadata.resolve_binding(spelling, scope_key=scope, outbound_model='glm-5.3-flash')
        assert binding.metadata['contextWindow'] == 800000
    listed = [b for b in model_metadata.list_bindings() if b.scope_key == scope]
    assert len(listed) == 1 and listed[0].client_visible_model == 'glm-5.3-flash'
    monkeypatch.setattr(model_metadata.model_pricing, 'catalog_model', lambda target: {})
    model_metadata.set_binding('glm-5.3-flash', 'zhipu/glm-5.3-flash', scope_key=scope, outbound_model='glm-5.3-flash')
    assert set(config.get()['modelBindings']['scoped'][scope]) == {'glm-5.3-flash'}
    assert model_metadata.delete_binding('GLM-5.3-Flash', scope_key=scope)
    assert not config.get()['modelBindings']['scoped']


@pytest.mark.parametrize('scoped_binding', [False, True])
def test_auto_metadata_sync_protects_manual_case_variant(account_env, monkeypatch, scoped_binding):
    account = credential(models=['glm-5.3-flash'])
    scope = 'oauth:' + om.get_account_key(account) if scoped_binding else None
    manual = {'target': 'zhipu/glm-5.3-flash', 'source': 'manual', 'outboundModel': 'GLM-5.3-Flash',
              'autoSnapshot': {'catalogRevision': 'manual', 'metadata': {'contextWindow': 123000}}}
    automatic = {**manual, 'source': 'auto', 'autoSnapshot': {'catalogRevision': 'auto', 'metadata': {'contextWindow': 999000}}}
    values = {'GLM-5.3-Flash': manual, 'glm-5.3-flash': automatic}
    bindings = {'defaults': {} if scope else values, 'scoped': {scope: values} if scope else {}}
    config.update(lambda c: c.update(oauthAccounts=[account], modelBindings=bindings, modelMetadataOverrides={}))
    before = copy.deepcopy(config.get()['modelBindings'])
    monkeypatch.setattr(model_metadata.model_pricing, 'canonical_official_model', lambda model: None)
    outcome = model_metadata.sync_auto_snapshots([('glm-5.3-flash', scope, 'glm-5.3-flash'),
                                                 ('GLM-5.3-Flash', scope, 'GLM-5.3-Flash')])
    assert outcome['scanned'] == (1 if scoped_binding else 2)
    assert outcome['protected'] == ['glm-5.3-flash' if scoped_binding else 'GLM-5.3-Flash']
    assert config.get()['modelBindings'] == before
    assert model_metadata.resolve_binding('glm-5.3-flash', scope_key=scope,
        outbound_model='glm-5.3-flash').metadata['contextWindow'] == (123000 if scoped_binding else 999000)


def test_duplicate_legacy_cooldowns_keep_strongest_and_clear_atomically(account_env, monkeypatch):
    scope = 'oauth:' + om.get_account_key(credential())
    until = int(time.time() * 1000) + 600000
    for model, deadline in [('GLM-5.3', until), ('glm-5.3', -1)]:
        state_db.error_save(scope, model, 3, deadline, 'legacy restriction')
    cooldown._initialized = False
    cooldown.init()
    assert cooldown.get_state(scope, 'gLm-5.3')['cooldown_until'] == -1
    before = cooldown.snapshot()
    original = state_db.error_delete_models
    def fail(*args):
        raise RuntimeError('persist failed')
    monkeypatch.setattr(state_db, 'error_delete_models', fail)
    with pytest.raises(RuntimeError, match='persist failed'):
        cooldown.clear(scope, 'gLm-5.3', notify_recovered=False)
    assert cooldown.snapshot() == before
    assert len([r for r in state_db.error_load_all() if r['channel_key'] == scope]) == 2
    monkeypatch.setattr(state_db, 'error_delete_models', original)
    cooldown.clear(scope, 'gLm-5.3', notify_recovered=False)
    assert cooldown.get_state(scope, 'glm-5.3') is None
    assert not [r for r in state_db.error_load_all() if r['channel_key'] == scope]


async def test_public_directory_keeps_legacy_grants_and_mapping_targets(env):
    import server
    from starlette.requests import Request
    account = credential(models=MODELS[:2])
    channel = ZhipuOAuthChannel(account)
    config.update(lambda c: c.update(oauthAccounts=[account], modelCenter={},
        modelMapping={'global': {'flash': 'GLM-5.3-Flash'}}))
    fake._install_channels(env[0], [channel])
    request = Request({'type': 'http', 'method': 'GET', 'path': '/v1/models', 'query_string': b'',
        'headers': [(b'authorization', b'Bearer ccp-test')], 'client': ('127.0.0.1', 1234)})
    for grants, expected in [([], ['flash', 'glm-5.3', 'glm-5.3-flash']),
                             (['GLM-5.3'], ['GLM-5.3']),
                             (['GLM-5.3', 'glm-5.3'], ['glm-5.3']),
                             (['GLM-5.3-Flash', 'flash'], ['GLM-5.3-Flash', 'flash'])]:
        config.update(lambda c: c['apiKeys']['k'].update(allowedModels=grants))
        assert [row['id'] for row in (await server.list_models(request))['data']] == expected
        assert config.get()['apiKeys']['k']['allowedModels'] == grants
    config.update(lambda c: c['apiKeys']['k'].update(channelBindingEnabled=True, allowedChannels=['api:denied']))
    assert (await server.list_models(request))['data'] == []


def test_other_providers_and_key_authorization_keep_exact_semantics(account_env):
    for provider in ('claude', 'openai', 'xai', 'cursor', 'workbuddy', 'antigravity', ''):
        assert model_names.canonical_id(provider, 'GLM-5.3') == 'GLM-5.3'
    channel = ApiChannel({'name': 'custom', 'models': [{'real': 'upper', 'alias': 'GLM-5.3'},
                                                    {'real': 'lower', 'alias': 'glm-5.3'}]})
    assert channel.supports_model('GLM-5.3') == 'upper'
    assert channel.supports_model('glm-5.3') == 'lower'
    config.update(lambda c: c.update(apiKeys={'exact': {'key': 'fixture', 'allowedModels': ['GLM-5.3']}}))
    _, allowed, error = auth.validate({'x-api-key': 'fixture'})
    assert error is None and allowed == ['GLM-5.3']
