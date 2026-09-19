"""V1/V2/V3: real isolated DB and HTTP-handler regressions, fake paid upstream."""
import asyncio
import copy
import uuid

import httpx
import pytest

from src import channel_state, concurrency, config, image_db, media_db, state_db
from src.channel import registry
from src.xai import imagine
from src.tests.test_xai_imagine import _setup, _install_channel, _build_app, _headers


async def request(method, path, body=None):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_build_app()), base_url='http://testserver') as client:
        return await client.request(method, path, headers=_headers(), json=body)


def pending_job(channel, request_id='job'):
    state_db.xai_video_job_save(request_id, channel_key=channel.key, api_key_name='media-key',
        model='grok-imagine-video', ttl_seconds=3600, state_key=channel.state_key)
    log_id = media_db.start_call(request_id='local-' + request_id, api_key_name='media-key',
        provider='xai', media_type='video', action='generate', model='grok-imagine-video')
    media_db.finish_call(log_id, status='pending', upstream_request_id=request_id, expires_at=10**12)
    return log_id


def video_request(poll=False):
    return request('GET', '/v1/videos/job') if poll else request('POST', '/v1/videos',
        {'model': 'grok-imagine-video', 'prompt': 'icon moves'})


@pytest.mark.asyncio
@pytest.mark.parametrize('poll', [False, True])
async def test_video_create_and_query_share_occupied_generation_slot(monkeypatch, poll):
    channel = _install_channel()
    pending_job(channel)
    config._cache['concurrency'] = {'enabled': True, 'defaultMaxConcurrent': 1}
    channel.max_concurrent = 1
    generation = channel_state.effect_key(channel)
    assert await concurrency.try_acquire(generation)
    calls = []

    async def upstream(*args, **kwargs):
        calls.append(kwargs['method'])
        assert concurrency._slots[generation].in_flight == 1
        assert channel.key not in concurrency._slots
        return httpx.Response(200, json={'request_id': 'accepted', 'status': 'pending'})

    monkeypatch.setattr(imagine, '_request_upstream', upstream)
    try:
        response = await video_request(poll)
        assert response.status_code == 503 and calls == []
        assert concurrency._slots[generation].in_flight == 1
    finally:
        concurrency.release(generation)
    response = await video_request(poll)
    assert response.status_code == 200 and calls == ['GET' if poll else 'POST']
    assert set(concurrency._slots) == {generation}
    assert concurrency._slots[generation].in_flight == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('poll', [False, True])
