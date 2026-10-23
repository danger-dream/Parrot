"""Independent security/integration review. Run ONLY through isolated_pytest.py.

Failing cases assert the approved behavior, not a snapshot of the defect.
Real notification dispatch/actions/control are used; only external I/O and the
operation scheduler are substituted. All account values below are fixtures.
"""
import copy
from dataclasses import replace

import pytest

from src import config, oauth_manager as om, state_db, cooldown
from src.notifications import actions, delivery, store, subjects
from src.notifications.events import Effect, NotificationEvent
from src.management_control.oauth.control import OAuthControl
from src.telegram import bot, states, ui
from src.telegram.menus import notification_menu
from src.tests.test_notifications import env, controlled, callback, event


@pytest.fixture
def real_control(env, controlled, monkeypatch):
    control = OAuthControl()
    monkeypatch.setattr(actions, "_control", control)
    monkeypatch.setattr(notification_menu, "_pages", {})
    # Unmodified domain setters still update a private fixture, never live config.
    def update(mutator, **kwargs):
        candidate = copy.deepcopy(env[0])
        mutator(candidate)
        env[0].clear()
        env[0].update(candidate)
        return env[0]
    monkeypatch.setattr(config, "update", update)
    states.clear_all()
    yield control, controlled[3]
    states.clear_all()


def delivered(sample, env):
    ids = store.record(sample.record(), env[0]["notifications"], [11, 22], transactional=True)
    for ident in ids:
        delivery.deliver(ident)
    return [store.get("deliveries", ident) for ident in ids]


def click(row, action, *, payload=None, panel=False):
    mid = store.get("deliveries", row["id"]).get("panel_id") if panel else row["message_id"]
    ref = store.action_ref(row["id"], action, message_id=mid, payload=payload or {})
    cb = callback({**row, "message_id": mid}, ref)
    bot._handle_callback(cb)
    return ref


