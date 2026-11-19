"""Real Google decoder -> runtime -> failover regressions (no upstream network)."""
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi.responses import JSONResponse

from src.tests.test_antigravity_package_a import _patch_failover_runtime, FailoverChannel
from src.tests.test_stream_error_details import terminal_details
from src.providers.antigravity_errors import parse_antigravity_error
from src.providers.antigravity_codec import GeminiStreamToResponses
from src.protocols.runtime import AttemptResult, prepare_non_stream_response
from src.transports.http_runtime import prepare_stream_response_start, read_http_error_response

SECRET = 'private prompt https://accounts.google.com/signin/continue?plt=PRIVATE_VALIDATION_TOKEN'


class Context:
    async def __aexit__(self, *args):
        pass


def payload(status='PERMISSION_DENIED', reason='VALIDATION_REQUIRED', code=403):
    return {'error': {'code': code, 'status': status, 'message': SECRET,
        'details': [{'@type': 'type.googleapis.com/google.rpc.ErrorInfo', 'reason': reason,
                     'domain': 'cloudcode-pa.googleapis.com', 'metadata': {'validation_url': SECRET}}]}}


async def decoded_result(raw, stream, ingress="responses"):
    ch = FailoverChannel('ag:first')
    ctx = {'antigravity_stream': GeminiStreamToResponses(model='m')}
    if ingress != 'responses':
        ctx['response_translator'] = 'anthropic_to_responses' if ingress == 'anthropic' else 'chat_to_responses'
    if stream:
        wire = ('data: ' + json.dumps(raw) + '\n\n').encode()
        start = await prepare_stream_response_start(Context(), httpx.Response(200, content=wire), ch,
            dynamic_map=None, connect_ms=0, deadline_ts=time.time()+30, first_byte_timeout=10,
            idle_timeout=10, ingress_protocol=ingress, translator_ctx=ctx)
        result = start.error
    else:
        prepared = await prepare_non_stream_response(ch, json.dumps(raw).encode(),
            dynamic_map=None, connect_ms=0, total_ms=0, translator_ctx=ctx)
        result = prepared.error
    assert result is not None
    return result


async def run(monkeypatch, first, *, stream=False, all_fail=False, effects=None, ingress="responses"):
    from src import failover, scheduler
    _patch_failover_runtime(monkeypatch, failover)
    monkeypatch.setattr(failover.cooldown, 'record_error', lambda *a, **k: None)
    monkeypatch.setattr(failover.finalize_policy, 'apply_error_health_effects',
                        lambda *a, **k: effects.append(k) if effects is not None else None)
    async def forbidden(*a, **k):
        pytest.fail('structured rejection must not refresh token')
    monkeypatch.setattr(failover.oauth_manager, 'force_refresh', forbidden)
    calls = []
    channels = [FailoverChannel('ag:first'), FailoverChannel('ag:second')]
    async def attempt(ch, *a, **k):
        calls.append(ch.key)
        return first if len(calls) == 1 or all_fail else AttemptResult(
            outcome='success', success=True, http_status=200, response=JSONResponse({'ok': True}))
    monkeypatch.setattr(failover, '_try_channel', attempt)
    route = scheduler.ScheduleResult([(c, 'm') for c in channels], None, False)
    response = await failover.run_failover(route, {'model': 'm'}, 'probe', 'k', 'ip', stream,
                                         time.time(), ingress_protocol=ingress)
    return calls, response


@pytest.mark.asyncio
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('status,reason,code', [
    ('PERMISSION_DENIED', 'VALIDATION_REQUIRED', 403),
    ('RESOURCE_EXHAUSTED', 'UNOBSERVED_REASON', 429),
    ('INVALID_ARGUMENT', '', 400),
])
async def test_http_final_diagnostic_is_safe_and_raw_records_survive(monkeypatch, stream, status, reason, code):
    raw = json.dumps(payload(status, reason, code))
    result = await read_http_error_response(Context(), httpx.Response(code, content=raw), connect_ms=0)
    calls, response = await run(monkeypatch, result, stream=stream, all_fail=True)
    assert SECRET not in response.body.decode()
    assert status in response.body.decode()
    assert SECRET in result.error_detail and SECRET in result.full_response_text
    assert len(calls) == (1 if status == 'INVALID_ARGUMENT' else 2)


