"""负载均衡菜单：调度算法、统一渠道顺序与模型专属优先级。

Priority 模式不再按协议家族拆分。Telegram 提供两条配置路径：

- 按渠道/账户调整：所有 live 渠道的统一默认顺序；
- 按模型调整：canonical client model 的专属顺序，优先于统一顺序。

批量模型选择一次展示全部模型（不分页）；确认后对所选模型支持渠道取并集，
用与单模型/渠道相同的移动编辑器排序，保存时按每个模型真实支持范围过滤。
"""

from __future__ import annotations

import math
import re
from typing import Optional

from ... import affinity, config, load_balancing, model_mapping
from ...channel import registry
from ...oauth_ids import provider_from_channel_key
from .. import states, ui

_MODE_LABELS = {
    "smart": "智能调度",
    "order": "顺序调度",
    "priority": "优先级调度",
}
_MODEL_PAGE_SIZE = 6
_MODEL_REF_PREFIX = "lb:model:"


def _all_channels() -> list:
    return list(registry.all_channels())


def _channel_icon(ch, *, model_context: bool = False) -> str:
    if ch.type == "oauth":
        provider = provider_from_channel_key(ch.key)
        if provider in {"openai", "xai", "cursor", "claude"}:
            return f"{ui.provider_custom_emoji_html(provider)} 🔐"
        return "✉ 🔐"
    return "🤖" if model_context else "🔀"


def _status_text(ch) -> str:
    if not ch.enabled:
        return "🚫 用户禁用"
    reason = ch.disabled_reason
    if not reason:
        return "✅ 正常"
    if reason == "quota":
        return "🔒 配额禁用"
    if reason == "auth_error":
        return "❌ 认证失败"
    if reason == "user":
        return "🚫 用户禁用"
    return f"⚠ {reason}"


def _compact_channel_label(ch) -> str:
    if ch.type == "oauth":
        provider = provider_from_channel_key(ch.key)
        same_provider = [
            item for item in _all_channels()
            if item.type == "oauth" and provider_from_channel_key(item.key) == provider
        ]
        if len(same_provider) == 1:
            return ui.provider_label(provider)
    return ui.channel_display_name(ch.key, with_family=False)


def _format_item_line(idx: int, key: str, *, model_context: bool = False) -> str:
    ch = registry.get_channel(key)
    if ch is None:
        return f"{idx}. <code>{ui.escape_html(key)}</code> ⚠ 已不存在"
    display = ui.channel_display_name(ch.key, with_family=False)
    return (
        f"{idx}. {_channel_icon(ch, model_context=model_context)} <code>{ui.escape_html(display)}</code> "
        f"{ui.escape_html(_status_text(ch))}"
    )


def _format_order_lines(keys: list[str], *, model_context: bool = False) -> list[str]:
    if not keys:
        return ["<i>当前没有可排序的账户/渠道。</i>"]
    return [
        _format_item_line(i, key, model_context=model_context)
        for i, key in enumerate(keys, start=1)
    ]


def _split_number_rows(n: int, max_cols: int = 6) -> list[list[int]]:
    if n <= 0:
        return []
    rows_count = math.ceil(n / max_cols)
    base = n // rows_count
    extra = n % rows_count
    rows: list[list[int]] = []
    current = 1
    for row_index in range(rows_count):
        size = base + (1 if row_index < extra else 0)
        rows.append(list(range(current, current + size)))
        current += size
    return rows


def _client_models() -> list[str]:
    models: set[str] = set()
    mapping = model_mapping.get_ingress_map(model_mapping.GLOBAL_MAPPING_LINE)
    for ch in _all_channels():
        try:
            values = ch.list_client_models()
        except Exception:
            values = getattr(ch, "models", []) or []
        for model in values or []:
            value = str(model or "").strip()
            if value:
                models.add(str(mapping.get(value) or value).strip())
    return sorted(models, key=lambda value: value.lower())


def _channels_for_model(model: str) -> list:
    result = []
    for ch in _all_channels():
        try:
            if ch.supports_model(model) is not None:
                result.append(ch)
        except Exception:
            continue
    return result


