"""Bound TG reauthentication: real isolated storage, mocked provider I/O only."""
from __future__ import annotations

import copy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from src.tests import _isolation
_isolation.isolate()
from src import config, log_db, oauth_manager as om, state_db
from src.management_control.errors import ManagementError
from src.management_control.oauth import OAuthControl, OAuthProvider
from src.telegram import states, ui
from src.telegram.menus import oauth_menu as menu, workbuddy_oauth_menu as wb, zhipu_oauth_menu as zh
from src.tests.test_oauth_overwrite_confirmation import _entry
from src.tests.test_workbuddy_lifecycle import context
from src.tests.test_zhipu_provider import credential
from src.oauth.workbuddy.auth import normalize_credential

PROVIDERS = ['claude', 'openai', 'xai', 'cursor', 'antigravity', 'workbuddy', 'zhipu']
GENERIC = PROVIDERS[:5]


@pytest.fixture(autouse=True)
def isolated_accounts(monkeypatch):
    before = copy.deepcopy(config.get())
    state_db.init()
    log_db.init()
    config.update(lambda c: c.update(oauthAccounts=[], channels=[], loadBalancing={}))
    ctl = OAuthControl()
    monkeypatch.setattr(ctl, '_post_save_account_effects', Mock(return_value={}))
    monkeypatch.setattr(menu, 'oauth_control', ctl)
    monkeypatch.setattr(wb, 'oauth_control', ctl)
    monkeypatch.setattr(zh, 'control', ctl)
    monkeypatch.setattr(menu, '_foreground_account_model_sync', Mock(return_value={'post_save': {}}))
    for name in ['answer_cb', 'edit', 'send', 'send_result']:
        monkeypatch.setattr(ui, name, Mock())
    monkeypatch.setattr(wb, '_start_login_worker', Mock())
    monkeypatch.setattr(zh, '_submit_remote', lambda run: run())
    monkeypatch.setattr(zh, '_start_login_wait', Mock())
    states.clear_all()
    yield ctl
    for job in wb._login_jobs.values():
        job['stop'].set()
        job['wake'].set()
    wb._login_jobs.clear()
    states.clear_all()
    config.update(lambda c: (c.clear(), c.update(before)))


def account(provider, reason='auth_error', /, **kwargs):
    if provider == 'workbuddy':
        value = normalize_credential(dict(realm='cn', uid='original', access_token='old-at', refresh_token='old-rt'))
    elif provider == 'zhipu':
        value = credential('oauth', organization_id='org', project_id='project', project_key_id='key-location', model_key='')
    else:
        value = _entry(provider)
        if provider == 'antigravity':
            value['project_id'] = 'original-project'
    value.update(enabled=reason is None, disabled_reason=reason, disabled_until=None,
                 models=['chosen-model'], maxConcurrent=7, label='kept-name', extension={'keep': True})
    value.update(kwargs)
    om.add_account(value)
    key = om.get_account_key(value)
    config.update(lambda c: next(a for a in c['oauthAccounts'] if om.get_account_key(a) == key).update(
        maxConcurrent=7, extension={'keep': True}))
    return key, copy.deepcopy(om.get_account(key))


def binding(key, value):
    return dict(target_key=key, account=value, payload=menu._callback_payload(ui.register_code(key), 3, 'invalid'))


def rows(key):
    _, kb = menu._detail_text_and_kb(key, refresh_quota=False, actor_chat_id=42, model_stats=[], stats_loading=False)
    return kb['inline_keyboard']


@pytest.mark.parametrize('provider', PROVIDERS)
@pytest.mark.parametrize('reason', [None, 'auth_error', 'quota', 'user'])
def test_detail_only_auth_failure_has_relogin(provider, reason):
    key, old = account(provider, reason)
    keyboard = rows(key)
    assert [b['text'] for b in keyboard[0]] == (['🔐 重新登录', '📊 刷新额度'] if reason == 'auth_error' else ['📊 刷新额度'])
    assert [b['text'] for b in keyboard[1]] == ['管理模型', '🚦 并发上限']
    assert [b['text'] for b in keyboard[2]] == ['🧹 清模型故障', '🔗 清亲和绑定']
    assert all('刷新 Token' not in b['text'] for r in keyboard for b in r)
    assert om.get_account(key) == old


