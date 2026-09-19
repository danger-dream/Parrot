"""MEDIA-01..09: correct-behavior regressions, real local state, no paid I/O."""
from __future__ import annotations

import asyncio
import base64
import copy
import json
import os
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import httpx
import pytest

from src import channel_state, concurrency, config, cooldown, image_artifacts, image_catalog, image_db, media_cache, state_db
from src.channel import registry
from src.management_control import ManagementError, ManagementErrorCode
from src.management_control.auxiliary.media import ImageControl, VideoControl
from src.openai import images_runtime as runtime
from src.tests.test_images_unified import setup, post, png, b64
from src.tests.test_management_mapping_support import domain_client
from src.tests.test_model_center_media import FakeConfig, admin_context

_REAL_ACQUIRE = concurrency.try_acquire
_REAL_RELEASE = concurrency.release
_REAL_BLOCKED = cooldown.is_blocked
_REAL_START = runtime.imagine._start_media_log
_REAL_FINISH = runtime.imagine._finish_media_log
_REAL_SEND = runtime._send
CTX = admin_context()


def install_concurrency(setup, monkeypatch, kind='api'):
    cfg = setup[0]
    cfg.update(channelSelection='order', concurrency={'enabled': True, 'defaultMaxConcurrent': 1})
    if kind == 'api':
        cfg['oauthAccounts'] = []
        cfg['channels'] = [dict(name='shared', protocol='openai-chat', enabled=True, maxConcurrent=1,
            models=[{'real': 'gpt-image-1', 'kind': 'image'}])]
    source = next(s for s in image_catalog.sources() if s.provider == 'openai')
    channel = SimpleNamespace(key=source.key, state_key=source.state_key, max_concurrent=1)
    monkeypatch.setattr(registry, '_channels', {source.key: channel})
    monkeypatch.setattr(concurrency, '_slots', {})
    monkeypatch.setattr(concurrency, '_slots_lock', asyncio.Lock())
    monkeypatch.setattr(concurrency, 'try_acquire', _REAL_ACQUIRE)
    monkeypatch.setattr(concurrency, 'release', _REAL_RELEASE)
    return source, channel


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['api', 'oauth'])
async def test_image_shares_chat_concurrency_and_releases_exact_slot(setup, monkeypatch, kind):
    source, channel = install_concurrency(setup, monkeypatch, kind)
    key = channel_state.effect_key(channel)
    assert await concurrency.try_acquire(key)  # failover.py uses this identity
    rejected = await post(setup, {'model': source.model, 'prompt': 'icon'})
    assert rejected.status_code >= 400 and not setup[1]
    assert set(concurrency._slots) == {key}
    assert concurrency._slots[key].in_flight == 1
    concurrency.release(key)
    accepted = await post(setup, {'model': source.model, 'prompt': 'icon'})
    assert accepted.status_code == 200, accepted.text
    assert len(setup[1]) == 1 and concurrency._slots[key].in_flight == 0
    assert set(concurrency._slots) == {key}


@pytest.mark.asyncio
async def test_cancel_releases_selected_generation_not_replacement(setup, monkeypatch):
    source, channel = install_concurrency(setup, monkeypatch)
    reached = asyncio.Event()
    async def send(*args, **kwargs):
        reached.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(runtime, '_send', send)
    task = asyncio.create_task(post(setup, {'model': source.model, 'prompt': 'icon'}))
    await reached.wait()
    old_key = source.state_key
    new_key = channel_state.register_api_generation(channel.key, uuid.uuid4().hex)
    registry._channels[channel.key] = SimpleNamespace(key=channel.key, state_key=new_key, max_concurrent=1)
    assert await concurrency.try_acquire(new_key)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert concurrency._slots[old_key].in_flight == 0
    assert concurrency._slots[new_key].in_flight == 1
    concurrency.release(new_key)


