"""Regression coverage for TG-01/02/04..08 and I-01, converted from audit probes.

Only temporary persistence and captured Telegram transport; no upstream requests.
TG-03 queued cancellation is owned/tested by the model backend package.
"""
from __future__ import annotations

import math
import os
import threading
import time
from html.parser import HTMLParser
from types import SimpleNamespace

import pytest

from src import config, state_db
from src.channel import registry
from src.management_control.mapping import MetadataOverridePatch
from src.management_control.models import ModelCenterControl, ModelSourceRef, ModelSourceType
from src.telegram import menu_cache, states, ui
from src.telegram.menus import model_center_menu as menu, model_center_sync as sync
from src.telegram.menus import model_center_media as media, model_center_usage as usage
from src.tests.test_management_mapping_support import domain_client
from src.tests.test_media_panel_independence import media_env, CTX
from src.tests.test_model_center_tg_core import env, _button, _buttons


@pytest.fixture(autouse=True)
def local_only(monkeypatch):
    assert os.environ['PARROT_TEST_NETWORK_GUARD'] == 'loopback-only'
    monkeypatch.setattr(ui, 'api', lambda *a, **kw: {'ok': True, 'result': {'message_id': 900}})
    monkeypatch.setattr(usage, 'peek', lambda *a, **kw: menu_cache.CacheRead({}, True, False))
    monkeypatch.setattr(usage, 'request', lambda *a, **kw: menu_cache.CacheRead({}, True, False))
    tables = (sync._EVENTS, sync._CHAT_BY_OPERATION, sync._MESSAGE_BY_OPERATION, sync._BACK_BY_OPERATION)
    for table in tables:
        table.clear()
    monkeypatch.setattr(sync, '_schedule_cleanup', lambda *a: None)
    yield
    for table in tables:
        table.clear()


@pytest.fixture
def real(domain_client, monkeypatch):
    client, runtime, admin, *_ = domain_client

    def seed(cfg):
        cfg['oauth']['mockMode'] = True
        cfg['stateDbPath'] = os.environ['PARROT_TEST_STATE_PATH']
        cfg['logDir'] = os.environ['PARROT_TEST_LOG_DIR']
        cfg['images']['dbPath'] = os.environ['PARROT_TEST_IMAGE_PATH']
        cfg['oauthAccounts'] = [{'provider': 'xai', 'type': 'xai', 'email': 'audit@example.test',
                                 'subject': 'audit', 'enabled': True, 'models': ['model-a']}]
        cfg['modelMapping'] = {'global': {}}
        cfg['channels'] = [{'name': 'audit', 'type': 'api', 'enabled': True,
                            'protocol': 'anthropic', 'providerId': 'anthropic',
                            'baseUrl': 'https://audit.invalid', 'apiKey': 'test-only',
                            'models': [{'real': 'wire-a', 'alias': 'model-a'}]}]

    config.update(seed)
    state_db.init()
    registry.rebuild_from_config()
    owner = runtime.control_owner()
    messages, answers = [], []
    monkeypatch.setattr(menu, '_CONTROL', owner.models)
    monkeypatch.setattr(ui, 'is_admin', lambda chat: chat == 42)
    monkeypatch.setattr(ui, 'edit', lambda chat, mid, text, reply_markup=None, **kw: messages.append((text, reply_markup)))
    monkeypatch.setattr(ui, 'send', lambda chat, text, reply_markup=None, **kw: messages.append((text, reply_markup)))
    monkeypatch.setattr(ui, 'send_result', lambda chat, text, **kw: messages.append((text, kw)))
    monkeypatch.setattr(ui, 'answer_cb', lambda cb, text=None, **kw: answers.append(text))
    menu.reset_for_tests()
    states.clear_all()
    view = next(item for item in owner.models.list_models(menu._ctx(42)).items if item.model_id == 'model-a')
    try:
        yield owner, view, messages, answers, client, admin
    finally:
        menu.reset_for_tests()
        states.clear_all()
        state_db.close()


def frozen_buttons(kb, chat, name):
    return [(b, action) for b in _buttons(kb)
            if b['callback_data'].startswith('mc:a:')
            and (action := menu._thaw(chat, b['callback_data'].split(':')[2]))
            and action.name == name]


