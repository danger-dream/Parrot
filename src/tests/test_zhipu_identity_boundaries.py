"""Global client names never inherit a provider rule from catalog membership."""
from __future__ import annotations

import copy

import pytest

from src import config, failover, load_balancing, model_metadata, model_state
from src.channel import registry
from src.channel.api_channel import ApiChannel
from src.channel.zhipu_oauth_channel import ZhipuOAuthChannel
from src.management_control.models import ModelCenterControl
from src.tests.test_zhipu_channel import env, fake  # noqa: F401
from src.tests.test_zhipu_provider import credential


@pytest.fixture(params=['GLM-5.3', 'GLM-5.3-Flash'])
def mixed(env, request):
    upper = request.param
    lower = upper.lower()
    account = credential(models=[upper])
    zh = ZhipuOAuthChannel(account)
    api_entry = {'name': 'case-sensitive', 'protocol': 'anthropic', 'enabled': True,
                 'baseUrl': 'https://case-sensitive.invalid', 'models': [
                     {'alias': upper, 'real': 'upper-real'}, {'alias': lower, 'real': 'lower-real'},
                 ]}
    api = ApiChannel(api_entry)
    config.update(lambda c: c.update(oauthAccounts=[account], channels=[api_entry], modelCenter={},
        modelMapping={}, modelBindings={'defaults': {}, 'scoped': {}},
        modelMetadataOverrides={'defaults': {
            upper: {'fields': {'contextWindow': 111000}}, lower: {'fields': {'contextWindow': 222000}},
        }}, loadBalancing={'channelPriorityOrder': [api.key, zh.key],
                          'modelPriorityOrders': {upper: [zh.key, api.key], lower: [api.key, zh.key]}}))
    fake._install_channels(env[0], [api, zh])
    return env, account, api, zh, upper, lower


def test_account_membership_cannot_change_global_reads_or_api_metadata(mixed):
    _, account, api, zh, upper, lower = mixed
    config.update(lambda c: c.update(modelCenter={'disabledModels': [upper], 'hiddenModels': [upper]}))
    for accounts in ([], [account], []):
        config.update(lambda c: c.update(oauthAccounts=accounts))
        before = copy.deepcopy(config.get())
        assert not model_state.is_global_enabled(upper)
        assert model_state.is_global_enabled(lower)
        assert not model_state.is_discovery_visible(upper)
        assert model_state.is_discovery_visible(lower)
        assert model_state.state_snapshot()['disabledModels'] == [upper]
        for model, context in ((upper, 111000), (lower, 222000)):
            common = model_metadata.get_override_fields(model)[0]
            api_common = model_metadata.get_override_fields(model, scope_key=api.key)[0]
            assert common == api_common == {'contextWindow': context}
            assert model_metadata.resolve_binding(model).metadata['contextWindow'] == context
            assert model_metadata.resolve_binding(model, scope_key=api.key,
                outbound_model=api.supports_model(model)).metadata['contextWindow'] == context
        assert load_balancing.model_priority_orders() == {upper: [zh.key, api.key], lower: [api.key, zh.key]}
        assert config.get() == before


def test_global_writes_keep_other_case_settings_independent(mixed, monkeypatch):
    _, _, _, _, upper, lower = mixed
    config.update(lambda c: c.update(modelCenter={'disabledModels': [upper, lower], 'hiddenModels': [upper, lower]}))
    config.update(lambda c: model_state.set_global_enabled_in_config(c, [lower], True))
    config.update(lambda c: model_state.set_visible_in_config(c, [lower], True))
    assert config.get()['modelCenter']['disabledModels'] == [upper]
    assert config.get()['modelCenter']['hiddenModels'] == [upper]
    assert model_metadata.patch_override_fields(lower, scope_key=None, outbound_model=None,
        set_fields={'contextWindow': 333000})
    assert model_metadata.get_override_fields(upper)[0] == {'contextWindow': 111000}
    assert model_metadata.get_override_fields(lower)[0] == {'contextWindow': 333000}
    assert model_metadata.delete_override_layer(lower)
    assert model_metadata.get_override_fields(upper)[0] == {'contextWindow': 111000}
    assert model_metadata.get_override_fields(lower)[0] == {}
    monkeypatch.setattr(model_metadata.model_pricing, 'catalog_model', lambda target: {})
    model_metadata.set_binding(upper, 'fixture/upper')
    model_metadata.set_binding(lower, 'fixture/lower')
    assert set(config.get()['modelBindings']['defaults']) == {upper, lower}
    assert model_metadata.delete_binding(lower)
    assert set(config.get()['modelBindings']['defaults']) == {upper}
    load_balancing.save_model_order(lower, ['api:other'])
    assert load_balancing.model_priority_orders()[upper] != ['api:other']
    assert load_balancing.clear_model_orders([lower]) == 1
    assert set(load_balancing.model_priority_orders()) == {upper}


