"""Sparse metadata editing, catalog matching and frozen sync operations.

The model_center_menu facade owns the one control binding and session state.
"""
from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from ...management_control import ManagementError
from ...management_control.models import ModelSourceRef, ModelSourceType, ModelView
from .. import ui
from . import model_center_menu as menu
from .model_center_icons import inline_kb


def _metadata_scope_kwargs(source: ModelSourceRef | None, outbound_model: str | None) -> dict[str, Any]:
    return {
        "scope": "global" if source is None else menu._enum_value(source.type),
        "account_id": source.id if source is not None and source.type is ModelSourceType.OAUTH else None,
        "channel_id": source.id if source is not None and source.type is ModelSourceType.API else None,
        "outbound_model": outbound_model if source is not None else None,
    }


def _metadata_record(chat_id: int, view: ModelView, source: ModelSourceRef | None):
    return menu._CONTROL.mapping.get_metadata(menu._ctx(chat_id), view.model_id, scope_id=source.id if source else None)


def _sync_callback(
    chat_id: int, views: list[ModelView], source: ModelSourceRef | None,
    *, name: str, back_callback: str,
) -> str:
    if not views:
        return "mc:noop_empty"
    Target = menu._mapping_symbol("MetadataSyncTarget")
    targets = tuple(Target(model_id=view.model_id, source=source) for view in views)
    # Mapping owns the sync CAS domain; a model-list revision is not its token.
    revision = menu._metadata_record(chat_id, views[0], source).revision
    return menu._freeze(
        chat_id, name, targets=targets, revision=revision,
        back_callback=back_callback,
    )


def _metadata_targets_render(
    chat_id: int, resource_key: str, detail_back: str = "mc:list",
) -> tuple[str, dict]:
    view = menu._CONTROL.get_model(menu._ctx(chat_id), resource_key)
    rows = [[ui.btn("模型通用值", menu._freeze(
        chat_id, "metadata_editor", resource_key=resource_key,
        source=None, group="capacity", detail_back=detail_back,
    ))]]
    source_labels = {
        (item.ref.type, item.ref.id): item.label
        for item in menu._source_options(chat_id)
    }
    for source in view.sources:
        ref = ModelSourceRef(source.type, source.id)
        label = source_labels.get(
            (ref.type, ref.id),
            "已移除的账户" if ref.type is ModelSourceType.OAUTH else "已移除的渠道",
        )
        rows.append([ui.provider_button(label, menu._freeze(
            chat_id, "metadata_editor", resource_key=resource_key,
            source=ref, group="capacity", detail_back=detail_back,
        ), source.provider)])
    rows.append([ui.btn("返回模型", menu._detail_callback(
        chat_id, resource_key, detail_back,
    ))])
    text = "\n".join([
        f"🧬 <b>调整元数据 · {ui.escape_html(view.model_id)}</b>",
        "",
        "选择模型通用值，或只调整一个账户 / 渠道。未单独设置的字段继续继承。",
    ])
    return menu._paged(chat_id, text, inline_kb(rows))