def _effective_model_keys(model: str) -> list[str]:
    channels = _channels_for_model(model)
    return load_balancing.effective_order_for_model(model, channels)


def _model_code(model: str) -> str:
    return ui.register_code(_MODEL_REF_PREFIX + str(model or ""))


def _resolve_model_code(code: str) -> str | None:
    raw = ui.resolve_code(str(code or "")) or ""
    if raw.startswith(_MODEL_REF_PREFIX):
        model = raw[len(_MODEL_REF_PREFIX):]
        return model if model in set(_client_models()) else None
    wanted = str(code or "")
    for model in _client_models():
        if _model_code(model) == wanted:
            return model
    return None


# ─── 调度模式一级页 ───────────────────────────────────────────────


def _main_text_and_kb() -> tuple[str, dict]:
    mode = (config.get().get("channelSelection") or "smart").lower()
    lines = ["⚖️ <b>负载均衡</b>", "", "当前调度算法:"]
    for value in ("smart", "order", "priority"):
        prefix = "✅ " if mode == value else ""
        lines.append(
            f"{prefix}{_MODE_LABELS[value]}（{load_balancing.mode_description(value)}）"
        )
    lines.extend(["", "请选择调度算法"])
    rows: list[list[dict]] = [[
        ui.btn(f"智能调度{' √' if mode == 'smart' else ''}", "lb:mode:smart"),
        ui.btn(f"顺序调度{' √' if mode == 'order' else ''}", "lb:mode:order"),
        ui.btn(f"优先级{' √' if mode == 'priority' else ''}", "lb:mode:priority"),
    ]]
    if mode == "priority":
        lines.extend([
            "",
            "优先级层级：<b>模型专属顺序 &gt; 统一渠道/账户顺序</b>",
            "请选择调整方式",
        ])
        rows.append([ui.btn("🤖 按模型调整优先级", "lb:models:1")])
        rows.append([ui.btn("🔀 按渠道/账户调整优先级", "lb:channels")])
        rows.append([ui.btn("🧹 清除全部亲和", "lb:aff_all")])
    rows.append([ui.btn("◀ 返回主菜单", "menu:main")])
    return "\n".join(lines), ui.inline_kb(rows)


def show(chat_id: int, message_id: int, cb_id: Optional[str] = None) -> None:
    if cb_id is not None:
        ui.answer_cb(cb_id)
    text, keyboard = _main_text_and_kb()
    ui.edit(chat_id, message_id, text, reply_markup=keyboard)


def send_new(chat_id: int) -> None:
    text, keyboard = _main_text_and_kb()
    ui.send(chat_id, text, reply_markup=keyboard)


def _on_mode(chat_id: int, message_id: int, cb_id: str, mode: str) -> None:
    try:
        load_balancing.set_mode(mode)
    except Exception as exc:
        ui.answer_cb(cb_id, "切换失败", show_alert=True)
        ui.send(chat_id, f"❌ 切换失败: <code>{ui.escape_html(str(exc))}</code>")
        return
    ui.answer_cb(cb_id, f"已切换到 {_MODE_LABELS.get(mode, mode)}")
    show(chat_id, message_id)


# ─── 通用顺序编辑器 ───────────────────────────────────────────────


def _edit_state(chat_id: int) -> dict | None:
    state = states.get_state(chat_id)
    if not state or state.get("action") != "lb_edit":
        return None
    return dict(state.get("data") or {})


def _store_edit_state(chat_id: int, data: dict) -> None:
    normalized = dict(data)
    normalized["draft"] = list(data.get("draft") or [])
    normalized["initial"] = list(data.get("initial") or normalized["draft"])
    normalized["selected"] = sorted({int(value) for value in data.get("selected") or []})
    normalized["models"] = [str(model) for model in data.get("models") or []]
    states.set_state(chat_id, "lb_edit", normalized)


def _selection_set(data: dict) -> set[int]:
    return {int(value) for value in data.get("selected") or []}


def _edit_title(data: dict) -> str:
    kind = data.get("kind")
    models = list(data.get("models") or [])
    if kind == "channels":
        return "🔀 <b>按渠道/账户调整优先级</b>"
    if kind == "model" and models:
        return f"🤖 <b>{ui.escape_html(models[0])} · 模型专属优先级</b>"
    return f"🤖 <b>批量模型优先级 · 已选 {len(models)} 个模型</b>"


