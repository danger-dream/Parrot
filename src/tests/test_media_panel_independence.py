"""Media panels backed by real CAS configuration, HTTP execution and SQLite rows."""
from __future__ import annotations
import base64
import copy
import io
import json
import os
from pathlib import Path
import sqlite3
import uuid
from urllib.parse import quote

import httpx
import pytest
from fastapi import FastAPI, Request
from PIL import Image
from src import config, image_db, image_catalog, media_config, media_cache, oauth_manager, state_db, image_artifacts, model_metadata
from src.channel import registry
from src.channel.xai_oauth_channel import XAIOAuthChannel
from src.management_control import ManagementError
from src.management_control.auxiliary.media import ImageControl, VideoControl, XaiMediaControl
from src.management_control.models import ModelCenterControl, ModelFilters, ModelKind
from src.management_control.observability import MediaControl
from src.openai import images_openai_compat, images_runtime
from src.xai import imagine
from src.tests.test_model_center_media import FakeConfig, admin_context
from src.tests.test_management_mapping_support import domain_client, operation_map

CTX = admin_context()


def test_read_only_legacy_inheritance_and_explicit_save_preserve_custom_values(tmp_path):
    root = {'images': {'toolModel': 'custom-openai-image', 'mainModel': 'retired-chat-main',
        'cacheEnabled': True, 'cachePath': str(tmp_path), 'cacheRetentionDays': 9, 'cacheMaxBytes': 3456},
        'xaiOAuth': {'imageModels': ['custom-grok'], 'videoModels': ['custom-video'],
                     'mediaRequestTimeoutSeconds': 71, 'videoJobTtlSeconds': 234},
        'oauthAccounts': [{'provider': 'xai', 'email': 'human@example.test', 'imageModels': ['owned-image'], 'videoModels': ['owned-video']}]}
    store = FakeConfig(root)
    images, videos = ImageControl(config_gateway=store), VideoControl(config_gateway=store)
    image, video = images.get_settings(CTX), videos.get_settings(CTX)
    assert image.models == {'openai': ['gpt-image-2', 'gpt-image-2.5', 'custom-openai-image'], 'xai': ['custom-grok']}
    assert video.models == {'xai': ['custom-video']}
    assert video.cache_enabled and video.cache_retention_days == 9 and video.cache_max_bytes == 3456
    assert video.request_timeout_seconds == 71 and video.job_ttl_seconds == 234
    assert store.value == root and store.updates == 0
    changed = images.update_settings(CTX, {'cacheEnabled': False, 'cacheMaxBytes': 7}, expected_revision=image.revision)
    assert not changed.cache_enabled and changed.cache_max_bytes == 7
    assert videos.get_settings(CTX).cache_enabled and videos.get_settings(CTX).cache_max_bytes == 3456
    assert store.value['image_models']['openai'][-1] == 'custom-openai-image'
    assert 'mainModel' not in store.value['images'] and 'toolModel' not in store.value['images']
    assert store.value['oauthAccounts'] == root['oauthAccounts']
    before_image = copy.deepcopy(store.value['images'])
    video = videos.get_settings(CTX)
    changed_video = videos.update_settings(CTX, {'models': {'xai': ['video-new']}, 'cachePath': str(tmp_path / 'video'),
        'requestTimeoutSeconds': 51, 'jobTtlSeconds': 456}, expected_revision=video.revision)
    assert store.value['images'] == before_image
    assert changed_video.models == {'xai': ['video-new']}
    assert ImageControl(config_gateway=FakeConfig(store.value)).get_settings(CTX) == images.get_settings(CTX)
    assert VideoControl(config_gateway=FakeConfig(store.value)).get_settings(CTX) == changed_video
    stale = copy.deepcopy(store.value)
    with pytest.raises(ManagementError): images.update_settings(CTX, {'enabled': False}, expected_revision=image.revision)
    assert store.value == stale