def test_sync_selection_and_start_keep_rendered_source_identity(env, monkeypatch):
    _control, edits, _answers, _sends = env
    a = ModelSourceRef(ModelSourceType.API, 'api:A')
    b = ModelSourceRef(ModelSourceType.API, 'api:B')
    rows = [('A', a), ('B', b)]
    monkeypatch.setattr(sync, '_available_sources', lambda chat: list(rows))
    captured = []
    monkeypatch.setattr(sync, '_start', lambda chat, mid, cb, refs, back: captured.append(refs) or True)
    sync.open_picker(7, 10, 'open', 'mc:list')
    choose_a = _button(edits[-1][3], '1')['callback_data']
    rows.reverse()  # The old number itself must not acquire B's identity.
    menu.handle_callback(7, 10, 'pick-A', choose_a)
    assert sync._selection(7)['selected'] == [a]
    start = next(b['callback_data'] for b in _buttons(edits[-1][3]) if '同步所选' in b['text'])
    menu.handle_callback(7, 10, 'also-B', _button(edits[-1][3], '1')['callback_data'])
    assert set(sync._selection(7)['selected']) == {a, b}
    menu.handle_callback(7, 10, 'old-start', start)
    assert captured == [(a,)]  # Not the new selection and not the old list index.


@pytest.mark.parametrize('change', ['removed', 'new-picker'])
def test_sync_expired_source_or_picker_cannot_retarget(env, monkeypatch, change):
    _control, edits, answers, _sends = env
    a = ModelSourceRef(ModelSourceType.API, 'api:A')
    b = ModelSourceRef(ModelSourceType.API, 'api:B')
    rows = [('A', a), ('B', b)]
    monkeypatch.setattr(sync, '_available_sources', lambda chat: list(rows))
    calls = []
    monkeypatch.setattr(sync, '_start', lambda *a: calls.append(a) or True)
    sync.open_picker(7, 10, 'open', 'mc:list')
    old_toggle = _button(edits[-1][3], '1')['callback_data']
    menu.handle_callback(7, 10, 'pick', old_toggle)
    start = next(b['callback_data'] for b in _buttons(edits[-1][3]) if '同步所选' in b['text'])
    if change == 'removed':
        rows[:] = [('B', b)]
    else:
        sync.open_picker(7, 11, 'new', 'mc:list')
    menu.handle_callback(7, 10, 'old-start', start)
    menu.handle_callback(7, 10, 'old-toggle', old_toggle)
    assert not calls and answers[-1][2]
    assert b not in sync._selection(7)['selected']


def test_sync_all_freezes_source_set_before_new_sources_arrive(env, monkeypatch):
    _control, edits, _answers, _sends = env
    a = ModelSourceRef(ModelSourceType.API, 'api:A')
    b = ModelSourceRef(ModelSourceType.API, 'api:B')
    rows = [('A', a)]
    monkeypatch.setattr(sync, '_available_sources', lambda chat: list(rows))
    captured = []
    monkeypatch.setattr(sync, '_start', lambda chat, mid, cb, refs, back: captured.append(refs) or True)
    sync.open_picker(7, 10, 'open', 'mc:list')
    all_button = next(b['callback_data'] for b in _buttons(edits[-1][3]) if '同步全部' in b['text'])
    rows.append(('B', b))
    menu.handle_callback(7, 10, 'old-all', all_button)
    assert captured == [(a,)]


def test_filter_selection_survives_query_change_without_false_checkmarks(env):
    control, edits, _answers, sends = env
    menu.handle_callback(7, 10, 'query', 'mc:query')
    menu.handle_text_state(7, 'mc_query', 'model-01')
    menu.handle_callback(7, 10, 'all', 'mc:select_all')
    menu.handle_callback(7, 10, 'query2', 'mc:query')
    menu.handle_text_state(7, 'mc_query', 'model-02')
    text, kb = sends[-1][1:]
    assert '1. ☑ <code>model-02</code>' not in text
    assert '已选 <b>1</b>' in text
    assert menu._session(7).selected == ['model-01']
    # New-query picks are additive, preserving already-supported cross-query selection.
    menu.handle_callback(7, 10, 'pick-02', _button(kb, '1')['callback_data'])
    assert '1. ☑ <code>model-02</code>' in edits[-1][2]
    toggle = _button(edits[-1][3], '状态：启用')['callback_data']
    menu.handle_callback(7, 10, 'disable', toggle)
    assert set(control.calls[-1][1].model_ids) == {'model-01', 'model-02'}
    assert not control.views['model-01'].global_enabled
    assert not control.views['model-02'].global_enabled
    assert control.views['model-05'].global_enabled


