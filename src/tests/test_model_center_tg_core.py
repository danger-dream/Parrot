"""Telegram model-center contract tests (pure fixture, no real upstream/service)."""
from __future__ import annotations

import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))
from src.tests import _isolation
_isolation.isolate()

from dataclasses import dataclass, replace
from enum import Enum
from types import SimpleNamespace

import pytest

from src.management_control import ManagementError
from src.management_control.errors import ManagementErrorCode
from src.management_control.mapping import MappingRecord
from src.management_control.models import (
    ModelFilters,
    ModelIdentity,
    ModelKind,
    ModelOwnerRef,
    ModelPage,
    ModelSelectionMode,
    ModelSourceRef,
    ModelSourceType,
    ModelSourceView,
    ModelStateField,
    ModelStatus,
    ModelView,
)
from src.management_control.models.common import ListPage
from src.telegram import states, ui
from src.telegram.menus import model_center_menu as menu


@dataclass(frozen=True)
class _Patch:
    set_fields: dict
    unset_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class _SyncTarget:
    model_id: str
    source: ModelSourceRef | None = None


class _SyncMode(str, Enum):
    ONE = "one"
    SELECTED = "selected"


class _Mapping:
    def __init__(self):
        self.records = {"quick": ("model-01", "r1")}
        self.update_calls = []
        self.put_calls = []
        self.delete_calls = []
        self.patch_calls = []
        self.reset_calls = []
        self.sync_calls = []

    def get_compression(self, _ctx):
        return None, "c1"

    def list_mappings(self, _ctx, *, query, sort, page, page_size):
        assert sort == "alias"
        values = [MappingRecord(alias, real, "global", revision) for alias, (real, revision) in self.records.items()]
        if query:
            values = [item for item in values if query.casefold() in (item.alias + item.real_model).casefold()]
        start = (page - 1) * page_size
        return ListPage(tuple(values[start:start + page_size]), page, page_size, len(values), "r1")

    def update_mapping(self, _ctx, old_alias, *, new_alias, real_model, expected_revision):
        self.update_calls.append((old_alias, new_alias, real_model, expected_revision))
        if expected_revision != self.records[old_alias][1]:
            raise ManagementError(ManagementErrorCode.REVISION_CONFLICT)
        revision = f"r{int(expected_revision[1:]) + 1}"
        self.records.pop(old_alias)
        self.records[new_alias] = (real_model, revision)
        return MappingRecord(new_alias, real_model, "global", revision)

    def put_mapping(self, _ctx, alias, real_model, *, expected_revision=None):
        self.put_calls.append((alias, real_model, expected_revision))
        self.records[alias] = (real_model, "r2")
        return MappingRecord(alias, real_model, "global", "r2")

    def delete_mapping(self, _ctx, alias, *, expected_revision=None):
        self.delete_calls.append((alias, expected_revision))
        self.records.pop(alias)

    def get_metadata(self, _ctx, model_id, *, scope_id=None):
        source_override = {"contextWindow": 300_000} if scope_id else {}
        return SimpleNamespace(
            model_id=model_id,
            effective={
                "contextWindow": 300_000 if scope_id else 1_000_000,
                "maxInputTokens": 200_000,
                "maxOutputTokens": 32_000,
                "compactTriggerTokens": 240_000,
                "vision": False,
                "toolCall": True,
                "structuredOutput": True,
                "reasoningEfforts": ["low", "high"],
                "serviceTiers": [],
                "knowledgeCutoff": "2025-06",
                "inputPricePer1M": 0,
                "outputPricePer1M": 2.5,
            },
            value_source={"contextWindow": "sourceOverride" if scope_id else "commonOverride"},
            constrained_by={},
            common_override={"contextWindow": 1_000_000} if not scope_id else {},
            source_override=source_override,
            revision="r1",
        )

    def patch_metadata_overrides(self, _ctx, model_id, **kwargs):
        self.patch_calls.append((model_id, kwargs))
        return self.get_metadata(_ctx, model_id, scope_id=kwargs.get("account_id") or kwargs.get("channel_id"))

    def delete_metadata_overrides(self, _ctx, model_id, **kwargs):
        self.reset_calls.append((model_id, kwargs))

    def start_metadata_sync(self, _ctx, **kwargs):
        self.sync_calls.append(kwargs)
        return SimpleNamespace(id="meta-op-1")