@pytest.mark.asyncio
@pytest.mark.parametrize('n', [1, 2])
async def test_url_and_history_share_one_file_and_expiry_keeps_history(setup, monkeypatch, n):
    cfg = setup[0]['images']
    cfg.update(cacheEnabled=True, cacheMaxBytes=len(png()) * n + 20)
    logs = []
    async def finish(_id, **fields): logs.append(fields)
    monkeypatch.setattr(runtime.imagine, '_finish_media_log', finish)
    result = await post(setup, {'model': 'gpt-image-2', 'prompt': 'icon', 'response_format': 'url', 'n': n})
    assert result.status_code == 200, result.text
    assert result.json()['parrot']['complete'] and len(setup[1]) == n
    items = result.json()['data']
    files = list(Path(cfg['cachePath']).rglob('*.png'))
    assert len(files) == n and sum(p.stat().st_size for p in files) <= cfg['cacheMaxBytes']
    assert set(map(str, files)) == set(logs[-1]['cache_paths'])
    assert logs[-1]['cached_media_count'] == n
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=setup[2]), base_url='http://testserver') as client:
        for item in items:
            downloaded = await client.get(item['url'])
            assert downloaded.status_code == 200 and downloaded.content == png()
        monkeypatch.setattr(image_artifacts.time, 'time', lambda: max(item['expires_at'] for item in items) + 1)
        for item in items:
            assert (await client.get(item['url'])).status_code == 404
    assert all(p.read_bytes() == png() for p in files)
    assert not image_artifacts._ASSETS
    image_artifacts._reap_stale_temporary(Path(cfg['cachePath']), time.time() + 1)
    assert all(p.exists() for p in files)


@pytest.mark.asyncio
async def test_failed_optional_cache_still_delivers_temporary_url(setup, monkeypatch):
    setup[0]['images']['cacheEnabled'] = True
    def fail(*args, **kwargs): raise OSError('fixture failure')
    monkeypatch.setattr(media_cache, 'cache_inline_base64', fail)
    result = await post(setup, {'model': 'paint', 'prompt': 'icon', 'response_format': 'url'})
    assert result.status_code == 200, result.text
    item = result.json()['data'][0]
    entry = image_artifacts._ASSETS[image_artifacts.media_token(item['url'])]
    assert entry.temporary and Path(entry.path).is_file()
    assert len(setup[1]) == 1 and result.json()['parrot']['warnings']
    monkeypatch.setattr(image_artifacts.time, 'time', lambda: item['expires_at'] + 1)
    image_artifacts._prune()
    assert not Path(entry.path).exists()


@pytest.mark.parametrize('kind,provider,control_class', [('image', 'openai', ImageControl), ('video', 'xai', VideoControl)])
def test_default_validated_against_one_final_cas_candidate(monkeypatch, kind, provider, control_class):
    # The gateway's snapshot is intentionally different from module-global config.
    monkeypatch.setattr(config, 'get', lambda: {kind + '_models': {provider: ['global-only']}})
    store = FakeConfig({kind + '_models': {provider: ['old'], 'custom': ['keep']}})
    ctl = control_class(config_gateway=store)
    initial = ctl.get_settings(CTX)
    changed = ctl.update_settings(CTX, {'models': {provider: ['new']}, 'defaultModel': 'new'}, expected_revision=initial.revision)
    assert store.updates == 1 and changed.default_model == 'new'
    assert changed.models == {provider: ['new'], 'custom': ['keep']}
    before = copy.deepcopy(store.value)
    for patch, revision, code in [
        ({'defaultModel': 'global-only'}, changed.revision, ManagementErrorCode.VALIDATION_FAILED),
        ({'models': {provider: []}, 'defaultModel': 'new'}, changed.revision, ManagementErrorCode.VALIDATION_FAILED),
        ({'models': {provider: ['next']}, 'defaultModel': 'next'}, initial.revision, ManagementErrorCode.REVISION_CONFLICT),
    ]:
        with pytest.raises(ManagementError) as caught:
            ctl.update_settings(CTX, patch, expected_revision=revision)
        assert caught.value.code is code
        assert store.value == before and store.updates == 1
    cleared = ctl.update_settings(CTX, {'defaultModel': ''}, expected_revision=changed.revision)
    assert cleared.default_model == '' and store.updates == 2


