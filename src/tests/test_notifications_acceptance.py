"""Approved notification acceptance matrix; isolated state, fake Telegram/control only."""
import copy
import json
import os
from pathlib import Path
from dataclasses import replace
import pytest
from src import notifier, state_db, config
from src.notifications import emit, subjects, store, delivery, actions, content, reporting, providers
from src.notifications.catalog import CATALOG, enabled, Spec, register
from src.notifications.events import NotificationEvent, Effect, Subject
from src.telegram.menus import notification_menu
from src.tests.test_notifications import env, controlled, event, callback, saved_event


SCENES = {
    'account.auth.refreshed': ({'expires_at': '未取得'}, 'confirmed', 'auth_refreshed'),
    'account.auth.refresh_failed': ({'error_code': 'network_timeout', 'retryable': True, 'auth_error': False, 'advice': '稍后重试'}, 'unchanged', 'not_disabled'),
    'account.auth.recovered': ({'expires_at': '2027-01-01T00:00:00Z'}, 'confirmed', 'auth_refreshed'),
    'account.quota.blocked': ({'windows': ['5h'], 'threshold': 95, 'metrics': [{'metric_id': '5h', 'display_name': '5小时', 'kind': 'percent', 'used': 96, 'unit': '%'}]}, 'confirmed', 'account_paused'),
    'account.quota.recovered': ({'windows': [], 'metrics': []}, 'confirmed', 'still_paused'),
    'model.quota.blocked': ({'models': ['m'], 'pool': '自有模型额度池', 'count': 1}, 'confirmed', 'model_paused'),
    'model.quota.recovered': ({'models': ['m'], 'pool': '自有模型额度池', 'count': 1}, 'confirmed', 'model_resumed'),
    'account.credits.started': ({'metrics': []}, 'confirmed', 'credits_allowed'),
    'account.action.attention': ({'operation': '自动签到', 'result': 'unknown', 'error_code': 'result_unknown'}, 'unknown', 'action_unknown'),
    'account.benefit.granted': ({'operation': '领取', 'result': '新增尚未核实', 'inventory': [{'name': '周卡库存', 'count': 2}]}, 'confirmed', 'benefit_saved'),
    'account.catalog.changed': ({'added': ['new'], 'removed': ['old']}, 'confirmed', 'catalog_updated'),
    'request.model.changed': ({'requested_model': 'asked', 'outbound_model': 'sent', 'actual_model': 'reported', 'request_id': 'safe-rid'}, 'confirmed', 'model_changed'),
    'request.model.conflict': ({'requested_model': 'asked', 'outbound_model': 'sent', 'signals': [{'source': 'header', 'models': ['a']}, {'source': 'body', 'models': ['b']}]}, 'confirmed', 'model_conflict'),
    'channel.model.frozen': ({'model': 'm', 'count': 6, 'error_code': 'repeated_upstream_failure'}, 'confirmed', 'model_paused'),
    'channel.model.recovered': ({'model': 'm', 'previous': '永久冻结'}, 'confirmed', 'model_resumed'),
    'model.entitlement.blocked': ({'model': 'm', 'error_code': '1311', 'retry_at': '2027-01-01T00:00:00Z'}, 'confirmed', 'model_paused'),
    'model.entitlement.recovered': ({'model': 'm', 'previous': '套餐排除冷却'}, 'confirmed', 'model_resumed'),
    'routing.unavailable': ({'model': 'm', 'ingress': 'responses_ws', 'exclusions': [{'subject': 'Fixture', 'reason': 'disabled'}]}, 'confirmed', 'route_blocked'),
    'system.responses_store.failed': ({'model': 'm', 'response_id': 'resp_fixture', 'error_code': 'OSError'}, 'failed', 'history_unavailable'),
    'operation.completed': ({'operation': '初始化', 'result': '各阶段已完成', 'saved': True, 'stages': [{'name': '模型目录', 'status': 'succeeded'}]}, 'confirmed', 'saved'),
    'operation.attention': ({'operation': '初始化', 'result': '部分完成', 'saved': True, 'stages': [{'name': '模型目录', 'status': 'failed'}, {'name': '额度快照', 'status': 'succeeded'}]}, 'confirmed', 'saved'),
}


