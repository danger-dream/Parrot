"""负载均衡菜单：调度算法选择 + priority 优先级编辑。

callback_data 前缀：`lb:...`
状态机 action：
  - `lb_edit`       优先级调整草稿（按钮多选/移动）
  - `lb_bulk_input` 等待用户输入完整序号排列
  - `lb_bulk_preview` 历史兼容：批量预览待保存
"""

from __future__ import annotations

import math
import re
from typing import Optional

from ... import affinity, config, load_balancing
from ...channel import registry
from ...oauth_ids import provider_from_channel_key
from .. import states, ui


_FAMILY_NAMES = {
    "anthropic": "Anthropic 协议",
    "openai": "OpenAI、Grok 与 Cursor 协议",
}
_FAMILY_PROVIDERS = {
    "anthropic": ("claude",),
    "openai": ("openai", "xai", "cursor"),
}


def _family_label(family: str, *, rich: bool = True) -> str:
    name = _FAMILY_NAMES.get(family, family)
    providers = _FAMILY_PROVIDERS.get(family, ())
    if not rich or not providers:
        return name
    icons = " ".join(ui.provider_custom_emoji_html(provider) for provider in providers)
    return f"{icons} {ui.escape_html(name)}"


def _family_button(family: str, callback_data: str) -> dict:
    providers = _FAMILY_PROVIDERS.get(family, ())
    custom_id = ui.provider_custom_emoji_id(providers[0]) if providers else ""
    return ui.btn(
        _family_label(family, rich=False),
        callback_data,
        icon_custom_emoji_id=custom_id,
    )

_MODE_LABELS = {
    "smart": "智能调度",
    "order": "顺序调度",
    "priority": "优先级调度",
}

_MODE_DESCS = {
    "smart": "按滑动窗口评分 + 20% 探索率排序",
    "order": "按配置顺序依次尝试",
    "priority": "按用户自行设定的优先级",
}


def _channels_for_family(family: str) -> list:
    return [ch for ch in registry.all_channels() if load_balancing.family_for_channel(ch) == family]


def _normalized_keys(family: str) -> list[str]:
    live = [ch.key for ch in _channels_for_family(family)]
    return load_balancing.normalize_order_for_family(family, live)


def _channel_icon(ch) -> str:
    if ch.type == "oauth":
        provider = provider_from_channel_key(ch.key)
        if provider in {"openai", "xai", "cursor", "claude"}:
            return f"{ui.provider_custom_emoji_html(provider)} 🔐"
        return "✉ 🔐"
    return "🔀"


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


def _format_item_line(idx: int, key: str) -> str:
    ch = registry.get_channel(key)
    if ch is None:
        return f"{idx}. <code>{ui.escape_html(key)}</code> ⚠ 已不存在"
    display = ui.channel_display_name(ch.key, with_family=False)
    return (
        f"{idx}. {_channel_icon(ch)} <code>{ui.escape_html(display)}</code> "
        f"{ui.escape_html(_status_text(ch))}"
    )


def _format_order_lines(keys: list[str]) -> list[str]:
    if not keys:
        return ["<i>当前没有该协议类型的账户/渠道。</i>"]
    return [_format_item_line(i, key) for i, key in enumerate(keys, start=1)]


def _split_number_rows(n: int, max_cols: int = 6) -> list[list[int]]:
    """把 1..n 智能拆成按钮行。

    每行不超过 max_cols；行数尽量少；各行尽量均衡，避免最后一行孤零零一个。
    """
    if n <= 0:
        return []
    rows_count = math.ceil(n / max_cols)
    base = n // rows_count
    extra = n % rows_count
    rows: list[list[int]] = []
    cur = 1
    for r in range(rows_count):
        size = base + (1 if r < extra else 0)
        rows.append(list(range(cur, cur + size)))
        cur += size
    return rows


def _state_data(chat_id: int) -> Optional[dict]:
    st = states.get_state(chat_id)
    if not st:
        return None
    data = st.get("data") or {}
    if st.get("action") not in ("lb_edit", "lb_bulk_input", "lb_bulk_preview"):
        return None
    return data


