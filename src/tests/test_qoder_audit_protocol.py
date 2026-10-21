"""Regression assertions for the independent prestart protocol audit (synthetic IO only)."""
import asyncio
import copy
import json
import threading

import httpx
import pytest

from src import config, oauth_manager as om
from src.oauth.qoder import auth, billing, catalog, common, device_identity, runtime
from src.providers.qoder_codec import QoderStream, Splitter
from src.tests.test_qoder_auth import stored
from src.tests.test_qoder_channel import env, call, request_body, frame
from src.tests import test_protocol_fake_upstreams as fake


def delta(value):
    return frame({'choices': [{'index': 0, 'delta': value}]})


def decode(raw):
    decoder = QoderStream('fixture')
    output = decoder.feed(raw) + decoder.finish()
    return [json.loads(line[6:]) for line in output.decode().splitlines()
            if line.startswith('data: ') and line != 'data: [DONE]']


@pytest.mark.parametrize('ingress', ['chat', 'responses', 'anthropic'])
@pytest.mark.parametrize('stream', [False, True])
async def test_clean_eof_remains_officially_compatible(env, ingress, stream):
    # Supplementary official evidence accepts clean EOF after normal output.
    response, text, seen = await call(env, ingress, request_body(ingress, stream), lambda req:
        httpx.Response(200, stream=fake.ChunkedByteStream([delta({'content': 'truncated'})]),
                       headers={'content-type': 'text/event-stream'}))
    row = env[0]['log_db']._get_conn().execute('SELECT status FROM request_log ORDER BY id DESC LIMIT 1').fetchone()
    assert row["status"] == "success" and response.status_code == 200


@pytest.mark.parametrize('quoted', ['`<tool_call>{"name":"erase","arguments":{"path":"example"}}</tool_call>`',
    '```json\n<tool_call>{"name":"erase","arguments":{"path":"example"}}</tool_call>\n```'])
def test_valid_quoted_example_is_prose(quoted):
    parts = Splitter().feed(quoted)
    assert not any(kind == 'call' for kind, value in parts)
    assert ''.join(value for kind, value in parts if kind == 'text') == quoted


def test_parallel_native_calls_preserve_indexes():
    first = [{'index': i, 'id': f'c{i}', 'function': {'name': f'f{i}', 'arguments': '{"x":'}} for i in (0, 1)]
    second = [{'index': i, 'function': {'arguments': f'{i}}}'}} for i in (0, 1)]
    output = decode(delta({'tool_calls': first}) + delta({'tool_calls': second}) + frame('[DONE]'))
    calls = [t for f in output if f.get('choices') for t in f['choices'][0]['delta'].get('tool_calls', [])]
    assert [t['index'] for t in calls] == [0, 1, 0, 1]
    assert [t['function'].get('name') for t in calls] == ['f0', 'f1', None, None]


@pytest.mark.parametrize('code', ['112', '10605', '406'])
async def test_business_error_in_200_body_is_not_hidden(env, code):
    raw = [delta({'content': 'partial'}), frame({'code': code, 'message': 'failure'}), frame('[DONE]')]
    response, text, seen = await call(env, 'chat', request_body('chat', False), lambda req:
        httpx.Response(200, stream=fake.ChunkedByteStream(raw), headers={'content-type': 'text/event-stream'}))
    assert response.status_code != 200
    assert code in text


