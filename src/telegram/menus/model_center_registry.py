"""Unified naming: pending ids, merge suggestions, manual rules and per-model default effort.

The model_center_menu facade owns the one control binding and session state.
"""
from __future__ import annotations

from typing import Any, Mapping

from ...management_control import ManagementError
from ...model_registry import runtime as model_registry
from ...model_registry.names import EFFORTS
from .. import ui
from . import model_center_menu as menu
from .model_center_icons import inline_kb

_NAME_INPUT = "mc_registry_name"


def _overview(chat_id: int):
    return menu._CONTROL.registry_overview(menu._ctx(chat_id))


def _label(source: str) -> str:
    from ...model_registry.report import source_label
    from ... import config
    return source_label(source, config.get())


def entry_button(chat_id: int) -> dict | None:
    if model_registry.current() is None:
        return None
    overview, _revision = _overview(chat_id)
    count = len(overview.pending) + len(overview.suggestions)
    return ui.btn("🧩 统一命名" + (f"（待处理 {count}）" if count else ""), menu._freeze(chat_id, "registry"))


def _rule_text(rule: Mapping[str, Any]) -> str:
    if rule.get("type") == "merge":
        return f"合并 <code>{ui.escape_html(rule['from'])}</code> → <code>{ui.escape_html(rule['into'])}</code>"
    return (f"指定 {ui.escape_html(_label(rule['source']))} 的 <code>{ui.escape_html(rule['wire'])}</code>"
            f" → <code>{ui.escape_html(rule['model'])}</code>")


def _number_rows(buttons: list[dict], width: int = 4) -> list[list[dict]]:
    return [buttons[index:index + width] for index in range(0, len(buttons), width)]


def _registry_render(chat_id: int) -> tuple[str, dict]:
    overview, _revision = _overview(chat_id)
    if not overview.enabled:
        return "🧩 <b>统一命名</b>\n\n统一模型名未开启。", inline_kb([[ui.btn("返回模型列表", "mc:list")]])
    lines = ["🧩 <b>统一命名</b>", ""]
    rows: list[list[dict]] = []
    groups = _pending_groups(overview)
    lines.append(f"<b>待确认 {len(groups)}</b>")
    buttons = []
    for index, ((provider, wire), items) in enumerate(groups.items(), 1):
        where = (ui.escape_html(_label(items[0].source)) if len(items) == 1
                 else f"{ui.escape_html(provider)}（{len(items)} 个账号）")
        lines.append(f"{index}. {where} 的 <code>{ui.escape_html(wire)}</code>"
                     f"（暂名 <code>{ui.escape_html(items[0].model)}</code>）")
        buttons.append(ui.btn(f"待确认 {index}", menu._freeze(
            chat_id, "registry_pending", provider=provider, wire=wire)))
    if not groups:
        lines.append("无。")
    rows.extend(_number_rows(buttons))
    lines += ["", f"<b>合并建议 {len(overview.suggestions)}</b>"]
    buttons = []
    for index, item in enumerate(overview.suggestions, 1):
        lines.append(f"{index}. " + ui.escape_html(item.text.replace("`", "")))
        buttons.append(ui.btn(f"建议 {index}", menu._freeze(chat_id, "registry_suggestion", key=item.key)))
    if not overview.suggestions:
        lines.append("无。")
    rows.extend(_number_rows(buttons))
    lines += ["", f"<b>人工规则 {len(overview.rules)}</b>"]
    buttons = []
    for index, rule in enumerate(overview.rules, 1):
        lines.append(f"{index}. " + _rule_text(rule))
        buttons.append(ui.btn(f"规则 {index}", menu._freeze(chat_id, "registry_rule", rule=dict(rule))))
    if not overview.rules:
        lines.append("无。")
    rows.extend(_number_rows(buttons))
    if overview.conflicts:
        lines += ["", "<b>别名冲突</b>"] + [ui.escape_html(item.replace("`", "")) for item in overview.conflicts]
    if overview.dismissed:
        lines += ["", f"已忽略的建议 {len(overview.dismissed)} 条。"]
        rows.append([ui.btn("恢复已忽略的建议", menu._freeze(chat_id, "registry_undismiss"))])
    rows.append([ui.btn("返回模型列表", "mc:list")])
    return menu._paged(chat_id, "\n".join(lines), inline_kb(rows))


def _pending_groups(overview) -> dict[tuple[str, str], list]:
    """One decision per provider and upstream id: every account of a provider names its ids the same way."""
    groups: dict[tuple[str, str], list] = {}
    for item in overview.pending:
        groups.setdefault((item.provider, item.wire), []).append(item)
    return groups