class _OAuth:
    def __init__(self):
        self.get_account_calls = []
        self.accounts = [
            SimpleNamespace(
                account_id="acct-a", display_name="OpenAI A", identity="openai-a@example.test",
                provider=SimpleNamespace(value="openai"),
            ),
            SimpleNamespace(
                account_id="cursor:opaque-a", display_name="Team", identity="cursor-a@example.test",
                provider=SimpleNamespace(value="cursor"),
            ),
            SimpleNamespace(
                account_id="cursor:opaque-b", display_name="Team", identity="cursor-b@example.test",
                provider=SimpleNamespace(value="cursor"),
            ),
            SimpleNamespace(
                account_id="xai:opaque-x", display_name="Grok Lab", identity="grok@example.test",
                provider=SimpleNamespace(value="xai"),
            ),
        ]

    def list_accounts(self, _ctx, *, page):
        return SimpleNamespace(
            items=tuple(self.accounts), meta=SimpleNamespace(total=len(self.accounts)), revision="or1",
        )

    def get_account(self, _ctx, account_id):
        self.get_account_calls.append(account_id)
        raise AssertionError("source labels must not load full OAuth account details")

    def list_models(self, _ctx, account_id, *, page):
        if account_id != "acct-a":
            return SimpleNamespace(items=(), revision="or1")
        item = SimpleNamespace(
            model_id="upstream-01", max_context_default=False,
            max_context_window=1_000_000,
        )
        return SimpleNamespace(items=(item,), revision="or1")

    def update_model_settings(self, *_args, **_kwargs):
        return None


class _Channels:
    def __init__(self):
        self.values = [SimpleNamespace(
            id="channel-public-a", display_name="Public A", provider_id="anthropic",
            protocol=SimpleNamespace(value="anthropic"),
        )]

    def list_all(self, _ctx):
        return tuple(self.values)


class _Control:
    def __init__(self):
        self.revision = "r1"
        self.calls = []
        self.mapping = _Mapping()
        self.oauth = _OAuth()
        self.channels = _Channels()
        # Media tabs are purpose panels, not filtered model-list fixtures.
        media_settings = SimpleNamespace(enabled=True, cache_enabled=False, cache_path='images',
            cache_retention_days=0, cache_max_bytes=0, models={}, request_timeout_seconds=180,
            job_ttl_seconds=10800, revision='media-r1')
        self.images = SimpleNamespace(get_settings=lambda ctx: media_settings, list_sources=lambda ctx: [],
            statistics=lambda ctx: {'models': [], 'cache': {'files': 0, 'bytes': 0}})
        self.videos = self.images
        self.query_calls = []
        self.views = {}
        source = ModelSourceRef(ModelSourceType.OAUTH, "acct-a")
        for index in range(1, 13):
            model_id = f"model-{index:02d}"
            source_view = ModelSourceView(
                type=source.type,
                id=source.id,
                label="OpenAI A",
                provider="openai",
                outbound_model=f"upstream-{index:02d}",
                source_enabled=index != 2,
                container_enabled=True,
                effective_routable=index != 2,
                effective_metadata={
                    "contextWindow": 300_000 if index == 1 else 1_000_000,
                    "maxOutputTokens": 32_000,
                    "vision": False,
                    "inputPricePer1M": 0,
                },
                value_source={"contextWindow": "sourceOverride" if index == 1 else "catalog"},
                constrained_by={},
            )
            identity = ModelIdentity(ModelKind.CHAT, model_id)
            self.views[model_id] = ModelView(
                resource_key=f"rk-{index}", identity=identity, model_id=model_id,
                aliases=(("quick",) if index == 1 else ()),
                global_enabled=index != 3,
                visible=index != 4,
                common_metadata={"contextWindow": 1_000_000},
                sources=(source_view,), editable=True, revision=self.revision,
            )

    def bind_telegram_actor(self, chat_id):
        return SimpleNamespace(chat_id=chat_id)

    def _matches(self, view, filters):
        if filters and filters.kinds and view.identity.kind not in filters.kinds:
            return False
        if filters and filters.text:
            haystack = " ".join((view.model_id, *view.aliases, *(item.outbound_model for item in view.sources))).casefold()
            if filters.text.casefold() not in haystack:
                return False
        if filters and filters.source:
            if not any(item.type is filters.source.type and item.id == filters.source.id for item in view.sources):
                return False
        if filters and filters.statuses:
            status = filters.statuses[0]
            source = filters.source
            enabled = bool(view.global_enabled)
            if source:
                enabled = next(item.source_enabled for item in view.sources if item.id == source.id)
            if status.value == "enabled" and not enabled:
                return False
            if status.value == "disabled" and enabled:
                return False
            if status.value == "hidden" and view.visible:
                return False
        return True

    def list_models(self, _ctx=None, *, filters=None, page=1, page_size=50):
        self.query_calls.append((filters, page, page_size))
        values = [replace(view, revision=self.revision) for view in self.views.values() if self._matches(view, filters)]
        start = (page - 1) * page_size
        return ModelPage(tuple(values[start:start + page_size]), page, page_size, len(values), start + page_size < len(values), self.revision)

    def get_model(self, _ctx, resource_key):
        for view in self.views.values():
            if view.resource_key == resource_key:
                return replace(view, revision=self.revision)
        raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)

    def set_state(self, _ctx, *, scope, selection, target, expected_revision):
        if expected_revision != self.revision:
            raise ManagementError(ManagementErrorCode.REVISION_CONFLICT)
        if selection.mode is ModelSelectionMode.IDS:
            ids = list(selection.model_ids)
        else:
            ids = [view.model_id for view in self.list_models(filters=selection.filters).items if view.model_id not in set(selection.excluded_model_ids)]
        self.calls.append((scope, selection, target, expected_revision))
        for model_id in ids:
            view = self.views[model_id]
            if target.field is ModelStateField.VISIBLE:
                self.views[model_id] = replace(view, visible=target.value)
            elif scope is None:
                self.views[model_id] = replace(view, global_enabled=target.value)
            else:
                sources = tuple(
                    replace(item, source_enabled=target.value)
                    if item.type is scope.type and item.id == scope.id else item
                    for item in view.sources
                )
                self.views[model_id] = replace(view, sources=sources)
        self.revision = "r2" if self.revision == "r1" else "r3"
        return SimpleNamespace(revision=self.revision)

    def sync_source_models(self, _ctx, source):
        return SimpleNamespace(id="op-1")

    def clear_model_errors(self, *_args, **_kwargs):
        return None


