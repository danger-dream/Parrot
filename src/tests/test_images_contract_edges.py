"""Unified image response/wire/capability boundaries; real image bytes, no network."""
import base64
import copy
import io
import json
from email import policy
from email.parser import BytesParser
from importlib import reload
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from src import image_artifacts, image_catalog
from src.channel import registry
from src.openai import images_runtime as runtime, images_openai_compat as compat
from src.management_control.models import ModelCenterControl, ModelFilters, ModelKind
from src.tests.test_images_unified import setup, post, png, b64, ref


def encoded(fmt, *, alpha=False):
    image = Image.new('RGBA' if alpha else 'RGB', (32, 32), (10, 20, 30, 0) if alpha else (10, 20, 30))
    out = io.BytesIO()
    image.save(out, format=fmt, **({'lossless': True} if fmt == 'WEBP' else {}))
    return out.getvalue()


def data_url(raw, mime):
    return 'data:' + mime + ';base64,' + base64.b64encode(raw).decode()


def usage_responses(monkeypatch, rows):
    calls = []
    async def send(*args, **kwargs):
        usage = rows[len(calls)]; calls.append(usage)
        obj = {'data': [{'b64_json': b64()}]}
        if usage is not None: obj['usage'] = copy.deepcopy(usage)
        return httpx.Response(200, json=obj)
    monkeypatch.setattr(runtime, '_send', send)
    return calls


@pytest.mark.asyncio
async def test_usage_n2_sums_known_tokens_and_details_and_preserves_raw(setup, monkeypatch):
    rows = [
        {'input_tokens': 2, 'output_tokens': 4, 'total_tokens': 6,
         'input_tokens_details': {'text_tokens': 2, 'image_tokens': 0, 'cached_tokens': 0},
         'output_tokens_details': {'image_tokens': 4}, 'vendor_ticks': 90},
        {'input_tokens': 3, 'output_tokens': 7, 'total_tokens': 10,
         'input_tokens_details': {'text_tokens': 1, 'image_tokens': 2},
         'output_tokens_details': {'image_tokens': 6, 'text_tokens': 1}, 'vendor_ticks': 30},
    ]
    usage_responses(monkeypatch, rows)
    response = await post(setup, {'model': 'paint', 'prompt': 'icon', 'n': 2})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['usage'] == {'input_tokens': 5, 'output_tokens': 11, 'total_tokens': 16,
        'input_tokens_details': {'text_tokens': 3, 'image_tokens': 2, 'cached_tokens': 0},
        'output_tokens_details': {'image_tokens': 10, 'text_tokens': 1}}
    assert body['parrot']['usage_by_call'] == rows


@pytest.mark.asyncio
@pytest.mark.parametrize('rows,expected', [
    ([{'input_tokens': 0, 'output_tokens': 2}], {'input_tokens': 0, 'output_tokens': 2}),
    ([{'prompt_tokens': 3, 'completion_tokens': 2, 'prompt_tokens_details': {'cached_tokens': 0}},
      {'input_tokens': 1, 'output_tokens': 0}], {'input_tokens': 4, 'output_tokens': 2, 'input_tokens_details': {'cached_tokens': 0}}),
    ([{'input_tokens': 4, 'prompt_tokens': 99, 'total_tokens': 4}], {'input_tokens': 4, 'total_tokens': 4}),
    ([{'input_tokens': 4}, None], {'input_tokens': 4}),
    ([{'cost_in_usd_ticks': 123}, {'cost_in_usd_ticks': 456}], None),
    ([{'input_tokens': True, 'output_tokens': -1, 'total_tokens': '20',
       'input_tokens_details': {'unknown_count': 3}}], None),
    ([None, None], None),
])
async def test_usage_missing_vendor_specific_and_aliases_are_not_invented(setup, monkeypatch, rows, expected):
    usage_responses(monkeypatch, rows)
    response = await post(setup, {'model': 'paint', 'prompt': 'icon', 'n': len(rows)})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body.get('usage') == expected
    if any(isinstance(row, dict) for row in rows):
        assert body['parrot']['usage_by_call'] == rows
    else:
        assert 'usage' not in body and 'usage_by_call' not in body['parrot']