def scene(kind):
    facts, status, code = SCENES[kind]
    subject = subjects.system() if kind.startswith('system.') else subjects.system('请求路由', kind='route') if kind.startswith('routing.') else event().subject
    scope = 'model' if kind.startswith(('model.', 'channel.')) else 'account'
    return NotificationEvent(kind, subject, Effect(status, code), copy.deepcopy(facts), scope=scope,
                             scope_ref='m' if scope == 'model' else '', occurred_at=1792108800)


@pytest.mark.parametrize('kind', SCENES)
def test_every_scene_has_typed_facts_safe_buttons_and_effect(env, controlled, kind):
    record = scene(kind).record()
    ids = store.record(record, env[0]['notifications'], [11], transactional=True)
    keyboard = delivery.keyboard(ids[0], record)
    buttons = [b for row in keyboard['inline_keyboard'] for b in row]
    assert 2 <= len(buttons) <= 6
    assert buttons[-1]['text'] == '✖ 关闭' and buttons[0]['text'] != '✖ 关闭'
    assert all(len(b['callback_data'].encode()) <= 64 for b in buttons)
    rendered = content.render(record)
    assert content.effect_text(record['effect']) in rendered
    assert 'oauth:' not in rendered and 'api:' not in rendered and '本地编号' not in rendered
    assert not any(code in rendered for code in ('_disabled', 'background', 'responses_ws'))
    assert len(rendered) < 3900
    with pytest.raises(ValueError):
        replace(scene(kind), facts={}).record()


def test_all_scene_template_snapshots(env, controlled):
    assert set(SCENES) == set(CATALOG)
    snapshots = {}
    for kind in SCENES:
        record = scene(kind).record()
        ids = store.record(record, env[0]['notifications'], [11], transactional=True)
        snapshots[kind] = {'text': content.render(record), 'actions': [b['text'] for row in delivery.keyboard(ids[0], record)['inline_keyboard'] for b in row]}
    path = Path(__file__).parent / 'fixtures' / 'notifications' / 'scenes.json'
    if os.environ.get('PARROT_UPDATE_NOTIFICATION_SNAPSHOTS') == '1':
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(snapshots, ensure_ascii=False, indent=2) + '\n')
    assert json.loads(path.read_text()) == snapshots


@pytest.mark.parametrize('kind', ['account.quota.recovered', 'model.quota.recovered', 'account.auth.recovered', 'channel.model.recovered', 'model.entitlement.recovered'])
@pytest.mark.parametrize('status', ['unknown', 'failed', 'unchanged'])
def test_unverified_recovery_rejected(env, kind, status):
    sample = scene(kind)
    with pytest.raises(ValueError):
        replace(sample, effect=Effect(status, sample.effect.code)).record()


def test_scene_specific_copy_and_long_fallback_truth(env):
    assert '重试' not in content.render(scene('account.auth.refreshed').record())
    assert '套餐开放前跳过' not in content.render(scene('model.entitlement.recovered').record())
    assert '累计失败次数：6' in content.render(scene('channel.model.frozen').record())
    subject = replace(event().subject, scopes=tuple(('作用域'*50, '<&>'*200) for _ in range(60)))
    e = NotificationEvent('account.quota.blocked', subject, Effect('unknown', 'account_paused'), {'windows': []}).record()
    text = content.render(e)
    assert '实际影响尚未确认' in text and '（待核实）' in text
    assert '已暂停调用此账号' not in text and len(text) < 3900


@pytest.mark.parametrize('kind,facts', [
    ('request.model.changed', {'requested_model':'x', 'outbound_model':'x', 'actual_model':'X'}),
    ('request.model.conflict', {'requested_model':'x', 'outbound_model':'x', 'signals':[{'source':'a', 'models':['x']}]}),
    ('account.auth.refresh_failed', {'error_code':'Bearer secret', 'retryable':True, 'auth_error':False}),
    ('account.catalog.changed', {'added':[], 'removed':[]}),
])
def test_invalid_scene_combinations_fail_closed(env, kind, facts):
    with pytest.raises(ValueError):
        replace(scene(kind), facts=facts).record()


def test_future_event_registration_requires_contract(env, monkeypatch):
    with pytest.raises(ValueError):
        register('virtual.empty', Spec('Empty', 'virtual', None, ()))
    monkeypatch.setattr('src.notifications.catalog.CATALOG', dict(CATALOG))
    register('virtual.typed', Spec('Virtual fact', 'virtual', None, ('result',), required=('result',), effects=('saved',)))


