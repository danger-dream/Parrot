"""Regression tests for TG-01..07 using actual callback producers/consumers."""
from __future__ import annotations

from dataclasses import replace
from html.parser import HTMLParser
import os

import pytest

from src.management_control.models import ModelSourceRef, ModelSourceType
from src.telegram import bot, states, ui
from src.telegram.menus import model_center_menu as menu, oauth_account_models_menu as oam
from src.telegram.menus.model_center_html import split_html
from src.tests.test_model_center_tg_core import env, _button, _buttons


def test_protection_loaded():
    assert os.environ['PARROT_TEST_CONFTEST_PROBE'] == 'absolute-paths-ok-before-collection'
    assert os.environ['PARROT_TEST_NETWORK_GUARD'] == 'loopback-only'


def test_sync_selected_freezes_rendered_targets_source_revision_and_parent(env, monkeypatch):
    control, edits, answers, sends = env
    source = ModelSourceRef(ModelSourceType.OAUTH, 'acct-a')
    menu._session(7).source = source
    menu.handle_callback(7, 10, 'multi', 'mc:multi')
    menu.handle_callback(7, 10, 'a', _button(edits[-1][3], '1')['callback_data'])
    old_sync = _button(edits[-1][3], '同步所选元数据')['callback_data']
    menu.handle_callback(7, 11, 'clear', 'mc:clear_selection')
    menu._session(7).source = None
    menu.handle_callback(7, 11, 'b', _button(edits[-1][3], '2')['callback_data'])
    original_get_metadata = control.mapping.get_metadata
    def no_reinterpretation(*args, **kwargs):
        raise AssertionError('sync consumer must not read current metadata/selection')
    monkeypatch.setattr(control.mapping, 'get_metadata', no_reinterpretation)
    menu.handle_callback(7, 10, 'old-sync', old_sync)
    call = control.mapping.sync_calls[-1]
    assert [(t.model_id, t.source) for t in call['targets']] == [('model-01', source)]
    assert call['expected_revision'] == 'r1'
    # Return to the exact selection represented by the original operation button.
    monkeypatch.setattr(control.mapping, 'get_metadata', original_get_metadata)
    menu.handle_callback(7, 10, 'back', _button(edits[-1][3], '返回')['callback_data'])
    assert menu._session(7).selected == ['model-01']
    assert menu._session(7).source == source


def test_sync_one_does_not_replace_old_revision_with_current(env, monkeypatch):
    control, edits, answers, sends = env
    _, kb = menu._detail_render(7, 'rk-1')
    old = _button(kb, '同步元数据')['callback_data']
    original = control.mapping.get_metadata
    def changed(*args, **kwargs):
        record = original(*args, **kwargs)
        record.revision = 'r2'
        return record
    monkeypatch.setattr(control.mapping, 'get_metadata', changed)
    menu.handle_callback(7, 10, 'old-sync', old)
    assert control.mapping.sync_calls[-1]['expected_revision'] == 'r1'


def test_unfrozen_legacy_sync_action_rejected_and_other_chat_cannot_replay(env):
    control, edits, answers, sends = env
    legacy = menu._freeze(7, 'sync_one', resource_key='rk-1', source=None)
    menu.handle_callback(7, 10, 'old', legacy)
    assert not control.mapping.sync_calls
    assert '过期' in answers[-1][1]
    _, kb = menu._detail_render(7, 'rk-1')
    assert menu._thaw(8, _button(kb, '同步元数据')['callback_data'].split(':')[2]) is None


