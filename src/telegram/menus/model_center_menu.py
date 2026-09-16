"""Unified Telegram model center.

This module is a transport adapter only.  Persistent state, revisions, metadata
resolution and prices are owned by ``ModelCenterControl`` and its specialist
controls.  Every callback/input re-checks the Telegram administrator allow-list
and binds an explicit management actor for the action.
"""

from __future__ import annotations

import json
import math
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Optional

from ...management_control import ManagementError
from ...management_control.models import (
    ModelCenterControl,
    ModelFilters,
    ModelKind,
    ModelOwnerRef,
    ModelSelection,
    ModelSelectionMode,
    ModelSourceRef,
    ModelSourceType,
    ModelStateField,
    ModelStateTarget,
    ModelStatus,
    ModelView,
)
from ...management_control.oauth import PageSpec
from .. import states, ui
from .model_center_html import paged as _paged, page_render as _html_page_render


# Production replaces this fallback with the lifecycle-owned ``controls.models``.
_CONTROL: ModelCenterControl = ModelCenterControl()

_PAGE_SIZE = 8
_ACTION_LIMIT = 4096
_ACTION_TTL = 1800
_INPUT_PREFIX = "mc_"
_CANCEL_WORDS = {"/cancel", "cancel", "取消"}


@dataclass
class _Session:
    tab: str = "chat"
    page: int = 1
    text: str = ""
    source: ModelSourceRef | None = None
    source_label: str = ""
    source_provider: str = ""
    status: ModelStatus | None = None
    origin: str = "menu:main"
    multiple: bool = False
    selection_mode: ModelSelectionMode = ModelSelectionMode.IDS
    selected: list[str] = field(default_factory=list)
    selected_resources: dict[str, str] = field(default_factory=dict)
    selection_filters: ModelFilters | None = None
    excluded: list[str] = field(default_factory=list)
    alias_query: str = ""
    alias_page: int = 1
    detail_key: str | None = None
    metadata_group: str = "capacity"
    generation: str = ""


@dataclass
class _AliasDraft:
    draft_id: str
    old_alias: str | None
    alias: str
    real_model: str
    revision: str
    target_page: int = 1


@dataclass(frozen=True)
class _FrozenAction:
    chat_id: int
    name: str
    data: Mapping[str, Any]
    created: float


@dataclass(frozen=True)
class _SourceOption:
    ref: ModelSourceRef
    label: str
    provider: str


@dataclass(frozen=True)
class _ListContext:
    tab: str
    page: int
    text: str
    source: ModelSourceRef | None
    source_label: str
    source_provider: str
    status: ModelStatus | None
    origin: str
    alias_page: int
    alias_query: str
    multiple: bool
    selection_mode: ModelSelectionMode
    selected: tuple[str, ...]
    selected_resources: tuple[tuple[str, str], ...]
    selection_filters: ModelFilters | None
    excluded: tuple[str, ...]


_sessions: dict[int, _Session] = {}
_alias_drafts: dict[int, _AliasDraft] = {}
_actions: "OrderedDict[str, _FrozenAction]" = OrderedDict()
_lock = threading.RLock()


@dataclass(frozen=True)
class _MetaField:
    key: str
    label: str
    group: str
    kind: str


# Canonical metadata keys are the keys returned by the shared resolver.  The TG
# adapter only parses user syntax; hard limits and cross-field rules stay in the
# mapping control.
_META_FIELDS = (
    _MetaField("contextWindow", "上下文", "capacity", "tokens"),
    _MetaField("maxInputTokens", "最大输入", "capacity", "tokens"),
    _MetaField("maxOutputTokens", "最大输出", "capacity", "tokens"),
    _MetaField("compactTriggerTokens", "压缩阈值", "capacity", "tokens"),
    _MetaField("vision", "图片输入", "capability", "bool"),
    _MetaField("toolCall", "工具调用", "capability", "bool"),
    _MetaField("structuredOutput", "结构化输出", "capability", "bool"),
    _MetaField("reasoningEfforts", "思考档位", "capability", "list"),
    _MetaField("serviceTiers", "服务档位", "capability", "list"),
    _MetaField("knowledgeCutoff", "知识截止", "capability", "date"),
    _MetaField("inputPricePer1M", "输入价格", "price", "price"),
    _MetaField("outputPricePer1M", "输出价格", "price", "price"),
    _MetaField("cacheReadPricePer1M", "缓存读取价格", "price", "price"),
    _MetaField("cacheWritePricePer1M", "缓存写入价格", "price", "price"),
    _MetaField("longContextInputPricePer1M", "长上下文输入价格", "price", "price"),
    _MetaField("longContextOutputPricePer1M", "长上下文输出价格", "price", "price"),
)
_META_BY_KEY = {item.key: item for item in _META_FIELDS}