def test_actual_disk_restart_dedup_receipts_refs_and_confirmation(env, controlled, monkeypatch):
    from src.state_store import StateStore
    cfg, db, queue, calls = env
    emit(event(version=5))
    first, second = queue
    delivery.deliver(first)
    store.update_delivery(second, state='sending')
    row = store.get('deliveries', first)
    ref = next('nt:v1:'+k for k,v in store.items('refs').items() if v['action']=='mute')
    paths = dict(db._paths)
    db.close()
    restarted = StateStore(paths['runtime'], paths['durable']); restarted.start()
    monkeypatch.setattr(state_db, '_store', restarted)
    try:
        queue.clear()
        delivery.install(delivery.transport())
        assert not queue and store.get('deliveries', second)['state'] == 'unknown'
        assert store.get('deliveries', first)['message_id'] == row['message_id']
        assert not emit(event(version=6)) and not queue
        notification_menu.handle_callback(callback(row, ref))
        assert store.get('deliveries', first)['panel_id']
    finally:
        restarted.close()


def test_episodic_new_grants_not_suppressed_or_capacity_leak(env, monkeypatch):
    monkeypatch.setattr(store, 'MAX_INCIDENTS', 3)
    monkeypatch.setattr(store, 'MAX_EVENTS', 4)
    for i in range(9):
        value = replace(scene('account.benefit.granted'), correlation_ref=str(i))
        assert emit(value)
        for key in env[2][-2:]:
            delivery.deliver(key)
            assert store.get('deliveries', key)['state'] == 'sent'
        assert not emit(value)
    assert len(store.items('incidents')) <= 3
    assert len(store.items('events')) <= 4
    assert not any(v['active'] for v in store.items('incidents').values())


def test_old_success_switch_delete_180_and_registry_record_only_default(env, monkeypatch):
    timers=[]
    class Timer:
        def __init__(self, delay, fn, args=()): timers.append((delay,fn,args))
        def start(self): pass
    monkeypatch.setattr(delivery.threading, 'Timer', Timer)
    assert CATALOG['account.auth.refreshed'].push is False
    assert enabled('account.auth.refreshed', {'events': {'oauth_refreshed': True}})
    assert emit(scene('account.auth.refreshed'))
    for key in env[2]: delivery.deliver(key)
    assert [v[0] for v in timers] == [180, 180]
    timers[0][1](*timers[0][2])
    assert store.get('deliveries', env[2][0])['state']=='closed'
    env[0]['notifications']['events']['oauth_refreshed']=False
    assert not emit(replace(scene('account.auth.refreshed'), correlation_ref='next'))


def test_diagnostics_explicit_resend_once_never_business(env, controlled):
    emit(event())
    delivery.install(lambda m,p:None, recover=False)
    delivery.deliver(env[2][0])
    failed=store.get('deliveries',env[2][0])
    def tg(method,payload):
        env[3].append((method,payload)); return {'ok':True,'result':{'message_id':301}}
    delivery.install(tg,recover=False)
    notification_menu.handle_callback({'id':'diag','data':'nt:diagnostics','from':{'id':11},'message':{'chat':{'id':11},'message_id':50}})
    diagnostic=next(v for v in store.items('deliveries').values() if v.get('transactional'))
    assert diagnostic['state']=='sent'
    data=next('nt:v1:'+k for k,v in store.items('refs').items() if v['action']=='resend')
    notification_menu.handle_callback(callback(diagnostic,data))
    confirm=next('nt:v1:'+k for k,v in store.items('refs').items() if v['action']=='confirm_resend')
    notification_menu.handle_callback(callback(diagnostic,confirm))
    size=len(env[2])
    notification_menu.handle_callback(callback(diagnostic,confirm))
    assert len(env[2])==size and not controlled[3].jobs
    assert len([v for v in store.items('deliveries').values() if v.get('resend_of')==failed['id']])==1


def test_api_channel_exact_view_and_stale_generation(env, monkeypatch):
    from src import channel_state
    from src.telegram.menus import channel_menu
    opened=[]
    monkeypatch.setattr(channel_menu,'open_detail',lambda chat,mid,name: opened.append(name) or True)
    cfg=env[0]
    cfg['channels']=[{'name':'Exact API','generationId':'nt-api','models':[{'alias':'m','real':'upstream'}],'enabled':True}]
    subject=subjects.channel('api:Exact API')
    sample=replace(scene('channel.model.frozen'),subject=subject)
    emit(sample);delivery.deliver(env[2][0])
    row=store.get('deliveries',env[2][0])
    send=next(p for m,p in env[3] if m=='sendMessage')
    assert send['reply_markup']['inline_keyboard'][0][0]['text']=='🔌 渠道详情'
    notification_menu.handle_callback(callback(row,send['reply_markup']['inline_keyboard'][0][0]['callback_data']))
    assert opened==['Exact API']
    cfg['channels'][0]['generationId']='nt-api-rebuilt'
    assert subjects.current(subject) is None
    assert subjects.channel(subject.generation).generation == subject.generation
    assert subjects.current(subjects.channel(subject.generation)) is None