def confirm_last(row, env):
    p = next(p for m, p in reversed(env[3]) if m == "editMessageReplyMarkup" and p["chat_id"] == row["chat_id"])
    data = p["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    mid = store.get("deliveries", row["id"])["panel_id"]
    bot._handle_callback(callback({**row, "message_id": mid}, data))
    return data


def test_review_quota_disable_failure_is_not_success(env, real_control, monkeypatch):
    """Real quota evaluation returns disable_failed, but nt currently says succeeded."""
    control, ops = real_control
    account = env[0]["oauthAccounts"][0]
    account["allowCredits"] = False
    async def fetch(key):
        return {"five_hour": {"utilization": 100}}
    def cannot_save(*a, **kw):
        raise OSError("fixture disk failure")
    monkeypatch.setattr(control.backend, "fetch_usage_snapshot", fetch)
    monkeypatch.setattr(om, "set_disabled_by_quota", cannot_save)
    row = delivered(event(), env)[0]
    click(row, "refresh_usage")
    ops.jobs[-1]()
    assert env[0]["oauthAccounts"][0]["enabled"] is True
    assert state_db.quota_load("openai:same@example.test:w1")["five_hour_util"] == 100
    receipts = [v for v in store.items("policy").values() if v.get("kind") == "operation"]
    assert receipts[-1]["status"] == "failed", receipts


def test_review_inflight_usage_credential_replacement_rejects_old_snapshot(env, real_control, monkeypatch):
    """Same incarnation, new login during usage I/O must not accept old credential data."""
    control, ops = real_control
    account = env[0]["oauthAccounts"][0]
    account.update(access_token="fixture-old-token", refresh_token="fixture-old-refresh", allowCredits=False)
    key = "openai:same@example.test:w1"
    async def fetch(key):
        # Simulate an independently committed replacement while the old request waits.
        config.update(lambda cfg: cfg["oauthAccounts"][0].update(
            access_token="fixture-new-token", refresh_token="fixture-new-refresh"))
        return {"five_hour": {"utilization": 100}}
    monkeypatch.setattr(control.backend, "fetch_usage_snapshot", fetch)
    row = delivered(event(), env)[0]
    click(row, "refresh_usage")
    ops.jobs[-1]()
    current = env[0]["oauthAccounts"][0]
    assert current["access_token"] == "fixture-new-token"
    assert current["enabled"] is True, (current.get("disabled_reason"), state_db.quota_load(key))
    assert state_db.quota_load(key) is None


def test_review_unknown_provider_cannot_clear_cooldown(env, real_control, monkeypatch):
    """Unknown provider is required to be read-only, including model-scoped events."""
    control, ops = real_control
    account = env[0]["oauthAccounts"][0]
    account.update(provider="future_unregistered", generationId="review-unknown", models=["m"])
    subject = subjects.account(account)
    key = "oauth:" + subject.exact_id
    monkeypatch.setattr(cooldown, "_entries", {(key, "m"): {"cooldown_until": -1, "error_count": 9}})
    sample = NotificationEvent("channel.model.frozen", subject, Effect("confirmed", "model_paused"),
        {"model": "m", "error_code": "upstream", "count": 9}, scope="model", scope_ref="m")
    row = delivered(sample, env)[0]
    # This is not an invented action: it is advertised in the real keyboard.
    if "clear_cooldown" in dict(actions.available(sample.record())):
        click(row, "clear_cooldown")
        confirm_last(row, env)
        ops.jobs[-1]()
    assert cooldown.is_blocked(key, "m"), "unregistered provider got an executable write action"


def test_review_different_benefit_confirmations_are_not_singleflight_equivalent(env, real_control, monkeypatch):
    """Real Claude two-stage plans, two admins, distinct programs share one nt job."""
    control, ops = real_control
    env[0]["oauthAccounts"] = [{"provider": "claude", "email": "c@example.test", "generationId": "review-claude"}]
    calls = []
    async def status(key, program):
        return {"generation": subjects.account(env[0]["oauthAccounts"][0]).generation,
                "organization_uuid": "fixture-org", "cedar_ember": {"next_grant_id": "fixture-grant"}}
    async def redeem(key, program, **kw):
        calls.append(program)
        return {"result": "reset"}
    monkeypatch.setattr(control.backend, "claude_reset_status", status)
    monkeypatch.setattr(control.backend, "claude_reset_eligibility", lambda *a: None)
    monkeypatch.setattr(control.backend, "claude_reset_redeem", redeem)
    sample = replace(event(), subject=subjects.account(env[0]["oauthAccounts"][0]))
    one, two = delivered(sample, env)
    for row, kind in ((one, "claude_reset"), (two, "claude_session_reset")):
        click(row, "benefit_plan", payload={"kind": kind})
        ops.jobs[-1]()
        confirm_last(row, env)  # explanation -> final
        ops.jobs[-1]()
    confirm_last(one, env)  # final consume is queued, not run yet
    pending = len(ops.jobs)
    confirm_last(two, env)
    for job in ops.jobs[pending-1:]:
        job()
    second_panel = store.get("deliveries", two["id"])["panel_id"]
    second_text = next(p["text"] for m, p in reversed(env[3]) if m == "editMessageText" and p["message_id"] == second_panel)
    assert "succeeded" not in second_text or "juniper_tide" in calls, (calls, second_text)
    # Explicit busy/conflict is also safe; silently adopting another intent is not.


def test_review_async_receipt_must_not_overwrite_existing_login_panel(env, real_control, monkeypatch):
    """Run actual Claude legacy reauthorization UI while another nt task is queued."""
    control, ops = real_control
    env[0]["oauthAccounts"] = [{"provider": "claude", "email": "login@example.test", "generationId": "review-login"}]
    # All Telegram I/O goes through the same fake protocol; login URL generation is local.
    monkeypatch.setattr(ui, "api", lambda method, payload: delivery.request(method, payload))
    # A transient failure permits retry; only a later explicit auth failure
    # permits reauthentication. The queued receipt must still respect that UI.
    auth = NotificationEvent("account.auth.refresh_failed", subjects.account(env[0]["oauthAccounts"][0]),
        Effect("unchanged", "not_disabled"), {"error_code": "network_timeout", "retryable": True, "auth_error": False})
    authrow = delivered(auth, env)[0]
    async def refresh(key):
        return "fixture-token"
    monkeypatch.delenv("PARROT_NO_REFRESH", raising=False)
    monkeypatch.setattr(control.backend, "force_refresh", refresh)
    click(authrow, "refresh_token")
    assert 'reauthorize' not in dict(actions.available(store.get('events', authrow['event_id'])))
    env[0]['oauthAccounts'][0].update(enabled=False, disabled_reason='auth_error')
    click(authrow, "reauthorize")
    assert states.get_state(11) is not None
    assert states.get_state(11)["action"] == "oa_login_code"
    panel = store.get("deliveries", authrow["id"])["panel_id"]
    before = next(p for m, p in reversed(env[3]) if m == "editMessageText" and p["message_id"] == panel)
    assert "登录" in before["text"]
    ops.jobs[-1]()
    after = next(p for m, p in reversed(env[3]) if m == "editMessageText" and p["message_id"] == panel)
    assert states.get_state(11)["action"] == "oa_login_code"
    assert after["text"] == before["text"], "late operation receipt erased the active login link and buttons"


def test_review_metadata_only_retry_really_runs_missing_stage(env, real_control, monkeypatch):
    control, ops = real_control
    from src import model_pricing
    env[0]['oauthAccounts'] = [dict(provider='zhipu', site='bigmodel', credential_mode='oauth',
        subject='fixture', project_id='p', organization_id='o', generationId='review-metadata')]
    called = []
    async def catalog(key):
        called.append('catalog')
        return {'action': 'updated'}
    async def metadata():
        called.append('metadata')
        return {'status': 'succeeded'}
    monkeypatch.setattr(om, 'refresh_account_models', catalog)
    monkeypatch.setattr(model_pricing, 'refresh_metadata_after_model_sync', metadata)
    sample = NotificationEvent('operation.attention', subjects.account(env[0]['oauthAccounts'][0]),
        Effect('confirmed', 'saved'), {'operation': '初始化', 'result': '元数据未完成', 'saved': True,
        'stages': [{'name': '模型目录', 'status': 'succeeded'}, {'name': '元数据', 'status': 'failed'}]})
    row = delivered(sample, env)[0]
    click(row, 'sync_models')
    ops.jobs[-1]()
    assert 'metadata' in called, called


@pytest.mark.parametrize('change', ['actor', 'other_admin', 'chat', 'message', 'expired', 'forged'])
def test_review_real_nt_dispatch_rejects_wrong_binding(env, real_control, change):
    _, ops = real_control
    row = delivered(event(), env)[0]
    ref = store.action_ref(row['id'], 'refresh_usage')
    cb = callback(row, ref)
    if change == 'actor': cb['from']['id'] = 99
    elif change == 'other_admin': cb['from']['id'] = 22
    elif change == 'chat': cb['message']['chat']['id'] = 22
    elif change == 'message': cb['message']['message_id'] += 1000
    elif change == 'expired': store.mutate('refs', lambda refs: refs[ref[6:]].update(expires_at=0))
    elif change == 'forged': cb['data'] += 'not-a-valid-reference'
    states.set_state(11, 'pending-original-input', {'keep': 'yes'})
    bot._handle_callback(cb)
    assert not ops.jobs
    assert states.get_state(11)['data'] == {'keep': 'yes'}


@pytest.mark.parametrize('change', ['deleted', 'credential', 'permission', 'recovered'])
def test_review_real_queued_control_rechecks_before_io(env, real_control, monkeypatch, change):
    control, ops = real_control
    calls = []
    async def fetch(key):
        calls.append(key)
        return {'five_hour': {'utilization': 2}}
    monkeypatch.setattr(control.backend, 'fetch_usage_snapshot', fetch)
    row = delivered(event(), env)[0]
    click(row, 'refresh_usage')
    if change == 'deleted': env[0]['oauthAccounts'][0]['generationId'] = 'review-rebuilt'
    elif change == 'credential': env[0]['oauthAccounts'][0]['access_token'] = 'fixture-replaced'
    elif change == 'permission': env[0]['telegram']['adminIds'] = [22]
    elif change == 'recovered':
        store.mutate('incidents', lambda rows: rows[row['incident_id']].update(active=False))
    ops.jobs[-1]()
    assert calls == []


def test_review_exact_workspace_and_renamed_account_singleflight(env, real_control, monkeypatch):
    control, ops = real_control
    from src import channel_state
    account = env[0]['oauthAccounts'][0]
    sibling = {**account, 'workspace_id': 'w2', 'generationId': 'review-w2'}
    env[0]['oauthAccounts'].append(sibling)
    one, two = delivered(event(), env)
    old_subject = store.get('events', one['event_id'])['subject']
    account['email'] = 'renamed@example.test'
    channel_state._install_alias('oauth:openai:same@example.test:w1', 'oauth:openai:renamed@example.test:w1')
    calls = []
    async def fetch(key):
        calls.append(key)
        return {'five_hour': {'utilization': 5}}
    monkeypatch.setattr(control.backend, 'fetch_usage_snapshot', fetch)
    click(one, 'refresh_usage')
    click(two, 'refresh_usage')
    assert len(ops.jobs) == 1
    ops.jobs[-1]()
    assert calls == ['openai:renamed@example.test:w1']
    assert subjects.current(old_subject)['key'] == calls[0]
    assert state_db.quota_load('openai:same@example.test:w2') is None
    for row in (one, two):
        mid = store.get('deliveries', row['id'])['panel_id']
        assert any(m == 'editMessageText' and p['message_id'] == mid and 'succeeded' in p['text'] for m, p in env[3])


@pytest.mark.parametrize('provider', ['claude', 'openai', 'xai', 'cursor', 'antigravity', 'workbuddy', 'zhipu'])
def test_review_account_local_views_are_read_only(env, real_control, monkeypatch, provider):
    account = dict(provider=provider, email='views@example.test', subject='fixture-user', uid='fixture-uid',
        workspace_id='workspace', organization_id='org', project_id='project', realm='cn',
        site='bigmodel', credential_mode='oauth', access_token='fixture-access-secret',
        zcode_token='fixture-zcode-secret', generationId='review-read-' + provider, enabled=True, models=['m'])
    env[0]['oauthAccounts'] = [account]
    before = copy.deepcopy(env[0])
    def forbid(*args, **kwargs):
        pytest.fail('read-only notification page attempted refresh or config mutation')
    for name in ('fetch_usage', 'fetch_usage_snapshot', 'force_refresh', 'ensure_valid_token', 'refresh_account_models', 'quota_monitor_once'):
        monkeypatch.setattr(om, name, forbid)
    monkeypatch.setattr(config, 'update', forbid)
    sample = replace(event(), subject=subjects.account(account)).record()
    sample['incident_id'] = ''
    states.set_state(11, 'other-input', {'keep': True})
    text = []
    for page in ('view', 'auth', 'models', 'records', 'benefits', 'event'):
        text.append(notification_menu._details(sample, page, 11))
    assert env[0] == before
    assert states.get_state(11)['action'] == 'other-input'
    assert 'fixture-access-secret' not in str(text) and 'fixture-zcode-secret' not in str(text)


def test_review_unknown_zhipu_consumption_never_reposts_and_secrets_stay_ephemeral(env, real_control, monkeypatch):
    import json
    import time
    from src.oauth.zhipu import common, actions as zh_actions
    control, ops = real_control
    account = dict(provider='zhipu', site='bigmodel', credential_mode='oauth',
        subject='fixture-user', project_id='p', organization_id='o', generationId='review-zhipu',
        access_token='fixture-private-access', zcode_token='fixture-private-zcode', enabled=True)
    env[0]['oauthAccounts'] = [account]
    posts = []
    expires = (time.time() + 1000) * 1000
    def wire(url, **kw):
        if url.endswith('/status'):
            return {'available_five_hour_resets': [{'expire_at': expires}], 'available_week_resets': [],
                    'latest_five_hour_reset_history': None, 'latest_week_reset_history': None}
        if url.endswith('/use') and kw.get('method') == 'POST':
            posts.append(kw)
            raise common.ZhipuError('reset_use', 'timeout')
        pytest.fail('unexpected external boundary: ' + url)
    monkeypatch.setattr(common, 'request', wire)
    sample = replace(event(), subject=subjects.account(account))
    row, other = delivered(sample, env)
    for recipient in (row, other):
        click(recipient, 'benefit_plan', payload={'kind': 'zhipu_use', 'type': 'FIVE_HOUR', 'index': 0})
        ops.jobs[-1]()
        confirm_last(recipient, env)
        ops.jobs[-1]()
    assert len(posts) == 1
    assert zh_actions.history(account)[-1]['status'] == 'unknown'
    data = json.dumps({d: store.items(d) for d in ('events', 'deliveries', 'policy', 'refs')})
    for forbidden in ('fixture-private-access', 'fixture-private-zcode'):
        assert forbidden not in data
    # Flush private test StateStore and inspect its actual notification files too.
    env[1].flush()
    durable = __import__('pathlib').Path(env[1]._paths['durable']).read_text()
    assert 'fixture-private-access' not in durable and 'fixture-private-zcode' not in durable
    assert 'zhaction_' not in durable and 'notification_' not in json.dumps(store.items('refs'))


def test_review_refresh_protection_rechecked_after_queue(env, real_control, monkeypatch):
    control, ops = real_control
    monkeypatch.delenv('PARROT_NO_REFRESH', raising=False)
    calls = []
    async def refresh(key):
        calls.append(key)
        return 'fixture-token'
    monkeypatch.setattr(control.backend, 'force_refresh', refresh)
    sample = NotificationEvent('account.auth.refresh_failed', event().subject,
        Effect('unchanged', 'not_disabled'), {'error_code': 'network_timeout', 'retryable': True, 'auth_error': False})
    row = delivered(sample, env)[0]
    click(row, 'refresh_token')
    monkeypatch.setenv('PARROT_NO_REFRESH', '1')
    ops.jobs[-1]()
    assert not calls


def test_review_api_channel_generation_and_local_view(env, real_control, monkeypatch):
    from src import channel_state
    from src.telegram.menus import channel_menu
    opened = []
    monkeypatch.setattr(channel_menu, 'open_detail', lambda chat, mid, name: opened.append(name) or True)
    env[0]['channels'] = [{'name': 'review-api', 'generationId': 'review-api-generation',
                           'apiKey': 'fixture-api-private', 'models': [{'alias': 'm', 'real': 'upstream-m'}]}]
    old = subjects.channel('api:review-api')
    notice = NotificationEvent('channel.model.frozen', old, Effect('confirmed', 'model_paused'),
        {'model': 'm', 'error_code': 'upstream'}, scope='model', scope_ref='m')
    sample = notice.record()
    assert 'clear_cooldown' not in dict(actions.available(sample))
    rows = delivered(notice, env)
    env[0]['channels'][0]['name'] = 'renamed-api'
    channel_state._install_alias('api:review-api', 'api:renamed-api')
    current = subjects.current(old)
    assert current['key'] == 'api:renamed-api'
    click(rows[0], 'view')
    assert opened == ['renamed-api']
    labels = [label for label, _ in delivery.layout(sample)]
    assert '🔌 渠道详情' in labels and '📋 账户模型' not in labels
    env[0]['channels'][0]['generationId'] = 'review-api-rebuilt'
    assert subjects.current(old) is None
    click(rows[1], 'view')
    assert opened == ['renamed-api']
    assert '原账户已删除' in [p for m, p in env[3] if m == 'editMessageText'][-1]['text']
    assert 'fixture-api-private' not in str(env[3])


def test_review_same_value_confirmation_is_one_shot(env, real_control):
    control, ops = real_control
    sample = NotificationEvent('account.credits.started', event().subject, Effect('confirmed', 'credits_allowed'), {'metrics': []})
    row = delivered(sample, env)[0]
    click(row, 'stop_credits')
    data = confirm_last(row, env)
    ops.jobs[-1]()
    assert env[0]['oauthAccounts'][0]['allowCredits'] is False
    mid = store.get('deliveries', row['id'])['panel_id']
    bot._handle_callback(callback({**row, 'message_id': mid}, data))
    assert len(ops.jobs) == 1
    assert env[0]['oauthAccounts'][0]['allowCredits'] is False