@pytest.fixture
def env(monkeypatch):
    control = _Control()
    edits = []
    answers = []
    sends = []
    monkeypatch.setattr(menu, "_CONTROL", control)
    symbols = {
        "MetadataOverridePatch": _Patch,
        "MetadataSyncTarget": _SyncTarget,
        "MetadataSyncMode": _SyncMode,
    }
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
    return [button for row in kb["inline_keyboard"] for button in row]


def _button(kb, text):
    # Flow locator only; exact icon rendering is tested independently.
    from src.telegram.menus.model_center_icons import label_with_icon
    return next(item for item in _buttons(kb) if item["text"] in {text, label_with_icon(text)})


def test_workbuddy_source_picker_uses_summary_name_without_full_detail_or_internal_id(env):
    control, edits, _answers, _sends = env
    cn_id = "workbuddy:cn:7009a407-312c-464e-8e69-67ba00000001:p"
    global_id = "workbuddy:global:03ebab31-2aef-4000-8000-000000000002:p"
    unnamed_id = "workbuddy:cn:10000000-2000-4000-8000-000000000003:p"
    control.oauth.accounts.extend([
        SimpleNamespace(
            account_id=cn_id, display_name="18213053121",
            identity="cn:7009a407-312c-464e-8e69-67ba00000001:p",
            provider=SimpleNamespace(value="workbuddy"),
        ),
        SimpleNamespace(
            account_id=global_id, display_name="soarsky0204@gmail.com",
            identity="soarsky0204@gmail.com",
            provider=SimpleNamespace(value="workbuddy"),
        ),
        SimpleNamespace(
            account_id=unnamed_id,
            display_name=unnamed_id,
            identity="cn:10000000-2000-4000-8000-000000000003:p",
            provider=SimpleNamespace(value="workbuddy"),
        ),
    ])

    menu.handle_callback(7, 10, "source", "mc:source")
    picker_text = "\n".join(button["text"] for button in _buttons(edits[-1][3]))
    assert "WorkBuddy · 18213053121" in picker_text
    assert "WorkBuddy · soarsky0204@gmail.com" in picker_text
    assert "WorkBuddy · 未命名账户" in picker_text
    assert "7009a407" not in picker_text and "03ebab31" not in picker_text
    assert "10000000-2000" not in picker_text
    assert "cn:" not in picker_text and "global:" not in picker_text
    assert control.oauth.get_account_calls == []


