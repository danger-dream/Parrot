"""Credential-observed OAuth recovery. Only isolated storage and mocked I/O."""
from __future__ import annotations

import asyncio
import copy
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from src.tests import _isolation
_isolation.isolate()
from src import config, oauth_manager as om, oauth_errors, oauth_refresh_failure, state_db
from src.oauth.workbuddy import auth as wb_auth, common as wb_common, runtime as wb_runtime
from src.management_control.oauth import OAuthControl, OAuthProvider
from src.tests.test_workbuddy_lifecycle import context
from src.tests.test_oauth_overwrite_confirmation import _entry


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    saved = copy.deepcopy(config.get())
    monkeypatch.setattr(config, '_reload_callbacks', [])
    config.update(lambda c: c.update(oauthAccounts=[], channels=[], loadBalancing={}))
    state_db.init()
    monkeypatch.delenv('PARROT_NO_REFRESH', raising=False)
    monkeypatch.setattr(om, '_profile_sync', lambda *a, **k: {})
    monkeypatch.setattr(om.reporting, 'refreshed', Mock())
    monkeypatch.setattr(om.reporting, 'refresh_failed', Mock())
    wb_runtime._refresh_states.clear()
    yield
    wb_runtime._refresh_states.clear()
    config.update(lambda c: (c.clear(), c.update(saved)))


def add(provider='claude', name='one'):
    if provider == 'workbuddy':
        entry = wb_auth.normalize_credential(dict(realm='cn', uid=name, access_token='old-at', refresh_token='old-rt'))
    else:
        entry = _entry(provider)
        entry['email'] = name + '@example.test'
        if provider in {'cursor', 'xai'}:
            entry.update(subject=name, sub=name)
        if provider == 'openai':
            entry.update(workspace_id=name, chatgpt_account_id=name,
                         codexDeviceInstallationId=str(uuid.uuid4()))
        if provider == 'antigravity':
            entry['project_id'] = name
    entry.update(access_token='old-at', refresh_token='old-rt', enabled=True, disabled_reason=None,
                 expired=(datetime.now(timezone.utc) + timedelta(hours=2)).isoformat())
    om.add_account(entry)
    key = om.get_account_key(entry)
    return key


def install_refresh(monkeypatch, provider, fn):
    owner = wb_auth if provider == 'workbuddy' else getattr(om, provider + '_provider', None)
    if provider == 'claude':
        monkeypatch.setattr(om, 'mock_mode_enabled', lambda: False)
        monkeypatch.setattr(om, '_do_refresh_http', fn)
    else:
        monkeypatch.setattr(owner, 'refresh_sync', fn)


@pytest.mark.parametrize('provider', ['claude', 'openai', 'xai', 'cursor', 'antigravity', 'workbuddy'])
def test_six_forced_old_401s_share_one_rotation_and_late_old_401_reuses(monkeypatch, provider):
    key = add(provider)
    seen = om.refresh_observation(om.get_account(key))
    gate = threading.Barrier(6)
    calls = []
    def refresh(*a, **kw):
        calls.append(a[0])
        return dict(access_token='fresh-at', refresh_token='fresh-rt', expires_in=3600)
    install_refresh(monkeypatch, provider, refresh)
    def run(_):
        gate.wait(timeout=5)
        return asyncio.run(om.force_refresh(key, observation=seen))
    with ThreadPoolExecutor(max_workers=6) as pool:
        assert list(pool.map(run, range(6))) == ['fresh-at'] * 6
    assert calls == ['old-rt']
    assert asyncio.run(om.force_refresh(key, observation=seen)) == 'fresh-at'
    assert calls == ['old-rt']
    # A rejection actually sent with the NEW credentials is not hidden by time.
    fresh = om.refresh_observation(om.get_account(key))
    asyncio.run(om.force_refresh(key, observation=fresh))
    assert calls == ['old-rt', 'fresh-rt']
    asyncio.run(om.force_refresh(key))  # Explicit admin force still rotates.
    assert calls == ['old-rt', 'fresh-rt', 'fresh-rt']