def test_status_change_keeps_only_actual_members_checked_and_invert_count(env):
    control, edits, _answers, _sends = env
    menu.handle_callback(7, 10, 'all', 'mc:select_all')
    # Remove model-03 from the old selection before changing to disabled status.
    menu.handle_callback(7, 10, 'exclude', _button(edits[-1][3], '3')['callback_data'])
    menu.handle_callback(7, 10, 'status', 'mc:status')
    menu.handle_callback(7, 10, 'disabled', _button(edits[-1][3], '停用')['callback_data'])
    assert '☑ <code>model-03</code>' not in edits[-1][2]
    assert menu._selected_count(7) == 11
    # Invert current results must not subtract exclusions outside these results.
    menu.handle_callback(7, 10, 'invert', 'mc:invert')
    assert menu._selected_count(7) == 1
    assert '☑ <code>model-03</code>' in edits[-1][2]
    chosen = menu._selected_views(7, menu._ctx(7))
    assert [view.model_id for view in chosen] == ['model-03']


@pytest.mark.parametrize('scope_kind', ['global', 'api', 'oauth'])
@pytest.mark.parametrize('value', [0, 1.25])
@pytest.mark.parametrize('field', [f for f in menu._META_FIELDS if f.kind == 'price'], ids=lambda f: f.key)
def test_prices_save_and_single_inherit_through_real_controls(real, field, value, scope_kind):
    owner, view, messages, answers, client, admin = real
    source_view = next((s for s in view.sources if s.type.value == scope_kind), None)
    source = ModelSourceRef(source_view.type, source_view.id) if source_view else None
    ctx = menu._ctx(42)
    kwargs = menu._metadata_scope_kwargs(source, source_view.outbound_model if source_view else None)
    before = owner.mapping.get_metadata(ctx, view.model_id, scope_id=source.id if source else None)
    owner.mapping.patch_metadata_overrides(ctx, view.model_id,
        patch=MetadataOverridePatch(set_fields={'vision': False}), expected_revision=before.revision, **kwargs)
    _, kb = menu._metadata_editor_render(42, view.resource_key, source, 'price')
    menu.handle_callback(42, 100, 'edit', _button(kb, field.label)['callback_data'])
    menu.handle_text_state(42, 'mc_metadata_field', str(value))
    assert states.get_state(42) is None
    record = owner.mapping.get_metadata(ctx, view.model_id, scope_id=source.id if source else None)
    overrides = record.source_override if source else record.common_override
    assert overrides == {'vision': False, field.key: value}
    assert menu._display_value(record.effective, field.key) == value
    # Read the same real persisted record through Management API as well.
    response = client.get('/api/management/v1/model-metadata/model-a', headers=admin,
                          params={'scopeId': source.id} if source else {})
    assert response.status_code == 200, response.text
    assert response.json()['data']['sourceOverride' if source else 'commonOverride'] == overrides
    text, kb = menu._metadata_editor_render(42, view.resource_key, source, 'price')
    assert f'{field.label}：<code>${float(value)}</code>（手工' in text
    menu.handle_callback(42, 100, 'field', _button(kb, field.label + ' ✎')['callback_data'])
    menu.handle_callback(42, 100, 'inherit', _button(messages[-1][1], '恢复继承')['callback_data'])
    record = owner.mapping.get_metadata(ctx, view.model_id, scope_id=source.id if source else None)
    assert (record.source_override if source else record.common_override) == {'vision': False}
    assert '已恢复继承' in answers[-1]


@pytest.mark.parametrize('preceding', [10, 205])
def test_exact_alias_opens_after_many_fuzzy_matches(real, preceding):
    owner, view, messages, answers, client, admin = real
    aliases = {f'a{i:03d}-quick': 'model-a' for i in range(preceding)}
    aliases['quick'] = 'model-a'
    config.update(lambda cfg: cfg.__setitem__('modelMapping', {'global': aliases}))
    menu._session(42).tab = 'alias'
    menu._session(42).alias_page = math.ceil(len(aliases) / menu._PAGE_SIZE)
    text, kb = menu.render(42)
    callback = _button(kb, str(preceding + 1))['callback_data']
    menu.handle_callback(42, 100, 'open-quick', callback)
    assert menu._alias_drafts[42].alias == 'quick'
    assert '编辑别名' in messages[-1][0]
    # A genuinely removed target still fails closed.
    config.update(lambda cfg: cfg['modelMapping']['global'].pop('quick'))
    menu.handle_callback(42, 100, 'removed', callback)
    assert answers[-1] == '别名已不存在'


