"""Fix boundaries: real dispatch/control/domain code, isolated IO and deterministic clocks."""
import copy
from dataclasses import replace

import httpx
import pytest

from src import config, oauth_manager as om, state_db
from src.notifications import actions, content, delivery, emit, store, subjects
from src.notifications.events import Effect, NotificationEvent
from src.tests.test_notifications import env, controlled, event
from src.tests.test_notifications_review_security import real_control, delivered, click
from src.tests.test_notifications_review_lifecycle import review, quota, restart, rows_for


def receipt():
    return [r for r in store.items('policy').values() if r.get('kind') == 'operation'][-1]


@pytest.mark.parametrize('renewal', ['expiry', '401'])
def test_usage_real_own_renewal_commits_without_rejecting_new_token(env, real_control, monkeypatch, renewal):
    control, ops = real_control
    account = dict(provider='claude', email='renew@example.test', generationId='review-renew-' + renewal,
                   access_token='fixture-old', refresh_token='fixture-rt', enabled=True,
                   expired='2000-01-01T00:00:00Z' if renewal == 'expiry' else '2999-01-01T00:00:00Z')
    env[0]['oauthAccounts'] = [account]
    monkeypatch.setattr(om, 'mock_mode_enabled', lambda: False)
    monkeypatch.delenv('PARROT_NO_REFRESH', raising=False)
    gets, posts = [], []
    def response(url, code, data):
        return httpx.Response(code, json=data, request=httpx.Request('GET', url))
    def get(url, **kw):
        if url == om.OAUTH_PROFILE_URL:
            return response(url, 200, {'organization': {}})
        if url.startswith(om.OAUTH_USAGE_URL + '?'):
            return response(url, 200, {})
        assert url == om.OAUTH_USAGE_URL
        gets.append(kw['headers']['Authorization'])
        if renewal == '401' and len(gets) == 1:
            return response(url, 401, {})
        return response(url, 200, {'five_hour': {'utilization': 2}})
    def post(url, **kw):
        assert url == om.OAUTH_TOKEN_URL
        posts.append(url)
        return response(url, 200, {'access_token': 'fixture-own-renewed', 'refresh_token': 'fixture-new-rt', 'expires_in': 3600})
    monkeypatch.setattr(om.network, 'get_sync', get)
    monkeypatch.setattr(om.network, 'post_sync', post)
    sample = replace(event(), subject=subjects.account(account))
    row = delivered(sample, env)[0]
    click(row, 'refresh_usage')
    ops.jobs[-1]()
    assert receipt()['status'] == 'succeeded'
    assert posts == [om.OAUTH_TOKEN_URL]
    assert gets[-1] == 'Bearer fixture-own-renewed'
    assert om.get_account('claude:renew@example.test')['access_token'] == 'fixture-own-renewed'
    assert state_db.quota_load('claude:renew@example.test')['five_hour_util'] == 2


def test_real_usage_request_inflight_replacement_never_commits(env, real_control, monkeypatch):
    control, ops = real_control
    account = env[0]['oauthAccounts'][0]
    account.update(access_token='fixture-original', refresh_token='fixture-rt', expired='2999-01-01T00:00:00Z', allowCredits=False)
    monkeypatch.setattr(om, 'mock_mode_enabled', lambda: False)
    called = []
    async def wham(token, **kw):
        called.append(token)
        config.update(lambda cfg: cfg['oauthAccounts'][0].update(access_token='fixture-relogin', refresh_token='fixture-relogin-rt'))
        return {'five_hour': {'utilization': 100}, 'openai': {'rate_limit_reset_credits': {'available_count': 0}}}
    monkeypatch.setattr(om.openai_provider, 'fetch_wham_usage', wham)
    row = delivered(event(), env)[0]
    click(row, 'refresh_usage'); ops.jobs[-1]()
    assert called == ['fixture-original']
    assert receipt()['status'] == 'failed'
    assert state_db.quota_load('openai:same@example.test:w1') is None
    assert env[0]['oauthAccounts'][0]['enabled'] is True
    assert env[0]['oauthAccounts'][0]['access_token'] == 'fixture-relogin'


