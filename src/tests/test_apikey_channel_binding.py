"""Source isolation when several accounts expose the same public model."""
from types import SimpleNamespace
import asyncio
import json

import pytest
from src import auth, config, image_catalog, scheduler
from src.channel import registry
from src.management_api.schemas.apikey import ApiKeyUpdateRequest
from src.management_control import ManagementError
from src.management_auth import Capability
from .test_management_apikey_control import make_control, context


class Channel(SimpleNamespace):
    enabled = True
    disabled_reason = None
    protocol = 'openai-chat'
    type = 'api'
    provider = ''
    def supports_model(self, model):
        return model if model in self.models else None
    def list_client_models(self):
        return self.models


@pytest.fixture
def pool(monkeypatch):
    channels = [Channel(key='oauth:claude:a', models=['shared', 'claude-only']),
                Channel(key='oauth:cursor:b', models=['shared', 'cursor-only']),
                Channel(key='oauth:openai:c', models=['shared', 'gpt-only'])]
    cfg = {'apiKeys': {'claude': {'key': 'a', 'allowedChannels': [channels[0].key]},
                       'cursor': {'key': 'b', 'allowedChannels': [channels[1].key]},
                       'shared': {'key': 'c'},
                       'multi': {'key': 'd', 'allowedChannels': [channels[0].key, channels[1].key]}},
           'channelSelection': 'order'}
    monkeypatch.setattr(config, 'get', lambda: cfg)
    monkeypatch.setattr(registry, '_channels', {ch.key: ch for ch in channels})
    monkeypatch.setattr(scheduler.cooldown, 'is_blocked', lambda *_: False)
    monkeypatch.setattr(scheduler.concurrency, 'is_saturated', lambda *_: False)
    monkeypatch.setattr(scheduler.affinity, 'get', lambda *_: None)
    monkeypatch.setattr(scheduler.affinity, 'client_get', lambda *_: None)
    monkeypatch.setattr(scheduler, 'capabilities_for_channel', lambda *_: None)
    class Matrix:
        def plan(self, ingress, upstream, **kwargs):
            return scheduler.RoutePlan(ingress_protocol=ingress, upstream_protocol=upstream)
    monkeypatch.setattr(scheduler, 'DEFAULT_MATRIX', Matrix())
    return cfg, channels


def route(key, protocol='chat', **body):
    return scheduler.schedule({'model': 'shared', **body}, key, '127.0.0.1',
                              ingress_protocol=protocol, fp_query='session')


@pytest.mark.parametrize('protocol', ['anthropic', 'chat', 'responses'])
def test_same_model_routes_to_the_selected_account(pool, protocol):
    assert [ch.key for ch, _ in route('claude', protocol).candidates] == ['oauth:claude:a']
    assert [ch.key for ch, _ in route('cursor', protocol).candidates] == ['oauth:cursor:b']
    assert len(route('shared', protocol).candidates) == 3
    assert len(route('multi', protocol).candidates) == 2


def test_discovery_preserves_duplicates_between_channels(pool):
    assert registry.available_models(api_key_name='claude') == ['claude-only', 'shared']
    assert registry.available_models(api_key_name='cursor') == ['cursor-only', 'shared']
    assert registry.available_models() == ['claude-only', 'cursor-only', 'gpt-only', 'shared']


@pytest.mark.parametrize('unavailable', ['disabled', 'cooling', 'removed'])
def test_unavailable_binding_does_not_escape_to_another_account(pool, monkeypatch, unavailable):
    cfg, channels = pool
    if unavailable == 'disabled': channels[0].enabled = False
    elif unavailable == 'cooling':
        monkeypatch.setattr(scheduler.cooldown, 'is_blocked', lambda key, _: key == channels[0].key)
        monkeypatch.setattr(scheduler.cooldown, 'get_state', lambda *_: {'cooldown_until': -1})
    else: registry._channels.pop(channels[0].key)
    result = route('claude')
    assert not result
    assert not result.candidates and not result.saturated


def test_queue_and_affinity_cannot_reintroduce_other_accounts(pool, monkeypatch):
    monkeypatch.setattr(scheduler.concurrency, 'is_saturated', lambda *_: True)
    monkeypatch.setattr(scheduler.affinity, 'get', lambda *_: {'channel_key': 'oauth:cursor:b', 'model': 'shared'})
    result = route('claude')
    assert not result.candidates
    assert [ch.key for ch, _ in result.saturated] == ['oauth:claude:a']


@pytest.mark.parametrize('value', ['oauth:claude:a', None, {}, [''], [42]])
def test_malformed_bindings_fail_closed(pool, value):
    cfg, _ = pool
    cfg['apiKeys']['claude']['allowedChannels'] = value
    assert auth.allowed_channels('claude') == frozenset()
    assert not route('claude')
    assert registry.available_models(api_key_name='claude') == []


def test_empty_binding_is_legacy_pool_and_reconfiguration_takes_effect(pool):
    cfg, channels = pool
    cfg['apiKeys']['claude']['allowedChannels'] = []
    assert len(route('claude').candidates) == 3
    cfg['apiKeys']['claude']['allowedChannels'] = [channels[1].key]
    assert [ch.key for ch, _ in route('claude').candidates] == [channels[1].key]