@pytest.mark.asyncio
@pytest.mark.parametrize('code', [400, 422])
@pytest.mark.parametrize('status', ['', 'FAILED_PRECONDITION'])
async def test_numeric_only_rejection_keeps_healthy_alternate(monkeypatch, code, status):
    raw = json.dumps({'error': {'code': code, 'status': status, 'message': 'account precondition'}})
    assert not parse_antigravity_error(raw, http_status=code)['stop_failover']
    calls, response = await run(monkeypatch, AttemptResult(outcome='http_error', http_status=code,
                                                         error_detail=raw, full_response_text=raw))
    assert calls == ['ag:first', 'ag:second'] and response.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('all_fail', [False, True])
@pytest.mark.parametrize('status,reason,code,kind', [
    ('INVALID_ARGUMENT', '', 400, 'request_rejected'),
    ('OUT_OF_RANGE', '', 400, 'request_rejected'),
    ('PERMISSION_DENIED', 'VALIDATION_REQUIRED', 403, 'validation_required'),
    ('VALIDATION_REQUIRED', 'QUOTA_EXHAUSTED', 429, 'validation_required'),
])
@pytest.mark.parametrize("ingress", ["responses", "chat", "anthropic"])
async def test_2xx_decoder_preserves_rejection(monkeypatch, stream, all_fail, status, reason, code, kind, ingress):
    from src.failover import _antigravity_error_evidence
    result = await decoded_result(payload(status, reason, code), stream, ingress)
    raw, signal = _antigravity_error_evidence(result)
    assert signal['classification'] == kind
    assert signal['diagnostic']['http_status'] == 200
    effects = []
    calls, response = await run(monkeypatch, result, stream=stream, all_fail=all_fail, effects=effects, ingress=ingress)
    assert calls == (['ag:first'] if kind == 'request_rejected' else ['ag:first', 'ag:second'])
    assert response.status_code == (400 if kind == 'request_rejected' else 403 if all_fail else 200)
    assert SECRET not in response.body.decode()
    assert not effects  # Neither request faults nor verification challenges score accounts.
    assert raw['error']['message'] == SECRET  # Internal evidence was not sanitized away.


@pytest.mark.asyncio
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('reason,expected_calls,credit_retry', [
    ('QUOTA_EXHAUSTED', ['ag:first', 'ag:first'], True),
    ('RATE_LIMIT_EXCEEDED', ['ag:first', 'ag:first'], False),
    ('INSUFFICIENT_G1_CREDITS_BALANCE', ['ag:first', 'ag:second'], False),
    ('UNOBSERVED_REASON', ['ag:first', 'ag:second'], False),
])
async def test_2xx_structured_quota_policy(monkeypatch, stream, reason, expected_calls, credit_retry):
    from src import failover, scheduler
    raw = payload('RESOURCE_EXHAUSTED', reason, 429)
    raw['error']['details'].append({'@type': 'type.googleapis.com/google.rpc.RetryInfo', 'retryDelay': '1s'})
    result = await decoded_result(raw, stream)
    _patch_failover_runtime(monkeypatch, failover)
    monkeypatch.setattr(failover.oauth_manager, 'antigravity_credits_usable', lambda _: True)
    disabled, pauses, sleeps, calls, bodies = [], [], [], [], []
    monkeypatch.setattr(failover.oauth_manager, 'set_disabled_by_quota', lambda *a, **k: disabled.append(a))
    monkeypatch.setattr(failover.cooldown, 'record_error', lambda *a, **k: pauses.append((a, k)))
    from src.antigravity import quota
    monkeypatch.setattr(quota, '_record_model_quota_exhausted_until', lambda *a: None)
    async def sleep(delay): sleeps.append(delay)
    monkeypatch.setattr(failover.asyncio, 'sleep', sleep)
    async def attempt(ch, model, body, *a, **k):
        calls.append(ch.key)
        bodies.append(body)
        if len(calls) == 1:
            return result
        return AttemptResult(outcome='success', success=True, http_status=200, response=JSONResponse({'ok': True}))
    monkeypatch.setattr(failover, '_try_channel', attempt)
    channels = [FailoverChannel('ag:first'), FailoverChannel('ag:second')]
    route = scheduler.ScheduleResult([(c, 'm') for c in channels], None, False)
    response = await failover.run_failover(route, {'model':'m'}, 'r', 'k', 'ip', stream, time.time(), ingress_protocol='responses')
    assert response.status_code == 200 and calls == expected_calls
    assert bool(bodies[-1].get('_parrot_antigravity_use_credits')) is credit_retry
    assert bool(disabled) is (reason == 'INSUFFICIENT_G1_CREDITS_BALANCE')
    assert sleeps == ([1.0] if reason == 'RATE_LIMIT_EXCEEDED' else [])
    assert bool(pauses) is (reason == 'UNOBSERVED_REASON')


