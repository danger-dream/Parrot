"""Model IDs come from /models, never from the ZCode recommendation template."""
from __future__ import annotations

import copy
from urllib.parse import parse_qs, urlsplit

import pytest

from src import config, model_state, oauth_manager as om
from src.channel.zhipu_oauth_channel import ZhipuOAuthChannel
from src.oauth.zhipu import catalog, common
from src.tests.test_zhipu_provider import account_env, credential  # noqa: F401


def install(monkeypatch, rows, *, rules=None, metadata_error=False):
    calls = []
    def request(url, **kwargs):
        calls.append((url, kwargs))
        if '/api/anthropic/v1/models' in url:
            assert kwargs['headers']['Authorization'] == 'Bearer fixture.secret'
            assert kwargs['headers']['x-api-key'] == 'fixture.secret'
            assert kwargs['envelope'] is False
            return rows(url) if callable(rows) else {'data': rows, 'hasMore': False}
        assert not kwargs.get('headers')
        if metadata_error:
            raise common.ZhipuError('metadata', 'network')
        if 'client/configs' in url:
            return {'configs': {'builtin_provider_config_json': 'https://cdn.example.test/config.json'}}
        assert url == 'https://cdn.example.test/config.json'
        return {'config': {'providerConfigRules': {'templateRules': [{
            'templateId': 'bigmodel-api', 'config': {'builtinModelIds': ['wrong-static-model']},
        }]}, 'modelConfigRules': rules or {}}}
    monkeypatch.setattr(common, 'request', request)
    return calls


@pytest.mark.parametrize('site', ['bigmodel', 'zai'])
def test_endpoint_membership_existing_case_and_no_metadata_credential_leak(monkeypatch, site):
    account = credential(site=site, models=['GLM-5.3'], disabledModels=['GLM-5.3'])
    before = copy.deepcopy(account)
    calls = install(monkeypatch, [{'id': 'glm-5.3', 'display_name': 'GLM-5.3'}, {'id': 'new-upstream-model'}],
        rules={'modelRules': [{'modelMatch': '^new-upstream-model$', 'config': {'properties': {'contextWindow': 123456}}}]})
    records = catalog.fetch_models(account, account_key='test-account')
    assert [row['id'] for row in records] == ['GLM-5.3', 'new-upstream-model']
    assert records[1]['contextWindow'] == 123456
    assert calls[0][0] == common.MODEL_ORIGINS[site] + '/api/anthropic/v1/models'
    assert all(kw['account_key'] == 'test-account' for _, kw in calls)
    assert account == before


def test_pagination_and_dedup(monkeypatch):
    def pages(url):
        query = parse_qs(urlsplit(url).query)
        if not query:
            return {'data': [{'id': 'first'}], 'hasMore': True, 'lastId': 'first'}
        assert query == {'after_id': ['first']}
        return {'data': [{'id': 'first'}, {'id': 'second'}], 'has_more': False}
    calls = install(monkeypatch, pages)
    records = catalog.fetch_models(credential())
    assert [row['id'] for row in records] == ['first', 'second']
    assert len([url for url, _ in calls if '/models' in url]) == 2


@pytest.mark.parametrize('data', [
    {'data': []}, {'data': [{'name': 'no-id'}]}, {'error': {'message': 'secret'}},
    {'data': [{'id': 'one'}], 'hasMore': True},
    {'data': [{'id': 'one'}], 'hasMore': 'true'},
    {'data': [{'id': 'one'}], 'hasMore': True, 'lastId': 'one'},
])
def test_invalid_or_incomplete_lists_do_not_fall_back_to_defaults(monkeypatch, data):
    calls = install(monkeypatch, lambda url: data)
    with pytest.raises(common.ZhipuError):
        catalog.fetch_models(credential())
    assert all('/api/anthropic/v1/models' in url for url, _ in calls)


def test_optional_metadata_failure_preserves_only_matching_records(monkeypatch):
    account = credential(models=['GLM-5.3', 'retired'], account_model_catalog={'models': [
        {'id': 'GLM-5.3', 'contextWindow': 1000000, 'reasoningEfforts': ['low', 'high', 'max']},
        {'id': 'retired', 'contextWindow': 123},
    ]})
    install(monkeypatch, [{'id': 'glm-5.3'}, {'id': 'new'}], metadata_error=True)
    records = catalog.fetch_models(account)
    assert records == [
        {'id': 'GLM-5.3', 'name': 'GLM-5.3', 'contextWindow': 1000000, 'reasoningEfforts': ['low', 'high', 'max']},
        {'id': 'new', 'name': 'new'},
    ]


@pytest.mark.asyncio
async def test_refresh_persists_live_membership_and_preserves_preferences(account_env, monkeypatch):
    monkeypatch.setattr(om, 'mock_mode_enabled', lambda: False)
    account = credential(models=['GLM-5.3', 'GLM-5.3-Flash'], disabledModels=['GLM-5.3-Flash'])
    om.add_account(account)
    key = om.get_account_key(account)
    config.update(lambda c: c.update(modelCenter={'disabledModels': ['glm-4.7']},
        modelMetadataOverrides={'defaults': {'GLM-5.3': {'maxOutputTokens': 12345}}}))
    before = copy.deepcopy(config.get())
    rows = [{'id': model} for model in ('glm-5.3', 'glm-5.3-flash', 'glm-4.7', 'glm-5.2')]
    install(monkeypatch, rows)
    outcome = await om.refresh_account_models(key)
    assert outcome['action'] == 'updated'
    saved = om.get_account(key)
    assert saved['models'] == ['GLM-5.3', 'GLM-5.3-Flash', 'glm-4.7', 'glm-5.2']
    assert saved['last_model_sync_source'] == 'upstream:zhipu'
    assert saved['disabledModels'] == ['GLM-5.3-Flash']
    assert not model_state.is_global_enabled('glm-4.7')
    for field in ('modelCenter', 'modelMetadataOverrides'):
        assert config.get()[field] == before[field]
    channel = ZhipuOAuthChannel(saved)
    assert channel.supports_model('GLM-5.3') and channel.supports_model('glm-5.2')
    assert not channel.supports_model('GLM-5.3-Flash')
    # The next refresh tracks upstream changes instead of shrinking to defaults
    # or retaining entries absent from the live endpoint.
    rows[:] = [{'id': 'glm-5.3'}, {'id': 'glm-5.2'}, {'id': 'new-server-model'}]
    assert (await om.refresh_account_models(key))['action'] == 'updated'
    assert om.get_account(key)['models'] == ['GLM-5.3', 'glm-5.2', 'new-server-model']
    assert om.get_account(key)['disabledModels'] == ['GLM-5.3-Flash']
    previous = copy.deepcopy(om.get_account(key))
    def offline(url, **kw):
        raise common.ZhipuError('catalog', 'network')
    monkeypatch.setattr(common, 'request', offline)
    assert (await om.refresh_account_models(key))['action'] == 'error'
    for field in ('models', 'account_model_catalog', 'disabledModels'):
        assert om.get_account(key)[field] == previous[field]