def test_routing_buttons_open_log_model_and_key_below_notification(env, controlled, monkeypatch):
    from src.telegram import states, ui
    from src.telegram.menus import logs_menu, model_center_menu, apikey_menu
    opened = []
    monkeypatch.setattr(logs_menu, 'show_detail', lambda chat, mid, cb, short: opened.append(('log', mid, ui.resolve_code(short))))
    monkeypatch.setattr(model_center_menu, 'open_model', lambda chat, mid, model: opened.append(('model', mid, model)) or True)
    monkeypatch.setattr(apikey_menu, 'open_detail', lambda chat, mid, name: opened.append(('key', mid, name)) or name == 'msdev')
    sample = scene('routing.unavailable')
    sample = replace(sample, facts={**sample.facts, 'api_key_name': 'msdev', 'request_id': 'rid-1'})
    emit(sample); delivery.deliver(env[2][0])
    row = store.get('deliveries', env[2][0])
    send = next(p for m, p in env[3] if m == 'sendMessage')
    labels = [b['text'] for r in send['reply_markup']['inline_keyboard'] for b in r]
    assert labels == ['📜 调用日志', '🧬 模型详情', '🔑 Key：msdev', '🔕 暂停提醒', '✖ 关闭']
    refs = {b['text']: b['callback_data'] for r in send['reply_markup']['inline_keyboard'] for b in r}
    states.set_state(11, 'unrelated', {'value': 'keep'})
    try:
        for label in labels[:3]:
            notification_menu.handle_callback(callback(row, refs[label]))
        # Buttons on notifications sent before the redesign reach the same pages.
        for legacy in ('routes', 'request'):
            notification_menu.handle_callback(callback(row, store.action_ref(row['id'], legacy)))
        assert [(kind, value) for kind, mid, value in opened] == [
            ('log', 'rid-1'), ('model', 'm'), ('key', 'msdev'), ('model', 'm'), ('log', 'rid-1')]
        assert row['message_id'] not in {mid for _, mid, _ in opened} and len({mid for _, mid, _ in opened}) == 5
        assert not any(m.startswith('edit') and p.get('message_id') == row['message_id'] for m, p in env[3])
        assert states.get_state(11)['data'] == {'value': 'keep'}
        env[0]['apiKeys'] = {}
        monkeypatch.setattr(apikey_menu, 'open_detail', lambda chat, mid, name: False)
        notification_menu.handle_callback(callback(row, refs['🔑 Key：msdev']))
        assert '已被删除或改名' in [p for m, p in env[3] if m == 'editMessageText'][-1]['text']
    finally:
        states.clear_all()


def test_routing_reasons_are_grouped_translated_and_advised(env):
    exclusions = [{'subject': 'OpenAI · a@example.test（Personal）', 'reason': 'global_model_disabled'},
                  {'subject': 'Antigravity · a@example.test', 'reason': 'source_model_disabled'},
                  {'subject': 'Cursor · a@example.test', 'reason': 'global_model_disabled'},
                  {'subject': 'Claude · c@example.test', 'reason': 'cooldown', 'detail': 'permanent'},
                  {'subject': 'Claude · d@example.test', 'reason': 'disabled_reason', 'detail': 'quota'}]
    sample = replace(scene('routing.unavailable'), facts={'model': 'gpt-x', 'ingress': 'responses', 'exclusions': exclusions,
                                                          'api_key_name': 'msdev', 'request_id': 'rid'})
    text = content.render(sample.record())
    lines = text.splitlines()
    assert lines[:3] == ['❌ <b>无可用渠道 · gpt-x</b>', '来源 Key：msdev', '入口：Responses API']
    assert lines[4:7] == ['模型已在模型中心全局停用（2 个渠道）', '· OpenAI · a@example.test（Personal）', '· Cursor · a@example.test']
    assert '· Claude · c@example.test（永久冻结）' in lines and '· Claude · d@example.test（额度用尽）' in lines
    assert '处理：在模型详情中启用该模型，或让 msdev 改用其他模型。' in text
    assert '_disabled' not in text and 'rid' not in text and '本地编号' not in text
    none = replace(sample, facts={**sample.facts, 'exclusions': []})
    assert '没有任何渠道提供此模型' in content.render(none.record())