def _metadata_editor_render(
    chat_id: int, resource_key: str, source: ModelSourceRef | None,
    group: str, detail_back: str = "mc:list",
) -> tuple[str, dict]:
    view = menu._CONTROL.get_model(menu._ctx(chat_id), resource_key)
    record = menu._metadata_record(chat_id, view, source)
    effective = dict(getattr(record, "effective", {}) or {})
    value_source = dict(getattr(record, "value_source", {}) or {})
    constrained_by = dict(getattr(record, "constrained_by", {}) or {})
    selected = dict(
        (getattr(record, "source_override", {}) if source else getattr(record, "common_override", {})) or {}
    )
    title = menu._source_label(chat_id, source, view) if source else "模型通用值"
    fields = [item for item in menu._META_FIELDS if item.group == group]
    lines = [f"🧬 <b>{ui.escape_html(view.model_id)}</b>", ui.escape_html(title), ""]
    for item in fields:
        marker = "手工" if item.key in selected else "继承"
        source_text = value_source.get(item.key)
        suffix = f"；{source_text}" if source_text else ""
        constrained = constrained_by.get(item.key)
        if constrained:
            suffix += "；已收紧"
        lines.append(f"{item.label}：<code>{ui.escape_html(menu._fmt_meta(menu._display_value(effective, item.key), item))}</code>（{marker}{ui.escape_html(suffix)}）")
    if group == "price":
        lines.append("\n单位：美元 / 百万 Token。0 是明确的免费值。")
    lines.append("\n这里只保存被编辑的字段；0、false 和空数组都不会被当作继承。")
    rows: list[list[dict]] = [[
        ui.btn(("✓ " if group == "capacity" else "") + "容量", menu._freeze(chat_id, "metadata_editor", resource_key=resource_key, source=source, group="capacity", detail_back=detail_back)),
        ui.btn(("✓ " if group == "capability" else "") + "能力", menu._freeze(chat_id, "metadata_editor", resource_key=resource_key, source=source, group="capability", detail_back=detail_back)),
        ui.btn(("✓ " if group == "price" else "") + "价格", menu._freeze(chat_id, "metadata_editor", resource_key=resource_key, source=source, group="price", detail_back=detail_back)),
    ]]
    buttons: list[dict] = []
    source_view = menu._source_for_view(view, source)
    outbound = source_view.outbound_model if source_view is not None else None
    for item in fields:
        buttons.append(ui.btn(item.label + (" ✎" if item.key in selected else ""), menu._freeze(
            chat_id,
            "field_edit",
            resource_key=resource_key,
            source=source,
            outbound_model=outbound,
            field=item.key,
            revision=getattr(record, "revision", view.revision),
            group=group, detail_back=detail_back,
        )))
        if len(buttons) == 2:
            rows.append(buttons)
            buttons = []
    if buttons:
        rows.append(buttons)
    rows.append([
        ui.btn("校正目录匹配", menu._freeze(
            chat_id,
            "matching_picker",
            resource_key=resource_key,
            source=source,
            outbound_model=outbound,
            revision=getattr(record, "revision", view.revision),
            page=1,
            group=group, detail_back=detail_back,
        )),
        ui.btn("全部恢复继承", menu._freeze(
            chat_id,
            "metadata_reset_ask",
            resource_key=resource_key,
            source=source,
            outbound_model=outbound,
            revision=getattr(record, "revision", view.revision),
            group=group, detail_back=detail_back,
        )),
    ])
    rows.append([ui.btn("选择其他账户 / 渠道", menu._freeze(
        chat_id, "metadata_targets", resource_key=resource_key,
        detail_back=detail_back,
    ))])
    rows.append([ui.btn("返回模型", menu._detail_callback(
        chat_id, resource_key, detail_back,
    ))])
    return menu._paged(chat_id, "\n".join(lines), inline_kb(rows))