@pytest.mark.parametrize('status,needed', [('relogin_required', True), ('model_key_lookup_failed', False), ('project_required', False), ('ready', False)])
def test_zhipu_management_auth_is_explicit_not_network(status, needed):
    key, _ = account('zhipu', None, management_status=status)
    assert any('重新登录' in b['text'] for b in rows(key)[0]) is needed


@pytest.mark.parametrize('reason', [None, 'auth_error'])
def test_api_key_never_offers_browser_login(reason):
    key, _ = account('zhipu', reason, **credential('api_key'))
    assert [b['text'] for b in rows(key)[0]] == (['🔄 刷新连接', '📊 刷新额度'] if reason else ['📊 刷新额度'])
    menu.on_relogin(42, 100, 'cb', ui.register_code(key))
    assert states.get_state(42) is None


def mock_start(monkeypatch, ctl, provider):
    if provider in ['workbuddy', 'zhipu']:
        monkeypatch.setattr(ctl.backend, provider + '_start_login', Mock(side_effect=lambda **kw: dict(
            realm='cn', site='bigmodel', status='pending', interval=1, auth_url='https://example.test/login')))
    elif provider == 'cursor':
        monkeypatch.setattr(ctl, 'cursor_generate_login', lambda: SimpleNamespace(uuid='u', verifier='v', login_url='https://example.test/login'))
    else:
        monkeypatch.setattr(ctl, provider + '_pkce_generate', lambda: ('verifier', 'challenge'))
        monkeypatch.setattr(ctl, provider + '_build_login_url', lambda *a, **k: 'https://example.test/login')
        if provider == 'xai':
            monkeypatch.setattr(ctl, 'xai_discover', lambda: {})


@pytest.mark.parametrize('provider', PROVIDERS)
def test_direct_start_and_cancel_return_exact_detail(isolated_accounts, monkeypatch, provider):
    ctl = isolated_accounts
    key, old = account(provider)
    mock_start(monkeypatch, ctl, provider)
    relogin = binding(key, old)
    assert menu.handle_callback(42, 100, 'cb', 'oa:relogin:' + relogin['payload'])
    data = states.get_state(42)['data']
    assert data['relogin'] == relogin
    kb = ui.edit.call_args.kwargs['reply_markup']['inline_keyboard']
    cancel = next(b['callback_data'] for r in kb for b in r if '取消' in b['text'])
    view = Mock()
    monkeypatch.setattr(menu, 'on_view', view)
    assert menu.handle_callback(42, 100, 'cb', cancel)
    view.assert_called_once_with(42, 100, '' if provider in ['workbuddy', 'zhipu'] else 'cb',
                                 ui.register_code(key), page=3, filter_key='invalid')
    assert states.get_state(42) is None
    assert om.get_account(key) == old
    if provider in ['workbuddy', 'zhipu']:
        assert ctl.poll_login_flow(menu._management_context(42), data['flow_id'], data['flow_secret']).status == 'cancelled'


@pytest.mark.parametrize('provider', GENERIC)
@pytest.mark.parametrize('wrong_existing', [False, True])
def test_wrong_identity_can_neither_replace_nor_add(provider, wrong_existing):
    key, old = account(provider)
    incoming = dict(old, access_token='new-at', refresh_token='new-rt')
    identity = {'claude': 'email', 'openai': 'workspace_id', 'xai': 'subject', 'cursor': 'subject', 'antigravity': 'project_id'}[provider]
    incoming[identity] = 'other'
    if provider == 'openai':
        incoming['chatgpt_account_id'] = 'other'
        incoming.pop('codexIdentity', None)
        incoming.pop('codexDeviceInstallationId', None)
    if wrong_existing:
        om.add_account(incoming)
    before = copy.deepcopy(om.list_accounts())
    with pytest.raises(ValueError, match='未修改或新增'):
        menu._persist_new_or_stage_overwrite(42, incoming, source='login', relogin=binding(key, old))
    assert om.list_accounts() == before
    assert states.get_state(42) is None