def _selection_set(data: dict) -> set[int]:
    return {int(x) for x in (data.get("selected") or [])}


def _set_edit_state(chat_id: int, family: str, draft: list[str],
                    selected: Optional[set[int]] = None) -> None:
    states.set_state(chat_id, "lb_edit", {
        "family": family,
        "draft": list(draft),
        "selected": sorted(selected or []),
    })


# ─── 一级菜单 ─────────────────────────────────────────────────────

def _main_text_and_kb() -> tuple[str, dict]:
    mode = (config.get().get("channelSelection") or "smart").lower()
    lines = [
        "⚖️ <b>负载均衡</b>",
        "",
        "当前调度算法:",
    ]
    for m in ("smart", "order", "priority"):
        prefix = "✅ " if mode == m else ""
        lines.append(f"{prefix}{_MODE_LABELS[m]}（{_MODE_DESCS[m]}）")
    lines.extend(["", "请选择调度算法"])

    rows = [[
        ui.btn(f"智能调度{' √' if mode == 'smart' else ''}", "lb:mode:smart"),
        ui.btn(f"顺序调度{' √' if mode == 'order' else ''}", "lb:mode:order"),
        ui.btn(f"优先级{' √' if mode == 'priority' else ''}", "lb:mode:priority"),
    ]]
    if mode == "priority":
        lines.extend(["", "请选择要调整的协议类型"])
        rows.append([_family_button("anthropic", "lb:fam:anthropic")])
        rows.append([_family_button("openai", "lb:fam:openai")])
        rows.append([ui.btn("🧹 清除全部亲和", "lb:aff_all")])
    rows.append([ui.btn("◀ 返回主菜单", "menu:main")])
    return "\n".join(lines), ui.inline_kb(rows)


def show(chat_id: int, message_id: int, cb_id: Optional[str] = None) -> None:
    if cb_id is not None:
        ui.answer_cb(cb_id)
    text, kb = _main_text_and_kb()
    ui.edit(chat_id, message_id, text, reply_markup=kb)


def send_new(chat_id: int) -> None:
    text, kb = _main_text_and_kb()
    ui.send(chat_id, text, reply_markup=kb)


def _on_mode(chat_id: int, message_id: int, cb_id: str, mode: str) -> None:
    try:
        load_balancing.set_mode(mode)
    except Exception as exc:
        ui.answer_cb(cb_id, "切换失败", show_alert=True)
        ui.send(chat_id, f"❌ 切换失败: <code>{ui.escape_html(str(exc))}</code>")
        return
    ui.answer_cb(cb_id, f"已切换到 {_MODE_LABELS.get(mode, mode)}")
    show(chat_id, message_id)


# ─── 优先级编辑 ───────────────────────────────────────────────────

def _edit_text_and_kb(family: str, draft: list[str], selected: set[int]) -> tuple[str, dict]:
    title = f"{_family_label(family)} · 调度优先级"
    lines = [
        f"{title}",
        "",
        "当前账户/渠道:",
        *_format_order_lines(draft),
        "",
        "调整方式:",
        "请在下方先勾选要调整的账户/渠道序号",
        "然后点击上移、下移按钮完成顺序调整",
        "若调整出错可点击还原按钮还原最后一次保存状态",
        "！！！调整完成后务必点击保存设置按钮",
    ]

    rows: list[list[dict]] = []
    for nums in _split_number_rows(len(draft)):
        row = []
        for n in nums:
            label = f"{n} ✅" if n in selected else str(n)
            row.append(ui.btn(label, f"lb:sel:{n}"))
        rows.append(row)
    if draft:
        rows.append([
            ui.btn("🔝 置顶", "lb:mv:top"),
            ui.btn("🔚 置底", "lb:mv:bottom"),
            ui.btn("⬆ 上移", "lb:mv:up"),
            ui.btn("⬇ 下移", "lb:mv:down"),
        ])
    rows.append([ui.btn("还原", "lb:reset"), ui.btn("保存设置", "lb:save")])
    rows.append([ui.btn("批量设置", "lb:bulk"),
                 ui.btn("🧹 清除本协议全部亲和", f"lb:aff_fam:{family}")])
    rows.append([ui.btn("◀ 返回主菜单", "menu:main"), ui.btn("取消", "lb:cancel")])
    return ui.truncate("\n".join(lines)), ui.inline_kb(rows)


