"""S1-S8 regressions: real boundaries, isolated config/SQLite and fake upstreams."""
from __future__ import annotations

import asyncio
import copy
import datetime
import json
from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from fastapi.responses import JSONResponse

from src import config, local_web_tools as web, log_db, oauth_manager
from src import search_service as service, search_tool_policy as policy
from src.management_api.dependencies import get_management_context
from src.management_auth import Capability
from src.management_control.search import SearchControl
from src.openai.transform.guard import GuardError
from src.telegram.menus import search_menu
from src.tests.test_search_management import (  # noqa: F401: isolated fixtures
    api, memory, control, ctx, tg, search_workers, callback, latest,
)
from src.tests.test_search_service_accounting import env  # noqa: F401
from src.tests.test_search_review_fixes import req, answer
from src.tests.search_stream_fixtures import wire


@pytest.fixture
def managed(monkeypatch):
    monkeypatch.setattr(web, '_settings', lambda: {**service.DEFAULTS, 'maxToolRounds': 4})
    policy._REPLAY.clear()
    yield
    policy._REPLAY.clear()


@pytest.mark.parametrize('uses_search', [False, True])
@pytest.mark.parametrize('streaming', [False, True])
async def test_s1_chat_round_options_follow_stream_mode(managed, monkeypatch, uses_search, streaming):
    from src.openai.channel.api_channel import OpenAIApiChannel
    channel = OpenAIApiChannel({'name': 'audit', 'baseUrl': 'https://audit.invalid',
                               'protocol': 'openai-chat', 'models': [{'real': 'm', 'alias': 'm'}]})
    body = {**req('chat'), 'stream': True, 'stream_options': {'include_usage': True}}
    original, sent = copy.deepcopy(body), []

    async def execute(calls, **kw):
        return [web.LocalToolResult(call.id, 'search result') for call in calls]

    async def invoke(current):
        upstream = await channel.build_upstream_request(current, 'm', ingress_protocol='chat')
        payload = json.loads(upstream.body)
        sent.append(payload)
        assert payload['stream'] is streaming
        if streaming:
            assert payload['stream_options']['include_usage'] is True
        else:
            assert 'stream_options' not in payload
        calls = [('s', 'web_search', {'query': 'docs'})] if uses_search and len(sent) == 1 else []
        return JSONResponse(answer('chat', calls, usage={'prompt_tokens': 2, 'completion_tokens': 1}))

    monkeypatch.setattr(web, 'execute_local_tool_calls', execute)
    if streaming:
        stream = policy.stream(body, 'chat', invoke)
        raw = b''.join([chunk async for chunk in stream.body_iterator])
        assert raw.endswith(b'data: [DONE]\n\n')
    else:
        raw = (await policy.run(body, 'chat', invoke)).body
    assert b'answer' in raw
    assert len(sent) == (2 if uses_search else 1)
    assert body == original


