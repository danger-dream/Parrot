"""Independent offline review reproductions; assertions express required behavior.

Run ONLY through src/tests/isolated_pytest.py. No production IO or remote IO.
"""
import copy
from dataclasses import replace
import json
import queue
import threading
from types import SimpleNamespace

import pytest

from src import config, notifier, state_db
from src.state_store import StateStore
from src.notifications import emit, store, delivery, content, subjects, operations
from src.notifications.events import NotificationEvent, Effect


class DrainDone(BaseException):
    pass


@pytest.fixture
def review(tmp_path, monkeypatch):
    cfg = copy.deepcopy(config.DEFAULT_CONFIG)
    cfg['telegram'] = {'adminIds': [11, 22]}
    cfg['oauthAccounts'] = [{'provider': 'openai', 'email': 'review@example.test',
                            'workspace_id': 'review', 'generationId': 'review-generation',
                            'enabled': True, 'label': 'Review <&>'}]
    monkeypatch.setattr(config, 'get', lambda: cfg)
    paths = (str(tmp_path / 'runtime.json'), str(tmp_path / 'durable.json'))
    db = StateStore(*paths)
    db.start()
    monkeypatch.setattr(state_db, '_store', db)
    q = queue.Queue(maxsize=1000)
    monkeypatch.setattr(notifier, '_queue', q)
    # Keep deterministic scheduling, but use the REAL enqueue_delivery and worker.
    monkeypatch.setattr(notifier, '_ensure_worker', lambda: None)
    calls = []
    def tg(method, payload):
        calls.append((method, copy.deepcopy(payload)))
        return {'ok': True, 'result': {'message_id': 100 + len(calls)}}
    monkeypatch.setattr(delivery, '_transport', tg)
    def drain():
        original_get = q.get
        def get():
            try:
                return original_get(block=False)
            except queue.Empty:
                raise DrainDone()
        q.get = get
        try:
            with pytest.raises(DrainDone):
                notifier._worker_loop()
        finally:
            q.get = original_get
        assert q.unfinished_tasks == 0
    env = SimpleNamespace(cfg=cfg, db=db, queue=q, calls=calls, drain=drain,
                          paths=paths, subject=subjects.account(cfg['oauthAccounts'][0]))
    yield env
    env.db.close()


def quota(env, *, recovered=False, **kw):
    return NotificationEvent('account.quota.recovered' if recovered else 'account.quota.blocked',
        env.subject, Effect('confirmed', 'account_resumed' if recovered else 'account_paused'),
        {'windows': ['5h']}, **kw)


def rows_for(event):
    return [r for r in store.items('deliveries').values() if r['event_id'] == event.event_id]


def restart(env, monkeypatch):
    env.db.close()
    env.db = StateStore(*env.paths)
    env.db.start()
    monkeypatch.setattr(state_db, '_store', env.db)


def test_review_real_queue_transport_receipts_and_restart_refs(review, monkeypatch):
    """Positive control: the real worker persists independent protocol receipts."""
    event = quota(review)
    assert emit(event)
    review.drain()
    rows = rows_for(event)
    assert {r['state'] for r in rows} == {'sent'}
    assert len({r['message_id'] for r in rows}) == 2
    refs = store.items('refs')
    assert refs and all(len(('nt:v1:' + k).encode()) <= 64 for k in refs)
    restart(review, monkeypatch)
    assert store.items('refs') == refs
    assert all(store.get('deliveries', r['id'])['message_id'] == r['message_id'] for r in rows)
    assert not emit(quota(review, source='ws'))
    assert not emit(quota(review, version=1))
    assert not review.queue.qsize()


def test_review_network_unknown_is_not_automatically_replayed(review, monkeypatch):
    monkeypatch.setattr(delivery, '_transport', lambda *a: None)
    event = quota(review)
    assert emit(event)
    review.drain()
    assert {r['state'] for r in rows_for(event)} == {'unknown'}
    restart(review, monkeypatch)
    assert store.recover_queue() == []
    assert not emit(quota(review))