def _session(chat_id: int) -> _Session:
    with _lock:
        return _sessions.setdefault(int(chat_id), _Session())


def reset_for_tests() -> None:
    """Clear adapter-only state.  No business state is stored here."""
    with _lock:
        _sessions.clear()
        _alias_drafts.clear()
        _actions.clear()


def _admin(chat_id: int, cb_id: str | None = None) -> bool:
    if ui.is_admin(chat_id):
        return True
    if cb_id is None:
        ui.send(chat_id, "⛔ 无权限。")
    else:
        ui.answer_cb(cb_id, "⛔ 无权限", show_alert=True)
    return False


def _ctx(chat_id: int):
    # The allow-list check is intentionally separate from the management actor.
    if not ui.is_admin(chat_id):
        raise PermissionError("telegram administrator required")
    return _CONTROL.bind_telegram_actor(chat_id)


def _clear_input(chat_id: int) -> None:
    current = states.get_state(chat_id)
    if current and str(current.get("action") or "").startswith(_INPUT_PREFIX):
        states.pop_state(chat_id)
    _session(chat_id).generation = ""


def abandon_edit(chat_id: int) -> None:
    """Leave this adapter's editor, without touching another menu's input."""
    _clear_input(chat_id)
    _alias_drafts.pop(chat_id, None)


def before_callback(chat_id: int, data: str) -> None:
    """Called by the real dispatcher before any early navigation return."""
    _clear_input(chat_id)
    draft = _alias_drafts.get(chat_id)
    if draft is None or data == "mc:noop":
        return
    action = _thaw(chat_id, data.split(":", 2)[2]) if data.startswith("mc:a:") else None
    if action and action.data.get("draft_id") == draft.draft_id:
        return
    _alias_drafts.pop(chat_id, None)


def before_command(chat_id: int, text: str) -> None:
    if text.startswith("/") and text.split()[0].split("@")[0] != "/cancel":
        abandon_edit(chat_id)


def _freeze(chat_id: int, name: str, **data: Any) -> str:
    now = time.time()
    with _lock:
        expired = [key for key, value in _actions.items() if now - value.created > _ACTION_TTL]
        for key in expired:
            _actions.pop(key, None)
        while len(_actions) >= _ACTION_LIMIT:
            _actions.popitem(last=False)
        token = secrets.token_hex(5)
        while token in _actions:
            token = secrets.token_hex(5)
        _actions[token] = _FrozenAction(int(chat_id), name, dict(data), now)
    return f"mc:a:{token}"


def _thaw(chat_id: int, token: str) -> _FrozenAction | None:
    with _lock:
        action = _actions.get(token)
        if action is None or action.chat_id != int(chat_id):
            return None
        if time.time() - action.created > _ACTION_TTL:
            _actions.pop(token, None)
            return None
        return action


def _mapping_symbol(name: str):
    from ...management_control import mapping as mapping_package
    value = getattr(mapping_package, name, None)
    if value is None:
        raise ManagementError("SERVICE_NOT_READY")
    return value


def _prompt(chat_id: int, action: str, data: dict[str, Any], text: str, cancel_callback: str = "mc:list") -> None:
    generation = secrets.token_hex(8)
    s = _session(chat_id)
    s.generation = generation
    payload = dict(data)
    payload["generation"] = generation
    payload["cancel_callback"] = cancel_callback
    states.set_state(chat_id, action, payload)
    rows = [[ui.btn("❌ 取消", cancel_callback)]]
    if action == "mc_metadata_field":
        rows.insert(0, [ui.btn("恢复继承", _freeze(chat_id, "field_inherit", **data))])
    ui.send(chat_id, text, reply_markup=ui.inline_kb(rows))