def _pending_render(chat_id: int, provider: str, wire: str) -> tuple[str, dict]:
    overview, _revision = _overview(chat_id)
    items = _pending_groups(overview).get((provider, wire), [])
    back = menu._freeze(chat_id, "registry")
    if not items:
        return "这一项已处理或来源已变化。", inline_kb([[ui.btn("返回统一命名", back)]])
    item = items[0]
    sources = [entry.source for entry in items]
    lines = [
        "🧩 <b>待确认</b>", "",
        "来源：" + "、".join(ui.escape_html(_label(source)) for source in sources),
        f"上游 ID：<code>{ui.escape_html(item.wire)}</code>",
        f"显示名：{ui.escape_html(item.name)}",
        f"暂名：<code>{ui.escape_html(item.model)}</code>",
        "", "原因：" + ui.escape_html(item.reason.replace("`", "")),
        "", "选定后对每个来源各写一条人工规则，可在统一命名页删除。" if len(sources) > 1
        else "选定后写成一条人工规则，可在统一命名页删除。",
    ]
    rows = [[ui.btn(f"归入 {option}", menu._freeze(
        chat_id, "registry_assign", sources=sources, wire=item.wire, model=option))] for option in item.options]
    rows.append([ui.btn(f"保持 {item.model}", menu._freeze(
        chat_id, "registry_assign", sources=sources, wire=item.wire, model=item.model))])
    rows.append([ui.btn("自定义名称", menu._freeze(
        chat_id, "registry_name_prompt", provider=provider, sources=sources, wire=item.wire))])
    rows.append([ui.btn("返回统一命名", back)])
    return "\n".join(lines), inline_kb(rows)


def _suggestion_render(chat_id: int, key: str) -> tuple[str, dict]:
    overview, _revision = _overview(chat_id)
    item = next((entry for entry in overview.suggestions if entry.key == key), None)
    back = menu._freeze(chat_id, "registry")
    if item is None:
        return "这条建议已处理或不再成立。", inline_kb([[ui.btn("返回统一命名", back)]])
    lines = [
        "🧩 <b>合并建议</b>", "",
        ui.escape_html(item.text.replace("`", "")), "",
        f"合并后 <code>{ui.escape_html(item.model)}</code> 的所有来源归入 <code>{ui.escape_html(item.into)}</code>，"
        f"旧名字继续可以调用。",
    ]
    rows = [
        [ui.btn(f"合并：{item.model} 并入 {item.into}", menu._freeze(
            chat_id, "registry_merge", model=item.model, into=item.into, pin=dict(item.pin)))],
        [ui.btn("忽略这条建议", menu._freeze(chat_id, "registry_dismiss", key=item.key))],
        [ui.btn("返回统一命名", back)],
    ]
    return "\n".join(lines), inline_kb(rows)


def _rule_render(chat_id: int, rule: Mapping[str, Any]) -> tuple[str, dict]:
    back = menu._freeze(chat_id, "registry")
    lines = ["🧩 <b>人工规则</b>", "", _rule_text(rule), "",
             "删除后按证据重新归并；合并时带过去的开关不会还原。"]
    rows = [[ui.btn("删除此规则", menu._freeze(chat_id, "registry_rule_delete", rule=dict(rule)))],
            [ui.btn("返回统一命名", back)]]
    return "\n".join(lines), inline_kb(rows)


def _write(chat_id: int, message_id: int, cb_id: str | None, change, done: str) -> bool:
    overview, revision = _overview(chat_id)
    rules = [dict(rule) for rule in overview.rules]
    dismissed = list(overview.dismissed)
    change(rules, dismissed)
    try:
        menu._CONTROL.replace_registry_rules(
            menu._ctx(chat_id), rules=rules, dismissed=dismissed, expected_revision=revision)
    except ManagementError as exc:
        if cb_id:
            menu._answer_error(cb_id, exc)
        else:
            ui.send(chat_id, "❌ " + ui.escape_html(menu._error_text(exc)))
        return False
    if cb_id:
        ui.answer_cb(cb_id, done)
    if message_id:
        menu._show_rendered(chat_id, message_id, None, lambda: _registry_render(chat_id))
    else:
        text, kb = _registry_render(chat_id)
        ui.send(chat_id, f"✅ {done}\n\n" + text, reply_markup=kb)
    return True


def _assign(sources, wire: str, model: str):
    targets = set(sources)

    def change(rules: list[dict], _dismissed: list[str]) -> None:
        rules[:] = [rule for rule in rules
                    if not (rule.get("type") == "assign" and rule.get("source") in targets and rule.get("wire") == wire)]
        rules.extend({"type": "assign", "source": source, "wire": wire, "model": model} for source in sources)
    return change


def handle_name_input(chat_id: int, data: Mapping[str, Any], raw: str) -> None:
    from .. import states
    if not raw or len(raw) > 100 or any(char.isspace() for char in raw):
        ui.send(chat_id, "❌ 名称需为 1—100 个字符且不能含空白，请重新输入：")
        return
    states.pop_state(chat_id)
    _write(chat_id, 0, None, _assign(data["sources"], data["wire"], raw), f"已归入 {raw.lower()}")


