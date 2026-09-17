"""Strict TG fixtures for model-center settings, source origins and media."""
from __future__ import annotations

import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))
from src.tests import _isolation
_isolation.isolate()

from dataclasses import replace
from enum import Enum
from types import SimpleNamespace

import pytest

from src.management_control import ManagementError
from src.management_control.errors import ManagementErrorCode
from src.management_control.models import (
    ModelIdentity,
    ModelKind,
    ModelOwnerRef,
    ModelSourceType,
    ModelView,
)
from src.management_control.models.common import ListPage
from src.telegram import states, ui
from src.telegram.menus import model_center_menu as menu
from src.tests.test_model_center_tg_core import _Control, _Patch, _SyncTarget


class _SyncMode(str, Enum):
    ONE = "one"
    SELECTED = "selected"
    SOURCE = "source"
    FULL = "full"


class _Images:
    def __init__(self):
        self.values = {
            "enabled": True,
            "cacheEnabled": False,
            "mainModel": "gpt-main",
            "toolModel": "gpt-image-tool",
            "cachePath": "images",
            "cacheRetentionDays": 30,
            "cacheMaxBytes": 1024**3,
        }
        self.revision = "i1"
        self.calls = []
        self.account_calls = []
        self.accounts = [SimpleNamespace(
            account_id="openai-a", email="a@example.invalid", oauth_enabled=True,
            image_enabled=True, image_cooldown_until=None,
            missing_account_id=False, revision="ia1",
        )]

    def get_settings(self, _ctx):
        v = self.values
        return SimpleNamespace(
            enabled=v["enabled"], cache_enabled=v["cacheEnabled"],
            models={'openai': ['gpt-main'], 'xai': ['grok-image-a', 'grok-image-b']},
            request_timeout_seconds=v.get('requestTimeoutSeconds', 180), job_ttl_seconds=v.get('jobTtlSeconds', 10800),
            cache_path=v["cachePath"], cache_retention_days=v["cacheRetentionDays"],
            cache_max_bytes=v["cacheMaxBytes"], revision=self.revision,
        )

    def update_settings(self, _ctx, patch, *, expected_revision=None):
        if expected_revision != self.revision:
            raise ManagementError(ManagementErrorCode.REVISION_CONFLICT)
        self.calls.append((dict(patch), expected_revision))
        self.values.update(patch)
        self.revision = "i2"
        return self.get_settings(_ctx)

    def list_sources(self, _ctx):
        row = self.accounts[0]
        return [dict(source_id=row.account_id, label=row.email, provider='openai', enabled=row.image_enabled,
            effective_available=row.image_enabled, unavailable_reason=None, revision=row.revision)]

    def update_source(self, ctx, source_id, *, enabled, expected_revision):
        return self.update_account(ctx, source_id, enabled=enabled, expected_revision=expected_revision)

    def statistics(self, _ctx):
        return {'models': [], 'cache': {'files': 0, 'bytes': 0}}

    def list_accounts(self, _ctx):
        return tuple(self.accounts)

    def update_account(self, _ctx, account_id, *, enabled, expected_revision=None):
        if expected_revision != self.accounts[0].revision:
            raise ManagementError(ManagementErrorCode.REVISION_CONFLICT)
        self.account_calls.append((account_id, enabled, expected_revision))
        self.accounts[0] = SimpleNamespace(**{
            **vars(self.accounts[0]), "image_enabled": enabled, "revision": "ia2",
        })
        return self.accounts[0]


