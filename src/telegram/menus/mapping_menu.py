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

import json
from typing import Optional

from ... import compact_rescue, model_mapping, model_metadata, model_pricing
from .. import states, ui


# ─── 常量 ─────────────────────────────────────────────────────────

_PAGE_SIZE = 10   # 真实模型按钮每页条数

# line <-> 短码(callback_data 不能塞带斜线的 line 名, 用固定 3 位 hex 避免爆 64B)
_LINE_CODE: dict[str, str] = {
    model_mapping.GLOBAL_MAPPING_LINE: "glo",
    "anthropic":        "anp",  # legacy callback compatibility
    "openai-chat":      "oac",
    "openai-responses": "oar",
}
_CODE_LINE: dict[str, str] = {v: k for k, v in _LINE_CODE.items()}

_LINE_ICON: dict[str, str] = {
    model_mapping.GLOBAL_MAPPING_LINE: "🔁",
    "anthropic":        ui.provider_icon("claude"),
    "openai-chat":      f"{ui.provider_icon('openai')}/{ui.provider_icon('xai')}",
    "openai-responses": f"{ui.provider_icon('openai')}/{ui.provider_icon('xai')}",
}


def _line_body_icon(line: str) -> str:
    if line == "anthropic":
        return ui.provider_custom_emoji_html("claude")
    if line in ("openai-chat", "openai-responses"):
        return f"{ui.provider_custom_emoji_html('openai')}/{ui.provider_custom_emoji_html('xai')}"
    return _LINE_ICON.get(line, "🔁")


def _line_body_label(line: str) -> str:
    return f"{_line_body_icon(line)} {ui.escape_html(model_mapping.INGRESS_LABEL[line])}"


def _code_of_line(line: str) -> str:
    return _LINE_CODE[line]


def _line_of_code(code: str) -> Optional[str]:
    return _CODE_LINE.get(code)


# ─── Level 1 总览 ─────────────────────────────────────────────────

def _overview_text() -> str:
    mp = model_mapping.get_ingress_map(model_mapping.GLOBAL_MAPPING_LINE)
    bindings = model_metadata.list_bindings()
    compact = model_metadata.get_compression_model() or "(未设置)"
    lines = [
        "🤖 <b>模型管理</b>",
        "",
        f"🔁 模型映射：<b>{len(mp)}</b> 条",
        f"🧾 模型元数据绑定：<b>{len(bindings)}</b> 条",
        f"🗜 压缩模型：<code>{ui.escape_html(compact)}</code>",
        f"🧩 分段目标：<code>{compact_rescue.chunk_target_tokens():,}</code> tokens",
        "",
        "<i>模型映射按模型名全局生效，不再区分 Anthropic / OpenAI & Grok 入口。</i>",
    ]
    return "\n".join(lines)


def _overview_kb() -> dict:
    return ui.inline_kb([
        [ui.btn("🔁 模型映射", f"map:line:{_code_of_line(model_mapping.GLOBAL_MAPPING_LINE)}")],
        [ui.btn("🧾 模型元数据", "map:meta")],
        [ui.btn("🗜 压缩模型设置", "map:compact:0")],
        [ui.btn("◀ 返回主菜单", "menu:main")],
    ])