def test_model_list_snapshot_pagination_emoji_and_wire_limits(env):
    _control, _edits, _answers, _sends = env
    text, kb = menu.render(7)
    assert text.startswith("🤖 <b>模型中心 · 对话 · 12 个</b>")
    assert "1. ✅ <code>model-01</code>" in text
    assert "8. ✅ <code>model-08</code>" in text
    assert "9. " not in text
    assert _button(kb, "下一页 ▶")["callback_data"] == "mc:page:2"
    numbered = [item for item in _buttons(kb) if item["text"].isdigit()]
    assert numbered[0]["icon_custom_emoji_id"] == ui.provider_custom_emoji_id("openai")
    assert len(text) <= 4096
    assert all(len(item.get("callback_data", "").encode()) <= 64 for item in _buttons(kb))


def test_cross_page_selection_keeps_two_items_on_each_page_until_done(env):
    _control, edits, _answers, _sends = env
    menu.handle_callback(7, 10, "multi", "mc:multi")
    first_kb = edits[-1][3]
    menu.handle_callback(7, 10, "pick-1", _button(first_kb, "1")["callback_data"])
    first_kb = edits[-1][3]
    menu.handle_callback(7, 10, "pick-2", _button(first_kb, "2")["callback_data"])
    menu.handle_callback(7, 10, "page-2", "mc:page:2")
    second_kb = edits[-1][3]
    menu.handle_callback(7, 10, "pick-9", _button(second_kb, "9")["callback_data"])
    second_kb = edits[-1][3]
    menu.handle_callback(7, 10, "pick-10", _button(second_kb, "10")["callback_data"])
    state = menu._session(7)
    assert state.selected == ["model-01", "model-02", "model-09", "model-10"]
    assert state.page == 2 and state.multiple is True
    menu.handle_callback(7, 10, "done", "mc:done")
    assert state.multiple is False and state.selected == []


def test_query_matches_id_alias_and_upstream_name(env):
    _control, _edits, _answers, sends = env
    menu.handle_callback(7, 10, "query", "mc:query")
    menu.handle_text_state(7, "mc_query", "upstream-11")
    assert "模型中心 · 对话 · 1 个" in sends[-1][1]
    assert "model-11" in sends[-1][1]
    menu.handle_callback(7, 10, "query", "mc:query")
    menu.handle_text_state(7, "mc_query", "quick")
    assert "model-01" in sends[-1][1]


def test_cross_page_selection_mixed_targets_and_visible_scope_none(env):
    control, edits, _answers, _sends = env
    session = menu._session(7)
    session.source = ModelSourceRef(ModelSourceType.OAUTH, "acct-a")
    session.multiple = True
    session.selected = ["model-01", "model-02"]
    session.selected_resources = {"model-01": "rk-1", "model-02": "rk-2"}
    text, kb = menu.render(7)
    assert "多选：已选 <b>2</b> 项" in text
    enabled = _button(kb, "状态：混合")["callback_data"]
    menu.handle_callback(7, 10, "cb-enabled", enabled)
    scope, _selection, target, expected = control.calls[-1]
    assert scope == ModelSourceRef(ModelSourceType.OAUTH, "acct-a")
    assert target.field is ModelStateField.ENABLED and target.value is True
    assert expected == "r1"
    # Replaying the old callback cannot calculate a fresh inverse toggle.
    menu.handle_callback(7, 10, "cb-replay", enabled)
    assert len(control.calls) == 1
    assert any("页面版本已变化" in str(answer[1]) for answer in _answers)

    control.views["model-01"] = replace(control.views["model-01"], visible=True)
    control.views["model-02"] = replace(control.views["model-02"], visible=False)
    _text, kb2 = menu.render(7)
    visible = _button(kb2, "展示开关：混合")["callback_data"]
    menu.handle_callback(7, 10, "cb-visible", visible)
    scope2, _selection2, target2, expected2 = control.calls[-1]
    assert scope2 is None
    assert target2.field is ModelStateField.VISIBLE and target2.value is True
    assert expected2 == "r2"


