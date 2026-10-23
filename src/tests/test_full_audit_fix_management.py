"""Management audit fixes: synthetic controls/transports, no external side effects."""
from __future__ import annotations

import asyncio
import copy
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize('available', [('other-model',), ()])
@pytest.mark.parametrize('clear', [False, True])
def test_m01_permission_editor_retains_missing_grants_unless_explicitly_cleared(monkeypatch, available, clear):
    from src.tests.test_management_apikey_control import make_control
    from src.telegram import ui, states
    from src.telegram.menus import apikey_menu as menu
    from src import auth
    original = ['missing-z', 'missing-a']
    control, store, _, _ = make_control({
        'apiKeys': {'restricted': {'key': 'synthetic-management-key', 'enabled': True,
                                  'allowedModels': original}},
        'oauthAccounts': [], 'channels': [], 'xaiOAuth': {'imageModels': [], 'videoModels': []}})
    monkeypatch.setattr(menu, '_CONTROL', control)
    monkeypatch.setattr(control, 'available_permission_models', lambda ctx: available)
    monkeypatch.setattr(control, 'available_permission_models_unchecked', lambda: available)
    monkeypatch.setattr(control, 'configured_media_models', lambda ctx: ((), ()))
    monkeypatch.setattr(menu, '_edit_cached_detail', lambda *a, **k: None)
    rendered = []
    monkeypatch.setattr(ui, 'edit', lambda *a, **k: rendered.append((a, k)))
    monkeypatch.setattr(ui, 'answer_cb', lambda *a, **k: None)
    monkeypatch.setattr(ui, 'send', lambda *a, **k: None)
    monkeypatch.setattr(auth.config, 'get', store.get)
    states.clear_all()
    try:
        short = menu._short_of('restricted')
        menu.on_perm_enter(42, 123, 'open', short)
        assert states.get_state(42)['data']['checked'] == original
        assert '当前不可用' in repr(rendered)
        if clear:
            menu.on_perm_clear(42, 123, 'clear', short)
        menu.on_perm_save(42, 123, 'save', short)
        expected = [] if clear else original
        assert store.value['apiKeys']['restricted']['allowedModels'] == expected
        _, allowed, error = auth.validate({'authorization': 'Bearer synthetic-management-key'})
        assert not error and allowed == expected
    finally:
        states.clear_all()


def test_m01_control_retains_existing_missing_models_but_rejects_new_unknown(monkeypatch):
    from src.tests.test_management_apikey_control import make_control, context
    from src.management_auth import Capability
    from src.management_control import ManagementError
    control, store, _, _ = make_control()
    store.value['apiKeys']['alpha']['allowedModels'] = ['gone']
    monkeypatch.setattr(control, 'available_permission_models_unchecked', lambda: ('new',))
    ctx = context(Capability.READ, Capability.WRITE)
    control.update_api_key(ctx, 'alpha', changes={'allowed_models': ['gone', 'new']})
    with pytest.raises(ManagementError):
        control.update_api_key(ctx, 'alpha', changes={'allowed_models': ['invented']})
    assert store.value['apiKeys']['alpha']['allowedModels'] == ['gone', 'new']


