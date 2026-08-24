"""Single-account OAuth model list/detail menu (non-Cursor)."""
from __future__ import annotations

import asyncio
import math
import threading
import time
from typing import Optional

from ... import cooldown, model_metadata, oauth_manager
from ...cursor_bridge import catalog as cursor_model_catalog
from .. import menu_cache, states, ui

_PAGE_SIZE = 6
_BULK_ACTION = "oam_bulk_disable"


def _context(raw: str) -> tuple[str, int, int, str]:
    parts = (raw or "").split(":")
    short = parts[0] if parts else ""
    try:
        model_page = max(1, int(parts[1]))
    except (IndexError, ValueError):
        model_page = 1
    try:
        account_page = max(1, int(parts[2]))
    except (IndexError, ValueError):
        account_page = 1
    filter_key = parts[3] if len(parts) > 3 and parts[3] else "all"
    return short, model_page, account_page, filter_key


def _cb(short: str, model_page: int, account_page: int, filter_key: str) -> str:
    return f"oam:list:{short}:{model_page}:{account_page}:{filter_key}"


def _status(account_key: str, model: str, disabled: set[str]) -> tuple[str, str, bool]:
    if model in disabled:
        return "🚫", "已停用", False
    state = cooldown.get_state(f"oauth:{account_key}", model) or {}
    until = state.get("cooldown_until")
    if until == -1:
        return "🔴", "已冻结", True
    if isinstance(until, (int, float)) and until > int(time.time() * 1000):
        return "🟠", "冷却中", True
    return "✅", "可用", False


_STATUS_SORT_ORDER = {"可用": 0, "冷却中": 1, "已冻结": 2, "已停用": 3}


def _sorted_status_entries(
    account_key: str, models: list[str], disabled: set[str],
) -> list[tuple[str, tuple[str, str, bool]]]:
    """Name-sort first, then stable-sort by effective display status."""
    entries = [(model, _status(account_key, model, disabled)) for model in models]
    entries.sort(key=lambda item: item[0].casefold())
    entries.sort(key=lambda item: _STATUS_SORT_ORDER.get(item[1][1], 99))
    return entries


def _format_tokens(value) -> str:
    try: number = int(value)
    except (TypeError, ValueError): return ""
    if number <= 0: return ""
    if number >= 1_000_000: return f"{number / 1_000_000:.1f}M"
    if number >= 1_000: return f"{number / 1_000:.0f}K"
    return str(number)


def _effective_binding(account_key: str, model: str) -> model_metadata.MetadataBinding | None:
    return model_metadata.resolve_binding(
        model, scope_key=f"oauth:{account_key}", outbound_model=model,
    )


def _effective_map(account_key: str, models: list[str]) -> dict[str, model_metadata.MetadataBinding]:
    return {model: binding for model in models
            if (binding := _effective_binding(account_key, model)) is not None}


def _summary_lines(record: dict | None) -> list[str]:
    if not record: return []
    facts = []
    context = _format_tokens(record.get("contextWindow"))
    if context: facts.append(f"上下文：{context}")
    efforts = [str(value) for value in record.get("reasoningEfforts") or [] if str(value)]
    if record.get("reasoning") is True or record.get("supportsThinking") is True or efforts: facts.append("🧠")
    input_modalities = {str(value).lower() for value in record.get("inputModalities") or []}
    vision = record.get("vision")
    if vision is True or (vision is None and (
        record.get("supportsImages") is True or "image" in input_modalities
    )): facts.append("🖼")
    lines = [" · ".join(facts)] if facts else []
    if efforts: lines.append("思考档位：" + "、".join(efforts))
    return lines


def _metadata_source_label(binding: model_metadata.MetadataBinding) -> str:
    if binding.authority == "account-upstream":
        return "账户上游目录"
    return "models.dev · 账户绑定" if binding.scope_key else "models.dev · 默认绑定"


def _source_label(selection: dict) -> str:
    source = str(selection.get("source") or "")
    if source.startswith("upstream:"):
        return "账户上游模型"
    if source == "lkg:legacy-config":
        return "账户模型（旧配置）"
    if source == "default:built-in":
        return "程序内置默认模型"
    return "默认模型"


