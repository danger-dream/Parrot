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
            main_model=v["mainModel"], tool_model=v["toolModel"],
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
    return next(item for item in _buttons(kb) if item["text"] == label)


def test_model_settings_snapshot_has_exact_five_destinations_and_return(env):
    _control, _edits, _answers, _sends = env
    text, kb = menu._settings_render(7)
    assert text == (
        "⚙️ <b>模型设置</b>\n\n适用于全部账号 / 渠道。\n"
        "压缩模型：<code>model-01</code>\n\n"
        "下游请求必须传 model；缺少模型名称返回 400，不补默认值。"
    )
    assert [[item["text"] for item in row] for row in kb["inline_keyboard"]] == [
        ["压缩模型", "OAuth 备用模型"], ["同步元数据"],
        ["图片设置", "视频设置"], ["返回模型列表"],
    ]
    callbacks = [item["callback_data"] for item in _buttons(kb)]
    assert callbacks[1] == "odm:show" and callbacks[-1] == "mc:list"
    frozen = [menu._thaw(7, value.split(":", 2)[2]) for value in callbacks if value.startswith("mc:a:")]
    assert [(item.name, item.data.get("page")) for item in frozen] == [
        ("settings_page", "compression"),
        ("settings_page", "metadata_sync"),
        ("settings_page", "image"),
        ("settings_page", "video"),
    ]


def test_image_snapshot_shared_scope_emoji_and_frozen_toggle(env):
    control, edits, answers, _sends = env
    text, kb = menu._image_settings_render(7)
    assert "GPT / Grok / Antigravity 共用" in text
    assert "GPT / Grok / Antigravity 图片 + Grok 视频" in text
    assert "Antigravity 全局图片模型：<code>3</code>" in text
    assert _button(kb, "Grok 图片模型")["icon_custom_emoji_id"] == ui.provider_custom_emoji_id("xai")
    assert _button(kb, "AG 图片模型")["icon_custom_emoji_id"] == ui.provider_custom_emoji_id("antigravity")
    toggle = _button(kb, "关闭图片接口")["callback_data"]
    menu.handle_callback(7, 10, "toggle", toggle)
    assert control.images.calls == [({"enabled": False}, "i1")]
    assert "图片接口：<code>关</code>" in edits[-1][2]
    menu.handle_callback(7, 10, "replay", toggle)
    assert control.images.calls == [({"enabled": False}, "i1")]
    assert any("页面版本已变化" in str(item[1]) for item in answers)


def test_gpt_account_target_is_explicit_and_only_changes_image_participation(env):
    control, edits, _answers, _sends = env
    _text, kb = menu._gpt_images_render(7)
    callback = _button(kb, "排除 · a@example.invalid")["callback_data"]
    menu.handle_callback(7, 10, "account", callback)
    assert control.images.account_calls == [("openai-a", False, "ia1")]
    assert "☐ <code>a@example.invalid</code> · 排除" in edits[-1][2]


def test_xai_single_rename_and_bulk_are_separate_and_preserve_sibling(env):
    control, _edits, _answers, sends = env
    callback = menu._freeze(
        7, "media_input", mode="rename", provider="xai", kind="image",
        model_id="grok-image-a", revision="x1", owner=ModelOwnerRef(ModelSourceType.GLOBAL), page=1,
    )
    menu.handle_callback(7, 10, "rename", callback)
    assert states.get_state(7)["action"] == "mc_media_edit"
    menu.handle_text_state(7, "mc_media_edit", "grok-image-renamed")
    assert control.xai_media.image_models == ["grok-image-renamed", "grok-image-b"]
    assert control.xai_media.calls == [(
        "rename", ModelKind.IMAGE, "grok-image-a", "grok-image-renamed", "x1",
    )]
    assert "媒体模型已更新" in sends[-1][1]