@pytest.mark.parametrize('metadata_status', ['succeeded', 'partial_failed', 'failed', 'unknown'])
def test_metadata_only_retry_never_redoes_catalog_and_reports_status(env, real_control, monkeypatch, metadata_status):
    from src import model_pricing
    from src.notifications import subjects
    _, ops = real_control
    env[0]['oauthAccounts'] = [dict(provider='zhipu', site='bigmodel', credential_mode='oauth', subject='stage',
                                    project_id='p', organization_id='o', generationId='review-stage')]
    calls = []
    async def catalog(*a, **kw):
        pytest.fail('completed catalog stage must not run again')
    async def metadata():
        calls.append('metadata')
        return {'status': metadata_status}
    monkeypatch.setattr(om, 'refresh_account_models', catalog)
    monkeypatch.setattr(model_pricing, 'refresh_metadata_after_model_sync', metadata)
    sample = NotificationEvent('operation.attention', subjects.account(env[0]['oauthAccounts'][0]), Effect('confirmed', 'saved'),
        {'operation': '初始化', 'result': '元数据未完成', 'stages': [{'name': '模型目录', 'status': 'succeeded'}, {'name': '元数据', 'status': 'failed'}]})
    row = delivered(sample, env)[0]; click(row, 'sync_models'); ops.jobs[-1]()
    assert calls == ['metadata']
    assert receipt()['status'] == ('succeeded' if metadata_status == 'succeeded' else 'unknown' if metadata_status == 'unknown' else 'failed')
    assert any('元数据' in p.get('text', '') for m, p in env[3] if m == 'editMessageText')


@pytest.mark.parametrize('quota_action', ['disable_failed', 'resume_failed', 'revision_conflict', 'noop_missing'])
def test_quota_state_failure_keeps_snapshot_stage_success_not_whole_success(env, real_control, monkeypatch, quota_action):
    control, ops = real_control
    async def fetch(key):
        return {'five_hour': {'utilization': 8}}
    monkeypatch.setattr(control.backend, 'fetch_usage_snapshot', fetch)
    monkeypatch.setattr(control.backend, 'evaluate_quota', lambda *a: {'action': quota_action})
    row = delivered(event(), env)[0]; click(row, 'refresh_usage'); ops.jobs[-1]()
    assert state_db.quota_load('openai:same@example.test:w1')['five_hour_util'] == 8
    assert receipt()['status'] == 'failed'
    text = next(p['text'] for m, p in reversed(env[3]) if m == 'editMessageText')
    assert 'failed' in text and '额度快照：已完成' in text and '状态评估/保存：未完成' in text


def test_retry_wait_restart_honors_deadline_and_limit(review, monkeypatch):
    clock = [store.time.time()]
    timers = []
    class Timer:
        def __init__(self, delay, fn, args=()): timers.append((delay, fn, args))
        def start(self): pass
    monkeypatch.setattr(delivery.threading, 'Timer', Timer)
    monkeypatch.setattr(store.time, 'time', lambda: clock[0])
    sends = []
    def tg(method, payload):
        sends.append(payload['chat_id'])
        return {'ok': False, 'error_code': 429, 'parameters': {'retry_after': 10}}
    monkeypatch.setattr(delivery, '_transport', tg)
    sample = quota(review); emit(sample); review.drain()
    assert len(sends) == 2 and {r['state'] for r in rows_for(sample)} == {'retry_wait'}
    restart(review, monkeypatch)
    timers.clear()
    from src import notifier
    for key in store.recover_queue(): notifier.enqueue_delivery(key)
    review.drain()
    assert len(sends) == 2 and len(timers) == 2 and all(0 < t[0] <= 10 for t in timers)
    for turn in range(2):
        clock[0] += 10
        pending = list(timers); timers.clear()
        for _, fn, args in pending: fn(*args)
        review.drain()
    assert len(sends) == 6 and timers == []
    assert {r['state'] for r in rows_for(sample)} == {'failed'}
    assert {r['attempts'] for r in rows_for(sample)} == {3}
    assert store.recover_queue() == []