def _pagination(
    current: int, total: int, short: str, account_page: int, filter_key: str,
    *, kind: str = "list",
) -> list[dict]:
    if total <= 1:
        return []
    def button(label: str, page: int | None) -> dict:
        callback = f"oam:{kind}:{short}:{page}:{account_page}:{filter_key}" if page else "oam:noop"
        return ui.btn(label, callback)
    if total <= 10:
        return [
            button("⬅ 上一页" if current > 1 else "◁ 上一页", current - 1 if current > 1 else None),
            button(f"{current}/{total}", None),
            button("➡ 下一页" if current < total else "下一页 ▷", current + 1 if current < total else None),
        ]
    lo, hi = max(1, current - 2), min(total, current + 2)
    if hi - lo + 1 < 5:
        if lo == 1:
            hi = min(total, 5)
        else:
            lo = max(1, total - 4)
    rows: list[dict] = []
    if lo > 1:
        rows.append(button("1", 1))
        if lo > 2:
            rows.append(button("…", None))
    for page in range(lo, hi + 1):
        rows.append(button(f"[{page}]" if page == current else str(page), None if page == current else page))
    if hi < total:
        if hi < total - 1:
            rows.append(button("…", None))
        rows.append(button(str(total), total))
    return rows


def render(account_key: str, *, model_page: int = 1, account_page: int = 1,
           filter_key: str = "all") -> tuple[str, dict]:
    account = oauth_manager.get_account(account_key)
    if not account:
        return "⚠️ OAuth 账户已不存在", ui.inline_kb([[ui.btn("◀ 返回列表", "menu:oauth")]])
    selection = oauth_manager.account_model_selection(account)
    disabled = selection["disabled_models"]
    entries = _sorted_status_entries(account_key, selection["models"], disabled)
    models = [model for model, _status_info in entries]
    statuses = [status_info for _model, status_info in entries]
    total = len(models)
    pages = max(1, math.ceil(total / _PAGE_SIZE))
    model_page = min(max(1, model_page), pages)
    start = (model_page - 1) * _PAGE_SIZE
    page_models = models[start:start + _PAGE_SIZE]
    label = str(account.get("label") or account.get("email") or account_key)
    lines = [
        f"🧬 <b>{ui.escape_html(label)} · 模型管理 · 第 {model_page}/{pages} 页 · 共 {total} 个</b>",
        "",
        f"目录来源: <code>{ui.escape_html(_source_label(selection))}</code>",
        f"同步时间: <code>{ui.escape_html(selection.get('synced_at') or '尚未成功同步')}</code>",
    ]
    provider = oauth_manager.provider_of(account)
    error = str(selection.get("error") or "").strip()
    if selection["fallback"]:
        if provider == "cursor":
            lines.extend(["", "⚠️ <b>没有可用账户目录，后台会重试。</b>"])
        else:
            lines.extend(["", "⚠️ <b>上游同步失败/尚无账户目录，正在使用默认模型。</b>"])
        if error:
            lines.append(f"最近错误: <code>{ui.escape_html(error[:300])}</code>")
    elif error:
        lines.extend([
            "", "⚠️ <b>本次同步失败，正在使用上次成功目录。</b>",
            f"错误摘要: <code>{ui.escape_html(error[:300])}</code>",
            f"上次成功时间: <code>{ui.escape_html(selection.get('synced_at') or '未知')}</code>",
        ])
    available = stopped = abnormal = 0
    for _icon, text, fault in statuses:
        if text == "已停用": stopped += 1
        elif fault: abnormal += 1
        else: available += 1
    lines.extend(["", f"统计: ✅ {available} · 🚫 {stopped} · ⚠️ {abnormal}", ""])
    rows: list[list[dict]] = []
    number_row: list[dict] = []
    short = ui.register_code(account_key)
    bindings = _effective_map(account_key, page_models)
    for offset, model in enumerate(page_models):
        global_index = start + offset + 1
        icon, text, _ = statuses[start + offset]
        lines.append(f"{global_index}. {icon} <code>{ui.escape_html(model)}</code> - {text}")
        lines.extend(_summary_lines(dict(bindings[model].metadata) if model in bindings else None))
        model_ref = ui.register_code(model)
        number_row.append(ui.btn(str(global_index), f"oam:detail:{short}:{model_ref}:{model_page}:{account_page}:{filter_key}"))
        if len(number_row) == 6:
            rows.append(number_row); number_row = []
    if number_row:
        rows.append(number_row)
    page_row = _pagination(model_page, pages, short, account_page, filter_key)
    if page_row:
        rows.append(page_row)
    if models:
        bulk_callback = (
            f"oa:cursor_disable:{short}:{model_page}"
            if oauth_manager.provider_of(account) == "cursor"
            else f"oam:bulk:{short}:{model_page}:{account_page}:{filter_key}"
        )
        rows.append([ui.btn("🚫 批量禁用", bulk_callback)])
    rows.extend([
        [ui.btn("🔄 同步上游", f"oam:sync:{short}:{model_page}:{account_page}:{filter_key}"),
         ui.btn("🧬 默认模型", "odm:show")],
        [ui.btn("◀ 返回账户", f"oa:view:{short}:{account_page}:{filter_key}")],
    ])
    return ui.truncate("\n".join(lines)), ui.inline_kb(rows)