def render(chat_id: int) -> tuple[str, dict]:
    s = _session(chat_id)
    return _alias_render(chat_id) if s.tab == "alias" else _model_list_render(chat_id)


def show(chat_id: int, message_id: int, cb_id: str | None = None) -> None:
    if not _admin(chat_id, cb_id):
        return
    try:
        text, kb = render(chat_id)
    except ManagementError as exc:
        if cb_id:
            _answer_error(cb_id, exc)
        else:
            ui.send(chat_id, "❌ " + ui.escape_html(_error_text(exc)))
        return
    if cb_id is not None:
        ui.answer_cb(cb_id)
    ui.edit(chat_id, message_id, text, reply_markup=kb)


def source_callback(
    chat_id: int, *, source_type: ModelSourceType | str, source_id: str,
    origin: str,
) -> str:
    del chat_id
    payload = json.dumps(
        {"type": _enum_value(source_type), "id": str(source_id), "origin": str(origin)},
        ensure_ascii=False, separators=(",", ":"),
    )
    return "mc:src:" + ui.register_code("mc-source:" + payload)


def open_source(
    chat_id: int, message_id: int, cb_id: str,
    *, source_type: ModelSourceType | str, source_id: str, origin: str,
) -> None:
    if not _admin(chat_id, cb_id):
        return
    source = ModelSourceRef(ModelSourceType(_enum_value(source_type)), str(source_id))
    option = _find_source_option(chat_id, source)
    if option is None:
        ui.answer_cb(cb_id, "来源已变化，请从当前来源列表重新选择", show_alert=True)
        return
    s = _session(chat_id)
    _clear_selection(s)
    s.tab = "chat"
    s.page = 1
    s.text = ""
    s.status = None
    s.source = source
    s.source_label = option.label
    s.source_provider = option.provider
    s.origin = origin
    _show_rendered(chat_id, message_id, cb_id, lambda: render(chat_id))


def send_new(chat_id: int, *, origin: str = "menu:main", source: ModelSourceRef | None = None) -> None:
    if not _admin(chat_id):
        return
    s = _session(chat_id)
    s.origin = origin
    if source is not None:
        option = _find_source_option(chat_id, source)
        if option is None:
            ui.send(chat_id, "❌ 来源已变化，请从当前来源列表重新选择。")
            return
        if not _source_equal(s.source, source):
            _clear_selection(s)
        s.source = source
        s.source_label = option.label
        s.source_provider = option.provider
    try:
        text, kb = render(chat_id)
    except ManagementError as exc:
        ui.send(chat_id, "❌ " + ui.escape_html(_error_text(exc)))
        return
    ui.send(chat_id, text, reply_markup=kb)


def _show_rendered(chat_id: int, message_id: int, cb_id: str, renderer) -> None:
    try:
        text, kb = renderer()
    except ManagementError as exc:
        _answer_error(cb_id, exc)
        return
    ui.answer_cb(cb_id)
    ui.edit(chat_id, message_id, text, reply_markup=kb)


def _clear_selection(s: _Session) -> None:
    s.multiple = False
    s.selection_mode = ModelSelectionMode.IDS
    s.selected.clear()
    s.selected_resources.clear()
    s.selection_filters = None
    s.excluded.clear()


def _toggle_selected(s: _Session, model_id: str, resource_key: str) -> None:
    s.selected_resources[model_id] = resource_key
    if s.selection_mode is ModelSelectionMode.FILTER:
        if model_id in s.excluded:
            s.excluded.remove(model_id)
        else:
            s.excluded.append(model_id)
        return
    if model_id in s.selected:
        s.selected.remove(model_id)
    else:
        s.selected.append(model_id)