def test_review_emit_does_not_wait_for_transport(review, monkeypatch):
    entered, release, emitted = threading.Event(), threading.Event(), threading.Event()
    def blocked_transport(*args):
        entered.set()
        assert release.wait(10)
        return {'ok': True, 'result': {'message_id': 123}}
    monkeypatch.setattr(delivery, '_transport', blocked_transport)
    stop = object()
    original_get = review.queue.get
    def get():
        item = original_get()
        if item is stop:
            review.queue.task_done()
            raise DrainDone()
        return item
    monkeypatch.setattr(review.queue, 'get', get)
    def worker():
        try:
            notifier._worker_loop()
        except DrainDone:
            pass
    w = threading.Thread(target=worker)
    w.start()
    producer = threading.Thread(target=lambda: (emit(quota(review)), emitted.set()))
    producer.start()
    try:
        assert entered.wait(5)
        assert emitted.wait(5), 'emit waited for a blocked transport'
    finally:
        release.set()
        producer.join(10)
        review.queue.put(stop)
        w.join(10)
    assert not w.is_alive() and not producer.is_alive()


def test_review_active_incident_capacity_still_allows_recovery(review, monkeypatch):
    monkeypatch.setattr(store, 'MAX_INCIDENTS', 2)
    first = quota(review, dimension='one')
    assert emit(first)
    assert emit(quota(review, dimension='two'))
    review.drain()
    recovered = quota(review, recovered=True, dimension='one')
    assert emit(recovered), 'full active-incident cap rejects recovery of an EXISTING incident'
    assert not store.get('incidents', store.incident_key(first.record()))['active']


def test_review_delivery_capacity_cannot_deadlock_gc(review):
    event = quota(review)
    assert emit(event)
    review.drain()
    original = rows_for(event)[0]
    # Compactly seed a reachable state: 2,667 recent repeated observations and
    # six recipients reach 16,000 deliveries before the 3,000-event cap. Avoid
    # thousands of snapshot writes just to prepare this boundary.
    def seed(t):
        template = t['notification_events'][event.event_id]
        for n in range(2667):
            eid = f'capacity-event-{n}'
            t['notification_events'][eid] = {**template, 'event_id': eid, 'version': n + 2}
            for recipient in ([11, 22, 33, 44, 55, 66] if n < 2666 else [11, 22]):
                did = f'capacity-{n}-{recipient}'
                t['notification_deliveries'][did] = {**original, 'id': did,
                    'event_id': eid, 'recipient': recipient, 'chat_id': recipient, 'state': 'suppressed'}
        old = t['notification_incidents'][original['incident_id']]
        t['notification_incidents'][original['incident_id']] = {**old, 'latest': eid, 'version': 2668}
    review.db._mutate_many(store.DOMAINS, seed, strict=True)
    assert len(store.items('events')) == 2668 < store.MAX_EVENTS
    assert len(store.items('deliveries')) == 16000
    assert emit(quota(review, recovered=True, version=2669)), 'delivery cap prevents recording recovery although recent non-protected data could be retired'


def test_review_disabled_recipient_setting_survives_retention(review):
    now = store.time.time()
    key = f'recipient:11:{review.subject.generation}:quota'
    store.mutate('policy', lambda rows: rows.__setitem__(key, {'disabled': True, 'at': now - store.RETENTION - 1}))
    assert emit(quota(review))  # recipient 22 should still get it
    review.drain()
    assert {p['chat_id'] for m, p in review.calls if m == 'sendMessage'} == {22}, 'GC silently re-enabled recipient 11'
    assert store.get('policy', key)['disabled'] is True


def test_review_full_queue_racing_duplicate_does_not_poison_pushed(review, monkeypatch):
    original = notifier.enqueue_delivery
    nested = []
    def queue_full_after_duplicate(key):
        if not nested:
            nested.append(True)
            assert not emit(quota(review, source='ws'))
        return False
    monkeypatch.setattr(notifier, 'enqueue_delivery', queue_full_after_duplicate)
    assert not emit(quota(review))
    assert not review.queue.qsize()
    monkeypatch.setattr(notifier, 'enqueue_delivery', original)
    assert emit(quota(review)), 'failed older enqueue leaves pushed on the newer duplicate; all future quota observations are suppressed'


def test_review_recovery_already_superseded_is_not_sent_as_current(review):
    assert emit(quota(review))
    review.drain()
    review.calls.clear()
    recovered = quota(review, recovered=True)
    assert emit(recovered)
    assert emit(quota(review))
    review.drain()
    assert not any('· 配额已恢复' in p.get('text', '') for m, p in review.calls if m == 'sendMessage'), 'worker delivered recovery after a newer confirmed fault'