@pytest.mark.parametrize('pending_pair', ['job', 'device'])
def test_other_pair_rotation_preserves_pending(stored, monkeypatch, pending_pair):
    spent = {'job': [], 'device': []}
    def job(a, **kw):
        spent['job'].append(a['refresh_token'])
        return {'access_token': 'jt2', 'refresh_token': 'jrt2', 'expired': '2099-01-01T00:00:00Z'}
    def device(a, **kw):
        spent['device'].append(a['device_refresh_token'])
        return {'device_token': 'dt2', 'device_refresh_token': 'drt2'}
    monkeypatch.setattr(auth, 'refresh_sync', job)
    monkeypatch.setattr(auth, 'rotate_device_sync', device)
    original = om._save_token_fields
    failures = [True]
    def save(*args, **kwargs):
        if failures:
            failures.pop()
            raise OSError('synthetic disk failure')
        return original(*args, **kwargs)
    monkeypatch.setattr(om, '_save_token_fields', save)
    def rotate(which):
        if which == 'job':
            return om._refresh_sync_locked(stored, True)
        return runtime.rotate_device(stored, 'dt')
    with pytest.raises(common.QoderError, match='save_failed_retry_save'):
        rotate(pending_pair)
    rotate('device' if pending_pair == 'job' else 'job')
    rotate(pending_pair)
    assert len(spent[pending_pair]) == 1
    assert om.get_account(stored)["access_token"] == "jt2"
    assert om.get_account(stored)["device_token"] == "dt2"


@pytest.mark.parametrize('which', ['job', 'device'])
def test_ambiguous_refresh_never_replays_old_token(stored, monkeypatch, which):
    sent = []
    def lost(*args, **kwargs):
        sent.append(kwargs['body']['refresh_token'])
        # Server accepted request and sent HTTP200 headers, but response body was lost.
        raise common.QoderError('refresh', kind='network', response_status=200,
                                transport_kind='read_timeout', transport_exception='ReadTimeout')
    monkeypatch.setattr(common, 'openapi', lost)
    def rotate():
        return om._refresh_sync_locked(stored, True) if which == 'job' else runtime.rotate_device(stored, 'dt')
    for i in range(2):
        if i and which == 'device':
            runtime._device_states[stored].retry_at = 0
        with pytest.raises(common.QoderError):
            rotate()
    assert len(sent) == 1
    assert runtime.refresh_status(om.get_account(stored), stored, which) == "unknown"


@pytest.mark.parametrize('error', [common.QoderError('device_identity', kind='umid_unavailable'),
    common.QoderError('device_identity', kind='identity_store_unavailable')])
async def test_identity_error_no_business_send_or_disable(env, monkeypatch, error):
    monkeypatch.delenv('PARROT_NO_REFRESH', raising=False)
    async def refresh(*args, **kwargs):
        pytest.fail('identity failure must not consume OAuth refresh')
    monkeypatch.setattr(om, 'force_refresh', refresh)
    from src.tests.conftest import _ORIG_TO_THREAD
    monkeypatch.setattr(asyncio, 'to_thread', _ORIG_TO_THREAD)
    count = []
    main_thread = threading.get_ident()
    def identity(*args, **kwargs):
        assert threading.get_ident() != main_thread
        count.append(1)
        raise error
    monkeypatch.setattr(device_identity, 'headers', identity)
    response, text, seen = await call(env, 'chat', request_body('chat', False), lambda req: pytest.fail('must not send'))
    assert len(count) >= 1 and not seen and response.status_code != 200 and not config.get()['oauthAccounts'][0].get('disabled_reason')


def test_campaign_business_error_is_not_inactive(monkeypatch):
    a = {'realm': 'cn', 'device_token': 'synthetic'}
    monkeypatch.setattr(common, 'request', lambda *a, **kw: (200, {'code': '112', 'message': 'failure'}))
    with pytest.raises(common.QoderError):
        billing.checkin_sync(a)

@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('framing', ['http', 'sse_single', 'sse_split'])
async def test_auth_error_recovery_actual_path(env, monkeypatch, stream, framing):
    monkeypatch.delenv('PARROT_NO_REFRESH', raising=False)
    refreshes = []
    async def refresh(*args, **kwargs):
        refreshes.append(1)
        return 'fixture-job'
    monkeypatch.setattr(om, 'force_refresh', refresh)
    error_frame = frame({'code': '105', 'message': 'login expired'})
    def answer(req):
        if framing == 'http':
            return httpx.Response(401, json={'code': '105', 'message': 'login expired'})
        pieces = [error_frame] if framing == 'sse_single' else [error_frame[:8], error_frame[8:]]
        return httpx.Response(200, stream=fake.ChunkedByteStream(pieces), headers={'content-type': 'text/event-stream'})
    response, text, seen = await call(env, 'chat', request_body('chat', stream), answer)
    assert len(refreshes) == 1 and len(seen) == 2
    assert not config.get()['oauthAccounts'][0].get('disabled_reason')