@pytest.mark.parametrize('provider', GENERIC)
@pytest.mark.parametrize('change', [None, 'user', 'delete_recreate'])
def test_bound_confirm_is_atomic_preserves_settings_and_recovers(provider, change):
    key, old = account(provider)
    incoming = dict(old, access_token='new-at', refresh_token='new-rt', enabled=True, models=[], maxConcurrent=1)
    relogin = binding(key, old)
    assert menu._persist_new_or_stage_overwrite(42, incoming, source='login', relogin=relogin) == 'staged'
    nonce = states.get_state(42)['data']['nonce']
    if change == 'user':
        om.set_enabled(key, False, reason='user')
    elif change == 'delete_recreate':
        om.delete_account(key)
        om.add_account(old)
    expected = copy.deepcopy(om.list_accounts())
    menu.on_oauth_overwrite_confirm(42, 100, 'cb', nonce)
    if change:
        assert om.list_accounts() == expected
    else:
        saved = om.get_account(key)
        assert saved['access_token'] == 'new-at'
        assert saved['enabled'] and saved['disabled_reason'] is None
        for field in ['models', 'maxConcurrent', 'extension', 'generationId']:
            assert saved[field] == old[field]
    once = copy.deepcopy(om.list_accounts())
    menu.on_oauth_overwrite_confirm(42, 100, 'cb', nonce)
    assert om.list_accounts() == once


@pytest.mark.parametrize('provider', GENERIC)
def test_exchange_paths_carry_original_binding(isolated_accounts, monkeypatch, provider):
    ctl = isolated_accounts
    key, old = account(provider)
    mock_start(monkeypatch, ctl, provider)
    relogin = binding(key, old)
    menu.on_relogin(42, 100, 'cb', ui.register_code(key), 3, 'invalid')
    incoming = dict(old, access_token='new-at', refresh_token='new-rt')
    if provider == 'claude':
        monkeypatch.setattr(ctl, 'claude_exchange_code', lambda *a: incoming)
        async def profile(*a):
            return {'account': {'email': old['email']}}
        monkeypatch.setattr(ctl, 'claude_fetch_profile', profile)
        menu.on_login_code_input(42, 'code')
    elif provider == 'cursor':
        monkeypatch.setattr(ctl, 'cursor_poll_login', lambda *a: SimpleNamespace(access_token='new-at', refresh_token='new-rt', expires_at_ms=2000000000000))
        monkeypatch.setattr(ctl, 'cursor_subject', lambda token: old['subject'])
        monkeypatch.setattr(ctl, 'cursor_profile', lambda *a, **kw: {'email': old['email']})
        monkeypatch.setattr(ctl, 'cursor_usage', lambda *a: {})
        menu.on_login_cursor_done(42, 100, 'cb')
    else:
        name = 'antigravity_complete_login' if provider == 'antigravity' else provider + '_exchange_code'
        monkeypatch.setattr(ctl, name, lambda *a, **kw: {})
        monkeypatch.setattr(menu, '_' + provider + '_token_to_entry', lambda tok: (incoming, {}))
        data = states.get_state(42)['data']
        text = 'code#' + data['state']
        if provider == 'antigravity':
            monkeypatch.setattr(ctl, 'antigravity_parse_callback', lambda raw: {'code': 'code', 'state': data['state']})
            text = 'http://example.test/callback'
        getattr(menu, 'on_login_' + provider + '_code_input')(42, text)
    state = states.get_state(42)
    assert state['action'] == 'oa_oauth_overwrite_confirm'
    assert state['data']['relogin'] == relogin
    assert state['data']['target_key'] == key
    assert om.get_account(key) == old


@pytest.mark.parametrize('provider', ['openai', 'xai', 'antigravity'])
def test_regeneration_keeps_target(isolated_accounts, monkeypatch, provider):
    key, old = account(provider)
    mock_start(monkeypatch, isolated_accounts, provider)
    menu.on_relogin(42, 100, 'cb', ui.register_code(key))
    before = states.get_state(42)['data']['relogin']
    getattr(menu, 'on_login_' + provider + '_regen')(42, 100, 'cb')
    assert states.get_state(42)['data']['relogin'] == before