@pytest.mark.parametrize('kind,model,control_class', [
    ('image', 'api-only-painter', ImageControl),
    ('image', 'account-image', ImageControl),
    ('video', 'account-video', VideoControl),
])
def test_source_only_default_names_are_accepted(setup, kind, model, control_class):
    root = copy.deepcopy(setup[0])
    root['channels'] = [dict(name='image-only', protocol='openai-chat', providerId='openai', enabled=True,
        models=[{'real': 'gpt-image-1', 'alias': 'api-only-painter', 'kind': 'image'}])]
    root['oauthAccounts'][0]['imageModels'] = ['account-image']
    root['oauthAccounts'][1]['videoModels'] = ['account-video']
    store = FakeConfig(root)
    ctl = control_class(config_gateway=store)
    assert model in image_catalog.models(store.get(), kind=kind)
    result = ctl.update_settings(CTX, {'defaultModel': model}, expected_revision=ctl.get_settings(CTX).revision)
    assert result.default_model == model and store.updates == 1
    assert store.value['channels'] == root['channels']
    assert store.value['oauthAccounts'] == root['oauthAccounts']


def test_management_default_model_http_roundtrip_cas_and_validation(domain_client):
    client, owner, admin, readonly, _ = domain_client
    from src.management_api.routers.media_settings import router
    client.app.include_router(router, prefix='/api/management/v1')
    for kind, provider in [('images', 'openai'), ('videos', 'xai')]:
        path = '/api/management/v1/' + kind + '/settings'
        initial = client.get(path, headers=admin).json()['data']
        assert initial['defaultModel'] == ''
        body = {'models': {provider: ['new-media-model']}, 'defaultModel': 'new-media-model'}
        assert client.patch(path, headers=readonly, json=body).status_code == 403
        assert client.patch(path, headers=admin, json=body).status_code == 400
        response = client.patch(path, headers={**admin, 'If-Match': initial['revision']}, json=body)
        assert response.status_code == 200, response.text
        saved = response.json()['data']
        assert saved['defaultModel'] == 'new-media-model' and saved['models'][provider] == ['new-media-model']
        assert client.get(path, headers=admin).json()['data'] == saved
        snapshot = copy.deepcopy(config.get())
        for patch in ({'defaultModel': 'not-configured'}, {'models': {provider: []}, 'defaultModel': 'new-media-model'}):
            bad = client.patch(path, headers={**admin, 'If-Match': saved['revision']}, json=patch)
            assert bad.status_code == 422 and config.get() == snapshot
        assert client.patch(path, headers={**admin, 'If-Match': initial['revision']}, json={'defaultModel': ''}).status_code == 409
        cleared = client.patch(path, headers={**admin, 'If-Match': saved['revision']}, json={'defaultModel': ''})
        assert cleared.status_code == 200 and cleared.json()['data']['defaultModel'] == ''
        schema = client.app.openapi()['components']['schemas']
        assert 'defaultModel' in schema['ImageSettingsData']['properties']
        assert 'defaultModel' in schema['VideoSettingsPatch' if kind == 'videos' else 'ImageSettingsPatch']['properties']