def show(chat_id: int, message_id: int, cb_id: Optional[str], account_key: str,
         *, model_page: int = 1, account_page: int = 1, filter_key: str = "all",
         auto_refresh: bool = False) -> None:
    if cb_id:
        ui.answer_cb(cb_id)
    text, kb = render(account_key, model_page=model_page, account_page=account_page, filter_key=filter_key)
    ui.edit(chat_id, message_id, text, reply_markup=kb)
    if not auto_refresh:
        return
    token = menu_cache.begin_view(chat_id, message_id)
    def worker():
        asyncio.run(oauth_manager.refresh_account_models(account_key))
        if not menu_cache.is_current_view(chat_id, message_id, token):
            return
        text2, kb2 = render(account_key, model_page=model_page, account_page=account_page, filter_key=filter_key)
        menu_cache.run_if_current(chat_id, message_id, token, lambda: ui.edit(chat_id, message_id, text2, reply_markup=kb2))
    threading.Thread(target=worker, daemon=True).start()


def _bulk_state(chat_id: int) -> dict | None:
    state = states.get_state(chat_id)
    if not state or state.get("action") != _BULK_ACTION:
        return None
    return state.get("data") or {}


def _new_bulk_state(
    account_key: str, *, model_page: int, account_page: int, filter_key: str,
) -> dict | None:
    account = oauth_manager.get_account(account_key)
    if not account or oauth_manager.provider_of(account) == "cursor":
        return None
    models = sorted(
        oauth_manager.account_model_selection(account)["models"],
        key=str.casefold,
    )
    visible = set(models)
    selected = oauth_manager.account_disabled_models(account) & visible
    models.sort(key=lambda model: model in selected)
    return {
        "account_key": account_key,
        "models": models,
        "selected": sorted(selected),
        "model_page": max(1, int(model_page or 1)),
        "account_page": max(1, int(account_page or 1)),
        "filter_key": filter_key or "all",
    }


def _load_bulk_state(
    chat_id: int, account_key: str, *, model_page: int,
    account_page: int, filter_key: str,
) -> dict | None:
    data = _bulk_state(chat_id)
    if data and data.get("account_key") == account_key:
        return data
    data = _new_bulk_state(
        account_key, model_page=model_page, account_page=account_page,
        filter_key=filter_key,
    )
    if data is not None:
        states.set_state(chat_id, _BULK_ACTION, data)
    return data