def _handle_set_state(chat_id: int, message_id: int, cb_id: str, data: Mapping[str, Any]) -> None:
    try:
        _CONTROL.set_state(
            _ctx(chat_id),
            scope=data.get("scope"),
            selection=data["selection"],
            target=data["target"],
            expected_revision=data.get("revision"),
        )
    except ManagementError as exc:
        _answer_error(cb_id, exc)
        detail_key = data.get("detail_key")
        renderer = (
            lambda: _detail_render(
                chat_id, detail_key,
                back_callback=str(data.get("detail_back") or "mc:list"),
            )
        ) if detail_key else (lambda: render(chat_id))
        try:
            text, kb = renderer()
        except ManagementError:
            return
        ui.edit(chat_id, message_id, text, reply_markup=kb)
        return
    target = data["target"]
    field = _enum_value(target.field)
    label = "已启用" if target.value else "已停用"
    if field == "visible":
        label = "下游展示开关已开启" if target.value else "下游展示开关已关闭"
    ui.answer_cb(cb_id, label)
    detail_key = data.get("detail_key")
    text, kb = _detail_render(
        chat_id, detail_key,
        back_callback=str(data.get("detail_back") or "mc:list"),
    ) if detail_key else render(chat_id)
    ui.edit(chat_id, message_id, text, reply_markup=kb)


def _handle_frozen(chat_id: int, message_id: int, cb_id: str, action: _FrozenAction) -> None:
    name, data = action.name, action.data
    s = _session(chat_id)
    if _aliases_actions.handle_action(chat_id, message_id, cb_id, action):
        return
    if _metadata_actions.handle_action(chat_id, message_id, cb_id, action):
        return
    if _settings_actions.handle_action(chat_id, message_id, cb_id, action):
        return
    if _media_actions.handle_action(chat_id, message_id, cb_id, action):
        return
    if name == "content_page":
        _show_rendered(chat_id, message_id, cb_id, lambda: _html_page_render(
            chat_id, data["pages"], data["keyboard"], data["page"],
            draft_id=data.get("draft_id"),
        ))
        return


    if name == "open_source":
        source = data["source"]
        option = _find_source_option(chat_id, source)
        if option is None:
            ui.answer_cb(cb_id, "来源已变化，请重新选择", show_alert=True)
            return
        _clear_selection(s)
        s.tab = "chat"
        s.page = 1
        s.text = ""
        s.status = None
        s.source = source
        s.source_label = option.label
        s.source_provider = option.provider
        s.origin = str(data["origin"])
        _show_rendered(chat_id, message_id, cb_id, lambda: render(chat_id))
        return


    if name == "restore_list":
        context = data.get("context")
        if not isinstance(context, _ListContext):
            ui.answer_cb(cb_id, "返回位置已过期", show_alert=True)
            return
        _restore_list_context(s, context)
        _show_rendered(chat_id, message_id, cb_id, lambda: render(chat_id))
        return


    if name == "detail":
        context = data.get("list_context")
        if isinstance(context, _ListContext):
            _restore_list_context(s, context)
        back_callback = str(data.get("back_callback") or _list_back_callback(
            chat_id, context if isinstance(context, _ListContext) else None,
        ))
        _show_rendered(chat_id, message_id, cb_id, lambda: _detail_render(
            chat_id, data["resource_key"], back_callback=back_callback,
        ))
        return


    if name == "select_model":
        _toggle_selected(s, str(data["model_id"]), str(data["resource_key"]))
        show(chat_id, message_id, cb_id)
        return


    if name == "set_source":
        if data.get("expected_tab") != s.tab:
            ui.answer_cb(cb_id, "来源选择页已过期，请重新打开", show_alert=True)
            return
        source = data.get("source")
        option = _find_source_option(chat_id, source) if source is not None else None
        if source is not None and option is None:
            ui.answer_cb(cb_id, "来源已变化，请重新选择", show_alert=True)
            return
        if not _source_equal(source, s.source):
            _clear_selection(s)
        s.source = source
        s.source_label = option.label if option is not None else ""
        s.source_provider = option.provider if option is not None else ""
        s.page = 1
        show(chat_id, message_id, cb_id)
        return


    if name == "set_status":
        s.status = data.get("status")
        s.page = 1
        show(chat_id, message_id, cb_id)
        return


    if name == "set_state":
        _handle_set_state(chat_id, message_id, cb_id, data)
        return


    if name == "sync_source":
        try:
            op = _CONTROL.sync_source_models(_ctx(chat_id), data["source"])
        except ManagementError as exc:
            _answer_error(cb_id, exc)
            return
        ui.answer_cb(cb_id, "同步任务已开始")
        ui.edit(
            chat_id, message_id,
            f"🔄 <b>同步任务已开始</b>\n\n任务：<code>{ui.escape_html(op.id)}</code>\n原有模型和停用状态在失败时保持。",
            reply_markup=_operation_keyboard(chat_id, op.id, "mc:list"),
        )
        return


    if name == "clear_error":
        try:
            _CONTROL.clear_model_errors(_ctx(chat_id), source=data["source"], model_id=data["model_id"])
        except ManagementError as exc:
            _answer_error(cb_id, exc)
            return
        ui.answer_cb(cb_id, "模型故障已清除")
        text, kb = _detail_render(
            chat_id, data["detail_key"],
            back_callback=str(data.get("detail_back") or "mc:list"),
        )
        ui.edit(chat_id, message_id, text, reply_markup=kb)
        return


    if name == "max_context":
        try:
            _CONTROL.oauth.update_model_settings(
                _ctx(chat_id),
                data["account_id"],
                model_id=data["model_id"],
                max_context_default=bool(data["target"]),
                expected_revision=data["revision"],
            )
        except ManagementError as exc:
            _answer_error(cb_id, exc)
            return
        ui.answer_cb(cb_id, "Max Context 已开启" if data["target"] else "Max Context 已关闭")
        text, kb = _detail_render(
            chat_id, data["detail_key"],
            back_callback=str(data.get("detail_back") or "mc:list"),
        )
        ui.edit(chat_id, message_id, text, reply_markup=kb)
        return


    ui.answer_cb(cb_id, "页面动作已过期", show_alert=True)