@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('choice', [
    {'type': 'web_search'},
    {'type': 'allowed_tools', 'mode': 'required', 'tools': [{'type': 'web_search'}, {'type': 'function', 'name': 'parrot_hosted_web_search'}]},
])
async def test_s2_http_responses_restore_hosted_metadata(managed, monkeypatch, streaming, choice):
    from src.tests import test_protocol_fake_upstreams as fx
    modules = fx._import_modules(); fx._setup(modules); fx._install_keys(modules, fx._default_key())
    channel = fx._make_openai_channel('audit-echo', 'https://audit-echo.example',
                                     protocol='openai-responses', alias='test-model', real='test-model')
    fx._install_channels(modules, [channel])
    tools = [{'type': 'web_search', 'filters': {'allowed_domains': ['example.com']}},
             {'type': 'function', 'name': 'parrot_hosted_web_search', 'parameters': {'type': 'object'}}]
    captured = []

    def upstream(request):
        payload = json.loads(request.content)
        captured.append(payload)
        assert payload['tools'][0]['name'] == 'parrot_hosted_web_search_1'
        obj = {**answer(), 'tools': payload['tools'],
            'tool_choice': payload['tool_choice'], 'metadata': {'literal': 'parrot_hosted_web_search_1'}}
        if payload.get('stream'):
            return httpx.Response(200, content=wire(obj, 'responses'), headers={'content-type': 'text/event-stream'})
        return httpx.Response(200, json=obj)

    router = fx.MockRouter(); router.register('https://audit-echo.example', upstream)
    body = {'model': 'test-model', 'input': 'hello', 'tools': tools,
            'tool_choice': copy.deepcopy(choice), 'stream': streaming}
    response, client = await fx._call_openai_handler(modules, router, 'responses', body)
    try:
        assert response.status_code == 200
        if streaming:
            raw = b''.join([chunk async for chunk in response.body_iterator])
            frames = [json.loads(line[5:]) for line in raw.splitlines() if line.startswith(b'data:')]
            objects = [frame['response'] for frame in frames if 'response' in frame]
            assert any(frame.get('type') == 'response.completed' for frame in frames)
        else:
            objects = [json.loads(response.body)]
        for obj in objects:
            assert obj['tools'] == tools
            assert obj['tool_choice'] == choice
            assert obj['metadata']['literal'] == 'parrot_hosted_web_search_1'
        assert len(captured) == 1
    finally:
        await client.aclose()


def test_s2_namespaced_metadata_restoration_is_structural(managed):
    source = {'type': 'web_search_preview', 'filters': {'allowed_domains': ['example.com']}}
    body = {'tools': [{'type': 'namespace', 'name': 'browser', 'tools': [source]}]}
    compiled, plan = policy.compile_request(body, 'responses')
    name = next(iter(plan.values())).name
    obj = {'tools': compiled['tools'], 'tool_choice': {'type': 'allowed_tools',
           'allowed_tools': {'mode': 'auto', 'tools': [{'type': 'function', 'name': name, 'namespace': 'browser'}]}},
           'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': name}]}]}
    policy._restore_hosted_metadata(obj, plan)
    assert obj['tools'] == body['tools']
    assert obj['tool_choice']['allowed_tools']['tools'] == [{'type': 'web_search_preview', 'namespace': 'browser'}]
    assert obj['output'][0]['content'][0]['text'] == name


def test_s3_write_only_receipts_succeed_without_granting_get(api, memory, ctx):
    client, headers, _, app = api
    writer = replace(ctx, actor=replace(ctx.actor, capabilities=frozenset({Capability.WRITE})))
    app.dependency_overrides[get_management_context] = lambda: writer
    base = '/api/management/v1/search'
    response = client.patch(base, json={'maxResults': 7}, headers=headers)
    assert response.status_code == 200 and response.json()['data']['maxResults'] == 7
    response = client.post(base + '/backends', json={'type': 'tavily', 'id': 'audit-source'}, headers=headers)
    assert response.status_code == 201
    response = client.patch(base + '/backends/audit-source', json={'name': 'Renamed'}, headers=headers)
    assert response.status_code == 200
    ids = [item['id'] for item in response.json()['data']['backends']][::-1]
    response = client.put(base + '/priority', json={'backendIds': ids}, headers=headers)
    assert response.status_code == 200
    assert [item['id'] for item in response.json()['data']['backends']] == ids
    assert memory.updates == 4
    assert client.get(base, headers=headers).status_code == 403
    denied = client.patch(base + '/backends/audit-source', json={'apiKeys': ['private-audit-key']}, headers=headers)
    assert denied.status_code == 403 and memory.updates == 4
    writer = replace(writer, actor=replace(writer.actor, capabilities=frozenset({Capability.WRITE, Capability.SECRETS_WRITE})))
    response = client.patch(base + '/backends/audit-source', json={'apiKeys': ['private-audit-key']}, headers=headers)
    assert response.status_code == 200 and memory.updates == 5
    assert 'private-audit-key' not in response.text and 'apiKeys' not in response.text
    assert client.get(base, headers=headers).status_code == 403
    writer = replace(writer, actor=replace(writer.actor, capabilities=frozenset({Capability.READ})))
    assert client.patch(base, json={'maxResults': 8}, headers=headers).status_code == 403
    assert service.settings()['maxResults'] == 7 and memory.updates == 5


