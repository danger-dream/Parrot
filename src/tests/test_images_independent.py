"""Explicit image-only OAuth authorization and selected-generation safety."""
import copy
import json
import uuid
from contextlib import asynccontextmanager
from importlib import reload
from urllib.parse import quote

import httpx
import pytest
from starlette.requests import Request

from src import config, image_catalog, oauth_manager
from src.channel import registry
from src.channel.openai_oauth_channel import OpenAIOAuthChannel
from src.management_control import ManagementError, ManagementErrorCode
from src.management_control.auxiliary.media import ImageControl
from src.management_control.models import ModelCenterControl, ModelFilters, ModelKind, ModelSourceRef, ModelSourceType
from src.openai import images_runtime as runtime, images_openai_compat as compat, images_simple
from src.tests.test_images_unified import setup, post, b64
from src.tests.test_model_center_media import admin_context
from src.tests.test_management_mapping_support import domain_client


@pytest.fixture
def image_env(setup, monkeypatch):
    cfg = setup[0]
    cfg['image_models'] = {'openai': ['gpt-image-2'], 'xai': ['grok-imagine-image', 'grok-imagine-image-quality']}
    from src.openai.codex_constants import current_codex_protocol_profile
    profile = current_codex_protocol_profile()
    cfg['openaiOAuth'].update(codexCliVersion=profile.client_version, codexProtocolProfile=profile.profile_id)
    account = cfg['oauthAccounts'][0]
    account.update(enabled=False, disabled_reason='user', generationId=uuid.uuid4().hex,
                   expired='2099-01-01T00:00:00Z', models=['chat-only'])
    # Writes target this in-memory fixture, not config.json or its listeners.
    def update(mutator):
        candidate = copy.deepcopy(cfg)
        mutator(candidate)
        cfg.clear(); cfg.update(candidate)
        return cfg
    monkeypatch.setattr(config, 'update', update)
    channel = OpenAIOAuthChannel(copy.deepcopy(account))
    monkeypatch.setattr(registry, '_channels', {channel.key: channel})
    return cfg, ImageControl(), oauth_manager.get_account_key(account)


def authorize(env, enabled=True):
    _cfg, control, key = env
    state = control.get_account(admin_context(), key)
    return control.update_account(admin_context(), key, independent_enabled=enabled, expected_revision=state.revision)


def image_view():
    return next(row for row in ModelCenterControl().list_models(filters=ModelFilters(kinds=(ModelKind.IMAGE,))).items
                if row.model_id == 'gpt-image-2')


@pytest.mark.asyncio
async def test_default_off_explicit_on_real_image_dispatch_chat_stays_off_then_close(image_env, setup, monkeypatch):
    import server
    cfg, control, key = image_env
    before = copy.deepcopy(cfg)
    request = Request({'type': 'http', 'method': 'GET', 'path': '/v1/models', 'headers': []})
    def listed(result): return {item['id'] for item in result['data']}
    assert not image_view().available_in()
    assert 'paint' not in listed(await server.list_models(request))
    assert (await post(setup, {'model': 'paint', 'prompt': 'icon'})).status_code == 503
    assert cfg == before  # Listing/failed calls must not install an override.
    current = control.get_account(admin_context(), key)
    assert not current.independent_enabled and current.independent_allowed
    assert '图片独立启用' in current.unavailable_reason
    active = authorize(image_env)
    assert active.independent_enabled and active.effective_available and not active.oauth_enabled
    assert image_view().available_in()
    assert 'paint' in listed(await server.list_models(request))
    assert cfg['oauthAccounts'] == before['oauthAccounts']  # no enabled/reason/token changes
    assert cfg['images']['independentAccounts'] == [oauth_manager.account_state_key(cfg['oauthAccounts'][0])]
    assert '@' not in cfg['images']['independentAccounts'][0]
    assert registry.available_models() == [] and not registry.enabled_channels()
    assert not OpenAIOAuthChannel(copy.deepcopy(cfg['oauthAccounts'][0])).enabled
    assert cfg['oauthAccounts'][1] == before['oauthAccounts'][1] and cfg['xaiOAuth'] == before['xaiOAuth']

    # Run the actual image adapter + actual cached-token lifecycle, mock only wire I/O.
    monkeypatch.setattr(runtime, '_send', reload(runtime)._send)
    seen = []
    def wire(req):
        seen.append(req)
        return httpx.Response(200, json={'data': [{'b64_json': b64()}]})
    monkeypatch.setattr(runtime.network, 'async_client', lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(wire)))
    response = await post(setup, {'model': 'paint', 'prompt': 'icon'})
    assert response.status_code == 200, response.text
    assert len(seen) == 1 and seen[0].headers['authorization'] == 'Bearer test'
    assert seen[0].headers['chatgpt-account-id'] == 'test-workspace'
    assert not registry.enabled_channels()
    closed = authorize(image_env, False)
    assert not closed.independent_enabled and not closed.effective_available
    assert not image_view().available_in()
    assert 'paint' not in listed(await server.list_models(request))
    assert (await post(setup, {'model': 'paint', 'prompt': 'icon'})).status_code == 503
    assert len(seen) == 1