def test_cache_legacy_files_type_scoped_cleanup_download_and_symlinks(tmp_path):
    root = tmp_path / 'shared'; root.mkdir()
    image = root / 'old.png'; image.write_bytes(b'legacy-image')
    video = root / 'old.mp4'; video.write_bytes(b'legacy-video')
    image_cfg = {'cacheEnabled': True, 'cachePath': str(root), 'cacheMaxBytes': 1, '_media_kind': 'image'}
    video_cfg = {**image_cfg, '_media_kind': 'video', 'cacheMaxBytes': 0}
    assert media_cache.occupancy(image_cfg) == {'files': 1, 'bytes': 12}
    assert media_cache.occupancy(video_cfg) == {'files': 1, 'bytes': 12}
    media_cache.cleanup(root, image_cfg)
    assert not image.exists() and video.read_bytes() == b'legacy-video'
    image_path = media_cache.write_bytes(b'new', cfg={**image_cfg, 'cacheMaxBytes': 0}, provider='openai', media_type='image', action='edit', extension='png', index=0)
    assert '/image/' in image_path
    media_cache.cleanup(root, {**video_cfg, 'cacheMaxBytes': 1})
    assert Path(image_path).read_bytes() == b'new' and not video.exists()
    assert not media_cache.artifact_path_is_safe(image_path, video_cfg)
    outside = tmp_path / 'outside'; outside.mkdir()
    unsafe = tmp_path / 'unsafe'; unsafe.mkdir(); (unsafe / 'video').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError): media_cache.write_bytes(b'v', cfg={**video_cfg, 'cachePath': str(unsafe)}, provider='xai', media_type='video', action='edit', extension='mp4', index=0)
    assert not list(outside.iterdir())


@pytest.fixture
def media_env(monkeypatch, tmp_path):
    root = copy.deepcopy(config.get())
    root.update(channels=[], oauthAccounts=[
        dict(provider='openai', email='openai-human@example.test', workspace_id='workspace-a', generationId=uuid.uuid4().hex,
             enabled=False, disabled_reason='user', access_token='synthetic-openai', expired='2099-01-01T00:00:00Z'),
        dict(provider='xai', email='grok-human@example.test', subject='stable-subject', generationId=uuid.uuid4().hex,
             enabled=False, disabled_reason='user', access_token='synthetic-xai', expired='2099-01-01T00:00:00Z')],
        image_models={'openai': ['gpt-image-2', 'gpt-image-2.5'], 'xai': ['grok-imagine-image']},
        video_models={'xai': ['grok-imagine-video']},
        images={'enabled': True, 'cacheEnabled': True, 'cachePath': str(tmp_path / 'shared'), 'cacheMaxBytes': 0, 'dbPath': str(tmp_path / 'actual-media.db')},
        videos={'enabled': True, 'cacheEnabled': False, 'cachePath': str(tmp_path / 'shared'), 'cacheMaxBytes': 0, 'requestTimeoutSeconds': 47, 'jobTtlSeconds': 600},
        apiKeys={'media': {'key': 'media-test', 'enabled': True, 'allowImages': True, 'allowVideos': True, 'allowedModels': []}},
        modelMapping={'global': {}}, modelCenter={}, channelSelection='order', concurrency={'enabled': False}, apiKeyConcurrency={'enabled': False})
    store = FakeConfig(root)
    monkeypatch.setattr(config, 'get', store.get)
    monkeypatch.setattr(config, 'update', store.update)
    monkeypatch.setattr(model_metadata, 'resolve_binding', lambda *args, **kwargs: None)
    monkeypatch.setattr(image_db, '_conn', None)
    image_db.init(); state_db.init()
    images, videos = ImageControl(config_gateway=store), VideoControl(config_gateway=store)
    channel = XAIOAuthChannel(store.value['oauthAccounts'][1])
    monkeypatch.setattr(registry, '_channels', {channel.key: channel})
    monkeypatch.setattr(imagine.cooldown, 'is_blocked', lambda *args: False)
    async def acquire(*args): return True
    monkeypatch.setattr(imagine.concurrency, 'try_acquire', acquire)
    monkeypatch.setattr(imagine.concurrency, 'release', lambda *args: None)
    image_artifacts._ASSETS.clear()
    yield store, images, videos, channel, tmp_path
    image_db._conn.close()


def change_source(control, source_id, enabled):
    source = next(row for row in control.list_sources(CTX) if row['source_id'] == source_id)
    return control.update_source(CTX, source_id, enabled=enabled, expected_revision=source['revision'])


