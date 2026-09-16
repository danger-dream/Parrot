"""Independent alias drafts, target selection and atomic mapping actions.

The model_center_menu facade owns the one control binding and session state.
"""
from __future__ import annotations

import math
import secrets

from ...management_control import ManagementError
from ...management_control.models import ModelFilters, ModelKind, ModelView
from .. import ui
from . import model_center_menu as menu


def _alias_render(chat_id: int) -> tuple[str, dict]:
    s = menu._session(chat_id)
    ctx = menu._ctx(chat_id)
    page = menu._CONTROL.mapping.list_mappings(
        ctx,
        query=s.alias_query or None,
        sort="alias",
        page=s.alias_page,
        page_size=menu._PAGE_SIZE,
    )
    pages = max(1, math.ceil(page.total / menu._PAGE_SIZE))
    if s.alias_page > pages:
        s.alias_page = pages
        page = menu._CONTROL.mapping.list_mappings(ctx, query=s.alias_query or None, sort="alias", page=s.alias_page, page_size=menu._PAGE_SIZE)
    lines = [f"🔀 <b>模型别名 · {page.total} 条</b>"]
    if s.alias_query:
        lines.append(f"查询：<code>{ui.escape_html(s.alias_query)}</code>")
    lines.append("")
    rows: list[list[dict]] = [menu._tabs("alias")]
    number_row: list[dict] = []
    start = (page.page - 1) * menu._PAGE_SIZE
    for offset, item in enumerate(page.items):
        index = start + offset + 1
        lines.append(f"{index}. <code>{ui.escape_html(item.alias)}</code> → <code>{ui.escape_html(item.real_model)}</code>")
        number_row.append(ui.btn(str(index), menu._freeze(
            chat_id, "alias_open", alias=item.alias, expected_tab="alias",
        )))
        if len(number_row) == 4:
            rows.append(number_row)
            number_row = []
    if number_row:
        rows.append(number_row)
    if not page.items:
        lines.append("没有匹配的别名。")
    pager = menu._page_row(chat_id, page.page, pages, alias=True)
    if pager:
        rows.append(pager)
    rows.append([ui.btn("🔎 查询", "mc:alias_query"), ui.btn(
        "新增别名", menu._freeze(
            chat_id, "alias_new", revision=page.revision, expected_tab="alias",
        ),
    )])
    rows.append([
        ui.btn("模型设置", menu._freeze(
            chat_id, "settings",
            back_callback=menu._list_back_callback(chat_id, menu._list_context(s)),
        )),
        ui.btn("返回", s.origin),
    ])
    return menu._paged(chat_id, "\n".join(lines), ui.inline_kb(rows))


def _alias_draft_render(chat_id: int) -> tuple[str, dict]:
    draft = menu._alias_drafts.get(chat_id)
    if draft is None:
        return menu._alias_render(chat_id)
    title = "编辑别名" if draft.old_alias else "新增别名"
    text = "\n".join([
        f"🔀 <b>{title}</b>",
        "",
        f"别名：<code>{ui.escape_html(draft.alias or '未填写')}</code>",
        f"真实模型：<code>{ui.escape_html(draft.real_model or '未选择')}</code>",
        "",
        "保存只修改这一条 mapping，不重写真实模型、Key 白名单、负载均衡、压缩或历史元数据。",
    ])
    rows = [
        [ui.btn("编辑别名名称", menu._freeze(chat_id, "alias_name", draft_id=draft.draft_id))],
        [ui.btn("选择真实模型", menu._freeze(chat_id, "alias_target_picker", draft_id=draft.draft_id, page=1))],
        [ui.btn("保存", menu._freeze(chat_id, "alias_save", draft_id=draft.draft_id))],
    ]
    if draft.old_alias:
        rows[-1].append(ui.btn("删除此别名", menu._freeze(chat_id, "alias_delete_ask", draft_id=draft.draft_id)))
    rows.append([ui.btn("返回别名列表", "mc:aliases")])
    return menu._paged(chat_id, text, ui.inline_kb(rows), draft_id=draft.draft_id)