@pytest.mark.parametrize('reason', ['auth_error', 'quota', '', 'unknown'])
def test_independent_cannot_override_non_user_disable(image_env, reason):
    cfg, control, key = image_env
    authorize(image_env)
    cfg['oauthAccounts'][0]['disabled_reason'] = reason
    state = control.get_account(admin_context(), key)
    assert state.independent_enabled and not state.effective_available and not state.independent_allowed
    assert '不能' in state.unavailable_reason
    assert 'gpt-image-2' not in image_catalog.available_models()
    with pytest.raises(ManagementError) as error:
        control.update_account(admin_context(), key, independent_enabled=True, expected_revision=state.revision)
    assert error.value.code is ManagementErrorCode.UNSUPPORTED_VALUE
    assert not authorize(image_env, False).independent_enabled  # revocation is always allowed


@pytest.mark.parametrize('gate', ['global', 'source', 'images', 'excluded', 'credentials'])
@pytest.mark.asyncio
async def test_other_image_gates_remain_authoritative(image_env, setup, gate):
    cfg, _control, key = image_env
    authorize(image_env)
    if gate == 'global': cfg['modelCenter'] = {'disabledModels': ['gpt-image-2']}
    if gate == 'source': cfg['oauthAccounts'][0]['disabledModels'] = ['gpt-image-2']
    if gate == 'images': cfg['images']['enabled'] = False
    if gate == 'excluded': cfg['images']['disabledAccounts'] = [key]
    if gate == 'credentials': cfg['oauthAccounts'][0]['access_token'] = ''
    managed = _control.get_account(admin_context(), key)
    assert not managed.effective_available and managed.unavailable_reason
    assert not image_view().available_in()
    assert 'gpt-image-2' not in image_catalog.available_models()
    assert (await post(setup, {'model': 'paint', 'prompt': 'icon'})).status_code >= 400
    assert not setup[1]


def test_binding_is_generation_not_email_order_and_recreated_account_needs_new_grant(image_env):
    cfg, control, key = image_env
    state = authorize(image_env)
    original = copy.deepcopy(cfg['oauthAccounts'][0])
    other = dict(original, chatgpt_account_id='different-workspace', generationId=uuid.uuid4().hex)
    cfg['oauthAccounts'].insert(0, other)
    states = {row.key[6:]: row.available for row in image_catalog.sources() if row.provider == 'openai'}
    assert states[key] and not states[oauth_manager.get_account_key(other)]
    cfg['oauthAccounts'] = [other]  # deleted; same email must not match
    with pytest.raises(ManagementError) as missing:
        control.get_account(admin_context(), key)
    assert missing.value.code is ManagementErrorCode.RESOURCE_NOT_FOUND
    replacement = dict(original, generationId=uuid.uuid4().hex, access_token='replacement-token')
    cfg['oauthAccounts'].append(replacement)
    fresh = control.get_account(admin_context(), key)
    assert not fresh.independent_enabled and not fresh.effective_available
    with pytest.raises(ManagementError) as stale:
        control.update_account(admin_context(), key, independent_enabled=True, expected_revision=state.revision)
    assert stale.value.code is ManagementErrorCode.REVISION_CONFLICT
    assert authorize(image_env).effective_available


def test_legacy_generation_persisted_only_on_explicit_authorization(image_env):
    cfg, control, key = image_env
    cfg['oauthAccounts'][0].pop('generationId')
    state = control.get_account(admin_context(), key)
    assert 'generationId' not in cfg['oauthAccounts'][0]
    authorize(image_env)
    assert cfg['oauthAccounts'][0]['generationId']
    saved = json.loads(json.dumps(cfg))
    assert image_catalog.openai_account_state(saved['oauthAccounts'][0], saved)['enabled']