def _show_edit(chat_id: int, message_id: int, cb_id: Optional[str] = None) -> None:
    data = _state_data(chat_id)
    if cb_id is not None:
        ui.answer_cb(cb_id)
    if not data:
        show(chat_id, message_id)
        return
    family = data.get("family") or "anthropic"
    draft = list(data.get("draft") or [])
    selected = _selection_set(data)
    text, kb = _edit_text_and_kb(family, draft, selected)
    ui.edit(chat_id, message_id, text, reply_markup=kb)


def _start_family(chat_id: int, message_id: int, cb_id: str, family: str) -> None:
    if family not in load_balancing.FAMILIES:
        ui.answer_cb(cb_id, "无效协议类型")
        return
    draft = _normalized_keys(family)
    _set_edit_state(chat_id, family, draft)
    _show_edit(chat_id, message_id, cb_id)


def _toggle_select(chat_id: int, message_id: int, cb_id: str, idx_str: str) -> None:
    data = _state_data(chat_id)
    if not data:
        ui.answer_cb(cb_id, "会话已失效")
        show(chat_id, message_id)
        return
    draft = list(data.get("draft") or [])
    try:
        idx = int(idx_str)
    except ValueError:
        ui.answer_cb(cb_id, "无效序号")
        return
    if idx < 1 or idx > len(draft):
        ui.answer_cb(cb_id, "序号越界")
        return
    selected = _selection_set(data)
    if idx in selected:
        selected.remove(idx)
    else:
        selected.add(idx)
    _set_edit_state(chat_id, data.get("family") or "anthropic", draft, selected)
    _show_edit(chat_id, message_id, cb_id)


def _move_top(draft: list[str], selected: set[int]) -> list[str]:
    idxs = [i - 1 for i in sorted(selected)]
    chosen = [draft[i] for i in idxs]
    rest = [x for i, x in enumerate(draft) if i not in idxs]
    return chosen + rest


def _move_bottom(draft: list[str], selected: set[int]) -> list[str]:
    idxs = [i - 1 for i in sorted(selected)]
    chosen = [draft[i] for i in idxs]
    rest = [x for i, x in enumerate(draft) if i not in idxs]
    return rest + chosen


def _move_up(draft: list[str], selected: set[int]) -> tuple[list[str], set[int]]:
    arr = list(draft)
    sel = {i - 1 for i in selected}
    for i in range(1, len(arr)):
        if i in sel and (i - 1) not in sel:
            arr[i - 1], arr[i] = arr[i], arr[i - 1]
            sel.remove(i)
            sel.add(i - 1)
    return arr, {i + 1 for i in sel}


def _move_down(draft: list[str], selected: set[int]) -> tuple[list[str], set[int]]:
    arr = list(draft)
    sel = {i - 1 for i in selected}
    for i in range(len(arr) - 2, -1, -1):
        if i in sel and (i + 1) not in sel:
            arr[i + 1], arr[i] = arr[i], arr[i + 1]
            sel.remove(i)
            sel.add(i + 1)
    return arr, {i + 1 for i in sel}