@pytest.mark.asyncio
async def test_api_multipart_reference_encodings_and_webp_mask_are_truthful(setup, monkeypatch):
    real_send = reload(runtime)._send
    setup[0]['channels'] = [dict(name='fixture', protocol='openai-chat', enabled=True, models=[dict(real='gpt-image-1', alias='paint-api', kind='image')])]
    source = next(row for row in image_catalog.sources() if row.key == 'api:fixture')
    channel_state_key = source.state_key
    channel = SimpleNamespace(base_url='https://upstream.invalid/v1', api_path=None, api_key='test')
    monkeypatch.setattr(registry, 'get_channel', lambda key: channel)
    requests = []
    def wire(request):
        requests.append(request)
        return httpx.Response(200, json={'data': [{'b64_json': b64()}]})
    monkeypatch.setattr(runtime.network, 'async_client', lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(wire)))
    jpeg, webp, mask = encoded('JPEG'), encoded('WEBP'), encoded('WEBP', alpha=True)
    parsed = compat._ParsedRequest(model='paint-api', prompt='edit',
        input_images=[data_url(jpeg, 'image/jpeg'), data_url(webp, 'image/webp')],
        mask_url=data_url(mask, 'image/webp'))
    channel.state_key = channel_state_key
    result = await real_send(source, parsed, action='edit', n=1, cfg={})
    assert result.status_code == 200
    request = requests[0]
    multipart = BytesParser(policy=policy.default).parsebytes(
        b'Content-Type: ' + request.headers['content-type'].encode() + b'\r\n\r\n' + request.content)
    parts = [part for part in multipart.iter_parts() if part.get_filename()]
    assert len(parts) == 3
    for part, fmt, ext, mime, signature in zip(parts,
            ('JPEG', 'WEBP', 'PNG'), ('jpeg', 'webp', 'png'),
            ('image/jpeg', 'image/webp', 'image/png'), (b'\xff\xd8\xff', b'RIFF', b'\x89PNG\r\n\x1a\n')):
        raw = part.get_payload(decode=True)
        assert part.get_content_type() == mime
        assert part.get_filename().endswith('.' + ext)
        assert raw.startswith(signature) and Image.open(io.BytesIO(raw)).format == fmt
    assert parts[0].get_payload(decode=True) == jpeg and parts[1].get_payload(decode=True) == webp
    assert Image.open(io.BytesIO(parts[2].get_payload(decode=True))).getchannel('A').getextrema() == (0, 0)


@pytest.mark.parametrize('fmt', ['JPEG', 'WEBP', 'PNG'])
def test_default_output_is_png_independent_of_pillow_format_retention(monkeypatch, fmt):
    raw = encoded(fmt)
    # Pillow is allowed to preserve format across decode/orientation operations.
    # The response contract must not accidentally depend on a copy losing it.
    original_open = image_artifacts.open_image
    def open_preserving_format(value):
        image = original_open(value); image.format = fmt; return image
    monkeypatch.setattr(image_artifacts, 'open_image', open_preserving_format)
    result, meta, _warnings = image_artifacts.normalize(raw, size=None, options={})
    assert result.startswith(b'\x89PNG\r\n\x1a\n')
    assert meta['output_format'] == 'png' and meta['mime_type'] == 'image/png'
    assert Image.open(io.BytesIO(result)).format == 'PNG'


def test_mask_prompt_does_not_misidentify_last_reference():
    parsed = compat._ParsedRequest(prompt='edit', input_images=['first', 'second'], mask_url='mask')
    prompt = runtime._prompt(parsed)
    assert 'provided mask' in prompt and 'last reference' not in prompt
    assert 'first image' in prompt


def test_oauth_source_labels_disambiguate_workspaces_without_opaque_ids(setup):
    cfg = setup[0]
    first = cfg['oauthAccounts'][0]
    first.update(workspace_name='SP', workspace_type='team', plan_type='team')
    other = dict(first, chatgpt_account_id='opaque-workspace-b', workspace_name='AU')
    personal_team = dict(first, chatgpt_account_id='opaque-workspace-c', workspace_name='Personal')
    cfg['oauthAccounts'].extend([other, personal_team])
    rows = [row for row in image_catalog.sources() if row.provider == 'openai' and row.model == 'gpt-image-2']
    assert rows[0].label == 'image@example.test · SP'
    assert rows[1].label == 'image@example.test · AU'
    assert rows[2].label == 'image@example.test · team'
    assert len({row.key for row in rows}) == 3
    assert all('opaque-workspace' not in row.label and 'test-workspace' not in row.label for row in rows)
    view = next(row for row in ModelCenterControl().list_models(filters=ModelFilters(kinds=(ModelKind.IMAGE,))).items
                if row.model_id == 'gpt-image-2')
    assert {row.label for row in view.sources} == {row.label for row in rows}
    cfg['oauthAccounts'].reverse()
    assert {row.key: row.label for row in image_catalog.sources() if row.provider == 'openai'} == {row.key: row.label for row in rows}