def test_more_operations_all_reachable_and_confirmation_cancel(env, controlled, monkeypatch):
    choices=[('quota','额度'),('auth','授权'),('models','模型'),('records','操作记录'),('event','事件'),('refresh_usage','刷新'),('sync_models','同步')]
    monkeypatch.setattr(actions,'available',lambda e: choices)
    emit(event());delivery.deliver(env[2][0]); row=store.get('deliveries',env[2][0])
    more=store.action_ref(row['id'],'more')
    notification_menu.handle_callback(callback(row,more))
    markup=[p for m,p in env[3] if m=='editMessageText'][-1]['reply_markup']
    assert any(b['text']=='下一页' for r in markup['inline_keyboard'] for b in r)
    next_data=next(b['callback_data'] for r in markup['inline_keyboard'] for b in r if b['text']=='下一页')
    row['message_id']=store.get('deliveries',row['id'])['panel_id']
    notification_menu.handle_callback(callback(row,next_data))
    markup=[p for m,p in env[3] if m=='editMessageText'][-1]['reply_markup']
    assert any(b['text']=='同步' for r in markup['inline_keyboard'] for b in r)


def test_claude_two_stage_confirmation_and_no_plan_secret_persistence(env, controlled):
    cfg=env[0];cfg['oauthAccounts']=[{'provider':'claude','email':'c@example.test','generationId':'nt-claude'}]
    _,fake,calls,ops=controlled
    fake.plan_claude_reset=lambda *a: {'available':True,'plan_token':'SECRET_EXPLAIN','program':'cedar_ember'}
    def confirm(*args): calls.append('explain-confirmed');return {'plan_token':'SECRET_FINAL','program':'cedar_ember','grant_id':'safe-grant'}
    def execute(*args): calls.append(('consume',args[-1]));return {'result':'unconfirmed'}
    fake.confirm_claude_reset=confirm;fake.execute_claude_reset=execute
    sample=replace(scene('account.quota.blocked'),subject=subjects.account(cfg['oauthAccounts'][0])).record();sample['incident_id']=''
    results=[]
    actions.execute(sample,'benefit_plan',11,payload={'kind':'claude_reset'},on_result=results.append);ops.jobs[-1]()
    first=results[-1]['confirmation'];assert not calls
    actions.execute(sample,'benefit_execute',11,token=first,on_result=results.append);ops.jobs[-1]()
    final=results[-1]['confirmation'];assert calls==['explain-confirmed']
    actions.execute(sample,'benefit_execute',11,token=final,on_result=results.append);ops.jobs[-1]()
    assert calls[-1]==('consume','SECRET_FINAL') and results[-1]['status']=='unknown'
    assert 'SECRET' not in json.dumps({d:store.items(d) for d in ('events','deliveries','policy','refs')})
    with pytest.raises(Exception): actions.execute(sample,'benefit_execute',11,token=final)


def test_reauthorize_preserves_existing_input_and_exact_service(env, controlled, monkeypatch):
    from src.telegram import states
    calls=[]
    monkeypatch.setitem(providers.REGISTRY,'openai',replace(providers.REGISTRY['openai'],login=lambda *a:calls.append(a)))
    sample=scene('account.auth.refresh_failed').record();sample['incident_id']=''
    env[0]['oauthAccounts'][0].update(enabled=False, disabled_reason='auth_error')
    states.set_state(11,'other',{'keep':1})
    try:
        with pytest.raises(ValueError): actions.reauthorize(sample,11,11,77,'cb')
        assert not calls
        states.clear_all()
        actions.reauthorize(sample,11,11,77,'cb')
        assert calls[0][-1]['workspace_id']=='w1'
    finally: states.clear_all()


def test_muted_recipient_upgrade_and_family_disabled_recovery(env):
    emit(event());first=store.get('deliveries',env[2][0])
    store.mutate('policy',lambda rows:rows.update({f"mute:11:{first['incident_id']}":{'until':9999999999}, f"recipient:22:{event().subject.generation}:quota":{'disabled':True}}))
    env[2].clear()
    emit(replace(event(), facts={'windows':['5h','7d']}, severity='error'))
    assert len(env[2])==1
    delivery.deliver(env[2][0]);assert store.get('deliveries',env[2][0])['state']=='sent'
    emit(event('account.quota.recovered'))
    assert len(env[2])==2 and store.get('deliveries',env[2][-1])['recipient']==11