def _move(chat_id: int, message_id: int, cb_id: str, op: str) -> None:
    data = _state_data(chat_id)
    if not data:
        ui.answer_cb(cb_id, "会话已失效")
        show(chat_id, message_id)
        return
    draft = list(data.get("draft") or [])
    selected = _selection_set(data)
    if not selected:
        ui.answer_cb(cb_id, "请先勾选序号")
        return
    if op == "top":
        new_draft = _move_top(draft, selected)
        new_sel = set(range(1, len(selected) + 1))
    elif op == "bottom":
        new_draft = _move_bottom(draft, selected)
        start = len(new_draft) - len(selected) + 1
        new_sel = set(range(start, len(new_draft) + 1))
    elif op == "up":
        new_draft, new_sel = _move_up(draft, selected)
    elif op == "down":
        new_draft, new_sel = _move_down(draft, selected)
    else:
        ui.answer_cb(cb_id, "未知移动操作")
        return
    _set_edit_state(chat_id, data.get("family") or "anthropic", new_draft, new_sel)
    _show_edit(chat_id, message_id, cb_id)


def _reset(chat_id: int, message_id: int, cb_id: str) -> None:
    data = _state_data(chat_id)
    family = (data or {}).get("family") or "anthropic"
    _set_edit_state(chat_id, family, _normalized_keys(family))
    ui.answer_cb(cb_id, "已还原最后一次保存状态")
    _show_edit(chat_id, message_id)


def _save(chat_id: int, message_id: int, cb_id: str) -> None:
    data = _state_data(chat_id)
    if not data:
        ui.answer_cb(cb_id, "会话已失效")
        show(chat_id, message_id)
        return
    family = data.get("family") or "anthropic"
    draft = list(data.get("draft") or [])
    load_balancing.save_family_order(family, draft)
    states.pop_state(chat_id)
    ui.answer_cb(cb_id, "已保存")
    ui.edit(
        chat_id, message_id,
        f"✅ 已保存 {_family_label(family)} · 调度优先级。",
        reply_markup=ui.inline_kb([
            [ui.btn("继续调整", f"lb:fam:{family}"), ui.btn("返回负载均衡", "menu:loadbalancing")],
            [ui.btn("🏠 主菜单", "menu:main")],
        ]),
    )


def _cancel(chat_id: int, message_id: int, cb_id: str) -> None:
    states.pop_state(chat_id)
    show(chat_id, message_id, cb_id)


# ─── 批量设置 ─────────────────────────────────────────────────────

def _bulk_start(chat_id: int, message_id: int, cb_id: str) -> None:
    data = _state_data(chat_id)
    if not data:
        ui.answer_cb(cb_id, "会话已失效")
        show(chat_id, message_id)
        return
    family = data.get("family") or "anthropic"
    draft = list(data.get("draft") or [])
    states.set_state(chat_id, "lb_bulk_input", {"family": family, "draft": draft})
    lines = [
        f"{_family_label(family)} · 调度优先级",
        "",
        "当前账户/渠道:",
        *_format_order_lines(draft),
        "",
        "当前顺序为:",
        ",".join(str(i) for i in range(1, len(draft) + 1)) or "-",
        "",
        "请回复新的顺序，例如:",
        "2,1,3...",
    ]
    ui.answer_cb(cb_id)
    ui.edit(chat_id, message_id, ui.truncate("\n".join(lines)), reply_markup=ui.inline_kb([
        [ui.btn("◀ 返回主菜单", "menu:main"), ui.btn("取消", "lb:bulk_cancel")],
    ]))


def _parse_order_input(text: str, n: int) -> tuple[Optional[list[int]], Optional[str]]:
    nums_raw = [x for x in re.split(r"[\s,，;；]+", (text or "").strip()) if x]
    if not nums_raw:
        return None, "请输入序号列表。"
    nums: list[int] = []
    bad: list[str] = []
    for raw in nums_raw:
        try:
            nums.append(int(raw))
        except ValueError:
            bad.append(raw)
    if bad:
        return None, "存在非法项: " + ", ".join(bad[:10])
    if any(x < 1 or x > n for x in nums):
        bad_nums = [str(x) for x in nums if x < 1 or x > n]
        return None, "序号越界: " + ", ".join(bad_nums[:10])
    dup = sorted({x for x in nums if nums.count(x) > 1})
    if dup:
        return None, "存在重复序号: " + ", ".join(str(x) for x in dup)
    missing = [x for x in range(1, n + 1) if x not in nums]
    if missing:
        return None, "缺少序号: " + ", ".join(str(x) for x in missing)
    if len(nums) != n:
        return None, f"需要完整排列 {n} 个序号，当前 {len(nums)} 个。"
    return nums, None