@pytest.mark.parametrize('code,status', [('777',403), ('112',403), ('10605',403), ('406',400)])
async def test_non_auth_does_not_refresh(env, monkeypatch, code, status):
    monkeypatch.delenv('PARROT_NO_REFRESH', raising=False)
    async def refresh(*args, **kwargs):
        pytest.fail('non-auth errors must not consume refresh')
    monkeypatch.setattr(om, 'force_refresh', refresh)
    response, text, seen = await call(env, 'chat', request_body('chat', False), lambda req:
        httpx.Response(status, json={'code': code, 'message': 'refused'}))
    assert response.status_code != 200 and not config.get()['oauthAccounts'][0].get('disabled_reason')


async def test_identity_success_and_models_get(env, monkeypatch):
    from src.tests.conftest import _ORIG_TO_THREAD
    from src.tests.test_qoder_channel import frames, LISTING
    monkeypatch.setattr(asyncio, 'to_thread', _ORIG_TO_THREAD)
    main = threading.get_ident()
    identities = []
    def identity(a, **kw):
        identities.append((threading.get_ident(), kw.get('account_key')))
        return {'Cosy-MachineId': a['machine_id'], 'Cosy-MachineToken': 'issued',
                'Cosy-MachineType': 'type', 'Cosy-MachineCode': 'code', 'Cosy-MachineOS': 'x86_64_linux'}
    monkeypatch.setattr(device_identity, 'headers', identity)
    response, text, seen = await call(env, 'chat', request_body('chat', False), lambda req:
        httpx.Response(200, stream=fake.ChunkedByteStream(frames()), headers={'content-type': 'text/event-stream'}))
    assert len(identities) == len(seen) == 1 and identities[0][0] != main
    requests = []
    def request(url, **kw):
        if kw.get("prepare_headers"):
            kw["headers"] = kw.pop("prepare_headers")()
        requests.append((url, kw))
        return 200, LISTING
    monkeypatch.setattr(common, 'request', request)
    a = om.get_account(env[1].account_key)
    records = catalog.fetch_models_sync(a, account_key=env[1].account_key)
    assert len(identities) == 2 and len(requests) == 1
    url, kw = requests[0]
    assert url.endswith('/algo/api/v2/model/list') and '?' not in url and 'content' not in kw and 'body' not in kw
    assert kw['headers']['Cosy-MachineId'] == a['machine_id'] and records[0]['qoderConfig']

@pytest.mark.parametrize('ingress', ['chat', 'responses', 'anthropic'])
async def test_tool_history_conversion(env, ingress):
    tool = 'fixture_tool'
    body = request_body(ingress, False, tool=True)
    if ingress == 'chat':
        body['messages'] += [
            {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'c1', 'type': 'function', 'function': {'name': tool, 'arguments': '{"x":1}'}}]},
            {'role': 'tool', 'tool_call_id': 'c1', 'content': 'result'}]
    elif ingress == 'responses':
        body['input'] = [
            {'role': 'user', 'content': 'ping'},
            {'type': 'function_call', 'call_id': 'c1', 'name': tool, 'arguments': '{"x":1}'},
            {'type': 'function_call_output', 'call_id': 'c1', 'output': 'result'}]
    else:
        body['messages'] += [
            {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'c1', 'name': tool, 'input': {'x': 1}}]},
            {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'c1', 'content': 'result'}]}]
    req = await env[1].build_upstream_request(body, 'ultimate', ingress_protocol=ingress)
    sent = json.loads(req.body)
    prior, result = sent['messages'][-2:]
    assert prior['tool_calls'][0]['id'] == result['tool_call_id'] == 'c1'
    assert prior['tool_calls'][0]['function'] == {'name': tool, 'arguments': '{"x":1}'}
    assert result['role'] == 'tool' and result['content'] == 'result'


