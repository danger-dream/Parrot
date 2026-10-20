"""Offline notification contract, lifecycle and Telegram protocol tests."""
import copy
import json
import pytest
from src import config, notifier, state_db
from src.state_store import StateStore
from src.notifications import emit, subjects, store, delivery
from src.notifications.events import NotificationEvent, Effect
from src.notifications import content, providers
from src.telegram.menus import notification_menu


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = copy.deepcopy(config.DEFAULT_CONFIG)
    cfg["telegram"] = {"adminIds": [11, 22]}
    cfg["oauthAccounts"] = [{"provider": "openai", "email": "same@example.test", "workspace_id": "w1", "workspace_name": "Team", "generationId": "nt-g1", "label": "A<b>", "enabled": True}]
    monkeypatch.setattr(config, "get", lambda: cfg)
    db = StateStore(str(tmp_path / "runtime.json"), str(tmp_path / "durable.json"))
    db.start()
    monkeypatch.setattr(state_db, "_store", db)
    queue = []
    monkeypatch.setattr(notifier, "enqueue_delivery", lambda key: queue.append(key) or True)
    calls = []
    def tg(method, payload):
        calls.append((method, payload))
        return {"ok": True, "result": {"message_id": len(calls) + 100}}
    delivery.install(tg, recover=False)
    monkeypatch.setattr(notification_menu, "_answer", lambda cb, message: calls.append(("answer", message)))
    yield cfg, db, queue, calls
    delivery.install(None, recover=False)
    db.close()


def event(kind="account.quota.blocked", **kwargs):
    return NotificationEvent(kind, subjects.account("openai:same@example.test:w1"), Effect("confirmed", "account_paused" if kind.endswith("blocked") else "account_resumed"),
        {"windows": ["5h"]}, **kwargs)


def callback(row, data, actor=None):
    return {"id": "cb", "from": {"id": actor or row["recipient"]}, "data": data,
            "message": {"chat": {"id": row["chat_id"]}, "message_id": row["message_id"]}}