def test_review_confirmed_pause_after_unknown_effect_is_new_decision(review):
    first = replace(quota(review), effect=Effect('unknown', 'account_paused'))
    assert emit(first)
    review.drain()
    assert emit(quota(review)), 'confirmed pause was deduplicated against an explicitly unconfirmed prior effect'


def test_review_recovery_fallback_respects_its_own_family_switch(review):
    review.cfg['notifications']['events']['channel_permanent'] = False
    # No earlier notification, but a genuine local transition supplies "previous".
    event = NotificationEvent('channel.model.recovered', review.subject,
        Effect('confirmed', 'model_resumed'), {'model': 'm', 'previous': '永久冻结'},
        scope='model', scope_ref='m')
    assert not emit(event), 'channel recovery fallback consults quota_disabled instead of channel_permanent'
    assert not review.queue.qsize()


def test_review_pending_429_retry_survives_restart(review, monkeypatch):
    timers = []
    class Timer:
        def __init__(self, delay, fn, args=()):
            timers.append((delay, fn, args))
        def start(self):
            pass
    monkeypatch.setattr(delivery.threading, 'Timer', Timer)
    monkeypatch.setattr(delivery, '_transport', lambda *a: {'ok': False, 'error_code': 429, 'parameters': {'retry_after': 10}})
    event = quota(review)
    assert emit(event)
    review.drain()
    assert len(timers) == 2
    restart(review, monkeypatch)
    assert len(store.recover_queue()) == 2, 'scheduled retry existed only in dead process Timer; restart loses both explicit-rejection retries'


def test_review_pretransport_persistence_failure_not_stranded_as_queued(review, monkeypatch):
    event = quota(review)
    assert emit(event)
    original = store.action_ref
    def fail(*args, **kwargs):
        raise OSError('synthetic temporary disk write failure')
    monkeypatch.setattr(store, 'action_ref', fail)
    review.drain()
    assert not review.calls
    monkeypatch.setattr(store, 'action_ref', original)
    stranded_states = {r['state'] for r in rows_for(event)}
    retry_accepted = emit(quota(review))
    assert 'queued' not in stranded_states and retry_accepted, (
        f'worker consumed both queue entries; states={stranded_states}, next observation accepted={retry_accepted}')


def test_review_ordinary_event_gc_accepts_safe_operation_summary(review):
    """Positive boundary control: operation summaries are policy rows, not events."""
    from src.notifications.actions import _record_operation
    _record_operation(quota(review).record(), 'refresh_usage', 11, {'id': 'review-op', 'status': 'running'})
    assert emit(quota(review))
    review.drain()
    store.recover_queue()
    assert store.get('policy', 'operation:review-op')['status'] == 'unknown'
    assert all('event_type' in row for row in store.items('events').values())


def test_review_completed_operation_cannot_contain_failed_stages(review):
    event = NotificationEvent('operation.completed', review.subject, Effect('confirmed', 'saved'),
        {'operation': '初始化', 'result': '各阶段已完成', 'saved': True,
         'stages': [{'name': '模型目录', 'status': 'failed'}]}, scope='operation')
    with pytest.raises(ValueError):
        event.record()


def test_review_present_not_saved_with_completed_substeps_still_reports_result(review):
    # All substeps may be done/skipped even though the account wasn't saved.
    result = operations.present(11, 77, review.subject, operation='导入', saved=False,
        stages=[{'name': '凭据预检', 'status': 'succeeded'}, {'name': '写入账户', 'status': 'skipped'}])
    assert result['event_type'] == 'operation.attention'
    assert review.calls and '未确认账户保存成功' in review.calls[-1][1]['text']


def test_review_metrics_are_registered_and_reset_unknown_is_not_lost(review):
    from src.notifications.providers import quota_metrics
    metrics = quota_metrics({'five_hour': {'utilization': 97, 'resets_at': '2026-10-18T00:00:00+08:00'}})
    assert metrics[0]['reset_certainty'] == 'upstream'
    event = replace(quota(review), facts={'metrics': metrics})
    text = content.render(event.record(), detail=True)
    assert '2026-10-18' in text, 'the REAL quota adapter retains reset_at, but even full event detail discards it'