def test_account_purpose_switches_stable_generation_and_cas(media_env):
    store, images, videos, channel, _tmp = media_env
    before = copy.deepcopy(store.value['oauthAccounts'])
    assert not channel.supports_media_model('image', 'grok-imagine-image')
    change_source(images, channel.key, True)
    assert channel.supports_media_model('image', 'grok-imagine-image')
    assert not channel.supports_media_model('video', 'grok-imagine-video')
    change_source(videos, channel.key, True)
    assert channel.supports_media_model('video', 'grok-imagine-video')
    image_values = copy.deepcopy(store.value['images'])
    change_source(videos, channel.key, False)
    assert store.value['images'] == image_values
    assert channel.supports_media_model('image', 'grok-imagine-image')
    assert store.value['oauthAccounts'] == before  # generation IDs pre-existed, no conversation changes
    selected = next(row for row in images.list_sources(CTX) if row['source_id'] == channel.key)
    store.value['oauthAccounts'].reverse()
    assert images.update_source(CTX, channel.key, enabled=False, expected_revision=selected['revision'])['enabled'] is False
    change_source(images, channel.key, True)
    selected = next(row for row in images.list_sources(CTX) if row['source_id'] == channel.key)
    store.value['oauthAccounts'][0]['generationId'] = uuid.uuid4().hex
    with pytest.raises(ManagementError): images.update_source(CTX, channel.key, enabled=False, expected_revision=selected['revision'])
    assert not channel.supports_media_model('image', 'grok-imagine-image')
    assert not next(row for row in images.list_sources(CTX) if row['source_id'] == channel.key)['enabled']