def test_leaving_model_input_for_main_cancels_write(env, monkeypatch):
    control, edits, answers, sends = env
    _, kb = menu._metadata_editor_render(7, 'rk-1', None, 'capacity')
    menu.handle_callback(7, 10, 'field', _button(kb, '上下文 ✎')['callback_data'])
    assert states.get_state(7)['action'] == 'mc_metadata_field'
    monkeypatch.setattr(bot.main_menu, 'handle_back', lambda *args: None)
    bot._handle_callback({'id':'main', 'from':{'id':7}, 'message':{'chat':{'id':7}, 'message_id':10}, 'data':'menu:main'})
    assert states.get_state(7) is None
    assert menu._session(7).generation == ''
    bot._handle_message({'chat':{'id':7}, 'text':'300k'})
    assert control.mapping.patch_calls == []


def test_commands_cancel_only_mc_and_permission_is_rechecked(env, monkeypatch):
    control, edits, answers, sends = env
    menu.handle_callback(7, 10, 'query', 'mc:query')
    monkeypatch.setattr(bot.main_menu, 'on_menu_command', lambda chat: None)
    bot._handle_message({'chat':{'id':7}, 'text':'/menu'})
    assert states.get_state(7) is None and menu._session(7).generation == ''
    states.set_state(7, 'legacy-input', {'preserve': True})
    menu.before_command(7, '/menu')
    menu.before_callback(7, 'menu:main')
    assert states.get_state(7)['action'] == 'legacy-input'
    menu.handle_callback(7, 10, 'query', 'mc:query')
    monkeypatch.setattr(ui, 'is_admin', lambda chat: False)
    menu.handle_text_state(7, 'mc_query', 'must-not-apply')
    assert menu._session(7).text == ''


def test_returning_to_alias_list_invalidates_old_target_without_undoing_committed_name(env):
    control, edits, answers, sends = env
    menu.handle_callback(7,10,'aliases','mc:aliases')
    menu.handle_callback(7,10,'open',_button(edits[-1][3],'1')['callback_data'])
    menu.handle_callback(7,10,'name',_button(edits[-1][3],'编辑别名名称')['callback_data'])
    menu.handle_text_state(7,'mc_alias_name','edited-immediately')
    draft = menu._alias_drafts[7]
    abandoned = menu._freeze(7, 'alias_target', draft_id=draft.draft_id, model_id='model-02')
    menu.handle_callback(7,10,'back',_button(sends[-1][2],'返回别名列表')['callback_data'])
    menu.handle_callback(7,11,'old-target',abandoned)
    assert control.mapping.update_calls == [('quick', 'edited-immediately', 'model-01', 'r1')]
    assert control.mapping.records['edited-immediately'] == ('model-01', 'r2')
    assert 7 not in menu._alias_drafts


class BalanceParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.text = []
    def handle_starttag(self, tag, attrs):
        self.stack.append(tag)
    def handle_endtag(self, tag):
        assert self.stack and self.stack[-1] == tag
        self.stack.pop()
    def handle_data(self, value):
        self.text.append(value)


def _plain(text):
    parser = BalanceParser()
    parser.feed(text)
    parser.close()
    assert parser.stack == []
    assert len(text) <= 4096 and len(text.encode('utf-16-le')) // 2 <= 4096
    return ''.join(parser.text)


def test_long_detail_keeps_all_aliases_valid_html_and_frozen_actions(env):
    control, edits, answers, sends = env
    aliases = tuple(f'a{i:02d}' + 'x'*297 for i in range(15))
    view = control.views['model-01']
    control.views['model-01'] = replace(view, aliases=aliases)
    text, kb = menu._detail_render(7, 'rk-1')
    original_write = _button(kb, '停用模型')['callback_data']
    pieces = []
    while True:
        pieces.append(_plain(text))
        assert _button(kb, '停用模型')['callback_data'] == original_write
        assert all(len(b['callback_data'].encode()) <= 64 for b in _buttons(kb))
        more = _button(kb, '内容 ▶')['callback_data']
        if more == 'mc:noop':
            break
        menu.handle_callback(7, 10, 'more', more)
        text, kb = edits[-1][2:4]
    assert len(pieces) > 1
    combined = ''.join(pieces)
    assert all(alias in combined for alias in aliases)
    assert '图片输入' in combined and '不支持' in combined and '$0' in combined


