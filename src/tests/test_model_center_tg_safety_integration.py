"""Real Bot → lifecycle controls → isolated persistence → Management API negatives."""
import pytest

from src.tests.test_management_mapping_support import domain_client
from src.tests.test_model_center_tg_integration import _catalog, _button
from src import config, state_db
from src.management_control.models import ModelFilters, ModelKind
from src.telegram import bot, states, ui
from src.telegram.menus import model_center_menu as menu


@pytest.fixture
def real_tg(domain_client, monkeypatch, request):
    client, runtime, admin, readonly, denied = domain_client
    state_db.init()
    request.addfinalizer(state_db.close)
    _catalog()
    owner = runtime.control_owner()
    monkeypatch.setattr(menu, '_CONTROL', owner.models)
    monkeypatch.setattr(ui, 'is_admin', lambda chat_id: chat_id == 42)
    calls = []
    def capture(method, data=None):
        calls.append((method, data))
        return {'ok': True, 'result': {'message_id': 123}}
    monkeypatch.setattr(ui, 'api', capture)
    menu.reset_for_tests()
    states.clear_all()
    view = owner.models.list_models(
        menu._ctx(42), filters=ModelFilters(kinds=(ModelKind.CHAT,)), page=1, page_size=8,
    ).items[0]
    try:
        yield client, runtime, admin, view, calls
    finally:
        menu.reset_for_tests()
        states.clear_all()


def _bot_callback(data, cb='test'):
    bot._handle_callback({
        'id': cb, 'from': {'id': 42},
        'message': {'chat': {'id': 42}, 'message_id': 10}, 'data': data,
    })


def _metadata(client, admin):
    response = client.get('/api/management/v1/model-metadata/model-one', headers=admin)
    assert response.status_code == 200, response.text
    return response.json()['data']


def test_main_navigation_must_not_leave_metadata_write_armed(real_tg):
    client, runtime, admin, view, calls = real_tg
    before = _metadata(client, admin)['commonOverride']
    _, kb = menu._metadata_editor_render(42, view.resource_key, None, 'capacity')
    _bot_callback(_button(kb, '上下文')['callback_data'], 'field')
    assert states.get_state(42)['action'] == 'mc_metadata_field'
    _bot_callback('menu:main', 'main')
    assert any(method == 'editMessageText' and 'Parrot' in data['text'] for method, data in calls)
    assert states.get_state(42) is None and menu._session(42).generation == ''
    bot._handle_message({'chat': {'id': 42}, 'text': '300k'})
    assert _metadata(client, admin)['commonOverride'] == before


@pytest.mark.parametrize('command', ['/models', '/mapping'])
def test_current_model_commands_dispatch_real_lifecycle_view_without_write(real_tg, command):
    client, runtime, admin, view, calls = real_tg
    before = _metadata(client, admin)
    bot._handle_message({'chat': {'id': 42}, 'text': command})
    text = next(data['text'] for method, data in reversed(calls) if method == 'sendMessage')
    assert '模型中心' in text and 'model-one' in text
    assert _metadata(client, admin) == before


def test_sync_render_freezes_real_mapping_domain_and_api_change_conflicts(real_tg, monkeypatch):
    client, runtime, admin, view, calls = real_tg
    _, kb = menu._detail_render(42, view.resource_key)
    callback = _button(kb, '同步元数据')['callback_data']
    frozen = menu._thaw(42, callback.split(':')[2])
    before = _metadata(client, admin)
    assert frozen.data['revision'] == before['revision']
    assert frozen.data['revision'] != view.revision
    response = client.patch(
        '/api/management/v1/model-metadata/model-one/overrides',
        headers={**admin, 'If-Match': before['revision']},
        json={'scope': 'global', 'set': {'cost': {'input': 0}}, 'unset': []},
    )
    assert response.status_code == 200, response.text
    after = _metadata(client, admin)
    assert after['revision'] != before['revision']
    def forbidden_create(*args, **kwargs):
        raise AssertionError('stale TG revision must fail before creating any sync operation')
    monkeypatch.setattr(runtime.operations, 'create', forbidden_create)
    _bot_callback(callback, 'old-sync')
    assert any(method == 'answerCallbackQuery' and data.get('show_alert') for method, data in calls)
    assert not any('同步任务已开始' in data.get('text', '') for method, data in calls)
    assert _metadata(client, admin) == after


def test_single_unset_via_bot_preserves_real_explicit_sibling_overrides(real_tg):
    client, runtime, admin, view, calls = real_tg
    before = _metadata(client, admin)
    fields = {'contextWindow': 300000, 'cost.input': 0, 'vision': False, 'serviceTiers': []}
    response = client.patch(
        '/api/management/v1/model-metadata/model-one/overrides',
        headers={**admin, 'If-Match': before['revision']},
        json={'scope': 'global', 'set': {
            'contextWindow': fields['contextWindow'], 'cost': {'input': 0},
            'vision': False, 'serviceTiers': [],
        }, 'unset': []},
    )
    assert response.status_code == 200, response.text
    _, kb = menu._metadata_editor_render(42, view.resource_key, None, 'capacity')
    _bot_callback(_button(kb, '上下文 ✎')['callback_data'], 'field')
    prompt = next(data for method, data in reversed(calls) if method == 'sendMessage')
    _bot_callback(_button(prompt['reply_markup'], '恢复继承')['callback_data'], 'unset')
    expected = dict(fields)
    expected.pop('contextWindow')
    assert _metadata(client, admin)['commonOverride'] == expected
    assert states.get_state(42) is None


def test_saved_runtime_unconfirmed_feedback_is_not_success_or_retry(real_tg, monkeypatch):
    client, runtime, admin, view, calls = real_tg
    _, kb = menu._detail_render(42, view.resource_key)
    callback = _button(kb, '停用模型')['callback_data']
    def failed_reload():
        raise RuntimeError('isolated reload failure')
    monkeypatch.setattr(config, '_reload_callbacks', [failed_reload])
    _bot_callback(callback, 'save-with-reload-failure')
    alerts = [data for method, data in calls if method == 'answerCallbackQuery']
    assert alerts[-1]['show_alert'] is True
    assert alerts[-1]['text'] == '配置已保存，运行时重载未确认；请刷新状态，勿重放旧操作。'
    assert not any(data.get('text') == '已停用' for data in alerts)
    response = client.get(f'/api/management/v1/models/{view.resource_key}', headers=admin)
    assert response.status_code == 200, response.text
    assert response.json()['data']['globalEnabled'] is False
    # Old callback is still the original fixed target/revision, not a new toggle.
    _bot_callback(callback, 'duplicate')
    assert '版本已变化' in calls[-2][1]['text']
    assert client.get(f'/api/management/v1/models/{view.resource_key}', headers=admin).json()['data']['globalEnabled'] is False