@pytest.mark.asyncio
async def test_real_images_video_create_poll_edit_cache_statistics_pipeline(media_env, monkeypatch):
    store, images, videos, channel, tmp = media_env
    for source in images.list_sources(CTX): change_source(images, source['source_id'], True)
    change_source(videos, channel.key, True)
    store.value['modelMapping']['global']['clip-alias'] = 'grok-imagine-video'
    encoded = io.BytesIO(); Image.new('RGB', (24, 32), 'blue').save(encoded, format='PNG'); raw_image = encoded.getvalue()
    seen = []
    video_number = [0]
    def wire(req):
        seen.append((req.method, req.url.path))
        if req.url.path.endswith('/images/generations') or req.url.path.endswith('/images/edits'):
            return httpx.Response(200, json={'data': [{'b64_json': base64.b64encode(raw_image).decode()}]})
        if req.method == 'POST' and '/videos/' in req.url.path:
            video_number[0] += 1
            return httpx.Response(200, json={'request_id': f'job-{video_number[0]}', 'status': 'pending'})
        if req.method == 'GET':
            return httpx.Response(200, json={'status': 'done', 'video': {'b64_json': base64.b64encode(b'actual-video-bytes').decode(), 'mime_type': 'video/mp4'}})
        raise AssertionError(str(req.url))
    monkeypatch.setattr(imagine.network, 'async_client', lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(wire)))
    app = FastAPI()
    app.add_api_route('/v1/images/generations', images_openai_compat.handle_generations, methods=['POST'])
    app.add_api_route('/v1/images/edits', images_openai_compat.handle_edits, methods=['POST'])
    app.add_api_route('/v1/images/assets/{token}', image_artifacts.download, methods=['GET'])
    async def video_create(request: Request): return await imagine.handle_video_create(request, action='generate')
    app.add_api_route('/v1/videos/generations', video_create, methods=['POST'])
    app.add_api_route('/v1/videos/{request_id}', imagine.handle_video_result, methods=['GET'])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://unit', headers={'Authorization': 'Bearer media-test'}) as client:
        image = await client.post('/v1/images/generations', json={'model': 'gpt-image-2.5', 'prompt': 'test', 'size': '32x32'})
        assert image.status_code == 200, image.text
        assert Image.open(io.BytesIO(base64.b64decode(image.json()['data'][0]['b64_json']))).size == (32, 32)
        vid = await client.post('/v1/videos/generations', json={'model': 'clip-alias', 'prompt': 'test'})
        assert vid.status_code == 200, vid.text
        assert (await client.get('/v1/videos/job-1')).status_code == 200
        assert media_cache.occupancy(media_config.settings('video')) == {'files': 0, 'bytes': 0}
        before = media_cache.occupancy(media_config.settings('image'))
        assert before['files'] == 1
        v = videos.get_settings(CTX)
        videos.update_settings(CTX, {'cacheEnabled': True}, expected_revision=v.revision)
        assert (await client.get('/v1/videos/job-1')).status_code == 200
        assert (await client.get('/v1/videos/job-1')).status_code == 200
        video_paths = list((tmp / 'shared' / 'video').rglob('*.mp4'))
        assert len(video_paths) == 1 and video_paths[0].read_bytes() == b'actual-video-bytes'
        assert media_cache.occupancy(media_config.settings('image')) == before
        i = images.get_settings(CTX)
        images.update_settings(CTX, {'cacheEnabled': False}, expected_revision=i.revision)
        edit = await client.post('/v1/images/edits', json={'model': 'gpt-image-2.5', 'prompt': 'edit',
            'image': 'data:image/png;base64,' + base64.b64encode(raw_image).decode()})
        assert edit.status_code == 200, edit.text
        assert media_cache.occupancy(media_config.settings('image')) == before
        assert video_paths[0].exists()
        # Turning the video purpose off blocks poll without disabling images/chat.
        change_source(videos, channel.key, False)
        polls = len(seen)
        assert (await client.get('/v1/videos/job-1')).status_code == 503
        assert len(seen) == polls
        assert (await client.post('/v1/images/generations', json={'model': 'grok-imagine-image', 'prompt': 'still enabled'})).status_code == 200
        # Same public account key, new generation: never poll an old paid job on it.
        change_source(videos, channel.key, True)
        replacement = store.value['oauthAccounts'][1]
        replacement['generationId'] = uuid.uuid4().hex
        change_source(videos, channel.key, True)
        registry._channels[channel.key] = XAIOAuthChannel(replacement)
        calls_before = len(seen)
        assert (await client.get('/v1/videos/job-1')).status_code == 503
        assert len(seen) == calls_before
    rows = image_db._get_conn().execute('SELECT * FROM image_call_logs ORDER BY id').fetchall()
    assert len(rows) == 4
    assert json.loads(rows[0]['output_sizes']) == ['32x32']
    assert rows[0]['image_bytes'] > 0 and rows[1]['image_count'] == 1
    assert rows[1]['image_bytes'] == len(b'actual-video-bytes')
    assert rows[2]['image_bytes'] > 0 and rows[2]['cached_images'] == 0
    stats = {row['model']: row for row in image_db.model_statistics('image')}
    assert stats['gpt-image-2.5']['generated_count'] == 2
    assert image_db.model_statistics('video')[0]['generated_count'] == 1  # repeated polling not counted again
    assert all(not account['enabled'] and account['disabled_reason'] == 'user' for account in store.value['oauthAccounts'])


def test_real_sqlite_sample_counts_exclude_attempts_failures_pending_unknowns(media_env):
    _store, images, videos, _channel, tmp = media_env
    def row(kind, model, status, count=None, size=None):
        identifier = image_db.start_media_call(request_id=str(uuid.uuid4()), api_key_name='test', provider='xai', media_type=kind, action='generate', model=model)
        image_db.finish_media_call(identifier, status=status, image_count=count, media_bytes=size)
        return identifier
    main = row('image', 'sample-image', 'success', 3, 300)
    attempt = image_db.start_attempt(main, request_id='attempt-only', account_key='test', account_email='sample@example.test')
    image_db.finish_attempt(attempt, status='success', image_count=3, image_bytes=300)
    row('image', 'sample-image', 'failed', 10, 1000)
    row('image', 'sample-image', 'pending', 10, 1000)
    row('image', 'sample-image', 'success')
    video = row('video', 'sample-video', 'success', 1, 500)
    image_db.finish_media_call(video, status='success', image_count=1, media_bytes=500)
    image_stats, video_stats = image_db.model_statistics('image'), image_db.model_statistics('video')
    assert image_stats == [dict(model='sample-image', completed_calls=2, generated_count=3, unknown_count_calls=1, recorded_bytes=300, unknown_bytes_calls=1)]
    assert video_stats == [dict(model='sample-video', completed_calls=1, generated_count=1, unknown_count_calls=0, recorded_bytes=500, unknown_bytes_calls=0)]
    assert images.statistics(CTX)['cache'] == {'files': 0, 'bytes': 0}
    destination = os.environ.get('PARROT_MEDIA_SAMPLE_REPORT')
    if destination:
        path = Path(destination); path.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path / 'media-statistics-sample.db') as output:
            image_db._get_conn().backup(output)
        (path / 'media-statistics-sample.json').write_text(json.dumps({'images': image_stats, 'videos': video_stats, 'cache': images.statistics(CTX)['cache'], 'note': 'synthetic inputs, real temporary SQLite main/attempt tables; no production data'}, indent=2))