def test_select_all_invert_clear_are_result_scoped_and_empty_does_not_write(env):
    control, _edits, answers, _sends = env
    menu.handle_callback(7, 10, "cb", "mc:multi")
    menu.handle_callback(7, 10, "cb", "mc:select_all")
    state = menu._session(7)
    assert state.selection_mode is ModelSelectionMode.FILTER
    assert state.selection_filters.kinds == (ModelKind.CHAT,)
    assert menu._selected_count(7) == 12
    menu.handle_callback(7, 10, "cb", "mc:invert")
    assert state.selection_mode is ModelSelectionMode.IDS and state.selected == []

    # Clearing is its own action and must also discard explicit IDs, resources,
    # a filter selection and exclusions without leaving a hidden write target.
    state.selected = ["model-01"]
    state.selected_resources = {"model-01": "rk-1"}
    state.selection_filters = ModelFilters(kinds=(ModelKind.CHAT,), text="model")
    state.excluded = ["model-02"]
    menu.handle_callback(7, 10, "cb", "mc:clear_selection")
    assert state.selection_mode is ModelSelectionMode.IDS
    assert state.selected == [] and state.selected_resources == {}
    assert state.selection_filters is None and state.excluded == []

    _text, kb = menu.render(7)
    menu.handle_callback(7, 10, "cb", _button(kb, "状态：—")["callback_data"])
    assert control.calls == []
    assert any(answer[1] == "请先勾选模型" for answer in answers)


def test_disabled_source_is_not_described_as_actually_listed_downstream(env):
    _control, _edits, _answers, _sends = env
    state = menu._session(7)
    state.source = ModelSourceRef(ModelSourceType.OAUTH, "acct-a")
    state.source_label = "OpenAI · OpenAI A · openai-a@example.test"
    text, _kb = menu.render(7)
    line = next(item for item in text.splitlines() if "model-02" in item)
    assert line == "2. 🚫 <code>model-02</code> · 来源停用"
    assert "✅" not in line and "启用" not in line
    assert "展示开关" not in line and "下游显示" not in line


def test_alias_name_and_target_each_commit_immediately_without_save(env):
    control, edits, _answers, sends = env
    menu._session(7).tab = "alias"
    _text, kb = menu.render(7)
    menu.handle_callback(7, 10, "open", _button(kb, "1")["callback_data"])
    edit_kb = edits[-1][3]
    assert all("保存" not in b["text"] for b in _buttons(edit_kb))
    old_picker = _button(edit_kb, "选择真实模型")["callback_data"]
    menu.handle_callback(7, 10, "rename", _button(edit_kb, "编辑别名名称")["callback_data"])
    menu.handle_text_state(7, "mc_alias_name", "fast")
    assert control.mapping.records["fast"] == ("model-01", "r2")
    assert "quick" not in control.mapping.records
    menu.handle_callback(7, 10, "target-picker", _button(sends[-1][2], "选择真实模型")["callback_data"])
    menu.handle_callback(7, 10, "target", _button(edits[-1][3], "2")["callback_data"])
    assert control.mapping.records["fast"] == ("model-02", "r3")
    assert control.mapping.update_calls == [("quick", "fast", "model-01", "r1"), ("fast", "fast", "model-02", "r2")]
    assert control.mapping.put_calls == control.mapping.delete_calls == []
    menu.handle_callback(7, 10, "old-picker", old_picker)
    assert len(control.mapping.update_calls) == 2


def test_alias_detail_edit_cancel_returns_b_then_page_a_and_old_button_expires(env):
    control, edits, answers, sends = env
    control.mapping.records.update({
        f"alias-{index:02d}": (f"model-{index:02d}", "r1")
        for index in range(1, 11)
    })
    state = menu._session(7)
    state.tab = "alias"
    state.alias_page = 2
    list_text, list_kb = menu.render(7)
    assert "模型别名 · 11 条" in list_text and "9. " in list_text
    item_callback = _button(list_kb, "9")["callback_data"]

    menu.handle_callback(7, 10, "open", item_callback)
    draft_kb = edits[-1][3]
    menu.handle_callback(
        7, 10, "edit", _button(draft_kb, "编辑别名名称")["callback_data"],
    )
    assert states.get_state(7)["action"] == "mc_alias_name"
    assert menu.handle_text_state(7, "mc_alias_name", "/cancel") is True
    menu.handle_callback(
        7, 10, "cancel", _button(sends[-1][2], "返回")["callback_data"],
    )
    draft_kb = edits[-1][3]
    menu.handle_callback(
        7, 10, "back", _button(draft_kb, "返回别名列表")["callback_data"],
    )
    assert "模型别名 · 11 条" in edits[-1][2] and "9. " in edits[-1][2]
    assert state.tab == "alias" and state.alias_page == 2

    menu.handle_callback(7, 10, "switch", "mc:tab:video")
    menu.handle_callback(7, 10, "old", item_callback)
    assert menu._session(7).tab == "video"
    assert answers[-1][1] == "别名列表已过期，请重新打开"
    assert control.mapping.update_calls == []