def _bulk_preview(chat_id: int, order: list[int]) -> None:
    """批量输入成功后只更新草稿，不直接保存配置。"""
    st = states.get_state(chat_id)
    data = (st.get("data") or {}) if st else {}
    family = data.get("family") or "anthropic"
    draft = list(data.get("draft") or [])
    new_draft = [draft[i - 1] for i in order]
    _set_edit_state(chat_id, family, new_draft)
    text, kb = _edit_text_and_kb(family, new_draft, set())
    text = (
        "✅ 批量设置已应用到草稿（尚未保存）。\n"
        "请确认顺序后点击「保存设置」。\n\n"
        + text
    )
    ui.send(chat_id, ui.truncate(text), reply_markup=kb)


def handle_text_state(chat_id: int, action: str, text: str) -> bool:
    if action != "lb_bulk_input":
        return False
    st = states.get_state(chat_id)
    data = (st.get("data") or {}) if st else {}
    draft = list(data.get("draft") or [])
    order, err = _parse_order_input(text, len(draft))
    if err:
        ui.send(chat_id, f"❌ {ui.escape_html(err)}\n请重新输入：")
        return True
    assert order is not None
    _bulk_preview(chat_id, order)
    return True


def _bulk_save(chat_id: int, message_id: int, cb_id: str) -> None:
    st = states.get_state(chat_id)
    if not st or st.get("action") != "lb_bulk_preview":
        ui.answer_cb(cb_id, "会话已失效")
        show(chat_id, message_id)
        return
    data = st.get("data") or {}
    family = data.get("family") or "anthropic"
    draft = list(data.get("draft") or [])
    load_balancing.save_family_order(family, draft)
    states.pop_state(chat_id)
    ui.answer_cb(cb_id, "已保存")
    ui.edit(chat_id, message_id, f"✅ 已保存 {_family_label(family)} · 批量优先级设置。",
            reply_markup=ui.inline_kb([[ui.btn("返回负载均衡", "menu:loadbalancing"), ui.btn("🏠 主菜单", "menu:main")]]))


def _bulk_retry(chat_id: int, message_id: int, cb_id: str) -> None:
    st = states.get_state(chat_id)
    if not st or st.get("action") != "lb_bulk_preview":
        ui.answer_cb(cb_id, "会话已失效")
        show(chat_id, message_id)
        return
    data = st.get("data") or {}
    family = data.get("family") or "anthropic"
    previous = list(data.get("previous") or data.get("draft") or [])
    states.set_state(chat_id, "lb_edit", {"family": family, "draft": previous, "selected": []})
    _bulk_start(chat_id, message_id, cb_id)


def _bulk_cancel(chat_id: int, message_id: int, cb_id: str) -> None:
    st = states.get_state(chat_id)
    if not st:
        show(chat_id, message_id, cb_id)
        return
    data = st.get("data") or {}
    family = data.get("family") or "anthropic"
    draft = list(data.get("previous") or data.get("draft") or _normalized_keys(family))
    _set_edit_state(chat_id, family, draft)
    _show_edit(chat_id, message_id, cb_id)


# ─── 清除亲和（二次确认） ─────────────────────────────────────────

def _aff_confirm_all(chat_id: int, message_id: int, cb_id: str) -> None:
    fp_total = affinity.count()
    cli_total = affinity.client_count()
    ui.answer_cb(cb_id)
    ui.edit(
        chat_id, message_id,
        (
            "⚠️ 确认清除【<b>全部协议</b>】的所有亲和绑定？\n"
            f"当前内存计数：fp <b>{fp_total}</b> 、 client <b>{cli_total}</b>\n"
            "此操作会同时清 fp 亲和与 client 软亲和，下一次调度将从头选。"
        ),
        reply_markup=ui.inline_kb([
            [ui.btn("✅ 确认清除全部", "lb:aff_all_exec"),
             ui.btn("❌ 取消", "menu:loadbalancing")],
        ]),
    )