def test_hot_http_schema_discovery_permissions_cas_and_rebuilt_controls(domain_client):
    client, runtime, admin, read_only, denied = domain_client
    from src.management_api.routers.media_settings import router
    client.app.include_router(router, prefix='/api/management/v1')
    config.update(lambda root: root.update(image_models={'openai': ['gpt-image-2', 'gpt-image-2.5'], 'custom': ['keep-custom']},
        video_models={'xai': ['video-old']}, oauthAccounts=[dict(provider='xai', email='display@example.test', subject='opaque-subject',
            generationId=uuid.uuid4().hex, enabled=False, disabled_reason='user', access_token='secret-never-public')]))
    base = '/api/management/v1'
    initial = client.get(base + '/images/settings', headers=admin).json()['data']
    assert 'mainModel' not in initial and 'toolModel' not in initial
    for path in ('/images/settings', '/videos/settings', '/media/image/sources', '/media/video/sources'):
        assert client.get(base + path).status_code == 401
        assert client.get(base + path, headers=denied).status_code == 403
        assert client.get(base + path + '?undeclared=1', headers=admin).status_code == 422
    assert client.patch(base + '/images/settings', headers=read_only, json={'models': {'openai': ['image-new']}}).status_code == 403
    assert client.patch(base + '/images/settings', headers=admin, json={'models': {'openai': ['image-new']}}).status_code == 400
    changed = client.patch(base + '/images/settings', headers={**admin, 'If-Match': initial['revision']}, json={'models': {'openai': ['image-new']}})
    assert changed.status_code == 200, changed.text
    assert changed.json()['data']['models']['custom'] == ['keep-custom']
    assert config.get()['image_models']['openai'] == ['image-new']
    assert client.patch(base + '/images/settings', headers={**admin, 'If-Match': initial['revision']}, json={'cacheEnabled': True}).status_code == 409
    assert client.patch(base + '/images/settings', headers=admin, json={'toolModel': 'retired'}).status_code == 422
    video = client.get(base + '/videos/settings', headers=admin).json()['data']
    saved = client.patch(base + '/videos/settings', headers={**admin, 'If-Match': video['revision']}, json={'models': {'xai': ['video-new']}, 'jobTtlSeconds': 72})
    assert saved.status_code == 200, saved.text
    assert config.get()['video_models']['xai'] == ['video-new'] and imagine.video_models() == ['video-new']
    source = client.get(base + '/media/video/sources', headers=admin).json()['data']['sources'][0]
    path = base + '/media/video/sources/' + quote(source['sourceId'], safe='')
    assert source['label'] == 'display@example.test' and 'secret-never-public' not in json.dumps(source)
    assert client.patch(path, headers=admin, json={'enabled': True}).status_code == 400
    assert client.patch(path, headers=read_only, json={'enabled': True}).status_code == 403
    enabled = client.patch(path, headers={**admin, 'If-Match': source['revision']}, json={'enabled': True})
    assert enabled.status_code == 200 and enabled.json()['data']['enabled']
    assert not config.get()['oauthAccounts'][0]['enabled']
    assert client.patch(path, headers={**admin, 'If-Match': source['revision']}, json={'enabled': False}).status_code == 409
    operations = operation_map(client)
    assert {'getVideoSettings', 'updateVideoSettings', 'listMediaSources', 'updateMediaSource'} <= operations.keys()
    assert VideoControl().get_settings(CTX).models == {'xai': ['video-new']}
    assert ImageControl().get_settings(CTX).models == changed.json()['data']['models']
    assert any(view.model_id == 'video-new' for view in ModelCenterControl().list_models(filters=ModelFilters(kinds=(ModelKind.VIDEO,))).items)
    persisted = json.loads(Path(config.CONFIG_PATH).read_text())
    for key in ('image_models', 'video_models', 'images', 'videos', 'oauthAccounts'):
        assert persisted[key] == config.get()[key]  # source/control result matches actual saved target