def show(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    ui.edit(chat_id, message_id, _overview_text(), reply_markup=_overview_kb())


def send_new(chat_id: int) -> None:
    ui.send(chat_id, _overview_text(), reply_markup=_overview_kb())


# ─── Level 2 单条 line 的管理页 ────────────────────────────────────

def _line_text(line: str) -> str:
    label = model_mapping.INGRESS_LABEL[line]
    default = model_mapping.get_default_model(line)
    mp = model_mapping.get_ingress_map(line)
    out = [
        f"{_line_body_icon(line)} <b>{ui.escape_html(label)}</b>",
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
        f"{_line_body_icon(line)} <b>映射条目 · "
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
        f"{_line_body_icon(line)} <b>修改别名 · "
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
        f"{_line_body_label(line)}\n"
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
        f"{_line_body_icon(line)} <b>修改真实模型 · "
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
        f"{_line_body_icon(line)} <b>新增映射 · "
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
        f"{_line_body_icon(line)} <b>新增映射 · "
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
        f"{_line_body_icon(line)} <b>设置默认模型 · "
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
        f"{_line_body_label(line)}\n"
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


# ─── 模型元数据绑定 / 压缩模型 ─────────────────────────────────────

def _fmt_bool(value) -> str:
    return "✅" if value is True else "❌" if value is False else "—"


def _binding_tag(*, scope: str | None, model: str, outbound: str | None = None) -> str:
    return ui.register_code(json.dumps({
        "scope": scope, "model": model, "outbound": outbound,
    }, ensure_ascii=False, separators=(",", ":")))


def _binding_selection(code: str) -> dict | None:
    raw = ui.resolve_code(code)
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict) or not str(value.get("model") or "").strip():
        return None
    return value


def _metadata_text(page: int = 0) -> str:
    bindings = model_metadata.list_bindings()
    defaults = sum(1 for item in bindings if item.scope_key is None)
    scoped = len(bindings) - defaults
    total_pages = max(1, (len(bindings) + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = min(max(0, page), total_pages - 1)
    lines = [
        "🧾 <b>模型元数据</b>", "",
        f"默认绑定：<b>{defaults}</b> 条",
        f"专属绑定：<b>{scoped}</b> 条", "",
    ]
    if not bindings:
        lines.append("<i>暂无有效绑定。可先自动同步默认绑定，或添加渠道/账户专属绑定。</i>")
    else:
        for binding in bindings[page * _PAGE_SIZE:(page + 1) * _PAGE_SIZE]:
            kind = "默认" if binding.scope_key is None else "专属"
            scope = "" if binding.scope_key is None else f" · {binding.scope_key}"
            lines.append(
                f"• <b>{kind}</b><code>{ui.escape_html(scope)}</code>\n"
                f"↳ <code>{ui.escape_html(binding.client_visible_model)}</code> → "
                f"<code>{ui.escape_html(binding.target)}</code>"
            )
        if total_pages > 1:
            lines.append(f"<i>第 {page + 1}/{total_pages} 页</i>")
    lines += ["", "<i>上下文、能力与价格均实时取自绑定指向的 models.dev 记录。</i>"]
    return "\n".join(lines)


def _metadata_kb(page: int = 0) -> dict:
    rows = [
        [ui.btn("🔄 自动同步元数据", "map:meta_sync")],
        [ui.btn("➕ 添加专属绑定", "map:meta_scope:0")],
    ]
    bindings = model_metadata.list_bindings()
    total_pages = max(1, (len(bindings) + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = min(max(0, page), total_pages - 1)
    for binding in bindings[page * _PAGE_SIZE:(page + 1) * _PAGE_SIZE]:
        code = _binding_tag(
            scope=binding.scope_key,
            model=binding.client_visible_model,
            outbound=binding.outbound_model,
        )
        prefix = "默认" if binding.scope_key is None else "专属"
        rows.append([ui.btn(
            f"🧾 {prefix} · {binding.client_visible_model}",
            f"map:meta_item:{code}",
        )])
    nav = []
    if page > 0:
        nav.append(ui.btn("◀", f"map:meta_page:{page - 1}"))
    if page + 1 < total_pages:
        nav.append(ui.btn("▶", f"map:meta_page:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([ui.btn("◀ 返回模型管理", "map:show")])
    return ui.inline_kb(rows)


def _show_metadata(chat_id: int, message_id: int, cb_id: str, page: int = 0) -> None:
    ui.answer_cb(cb_id)
    ui.edit(
        chat_id, message_id, _metadata_text(page),
        reply_markup=_metadata_kb(page),
    )


def _sync_metadata(chat_id: int, message_id: int, cb_id: str) -> None:
    try:
        result = model_metadata.auto_sync_metadata()
    except Exception as exc:
        ui.answer_cb(cb_id, "同步失败")
        ui.edit(
            chat_id, message_id,
            f"❌ <b>自动同步元数据失败</b>\n\n<code>{ui.escape_html(str(exc))}</code>",
            reply_markup=ui.inline_kb([[ui.btn("◀ 返回模型元数据", "map:meta")]]),
        )
        return
    ui.answer_cb(cb_id, "✅ 同步完成")
    lines = [
        "🔄 <b>自动同步元数据完成</b>", "",
        f"扫描去重模型：<b>{result['scanned']}</b>",
        f"新增默认绑定：<b>{len(result['created'])}</b>",
        f"更新默认绑定：<b>{len(result['updated'])}</b>",
        f"未变化：<b>{len(result['unchanged'])}</b>",
        f"未匹配：<b>{len(result['unmatched'])}</b>",
    ]
    if result["unmatched"]:
        preview = "、".join(result["unmatched"][:15])
        lines += ["", f"未匹配：<code>{ui.escape_html(preview)}</code>"]
        if len(result["unmatched"]) > 15:
            lines.append(f"<i>另有 {len(result['unmatched']) - 15} 个未显示</i>")
    lines += ["", "<i>专属绑定未被修改。</i>"]
    ui.edit(
        chat_id, message_id, "\n".join(lines),
        reply_markup=ui.inline_kb([[ui.btn("◀ 返回模型元数据", "map:meta")]]),
    )


def _scope_inventory() -> list[tuple[str, str, list[model_metadata.ModelInventoryItem]]]:
    grouped: dict[str, list[model_metadata.ModelInventoryItem]] = {}
    for item in model_metadata.inventory_items():
        grouped.setdefault(item.scope_key, []).append(item)
    return [
        (scope, values[0].scope_label, values)
        for scope, values in sorted(grouped.items())
    ]


def _show_scope_picker(chat_id: int, message_id: int, cb_id: str, page: int) -> None:
    scopes = _scope_inventory()
    page = max(0, page)
    total_pages = max(1, (len(scopes) + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = min(page, total_pages - 1)
    rows = []
    for scope, label, values in scopes[page * _PAGE_SIZE:(page + 1) * _PAGE_SIZE]:
        code = _binding_tag(scope=scope, model="__scope__")
        icon = "🔐" if values[0].scope_type == "oauth" else "🔌"
        rows.append([ui.btn(f"{icon} {label} ({len(values)})", f"map:meta_models:{code}:0")])
    nav = []
    if page > 0:
        nav.append(ui.btn("◀", f"map:meta_scope:{page - 1}"))
    if page + 1 < total_pages:
        nav.append(ui.btn("▶", f"map:meta_scope:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([ui.btn("❌ 取消", "map:meta")])
    ui.answer_cb(cb_id)
    ui.edit(
        chat_id, message_id,
        "🧾 <b>添加专属元数据绑定</b>\n\n第一步：选择 OAuth 账户或 API 渠道。",
        reply_markup=ui.inline_kb(rows),
    )


def _show_scope_models(
    chat_id: int, message_id: int, cb_id: str, scope_code: str, page: int,
) -> None:
    selection = _binding_selection(scope_code)
    if not selection:
        ui.answer_cb(cb_id, "会话已过期"); return
    scope = str(selection.get("scope") or "")
    items = [item for item in model_metadata.inventory_items() if item.scope_key == scope]
    unique = {(item.client_visible_model, item.outbound_model): item for item in items}
    models = list(unique.values())
    total_pages = max(1, (len(models) + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = min(max(0, page), total_pages - 1)
    rows = []
    for item in models[page * _PAGE_SIZE:(page + 1) * _PAGE_SIZE]:
        code = _binding_tag(
            scope=scope, model=item.client_visible_model, outbound=item.outbound_model,
        )
        label = item.client_visible_model
        if item.outbound_model != item.client_visible_model:
            label += f" → {item.outbound_model}"
        rows.append([ui.btn(label, f"map:meta_providers:{code}:0")])
    nav = []
    if page > 0:
        nav.append(ui.btn("◀", f"map:meta_models:{scope_code}:{page - 1}"))
    if page + 1 < total_pages:
        nav.append(ui.btn("▶", f"map:meta_models:{scope_code}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([ui.btn("◀ 返回账户/渠道", "map:meta_scope:0")])
    ui.answer_cb(cb_id)
    ui.edit(
        chat_id, message_id,
        f"🧾 <b>添加专属绑定</b>\n\nScope：<code>{ui.escape_html(scope)}</code>\n"
        "第二步：选择该 scope 内的客户端可见模型。",
        reply_markup=ui.inline_kb(rows),
    )


def _show_provider_picker(
    chat_id: int, message_id: int, cb_id: str, selection_code: str, page: int,
) -> None:
    selection = _binding_selection(selection_code)
    if not selection:
        ui.answer_cb(cb_id, "会话已过期"); return
    providers = model_pricing.catalog_providers()
    total_pages = max(1, (len(providers) + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = min(max(0, page), total_pages - 1)
    rows = []
    for provider in providers[page * _PAGE_SIZE:(page + 1) * _PAGE_SIZE]:
        provider_code = ui.register_code(f"models-provider:{provider['id']}")
        rows.append([ui.btn(
            f"{provider['name']} ({provider['id']})",
            f"map:meta_catalog:{selection_code}:{provider_code}:0",
        )])
    nav = []
    if page > 0:
        nav.append(ui.btn("◀", f"map:meta_providers:{selection_code}:{page - 1}"))
    if page + 1 < total_pages:
        nav.append(ui.btn("▶", f"map:meta_providers:{selection_code}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([ui.btn("❌ 取消", "map:meta")])
    ui.answer_cb(cb_id)
    ui.edit(
        chat_id, message_id,
        f"🧾 <b>选择 models.dev Provider</b>\n\n"
        f"模型：<code>{ui.escape_html(str(selection['model']))}</code>\n"
        "第三步：精确选择 Provider，不做名称推断。",
        reply_markup=ui.inline_kb(rows),
    )


def _provider_from_code(code: str) -> str | None:
    raw = ui.resolve_code(code)
    prefix = "models-provider:"
    return raw[len(prefix):] if isinstance(raw, str) and raw.startswith(prefix) else None


def _show_catalog_models(
    chat_id: int, message_id: int, cb_id: str,
    selection_code: str, provider_code: str, page: int,
) -> None:
    selection = _binding_selection(selection_code)
    provider = _provider_from_code(provider_code)
    if not selection or not provider:
        ui.answer_cb(cb_id, "会话已过期"); return
    models = model_pricing.catalog_provider_models(provider)
    total_pages = max(1, (len(models) + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = min(max(0, page), total_pages - 1)
    rows = []
    for item in models[page * _PAGE_SIZE:(page + 1) * _PAGE_SIZE]:
        target_code = ui.register_code(f"models-target:{item['key']}")
        rows.append([ui.btn(
            f"{item['name']} ({item['id']})",
            f"map:meta_save:{selection_code}:{target_code}",
        )])
    nav = []
    if page > 0:
        nav.append(ui.btn("◀", f"map:meta_catalog:{selection_code}:{provider_code}:{page - 1}"))
    if page + 1 < total_pages:
        nav.append(ui.btn("▶", f"map:meta_catalog:{selection_code}:{provider_code}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([ui.btn("◀ 返回 Provider", f"map:meta_providers:{selection_code}:0")])
    ui.answer_cb(cb_id)
    ui.edit(
        chat_id, message_id,
        f"🧾 <b>选择 models.dev 模型</b>\n\nProvider：<code>{ui.escape_html(provider)}</code>\n"
        "第四步：选择精确模型并保存绑定。",
        reply_markup=ui.inline_kb(rows),
    )


def _save_binding(
    chat_id: int, message_id: int, cb_id: str,
    selection_code: str, target_code: str,
) -> None:
    selection = _binding_selection(selection_code)
    raw_target = ui.resolve_code(target_code)
    prefix = "models-target:"
    if not selection or not isinstance(raw_target, str) or not raw_target.startswith(prefix):
        ui.answer_cb(cb_id, "会话已过期"); return
    target = raw_target[len(prefix):]
    scope = str(selection.get("scope") or "").strip() or None
    outbound = str(selection.get("outbound") or "").strip() or None
    try:
        model_metadata.set_binding(
            str(selection["model"]), target,
            scope_key=scope, outbound_model=outbound, source="manual",
        )
    except ValueError as exc:
        ui.answer_cb(cb_id, str(exc)); return
    ui.answer_cb(cb_id, "✅ 已保存绑定")
    code = _binding_tag(scope=scope, model=str(selection["model"]), outbound=outbound)
    _show_meta_item(chat_id, message_id, "-", code)


def _binding_from_selection(selection: dict) -> model_metadata.MetadataBinding | None:
    scope = str(selection.get("scope") or "").strip() or None
    model = str(selection.get("model") or "").strip()
    for binding in model_metadata.list_bindings():
        if binding.scope_key == scope and binding.client_visible_model == model:
            return binding
    return None


def _show_meta_item(chat_id: int, message_id: int, cb_id: str, code: str) -> None:
    selection = _binding_selection(code)
    binding = _binding_from_selection(selection) if selection else None
    if binding is None:
        ui.answer_cb(cb_id, "该绑定已不存在或目录记录不可用")
        _show_metadata(chat_id, message_id, "-")
        return
    meta = binding.metadata
    cost = meta.get("cost") if isinstance(meta.get("cost"), dict) else {}
    lines = [
        "🧾 <b>模型元数据绑定详情</b>", "",
        f"类型：<b>{'专属' if binding.scope_key else '默认'}</b>",
        f"Scope：<code>{ui.escape_html(binding.scope_key or '全局默认')}</code>",
        f"客户端模型：<code>{ui.escape_html(binding.client_visible_model)}</code>",
        f"出站模型：<code>{ui.escape_html(binding.outbound_model or '由路由决定')}</code>",
        f"models.dev：<code>{ui.escape_html(binding.target)}</code>",
        f"来源：<code>{ui.escape_html(binding.source)}</code>", "",
        f"上下文：<code>{ui.escape_html(str(meta.get('contextWindow', '—')))}</code>",
        f"最大输出：<code>{ui.escape_html(str(meta.get('maxOutputTokens', '—')))}</code>",
        f"图片输入：{_fmt_bool(meta.get('vision'))}",
        f"推理：{_fmt_bool(meta.get('reasoning'))}",
        f"输入/输出价格：<code>{ui.escape_html(str(cost.get('input', '—')))}</code> / "
        f"<code>{ui.escape_html(str(cost.get('output', '—')))}</code> USD / 1M",
    ]
    update_code = _binding_tag(
        scope=binding.scope_key, model=binding.client_visible_model,
        outbound=binding.outbound_model,
    )
    kb = ui.inline_kb([
        [ui.btn("✏ 更新绑定", f"map:meta_providers:{update_code}:0")],
        [ui.btn("🗑 删除绑定", f"map:meta_del:{update_code}")],
        [ui.btn("◀ 返回元数据", "map:meta")],
    ])
    ui.answer_cb(cb_id)
    ui.edit(chat_id, message_id, "\n".join(lines), reply_markup=kb)


def _ask_meta_delete(chat_id: int, message_id: int, cb_id: str, code: str) -> None:
    selection = _binding_selection(code)
    binding = _binding_from_selection(selection) if selection else None
    if binding is None:
        ui.answer_cb(cb_id, "该绑定已不存在"); return
    ui.answer_cb(cb_id)
    ui.edit(
        chat_id, message_id,
        f"确认删除{'专属' if binding.scope_key else '默认'}元数据绑定？\n\n"
        f"<code>{ui.escape_html(binding.client_visible_model)}</code> → "
        f"<code>{ui.escape_html(binding.target)}</code>",
        reply_markup=ui.confirm_kb(
            confirm_callback=f"map:meta_del_ok:{code}",
            cancel_callback=f"map:meta_item:{code}",
        ),
    )


def _meta_delete(chat_id: int, message_id: int, cb_id: str, code: str) -> None:
    selection = _binding_selection(code)
    if not selection:
        ui.answer_cb(cb_id, "会话已过期"); return
    removed = model_metadata.delete_binding(
        str(selection["model"]),
        scope_key=str(selection.get("scope") or "").strip() or None,
    )
    ui.answer_cb(cb_id, "✅ 已删除" if removed else "未命中")
    _show_metadata(chat_id, message_id, "-")


def _compression_text() -> str:
    selected = model_metadata.get_compression_model() or "(未设置)"
    binding = model_metadata.resolve_binding(selected) if selected != "(未设置)" else None
    status = binding.target if binding else "等待按实际路由解析有效绑定"
    return "\n".join([
        "🗜 <b>压缩模型设置</b>", "",
        f"当前模型：<code>{ui.escape_html(selected)}</code>",
        f"默认元数据：<code>{ui.escape_html(status)}</code>",
        f"分段目标：<code>{compact_rescue.chunk_target_tokens():,}</code> tokens", "",
        "<i>运行时按实际 scope 解析专属绑定，未命中再用默认绑定。</i>",
    ])


def _compression_models() -> list[str]:
    return sorted({item.client_visible_model for item in model_metadata.inventory_items()})


def _show_compression(chat_id: int, message_id: int, cb_id: str, page: int = 0) -> None:
    models = _compression_models()
    total_pages = max(1, (len(models) + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = min(max(0, page), total_pages - 1)
    current = model_metadata.get_compression_model()
    rows = []
    for model in models[page * _PAGE_SIZE:(page + 1) * _PAGE_SIZE]:
        code = ui.register_code(f"compact-model:{model}")
        rows.append([ui.btn(
            f"{'✅ ' if model == current else ''}{model}",
            f"map:compact_pick:{code}",
        )])
    nav = []
    if page > 0:
        nav.append(ui.btn("◀", f"map:compact:{page - 1}"))
    if page + 1 < total_pages:
        nav.append(ui.btn("▶", f"map:compact:{page + 1}"))
    if nav:
        rows.append(nav)
    if current:
        rows.append([ui.btn("🗑 清除压缩模型", "map:compact_clear")])
    rows.append([ui.btn("◀ 返回模型管理", "map:show")])
    ui.answer_cb(cb_id)
    ui.edit(chat_id, message_id, _compression_text(), reply_markup=ui.inline_kb(rows))


def _pick_compression(chat_id: int, message_id: int, cb_id: str, code: str) -> None:
    raw = ui.resolve_code(code)
    prefix = "compact-model:"
    if not isinstance(raw, str) or not raw.startswith(prefix):
        ui.answer_cb(cb_id, "会话已过期"); return
    model_metadata.set_compression_model(raw[len(prefix):])
    ui.answer_cb(cb_id, "✅ 已设置压缩模型")
    _show_compression(chat_id, message_id, "-")


def _clear_compression(chat_id: int, message_id: int, cb_id: str) -> None:
    changed = model_metadata.clear_compression_model()
    ui.answer_cb(cb_id, "✅ 已清除" if changed else "当前未设置")
    _show_compression(chat_id, message_id, "-")


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
    if action == "meta":
        _show_metadata(chat_id, message_id, cb_id)
        return True
    if action == "meta_sync":
        _sync_metadata(chat_id, message_id, cb_id)
        return True
    if action == "meta_page":
        try:
            page = int(parts[2])
        except (IndexError, ValueError):
            page = 0
        _show_metadata(chat_id, message_id, cb_id, page)
        return True
    if action == "meta_scope":
        try:
            page = int(parts[2])
        except (IndexError, ValueError):
            page = 0
        _show_scope_picker(chat_id, message_id, cb_id, page)
        return True
    if action == "meta_models" and len(parts) >= 4:
        try:
            page = int(parts[3])
        except ValueError:
            page = 0
        _show_scope_models(chat_id, message_id, cb_id, parts[2], page)
        return True
    if action == "meta_providers" and len(parts) >= 4:
        try:
            page = int(parts[3])
        except ValueError:
            page = 0
        _show_provider_picker(chat_id, message_id, cb_id, parts[2], page)
        return True
    if action == "meta_catalog" and len(parts) >= 5:
        try:
            page = int(parts[4])
        except ValueError:
            page = 0
        _show_catalog_models(
            chat_id, message_id, cb_id, parts[2], parts[3], page,
        )
        return True
    if action == "meta_save" and len(parts) >= 4:
        _save_binding(chat_id, message_id, cb_id, parts[2], parts[3])
        return True
    if action == "meta_item" and len(parts) >= 3:
        _show_meta_item(chat_id, message_id, cb_id, parts[2])
        return True
    if action == "meta_del" and len(parts) >= 3:
        _ask_meta_delete(chat_id, message_id, cb_id, parts[2])
        return True
    if action == "meta_del_ok" and len(parts) >= 3:
        _meta_delete(chat_id, message_id, cb_id, parts[2])
        return True
    if action == "compact":
        try:
            page = int(parts[2])
        except (IndexError, ValueError):
            page = 0
        _show_compression(chat_id, message_id, cb_id, page)
        return True
    if action == "compact_pick" and len(parts) >= 3:
        _pick_compression(chat_id, message_id, cb_id, parts[2])
        return True
    if action == "compact_clear":
        _clear_compression(chat_id, message_id, cb_id)
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