@pytest.mark.asyncio
@pytest.mark.parametrize('timing', ['before-token', 'during-token', 'client-enter'])
async def test_selected_generation_rejected_before_token_or_dispatch(image_env, monkeypatch, timing):
    cfg, _control, _key = image_env
    authorize(image_env)
    real_send = reload(runtime)._send
    source = next(row for row in image_catalog.sources() if row.provider == 'openai')
    token_calls = []; sent = []
    def replace():
        cfg['oauthAccounts'][0] = dict(cfg['oauthAccounts'][0], generationId=uuid.uuid4().hex, access_token='new-identity-token')
    async def token(key, *, expected_state_key):
        token_calls.append(expected_state_key)
        assert expected_state_key == source.state_key
        if timing == 'during-token': replace()
        return 'old-identity-token'
    @asynccontextmanager
    async def client(**kwargs):
        if timing == 'client-enter': replace()
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: sent.append(req))) as value:
            yield value
    monkeypatch.setattr(oauth_manager, 'ensure_valid_token', token)
    monkeypatch.setattr(runtime.network, 'async_client', client)
    if timing == 'before-token': replace()
    parsed = compat._ParsedRequest(model='gpt-image-2', prompt='icon')
    with pytest.raises(ValueError, match='generation was deleted'):
        await real_send(source, parsed, action='generate', n=1, cfg={})
    assert not sent
    assert len(token_calls) == (0 if timing == 'before-token' else 1)


@pytest.mark.asyncio
async def test_revocation_during_token_prevents_dispatch(image_env, monkeypatch):
    _cfg, _control, _key = image_env
    authorize(image_env)
    real_send = reload(runtime)._send
    source = next(row for row in image_catalog.sources() if row.provider == 'openai')
    async def token(key, *, expected_state_key):
        authorize(image_env, False)
        return 'test'
    monkeypatch.setattr(oauth_manager, 'ensure_valid_token', token)
    with pytest.raises(ValueError, match='图片独立启用'):
        await real_send(source, compat._ParsedRequest(model='gpt-image-2', prompt='icon'), action='generate', n=1, cfg={})


@pytest.mark.asyncio
async def test_direct_images_keeps_selected_generation(image_env, monkeypatch):
    cfg, _control, key = image_env
    authorize(image_env)
    source = next(row for row in image_catalog.sources() if row.provider == 'openai')
    row = dict(account_key=key, account=cfg['oauthAccounts'][0], state_key=source.state_key, image_source=source)
    async def token(key, *, expected_state_key):
        assert expected_state_key == source.state_key
        cfg['oauthAccounts'][0] = dict(cfg['oauthAccounts'][0], generationId=uuid.uuid4().hex)
        return 'test'
    monkeypatch.setattr(oauth_manager, 'ensure_valid_token', token)
    with pytest.raises(ValueError, match='generation was deleted'):
        await reload(runtime)._send(source, compat._ParsedRequest(model='gpt-image-2', prompt='icon'), action='generate', n=1, cfg={})