def test_video_settings_and_source_have_production_audit_records(domain_client):
    client, owner, admin, *_ = domain_client
    from src.management_api.routers.media_settings import router
    client.app.include_router(router, prefix='/api/management/v1')
    graph = owner.control_owner()
    assert graph.auxiliary.videos._audit_sink is graph.auxiliary.images._audit_sink
    config.update(lambda root: root.update(oauthAccounts=[dict(provider='xai', email='audit@example.test',
        generationId=uuid.uuid4().hex, enabled=False, disabled_reason='user', access_token='synthetic')]))
    before = owner.state_store.audit_snapshot()
    for kind in ('images', 'videos'):
        path = '/api/management/v1/' + kind + '/settings'
        dto = client.get(path, headers=admin).json()['data']
        result = client.patch(path, headers={**admin, 'If-Match': dto['revision']}, json={'requestTimeoutSeconds': 271})
        assert result.status_code == 200, result.text
    source = client.get('/api/management/v1/media/video/sources', headers=admin).json()['data']['sources'][0]
    result = client.patch('/api/management/v1/media/video/sources/' + quote(source['sourceId'], safe=''),
        headers={**admin, 'If-Match': source['revision']}, json={'enabled': True})
    assert result.status_code == 200, result.text
    added = [r for r in owner.state_store.audit_snapshot() if r not in before]
    assert sorted(r['action'] for r in added) == ['image.settings.update', 'video.settings.update', 'video.source.update']
    assert all(r['actor'] == 'administrator' and r['result'] == 'succeeded' and r['request_id'] for r in added)


@pytest.mark.parametrize('kind,mime,suffix', [('image', 'image/png', 'png'), ('video', 'video/mp4', 'mp4')])
def test_restart_reaps_orphan_temporary_not_history_or_symlink(tmp_path, kind, mime, suffix):
    cfg = {'cachePath': str(tmp_path / 'cache'), 'cacheRetentionDays': 0, 'cacheMaxBytes': 0, '_media_kind': kind}
    image_artifacts._ASSETS.clear()
    url, _ = image_artifacts.publish(b'fixture', mime=mime, cfg=cfg, provider='xai', action='generate',
        index=0, media_type=kind, base_url='http://unit.invalid')
    old = Path(image_artifacts._ASSETS[image_artifacts.media_token(url)].path)
    history = old.with_name('xai-' + kind + '-generate-old.' + suffix)
    history.write_bytes(b'history')
    outside = tmp_path / ('outside.' + suffix)
    outside.write_bytes(b'outside')
    link = old.with_name('url-' + kind + '-temporary-link.' + suffix)
    link.symlink_to(outside)
    for path in (old, history): os.utime(path, (time.time()-7200, time.time()-7200))
    image_artifacts._ASSETS.clear()
    image_artifacts.publish(b'new', mime=mime, cfg=cfg, provider='xai', action='generate',
        index=1, media_type=kind, base_url='http://unit.invalid')
    assert not old.exists() and history.read_bytes() == b'history'
    assert link.is_symlink() and outside.read_bytes() == b'outside'
    image_artifacts._ASSETS.clear()


def test_live_video_with_longer_ttl_is_not_reaped_as_orphan(tmp_path, monkeypatch):
    cfg = {'cachePath': str(tmp_path), 'cacheRetentionDays': 0, 'cacheMaxBytes': 0, '_media_kind': 'video'}
    image_artifacts._ASSETS.clear()
    url, expiry = image_artifacts.publish(b'video', mime='video/mp4', cfg=cfg, provider='xai', action='generate',
        index=0, media_type='video', ttl_seconds=7200, base_url='http://unit.invalid')
    old = Path(image_artifacts._ASSETS[image_artifacts.media_token(url)].path)
    monkeypatch.setattr(image_artifacts.time, 'time', lambda: expiry - 1200)
    image_artifacts.publish(b'new', mime='video/mp4', cfg=cfg, provider='xai', action='generate',
        index=1, media_type='video', base_url='http://unit.invalid')
    assert old.exists() and image_artifacts.url_available(url)
    image_artifacts._ASSETS.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize('model', ['api-image', 'gpt-image-2', 'grok-imagine-image'])
