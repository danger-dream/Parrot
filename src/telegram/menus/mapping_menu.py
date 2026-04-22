"""模型映射 & 默认模型管理菜单。

callback_data 前缀: `map:...`
状态机 action: `map_alias_input:<line_code>`

交互结构:

  Level 1 (map:show)
    三条 ingress line 总览, 显示默认模型 + 映射条数 + 映射全部列表。
    每条 line 一个 [管理 ...] 按钮进入 Level 2。

  Level 2 (map:line:<line_code>)
    单条 line 的管理页:
      [✏ 设置默认] [🗑 清除默认]     — 默认模型
      [➕ 新增映射]                   — 入口 3a
      [🗑 alias → real]              — 每条映射一个删除按钮
      [◀ 返回]

  Level 3a 新增映射
    Step-1 (map:add:<line_code>)      → 进入状态机, 等用户输入别名
    Step-2 (map_alias_input 触发)     → 拿到别名后直接弹真实模型按钮列表
    Step-3 (map:pick_real:<line_code>:<alias_code>:<model_code>:<page>)
                                      → 真正落库

  Level 3b 设置默认
    (map:set_default:<line_code>)     → 弹真实模型按钮列表
    (map:pick_default:<line_code>:<model_code>:<page>)
                                      → 落库

  Level 3c 删除
    (map:rm:<line_code>:<alias_code>) → 弹确认
    (map:rm_confirm:<line_code>:<alias_code>) → 真删
    (map:clear_default:<line_code>)   → 直接清(不二次确认, 改错重设即可)
"""

from __future__ import annotations

from typing import Optional

from ... import model_mapping
from .. import states, ui


# ─── 常量 ─────────────────────────────────────────────────────────

_PAGE_SIZE = 10   # 真实模型按钮每页条数

# line <-> 短码(callback_data 不能塞带斜线的 line 名, 用固定 3 位 hex 避免爆 64B)
_LINE_CODE: dict[str, str] = {
    "anthropic":        "anp",
    "openai-chat":      "oac",
    "openai-responses": "oar",
}
_CODE_LINE: dict[str, str] = {v: k for k, v in _LINE_CODE.items()}

_LINE_ICON: dict[str, str] = {
    "anthropic":        "🅰",
    "openai-chat":      "🅞",
    "openai-responses": "🅞",
}


def _code_of_line(line: str) -> str:
    return _LINE_CODE[line]


def _line_of_code(code: str) -> Optional[str]:
    return _CODE_LINE.get(code)


# ─── Level 1 总览 ─────────────────────────────────────────────────

def _overview_text() -> str:
    lines = ["🔁 <b>模型映射 &amp; 默认模型</b>", ""]
    for line in model_mapping.INGRESS_LINES:
        icon = _LINE_ICON[line]
        label = model_mapping.INGRESS_LABEL[line]
        default = model_mapping.get_default_model(line) or "(未设置)"
        mp = model_mapping.get_ingress_map(line)
        lines.append(f"{icon} <b>{ui.escape_html(label)}</b>")
        lines.append(f"  默认: <code>{ui.escape_html(default)}</code>")
        lines.append(f"  映射: <b>{len(mp)}</b> 条")
        if mp:
            for alias, real in sorted(mp.items()):
                lines.append(
                    f"    • <code>{ui.escape_html(alias)}</code> → "
                    f"<code>{ui.escape_html(real)}</code>"
                )
        lines.append("")
    lines.append("<i>点下方按钮进入某条入口管理。</i>")
    return "\n".join(lines)


def _overview_kb() -> dict:
    rows: list = []
    for line in model_mapping.INGRESS_LINES:
        icon = _LINE_ICON[line]
        label = model_mapping.INGRESS_LABEL[line].split(" (")[0]
        rows.append([ui.btn(f"{icon} 管理 {label}",
                            f"map:line:{_code_of_line(line)}")])
    rows.append([ui.btn("◀ 返回主菜单", "menu:main")])
    return ui.inline_kb(rows)