def _all_chat_views(chat_id: int) -> list[ModelView]:
    ctx = menu._ctx(chat_id)
    result: list[ModelView] = []
    page_no = 1
    filters = ModelFilters(kinds=(ModelKind.CHAT,))
    while True:
        page = menu._CONTROL.list_models(ctx, filters=filters, page=page_no, page_size=200)
        result.extend(page.items)
        if not page.has_next:
            break
        page_no += 1
    return result


def _alias_target_render(chat_id: int, draft_id: str, page_no: int) -> tuple[str, dict]:
    draft = menu._alias_drafts.get(chat_id)
    if draft is None or draft.draft_id != draft_id:
        return "编辑页已过期。", ui.inline_kb([[ui.btn("返回别名列表", "mc:aliases")]])
    models = menu._all_chat_views(chat_id)
    pages = max(1, math.ceil(len(models) / menu._PAGE_SIZE))
    page_no = min(max(1, page_no), pages)
    draft.target_page = page_no
    start = (page_no - 1) * menu._PAGE_SIZE
    visible = models[start:start + menu._PAGE_SIZE]
    lines = [f"🔀 <b>选择真实模型 · 第 {page_no}/{pages} 页</b>", ""]
    rows: list[list[dict]] = []
    buttons: list[dict] = []
    for offset, view in enumerate(visible):
        index = start + offset + 1
        selected = draft.real_model == view.model_id
        lines.append(f"{index}. {'✓ ' if selected else ''}<code>{ui.escape_html(view.model_id)}</code>")
        buttons.append(ui.btn(str(index), menu._freeze(chat_id, "alias_target", draft_id=draft_id, model_id=view.model_id)))
        if len(buttons) == 4:
            rows.append(buttons)
            buttons = []
    if buttons:
        rows.append(buttons)
    if pages > 1:
        rows.append([
            ui.btn("◀ 上一页", menu._freeze(chat_id, "alias_target_picker", draft_id=draft_id, page=max(1, page_no - 1))),
            ui.btn(f"{page_no}/{pages}", "mc:noop"),
            ui.btn("下一页 ▶", menu._freeze(chat_id, "alias_target_picker", draft_id=draft_id, page=min(pages, page_no + 1))),
        ])
    rows.append([ui.btn("返回编辑", menu._freeze(chat_id, "alias_edit_back", draft_id=draft_id))])
    return menu._paged(chat_id, "\n".join(lines), ui.inline_kb(rows), draft_id=draft_id)


def _handle_alias_save(chat_id: int, message_id: int, cb_id: str, draft_id: str) -> None:
    draft = menu._alias_drafts.get(chat_id)
    if draft is None or draft.draft_id != draft_id:
        ui.answer_cb(cb_id, "编辑页已过期", show_alert=True)
        return
    if not draft.alias.strip() or not draft.real_model.strip():
        ui.answer_cb(cb_id, "请填写别名并选择真实模型", show_alert=True)
        return
    try:
        ctx = menu._ctx(chat_id)
        if draft.old_alias:
            record = menu._CONTROL.mapping.update_mapping(
                ctx,
                draft.old_alias,
                new_alias=draft.alias,
                real_model=draft.real_model,
                expected_revision=draft.revision,
            )
        else:
            record = menu._CONTROL.mapping.put_mapping(
                ctx,
                draft.alias,
                draft.real_model,
                expected_revision=draft.revision,
            )
    except ManagementError as exc:
        menu._answer_error(cb_id, exc)
        return
    menu._alias_drafts.pop(chat_id, None)
    ui.answer_cb(cb_id, "别名已保存")
    text, kb = menu._alias_render(chat_id)
    ui.edit(chat_id, message_id, text, reply_markup=kb)