def test_rotated_rt_with_unchanged_at_still_coalesces(monkeypatch):
    key = add()
    seen = om.refresh_observation(om.get_account(key))
    refresh = Mock(return_value=dict(access_token='old-at', refresh_token='new-rt', expires_in=3600))
    install_refresh(monkeypatch, 'claude', refresh)
    for _ in range(3):
        assert asyncio.run(om.force_refresh(key, observation=seen)) == 'old-at'
    refresh.assert_called_once()
    # A newly sent request really rejects the current token: no time masking.
    asyncio.run(om.force_refresh(key, observation=om.refresh_observation(om.get_account(key))))
    assert refresh.call_count == 2


def test_accounts_do_not_share_refresh_lock(monkeypatch):
    one, two = add(name='one'), add(name='two')
    gate = threading.Barrier(2)
    calls = []
    def refresh(*a, **kw):
        gate.wait(timeout=3)
        calls.append(kw['account_key'])
        return dict(access_token='new', refresh_token='new-rt', expires_in=3600)
    install_refresh(monkeypatch, 'claude', refresh)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda k: asyncio.run(om.force_refresh(k, observation=om.refresh_observation(om.get_account(k)))), [one, two]))
    assert results == ['new', 'new'] and set(calls) == {one, two}


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['claude', 'workbuddy'])
@pytest.mark.parametrize('winner', ['request', 'background'])
async def test_background_expiry_snapshot_racing_request_reuses_new_credentials(monkeypatch, provider, winner):
    key = add(provider)
    config.update(lambda c: c['oauthAccounts'][0].update(expired=(datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()))
    old = copy.deepcopy(om.list_accounts())
    seen = om.refresh_observation(old[0])
    calls = []
    def refresh(*a, **kw):
        calls.append(1)
        return dict(access_token='new', refresh_token='new-rt', expires_in=3600)
    install_refresh(monkeypatch, provider, refresh)
    monkeypatch.setattr(om, 'list_accounts', lambda: old)
    async def usage(*a, **k):
        return {}
    monkeypatch.setattr(om, 'fetch_usage_snapshot', usage)
    if winner == 'request':
        await om.force_refresh(key, observation=seen)
    await om.proactive_refresh_once()
    assert calls == [1]
    assert await om.force_refresh(key, observation=seen) == 'new'
    assert calls == [1]


def test_observation_is_of_wire_token_not_latest_account_snapshot():
    key = add()
    channel = SimpleNamespace(type='oauth', provider='claude', account_key=key, state_key=om.account_state_key(om.get_account(key)))
    request = SimpleNamespace(headers={'Authorization': 'Bearer actually-sent-old'}, translator_ctx={'keep': True})
    om.observe_request_credentials(channel, request)
    seen = request.translator_ctx['oauth_refresh']
    assert request.translator_ctx['keep'] is True
    assert seen['credential_fingerprint'] is None
    assert not om.refresh_observation_matches(om.get_account(key), seen)
    assert 'actually-sent-old' not in str(seen)
    assert asyncio.run(om.force_refresh(key, observation=seen)) == 'old-at'


@pytest.mark.parametrize('change', ['login', 'delete_recreate', 'user'])
def test_late_refresh_failure_cannot_disable_new_credentials_or_override_user(monkeypatch, change):
    key = add()
    old = copy.deepcopy(om.get_account(key))
    request = httpx.Request('POST', 'https://fixture.test/token')
    error = httpx.HTTPStatusError('fixture', request=request, response=httpx.Response(401, request=request))
    def fail(*a, **kw):
        raise error
    install_refresh(monkeypatch, 'claude', fail)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(om.force_refresh(key, observation=om.refresh_observation(old)))
    if change == 'login':
        om.replace_exact_identity(key, dict(old, access_token='login-at', refresh_token='login-rt'))
    elif change == 'delete_recreate':
        om.delete_account(key)
        om.add_account(old)
    else:
        om.set_enabled(key, False, reason='user')
    before = copy.deepcopy(om.get_account(key))
    _, effect = oauth_refresh_failure.apply(key, error, expected_state_key=om.account_state_key(old))
    assert om.get_account(key) == before
    assert effect is None if change != 'user' else effect.code == 'still_paused'


@pytest.mark.parametrize('failure', ['network', 'auth'])
def test_refresh_failure_propagates_and_does_not_return_old_token(monkeypatch, failure):
    key = add()
    old = copy.deepcopy(om.get_account(key))
    request = httpx.Request('POST', 'https://fixture.test/token')
    error = httpx.ReadTimeout('fixture') if failure == 'network' else httpx.HTTPStatusError('fixture', request=request, response=httpx.Response(401, request=request))
    install_refresh(monkeypatch, 'claude', Mock(side_effect=error))
    with pytest.raises(type(error)):
        asyncio.run(om.force_refresh(key, observation=om.refresh_observation(old)))
    assert om.get_account(key) == old
    display, effect = oauth_refresh_failure.apply(key, error)
    assert display.auth_error is (failure == 'auth')
    if failure == 'auth':
        assert effect.code == 'account_paused'
        with pytest.raises(oauth_errors.OAuthRefreshStateError):
            asyncio.run(om.force_refresh(key, observation=om.refresh_observation(old)))
    else:
        assert om.get_account(key) == old


@pytest.mark.parametrize('manual', [False, True])
def test_workbuddy_candidate_loss_persists_relogin_and_preserves_manual_pause(monkeypatch, manual):
    key = add('workbuddy')
    if manual:
        om.set_enabled(key, False, reason='user')
    old = copy.deepcopy(om.get_account(key))
    wb_runtime._refresh_states[key] = wb_runtime._RefreshState(wb_runtime.credential_fingerprint(old),
        pending={'access_token': 'lost', 'refresh_token': 'lost-rt'}, pending_until=time.monotonic()-1)
    upstream = Mock(side_effect=AssertionError('must never replay old RT'))
    monkeypatch.setattr(wb_auth, 'refresh_sync', upstream)
    with pytest.raises(wb_common.WorkBuddyError) as failed:
        asyncio.run(om.force_refresh(key))
    saved = om.get_account(key)
    assert saved['workbuddy_relogin_required'] is True
    import json
    from pathlib import Path
    persisted = json.loads(Path(config.CONFIG_PATH).read_text())
    assert persisted['oauthAccounts'][0]['workbuddy_relogin_required'] is True
    assert saved['enabled'] is False
    assert saved['disabled_reason'] == ('user' if manual else 'auth_error')
    assert OAuthControl.account_authentication_failed(saved)
    from src.telegram.menus import oauth_menu
    from src.notifications import providers
    _, keyboard = oauth_menu._detail_text_and_kb(key, refresh_quota=False, actor_chat_id=42, model_stats=[], stats_loading=True)
    assert keyboard['inline_keyboard'][0][0]['text'] == '🔐 重新登录'
    assert 'reauthorize' in providers.capabilities(saved)
    display = oauth_errors.describe_oauth_error(failed.value, provider='workbuddy')
    assert display.auth_error and not display.retryable and display.status is None
    assert '本地' in display.reason and '并非上游返回 401' in display.reason
    # Losing in-memory pending state (process restart) cannot reopen old RT usage.
    wb_runtime._refresh_states.clear()
    # Reimporting the same unusable old credentials is not a fresh login.
    om.replace_exact_identity(key, old)
    assert om.get_account(key)['workbuddy_relogin_required']
    assert om.get_account(key)['enabled'] is False
    with pytest.raises(wb_common.WorkBuddyError, match='save_candidate_expired_relogin'):
        asyncio.run(om.ensure_valid_token(key))
    upstream.assert_not_called()
    ctl = OAuthControl()
    monkeypatch.setattr(ctl, '_post_save_account_effects', lambda *a, **k: {})
    monkeypatch.setattr(ctl.backend, 'workbuddy_start_login', lambda: dict(status='pending', auth_url='https://fixture.test/login'))
    flow = ctl.start_login_flow(context(), OAuthProvider.WORKBUDDY, relogin_account_id=key)
    monkeypatch.setattr(ctl.backend, 'workbuddy_poll_login', lambda p: p.update(status='ready', entry=dict(old, access_token='login-at', refresh_token='login-rt')))
    assert ctl.poll_login_flow(context(), flow.flow_id, flow.flow_secret).status == 'completed'
    recovered = om.get_account(key)
    assert not recovered.get('workbuddy_relogin_required')
    assert recovered['disabled_reason'] == ('user' if manual else None)
    assert recovered['enabled'] is (not manual)
    assert not ctl.account_authentication_failed(recovered)


def test_workbuddy_save_candidate_retry_never_re_rotates_or_marks_auth_error(monkeypatch):
    key = add('workbuddy')
    seen = om.refresh_observation(om.get_account(key))
    refresh = Mock(return_value=dict(access_token='rotated', refresh_token='rotated-rt'))
    monkeypatch.setattr(wb_auth, 'refresh_sync', refresh)
    save = om._save_token_fields
    monkeypatch.setattr(om, '_save_token_fields', Mock(side_effect=OSError('fixture write failure')))
    for _ in range(2):
        with pytest.raises(wb_common.WorkBuddyError) as failure:
            asyncio.run(om.force_refresh(key, observation=seen))
        error, _ = oauth_refresh_failure.apply(key, failure.value)
        assert not error.auth_error and error.retryable
        assert om.get_account(key)['enabled']
        assert not om.get_account(key).get('workbuddy_relogin_required')
    monkeypatch.setattr(om, '_save_token_fields', save)
    assert asyncio.run(om.force_refresh(key, observation=seen)) == 'rotated'
    refresh.assert_called_once()


@pytest.mark.parametrize('change', ['login', 'delete_recreate'])
def test_old_workbuddy_terminal_cannot_mark_new_account(monkeypatch, change):
    key = add('workbuddy')
    old = copy.deepcopy(om.get_account(key))
    expired = wb_runtime._RefreshState(wb_runtime.credential_fingerprint(old), candidate_expired=True)
    if change == 'login':
        om.replace_exact_identity(key, dict(old, access_token='new', refresh_token='new-rt'))
    else:
        om.delete_account(key)
        om.add_account(old)
    before = copy.deepcopy(om.get_account(key))
    with pytest.raises(wb_common.WorkBuddyError):
        wb_runtime._candidate_lost(old, key, expired)
    assert om.get_account(key) == before
    om.reporting.refresh_failed.assert_not_called()


def test_workbuddy_terminal_persistence_failure_retries_only_local_write(monkeypatch):
    key = add('workbuddy')
    old = om.get_account(key)
    wb_runtime._refresh_states[key] = wb_runtime._RefreshState(wb_runtime.credential_fingerprint(old), candidate_expired=True)
    upstream = Mock(side_effect=AssertionError('old RT must never be replayed'))
    monkeypatch.setattr(wb_auth, 'refresh_sync', upstream)
    save = om.mutate_account_if_unchanged
    monkeypatch.setattr(om, 'mutate_account_if_unchanged', Mock(side_effect=OSError('fixture storage unavailable')))
    with pytest.raises(wb_common.WorkBuddyError):
        asyncio.run(om.force_refresh(key))
    assert wb_runtime.refresh_due(om.get_account(key), key)
    assert om.reporting.refresh_failed.call_args.args[2].code == 'disable_failed'
    monkeypatch.setattr(om, 'mutate_account_if_unchanged', save)
    with pytest.raises(wb_common.WorkBuddyError):
        asyncio.run(om.force_refresh(key))
    assert om.get_account(key)['workbuddy_relogin_required']
    assert not wb_runtime.refresh_due(om.get_account(key), key)
    upstream.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('transport', ['http', 'ws'])
@pytest.mark.parametrize('mode', ['simultaneous', 'late', 'failed', 'auth_failed', 'still_401'])
async def test_real_recovery_entry_coalesces_and_never_retries_failed_refresh(monkeypatch, transport, mode):
    from src.tests import test_openai_responses_ws as wt
    from src.scheduler import ScheduleResult
    from starlette.responses import JSONResponse
    m = wt._import_modules()
    cfg = wt._setup(m)
    cfg['retry'] = {'transient': {'enabled': False}, 'recovery': {'oauthRefresh': True}}
    ch = wt._make_oauth_channel_for_failover(m, name='coalesce@example.test')
    key = ch.account_key
    sent = om.get_account(key)['access_token']
    calls = []
    def refresh(*a, **kw):
        calls.append(1)
        if mode == 'failed':
            raise httpx.ReadTimeout('fixture')
        if mode == 'auth_failed':
            request = httpx.Request('POST', 'https://fixture.test/token')
            raise httpx.HTTPStatusError('fixture', request=request, response=httpx.Response(401, request=request))
        return dict(access_token='new-at', refresh_token='new-rt', expires_in=3600)
    monkeypatch.setattr(om.openai_provider, 'refresh_sync', refresh)
    ready = asyncio.Event()
    dispatched = []
    attempts = {}
    async def attempt(channel, model, body, *args, **kwargs):
        request_id = body['test_request']
        attempts[request_id] = attempts.get(request_id, 0) + 1
        token = om.get_account(key)['access_token']
        req = SimpleNamespace(headers={'Authorization': 'Bearer ' + token}, translator_ctx={})
        om.observe_request_credentials(channel, req)
        if attempts[request_id] == 1:
            dispatched.append(token)
            if mode == 'late':
                await om.force_refresh(key, observation=req.translator_ctx['oauth_refresh'])
            elif mode == 'simultaneous':
                if len(dispatched) == 4:
                    ready.set()
                await asyncio.wait_for(ready.wait(), 5)
            cls = m['responses_ws']._WsAttemptResult if transport == 'ws' else m['failover'].AttemptResult
            return cls(outcome='http_auth_error', http_status=401, translator_ctx=req.translator_ctx)
        assert token == 'new-at'
        if mode == 'still_401':
            cls = m['responses_ws']._WsAttemptResult if transport == 'ws' else m['failover'].AttemptResult
            return cls(outcome='http_auth_error', http_status=401, translator_ctx=req.translator_ctx)
        if transport == 'ws':
            return m['responses_ws']._WsAttemptResult(ok=True, outcome='success')
        return m['failover'].AttemptResult(success=True, outcome='success', http_status=200, response=JSONResponse({'ok': True}))
    if transport == 'ws':
        async def ws_attempt(websocket, *, ch, resolved_model, body, **kw):
            return await attempt(ch, resolved_model, body)
        monkeypatch.setattr(m['responses_ws'], '_try_ws_channel', ws_attempt)
    else:
        monkeypatch.setattr(m['failover'], '_try_channel', attempt)
        monkeypatch.setattr(m['failover'], '_try_openai_oauth_responses_ws_channel', attempt)
    async def run(i):
        rid = 'refresh-observation-' + transport + mode + str(i)
        body = dict(model='test-model', input='hi', stream=False, test_request=rid)
        m['log_db'].insert_pending(rid, '127.0.0.1', 'ws-key', 'test-model', False, 1, 0, {}, body)
        route = ScheduleResult(candidates=[(ch, 'test-model')], saturated=[], affinity_hit=False, fp_query=None, client_key=None)
        if transport == 'ws':
            ws = wt.FakeWebSocket(dict(type='response.create', **body))
            await ws.accept()
            return await m['responses_ws']._run_ws_failover(ws, first_obj=dict(type='response.create', **body),
                schedule_result=route, body=body, request_id=rid, api_key_name='ws-key', client_ip='127.0.0.1',
                start_time=time.time(), start_monotonic=time.monotonic(), fp_query=None)
        return await m['failover'].run_failover(route, body, rid, 'ws-key', '127.0.0.1', is_stream=False,
            start_time=time.time(), start_monotonic=time.monotonic(), ingress_protocol='responses')
    await asyncio.gather(*(run(i) for i in range(4 if mode == 'simultaneous' else 1)))
    assert calls == [1]
    assert all(n == (1 if mode in {'failed', 'auth_failed'} else 2) for n in attempts.values())
    assert all(token == sent for token in dispatched)
    assert om.get_account(key)['access_token'] == (sent if mode in {'failed', 'auth_failed'} else 'new-at')
    if mode == 'auth_failed':
        assert om.get_account(key)['enabled'] is False
        assert om.get_account(key)['disabled_reason'] == 'auth_error'


@pytest.mark.asyncio
@pytest.mark.parametrize('transport', ['http', 'http_over_ws', 'native_ws'])
async def test_actual_transport_401_preserves_wire_credential_observation(monkeypatch, transport):
    from src.tests import test_openai_responses_ws as wt
    from websockets.exceptions import InvalidStatus
    m = wt._import_modules()
    wt._setup(m)
    ch = wt._make_oauth_channel_for_failover(m, name='wire@example.test')
    key = ch.account_key
    body = dict(model='test-model', input='hi', stream=True)
    rid = 'observation-wire-' + transport
    m['log_db'].insert_pending(rid, '127.0.0.1', 'ws-key', 'test-model', True, 1, 0, {}, body)
    sent_headers = []
    def rotated(headers):
        sent_headers.append(headers)
        om._save_token_fields(key, dict(access_token='new-at', refresh_token='new-rt'))
    if transport == 'http':
        async def open_error(**kw):
            rotated(kw['upstream_req'].headers)
            return SimpleNamespace(error=m['failover'].AttemptResult(outcome='http_auth_error', http_status=401))
        monkeypatch.setattr(m['failover'], 'open_response_with_proxy_chain', open_error)
    else:
        async def fail_ws(url, **kw):
            rotated(kw['headers'])
            raise InvalidStatus(SimpleNamespace(status_code=401, reason_phrase='Unauthorized', body=b'{}', headers={}))
        monkeypatch.setattr(m['responses_ws'] if transport == 'native_ws' else m['failover'],
                            '_connect_upstream_ws' if transport == 'native_ws' else '_connect_oauth_responses_ws', fail_ws)
    args = (ch, 'test-model', body, True, time.time()+30, time.time(), None, [], 'ws-key', '127.0.0.1', rid, 0, 0)
    if transport == 'native_ws':
        ws = wt.FakeWebSocket(dict(type='response.create', **body))
        result = await m['responses_ws']._try_ws_channel(ws, first_obj=dict(type='response.create', **body),
            ch=ch, resolved_model='test-model', body=body, allowed_models=None, deadline_ts=time.time()+30,
            start_time=time.time(), request_id=rid, retry_count_so_far=0, affinity_hit=0, api_key_name='ws-key',
            client_ip='127.0.0.1', fp_query=None, client_key=None, retry_attempt_id=None,
            start_monotonic=time.monotonic(), attempt_start_monotonic=time.monotonic(),
            turn_capacity=m['responses_ws']._WsTurnCapacity())
    elif transport == 'http_over_ws':
        result = await m['failover']._try_openai_oauth_responses_ws_channel(*args)
    else:
        result = await m['failover']._try_channel(*args, ingress_protocol='responses')
    assert result.http_status == 401
    seen = result.translator_ctx['oauth_refresh']
    assert sent_headers and next(v for k,v in sent_headers[0].items() if k.lower()=='authorization') == 'Bearer tok'
    assert not om.refresh_observation_matches(om.get_account(key), seen)
    monkeypatch.setattr(om.openai_provider, 'refresh_sync', Mock(side_effect=AssertionError('late 401 must not rotate')))
    assert await om.force_refresh(key, observation=seen) == 'new-at'


@pytest.mark.asyncio
async def test_responses_ws_sse_fallback_401_preserves_observed_credentials(monkeypatch):
    from unittest.mock import AsyncMock
    from src.tests import test_openai_responses_ws as wt
    m = wt._import_modules()
    wt._setup(m)
    ch = wt._make_oauth_channel_for_failover(m, name='sse-observation@example.test')
    ws = m['responses_ws']
    body = dict(model='test-model', input='hi', stream=True)
    rid = 'observed-sse'
    m['log_db'].insert_pending(rid, '127.0.0.1', 'ws-key', 'test-model', True, 1, 0, {}, body)
    async def opened(**kw):
        assert next(v for k, v in kw['upstream_req'].headers.items() if k.lower() == 'authorization') == 'Bearer tok'
        om._save_token_fields(ch.account_key, dict(access_token='new-at', refresh_token='new-rt'))
        return SimpleNamespace(error=None, response=httpx.Response(401, content=b'{}'), ctx=None, proxy_client=None,
                               proxy_bytes={}, proxy_name=None, connect_ms=0, timing=None, round_timeouts=None)
    monkeypatch.setattr(ws, 'open_response_with_proxy_chain', opened)
    monkeypatch.setattr(ws, 'next_nonempty_http_chunk', AsyncMock(side_effect=StopAsyncIteration))
    monkeypatch.setattr(ws, 'finalize_opened_http_response', AsyncMock())
    monkeypatch.setattr(ws, 'close_response_context', AsyncMock())
    result = await ws._try_sse_channel(wt.FakeWebSocket(dict(type='response.create', **body)),
        first_obj=dict(type='response.create', **body), ch=ch, resolved_model='test-model', body=body,
        deadline_ts=time.time()+30, start_time=time.time(), request_id=rid, retry_count_so_far=0, affinity_hit=0,
        api_key_name='ws-key', client_ip='127.0.0.1', fp_query=None, client_key=None, retry_attempt_id=None,
        start_monotonic=time.monotonic(), attempt_start_monotonic=time.monotonic())
    assert result.http_status == 401
    monkeypatch.setattr(om.openai_provider, 'refresh_sync', Mock(side_effect=AssertionError('must reuse')))
    assert await om.force_refresh(ch.account_key, observation=result.translator_ctx['oauth_refresh']) == 'new-at'


def test_workbuddy_lost_candidate_during_remote_call_cannot_mark_new_login(monkeypatch):
    key = add('workbuddy')
    old = copy.deepcopy(om.get_account(key))
    tick = [0]
    monkeypatch.setattr(wb_runtime, 'time', SimpleNamespace(time=time.time, monotonic=lambda: tick[0]))
    def refresh(*a, **kw):
        om.replace_exact_identity(key, dict(old, access_token='login-at', refresh_token='login-rt'))
        tick[0] = 601
        return dict(access_token='stale-rotation', refresh_token='stale-rt')
    monkeypatch.setattr(wb_auth, 'refresh_sync', refresh)
    with pytest.raises(wb_common.WorkBuddyError, match='save_candidate_expired_relogin'):
        asyncio.run(om.force_refresh(key))
    current = om.get_account(key)
    assert current['enabled'] and not current.get('workbuddy_relogin_required')
    assert current['access_token'] == 'login-at'
    om.reporting.refresh_failed.assert_not_called()


@pytest.mark.asyncio
async def test_cursor_401_carries_rejected_session_not_latest_client_or_bridge_token(monkeypatch):
    import json
    from unittest.mock import AsyncMock
    from src.cursor_bridge.client import CursorClient
    from src.cursor_bridge.session import SessionEvent
    from src.cursor_bridge import runtime as bridge
    from src.cursor_bridge.runtime import _Handler
    from src.channel.cursor_oauth_channel import CursorOAuthChannel
    from src.channel.base import UpstreamRequest
    from src import failover, log_db
    key = add('cursor')
    monkeypatch.setattr(bridge, 'base_url', lambda: 'http://127.0.0.1/mock')
    monkeypatch.setattr(bridge, 'bearer_secret', lambda: 'bridge-secret')
    ch = CursorOAuthChannel(om.get_account(key))
    sessions = []
    client = None
    def session_factory(**kw):
        # The provider rejects an already-sent old session after a concurrent
        # refresh updated BOTH the account and the reusable client's AT.
        om._save_token_fields(key, dict(access_token='new-at', refresh_token='new-rt'))
        client.update_access_token('new-at')
        session = SimpleNamespace(access_token=kw['access_token'], close=Mock(),
            next=Mock(return_value=SessionEvent(type='done', error='unauthorized', http_status=401)))
        sessions.append(session)
        return session
    client = CursorClient('old-at', account_key=key, session_factory=session_factory)
    handler = SimpleNamespace(runtime=SimpleNamespace(finish_session=Mock()), _json=Mock())
    _Handler._complete(handler, client, key, 's1', 'owner', 'principal',
                       dict(model='fixture', stream=False, messages=[dict(role='user', content='hi')]), threading.Event())
    args, kwargs = handler._json.call_args
    assert args[0] == 401 and sessions[0].access_token == 'old-at'
    header = kwargs['headers']['X-Parrot-OAuth-Observation']
    seen = json.loads(header)
    assert not om.refresh_observation_matches(om.get_account(key), seen)
    assert 'old-at' not in header and 'new-at' not in header
    req = UpstreamRequest(url='http://127.0.0.1/mock', headers={'Authorization': 'Bearer bridge-secret'}, body=b'{}')
    monkeypatch.setattr(ch, 'build_upstream_request', AsyncMock(return_value=req))
    resp = httpx.Response(401, headers=kwargs['headers'], content=b'{}')
    opened = SimpleNamespace(error=None, ctx=None, response=resp, connect_ms=0, timing=None, proxy_name=None,
                             proxy_bytes=SimpleNamespace(up=0, down=0), proxy_client=None, round_timeouts=None)
    monkeypatch.setattr(failover, 'open_response_with_proxy_chain', AsyncMock(return_value=opened))
    monkeypatch.setattr(failover, 'read_http_error_response', AsyncMock(return_value=failover.AttemptResult(outcome='http_auth_error', http_status=401)))
    async def finalize(opened, result):
        return result
    monkeypatch.setattr(failover, '_finalize_http_attempt', finalize)
    log_db.init()
    body = dict(model='fixture', messages=[dict(role='user', content='hi')])
    rid = 'cursor-observed-token'
    log_db.insert_pending(rid, '127.0.0.1', 'fixture', 'fixture', False, 1, 0, {}, body)
    result = await failover._try_channel(ch, 'fixture', body, False, time.time()+30, time.time(), None,
        [], 'fixture', '127.0.0.1', rid, 0, 0, ingress_protocol='chat')
    assert result.http_status == 401 and result.translator_ctx['oauth_refresh'] == seen
    monkeypatch.setattr(om.cursor_provider, 'refresh_sync', Mock(side_effect=AssertionError('must reuse')))
    assert await om.force_refresh(key, observation=result.translator_ctx['oauth_refresh']) == 'new-at'