def test_real_tg_panel_buttons_display_sources_stats_and_immediate_scoped_actions(media_env, monkeypatch):
    from src.telegram import ui, states
    from src.telegram.menus import model_center_menu as menu
    store, images, videos, channel, _tmp = media_env
    control = ModelCenterControl(images=images, videos=videos)
    monkeypatch.setattr(menu, '_CONTROL', control)
    monkeypatch.setattr(ui, 'is_admin', lambda chat: chat == 91)
    edits, answers, sends = [], [], []
    monkeypatch.setattr(ui, 'edit', lambda *args, **kw: edits.append((args, kw)))
    monkeypatch.setattr(ui, 'answer_cb', lambda *args, **kw: answers.append((args, kw)))
    monkeypatch.setattr(ui, 'send', lambda *args, **kw: sends.append((args, kw)))
    menu.reset_for_tests(); states.clear_all()
    menu._session(91).tab = 'image'
    def button(kb, action, **match):
        for row in kb['inline_keyboard']:
            for item in row:
                frozen = menu._thaw(91, item.get('callback_data', '').removeprefix('mc:a:'))
                if frozen and frozen.name == action and all(frozen.data.get(key) == value for key,value in match.items()):
                    return item
        raise AssertionError((action, match))
    log = image_db.start_media_call(request_id='panel-real-record', api_key_name='test', provider='xai', media_type='image', action='edit', model='grok-imagine-image')
    image_db.finish_media_call(log, status='success', image_count=3, media_bytes=2048, output_sizes=['24x32'] * 3)
    text, kb = menu.render(91)
    assert '已生成：3 张图片 · 已记录 2.0KB' in text
    destination = os.environ.get('PARROT_MEDIA_SAMPLE_REPORT')
    if destination:
        Path(destination, 'image-panel-sample.json').write_text(json.dumps({'text': text, 'keyboard': kb}, ensure_ascii=False, indent=2))
    assert all(word in text for word in ('图片接口：开', '保留天数', '空间上限', '当前缓存占用', '已生成', 'gpt-image-2.5'))
    assert not any(word in str(kb) for word in ('查询', '多选', '模型编号', '批量编辑', '新增模型', '主模型'))
    account = button(kb, 'media_source', source_id=channel.key)
    assert account['icon_custom_emoji_id'] == ui.provider_custom_emoji_id('xai')
    assert 'grok-human@example.test' in account['text'] and 'stable-subject' not in text
    snapshot = copy.deepcopy(store.value['oauthAccounts'])
    assert menu.handle_callback(91, 2, 'on', account['callback_data'])
    assert channel.supports_media_model('image', 'grok-imagine-image')
    assert not channel.supports_media_model('video', 'grok-imagine-video')
    assert store.value['oauthAccounts'] == snapshot
    assert '可用来源：grok-human@example.test' in edits[-1][0][2]
    assert menu.handle_callback(91, 2, 'stale', account['callback_data'])
    assert any('页面版本已变化' in str(answer) for answer in answers)
    video_text, video_kb = menu._video_settings_render(91)
    assert '任务 TTL：600 秒' in video_text and '请求超时：47 秒' in video_text
    ttl = button(video_kb, 'media_field', field='jobTtlSeconds')
    menu.handle_callback(91, 2, 'ttl', ttl['callback_data'])
    assert states.get_state(91)['action'] == 'mc_media_field'
    menu.handle_text_state(91, 'mc_media_field', '2h')
    assert videos.get_settings(CTX).job_ttl_seconds == 7200
    assert images.get_settings(CTX).request_timeout_seconds == 180
    menu.reset_for_tests(); states.clear_all()