def _catalog_picker_render(
    chat_id: int,
    resource_key: str,
    source: ModelSourceRef | None,
    outbound_model: str | None,
    revision: str,
    page_no: int,
    group: str,
    detail_back: str = "mc:list",
) -> tuple[str, dict]:
    view = menu._CONTROL.get_model(menu._ctx(chat_id), resource_key)
    page = menu._CONTROL.mapping.search_catalog(
        menu._ctx(chat_id), provider=None, query=None, sort="name",
        page=max(1, page_no), page_size=menu._PAGE_SIZE,
    )
    pages = max(1, math.ceil(page.total / menu._PAGE_SIZE))
    page_no = min(max(1, page_no), pages)
    if page_no != page.page:
        page = menu._CONTROL.mapping.search_catalog(
            menu._ctx(chat_id), provider=None, query=None, sort="name",
            page=page_no, page_size=menu._PAGE_SIZE,
        )
    record = menu._metadata_record(chat_id, view, source)
    current = str(getattr(record, "target", "") or "")
    title = menu._source_label(chat_id, source, view) if source else "模型通用匹配"
    lines = [
        f"🧬 <b>校正匹配 · {ui.escape_html(view.model_id)}</b>",
        ui.escape_html(title), "",
        "仅在自动匹配到错误型号时使用；不会修改真实模型名称。", "",
    ]
    rows: list[list[dict]] = [[ui.btn(
        "恢复自动匹配 / 继承",
        menu._freeze(
            chat_id, "matching_clear", resource_key=resource_key,
            source=source, outbound_model=outbound_model,
            revision=revision, group=group, detail_back=detail_back,
        ),
    )]]
    buttons: list[dict] = []
    start = (page_no - 1) * menu._PAGE_SIZE
    for offset, item in enumerate(page.items):
        index = start + offset + 1
        target = str(item.key)
        selected = target == current
        lines.append(
            f"{index}. {'✓ ' if selected else ''}{ui.provider_tag(item.provider_id)} / "
            f"<code>{ui.escape_html(item.name or item.model_id)}</code>"
        )
        buttons.append(ui.provider_button(
            str(index),
            menu._freeze(
                chat_id, "matching_save", resource_key=resource_key,
                source=source, outbound_model=outbound_model,
                revision=revision, group=group, detail_back=detail_back,
                target_model_id=target, provider_id=str(item.provider_id),
            ),
            str(item.provider_id),
        ))
        if len(buttons) == 4:
            rows.append(buttons)
            buttons = []
    if buttons:
        rows.append(buttons)
    if pages > 1:
        rows.append([
            ui.btn("◀ 上一页", menu._freeze(
                chat_id, "matching_picker", resource_key=resource_key,
                source=source, outbound_model=outbound_model,
                revision=revision, page=max(1, page_no - 1), group=group,
                detail_back=detail_back,
            )),
            ui.btn(f"{page_no}/{pages}", "mc:noop"),
            ui.btn("下一页 ▶", menu._freeze(
                chat_id, "matching_picker", resource_key=resource_key,
                source=source, outbound_model=outbound_model,
                revision=revision, page=min(pages, page_no + 1), group=group,
                detail_back=detail_back,
            )),
        ])
    rows.append([ui.btn("返回字段编辑", menu._freeze(
        chat_id, "metadata_editor", resource_key=resource_key,
        source=source, group=group, detail_back=detail_back,
    ))])
    return menu._paged(chat_id, "\n".join(lines), inline_kb(rows))


def _parse_field(text: str, item: menu._MetaField) -> Any:
    raw = str(text or "").strip()
    if item.kind == "tokens":
        normalized = raw.replace(",", "")
        multiplier = 1
        if normalized[-1:].lower() in {"k", "m"}:
            multiplier = 1000 if normalized[-1:].lower() == "k" else 1_000_000
            normalized = normalized[:-1]
        try:
            value = Decimal(normalized) * multiplier
        except (InvalidOperation, ValueError):
            raise ValueError("请输入正整数，支持 300k、1M。")
        if value != value.to_integral_value() or value <= 0 or value > 2_147_483_647:
            raise ValueError("请输入1—2147483647的整数，支持 300k、1M。")
        return int(value)
    if item.kind == "price":
        try:
            value = Decimal(raw.lstrip("$"))
        except InvalidOperation:
            raise ValueError("价格必须是不小于0的数字。")
        if not value.is_finite() or value < 0:
            raise ValueError("价格必须是不小于0的数字。")
        return float(value)
    if item.kind == "list":
        if raw in {"-", "无", "none", "empty"}:
            return []
        values: list[str] = []
        for part in raw.replace("，", ",").replace("/", ",").replace(" ", ",").split(","):
            value = part.strip()
            if value and value not in values:
                values.append(value)
        if not values or len(values) > 20 or any(len(value) > 80 for value in values):
            raise ValueError("请填写1—20项；每项最多80字符，发送 - 表示空数组。")
        return values
    if item.kind == "date":
        import re
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])(?:-(0[1-9]|[12]\d|3[01]))?", raw):
            raise ValueError("请使用 YYYY-MM 或 YYYY-MM-DD。")
        return raw
    raise ValueError("该字段请用页面按钮设置。")