@pytest.mark.parametrize('provider', ['workbuddy', 'zhipu'])
@pytest.mark.parametrize('outcome', ['same', 'wrong_new', 'wrong_existing', 'wrong_scope', 'changed', 'cancelled', 'deleted'])
def test_device_save_bound_in_control_not_just_ui(isolated_accounts, monkeypatch, provider, outcome):
    ctl = isolated_accounts
    key, old = account(provider)
    mock_start(monkeypatch, ctl, provider)
    options = {'site': old['site']} if provider == 'zhipu' else {'realm': old['realm']}
    flow = ctl.start_login_flow(context(), OAuthProvider(provider), relogin_account_id=key, **options)
    incoming = dict(old, access_token='new-at', refresh_token='new-rt', models=[], maxConcurrent=1, label='login-default')
    if provider == 'zhipu':
        for field in ['organization_id', 'project_id', 'project_key_id', 'model_key']:
            incoming.pop(field, None)  # real browser login has no selected project/Key
    if outcome == 'wrong_scope':
        incoming['enterprise_id' if provider == 'workbuddy' else 'project_id'] = 'other-scope'
    elif outcome.startswith('wrong'):
        incoming['uid' if provider == 'workbuddy' else 'subject'] = 'other'
        if outcome == 'wrong_existing':
            om.add_account(incoming)
    elif outcome == 'changed':
        om.set_enabled(key, False, reason='user')
    elif outcome == 'deleted':
        om.delete_account(key)
    elif outcome == 'cancelled':
        ctl.cancel_login_flow(context(), flow.flow_id, flow.flow_secret)
    monkeypatch.setattr(ctl.backend, provider + '_poll_login', lambda p: p.update(status='ready', entry=incoming))
    before = copy.deepcopy(om.list_accounts())
    if outcome not in ['same', 'cancelled']:
        with pytest.raises(ManagementError):
            ctl.poll_login_flow(context(), flow.flow_id, flow.flow_secret)
        assert om.list_accounts() == before
        ctl._post_save_account_effects.assert_not_called()
    elif outcome == 'cancelled':
        assert ctl.poll_login_flow(context(), flow.flow_id, flow.flow_secret).status == 'cancelled'
        assert om.list_accounts() == before
    else:
        result = ctl.poll_login_flow(context(), flow.flow_id, flow.flow_secret)
        assert result.status == 'completed' and result.account_id == key
        assert ctl.poll_login_flow(context(), flow.flow_id, flow.flow_secret) == result
        saved = om.get_account(key)
        assert saved['access_token'] == 'new-at'
        assert saved['enabled'] and saved['disabled_reason'] is None
        for field in ['models', 'maxConcurrent', 'label', 'extension', 'generationId']:
            assert saved[field] == old[field]
        ctl._post_save_account_effects.assert_called_once()


@pytest.mark.parametrize('reason', [None, 'user'])
def test_zhipu_management_login_recovers_without_enabling_manual_pause(isolated_accounts, monkeypatch, reason):
    ctl = isolated_accounts
    key, old = account('zhipu', reason, management_status='relogin_required')
    mock_start(monkeypatch, ctl, 'zhipu')
    flow = ctl.start_login_flow(context(), OAuthProvider.ZHIPU, relogin_account_id=key)
    incoming = credential('oauth', model_key='', access_token='new-at')
    monkeypatch.setattr(ctl.backend, 'zhipu_poll_login', lambda p: p.update(status='ready', entry=incoming))
    assert ctl.poll_login_flow(context(), flow.flow_id, flow.flow_secret).account_id == key
    saved = om.get_account(key)
    assert not ctl.account_authentication_failed(saved)
    assert saved['enabled'] is (reason is None)
    assert saved['disabled_reason'] == reason


@pytest.mark.parametrize('realm', ['cn', 'global'])
def test_workbuddy_relogin_uses_original_region(isolated_accounts, monkeypatch, realm):
    ctl = isolated_accounts
    key, _ = account('workbuddy', realm=realm, client_profile='ide' if realm == 'global' else 'cli')
    start = Mock(return_value=dict(status='pending', auth_url='https://example.test/login'))
    monkeypatch.setattr(ctl.backend, 'workbuddy_start_login', start)
    ctl.start_login_flow(context(), OAuthProvider.WORKBUDDY, relogin_account_id=key, realm='global' if realm == 'cn' else 'cn')
    if realm == 'cn':
        start.assert_called_once_with()
    else:
        start.assert_called_once_with(realm='global', client_profile='ide')