def show(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    ui.edit(chat_id, message_id, _overview_text(), reply_markup=_overview_kb())


def send_new(chat_id: int) -> None:
    ui.send(chat_id, _overview_text(), reply_markup=_overview_kb())


# ─── Level 2 单条 line 的管理页 ────────────────────────────────────

def _line_text(line: str) -> str:
    icon = _LINE_ICON[line]
    label = model_mapping.INGRESS_LABEL[line]
    default = model_mapping.get_default_model(line)
    mp = model_mapping.get_ingress_map(line)
    out = [
        f"{icon} <b>{ui.escape_html(label)}</b>",
        "",
        f"默认模型: <code>{ui.escape_html(default) if default else '(未设置)'}</code>",
        f"映射 ({len(mp)}):",
    ]
    if mp:
        for alias, real in sorted(mp.items()):
            out.append(
                f"  • <code>{ui.escape_html(alias)}</code> → "
                f"<code>{ui.escape_html(real)}</code>"
            )
    else:
        out.append("  <i>(空)</i>")
    return "\n".join(out)


def _line_kb(line: str) -> dict:
    lc = _code_of_line(line)
    rows: list = []

    # 默认模型操作
    rows.append([
        ui.btn("✏ 设置默认", f"map:set_default:{lc}"),
        ui.btn("🗑 清除默认", f"map:clear_default:{lc}"),
    ])

    # 新增映射
    rows.append([ui.btn("➕ 新增映射", f"map:add:{lc}")])

    # 每条映射一个按钮 → 点进去看详情/改/删
    mp = model_mapping.get_ingress_map(line)
    for alias, real in sorted(mp.items()):
        # alias 可能带符号, 用短码
        ac = ui.register_code(f"map:alias:{line}:{alias}")
        btn_label = f"{alias} → {real}"
        rows.append([ui.btn(btn_label, f"map:item:{lc}:{ac}")])

    rows.append([ui.btn("◀ 返回映射总览", "map:show")])
    return ui.inline_kb(rows)


def _show_line(chat_id: int, message_id: int, cb_id: str, line: str) -> None:
    ui.answer_cb(cb_id)
    ui.edit(chat_id, message_id, _line_text(line), reply_markup=_line_kb(line))

# ─── Level 3d 条目详情页 (点某条映射时弹出) ──────────────────────

def _show_item(
    chat_id: int, message_id: int, cb_id: str,
    line: str, alias_code: str,
) -> None:
    alias_tag = ui.resolve_code(alias_code)
    if not alias_tag:
        ui.answer_cb(cb_id, "会话已过期"); return
    try:
        _, _, at_line, alias = alias_tag.split(":", 3)
    except ValueError:
        ui.answer_cb(cb_id, "会话异常"); return
    if at_line != line:
        ui.answer_cb(cb_id, "会话异常"); return

    mp = model_mapping.get_ingress_map(line)
    real = mp.get(alias)
    if real is None:
        ui.answer_cb(cb_id, "该映射已不存在")
        _show_line(chat_id, message_id, "-", line)
        return

    ui.answer_cb(cb_id)
    lc = _code_of_line(line)
    text = (
        f"{_LINE_ICON[line]} <b>映射条目 · "
        f"{ui.escape_html(model_mapping.INGRESS_LABEL[line])}</b>\n\n"
        f"别名: <code>{ui.escape_html(alias)}</code>\n"
        f"真实: <code>{ui.escape_html(real)}</code>\n\n"
        "请选择操作:"
    )
    kb = ui.inline_kb([
        [ui.btn("🏷 修改别名", f"map:edit_alias:{lc}:{alias_code}"),
         ui.btn("🎯 修改真实", f"map:edit_real:{lc}:{alias_code}")],
        [ui.btn("🗑 删除本条", f"map:rm:{lc}:{alias_code}")],
        [ui.btn("◀ 返回", f"map:line:{lc}")],
    ])
    ui.edit(chat_id, message_id, text, reply_markup=kb)


def _start_edit_alias(
    chat_id: int, message_id: int, cb_id: str,
    line: str, alias_code: str,
) -> None:
    """修改别名: 提示输入新别名, 用 `map_alias_edit:<line>:<old_alias_code>` 状态."""
    alias_tag = ui.resolve_code(alias_code)
    if not alias_tag:
        ui.answer_cb(cb_id, "会话已过期"); return
    try:
        _, _, at_line, alias = alias_tag.split(":", 3)
    except ValueError:
        ui.answer_cb(cb_id, "会话异常"); return
    if at_line != line:
        ui.answer_cb(cb_id, "会话异常"); return
    mp = model_mapping.get_ingress_map(line)
    if alias not in mp:
        ui.answer_cb(cb_id, "该映射已不存在")
        _show_line(chat_id, message_id, "-", line); return

    ui.answer_cb(cb_id)
    lc = _code_of_line(line)
    states.set_state(chat_id, f"map_alias_edit:{lc}:{alias_code}")
    ui.edit(
        chat_id, message_id,
        f"{_LINE_ICON[line]} <b>修改别名 · "
        f"{ui.escape_html(model_mapping.INGRESS_LABEL[line])}</b>\n\n"
        f"当前别名: <code>{ui.escape_html(alias)}</code> → "
        f"<code>{ui.escape_html(mp[alias])}</code>\n\n"
        "请输入<b>新别名</b>(保持真实模型不变):",
        reply_markup=ui.inline_kb([
            [ui.btn("❌ 取消", f"map:item:{lc}:{alias_code}")],
        ]),
    )


def _on_alias_edit(chat_id: int, action: str, text: str) -> None:
    """状态机回调: 用户发来新别名。action = map_alias_edit:<lc>:<alias_code>"""
    parts = action.split(":")
    if len(parts) < 3:
        states.pop_state(chat_id); return
    lc, alias_code = parts[1], parts[2]
    line = _line_of_code(lc)
    if not line:
        states.pop_state(chat_id)
        ui.send(chat_id, "❌ 会话异常"); return
    alias_tag = ui.resolve_code(alias_code)
    if not alias_tag:
        states.pop_state(chat_id)
        ui.send(chat_id, "❌ 会话已过期, 请重新操作"); return
    try:
        _, _, at_line, old_alias = alias_tag.split(":", 3)
    except ValueError:
        states.pop_state(chat_id)
        ui.send(chat_id, "❌ 会话异常"); return
    if at_line != line:
        states.pop_state(chat_id)
        ui.send(chat_id, "❌ 会话异常"); return

    new_alias = (text or "").strip()
    if not new_alias:
        ui.send(chat_id, "❌ 别名不能为空, 请重新输入:"); return
    if any(c.isspace() for c in new_alias):
        ui.send(chat_id, "❌ 别名不能包含空白, 请重新输入:"); return
    if new_alias == old_alias:
        # 没变, 直接当取消处理
        states.pop_state(chat_id)
        ui.send(chat_id, "ℹ 新别名与原别名一致, 未做更改。")
        return
    real_models = model_mapping.list_available_models_for(line)
    if new_alias in real_models:
        ui.send(
            chat_id,
            f"❌ 新别名 <code>{ui.escape_html(new_alias)}</code> 已经是真实模型名。请换一个:",
        ); return
    existing = model_mapping.get_ingress_map(line)
    if new_alias in existing:
        ui.send(
            chat_id,
            f"❌ 新别名 <code>{ui.escape_html(new_alias)}</code> 在该入口已被占用 "
            f"(当前指向 <code>{ui.escape_html(existing[new_alias])}</code>)。请换一个:",
        ); return
    if old_alias not in existing:
        states.pop_state(chat_id)
        ui.send(chat_id, "❌ 原映射已不存在 (可能被另一处删除)"); return

    real = existing[old_alias]
    # 原子替换: 先加新的, 再删旧的 (中间状态下两条都存在, 不影响可用性)
    model_mapping.set_mapping(line, new_alias, real)
    model_mapping.remove_mapping(line, old_alias)
    states.pop_state(chat_id)
    ui.send_result(
        chat_id,
        f"✅ 已修改别名\n"
        f"{_LINE_ICON[line]} {ui.escape_html(model_mapping.INGRESS_LABEL[line])}\n"
        f"<code>{ui.escape_html(old_alias)}</code> → "
        f"<code>{ui.escape_html(new_alias)}</code> (真实: <code>{ui.escape_html(real)}</code>)",
        back_label="◀ 返回该入口",
        back_callback=f"map:line:{_code_of_line(line)}",
    )


def _start_edit_real(
    chat_id: int, message_id: int, cb_id: str,
    line: str, alias_code: str,
) -> None:
    """修改真实模型: 直接弹 picker, 保持别名不变."""
    alias_tag = ui.resolve_code(alias_code)
    if not alias_tag:
        ui.answer_cb(cb_id, "会话已过期"); return
    try:
        _, _, at_line, alias = alias_tag.split(":", 3)
    except ValueError:
        ui.answer_cb(cb_id, "会话异常"); return
    if at_line != line:
        ui.answer_cb(cb_id, "会话异常"); return
    mp = model_mapping.get_ingress_map(line)
    if alias not in mp:
        ui.answer_cb(cb_id, "该映射已不存在")
        _show_line(chat_id, message_id, "-", line); return
    if not model_mapping.list_available_models_for(line):
        ui.answer_cb(cb_id, "无可用真实模型")
        return
    ui.answer_cb(cb_id)
    _edit_edit_real_picker(chat_id, message_id, line, alias_code, page=0)


def _edit_edit_real_picker(
    chat_id: int, message_id: int, line: str, alias_code: str, page: int,
) -> None:
    alias_tag = ui.resolve_code(alias_code) or ""
    alias = alias_tag.split(":", 3)[-1] if alias_tag else "?"
    mp = model_mapping.get_ingress_map(line)
    current = mp.get(alias, "?")

    models = model_mapping.list_available_models_for(line)
    total = len(models)
    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    text = (
        f"{_LINE_ICON[line]} <b>修改真实模型 · "
        f"{ui.escape_html(model_mapping.INGRESS_LABEL[line])}</b>\n\n"
        f"别名: <code>{ui.escape_html(alias)}</code>\n"
        f"当前真实: <code>{ui.escape_html(current)}</code>\n\n"
        "请选择新的真实模型:\n"
        f"<i>第 {page + 1}/{total_pages} 页, 共 {total} 个可选模型。</i>"
    )
    kb = _picker_kb(
        models, page,
        make_row_callback=lambda mc, p: (
            f"map:pick_edit_real:{_code_of_line(line)}:{alias_code}:{mc}:{p}"
        ),
        make_nav_callback=lambda p: (
            f"map:page_edit_real:{_code_of_line(line)}:{alias_code}:{p}"
        ),
        cancel_callback=f"map:item:{_code_of_line(line)}:{alias_code}",
    )
    ui.edit(chat_id, message_id, text, reply_markup=kb)


def _on_pick_edit_real(
    chat_id: int, message_id: int, cb_id: str,
    line: str, alias_code: str, model_code: str,
) -> None:
    alias_tag = ui.resolve_code(alias_code)
    model_tag = ui.resolve_code(model_code)
    if not alias_tag or not model_tag:
        ui.answer_cb(cb_id, "会话已过期"); return
    try:
        _, _, at_line, alias = alias_tag.split(":", 3)
    except ValueError:
        ui.answer_cb(cb_id, "会话异常"); return
    if at_line != line:
        ui.answer_cb(cb_id, "会话异常"); return
    try:
        _, _, real = model_tag.split(":", 2)
    except ValueError:
        ui.answer_cb(cb_id, "会话异常"); return
    mp = model_mapping.get_ingress_map(line)
    if alias not in mp:
        ui.answer_cb(cb_id, "该映射已不存在")
        _show_line(chat_id, message_id, "-", line); return
    try:
        model_mapping.set_mapping(line, alias, real)
    except ValueError as exc:
        ui.answer_cb(cb_id, str(exc)); return
    ui.answer_cb(cb_id, "✅ 已更新")
    _show_item(chat_id, message_id, "-", line, alias_code)



# ─── Level 3a 新增映射: Step 1 输入别名 ──────────────────────────

def _start_add(chat_id: int, message_id: int, cb_id: str, line: str) -> None:
    ui.answer_cb(cb_id)
    lc = _code_of_line(line)
    states.set_state(chat_id, f"map_alias_input:{lc}")
    ui.edit(
        chat_id, message_id,
        f"{_LINE_ICON[line]} <b>新增映射 · "
        f"{ui.escape_html(model_mapping.INGRESS_LABEL[line])}</b>\n\n"
        "请输入<b>别名</b>(客户端请求时传递的模型名):\n"
        "例如: <code>gpt-5.5</code>、<code>my-fast-model</code>\n\n"
        "<i>规则: 别名不能与任何真实模型重名, 也不能与该入口已有别名重复。</i>",
        reply_markup=ui.inline_kb([
            [ui.btn("❌ 取消", f"map:line:{lc}")],
        ]),
    )


def _on_alias_input(chat_id: int, action: str, text: str) -> None:
    """状态机回调: 用户发来别名。

    action 格式: `map_alias_input:<line_code>`
    """
    lc = action.split(":", 1)[1] if ":" in action else ""
    line = _line_of_code(lc)
    if not line:
        states.pop_state(chat_id)
        ui.send(chat_id, "❌ 会话异常, 请重新进入映射菜单。")
        return

    alias = (text or "").strip()
    if not alias:
        ui.send(chat_id, "❌ 别名不能为空, 请重新输入:")
        return
    if any(c.isspace() for c in alias):
        ui.send(chat_id, "❌ 别名不能包含空白字符, 请重新输入:")
        return

    # 不能与真实模型重名(那是 no-op)
    real_models = model_mapping.list_available_models_for(line)
    if alias in real_models:
        ui.send(
            chat_id,
            f"❌ 别名 <code>{ui.escape_html(alias)}</code> 已经是真实模型名, "
            "映射无意义。请换一个别名:",
        )
        return
    # 不能与该入口已有别名重复
    existing = model_mapping.get_ingress_map(line)
    if alias in existing:
        ui.send(
            chat_id,
            f"⚠ 别名 <code>{ui.escape_html(alias)}</code> 已存在 "
            f"(当前指向 <code>{ui.escape_html(existing[alias])}</code>)。\n"
            "继续选择真实模型会<b>覆盖</b>旧值。",
        )

    if not real_models:
        states.pop_state(chat_id)
        ui.send(
            chat_id,
            "❌ 当前该入口没有任何可用真实模型\n"
            "(检查是否有启用的对应家族渠道)。",
        )
        return

    # 清掉状态(后面是按钮流, 不再接收输入)
    states.pop_state(chat_id)

    alias_code = ui.register_code(f"map:pending_alias:{line}:{alias}")
    _send_real_picker_for_add(chat_id, line, alias, alias_code, page=0)


def _send_real_picker_for_add(
    chat_id: int, line: str, alias: str, alias_code: str, page: int,
) -> None:
    """发一条新消息: 让用户从真实模型按钮列表里选一个绑到 alias。"""
    models = model_mapping.list_available_models_for(line)
    text = _picker_text_add(line, alias, page, len(models))
    kb = _picker_kb(
        models, page,
        make_row_callback=lambda mc, p: (
            f"map:pick_real:{_code_of_line(line)}:{alias_code}:{mc}:{p}"
        ),
        make_nav_callback=lambda p: (
            f"map:page_add:{_code_of_line(line)}:{alias_code}:{p}"
        ),
        cancel_callback=f"map:line:{_code_of_line(line)}",
    )
    ui.send(chat_id, text, reply_markup=kb)


def _picker_text_add(line: str, alias: str, page: int, total: int) -> str:
    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    return (
        f"{_LINE_ICON[line]} <b>新增映射 · "
        f"{ui.escape_html(model_mapping.INGRESS_LABEL[line])}</b>\n\n"
        f"别名 <code>{ui.escape_html(alias)}</code> → 请选择真实模型:\n\n"
        f"<i>第 {page + 1}/{total_pages} 页, 共 {total} 个可选模型。</i>"
    )


# ─── Level 3b 设置默认 (也用 picker, 直接 edit 在当前页) ─────────

def _start_set_default(
    chat_id: int, message_id: int, cb_id: str, line: str,
) -> None:
    ui.answer_cb(cb_id)
    models = model_mapping.list_available_models_for(line)
    if not models:
        ui.edit(
            chat_id, message_id,
            "❌ 当前该入口没有任何可用真实模型\n"
            "(检查是否有启用的对应家族渠道)。",
            reply_markup=ui.inline_kb([
                [ui.btn("◀ 返回", f"map:line:{_code_of_line(line)}")],
            ]),
        )
        return
    _edit_default_picker(chat_id, message_id, line, page=0)


def _edit_default_picker(
    chat_id: int, message_id: int, line: str, page: int,
) -> None:
    models = model_mapping.list_available_models_for(line)
    text = _picker_text_default(line, page, len(models))
    kb = _picker_kb(
        models, page,
        make_row_callback=lambda mc, p: (
            f"map:pick_default:{_code_of_line(line)}:{mc}:{p}"
        ),
        make_nav_callback=lambda p: (
            f"map:page_default:{_code_of_line(line)}:{p}"
        ),
        cancel_callback=f"map:line:{_code_of_line(line)}",
    )
    ui.edit(chat_id, message_id, text, reply_markup=kb)


def _picker_text_default(line: str, page: int, total: int) -> str:
    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    current = model_mapping.get_default_model(line)
    return (
        f"{_LINE_ICON[line]} <b>设置默认模型 · "
        f"{ui.escape_html(model_mapping.INGRESS_LABEL[line])}</b>\n\n"
        f"当前: <code>{ui.escape_html(current) if current else '(未设置)'}</code>\n\n"
        "请点击一个真实模型作为默认:\n"
        f"<i>第 {page + 1}/{total_pages} 页, 共 {total} 个可选模型。</i>"
    )


# ─── 通用 picker 键盘 ─────────────────────────────────────────────

def _picker_kb(
    models: list[str], page: int, *,
    make_row_callback,
    make_nav_callback,
    cancel_callback: str,
) -> dict:
    """一个 10 条 + 分页导航的模型选择键盘。

    make_row_callback(model_code, page) -> str
    make_nav_callback(new_page) -> str
    """
    total = len(models)
    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * _PAGE_SIZE
    end = min(start + _PAGE_SIZE, total)

    rows: list = []
    for m in models[start:end]:
        mc = ui.register_code(f"map:model:{m}")
        rows.append([ui.btn(m, make_row_callback(mc, page))])

    nav: list = []
    if page > 0:
        nav.append(ui.btn("◀ 上一页", make_nav_callback(page - 1)))
    if page < total_pages - 1:
        nav.append(ui.btn("下一页 ▶", make_nav_callback(page + 1)))
    if nav:
        rows.append(nav)
    rows.append([ui.btn("❌ 取消", cancel_callback)])
    return ui.inline_kb(rows)


# ─── 真正落库的 callback 分支 ────────────────────────────────────

def _on_pick_real(
    chat_id: int, message_id: int, cb_id: str,
    line: str, alias_code: str, model_code: str,
) -> None:
    """新增映射的最后一步: 从 alias_code + model_code 里解出 alias/real 写库。"""
    alias_tag = ui.resolve_code(alias_code)
    model_tag = ui.resolve_code(model_code)
    # alias_tag 格式: map:pending_alias:<line>:<alias>
    # model_tag 格式: map:model:<model>
    if not alias_tag or not model_tag:
        ui.answer_cb(cb_id, "会话已过期, 请重新操作")
        return
    try:
        _, _, at_line, alias = alias_tag.split(":", 3)
    except ValueError:
        ui.answer_cb(cb_id, "会话异常"); return
    try:
        _, _, real = model_tag.split(":", 2)
    except ValueError:
        ui.answer_cb(cb_id, "会话异常"); return
    if at_line != line:
        ui.answer_cb(cb_id, "会话异常 (line 不匹配)"); return

    try:
        model_mapping.set_mapping(line, alias, real)
    except ValueError as exc:
        ui.answer_cb(cb_id, str(exc))
        return

    ui.answer_cb(cb_id, "✅ 已添加")
    # 删掉 picker 这条消息, 重新回 line 菜单
    ui.delete_message(chat_id, message_id)
    ui.send_result(
        chat_id,
        f"✅ 已新增映射\n"
        f"{_LINE_ICON[line]} {ui.escape_html(model_mapping.INGRESS_LABEL[line])}\n"
        f"<code>{ui.escape_html(alias)}</code> → "
        f"<code>{ui.escape_html(real)}</code>",
        back_label="◀ 返回该入口",
        back_callback=f"map:line:{_code_of_line(line)}",
    )


def _on_pick_default(
    chat_id: int, message_id: int, cb_id: str,
    line: str, model_code: str,
) -> None:
    model_tag = ui.resolve_code(model_code)
    if not model_tag:
        ui.answer_cb(cb_id, "会话已过期"); return
    try:
        _, _, real = model_tag.split(":", 2)
    except ValueError:
        ui.answer_cb(cb_id, "会话异常"); return
    try:
        model_mapping.set_default(line, real)
    except ValueError as exc:
        ui.answer_cb(cb_id, str(exc)); return
    ui.answer_cb(cb_id, "✅ 已保存")
    # 刷新回 line 页
    _show_line(chat_id, message_id, "-", line)


def _on_clear_default(
    chat_id: int, message_id: int, cb_id: str, line: str,
) -> None:
    cleared = model_mapping.clear_default(line)
    ui.answer_cb(cb_id, "✅ 已清除" if cleared else "无默认可清")
    _show_line(chat_id, message_id, "-", line)


# ─── 删除映射 ─────────────────────────────────────────────────────

def _ask_rm(
    chat_id: int, message_id: int, cb_id: str,
    line: str, alias_code: str,
) -> None:
    alias_tag = ui.resolve_code(alias_code)
    if not alias_tag:
        ui.answer_cb(cb_id, "会话已过期"); return
    try:
        _, _, at_line, alias = alias_tag.split(":", 3)
    except ValueError:
        ui.answer_cb(cb_id, "会话异常"); return
    if at_line != line:
        ui.answer_cb(cb_id, "会话异常"); return
    current = model_mapping.get_ingress_map(line).get(alias)
    if not current:
        ui.answer_cb(cb_id, "该映射已不存在")
        _show_line(chat_id, message_id, "-", line)
        return
    ui.answer_cb(cb_id)
    lc = _code_of_line(line)
    ui.edit(
        chat_id, message_id,
        f"确认删除映射:\n\n"
        f"<code>{ui.escape_html(alias)}</code> → "
        f"<code>{ui.escape_html(current)}</code>\n\n"
        "删除后, 下游传 <code>"
        f"{ui.escape_html(alias)}</code> 将按原名走调度(可能因找不到渠道而 404)。",
        reply_markup=ui.confirm_kb(
            confirm_callback=f"map:rm_ok:{lc}:{alias_code}",
            cancel_callback=f"map:line:{lc}",
        ),
    )


def _on_rm_confirm(
    chat_id: int, message_id: int, cb_id: str,
    line: str, alias_code: str,
) -> None:
    alias_tag = ui.resolve_code(alias_code)
    if not alias_tag:
        ui.answer_cb(cb_id, "会话已过期"); return
    try:
        _, _, at_line, alias = alias_tag.split(":", 3)
    except ValueError:
        ui.answer_cb(cb_id, "会话异常"); return
    if at_line != line:
        ui.answer_cb(cb_id, "会话异常"); return
    removed = model_mapping.remove_mapping(line, alias)
    ui.answer_cb(cb_id, "✅ 已删除" if removed else "未命中")
    _show_line(chat_id, message_id, "-", line)


# ─── 分页导航 ─────────────────────────────────────────────────────

def _on_page_default(
    chat_id: int, message_id: int, cb_id: str, line: str, page: int,
) -> None:
    ui.answer_cb(cb_id)
    _edit_default_picker(chat_id, message_id, line, page)


def _on_page_add(
    chat_id: int, message_id: int, cb_id: str,
    line: str, alias_code: str, page: int,
) -> None:
    """新增映射的 picker 翻页(直接 edit 当前消息)。"""
    alias_tag = ui.resolve_code(alias_code)
    if not alias_tag:
        ui.answer_cb(cb_id, "会话已过期"); return
    try:
        _, _, at_line, alias = alias_tag.split(":", 3)
    except ValueError:
        ui.answer_cb(cb_id, "会话异常"); return
    if at_line != line:
        ui.answer_cb(cb_id, "会话异常"); return
    ui.answer_cb(cb_id)
    models = model_mapping.list_available_models_for(line)
    text = _picker_text_add(line, alias, page, len(models))
    kb = _picker_kb(
        models, page,
        make_row_callback=lambda mc, p: (
            f"map:pick_real:{_code_of_line(line)}:{alias_code}:{mc}:{p}"
        ),
        make_nav_callback=lambda p: (
            f"map:page_add:{_code_of_line(line)}:{alias_code}:{p}"
        ),
        cancel_callback=f"map:line:{_code_of_line(line)}",
    )
    ui.edit(chat_id, message_id, text, reply_markup=kb)


# ─── 路由入口 ─────────────────────────────────────────────────────

def handle_callback(chat_id: int, message_id: int, cb_id: str,
                    data: str) -> bool:
    if not data.startswith("map:"):
        return False
    parts = data.split(":")
    # parts[0] == "map"
    action = parts[1] if len(parts) > 1 else ""

    if action == "show":
        show(chat_id, message_id, cb_id)
        return True

    # 所有下面的 action 都带 line_code
    if len(parts) < 3:
        ui.answer_cb(cb_id, "非法 callback")
        return True
    line = _line_of_code(parts[2])
    if not line:
        ui.answer_cb(cb_id, "未知入口")
        return True

    if action == "line":
        _show_line(chat_id, message_id, cb_id, line)
        return True
    if action == "add":
        _start_add(chat_id, message_id, cb_id, line)
        return True
    if action == "set_default":
        _start_set_default(chat_id, message_id, cb_id, line)
        return True
    if action == "clear_default":
        _on_clear_default(chat_id, message_id, cb_id, line)
        return True
    if action == "page_default":
        try:
            page = int(parts[3])
        except (IndexError, ValueError):
            page = 0
        _on_page_default(chat_id, message_id, cb_id, line, page)
        return True
    if action == "pick_default":
        if len(parts) < 4:
            ui.answer_cb(cb_id, "会话异常"); return True
        model_code = parts[3]
        _on_pick_default(chat_id, message_id, cb_id, line, model_code)
        return True
    if action == "page_add":
        if len(parts) < 5:
            ui.answer_cb(cb_id, "会话异常"); return True
        alias_code = parts[3]
        try:
            page = int(parts[4])
        except ValueError:
            page = 0
        _on_page_add(chat_id, message_id, cb_id, line, alias_code, page)
        return True
    if action == "pick_real":
        if len(parts) < 5:
            ui.answer_cb(cb_id, "会话异常"); return True
        alias_code = parts[3]
        model_code = parts[4]
        _on_pick_real(chat_id, message_id, cb_id, line, alias_code, model_code)
        return True
    if action == "item":
        if len(parts) < 4:
            ui.answer_cb(cb_id, "会话异常"); return True
        alias_code = parts[3]
        _show_item(chat_id, message_id, cb_id, line, alias_code)
        return True
    if action == "edit_alias":
        if len(parts) < 4:
            ui.answer_cb(cb_id, "会话异常"); return True
        alias_code = parts[3]
        _start_edit_alias(chat_id, message_id, cb_id, line, alias_code)
        return True
    if action == "edit_real":
        if len(parts) < 4:
            ui.answer_cb(cb_id, "会话异常"); return True
        alias_code = parts[3]
        _start_edit_real(chat_id, message_id, cb_id, line, alias_code)
        return True
    if action == "pick_edit_real":
        if len(parts) < 5:
            ui.answer_cb(cb_id, "会话异常"); return True
        alias_code = parts[3]
        model_code = parts[4]
        _on_pick_edit_real(chat_id, message_id, cb_id, line, alias_code, model_code)
        return True
    if action == "page_edit_real":
        if len(parts) < 5:
            ui.answer_cb(cb_id, "会话异常"); return True
        alias_code = parts[3]
        try:
            page = int(parts[4])
        except ValueError:
            page = 0
        ui.answer_cb(cb_id)
        _edit_edit_real_picker(chat_id, message_id, line, alias_code, page)
        return True
    if action == "rm":
        if len(parts) < 4:
            ui.answer_cb(cb_id, "会话异常"); return True
        alias_code = parts[3]
        _ask_rm(chat_id, message_id, cb_id, line, alias_code)
        return True
    if action == "rm_ok":
        if len(parts) < 4:
            ui.answer_cb(cb_id, "会话异常"); return True
        alias_code = parts[3]
        _on_rm_confirm(chat_id, message_id, cb_id, line, alias_code)
        return True

    ui.answer_cb(cb_id, "未知操作")
    return True


def handle_text_state(chat_id: int, action: str, text: str) -> bool:
    if action.startswith("map_alias_input:"):
        _on_alias_input(chat_id, action, text)
        return True
    if action.startswith("map_alias_edit:"):
        _on_alias_edit(chat_id, action, text)
        return True
    return False