def test_content_paging_preserves_only_its_own_alias_draft(env):
    control, edits, answers, sends = env
    view = control.views['model-01']
    control.views['model-01'] = replace(view, aliases=tuple('x'*300 for _ in range(16)))
    _, old_detail = menu._detail_render(7, 'rk-1')
    old_content = _button(old_detail, '内容 ▶')['callback_data']
    menu.handle_callback(7, 10, 'aliases', 'mc:aliases')
    menu.handle_callback(7, 10, 'open', _button(edits[-1][3], '1')['callback_data'])
    draft = menu._alias_drafts[7]
    old_save = menu._freeze(7, 'alias_target', draft_id=draft.draft_id, model_id='model-02')
    for key, item in list(control.views.items()):
        control.views[key] = replace(item, model_id=key + '<&>'*90)
    _, target = menu._alias_target_render(7, draft.draft_id, 1)
    menu.handle_callback(7, 10, 'target-content', _button(target, '内容 ▶')['callback_data'])
    assert menu._alias_drafts[7] is draft
    _plain(edits[-1][2])
    menu.handle_callback(7, 10, 'old-detail-content', old_content)
    assert 7 not in menu._alias_drafts
    menu.handle_callback(7, 10, 'old-save', old_save)
    assert control.mapping.update_calls == []


@pytest.mark.parametrize('raw', ['<&>中文🦜'*1200, '&amp;'*4000, 'x'*10000])
def test_html_continuations_are_lossless_with_entities_and_unicode(raw):
    escaped = ui.escape_html(raw)
    pages = split_html('<b>标题</b>\n<code>' + escaped + '</code>')
    assert ''.join(_plain(page) for page in pages) == '标题\n' + raw


def test_long_filtered_list_query_remains_accessible(env):
    control, edits, answers, sends = env
    raw = '<&>🦜'*1200
    menu._session(7).text = raw
    text, kb = menu.render(7)
    parts = []
    while True:
        parts.append(_plain(text))
        more = _button(kb, '内容 ▶')['callback_data']
        if more == 'mc:noop':
            break
        menu.handle_callback(7, 10, 'more', more)
        text, kb = edits[-1][2:4]
    assert raw in ''.join(parts)
    assert menu._session(7).text == raw


@pytest.mark.parametrize('exit_callback', ['mc:aliases', 'mc:list', 'mc:settings', 'mc:tab:chat'])
def test_exiting_alias_editor_revokes_draft_but_field_cancel_keeps_it(env, exit_callback, monkeypatch):
    control, edits, answers, sends = env
    monkeypatch.setattr(control.mapping, 'get_compression', lambda ctx: (None, 'r1'), raising=False)
    menu.handle_callback(7, 10, 'aliases', 'mc:aliases')
    menu.handle_callback(7, 10, 'open', _button(edits[-1][3], '1')['callback_data'])
    save = menu._freeze(7, 'alias_target', draft_id=menu._alias_drafts[7].draft_id, model_id='model-02')
    menu.handle_callback(7, 10, 'name', _button(edits[-1][3], '编辑别名名称')['callback_data'])
    draft = menu._alias_drafts[7]
    menu.handle_text_state(7, 'mc_alias_name', '/cancel')
    assert menu._alias_drafts[7] is draft
    assert control.mapping.update_calls == []
    menu.handle_callback(7, 10, 'back', _button(sends[-1][2], '返回')['callback_data'])
    assert '编辑别名' in edits[-1][2]
    menu.handle_callback(7, 10, 'exit', exit_callback)
    assert 7 not in menu._alias_drafts
    menu.handle_callback(7, 10, 'old-save', save)
    assert control.mapping.update_calls == []


