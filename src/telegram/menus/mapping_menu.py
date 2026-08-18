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
import math
import threading
from typing import Mapping, Optional

from ... import compact_rescue, model_mapping, model_metadata, model_pricing
from .. import states, ui


# ─── 常量 ─────────────────────────────────────────────────────────

_PAGE_SIZE = 10   # 真实模型按钮每页条数
_METADATA_SYNC_LOCK = threading.Lock()
_METADATA_SYNC_RUNNING = False

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
}


def _line_body_label(line: str) -> str:
    if line == "anthropic":
        return f"{ui.family_tag('anthropic')} (/v1/messages)"
    if line == "openai-chat":
        return f"{ui.family_tag('openai')} Chat (/v1/chat/completions)"
    if line == "openai-responses":
        return f"{ui.family_tag('openai')} Responses (/v1/responses)"
    return f"{_LINE_ICON.get(line, '🔁')} {ui.escape_html(model_mapping.INGRESS_LABEL[line])}"


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
        f"<i>模型映射按模型名全局生效，不再区分 {ui.family_tag('anthropic')} / {ui.family_tag('openai')} 入口。</i>",
    ]
    return "\n".join(lines)


def _overview_kb() -> dict:
    return ui.inline_kb([
        [
            ui.btn("🔁 模型映射", f"map:line:{_code_of_line(model_mapping.GLOBAL_MAPPING_LINE)}"),
            ui.btn("🧾 模型元数据", "map:meta"),
        ],
        [
            ui.btn("🗜 压缩模型", "map:compact:0"),
            ui.btn("◀ 返回主菜单", "menu:main"),
        ],
    ])