def _edit_text_and_kb(data: dict) -> tuple[str, dict]:
    draft = list(data.get("draft") or [])
    selected = _selection_set(data)
    kind = data.get("kind") or "channels"
    models = list(data.get("models") or [])
    lines = [
        _edit_title(data),
        "",
    ]
    if kind == "model" and models:
        inherited = not load_balancing.has_model_priority(models[0])
        lines.append(
            "当前来源：<b>统一渠道/账户顺序（尚无模型专属覆盖）</b>"
            if inherited else "当前来源：<b>模型专属顺序</b>"
        )
        lines.append("")
    elif kind == "models_batch":
        lines.extend([
            "所选模型：" + "、".join(f"<code>{ui.escape_html(model)}</code>" for model in models),
            "<i>保存时会按每个模型的真实支持范围过滤此渠道并集。</i>",
            "",
        ])
    lines.extend([
        "当前账户/渠道:",
        *_format_order_lines(draft, model_context=(kind != "channels")),
        "",
        "请先勾选序号，再使用置顶、置底、上移、下移。",
        "“还原”恢复进入本页时的已保存/有效顺序；完成后必须点击保存。",
    ])

    rows: list[list[dict]] = []
    for numbers in _split_number_rows(len(draft)):
        row = []
        for number in numbers:
            label = f"{number} ✅" if number in selected else str(number)
            row.append(ui.btn(label, f"lb:sel:{number}"))
        rows.append(row)
    if draft:
        rows.append([
            ui.btn("🔝 置顶", "lb:mv:top"),
            ui.btn("🔚 置底", "lb:mv:bottom"),
            ui.btn("⬆ 上移", "lb:mv:up"),
            ui.btn("⬇ 下移", "lb:mv:down"),
        ])
    rows.append([ui.btn("还原", "lb:reset"), ui.btn("保存设置", "lb:save")])
    rows.append([ui.btn("⌨ 输入完整顺序", "lb:order_input")])
    if kind == "model" and models:
        rows.append([ui.btn("🧹 清除模型专属顺序", "lb:model_clear")])
    if kind == "channels":
        rows.append([ui.btn("🧹 清除全部亲和", "lb:aff_all")])
    rows.append([ui.btn("◀ 返回", "lb:cancel"), ui.btn("🏠 主菜单", "menu:main")])
    return ui.truncate("\n".join(lines)), ui.inline_kb(rows)


def _show_edit(chat_id: int, message_id: int, cb_id: str | None = None) -> None:
    data = _edit_state(chat_id)
    if cb_id is not None:
        ui.answer_cb(cb_id)
    if not data:
        show(chat_id, message_id)
        return
    text, keyboard = _edit_text_and_kb(data)
    ui.edit(chat_id, message_id, text, reply_markup=keyboard)


def _start_channels(chat_id: int, message_id: int, cb_id: str) -> None:
    draft = load_balancing.normalize_channel_order()
    _store_edit_state(chat_id, {
        "kind": "channels", "draft": draft, "initial": draft, "selected": [],
    })
    _show_edit(chat_id, message_id, cb_id)


def _start_model(
    chat_id: int,
    message_id: int,
    cb_id: str,
    model_code: str,
    page: int,
) -> None:
    model = _resolve_model_code(model_code)
    if not model:
        ui.answer_cb(cb_id, "模型短码已失效", show_alert=True)
        return
    draft = _effective_model_keys(model)
    _store_edit_state(chat_id, {
        "kind": "model",
        "models": [model],
        "draft": draft,
        "initial": draft,
        "selected": [],
        "return_page": max(1, int(page or 1)),
    })
    _show_edit(chat_id, message_id, cb_id)