@pytest.mark.parametrize('provider,result,status,outcome,local', [
    ('openai', {'outcome': 'noCredit'}, 'noCredit', 'noCredit', None),
    ('openai', {'outcome': 'nothingToReset'}, 'nothingToReset', 'nothingToReset', None),
    ('claude', {'action': 'reset_failed', 'error_code': 'runtime_state_clear_failed'}, 'reset_failed', None, 'reset_failed'),
    ('claude', {'action': 'noop_auth_error'}, 'noop_auth_error', None, 'noop_auth_error'),
    ('claude', {'action': 'reset'}, 'reset', None, 'reset'),
    ('claude', {'action': 'cleared_runtime_state'}, 'cleared_runtime_state', None, 'cleared_runtime_state'),
    ('openai', {'outcome': 'reset', 'quota_action': {'action': 'resumed'}}, 'reset', 'reset', 'resumed'),
    ('openai', {'outcome': 'alreadyRedeemed', 'quota_action': {'action': 'kept_enabled'}}, 'alreadyRedeemed', 'alreadyRedeemed', 'kept_enabled'),
    ('openai', {'outcome': 'reset', 'quota_action': {'action': 'still_over_quota'}}, 'partial', 'reset', 'still_over_quota'),
    ('openai', {'outcome': 'reset', 'refresh_error': 'synthetic-secret'}, 'partial', 'reset', 'refresh_failed_keep_disabled'),
    ('openai', {'outcome': 'reset', 'quota_action': {'action': 'state_update_failed_keep_disabled'}}, 'partial', 'reset', 'state_update_failed_keep_disabled'),
    ('openai', {'outcome': 'reset', 'quota_action': {'action': 'kept_enabled'}, 'runtime_clear': {'required_state_cleared': False}}, 'partial', 'reset', 'runtime_state_clear_failed'),
    ('openai', {'outcome': 'synthetic-secret'}, 'unknown', None, 'unknown'),
    ('openai', {'action': 'reset'}, 'unknown', None, 'unknown'),
    ('openai', {'action': 'noop_user'}, 'noop_user', None, 'noop_user'),
    ('openai', {'outcome': 'reset', 'quota_action': {'action': 'resume_failed'}}, 'partial', 'reset', 'resume_failed'),
])
def test_m02_quota_api_preserves_business_result_and_never_repeats(tmp_path, monkeypatch, provider, result, status, outcome, local):
    from src.tests.test_management_oauth_api import auth_client, request, ACCOUNT_ID, INVALID_ID
    client, headers, runtime, control, backend = auth_client(tmp_path)
    control._audit_sink = runtime.audit_sink
    account_id = ACCOUNT_ID if provider == 'openai' else INVALID_ID
    calls = []
    async def remote(*a, **kw):
        calls.append(1)
        return result
    def reset(*a, **kw):
        calls.append(1)
        return result
    monkeypatch.setattr(backend, 'redeem_openai_reset_credit', remote)
    monkeypatch.setattr(backend, 'reset_quota', reset)
    try:
        plan = request(client, 'POST', f'/oauth/accounts/{account_id}/actions/reset-quota-plan', {}, headers)
        assert plan.status_code == 200, plan.text
        body = {'planToken': plan.json()['data']['planToken']}
        path = f'/oauth/accounts/{account_id}/actions/reset-quota'
        reply = request(client, 'POST', path, body, headers)
        assert reply.status_code == 200, reply.text
        data = reply.json()['data']
        assert (data['status'], data['upstreamOutcome'], data['localAction']) == (status, outcome, local)
        assert 'synthetic-secret' not in reply.text
        replay = request(client, 'POST', path, body, headers)
        assert replay.status_code == 400
        assert calls == [1]
        audits = runtime.state_store.audit_snapshot()
        assert any(a.get('action') == 'oauth.quota.reset' and a.get('result') == status for a in audits)
    finally:
        client.__exit__(None, None, None)
        runtime.close()


def test_m02_quota_exception_is_not_retryable_after_consumption(tmp_path, monkeypatch):
    from src.tests.test_management_oauth_api import auth_client, request, ACCOUNT_ID
    client, headers, runtime, _, backend = auth_client(tmp_path)
    calls = []
    async def remote(*args):
        calls.append(1)
        raise RuntimeError('synthetic-secret')
    monkeypatch.setattr(backend, 'redeem_openai_reset_credit', remote)
    try:
        plan = request(client, 'POST', f'/oauth/accounts/{ACCOUNT_ID}/actions/reset-quota-plan', {}, headers)
        body = {'planToken': plan.json()['data']['planToken']}
        path = f'/oauth/accounts/{ACCOUNT_ID}/actions/reset-quota'
        reply = request(client, 'POST', path, body, headers)
        assert reply.status_code == 502
        assert reply.json()['error']['retryable'] is False
        assert 'synthetic-secret' not in reply.text
        assert request(client, 'POST', path, body, headers).status_code == 400
        assert calls == [1]
    finally:
        client.__exit__(None, None, None)
        runtime.close()


def test_m03_name_limit_and_encoded_resource_remain_manageable(tmp_path):
    from urllib.parse import quote
    from fastapi.testclient import TestClient
    from src.tests.test_management_channels_api import _build_app, _session, _manual_create, _reset_channels
    _reset_channels()
    app, runtime = _build_app(tmp_path)
    try:
        with TestClient(app) as client:
            headers = _session(client)
            prefix = '/api/management/v1/channels/'
            assert client.post(prefix[:-1], json=_manual_create('normal'), headers=headers).status_code == 201
            reply = client.patch(prefix+'api:normal', json={'name': 'X'*300}, headers=headers)
            assert reply.status_code == 422
            assert client.get(prefix+'api:normal', headers=headers).status_code == 200
            name = 'region/'+'X'*57  # Existing 64-character creation contract.
            assert len(name) == 64
            changed = client.patch(prefix+'api:normal', json={'name': name}, headers=headers)
            assert changed.status_code == 200, changed.text
            path = prefix + quote(changed.json()['data']['id'], safe='')
            assert client.get(path, headers=headers).status_code == 200
            assert client.delete(path, headers={**headers, 'If-Match': changed.json()['data']['revision']}).status_code == 204
    finally:
        runtime.close()
        _reset_channels()