def _effort_choices(model_id: str) -> tuple[str, ...]:
    registry = model_registry.current()
    model = registry.models.get(model_id) if registry else None
    if model is None:
        return ()
    declared = set()
    for offer in model.offers:
        declared.update(offer.efforts)
        if offer.traits.get("effort"):
            declared.add(offer.traits["effort"])
    return tuple(level for level in EFFORTS if level in declared)


def effort_row(chat_id: int, model_id: str, detail_key: str, detail_back: str) -> list[list[dict]]:
    """Detail-page row; empty when no source of the model declares levels (the default would never apply)."""
    if not _effort_choices(model_id):
        return []
    overview, _revision = _overview(chat_id)
    current = overview.defaults.get(model_id)
    label = f"默认强度：{current}" if current else f"默认强度：跟随全局（{model_registry.global_default_effort()}）"
    return [[ui.btn(label, menu._freeze(
        chat_id, "registry_effort", model_id=model_id, detail_key=detail_key, detail_back=detail_back))]]


def _effort_render(chat_id: int, model_id: str, detail_key: str, detail_back: str) -> tuple[str, dict]:
    overview, revision = _overview(chat_id)
    current = overview.defaults.get(model_id)
    choices = _effort_choices(model_id)
    lines = [
        "🧠 <b>默认思考强度</b>", "",
        f"模型：<code>{ui.escape_html(model_id)}</code>",
        "下游没有传强度时使用；各来源按自己支持的档位就近取。下游明确传了的照旧。",
        "输出上限不超过 1024 token 的短请求不补默认强度。",
    ]
    keep = dict(model_id=model_id, revision=revision, detail_key=detail_key, detail_back=detail_back)
    rows = _number_rows([ui.btn(("✓ " if level == current else "") + level, menu._freeze(
        chat_id, "registry_effort_set", effort=level, **keep)) for level in choices], 3)
    rows.append([ui.btn(("✓ " if current is None else "") + f"跟随全局（{model_registry.global_default_effort()}）",
                        menu._freeze(chat_id, "registry_effort_set", effort=None, **keep))])
    rows.append([ui.btn("返回模型详情", menu._detail_callback(chat_id, detail_key, detail_back))])
    return "\n".join(lines), inline_kb(rows)


def handle_action(chat_id: int, message_id: int, cb_id: str, action) -> bool:
    name, data = action.name, action.data
    if not name.startswith("registry"):
        return False
    show = lambda renderer: menu._show_rendered(chat_id, message_id, cb_id, renderer)
    if name == "registry":
        show(lambda: _registry_render(chat_id))
    elif name == "registry_pending":
        show(lambda: _pending_render(chat_id, data["provider"], data["wire"]))
    elif name == "registry_suggestion":
        show(lambda: _suggestion_render(chat_id, data["key"]))
    elif name == "registry_rule":
        show(lambda: _rule_render(chat_id, data["rule"]))
    elif name == "registry_assign":
        _write(chat_id, message_id, cb_id, _assign(data["sources"], data["wire"], data["model"]),
               f"已归入 {data['model']}")
    elif name == "registry_name_prompt":
        menu._prompt(chat_id, _NAME_INPUT, {"sources": list(data["sources"]), "wire": data["wire"]},
                     f"发送 <code>{ui.escape_html(data['wire'])}</code> 要归入的模型名（已有模型名即并入，新名字即单独成为一个模型）。",
                     menu._freeze(chat_id, "registry_pending", provider=data["provider"], wire=data["wire"]))
        ui.answer_cb(cb_id)
    elif name == "registry_merge":
        rule = {"type": "merge", "from": data["model"], "into": data["into"]}
        if data.get("pin"):
            rule["pin"] = dict(data["pin"])
        _write(chat_id, message_id, cb_id, lambda rules, _dismissed: rules.append(rule),
               f"已合并到 {data['into']}")
    elif name == "registry_dismiss":
        _write(chat_id, message_id, cb_id, lambda _rules, dismissed: dismissed.append(data["key"]), "已忽略")
    elif name == "registry_undismiss":
        _write(chat_id, message_id, cb_id, lambda _rules, dismissed: dismissed.clear(), "已恢复")
    elif name == "registry_rule_delete":
        target = data["rule"]
        _write(chat_id, message_id, cb_id,
               lambda rules, _dismissed: rules.__setitem__(slice(None), [rule for rule in rules if rule != target]),
               "规则已删除")
    elif name == "registry_effort":
        show(lambda: _effort_render(chat_id, data["model_id"], data["detail_key"], data["detail_back"]))
    elif name == "registry_effort_set":
        try:
            menu._CONTROL.set_default_effort(menu._ctx(chat_id), model_id=data["model_id"], effort=data["effort"],
                                             expected_revision=data["revision"])
        except ManagementError as exc:
            menu._answer_error(cb_id, exc)
        else:
            ui.answer_cb(cb_id, "已保存")
        menu._show_rendered(chat_id, message_id, None, lambda: _effort_render(
            chat_id, data["model_id"], data["detail_key"], data["detail_back"]))
    else:
        return False
    return True