@pytest.mark.parametrize('state', ['sending', 'retry_wait', 'unknown'])
def test_gc_preserves_inflight_and_safe_retry_at_capacity(review, monkeypatch, state):
    monkeypatch.setattr(store, 'MAX_EVENTS', 2)
    monkeypatch.setattr(store, 'MAX_DELIVERIES', 3)
    first = quota(review); emit(first)
    keys = [row['id'] for row in rows_for(first)]
    for key in keys:
        store.update_delivery(key, state=state, retry_at=store.time.time() + 10)
    for _ in range(4):
        assert not emit(quota(review))
    assert store.get('events', first.event_id)
    assert all(store.get('deliveries', key)['state'] == state for key in keys)
    assert len(store.items('events')) <= 2 and len(store.items('deliveries')) <= 3


def test_no_room_for_outbox_rejects_new_allocation_without_losing_pending(review, monkeypatch):
    monkeypatch.setattr(store, 'MAX_DELIVERIES', 2)
    sample = quota(review); assert emit(sample)
    assert not emit(quota(review, dimension='different'))
    assert len(store.items('deliveries')) == 2
    review.drain()
    assert {r['state'] for r in rows_for(sample)} == {'sent'}


def test_operation_budget_does_not_expire_preferences_or_inflight_summaries(review, monkeypatch):
    monkeypatch.setattr(store, 'MAX_OPERATIONS', 2)
    now = store.time.time()
    pref = f'recipient:11:{review.subject.generation}:quota'
    store.mutate('policy', lambda rows: rows.update({pref: {'disabled': True, 'at': now - store.RETENTION * 2},
        'operation:running': {'kind': 'operation', 'status': 'running', 'at': now - 4},
        'operation:old': {'kind': 'operation', 'status': 'succeeded', 'at': now - 3}}))
    actions._record_operation(quota(review).record(), 'refresh_usage', 11, {'id': 'new', 'status': 'succeeded'})
    assert store.get('policy', 'operation:running')['status'] == 'running'
    assert store.get('policy', 'operation:old') is None and store.get('policy', pref)['disabled']
    assert emit(quota(review)); review.drain()
    assert {p['chat_id'] for m, p in review.calls if m == 'sendMessage'} == {22}


@pytest.mark.parametrize('status', ['unknown', 'running', 'queued', 'partial_failed'])
def test_completed_stage_contract_rejects_nonterminal_success(review, status):
    sample = NotificationEvent('operation.completed', review.subject, Effect('confirmed', 'saved'),
        {'operation': '初始化', 'result': '已完成', 'stages': [{'name': '元数据', 'status': status}]})
    with pytest.raises(ValueError): sample.record()


@pytest.mark.parametrize('field,value', [('scope_kind', 'unregistered'), ('reset_certainty', 'guaranteed')])
def test_metric_semantic_contract_rejects_unregistered_fields(review, field, value):
    sample = replace(quota(review), facts={'metrics': [{'metric_id': 'r', 'kind': 'requests', field: value}]})
    with pytest.raises(ValueError): sample.record()


def test_metric_reset_certainty_and_zero_are_truthful(review):
    sample = replace(quota(review), facts={'metrics': [{'metric_id': 'calls', 'kind': 'requests', 'remaining': 0,
        'unit': '次', 'reset_at': '2026-10-18T00:00:00Z', 'reset_certainty': 'estimated'}]})
    text = content.render(sample.record(), detail=True)
    assert '剩余 0 次' in text and '预计 2026-10-18 08:00 重置' in text
    assert '上游给出的重置时间' not in text


def test_recovery_can_replace_unsubmitted_fault_at_exact_event_capacity(review, monkeypatch):
    monkeypatch.setattr(store, 'MAX_EVENTS', 1)
    first = quota(review); assert emit(first)
    recovered = quota(review, recovered=True); assert emit(recovered)
    review.drain()
    assert len(store.items('events')) <= 1
    assert len(review.calls) == 2 and all('额度已恢复' in p['text'] for m, p in review.calls)