def _oauth_env(monkeypatch, kind, *, attempts=1):
    account = {'provider': 'claude' if kind == 'anthropic' else kind,
               'email': 'audit@example.test', 'workspace_id': 'audit-workspace',
               'subject': 'audit-subject', 'access_token': 'private-audit-token'}
    cfg = copy.deepcopy(config.get())
    cfg['search'] = {**service.DEFAULTS, 'backends': [service.default_backend(kind)], 'maxAttempts': attempts}
    cfg['oauthAccounts'] = [account]
    monkeypatch.setattr(config, 'get', lambda: cfg)
    async def token(*args, **kwargs):
        return 'private-audit-token'
    monkeypatch.setattr(oauth_manager, 'ensure_valid_token', token)
    monkeypatch.setattr(oauth_manager, 'account_state_key', lambda account: 'audit-generation')
    monkeypatch.setattr(oauth_manager, 'account_generation_guard', lambda state: nullcontext(True))
    monkeypatch.setattr(oauth_manager, 'get_account', lambda key: account)
    if kind == 'anthropic':
        from src.channel.oauth_channel import OAuthChannel
        async def build(self, body, model, **kwargs):
            return SimpleNamespace(url='https://audit.example/messages', headers={}, body=body)
        monkeypatch.setattr(OAuthChannel, 'build_upstream_request', build)
    return cfg['search']


@pytest.mark.parametrize('kind,shape,expected_code', [
    ('xai', 'no_search', 'search_not_executed'),
    ('xai', 'malformed', 'search_backend_error'),
    ('xai', 'failed', 'search_upstream_error'),
    ('xai', 'incomplete', 'search_upstream_error'),
    ('openai', 'missing_results', 'invalid_search_response'),
    ('anthropic', 'tool_error', 'search_upstream_error'),
])
async def test_s4_paid_oauth_failures_settle_observed_usage(env, monkeypatch, kind, shape, expected_code):
    _oauth_env(monkeypatch, kind)
    model = {'xai': 'grok-4.6', 'openai': 'gpt-5.5', 'anthropic': 'claude-sonnet-4-6'}[kind]
    data = {'model': model, 'usage': {'input_tokens': 100, 'output_tokens': 20}, 'private_detail': 'private-upstream-body'}
    if kind == 'xai':
        data['usage']['cost_in_usd_ticks'] = 123456789
        data['output'] = [42] if shape == 'malformed' else []
        typ = 'response.' + (shape if shape in ('failed', 'incomplete') else 'completed')
        upstream = httpx.Response(200, text='data: ' + json.dumps({'type': typ, 'response': data}) + '\n\n')
    else:
        if kind == 'anthropic':
            data['content'] = [{'type': 'web_search_tool_result', 'content': {'type': 'web_search_tool_result_error', 'error_code': 'unavailable'}}]
        upstream = httpx.Response(200, json=data)
    monkeypatch.setattr(service.network, 'async_client', lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(lambda req: upstream)))
    with pytest.raises(service.SearchError) as exc:
        await service.search({'query': 'audit search'})
    assert exc.value.code == expected_code
    assert 'private-' not in str(exc.value)
    assert '_billing_body' not in vars(exc.value)
    rows = log_db.search_call_entries(0)
    assert len(rows) == 1
    row = rows[0]
    assert row['status'] == 'error' and row['error_code'] == expected_code
    assert row['usage_observed'] == 1 and row['input_tokens'] == 100 and row['output_tokens'] == 20
    assert row['model'] == model
    if kind == 'xai':
        assert row['cost_source'] == 'actual' and row['cost_ticks'] == 123456789