def test_notification_and_stale_refresh_use_same_relogin_gate(isolated_accounts, monkeypatch):
    from src.notifications import providers
    key, old = account('claude', None)
    refresh = Mock(side_effect=AssertionError('no manual refresh'))
    monkeypatch.setattr(isolated_accounts, 'force_refresh_raw', refresh)
    assert menu.handle_callback(42, 100, 'cb', 'oa:refresh_token:' + ui.register_code(key) + ':1')
    refresh.assert_not_called()
    assert states.get_state(42) is None
    start = Mock()
    monkeypatch.setattr(menu, 'on_relogin', start)
    providers.REGISTRY['claude'].login(42, 100, 'cb', old)
    start.assert_called_once_with(42, 100, 'cb', ui.register_code(key))
    assert 'reauthorize' not in providers.capabilities(old)
    assert 'reauthorize' in providers.capabilities(dict(old, disabled_reason='auth_error'))


@pytest.mark.parametrize('provider', GENERIC)
def test_failed_exchange_keeps_credentials_and_account_navigation(isolated_accounts, monkeypatch, provider):
    ctl = isolated_accounts
    key, old = account(provider)
    mock_start(monkeypatch, ctl, provider)
    menu.on_relogin(42, 100, 'cb', ui.register_code(key), 3, 'invalid')
    data = states.get_state(42)['data']
    failure = Mock(side_effect=RuntimeError('fixture network failure'))
    if provider == 'cursor':
        monkeypatch.setattr(ctl, 'cursor_poll_login', failure)
        menu.on_login_cursor_done(42, 100, 'cb')
        markup = str(ui.edit.call_args.kwargs)
    else:
        method = 'antigravity_complete_login' if provider == 'antigravity' else provider + '_exchange_code'
        monkeypatch.setattr(ctl, method, failure)
        if provider == 'antigravity':
            monkeypatch.setattr(ctl, 'antigravity_parse_callback', lambda raw: {'code': 'code', 'state': data['state']})
        fn = menu.on_login_code_input if provider == 'claude' else getattr(menu, 'on_login_' + provider + '_code_input')
        fn(42, 'code#' + data['state'])
        markup = str(ui.send_result.call_args.kwargs)
    assert 'oa:relogin_cancel:' + data['relogin']['payload'] in markup
    assert om.get_account(key) == old


def test_workbuddy_worker_stops_on_wrong_identity_with_return_to_account(isolated_accounts, monkeypatch):
    ctl = isolated_accounts
    key, old = account('workbuddy')
    mock_start(monkeypatch, ctl, 'workbuddy')
    monkeypatch.setattr(ctl.backend, 'workbuddy_poll_login', lambda p: p.update(status='ready', entry=dict(old, uid='other')))
    menu.on_relogin(42, 100, 'cb', ui.register_code(key), 3, 'invalid')
    data = states.get_state(42)['data']
    wb._run_login_job(42, wb._login_jobs[42])
    assert states.get_state(42) is None
    assert om.get_account(key) == old and len(om.list_accounts()) == 1
    assert '未修改或新增账户' in ui.edit.call_args.args[2]
    assert 'oa:relogin_cancel:' + data['relogin']['payload'] in str(ui.edit.call_args.kwargs)
    assert ctl.poll_login_flow(menu._management_context(42), data['flow_id'], data['flow_secret']).status == 'cancelled'


@pytest.mark.parametrize('provider', ['workbuddy', 'zhipu'])
def test_two_bound_flows_cannot_replace_each_others_fresh_login(isolated_accounts, monkeypatch, provider):
    ctl = isolated_accounts
    key, old = account(provider)
    mock_start(monkeypatch, ctl, provider)
    flow1 = ctl.start_login_flow(context('admin-one'), OAuthProvider(provider), relogin_account_id=key)
    flow2 = ctl.start_login_flow(context('admin-two'), OAuthProvider(provider), relogin_account_id=key)
    monkeypatch.setattr(ctl.backend, provider + '_poll_login', lambda p: p.update(status='ready', entry=dict(old, access_token='new-at')))
    assert ctl.poll_login_flow(context('admin-one'), flow1.flow_id, flow1.flow_secret).status == 'completed'
    saved = copy.deepcopy(om.get_account(key))
    with pytest.raises(ManagementError):
        ctl.poll_login_flow(context('admin-two'), flow2.flow_id, flow2.flow_secret)
    assert om.get_account(key) == saved