def test_media_limits_clear_duration_and_timeout_rules(env):
    _control, _edits, _answers, _sends = env
    assert len(menu._parse_media_models(",".join(f"m{i}" for i in range(50)), "xai")) == 50
    with pytest.raises(ValueError):
        menu._parse_media_models(",".join(f"m{i}" for i in range(51)), "xai")
    assert menu._parse_media_model("x" * 128, "xai") == "x" * 128
    with pytest.raises(ValueError):
        menu._parse_media_model("x" * 129, "xai")
    assert len(menu._parse_media_models(",".join(f"a{i}" for i in range(80)), "antigravity")) == 80
    with pytest.raises(ValueError):
        menu._parse_media_models(",".join(f"a{i}" for i in range(81)), "antigravity")
    assert menu._parse_media_models("clear", "antigravity") == ()
    assert menu._parse_duration_input("3d", allow_days=True) == 259200
    with pytest.raises(ValueError):
        menu._parse_duration_input("3d", allow_days=False)
    assert menu._parse_bytes_input("1GB") == 1024**3
    assert menu._parse_bytes_input("500MB") == 500 * 1024**2


def test_antigravity_account_specific_model_is_readonly_and_global_stays_editable(env):
    _control, _edits, _answers, _sends = env
    text, _kb = menu._media_manager_render(7, "antigravity", "image", 1)
    assert "账户专属（只读）" in text and "ag-private" in text
    detail, kb = menu._detail_render(7, "rk-ag-private")
    assert "账户专属 Antigravity 图片模型只读" in detail
    assert "查看只读归属" not in {item["text"] for item in _buttons(kb)}
    assert not any(item["text"] in {"修改当前名称", "移除当前模型"} for item in _buttons(kb))
    readonly_text, readonly_kb = menu._media_detail_render(
        7, "antigravity", "image", "ag-private", "ag1", 1,
        ModelOwnerRef(ModelSourceType.OAUTH, "ag-account-a"), True,
    )
    assert "账户专属 Antigravity 图片模型只读" in readonly_text
    assert not any(item["text"] in {"修改当前名称", "移除当前模型"} for item in _buttons(readonly_kb))


@pytest.mark.parametrize(
    ("resource_key", "heading", "can_remove"),
    [
        ("rk-gpt-main", "GPT / Codex 图片主模型", False),
        ("rk-grok-image-a", "Grok 图片模型", True),
        ("rk-grok-video-a", "Grok 视频模型", True),
        ("rk-ag-global-a", "Antigravity 图片模型", True),
    ],
)
def test_gpt_grok_ag_list_items_have_direct_single_item_actions(
    env, resource_key, heading, can_remove,
):
    _control, _edits, _answers, _sends = env
    text, kb = menu._detail_render(7, resource_key)
    assert heading in text
    labels = {item["text"] for item in _buttons(kb)}
    assert "修改当前名称" in labels
    assert ("移除当前模型" in labels) is can_remove
    assert "查看当前媒体模型" not in labels


def test_media_list_opens_actionable_item_and_cancel_returns_b_then_a(env):
    _control, edits, _answers, sends = env
    state = menu._session(7)
    state.tab = "image"
    state.text = "ag-global-a"
    state.page = 1
    list_text, list_kb = menu.render(7)
    assert "模型中心 · 图片 · 1 个" in list_text

    menu.handle_callback(7, 10, "open", _button(list_kb, "1")["callback_data"])
    detail_text, detail_kb = edits[-1][2], edits[-1][3]
    assert "Antigravity 图片模型" in detail_text
    assert "模型：<code>ag-global-a</code>" in detail_text
    assert "修改当前名称" in {item["text"] for item in _buttons(detail_kb)}
    assert "查看当前媒体模型" not in {item["text"] for item in _buttons(detail_kb)}

    menu.handle_callback(
        7, 10, "rename", _button(detail_kb, "修改当前名称")["callback_data"],
    )
    assert states.get_state(7)["action"] == "mc_media_edit"
    assert menu.handle_text_state(7, "mc_media_edit", "/cancel") is True
    cancel_kb = sends[-1][2]
    menu.handle_callback(7, 10, "cancel-back", _button(cancel_kb, "返回")["callback_data"])
    detail_again, detail_again_kb = edits[-1][2], edits[-1][3]
    assert "模型：<code>ag-global-a</code>" in detail_again

    menu.handle_callback(
        7, 10, "back-list", _button(detail_again_kb, "返回")["callback_data"],
    )
    returned = edits[-1][2]
    assert "模型中心 · 图片 · 1 个" in returned
    assert "查询：<code>ag-global-a</code>" in returned
    assert (state.tab, state.text, state.page, state.source) == (
        "image", "ag-global-a", 1, None,
    )