def api_sources(setup, monkeypatch, *, alternative=False, transparent=False):
    cfg = setup[0]
    cfg['oauthAccounts'] = []
    cfg['channelSelection'] = 'round_robin'
    cfg['channels'] = [{'name': 'xai', 'protocol': 'openai-chat', 'providerId': 'xai',
        'models': [{'real': 'grok-imagine-image', 'alias': 'mixed-image'}]}]
    if alternative:
        cfg['channels'].append({'name': 'openai', 'protocol': 'openai-chat', 'providerId': 'openai',
            'models': [{'real': 'gpt-image-1', 'alias': 'mixed-image'}]})
    channels = {f'api:{row["name"]}': SimpleNamespace(key=f'api:{row["name"]}', base_url=f'https://{row["name"]}.invalid/v1',
        api_path=None, api_key='test') for row in cfg['channels']}
    for row in image_catalog.sources():
        if row.key in channels: channels[row.key].state_key = row.state_key
    monkeypatch.setattr(registry, 'get_channel', lambda key: channels.get(key))
    monkeypatch.setattr(runtime, '_send', reload(runtime)._send)
    seen = []
    def wire(request):
        seen.append(request)
        return httpx.Response(200, json={'data': [{'b64_json': b64(encoded('PNG', alpha=True)) if transparent else b64()}]})
    monkeypatch.setattr(runtime.network, 'async_client', lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(wire)))
    return seen


@pytest.mark.asyncio
@pytest.mark.parametrize('limitation', ['references', 'moderation', 'transparency'])
@pytest.mark.parametrize('alternative', [False, True])
async def test_capability_preflight_400_or_skips_to_compatible_source(setup, monkeypatch, limitation, alternative):
    seen = api_sources(setup, monkeypatch, alternative=alternative, transparent=limitation == 'transparency')
    body = {'model': 'mixed-image', 'prompt': 'edit', 'images': [ref()] * (4 if limitation == 'references' else 1)}
    if limitation == 'moderation': body['moderation'] = 'low'
    if limitation == 'transparency': body['background'] = 'transparent'
    response = await post(setup, body, path='/v1/images/edits')
    if alternative:
        assert response.status_code == 200, response.text
        assert len(seen) == 1 and seen[0].url.host == 'openai.invalid'
        assert response.json()['parrot']['upstream_calls'] == 1
    else:
        assert response.status_code == 400, response.text
        assert {'references': '3 input images', 'moderation': 'moderation', 'transparency': 'transparent background'}[limitation] in response.text
        assert not seen


@pytest.mark.asyncio
async def test_invalid_api_reference_is_400_without_post_or_generation_attempt(setup, monkeypatch):
    seen = api_sources(setup, monkeypatch, alternative=True)
    setup[0]['channels'] = setup[0]['channels'][1:]
    response = await post(setup, {'model': 'mixed-image', 'prompt': 'edit',
        'image': data_url(b'not an image', 'image/png')}, path='/v1/images/edits')
    assert response.status_code == 400, response.text
    assert not seen