def test_client_shaped_context_and_nested_metadata_are_not_trusted():
    from src.failover import _antigravity_error_evidence
    fake = {'error': {'status': 'INVALID_ARGUMENT'}}
    result = AttemptResult(outcome='http_error', error_detail='ordinary failure', translator_ctx={
        'antigravity_error': fake,
        'antigravity_stream': {'google_error': fake},
    })
    assert _antigravity_error_evidence(result)[1]['classification'] == 'unknown'
    decoder = GeminiStreamToResponses(model='m')
    decoder.feed(json.dumps({'provider_metadata': {'gemini': fake}, 'candidates': []}).encode())
    assert decoder.google_error is None


@pytest.mark.asyncio
async def test_200_unknown_error_without_decoder_remains_recoverable(monkeypatch):
    result = AttemptResult(outcome='http_error', http_status=200, error_detail='unknown body', full_response_text='unknown body')
    calls, response = await run(monkeypatch, result)
    assert calls == ['ag:first', 'ag:second'] and response.status_code == 200


def test_other_provider_final_message_unchanged():
    from src.failover import _structured_attempt_error
    result = AttemptResult(outcome='http_error', http_status=400, error_detail='ordinary provider message')
    assert _structured_attempt_error(result, 1, SimpleNamespace(provider='other'))['message'] == 'ordinary provider message'


@pytest.mark.asyncio
@pytest.mark.parametrize('ingress', ['responses', 'anthropic', 'chat'])
@pytest.mark.parametrize('known_status', [False, True])
async def test_committed_stream_terminal_is_safe_and_does_not_replay(monkeypatch, ingress, known_status):
    from src import failover
    _patch_failover_runtime(monkeypatch, failover)
    logs = []
    monkeypatch.setattr(failover.log_db, 'finish_error', lambda *a, **k: logs.append((a, k)))
    monkeypatch.setattr(failover.finalize_policy, 'apply_error_health_effects', lambda *a, **k: None)
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield ('data: ' + json.dumps({'response': {'candidates': [{'content': {'parts': [{'text': 'hello'}]}}]}}) + '\n\n').encode()
            failure = payload('INVALID_ARGUMENT' if known_status else '', '', 400 if known_status else SECRET)
            failure['candidates'] = [{'finishReason': 'OTHER', 'finishMessage': SECRET}]
            failure['promptFeedback'] = {'blockReason': 'SAFETY', 'blockReasonMessage': SECRET}
            yield ('data: ' + json.dumps(failure) + '\n\n').encode()
    ctx = {'antigravity_stream': GeminiStreamToResponses(model='m')}
    if ingress != 'responses':
        ctx['response_translator'] = 'anthropic_to_responses' if ingress == 'anthropic' else 'chat_to_responses'
    ch = FailoverChannel('ag:first')
    response = httpx.Response(200, stream=Stream())
    result = await failover._consume_stream(Context(), response, ch, 'm', None, 0,
        time.time(), time.time()+30, 10, 10, 'r', [], 'k', 'ip', None, 0, 0,
        ingress_protocol=ingress, translator_ctx=ctx)
    assert result.stream_started
    calls, downstream = await run(monkeypatch, result, stream=True)
    monkeypatch.setattr(failover.log_db, 'finish_error', lambda *a, **k: logs.append((a, k)))
    output = b''.join([chunk async for chunk in downstream.body_iterator]).decode()
    assert calls == ['ag:first'] and 'hello' in output
    assert SECRET not in output
    assert ('INVALID_ARGUMENT' if known_status else 'antigravity_upstream_error') in output
    # Public sanitization must not erase internal evidence.
    assert ctx['antigravity_stream'].google_error.error['message'] == SECRET
    assert any(SECRET in entry[1].get('response_body', '') for entry in logs)