class _XaiMedia:
    def __init__(self):
        self.image_models = ["grok-image-a", "grok-image-b"]
        self.video_models = ["grok-video-a"]
        self.job_ttl_seconds = 10800
        self.request_timeout_seconds = 180
        self.revision = "x1"
        self.calls = []

    def get_settings(self, _ctx):
        return SimpleNamespace(
            image_models=tuple(self.image_models), video_models=tuple(self.video_models),
            job_ttl_seconds=self.job_ttl_seconds,
            request_timeout_seconds=self.request_timeout_seconds,
            revision=self.revision,
        )

    def _check(self, expected_revision):
        if expected_revision != self.revision:
            raise ManagementError(ManagementErrorCode.REVISION_CONFLICT)

    def update_settings(self, _ctx, patch, *, expected_revision=None):
        self._check(expected_revision)
        self.calls.append(("bulk", dict(patch), expected_revision))
        if "imageModels" in patch:
            self.image_models = list(patch["imageModels"])
        if "videoModels" in patch:
            self.video_models = list(patch["videoModels"])
        if "jobTtlSeconds" in patch:
            self.job_ttl_seconds = patch["jobTtlSeconds"]
        if "requestTimeoutSeconds" in patch:
            self.request_timeout_seconds = patch["requestTimeoutSeconds"]
        self.revision = "x2"
        return self.get_settings(_ctx)

    def add_model(self, _ctx, *, kind, model_id, expected_revision):
        self._check(expected_revision)
        values = self.image_models if kind is ModelKind.IMAGE else self.video_models
        values.append(model_id)
        self.calls.append(("add", kind, model_id, expected_revision))
        self.revision = "x2"

    def rename_model(self, _ctx, *, kind, old_model_id, new_model_id, expected_revision):
        self._check(expected_revision)
        values = self.image_models if kind is ModelKind.IMAGE else self.video_models
        values[values.index(old_model_id)] = new_model_id
        self.calls.append(("rename", kind, old_model_id, new_model_id, expected_revision))
        self.revision = "x2"

    def remove_model(self, _ctx, *, kind, model_id, expected_revision):
        self._check(expected_revision)
        values = self.image_models if kind is ModelKind.IMAGE else self.video_models
        values.remove(model_id)
        self.calls.append(("remove", kind, model_id, expected_revision))
        self.revision = "x2"


class _AntigravityMedia:
    def __init__(self):
        self.image_models = ["ag-global-a", "ag-global-b", "same-name"]
        self.account_overrides = (("ag-account-a", ("ag-private", "same-name")),)
        self.revision = "ag1"
        self.calls = []

    def get_settings(self, _ctx):
        return SimpleNamespace(
            image_models=tuple(self.image_models),
            account_overrides=self.account_overrides,
            revision=self.revision,
        )

    def _check(self, expected_revision):
        if expected_revision != self.revision:
            raise ManagementError(ManagementErrorCode.REVISION_CONFLICT)

    def update_settings(self, _ctx, *, image_models, expected_revision):
        self._check(expected_revision)
        self.image_models = list(image_models)
        self.calls.append(("bulk", tuple(image_models), expected_revision))
        self.revision = "ag2"
        return self.get_settings(_ctx)

    def add_model(self, _ctx, *, owner, model_id, expected_revision):
        self._check(expected_revision)
        assert owner.type is ModelSourceType.GLOBAL
        self.image_models.append(model_id)
        self.calls.append(("add", owner, model_id, expected_revision))
        self.revision = "ag2"

    def rename_model(self, _ctx, *, owner, old_model_id, new_model_id, expected_revision):
        self._check(expected_revision)
        assert owner.type is ModelSourceType.GLOBAL
        self.image_models[self.image_models.index(old_model_id)] = new_model_id
        self.calls.append(("rename", owner, old_model_id, new_model_id, expected_revision))
        self.revision = "ag2"

    def remove_model(self, _ctx, *, owner, model_id, expected_revision):
        self._check(expected_revision)
        assert owner.type is ModelSourceType.GLOBAL
        self.image_models.remove(model_id)
        self.calls.append(("remove", owner, model_id, expected_revision))
        self.revision = "ag2"