def test_api_and_tg_share_explicit_authorization_and_return_context(domain_client, monkeypatch):
    from src.management_api.routers.media_settings import router
    from src.telegram import ui, states
    from src.telegram.menus import model_center_menu as menu
    client, runtime_owner, admin, read_only, _denied = domain_client
    client.app.include_router(router, prefix='/api/management/v1')
    config.update(lambda root: root.update(oauthAccounts=[dict(provider='openai', email='same@example.test',
        workspace_id='workspace-a', generationId=uuid.uuid4().hex, enabled=False, disabled_reason='user',
        access_token='test', expired='2099-01-01T00:00:00Z')]))
    channel = OpenAIOAuthChannel(copy.deepcopy(config.get()['oauthAccounts'][0]))
    monkeypatch.setattr(registry, '_channels', {channel.key: channel})
    key = oauth_manager.get_account_key(config.get()['oauthAccounts'][0])
    path = '/api/management/v1/images/accounts/' + quote(key, safe='')
    state = client.get(path, headers=admin).json()['data']
    assert not state['independentEnabled'] and not state['effectiveAvailable']
    def api_image_source():
        models = client.get('/api/management/v1/models?type=image', headers=admin)
        assert models.status_code == 200, models.text
        return next(row for row in models.json()['data'] if row['modelId'] == 'gpt-image-2')['sources'][0]
    assert not api_image_source()['effectiveRoutable']
    assert '图片独立启用' in api_image_source()['unavailableReason']
    assert client.patch(path, headers=read_only, json={'independentEnabled': True}).status_code == 403
    missing_revision = client.patch(path, headers=admin, json={'independentEnabled': True})
    assert missing_revision.status_code == 400 and missing_revision.json()['error']['code'] == 'CONFIRMATION_REQUIRED'
    assert client.patch(path, headers=admin, json={'enabled': True, 'independentEnabled': True}).status_code == 422
    enabled = client.patch(path, headers={**admin, 'If-Match': state['revision']}, json={'independentEnabled': True})
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()['data']['independentEnabled'] and not enabled.json()['data']['oauthEnabled']
    assert api_image_source()['effectiveRoutable'] and api_image_source()['unavailableReason'] is None
    owner = runtime_owner.control_owner()
    monkeypatch.setattr(menu, '_CONTROL', owner.models)
    monkeypatch.setattr(ui, 'is_admin', lambda chat_id: chat_id == 42)
    edits = []
    monkeypatch.setattr(ui, 'edit', lambda *args, **kwargs: edits.append((args, kwargs)))
    monkeypatch.setattr(ui, 'answer_cb', lambda *args, **kwargs: None)
    menu.reset_for_tests(); states.clear_all()
    session = menu._session(42)
    session.tab = 'image'
    session.source = ModelSourceRef(ModelSourceType.OAUTH, key)
    session.query = 'gpt-image'
    session.page = 2
    view = next(row for row in owner.models.list_models(menu._ctx(42), filters=ModelFilters(kinds=(ModelKind.IMAGE,))).items
                if row.model_id == 'gpt-image-2')
    back = menu._list_back_callback(42, menu._list_context(session))
    text, kb = menu._detail_render(42, view.resource_key, back_callback=back)
    assert '可用来源' in text and 'same@example.test' in text
    button = next(b for row in kb['inline_keyboard'] for b in row
                  if (menu._thaw(42, b.get('callback_data', '').removeprefix('mc:a:')) is not None and menu._thaw(42, b.get('callback_data', '').removeprefix('mc:a:')).name == 'media_source'))
    assert menu.handle_callback(42, 10, 'toggle-off', button['callback_data'])
    assert not client.get(path, headers=admin).json()['data']['independentEnabled']
    assert not api_image_source()['effectiveRoutable']
    assert session.tab == 'image' and session.source.id == key and session.query == 'gpt-image' and session.page == 2
    assert any(b['callback_data'] == back for row in edits[-1][1]['reply_markup']['inline_keyboard'] for b in row)
    text, kb = menu._detail_render(42, view.resource_key, back_callback=back)
    assert '已禁用' in str(kb)
    assert config.get()['oauthAccounts'][0]['enabled'] is False
    assert config.get()['oauthAccounts'][0]['disabled_reason'] == 'user'
    menu.reset_for_tests(); states.clear_all()


def test_tombstoned_generation_cannot_be_reauthorized(image_env):
    from src import channel_state
    cfg, control, key = image_env
    authorize(image_env)
    selected = oauth_manager.account_state_key(cfg['oauthAccounts'][0])
    channel_state.retire_deleted(selected)
    current = control.get_account(admin_context(), key)
    assert not current.effective_available and not current.independent_allowed
    assert '已删除' in current.unavailable_reason
    with pytest.raises(ManagementError) as error:
        control.update_account(admin_context(), key, independent_enabled=True, expected_revision=current.revision)
    assert error.value.code is ManagementErrorCode.UNSUPPORTED_VALUE
    assert not image_view().available_in()


@pytest.mark.asyncio
async def test_real_token_lifecycle_rejects_replacement_generation(image_env):
    cfg, _control, key = image_env
    authorize(image_env)
    selected = oauth_manager.account_state_key(cfg['oauthAccounts'][0])
    cfg['oauthAccounts'][0] = dict(cfg['oauthAccounts'][0], generationId=uuid.uuid4().hex,
                                 access_token='must-not-be-returned')
    with pytest.raises(ValueError, match='generation was deleted'):
        await oauth_manager.ensure_valid_token(key, expected_state_key=selected)