def test_provider_compatibility_applies_only_to_selected_candidate(mixed):
    env, _, api, zh, upper, lower = mixed
    config.update(lambda c: c.update(modelCenter={'disabledModels': [upper]}))
    value = {'model': lower, 'messages': [{'role': 'user', 'content': 'test'}]}
    route = env[0]['scheduler'].schedule(value, api_key_name='k', client_ip='127.0.0.1', ingress_protocol='anthropic')
    assert [(ch.key, model) for ch, model in route.candidates] == [(api.key, 'lower-real')]
    assert model_state.is_global_enabled(lower, source_key=api.key)
    assert not model_state.is_global_enabled(lower, source_key=zh.key)
    failover._validate_candidate_model_enabled(api, lower)
    with pytest.raises(failover.GuardError):
        failover._validate_candidate_model_enabled(zh, lower)
    row = next(r for r in ModelCenterControl().list_models().items if r.model_id == lower)
    assert row.global_enabled
    assert row.common_metadata['contextWindow'] == 222000
    sources = {source.provider: source for source in row.sources}
    assert not sources['zhipu'].effective_routable
    api_source = next(source for source in row.sources if source.id == api.key)
    assert api_source.effective_routable
    assert api_source.effective_metadata['contextWindow'] == row.common_metadata['contextWindow']
    assert lower in registry.available_models()
    fake._install_channels(env[0], [zh])
    assert lower not in registry.available_models()
    config.update(lambda c: model_state.set_global_enabled_in_config(c, [upper], True))
    route = env[0]['scheduler'].schedule(value, api_key_name='k', client_ip='127.0.0.1', ingress_protocol='anthropic')
    assert [(ch.key, model) for ch, model in route.candidates] == [(zh.key, lower)]


async def test_hidden_case_does_not_hide_unrelated_api_model(mixed):
    import server
    from starlette.requests import Request
    env, _, api, zh, upper, lower = mixed
    config.update(lambda c: c.update(modelCenter={'hiddenModels': [upper]}))
    request = Request({'type': 'http', 'method': 'GET', 'path': '/v1/models', 'query_string': b'',
        'headers': [(b'authorization', b'Bearer ccp-test')], 'client': ('127.0.0.1', 1234)})
    assert [row['id'] for row in (await server.list_models(request))['data']] == [lower]
    fake._install_channels(env[0], [zh])
    assert (await server.list_models(request))['data'] == []
    assert zh.supports_model(lower) == lower
    assert model_state.is_global_enabled(lower, source_key=zh.key)
    fake._install_channels(env[0], [api])
    assert [row['id'] for row in (await server.list_models(request))['data']] == [lower]


def test_priority_fallback_cannot_reorder_mixed_or_api_candidates(mixed):
    _, _, api, zh, upper, lower = mixed
    config.update(lambda c: c['loadBalancing'].update(modelPriorityOrders={upper: [zh.key, api.key]}))
    assert not load_balancing.has_model_priority(lower)
    assert load_balancing.effective_order_for_model(lower, [api, zh]) == [api.key, zh.key]
    assert load_balancing.effective_order_for_model(upper, [api, zh]) == [zh.key, api.key]
    assert load_balancing.effective_order_for_model(lower, [api]) == [api.key]
    assert load_balancing.effective_order_for_model(lower, [zh]) == [zh.key]


def test_global_auto_bindings_do_not_merge_case_names(mixed, monkeypatch):
    _, _, _, _, upper, lower = mixed
    monkeypatch.setattr(model_metadata.model_pricing, 'canonical_official_model', lambda model: 'fixture/' + model)
    monkeypatch.setattr(model_metadata.model_pricing, 'binding_snapshot', lambda target: {
        'catalogRevision': 'fixture', 'catalogSource': 'fixture', 'metadata': {'contextWindow': 400000},
    })
    config.update(lambda c: c.update(modelBindings={'defaults': {
        upper: {'target': 'fixture/manual', 'source': 'manual'},
    }, 'scoped': {}}))
    result = model_metadata.sync_auto_snapshots([(upper, None, None), (lower, None, None)])
    assert result['scanned'] == 2 and result['protected'] == [upper] and result['created'] == [lower]
    assert config.get()['modelBindings']['defaults'][upper] == {'target': 'fixture/manual', 'source': 'manual'}
    assert config.get()['modelBindings']['defaults'][lower]['source'] == 'auto'
    assert {binding.client_visible_model for binding in model_metadata.list_bindings()} >= {upper, lower}