class _SettingsControl(_Control):
    def __init__(self):
        super().__init__()
        self.images = _Images()
        self.videos = _Images()
        self.xai_media = _XaiMedia()
        self.antigravity_media = _AntigravityMedia()
        self.oauth.accounts.append(SimpleNamespace(
            account_id="ag-account-a", display_name="AG Team",
            identity="ag-owner@example.test",
            provider=SimpleNamespace(value="antigravity"),
        ))
        self.mapping.compression = "model-01"
        self.mapping.compression_revision = "c1"
        self.mapping.compression_calls = []
        self.mapping.catalog_revision = "cat1"
        self.mapping.get_compression = lambda _ctx: (
            self.mapping.compression, self.mapping.compression_revision,
        )

        def put_compression(_ctx, model_id, *, expected_revision=None):
            if expected_revision != self.mapping.compression_revision:
                raise ManagementError(ManagementErrorCode.REVISION_CONFLICT)
            self.mapping.compression_calls.append(("put", model_id, expected_revision))
            self.mapping.compression = model_id
            self.mapping.compression_revision = "c2"
            return model_id, "c2"

        def delete_compression(_ctx, *, expected_revision=None):
            if expected_revision != self.mapping.compression_revision:
                raise ManagementError(ManagementErrorCode.REVISION_CONFLICT)
            self.mapping.compression_calls.append(("delete", expected_revision))
            self.mapping.compression = None
            self.mapping.compression_revision = "c2"

        def search_catalog(_ctx, *, provider, query, sort, page, page_size):
            del provider, query, sort
            item = SimpleNamespace(
                key="openai/model-01", model_id="model-01", name="Model 01",
                provider_id="openai", provider_name="OpenAI", revision="cat1",
            )
            values = (item,) if page == 1 else ()
            return ListPage(values[:page_size], page, page_size, 1, "cat1")

        self.mapping.put_compression = put_compression
        self.mapping.delete_compression = delete_compression
        self.mapping.search_catalog = search_catalog

        global_owner = ModelOwnerRef(ModelSourceType.GLOBAL)
        account_owner = ModelOwnerRef(ModelSourceType.OAUTH, "ag-account-a")
        media = (
            ("rk-gpt-main", ModelKind.IMAGE, "gpt-main", "openai", global_owner, True),
            ("rk-grok-image-a", ModelKind.IMAGE, "grok-image-a", "xai", global_owner, True),
            ("rk-grok-video-a", ModelKind.VIDEO, "grok-video-a", "xai", global_owner, True),
            ("rk-ag-global-a", ModelKind.IMAGE, "ag-global-a", "antigravity", global_owner, True),
            ("rk-ag-global-same", ModelKind.IMAGE, "same-name", "antigravity", global_owner, True),
            ("rk-ag-account-same", ModelKind.IMAGE, "same-name", "antigravity", account_owner, False),
        )
        for key, kind, model_id, provider, owner, editable in media:
            self.views[key] = ModelView(
                resource_key=key,
                identity=ModelIdentity(kind, model_id, provider, owner),
                model_id=model_id, aliases=(), global_enabled=None, visible=None,
                common_metadata={}, sources=(), editable=editable, revision="m-media1",
            )
        self.views["ag-private"] = ModelView(
            resource_key="rk-ag-private",
            identity=ModelIdentity(ModelKind.IMAGE, "ag-private", "antigravity", account_owner),
            model_id="ag-private", aliases=(), global_enabled=None, visible=None,
            common_metadata={}, sources=(), editable=False, revision="m-ag1",
        )


@pytest.fixture
def env(monkeypatch):
    control = _SettingsControl()
    edits, answers, sends = [], [], []
    monkeypatch.setattr(menu, "_CONTROL", control)
    symbols = {"MetadataSyncMode": _SyncMode, "MetadataSyncTarget": _SyncTarget,
               "MetadataOverridePatch": _Patch}
    monkeypatch.setattr(menu, "_mapping_symbol", lambda name: symbols[name])
    monkeypatch.setattr(ui, "is_admin", lambda chat_id: int(chat_id) == 7)
    monkeypatch.setattr(ui, "edit", lambda chat, message, text, reply_markup=None, parse_mode="HTML": edits.append((chat, message, text, reply_markup)))
    monkeypatch.setattr(ui, "answer_cb", lambda cb, text=None, show_alert=False: answers.append((cb, text, show_alert)))
    monkeypatch.setattr(ui, "send", lambda chat, text, reply_markup=None, parse_mode="HTML": sends.append((chat, text, reply_markup)))
    monkeypatch.setattr(ui, "send_result", lambda chat, text, **kwargs: sends.append((chat, text, kwargs)))
    menu.reset_for_tests()
    states.clear_all()
    yield control, edits, answers, sends
    menu.reset_for_tests()
    states.clear_all()


def _buttons(kb):
    return [item for row in kb["inline_keyboard"] for item in row]


def _button(kb, label):
    from src.telegram.menus.model_center_icons import label_with_icon
    return next(item for item in _buttons(kb) if item["text"] in (label, label_with_icon(label)))


def test_chat_replaces_settings_with_metadata_and_upstream_sync(env):
    _control, _edits, _answers, _sends = env
    _text, kb = menu.render(7)
    assert all("模型设置" not in b["text"] and "OAuth 备用" not in b["text"] for b in _buttons(kb))
    assert _button(kb, "同步元数据") and _button(kb, "同步上游模型")
    assert not hasattr(menu, "_settings_render") and not hasattr(menu, "_compression_picker_render")