def handle_callback(chat_id: int, message_id: int, cb_id: str, data: str) -> bool:
    if data.startswith("oam:"):
        from . import oauth_account_models_menu
        return oauth_account_models_menu.redirect_legacy_callback(
            chat_id, message_id, cb_id, data,
        )
    if data.startswith("oa:cursor_"):
        from . import oauth_menu
        return oauth_menu._redirect_legacy_cursor_callback(
            chat_id, message_id, cb_id, data,
        )
    legacy_renderer = None
    legacy_alias = False
    if data == "menu:images" or data in {
        "img:show", "img:toggle", "img:cache_toggle", "img:set_path",
        "img:set_retention", "img:set_max",
    }:
        legacy_renderer = lambda: _image_settings_render(chat_id)
    elif data in {"img:set_main", "img:set_tool", "img:accounts"} or data.startswith("img:acc_toggle:"):
        legacy_renderer = lambda: _gpt_images_render(chat_id)
    elif data == "xim:show":
        legacy_renderer = lambda: _settings_render(chat_id)
    elif data == "xim:edit:image":
        legacy_renderer = lambda: _media_manager_render(chat_id, "xai", "image", 1)
    elif data == "xim:edit:video":
        legacy_renderer = lambda: _media_manager_render(chat_id, "xai", "video", 1)
    elif data in {"xim:edit:ttl", "xim:edit:timeout"}:
        legacy_renderer = lambda: _video_settings_render(chat_id)
    elif data.startswith("map:"):
        action = data.split(":", 2)[1] if ":" in data else ""
        if action.startswith("compact"):
            legacy_renderer = lambda: _compression_render(chat_id)
        elif action.startswith("meta"):
            legacy_renderer = lambda: _metadata_sync_render(chat_id)
        elif action in {"set_default", "clear_default", "page_default", "pick_default"}:
            legacy_renderer = lambda: _settings_render(chat_id)
        else:
            legacy_alias = True
    if legacy_renderer is not None or legacy_alias:
        if not _admin(chat_id, cb_id):
            return True
        abandon_edit(chat_id)
        if legacy_alias:
            s = _session(chat_id)
            _clear_selection(s)
            s.tab = "alias"
            s.source = None
            s.source_label = ""
            s.source_provider = ""
            s.status = None
            s.alias_page = 1
            _show_rendered(chat_id, message_id, cb_id, lambda: _alias_render(chat_id))
        else:
            _show_rendered(chat_id, message_id, cb_id, legacy_renderer)
        return True
    if not data.startswith("mc:"):
        return False
    if not _admin(chat_id, cb_id):
        return True
    before_callback(chat_id, data)
    s = _session(chat_id)
    try:
        if data in {"mc:show", "mc:list"}:
            show(chat_id, message_id, cb_id)
            return True
        if data.startswith("mc:src:"):
            raw = ui.resolve_code(data.split(":", 2)[2]) or ""
            if not raw.startswith("mc-source:"):
                ui.answer_cb(cb_id, "来源入口已过期", show_alert=True)
                return True
            payload = json.loads(raw[len("mc-source:"):])
            open_source(
                chat_id, message_id, cb_id,
                source_type=payload["type"], source_id=payload["id"],
                origin=payload["origin"],
            )
            return True
        if data == "mc:noop":
            ui.answer_cb(cb_id, "当前页")
            return True
        if data == "mc:noop_empty":
            ui.answer_cb(cb_id, "请先勾选模型", show_alert=True)
            return True
        if data.startswith("mc:tab:"):
            tab = data.rsplit(":", 1)[1]
            if tab not in {"chat", "alias", "image", "video"}:
                ui.answer_cb(cb_id, "未知类型", show_alert=True)
                return True
            if tab != s.tab:
                _clear_selection(s)
                _alias_drafts.pop(chat_id, None)
                s.source = None
                s.source_label = ""
                s.source_provider = ""
                s.status = None
            s.tab = tab
            s.page = 1
            show(chat_id, message_id, cb_id)
            return True
        if data.startswith("mc:page:"):
            s.page = max(1, int(data.rsplit(":", 1)[1]))
            show(chat_id, message_id, cb_id)
            return True
        if data.startswith("mc:alias_page:"):
            s.alias_page = max(1, int(data.rsplit(":", 1)[1]))
            show(chat_id, message_id, cb_id)
            return True
        if data == "mc:query":
            _prompt(chat_id, "mc_query", {}, "发送模型ID、别名或上游名关键词。发送 - 清空。", "mc:list")
            ui.answer_cb(cb_id)
            return True
        if data == "mc:alias_query":
            _prompt(chat_id, "mc_alias_query", {}, "发送别名或真实模型关键词。发送 - 清空。", "mc:aliases")
            ui.answer_cb(cb_id)
            return True
        if data == "mc:source":
            _show_rendered(chat_id, message_id, cb_id, lambda: _source_picker_render(chat_id))
            return True
        if data == "mc:status":
            _show_rendered(chat_id, message_id, cb_id, lambda: _status_picker_render(chat_id))
            return True
        if data == "mc:multi":
            if s.tab != "chat":
                ui.answer_cb(cb_id, "只有对话模型支持批量状态操作", show_alert=True)
                return True
            if s.multiple:
                _clear_selection(s)
            else:
                s.multiple = True
            show(chat_id, message_id, cb_id)
            return True
        if data == "mc:select_all":
            s.multiple = True
            s.selection_mode = ModelSelectionMode.FILTER
            s.selection_filters = _filters(s)
            s.selected.clear()
            s.selected_resources.clear()
            s.excluded.clear()
            show(chat_id, message_id, cb_id)
            return True
        if data == "mc:invert":
            if s.selection_mode is ModelSelectionMode.FILTER:
                s.selection_mode = ModelSelectionMode.IDS
                s.selected = list(s.excluded)
                s.selection_filters = None
                s.excluded.clear()
            else:
                s.selection_mode = ModelSelectionMode.FILTER
                s.selection_filters = _filters(s)
                s.excluded = list(s.selected)
                s.selected.clear()
            show(chat_id, message_id, cb_id)
            return True
        if data == "mc:clear_selection":
            s.selection_mode = ModelSelectionMode.IDS
            s.selected.clear()
            s.selected_resources.clear()
            s.selection_filters = None
            s.excluded.clear()
            show(chat_id, message_id, cb_id)
            return True
        if data == "mc:done":
            _clear_selection(s)
            show(chat_id, message_id, cb_id)
            return True
        if data == "mc:aliases":
            _alias_drafts.pop(chat_id, None)
            if s.tab != "alias":
                s.source = None
                s.source_label = ""
                s.source_provider = ""
                s.status = None
            s.tab = "alias"
            _clear_selection(s)
            show(chat_id, message_id, cb_id)
            return True
        if data == "mc:settings":
            _show_rendered(chat_id, message_id, cb_id, lambda: _settings_render(chat_id))
            return True
        if data == "mc:compression":
            _show_rendered(chat_id, message_id, cb_id, lambda: _compression_render(chat_id))
            return True
        if data == "mc:metadata_sync":
            _show_rendered(chat_id, message_id, cb_id, lambda: _metadata_sync_render(chat_id))
            return True
        if data == "mc:image_settings":
            _show_rendered(chat_id, message_id, cb_id, lambda: _image_settings_render(chat_id))
            return True
        if data == "mc:video_settings":
            _show_rendered(chat_id, message_id, cb_id, lambda: _video_settings_render(chat_id))
            return True
        if data == "mc:gpt_images":
            _show_rendered(chat_id, message_id, cb_id, lambda: _gpt_images_render(chat_id))
            return True
        if data.startswith("mc:a:"):
            frozen = _thaw(chat_id, data.split(":", 2)[2])
            if frozen is None:
                ui.answer_cb(cb_id, "页面已过期，请刷新", show_alert=True)
                return True
            _handle_frozen(chat_id, message_id, cb_id, frozen)
            return True
    except (ValueError, TypeError):
        ui.answer_cb(cb_id, "页面参数已过期", show_alert=True)
        return True
    except ManagementError as exc:
        _answer_error(cb_id, exc)
        return True
    ui.answer_cb(cb_id, "未知模型中心操作")
    return True