async def test_unsupported_request_parameters_are_rejected(env):
    from src.tests.test_qoder_channel import frames
    body = dict(request_body('chat', False), response_format={'type': 'json_schema', 'json_schema': {
        'name': 'answer', 'strict': True, 'schema': {'type': 'object', 'properties': {'ok': {'type': 'boolean'}}, 'required': ['ok'], 'additionalProperties': False}}},
        temperature=0, top_p=0.1, stop=['Qoder'])
    response, text, seen = await call(env, 'chat', body, lambda req:
        httpx.Response(200, stream=fake.ChunkedByteStream(frames()), headers={'content-type': 'text/event-stream'}))
    assert response.status_code == 400 and not seen


def test_pat_manual_checkin_ensures_job_token(stored, monkeypatch):
    from src.oauth.qoder import actions
    config.update(lambda c: c['oauthAccounts'][0].update(qoder_auth_method='pat', qoder_pat='pt-synthetic',
        device_token='', device_refresh_token='', expired='2000-01-01T00:00:00Z'))
    monkeypatch.setattr(device_identity, 'headers', lambda *a, **kw: {'Cosy-MachineToken': 'issued', 'Cosy-MachineType': 'type'})
    refreshes = []
    def refresh(*a, **kw):
        refreshes.append(1)
        return {'access_token': 'jt2', 'refresh_token': 'jrt2', 'expired': '2099-01-01T00:00:00Z'}
    monkeypatch.setattr(auth, 'refresh_sync', refresh)
    requests = []
    def request(*a, **kw):
        requests.append(kw['headers']['Authorization'])
        return 200, {'campaigns': []}
    monkeypatch.setattr(common, 'request', request)
    result = actions.checkin_now(stored)
    assert result['status'] == 'inactive' and refreshes == [1] and requests == ['Bearer jt2']


async def test_sse_event_error_after_output_is_not_hidden(env):
    raw = [delta({'content': 'partial'}), b'event:error\ndata:{"message":"upstream failed","localizedMessage":"upstream failed"}\n\n']
    response, text, seen = await call(env, 'chat', request_body('chat', False), lambda req:
        httpx.Response(200, stream=fake.ChunkedByteStream(raw), headers={'content-type': 'text/event-stream'}))
    assert response.status_code != 200

@pytest.mark.parametrize('code', ['112','406','10605'])
@pytest.mark.parametrize('split', [False, True])
async def test_sse_mapped_status_preserved(env, code, split):
    raw = b'data:' + json.dumps({'statusCodeValue': 403, 'body': json.dumps({'code': code, 'message': 'refused'})}).encode() + b'\n\n'
    chunks = [raw[:8], raw[8:]] if split else [raw]
    response, text, seen = await call(env, 'chat', request_body('chat', False), lambda req:
        httpx.Response(200, stream=fake.ChunkedByteStream(chunks), headers={'content-type': 'text/event-stream'}))
    error = json.loads(text)['error']
    root = error.get('details', {}).get('root_cause', error)
    assert root.get('status', response.status_code) == {'112': 429, '406': 400, '10605': 503}[code]
    assert code in text

