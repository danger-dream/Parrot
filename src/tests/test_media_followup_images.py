"""I1/I2: correct-behavior regressions converted from the media audit probes."""
import json

import httpx
import pytest

from src import image_db, media_db
from src.openai import images_runtime as runtime
from src.tests.test_images_unified import setup, post, b64, ref

_REAL_START = runtime.imagine._start_media_log
_REAL_FINISH = runtime.imagine._finish_media_log


@pytest.mark.asyncio
@pytest.mark.parametrize('reported_cost', [123456, 0, None])
async def test_safe_failover_keeps_observed_cost_and_unknown_usage(setup, monkeypatch, tmp_path, reported_cost):
    cfg = setup[0]
    cfg['images']['dbPath'] = str(tmp_path / 'isolated-media.db')
    cfg['oauthAccounts'] = []
    cfg['channelSelection'] = 'order'
    cfg['channels'] = [dict(name=name, protocol='openai-chat', providerId='xai', enabled=True,
        models=[{'real': 'grok-imagine-image', 'alias': 'my-image'}]) for name in ['first', 'second']]
    monkeypatch.setattr(image_db, '_conn', None)
    image_db.init()
    monkeypatch.setattr(runtime.imagine, '_start_media_log', _REAL_START)
    monkeypatch.setattr(runtime.imagine, '_finish_media_log', _REAL_FINISH)
    calls = []
    usage = {} if reported_cost is None else {'cost_in_usd_ticks': reported_cost}

    async def send(source, *args, **kwargs):
        calls.append(source.key)
        if len(calls) == 1:
            return httpx.Response(429, json={'error': {'message': 'busy'}})
        return httpx.Response(200, json={'data': [{'b64_json': b64()}], 'usage': usage})

    monkeypatch.setattr(runtime, '_send', send)
    try:
        response = await post(setup, {'model': 'my-image', 'prompt': 'icon'})
        assert response.status_code == 200
        assert calls == ['api:first', 'api:second']
        row = media_db.recent()[0]
        assert response.json()['parrot']['usage_by_call'] == [None, usage]
        assert json.loads(row['usage_json']) == {'parrot_usage_by_call': [None, usage]}
        assert row['cost_usd_ticks'] == reported_cost
        assert media_db.summary()['cost_usd_ticks'] == (reported_cost or 0)
        assert media_db.account_top()[0]['cost_usd_ticks'] == (reported_cost or 0)
    finally:
        image_db._conn.close()


@pytest.mark.parametrize('usage,expected', [
    (None, None),
    ({'parrot_usage_by_call': [None, {}]}, None),
    ({'parrot_usage_by_call': [None, {'cost_in_usd_ticks': 0}]}, 0),
    ({'parrot_usage_by_call': [{'cost_in_usd_ticks': 123}, None, {'cost_in_usd_ticks': 456}]}, 579),
    ({'cost_in_usd_ticks': 579, 'parrot_usage_by_call': [{'cost_in_usd_ticks': 123}, {'cost_in_usd_ticks': 456}]}, 579),
])
def test_cost_extraction_sums_only_reported_amounts_without_double_counting(usage, expected):
    assert image_db._cost_ticks_from_usage(usage) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize('source_kind,field', [('api', 'image'), ('api', 'mask'), ('oauth', 'image'), ('oauth', 'mask')])
async def test_edit_reference_or_mask_timeout_is_structured_before_post(setup, monkeypatch, source_kind, field):
    model = 'gpt-image-2'
    if source_kind == 'api':
        setup[0]['oauthAccounts'] = []
        setup[0]['channels'] = [dict(name='api', protocol='openai-chat', enabled=True,
            models=[{'real': 'gpt-image-1', 'alias': 'api-image'}])]
        model = 'api-image'
    remote = 'https://public.example/private-input.png'
    body = {'model': model, 'prompt': 'change color', 'image': ref(), 'mask': ref()}
    body[field] = remote
    downloaded = []
    logs = []

    async def download(value, **kwargs):
        downloaded.append(value)
        raise httpx.ReadTimeout(remote)

    async def start(**kwargs):
        logs.append(kwargs)

    monkeypatch.setattr(runtime.image_artifacts, 'download_https_image', download)
    monkeypatch.setattr(runtime.imagine, '_start_media_log', start)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=setup[2], raise_app_exceptions=False), base_url='http://testserver') as client:
        response = await client.post('/v1/images/edits', json=body)
    assert response.status_code == 504
    assert response.json()['error']['type'] == 'image_input_timeout'
    assert 'no generation was attempted' in response.json()['error']['message']
    assert remote not in response.text
    assert downloaded == [remote]
    assert not setup[1] and not logs