def test_image_snapshot_independent_panel_and_frozen_toggle(env):
    control, edits, answers, _sends = env
    text, kb = menu._image_settings_render(7)
    assert '模型中心 · 图片' in text and '当前缓存占用' in text
    assert '共用' not in text and '主模型' not in text
    assert _button(kb, 'a@example.invalid · 已启用')['icon_custom_emoji_id'] == ui.provider_custom_emoji_id('openai')
    toggle = _button(kb, '图片接口：开启')['callback_data']
    menu.handle_callback(7, 10, 'toggle', toggle)
    assert control.images.calls == [({'enabled': False}, 'i1')]
    assert '图片接口：关' in edits[-1][2] and control.videos.calls == []
    menu.handle_callback(7, 10, 'replay', toggle)
    assert len(control.images.calls) == 1
    assert any('页面版本已变化' in str(item[1]) for item in answers)


def test_gpt_account_target_is_explicit_and_only_changes_image_participation(env):
    control, _edits, _answers, _sends = env
    _text, kb = menu._gpt_images_render(7)
    menu.handle_callback(7, 10, 'account', _button(kb, 'a@example.invalid · 已启用')['callback_data'])
    assert control.images.account_calls == [('openai-a', False, 'ia1')]
    assert control.videos.account_calls == [] and control.images.accounts[0].oauth_enabled


def test_old_xai_model_edit_and_input_do_not_write(env):
    control, _edits, _answers, _sends = env
    callback = menu._freeze(7, 'media_input', mode='rename', provider='xai', kind='image', model_id='grok-image-a')
    menu.handle_callback(7, 10, 'old-edit', callback)
    assert states.get_state(7) is None
    states.set_state(7, 'mc_media_edit', {'kind': 'image', 'provider': 'xai'})
    menu.handle_text_state(7, 'mc_media_edit', 'must-not-save')
    assert control.xai_media.calls == [] and control.xai_media.image_models == ['grok-image-a', 'grok-image-b']


def test_media_duration_and_cache_input_rules(env):
    assert menu._parse_duration_input('3d', allow_days=True) == 259200
    with pytest.raises(ValueError): menu._parse_duration_input('3d', allow_days=False)
    assert menu._parse_bytes_input('1GB') == 1024**3
    assert menu._parse_bytes_input('500MB') == 500 * 1024**2
    control, edits, _answers, sends = env
    _text, kb = menu._video_settings_render(7)
    menu.handle_callback(7, 10, 'ttl', _button(kb, '设置任务 TTL')['callback_data'])
    assert states.get_state(7)['action'] == 'mc_media_field'
    menu.handle_text_state(7, 'mc_media_field', '2h')
    assert control.videos.calls == [({'jobTtlSeconds': 7200}, 'i1')] and control.images.calls == []


def test_retired_antigravity_callback_cannot_write(env):
    control, _edits, answers, _sends = env
    callback = menu._freeze(7, "media_input", provider="antigravity", kind="image", mode="add", revision="ag1")
    menu.handle_callback(7, 10, "retired", callback)
    assert control.antigravity_media.calls == []
    assert any("已移除" in str(row) for row in answers)
    with pytest.raises(ValueError):
        menu._media_manager_render(7, "antigravity", "image", 1)


@pytest.mark.parametrize('resource_key', ['rk-gpt-main', 'rk-grok-image-a', 'rk-grok-video-a'])
def test_old_media_details_land_directly_on_panel_without_model_edits(env, resource_key):
    text, kb = menu._detail_render(7, resource_key)
    assert '模型中心 · ' in text and '配置 / 管理 API' not in text
    assert not any(word in str(kb) for word in ('修改当前名称', '移除当前模型', '查看当前媒体模型', '批量编辑'))


def test_image_panel_does_not_use_old_list_filters(env):
    _control, _edits, _answers, _sends = env
    state = menu._session(7)
    state.tab, state.text, state.page = 'image', 'filter-matches-nothing', 5
    text, kb = menu.render(7)
    assert 'grok-image-a' in text and 'gpt-main' in text
    assert not any(word in str(kb) for word in ('查询', '来源：', '状态：', '多选'))