def test_metadata_detail_shows_zero_false_sources_and_patch_is_sparse(env):
    control, edits, _answers, _sends = env
    session = menu._session(7)
    session.source = ModelSourceRef(ModelSourceType.OAUTH, "acct-a")
    text, _kb = menu._detail_render(7, "rk-1")
    assert "📐 <b>容量</b>" in text
    assert "上下文：<code>300,000 tokens</code>" in text
    assert "图片输入：<code>不支持</code>" in text
    assert "输入价格：<code>$0</code>" in text
    assert "sourceOverride" not in text
    assert "catalog" not in text and "constrainedBy" not in text
    assert "服务档位" not in text
    assert "知识截止" not in text and "最大输入" not in text
    assert "长上下文输入价格" not in text
    assert "容器" not in text
    assert "账户：<code>启用</code>" in text
    action_cb = menu._freeze(
        7,
        "field_bool",
        resource_key="rk-1",
        source=session.source,
        outbound_model="upstream-01",
        field="vision",
        revision="r1",
        group="capability",
        value=False,
    )
    menu.handle_callback(7, 10, "patch", action_cb)
    model_id, kwargs = control.mapping.patch_calls[-1]
    assert model_id == "model-01"
    assert kwargs["scope"] == "oauth" and kwargs["account_id"] == "acct-a"
    assert kwargs["channel_id"] is None and kwargs["outbound_model"] == "upstream-01"
    assert kwargs["patch"].set_fields == {"vision": False}
    assert kwargs["patch"].unset_fields == ()


@pytest.mark.parametrize("target_tab", ["alias", "image", "video"])
def test_type_switch_clears_source_status_labels_and_frozen_selection(env, target_tab):
    _control, _edits, _answers, _sends = env
    state = menu._session(7)
    state.source = ModelSourceRef(ModelSourceType.OAUTH, "acct-a")
    state.source_label = "OpenAI · OpenAI A"
    state.source_provider = "openai"
    state.status = next(item for item in ModelStatus if item.value == "disabled")
    state.multiple = True
    state.selected = ["model-01"]
    state.selected_resources = {"model-01": "rk-1"}
    state.selection_filters = ModelFilters(kinds=(ModelKind.CHAT,), source=state.source)
    state.excluded = ["model-02"]

    menu.handle_callback(7, 10, "tab", f"mc:tab:{target_tab}")
    assert state.tab == target_tab
    assert state.source is None and state.source_label == "" and state.source_provider == ""
    assert state.status is None and state.multiple is False
    assert state.selected == [] and state.selected_resources == {}
    assert state.selection_filters is None and state.excluded == []