def test_refresh_failure_actual_committed_truth(env, monkeypatch):
    from src import oauth_manager as om, oauth_refresh_failure
    import httpx
    account=env[0]['oauthAccounts'][0];key='openai:same@example.test:w1'
    expected=om.account_state_key(account)
    calls=[]
    def setter(*args,**kwargs):calls.append((args,kwargs));raise OSError('SECRET')
    monkeypatch.setattr(om,'set_enabled',setter)
    req=httpx.Request('POST','https://auth.example.test/token')
    failure=httpx.HTTPStatusError('SECRET',request=req,response=httpx.Response(401,request=req))
    info,effect=oauth_refresh_failure.apply(key,failure,expected_state_key=expected)
    assert info.auth_error and effect==Effect('failed','disable_failed') and len(calls)==1
    report=NotificationEvent('account.auth.refresh_failed',subjects.account(account),effect,{'error_code':info.code,'retryable':info.retryable,'auth_error':info.auth_error}).record()
    assert '暂停账号时保存失败' in content.render(report) and account['enabled']
    calls.clear()
    info,effect=oauth_refresh_failure.apply(key,httpx.ReadTimeout('SECRET'),expected_state_key=expected)
    assert effect==Effect('unchanged','not_disabled') and not calls


def test_auth_recovery_only_once_across_credential_and_background(env):
    sample=scene('account.auth.refresh_failed');emit(sample)
    reporting.refreshed('openai:same@example.test:w1',source='credential_commit',recovery_only=True)
    reporting.refreshed('openai:same@example.test:w1',source='background')
    assert len(env[2])==4
    assert [e['event_type'] for e in store.items('events').values()]==['account.auth.refresh_failed','account.auth.recovered']


@pytest.mark.skipif(os.environ.get("PARROT_UPDATE_NOTIFICATION_MENU") != "1", reason="explicit scoped snapshot regeneration only")
def test_regenerate_notification_settings_trace(monkeypatch):
    from src.tests.test_tg_contract_system_settings import CASES, _notifications
    from src.tests.test_tg_contract_system_helpers import SystemEnv, actual_case
    case = next(c for c in CASES if c["caseId"] == "TG-SYS-04.001-notifications")
    fixture = SystemEnv(case, monkeypatch)
    _notifications(fixture)
    actual = actual_case(case, fixture)
    path = Path(__file__).parent / "fixtures" / "tg_contract" / "notification-2026-10-17" / "overrides" / "system.jsonl"
    # The archive stays immutable. Only the approved notification-settings overlay changes.
    path.write_text(json.dumps(actual, ensure_ascii=False, separators=(",", ":")) + "\n")


def test_shared_task_returns_terminal_receipt_to_each_actor(env, controlled):
    _,_,calls,ops=controlled
    sample=saved_event('account.quota.blocked');one=[];two=[]
    a=actions.execute(sample,'refresh_usage',11,on_result=one.append)
    b=actions.execute(sample,'refresh_usage',22,on_result=two.append)
    assert a['id']==b['id'] and len(ops.jobs)==1
    ops.jobs[0]()
    assert one[0]['status']==two[0]['status']=='succeeded' and len(calls)==1


def test_panel_edit_unknown_not_reported_opened(env):
    emit(event());delivery.deliver(env[2][0]);row=store.get('deliveries',env[2][0])
    ref=store.action_ref(row['id'],'event')
    delivery.install(lambda method,payload: {'ok':True,'result':{'message_id':700}} if method=='sendMessage' else None,recover=False)
    notification_menu.handle_callback(callback(row,ref))
    latest=store.get('deliveries',row['id'])
    assert latest['state']=='sent' and latest['panel_state']=='unknown'
    assert '未确认' in env[3][-1][1] and '已打开' not in env[3][-1][1]


def test_all_four_store_routes_share_system_incident_not_provider(env):
    from src.notifications import store_failed
    for source in ('native_responses','responses_nonstream','chat_responses_stream','anthropic_responses_stream'):
        store_failed(model='m',api_key_name='display-only',response_id='safe-id',error=OSError('SECRET_PATH'),source=source)
    assert len(env[2])==2
    records=list(store.items('events').values())
    assert len(records)==4 and all(e['subject']['kind']=='system' and e['effect']['code']=='history_unavailable' for e in records)
    assert 'SECRET_PATH' not in json.dumps(records)
    assert len(next(iter(store.items('incidents').values()))['sources'])==4


