"""Catalog filtering, stable selection, source labels and chat details.

The model_center_menu facade owns the one control binding and session state.
"""
from __future__ import annotations

import math
from typing import Any, Mapping

from ...management_control import ManagementError
from ...management_control.models import (
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
from .. import ui
from . import model_center_menu as menu


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


def _error_code(exc: BaseException) -> str:
    return menu._enum_value(getattr(exc, "code", ""))


def _error_text(exc: BaseException) -> str:
    code = menu._error_code(exc)
    if code in {"CONFIRMATION_REQUIRED", "REVISION_CONFLICT"}:
        return "页面版本已变化，请在刷新后的页面重试。"
    if code == "CAPABILITY_DENIED":
        return "权限不足。"
    if code == "RESOURCE_NOT_FOUND":
        return "资源已移除或页面已过期。"
    if code in {"RESOURCE_CONFLICT", "IDENTITY_CONFLICT", "STATE_CONFLICT"}:
        return "名称或状态发生冲突，请修改后重试。"
    if code == "OPERATION_ALREADY_RUNNING":
        return "已有同类任务正在运行。"
    if code == "DEPENDENCY_UNAVAILABLE" and any(
        getattr(item, "path", None) == "runtime"
        and getattr(item, "code", None) == "SAVED_RELOAD_UNCONFIRMED"
        for item in (getattr(exc, "fields", ()) or ())
    ):
        return "配置已保存，运行时重载未确认；请刷新状态，勿重放旧操作。"
    if code in {"DEPENDENCY_UNAVAILABLE", "SERVICE_NOT_READY"}:
        return "服务暂未就绪，请稍后重试。"
    if code in {"VALIDATION_FAILED", "UNSUPPORTED_VALUE"}:
        fields = tuple(getattr(exc, "fields", ()) or ())
        if fields:
            rendered = []
            for item in fields[:4]:
                path = str(getattr(item, "path", "") or "输入")
                message = str(getattr(item, "message", "") or getattr(item, "code", "") or "无效")
                rendered.append(f"{path}: {message}")
            return "；".join(rendered)
        return "输入或操作不受支持。"
    return "操作失败，请稍后重试。"


def _answer_error(cb_id: str, exc: BaseException) -> None:
    ui.answer_cb(cb_id, menu._error_text(exc), show_alert=True)


def _filters(s: menu._Session) -> ModelFilters:
    kind = {
        "chat": ModelKind.CHAT,
        "image": ModelKind.IMAGE,
        "video": ModelKind.VIDEO,
    }.get(s.tab, ModelKind.CHAT)
    return ModelFilters(
        kinds=(kind,),
        text=s.text or None,
        source=s.source,
        statuses=((s.status,) if s.status is not None else ()),
    )


def _source_equal(left: ModelSourceRef | None, right: ModelSourceRef | None) -> bool:
    if left is None or right is None:
        return left is right
    return left.type is right.type and left.id == right.id


def _source_for_view(view: ModelView, source: ModelSourceRef | None):
    if source is None:
        return None
    return next(
        (item for item in view.sources if item.type is source.type and item.id == source.id),
        None,
    )


def _source_label(
    chat_id: int, source: ModelSourceRef | None, view: ModelView | None = None,
) -> str:
    del view
    if source is None:
        return "全部来源"
    session = menu._session(chat_id)
    if menu._source_equal(session.source, source) and session.source_label:
        return session.source_label
    option = menu._find_source_option(chat_id, source)
    if option is not None:
        return option.label
    # Old callbacks may outlive a deleted source. Never expose its opaque ID or
    # guess another account by label, provider, or list position.
    return "已移除的账户" if source.type is ModelSourceType.OAUTH else "已移除的渠道"


def _status_label(status: ModelStatus | None) -> str:
    return {
        None: "全部状态",
        ModelStatus.ENABLED: "启用",
        ModelStatus.DISABLED: "停用",
        ModelStatus.HIDDEN: "下游展示开关：关",
        ModelStatus.VISIBLE: "下游展示开关：开",
    }[status]


def _kind_label(tab: str) -> str:
    return {"chat": "对话", "alias": "别名", "image": "图片", "video": "视频"}.get(tab, tab)


def _tabs(active: str) -> list[dict]:
    def label(name: str, text: str) -> str:
        return ("✓ " if active == name else "") + text
    return [
        ui.btn(label("chat", "对话"), "mc:tab:chat"),
        ui.btn(label("alias", "别名"), "mc:tab:alias"),
        ui.btn(label("image", "图片"), "mc:tab:image"),
        ui.btn(label("video", "视频"), "mc:tab:video"),
    ]


def _page_row(chat_id: int, page: int, pages: int, *, alias: bool = False) -> list[dict]:
    if pages <= 1:
        return []
    prefix = "mc:alias_page" if alias else "mc:page"
    return [
        ui.btn("◀ 上一页" if page > 1 else "◁ 上一页", f"{prefix}:{page - 1}" if page > 1 else "mc:noop"),
        ui.btn(f"{page}/{pages}", "mc:noop"),
        ui.btn("下一页 ▶" if page < pages else "下一页 ▷", f"{prefix}:{page + 1}" if page < pages else "mc:noop"),
    ]


def _display_value(metadata: Mapping[str, Any], key: str) -> Any:
    if key in metadata:
        return metadata[key]
    # Older catalog snapshots keep prices under ``cost``.  This is display-only;
    # the adapter never derives prices or request budgets.
    cost = metadata.get("cost")
    if isinstance(cost, Mapping):
        nested = {
            "inputPricePer1M": ("input", "inputPrice"),
            "outputPricePer1M": ("output", "outputPrice"),
            "cacheReadPricePer1M": ("cacheRead", "cache_read"),
            "cacheWritePricePer1M": ("cacheWrite", "cache_write"),
            "longContextInputPricePer1M": ("longContextInput", "long_input"),
            "longContextOutputPricePer1M": ("longContextOutput", "long_output"),
        }.get(key, ())
        for candidate in nested:
            if candidate in cost:
                return cost[candidate]
    return None


def _fmt_meta(value: Any, item: menu._MetaField) -> str:
    if value is None:
        return "未提供"
    if item.kind == "bool":
        return "支持" if value is True else "不支持"
    if isinstance(value, (tuple, list)):
        return " / ".join(str(part) for part in value) if value else "无"
    if item.kind == "price":
        return f"${value}"
    if item.kind == "tokens":
        try:
            return f"{int(value):,} tokens"
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def _metadata_lines(
    metadata: Mapping[str, Any],
    *,
    compact: bool = False,
) -> list[str]:
    """普通详情只展示已知有效值；resolver溯源仍留在编辑层和API。"""
    fields = menu._META_FIELDS[:4] if compact else menu._META_FIELDS
    grouped: dict[str, list[str]] = {"capacity": [], "capability": [], "price": []}
    for item in fields:
        value = menu._display_value(metadata, item.key)
        # 未知/空目录字段不占行；明确 False 和数值 0 必须保留。
        if value is None or value == "" or value == () or value == []:
            continue
        grouped[item.group].append(
            f"{item.label}：<code>{ui.escape_html(menu._fmt_meta(value, item))}</code>"
        )
    titles = {
        "capacity": "📐 <b>容量</b>",
        "capability": "🧩 <b>能力</b>",
        "price": "💵 <b>价格（每百万 Tokens）</b>",
    }
    result: list[str] = []
    for group in ("capacity", "capability", "price"):
        if not grouped[group]:
            continue
        if result:
            result.append("")
        result.extend([titles[group], *grouped[group]])
    return result


def _model_state(view: ModelView, source: ModelSourceRef | None) -> tuple[bool | None, str]:
    if view.identity.kind is not ModelKind.CHAT:
        return None, "已配置"
    source_view = menu._source_for_view(view, source)
    if source is not None:
        if source_view is None:
            return False, "不在当前来源"
        enabled = bool(source_view.source_enabled)
        if not source_view.container_enabled:
            return enabled, "来源整体已停用"
        if not view.global_enabled:
            return enabled, "模型已全局停用"
        return enabled, "启用" if enabled else "此来源停用"
    return bool(view.global_enabled), "启用" if view.global_enabled else "停用"


def _list_model_state(view: ModelView, source: ModelSourceRef | None) -> tuple[bool | None, str]:
    """One concise effective state; raw switches remain on the detail page."""
    if view.identity.kind is not ModelKind.CHAT:
        return None, "已配置"
    if not view.global_enabled:
        return False, "停用"
    if view.available_in(source):
        return True, ""
    sources = tuple(
        row for row in view.sources
        if source is None or source.type is ModelSourceType.GLOBAL
        or (row.type is source.type and row.id == source.id)
    )
    if not sources:
        return False, "无来源"
    if all(not row.container_enabled for row in sources):
        kinds = {row.type for row in sources}
        noun = "账户" if kinds == {ModelSourceType.OAUTH} else "渠道" if kinds == {ModelSourceType.API} else "来源"
        return False, f"{noun}停用"
    if all(not row.source_enabled for row in sources):
        return False, "来源停用"
    return False, "不可用"


def _current_availability(view: ModelView, source: ModelSourceRef | None) -> str:
    if source is not None:
        selected = menu._source_for_view(view, source)
        return "当前来源可用" if selected is not None and selected.effective_routable else "当前来源不可用"
    return "当前可用" if any(item.effective_routable for item in view.sources) else "当前无可用来源"


def _container_label(source_type: ModelSourceType) -> str:
    return "账户" if source_type is ModelSourceType.OAUTH else "渠道"


def _list_context(s: menu._Session) -> menu._ListContext:
    return menu._ListContext(
        tab=s.tab, page=s.page, text=s.text, source=s.source,
        source_label=s.source_label, source_provider=s.source_provider,
        status=s.status, origin=s.origin, alias_page=s.alias_page,
        alias_query=s.alias_query, multiple=s.multiple,
        selection_mode=s.selection_mode, selected=tuple(s.selected),
        selected_resources=tuple(s.selected_resources.items()),
        selection_filters=s.selection_filters, excluded=tuple(s.excluded),
    )


def _restore_list_context(s: menu._Session, context: menu._ListContext) -> None:
    s.tab = context.tab
    s.page = context.page
    s.text = context.text
    s.source = context.source
    s.source_label = context.source_label
    s.source_provider = context.source_provider
    s.status = context.status
    s.origin = context.origin
    s.alias_page = context.alias_page
    s.alias_query = context.alias_query
    s.multiple = context.multiple
    s.selection_mode = context.selection_mode
    s.selected = list(context.selected)
    s.selected_resources = dict(context.selected_resources)
    s.selection_filters = context.selection_filters
    s.excluded = list(context.excluded)


def _list_back_callback(chat_id: int, context: menu._ListContext | None = None) -> str:
    return menu._freeze(chat_id, "restore_list", context=context or menu._list_context(menu._session(chat_id)))


def _detail_callback(chat_id: int, resource_key: str, back_callback: str) -> str:
    return menu._freeze(
        chat_id, "detail", resource_key=resource_key,
        back_callback=back_callback,
    )


def _provider_for_view(view: ModelView, source: ModelSourceRef | None) -> str | None:
    if view.identity.provider:
        return view.identity.provider
    selected = menu._source_for_view(view, source)
    if selected is not None:
        return selected.provider
    return view.sources[0].provider if view.sources else None


def _selected_count(chat_id: int, ctx=None) -> int:
    s = menu._session(chat_id)
    if not s.multiple:
        return 0
    if s.selection_mode is ModelSelectionMode.IDS:
        return len(s.selected)
    if s.selection_filters is None:
        return 0
    ctx = ctx or menu._ctx(chat_id)
    page = menu._CONTROL.list_models(ctx, filters=s.selection_filters, page=1, page_size=1)
    return max(0, page.total - len(set(s.excluded)))


def _selected_views(chat_id: int, ctx) -> list[ModelView]:
    s = menu._session(chat_id)
    if s.selection_mode is ModelSelectionMode.FILTER and s.selection_filters is not None:
        page_no = 1
        values: list[ModelView] = []
        excluded = set(s.excluded)
        while True:
            page = menu._CONTROL.list_models(ctx, filters=s.selection_filters, page=page_no, page_size=200)
            values.extend(item for item in page.items if item.model_id not in excluded)
            if not page.has_next:
                break
            page_no += 1
        return values
    result = []
    for model_id in s.selected:
        resource_key = s.selected_resources.get(model_id)
        if not resource_key:
            continue
        try:
            result.append(menu._CONTROL.get_model(ctx, resource_key))
        except ManagementError:
            continue
    return result


def _selection_dto(s: menu._Session) -> ModelSelection:
    if s.selection_mode is ModelSelectionMode.FILTER:
        return ModelSelection(
            mode=ModelSelectionMode.FILTER,
            filters=s.selection_filters,
            excluded_model_ids=tuple(s.excluded),
        )
    return ModelSelection(mode=ModelSelectionMode.IDS, model_ids=tuple(s.selected))


def _batch_value(view: ModelView, field: ModelStateField, source: ModelSourceRef | None) -> bool:
    if field is ModelStateField.VISIBLE:
        return bool(view.visible)
    source_view = menu._source_for_view(view, source)
    return bool(source_view.source_enabled) if source is not None and source_view is not None else bool(view.global_enabled)


def _batch_button(
    chat_id: int,
    ctx,
    field: ModelStateField,
    revision: str,
) -> dict:
    s = menu._session(chat_id)
    views = menu._selected_views(chat_id, ctx)
    prefix = "状态：" if field is ModelStateField.ENABLED else "展示开关："
    if not views:
        return ui.btn(prefix + "—", "mc:noop_empty")
    source = s.selection_filters.source if s.selection_mode is ModelSelectionMode.FILTER and s.selection_filters else s.source
    values = [menu._batch_value(view, field, source) for view in views]
    mixed = any(value != values[0] for value in values[1:])
    if mixed:
        status = "混合"
    elif field is ModelStateField.ENABLED:
        status = "启用" if values[0] else "禁用"
    else:
        status = "开" if values[0] else "关"
    target = not all(values)
    callback = menu._freeze(
        chat_id,
        "set_state",
        selection=menu._selection_dto(s),
        scope=(source if field is ModelStateField.ENABLED else None),
        target=ModelStateTarget(field=field, value=target),
        revision=revision,
        detail_key=None,
    )
    return ui.btn(prefix + status, callback)


def _model_list_render(chat_id: int) -> tuple[str, dict]:
    s = menu._session(chat_id)
    ctx = menu._ctx(chat_id)
    filters = menu._filters(s)
    page = menu._CONTROL.list_models(ctx, filters=filters, page=s.page, page_size=menu._PAGE_SIZE)
    pages = max(1, math.ceil(page.total / menu._PAGE_SIZE))
    if s.page > pages:
        s.page = pages
        page = menu._CONTROL.list_models(ctx, filters=filters, page=s.page, page_size=menu._PAGE_SIZE)
    source_text = menu._source_label(chat_id, s.source, page.items[0] if page.items else None)
    source_icon = (
        ui.provider_custom_emoji_html(s.source_provider)
        if s.source is not None and ui.provider_custom_emoji_id(s.source_provider)
        else ""
    )
    source_prefix = f"{source_icon} " if source_icon else ""
    lines = [
        f"🤖 <b>模型中心 · {menu._kind_label(s.tab)} · {page.total} 个</b>",
        f"查询：<code>{ui.escape_html(s.text or '未设置')}</code>",
        f"来源：{source_prefix}<code>{ui.escape_html(source_text)}</code> · 状态：<code>{ui.escape_html(menu._status_label(s.status))}</code>",
    ]
    if s.multiple:
        lines.append(f"多选：已选 <b>{menu._selected_count(chat_id, ctx)}</b> 项")
    lines.append("")
    rows: list[list[dict]] = [menu._tabs(s.tab)]
    number_row: list[dict] = []
    start = (page.page - 1) * menu._PAGE_SIZE
    for offset, view in enumerate(page.items):
        index = start + offset + 1
        enabled, state_text = _list_model_state(view, s.source)
        selected = (
            view.model_id not in set(s.excluded)
            if s.multiple and s.selection_mode is ModelSelectionMode.FILTER
            else view.model_id in set(s.selected)
        )
        marker = "☑" if selected and s.multiple else ("✅" if enabled is True else "🚫" if enabled is False else "•")
        hidden_note = " · 已隐藏" if view.visible is False else ""
        owner = view.identity.owner
        owner_note = ""
        if owner is not None and owner.type is ModelSourceType.OAUTH:
            owner_note = " · 账户专属只读" if not view.editable else " · 账户专属"
        lines.append(
            f"{index}. {marker} <code>{ui.escape_html(view.model_id)}</code>"
            + (f" · {ui.escape_html(state_text)}" if state_text else "")
            + (
                hidden_note
                if view.identity.kind is ModelKind.CHAT
                else f" · {ui.escape_html(ui.provider_label(view.identity.provider or ''))}{owner_note}"
            )
        )
        action = "select_model" if s.multiple else "detail"
        callback = menu._freeze(
            chat_id, action, resource_key=view.resource_key, model_id=view.model_id,
            **({} if s.multiple else {"list_context": menu._list_context(s)}),
        )
        provider = menu._provider_for_view(view, s.source)
        number_row.append(ui.provider_button(str(index), callback, provider))
        if len(number_row) == 4:
            rows.append(number_row)
            number_row = []
    if number_row:
        rows.append(number_row)
    if not page.items:
        lines.append("没有匹配的模型。")
    pager = menu._page_row(chat_id, page.page, pages)
    if pager:
        rows.append(pager)
    rows.append([
        ui.btn("🔎 查询" + (f"：{s.text[:12]}" if s.text else ""), "mc:query"),
        ui.provider_button(
            "来源：" + source_text[:18], "mc:source",
            s.source_provider if s.source is not None else None,
        ),
    ])
    if s.tab == "chat":
        rows.append([
            ui.btn("状态：" + menu._status_label(s.status), "mc:status"),
            ui.btn("退出多选" if s.multiple else "多选", "mc:multi"),
        ])
    if s.multiple and s.tab == "chat":
        rows.append([
            ui.btn("全选结果", "mc:select_all"),
            ui.btn("反选", "mc:invert"),
            ui.btn("清空", "mc:clear_selection"),
        ])
        rows.append([
            menu._batch_button(chat_id, ctx, ModelStateField.ENABLED, page.revision),
            menu._batch_button(chat_id, ctx, ModelStateField.VISIBLE, page.revision),
        ])
        rows.append([
            ui.btn("同步所选元数据", menu._sync_callback(
                chat_id, menu._selected_views(chat_id, ctx),
                s.selection_filters.source if s.selection_mode is ModelSelectionMode.FILTER and s.selection_filters else s.source,
                name="sync_selected", back_callback=menu._list_back_callback(chat_id),
            )),
            ui.btn("完成", "mc:done"),
        ])
    elif s.source is not None:
        rows.append([ui.btn("同步上游", menu._freeze(chat_id, "sync_source", source=s.source))])
    rows.append([
        ui.btn("模型设置", menu._freeze(
            chat_id, "settings",
            back_callback=menu._list_back_callback(chat_id, menu._list_context(s)),
        )),
        ui.btn("返回", s.origin),
    ])
    return menu._paged(chat_id, "\n".join(lines), ui.inline_kb(rows))


def _detail_render(
    chat_id: int, resource_key: str, *, back_callback: str = "mc:list",
) -> tuple[str, dict]:
    s = menu._session(chat_id)
    ctx = menu._ctx(chat_id)
    view = menu._CONTROL.get_model(ctx, resource_key)
    s.detail_key = view.resource_key
    provider = menu._provider_for_view(view, s.source)
    title = f"{ui.provider_tag(provider)} <b>{ui.escape_html(view.model_id)}</b>" if provider else f"🧬 <b>{ui.escape_html(view.model_id)}</b>"
    lines = [title, "", f"类型：<code>{ui.escape_html(menu._kind_label(menu._enum_value(view.identity.kind)))}</code>"]
    if view.aliases:
        lines.append("别名：" + "、".join(f"<code>{ui.escape_html(item)}</code>" for item in view.aliases))
    rows: list[list[dict]] = []
    if view.identity.kind is not ModelKind.CHAT:
        return menu._media_view_detail_render(
            chat_id, view, back_callback=back_callback,
        )
    if view.identity.kind is ModelKind.CHAT:
        enabled, state_text = menu._model_state(view, s.source)
        lines.extend([
            f"状态：<code>{ui.escape_html(state_text)}</code>",
            f"下游展示开关：<code>{'开' if view.visible else '关'}</code>",
            "关闭时不出现在 /v1/models，仍可按名称调用；开启不代表停用模型会出现在列表，只有可用来源才会贡献模型。",
            "",
            "<b>容量、能力与价格</b>",
        ])
        selected_source = menu._source_for_view(view, s.source)
        metadata_sources = (selected_source,) if selected_source is not None else view.sources
        if metadata_sources:
            source_options = {
                (item.ref.type, item.ref.id): item
                for item in menu._source_options(chat_id)
            }
            for source_view in metadata_sources:
                source_option = source_options.get((source_view.type, source_view.id))
                human_source = (
                    source_option.label if source_option is not None else
                    "已移除的账户" if source_view.type is ModelSourceType.OAUTH else
                    "已移除的渠道"
                )
                brand_provider = source_option.provider if source_option is not None else None
                brand_icon = (
                    ui.provider_custom_emoji_html(brand_provider)
                    if ui.provider_custom_emoji_id(brand_provider) else ""
                )
                brand_prefix = f"{brand_icon} " if brand_icon else ""
                lines.extend([
                    "",
                    f"{brand_prefix}<b>{ui.escape_html(human_source)}</b>",
                    f"上游名：<code>{ui.escape_html(source_view.outbound_model)}</code>",
                    f"此模型：<code>{'启用' if source_view.source_enabled else '停用'}</code> · "
                    f"{menu._container_label(source_view.type)}：<code>{'启用' if source_view.container_enabled else '停用'}</code> · "
                    f"当前可用：<code>{'是' if source_view.effective_routable else '否'}</code>",
                    *menu._metadata_lines(source_view.effective_metadata),
                ])
        else:
            lines.extend(menu._metadata_lines(view.common_metadata))
        target_enabled = not bool(enabled)
        state_scope = s.source if s.source is not None else None
        rows.append([
            ui.btn(
                ("停用" if enabled else "启用") + ("此来源" if state_scope is not None else "模型"),
                menu._freeze(
                    chat_id,
                    "set_state",
                    selection=ModelSelection(mode=ModelSelectionMode.IDS, model_ids=(view.model_id,)),
                    scope=state_scope,
                    target=ModelStateTarget(ModelStateField.ENABLED, target_enabled),
                    revision=view.revision,
                    detail_key=view.resource_key,
                    detail_back=back_callback,
                ),
            ),
            ui.btn(
                "对下游隐藏" if view.visible else "对下游展示",
                menu._freeze(
                    chat_id,
                    "set_state",
                    selection=ModelSelection(mode=ModelSelectionMode.IDS, model_ids=(view.model_id,)),
                    scope=None,
                    target=ModelStateTarget(ModelStateField.VISIBLE, not bool(view.visible)),
                    revision=view.revision,
                    detail_key=view.resource_key,
                    detail_back=back_callback,
                ),
            ),
        ])
        rows.append([
            ui.btn("调整元数据", menu._freeze(
                chat_id, "metadata_targets", resource_key=view.resource_key,
                detail_back=back_callback,
            )),
            ui.btn("同步元数据", menu._sync_callback(
                chat_id, [view], s.source, name="sync_one",
                back_callback=menu._detail_callback(chat_id, view.resource_key, back_callback),
            )),
        ])
        if s.source is not None:
            rows.append([
                ui.btn("清除此模型故障", menu._freeze(
                    chat_id, "clear_error", source=s.source, model_id=view.model_id,
                    detail_key=view.resource_key, detail_back=back_callback,
                )),
            ])
            source_view = menu._source_for_view(view, s.source)
            if source_view is not None and s.source.type is ModelSourceType.OAUTH:
                rows.extend(menu._max_context_row(chat_id, view, source_view, back_callback))
    else:
        owner = view.identity.owner
        owner_text = "全局"
        if owner is not None and owner.type is ModelSourceType.OAUTH:
            owner_text = f"账户专属 · {owner.id}"
        lines.extend([
            f"提供方：<code>{ui.escape_html(view.identity.provider or '')}</code>",
            f"归属：<code>{ui.escape_html(owner_text)}</code>",
            f"编辑：<code>{'可编辑' if view.editable else '只读'}</code>",
        ])
        if not view.editable:
            lines.append("账户专属媒体模型只读；全局设置不会覆盖此项。")
        kind = menu._enum_value(view.identity.kind)
        if provider == "openai" and kind == "image":
            rows.append([ui.provider_button("打开 GPT 图片管线", "mc:gpt_images", "openai")])
        elif provider in {"xai", "antigravity"}:
            _models, media_revision, _account_overrides = menu._media_values(chat_id, provider, kind)
            owner = owner or ModelOwnerRef(ModelSourceType.GLOBAL)
            rows.append([ui.provider_button(
                "查看当前媒体模型" if view.editable else "查看只读归属",
                menu._freeze(
                    chat_id, "media_detail", provider=provider, kind=kind,
                    model_id=view.model_id, revision=media_revision, page=1,
                    owner=owner, readonly=not view.editable,
                ),
                provider,
            )])
        else:
            rows.append([ui.btn("打开媒体设置", "mc:settings")])
    rows.append([ui.btn("返回模型列表" if back_callback != "mc:compression" else "返回压缩模型", back_callback)])
    return menu._paged(chat_id, "\n".join(lines), ui.inline_kb(rows))


def _max_context_row(
    chat_id: int, view: ModelView, source_view, detail_back: str,
) -> list[list[dict]]:
    """Render Cursor's existing explicit max-context switch through OAuthControl."""
    if str(source_view.provider).lower() != "cursor":
        return []
    try:
        page = menu._CONTROL.oauth.list_models(menu._ctx(chat_id), source_view.id, page=PageSpec(page=1, page_size=200))
    except (ManagementError, TypeError):
        return []
    model = next((item for item in page.items if item.model_id == source_view.outbound_model), None)
    if model is None or model.max_context_default is None or not model.max_context_window:
        return []
    target = not bool(model.max_context_default)
    callback = menu._freeze(
        chat_id,
        "max_context",
        account_id=source_view.id,
        model_id=source_view.outbound_model,
        target=target,
        revision=page.revision,
        detail_key=view.resource_key,
        detail_back=detail_back,
    )
    return [[ui.btn(f"Max Context：{'开' if model.max_context_default else '关'}", callback)]]


def _workbuddy_source_name(account, account_id: str) -> str:
    """Use only WorkBuddy's summary display name; never query account statistics."""
    display_name = str(getattr(account, "display_name", "") or "").strip()
    # OAuth account IDs are ``provider:provider-local-identity``.  For a
    # WorkBuddy summary only that exact suffix is known to be an internal
    # identity; ``summary.identity`` may instead be the account's real email.
    provider, separator, internal_identity = account_id.partition(":")
    internal_identity = (
        internal_identity if separator and provider == "workbuddy" else ""
    )
    if not display_name or display_name in {account_id, internal_identity}:
        return "未命名账户"
    return display_name


def _source_options(chat_id: int) -> list[menu._SourceOption]:
    """Return human labels paired with canonical IDs; labels never select identity."""
    ctx = menu._ctx(chat_id)
    result: list[menu._SourceOption] = []
    oauth_page = menu._CONTROL.oauth.list_accounts(ctx, page=PageSpec(page=1, page_size=200))
    for account in oauth_page.items:
        provider = menu._enum_value(account.provider)
        provider_label = ui.provider_label(provider)
        display_name = str(getattr(account, "display_name", "") or "").strip()
        identity = str(getattr(account, "identity", "") or "").strip()
        account_id = str(account.account_id)
        if provider == "workbuddy":
            display_name = menu._workbuddy_source_name(account, account_id)
        else:
            if not display_name or display_name == account_id:
                display_name = identity or "未命名账户"
            if identity and identity not in {display_name, account_id}:
                display_name = f"{display_name} · {identity}"
        result.append(menu._SourceOption(
            ModelSourceRef(ModelSourceType.OAUTH, account_id),
            f"{provider_label} · {display_name}", provider,
        ))
    for channel in menu._CONTROL.channels.list_all(ctx):
        provider = str(channel.provider_id or "")
        provider_label = ui.provider_label(provider) if provider else "API 渠道"
        result.append(menu._SourceOption(
            ModelSourceRef(ModelSourceType.API, channel.id),
            f"{provider_label} · {channel.display_name}", provider,
        ))
    return result


def _find_source_option(chat_id: int, source: ModelSourceRef) -> menu._SourceOption | None:
    return next(
        (item for item in menu._source_options(chat_id) if menu._source_equal(item.ref, source)),
        None,
    )


def _source_picker_render(chat_id: int) -> tuple[str, dict]:
    s = menu._session(chat_id)
    lines = [
        "🔎 <b>选择模型来源</b>", "",
        "来源只限定查询和所选集合；下游展示开关独立。开启不代表停用模型会出现在下游列表，只有可用来源才会贡献模型。",
    ]
    rows: list[list[dict]] = [[ui.btn(
        ("✓ " if s.source is None else "") + "全部来源",
        menu._freeze(chat_id, "set_source", source=None, expected_tab=s.tab),
    )]]
    for option in menu._source_options(chat_id):
        selected = menu._source_equal(s.source, option.ref)
        rows.append([ui.provider_button(
            ("✓ " if selected else "") + option.label,
            menu._freeze(
                chat_id, "set_source", source=option.ref,
                expected_tab=s.tab,
            ),
            option.provider,
        )])
    rows.append([ui.btn("取消", "mc:list")])
    return menu._paged(chat_id, "\n".join(lines), ui.inline_kb(rows))


def _status_picker_render(chat_id: int) -> tuple[str, dict]:
    s = menu._session(chat_id)
    options = [
        (None, "全部状态"),
        (ModelStatus.ENABLED, "启用"),
        (ModelStatus.DISABLED, "停用"),
        (ModelStatus.HIDDEN, "下游展示开关：关"),
    ]
    rows = [[ui.btn(("✓ " if s.status is value else "") + label, menu._freeze(chat_id, "set_status", status=value))] for value, label in options]
    rows.append([ui.btn("取消", "mc:list")])
    return "🔎 <b>选择状态条件</b>", ui.inline_kb(rows)