async def test_own_http_url_edit_and_mask_inline_without_remote_fetch(setup, monkeypatch, model):
    generated = await post(setup, {'model': 'gpt-image-2', 'prompt': 'icon', 'response_format': 'url'})
    assert generated.status_code == 200
    url = generated.json()['data'][0]['url']
    assert url.startswith('http://') and image_artifacts.url_available(url)
    mask_url, _ = image_artifacts.publish(png((0, 0, 0, 0), mode='RGBA'), mime='image/png', cfg=setup[0]['images'],
        provider='openai', action='generate', index=0, base_url='http://testserver')
    setup[0]['channels'] = [dict(name='api', protocol='openai-chat', enabled=True,
        models=[{'real': 'gpt-image-1', 'alias': 'api-image'}])]
    async def no_external(*args, **kwargs): raise AssertionError('own URL must not make a network request')
    monkeypatch.setattr(image_artifacts, 'download_https_image', no_external)
    edited = await post(setup, {'model': model, 'prompt': 'change color', 'image': url, 'mask': mask_url}, path='/v1/images/edits')
    assert edited.status_code == 200, edited.text
    parsed = setup[1][-1][1]
    assert parsed.input_images == ['data:image/png;base64,' + b64()]
    assert parsed.mask_url.startswith('data:image/png;base64,')
    if model == 'api-image':
        assert [field for field, _file in parsed._api_edit_files] == ['image[]', 'mask']
    assert len(setup[1]) == 2


@pytest.mark.asyncio
async def test_local_url_resolution_keeps_remote_and_path_security_boundaries(setup, monkeypatch, tmp_path):
    cfg = setup[0]['images']
    url, expiry = image_artifacts.publish(png(), mime='image/png', cfg=cfg, provider='openai', action='generate',
        index=0, base_url='http://testserver')
    entry = image_artifacts._ASSETS[image_artifacts.media_token(url)]
    assert await image_artifacts.reference_bytes(url) == png()
    for fake in ('http://127.0.0.1/private.png', url.replace('testserver', '127.0.0.1'),
                 url.replace('/v1/images/assets/', '/other/'), url + '?extra=1', url + '-unknown'):
        with pytest.raises(ValueError, match='HTTPS'):
            await image_artifacts.reference_bytes(fake)
    remote_calls = []
    async def remote(value, **kwargs):
        remote_calls.append(value)
        return png(), 'image/png'
    with monkeypatch.context() as patch:
        patch.setattr(image_artifacts, 'download_https_image', remote)
        assert await image_artifacts.reference_bytes('https://public.example/image.png') == png()
    assert remote_calls == ['https://public.example/image.png']
    with monkeypatch.context() as patch:
        patch.setattr(image_artifacts.time, 'time', lambda: expiry + 1)
        with pytest.raises(ValueError, match='expired'):
            await image_artifacts.reference_bytes(url)
    outside = tmp_path / 'outside.png'; outside.write_bytes(png())
    path = Path(entry.path); path.unlink(); path.symlink_to(outside)
    with pytest.raises(ValueError, match='unavailable'):
        await image_artifacts.reference_bytes(url)
    path.unlink(); path.write_bytes(png())
    cfg['cachePath'] = str(tmp_path / 'new-root')
    with pytest.raises(ValueError, match='unavailable'):
        await image_artifacts.reference_bytes(url)