def _bulk_render(data: dict) -> tuple[str, dict] | None:
    account_key = str(data.get("account_key") or "")
    account = oauth_manager.get_account(account_key)
    if not account or oauth_manager.provider_of(account) == "cursor":
        return None
    models = [str(model) for model in data.get("models") or [] if str(model)]
    visible = set(models)
    selected = {
        str(model) for model in data.get("selected") or [] if str(model) in visible
    }
    # ``model_page`` is retained only as the page to return to in the normal
    # model browser. The batch editor intentionally renders the whole snapshot.
    model_page = max(1, int(data.get("model_page") or 1))
    data["model_page"] = model_page
    account_page = max(1, int(data.get("account_page") or 1))
    filter_key = str(data.get("filter_key") or "all")
    label = str(account.get("label") or account.get("email") or account_key)
    hidden_count = len(oauth_manager.account_disabled_models(account) - visible)
    lines = [
        f"🚫 <b>{ui.escape_html(label)} · 批量禁用模型</b>",
        f"共 <b>{len(models)}</b> 个模型 · 将停用 <b>{len(selected)}</b> 个",
        "",
        "点击数字修改草稿；全部停用、全部启用和反选作用于完整列表。",
        "这里只修改账户手动停用，冷却和冻结状态不受影响。",
    ]
    if hidden_count:
        lines.append(f"另有 <b>{hidden_count}</b> 个目录外停用记录将继续保留。")
    lines.append("")

    short = ui.register_code(account_key)
    rows: list[list[dict]] = []
    number_row: list[dict] = []
    for global_index, model in enumerate(models, start=1):
        disabled = model in selected
        lines.append(
            f"{global_index}. {'🚫' if disabled else '✅'} "
            f"<code>{ui.escape_html(model)}</code> - "
            f"{'将停用' if disabled else '保持启用'}"
        )
        number_row.append(ui.btn(
            str(global_index),
            f"oam:bsel:{short}:{global_index}:{model_page}:{account_page}:{filter_key}",
        ))
        if len(number_row) == 3:
            rows.append(number_row)
            number_row = []
    if number_row:
        rows.append(number_row)

    context = f"{short}:{model_page}:{account_page}:{filter_key}"
    rows.extend([
        [ui.btn("🚫 全部停用", f"oam:ball:{context}"),
         ui.btn("✅ 全部启用", f"oam:bclear:{context}")],
        [ui.btn("🔄 反选", f"oam:binv:{context}")],
        [ui.btn(f"💾 保存（停用 {len(selected)}）", f"oam:bsave:{context}")],
        [ui.btn("◀ 取消并返回", f"oam:bcancel:{context}")],
    ])
    return ui.truncate("\n".join(lines)), ui.inline_kb(rows)


def _show_bulk(
    chat_id: int, message_id: int, cb_id: Optional[str], data: dict,
) -> None:
    rendered = _bulk_render(data)
    if rendered is None:
        if cb_id:
            ui.answer_cb(cb_id, "账户或批量编辑页面已失效", show_alert=True)
        return
    states.set_state(chat_id, _BULK_ACTION, data)
    if cb_id:
        ui.answer_cb(cb_id)
    text, kb = rendered
    ui.edit(chat_id, message_id, text, reply_markup=kb)