@pytest.mark.parametrize('ingress', ['chat', 'responses', 'anthropic'])
@pytest.mark.parametrize('code', ['105', '112', '406', '10605'])
@pytest.mark.parametrize('split', [False, True])
async def test_committed_errors_never_replay_and_preserve_status(env, monkeypatch, ingress, code, split):
    monkeypatch.delenv('PARROT_NO_REFRESH', raising=False)
    async def forbidden(*a, **kw):
        pytest.fail('a committed stream must never refresh/replay')
    monkeypatch.setattr(om, 'force_refresh', forbidden)
    raw = frame({'code': code, 'message': 'secret-payload-must-not-escape'})
    pieces = [delta({'content': 'visible'})] + ([raw[:8], raw[8:]] if split else [raw])
    response, text, seen = await call(env, ingress, request_body(ingress, True), lambda req:
        httpx.Response(200, stream=fake.ChunkedByteStream(pieces), headers={'content-type': 'text/event-stream'}))
    assert response.status_code == 200 and len(seen) == 1 and 'visible' in text
    assert code in text and 'secret-payload-must-not-escape' not in text
    row = env[0]['log_db']._get_conn().execute('SELECT status,http_status FROM request_log ORDER BY id DESC LIMIT 1').fetchone()
    assert row['status'] != 'success'
    assert row['http_status'] == {'105': 401, '112': 429, '406': 400, '10605': 503}[code]
    assert not config.get()['oauthAccounts'][0].get('disabled_reason')


@pytest.mark.parametrize('ingress', ['chat', 'responses', 'anthropic'])
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('tail', [b'data:{broken', b'data:{broken}\n\n', b'event:finish\ndata:forceFinish:firstPack\n\n'])
async def test_corrupt_or_forced_tail_is_never_success(env, ingress, stream, tail):
    response, text, _ = await call(env, ingress, request_body(ingress, stream), lambda req:
        httpx.Response(200, stream=fake.ChunkedByteStream([delta({'content': 'visible'}), tail]), headers={'content-type': 'text/event-stream'}))
    row = env[0]['log_db']._get_conn().execute('SELECT status FROM request_log ORDER BY id DESC LIMIT 1').fetchone()
    assert row['status'] != 'success'


def test_official_finish_and_usage_after_finish_reason():
    output = decode(delta({'content': 'x'}) + frame({'choices': [{'delta': {}, 'finish_reason': 'stop'}]})
                    + frame({'choices': [], 'usage': {'prompt_tokens': 7, 'completion_tokens': 3}})
                    + b'event:finish\ndata:{"totalDuration":123}\n\n')
    assert output[-1]['usage']['total_tokens'] == 10
    assert output[-1]['choices'][0]['finish_reason'] == 'stop'


def test_incomplete_native_tool_is_not_success():
    output = decode(delta({'tool_calls': [{'index': 0, 'function': {'name': 'f', 'arguments': '{'}}]}))
    assert output[-1]['error']['code'] == 'qoder_tool_call_unreadable'


@pytest.mark.parametrize('ingress', ['chat', 'responses', 'anthropic'])
async def test_temperature_is_mapped_to_official_parameters(env, ingress):
    body = dict(request_body(ingress, False), temperature=0.1)
    request = await env[1].build_upstream_request(body, 'ultimate', ingress_protocol=ingress)
    assert json.loads(request.body)['parameters']['temperature'] == 0.1


@pytest.mark.parametrize('field,value', [('stop', ['x']), ('top_p', .1), ('response_format', {'type': 'json_object'}),
    ('tool_choice', 'required'), ('tool_choice', {'type': 'function', 'function': {'name': 'f'}})])
async def test_each_unsupported_semantic_is_candidate_guard(env, field, value):
    from src.openai.transform.guard import GuardError
    with pytest.raises(GuardError) as e:
        await env[1].build_upstream_request(dict(request_body('chat', False), **{field: value}), 'ultimate', ingress_protocol='chat')
    assert e.value.scope == 'candidate'