def test_m03_shared_control_rejects_long_new_name_but_repairs_legacy_id(tmp_path):
    from src.tests.test_management_channels_api import _reset_channels
    from src.tests.test_management_apikey_control import context
    from src.management_auth import Capability
    from src.management_control import ManagementError
    from src.management_control.channels import ChannelControl, ChannelUpdateCommand
    from src.channel import registry
    _reset_channels()
    try:
        legacy = 'X'*300
        registry.add_api_channel({'name': legacy, 'baseUrl': 'https://fake.invalid', 'apiKey': 'fake-key',
                                  'models': [{'real': 'm', 'alias': 'm'}]})
        control = ChannelControl()
        ctx = context(Capability.READ, Capability.WRITE)
        with pytest.raises(ManagementError):
            control.update_channel(ctx, 'api:'+legacy, ChannelUpdateCommand(name='Y'*65))
        # TG/shared control can still repair an already stored legacy long name.
        changed = control.update_channel(ctx, 'api:'+legacy, ChannelUpdateCommand(name='fixed'))
        assert changed.channel.id == 'api:fixed'
    finally:
        _reset_channels()


@pytest.mark.parametrize('ok', [False, True])
def test_m04_tg_cancel_has_truthful_receipt_and_only_one_attempt(monkeypatch, ok):
    from src.telegram import ui
    from src.telegram.menus import update_menu as menu
    calls, captured = [], []
    def cancel(*a, **k):
        calls.append(1)
        return ok, 'synthetic <detail>'
    monkeypatch.setattr(menu._CONTROL, 'cancel_direct', cancel)
    monkeypatch.setattr(ui, 'answer_cb', lambda *a, **k: None)
    monkeypatch.setattr(ui, 'edit', lambda chat, mid, text, **k: captured.append(text))
    menu._cancel_staged(42, 1, 'cancel')
    assert calls == [1]
    assert ('已取消更新' in captured[0]) is ok
    assert ('取消更新失败' in captured[0]) is not ok
    assert '&lt;detail&gt;' in captured[0]


def test_m05_last_target_stays_empty_in_tg_control_and_monitor(monkeypatch):
    from src.tests.test_management_apikey_control import FakeConfig
    from src.management_control.auxiliary.status_alerts import StatusAlertControl
    from src.management_control.auxiliary.common import telegram_context
    from src import status_monitor
    from src.telegram import ui
    from src.telegram.menus import status_alert_menu as menu
    store = FakeConfig({'statusMonitor': {'enabled': True, 'targets': ['claude']}})
    control = StatusAlertControl(config_gateway=store, status_gateway=SimpleNamespace(forget_provider=lambda p: None))
    monkeypatch.setattr(menu, '_CONTROL', control)
    monkeypatch.setattr(menu, 'show', lambda *a, **k: None)
    messages = []
    monkeypatch.setattr(ui, 'answer_cb', lambda cb, text, **k: messages.append(text))
    monkeypatch.setattr(status_monitor.config, 'get', store.get)
    menu._toggle_target(42, 1, 'callback', 'claude')
    assert store.value['statusMonitor']['targets'] == []
    assert messages == ['已移除 claude']
    assert control.get_settings(telegram_context(42)).targets == ()
    assert status_monitor._cfg()['targets'] == []
    # The history page must not fall back to remote fetches for an empty list.
    monkeypatch.setattr(control, 'recent_direct', lambda *a, **k: pytest.fail('empty target fetched'))
    monkeypatch.setattr(ui, 'edit', lambda *a, **k: None)
    menu._history(42, 1, 'history')
    del store.value['statusMonitor']['targets']
    assert control.get_settings(telegram_context(42)).targets == ('claude', 'openai', 'cloudflare')
    assert status_monitor._cfg()['targets'] == ['claude', 'openai', 'cloudflare']