@pytest.mark.parametrize('result', [{'status': 'succeeded'}, {'status': 'succeeded', 'quota_action': 'resumed'}])
def test_workbuddy_old_success_never_clears_new_generation_or_emits_new_recovery(env, monkeypatch, result):
    from src.oauth.workbuddy import actions as wb
    account = dict(provider='workbuddy', realm='cn', uid='boundary', generationId='wb-before')
    env[0]['oauthAccounts'] = [account]
    old = subjects.account(account)
    env[0]['oauthAccounts'] = [{**account, 'generationId': 'wb-after'}]
    current = subjects.account(env[0]['oauthAccounts'][0])
    sample = NotificationEvent('account.action.attention', current, Effect('unknown', 'action_unknown'),
        {'operation': '签到', 'result': 'unknown', 'error_code': 'timeout'}, scope='operation', scope_ref='checkin', dimension=wb.business_date())
    emit(sample)
    before = copy.deepcopy(store.items('incidents'))
    wb._notify_auto_result(om.get_account_key(account), result, observation=(old, 1))
    assert store.items('incidents') == before
    assert len(store.items('events')) == 1


def test_workbuddy_older_success_cannot_clear_newer_failure_same_generation(env):
    from src.oauth.workbuddy import actions as wb
    account = dict(provider='workbuddy', realm='cn', uid='boundary', generationId='wb-order')
    env[0]['oauthAccounts'] = [account]
    subject = subjects.account(account)
    sample = NotificationEvent('account.action.attention', subject, Effect('unknown', 'action_unknown'),
        {'operation': '签到', 'result': 'unknown', 'error_code': 'timeout'}, scope='operation', scope_ref='checkin', dimension=wb.business_date(), version=5)
    emit(sample)
    wb._notify_auto_result(om.get_account_key(account), {'status': 'succeeded'}, observation=(subject, 4))
    assert next(iter(store.items('incidents').values()))['active'] is True


def test_worker_reconciles_disk_outage_without_resending_unknown(review, monkeypatch):
    sample = quota(review); emit(sample)
    timers = []
    class Timer:
        def __init__(self, delay, fn, args=()): timers.append((fn, args))
        def start(self): pass
    monkeypatch.setattr(delivery.threading, 'Timer', Timer)
    write = review.db.write_snapshot
    def failed(*args): raise OSError('temporary fixture disk unavailable')
    monkeypatch.setattr(review.db, 'write_snapshot', failed)
    review.drain()
    assert not review.calls and len(timers) == 2
    monkeypatch.setattr(review.db, 'write_snapshot', write)
    for fn, args in timers: fn(*args)
    assert {r['state'] for r in rows_for(sample)} == {'failed'}
    assert emit(quota(review)); review.drain()
    assert len(review.calls) == 2


@pytest.mark.parametrize('action_result', ['noop_unknown', 'quota_unknown_keep_disabled', 'quota_stale_keep_disabled'])
def test_unknown_evaluation_retains_unknown_not_success(env, real_control, monkeypatch, action_result):
    control, ops = real_control
    async def fetch(key): return {}
    monkeypatch.setattr(control.backend, 'fetch_usage_snapshot', fetch)
    monkeypatch.setattr(control.backend, 'evaluate_quota', lambda *a: {'action': action_result})
    row = delivered(event(), env)[0]; click(row, 'refresh_usage'); ops.jobs[-1]()
    assert receipt()['status'] == 'unknown'


def test_restart_queue_full_preserves_durable_safe_retry(review, monkeypatch):
    from src import notifier
    sample = quota(review); emit(sample)
    keys = [r['id'] for r in rows_for(sample)]
    for key in keys:
        store.update_delivery(key, state='retry_wait', retry_at=store.time.time() + 10, attempts=1)
    timers = []
    class Timer:
        def __init__(self, delay, fn, args=()): timers.append((delay, fn, args))
        def start(self): pass
    monkeypatch.setattr(delivery.threading, 'Timer', Timer)
    monkeypatch.setattr(notifier, 'enqueue_delivery', lambda key: False)
    delivery.install(delivery.transport(), recover=True)
    assert len(timers) == 2
    assert all(store.get('deliveries', key)['state'] == 'retry_wait' for key in keys)
    assert all(store.get('deliveries', key)['attempts'] == 1 for key in keys)