def _toggle_select(chat_id: int, message_id: int, cb_id: str, raw_index: str) -> None:
    data = _edit_state(chat_id)
    if not data:
        ui.answer_cb(cb_id, "会话已失效")
        show(chat_id, message_id)
        return
    draft = list(data.get("draft") or [])
    try:
        index = int(raw_index)
    except ValueError:
        ui.answer_cb(cb_id, "无效序号")
        return
    if index < 1 or index > len(draft):
        ui.answer_cb(cb_id, "序号越界")
        return
    selected = _selection_set(data)
    if index in selected:
        selected.remove(index)
    else:
        selected.add(index)
    data["selected"] = sorted(selected)
    _store_edit_state(chat_id, data)
    _show_edit(chat_id, message_id, cb_id)


def _move_top(draft: list[str], selected: set[int]) -> list[str]:
    indexes = [index - 1 for index in sorted(selected)]
    chosen = [draft[index] for index in indexes]
    rest = [value for index, value in enumerate(draft) if index not in indexes]
    return chosen + rest


def _move_bottom(draft: list[str], selected: set[int]) -> list[str]:
    indexes = [index - 1 for index in sorted(selected)]
    chosen = [draft[index] for index in indexes]
    rest = [value for index, value in enumerate(draft) if index not in indexes]
    return rest + chosen


def _move_up(draft: list[str], selected: set[int]) -> tuple[list[str], set[int]]:
    result = list(draft)
    zero_based = {index - 1 for index in selected}
    for index in range(1, len(result)):
        if index in zero_based and index - 1 not in zero_based:
            result[index - 1], result[index] = result[index], result[index - 1]
            zero_based.remove(index)
            zero_based.add(index - 1)
    return result, {index + 1 for index in zero_based}


def _move_down(draft: list[str], selected: set[int]) -> tuple[list[str], set[int]]:
    result = list(draft)
    zero_based = {index - 1 for index in selected}
    for index in range(len(result) - 2, -1, -1):
        if index in zero_based and index + 1 not in zero_based:
            result[index + 1], result[index] = result[index], result[index + 1]
            zero_based.remove(index)
            zero_based.add(index + 1)
    return result, {index + 1 for index in zero_based}


def _move(chat_id: int, message_id: int, cb_id: str, operation: str) -> None:
    data = _edit_state(chat_id)
    if not data:
        ui.answer_cb(cb_id, "会话已失效")
        show(chat_id, message_id)
        return
    draft = list(data.get("draft") or [])
    selected = _selection_set(data)
    if not selected:
        ui.answer_cb(cb_id, "请先勾选序号")
        return
    if operation == "top":
        draft = _move_top(draft, selected)
        selected = set(range(1, len(selected) + 1))
    elif operation == "bottom":
        draft = _move_bottom(draft, selected)
        start = len(draft) - len(selected) + 1
        selected = set(range(start, len(draft) + 1))
    elif operation == "up":
        draft, selected = _move_up(draft, selected)
    elif operation == "down":
        draft, selected = _move_down(draft, selected)
    else:
        ui.answer_cb(cb_id, "未知移动操作")
        return
    data["draft"] = draft
    data["selected"] = sorted(selected)
    _store_edit_state(chat_id, data)
    _show_edit(chat_id, message_id, cb_id)


def _reset(chat_id: int, message_id: int, cb_id: str) -> None:
    data = _edit_state(chat_id)
    if not data:
        ui.answer_cb(cb_id, "会话已失效")
        return
    data["draft"] = list(data.get("initial") or [])
    data["selected"] = []
    _store_edit_state(chat_id, data)
    ui.answer_cb(cb_id, "已还原进入本页时的顺序")
    _show_edit(chat_id, message_id)


def _save(chat_id: int, message_id: int, cb_id: str) -> None:
    data = _edit_state(chat_id)
    if not data:
        ui.answer_cb(cb_id, "会话已失效")
        show(chat_id, message_id)
        return
    kind = data.get("kind")
    draft = list(data.get("draft") or [])
    models = list(data.get("models") or [])
    if kind == "channels":
        load_balancing.save_channel_order(draft)
        success = "✅ 已保存统一渠道/账户优先级。"
        back = "lb:channels"
    else:
        orders: dict[str, list[str]] = {}
        for model in models:
            supported = {ch.key for ch in _channels_for_model(model)}
            orders[model] = [key for key in draft if key in supported]
        load_balancing.save_model_orders(orders)
        success = f"✅ 已保存 {len(models)} 个模型的专属优先级。"
        back = f"lb:models:{max(1, int(data.get('return_page') or 1))}"
    states.pop_state(chat_id)
    ui.answer_cb(cb_id, "已保存")
    ui.edit(chat_id, message_id, success, reply_markup=ui.inline_kb([
        [ui.btn("继续调整", back), ui.btn("返回负载均衡", "menu:loadbalancing")],
        [ui.btn("🏠 主菜单", "menu:main")],
    ]))