def test_source_picker_binds_exact_ids_across_provider_and_account_reordering(env):
    control, edits, answers, _sends = env

    def source_for(model_id, *, source_id, provider, label, source_type=None):
        original = control.views[model_id].sources[0]
        return replace(
            original, type=source_type or original.type,
            id=source_id, provider=provider, label=label,
            outbound_model=f"{provider}-wire-{model_id}",
            source_enabled=True, effective_routable=True,
        )

    control.views["model-01"] = replace(
        control.views["model-01"],
        sources=(source_for(
            "model-01", source_id="cursor:opaque-a", provider="cursor",
            label="cursor:opaque-a",
        ),),
    )
    control.views["model-02"] = replace(
        control.views["model-02"],
        sources=(source_for(
            "model-02", source_id="cursor:opaque-b", provider="cursor",
            label="cursor:opaque-b",
        ),),
    )
    for index in range(3, 10):
        model_id = f"model-{index:02d}"
        control.views[model_id] = replace(
            control.views[model_id],
            sources=(source_for(
                model_id, source_id="xai:opaque-x", provider="xai",
                label="xai:opaque-x",
            ),),
        )
    control.views["model-10"] = replace(
        control.views["model-10"],
        sources=(source_for(
            "model-10", source_id="channel-public-a", provider="anthropic",
            label="channel-public-a", source_type=ModelSourceType.API,
        ),),
    )

    menu.handle_callback(7, 10, "picker", "mc:source")
    picker = edits[-1][3]
    cursor_b = _button(picker, "Cursor · Team · cursor-b@example.test")["callback_data"]
    assert all("opaque" not in item["text"] for item in _buttons(picker))

    # Reordering a same-provider account list cannot retarget a frozen ID.
    control.oauth.accounts.reverse()
    menu.handle_callback(7, 10, "cursor-b", cursor_b)
    state = menu._session(7)
    assert state.source == ModelSourceRef(ModelSourceType.OAUTH, "cursor:opaque-b")
    assert state.source_label == "Cursor · Team · cursor-b@example.test"
    assert control.query_calls[-1][0].source == state.source
    assert "模型中心 · 对话 · 1 个" in edits[-1][2]
    assert "model-02" in edits[-1][2] and "xai:opaque-x" not in edits[-1][2]
    assert "Cursor · Team · cursor-b@example.test" in edits[-1][2]
    filtered_kb = edits[-1][3]
    menu.handle_callback(7, 10, "detail", _button(filtered_kb, "1")["callback_data"])
    assert "Cursor · Team · cursor-b@example.test" in edits[-1][2]
    assert "cursor:opaque-b" not in edits[-1][2]
    menu.handle_callback(
        7, 10, "detail-back", _button(edits[-1][3], "返回模型列表")["callback_data"],
    )

    # A source callback from the old type cannot pollute a newly selected type.
    menu.handle_callback(7, 10, "image", "mc:tab:image")
    assert state.source is None and state.source_label == "" and state.source_provider == ""
    menu.handle_callback(7, 10, "stale", cursor_b)
    assert state.tab == "image" and state.source is None
    assert answers[-1][1] == "来源选择页已过期，请重新打开"

    # The public API-channel entry also resolves by exact canonical ID.
    menu.handle_callback(7, 10, "chat", "mc:tab:chat")
    public = menu.source_callback(
        7, source_type="api", source_id="channel-public-a", origin="ch:view:3",
    )
    menu.handle_callback(7, 10, "api", public)
    assert state.source == ModelSourceRef(ModelSourceType.API, "channel-public-a")
    assert state.source_label == "Claude · Public A"
    assert control.query_calls[-1][0].source == state.source
    public_text, public_kb = edits[-1][2], edits[-1][3]
    anthropic_icon = ui.provider_custom_emoji_id("anthropic")
    assert anthropic_icon in public_text
    source_button = _button(public_kb, "来源：Claude · Public A")
    assert source_button["icon_custom_emoji_id"] == anthropic_icon
    menu.handle_callback(
        7, 10, "api-detail", _button(public_kb, "1")["callback_data"],
    )
    assert anthropic_icon in edits[-1][2]
    assert "Claude · Public A" in edits[-1][2]


def test_metadata_single_sync_uses_frozen_source_and_control_operation(env):
    control, edits, _answers, _sends = env
    source = ModelSourceRef(ModelSourceType.OAUTH, "acct-a")
    menu._session(7).source = source
    _text, keyboard = menu._detail_render(7, "rk-1")
    callback = _button(keyboard, "同步元数据")["callback_data"]
    menu.handle_callback(7, 10, "sync", callback)
    call = control.mapping.sync_calls[-1]
    assert call["mode"] is _SyncMode.ONE
    assert call["targets"] == (_SyncTarget("model-01", source),)
    assert call["source"] is None
    assert call["refresh_catalog"] is True
    assert call["expected_revision"] == "r1"
    assert "meta-op-1" in edits[-1][2]


def test_input_cancel_stale_generation_and_direct_admin_gate(env, monkeypatch):
    control, _edits, answers, sends = env
    menu.handle_callback(7, 10, "query", "mc:query")
    assert states.get_state(7)["action"] == "mc_query"
    assert menu.handle_text_state(7, "mc_query", "/cancel") is True
    assert menu._session(7).text == ""
    assert states.get_state(7) is None
    assert any("已取消" in item[1] for item in sends)

    monkeypatch.setattr(ui, "is_admin", lambda _chat_id: False)
    before = len(control.calls)
    menu.handle_callback(9, 10, "denied", menu._freeze(9, "set_state", scope=None))
    assert len(control.calls) == before
    assert answers[-1] == ("denied", "⛔ 无权限", True)