def test_models_http_intersects_source_binding_and_model_grants(pool, monkeypatch):
    import server
    from starlette.requests import Request
    cfg, channels = pool
    cfg['apiKeys']['claude']['allowedModels'] = ['shared', 'cursor-only', 'alias']
    monkeypatch.setattr(server.model_mapping, 'get_global_map', lambda: {'alias': 'shared', 'wrong': 'cursor-only'})
    request = Request({'type': 'http', 'headers': [(b'authorization', b'Bearer a')]})
    response = asyncio.run(server.list_models(request))
    assert [m['id'] for m in response['data']] == ['alias', 'shared']


def test_image_discovery_and_execution_use_the_same_scope(pool, monkeypatch):
    from src.openai import images_runtime
    rows = [image_catalog.ImageSource(model='image', upstream='image', provider='openai',
            key='oauth:openai:c', label='fixture', enabled=True, source_enabled=True)]
    monkeypatch.setattr(image_catalog, 'sources', lambda **_: rows)
    assert image_catalog.available_models(api_key_name='claude') == []
    assert image_catalog.available_models(api_key_name='shared') == ['image']
    response = asyncio.run(images_runtime.execute(SimpleNamespace(model='image'), request=None,
                            action='generate', key_name='claude', cfg={}))
    assert response.status_code == 503


def test_management_can_bind_and_clear_sources_but_rejects_typos(pool):
    control, store, _, _ = make_control()
    ctx = context(Capability.READ, Capability.WRITE)
    view = control.update_api_key(ctx, 'alpha', changes={'allowed_channels': ['oauth:cursor:b']})
    assert view.allowed_channels == ('oauth:cursor:b',)
    assert store.get()['apiKeys']['alpha']['allowedChannels'] == ['oauth:cursor:b']
    with pytest.raises(ManagementError):
        control.update_api_key(ctx, 'alpha', changes={'allowed_channels': ['oauth:unknown']})
    assert store.get()['apiKeys']['alpha']['allowedChannels'] == ['oauth:cursor:b']
    view = control.update_api_key(ctx, 'alpha', changes={'allowed_channels': []})
    assert view.allowed_channels == ()
    with pytest.raises(ValueError): ApiKeyUpdateRequest(allowedChannels=None)
    assert ApiKeyUpdateRequest(allowedChannels=[]).allowedChannels == []



def test_switch_preserves_selection_and_restores_shared_pool(pool):
    cfg, channels = pool
    entry = cfg['apiKeys']['claude']
    entry['channelBindingEnabled'] = False
    assert len(route('claude').candidates) == 3
    assert 'cursor-only' in registry.available_models(api_key_name='claude')
    assert entry['allowedChannels'] == [channels[0].key]
    entry['channelBindingEnabled'] = True
    assert [ch.key for ch, _ in route('claude').candidates] == [channels[0].key]
    entry['allowedChannels'] = []
    assert not route('claude')
    assert registry.available_models(api_key_name='claude') == []


def test_management_binding_switch_requires_selection(pool):
    control, store, _, _ = make_control()
    ctx = context(Capability.READ, Capability.WRITE)
    with pytest.raises(ManagementError):
        control.update_api_key(ctx, 'alpha', changes={'channel_binding_enabled': True})
    view = control.update_api_key(ctx, 'alpha', changes={
        'allowed_channels': ['oauth:cursor:b'], 'channel_binding_enabled': True})
    assert view.channel_binding_enabled
    view = control.update_api_key(ctx, 'alpha', changes={'channel_binding_enabled': False})
    assert not view.channel_binding_enabled
    assert view.allowed_channels == ('oauth:cursor:b',)
    assert store.get()['apiKeys']['alpha']['allowedChannels'] == ['oauth:cursor:b']
    with pytest.raises(ManagementError):
        control.update_api_key(ctx, 'alpha', changes={'channel_binding_enabled': 'false'})


def test_management_http_switch_roundtrip(pool, tmp_path):
    from fastapi.testclient import TestClient
    from .test_management_apikey_api import build_app, session_headers
    app, runtime, _, _, _ = build_app(tmp_path)
    headers = session_headers(runtime)
    with TestClient(app) as client:
        path = '/api/management/v1/api-keys/alpha'
        revision = client.get(path, headers=headers).json()['data']['revision']
        bound = client.patch(path, headers={**headers, 'If-Match': revision}, json={
            'allowedChannels': ['oauth:cursor:b'], 'channelBindingEnabled': True})
        assert bound.status_code == 200, bound.text
        assert bound.json()['data']['channelBindingEnabled'] is True
        unbound = client.patch(path, headers={**headers, 'If-Match': bound.json()['data']['revision']},
                              json={'channelBindingEnabled': False})
        assert unbound.status_code == 200, unbound.text
        assert unbound.json()['data']['channelBindingEnabled'] is False
        assert unbound.json()['data']['allowedChannels'] == ['oauth:cursor:b']
        assert client.patch(path, headers=headers, json={'channelBindingEnabled': None}).status_code == 422