def _cancel(chat_id: int, message_id: int, cb_id: str) -> None:
    data = _edit_state(chat_id) or {}
    kind = data.get("kind")
    page = max(1, int(data.get("return_page") or 1))
    states.pop_state(chat_id)
    if kind in {"model", "models_batch"}:
        _show_models(chat_id, message_id, cb_id, page)
    else:
        show(chat_id, message_id, cb_id)


def _clear_single_model(chat_id: int, message_id: int, cb_id: str) -> None:
    data = _edit_state(chat_id)
    models = list((data or {}).get("models") or [])
    if not data or data.get("kind") != "model" or len(models) != 1:
        ui.answer_cb(cb_id, "会话已失效")
        return
    removed = load_balancing.clear_model_orders(models)
    model = models[0]
    draft = _effective_model_keys(model)
    data["draft"] = draft
    data["initial"] = draft
    data["selected"] = []
    _store_edit_state(chat_id, data)
    ui.answer_cb(cb_id, "已恢复统一渠道默认" if removed else "当前已继承统一默认")
    _show_edit(chat_id, message_id)


# ─── 完整序号输入（通用编辑器）────────────────────────────────────


def _parse_order_input(text: str, size: int) -> tuple[list[int] | None, str | None]:
    raw_values = [
        value for value in re.split(r"[\s,，;；]+", (text or "").strip())
        if value
    ]
    if not raw_values:
        return None, "请输入序号列表。"
    numbers: list[int] = []
    bad: list[str] = []
    for raw in raw_values:
        try:
            numbers.append(int(raw))
        except ValueError:
            bad.append(raw)
    if bad:
        return None, "存在非法项: " + ", ".join(bad[:10])
    if any(number < 1 or number > size for number in numbers):
        return None, "序号越界。"
    if len(set(numbers)) != len(numbers):
        return None, "存在重复序号。"
    missing = [number for number in range(1, size + 1) if number not in numbers]
    if missing:
        return None, "缺少序号: " + ", ".join(str(number) for number in missing[:10])
    return numbers, None


def _order_input_start(chat_id: int, message_id: int, cb_id: str) -> None:
    data = _edit_state(chat_id)
    if not data:
        ui.answer_cb(cb_id, "会话已失效")
        return
    states.set_state(chat_id, "lb_order_input", {"edit": data})
    draft = list(data.get("draft") or [])
    lines = [
        _edit_title(data), "", "当前账户/渠道:",
        *_format_order_lines(draft, model_context=(data.get("kind") != "channels")), "",
        "请回复完整的新序号排列，例如：", "2,1,3...",
    ]
    ui.answer_cb(cb_id)
    ui.edit(chat_id, message_id, ui.truncate("\n".join(lines)), reply_markup=ui.inline_kb([
        [ui.btn("取消输入", "lb:order_cancel")],
    ]))


def handle_text_state(chat_id: int, action: str, text: str) -> bool:
    if action != "lb_order_input":
        return False
    state = states.get_state(chat_id)
    data = dict(((state or {}).get("data") or {}).get("edit") or {})
    draft = list(data.get("draft") or [])
    order, error = _parse_order_input(text, len(draft))
    if error:
        ui.send(chat_id, f"❌ {ui.escape_html(error)}\n请重新输入：")
        return True
    assert order is not None
    data["draft"] = [draft[index - 1] for index in order]
    data["selected"] = []
    _store_edit_state(chat_id, data)
    preview, keyboard = _edit_text_and_kb(data)
    ui.send(
        chat_id,
        "✅ 新顺序已应用到草稿（尚未保存）。\n\n" + preview,
        reply_markup=keyboard,
    )
    return True