def test_review_metric_kind_rejects_unregistered_semantics(review):
    event = replace(quota(review), facts={'metrics': [{'metric_id': 'new', 'kind': 'arbitrary_unregistered_kind'}]})
    with pytest.raises(ValueError):
        event.record()


def test_review_html_and_unknown_values_remain_truthful(review):
    event = replace(quota(review), effect=Effect('unknown', 'account_paused'), facts={'metrics': [{'metric_id': 'credits', 'kind': 'credits'}]})
    rendered = content.render(event.record())
    assert 'Review &lt;&amp;&gt;' in rendered
    assert '数值未取得' in rendered and '0%' not in rendered
    assert '当前影响尚未确认' in rendered and 'Parrot 已暂停此账号调用' not in rendered


def test_review_gc_preserves_unsubmitted_outbox_event(review, monkeypatch):
    monkeypatch.setattr(store, 'MAX_EVENTS', 2)
    first = quota(review)
    assert emit(first)
    assert not emit(quota(review, source='ws'))
    assert not emit(quota(review, source='background'))
    # Pending failure is still in the REAL notifier queue; only later duplicates
    # are suppressed. Event GC must not erase its outbox before transport runs.
    review.drain()
    assert len([p for m, p in review.calls if m == 'sendMessage']) == 2, 'GC deleted the queued first event and both deliveries; worker silently skipped them'
    assert {r['state'] for r in rows_for(first)} == {'sent'}


def test_review_operation_summary_policy_is_bounded_independently(review):
    from src.notifications.actions import _record_operation
    now = store.time.time()
    summary = {'kind': 'operation', 'generation': review.subject.generation,
               'action': 'refresh_usage', 'actor': 11, 'status': 'succeeded', 'at': now}
    store.mutate('policy', lambda rows: rows.update({f'operation:old-{n}':
        {**summary, 'id': f'old-{n}'} for n in range(8000)}))
    _record_operation(quota(review).record(), 'refresh_usage', 11,
                      {'id': 'one-more', 'status': 'succeeded'})
    summary_count = len(store.items('policy'))
    ordinary_accepted = emit(quota(review))
    assert summary_count <= 8000 and ordinary_accepted, (
        f'operation summaries bypass cap: {summary_count}; unrelated ordinary event accepted={ordinary_accepted}')


def test_review_capture_order_prevents_stale_fault_after_new_recovery(review):
    from src.notifications import capture
    old_subject, old_version = capture('openai:review@example.test:review')
    _, failure_version = capture('openai:review@example.test:review')
    assert emit(quota(review, version=failure_version))
    _, recovered_version = capture('openai:review@example.test:review')
    assert emit(quota(review, recovered=True, version=recovered_version))
    assert not emit(replace(quota(review, version=old_version), subject=old_subject))
    review.drain()
    assert all('配额已恢复' in p['text'] for m, p in review.calls if m == 'sendMessage')
    assert not next(iter(store.items('incidents').values()))['active']


def test_review_operation_result_survives_auxiliary_persistence_failure(review, monkeypatch):
    monkeypatch.setattr(review.db, 'write_snapshot', lambda *a, **kw: (_ for _ in ()).throw(OSError('synthetic disk failure')))
    event = operations.present(11, 77, review.subject, operation='导入',
        stages=[{'name': '保存', 'status': 'succeeded'}],
        continuation=[[{'text': '继续既有流程', 'callback_data': 'existing-workflow:synthetic-nonce'}]])
    assert event['event_type'] == 'operation.completed'
    assert len(review.calls) == 1 and review.calls[0][0] == 'editMessageText'
    assert '账户已保存' in review.calls[0][1]['text']
    assert all('synthetic-nonce' not in json.dumps(review.db.items(d)) for d in store.DOMAINS)


def test_review_store_transaction_failure_does_not_publish_partial_domains(review, monkeypatch):
    before = {d: review.db.items(d) for d in store.DOMAINS}
    monkeypatch.setattr(review.db, 'write_snapshot', lambda *a, **kw: (_ for _ in ()).throw(OSError('synthetic disk failure')))
    # Explicit version avoids the independent allocation transaction.
    assert not emit(quota(review, version=1))
    assert {d: review.db.items(d) for d in store.DOMAINS} == before
    assert not review.queue.qsize()
    assert review.cfg['oauthAccounts'][0]['enabled'] is True