def test_metadata_round_trip_preserves_existing_cross_page_selection(env):
    _control, edits, _answers, _sends = env
    state = menu._session(7)
    state.tab = "chat"
    state.page = 2
    state.multiple = True
    state.selected = ["model-01", "model-09"]
    state.selected_resources = {"model-01": "rk-1", "model-09": "rk-9"}
    _text, list_kb = menu.render(7)
    menu.handle_callback(7, 10, "settings", _button(list_kb, "同步元数据")["callback_data"])
    settings_kb = edits[-1][3]
    menu.handle_callback(
        7, 10, "back", _button(settings_kb, "返回模型列表")["callback_data"],
    )
    assert state.page == 2 and state.multiple is True
    assert state.selected == ["model-01", "model-09"]
    assert state.selected_resources == {"model-01": "rk-1", "model-09": "rk-9"}
    assert "多选：已选 <b>2</b> 项" in edits[-1][2]


@pytest.mark.parametrize("kind", ["image", "video"])
def test_media_panel_exact_bottom_rows_and_no_unknown_notes(env, monkeypatch, kind):
    control, edits, _answers, _sends = env
    target = control.images if kind == "image" else control.videos
    monkeypatch.setattr(target, 'statistics', lambda ctx: {
        'models': [{'model': 'gpt-main' if kind == 'image' else 'grok-video-a',
                    'generated_count': 4, 'recorded_bytes': 1024,
                    'unknown_count_calls': 2, 'unknown_bytes_calls': 1}],
        'cache': {'files': 1, 'bytes': 100}})
    menu._session(7).tab = kind
    text, kb = menu.render(7)
    assert '已生成：4' in text
    for removed in ('未知', '统计为历史成功产物', '模型名称通过', '模型设置'):
        assert removed not in text + str(kb)
    rows = kb['inline_keyboard']
    if kind == 'image':
        assert rows[-1] == [_button(kb, '多媒体日志'), _button(kb, '返回主菜单')]
    else:
        assert rows[-2] == [_button(kb, '设置任务 TTL'), _button(kb, '多媒体日志')]
        assert rows[-1] == [_button(kb, '返回主菜单')]


def test_detail_compression_changes_immediately_and_returns_to_detail(env):
    control, edits, answers, _sends = env
    _text, kb = menu._detail_render(7, 'rk-2')
    callback = _button(kb, '设置为压缩模型')['callback_data']
    menu.handle_callback(7, 10, 'set', callback)
    assert control.mapping.compression_calls == [('put', 'model-02', 'c1')]
    assert '当前压缩模型' in edits[-1][2] and '<b>model-02</b>' in edits[-1][2]
    assert _button(edits[-1][3], '清除压缩指定')
    menu.handle_callback(7, 10, 'replay', callback)
    assert len(control.mapping.compression_calls) == 1
    assert any('页面版本已变化' in str(a) for a in answers)
    clear = _button(edits[-1][3], '清除压缩指定')['callback_data']
    menu.handle_callback(7, 10, 'clear', clear)
    assert control.mapping.compression is None and control.mapping.compression_calls[-1] == ('delete', 'c2')
    assert _button(edits[-1][3], '设置为压缩模型')


def test_retired_compression_picker_callback_cannot_write(env):
    control, _edits, answers, _sends = env
    callback = menu._freeze(7, 'compression_save', model_id='model-02', revision='c1')
    menu.handle_callback(7, 10, 'old', callback)
    assert not control.mapping.compression_calls
    assert any('请从模型详情' in str(a) for a in answers)


def test_legacy_image_mapping_callbacks_redirect_without_old_write(env):
    control, edits, _answers, _sends = env
    assert menu.handle_callback(7, 10, "old-img", "img:toggle") is True
    assert control.images.calls == []
    assert "模型中心 · 图片" in edits[-1][2]
    assert menu.handle_callback(7, 10, "old-map", "map:edit_alias:any:any") is True
    assert control.mapping.update_calls == []
    assert edits[-1][2].startswith("🔀 <b>模型别名")


def test_public_source_callback_freezes_origin_and_source(env):
    _control, edits, _answers, _sends = env
    callback = menu.source_callback(
        7, source_type="api", source_id="channel-public-a", origin="ch:view:short:3",
    )
    menu.handle_callback(7, 10, "source", callback)
    state = menu._session(7)
    assert state.source.type is ModelSourceType.API
    assert state.source.id == "channel-public-a"
    assert state.origin == "ch:view:short:3"
    assert f"来源：{ui.provider_custom_emoji_html('anthropic')} <code>Claude · Public A</code>" in edits[-1][2]
    assert "channel-public-a" not in edits[-1][2]
    assert len(callback.encode()) <= 64
