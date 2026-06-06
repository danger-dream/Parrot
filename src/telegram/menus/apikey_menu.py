"""API Key 管理菜单。

callback_data 前缀：`ak:...`

交互树：
  列表                      ak: list (= menu:apikey)
  ├─ 添加名称                ak:add
  │    ├─ 自动生成            ak:add_auto:<short>
  │    └─ 自定义输入          ak:add_custom:<short>
  └─ 详情                   ak:view:<short>
       ├─ 重新生成 key       ak:regen:<short>
       │    └─ 确认覆盖       ak:regen_exec:<short>
       ├─ 自定义新 key       ak:rekey:<short>
       ├─ 编辑允许模型       ak:perm:<short>
       │    ├─ 切换单个       ak:pt:<short>:<idx>
       │    ├─ 清空（=不限制） ak:pclr:<short>
       │    ├─ 保存           ak:psave:<short>
       │    └─ 取消           ak:pcancel:<short>
       ├─ 编辑允许协议       ak:proto:<short>
       │    ├─ 切换单个       ak:ptp:<short>:<proto>
       │    ├─ 清空（=不限制） ak:ptpclr:<short>
       │    ├─ 保存           ak:ptpsave:<short>
       │    └─ 取消           ak:ptpcancel:<short>
       ├─ 删除确认           ak:del:<short>
       │    └─ 执行删除       ak:del_exec:<short>
       └─ 返回列表

状态机:
  ak_add_name: 等待用户输入新 Key 名称
  ak_add_key_input: 等待用户输入新 Key 的自定义密钥
  ak_rekey_input: 等待用户输入已有 Key 的自定义新密钥
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from ... import config, log_db
from ...channel import registry
from .. import states, ui


_BJT = timezone(timedelta(hours=8))


def _month_start_ts() -> float:
    return datetime.now(_BJT).replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()


def _key_month_stats(name: str) -> Optional[dict]:
    """本月该 API Key 的统计。无数据返回 None。"""
    try:
        s = log_db.tokens_for_apikey(name, since_ts=_month_start_ts())
    except Exception:
        return None
    if not s or s.get("total", 0) <= 0:
        return None
    return s


_KEY_PREFIX = "ccp-"
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_\-\.]{1,64}$")
_CUSTOM_KEY_PATTERN = re.compile(r"^[A-Za-z0-9\-_.~+/=]{8,256}$")


# ─── 工具 ─────────────────────────────────────────────────────────

def _get_entry(name: str) -> Optional[dict]:
    """取指定 name 的 apiKeys 条目（新结构 dict）。兼容尚未 normalize 的情况。"""
    entry = (config.get().get("apiKeys") or {}).get(name)
    if entry is None:
        return None
    if isinstance(entry, str):
        return {"key": entry, "allowedModels": []}
    if isinstance(entry, dict):
        return entry
    return None


def _short_of(name: str) -> str:
    """为 name 申请（或复用）一个 callback 短码。"""
    return ui.register_code(f"ak:{name}")


def _name_of(short: str) -> Optional[str]:
    full = ui.resolve_code(short)
    if not full or not full.startswith("ak:"):
        return None
    return full[3:]


def _fmt_allowed(allowed: list[str]) -> str:
    """allowed 空 = 无限制（全部）；否则列出。"""
    if not allowed:
        return "🎯 允许: <b>全部模型</b>（无限制）"
    return f"🎯 允许: <b>{len(allowed)}</b> 个模型"


def _all_api_key_values(exclude_name: Optional[str] = None) -> list[str]:
    values: list[str] = []
    for name, entry in (config.get().get("apiKeys") or {}).items():
        if exclude_name is not None and name == exclude_name:
            continue
        if isinstance(entry, str):
            key_value = entry
        elif isinstance(entry, dict):
            key_value = entry.get("key", "")
        else:
            key_value = ""
        if key_value:
            values.append(key_value)
    return values


def _validate_custom_key(key: str, existing_keys: list[str]) -> Optional[str]:
    if len(key) < 8:
        return "key 太短，至少 8 个字符。"
    if len(key) > 256:
        return "key 太长，最多 256 个字符。"
    if not _CUSTOM_KEY_PATTERN.fullmatch(key):
        return "key 含非法字符。仅允许可见 ASCII 字母数字和 -_.~+/=，不允许空格、换行或控制字符。"
    if key in existing_keys:
        return "key 已被其他 key 使用，请换一个。"
    return None


def _new_generated_key() -> str:
    return f"{_KEY_PREFIX}{secrets.token_hex(24)}"


def _create_api_key_entry(name: str, api_key: str) -> None:
    def _mutate(cfg):
        cfg.setdefault("apiKeys", {})[name] = {
            "key": api_key,
            "allowedModels": [],
            "allowImages": False,
        }
    config.update(_mutate)


def _set_api_key_value(name: str, api_key: str) -> bool:
    updated = False

    def _mutate(cfg):
        nonlocal updated
        keys = cfg.setdefault("apiKeys", {})
        entry = keys.get(name)
        if isinstance(entry, str):
            keys[name] = {"key": api_key, "allowedModels": []}
            updated = True
        elif isinstance(entry, dict):
            entry["key"] = api_key
            updated = True
    config.update(_mutate)
    return updated


def _send_created(chat_id: int, name: str, api_key: str) -> None:
    ui.send_result(
        chat_id,
        "✅ <b>API Key 已创建</b>\n\n"
        f"名称: <b>{ui.escape_html(name)}</b>\n"
        f"Key: <code>{ui.escape_html(api_key)}</code>\n\n"
        "<i>默认不限制模型。可在 API Key 详情页配置「允许模型」白名单。</i>",
        back_label="◀ 返回 API Key 管理",
        back_callback="menu:apikey",
    )


def _send_rekeyed(chat_id: int, name: str, api_key: str) -> None:
    ui.send_result(
        chat_id,
        "✅ <b>API Key 已更新</b>\n\n"
        f"名称: <b>{ui.escape_html(name)}</b>\n"
        f"新 Key: <code>{ui.escape_html(api_key)}</code>\n\n"
        "⚠ 下游客户端需要更新为新 key。",
        back_label="◀ 返回 API Key 管理",
        back_callback="menu:apikey",
    )


# ─── 允许协议相关常量 ─────────────────────────────────────────────

# 展示顺序固定，idx 在按钮 callback 中用 proto 原值（稳定、直观）
PROTOCOL_CHOICES: list[tuple[str, str]] = [
    ("anthropic", "🅰 Anthropic (/v1/messages)"),
    ("chat",      "🅞 OpenAI Chat (/v1/chat/completions)"),
    ("responses", "🅞 OpenAI Responses (/v1/responses)"),
]

_PROTOCOL_SET = {p for p, _ in PROTOCOL_CHOICES}
_PROTOCOL_LABEL = {p: label for p, label in PROTOCOL_CHOICES}


def _fmt_allowed_protocols(protos: list[str]) -> str:
    if not protos:
        return "🔌 允许协议: <b>全部入口</b>（无限制）"
    return "🔌 允许协议: " + " · ".join(f"<b>{_PROTOCOL_LABEL.get(p, p)}</b>" for p in protos)


def _fmt_allow_images(entry: dict) -> str:
    return "🖼 图片接口: " + ("<b>允许</b>" if entry.get("allowImages") else "<b>禁止</b>")


# ─── 列表视图 ─────────────────────────────────────────────────────

_ALL_PROTO = {"anthropic", "chat", "responses"}
_PROTO_SHORT = {"anthropic": "Anthropic", "chat": "Chat", "responses": "Responses"}
_PROTO_FULL = {
    "anthropic": "Anthropic (/v1/messages)",
    "chat": "OpenAI Chat (/v1/chat/completions)",
    "responses": "OpenAI Responses (/v1/responses)",
}


def _perm_summary_short(allowed: list[str], protos: list[str], img: bool) -> str:
    """列表页用：单行简短权限串。默认全开显示「全部模型 · 全部入口」。"""
    m = "全部模型" if not allowed else f"{len(allowed)} 个模型"
    if not protos or set(protos) == _ALL_PROTO:
        p = "全部入口"
    else:
        p = "/".join(_PROTO_SHORT.get(x, x) for x in protos)
    s = f"{m} · {p}"
    if img:
        s += " · 🖼 图片"
    return s


def _render_list() -> tuple[str, dict]:
    keys = (config.get().get("apiKeys") or {})
    if not keys:
        text = "🔑 <b>API Key 管理</b>\n当前: 0 个\n\n暂无 Key，点「➕ 添加」创建。"
        rows = [[ui.btn("➕ 添加", "ak:add")], [ui.btn("◀ 返回主菜单", "menu:main")]]
        return text, ui.inline_kb(rows)

    since_ts = _month_start_ts()
    per: dict[str, dict] = {}
    agg = {"total": 0, "input": 0, "output": 0, "cache_creation": 0, "cache_read": 0}
    for name in keys:
        try:
            s = log_db.tokens_for_apikey(name, since_ts=since_ts)
        except Exception:
            s = {"total": 0, "success_count": 0, "error_count": 0, "input": 0,
                 "output": 0, "cache_creation": 0, "cache_read": 0, "avg_tps": None,
                 "max_tps": None, "min_tps": None}
        per[name] = s
        for k in agg:
            agg[k] += int(s.get(k, 0) or 0)
    active = sum(1 for n in keys if per[n]["total"] > 0)
    idle = len(keys) - active
    agg_prompt = ui.prompt_total(agg["input"], agg["cache_creation"], agg["cache_read"])

    head = (
        "🔑 <b>API Key 管理</b>\n"
        f"共 {len(keys)} 个 · 活跃 {active}"
        + (f" · 闲置 {idle}" if idle else "")
        + f" | 本月 {agg['total']:,} 次"
    )
    if agg["total"] > 0:
        head += f" · ↑ {ui.fmt_tokens(agg_prompt)} · ↓ {ui.fmt_tokens(agg['output'])}"

    lines = [head, ""]
    for i, (name, entry) in enumerate(keys.items(), 1):
        if isinstance(entry, str):
            key_str = entry
            allowed: list[str] = []
            protos: list[str] = []
            img = False
        else:
            key_str = entry.get("key", "")
            allowed = list(entry.get("allowedModels") or [])
            protos = list(entry.get("allowedProtocols") or [])
            img = bool(entry.get("allowImages"))
        s = per[name]
        dot = "🟢" if s["total"] > 0 else "⚪"
        lines.append(f"{i}. {dot} <b>{ui.escape_html(name)}</b>")
        lines.append(f"<code>{ui.escape_html(key_str)}</code>")
        lines.append(f"🏷️ {_perm_summary_short(allowed, protos, img)}")
        if s["total"] > 0:
            prompt = ui.prompt_total(s["input"], s["cache_creation"], s["cache_read"])
            stat = f"💎 本月: {s['total']:,} 次 · ↑ {ui.fmt_tokens(prompt)} · ↓ {ui.fmt_tokens(s['output'])}"
            if (s.get("cache_read") or 0) > 0:
                stat += f" · {ui.fmt_cache_phrase(s['cache_read'], prompt)}"
            lines.append(stat)
            if s.get("avg_tps") is not None:
                lines.append(
                    f"⚡ TPS: 平均 {ui.fmt_tps(s.get('avg_tps'))} · "
                    f"峰值 {ui.fmt_tps(s.get('max_tps'))} · "
                    f"最低 {ui.fmt_tps(s.get('min_tps'))}"
                )
        else:
            try:
                s0 = log_db.tokens_for_apikey(name, since_ts=0)
                hist = s0["total"]
            except Exception:
                hist = 0
            lines.append(f"💎 本月: <i>闲置</i>（历史 {hist:,} 次）")
        lines.append("")
    text = ui.truncate("\n".join(lines).rstrip())

    rows: list[list[dict]] = []
    cur: list[dict] = []
    for name in keys:
        cur.append(ui.btn(f"✏ {name}", f"ak:view:{_short_of(name)}"))
        if len(cur) >= 2:
            rows.append(cur)
            cur = []
    if cur:
        rows.append(cur)
    rows.append([ui.btn("➕ 添加", "ak:add"), ui.btn("🔄 刷新", "menu:apikey")])
    rows.append([ui.btn("◀ 返回主菜单", "menu:main")])
    return text, ui.inline_kb(rows)


def show(chat_id: int, message_id: int, cb_id: Optional[str] = None) -> None:
    if cb_id is not None:
        ui.answer_cb(cb_id)
    text, kb = _render_list()
    ui.edit(chat_id, message_id, text, reply_markup=kb)


def send_new(chat_id: int) -> None:
    """命令入口：直接 send 一条新消息（不依赖 message_id）。"""
    text, kb = _render_list()
    ui.send(chat_id, text, reply_markup=kb)


# ─── 详情视图 ─────────────────────────────────────────────────────

def _render_detail(name: str) -> tuple[Optional[str], Optional[dict]]:
    entry = _get_entry(name)
    if entry is None:
        return None, None
    key_str = entry.get("key", "")
    allowed = list(entry.get("allowedModels") or [])
    allowed_protos = list(entry.get("allowedProtocols") or [])
    img = bool(entry.get("allowImages"))

    since_ts = _month_start_ts()
    try:
        s = log_db.tokens_for_apikey(name, since_ts=since_ts)
    except Exception:
        s = {"total": 0, "success_count": 0, "error_count": 0, "input": 0,
             "output": 0, "cache_creation": 0, "cache_read": 0, "avg_tps": None,
             "max_tps": None, "min_tps": None}
    active = s["total"] > 0
    dot = "🟢 活跃" if active else "⚪ 闲置"

    if not allowed_protos or set(allowed_protos) == _ALL_PROTO:
        proto_str = "全部入口（无限制）"
    else:
        proto_str = " / ".join(_PROTO_FULL.get(p, p) for p in allowed_protos)

    lines = [
        f"🔑 <b>{ui.escape_html(name)}</b>  {dot}",
        "",
        f"Key: <code>{ui.escape_html(key_str)}</code>",
        "",
        "状态: <code>enabled</code>",
        f"🎯 模型: <code>{'全部模型（无限制）' if not allowed else str(len(allowed)) + ' 个白名单'}</code>",
        f"🔌 协议: <code>{ui.escape_html(proto_str)}</code>",
        f"🖼 图片接口: <code>{'允许' if img else '禁止'}</code>",
    ]
    if allowed:
        for m in allowed:
            lines.append(f"    • <code>{ui.escape_html(m)}</code>")

    lines.append("")
    lines.append("<b>📊 本月使用统计</b>")
    if active:
        prompt = ui.prompt_total(s["input"], s["cache_creation"], s["cache_read"])
        token_line = f"↑ {ui.fmt_tokens(prompt)} · ↓ {ui.fmt_tokens(s['output'])}"
        if (s.get("cache_read") or 0) > 0:
            token_line += f" · {ui.fmt_cache_phrase(s['cache_read'], prompt)}"
        lines.append(f"总体: {s['total']:,} 次 · ✅ {s['success_count']} · ❌ {s['error_count']}")
        lines.append(token_line)
        lines.append(
            f"平均 {ui.fmt_tps(s.get('avg_tps'))} · "
            f"峰值 {ui.fmt_tps(s.get('max_tps'))} · "
            f"最低 {ui.fmt_tps(s.get('min_tps'))}"
        )
        try:
            by_model = log_db.apikey_model_stats(name, since_ts=since_ts)
        except Exception:
            by_model = []
        if by_model:
            lines.append("")
            lines.append("按模型:")
            for mrow in by_model[:8]:
                model = ui.escape_html(mrow.get("final_model") or "?")
                m_prompt = ui.prompt_total(mrow["input"], mrow["cache_creation"], mrow["cache_read"])
                model_line = (
                    f"    {mrow['total']:,} 次 · ✅ {mrow['success_count']} · ❌ {mrow['error_count']}"
                    f" · ↑ {ui.fmt_tokens(m_prompt)} · ↓ {ui.fmt_tokens(mrow['output'])}"
                )
                if (mrow.get("cache_read") or 0) > 0:
                    model_line += f" · {ui.fmt_cache_phrase(mrow['cache_read'], m_prompt)}"
                lines.append(f"  • <code>{model}</code>")
                lines.append(model_line)
            if len(by_model) > 8:
                lines.append(f"  <i>… 其余 {len(by_model) - 8} 个模型未展开</i>")
    else:
        lines.append("<i>本月暂无调用</i>")

    short = _short_of(name)
    img_label = "🖼 禁用图片接口" if entry.get("allowImages") else "🖼 允许图片接口"
    rows = [
        [ui.btn("🔁 重新生成 key", f"ak:regen:{short}"),
         ui.btn("✏ 自定义新 key", f"ak:rekey:{short}")],
        [ui.btn("🎯 编辑允许模型", f"ak:perm:{short}")],
        [ui.btn("🔌 编辑允许协议", f"ak:proto:{short}")],
        [ui.btn(img_label, f"ak:img:{short}")],
        [ui.btn("🗑 删除", f"ak:del:{short}")],
        [ui.btn("◀ 返回列表", "menu:apikey")],
    ]
    return ui.truncate("\n".join(lines)), ui.inline_kb(rows)


def on_view(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    ui.answer_cb(cb_id)
    name = _name_of(short)
    if not name:
        show(chat_id, message_id)
        return
    text, kb = _render_detail(name)
    if text is None:
        ui.edit(chat_id, message_id, f"⚠ 未找到 <code>{ui.escape_html(name)}</code>",
                reply_markup=ui.inline_kb([[ui.btn("◀ 返回列表", "menu:apikey")]]))
        return
    ui.edit(chat_id, message_id, text, reply_markup=kb)


# ─── 添加 ─────────────────────────────────────────────────────────

def on_add(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    states.set_state(chat_id, "ak_add_name")
    ui.edit(
        chat_id, message_id,
        "请输入新 API Key 的名称（允许 字母/数字/<code>_ - .</code>，长度 ≤ 64）：",
        reply_markup=ui.inline_kb([[ui.btn("❌ 取消", "menu:apikey")]]),
    )


def on_add_name_input(chat_id: int, text: str) -> None:
    name = (text or "").strip()
    if not _NAME_PATTERN.match(name):
        ui.send(chat_id, "❌ 名称无效。允许字符：字母、数字、<code>_ - .</code>；长度 1-64。请重新输入：")
        return
    if name in (config.get().get("apiKeys") or {}):
        ui.send(chat_id, f"❌ 名称 <code>{ui.escape_html(name)}</code> 已存在，请换一个：")
        return

    states.set_state(chat_id, "ak_add_key_input", {"name": name})
    short = _short_of(name)
    ui.send(
        chat_id,
        "请选择 API Key 密钥生成方式：\n\n"
        f"名称: <b>{ui.escape_html(name)}</b>",
        reply_markup=ui.inline_kb([
            [ui.btn("🎲 自动生成", f"ak:add_auto:{short}")],
            [ui.btn("✏ 自定义输入", f"ak:add_custom:{short}")],
            [ui.btn("❌ 取消", "menu:apikey")],
        ]),
    )


def on_add_auto(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    state = states.get_state(chat_id)
    name = (state or {}).get("data", {}).get("name")
    if not state or state.get("action") != "ak_add_key_input" or _name_of(short) != name:
        ui.answer_cb(cb_id, "会话已过期")
        show(chat_id, message_id)
        return
    if name in (config.get().get("apiKeys") or {}):
        ui.answer_cb(cb_id, "名称已存在")
        states.set_state(chat_id, "ak_add_name")
        ui.edit(chat_id, message_id, f"❌ 名称 <code>{ui.escape_html(name)}</code> 已存在，请重新输入名称：")
        return

    api_key = _new_generated_key()
    _create_api_key_entry(name, api_key)
    states.pop_state(chat_id)
    ui.answer_cb(cb_id, "已创建")
    _send_created(chat_id, name, api_key)


def on_add_custom(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    state = states.get_state(chat_id)
    name = (state or {}).get("data", {}).get("name")
    if not state or state.get("action") != "ak_add_key_input" or _name_of(short) != name:
        ui.answer_cb(cb_id, "会话已过期")
        show(chat_id, message_id)
        return
    ui.answer_cb(cb_id)
    ui.edit(
        chat_id, message_id,
        "请输入自定义 key 密钥（8-256 字符）：\n\n"
        "允许可见 ASCII 字母数字和 <code>-_.~+/=</code>；不允许空格、换行或控制字符。",
        reply_markup=ui.inline_kb([[ui.btn("❌ 取消", "menu:apikey")]]),
    )


def on_add_key_input(chat_id: int, text: str) -> None:
    state = states.get_state(chat_id)
    name = (state or {}).get("data", {}).get("name")
    if not state or state.get("action") != "ak_add_key_input" or not name:
        ui.send(chat_id, "⚠ 会话已过期，请重新添加。")
        states.pop_state(chat_id)
        return
    if name in (config.get().get("apiKeys") or {}):
        ui.send(chat_id, f"❌ 名称 <code>{ui.escape_html(name)}</code> 已存在，请重新添加。")
        states.pop_state(chat_id)
        return
    api_key = text or ""
    err = _validate_custom_key(api_key, _all_api_key_values())
    if err:
        ui.send(chat_id, f"❌ {ui.escape_html(err)}\n请重新输入：")
        return

    _create_api_key_entry(name, api_key)
    states.pop_state(chat_id)
    _send_created(chat_id, name, api_key)


def on_regen_confirm(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    ui.answer_cb(cb_id)
    name = _name_of(short)
    entry = _get_entry(name) if name else None
    if entry is None:
        ui.edit(chat_id, message_id, "⚠ 未找到该 Key（可能已被删除）",
                reply_markup=ui.inline_kb([[ui.btn("◀ 返回", "menu:apikey")]]))
        return
    ui.edit(
        chat_id, message_id,
        f"确认为 <b>{ui.escape_html(name)}</b> 重新生成 key？\n\n"
        "⚠ 当前 key 会被覆盖，下游客户端需要更新。",
        reply_markup=ui.inline_kb([[
            ui.btn("✅ 确认重新生成", f"ak:regen_exec:{short}"),
            ui.btn("❌ 取消", f"ak:view:{short}"),
        ]]),
    )


def on_regen_exec(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    name = _name_of(short)
    if not name or _get_entry(name) is None:
        ui.answer_cb(cb_id, "未找到 Key")
        show(chat_id, message_id)
        return
    api_key = _new_generated_key()
    _set_api_key_value(name, api_key)
    ui.answer_cb(cb_id, "已重新生成")
    _send_rekeyed(chat_id, name, api_key)


def on_rekey_enter(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    name = _name_of(short)
    entry = _get_entry(name) if name else None
    if entry is None:
        ui.answer_cb(cb_id, "未找到 Key")
        show(chat_id, message_id)
        return
    states.set_state(chat_id, "ak_rekey_input", {"name": name, "short": short})
    ui.answer_cb(cb_id)
    ui.edit(
        chat_id,
        message_id,
        f"请输入 <b>{ui.escape_html(name)}</b> 的自定义新 key（8-256 字符）：\n\n"
        "允许可见 ASCII 字母数字和 <code>-_.~+/=</code>；不允许空格、换行或控制字符。",
        reply_markup=ui.inline_kb([[ui.btn("❌ 取消", f"ak:view:{short}")]]),
    )


def on_rekey_input(chat_id: int, text: str) -> None:
    state = states.get_state(chat_id)
    data = (state or {}).get("data", {})
    name = data.get("name")
    if not state or state.get("action") != "ak_rekey_input" or not name:
        ui.send(chat_id, "⚠ 会话已过期，请重新操作。")
        states.pop_state(chat_id)
        return
    if _get_entry(name) is None:
        ui.send(chat_id, "⚠ 未找到该 Key（可能已被删除）。")
        states.pop_state(chat_id)
        return
    api_key = text or ""
    err = _validate_custom_key(api_key, _all_api_key_values(exclude_name=name))
    if err:
        ui.send(chat_id, f"❌ {ui.escape_html(err)}\n请重新输入：")
        return
    _set_api_key_value(name, api_key)
    states.pop_state(chat_id)
    _send_rekeyed(chat_id, name, api_key)


# ─── 删除（二次确认） ────────────────────────────────────────────

def on_del_confirm(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    ui.answer_cb(cb_id)
    name = _name_of(short)
    entry = _get_entry(name) if name else None
    if entry is None:
        ui.edit(chat_id, message_id, "⚠ 未找到该 Key（可能已被删除）",
                reply_markup=ui.inline_kb([[ui.btn("◀ 返回", "menu:apikey")]]))
        return
    key_value = entry.get("key", "")
    tail = key_value[-8:] if len(key_value) > 8 else key_value
    ui.edit(
        chat_id, message_id,
        f"确认删除 <b>{ui.escape_html(name)}</b>？\n"
        f"Key 末尾: <code>…{ui.escape_html(tail)}</code>\n"
        f"⚠ 删除后使用该 Key 的下游客户端将立即失效。",
        reply_markup=ui.inline_kb([[
            ui.btn("✅ 确认删除", f"ak:del_exec:{short}"),
            ui.btn("❌ 取消", f"ak:view:{short}"),
        ]]),
    )


def on_del_exec(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    name = _name_of(short)
    if not name:
        ui.answer_cb(cb_id, "已过期，请重试")
        show(chat_id, message_id)
        return

    def _mutate(cfg):
        (cfg.get("apiKeys") or {}).pop(name, None)
    config.update(_mutate)

    ui.answer_cb(cb_id, "已删除")
    ui.edit(
        chat_id, message_id,
        f"✅ 已删除 <code>{ui.escape_html(name)}</code>",
        reply_markup=ui.inline_kb([
            [ui.btn("◀ 返回 API Key 管理", "menu:apikey"),
             ui.btn("🏠 主菜单", "menu:main")],
        ]),
    )


def on_images_toggle(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    name = _name_of(short)
    entry = _get_entry(name) if name else None
    if entry is None:
        ui.answer_cb(cb_id, "未找到 Key")
        show(chat_id, message_id)
        return

    def _mutate(cfg):
        keys = cfg.setdefault("apiKeys", {})
        cur = keys.get(name)
        if isinstance(cur, str):
            cur = {"key": cur, "allowedModels": [], "allowImages": False}
            keys[name] = cur
        if isinstance(cur, dict):
            cur["allowImages"] = not bool(cur.get("allowImages", False))
    config.update(_mutate)
    ui.answer_cb(cb_id, "已切换")
    text, kb = _render_detail(name)
    if text:
        ui.edit(chat_id, message_id, text, reply_markup=kb)


# ─── 允许模型多选 ────────────────────────────────────────────────

_PERM_STATE = "ak_perm_editing"


def _render_perm_edit(name: str, models: list[str], checked: set[str]) -> tuple[str, dict]:
    lines = [
        f"🎯 <b>编辑允许模型</b>: {ui.escape_html(name)}",
        "",
        "点击下方模型切换勾选。清空 → 视为无限制。",
        f"当前已选: <b>{len(checked)}</b>" + ("（= 不限制）" if not checked else " 个"),
    ]

    rows: list[list[dict]] = []
    cur: list[dict] = []
    for idx, m in enumerate(models):
        mark = "☑" if m in checked else "☐"
        cur.append(ui.btn(f"{mark} {m}", f"ak:pt:{_short_of(name)}:{idx}"))
        if len(cur) >= 2:
            rows.append(cur)
            cur = []
    if cur:
        rows.append(cur)

    short = _short_of(name)
    save_label = f"✅ 保存（{len(checked)} 个）" if checked else "✅ 保存（不限制）"
    rows.append([
        ui.btn(save_label, f"ak:psave:{short}"),
        ui.btn("🚫 清空(=不限制)", f"ak:pclr:{short}"),
    ])
    rows.append([ui.btn("❌ 取消", f"ak:pcancel:{short}")])
    return "\n".join(lines), ui.inline_kb(rows)


def on_perm_enter(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    ui.answer_cb(cb_id)
    name = _name_of(short)
    entry = _get_entry(name) if name else None
    if entry is None:
        show(chat_id, message_id)
        return

    models = registry.available_models()
    if not models:
        ui.edit(
            chat_id, message_id,
            "⚠ 当前无可用渠道/模型，请先添加 OAuth 或渠道。",
            reply_markup=ui.inline_kb([[ui.btn("◀ 返回详情", f"ak:view:{short}")]]),
        )
        return

    current = set(entry.get("allowedModels") or [])
    # 交集：仅保留仍然存在的模型
    checked = {m for m in current if m in models}
    states.set_state(chat_id, _PERM_STATE, {
        "name": name,
        "models": models,     # 稳定顺序，用 idx 索引
        "checked": list(checked),
    })
    text, kb = _render_perm_edit(name, models, checked)
    ui.edit(chat_id, message_id, text, reply_markup=kb)


def on_perm_toggle(chat_id: int, message_id: int, cb_id: str, short: str, idx_str: str) -> None:
    state = states.get_state(chat_id)
    if not state or state.get("action") != _PERM_STATE:
        ui.answer_cb(cb_id, "会话已过期")
        show(chat_id, message_id)
        return
    data = state["data"]
    if _name_of(short) != data.get("name"):
        ui.answer_cb(cb_id, "短码不匹配")
        return
    try:
        idx = int(idx_str)
        model = data["models"][idx]
    except (ValueError, IndexError):
        ui.answer_cb(cb_id, "索引无效")
        return

    checked = set(data.get("checked") or [])
    if model in checked:
        checked.remove(model)
    else:
        checked.add(model)
    data["checked"] = list(checked)
    states.set_state(chat_id, _PERM_STATE, data)

    ui.answer_cb(cb_id)
    text, kb = _render_perm_edit(data["name"], data["models"], checked)
    ui.edit(chat_id, message_id, text, reply_markup=kb)


def on_perm_clear(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    state = states.get_state(chat_id)
    if not state or state.get("action") != _PERM_STATE:
        ui.answer_cb(cb_id, "会话已过期")
        show(chat_id, message_id)
        return
    data = state["data"]
    if _name_of(short) != data.get("name"):
        ui.answer_cb(cb_id, "短码不匹配")
        return
    data["checked"] = []
    states.set_state(chat_id, _PERM_STATE, data)
    ui.answer_cb(cb_id, "已清空（= 不限制）")
    text, kb = _render_perm_edit(data["name"], data["models"], set())
    ui.edit(chat_id, message_id, text, reply_markup=kb)


def on_perm_save(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    state = states.get_state(chat_id)
    if not state or state.get("action") != _PERM_STATE:
        ui.answer_cb(cb_id, "会话已过期")
        show(chat_id, message_id)
        return
    data = state["data"]
    name = data["name"]
    if _name_of(short) != name:
        ui.answer_cb(cb_id, "短码不匹配")
        return
    checked = list(data.get("checked") or [])

    def _mutate(cfg):
        keys = cfg.setdefault("apiKeys", {})
        entry = keys.get(name)
        if isinstance(entry, str):
            entry = {"key": entry, "allowedModels": []}
            keys[name] = entry
        if not isinstance(entry, dict):
            return
        entry["allowedModels"] = checked
    config.update(_mutate)
    states.pop_state(chat_id)

    ui.answer_cb(cb_id, "已保存")
    # 回到详情页
    text, kb = _render_detail(name)
    if text:
        ui.edit(chat_id, message_id, text, reply_markup=kb)


def on_perm_cancel(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    states.pop_state(chat_id)
    ui.answer_cb(cb_id, "已取消")
    name = _name_of(short)
    if not name:
        show(chat_id, message_id)
        return
    text, kb = _render_detail(name)
    if text:
        ui.edit(chat_id, message_id, text, reply_markup=kb)


# ─── 允许协议多选 ────────────────────────────────────────────────

_PROTO_STATE = "ak_proto_editing"


def _render_proto_edit(name: str, checked: set[str]) -> tuple[str, dict]:
    lines = [
        f"🔌 <b>编辑允许协议</b>: {ui.escape_html(name)}",
        "",
        "点击下方入口切换勾选。清空 → 视为无限制（所有入口都放行）。",
        "同一 Key 可同时勾选多个；如只想让该 Key 走 OpenAI 入口，就只勾 chat / responses。",
        "",
        f"当前已选: <b>{len(checked)}</b>" + ("（= 不限制）" if not checked else " 个"),
    ]
    short = _short_of(name)
    rows: list[list[dict]] = []
    for proto, label in PROTOCOL_CHOICES:
        mark = "☑" if proto in checked else "☐"
        rows.append([ui.btn(f"{mark} {label}", f"ak:ptp:{short}:{proto}")])
    save_label = f"✅ 保存（{len(checked)} 个）" if checked else "✅ 保存（不限制）"
    rows.append([
        ui.btn(save_label, f"ak:ptpsave:{short}"),
        ui.btn("🚫 清空(=不限制)", f"ak:ptpclr:{short}"),
    ])
    rows.append([ui.btn("❌ 取消", f"ak:ptpcancel:{short}")])
    return "\n".join(lines), ui.inline_kb(rows)


def on_proto_enter(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    ui.answer_cb(cb_id)
    name = _name_of(short)
    entry = _get_entry(name) if name else None
    if entry is None:
        show(chat_id, message_id)
        return
    current = set(entry.get("allowedProtocols") or [])
    checked = {p for p in current if p in _PROTOCOL_SET}
    states.set_state(chat_id, _PROTO_STATE, {
        "name": name,
        "checked": list(checked),
    })
    text, kb = _render_proto_edit(name, checked)
    ui.edit(chat_id, message_id, text, reply_markup=kb)


def on_proto_toggle(chat_id: int, message_id: int, cb_id: str, short: str, proto: str) -> None:
    state = states.get_state(chat_id)
    if not state or state.get("action") != _PROTO_STATE:
        ui.answer_cb(cb_id, "会话已过期")
        show(chat_id, message_id)
        return
    data = state["data"]
    if _name_of(short) != data.get("name"):
        ui.answer_cb(cb_id, "短码不匹配")
        return
    if proto not in _PROTOCOL_SET:
        ui.answer_cb(cb_id, "未知协议")
        return
    checked = set(data.get("checked") or [])
    if proto in checked:
        checked.remove(proto)
    else:
        checked.add(proto)
    data["checked"] = list(checked)
    states.set_state(chat_id, _PROTO_STATE, data)

    ui.answer_cb(cb_id)
    text, kb = _render_proto_edit(data["name"], checked)
    ui.edit(chat_id, message_id, text, reply_markup=kb)


def on_proto_clear(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    state = states.get_state(chat_id)
    if not state or state.get("action") != _PROTO_STATE:
        ui.answer_cb(cb_id, "会话已过期")
        show(chat_id, message_id)
        return
    data = state["data"]
    if _name_of(short) != data.get("name"):
        ui.answer_cb(cb_id, "短码不匹配")
        return
    data["checked"] = []
    states.set_state(chat_id, _PROTO_STATE, data)
    ui.answer_cb(cb_id, "已清空（= 不限制）")
    text, kb = _render_proto_edit(data["name"], set())
    ui.edit(chat_id, message_id, text, reply_markup=kb)


def on_proto_save(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    state = states.get_state(chat_id)
    if not state or state.get("action") != _PROTO_STATE:
        ui.answer_cb(cb_id, "会话已过期")
        show(chat_id, message_id)
        return
    data = state["data"]
    name = data["name"]
    if _name_of(short) != name:
        ui.answer_cb(cb_id, "短码不匹配")
        return
    checked = [p for p, _ in PROTOCOL_CHOICES if p in (data.get("checked") or [])]  # 保稳定顺序

    def _mutate(cfg):
        keys = cfg.setdefault("apiKeys", {})
        entry = keys.get(name)
        if isinstance(entry, str):
            entry = {"key": entry, "allowedModels": []}
            keys[name] = entry
        if not isinstance(entry, dict):
            return
        if checked:
            entry["allowedProtocols"] = checked
        else:
            # 清空 = 不限制 → 直接删字段，避免 config 里堆积空数组
            entry.pop("allowedProtocols", None)
    config.update(_mutate)
    states.pop_state(chat_id)

    ui.answer_cb(cb_id, "已保存")
    text, kb = _render_detail(name)
    if text:
        ui.edit(chat_id, message_id, text, reply_markup=kb)


def on_proto_cancel(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    states.pop_state(chat_id)
    ui.answer_cb(cb_id, "已取消")
    name = _name_of(short)
    if not name:
        show(chat_id, message_id)
        return
    text, kb = _render_detail(name)
    if text:
        ui.edit(chat_id, message_id, text, reply_markup=kb)


# ─── 路由分发 ─────────────────────────────────────────────────────

def handle_callback(chat_id: int, message_id: int, cb_id: str, data: str) -> bool:
    if data == "menu:apikey":
        show(chat_id, message_id, cb_id)
        return True
    if data == "ak:add":
        on_add(chat_id, message_id, cb_id)
        return True
    if data.startswith("ak:add_auto:"):
        on_add_auto(chat_id, message_id, cb_id, data.split(":", 2)[2])
        return True
    if data.startswith("ak:add_custom:"):
        on_add_custom(chat_id, message_id, cb_id, data.split(":", 2)[2])
        return True
    if data.startswith("ak:view:"):
        on_view(chat_id, message_id, cb_id, data.split(":", 2)[2])
        return True
    if data.startswith("ak:regen_exec:"):
        on_regen_exec(chat_id, message_id, cb_id, data.split(":", 2)[2])
        return True
    if data.startswith("ak:regen:"):
        on_regen_confirm(chat_id, message_id, cb_id, data.split(":", 2)[2])
        return True
    if data.startswith("ak:rekey:"):
        on_rekey_enter(chat_id, message_id, cb_id, data.split(":", 2)[2])
        return True
    if data.startswith("ak:del_exec:"):
        on_del_exec(chat_id, message_id, cb_id, data.split(":", 2)[2])
        return True
    if data.startswith("ak:del:"):
        on_del_confirm(chat_id, message_id, cb_id, data.split(":", 2)[2])
        return True
    if data.startswith("ak:img:"):
        on_images_toggle(chat_id, message_id, cb_id, data.split(":", 2)[2])
        return True

    # 允许模型多选
    if data.startswith("ak:perm:"):
        on_perm_enter(chat_id, message_id, cb_id, data.split(":", 2)[2])
        return True
    if data.startswith("ak:pt:"):
        parts = data.split(":")
        if len(parts) >= 4:
            on_perm_toggle(chat_id, message_id, cb_id, parts[2], parts[3])
            return True
    if data.startswith("ak:pclr:"):
        on_perm_clear(chat_id, message_id, cb_id, data.split(":", 2)[2])
        return True
    if data.startswith("ak:psave:"):
        on_perm_save(chat_id, message_id, cb_id, data.split(":", 2)[2])
        return True
    if data.startswith("ak:pcancel:"):
        on_perm_cancel(chat_id, message_id, cb_id, data.split(":", 2)[2])
        return True

    # 允许协议多选
    if data.startswith("ak:proto:"):
        on_proto_enter(chat_id, message_id, cb_id, data.split(":", 2)[2])
        return True
    if data.startswith("ak:ptp:"):
        parts = data.split(":")
        if len(parts) >= 4:
            on_proto_toggle(chat_id, message_id, cb_id, parts[2], parts[3])
            return True
    if data.startswith("ak:ptpclr:"):
        on_proto_clear(chat_id, message_id, cb_id, data.split(":", 2)[2])
        return True
    if data.startswith("ak:ptpsave:"):
        on_proto_save(chat_id, message_id, cb_id, data.split(":", 2)[2])
        return True
    if data.startswith("ak:ptpcancel:"):
        on_proto_cancel(chat_id, message_id, cb_id, data.split(":", 2)[2])
        return True
    return False


def handle_text_state(chat_id: int, action: str, text: str) -> bool:
    """返回 True 表示本模块消费了该输入。"""
    if action == "ak_add_name":
        on_add_name_input(chat_id, text)
        return True
    if action == "ak_add_key_input":
        on_add_key_input(chat_id, text)
        return True
    if action == "ak_rekey_input":
        on_rekey_input(chat_id, text)
        return True
    return False