@pytest.mark.parametrize('pair', ['job', 'device'])
def test_durable_intent_failure_prevents_consumption(stored, monkeypatch, pair):
    from src import state_db
    monkeypatch.setattr(state_db, 'qoder_refresh_save', lambda *a: (_ for _ in ()).throw(OSError('disk')))
    monkeypatch.setattr(auth, 'refresh_sync', lambda *a, **kw: pytest.fail('no RT before durable intent'))
    monkeypatch.setattr(auth, 'rotate_device_sync', lambda *a, **kw: pytest.fail('no RT before durable intent'))
    with pytest.raises(common.QoderError, match='refresh_store_unavailable'):
        runtime._rotate(om.get_account(stored), stored, pair)


@pytest.mark.parametrize('pair', ['job', 'device'])
def test_durable_intent_without_candidate_after_restart_never_replays(stored, monkeypatch, pair):
    from src import state_db
    a = om.get_account(stored)
    state_db.qoder_refresh_save(runtime._journal_key(a, stored, pair), {'phase': 'intent'})
    runtime.forget(stored)
    state_db.close(); state_db.init()
    monkeypatch.setattr(auth, 'refresh_sync', lambda *a, **kw: pytest.fail('no replay'))
    monkeypatch.setattr(auth, 'rotate_device_sync', lambda *a, **kw: pytest.fail('no replay'))
    with pytest.raises(common.QoderError, match='refresh_outcome_unknown_reauthorize') as e:
        runtime._rotate(a, stored, pair)
    assert not e.value.auth_error


@pytest.mark.parametrize('status,code', [(429, '429'), (401, '401'), (403, '105'), (200, '105')])
def test_cn_refusals_never_mark_device_ready(monkeypatch, status, code):
    from src.oauth.qoder import device_profile
    flow = auth.start_login_sync(profile=device_profile.generate(), realm='cn')
    calls = []
    def request(a, path, **kw):
        calls.append(path)
        if path.endswith('poll'):
            return 200, {'token': 'dt', 'refresh_token': 'drt', 'user_id': 'u'}
        return status, {'code': code}
    monkeypatch.setattr(common, 'openapi', request)
    monkeypatch.setattr(catalog, 'fetch_models_sync', lambda *a: pytest.fail('explicit refusal is not fallback'))
    for _ in range(2):
        with pytest.raises(common.QoderError):
            auth.poll_login_sync(flow)
    assert flow['status'] == 'identity_pending' and flow['device']['token'] == 'dt'
    assert sum(p.endswith('poll') for p in calls) == 1


def test_cn_failed_capability_validation_retains_device_candidate(monkeypatch):
    from src.oauth.qoder import device_profile
    flow = auth.start_login_sync(profile=device_profile.generate(), realm='cn')
    monkeypatch.setattr(common, 'openapi', lambda a, path, **kw: (200, {'token': 'dt', 'user_id': 'u'}) if path.endswith('poll') else (404, {}))
    monkeypatch.setattr(catalog, 'fetch_models_sync', lambda *a: (_ for _ in ()).throw(common.QoderError('models', kind='network')))
    with pytest.raises(common.QoderError):
        auth.poll_login_sync(flow)
    assert flow['status'] == 'identity_pending' and 'chat' not in flow and flow['device']['token'] == 'dt'


@pytest.mark.parametrize('body', [{}, {'campaigns': None}, {'campaigns': [None]}, {'code': '112'}])
def test_campaign_invalid_objects_are_not_inactive(monkeypatch, body):
    monkeypatch.setattr(common, 'request', lambda *a, **kw: (200, body))
    with pytest.raises(common.QoderError):
        billing.checkin_sync({'realm': 'cn', 'device_token': 'dt'})

@pytest.mark.parametrize('syntax', ['{"name":"erase","arguments":{"x":1}}', '<function=erase><parameter=x>1</parameter></function>'])
@pytest.mark.parametrize('quote', ['`', '```'])
def test_quoted_tools_remain_prose_across_every_byte(syntax, quote):
    text = quote + '<tool_call>' + syntax + '</tool_call>' + quote
    splitter = Splitter()
    result = [item for ch in text for item in splitter.feed(ch)] + splitter.flush()
    assert all(kind == 'text' for kind, _ in result)
    assert ''.join(value for _, value in result) == text