async def test_s4_retry_keeps_attempt_billing_separate_and_private(env, monkeypatch):
    _oauth_env(monkeypatch, 'xai', attempts=2)
    sent = []
    def upstream(request):
        sent.append(request)
        data = {'model': 'grok-4.6', 'usage': {'input_tokens': len(sent)*100, 'output_tokens': 20,
                'cost_in_usd_ticks': len(sent)*123},
                'output': [] if len(sent) == 1 else [{'type': 'web_search_call', 'action': {'sources': []}}]}
        return httpx.Response(200, text='data: ' + json.dumps({'type': 'response.completed', 'response': data}) + '\n\n')
    monkeypatch.setattr(service.network, 'async_client', lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    result = await service.search({'query': 'audit search'})
    rows = sorted(log_db.search_call_entries(0), key=lambda row: row['attempt_no'])
    assert [row['status'] for row in rows] == ['error', 'success']
    assert [row['input_tokens'] for row in rows] == [100, 200]
    assert [row['cost_ticks'] for row in rows] == [123, 246]
    assert '_billing_body' not in result and '_upstream_model' not in result
    visible = web._model_visible_search_result(result, 'search')
    assert not {'usage', 'attempts', 'provider', 'backend_id'} & visible.keys()


async def test_s4_unobserved_failure_stays_unpriced(env, monkeypatch):
    _oauth_env(monkeypatch, 'xai')
    monkeypatch.setattr(service.network, 'async_client', lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, text='data: {"type":"response.completed","response":{"output":[]}}\n\n'))))
    with pytest.raises(service.SearchError):
        await service.search({'query': 'audit search'})
    row = log_db.search_call_entries(0)[0]
    assert row['usage_observed'] == 0 and row['cost_ticks'] is None and row['cost_source'] == 'unpriced'


@pytest.mark.parametrize('protocol', ['chat', 'responses', 'anthropic'])
@pytest.mark.parametrize('bad', [
    {'blocked_domains': 42}, {'blocked_domains': 'example.com'}, {'blocked_domains': False},
    {'excluded_domains': [{}]}, {'allowed_domains': [[]]}, {'filters': []},
    {'filters': {'blocked_domains': [42]}},
])
async def test_s5_bad_domains_are_tool_errors_and_model_can_recover(managed, monkeypatch, protocol, bad):
    sent, executed = [], []
    async def execute(calls, **kwargs):
        executed.extend(calls)
        return [web.LocalToolResult(call.id, 'valid search result') for call in calls]
    async def invoke(body):
        sent.append(copy.deepcopy(body))
        if len(sent) == 1:
            return JSONResponse(answer(protocol, [('bad', 'web_search', {'query': 'docs', **bad})]))
        if len(sent) == 2:
            assert 'invalid_input:' in json.dumps(policy._history(body, protocol))
            assert executed == []
            return JSONResponse(answer(protocol, [('good', 'web_search', {'query': 'docs', 'blocked_domains': ['example.com']})]))
        return JSONResponse(answer(protocol))
    monkeypatch.setattr(web, 'execute_local_tool_calls', execute)
    stream = policy.stream(req(protocol), protocol, invoke)
    raw = b''.join([chunk async for chunk in stream.body_iterator])
    assert b'answer' in raw and len(sent) == 3
    assert [call.id for call in executed] == ['good']


@pytest.mark.parametrize('bad', [{'blocked_domains': 42}, {'filters': []}, {'filters': {'excluded_domains': [{}]}}])
def test_s5_invalid_declaration_is_rejected_before_dispatch(managed, bad):
    with pytest.raises(GuardError) as exc:
        policy.compile_request({'tools': [{'type': 'web_search', **bad}]}, 'responses')
    assert exc.value.status == 400