class _BalancedHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []

    def handle_starttag(self, tag, attrs):
        self.stack.append(tag)

    def handle_endtag(self, tag):
        assert self.stack.pop() == tag


def assert_wire(text, kb):
    parser = _BalancedHTML()
    parser.feed(text)
    parser.close()
    assert not parser.stack
    assert len(text.encode('utf-16-le')) // 2 <= 4096
    assert len(_buttons(kb)) < 100
    assert all(len(b['callback_data'].encode()) <= 64 for b in _buttons(kb))


@pytest.mark.parametrize('label', ['A' * 60, '<&🦜' * 20])
def test_sync_source_pagination_preserves_all_cross_page_actions(env, monkeypatch, label):
    _control, edits, _answers, _sends = env
    refs = [ModelSourceRef(ModelSourceType.API, f'api:{i}') for i in range(65)]
    monkeypatch.setattr(sync, '_available_sources', lambda chat: [(label, ref) for ref in refs])
    sync.open_picker(7, 10, 'open', 'mc:list')
    seen = []
    while True:
        text, kb = edits[-1][2:]
        assert_wire(text, kb)
        picks = frozen_buttons(kb, 7, 'sync_pick_toggle')
        seen.extend(action.data['source'] for _, action in picks)
        menu.handle_callback(7, 10, 'pick-first-on-page', picks[0][0]['callback_data'])
        text, kb = edits[-1][2:]
        # Validate all HTML continuations as well as the bounded numbered keyboard.
        while any(b['text'] == '内容 ▶' and b['callback_data'] != 'mc:noop' for b in _buttons(kb)):
            menu.handle_callback(7, 10, 'content', _button(kb, '内容 ▶')['callback_data'])
            text, kb = edits[-1][2:]
            assert_wire(text, kb)
        next_page = _button(kb, '下一页 ▶')['callback_data']
        if next_page == 'mc:noop':
            break
        menu.handle_callback(7, 10, 'next', next_page)
    assert seen == refs
    assert sync._selection(7)['selected'] == [refs[i] for i in (0, 20, 40, 60)]
    menu.handle_callback(7, 10, 'all', _button(kb, '✅ 全选')['callback_data'])
    assert sync._selection(7)['selected'] == refs
    menu.handle_callback(7, 10, 'invert', _button(edits[-1][3], '🔄 反选')['callback_data'])
    assert sync._selection(7)['selected'] == []


def test_all_oauth_source_pages_are_accessible_and_syncable(real):
    owner, view, messages, answers, client, admin = real
    accounts = [{'provider': 'xai', 'type': 'xai', 'email': f'u{i:03d}@example.test',
                 'subject': f's{i:03d}', 'enabled': True, 'models': ['model-a']} for i in range(201)]
    config.update(lambda cfg: cfg.__setitem__('oauthAccounts', accounts))
    registry.rebuild_from_config()
    options = menu._source_options(42)
    assert len(options) == 202  # 201 OAuth accounts and the API channel.
    last = options[200]
    assert menu._find_source_option(42, last.ref) == last
    menu.open_source(42, 100, 'last', source_type='oauth', source_id=last.ref.id, origin='menu:oauth')
    assert menu._session(42).source == last.ref
    menu.handle_callback(42, 100, 'picker', 'mc:source')
    seen = []
    while True:
        text, kb = messages[-1]
        assert_wire(text, kb)
        seen.extend(a.data['source'] for _, a in frozen_buttons(kb, 42, 'set_source') if a.data['source'])
        next_page = _button(kb, '下一页 ▶')['callback_data']
        if next_page == 'mc:noop':
            break
        menu.handle_callback(42, 100, 'next', next_page)
    assert seen == [option.ref for option in options]
    _, kb = sync._picker_render(42, 'mc:list')
    all_action = next(a for b, a in frozen_buttons(kb, 42, 'sync_pick_start') if '同步全部' in b['text'])
    assert all_action.data['sources'] == tuple(seen)