@pytest.mark.parametrize('group', [False, True])
@pytest.mark.parametrize('conflict', [False, True])
def test_m06_concurrent_probe_claim_is_atomic_and_execution_unlocked(monkeypatch, group, conflict):
    from src.tests.test_management_apikey_control import context
    from src.management_auth import Capability
    from src.management_control import OperationStore, ManagementError
    from src.management_control.proxy.control import ProxyControl, proxy_manager
    store = OperationStore()
    control = ProxyControl(operation_store=store)
    ctx = replace(context(Capability.READ, Capability.WRITE, subject=f'claim-{group}-{conflict}'), idempotency_key='retry')
    submitted, calls = [], []
    start = threading.Barrier(2)
    entered, release = threading.Event(), threading.Event()
    second_claim_attempt = threading.Event()
    class ObservedLock:
        def __init__(self):
            self.lock = threading.RLock()
            self.guard = threading.Lock()
            self.threads = set()
        def __enter__(self):
            with self.guard:
                self.threads.add(threading.get_ident())
                if len(self.threads) >= 2:
                    second_claim_attempt.set()
            self.lock.acquire()
            return self
        def __exit__(self, *args):
            self.lock.release()
    monkeypatch.setattr(ProxyControl, '_idempotency_lock', ObservedLock())
    def snapshot():
        entered.set()
        assert release.wait(5)
        return {'proxies': {'a': {}, 'b': {}}, 'groups': {'a': ['one'], 'b': ['two']}, 'routing': {}}
    async def probe(*args, **kw):
        # A distinct thread must acquire the claim lock during slow execution.
        with ThreadPoolExecutor(max_workers=1) as pool:
            def acquire():
                with control._idempotency_lock:
                    return True
            assert pool.submit(acquire).result(timeout=3)
        calls.append(args)
        return [{'ok': True, 'ip': '192.0.2.1'}] if group else {'ok': True, 'ip': '192.0.2.1'}
    monkeypatch.setattr(control, '_network_snapshot', snapshot)
    monkeypatch.setattr(store, 'submit', lambda oid, worker: submitted.append(worker))
    monkeypatch.setattr(proxy_manager, 'test_group' if group else 'test_proxy', probe)
    method = control.start_group_test if group else control.start_proxy_test
    def call(target):
        start.wait(timeout=5)
        try:
            return method(ctx, target)
        except ManagementError as exc:
            return exc
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(call, 'a'), pool.submit(call, 'b' if conflict else 'a')]
            assert entered.wait(5)
            # The second caller reaches the claim while the first is parked.
            assert second_claim_attempt.wait(5)
            release.set()
            results = [f.result(timeout=5) for f in futures]
        errors = [r for r in results if isinstance(r, ManagementError)]
        operations = [r for r in results if not isinstance(r, ManagementError)]
        if conflict:
            assert len(errors) == 1 and errors[0].code.value == 'STATE_CONFLICT'
        else:
            assert not errors and operations[0].id == operations[1].id
        assert len(submitted) == 1
        submitted[0]()
        assert len(calls) == 1
        assert store.get(ctx, operations[0].id).status.value == 'succeeded'
    finally:
        release.set()
        store.close()


@pytest.fixture
def fake_status(monkeypatch):
    from src import status_monitor as sm
    monkeypatch.setattr(sm, '_cfg', lambda: {'enabled': True, 'intervalSeconds': 60, 'targets': ['claude'], 'minImpact': 'minor'})
    monkeypatch.setattr(sm, '_active', {'claude': {}})
    monkeypatch.setattr(sm, '_muted', {'claude': set()})
    monkeypatch.setattr(sm, '_mute_loaded', True)
    monkeypatch.setattr(sm, '_initialized_providers', set())
    seen, notifications = set(), []
    monkeypatch.setattr(sm, '_load_seen', lambda p: seen.copy())
    monkeypatch.setattr(sm, '_mark_seen', lambda p, u, i, s: seen.add(u))
    monkeypatch.setattr(sm.notifier, 'notify_event', lambda key, text, **kw: notifications.append(text) or True)
    monkeypatch.setattr(sm, '_cleanup_stale_mutes', lambda ids: 0)
    return sm, seen, notifications


def incident(status='investigating', **extra):
    return {'id': 'live', 'name': 'Synthetic incident', 'status': status, 'impact': 'major',
            'incident_updates': [], **extra}


async def one_status_round(sm, monkeypatch):
    async def stop(delay):
        raise asyncio.CancelledError
    with monkeypatch.context() as patch:
        patch.setattr(sm.asyncio, 'sleep', stop)
        with pytest.raises(asyncio.CancelledError):
            await sm.monitor_loop()


