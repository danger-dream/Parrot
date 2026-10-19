"""Image/video/MCP consumers honor full source keys without changing paid retry policy."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager

import httpx
import pytest
from starlette.requests import Request

from src import auth, config, image_catalog, model_state, oauth_manager
from src.channel import registry
from src.channel.xai_oauth_channel import XAIOAuthChannel
from src.channel.antigravity_oauth_channel import AntigravityOAuthChannel
from src.mcp import catalog
from src.openai import images_runtime as runtime, images_openai_compat as compat
from src.xai import imagine
from src.tests.test_images_unified import setup, post

REAL_SEND = runtime._send


@pytest.mark.asyncio
async def test_selected_api_image_rechecks_alias_after_awaited_transport_setup(setup, monkeypatch):
    from src.openai.channel.api_channel import OpenAIApiChannel
    cfg = setup[0]
    entry = {'name': 'image-api', 'protocol': 'openai-chat', 'baseUrl': 'https://fixture.invalid',
             'models': [{'alias': 'art', 'real': 'gpt-image-2', 'kind': 'image'}]}
    cfg['channels'] = [entry]
    source = next(s for s in image_catalog.sources() if s.model == 'art')
    channel = OpenAIApiChannel(entry)
    channel.state_key = source.state_key
    monkeypatch.setattr(registry, '_channels', {channel.key: channel})
    model_state.set_global_enabled_in_config(cfg, ['paint', 'art', 'gpt-image-2'], False)
    grant(cfg, source.key, 'paint', 'art', 'gpt-image-2')
    sent = []
    def transport(req):
        sent.append(json.loads(req.content))
        return httpx.Response(200, json={'data': []})
    revoke = False
    @asynccontextmanager
    async def client(**kwargs):
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as session:
            if revoke:
                model_state.set_global_enabled_in_config(cfg, ['paint'], False)
            yield session
    monkeypatch.setattr(runtime.network, 'async_client', client)
    parsed = compat._ParsedRequest(model='art', requested_model='paint', prompt='icon')
    assert (await REAL_SEND(source, parsed, action='generate', n=1, cfg={})).status_code == 200
    assert sent[0]['model'] == 'gpt-image-2'
    revoke = True
    with pytest.raises(ValueError, match='disabled'):
        await REAL_SEND(source, parsed, action='generate', n=1, cfg={})
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_selected_antigravity_image_rechecks_alias_after_headers(setup, monkeypatch):
    from src.antigravity import images as ag_images
    cfg = setup[0]
    account = next(a for a in cfg['oauthAccounts'] if a['provider'] == 'antigravity')
    channel = AntigravityOAuthChannel(account)
    source = next(s for s in image_catalog.sources() if s.provider == 'antigravity')
    monkeypatch.setattr(registry, '_channels', {channel.key: channel})
    model_state.set_global_enabled_in_config(cfg, ['paint', source.model], False)
    grant(cfg, source.key, 'paint', source.model)
    async def headers():
        model_state.set_global_enabled_in_config(cfg, ['paint'], False)
        return {}
    monkeypatch.setattr(channel, 'build_media_headers', headers)
    monkeypatch.setattr(ag_images.network, 'async_client', lambda **kw: pytest.fail('no transport after revoke'))
    parsed = compat._ParsedRequest(model=source.model, requested_model='paint', prompt='icon')
    with pytest.raises(ValueError, match='no longer enabled'):
        await REAL_SEND(source, parsed, action='generate', n=1, cfg={})


def grant(cfg, key, *models):
    model_state.set_source_enable_override_in_config(cfg, key, models, True)


def request(body):
    async def receive(): return {"type": "http.request", "body": json.dumps(body).encode()}
    return Request({"type": "http", "method": "POST", "path": "/v1/videos", "headers": []}, receive)


@pytest.mark.asyncio
async def test_image_entry_only_granted_same_label_account_and_rejection_does_not_retry_other(setup, monkeypatch):
    cfg, calls, _app = setup
    cfg['oauthAccounts'].append({**cfg['oauthAccounts'][0], 'chatgpt_account_id': 'other-workspace'})
    model = 'gpt-image-2'
    rows = [s for s in image_catalog.sources() if s.model == model]
    assert len(rows) == 2 and rows[0].key != rows[1].key
    model_state.set_global_enabled_in_config(cfg, [model], False)
    assert (await post(setup, {'model': 'paint', 'prompt': 'icon'})).status_code == 403
    grant(cfg, rows[0].key, model)
    assert (await post(setup, {'model': 'paint', 'prompt': 'icon'})).status_code == 200
    assert {call[0].key for call in calls} == {rows[0].key}
    assert model in image_catalog.available_models()
    cfg['oauthAccounts'].append({**cfg['oauthAccounts'][0], 'chatgpt_account_id': 'new-workspace'})
    assert [s.key for s in image_catalog.sources() if s.model == model and s.available] == [rows[0].key]
    rejected = []
    async def send(source, parsed, **kwargs):
        rejected.append(source.key)
        return httpx.Response(429, json={'error': {'message': 'fixture rejection'}})
    monkeypatch.setattr(runtime, '_send', send)
    assert (await post(setup, {'model': 'paint', 'prompt': 'icon'})).status_code == 429
    assert rejected == [rows[0].key]
    cfg['modelCenter']['hiddenModels'] = [model]
    assert model not in image_catalog.available_models()
    assert (await post(setup, {'model': 'paint', 'prompt': 'icon'})).status_code == 429
    model_state.set_global_enabled_in_config(cfg, [model], False)
    assert (await post(setup, {'model': 'paint', 'prompt': 'icon'})).status_code == 403


@pytest.mark.asyncio
async def test_image_alias_and_target_must_be_enabled_on_same_source_and_acl_still_applies(setup):
    cfg = setup[0]
    cfg['oauthAccounts'].append({**cfg['oauthAccounts'][0], 'chatgpt_account_id': 'other-workspace'})
    rows = [s for s in image_catalog.sources() if s.model == 'gpt-image-2']
    model_state.set_global_enabled_in_config(cfg, ['gpt-image-2', 'paint'], False)
    grant(cfg, rows[0].key, 'gpt-image-2')
    grant(cfg, rows[1].key, 'paint')
    assert (await post(setup, {'model': 'paint', 'prompt': 'icon'})).status_code == 403
    grant(cfg, rows[0].key, 'paint')
    cfg['apiKeys'] = {'test': {'enabled': True, 'allowedModels': ['paint'], 'allowedChannels': [rows[0].key]}}
    assert catalog.media_sources('image', 'test') == ['paint']
    assert (await post(setup, {'model': 'paint', 'prompt': 'icon'})).status_code == 200
    cfg['apiKeys']['test']['allowedChannels'] = [rows[1].key]
    assert catalog.media_sources('image', 'test') == []
    assert (await post(setup, {'model': 'paint', 'prompt': 'icon'})).status_code == 503
    cfg['apiKeys']['test']['allowedChannels'] = [rows[0].key]
    cfg['modelCenter']['hiddenModels'] = ['paint']
    assert catalog.media_sources('image', 'test') == []
    assert (await post(setup, {'model': 'paint', 'prompt': 'icon'})).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize('revoked', ['paint', 'gpt-image-2'])
async def test_selected_openai_image_rechecks_after_token_refresh(setup, monkeypatch, revoked):
    cfg = setup[0]
    source = next(s for s in image_catalog.sources() if s.model == 'gpt-image-2')
    model_state.set_global_enabled_in_config(cfg, ['paint', source.model], False)
    grant(cfg, source.key, 'paint', source.model)
    assert image_catalog.openai_account_state(cfg['oauthAccounts'][0], cfg)['effective_available']
    from src.openai.codex_constants import current_codex_protocol_profile
    profile = current_codex_protocol_profile()
    cfg['openaiOAuth'].update(codexCliVersion=profile.client_version, codexProtocolProfile=profile.profile_id)
    async def token(*args, **kwargs):
        model_state.set_global_enabled_in_config(cfg, [revoked], False)
        return 'fixture-token'
    monkeypatch.setattr(oauth_manager, 'ensure_valid_token', token)
    monkeypatch.setattr(runtime.network, 'async_client', lambda **kw: pytest.fail('revoked source must not create transport'))
    parsed = compat._ParsedRequest(model=source.model, requested_model='paint', prompt='icon')
    with pytest.raises(ValueError, match='disabled'):
        await REAL_SEND(source, parsed, action='generate', n=1, cfg={})


@pytest.mark.parametrize('provider,kind,model', [
    ('xai', 'image', 'grok-imagine-image'), ('xai', 'video', 'grok-imagine-video'),
    ('antigravity', 'image', 'gemini-3.1-flash-image'),
])
def test_direct_media_support_keeps_account_and_source_gates(setup, provider, kind, model):
    cfg = setup[0]
    original = next(a for a in cfg['oauthAccounts'] if a['provider'] == provider)
    original['subject'] = 'one'
    other = {**original, 'subject': 'two', 'generationId': 'separate'}
    cfg['oauthAccounts'].append(other)
    cls = XAIOAuthChannel if provider == 'xai' else AntigravityOAuthChannel
    one, two = cls(original), cls(other)
    model_state.set_global_enabled_in_config(cfg, [model], False)
    grant(cfg, one.key, model)
    assert one.supports_media_model(kind, model)
    assert not two.supports_media_model(kind, model)
    original['disabledModels'] = [model]
    assert not one.supports_media_model(kind, model)
    original['disabledModels'] = []
    original['enabled'] = False
    assert not one.supports_media_model(kind, model)


@pytest.mark.asyncio
async def test_video_entry_retry_discovery_and_live_alias_revocation(setup, monkeypatch):
    cfg = setup[0]
    account = next(a for a in cfg['oauthAccounts'] if a['provider'] == 'xai')
    account['subject'] = 'one'
    second = {**account, 'subject': 'two'}
    cfg['oauthAccounts'].append(second)
    one, two = XAIOAuthChannel(account), XAIOAuthChannel(second)
    monkeypatch.setattr(registry, '_channels', {one.key: one, two.key: two})
    monkeypatch.setattr(auth, 'videos_allowed', lambda key: True)
    model, alias = 'grok-imagine-video', 'movie'
    cfg['modelMapping']['global'][alias] = model
    model_state.set_global_enabled_in_config(cfg, [model, alias], False)
    grant(cfg, one.key, model, alias)
    assert imagine._eligible_channels('video', model) == [one]
    cfg['apiKeys'] = {'test': {'enabled': True, 'allowedModels': [alias]}}
    assert catalog.media_sources('video', 'test') == [alias]
    sent = []
    async def headers(): return {}
    monkeypatch.setattr(one, 'build_media_headers', headers)
    def transport(req):
        sent.append(json.loads(req.content))
        return httpx.Response(429, json={'error': {'message': 'fixture'}})
    monkeypatch.setattr(imagine.network, 'async_client', lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(transport)))
    monkeypatch.setattr(imagine, '_record_upstream_status', lambda *a: None)
    response = await imagine.handle_video_create(request({'model': alias, 'prompt': 'clip'}), action='generate')
    assert response.status_code == 429 and len(sent) == 1
    assert sent[0]['model'] == model
    cfg['modelCenter']['hiddenModels'] = [model]
    assert model not in catalog.video_sources()
    assert imagine._eligible_channels('video', model) == [one]
    async def revoke_headers():
        model_state.set_global_enabled_in_config(cfg, [alias], False)
        return {}
    monkeypatch.setattr(one, 'build_media_headers', revoke_headers)
    response = await imagine.handle_video_create(request({'model': alias, 'prompt': 'clip'}), action='generate')
    assert response.status_code == 502 and len(sent) == 1
    assert (await imagine.handle_video_create(request({'model': alias, 'prompt': 'clip'}), action='generate')).status_code == 403