def test_vertical_delivery_view_close_per_recipient(env, monkeypatch):
    from src import oauth_ids
    from src.telegram.menus import oauth_menu
    opened = []
    monkeypatch.setattr(oauth_menu, "open_detail", lambda chat, mid, key: opened.append((chat, mid, key)) or True)
    cfg, db, queue, calls = env
    assert emit(event())
    assert len(queue) == 2
    for key in queue:
        delivery.deliver(key)
    rows = list(store.items("deliveries").values())
    assert all(row["state"] == "sent" for row in rows)
    assert len({r["message_id"] for r in rows}) == 2
    send = next(p for m,p in calls if m == "sendMessage")
    assert "OpenAI" in send["text"] and "A&lt;b&gt;" in send["text"]
    assert "Team" in send["text"] and "oauth:" not in send["text"]
    data = send["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    notification_menu.handle_callback(callback(rows[0], data))
    assert opened == [(rows[0]["chat_id"], rows[0]["message_id"], oauth_ids.account_key(cfg["oauthAccounts"][0]))]
    viewed = store.get("deliveries", rows[0]["id"])
    assert viewed["message_owner"] == "management" and not viewed.get("panel_id")
    deletes = lambda: [p for m, p in calls if m == "deleteMessage"]
    # The notification became the account page: auto-delete/close must not remove it.
    assert delivery.close(rows[0]["id"]) and not deletes()
    assert store.get("deliveries", rows[0]["id"])["state"] == "sent"
    close = next("nt:v1:" + k for k,r in store.items("refs").items() if r["delivery_id"] == rows[1]["id"] and r["action"] == "close")
    notification_menu.handle_callback(callback(rows[1], close))
    assert store.get("deliveries", rows[1]["id"])["state"] == "closed"
    assert [p["message_id"] for p in deletes()] == [rows[1]["message_id"]]
    assert cfg["oauthAccounts"][0]["enabled"] is True


def test_unknown_and_failed_are_not_sent(env):
    _, _, queue, _ = env
    emit(event())
    delivery.install(lambda m,p: None, recover=False)
    delivery.deliver(queue[0])
    delivery.install(lambda m,p: {"ok": False, "error_code": 403}, recover=False)
    delivery.deliver(queue[1])
    assert [store.get("deliveries", k)["state"] for k in queue] == ["unknown", "failed"]


def test_durable_dedup_recovery_and_old_observation(env):
    _, _, queue, _ = env
    emit(event(version=2))
    emit(event(version=2, source="ws"))
    assert len(queue) == 2
    emit(event("account.quota.recovered", version=3))
    assert len(queue) == 4
    emit(event(version=1))
    assert len(queue) == 4
    delivery.deliver(queue[0])
    assert store.get("deliveries", queue[0])["reason"] == "recovered_before_send"
    assert {"local", "ws"} <= set(next(iter(store.items("incidents").values()))["sources"])


def test_initial_healthy_no_recovery_broadcast(env):
    assert not emit(event("account.quota.recovered"))
    assert not env[2]


def test_old_switch_and_explicit_new_switch(env):
    cfg, _, queue, _ = env
    cfg["notifications"]["events"]["quota_disabled"] = False
    assert not emit(event())
    cfg["notifications"]["events"]["account.quota.blocked"] = True
    assert emit(event())
    assert len(queue) == 2


def test_facts_fail_closed_no_raw_exception(env):
    e = event()
    e.facts["access_token"] = "DO_NOT_STORE"
    assert not emit(e)
    assert "DO_NOT_STORE" not in json.dumps(store.items("events"))


@pytest.mark.parametrize("provider", ["claude", "openai", "xai", "cursor", "antigravity", "workbuddy", "zhipu", "virtual"])
def test_registered_provider_same_template(env, provider):
    cfg, _, _, _ = env
    providers.register("virtual", providers.Provider("Virtual Service", identity_fields=("nickname",), capabilities=("quota",)))
    a = {"provider": provider, "email": "test@example.test", "label": "remark", "nickname": "Nick", "site": "zai", "credential_mode": "oauth", "subject": "u", "uid": "u", "realm": "cn", "project_id": "p", "generationId": "nt-" + provider}
    cfg["oauthAccounts"] = [a]
    s = subjects.account(a)
    e = NotificationEvent("account.quota.blocked", s, Effect("confirmed", "account_paused"), {"metrics": [{"metric_id": "credit", "display_name": "积分", "kind": "credits"}]}).record()
    html = content.render(e)
    assert s.service in html and "数值未取得" in html and "0%" not in html
    assert "oauth:" not in html


def test_callback_actor_message_generation_and_expiry(env):
    cfg, _, queue, calls = env
    emit(event())
    delivery.deliver(queue[0])
    row = store.get("deliveries", queue[0])
    ref = next(k for k,v in store.items("refs").items() if v["action"] == "view")
    cb = callback(row, "nt:v1:" + ref, actor=99)
    notification_menu.handle_callback(cb)
    assert calls[-1] == ("answer", "无权限")
    cb["from"]["id"] = row["recipient"]
    cb["message"]["message_id"] += 1
    notification_menu.handle_callback(cb)
    assert "不匹配" in calls[-1][1]
    cfg["oauthAccounts"][0]["generationId"] = "nt-new"
    assert subjects.current(event().subject) is not None
    old = store.get("events", row["event_id"])
    assert subjects.current(old["subject"]) is None
    store.mutate("refs", lambda refs: refs.__setitem__(ref, {**refs[ref], "expires_at": 0}))
    notification_menu.handle_callback(callback(row, "nt:v1:"+ref))
    assert "过期" in calls[-1][1]


def test_restart_marks_sending_unknown(env):
    _, _, queue, _ = env
    emit(event())
    store.update_delivery(queue[0], state="sending")
    assert store.recover_queue() == [queue[1]]
    assert store.get("deliveries", queue[0])["state"] == "unknown"


def test_dynamic_reset_not_in_entitlement_key(env):
    _, _, queue, _ = env
    for reset in ("later1", "later2"):
        emit(NotificationEvent("model.entitlement.blocked", event().subject, Effect("confirmed", "model_paused"), {"model": "m", "retry_at": reset, "error_code": "1311"}, scope="model", scope_ref="m"))
    assert len(queue) == 2


class FakeOperations:
    def __init__(self):
        self.jobs, self.results = [], {}
    def create(self, context, **kw):
        from types import SimpleNamespace
        return SimpleNamespace(id='op-' + str(len(self.jobs)))
    def submit(self, key, fn):
        self.jobs.append(fn)
    def mark_running(self, key):
        self.results[key] = 'running'
    def succeed(self, key, result):
        self.results[key] = result
    def fail(self, key, **kw):
        self.results[key] = 'failed'


@pytest.fixture
def controlled(env, monkeypatch):
    from src.notifications import actions
    from types import SimpleNamespace
    cfg = env[0]
    cfg['oauthAccounts'][0]['allowCredits'] = True
    calls, ops = [], FakeOperations()
    def operation(ctx, key, expected, action):
        calls.append((action, key))
        return {'status': 'succeeded'}
    def credits(ctx, key, expected, **kw):
        calls.append(('set_credits', key, kw))
        cfg['oauthAccounts'][0]['allowCredits'] = False
        return {'allowed': False}
    fake = SimpleNamespace(notification_operation=operation, set_notification_policy=credits)
    monkeypatch.setattr(actions, '_control', fake)
    monkeypatch.setattr(actions, '_operations', ops)
    monkeypatch.setattr(actions, '_active', {})
    monkeypatch.setattr(actions, '_listeners', {})
    monkeypatch.setattr(actions, '_plans', {})
    monkeypatch.setattr(notification_menu, '_confirmations', {})
    return actions, fake, calls, ops


def saved_event(kind, facts=None):
    from src.notifications.catalog import CATALOG
    default = {"account.quota.blocked": {"windows": []}, "account.credits.started": {"metrics": []},
               "account.benefit.granted": {"operation": "领取", "result": "已确认"}}
    e = NotificationEvent(kind, event().subject, Effect('confirmed', CATALOG[kind].effects[0]), facts if facts is not None else default.get(kind, {})).record()
    e['incident_id'] = ''
    return e


def test_action_single_flight_exact_workspace_no_other_jobs(env, controlled):
    actions, _, calls, ops = controlled
    e = saved_event('account.quota.blocked')
    one = actions.execute(e, 'refresh_usage', 11)
    assert actions.execute(e, 'refresh_usage', 22)['id'] == one['id']
    assert len(ops.jobs) == 1 and not calls
    ops.jobs[0]()
    assert calls == [('refresh_usage', 'openai:same@example.test:w1')]
    rows = [v for v in store.items('policy').values() if v.get('kind') == 'operation']
    assert rows[0]['status'] == 'succeeded'


def test_confirmed_set_not_toggle_replay_rejected(env, controlled):
    actions, _, calls, ops = controlled
    e = saved_event('account.credits.started')
    token = actions.prepare(e, 'stop_credits', 11)
    with pytest.raises(Exception):
        actions.execute(e, 'stop_credits', 22, token=token)
    actions.execute(e, 'stop_credits', 11, token=token)
    ops.jobs[0]()
    assert calls[0][2] == {'allow_credits': False}
    assert env[0]['oauthAccounts'][0]['allowCredits'] is False
    with pytest.raises(Exception):
        actions.execute(e, 'stop_credits', 11, token=token)
    assert len(calls) == 1


@pytest.mark.parametrize('change', ['generation', 'permission', 'revision'])
def test_queued_write_revalidates_before_io(env, controlled, change):
    actions, _, calls, ops = controlled
    e = saved_event('account.quota.blocked')
    actions.execute(e, 'refresh_usage', 11)
    if change == 'generation':
        env[0]['oauthAccounts'][0]['generationId'] = 'replacement'
    elif change == 'permission':
        env[0]['telegram']['adminIds'] = [22]
    else:
        env[0]['oauthAccounts'][0]['access_token'] = 'changed-secret'
    ops.jobs[0]()
    assert not calls
    assert list(ops.results.values()) == ['failed']


@pytest.mark.parametrize('status', ['failed', 'unknown'])
def test_action_non_success_is_not_success(env, controlled, status):
    actions, fake, _, ops = controlled
    fake.notification_operation = lambda *a: {'status': status}
    result = []
    actions.execute(saved_event('account.quota.blocked'), 'refresh_usage', 11, on_result=result.append)
    ops.jobs[0]()
    assert result[0]['status'] == status
    assert list(ops.results.values()) == ['failed']


def test_restart_and_expired_confirmation_never_execute(env, controlled, monkeypatch):
    actions, _, calls, ops = controlled
    e = saved_event('account.credits.started')
    token = actions.prepare(e, 'stop_credits', 11)
    monkeypatch.setattr(actions, '_plans', {})
    with pytest.raises(ValueError, match='过期'):
        actions.execute(e, 'stop_credits', 11, token=token)
    assert not calls and not ops.jobs
    store.mutate('policy', lambda rows: rows.update({'operation:lost': {'kind': 'operation', 'status': 'running'}}))
    store.recover_queue()
    assert store.get('policy', 'operation:lost')['status'] == 'unknown'


def test_view_opens_oauth_account_detail_and_preserves_input(env, monkeypatch):
    from src.telegram import states
    from src.telegram.menus import oauth_menu
    rendered = []
    monkeypatch.setattr(oauth_menu, "_edit_cached_detail", lambda *args, **kwargs: rendered.append((args, kwargs)) or True)
    emit(event())
    delivery.deliver(env[2][0])
    row = store.get('deliveries', env[2][0])
    data = next('nt:v1:'+key for key, v in store.items('refs').items() if v['action'] == 'view')
    states.set_state(11, 'unrelated_input', {'keep': 'original'})
    before = copy.deepcopy(states.get_state(11))
    sends = [p for m,p in env[3] if m == 'sendMessage']
    assert sends[0]['reply_markup']['inline_keyboard'][0][0]['text'] == '👤 查看账户'
    notification_menu.handle_callback(callback(row, data))
    key = event().subject.exact_id
    assert rendered == [((row['chat_id'], row['message_id'], key, 1, oauth_menu._FILTER_ALL), {'refresh_quota': False, 'start_view': True})]
    assert states.get_state(11) == before
    notification_menu.handle_callback(callback(row, data))
    notification_menu.handle_callback(callback(row, store.action_ref(row['id'], 'quota')))
    assert len(rendered) == 3 and rendered[1] == rendered[2] == rendered[0]
    assert len([p for m,p in env[3] if m == 'sendMessage']) == len(sends)
    states.clear_all()


def test_view_of_deleted_account_keeps_event_history_panel(env, monkeypatch):
    from src.telegram.menus import oauth_menu
    monkeypatch.setattr(oauth_menu, "open_detail", lambda *args: pytest.fail('deleted account must not open a same-name page'))
    emit(event())
    delivery.deliver(env[2][0])
    row = store.get('deliveries', env[2][0])
    data = next('nt:v1:'+key for key, v in store.items('refs').items() if v['action'] == 'view')
    env[0]['oauthAccounts'][0]['generationId'] = 'nt-g2'
    notification_menu.handle_callback(callback(row, data))
    current = store.get('deliveries', row['id'])
    assert current['panel_id'] and current.get('message_owner') is None
    assert '原账户已删除' in [p for m, p in env[3] if m == 'editMessageText'][-1]['text']


def test_real_operation_ledger_not_notification_list(env, controlled):
    from src.notifications import views
    actions, _, _, ops = controlled
    e = saved_event('account.quota.blocked')
    actions.execute(e, 'refresh_usage', 11)
    ops.jobs[0]()
    text = '\n'.join(views.records(e, 11))
    assert 'refresh_usage' in text and 'succeeded' in text and 'op-0' in text
    assert '额度达到暂停条件' not in text


def test_callbacks_cancel_and_more_pagination(env, controlled):
    actions, _, calls, ops = controlled
    emit(NotificationEvent('account.credits.started', event().subject, Effect('confirmed','credits_allowed'), {'metrics': []}))
    delivery.deliver(env[2][0])
    row = store.get('deliveries', env[2][0])
    ref = store.action_ref(row['id'], 'stop_credits')
    notification_menu.handle_callback(callback(row, ref))
    markup = [p for m,p in env[3] if m == 'editMessageReplyMarkup'][-1]['reply_markup']
    buttons = markup['inline_keyboard'][0]
    assert [b['text'] for b in buttons] == ['确认执行', '取消']
    row['message_id'] = store.get('deliveries', row['id'])['panel_id']
    notification_menu.handle_callback(callback(row, buttons[1]['callback_data']))
    notification_menu.handle_callback(callback(row, buttons[0]['callback_data']))
    assert not ops.jobs and not calls


def test_registered_benefit_query_select_preview_confirm_real_control(env, controlled, monkeypatch):
    from dataclasses import replace
    actions, fake, calls, ops = controlled
    old = providers.REGISTRY['openai']
    def query(*a):
        calls.append('query')
        return {'known': True}
    def plan(*a):
        calls.append('plan')
        return {'plan_token': 'SECRET_PLAN', 'available': True}
    def consume(*a):
        calls.append(('consume', a[-1]))
        return {'status': 'unknown'}
    monkeypatch.setitem(providers.REGISTRY, 'openai', replace(old, capabilities=old.capabilities+('benefits', 'virtual_use'),
        benefit_query=query, benefit_view=lambda v: (['inventory verified'], []), benefit_actions=(('virtual_use', 'Test benefit', plan, consume),)))
    e = saved_event('account.benefit.granted')
    results=[]
    actions.execute(e, 'query_benefits', 11, on_result=results.append)
    ops.jobs[-1]()
    assert calls == ['query']
    assert results[-1]['choices'][0][2]['kind'] == 'virtual_use'
    actions.execute(e, 'benefit_plan', 11, payload={'kind':'virtual_use'}, on_result=results.append)
    ops.jobs[-1]()
    token=results[-1]['confirmation']
    assert calls == ['query','plan']
    assert 'SECRET_PLAN' not in json.dumps(store.items('policy'))
    actions.execute(e,'benefit_execute',11,token=token,on_result=results.append)
    ops.jobs[-1]()
    assert calls[-1] == ('consume','SECRET_PLAN') and results[-1]['status']=='unknown'
    with pytest.raises(Exception):
        actions.execute(e,'benefit_execute',11,token=token)


def test_write_failure_and_unknown_telegram_do_not_retry(env, monkeypatch):
    cfg, db, queue, calls = env
    monkeypatch.setattr(db, 'write_snapshot', lambda *a, **kw: (_ for _ in ()).throw(OSError('fixture')))
    assert not emit(event()) and not queue
    assert cfg['oauthAccounts'][0]['enabled']


def test_queue_full_allows_next_observation(env, monkeypatch):
    monkeypatch.setattr(notifier, 'enqueue_delivery', lambda key: False)
    assert not emit(event())
    assert {r['reason'] for r in store.items('deliveries').values()} == {'queue_full'}
    monkeypatch.setattr(notifier, 'enqueue_delivery', lambda key: env[2].append(key) or True)
    assert emit(event()) and len(env[2]) == 2


def test_per_recipient_mute_close_and_recovery(env):
    emit(event())
    for key in list(env[2]): delivery.deliver(key)
    row=store.get('deliveries',env[2][0])
    ref=store.action_ref(row['id'],'mute1')
    notification_menu.handle_callback(callback(row,ref))
    assert env[0]['oauthAccounts'][0]['enabled']
    emit(event('account.quota.recovered'))
    recovery=env[2][-2:]
    for key in recovery: delivery.deliver(key)
    assert all(store.get('deliveries',key)['state']=='sent' for key in recovery)


def test_telegram_429_bounded_and_failed_close_truthful(env, monkeypatch):
    timers=[]
    clock = [delivery.time.time()]
    monkeypatch.setattr(delivery.time, 'time', lambda: clock[0])
    class Timer:
        def __init__(self, delay, fn, args=()): timers.append((fn,args))
        def start(self): pass
    monkeypatch.setattr(delivery.threading,'Timer',Timer)
    emit(event())
    key=env[2][0]
    delivery.install(lambda m,p:{'ok':False,'error_code':429,'parameters':{'retry_after':1}},recover=False)
    delivery.deliver(key)
    for index in range(2):
        clock[0] += 1  # Fire only when the durable retry_at deadline is reached.
        fn,args=timers[index];fn(*args);delivery.deliver(key)
    assert len(timers)==2 and store.get('deliveries',key)['attempts']==3
    store.update_delivery(key,state='sent',message_id=501)
    assert '未允许删除' in delivery.close(key)
    assert store.get('deliveries',key)['state']=='sent'


def test_identity_conflict_legacy_restart_and_long_html(env, monkeypatch):
    cfg=env[0]
    old=event().subject
    cfg['oauthAccounts'].append({**cfg['oauthAccounts'][0], 'workspace_id':'w2'})
    assert subjects.current(old) is None  # duplicated generation is ambiguous, never first-match
    cfg['oauthAccounts']=cfg['oauthAccounts'][:1]
    cfg['oauthAccounts'][0].pop('generationId')
    from src import channel_state
    legacy=subjects.account(cfg['oauthAccounts'][0])
    monkeypatch.setattr(channel_state,'_legacy_oauth_generations',{})
    assert subjects.current(legacy) is None
    cfg['oauthAccounts'][0]['label']='<i>&'*500
    html=content.render(NotificationEvent('account.quota.blocked',subjects.account(cfg['oauthAccounts'][0]),Effect('unknown','account_paused'),{'metrics':[{'metric_id':'unknown','display_name':'unknown','kind':'credits'}]}).record())
    assert len(html)<3900 and '<i>' not in html and '数值未取得' in html and '0%' not in html


def test_store_and_request_details_read_only(env, monkeypatch):
    from src.notifications import views
    from src.management_control.observability.logs import LogsControl
    monkeypatch.setattr(LogsControl,'detail',lambda self,ctx,rid:{'log':{'id':rid,'status':'success','requestedModel':'m'},'attempts':[{}]})
    e=saved_event('request.model.changed',{'request_id':'rid','requested_model':'m','outbound_model':'m','actual_model':'n'})
    assert '关联请求实际记录' in '\n'.join(views.request_lines(e,11))
    assert 'rid' in '\n'.join(views.request_lines(e,11))
    text='\n'.join(views.storage_lines())
    assert '配置启用' in text and '本进程已初始化' in text