def test_generation_rename_retains_incident_and_historical_name(env,monkeypatch):
    from src import channel_state
    original=event().subject
    emit(event())
    account=env[0]['oauthAccounts'][0]
    old_key='oauth:openai:same@example.test:w1'
    account['email']='renamed@example.test';account['label']='New Name'
    monkeypatch.setattr(channel_state,'_aliases',dict(channel_state._aliases))
    channel_state._install_alias(old_key,'oauth:openai:renamed@example.test:w1')
    renamed=subjects.account(account)
    assert subjects.current(original)['account']['label']=='New Name'
    assert not emit(replace(event(),subject=renamed))
    stored=next(iter(store.items('events').values()))
    assert stored['subject']['label']=='A<b>'
    assert store.incident_for('account.quota.blocked',original)==store.incident_for('account.quota.blocked',renamed)


def test_registered_units_unknown_never_zero(env):
    ag=providers.REGISTRY['antigravity'].facts({'antigravity':{'known':True,'credit_amount':12.5}})
    assert ag[-1]['remaining']==12.5 and ag[-1]['kind']=='credits'
    missing=providers.REGISTRY['antigravity'].facts({'antigravity':{'known':False}})
    assert 'remaining' not in missing[-1]
    zh=providers.REGISTRY['zhipu'].facts({'zhipu':{'limits':[{'type':'TIME_LIMIT','remaining':40,'usage':999,'currentValue':44}]}})
    assert zh[-1]['kind']=='requests' and zh[-1]['remaining']==40
    assert 'used' not in zh[-1] and 'total' not in zh[-1]


def test_real_control_adapter_fetches_one_account_only_and_never_monitor(env,monkeypatch):
    from types import SimpleNamespace
    from src.management_control.oauth.account_mutations import OAuthAccountMutationControlMixin
    from src import oauth_manager as om
    cfg=env[0];target=copy.deepcopy(cfg['oauthAccounts'][0]);key='openai:same@example.test:w1'
    calls=[]
    async def fetch(account_id): calls.append(('fetch',account_id));return {'five_hour':{'utilization':3}}
    async def models(account_id):calls.append(('models',account_id));return {'action':'updated'}
    class Control(OAuthAccountMutationControlMixin):
        backend=SimpleNamespace(fetch_usage_snapshot=fetch,refresh_account_models=models)
        def _require(self,*a): pass
        def _account(self,k):
            assert k==key
            return cfg['oauthAccounts'][0]
        def _save_and_evaluate_usage(self,k,u):calls.append(('evaluate',k));return u,{'action':'noop'}
    c=Control()
    monkeypatch.setattr(om,'quota_monitor_once',lambda:pytest.fail('global monitor forbidden'))
    assert c.notification_operation(actions._context(11),key,target,'refresh_usage')['status']=='succeeded'
    assert c.notification_operation(actions._context(11),key,target,'sync_models')['status']=='succeeded'
    assert calls==[('fetch',key),('evaluate',key),('models',key)]


def test_no_capability_or_protection_never_synthesizes_write(env,controlled,monkeypatch):
    monkeypatch.setenv('PARROT_NO_REFRESH','1')
    sample=scene('account.auth.refresh_failed').record();sample['incident_id']=''
    assert 'refresh_token' not in dict(actions.available(sample))
    account=env[0]['oauthAccounts'][0]
    account['provider']='never_registered'
    unknown=replace(scene('account.quota.blocked'),subject=subjects.account(account)).record();unknown['incident_id']=''
    assert not set(dict(actions.available(unknown))) & {'refresh_usage','sync_models','refresh_token','reauthorize','benefits'}


def test_initialization_result_not_silenced_and_continuation_does_not_persist_secret(env):
    from src.notifications.operations import present
    env[0]['notifications']['enabled']=False
    result=present(11,None,event().subject,operation='导入结果',stages=[{'name':'账户保存','status':'succeeded'}],
        result_text='请发送账户名称',continuation=[[{'text':'跳过命名','callback_data':'oa:zh:name_skip:nonce'}]])
    rows=list(store.items('deliveries').values())
    assert len(rows)==1 and rows[0]['transactional'] and rows[0]['state']=='sent'
    assert any('请发送账户名称' in p['text'] for m,p in env[3] if m=='sendMessage')
    assert 'nonce' not in json.dumps(store.items('events'))