@pytest.mark.asyncio
@pytest.mark.parametrize('ingress', ['responses', 'anthropic', 'chat'])
@pytest.mark.parametrize('ending,code', [('eof', 'upstream_stream_incomplete'), ('thought_eof', 'upstream_stream_incomplete'), ('timeout', 'timeout'), ('invalid', 'invalid_upstream_json')])
async def test_local_stream_failure_diagnostic(monkeypatch, ingress, ending, code):
    from src import failover
    _patch_failover_runtime(monkeypatch, failover)
    monkeypatch.setattr(failover.finalize_policy, 'apply_error_health_effects', lambda *a, **k: None)
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            if ending == 'thought_eof':
                # Same frame shape as the controlled replay: two thought-only
                # frames, then usage-only EOF; no finishReason/error/tool call.
                for text, tokens in [('synthetic thought one', 59), ('synthetic thought two', 1021)]:
                    frame = {'response': {
                        'candidates': [{'content': {'parts': [{'thought': True, 'text': text}]}}],
                        'usageMetadata': {'promptTokenCount': 315707, 'thoughtsTokenCount': tokens,
                                          'totalTokenCount': 315707 + tokens},
                    }, 'private_metadata': SECRET}
                    yield ('data: ' + json.dumps(frame) + '\n\n').encode()
                yield ('data: ' + json.dumps({'response': {'usageMetadata': {
                    'promptTokenCount': 321032, 'thoughtsTokenCount': 1021, 'totalTokenCount': 322053,
                }}, 'private_metadata': SECRET}) + '\n\n').encode()
                return
            yield b'data: {"candidates":[{"content":{"parts":[{"text":"hello"}]}}]}\n\n'
            if ending == 'timeout':
                raise httpx.ReadTimeout(SECRET)
            if ending == 'invalid':
                yield ('data: invalid ' + SECRET + '\n\n').encode()
    ctx = {'antigravity_stream': GeminiStreamToResponses(model='m')}
    if ingress != 'responses':
        ctx['response_translator'] = 'anthropic_to_responses' if ingress == 'anthropic' else 'chat_to_responses'
    if ingress == 'anthropic':
        # Match AntigravityOAuthChannel's production translator context.
        ctx['antigravity_anthropic_reasoning_bridge'] = True
    result = await failover._consume_stream(Context(), httpx.Response(200, stream=Stream()),
        FailoverChannel('ag:first'), 'm', None, 0, time.time(), time.time()+30,
        10, 10, 'r', [], 'k', 'ip', None, 0, 0, ingress_protocol=ingress, translator_ctx=ctx)
    calls, response = await run(monkeypatch, result, stream=True)
    output = b''.join([c async for c in response.body_iterator]).decode()
    assert calls == ['ag:first']
    assert code in output and 'rejected' not in output and SECRET not in output
    assert 'http_status' in output and '200' in output
    assert 'http_status\\": null' not in output
    root = terminal_details(output)['root_cause']
    assert root['code'] == code and SECRET not in root['message']
    assert (root['retryable'], root['retry_scope']) == (True, 'request')
    if ending == 'thought_eof':
        assert result.stream_started
        decoder = ctx['antigravity_stream']
        assert decoder.finished and decoder.local_error_code == 'upstream_stream_incomplete'
        assert decoder.usage['output_tokens'] == 1021
        assert decoder.google_error is None
        assert ('response.failed' if ingress == 'responses' else 'error') in output