@pytest.mark.parametrize('field_key,raw,expected', [
    ('inputPricePer1M', '0', 0), ('reasoningEfforts', '-', []), ('serviceTiers', '-', []),
])
def test_explicit_zero_and_empty_lists_remain_set_not_unset(env, field_key, raw, expected):
    control, edits, answers, sends = env
    field = menu._META_BY_KEY[field_key]
    _, kb = menu._metadata_editor_render(7, 'rk-1', None, field.group)
    from src.telegram.menus.model_center_icons import label_with_icon
    button = next(b for b in _buttons(kb) if b['text'] in {label_with_icon(field.label), label_with_icon(field.label + ' ✎')})
    menu.handle_callback(7, 10, 'field', button['callback_data'])
    menu.handle_text_state(7, 'mc_metadata_field', raw)
    _, call = control.mapping.patch_calls[-1]
    assert call['patch'].set_fields == {field_key: expected}
    assert call['patch'].unset_fields == ()


@pytest.mark.parametrize('field', menu._META_FIELDS, ids=lambda item: item.key)
@pytest.mark.parametrize('source', [
    None, ModelSourceRef(ModelSourceType.OAUTH, 'acct-a'),
    ModelSourceRef(ModelSourceType.API, 'channel-public-a'),
])
def test_all_sixteen_fields_have_sparse_single_unset(env, field, source):
    control, edits, answers, sends = env
    if source is not None and source.type is ModelSourceType.API:
        view = control.views['model-01']
        control.views['model-01'] = replace(view, sources=(replace(
            view.sources[0], type=source.type, id=source.id, provider='anthropic', label='Public A',
        ),))
    _, kb = menu._metadata_editor_render(7, 'rk-1', source, field.group)
    from src.telegram.menus.model_center_icons import label_with_icon
    button = next(b for b in _buttons(kb) if b['text'] in {label_with_icon(field.label), label_with_icon(field.label + ' ✎')})
    menu.handle_callback(7, 10, 'field', button['callback_data'])
    choices = edits[-1][3] if field.kind == 'bool' else sends[-1][2]
    menu.handle_callback(7, 10, 'inherit', _button(choices, '恢复继承')['callback_data'])
    model, call = control.mapping.patch_calls[-1]
    assert model == 'model-01'
    assert call['patch'].set_fields == {}
    assert call['patch'].unset_fields == (field.key,)
    assert call['expected_revision'] == 'r1'
    assert call['scope'] == (source.type.value if source else 'global')
    assert states.get_state(7) is None


@pytest.mark.parametrize('kind', ['open','list','sync','bulk','bpage','ball','bclear','binv','bsave','bcancel','detail','toggle','clear','maxctx','bsel'])
def test_legacy_oam_all_formats_keep_account_page_filter_without_write(env, monkeypatch, kind):
    control, edits, answers, sends = env
    short = ui.register_code('old-account-key')
    ref = ui.register_code('old-model-name')
    monkeypatch.setattr(oam.oauth_control,'account_snapshot',lambda key:{'fixture':True} if key=='old-account-key' else None)
    monkeypatch.setattr(oam.oauth_control,'account_id_from_entry',lambda acc:'acct-a')
    inserted = f':{ref}' if kind in {'detail','toggle','clear','maxctx'} else ':1' if kind == 'bsel' else ''
    menu.handle_callback(7,10,'old',f'oam:{kind}:{short}{inserted}:2:3:quota')
    assert menu._session(7).origin == f'oa:view:{short}:3:quota'
    assert control.calls == []


@pytest.mark.parametrize('protocol', ['anthropic', 'openai-chat', 'openai-responses'])
def test_source_brand_not_guessed_from_protocol(env, protocol):
    control, edits, answers, sends=env
    channel=control.channels.values[0]
    channel.provider_id=None
    channel.protocol=protocol
    source=next(o for o in menu._source_options(7) if o.ref.id==channel.id)
    assert source.label == 'API 渠道 · Public A'
    assert source.provider == ''