@pytest.mark.asyncio
async def test_local_reference_rejects_video_and_oversized_file(setup, monkeypatch):
    cfg = setup[0]['images']
    video, _ = image_artifacts.publish(b'video', mime='video/mp4', cfg=cfg, provider='xai', action='generate',
        index=0, media_type='video', base_url='http://testserver')
    with pytest.raises(ValueError, match='not an image'):
        await image_artifacts.reference_bytes(video)
    url, _ = image_artifacts.publish(png(), mime='image/png', cfg=cfg, provider='openai', action='generate',
        index=0, base_url='http://testserver')
    monkeypatch.setattr(media_cache, 'HARD_FILE_LIMIT', 8)
    with pytest.raises(ValueError, match='oversized'):
        await image_artifacts.reference_bytes(url)


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['success', 'failed', 'cancelled'])
async def test_failover_log_uses_actual_provider_and_account(setup, monkeypatch, tmp_path, outcome):
    cfg = setup[0]
    cfg.update(oauthAccounts=[], channelSelection='order')
    cfg['images']['dbPath'] = str(tmp_path / 'media-log.db')
    cfg['channels'] = [dict(name='xai-first', protocol='openai-chat', providerId='xai', enabled=True,
            models=[{'real': 'grok-imagine-image', 'alias': 'mixed-image'}]),
        dict(name='openai-last', protocol='openai-chat', providerId='openai', enabled=True,
            models=[{'real': 'gpt-image-1', 'alias': 'mixed-image'}])]
    monkeypatch.setattr(image_db, '_conn', None)
    image_db.init()
    monkeypatch.setattr(runtime.imagine, '_start_media_log', _REAL_START)
    monkeypatch.setattr(runtime.imagine, '_finish_media_log', _REAL_FINISH)
    calls = []
    async def send(source, *args, **kwargs):
        calls.append(source.key)
        if source.provider == 'xai': return httpx.Response(403, json={})
        if outcome == 'cancelled': raise asyncio.CancelledError()
        if outcome == 'failed': return httpx.Response(500, json={})
        return httpx.Response(200, json={'data': [{'b64_json': b64()}]})
    monkeypatch.setattr(runtime, '_send', send)
    try:
        if outcome == 'cancelled':
            with pytest.raises(asyncio.CancelledError):
                await post(setup, {'model': 'mixed-image', 'prompt': 'icon'})
        else:
            response = await post(setup, {'model': 'mixed-image', 'prompt': 'icon'})
            assert response.status_code == (200 if outcome == 'success' else 500)
        rows = image_db._get_conn().execute('SELECT * FROM image_call_logs').fetchall()
        assert len(rows) == 1
        row = dict(rows[0])
        assert calls == ['api:xai-first', 'api:openai-last']
        assert row['provider'] == 'openai' and row['account_key'] == 'api:openai-last' and row['status'] == outcome
    finally:
        image_db._conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [401, 403, 429, 500])
async def test_xai_image_records_real_cooldown_and_success_clears_expired_errors(setup, monkeypatch, tmp_path, status):
    setup[0]['oauthGraceCount'] = 0
    setup[0]['stateDbPath'] = str(tmp_path / 'media-state.db')
    state_db.close(); state_db.init()
    monkeypatch.setattr(cooldown, '_entries', {})
    monkeypatch.setattr(cooldown, 'is_blocked', _REAL_BLOCKED)
    requests = []
    source = next(row for row in image_catalog.sources() if row.provider == 'xai')
    async def headers(): return {'authorization': 'Bearer synthetic'}
    channel = SimpleNamespace(key=source.key, state_key=source.state_key, base_url='https://api.x.ai/v1',
                              build_media_headers=headers, supports_media_model=lambda *args: True)
    monkeypatch.setattr(registry, '_channels', {source.key: channel})
    def wire(request):
        requests.append(request.url.path)
        if len(requests) == 1:
            return httpx.Response(status, json={'error': {'message': 'fixture rejection'}})
        return httpx.Response(200, json={'data': [{'b64_json': b64()}]})
    monkeypatch.setattr(runtime.network, 'async_client', lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(wire)))
    monkeypatch.setattr(runtime, '_send', _REAL_SEND)
    try:
        first = await post(setup, {'model': source.model, 'prompt': 'icon'})
        assert first.status_code == status, first.text
        key = (source.key, source.upstream)
        assert cooldown._entries[key]['error_count'] == 1
        assert cooldown.is_blocked(source.state_key, source.upstream)
        second = await post(setup, {'model': source.model, 'prompt': 'icon'})
        assert second.status_code == 503 and len(requests) == 1
        cooldown._entries[key]['cooldown_until'] = int((time.time()-1)*1000)
        third = await post(setup, {'model': source.model, 'prompt': 'icon'})
        assert third.status_code == 200, third.text
        assert len(requests) == 2 and key not in cooldown._entries
        assert not any(row['channel_key'] == source.key for row in state_db.error_load_all())
    finally:
        state_db.close()