def _aff_exec_all(chat_id: int, message_id: int, cb_id: str) -> None:
    fp_total = affinity.count()
    cli_total = affinity.client_count()
    affinity.delete_all()
    affinity.client_delete_all()
    ui.answer_cb(cb_id, f"已清 fp {fp_total}、client {cli_total}")
    show(chat_id, message_id)


def _aff_confirm_family(chat_id: int, message_id: int, cb_id: str, family: str) -> None:
    if family not in load_balancing.FAMILIES:
        ui.answer_cb(cb_id, "无效协议类型")
        return
    ui.answer_cb(cb_id)
    ui.edit(
        chat_id, message_id,
        (
            f"⚠️ 确认清除【{_family_label(family)}】的所有亲和绑定？\n"
            "仅清除本协议下所有渠道的 fp 亲和 + client 软亲和。"
        ),
        reply_markup=ui.inline_kb([
            [ui.btn("✅ 确认清除", f"lb:aff_fam_exec:{family}"),
             ui.btn("❌ 取消", f"lb:fam:{family}")],
        ]),
    )


def _aff_exec_family(chat_id: int, message_id: int, cb_id: str, family: str) -> None:
    if family not in load_balancing.FAMILIES:
        ui.answer_cb(cb_id, "无效协议类型")
        return
    fp_cnt = affinity.delete_by_protocol(family)
    cli_cnt = affinity.client_delete_by_protocol(family)
    ui.answer_cb(cb_id, f"已清 fp {fp_cnt}、client {cli_cnt}")
    # 清完后重新进入该协议的编辑页（重新拉 draft，避免重复 answer cb）
    draft = _normalized_keys(family)
    _set_edit_state(chat_id, family, draft)
    _show_edit(chat_id, message_id, cb_id=None)


# ─── 路由 ─────────────────────────────────────────────────────────

def handle_callback(chat_id: int, message_id: int, cb_id: str, data: str) -> bool:
    if data == "menu:loadbalancing":
        show(chat_id, message_id, cb_id); return True
    if data.startswith("lb:mode:"):
        _on_mode(chat_id, message_id, cb_id, data.split(":", 2)[2]); return True
    if data.startswith("lb:fam:"):
        _start_family(chat_id, message_id, cb_id, data.split(":", 2)[2]); return True
    if data.startswith("lb:sel:"):
        _toggle_select(chat_id, message_id, cb_id, data.split(":", 2)[2]); return True
    if data.startswith("lb:mv:"):
        _move(chat_id, message_id, cb_id, data.split(":", 2)[2]); return True
    if data == "lb:reset":
        _reset(chat_id, message_id, cb_id); return True
    if data == "lb:save":
        _save(chat_id, message_id, cb_id); return True
    if data == "lb:cancel":
        _cancel(chat_id, message_id, cb_id); return True
    if data == "lb:bulk":
        _bulk_start(chat_id, message_id, cb_id); return True
    if data == "lb:bulk_save":
        _bulk_save(chat_id, message_id, cb_id); return True
    if data == "lb:bulk_retry":
        _bulk_retry(chat_id, message_id, cb_id); return True
    if data == "lb:bulk_cancel":
        _bulk_cancel(chat_id, message_id, cb_id); return True
    if data == "lb:aff_all":
        _aff_confirm_all(chat_id, message_id, cb_id); return True
    if data == "lb:aff_all_exec":
        _aff_exec_all(chat_id, message_id, cb_id); return True
    if data.startswith("lb:aff_fam:"):
        _aff_confirm_family(chat_id, message_id, cb_id, data.split(":", 2)[2]); return True
    if data.startswith("lb:aff_fam_exec:"):
        _aff_exec_family(chat_id, message_id, cb_id, data.split(":", 2)[2]); return True
    return False