@pytest.mark.asyncio
@pytest.mark.parametrize('source_kind', ['codex', 'xai', 'api'])
async def test_common_edit_parameters_keep_one_response_contract_across_models(setup, monkeypatch, source_kind):
    cfg = setup[0]
    cfg['channelSelection'] = 'round_robin'
    if source_kind == 'codex':
        model = 'gpt-image-2'
        monkeypatch.setattr(registry, 'get_channel', lambda key: None)
        async def token(key, *, expected_state_key):
            assert expected_state_key
            return 'test'
        monkeypatch.setattr(runtime.oauth_manager, 'ensure_valid_token', token)
        monkeypatch.setattr(runtime.legacy, '_build_headers', lambda *args: {'Authorization': 'Bearer test'})
        monkeypatch.setattr(runtime, 'codex_responses_url', lambda cfg: 'https://codex.invalid/responses')
    else:
        cfg['oauthAccounts'] = []
        model = 'standard-image'
        provider, upstream = ('xai', 'grok-imagine-image') if source_kind == 'xai' else ('openai', 'gpt-image-1')
        cfg['channels'] = [{'name': 'fixture', 'protocol': 'openai-chat', 'providerId': provider,
                            'models': [{'real': upstream, 'alias': model}]}]
        channel = SimpleNamespace(key='api:fixture', base_url='https://api.invalid/v1', api_path=None, api_key='test', state_key=next(row.state_key for row in image_catalog.sources() if row.key == 'api:fixture'))
        monkeypatch.setattr(registry, 'get_channel', lambda key: channel)
    monkeypatch.setattr(runtime, '_send', reload(runtime)._send)
    # reload resets this imported helper; install the test endpoint afterward.
    if source_kind == 'codex':
        monkeypatch.setattr(runtime, 'codex_responses_url', lambda cfg: 'https://codex.invalid/responses')
    wire_payloads = []
    def wire(request):
        if request.headers['content-type'].startswith('multipart/'):
            parsed = BytesParser(policy=policy.default).parsebytes(
                b'Content-Type: ' + request.headers['content-type'].encode() + b'\r\n\r\n' + request.content)
            parts = list(parsed.iter_parts())
            fields = {part.get_param('name', header='content-disposition'): part.get_payload(decode=True).decode()
                      for part in parts if not part.get_filename()}
            assert sum(part.get_filename() is not None for part in parts) == 3
        else:
            fields = json.loads(request.content)
            if source_kind == 'codex': assert len(fields['images']) == 3
            if source_kind == 'xai': assert len(fields['images']) == 2 and 'mask' in fields
        assert 'provided mask' in fields['prompt'] and 'last reference' not in fields['prompt']
        wire_payloads.append(fields)
        return httpx.Response(200, json={'data': [{'b64_json': b64()} for _ in range(int(fields['n']))],
            'usage': {'input_tokens': 2, 'output_tokens': 3, 'total_tokens': 5}})
    monkeypatch.setattr(runtime.network, 'async_client', lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(wire)))
    response = await post(setup, {'model': model, 'prompt': 'edit', 'images': [ref(), ref()],
        'mask': ref(png((0, 0, 0, 0), mode='RGBA')), 'n': 2, 'size': '48x32', 'quality': 'medium',
        'background': 'opaque', 'output_format': 'webp', 'output_compression': 80,
        'input_fidelity': 'high', 'style': 'natural', 'moderation': 'auto'}, path='/v1/images/edits')
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['parrot']['complete'] and len(body['data']) == 2
    calls = 2 if source_kind == 'codex' else 1
    assert len(wire_payloads) == body['parrot']['upstream_calls'] == calls
    assert body['usage'] == {'input_tokens': 2*calls, 'output_tokens': 3*calls, 'total_tokens': 5*calls}
    for item in body['data']:
        image = Image.open(io.BytesIO(base64.b64decode(item['b64_json'])))
        assert image.format == 'WEBP' and image.size == (48, 32)
        assert item['mime_type'] == 'image/webp' and item['background'] == 'opaque'


def test_image_source_picker_uses_workspace_names_and_keeps_chat_labels(setup, monkeypatch):
    from src.telegram import ui
    from src.telegram.menus import model_center_menu as menu
    from src.oauth_ids import account_key
    first = setup[0]['oauthAccounts'][0]
    first.update(workspace_name='SP')
    second = dict(first, chatgpt_account_id='opaque-b', workspace_name='AU')
    setup[0]['oauthAccounts'] = [first, second]
    accounts = [SimpleNamespace(account_id=account_key(account), provider='openai',
        display_name=account['email'], identity=account['email']) for account in (first, second)]
    monkeypatch.setattr(ui, 'is_admin', lambda chat: True)
    monkeypatch.setattr(menu, '_CONTROL', SimpleNamespace(
        bind_telegram_actor=lambda chat: None,
        oauth=SimpleNamespace(list_accounts=lambda *args, **kwargs: SimpleNamespace(items=accounts)),
        channels=SimpleNamespace(list_all=lambda ctx: [])))
    menu.reset_for_tests()
    session = menu._session(42); session.tab = 'image'
    options = menu._source_options(42)
    assert options[0].label.endswith(' · SP') and options[1].label.endswith(' · AU')
    assert options[0].ref.id == account_key(first) and options[1].ref.id == account_key(second)
    assert all('opaque-b' not in option.label and 'test-workspace' not in option.label for option in options)
    session.tab = 'chat'
    assert all(option.label.endswith('image@example.test') for option in menu._source_options(42))
    menu.reset_for_tests()


@pytest.mark.asyncio
async def test_empty_image_result_still_preserves_real_usage(setup, monkeypatch):
    async def send(*args, **kwargs):
        return httpx.Response(200, json={'data': [], 'usage': {'input_tokens': 2, 'output_tokens': 0}})
    monkeypatch.setattr(runtime, '_send', send)
    response = await post(setup, {'model': 'paint', 'prompt': 'icon'})
    assert response.status_code == 502
    assert response.json()['usage'] == {'input_tokens': 2, 'output_tokens': 0}
    assert not response.json()['parrot']['complete']