def test_cancel_then_navigate_ignores_late_sync_event(real, monkeypatch):
    owner, view, messages, answers, client, admin = real
    entered = threading.Event()
    release = threading.Event()
    worker = owner.models._upstream_sync

    async def fake_api(ctx, source):
        entered.set()
        assert release.wait(5)
        return {'count': 1}

    monkeypatch.setattr(worker, '_api', fake_api)
    try:
        sync._start(42, 100, 'start', (ModelSourceRef(ModelSourceType.API, 'api:audit'),), 'mc:list')
        assert entered.wait(5)
        op_id = next(iter(sync._EVENTS))
        cancel = menu._freeze(42, 'sync_cancel', operation_id=op_id, back_callback='mc:list')
        menu.handle_callback(42, 100, 'cancel', cancel)
        assert _button(messages[-1][1], '◀ 返回模型中心')
        menu.handle_callback(42, 100, 'back', 'mc:list')
        assert '模型中心 · 对话' in messages[-1][0]
        before = list(messages)
        release.set()
        deadline = time.monotonic() + 5
        while worker._active and time.monotonic() < deadline:
            time.sleep(.005)
        assert not worker._active
        assert messages == before
    finally:
        release.set()


def test_sink_before_start_returns_keeps_its_view_token(env, monkeypatch):
    control, edits, _answers, _sends = env
    control.operations = SimpleNamespace(get=lambda *a: SimpleNamespace(status='running'))

    def start(ctx, source, *, sources, progress_sink):
        progress_sink('op', {'phase': 'start', 'total': 1, 'index': 0, 'label': 'A'})
        assert 'A' in edits[-1][2]
        menu._show_rendered(7, 10, None, lambda: ('new page', ui.inline_kb([])))
        return SimpleNamespace(id='op')

    monkeypatch.setattr(control, 'start_upstream_sync', start, raising=False)
    sync._start(7, 10, 'start', (ModelSourceRef(ModelSourceType.API, 'api:A'),), 'mc:list')
    assert edits[-1][2] == 'new page'  # Final initial paint cannot resurrect the old page.


@pytest.mark.parametrize('kind', ['image', 'video'])
@pytest.mark.parametrize('clear', [False, True])
def test_media_default_buttons_use_rendered_revision(media_env, monkeypatch, kind, clear):
    store, images, videos, _, _ = media_env
    names = ['gpt-image-2', 'gpt-image-2.5'] if kind == 'image' else ['grok-imagine-video', 'audit-video-new']
    if kind == 'video':
        store.value['video_models']['xai'] = names
    control = images if kind == 'image' else videos
    monkeypatch.setattr(menu, '_CONTROL', ModelCenterControl(images=images, videos=videos))
    monkeypatch.setattr(ui, 'is_admin', lambda chat: chat == 91)
    # Source eligibility is tested by the media package; this test isolates the CAS boundary.
    monkeypatch.setattr(media, '_available_model_names', lambda *a: names)
    events = []
    monkeypatch.setattr(ui, 'api', lambda method, data=None, **kw: events.append((method, data)) or {'ok': True})
    menu.reset_for_tests()
    try:
        settings = control.get_settings(CTX)
        if clear:
            control.update_settings(CTX, {'defaultModel': names[0]}, expected_revision=settings.revision)
        _, kb = media._default_model_panel(91, kind)
        target = '' if clear else names[0]
        old = next(b['callback_data'] for b, a in frozen_buttons(kb, 91, 'media_default_set') if a.data['model'] == target)
        before = control.get_settings(CTX)
        control.update_settings(CTX, {'defaultModel': names[1]}, expected_revision=before.revision)
        menu.handle_callback(91, 7, 'old', old)
        assert control.get_settings(CTX).default_model == names[1]
        assert any('版本已变化' in str(e) for e in events)
        _, kb = media._default_model_panel(91, kind)
        fresh = next(b['callback_data'] for b, a in frozen_buttons(kb, 91, 'media_default_set') if a.data['model'] == target)
        menu.handle_callback(91, 7, 'fresh', fresh)
        assert control.get_settings(CTX).default_model == target
        legacy = menu._freeze(91, 'media_default_set', kind=kind, model=names[1])
        menu.handle_callback(91, 7, 'legacy-unversioned', legacy)
        assert control.get_settings(CTX).default_model == target
        assert any('页面已过期' in str(e) for e in events)
    finally:
        menu.reset_for_tests()