@pytest.mark.parametrize('outcome,expected', [('guard_error', 'model availability'), ('candidate_guard', 'thinking-tier compatibility'), ('transform_error', 'could not prepare'), ('read_timeout', 'timed out'), ('connect_error', 'connection failed')])
def test_local_attempt_has_safe_meaning(outcome, expected):
    from src.failover import _structured_attempt_error
    error = _structured_attempt_error(AttemptResult(outcome=outcome, error_detail=SECRET), 1, FailoverChannel('ag:first'))
    assert expected in error['message'] and SECRET not in error['message']
    assert 'null' not in error['message'] and 'rejected' not in error['message']


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.asyncio
async def test_missing_finish_precommit_is_not_rejection(monkeypatch, stream):
    from src.failover import _antigravity_error_evidence
    result = await decoded_result({'candidates': []}, stream)
    _, signal = _antigravity_error_evidence(result)
    from src.providers.antigravity_errors import safe_error_message
    assert signal['public_code'] == 'upstream_stream_incomplete'
    assert 'rejected' not in safe_error_message(signal)
    assert not signal['stop_failover']
    calls, response = await run(monkeypatch, result, stream=stream, all_fail=True)
    text = response.body.decode()
    assert calls == ['ag:first', 'ag:second']
    assert 'upstream_stream_incomplete' in text and 'rejected' not in text


def test_max_tokens_remains_incomplete():
    decoder = GeminiStreamToResponses(model='m')
    response = json.loads(decoder.feed(json.dumps({'candidates': [{'finishReason': 'MAX_TOKENS'}]}).encode()))
    assert response['status'] == 'incomplete' and response['error'] is None
    assert response['incomplete_details']['reason'] == 'max_output_tokens'
    assert decoder.local_error_code is None


@pytest.mark.parametrize('outcome,code,phrase', [
    ('connect_error', 'transport_error', 'connection failed'),
    ('read_timeout', 'timeout', 'timed out'),
])
def test_decoder_before_response_does_not_invent_http_status(outcome, code, phrase):
    from src.failover import _antigravity_error_evidence, _structured_attempt_error
    result = AttemptResult(outcome=outcome, error_detail=SECRET, translator_ctx={
        'antigravity_stream': GeminiStreamToResponses(model='m'),
    })
    _, signal = _antigravity_error_evidence(result)
    assert signal['public_code'] == code
    assert 'http_status' not in signal['diagnostic']
    error = _structured_attempt_error(result, 1, FailoverChannel('ag:first'))
    assert error['code'] == code and phrase in error['message']
    assert 'http_status' not in error['message'] and SECRET not in error['message']


@pytest.mark.parametrize('guard_code,phrase', [
    ('antigravity_thinking_unsupported', 'thinking tier or reasoning effort'),
    ('antigravity_model_unavailable', 'unavailable or changed before dispatch'),
    ('antigravity_unsupported_input', 'input items or server-side state fields'),
    ('antigravity_unsupported_tool', 'tool definitions or tool_choice modes'),
    ('antigravity_unsupported_tool_result', 'tool results only support text and supported images'),
    ('antigravity_invalid_function_call', 'without a valid function name'),
    ('antigravity_remote_image_failed', 'download the remote input image'),
    ('antigravity_invalid_image', 'rejected the input image URL or format'),
])
def test_typed_antigravity_guard_code_surfaces_specific_safe_message(guard_code, phrase):
    from src.failover import _structured_attempt_error
    result = AttemptResult(
        outcome='candidate_guard',
        http_status=400,
        error_code=guard_code,
        error_detail=SECRET,
    )
    error = _structured_attempt_error(result, 1, FailoverChannel('ag:first'))
    assert error['code'] == guard_code
    assert phrase in error['message']
    assert 'thinking-tier compatibility' not in error['message']
    assert SECRET not in error['message']


def test_unknown_guard_error_code_falls_back_safely_without_leaking_secret():
    from src.failover import _structured_attempt_error
    result = AttemptResult(
        outcome='candidate_guard',
        http_status=400,
        error_code=SECRET,
        error_detail=SECRET,
    )
    error = _structured_attempt_error(result, 1, FailoverChannel('ag:first'))
    assert error['code'] == 'candidate_guard'
    assert 'thinking-tier compatibility' in error['message']
    assert SECRET not in error['message']