async def test_video_cancel_releases_selected_slot_not_replacement(monkeypatch, poll):
    channel = _install_channel()
    pending_job(channel)
    config._cache['concurrency'] = {'enabled': True, 'defaultMaxConcurrent': 1}
    old_generation = channel_state.effect_key(channel)
    entered = asyncio.Event()
    calls = []

    async def upstream(*args, **kwargs):
        calls.append(kwargs['method'])
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(imagine, '_request_upstream', upstream)
    task = asyncio.create_task(video_request(poll))
    await entered.wait()
    replacement = copy.copy(channel)
    replacement.state_key = channel_state.register_oauth_generation(channel.key, uuid.uuid4().hex)
    registry._channels[channel.key] = replacement
    assert await concurrency.try_acquire(replacement.state_key)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == ['GET' if poll else 'POST']
    assert concurrency._slots[old_generation].in_flight == 0
    assert concurrency._slots[replacement.state_key].in_flight == 1
    assert channel.key not in concurrency._slots
    concurrency.release(replacement.state_key)


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['start', 'post', 'cache', 'finish', 'finish_error'])
async def test_video_creation_cancel_always_finishes_owned_log(monkeypatch, phase):
    channel = _install_channel()
    config._cache['concurrency'] = {'enabled': True, 'defaultMaxConcurrent': 1}
    entered = asyncio.Event()
    release = asyncio.Event()
    cleanup_entered = asyncio.Event()
    cleanup_release = asyncio.Event()
    calls = []
    finishes = []
    real_start = imagine._start_media_log
    real_finish = imagine._finish_media_log

    async def start(**fields):
        if phase == 'start':
            entered.set()
            await release.wait()
        return await real_start(**fields)

    async def upstream(*args, **kwargs):
        calls.append(kwargs['method'])
        if phase == 'post':
            entered.set()
            await asyncio.Event().wait()
        if phase == 'finish_error':
            return httpx.Response(500, json={'error': {'message': 'failed'}})
        return httpx.Response(200, json={'request_id': 'accepted', 'status': 'done' if phase == 'cache' else 'pending',
            'video': {'url': 'https://media.x.ai/result.mp4'}, 'usage': {'cost_in_usd_ticks': 123}})

    async def cache(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    async def finish(log_id, **fields):
        finishes.append(fields)
        if phase in {'finish', 'finish_error'}:
            entered.set()
            await release.wait()
        elif phase == 'post':
            cleanup_entered.set()
            await cleanup_release.wait()
        return await real_finish(log_id, **fields)

    monkeypatch.setattr(imagine, '_start_media_log', start)
    monkeypatch.setattr(imagine, '_finish_media_log', finish)
    monkeypatch.setattr(imagine, '_request_upstream', upstream)
    monkeypatch.setattr(imagine, '_cache_xai_results', cache)
    task = asyncio.create_task(video_request())
    await entered.wait()
    task.cancel()
    if phase == 'post':
        await cleanup_entered.wait()
        task.cancel()  # Repeated cancellation must not interrupt the terminal write.
        cleanup_release.set()
    else:
        await asyncio.sleep(0)
        task.cancel()
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    rows = media_db.recent()
    assert len(rows) == 1 and len(finishes) == 1
    row = rows[0]
    assert calls == ([] if phase == 'start' else ['POST'])
    if phase in {'finish', 'finish_error'}:
        expected = 'pending' if phase == 'finish' else 'failed'
        assert row['status'] == expected  # Preserve an already-owned known outcome.
        assert row['http_status'] == (200 if phase == 'finish' else 500)
        if phase == 'finish':
            assert row['expires_at'] is not None
    else:
        assert row['status'] == 'cancelled' and row['finished_at'] is not None
        assert row['http_status'] == 499 and row['error_type'] == 'cancelled'
        assert row['duration_ms'] is not None
        assert media_db.summary()['pending_count'] == 0
    if phase == 'cache':
        assert row['upstream_request_id'] == 'accepted'
        assert row['account_key'] == channel.account_key
        assert row['cost_usd_ticks'] == 123
        assert state_db.xai_video_job_load('accepted') is not None
    assert all(slot.in_flight == 0 for slot in concurrency._slots.values())


@pytest.mark.asyncio
@pytest.mark.parametrize('upstream_status,terminal', [('done', 'success'), ('failed', 'failed'), ('expired', 'expired'), ('cancelled', 'cancelled')])
async def test_concurrent_video_polls_preserve_terminal_result(monkeypatch, upstream_status, terminal):
    channel = _install_channel()
    log_id = pending_job(channel, 'race')
    pending_started = asyncio.Event()
    release_pending = asyncio.Event()
    calls = 0

    async def upstream(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            pending_started.set()
            await release_pending.wait()
            return httpx.Response(200, json={'status': 'pending', 'progress': 10, 'usage': {'cost_in_usd_ticks': 0}})
        return httpx.Response(200, json={'status': upstream_status, 'progress': 100,
            'video': {'url': 'https://media.x.ai/result.mp4'}, 'usage': {'cost_in_usd_ticks': 123},
            'error': {'type': 'fixture_terminal', 'message': 'terminal detail'}})

    monkeypatch.setattr(imagine, '_request_upstream', upstream)
    old = asyncio.create_task(request('GET', '/v1/videos/race'))
    await pending_started.wait()
    new = await request('GET', '/v1/videos/race')
    completed = media_db.get_log(log_id)
    assert new.status_code == 200 and completed['status'] == terminal
    release_pending.set()
    assert (await old).json()['status'] == 'pending'  # Keep HTTP upstream pass-through.
    assert media_db.get_log(log_id) == completed
    assert completed['finished_at'] is not None and completed['cost_usd_ticks'] == 123
    assert media_db.summary()['pending_count'] == 0
    assert media_db.cleanup_expired(now=10**13) == 0


@pytest.mark.parametrize('writer', ['finish', 'job', 'job_without_status'])
def test_terminal_guard_is_atomic_at_real_db_write_even_with_stale_read(monkeypatch, writer):
    channel = _install_channel()
    log_id = pending_job(channel)
    stale_row = media_db.get_log(log_id)
    media_db.finish_call(log_id, status='success', upstream_status='done', progress=100,
        usage={'cost_in_usd_ticks': 99}, image_count=1, cache_paths=['retained.mp4'])
    completed = media_db.get_log(log_id)
    fields = dict(upstream_status='pending', progress=10, usage={'cost_in_usd_ticks': 0}, cache_paths=[])
    if writer == 'finish':
        media_db.finish_call(log_id, status='pending', **fields)
    else:
        # Simulate a DB worker reading before another worker commits completion.
        monkeypatch.setattr(image_db, 'media_log_for_upstream', lambda _: stale_row)
        assert media_db.update_job('job', status=None if writer == 'job_without_status' else 'pending', **fields)
    assert media_db.get_log(log_id) == completed
    # Normal same-terminal polls may still append cache information without
    # counting another generation or moving the original finish timestamp.
    media_db.finish_call(log_id, status='success', cache_paths=['new-retained.mp4'])
    assert media_db.get_log(log_id)['finished_at'] == completed['finished_at']
    assert media_db.count() == 1