def test_query_selection_real_state_write_matches_rendered_checkmarks(real):
    owner, view, messages, answers, client, admin = real
    config.update(lambda cfg: cfg['channels'][0]['models'].extend([
        {'alias': 'model-b', 'real': 'wire-b'}, {'alias': 'model-c', 'real': 'wire-c'},
    ]))
    registry.rebuild_from_config()
    menu.handle_callback(42, 100, 'query', 'mc:query')
    menu.handle_text_state(42, 'mc_query', 'model-a')
    menu.handle_callback(42, 100, 'all', 'mc:select_all')
    menu.handle_callback(42, 100, 'query-b', 'mc:query')
    menu.handle_text_state(42, 'mc_query', 'model-b')
    text, kb = messages[-1]
    assert '☑ <code>model-b</code>' not in text
    menu.handle_callback(42, 100, 'select-b', _button(kb, '1')['callback_data'])
    assert '☑ <code>model-b</code>' in messages[-1][0]
    menu.handle_callback(42, 100, 'disable-both', _button(messages[-1][1], '状态：启用')['callback_data'])
    states_by_id = {v.model_id: v.global_enabled for v in owner.models.list_models(menu._ctx(42)).items}
    assert states_by_id['model-a'] is False and states_by_id['model-b'] is False
    assert states_by_id['model-c'] is True
    response = client.get('/api/management/v1/models', headers=admin)
    assert {v['modelId'] for v in response.json()['data'] if not v['globalEnabled']} == {'model-a', 'model-b'}


def test_sync_reorder_uses_real_control_target_and_finishes_current_view(real, monkeypatch):
    owner, view, messages, answers, client, admin = real
    config.update(lambda cfg: cfg['channels'].append({
        **cfg['channels'][0], 'name': 'other', 'models': [{'alias': 'model-other', 'real': 'wire-other'}],
    }))
    registry.rebuild_from_config()
    sync.open_picker(42, 100, 'open', 'mc:list')
    # OAuth comes first, then the selected API channel and another API channel.
    choose_a = _button(messages[-1][1], '2')['callback_data']
    config.update(lambda cfg: cfg['channels'].reverse())
    menu.handle_callback(42, 100, 'pick-original-a', choose_a)
    start = next(b['callback_data'] for b in _buttons(messages[-1][1]) if '同步所选' in b['text'])
    called = []

    async def fake_api(ctx, source):
        called.append(source.id)
        return {'count': 1}

    monkeypatch.setattr(owner.models._upstream_sync, '_api', fake_api)
    menu.handle_callback(42, 100, 'start', start)
    deadline = time.monotonic() + 5
    while owner.models._upstream_sync._active and time.monotonic() < deadline:
        time.sleep(.005)
    assert not owner.models._upstream_sync._active
    assert called == ['api:audit']
    assert '全部完成：1/1' in messages[-1][0]
    assert _button(messages[-1][1], '◀ 返回模型中心')


def test_large_filter_selection_keeps_batch_targets_after_query_change(env, monkeypatch):
    from dataclasses import replace
    from src.management_control.models import ModelIdentity, ModelKind, ModelSelectionMode

    control, edits, _answers, sends = env
    template = control.views['model-01']
    wanted = [f'A-{i:05d}' for i in range(10_001)]
    control.views = {name: replace(template, model_id=name, resource_key=name,
                                  identity=ModelIdentity(ModelKind.CHAT, name))
                     for name in [*wanted, 'B-other']}
    monkeypatch.setattr(control, 'get_model', lambda ctx, key: control.views[key])
    menu.handle_callback(7, 10, 'query-A', 'mc:query')
    menu.handle_text_state(7, 'mc_query', 'A-')
    menu.handle_callback(7, 10, 'all', 'mc:select_all')
    menu.handle_callback(7, 10, 'query-B', 'mc:query')
    menu.handle_text_state(7, 'mc_query', 'B-other')
    text, kb = sends[-1][1:]
    assert '☑ <code>B-other</code>' not in text
    assert '已选 <b>10001</b>' in text
    callback = _button(kb, '状态：启用')['callback_data']
    action = menu._thaw(7, callback.split(':')[2])
    # FILTER remains available above the backend's 10,000 explicit-ID limit.
    assert action.data['selection'].mode is ModelSelectionMode.FILTER
    selected = ModelCenterControl()._selection_models(list(control.views.values()), action.data['selection'])
    assert selected == wanted
    assert action.data['revision'] == control.revision