def show(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    ui.edit(chat_id, message_id, _overview_text(), reply_markup=_overview_kb())


def send_new(chat_id: int) -> None:
    ui.send(chat_id, _overview_text(), reply_markup=_overview_kb())


# ─── Level 2 单条 line 的管理页 ────────────────────────────────────

def _line_text(line: str) -> str:
    default = model_mapping.get_default_model(line)
    mp = model_mapping.get_ingress_map(line)
    out = [
        _line_body_label(line),
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
        f"{_line_body_label(line)} · <b>映射条目</b>\n\n"
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
        f"{_line_body_label(line)} · <b>修改别名</b>\n\n"
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
        f"{_line_body_label(line)} · <b>修改真实模型</b>\n\n"
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
        f"{_line_body_label(line)} · <b>新增映射</b>\n\n"
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
        f"{_line_body_label(line)} · <b>新增映射</b>\n\n"
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
        f"{_line_body_label(line)} · <b>设置默认模型</b>\n\n"
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

_META_PAGE_SIZE = 6
_META_DEFAULT = "d"
_META_SCOPED = "s"
_SCOPE_OAUTH = "o"
_SCOPE_API = "a"


def _fmt_bool(value) -> str:
    return "✅" if value is True else "❌" if value is False else "—"


def _fmt_limit(value) -> str:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return "—"
    return ui.fmt_tokens(parsed) if parsed > 0 else "—"


def _fmt_catalog_price(value) -> str:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return "—"
    if not math.isfinite(parsed) or parsed < 0:
        return "—"
    text = f"{parsed:,.6f}".rstrip("0").rstrip(".")
    return f"${text or '0'}"


def _metadata_limit_line(meta: Mapping[str, object], *, indent: str = "   ") -> str:
    parts = [f"上下文 {_fmt_limit(meta.get('contextWindow'))}"]
    trigger = _fmt_limit(meta.get("compactTriggerTokens"))
    if trigger != "—":
        parts.append(f"压缩阈值 {trigger}")
    parts.append(f"最大输出 {_fmt_limit(meta.get('maxOutputTokens'))}")
    return indent + " · ".join(parts)


def _short_button_label(value: object, limit: int = 24) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _grid_rows(buttons: list[dict], columns: int = 2) -> list[list[dict]]:
    return [buttons[index:index + columns] for index in range(0, len(buttons), columns)]


def _page_slice(items: list, page: int, page_size: int = _META_PAGE_SIZE) -> tuple[int, int, list]:
    total_pages = max(1, (len(items) + page_size - 1) // page_size)
    current = min(max(0, page), total_pages - 1)
    start = current * page_size
    return current, total_pages, items[start:start + page_size]


def _pager_row(page: int, total_pages: int, callback_for_page) -> list[dict]:
    return [
        ui.btn(
            "⬅ 上一页" if page > 0 else "◁ 上一页",
            callback_for_page(page - 1) if page > 0 else "map:meta_noop",
        ),
        ui.btn(f"{page + 1}/{total_pages}", "map:meta_noop"),
        ui.btn(
            "下一页 ➡" if page + 1 < total_pages else "下一页 ▷",
            callback_for_page(page + 1) if page + 1 < total_pages else "map:meta_noop",
        ),
    ]


def _meta_bottom_row(back_label: str, back_callback: str) -> list[dict]:
    return [
        ui.btn("🏠 返回主菜单", "map:meta_main"),
        ui.btn(f"◀ {back_label}", back_callback),
    ]


def _binding_tag(
    *, scope: str | None, model: str, outbound: str | None = None, **context,
) -> str:
    payload = {"scope": scope, "model": model, "outbound": outbound}
    payload.update({key: value for key, value in context.items() if value is not None})
    return ui.register_code(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ))


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


def _selection_tag(selection: Mapping[str, object], **updates) -> str:
    context = {
        key: value for key, value in selection.items()
        if key not in {"scope", "model", "outbound"}
    }
    context.update(updates)
    return _binding_tag(
        scope=str(selection.get("scope") or "").strip() or None,
        model=str(selection.get("model") or ""),
        outbound=str(selection.get("outbound") or "").strip() or None,
        **context,
    )


def _scope_label_map() -> dict[str, str]:
    return {scope: label for scope, label, _values in _scope_inventory()}


def _scope_label(scope: str | None, labels: Mapping[str, str] | None = None) -> str:
    if not scope:
        return "默认元数据"
    values = labels if labels is not None else _scope_label_map()
    return values.get(scope, scope)


def _scope_provider(scope_type: str, scope_key: str) -> str:
    if scope_type != "oauth":
        return ""
    parts = scope_key.split(":", 2)
    return parts[1] if len(parts) > 1 else ""


def _scope_icon(scope_type: str, scope_key: str) -> str:
    provider = _scope_provider(scope_type, scope_key)
    return ui.provider_custom_emoji_html(provider) if provider else "🔌"


def _metadata_item_lines(
    binding: model_metadata.MetadataBinding, number: int, *, scoped: bool,
    scope_labels: Mapping[str, str] | None = None,
) -> list[str]:
    meta = binding.metadata if isinstance(binding.metadata, Mapping) else {}
    name = str(meta.get("name") or binding.client_visible_model)
    if scoped:
        return [
            f"{number}. <b>{ui.escape_html(_scope_label(binding.scope_key, scope_labels))} · {ui.escape_html(name)}</b>",
            f"   客户端：<code>{ui.escape_html(binding.client_visible_model)}</code>",
            f"   出站：<code>{ui.escape_html(binding.outbound_model or '由路由决定')}</code>",
            f"   绑定：<code>{ui.escape_html(binding.target)}</code>",
        ]
    cost = meta.get("cost") if isinstance(meta.get("cost"), Mapping) else {}
    lines = [
        f"{number}. <b>{ui.escape_html(name)}</b>",
        f"   <code>{ui.escape_html(binding.client_visible_model)}</code> → "
        f"<code>{ui.escape_html(binding.target)}</code>",
        _metadata_limit_line(meta),
    ]
    if cost.get("input") is not None or cost.get("output") is not None:
        lines.append(
            f"   输入 {_fmt_catalog_price(cost.get('input'))}/M · "
            f"输出 {_fmt_catalog_price(cost.get('output'))}/M"
        )
    return lines


def _metadata_text(
    view: str = _META_DEFAULT, page: int = 0,
    scope_labels: Mapping[str, str] | None = None,
) -> str:
    view = _META_SCOPED if view == _META_SCOPED else _META_DEFAULT
    all_bindings = model_metadata.list_bindings()
    defaults = sum(1 for item in all_bindings if item.scope_key is None)
    scoped_count = len(all_bindings) - defaults
    bindings = [
        item for item in all_bindings
        if (item.scope_key is not None) == (view == _META_SCOPED)
    ]
    page, total_pages, visible = _page_slice(bindings, page)
    status = model_pricing.catalog_status()
    title = "专属元数据" if view == _META_SCOPED else "默认元数据"
    lines = [
        "🧾 <b>模型元数据</b>", "",
        f"models.dev 已加载 <b>{int(status.get('metadata_models') or 0):,}</b> 个模型 · "
        f"<b>{int(status.get('providers') or 0):,}</b> 个提供商",
        f"默认元数据 <b>{defaults}</b> · 专属元数据 <b>{scoped_count}</b>", "",
        f"<b>{title}</b> · 第 {page + 1}/{total_pages} 页 · 共 {len(bindings)} 条",
        "──────────────────",
    ]
    if not visible:
        hint = (
            "暂无专属元数据。可为某个 OAuth 账户或 API 渠道单独绑定。"
            if view == _META_SCOPED
            else "暂无默认元数据。可先自动同步已有模型的官方元数据。"
        )
        lines.append(f"<i>{hint}</i>")
    else:
        for offset, binding in enumerate(visible, start=page * _META_PAGE_SIZE + 1):
            if lines[-1] != "──────────────────":
                lines.append("")
            lines.extend(_metadata_item_lines(
                binding, offset, scoped=view == _META_SCOPED,
                scope_labels=scope_labels,
            ))
    return "\n".join(lines)


def _metadata_kb(
    view: str = _META_DEFAULT, page: int = 0,
    scope_labels: Mapping[str, str] | None = None,
) -> dict:
    view = _META_SCOPED if view == _META_SCOPED else _META_DEFAULT
    all_bindings = model_metadata.list_bindings()
    defaults = [item for item in all_bindings if item.scope_key is None]
    scoped = [item for item in all_bindings if item.scope_key is not None]
    bindings = scoped if view == _META_SCOPED else defaults
    page, total_pages, visible = _page_slice(bindings, page)
    rows: list[list[dict]] = [[
        ui.btn(
            f"默认元数据 · {len(defaults)}{' ✓' if view == _META_DEFAULT else ''}",
            f"map:meta_view:{_META_DEFAULT}:0",
        ),
        ui.btn(
            f"专属元数据 · {len(scoped)}{' ✓' if view == _META_SCOPED else ''}",
            f"map:meta_view:{_META_SCOPED}:0",
        ),
    ]]
    item_buttons: list[dict] = []
    for offset, binding in enumerate(visible, start=page * _META_PAGE_SIZE + 1):
        code = _binding_tag(
            scope=binding.scope_key,
            model=binding.client_visible_model,
            outbound=binding.outbound_model,
            flow="detail", view=view, page=page,
        )
        name = str(binding.metadata.get("name") or binding.client_visible_model)
        label = (
            f"{offset}. {_short_button_label(_scope_label(binding.scope_key, scope_labels), 10)} · "
            f"{_short_button_label(name, 12)}"
            if binding.scope_key else f"{offset}. {_short_button_label(name)}"
        )
        item_buttons.append(ui.btn(label, f"map:meta_item:{code}"))
    rows.extend(_grid_rows(item_buttons))
    rows.append(_pager_row(
        page, total_pages, lambda target: f"map:meta_view:{view}:{target}",
    ))
    rows.append([
        ui.btn("🔄 自动同步", "map:meta_sync"),
        ui.btn("➕ 新增专属", f"map:meta_scope:{_SCOPE_OAUTH}:0"),
    ])
    rows.append(_meta_bottom_row("返回模型管理", "map:show"))
    return ui.inline_kb(rows)


def _show_metadata(
    chat_id: int, message_id: int, cb_id: str,
    view: str = _META_DEFAULT, page: int = 0,
) -> None:
    states.pop_state(chat_id)
    scope_labels = _scope_label_map() if view == _META_SCOPED else {}
    ui.answer_cb(cb_id)
    ui.edit(
        chat_id, message_id, _metadata_text(view, page, scope_labels),
        reply_markup=_metadata_kb(view, page, scope_labels),
    )


def _sync_result_tag(result: Mapping[str, object]) -> str:
    payload = {
        key: [str(value) for value in result.get(key, [])]
        for key in ("created", "updated", "unchanged", "unmatched")
    }
    payload["scanned"] = int(result.get("scanned") or 0)
    payload["catalog"] = str(result.get("catalog") or "local")
    return ui.register_code("metadata-sync:" + json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ))


def _sync_result_from_code(code: str) -> dict | None:
    raw = ui.resolve_code(code)
    prefix = "metadata-sync:"
    if not isinstance(raw, str) or not raw.startswith(prefix):
        return None
    try:
        result = json.loads(raw[len(prefix):])
    except (TypeError, ValueError):
        return None
    return result if isinstance(result, dict) else None


def _sync_result_text(result: Mapping[str, object]) -> str:
    catalog_text = (
        "已更新本地目录"
        if result.get("catalog") == "updated"
        else "使用本地目录"
    )
    return "\n".join([
        "🔄 <b>自动同步完成</b>", "",
        f"models.dev：<b>{catalog_text}</b>",
        f"扫描模型 <b>{int(result.get('scanned') or 0)}</b> 个",
        f"新增默认元数据 <b>{len(result.get('created') or [])}</b> 个",
        f"更新默认元数据 <b>{len(result.get('updated') or [])}</b> 个",
        f"保持不变 <b>{len(result.get('unchanged') or [])}</b> 个",
        f"未找到官方同名模型 <b>{len(result.get('unmatched') or [])}</b> 个",
        "", "<i>专属元数据没有被修改。</i>",
    ])


def _sync_result_kb(result: Mapping[str, object], code: str) -> dict:
    def action(label: str, kind: str, count: int) -> dict:
        callback = f"map:meta_sync_list:{code}:{kind}:0" if count else "map:meta_noop"
        return ui.btn(f"{label} · {count}", callback)

    created = len(result.get("created") or [])
    updated = len(result.get("updated") or [])
    unmatched = len(result.get("unmatched") or [])
    scanned = int(result.get("scanned") or 0)
    return ui.inline_kb([
        [action("查看新增", "c", created), action("查看更新", "u", updated)],
        [action("查看未匹配", "m", unmatched), action("查看全部", "a", scanned)],
        _meta_bottom_row("返回模型元数据", "map:meta"),
    ])


def _show_sync_result(
    chat_id: int, message_id: int, cb_id: str, code: str,
) -> None:
    result = _sync_result_from_code(code)
    if result is None:
        ui.answer_cb(cb_id, "同步结果已过期")
        _show_metadata(chat_id, message_id, "-")
        return
    ui.answer_cb(cb_id)
    ui.edit(
        chat_id, message_id, _sync_result_text(result),
        reply_markup=_sync_result_kb(result, code),
    )


def _perform_metadata_sync() -> dict[str, object]:
    """Refresh the local bundle when possible, then sync from local data only."""

    catalog_updated = False
    try:
        catalog_updated = model_pricing.refresh_remote_catalog_sync()
    except Exception as exc:
        # A network failure must not block metadata sync.  Keep the previous
        # last-known-good bundle and continue below.
        print(f"[metadata] models.dev refresh failed; using local catalog: {exc}")

    # On success this reloads the bundle just written above.  On failure it
    # reloads the previous last-known-good bundle.  Missing/invalid cache leaves
    # the already initialized bundled catalog untouched.
    model_pricing.reload_local_catalog()
    result: dict[str, object] = model_metadata.auto_sync_metadata()
    result["catalog"] = "updated" if catalog_updated else "local"
    return result


def _start_metadata_sync_worker(target) -> None:
    threading.Thread(
        target=target, daemon=True, name="tg-metadata-sync",
    ).start()


def _sync_metadata(chat_id: int, message_id: int, cb_id: str) -> None:
    global _METADATA_SYNC_RUNNING
    with _METADATA_SYNC_LOCK:
        if _METADATA_SYNC_RUNNING:
            ui.answer_cb(cb_id, "元数据正在同步，请稍候")
            return
        _METADATA_SYNC_RUNNING = True

    ui.answer_cb(cb_id, "正在更新 models.dev…")

    def worker() -> None:
        global _METADATA_SYNC_RUNNING
        try:
            result = _perform_metadata_sync()
            code = _sync_result_tag(result)
            ui.edit(
                chat_id, message_id, _sync_result_text(result),
                reply_markup=_sync_result_kb(result, code),
            )
        except Exception as exc:
            ui.edit(
                chat_id, message_id,
                f"❌ <b>自动同步元数据失败</b>\n\n<code>{ui.escape_html(str(exc))}</code>",
                reply_markup=ui.inline_kb([
                    _meta_bottom_row("返回模型元数据", "map:meta"),
                ]),
            )
        finally:
            with _METADATA_SYNC_LOCK:
                _METADATA_SYNC_RUNNING = False

    _start_metadata_sync_worker(worker)


def _sync_entries(result: Mapping[str, object], kind: str) -> list[tuple[str, str]]:
    groups = {
        "c": ("新增", [str(value) for value in result.get("created", [])]),
        "u": ("更新", [str(value) for value in result.get("updated", [])]),
        "m": ("未匹配", [str(value) for value in result.get("unmatched", [])]),
    }
    if kind in groups:
        label, values = groups[kind]
        return [(label, value) for value in values]
    entries: list[tuple[str, str]] = []
    for label, key in (
        ("新增", "created"), ("更新", "updated"),
        ("未变化", "unchanged"), ("未匹配", "unmatched"),
    ):
        entries.extend((label, str(value)) for value in result.get(key, []))
    return entries


def _show_sync_list(
    chat_id: int, message_id: int, cb_id: str,
    result_code: str, kind: str, page: int,
) -> None:
    result = _sync_result_from_code(result_code)
    if result is None:
        ui.answer_cb(cb_id, "同步结果已过期")
        _show_metadata(chat_id, message_id, "-")
        return
    entries = _sync_entries(result, kind)
    page, total_pages, visible = _page_slice(entries, page)
    title = {"c": "新增", "u": "更新", "m": "未匹配", "a": "全部"}.get(kind, "全部")
    lines = [
        f"🔄 <b>自动同步 · {title}</b>", "",
        f"第 {page + 1}/{total_pages} 页 · 共 {len(entries)} 个模型",
        "──────────────────",
    ]
    buttons: list[dict] = []
    for offset, (status, model) in enumerate(visible, start=page * _META_PAGE_SIZE + 1):
        binding = None if status == "未匹配" else model_metadata.resolve_binding(model)
        lines.append(f"{offset}. <b>{ui.escape_html(model)}</b> · {status}")
        if binding:
            lines.append(f"   → <code>{ui.escape_html(binding.target)}</code>")
            code = _binding_tag(
                scope=None, model=model, flow="detail", view=_META_DEFAULT,
                page=0, back="sync", sync=result_code,
                sync_kind=kind, sync_page=page,
            )
            buttons.append(ui.btn(
                f"{offset}. {_short_button_label(model)}", f"map:meta_item:{code}",
            ))
        else:
            name_code = ui.register_code(f"metadata-unmatched:{model}")
            buttons.append(ui.btn(
                f"{offset}. {_short_button_label(model)}",
                f"map:meta_unmatched:{name_code}",
            ))
        if offset != page * _META_PAGE_SIZE + len(visible):
            lines.append("")
    if not visible:
        lines.append("<i>这一分类暂无模型。</i>")
    rows = _grid_rows(buttons)
    rows.append(_pager_row(
        page, total_pages,
        lambda target: f"map:meta_sync_list:{result_code}:{kind}:{target}",
    ))
    rows.append(_meta_bottom_row("返回同步结果", f"map:meta_sync_result:{result_code}"))
    ui.answer_cb(cb_id)
    ui.edit(chat_id, message_id, "\n".join(lines), reply_markup=ui.inline_kb(rows))


def _show_unmatched_info(cb_id: str, code: str) -> None:
    raw = ui.resolve_code(code)
    prefix = "metadata-unmatched:"
    model = raw[len(prefix):] if isinstance(raw, str) and raw.startswith(prefix) else "该模型"
    ui.answer_cb(
        cb_id,
        f"{model} 没有官方同名元数据，可通过“新增专属”手动选择。",
        show_alert=True,
    )


def _scope_inventory(
    scope_type: str | None = None,
) -> list[tuple[str, str, list[model_metadata.ModelInventoryItem]]]:
    grouped: dict[str, list[model_metadata.ModelInventoryItem]] = {}
    for item in model_metadata.inventory_items():
        if scope_type and item.scope_type != scope_type:
            continue
        grouped.setdefault(item.scope_key, []).append(item)
    return sorted(
        [
            (scope, values[0].scope_label, sorted(
                values, key=lambda item: (item.client_visible_model.casefold(), item.outbound_model.casefold()),
            ))
            for scope, values in grouped.items()
        ],
        key=lambda item: (item[1].casefold(), item[0].casefold()),
    )


def _scope_kind_from_type(scope_type: str) -> str:
    return _SCOPE_API if scope_type == "api" else _SCOPE_OAUTH


def _show_scope_picker(
    chat_id: int, message_id: int, cb_id: str,
    kind: str = _SCOPE_OAUTH, page: int = 0,
) -> None:
    states.pop_state(chat_id)
    kind = _SCOPE_API if kind == _SCOPE_API else _SCOPE_OAUTH
    oauth_scopes = _scope_inventory("oauth")
    api_scopes = _scope_inventory("api")
    scopes = api_scopes if kind == _SCOPE_API else oauth_scopes
    page, total_pages, visible = _page_slice(scopes, page)
    scoped_counts: dict[str, int] = {}
    for binding in model_metadata.list_bindings():
        if binding.scope_key:
            scoped_counts[binding.scope_key] = scoped_counts.get(binding.scope_key, 0) + 1
    title = "API 渠道" if kind == _SCOPE_API else "OAuth 账户"
    lines = [
        "🧾 <b>新增专属元数据 · 1/3</b>", "",
        "请选择需要单独设置元数据的 OAuth 账户或 API 渠道。", "",
        f"<b>{title}</b> · 第 {page + 1}/{total_pages} 页 · 共 {len(scopes)} 个",
        "──────────────────",
    ]
    item_buttons: list[dict] = []
    for offset, (scope, label, values) in enumerate(visible, start=page * _META_PAGE_SIZE + 1):
        icon = _scope_icon(values[0].scope_type, scope)
        lines.extend([
            f"{offset}. {icon} <b>{ui.escape_html(label)}</b>",
            f"   {len(values)} 个模型 · {scoped_counts.get(scope, 0)} 个专属元数据",
        ])
        if offset != page * _META_PAGE_SIZE + len(visible):
            lines.append("")
        code = _binding_tag(
            scope=scope, model="__scope__", flow="add",
            scope_kind=kind, scope_page=page,
        )
        provider = _scope_provider(values[0].scope_type, scope)
        item_buttons.append(ui.btn(
            f"{offset}. {_short_button_label(label, 20)}",
            f"map:meta_models:{code}:0",
            icon_custom_emoji_id=(
                ui.provider_custom_emoji_id(provider) if provider else None
            ),
        ))
    if not visible:
        lines.append(f"<i>当前没有已配置的{title}。</i>")
    rows: list[list[dict]] = [[
        ui.btn(
            f"OAuth 账户 · {len(oauth_scopes)}{' ✓' if kind == _SCOPE_OAUTH else ''}",
            f"map:meta_scope:{_SCOPE_OAUTH}:0",
        ),
        ui.btn(
            f"API 渠道 · {len(api_scopes)}{' ✓' if kind == _SCOPE_API else ''}",
            f"map:meta_scope:{_SCOPE_API}:0",
        ),
    ]]
    rows.extend(_grid_rows(item_buttons))
    rows.append(_pager_row(
        page, total_pages, lambda target: f"map:meta_scope:{kind}:{target}",
    ))
    rows.append(_meta_bottom_row("返回模型元数据", "map:meta"))
    ui.answer_cb(cb_id)
    ui.edit(chat_id, message_id, "\n".join(lines), reply_markup=ui.inline_kb(rows))


def _scope_selection_back(selection: Mapping[str, object]) -> str:
    kind = str(selection.get("scope_kind") or _SCOPE_OAUTH)
    page = int(selection.get("scope_page") or 0)
    return f"map:meta_scope:{kind}:{page}"


def _show_scope_models(
    chat_id: int, message_id: int, cb_id: str, scope_code: str, page: int,
) -> None:
    selection = _binding_selection(scope_code)
    if not selection:
        ui.answer_cb(cb_id, "会话已过期")
        return
    scope = str(selection.get("scope") or "")
    items = [item for item in model_metadata.inventory_items() if item.scope_key == scope]
    unique = {(item.client_visible_model, item.outbound_model): item for item in items}
    models = sorted(
        unique.values(), key=lambda item: (item.client_visible_model.casefold(), item.outbound_model.casefold()),
    )
    page, total_pages, visible = _page_slice(models, page)
    label = models[0].scope_label if models else _scope_label(scope)
    lines = [
        "🧾 <b>新增专属元数据 · 2/3</b>", "",
        f"账户/渠道：<b>{ui.escape_html(label)}</b>",
        "请选择需要单独绑定元数据的模型。", "",
        f"第 {page + 1}/{total_pages} 页 · 共 {len(models)} 个模型",
        "──────────────────",
    ]
    item_buttons: list[dict] = []
    for offset, item in enumerate(visible, start=page * _META_PAGE_SIZE + 1):
        effective = model_metadata.resolve_binding(
            item.client_visible_model,
            scope_key=scope,
            outbound_model=item.outbound_model,
        )
        if effective is None:
            current = "当前没有可用元数据"
        elif effective.scope_key == scope:
            current = f"当前专属元数据：{effective.target}"
        else:
            current = f"当前默认元数据：{effective.target}"
        lines.extend([
            f"{offset}. <b>{ui.escape_html(item.client_visible_model)}</b>",
            f"   出站：<code>{ui.escape_html(item.outbound_model)}</code>",
            f"   {ui.escape_html(current)}",
        ])
        if offset != page * _META_PAGE_SIZE + len(visible):
            lines.append("")
        code = _binding_tag(
            scope=scope,
            model=item.client_visible_model,
            outbound=item.outbound_model,
            flow="add",
            scope_kind=selection.get("scope_kind") or _scope_kind_from_type(item.scope_type),
            scope_page=int(selection.get("scope_page") or 0),
            model_page=page,
        )
        item_buttons.append(ui.btn(
            f"{offset}. {_short_button_label(item.client_visible_model)}",
            f"map:meta_candidates:{code}:0",
        ))
    if not visible:
        lines.append("<i>这个账户或渠道当前没有可选模型。</i>")
    rows = _grid_rows(item_buttons)
    rows.append(_pager_row(
        page, total_pages,
        lambda target: f"map:meta_models:{scope_code}:{target}",
    ))
    rows.append(_meta_bottom_row("返回账户/渠道", _scope_selection_back(selection)))
    ui.answer_cb(cb_id)
    ui.edit(chat_id, message_id, "\n".join(lines), reply_markup=ui.inline_kb(rows))


def _candidate_models(selection: Mapping[str, object]) -> list[dict[str, str]]:
    client_model = str(selection.get("model") or "").strip().lower()
    outbound_model = str(selection.get("outbound") or "").strip().lower()
    names = {name for name in (client_model, outbound_model) if name}
    leaves = {name.rsplit("/", 1)[-1] for name in names}
    scope = str(selection.get("scope") or "").strip()

    def compact(value: object) -> str:
        return "".join(char for char in str(value or "").casefold() if char.isalnum())

    scope_tail = scope.split(":", 1)[-1] if ":" in scope else scope
    scope_hints = {compact(scope_tail), compact(_scope_label(scope))} if scope else set()
    scope_hints.discard("")
    official: set[str] = set()
    for name in names | leaves:
        target = model_pricing.canonical_official_model(name)
        if target:
            official.add(target)
    matched: list[dict[str, str]] = []
    for item in model_pricing.catalog_models():
        model_id = item["id"].lower()
        leaf = model_id.rsplit("/", 1)[-1]
        if model_id not in names and leaf not in leaves:
            continue
        row = dict(item)
        provider_values = {
            compact(item.get("provider_id")), compact(item.get("provider_name")),
        }
        provider_matches_scope = any(
            provider == hint or (len(provider) >= 4 and provider in hint)
            for provider in provider_values if provider
            for hint in scope_hints
        )
        row["official"] = "1" if item["key"] in official else "0"
        row["rank"] = str(
            0 if item["key"] in official else
            1 if provider_matches_scope else
            2 if outbound_model and model_id == outbound_model else
            3 if client_model and model_id == client_model else 4
        )
        matched.append(row)
    return sorted(
        matched,
        key=lambda item: (
            int(item["rank"]), item["provider_name"].casefold(),
            item["name"].casefold(), item["key"],
        ),
    )


def _catalog_item_lines(item: Mapping[str, str], number: int) -> list[str]:
    meta = model_pricing.catalog_metadata(item["key"]) or {}
    cost = meta.get("cost") if isinstance(meta.get("cost"), Mapping) else {}
    provider = item.get("provider_name") or item.get("provider_id") or "models.dev"
    official = item.get("official") == "1"
    heading = f"⭐ {provider} 官方" if official else provider
    lines = [
        f"{number}. <b>{ui.escape_html(heading)}</b>",
        f"   <code>{ui.escape_html(item['key'])}</code>",
        _metadata_limit_line(meta),
    ]
    if cost.get("input") is not None or cost.get("output") is not None:
        lines.append(
            f"   输入 {_fmt_catalog_price(cost.get('input'))}/M · "
            f"输出 {_fmt_catalog_price(cost.get('output'))}/M"
        )
    return lines


def _candidate_back_callback(selection_code: str, selection: Mapping[str, object]) -> str:
    if selection.get("flow") == "detail":
        return f"map:meta_item:{selection_code}"
    scope_code = _binding_tag(
        scope=str(selection.get("scope") or ""),
        model="__scope__",
        flow="add",
        scope_kind=selection.get("scope_kind") or _SCOPE_OAUTH,
        scope_page=int(selection.get("scope_page") or 0),
    )
    return f"map:meta_models:{scope_code}:{int(selection.get('model_page') or 0)}"


def _show_candidate_picker(
    chat_id: int, message_id: int, cb_id: str,
    selection_code: str, page: int = 0,
) -> None:
    states.pop_state(chat_id)
    selection = _binding_selection(selection_code)
    if not selection:
        ui.answer_cb(cb_id, "会话已过期")
        return
    candidates = _candidate_models(selection)
    page, total_pages, visible = _page_slice(candidates, page)
    selection_code = _selection_tag(selection, candidate_page=page)
    selection = _binding_selection(selection_code) or selection
    scope = str(selection.get("scope") or "").strip()
    lines = [
        "🧾 <b>新增专属元数据 · 3/3</b>" if scope else "🧾 <b>更换模型元数据</b>", "",
    ]
    if scope:
        lines.append(f"账户/渠道：<b>{ui.escape_html(_scope_label(scope))}</b>")
    lines.extend([
        f"客户端模型：<code>{ui.escape_html(str(selection.get('model') or ''))}</code>",
        f"出站模型：<code>{ui.escape_html(str(selection.get('outbound') or '由路由决定'))}</code>", "",
        f"找到 <b>{len(candidates)}</b> 个同名元数据候选 · 第 {page + 1}/{total_pages} 页",
        "──────────────────",
    ])
    item_buttons: list[dict] = []
    for offset, item in enumerate(visible, start=page * _META_PAGE_SIZE + 1):
        lines.extend(_catalog_item_lines(item, offset))
        if offset != page * _META_PAGE_SIZE + len(visible):
            lines.append("")
        target_code = ui.register_code(f"models-target:{item['key']}")
        provider = item["provider_name"]
        prefix = "⭐ " if item.get("official") == "1" else ""
        item_buttons.append(ui.btn(
            f"{prefix}{offset}. {_short_button_label(provider)}",
            f"map:meta_save:{selection_code}:{target_code}",
        ))
    if not visible:
        lines.append("<i>没有找到同名候选，可按名称筛选或浏览全部提供商。</i>")
    rows = _grid_rows(item_buttons)
    rows.append(_pager_row(
        page, total_pages,
        lambda target: f"map:meta_candidates:{selection_code}:{target}",
    ))
    rows.append([
        ui.btn("🔎 选择其他模型", f"map:meta_search:{selection_code}"),
        ui.btn("🏢 浏览提供商", f"map:meta_providers:{selection_code}:0"),
    ])
    rows.append(_meta_bottom_row(
        "返回模型选择" if scope else "返回元数据详情",
        _candidate_back_callback(selection_code, selection),
    ))
    ui.answer_cb(cb_id)
    ui.edit(chat_id, message_id, "\n".join(lines), reply_markup=ui.inline_kb(rows))


def _start_catalog_search(
    chat_id: int, message_id: int, cb_id: str, selection_code: str,
) -> None:
    selection = _binding_selection(selection_code)
    if not selection:
        ui.answer_cb(cb_id, "会话已过期")
        return
    states.set_state(chat_id, f"meta_catalog_search:{selection_code}")
    ui.answer_cb(cb_id)
    ui.edit(
        chat_id, message_id,
        "🔎 <b>选择其他 models.dev 模型</b>\n\n"
        "请输入模型名称或提供商名称，例如：\n"
        "<code>gpt-5.6</code>、<code>openrouter</code>、<code>claude opus</code>",
        reply_markup=ui.inline_kb([
            _meta_bottom_row(
                "返回同名候选",
                f"map:meta_candidates:{selection_code}:{int(selection.get('candidate_page') or 0)}",
            ),
        ]),
    )


def _catalog_search(query: str) -> list[dict[str, str]]:
    needle = " ".join(str(query or "").strip().lower().split())
    if not needle:
        return []
    tokens = needle.split()
    results: list[dict[str, str]] = []
    for item in model_pricing.catalog_models():
        haystacks = [
            item["key"].lower(), item["id"].lower(), item["name"].lower(),
            item["provider_id"].lower(), item["provider_name"].lower(),
        ]
        combined = " ".join(haystacks)
        if not all(token in combined for token in tokens):
            continue
        row = dict(item)
        exact = needle in {item["key"].lower(), item["id"].lower(), item["name"].lower()}
        prefix = any(value.startswith(needle) for value in haystacks)
        row["rank"] = "0" if exact else "1" if prefix else "2"
        row["official"] = "1" if model_pricing.canonical_official_model(item["id"]) == item["key"] else "0"
        results.append(row)
    return sorted(results, key=lambda item: (
        int(item["rank"]), item["provider_name"].casefold(),
        item["name"].casefold(), item["key"],
    ))


def _search_query_tag(query: str) -> str:
    return ui.register_code(f"models-query:{query}")


def _search_query_from_code(code: str) -> str | None:
    raw = ui.resolve_code(code)
    prefix = "models-query:"
    return raw[len(prefix):] if isinstance(raw, str) and raw.startswith(prefix) else None


def _search_results_payload(
    selection_code: str, query: str, page: int,
) -> tuple[str, dict]:
    results = _catalog_search(query)
    page, total_pages, visible = _page_slice(results, page)
    lines = [
        "🔎 <b>models.dev 筛选结果</b>", "",
        f"关键词：<code>{ui.escape_html(query)}</code>",
        f"找到 {len(results)} 个模型 · 第 {page + 1}/{total_pages} 页",
        "──────────────────",
    ]
    buttons: list[dict] = []
    for offset, item in enumerate(visible, start=page * _META_PAGE_SIZE + 1):
        lines.extend(_catalog_item_lines(item, offset))
        if offset != page * _META_PAGE_SIZE + len(visible):
            lines.append("")
        target_code = ui.register_code(f"models-target:{item['key']}")
        buttons.append(ui.btn(
            f"{offset}. {_short_button_label(item['provider_name'], 9)} · "
            f"{_short_button_label(item['name'], 13)}",
            f"map:meta_save:{selection_code}:{target_code}",
        ))
    if not visible:
        lines.append("<i>没有匹配结果，可换一个关键词或浏览提供商。</i>")
    query_code = _search_query_tag(query)
    rows = _grid_rows(buttons)
    rows.append(_pager_row(
        page, total_pages,
        lambda target: f"map:meta_search_results:{selection_code}:{query_code}:{target}",
    ))
    rows.append([
        ui.btn("🔎 重新筛选", f"map:meta_search:{selection_code}"),
        ui.btn("🏢 浏览提供商", f"map:meta_providers:{selection_code}:0"),
    ])
    selection = _binding_selection(selection_code) or {}
    rows.append(_meta_bottom_row(
        "返回同名候选",
        f"map:meta_candidates:{selection_code}:{int(selection.get('candidate_page') or 0)}",
    ))
    return "\n".join(lines), ui.inline_kb(rows)


def _send_catalog_search_results(chat_id: int, selection_code: str, query: str) -> None:
    text, markup = _search_results_payload(selection_code, query, 0)
    ui.send(chat_id, text, reply_markup=markup)


def _on_catalog_search_input(chat_id: int, action: str, text: str) -> None:
    selection_code = action.split(":", 1)[1] if ":" in action else ""
    if not _binding_selection(selection_code):
        states.pop_state(chat_id)
        ui.send(chat_id, "❌ 筛选会话已过期，请重新进入模型元数据菜单。")
        return
    query = " ".join(str(text or "").strip().split())
    if not query:
        ui.send(chat_id, "❌ 关键词不能为空，请重新输入：")
        return
    if len(query) > 80:
        ui.send(chat_id, "❌ 关键词过长，请控制在 80 个字符以内：")
        return
    states.pop_state(chat_id)
    _send_catalog_search_results(chat_id, selection_code, query)


def _show_catalog_search_results(
    chat_id: int, message_id: int, cb_id: str,
    selection_code: str, query_code: str, page: int,
) -> None:
    query = _search_query_from_code(query_code)
    if query is None or not _binding_selection(selection_code):
        ui.answer_cb(cb_id, "筛选会话已过期")
        return
    text, markup = _search_results_payload(selection_code, query, page)
    ui.answer_cb(cb_id)
    ui.edit(chat_id, message_id, text, reply_markup=markup)


def _show_provider_picker(
    chat_id: int, message_id: int, cb_id: str, selection_code: str, page: int,
) -> None:
    selection = _binding_selection(selection_code)
    if not selection:
        ui.answer_cb(cb_id, "会话已过期")
        return
    providers = model_pricing.catalog_providers()
    model_counts: dict[str, int] = {}
    for item in model_pricing.catalog_models():
        provider = item["provider_id"]
        model_counts[provider] = model_counts.get(provider, 0) + 1
    page, total_pages, visible = _page_slice(providers, page)
    selection_code = _selection_tag(selection, provider_page=page)
    selection = _binding_selection(selection_code) or selection
    lines = [
        "🏢 <b>选择元数据提供商</b>", "",
        f"第 {page + 1}/{total_pages} 页 · 共 {len(providers)} 个提供商",
        "──────────────────",
    ]
    buttons: list[dict] = []
    for offset, provider in enumerate(visible, start=page * _META_PAGE_SIZE + 1):
        count = model_counts.get(provider["id"], 0)
        lines.append(
            f"{offset}. <b>{ui.escape_html(provider['name'])}</b> · {count} 个模型"
        )
        provider_code = ui.register_code(f"models-provider:{provider['id']}")
        buttons.append(ui.btn(
            f"{offset}. {_short_button_label(provider['name'])}",
            f"map:meta_catalog:{selection_code}:{provider_code}:0",
        ))
    rows = _grid_rows(buttons)
    rows.append(_pager_row(
        page, total_pages,
        lambda target: f"map:meta_providers:{selection_code}:{target}",
    ))
    rows.append(_meta_bottom_row(
        "返回同名候选",
        f"map:meta_candidates:{selection_code}:{int(selection.get('candidate_page') or 0)}",
    ))
    ui.answer_cb(cb_id)
    ui.edit(chat_id, message_id, "\n".join(lines), reply_markup=ui.inline_kb(rows))


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
        ui.answer_cb(cb_id, "会话已过期")
        return
    models = model_pricing.catalog_provider_models(provider)
    provider_info = next(
        (item for item in model_pricing.catalog_providers() if item["id"] == provider),
        {"id": provider, "name": provider},
    )
    page, total_pages, visible = _page_slice(models, page)
    lines = [
        "🧾 <b>选择 models.dev 模型</b>", "",
        f"提供商：<b>{ui.escape_html(provider_info['name'])}</b>",
        f"第 {page + 1}/{total_pages} 页 · 共 {len(models)} 个模型",
        "──────────────────",
    ]
    buttons: list[dict] = []
    for offset, item in enumerate(visible, start=page * _META_PAGE_SIZE + 1):
        descriptor = {
            **item,
            "provider_id": provider,
            "provider_name": provider_info["name"],
            "official": "0",
        }
        lines.extend(_catalog_item_lines(descriptor, offset))
        if offset != page * _META_PAGE_SIZE + len(visible):
            lines.append("")
        target_code = ui.register_code(f"models-target:{item['key']}")
        buttons.append(ui.btn(
            f"{offset}. {_short_button_label(item['name'])}",
            f"map:meta_save:{selection_code}:{target_code}",
        ))
    if not visible:
        lines.append("<i>这个提供商当前没有模型记录。</i>")
    rows = _grid_rows(buttons)
    rows.append(_pager_row(
        page, total_pages,
        lambda target: f"map:meta_catalog:{selection_code}:{provider_code}:{target}",
    ))
    rows.append(_meta_bottom_row(
        "返回提供商",
        f"map:meta_providers:{selection_code}:{int(selection.get('provider_page') or 0)}",
    ))
    ui.answer_cb(cb_id)
    ui.edit(chat_id, message_id, "\n".join(lines), reply_markup=ui.inline_kb(rows))


def _save_binding(
    chat_id: int, message_id: int, cb_id: str,
    selection_code: str, target_code: str,
) -> None:
    selection = _binding_selection(selection_code)
    raw_target = ui.resolve_code(target_code)
    prefix = "models-target:"
    if not selection or not isinstance(raw_target, str) or not raw_target.startswith(prefix):
        ui.answer_cb(cb_id, "会话已过期")
        return
    target = raw_target[len(prefix):]
    scope = str(selection.get("scope") or "").strip() or None
    outbound = str(selection.get("outbound") or "").strip() or None
    try:
        model_metadata.set_binding(
            str(selection["model"]), target,
            scope_key=scope, outbound_model=outbound, source="manual",
        )
    except ValueError as exc:
        ui.answer_cb(cb_id, str(exc))
        return
    ui.answer_cb(cb_id, "✅ 已保存元数据")
    if selection.get("flow") == "detail":
        detail_code = selection_code
    else:
        detail_code = _binding_tag(
            scope=scope, model=str(selection["model"]), outbound=outbound,
            flow="detail", view=_META_SCOPED if scope else _META_DEFAULT, page=0,
        )
    _show_meta_item(chat_id, message_id, "-", detail_code)


def _binding_from_selection(selection: dict) -> model_metadata.MetadataBinding | None:
    scope = str(selection.get("scope") or "").strip() or None
    model = str(selection.get("model") or "").strip()
    for binding in model_metadata.list_bindings():
        if binding.scope_key == scope and binding.client_visible_model == model:
            return binding
    return None


def _source_label(source: str) -> str:
    return {
        "auto": "自动同步", "manual": "手动绑定",
        "legacy": "旧配置迁移", "config": "配置文件",
        "cursor.AvailableModels": "Cursor 账号自动同步",
    }.get(str(source or "").strip(), str(source or "未知"))


def _detail_back_callback(selection: Mapping[str, object]) -> str:
    if selection.get("back") == "sync" and selection.get("sync"):
        return (
            f"map:meta_sync_list:{selection['sync']}:"
            f"{selection.get('sync_kind') or 'a'}:{int(selection.get('sync_page') or 0)}"
        )
    view = _META_SCOPED if selection.get("view") == _META_SCOPED else _META_DEFAULT
    return f"map:meta_view:{view}:{int(selection.get('page') or 0)}"


def _raw_price_lines(cost: Mapping[str, object], *, indent: str = "") -> list[str]:
    lines = [
        f"{indent}输入 {_fmt_catalog_price(cost.get('input'))} · "
        f"输出 {_fmt_catalog_price(cost.get('output'))}",
    ]
    if cost.get("cache_write") is not None or cost.get("cache_read") is not None:
        lines.append(
            f"{indent}缓存写入 {_fmt_catalog_price(cost.get('cache_write'))} · "
            f"缓存读取 {_fmt_catalog_price(cost.get('cache_read'))}"
        )
    return lines


def _long_context_lines(raw: Mapping[str, object]) -> list[str]:
    cost = raw.get("cost") if isinstance(raw.get("cost"), Mapping) else {}
    tiers = cost.get("tiers") if isinstance(cost, Mapping) else None
    result: list[str] = []
    if isinstance(tiers, list):
        for tier in tiers:
            if not isinstance(tier, Mapping):
                continue
            tier_meta = tier.get("tier") if isinstance(tier.get("tier"), Mapping) else {}
            if tier_meta.get("type") != "context" or not tier_meta.get("size"):
                continue
            result.append(
                f"超过 {_fmt_limit(tier_meta.get('size'))} Prompt Tokens："
            )
            result.extend(_raw_price_lines(tier))
    legacy = cost.get("context_over_200k") if isinstance(cost, Mapping) else None
    if not result and isinstance(legacy, Mapping):
        result.append("超过 200K Prompt Tokens：")
        result.extend(_raw_price_lines(legacy))
    return result


def _show_meta_item(chat_id: int, message_id: int, cb_id: str, code: str) -> None:
    selection = _binding_selection(code)
    binding = _binding_from_selection(selection) if selection else None
    if binding is None:
        ui.answer_cb(cb_id, "该元数据已不存在或目录记录不可用")
        _show_metadata(chat_id, message_id, "-")
        return
    meta = binding.metadata if isinstance(binding.metadata, Mapping) else {}
    raw = model_pricing.catalog_model(binding.target) or {}
    cost = meta.get("cost") if isinstance(meta.get("cost"), Mapping) else {}
    name = str(meta.get("name") or binding.client_visible_model)
    kind = (
        "Cursor 自动元数据" if binding.source == "cursor.AvailableModels"
        else "专属元数据" if binding.scope_key else "默认元数据"
    )
    lines = [
        "🧾 <b>模型元数据详情</b>", "",
        f"<b>{ui.escape_html(kind)}</b> · {ui.escape_html(_source_label(binding.source))}",
        f"<b>{ui.escape_html(name)}</b>",
        f"<code>{ui.escape_html(binding.client_visible_model)}</code> → "
        f"<code>{ui.escape_html(binding.target)}</code>",
    ]
    if binding.scope_key:
        lines.extend([
            "",
            f"作用范围：<b>{ui.escape_html(_scope_label(binding.scope_key))}</b>",
            f"出站模型：<code>{ui.escape_html(binding.outbound_model or '由路由决定')}</code>",
            "优先级：专属元数据优先于默认元数据",
        ])
    compact_trigger = _fmt_limit(meta.get("compactTriggerTokens"))
    lines.extend([
        "", "📐 <b>模型限制</b>",
        f"上下文：{_fmt_limit(meta.get('contextWindow'))} Tokens",
    ])
    if meta.get("contextWindowMaxMode"):
        lines.append(
            f"Cursor Max 上下文：{_fmt_limit(meta.get('contextWindowMaxMode'))} Tokens"
        )
    lines.extend([
        (
            f"压缩阈值：{compact_trigger} Tokens"
            if compact_trigger != "—" else "压缩阈值：按上下文容量"
        ),
        f"最大输出：{_fmt_limit(meta.get('maxOutputTokens'))} Tokens",
    ])
    if meta.get("releaseDate"):
        lines.append(f"发布日期：{ui.escape_html(meta.get('releaseDate'))}")
    lines.extend([
        "", "🧠 <b>模型能力</b>",
        f"图片输入 {_fmt_bool(meta.get('vision'))} · 推理 {_fmt_bool(meta.get('reasoning'))}",
        f"工具调用 {_fmt_bool(meta.get('toolCall'))} · "
        f"结构化输出 {_fmt_bool(meta.get('structuredOutput'))}",
    ])
    efforts = meta.get("reasoningEfforts")
    if isinstance(efforts, list) and efforts:
        lines.append("推理强度：" + " / ".join(ui.escape_html(value) for value in efforts))
    lines.extend(["", "💵 <b>标准价格（USD / 1M Tokens）</b>"])
    if cost.get("input") is None and cost.get("output") is None:
        lines.append("当前记录未提供 Token 价格")
    else:
        lines.extend(_raw_price_lines(cost))
    long_context = _long_context_lines(raw)
    if long_context:
        lines.extend(["", "📏 <b>长上下文价格</b>", *long_context])
    update_code = _binding_tag(
        scope=binding.scope_key,
        model=binding.client_visible_model,
        outbound=binding.outbound_model,
        **{key: value for key, value in (selection or {}).items() if key not in {"scope", "model", "outbound"}},
    )
    if binding.source == "cursor.AvailableModels":
        kb = ui.inline_kb([
            [ui.btn("🔄 到 OAuth 账户刷新模型", "menu:oauth")],
            _meta_bottom_row("返回元数据列表", _detail_back_callback(selection or {})),
        ])
    else:
        kb = ui.inline_kb([
            [
                ui.btn("✏ 更换绑定", f"map:meta_candidates:{update_code}:0"),
                ui.btn("🗑 删除绑定", f"map:meta_del:{update_code}"),
            ],
            _meta_bottom_row("返回元数据列表", _detail_back_callback(selection or {})),
        ])
    ui.answer_cb(cb_id)
    ui.edit(chat_id, message_id, "\n".join(lines), reply_markup=kb)


def _ask_meta_delete(chat_id: int, message_id: int, cb_id: str, code: str) -> None:
    selection = _binding_selection(code)
    binding = _binding_from_selection(selection) if selection else None
    if binding is None:
        ui.answer_cb(cb_id, "该元数据已不存在")
        return
    lines = [
        "⚠️ <b>删除模型元数据</b>", "",
        "即将删除：", "",
        f"<b>{'专属元数据' if binding.scope_key else '默认元数据'}</b>",
    ]
    if binding.scope_key:
        lines.append(f"{ui.escape_html(_scope_label(binding.scope_key))} · {ui.escape_html(binding.client_visible_model)}")
    else:
        lines.append(ui.escape_html(binding.client_visible_model))
    lines.append(f"→ <code>{ui.escape_html(binding.target)}</code>")
    if binding.scope_key:
        fallback = model_metadata.resolve_binding(binding.client_visible_model)
        lines.append("")
        if fallback:
            lines.extend([
                "删除后将回退使用默认元数据：",
                f"<code>{ui.escape_html(fallback.target)}</code>",
            ])
        else:
            lines.append("删除后该模型将没有可用元数据。")
    ui.answer_cb(cb_id)
    ui.edit(
        chat_id, message_id, "\n".join(lines),
        reply_markup=ui.inline_kb([[
            ui.btn("✅ 确认删除", f"map:meta_del_ok:{code}"),
            ui.btn("取消返回", f"map:meta_item:{code}"),
        ]]),
    )


def _meta_delete(chat_id: int, message_id: int, cb_id: str, code: str) -> None:
    selection = _binding_selection(code)
    if not selection:
        ui.answer_cb(cb_id, "会话已过期")
        return
    scope = str(selection.get("scope") or "").strip() or None
    removed = model_metadata.delete_binding(
        str(selection["model"]), scope_key=scope,
    )
    ui.answer_cb(cb_id, "✅ 已删除" if removed else "未命中")
    view = _META_SCOPED if scope else _META_DEFAULT
    page = int(selection.get("page") or 0)
    _show_metadata(chat_id, message_id, "-", view, page)


def _compression_text(page: int, total_pages: int, total_models: int) -> str:
    selected = model_metadata.get_compression_model() or "(未设置)"
    binding = model_metadata.resolve_binding(selected) if selected != "(未设置)" else None
    status = binding.target if binding else "等待按实际路由解析有效绑定"
    if binding:
        trigger = model_metadata.compact_trigger_tokens(selected)
        trigger_text = f"{trigger:,} tokens" if trigger is not None else "按上下文容量"
    else:
        trigger_text = "等待按实际路由解析"
    return "\n".join([
        "🗜 <b>压缩模型设置</b>", "",
        f"当前模型：<code>{ui.escape_html(selected)}</code>",
        f"默认元数据：<code>{ui.escape_html(status)}</code>",
        f"默认压缩阈值：<code>{ui.escape_html(trigger_text)}</code>",
        f"分段目标：<code>{compact_rescue.chunk_target_tokens():,}</code> tokens", "",
        f"<b>可选模型</b> · 第 {page + 1}/{total_pages} 页 · 共 {total_models} 个", "",
        "<i>运行时按实际 scope 解析专属绑定，未命中再用默认绑定。</i>",
    ])


def _compression_models() -> list[str]:
    return sorted({item.client_visible_model for item in model_metadata.inventory_items()})


def _show_compression(chat_id: int, message_id: int, cb_id: str, page: int = 0) -> None:
    models = _compression_models()
    page, total_pages, visible = _page_slice(models, page)
    current = model_metadata.get_compression_model()
    item_buttons: list[dict] = []
    for offset, model in enumerate(visible, start=page * _META_PAGE_SIZE + 1):
        code = ui.register_code(f"compact-model:{model}")
        item_buttons.append(ui.btn(
            f"{'✅ ' if model == current else ''}{offset}. {_short_button_label(model)}",
            f"map:compact_pick:{code}:{page}",
        ))
    rows = _grid_rows(item_buttons)
    rows.append(_pager_row(
        page, total_pages, lambda target: f"map:compact:{target}",
    ))
    rows.append([
        ui.btn("清除压缩模型", f"map:compact_clear:{page}"),
        ui.btn("返回模型管理", "map:show"),
    ])
    ui.answer_cb(cb_id)
    ui.edit(
        chat_id, message_id,
        _compression_text(page, total_pages, len(models)),
        reply_markup=ui.inline_kb(rows),
    )


def _pick_compression(
    chat_id: int, message_id: int, cb_id: str, code: str, page: int = 0,
) -> None:
    raw = ui.resolve_code(code)
    prefix = "compact-model:"
    if not isinstance(raw, str) or not raw.startswith(prefix):
        ui.answer_cb(cb_id, "会话已过期"); return
    model_metadata.set_compression_model(raw[len(prefix):])
    ui.answer_cb(cb_id, "✅ 已设置压缩模型")
    _show_compression(chat_id, message_id, "-", page)


def _clear_compression(
    chat_id: int, message_id: int, cb_id: str, page: int = 0,
) -> None:
    changed = model_metadata.clear_compression_model()
    ui.answer_cb(cb_id, "✅ 已清除" if changed else "当前未设置")
    _show_compression(chat_id, message_id, "-", page)


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
    if action == "meta_main":
        states.pop_state(chat_id)
        from . import main as main_menu
        main_menu.handle_back(chat_id, message_id, cb_id)
        return True
    if action == "meta_noop":
        ui.answer_cb(cb_id)
        return True
    if action == "meta_view" and len(parts) >= 4:
        try:
            page = int(parts[3])
        except ValueError:
            page = 0
        _show_metadata(chat_id, message_id, cb_id, parts[2], page)
        return True
    if action == "meta_page":  # 旧按钮兼容：回到默认元数据对应页
        try:
            page = int(parts[2])
        except (IndexError, ValueError):
            page = 0
        _show_metadata(chat_id, message_id, cb_id, _META_DEFAULT, page)
        return True
    if action == "meta_sync":
        _sync_metadata(chat_id, message_id, cb_id)
        return True
    if action == "meta_sync_result" and len(parts) >= 3:
        _show_sync_result(chat_id, message_id, cb_id, parts[2])
        return True
    if action == "meta_sync_list" and len(parts) >= 5:
        try:
            page = int(parts[4])
        except ValueError:
            page = 0
        _show_sync_list(chat_id, message_id, cb_id, parts[2], parts[3], page)
        return True
    if action == "meta_unmatched" and len(parts) >= 3:
        _show_unmatched_info(cb_id, parts[2])
        return True
    if action == "meta_scope":
        if len(parts) >= 4 and parts[2] in {_SCOPE_OAUTH, _SCOPE_API}:
            kind = parts[2]
            try:
                page = int(parts[3])
            except ValueError:
                page = 0
        else:  # 旧 callback: map:meta_scope:<page>
            kind = _SCOPE_OAUTH if _scope_inventory("oauth") else _SCOPE_API
            try:
                page = int(parts[2])
            except (IndexError, ValueError):
                page = 0
        _show_scope_picker(chat_id, message_id, cb_id, kind, page)
        return True
    if action == "meta_models" and len(parts) >= 4:
        try:
            page = int(parts[3])
        except ValueError:
            page = 0
        _show_scope_models(chat_id, message_id, cb_id, parts[2], page)
        return True
    if action == "meta_candidates" and len(parts) >= 4:
        try:
            page = int(parts[3])
        except ValueError:
            page = 0
        _show_candidate_picker(chat_id, message_id, cb_id, parts[2], page)
        return True
    if action == "meta_search" and len(parts) >= 3:
        _start_catalog_search(chat_id, message_id, cb_id, parts[2])
        return True
    if action == "meta_search_results" and len(parts) >= 5:
        try:
            page = int(parts[4])
        except ValueError:
            page = 0
        _show_catalog_search_results(
            chat_id, message_id, cb_id, parts[2], parts[3], page,
        )
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
        try:
            page = int(parts[3])
        except (IndexError, ValueError):
            page = 0
        _pick_compression(chat_id, message_id, cb_id, parts[2], page)
        return True
    if action == "compact_clear":
        try:
            page = int(parts[2])
        except (IndexError, ValueError):
            page = 0
        _clear_compression(chat_id, message_id, cb_id, page)
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
    if action.startswith("meta_catalog_search:"):
        _on_catalog_search_input(chat_id, action, text)
        return True
    return False