def handle_action(chat_id: int, message_id: int, cb_id: str, action) -> bool:
    name, data = action.name, action.data
    s = menu._session(chat_id)
    if name == "alias_open":
        if data.get("expected_tab") not in {None, s.tab}:
            ui.answer_cb(cb_id, "别名列表已过期，请重新打开", show_alert=True)
            return True
        try:
            page = menu._CONTROL.mapping.list_mappings(menu._ctx(chat_id), query=data["alias"], sort="alias", page=1, page_size=10)
            record = next(item for item in page.items if item.alias == data["alias"])
        except (ManagementError, StopIteration) as exc:
            if isinstance(exc, ManagementError):
                menu._answer_error(cb_id, exc)
            else:
                ui.answer_cb(cb_id, "别名已不存在", show_alert=True)
            return True
        menu._alias_drafts[chat_id] = menu._AliasDraft(secrets.token_hex(8), record.alias, record.alias, record.real_model, record.revision)
        menu._show_rendered(chat_id, message_id, cb_id, lambda: menu._alias_draft_render(chat_id))
        return True


    if name == "alias_new":
        if data.get("expected_tab") not in {None, s.tab}:
            ui.answer_cb(cb_id, "别名列表已过期，请重新打开", show_alert=True)
            return True
        draft = menu._AliasDraft(secrets.token_hex(8), None, "", "", str(data["revision"]))
        menu._alias_drafts[chat_id] = draft
        menu._prompt(chat_id, "mc_alias_name", {"draft_id": draft.draft_id}, "发送新别名（1—300字符）。别名不能与真实模型或已有别名冲突。", "mc:aliases")
        ui.answer_cb(cb_id)
        return True


    if name == "alias_name":
        draft = menu._alias_drafts.get(chat_id)
        if draft is None or draft.draft_id != data.get("draft_id"):
            ui.answer_cb(cb_id, "编辑页已过期", show_alert=True)
            return True
        menu._prompt(chat_id, "mc_alias_name", {"draft_id": draft.draft_id}, "发送新的别名（1—300字符）。真实模型保持不变。", menu._freeze(chat_id, "alias_edit_back", draft_id=draft.draft_id))
        ui.answer_cb(cb_id)
        return True


    if name == "alias_target_picker":
        menu._show_rendered(chat_id, message_id, cb_id, lambda: menu._alias_target_render(chat_id, str(data["draft_id"]), int(data.get("page") or 1)))
        return True


    if name == "alias_target":
        draft = menu._alias_drafts.get(chat_id)
        if draft is None or draft.draft_id != data.get("draft_id"):
            ui.answer_cb(cb_id, "编辑页已过期", show_alert=True)
            return True
        draft.real_model = str(data["model_id"])
        menu._show_rendered(chat_id, message_id, cb_id, lambda: menu._alias_draft_render(chat_id))
        return True


    if name == "alias_edit_back":
        draft = menu._alias_drafts.get(chat_id)
        if draft is None or draft.draft_id != data.get("draft_id"):
            ui.answer_cb(cb_id, "编辑页已过期", show_alert=True)
            return True
        menu._show_rendered(chat_id, message_id, cb_id, lambda: menu._alias_draft_render(chat_id))
        return True


    if name == "alias_save":
        menu._handle_alias_save(chat_id, message_id, cb_id, str(data["draft_id"]))
        return True


    if name == "alias_delete_ask":
        draft = menu._alias_drafts.get(chat_id)
        if draft is None or draft.draft_id != data.get("draft_id") or not draft.old_alias:
            ui.answer_cb(cb_id, "编辑页已过期", show_alert=True)
            return True
        text = f"删除别名 <code>{ui.escape_html(draft.old_alias)}</code>？\n\n真实模型及其他设置不受影响。"
        kb = ui.inline_kb([[ui.btn("确认删除", menu._freeze(chat_id, "alias_delete", draft_id=draft.draft_id)), ui.btn("取消", menu._freeze(chat_id, "alias_edit_back", draft_id=draft.draft_id))]])
        ui.answer_cb(cb_id)
        ui.edit(chat_id, message_id, text, reply_markup=kb)
        return True


    if name == "alias_delete":
        draft = menu._alias_drafts.get(chat_id)
        if draft is None or draft.draft_id != data.get("draft_id") or not draft.old_alias:
            ui.answer_cb(cb_id, "编辑页已过期", show_alert=True)
            return True
        try:
            menu._CONTROL.mapping.delete_mapping(menu._ctx(chat_id), draft.old_alias, expected_revision=draft.revision)
        except ManagementError as exc:
            menu._answer_error(cb_id, exc)
            return True
        menu._alias_drafts.pop(chat_id, None)
        ui.answer_cb(cb_id, "别名已删除")
        text, kb = menu._alias_render(chat_id)
        ui.edit(chat_id, message_id, text, reply_markup=kb)
        return True


    return False