def _handle_bulk_callback(
    chat_id: int, message_id: int, cb_id: str, kind: str, raw: str,
) -> bool:
    index = 0
    if kind == "bsel":
        parts = raw.split(":")
        if len(parts) < 5:
            ui.answer_cb(cb_id, "页面已过期", show_alert=True)
            return True
        short = parts[0]
        try:
            index = int(parts[1])
            model_page = max(1, int(parts[2]))
            account_page = max(1, int(parts[3]))
        except ValueError:
            ui.answer_cb(cb_id, "模型序号已失效", show_alert=True)
            return True
        filter_key = parts[4] or "all"
    else:
        short, model_page, account_page, filter_key = _context(raw)

    account_key = ui.resolve_code(short)
    if not account_key:
        ui.answer_cb(cb_id, "页面已过期", show_alert=True)
        return True
    if kind == "bulk":
        data = _new_bulk_state(
            account_key, model_page=model_page, account_page=account_page,
            filter_key=filter_key,
        )
    else:
        data = _load_bulk_state(
            chat_id, account_key, model_page=model_page,
            account_page=account_page, filter_key=filter_key,
        )
    if data is None:
        ui.answer_cb(cb_id, "该账户不支持此批量编辑页", show_alert=True)
        return True

    models = [str(model) for model in data.get("models") or [] if str(model)]
    visible = set(models)
    selected = {
        str(model) for model in data.get("selected") or [] if str(model) in visible
    }
    data["model_page"] = model_page
    if kind == "bsel":
        if index < 1 or index > len(models):
            ui.answer_cb(cb_id, "模型序号已失效", show_alert=True)
            return True
        model = models[index - 1]
        selected.remove(model) if model in selected else selected.add(model)
    elif kind == "ball":
        selected = set(models)
    elif kind == "bclear":
        selected = set()
    elif kind == "binv":
        selected = set(models) - selected
    elif kind == "bsave":
        try:
            oauth_manager.set_account_disabled_models(
                account_key, selected, visible_models=models,
            )
        except ValueError as exc:
            ui.answer_cb(cb_id, str(exc), show_alert=True)
            return True
        states.pop_state(chat_id)
        ui.answer_cb(cb_id, f"已停用 {len(selected)} 个账户模型")
        show(
            chat_id, message_id, None, account_key,
            model_page=model_page, account_page=account_page,
            filter_key=filter_key,
        )
        return True
    elif kind == "bcancel":
        states.pop_state(chat_id)
        ui.answer_cb(cb_id, "已取消，未保存修改")
        show(
            chat_id, message_id, None, account_key,
            model_page=model_page, account_page=account_page,
            filter_key=filter_key,
        )
        return True
    elif kind not in {"bulk", "bpage"}:
        return False

    data["selected"] = sorted(selected)
    _show_bulk(chat_id, message_id, cb_id, data)
    return True


def _detail_render(account_key: str, model: str, *, model_page: int,
                   account_page: int, filter_key: str) -> tuple[str, dict]:
    account = oauth_manager.get_account(account_key)
    if not account:
        return "⚠️ OAuth 账户已不存在", ui.inline_kb([[ui.btn("◀ 返回列表", "menu:oauth")]])
    selection = oauth_manager.account_model_selection(account)
    if model not in selection["models"]:
        return "⚠️ 模型已不在当前账户目录", ui.inline_kb([[
            ui.btn("◀ 返回模型列表", _cb(ui.register_code(account_key), model_page, account_page, filter_key))
        ]])
    disabled = selection["disabled_models"]
    binding = _effective_binding(account_key, model)
    record = dict(binding.metadata) if binding else {}
    icon, status, fault = _status(account_key, model, disabled)
    state = cooldown.get_state(f"oauth:{account_key}", model) or {}
    lines = [f"🧬 <b>模型详情</b>", "", f"完整 ID: <code>{ui.escape_html(model)}</code>"]
    detail_fields = [
        ("显示名称", record.get("name")), ("描述", record.get("description") or record.get("tagline")),
        ("上下文", _format_tokens(record.get("contextWindow"))), ("Max Context", _format_tokens(record.get("contextWindowMaxMode"))),
        ("最大输出", _format_tokens(record.get("maxOutputTokens"))),
        ("输入模态", "、".join(record.get("inputModalities") or [])), ("输出模态", "、".join(record.get("outputModalities") or [])),
        ("思考档位", "、".join(record.get("reasoningEfforts") or [])), ("别名", "、".join(record.get("aliases") or [])),
    ]
    for label, value in detail_fields:
        if value: lines.append(f"{label}: <code>{ui.escape_html(str(value))}</code>")
    if record.get("reasoning") is True or record.get("supportsThinking") is True or record.get("reasoningEfforts"): lines.append("推理/Thinking: <code>支持</code>")
    vision = record.get("vision")
    if vision is True or (vision is None and (record.get("supportsImages") is True or "image" in {str(value).lower() for value in record.get("inputModalities") or []})):
        lines.append("图片输入: <code>支持</code>")
    if binding:
        lines.append(f"元数据来源: <code>{ui.escape_html(_metadata_source_label(binding))}</code>")
    lines.extend([
        f"目录来源: <code>{ui.escape_html(_source_label(selection))}</code>",
        f"当前状态: {icon} <b>{status}</b>",
        f"账户手动停用: <code>{'是' if model in disabled else '否'}</code>",
        f"临时冷却: <code>{'是' if status == '冷却中' else '否'}</code>",
        f"永久冻结: <code>{'是' if state.get('cooldown_until') == -1 else '否'}</code>",
    ])
    short, model_ref = ui.register_code(account_key), ui.register_code(model)
    context = f"{short}:{model_ref}:{model_page}:{account_page}:{filter_key}"
    rows = [[ui.btn("✅ 启用模型" if model in disabled else "🚫 停用模型", f"oam:toggle:{context}")]]
    cursor_record = (
        cursor_model_catalog.find_record(account, model)
        if oauth_manager.provider_of(account) == "cursor" else None
    )
    if cursor_record:
        normal_context = int(cursor_record.get("context_window") or 0)
        max_context = int(cursor_record.get("context_window_max_mode") or normal_context or 0)
        if max_context > normal_context > 0:
            max_on = oauth_manager.cursor_max_context_default(account_key, model)
            rows.append([ui.btn("关闭 Max Context" if max_on else "开启 Max Context", f"oam:maxctx:{context}")])
    if fault:
        rows.append([ui.btn("🧹 清除此模型故障", f"oam:clear:{context}")])
    rows.append([ui.btn("◀ 返回模型列表", _cb(short, model_page, account_page, filter_key))])
    return "\n".join(lines), ui.inline_kb(rows)