def _handle_metadata_patch(chat_id: int, message_id: int, cb_id: str, data: Mapping[str, Any], *, value: Any = None, inherit: bool = False) -> None:
    Patch = menu._mapping_symbol("MetadataOverridePatch")
    source = data.get("source")
    kwargs = menu._metadata_scope_kwargs(source, data.get("outbound_model"))
    patch = Patch(set_fields={} if inherit else {data["field"]: value}, unset_fields=(data["field"],) if inherit else ())
    try:
        menu._CONTROL.mapping.patch_metadata_overrides(
            menu._ctx(chat_id),
            menu._CONTROL.get_model(menu._ctx(chat_id), data["resource_key"]).model_id,
            patch=patch,
            expected_revision=data.get("revision"),
            **kwargs,
        )
    except ManagementError as exc:
        menu._answer_error(cb_id, exc)
        return
    ui.answer_cb(cb_id, "已恢复继承" if inherit else "字段已保存")
    text, kb = menu._metadata_editor_render(
        chat_id, data["resource_key"], source, data.get("group") or "capacity",
        str(data.get("detail_back") or "mc:list"),
    )
    ui.edit(chat_id, message_id, text, reply_markup=kb)


def handle_action(chat_id: int, message_id: int, cb_id: str, action) -> bool:
    name, data = action.name, action.data
    s = menu._session(chat_id)
    if name == "metadata_targets":
        menu._show_rendered(chat_id, message_id, cb_id, lambda: menu._metadata_targets_render(
            chat_id, data["resource_key"], str(data.get("detail_back") or "mc:list"),
        ))
        return True


    if name == "metadata_editor":
        menu._show_rendered(chat_id, message_id, cb_id, lambda: menu._metadata_editor_render(
            chat_id, data["resource_key"], data.get("source"),
            data.get("group") or "capacity", str(data.get("detail_back") or "mc:list"),
        ))
        return True


    if name == "field_edit":
        item = menu._META_BY_KEY[str(data["field"])]
        if item.kind == "bool":
            rows = [[
                ui.btn("恢复继承", menu._freeze(chat_id, "field_inherit", **dict(data))),
                ui.btn("支持", menu._freeze(chat_id, "field_bool", value=True, **dict(data))),
                ui.btn("不支持", menu._freeze(chat_id, "field_bool", value=False, **dict(data))),
            ], [ui.btn("取消", menu._freeze(
                chat_id, "metadata_editor", resource_key=data["resource_key"],
                source=data.get("source"), group=data.get("group") or "capacity",
                detail_back=data.get("detail_back"),
            ))]]
            ui.answer_cb(cb_id)
            ui.edit(chat_id, message_id, f"调整 <b>{item.label}</b>\n\n只修改这一项；恢复继承会删除覆盖键。", reply_markup=inline_kb(rows))
            return True
        instructions = {
            "tokens": "发送正整数 Token 数，可写 300k、1M。",
            "price": "发送美元 / 百万 Token 的价格，允许0。",
            "list": "用逗号或空格分隔，发送 - 表示明确空数组。",
            "date": "发送 YYYY-MM 或 YYYY-MM-DD。",
        }[item.kind]
        prompt_data = dict(data)
        menu._prompt(
            chat_id, "mc_metadata_field", prompt_data,
            f"调整 <b>{item.label}</b>\n\n{instructions}\n只修改这一项，不复制其他字段。",
            menu._freeze(
                chat_id, "metadata_editor", resource_key=data["resource_key"],
                source=data.get("source"), group=data.get("group") or "capacity",
                detail_back=data.get("detail_back"),
            ),
        )
        ui.answer_cb(cb_id)
        return True


    if name == "field_bool":
        menu._handle_metadata_patch(chat_id, message_id, cb_id, data, value=bool(data["value"]))
        return True


    if name == "field_inherit":
        menu._handle_metadata_patch(chat_id, message_id, cb_id, data, inherit=True)
        return True


    if name == "matching_picker":
        menu._show_rendered(
            chat_id, message_id, cb_id,
            lambda: menu._catalog_picker_render(
                chat_id, data["resource_key"], data.get("source"),
                data.get("outbound_model"), str(data.get("revision") or ""),
                int(data.get("page") or 1), data.get("group") or "capacity",
                str(data.get("detail_back") or "mc:list"),
            ),
        )
        return True


    if name == "matching_save":
        source = data.get("source")
        kwargs = menu._metadata_scope_kwargs(source, data.get("outbound_model"))
        try:
            view = menu._CONTROL.get_model(menu._ctx(chat_id), data["resource_key"])
            menu._CONTROL.mapping.put_binding(
                menu._ctx(chat_id), view.model_id,
                target_model_id=data["target_model_id"],
                provider_id=data["provider_id"],
                expected_revision=data.get("revision"),
                **kwargs,
            )
        except ManagementError as exc:
            menu._answer_error(cb_id, exc)
            return True
        ui.answer_cb(cb_id, "目录匹配已校正")
        text, kb = menu._metadata_editor_render(
            chat_id, data["resource_key"], source,
            data.get("group") or "capacity",
            str(data.get("detail_back") or "mc:list"),
        )
        ui.edit(chat_id, message_id, text, reply_markup=kb)
        return True


    if name == "matching_clear":
        source = data.get("source")
        kwargs = menu._metadata_scope_kwargs(source, data.get("outbound_model"))
        kwargs.pop("outbound_model", None)
        try:
            view = menu._CONTROL.get_model(menu._ctx(chat_id), data["resource_key"])
            menu._CONTROL.mapping.delete_binding_control(
                menu._ctx(chat_id), view.model_id,
                expected_revision=data.get("revision"), **kwargs,
            )
        except ManagementError as exc:
            menu._answer_error(cb_id, exc)
            return True
        ui.answer_cb(cb_id, "已恢复自动匹配 / 继承")
        text, kb = menu._metadata_editor_render(
            chat_id, data["resource_key"], source,
            data.get("group") or "capacity",
            str(data.get("detail_back") or "mc:list"),
        )
        ui.edit(chat_id, message_id, text, reply_markup=kb)
        return True


    if name == "metadata_reset_ask":
        text = "恢复这一层全部字段的继承？\n\n目录匹配、模型启停以及其他来源的覆盖保持不变。"
        kb = inline_kb([[
            ui.btn("确认恢复", menu._freeze(chat_id, "metadata_reset", **dict(data))),
            ui.btn("取消", menu._freeze(
                chat_id, "metadata_editor", resource_key=data["resource_key"],
                source=data.get("source"), group=data.get("group") or "capacity",
                detail_back=data.get("detail_back"),
            )),
        ]])
        ui.answer_cb(cb_id)
        ui.edit(chat_id, message_id, text, reply_markup=kb)
        return True


    if name == "metadata_reset":
        source = data.get("source")
        kwargs = menu._metadata_scope_kwargs(source, data.get("outbound_model"))
        try:
            view = menu._CONTROL.get_model(menu._ctx(chat_id), data["resource_key"])
            menu._CONTROL.mapping.delete_metadata_overrides(menu._ctx(chat_id), view.model_id, expected_revision=data.get("revision"), **kwargs)
        except ManagementError as exc:
            menu._answer_error(cb_id, exc)
            return True
        ui.answer_cb(cb_id, "已恢复全部字段继承")
        text, kb = menu._metadata_editor_render(
            chat_id, data["resource_key"], source,
            data.get("group") or "capacity",
            str(data.get("detail_back") or "mc:list"),
        )
        ui.edit(chat_id, message_id, text, reply_markup=kb)
        return True


    if name in {"sync_one", "sync_selected"}:
        try:
            Mode = menu._mapping_symbol("MetadataSyncMode")
            targets = data.get("targets")
            revision = data.get("revision")
            if not targets or not revision:
                ui.answer_cb(cb_id, "页面已过期，请重新打开同步按钮", show_alert=True)
                return True
            mode = Mode.ONE if name == "sync_one" else Mode.SELECTED
            operation_back = str(data.get("back_callback") or "mc:list")
            operation = menu._CONTROL.mapping.start_metadata_sync(
                menu._ctx(chat_id),
                mode=mode,
                targets=targets,
                source=None,
                refresh_catalog=True,
                expected_revision=revision,
            )
        except ManagementError as exc:
            menu._answer_error(cb_id, exc)
            return True
        ui.answer_cb(cb_id, "元数据同步任务已开始")
        ui.edit(
            chat_id, message_id,
            f"🔄 <b>元数据同步已开始</b>\n\n任务：<code>{ui.escape_html(operation.id)}</code>\n人工匹配、稀疏字段覆盖和服务方原生资料保持。",
            reply_markup=menu._operation_keyboard(chat_id, operation.id, operation_back),
        )
        return True


    return False