def _order_input_cancel(chat_id: int, message_id: int, cb_id: str) -> None:
    state = states.get_state(chat_id)
    data = dict(((state or {}).get("data") or {}).get("edit") or {})
    if not data:
        show(chat_id, message_id, cb_id)
        return
    _store_edit_state(chat_id, data)
    _show_edit(chat_id, message_id, cb_id)


# ─── 模型列表（6 个/页）────────────────────────────────────────────


def _model_summary_line(model: str) -> str:
    keys = _effective_model_keys(model)
    labels = []
    for key in keys:
        ch = registry.get_channel(key)
        labels.append(_compact_channel_label(ch) if ch is not None else key)
    source = "专属" if load_balancing.has_model_priority(model) else "默认"
    text = f"渠道（{source}）：" + (" → ".join(labels) if labels else "无可用渠道")
    return text if len(text) <= 420 else text[:419] + "…"


def _models_text_and_kb(page: int) -> tuple[str, dict]:
    models = _client_models()
    total_pages = max(1, math.ceil(len(models) / _MODEL_PAGE_SIZE))
    page = max(1, min(int(page or 1), total_pages))
    start = (page - 1) * _MODEL_PAGE_SIZE
    visible = models[start:start + _MODEL_PAGE_SIZE]
    lines = [
        "🤖 <b>按模型调整优先级</b>",
        f"共 <b>{len(models)}</b> 个模型 · 第 <b>{page}/{total_pages}</b> 页",
        "<i>模型专属顺序优先于统一渠道/账户顺序。</i>",
    ]
    for offset, model in enumerate(visible, start=1):
        index = start + offset
        lines.extend([
            "",
            f"<b>{index}.</b> <code>{ui.escape_html(model)}</code>",
            ui.escape_html(_model_summary_line(model)),
        ])

    rows: list[list[dict]] = []
    detail_buttons: list[dict] = []
    for offset, model in enumerate(visible, start=1):
        index = start + offset
        detail_buttons.append(ui.btn(
            f"📄 #{index}", f"lb:model:{_model_code(model)}:{page}",
        ))
        if len(detail_buttons) == 3:
            rows.append(detail_buttons)
            detail_buttons = []
    if detail_buttons:
        rows.append(detail_buttons)
    previous = max(1, page - 1)
    following = min(total_pages, page + 1)
    rows.append([
        ui.btn("🏠 首页", "lb:models:1"),
        ui.btn("◀ 上一页", f"lb:models:{previous}"),
        ui.btn(f"{page}/{total_pages}", f"lb:models:{page}"),
        ui.btn("下一页 ▶", f"lb:models:{following}"),
    ])
    rows.append([ui.btn("☑ 批量修改模型优先级", "lb:model_bulk")])
    rows.append([ui.btn("◀ 返回负载均衡", "menu:loadbalancing")])
    return ui.truncate("\n".join(lines)), ui.inline_kb(rows)


def _show_models(
    chat_id: int,
    message_id: int,
    cb_id: str | None,
    page: int,
) -> None:
    if cb_id is not None:
        ui.answer_cb(cb_id)
    text, keyboard = _models_text_and_kb(page)
    ui.edit(chat_id, message_id, text, reply_markup=keyboard)


# ─── 批量模型选择（明确不分页）────────────────────────────────────


def _bulk_selection_state(chat_id: int) -> dict | None:
    state = states.get_state(chat_id)
    if not state or state.get("action") != "lb_model_select":
        return None
    return dict(state.get("data") or {})