@pytest.mark.asyncio
async def test_ao01_single_snapshot_is_fetched_off_loop_and_reused(fake_status, monkeypatch):
    from src.tests.conftest import _ORIG_TO_THREAD
    monkeypatch.setattr(asyncio, 'to_thread', _ORIG_TO_THREAD)
    sm, _, _ = fake_status
    loop = asyncio.get_running_loop()
    main_tid = threading.get_ident()
    callbacks, fetch_threads, cleanups = [], [], []
    responsive = threading.Event()
    def heartbeat():
        callbacks.append(True)
        responsive.set()
    def fetch(provider):
        fetch_threads.append(threading.get_ident())
        loop.call_soon_threadsafe(heartbeat)
        assert responsive.wait(3), 'event loop blocked by sync fetch'
        return [incident()]
    monkeypatch.setattr(sm, '_fetch_incidents', fetch)
    monkeypatch.setattr(sm, '_cleanup_stale_mutes', lambda ids: cleanups.append((threading.get_ident(), ids)) or 0)
    await one_status_round(sm, monkeypatch)
    assert len(fetch_threads) == 1 and fetch_threads[0] != main_tid
    assert callbacks == [True]
    assert len(cleanups) == 1 and cleanups[0][0] != main_tid
    assert cleanups[0][1] == {'claude': {'live'}}
    assert sm._initialized_providers == {'claude'}


@pytest.mark.asyncio
async def test_ao02_unknown_preserves_prime_mute_and_last_known_state(fake_status, monkeypatch):
    sm, seen, notifications = fake_status
    sm._muted['claude'].add('muted')
    sm._active['claude']['old'] = incident(id='old')
    cleanup = []
    monkeypatch.setattr(sm, '_cleanup_stale_mutes', lambda ids: cleanup.append(ids) or 0)
    monkeypatch.setattr(sm, '_fetch_incidents', lambda p: None)
    await one_status_round(sm, monkeypatch)
    assert not sm._initialized_providers and not cleanup
    assert sm._muted['claude'] == {'muted'} and 'old' in sm._active['claude']
    monkeypatch.setattr(sm, '_fetch_incidents', lambda p: [incident(incident_updates=[{'id': 'historic', 'status': 'investigating'}])])
    await one_status_round(sm, monkeypatch)
    assert sm._initialized_providers == {'claude'}
    assert seen == {'historic'} and not notifications
    # A successful empty list is authoritative for stale-mute cleanup, not None.
    monkeypatch.setattr(sm, '_fetch_incidents', lambda p: [])
    await one_status_round(sm, monkeypatch)
    assert cleanup == [{'claude': {'live'}}, {'claude': set()}]


def test_ao02_manual_refresh_does_not_consume_failed_prime(fake_status, monkeypatch):
    from src.management_control.auxiliary.status_alerts import ModuleStatusGateway
    sm, seen, notifications = fake_status
    gateway = ModuleStatusGateway()
    monkeypatch.setattr(sm, '_fetch_incidents', lambda p: None)
    with pytest.raises(RuntimeError):
        gateway.refresh_provider('claude')
    assert not sm._initialized_providers
    with pytest.raises(RuntimeError):
        gateway.refresh_provider('claude', raise_on_error=True)
    monkeypatch.setattr(sm, '_fetch_incidents', lambda p: [incident(incident_updates=[{'id': 'old', 'status': 'investigating'}])])
    gateway.refresh_provider('claude')
    assert seen == {'old'} and sm._initialized_providers == {'claude'} and not notifications


def test_ao03_absence_is_pending_until_positive_recovery_and_no_duplicate_edge(fake_status, monkeypatch):
    sm, _, notifications = fake_status
    sm._active['claude']['live'] = incident()
    monkeypatch.setattr(sm, '_fetch_incidents', lambda p: [])
    assert sm._process_provider('claude', push=True) == []
    assert not notifications
    assert sm._active['claude']['live']['_status_unconfirmed']
    assert '待确认' in sm.get_active_summary()
    resolved = incident('resolved', resolved_at='2026-09-01T00:00:00Z')
    monkeypatch.setattr(sm, '_fetch_incidents', lambda p: [resolved])
    sm._process_provider('claude', push=True)
    assert len(notifications) == 1 and '已恢复' in notifications[0]
    assert not sm.snapshot_active()['claude']
    sm._process_provider('claude', push=True)
    assert len(notifications) == 1