# Explicit compatibility exports: runtime/tests patch this single facade.
from .model_center_catalog import (
    _enum_value,
    _error_code,
    _error_text,
    _answer_error,
    _filters,
    _source_equal,
    _source_for_view,
    _source_label,
    _status_label,
    _kind_label,
    _tabs,
    _page_row,
    _display_value,
    _fmt_meta,
    _metadata_lines,
    _model_state,
    _current_availability,
    _container_label,
    _list_context,
    _restore_list_context,
    _list_back_callback,
    _detail_callback,
    _provider_for_view,
    _selected_count,
    _selected_views,
    _selection_dto,
    _batch_value,
    _batch_button,
    _model_list_render,
    _detail_render,
    _max_context_row,
    _workbuddy_source_name,
    _source_options,
    _find_source_option,
    _source_picker_render,
    _status_picker_render,
)
from .model_center_aliases import (
    _alias_render,
    _alias_draft_render,
    _all_chat_views,
    _alias_target_render,
    _handle_alias_save,
)
from .model_center_metadata import (
    _metadata_scope_kwargs,
    _metadata_record,
    _sync_callback,
    _metadata_targets_render,
    _metadata_editor_render,
    _catalog_picker_render,
    _parse_field,
    _handle_metadata_patch,
)
from .model_center_settings import (
    _settings_render,
    _compression_render,
    _compression_picker_render,
    _operation_render,
    _operation_keyboard,
    _metadata_sync_render,
)
from .model_center_media import (
    _fmt_bytes,
    _ag_settings_optional,
    _image_settings_render,
    _gpt_images_render,
    _video_settings_render,
    _media_values,
    _media_limits,
    _media_manager_render,
    _media_view_detail_render,
    _media_detail_render,
    _media_method,
    _media_owner,
    _parse_media_model,
    _parse_media_models,
    _parse_duration_input,
    _parse_bytes_input,
)
from .model_center_inputs import (
    handle_text_state,
)
from . import model_center_aliases as _aliases_actions
from . import model_center_metadata as _metadata_actions
from . import model_center_settings as _settings_actions
from . import model_center_media as _media_actions