def test_legacy_migration_cannot_erase_new_durable_notification_domains(env):
    emit(event())
    before={domain:store.items(domain) for domain in ('events','incidents','deliveries','policy')}
    env[1].install_migration({'performance_stats':{},'workbuddy_actions':{}})
    assert before=={domain:store.items(domain) for domain in before}


def test_system_notification_shortcut_opens_page_below_and_rejects_others(env, monkeypatch):
    from src.telegram import bot, ui
    dispatched, answers = [], []
    monkeypatch.setattr(bot, '_handle_callback', dispatched.append)
    monkeypatch.setattr(ui, 'answer_cb', lambda *args, **kwargs: answers.append(args))
    cb = {'id': 'cb', 'from': {'id': 11}, 'data': 'nt:open:translation', 'message': {'chat': {'id': 11}, 'message_id': 5}}
    notification_menu.handle_callback(cb)
    page = [i for i, (m, p) in enumerate(env[3]) if m == 'sendMessage']
    assert env[3][page[-1]][1]['text'] == '正在打开…'
    assert dispatched[0]['data'] == 'tl:show' and dispatched[0]['message']['message_id'] == page[-1] + 101
    notification_menu.handle_callback({**cb, 'data': 'nt:open:anything'})
    notification_menu.handle_callback({**cb, 'from': {'id': 99}, 'message': {'chat': {'id': 99}, 'message_id': 5}})
    assert len(dispatched) == 1 and len(answers) == 2


def test_status_alert_is_translated_with_event_page_and_mute_buttons(monkeypatch):
    from src import status_monitor as sm
    from src.telegram import ui
    monkeypatch.setattr(ui, 'register_code', lambda payload: 'abcd1234')
    inc = {'id': 'inc1', 'name': 'Elevated errors', 'impact': 'minor', 'status': 'investigating',
           'created_at': '2026-10-21T14:05:00Z', 'shortlink': 'https://stspg.io/x'}
    text = sm._format_new_incident('openai', inc)
    assert '影响：轻微' in text and '调查中' in text and '开始：2026-10-21 22:05' in text
    assert 'minor' not in text and 'investigating' not in text and 'stspg.io' not in text
    row = sm._incident_buttons('openai', inc)['inline_keyboard'][0]
    assert row[0] == {'text': '🔗 事件页', 'url': 'https://stspg.io/x'}
    assert row[1]['callback_data'] == 'stat:mute:abcd1234'
    assert sm._incident_buttons('openai', inc, mute=False)['inline_keyboard'] == [[row[0]]]
    resolved = sm._format_resolved('openai', {**inc, 'resolved_at': '2026-10-21T15:00:00Z'})
    assert '已恢复：Elevated errors' in resolved and '恢复：2026-10-21 23:00' in resolved


def test_translation_failure_alert_uses_switch_and_links_settings(monkeypatch):
    from src import notifier, translation
    sent = []
    monkeypatch.setattr(notifier, 'notify_event', lambda key, text, **kw: sent.append((key, text, kw)) or True)
    monkeypatch.setattr(translation, '_consecutive_failures', 0)
    translation._record_failure({'failureAlertThreshold': 1, 'model': 'tr-<main>', 'fallbackModel': 'tr-backup'})
    (key, text, kw), = sent
    assert key == 'translation_failed' and 'tr-&lt;main&gt;' in text
    assert kw['reply_markup']['inline_keyboard'][0][0]['callback_data'] == 'nt:open:translation'


def test_apikey_detail_opens_only_existing_key_for_admin(monkeypatch):
    from src.telegram import ui
    from src.telegram.menus import apikey_menu
    opened = []
    monkeypatch.setattr(ui, 'is_admin', lambda chat: chat == 11)
    monkeypatch.setattr(apikey_menu, '_get_entry', lambda name: {'name': name} if name == 'msdev' else None)
    monkeypatch.setattr(apikey_menu, '_edit_cached_detail', lambda *args, **kwargs: opened.append((args, kwargs)) or True)
    assert apikey_menu.open_detail(11, 7, 'msdev') is True
    assert opened == [((11, 7, 'msdev', 1), {'start_view': True})]
    assert apikey_menu.open_detail(11, 8, 'gone') is False and apikey_menu.open_detail(99, 9, 'msdev') is False
    assert len(opened) == 1