def test_media_same_name_uses_owner_identity_not_label_or_position(env):
    _control, _edits, _answers, _sends = env
    global_text, global_kb = menu._detail_render(7, "rk-ag-global-same")
    account_text, account_kb = menu._detail_render(7, "rk-ag-account-same")
    assert "归属：<code>全局</code>" in global_text
    assert {item["text"] for item in _buttons(global_kb)} >= {
        "修改当前名称", "移除当前模型",
    }
    assert "AG Team · ag-owner@example.test" in account_text
    assert not ({"修改当前名称", "移除当前模型"} & {
        item["text"] for item in _buttons(account_kb)
    })


def test_settings_round_trip_preserves_existing_cross_page_selection(env):
    _control, edits, _answers, _sends = env
    state = menu._session(7)
    state.tab = "chat"
    state.page = 2
    state.multiple = True
    state.selected = ["model-01", "model-09"]
    state.selected_resources = {"model-01": "rk-1", "model-09": "rk-9"}
    _text, list_kb = menu.render(7)
    menu.handle_callback(7, 10, "settings", _button(list_kb, "模型设置")["callback_data"])
    settings_kb = edits[-1][3]
    menu.handle_callback(
        7, 10, "back", _button(settings_kb, "返回模型列表")["callback_data"],
    )
    assert state.page == 2 and state.multiple is True
    assert state.selected == ["model-01", "model-09"]
    assert state.selected_resources == {"model-01": "rk-1", "model-09": "rk-9"}
    assert "多选：已选 <b>2</b> 项" in edits[-1][2]


def test_public_settings_provider_item_returns_each_direct_parent_and_list_context(env):
    _control, edits, _answers, _sends = env
    state = menu._session(7)
    state.tab = "image"
    state.text = "same"
    state.page = 1
    list_text, list_kb = menu.render(7)
    assert "模型中心 · 图片 · 2 个" in list_text
    expected = (state.tab, state.text, state.page, state.source, state.status)

    menu.handle_callback(7, 10, "settings", _button(list_kb, "模型设置")["callback_data"])
    settings_kb = edits[-1][3]
    menu.handle_callback(7, 10, "image", _button(settings_kb, "图片设置")["callback_data"])
    image_kb = edits[-1][3]
    menu.handle_callback(7, 10, "ag", _button(image_kb, "AG 图片模型")["callback_data"])
    manager_kb = edits[-1][3]
    menu.handle_callback(7, 10, "item", _button(manager_kb, "1")["callback_data"])
    detail_kb = edits[-1][3]

    menu.handle_callback(7, 10, "to-manager", _button(detail_kb, "返回")["callback_data"])
    assert "Antigravity 图片模型" in edits[-1][2]
    manager_kb = edits[-1][3]
    menu.handle_callback(7, 10, "to-image", _button(manager_kb, "返回图片设置")["callback_data"])
    assert edits[-1][2].startswith("🖼 <b>图片设置</b>")
    image_kb = edits[-1][3]
    menu.handle_callback(7, 10, "to-settings", _button(image_kb, "返回模型设置")["callback_data"])
    assert edits[-1][2].startswith("⚙️ <b>模型设置</b>")
    settings_kb = edits[-1][3]
    menu.handle_callback(7, 10, "to-list", _button(settings_kb, "返回模型列表")["callback_data"])
    assert "模型中心 · 图片 · 2 个" in edits[-1][2]
    assert (state.tab, state.text, state.page, state.source, state.status) == expected


def test_legacy_image_mapping_callbacks_redirect_without_old_write(env):
    control, edits, _answers, _sends = env
    assert menu.handle_callback(7, 10, "old-img", "img:toggle") is True
    assert control.images.calls == []
    assert edits[-1][2].startswith("🖼 <b>图片设置</b>")
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