def test_pat_checkin_105_recovers_once(stored, monkeypatch):
    from src.oauth.qoder import actions
    config.update(lambda c: c['oauthAccounts'][0].update(qoder_auth_method='pat', qoder_pat='pt-fixture', device_token='', device_refresh_token='', expired='2099-01-01T00:00:00Z'))
    issued, sent = [], []
    def refresh(*a, **kw):
        issued.append(1)
        return {'access_token': 'jt2', 'refresh_token': 'jrt2', 'expired': '2099-01-01T00:00:00Z'}
    monkeypatch.setattr(auth, 'refresh_sync', refresh)
    def request(*a, **kw):
        sent.append(kw['headers']['Authorization'])
        return (401, {'code': '105'}) if len(sent) == 1 else (200, {'campaigns': []})
    monkeypatch.setattr(common, 'request', request)
    assert actions.checkin_now(stored)['status'] == 'inactive'
    assert issued == [1] and sent == ['Bearer jt', 'Bearer jt2']

@pytest.mark.parametrize('ingress', ['chat', 'responses', 'anthropic'])
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('error', [httpx.ReadError('fixture read error'), httpx.ReadTimeout('fixture read timeout')])
async def test_read_failure_after_output_never_becomes_clean_eof(env, ingress, stream, error):
    class Broken(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield delta({'content': 'partial'})
            raise error
    response, text, seen = await call(env, ingress, request_body(ingress, stream), lambda req:
        httpx.Response(200, stream=Broken(), headers={'content-type': 'text/event-stream'}))
    row = env[0]['log_db']._get_conn().execute('SELECT status FROM request_log ORDER BY id DESC LIMIT 1').fetchone()
    assert row['status'] != 'success' and len(seen) == 1


@pytest.mark.parametrize('ingress', ['chat', 'responses', 'anthropic'])
@pytest.mark.parametrize('stream', [False, True])
async def test_nested_auth_error_before_commit_restores_once(env, monkeypatch, ingress, stream):
    monkeypatch.delenv('PARROT_NO_REFRESH', raising=False)
    renewals = []
    async def refresh(*a, **kw):
        renewals.append(1)
        return 'fixture-job'
    monkeypatch.setattr(om, 'force_refresh', refresh)
    raw = b'event:error\ndata:{"error":{"code":"105","message":"login expired"}}\n\n'
    response, text, seen = await call(env, ingress, request_body(ingress, stream), lambda req:
        httpx.Response(200, stream=fake.ChunkedByteStream([raw[:9], raw[9:]]), headers={'content-type': 'text/event-stream'}))
    assert len(renewals) == 1 and len(seen) == 2


@pytest.mark.parametrize('pair', ['job', 'device'])
def test_durable_candidate_survives_process_memory_loss(stored, monkeypatch, pair):
    from src import state_db
    spent = []
    fields = {'access_token': 'a2', 'refresh_token': 'r2', 'expired': '2099-01-01T00:00:00Z'} if pair == 'job' else {'device_token': 'd2', 'device_refresh_token': 'dr2'}
    def rotate(*a, **kw):
        spent.append(1)
        return fields
    monkeypatch.setattr(auth, 'refresh_sync' if pair == 'job' else 'rotate_device_sync', rotate)
    save = om._save_token_fields
    monkeypatch.setattr(om, '_save_token_fields', lambda *a, **kw: (_ for _ in ()).throw(OSError('disk')))
    with pytest.raises(common.QoderError, match='save_failed_retry_save'):
        runtime._rotate(om.get_account(stored), stored, pair)
    runtime.forget(stored)
    state_db.close(); state_db.init()
    monkeypatch.setattr(om, '_save_token_fields', save)
    runtime._rotate(om.get_account(stored), stored, pair)
    assert spent == [1]
    assert all(om.get_account(stored)[k] == v for k, v in fields.items())
