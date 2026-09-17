"""Immediate edits use real MappingControl/config/API; pagination uses exact inventory."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import pytest

from src import config, state_db
from src.channel import registry
from src.telegram import states, ui
from src.telegram.menus import model_center_menu as menu
from src.telegram.menus.model_center_icons import decorate_keyboard
from src.tests.test_management_mapping_support import domain_client
from src.tests.test_model_center_tg_core import env, _buttons, _button


@pytest.fixture
def real_aliases(domain_client, monkeypatch):
    client, runtime, admin, *_ = domain_client
    state_db.init()
    config.update(lambda c: c.update({
        'channels': [{'name':'aliases', 'type':'api', 'protocol':'anthropic', 'providerId':'anthropic',
            'baseUrl':'https://example.invalid', 'apiKey':'synthetic', 'enabled':True,
            'models':[{'real':'model-a', 'alias':'model-a'}, {'real':'model-b', 'alias':'model-b'}]}],
        'oauthAccounts':[], 'modelMapping': {'global':{'quick':'model-a','other':'model-b'}},
    }))
    registry.rebuild_from_config()
    monkeypatch.setattr(menu, '_CONTROL', runtime.control_owner().models)
    monkeypatch.setattr(ui, 'is_admin', lambda chat: chat == 42)
    messages, answers = [], []
    monkeypatch.setattr(ui, 'edit', lambda chat, mid, text, reply_markup=None, **kw: messages.append((text,reply_markup)))
    monkeypatch.setattr(ui, 'send', lambda chat, text, reply_markup=None, **kw: messages.append((text,reply_markup)))
    monkeypatch.setattr(ui, 'answer_cb', lambda cb, text=None, **kw: answers.append(text))
    menu.reset_for_tests(); states.clear_all()
    def open_alias(name='quick'):
        s=menu._session(42); s.tab='alias'
        _, kb=menu.render(42)
        menu.handle_callback(42,100,'open',action_button(kb,42,'alias_open',alias=name))
        return messages[-1][1]
    yield client, admin, messages, answers, open_alias
    menu.reset_for_tests(); states.clear_all(); state_db.close()


def action_button(kb, chat, name, **values):
    for b in _buttons(kb):
        cb=b.get('callback_data','')
        action=menu._thaw(chat, cb.split(':',2)[-1]) if cb.startswith('mc:a:') else None
        if action and action.name==name and all(action.data.get(k)==v for k,v in values.items()):
            return b['callback_data']
    raise AssertionError(f'missing action: {name} {values}')


def mappings(client, admin):
    result=client.get('/api/management/v1/model-mappings',headers=admin)
    assert result.status_code==200
    return {r['alias']:r['realModel'] for r in result.json()['data']}, result.json()['meta']['revision']


def test_real_name_and_target_apply_without_save_and_preserve_other_domains(real_aliases):
    client, admin, messages, _, open_alias=real_aliases
    kb=open_alias()
    preserved={k:deepcopy(config.get().get(k)) for k in ('channels','apiKeys','loadBalancing','modelMetadata','compression')}
    assert not any('保存' in b['text'] for b in _buttons(kb))
    old_id=menu._alias_drafts[42].draft_id
    menu.handle_callback(42,100,'name',action_button(kb,42,'alias_name'))
    menu.handle_text_state(42,'mc_alias_name','fast')
    assert mappings(client,admin)[0]=={'fast':'model-a','other':'model-b'}
    assert menu._alias_drafts[42].draft_id!=old_id
    menu.handle_callback(42,100,'picker',action_button(messages[-1][1],42,'alias_target_picker'))
    select=action_button(messages[-1][1],42,'alias_target',model_id='model-b')
    menu.handle_callback(42,100,'select',select)
    assert mappings(client,admin)[0]=={'fast':'model-b','other':'model-b'}
    committed=deepcopy(config.get()['modelMapping'])
    menu.handle_callback(42,100,'repeat',select)
    assert config.get()['modelMapping']==committed
    assert all(config.get().get(k)==v for k,v in preserved.items())


@pytest.mark.parametrize('field', ['name','target'])
def test_real_external_revision_change_is_not_overwritten(real_aliases, field):
    client, admin, messages, answers, open_alias=real_aliases
    kb=open_alias()
    if field=='name':
        menu.handle_callback(42,100,'name',action_button(kb,42,'alias_name'))
    else:
        menu.handle_callback(42,100,'picker',action_button(kb,42,'alias_target_picker'))
        target=action_button(messages[-1][1],42,'alias_target',model_id='model-b')
    _, revision=mappings(client,admin)
    changed=client.patch('/api/management/v1/model-mappings/quick',headers={**admin,'If-Match':revision},json={'alias':'quick','realModel':'model-b'})
    assert changed.status_code==200
    if field=='name': menu.handle_text_state(42,'mc_alias_name','must-not-rename')
    else: menu.handle_callback(42,100,'stale',target)
    assert mappings(client,admin)[0]=={'quick':'model-b','other':'model-b'}
    assert menu._alias_drafts[42].alias=='quick'
    assert menu._alias_drafts[42].real_model=='model-a'  # failed operation never falsifies editor state
    assert any('版本已变化' in str(t) for t in [*answers, *(m[0] for m in messages)])


def test_real_new_alias_requires_name_and_target_then_creates_once(real_aliases):
    client, admin, messages, _, _=real_aliases
    s=menu._session(42); s.tab='alias'
    _, kb=menu.render(42)
    menu.handle_callback(42,100,'new',action_button(kb,42,'alias_new'))
    menu.handle_text_state(42,'mc_alias_name','new-alias')
    assert 'new-alias' not in mappings(client,admin)[0]
    target=action_button(messages[-1][1],42,'alias_target',model_id='model-b')
    menu.handle_callback(42,100,'select',target)
    assert mappings(client,admin)[0]['new-alias']=='model-b'
    assert not any('保存' in b['text'] for b in _buttons(messages[-1][1]))
    menu.handle_callback(42,100,'repeat',target)
    assert mappings(client,admin)[0]=={'new-alias':'model-b','quick':'model-a','other':'model-b'}


def test_real_new_alias_cannot_upsert_existing_mapping(real_aliases):
    client, admin, messages, _, _=real_aliases
    menu._session(42).tab='alias'
    _, kb=menu.render(42)
    menu.handle_callback(42,100,'new',action_button(kb,42,'alias_new'))
    menu.handle_text_state(42,'mc_alias_name','other')
    assert mappings(client,admin)[0]=={'quick':'model-a','other':'model-b'}
    assert states.get_state(42)['action']=='mc_alias_name'
    assert menu._alias_drafts[42].alias==''
    menu.handle_text_state(42,'mc_alias_name','/cancel')
    assert 42 not in menu._alias_drafts
    assert mappings(client,admin)[0]=={'quick':'model-a','other':'model-b'}


def test_target_selector_is_six_columns_four_rows_then_correct_last_page(env):
    control, edits, _, _=env
    base=control.views['model-01']
    control.views={f'model-{i:02d}':replace(base,model_id=f'model-{i:02d}',resource_key=f'rk-{i}') for i in range(1,51)}
    menu._session(7).tab='alias'
    _,kb=menu.render(7)
    menu.handle_callback(7,10,'open',_button(kb,'1')['callback_data'])
    draft=menu._alias_drafts[7]
    for page,expected in ((1,list(range(1,25))),(2,list(range(25,49))),(3,[49,50])):
        text,kb=menu._alias_target_render(7,draft.draft_id,page)
        rows=[row for row in kb['inline_keyboard'] if all((b['text'].removeprefix('✓ ')).isdigit() for b in row)]
        assert [len(r) for r in rows]==([6,6,6,6] if page<3 else [2])
        assert [int(b['text'].removeprefix('✓ ')) for row in rows for b in row]==expected
        assert f'第 {page}/3 页' in text
        assert all(len(b['callback_data'].encode())<=64 for b in _buttons(kb))


def test_model_center_buttons_have_explicit_semantic_icons_and_preserve_provider_icons(env):
    _,_,_,_=env
    _,kb=menu.render(7)
    labels=[b['text'] for b in _buttons(kb)]
    for label in ('✓ 💬 对话','🔀 别名','🖼 图片','🎬 视频','📡 来源：全部来源','🚦 状态：全部状态','☑️ 多选','🔄 同步元数据','🔄 同步上游模型','◀ 返回主菜单'):
        assert label in labels
    menu._session(7).tab='alias'
    _,alias=menu.render(7)
    assert '➕ 新增别名' in [b['text'] for b in _buttons(alias)]
    _,metadata=menu._metadata_editor_render(7,'rk-1',None,'capacity')
    assert '📐 上下文 ✎' in [b['text'] for b in _buttons(metadata)]
    raw={'inline_keyboard':[[{'text':'供应商账户','callback_data':'fixed','icon_custom_emoji_id':'123','style':'primary'},
        {'text':'返回','callback_data':'parent','extra':'preserved'}]]}
    before=deepcopy(raw)
    result=decorate_keyboard(raw)
    assert raw==before
    assert result['inline_keyboard'][0][0]==raw['inline_keyboard'][0][0]
    assert result['inline_keyboard'][0][1]=={'text':'◀ 返回','callback_data':'parent','extra':'preserved'}
    assert decorate_keyboard(result)==result


def test_nonpaged_delete_and_boolean_submenus_have_icons(env):
    _,edits,_,_=env
    menu._session(7).tab='alias'
    _,kb=menu.render(7)
    menu.handle_callback(7,10,'open',_button(kb,'1')['callback_data'])
    menu.handle_callback(7,10,'delete',_button(edits[-1][3],'删除此别名')['callback_data'])
    assert [b['text'] for b in _buttons(edits[-1][3])]==['🗑️ 确认删除','❌ 取消']
    menu.handle_callback(7,10,'cancel',_button(edits[-1][3],'取消')['callback_data'])
    _,kb=menu._metadata_editor_render(7,'rk-1',None,'capability')
    menu.handle_callback(7,10,'vision',_button(kb,'图片输入')['callback_data'])
    assert [b['text'] for b in _buttons(edits[-1][3])]==['♻️ 恢复继承','✅ 支持','🚫 不支持','❌ 取消']