def handle_callback(chat_id: int, message_id: int, cb_id: str, data: str) -> bool:
    if not data.startswith("oam:"):
        return False
    if data == "oam:noop":
        ui.answer_cb(cb_id, "当前页")
        return True
    action, _, payload = data.partition(":")
    del action
    kind, _, raw = payload.partition(":")
    if kind in {"bulk", "bpage", "bsel", "ball", "bclear", "binv", "bsave", "bcancel"}:
        return _handle_bulk_callback(chat_id, message_id, cb_id, kind, raw)
    if kind in {"list", "open"}:
        short, model_page, account_page, filter_key = _context(raw)
        account_key = ui.resolve_code(short)
        if account_key:
            show(chat_id, message_id, cb_id, account_key, model_page=model_page,
                 account_page=account_page, filter_key=filter_key,
                 auto_refresh=(kind == "open"))
        else: ui.answer_cb(cb_id, "页面已过期")
        return True
    parts = raw.split(":")
    short = parts[0] if parts else ""
    account_key = ui.resolve_code(short)
    if not account_key:
        ui.answer_cb(cb_id, "页面已过期"); return True
    if kind == "sync":
        _, model_page, account_page, filter_key = _context(raw)
        ui.answer_cb(cb_id, "正在同步上游...")
        show(chat_id, message_id, None, account_key, model_page=model_page,
             account_page=account_page, filter_key=filter_key, auto_refresh=True)
        return True
    if len(parts) < 5:
        ui.answer_cb(cb_id, "页面已过期"); return True
    model = ui.resolve_code(parts[1])
    if not model:
        ui.answer_cb(cb_id, "页面已过期"); return True
    try: model_page, account_page = max(1, int(parts[2])), max(1, int(parts[3]))
    except ValueError: model_page = account_page = 1
    filter_key = parts[4] or "all"
    if kind == "detail":
        ui.answer_cb(cb_id)
    elif kind == "toggle":
        current = model in oauth_manager.account_disabled_models(account_key)
        oauth_manager.set_account_model_disabled(account_key, model, not current)
        ui.answer_cb(cb_id, "模型已启用" if current else "模型已停用")
    elif kind == "clear":
        cooldown.clear(f"oauth:{account_key}", model=model)
        ui.answer_cb(cb_id, "模型故障已清除")
    elif kind == "maxctx":
        current = oauth_manager.cursor_max_context_default(account_key, model)
        oauth_manager.set_cursor_max_context_default(account_key, model, not current)
        ui.answer_cb(cb_id, "Max Context 已关闭" if current else "Max Context 已开启")
    else:
        return False
    text, kb = _detail_render(account_key, model, model_page=model_page,
                              account_page=account_page, filter_key=filter_key)
    ui.edit(chat_id, message_id, text, reply_markup=kb)
    return True