def _render_bulk_selection(chat_id: int) -> tuple[str, dict]:
    data = _bulk_selection_state(chat_id) or {"selected_models": []}
    selected = {str(model) for model in data.get("selected_models") or []}
    models = _client_models()
    selected_ordered = [model for model in models if model in selected]
    lines = [
        "☑ <b>批量修改模型优先级</b>",
        f"共 <b>{len(models)}</b> 个模型 · 已选择 <b>{len(selected_ordered)}</b> 个",
        "<i>本页一次展示全部模型，不分页。确认后将所选模型的可用渠道取并集进行排序。</i>",
    ]
    if selected_ordered:
        lines.extend(["", "已选择：" + "、".join(
            f"<code>{ui.escape_html(model)}</code>" for model in selected_ordered
        )])
    buttons = [
        ui.btn(
            ("✅ " if model in selected else "▫️ ") + model,
            f"lb:model_pick:{_model_code(model)}",
        )
        for model in models
    ]
    rows: list[list[dict]] = []
    for index in range(0, len(buttons), 2):
        rows.append(buttons[index:index + 2])
    rows.append([
        ui.btn(f"✅ 确认（{len(selected_ordered)}）", "lb:model_bulk_confirm"),
        ui.btn("❌ 取消", "lb:model_bulk_cancel"),
    ])
    return ui.truncate("\n".join(lines)), ui.inline_kb(rows)


def _start_model_bulk(chat_id: int, message_id: int, cb_id: str) -> None:
    states.set_state(chat_id, "lb_model_select", {"selected_models": []})
    text, keyboard = _render_bulk_selection(chat_id)
    ui.answer_cb(cb_id)
    ui.edit(chat_id, message_id, text, reply_markup=keyboard)


def _toggle_bulk_model(
    chat_id: int,
    message_id: int,
    cb_id: str,
    model_code: str,
) -> None:
    data = _bulk_selection_state(chat_id)
    model = _resolve_model_code(model_code)
    if data is None or not model:
        ui.answer_cb(cb_id, "批量选择状态已失效", show_alert=True)
        return
    selected = {str(value) for value in data.get("selected_models") or []}
    if model in selected:
        selected.remove(model)
    else:
        selected.add(model)
    states.set_state(chat_id, "lb_model_select", {
        "selected_models": sorted(selected, key=str.lower),
    })
    text, keyboard = _render_bulk_selection(chat_id)
    ui.answer_cb(cb_id)
    ui.edit(chat_id, message_id, text, reply_markup=keyboard)


def _confirm_model_bulk(chat_id: int, message_id: int, cb_id: str) -> None:
    data = _bulk_selection_state(chat_id)
    models = sorted(
        {str(model) for model in (data or {}).get("selected_models") or []},
        key=str.lower,
    )
    if not models:
        ui.answer_cb(cb_id, "请至少选择一个模型", show_alert=True)
        return
    union: list[str] = []
    seen: set[str] = set()
    for model in models:
        for key in _effective_model_keys(model):
            if key not in seen:
                union.append(key)
                seen.add(key)
    _store_edit_state(chat_id, {
        "kind": "models_batch",
        "models": models,
        "draft": union,
        "initial": union,
        "selected": [],
        "return_page": 1,
    })
    _show_edit(chat_id, message_id, cb_id)


def _cancel_model_bulk(chat_id: int, message_id: int, cb_id: str) -> None:
    states.pop_state(chat_id)
    _show_models(chat_id, message_id, cb_id, 1)


# ─── 亲和清理 ─────────────────────────────────────────────────────


def _aff_confirm_all(chat_id: int, message_id: int, cb_id: str) -> None:
    fp_total = affinity.count()
    client_total = affinity.client_count()
    ui.answer_cb(cb_id)
    ui.edit(
        chat_id,
        message_id,
        (
            "⚠️ 确认清除全部渠道的所有亲和绑定？\n"
            f"当前内存计数：fp <b>{fp_total}</b>、client <b>{client_total}</b>\n"
            "此操作会同时清 fp 亲和与 client 软亲和。"
        ),
        reply_markup=ui.inline_kb([[
            ui.btn("✅ 确认清除全部", "lb:aff_all_exec"),
            ui.btn("❌ 取消", "menu:loadbalancing"),
        ]]),
    )


def _aff_exec_all(chat_id: int, message_id: int, cb_id: str) -> None:
    fp_total = affinity.count()
    client_total = affinity.client_count()
    affinity.delete_all()
    affinity.client_delete_all()
    ui.answer_cb(cb_id, f"已清 fp {fp_total}、client {client_total}")
    show(chat_id, message_id)