@pytest.mark.parametrize('row', [incident('postmortem'), incident('completed'), incident(resolved_at='2026-09-01T00:00:00Z')])
def test_ao03_terminal_incidents_never_reenter_banner_or_headline(fake_status, monkeypatch, row):
    sm, _, notifications = fake_status
    monkeypatch.setattr(sm, '_fetch_incidents', lambda p: [row])
    sm._process_provider('claude', push=True)
    assert sm.snapshot_active()['claude'] == []
    assert sm.get_active_summary() is None and not notifications


def test_ao03_muted_terminal_remains_silent_and_updates_seen(fake_status, monkeypatch):
    sm, seen, notifications = fake_status
    sm._muted['claude'].add('live')
    sm._active['claude']['live'] = incident()
    monkeypatch.setattr(sm, '_fetch_incidents', lambda p: [incident('postmortem', incident_updates=[{'id': 'muted-update', 'status': 'resolved'}])])
    sm._process_provider('claude', push=True)
    assert not notifications and not sm.snapshot_active()['claude']
    assert seen == {'muted-update'}


def test_m03_tg_rejects_long_name_before_mutation(monkeypatch):
    from src.telegram import states, ui
    from src.telegram.menus import channel_menu as menu
    captured = []
    states.clear_all()
    try:
        short = ui.register_code('normal')
        states.set_state(42, 'ch_edit_name', {'short': short})
        monkeypatch.setattr(ui, 'send', lambda chat, text, **k: captured.append(text))
        monkeypatch.setattr(menu, '_do_edit', lambda *a, **k: pytest.fail('invalid name mutated'))
        assert menu.handle_edit_text(42, 'ch_edit_name', 'X'*65)
        assert len(captured) == 1 and '64' in captured[0]
        assert states.get_state(42)['action'] == 'ch_edit_name'
    finally:
        states.clear_all()


def test_m05_api_empty_target_roundtrip(tmp_path):
    from fastapi.testclient import TestClient
    from src.tests.test_management_status_alert_revision import _build_status_app
    from src.tests.management_auxiliary_support import bearer, create_session
    app, _gateway = _build_status_app(tmp_path)
    with TestClient(app) as client:
        headers = bearer(create_session(client))
        path = '/api/management/v1/status-alerts/settings'
        current = client.get(path, headers=headers)
        assert current.status_code == 200, current.text
        response = client.patch(path, headers={**headers, 'If-Match': current.json()['data']['revision']}, json={'targets': []})
        assert response.status_code == 200, response.text
        assert response.json()['data']['targets'] == []
        assert client.get(path, headers=headers).json()['data']['targets'] == []


def test_ao03_existing_update_headline_threshold_and_recovery_edges(fake_status, monkeypatch):
    sm, seen, notifications = fake_status
    updates = [{'id': 'u1', 'status': 'investigating', 'body': 'working'}]
    row = incident(incident_updates=updates)
    monkeypatch.setattr(sm, '_fetch_incidents', lambda p: [row])
    sm._process_provider('claude', push=True)
    assert len(notifications) == 2  # New update plus the existing headline edge.
    sm._process_provider('claude', push=True)
    assert len(notifications) == 2 and seen == {'u1'}
    row.update(status='resolved', resolved_at='2026-09-01T00:00:00Z', impact='none',
               incident_updates=[{'id': 'u2', 'status': 'resolved'}])
    sm._process_provider('claude', push=True)
    assert len(notifications) == 4  # Resolved update and recovery stay below-threshold exceptions.
    assert '已恢复' in notifications[-1]
    sm._process_provider('claude', push=True)
    assert len(notifications) == 4
    row.update(status='postmortem', incident_updates=[])
    sm._process_provider('claude', push=True)
    assert not sm.snapshot_active()['claude'] and len(notifications) == 4


@pytest.mark.asyncio
async def test_ao02_failed_provider_excluded_from_cleanup_not_other_successes(fake_status, monkeypatch):
    sm, _, _ = fake_status
    monkeypatch.setattr(sm, '_cfg', lambda: {'enabled': True, 'intervalSeconds': 60, 'targets': ['claude', 'openai'], 'minImpact': 'minor'})
    monkeypatch.setattr(sm, '_fetch_incidents', lambda p: None if p == 'claude' else [])
    cleanups = []
    monkeypatch.setattr(sm, '_cleanup_stale_mutes', lambda ids: cleanups.append(ids) or 0)
    await one_status_round(sm, monkeypatch)
    assert cleanups == [{'openai': set()}]
    assert sm._initialized_providers == {'openai'}
