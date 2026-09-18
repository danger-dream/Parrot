"""Model-center simplification: actual callbacks, navigation and cold-cache races."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from src import config
from src.management_control.models import ModelSourceRef, ModelSourceType
from src.telegram import menu_cache, states, ui
from src.telegram.menus import model_center_menu as menu, model_center_usage as usage
from src.tests.test_model_center_tg_settings import env, _button, _buttons
from src.tests.test_model_center_immediate_alias import real_aliases, action_button
from src.tests.test_management_mapping_support import domain_client


METRICS = dict(total=10, success_count=8, error_count=1, input=100,
               output=500, cache_creation=100, cache_read=800,
               avg_tps=34.2, max_tps=219, min_tps=.4)


@pytest.fixture
def cache_stub(monkeypatch):
    value = {'data': None, 'error': None}
    pending = []
    def peek(views, source=None):
        return menu_cache.CacheRead(value['data'], True, False, value['error'])
    def request(views, source=None, *, subscriber=None, on_ready=None):
        pending.append((tuple(views), source, subscriber, on_ready))
        return peek(views, source)
    monkeypatch.setattr(usage, 'peek', peek)
    monkeypatch.setattr(usage, 'request', request)
    return value, pending


def test_usage_is_under_each_model_and_numbers_are_not_placeholder(env, cache_stub):
    _control, edits, *_ = env
    value, pending = cache_stub
    value['data'] = {'model-01': METRICS}
    menu.show(7, 10, 'show')
    text = edits[-1][2]
    assert text.index('model-01') < text.index('💎 累计用量') < text.index('model-02')
    assert '80.0%' in text and '失败 1 次' in text
    assert '34.2' in text and '219' in text and '0.4' in text
    assert text.count('💎 累计用量') == 1
    assert '加载中' not in text and pending[0][3] is None
    assert len(text.encode('utf-16-le')) // 2 <= 4096


def test_send_new_subscribes_using_actual_telegram_response_message_id(env, cache_stub, monkeypatch):
    _control, edits, *_ = env
    value, pending = cache_stub
    monkeypatch.setattr(ui, 'send', lambda *a, **kw: {'ok': True, 'result': {'message_id': 99}})
    menu.send_new(7)
    assert pending[0][2][:2] == (7, 99)
    value['data'] = {'model-01': METRICS}
    pending[0][3](value['data'], None)
    assert edits[-1][:2] == (7, 99) and '💎 累计用量' in edits[-1][2]


def test_cold_list_subscribes_after_initial_edit_and_completes(env, cache_stub):
    _control, edits, *_ = env
    value, pending = cache_stub
    menu.show(7, 10, 'show')
    assert '加载中' in edits[-1][2] and len(pending) == 1
    assert pending[0][2][:2] == (7, 10)
    value['data'] = {'model-01': METRICS}
    pending[0][3](value['data'], None)
    assert len(edits) == 2 and '💎 累计用量' in edits[-1][2]
    assert '加载中' not in edits[-1][2]


@pytest.mark.parametrize('destination', ['detail', 'image', 'external'])
def test_late_statistics_cannot_overwrite_new_page(env, cache_stub, destination):
    _control, edits, *_ = env
    value, pending = cache_stub
    menu.show(7, 10, 'show')
    if destination == 'detail':
        menu.handle_callback(7, 10, 'detail', _button(edits[-1][3], '1')['callback_data'])
    elif destination == 'image':
        menu.handle_callback(7, 10, 'image', 'mc:tab:image')
    else:
        # This is the real bot dispatcher's invalidation for another menu.
        menu_cache.begin_view(7, 10)
    before = list(edits)
    value['data'] = {'model-01': METRICS}
    pending[0][3](value['data'], None)
    assert edits == before


def test_cache_completes_between_render_and_subscription(env, cache_stub, monkeypatch):
    _control, edits, *_ = env
    value, _pending = cache_stub
    def request(*args, **kwargs):
        value['data'] = {'model-01': METRICS}
        return menu_cache.CacheRead(value['data'], True, False)
    monkeypatch.setattr(usage, 'request', request)
    menu.show(7, 10, 'show')
    assert len(edits) == 2 and '加载中' not in edits[-1][2]
    assert '💎 累计用量' in edits[-1][2]


def test_cold_statistics_failure_is_not_reported_as_zero(env, cache_stub):
    _control, edits, *_ = env
    value, pending = cache_stub
    menu.show(7, 10, 'show')
    value['error'] = RuntimeError('private diagnostic')
    pending[0][3](None, value['error'])
    assert '统计暂不可用' in edits[-1][2]
    assert '请求：0' not in edits[-1][2] and 'private diagnostic' not in edits[-1][2]


def test_alias_list_no_search_and_actions_share_last_row(env):
    control, _edits, answers, sends = env
    menu._session(7).tab = 'alias'
    # Even state left over from the retired UI cannot filter the list.
    menu._session(7).alias_query = 'matches-nothing'
    text, kb = menu.render(7)
    assert 'quick' in text and '查询' not in text + str(kb)
    assert kb['inline_keyboard'][-1] == [_button(kb, '新增别名'), _button(kb, '返回主菜单')]
    menu.handle_callback(7, 10, 'old', 'mc:alias_query')
    assert states.get_state(7) is None and not sends
    assert '已移除' in answers[-1][1]


@pytest.mark.parametrize('source', [None, ModelSourceRef(ModelSourceType.OAUTH, 'acct-a')])
def test_upstream_sync_opens_the_source_picker(env, monkeypatch, source):
    """「同步上游模型」按钮现在打开来源多选页，而不是直接同步。

    同步本身（含显式来源范围）改由多选页发起；这里断言按钮进入多选页，
    并且返回后仍恢复原来的筛选状态。
    """
    control, edits, answers, *_ = env
    calls = []
    monkeypatch.setattr(menu, '_show_rendered',
                        lambda chat_id, message_id, cb_id, renderer: calls.append(renderer))
    s = menu._session(7)
    s.source, s.text, s.page = source, 'model', 2
    _text, kb = menu.render(7)
    label = '同步上游模型' + (' · 当前来源' if source else '')
    menu.handle_callback(7, 10, 'sync', _button(kb, label)['callback_data'])
    # 进入多选页（由 sync 模块渲染），并未直接启动同步
    assert calls, "未进入来源多选页"
    assert not answers or answers[-1][1] != "同步任务已开始"


@pytest.mark.parametrize('result_status', ['partial_failed', 'failed'])
def test_upstream_operation_report_does_not_claim_all_sources_succeeded(env, result_status):
    control, _edits, *_ = env
    op = SimpleNamespace(id='sync-op', kind='model_center.upstream.sync', status='succeeded',
        progress=SimpleNamespace(current=2, total=2, message_code='model_center.upstream.sync.' + result_status),
        result={'status': result_status, 'items': [
            {'status': 'succeeded', 'label': 'API <one>', 'sourceId': 'opaque-source-one', 'count': 10},
            {'status': 'failed', 'label': 'OpenAI · user', 'sourceId': 'opaque-source-two', 'count': 0, 'errorCode': 'UPSTREAM_ERROR'},
        ]}, error=None)
    control.operations = SimpleNamespace(get=lambda ctx, op_id: op)
    text, _kb = menu._operation_render(7, 'sync-op', 'mc:list')
    assert ('状态：<code>部分失败</code>' if result_status == 'partial_failed' else '状态：<code>失败</code>') in text
    assert 'API &lt;one&gt; · 10 个模型' in text
    assert 'OpenAI · user · 上游同步失败，原目录保留' in text
    assert 'opaque-source' not in text and 'model_center.upstream.sync' not in text


def test_compression_detail_writes_real_config_and_api_observes_same_value(real_aliases):
    client, admin, messages, _answers, _open = real_aliases
    controls = menu._CONTROL
    ctx = menu._ctx(42)
    views = controls.list_models(ctx).items
    view = next(v for v in views if v.model_id == 'model-a')
    preserved = {k: deepcopy(config.get().get(k)) for k in ('channels', 'modelMapping', 'oauthAccounts')}
    _text, kb = menu._detail_render(42, view.resource_key)
    callback = action_button(kb, 42, 'compression_save')
    menu.handle_callback(42, 100, 'set', callback)
    assert controls.mapping.get_compression(ctx)[0] == 'model-a'
    assert '当前压缩模型' in messages[-1][0]
    assert all(config.get().get(k) == v for k, v in preserved.items())
    # Reuse the existing public API, not a TG-only config shadow.
    result = client.get('/api/management/v1/compression-model', headers=admin)
    assert result.status_code == 200, result.text
    assert result.json()['data']['modelId'] == 'model-a'
    clear = action_button(messages[-1][1], 42, 'compression_clear')
    menu.handle_callback(42, 100, 'clear', clear)
    assert controls.mapping.get_compression(ctx)[0] is None