@pytest.mark.parametrize('kind', ['anysearch', 'tavily', 'exa', 'openai'])
@pytest.mark.parametrize('size', [999, 1000, 1001])
async def test_s6_extract_limit_reports_actual_truncation(monkeypatch, kind, size):
    if kind == 'openai':
        cfg = _oauth_env(monkeypatch, kind)
    else:
        cfg = {**service.DEFAULTS, 'backends': [{**service.default_backend(kind), 'apiKeys': ['audit-key']}], 'maxAttempts': 1}
        monkeypatch.setattr(service, 'settings', lambda: cfg)
    cfg['maxFetchChars'] = 1000
    content = '字' * size
    upstream = {'anysearch': {'code': 0, 'data': {'content': content}},
                'tavily': {'results': [{'raw_content': content}]},
                'exa': {'results': [{'text': content}]},
                'openai': {'output': content}}[kind]
    monkeypatch.setattr(service.network, 'async_client', lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=upstream))))
    result = await web.execute_local_tool_call(web.LocalToolCall('f', 'web_fetch', {
        'url': 'https://example.com/page', '_known_urls': ['https://example.com/page']}))
    obj = json.loads(result.content)
    assert not result.is_error and obj['content'] == content[:1000]
    assert obj['url'] == 'https://example.com/page'
    if size > 1000:
        assert obj['truncated'] is True
        assert obj['warnings'] == ['Content truncated to 1000 characters by Parrot (maxFetchChars).']
    else:
        assert 'truncated' not in obj and 'warnings' not in obj


def test_s7_today_and_month_api_windows_with_enum_and_strings(api, control, monkeypatch):
    real_datetime = datetime.datetime
    bjt = datetime.timezone(datetime.timedelta(hours=8))
    fixed = real_datetime(2026, 9, 19, 17, 30, tzinfo=bjt)
    class Clock(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)
    monkeypatch.setattr(datetime, 'datetime', Clock)
    seen = []
    def observe(context, **kwargs):
        seen.append(kwargs['since_ts'])
        return []
    monkeypatch.setattr(control, 'logs', observe)
    monkeypatch.setattr(control, 'stats', observe)
    client, headers, _, _ = api
    today = real_datetime(2026, 9, 19, tzinfo=bjt).timestamp()
    month = real_datetime(2026, 9, 1, tzinfo=bjt).timestamp()
    for endpoint in ('logs', 'stats'):
        for query, expected in (('?period=today', today), ('?period=month', month), ('', month)):
            response = client.get('/api/management/v1/search/' + endpoint + query, headers=headers)
            assert response.status_code == 200
            assert seen[-1] == expected
    assert SearchControl._since('today') == today and SearchControl._since('month') == month


@pytest.mark.parametrize('move,selected,expected,new_selection', [
    ('up', [2, 3], 'bcad', [1, 2]), ('down', [2, 3], 'adbc', [3, 4]),
    ('top', [2, 4], 'bdac', [1, 2]), ('bottom', [1, 3], 'bdac', [3, 4]),
    ('up', [1], 'abcd', [1]), ('down', [4], 'abcd', [4]),
])
def test_s8_multi_selection_moves_one_step_and_keeps_identity(monkeypatch, move, selected, expected, new_selection):
    monkeypatch.setattr(search_menu, '_view', lambda chat: {'backends': [{'id': ident} for ident in 'abcd']})
    monkeypatch.setattr(search_menu, '_sort_draft', {42: {'ids': list('abcd'), 'selected': selected}})
    ids, actual = search_menu._sort_apply(42, move)
    assert ids == list(expected) and actual == new_selection
    assert {ids[i-1] for i in actual} == {list('abcd')[i-1] for i in selected}


def test_s8_tg_repeated_moves_and_save_follow_same_source(tg, control, ctx):
    for ident in 'abcd':
        control.add_backend(ctx, {'type': 'tavily', 'id': ident, 'name': ident})
    callback('srch:sort')
    callback('srch:sortpick:0:2')
    for action, ids, selection in [('down', 'acbd', [3]), ('down', 'acdb', [4]), ('up', 'acbd', [3])]:
        callback('srch:sortmove:0:' + action)
        assert search_menu._sort_draft[42] == {'ids': list(ids), 'selected': selection}
        assert f'{selection[0]}. b ✅' in latest(tg)['text']
    callback('srch:sortsave:0')
    assert [item['id'] for item in control.get(ctx)['backends']][:4] == list('acbd')