def _aff_confirm_family(chat_id: int, message_id: int, cb_id: str, family: str) -> None:
    """旧 Telegram 消息兼容入口；新 UI 不再按家族展示。"""
    if family not in load_balancing.FAMILIES:
        ui.answer_cb(cb_id, "无效协议类型")
        return
    ui.answer_cb(cb_id)
    ui.edit(
        chat_id,
        message_id,
        f"⚠️ 确认清除旧版 {ui.escape_html(family)} 家族的所有亲和绑定？",
        reply_markup=ui.inline_kb([[
            ui.btn("✅ 确认清除", f"lb:aff_fam_exec:{family}"),
            ui.btn("❌ 取消", "menu:loadbalancing"),
        ]]),
    )


def _aff_exec_family(chat_id: int, message_id: int, cb_id: str, family: str) -> None:
    if family not in load_balancing.FAMILIES:
        ui.answer_cb(cb_id, "无效协议类型")
        return
    fp_count = affinity.delete_by_protocol(family)
    client_count = affinity.client_delete_by_protocol(family)
    ui.answer_cb(cb_id, f"已清 fp {fp_count}、client {client_count}")
    show(chat_id, message_id)


# ─── callback 路由 ─────────────────────────────────────────────────


def handle_callback(chat_id: int, message_id: int, cb_id: str, data: str) -> bool:
    if data == "menu:loadbalancing":
        show(chat_id, message_id, cb_id); return True
    if data.startswith("lb:mode:"):
        _on_mode(chat_id, message_id, cb_id, data.split(":", 2)[2]); return True
    if data == "lb:channels":
        _start_channels(chat_id, message_id, cb_id); return True
    if data.startswith("lb:fam:"):
        # 旧消息兼容：原家族按钮统一进入全渠道编辑器。
        _start_channels(chat_id, message_id, cb_id); return True
    if data.startswith("lb:models:"):
        try:
            page = int(data.rsplit(":", 1)[-1])
        except ValueError:
            page = 1
        _show_models(chat_id, message_id, cb_id, page); return True
    if data.startswith("lb:model:"):
        payload = data.split(":", 2)[2]
        code, _, page_raw = payload.partition(":")
        try:
            page = int(page_raw or 1)
        except ValueError:
            page = 1
        _start_model(chat_id, message_id, cb_id, code, page); return True
    if data == "lb:model_bulk":
        _start_model_bulk(chat_id, message_id, cb_id); return True
    if data.startswith("lb:model_pick:"):
        _toggle_bulk_model(chat_id, message_id, cb_id, data.rsplit(":", 1)[-1]); return True
    if data == "lb:model_bulk_confirm":
        _confirm_model_bulk(chat_id, message_id, cb_id); return True
    if data == "lb:model_bulk_cancel":
        _cancel_model_bulk(chat_id, message_id, cb_id); return True
    if data.startswith("lb:sel:"):
        _toggle_select(chat_id, message_id, cb_id, data.split(":", 2)[2]); return True
    if data.startswith("lb:mv:"):
        _move(chat_id, message_id, cb_id, data.split(":", 2)[2]); return True
    if data == "lb:reset":
        _reset(chat_id, message_id, cb_id); return True
    if data == "lb:save":
        _save(chat_id, message_id, cb_id); return True
    if data == "lb:model_clear":
        _clear_single_model(chat_id, message_id, cb_id); return True
    if data == "lb:cancel":
        _cancel(chat_id, message_id, cb_id); return True
    if data in {"lb:order_input", "lb:bulk"}:
        _order_input_start(chat_id, message_id, cb_id); return True
    if data in {"lb:order_cancel", "lb:bulk_cancel"}:
        _order_input_cancel(chat_id, message_id, cb_id); return True
    if data == "lb:aff_all":
        _aff_confirm_all(chat_id, message_id, cb_id); return True
    if data == "lb:aff_all_exec":
        _aff_exec_all(chat_id, message_id, cb_id); return True
    if data.startswith("lb:aff_fam:"):
        _aff_confirm_family(chat_id, message_id, cb_id, data.split(":", 2)[2]); return True
    if data.startswith("lb:aff_fam_exec:"):
        _aff_exec_family(chat_id, message_id, cb_id, data.split(":", 2)[2]); return True
    return False
