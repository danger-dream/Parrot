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
       ├─ 删除确认           ak:del:<short>
       │    └─ 执行删除       ak:del_exec:<short>
       └─ 返回列表

状态机:
  ak_add_name: 等待用户输入新 Key 名称
  ak_add_key_input: 等待用户输入新 Key 的自定义密钥
  ak_rekey_input: 等待用户输入已有 Key 的自定义新密钥
"""

from __future__ import annotations

import math
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from ... import apikey_limiter, config, log_db
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
        return {
            "key": entry,
            "enabled": True,
            "allowedModels": [],
            "allowImages": False,
            "allowVideos": False,
        }
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


def _fmt_duration(seconds: int) -> str:
    seconds = max(0, int(seconds or 0))
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds >= 60 and seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def _parse_duration(text: str) -> Optional[int]:
    raw = (text or "").strip().lower()
    if not raw:
        return None
    if raw in ("default", "默认", "继承"):
        return None
    mult = 1
    if raw.endswith("ms"):
        # UI 不鼓励毫秒，这里向上取整到秒。
        try:
            return max(0, int((int(raw[:-2].strip()) + 999) / 1000))
        except Exception:
            raise ValueError
    if raw.endswith("s"):
        raw = raw[:-1].strip(); mult = 1
    elif raw.endswith("m"):
        raw = raw[:-1].strip(); mult = 60
    elif raw.endswith("h"):
        raw = raw[:-1].strip(); mult = 3600
    try:
        v = int(raw) * mult
    except Exception:
        raise ValueError
    if v < 0:
        raise ValueError
    return v


def _source_label(src: str) -> str:
    return "单独设置" if src == "key" else "继承默认"


def _limit_brief(name: str) -> str:
    snap = apikey_limiter.key_snapshot(name)
    if not snap.get("enabled", True):
        return "关闭"
    max_c = "∞" if snap.get("unlimited") else str(snap.get("max_concurrent", 0))
    return (
        f"{snap.get('in_flight', 0)}/{max_c} 在途 · "
        f"{snap.get('waiting', 0)}/{snap.get('max_queue', 0)} 排队 · "
        f"最长 {_fmt_duration(int(snap.get('queue_wait_seconds', 0)))}"
    )


_PAGE_SIZE = 4


def _page_callback(page: int) -> str:
    return f"ak:page:{max(1, int(page or 1))}"


def _parse_page(payload: str, default_page: int = 1) -> int:
    raw = (payload or "").strip()
    if not raw or raw == "noop":
        return max(1, int(default_page or 1))
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return max(1, int(default_page or 1))


def _build_pagination_row(current: int, total_pages: int) -> list[dict]:
    if total_pages <= 1:
        return []
    current = max(1, min(int(current or 1), total_pages))
    if total_pages <= 10:
        row: list[dict] = []
        row.append(ui.btn("⬅ 上一页" if current > 1 else "◁ 上一页", _page_callback(current - 1) if current > 1 else "ak:page:noop"))
        row.append(ui.btn(f"{current}/{total_pages}", "ak:page:noop"))
        row.append(ui.btn("➡ 下一页" if current < total_pages else "下一页 ▷", _page_callback(current + 1) if current < total_pages else "ak:page:noop"))
        return row

    window = 2
    lo = max(1, current - window)
    hi = min(total_pages, current + window)
    if hi - lo + 1 < 5:
        if lo == 1:
            hi = min(total_pages, lo + 4)
        else:
            lo = max(1, hi - 4)
    row: list[dict] = []
    if lo > 1:
        row.append(ui.btn("1", _page_callback(1)))
        if lo > 2:
            row.append(ui.btn("…", "ak:page:noop"))
    for p in range(lo, hi + 1):
        row.append(ui.btn(f"[{p}]" if p == current else str(p), "ak:page:noop" if p == current else _page_callback(p)))
    if hi < total_pages:
        if hi < total_pages - 1:
            row.append(ui.btn("…", "ak:page:noop"))
        row.append(ui.btn(str(total_pages), _page_callback(total_pages)))
    return row


def _split_short_page(payload: str, default_page: int = 1) -> tuple[str, int]:
    raw = (payload or "").strip()
    if ":" not in raw:
        return raw, max(1, int(default_page or 1))
    short, _, page_s = raw.rpartition(":")
    try:
        page = max(1, int(page_s))
    except ValueError:
        return raw, max(1, int(default_page or 1))
    return short, page


def _callback_payload(short: str, page: int) -> str:
    return f"{short}:{max(1, int(page or 1))}"


def _short_key_value(key_str: str) -> str:
    key_str = str(key_str or "")
    if len(key_str) <= 24:
        return key_str
    return f"{key_str[:8]}…{key_str[-8:]}"


def _clamp_page(page: int, total: int) -> int:
    total_pages = max(1, math.ceil(total / _PAGE_SIZE)) if total else 1
    return max(1, min(int(page or 1), total_pages))


def _all_key_names() -> list[str]:
    keys = config.get().get("apiKeys") or {}
    return list(keys.keys()) if isinstance(keys, dict) else []


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
            "enabled": True,
            "allowedModels": [],
            "allowImages": False,
            "allowVideos": False,
        }
    config.update(_mutate)


def _set_api_key_value(name: str, api_key: str) -> bool:
    updated = False

    def _mutate(cfg):
        nonlocal updated
        keys = cfg.setdefault("apiKeys", {})
        entry = keys.get(name)
        if isinstance(entry, str):
            keys[name] = {
                "key": api_key,
                "enabled": True,
                "allowedModels": [],
                "allowImages": False,
                "allowVideos": False,
            }
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


def _send_rekeyed(chat_id: int, name: str, api_key: str, page: int = 1) -> None:
    ui.send_result(
        chat_id,
        "✅ <b>API Key 已更新</b>\n\n"
        f"名称: <b>{ui.escape_html(name)}</b>\n"
        f"新 Key: <code>{ui.escape_html(api_key)}</code>\n\n"
        "⚠ 下游客户端需要更新为新 key。",
        back_label="◀ 返回 API Key 管理",
        back_callback=_page_callback(page),
    )


# ─── 列表视图 ─────────────────────────────────────────────────────

def _perm_summary_short(
    allowed: list[str],
    img: bool,
    video: bool,
    enabled: bool = True,
) -> str:
    """列表页用：单行简短权限串。协议入口不再按 Key 限制。"""
    m = "全部模型" if not allowed else f"{len(allowed)} 个模型"
    s = m
    if img:
        s += " · 🖼 图片"
    if video:
        s += " · 🎬 视频"
    if not enabled:
        s += " · ⛔ 停用"
    return s


def _render_list(page: int = 1) -> tuple[str, dict]:
    keys = (config.get().get("apiKeys") or {})
    if not isinstance(keys, dict) or not keys:
        text = "🔑 <b>API Key 管理</b>\n当前: 0 个\n\n暂无 Key，点「➕ 添加」创建。"
        rows = [[ui.btn("➕ 添加", "ak:add")], [ui.btn("◀ 返回主菜单", "menu:main")]]
        return text, ui.inline_kb(rows)

    names = list(keys.keys())
    total = len(names)
    total_pages = max(1, math.ceil(total / _PAGE_SIZE))
    page = max(1, min(int(page or 1), total_pages))
    start = (page - 1) * _PAGE_SIZE
    end = min(start + _PAGE_SIZE, total)
    page_names = names[start:end]

    since_ts = _month_start_ts()
    per: dict[str, dict] = {}
    agg = {"total": 0, "input": 0, "output": 0, "cache_creation": 0, "cache_read": 0}
    for name in names:
        try:
            s = log_db.tokens_for_apikey(name, since_ts=since_ts)
        except Exception:
            s = {"total": 0, "success_count": 0, "error_count": 0, "input": 0,
                 "output": 0, "cache_creation": 0, "cache_read": 0, "avg_tps": None,
                 "max_tps": None, "min_tps": None}
        per[name] = s
        for k in agg:
            agg[k] += int(s.get(k, 0) or 0)
    active = sum(1 for n in names if per[n]["total"] > 0)
    disabled = sum(1 for n in names if isinstance(keys.get(n), dict) and keys[n].get("enabled") is False)
    idle = total - active
    agg_prompt = ui.prompt_total(agg["input"], agg["cache_creation"], agg["cache_read"])

    head = (
        "🔑 <b>API Key 管理</b>\n"
        f"共 {total} 个 · 活跃 {active}"
        + (f" · 停用 {disabled}" if disabled else "")
        + (f" · 闲置 {idle}" if idle else "")
        + f" | 本月 {agg['total']:,} 次"
        + (f" | 第 {page}/{total_pages} 页" if total_pages > 1 else "")
    )
    if agg["total"] > 0:
        head += f" · ↑ {ui.fmt_tokens(agg_prompt)} · ↓ {ui.fmt_tokens(agg['output'])}"

    lines = [head, ""]
    for i, name in enumerate(page_names, start=start + 1):
        entry = keys.get(name)
        if isinstance(entry, str):
            key_str = entry
            allowed: list[str] = []
            img = False
            video = False
            key_enabled = True
        else:
            entry = entry if isinstance(entry, dict) else {}
            key_str = entry.get("key", "")
            allowed = list(entry.get("allowedModels") or [])
            img = bool(entry.get("allowImages"))
            video = bool(entry.get("allowVideos"))
            key_enabled = entry.get("enabled") is not False
        s = per[name]
        dot = "⛔" if not key_enabled else ("🟢" if s["total"] > 0 else "⚪")
        lines.append(f"{i}. {dot} <b>{ui.escape_html(name)}</b>")
        lines.append(f"Key: <code>{ui.escape_html(key_str)}</code>")
        lines.append(f"🏷️ {_perm_summary_short(allowed, img, video, key_enabled)}")
        lines.append(f"🚦 限流: <code>{ui.escape_html(_limit_brief(name))}</code>")
        if s["total"] > 0:
            prompt = ui.prompt_total(s["input"], s["cache_creation"], s["cache_read"])
            stat = f"💎 本月: {s['total']:,} 次 · ↑ {ui.fmt_tokens(prompt)} · ↓ {ui.fmt_tokens(s['output'])}"
            if (s.get("cache_read") or 0) > 0:
                stat += (
                    f" · {ui.fmt_cache_phrase(s['cache_read'], prompt)}"
                    f" · 💵 {ui.fmt_cost(s)}"
                )
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
    for idx in range(0, len(page_names), 2):
        row: list[dict] = []
        for offset, name in enumerate(page_names[idx:idx + 2], start=idx):
            num = start + offset + 1
            row.append(ui.btn(f"{num}. {name}", f"ak:view:{_callback_payload(_short_of(name), page)}"))
        rows.append(row)

    if total_pages > 1:
        pag_row = _build_pagination_row(page, total_pages)
        pag_row.append(ui.btn("↕ 排序", f"ak:sort:{page}"))
        rows.append(pag_row)
    elif total > 1:
        rows.append([ui.btn("↕ 排序", f"ak:sort:{page}")])

    rows.append([ui.btn("➕ 添加", "ak:add"), ui.btn("🔄 刷新", _page_callback(page))])
    rows.append([ui.btn("◀ 返回主菜单", "menu:main")])
    return text, ui.inline_kb(rows)


def show(chat_id: int, message_id: int, cb_id: Optional[str] = None, page: int = 1) -> None:
    if cb_id is not None:
        ui.answer_cb(cb_id)
    text, kb = _render_list(page=page)
    ui.edit(chat_id, message_id, text, reply_markup=kb)


def send_new(chat_id: int, page: int = 1) -> None:
    """命令入口：直接 send 一条新消息（不依赖 message_id）。"""
    text, kb = _render_list(page=page)
    ui.send(chat_id, text, reply_markup=kb)


# ─── 详情视图 ─────────────────────────────────────────────────────

def _render_detail(name: str, page: int = 1) -> tuple[Optional[str], Optional[dict]]:
    entry = _get_entry(name)
    if entry is None:
        return None, None
    key_str = entry.get("key", "")
    allowed = list(entry.get("allowedModels") or [])
    img = bool(entry.get("allowImages"))
    video = bool(entry.get("allowVideos"))
    key_enabled = entry.get("enabled") is not False

    since_ts = _month_start_ts()
    try:
        s = log_db.tokens_for_apikey(name, since_ts=since_ts)
    except Exception:
        s = {"total": 0, "success_count": 0, "error_count": 0, "input": 0,
             "output": 0, "cache_creation": 0, "cache_read": 0, "avg_tps": None,
             "max_tps": None, "min_tps": None}
    active = s["total"] > 0
    dot = "🟢 活跃" if active else "⚪ 闲置"

    lines = [
        f"🔑 <b>{ui.escape_html(name)}</b>  {dot}",
        "",
        f"Key: <code>{ui.escape_html(key_str)}</code>",
        "",
        f"状态: <code>{'enabled' if key_enabled else 'disabled'}</code>",
        f"🎯 模型: <code>{'全部模型（无限制）' if not allowed else str(len(allowed)) + ' 个白名单'}</code>",
        f"🖼 图片接口: <code>{'允许' if img else '禁止'}</code>",
        f"🎬 视频接口: <code>{'允许' if video else '禁止（默认）'}</code>",
        f"🚦 Key 限流: <code>{ui.escape_html(_limit_brief(name))}</code>",
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
            token_line += (
                f" · {ui.fmt_cache_phrase(s['cache_read'], prompt)}"
                f" · 💵 {ui.fmt_cost(s)}"
            )
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
                    model_line += (
                        f" · {ui.fmt_cache_phrase(mrow['cache_read'], m_prompt)}"
                        f" · 💵 {ui.fmt_cost(mrow)}"
                    )
                lines.append(f"  • <code>{model}</code>")
                lines.append(model_line)
            if len(by_model) > 8:
                lines.append(f"  <i>… 其余 {len(by_model) - 8} 个模型未展开</i>")
    else:
        lines.append("<i>本月暂无调用</i>")

    short = _short_of(name)
    payload = _callback_payload(short, page)
    img_label = "🖼 禁用图片接口" if entry.get("allowImages") else "🖼 允许图片接口"
    video_label = "🎬 禁用视频接口" if entry.get("allowVideos") else "🎬 允许视频接口"
    enabled_label = "⛔ 停用 API Key" if key_enabled else "✅ 启用 API Key"
    rows = [
        [ui.btn("🔁 重新生成 key", f"ak:regen:{payload}"),
         ui.btn("✏ 自定义新 key", f"ak:rekey:{payload}")],
        [ui.btn("🎯 编辑允许模型", f"ak:perm:{payload}"),
         ui.btn("🚦 请求限流", f"ak:lim:{payload}")],
        [ui.btn(img_label, f"ak:img:{payload}"),
         ui.btn(video_label, f"ak:vid:{payload}")],
        [ui.btn("🗑 删除", f"ak:del:{payload}"),
         ui.btn(enabled_label, f"ak:enabled:{payload}")],
        [ui.btn("◀ 返回列表", _page_callback(page)),
         ui.btn("🏠 主菜单", "menu:main")],
    ]
    return ui.truncate("\n".join(lines)), ui.inline_kb(rows)


def on_view(chat_id: int, message_id: int, cb_id: str, short: str, page: int = 1) -> None:
    ui.answer_cb(cb_id)
    name = _name_of(short)
    if not name:
        show(chat_id, message_id, page=page)
        return
    text, kb = _render_detail(name, page=page)
    if text is None:
        ui.edit(chat_id, message_id, f"⚠ 未找到 <code>{ui.escape_html(name)}</code>",
                reply_markup=ui.inline_kb([[ui.btn("◀ 返回列表", _page_callback(page))]]))
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


def on_regen_confirm(chat_id: int, message_id: int, cb_id: str, short: str, page: int = 1) -> None:
    ui.answer_cb(cb_id)
    name = _name_of(short)
    entry = _get_entry(name) if name else None
    if entry is None:
        ui.edit(chat_id, message_id, "⚠ 未找到该 Key（可能已被删除）",
                reply_markup=ui.inline_kb([[ui.btn("◀ 返回", _page_callback(page))]]))
        return
    payload = _callback_payload(short, page)
    ui.edit(
        chat_id, message_id,
        f"确认为 <b>{ui.escape_html(name)}</b> 重新生成 key？\n\n"
        "⚠ 当前 key 会被覆盖，下游客户端需要更新。",
        reply_markup=ui.inline_kb([[
            ui.btn("✅ 确认重新生成", f"ak:regen_exec:{payload}"),
            ui.btn("❌ 取消", f"ak:view:{payload}"),
        ]]),
    )


def on_regen_exec(chat_id: int, message_id: int, cb_id: str, short: str, page: int = 1) -> None:
    name = _name_of(short)
    if not name or _get_entry(name) is None:
        ui.answer_cb(cb_id, "未找到 Key")
        show(chat_id, message_id, page=page)
        return
    api_key = _new_generated_key()
    _set_api_key_value(name, api_key)
    ui.answer_cb(cb_id, "已重新生成")
    _send_rekeyed(chat_id, name, api_key, page=page)


def on_rekey_enter(chat_id: int, message_id: int, cb_id: str, short: str, page: int = 1) -> None:
    name = _name_of(short)
    entry = _get_entry(name) if name else None
    if entry is None:
        ui.answer_cb(cb_id, "未找到 Key")
        show(chat_id, message_id, page=page)
        return
    states.set_state(chat_id, "ak_rekey_input", {"name": name, "short": short, "page": page})
    ui.answer_cb(cb_id)
    ui.edit(
        chat_id,
        message_id,
        f"请输入 <b>{ui.escape_html(name)}</b> 的自定义新 key（8-256 字符）：\n\n"
        "允许可见 ASCII 字母数字和 <code>-_.~+/=</code>；不允许空格、换行或控制字符。",
        reply_markup=ui.inline_kb([[ui.btn("❌ 取消", f"ak:view:{_callback_payload(short, page)}")]]),
    )


def on_rekey_input(chat_id: int, text: str) -> None:
    state = states.get_state(chat_id)
    data = (state or {}).get("data", {})
    name = data.get("name")
    page = int(data.get("page") or 1)
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
    _send_rekeyed(chat_id, name, api_key, page=page)


# ─── 删除（二次确认） ────────────────────────────────────────────

def on_del_confirm(chat_id: int, message_id: int, cb_id: str, short: str, page: int = 1) -> None:
    ui.answer_cb(cb_id)
    name = _name_of(short)
    entry = _get_entry(name) if name else None
    if entry is None:
        ui.edit(chat_id, message_id, "⚠ 未找到该 Key（可能已被删除）",
                reply_markup=ui.inline_kb([[ui.btn("◀ 返回", _page_callback(page))]]))
        return
    key_value = entry.get("key", "")
    tail = key_value[-8:] if len(key_value) > 8 else key_value
    ui.edit(
        chat_id, message_id,
        f"确认删除 <b>{ui.escape_html(name)}</b>？\n"
        f"Key 末尾: <code>…{ui.escape_html(tail)}</code>\n"
        f"⚠ 删除后使用该 Key 的下游客户端将立即失效。",
        reply_markup=ui.inline_kb([[
            ui.btn("✅ 确认删除", f"ak:del_exec:{_callback_payload(short, page)}"),
            ui.btn("❌ 取消", f"ak:view:{_callback_payload(short, page)}"),
        ]]),
    )


def on_del_exec(chat_id: int, message_id: int, cb_id: str, short: str, page: int = 1) -> None:
    name = _name_of(short)
    if not name:
        ui.answer_cb(cb_id, "已过期，请重试")
        show(chat_id, message_id, page=page)
        return

    def _mutate(cfg):
        (cfg.get("apiKeys") or {}).pop(name, None)
    config.update(_mutate)
    apikey_limiter.forget_key(name)

    ui.answer_cb(cb_id, "已删除")
    page = _clamp_page(page, len(_all_key_names()))
    ui.edit(
        chat_id, message_id,
        f"✅ 已删除 <code>{ui.escape_html(name)}</code>",
        reply_markup=ui.inline_kb([
            [ui.btn("◀ 返回 API Key 管理", _page_callback(page)),
             ui.btn("🏠 主菜单", "menu:main")],
        ]),
    )


def on_images_toggle(chat_id: int, message_id: int, cb_id: str, short: str, page: int = 1) -> None:
    name = _name_of(short)
    entry = _get_entry(name) if name else None
    if entry is None:
        ui.answer_cb(cb_id, "未找到 Key")
        show(chat_id, message_id, page=page)
        return

    def _mutate(cfg):
        keys = cfg.setdefault("apiKeys", {})
        cur = keys.get(name)
        if isinstance(cur, str):
            cur = {
                "key": cur,
                "enabled": True,
                "allowedModels": [],
                "allowImages": False,
                "allowVideos": False,
            }
            keys[name] = cur
        if isinstance(cur, dict):
            cur["allowImages"] = not bool(cur.get("allowImages", False))
    config.update(_mutate)
    ui.answer_cb(cb_id, "已切换")
    text, kb = _render_detail(name, page=page)
    if text:
        ui.edit(chat_id, message_id, text, reply_markup=kb)


def on_videos_toggle(chat_id: int, message_id: int, cb_id: str, short: str, page: int = 1) -> None:
    name = _name_of(short)
    entry = _get_entry(name) if name else None
    if entry is None:
        ui.answer_cb(cb_id, "未找到 Key")
        show(chat_id, message_id, page=page)
        return

    def _mutate(cfg):
        keys = cfg.setdefault("apiKeys", {})
        cur = keys.get(name)
        if isinstance(cur, str):
            cur = {
                "key": cur,
                "enabled": True,
                "allowedModels": [],
                "allowImages": False,
                "allowVideos": False,
            }
            keys[name] = cur
        if isinstance(cur, dict):
            cur["allowVideos"] = not bool(cur.get("allowVideos", False))
    config.update(_mutate)
    ui.answer_cb(cb_id, "已切换")
    text, kb = _render_detail(name, page=page)
    if text:
        ui.edit(chat_id, message_id, text, reply_markup=kb)


def on_key_enabled_toggle(chat_id: int, message_id: int, cb_id: str, short: str, page: int = 1) -> None:
    name = _name_of(short)
    entry = _get_entry(name) if name else None
    if entry is None:
        ui.answer_cb(cb_id, "未找到 Key")
        show(chat_id, message_id, page=page)
        return

    def _mutate(cfg):
        keys = cfg.setdefault("apiKeys", {})
        cur = keys.get(name)
        if isinstance(cur, str):
            cur = {
                "key": cur,
                "enabled": True,
                "allowedModels": [],
                "allowImages": False,
                "allowVideos": False,
            }
            keys[name] = cur
        if isinstance(cur, dict):
            cur["enabled"] = not (cur.get("enabled") is not False)
    config.update(_mutate)
    ui.answer_cb(cb_id, "已切换")
    text, kb = _render_detail(name, page=page)
    if text:
        ui.edit(chat_id, message_id, text, reply_markup=kb)


# ─── API Key 限流设置 ─────────────────────────────────────────────

_LIMIT_STATE_PREFIX = "ak_limit_edit:"


def _render_limit_detail(name: str, page: int = 1) -> tuple[str, dict]:
    entry = _get_entry(name) or {}
    raw_limits = entry.get("limits") if isinstance(entry.get("limits"), dict) else {}
    snap = apikey_limiter.key_snapshot(name)
    max_c = "不限" if snap.get("unlimited") else str(snap.get("max_concurrent", 0))
    enabled_label = "开" if snap.get("enabled", True) else "关"
    oldest = int(snap.get("oldest_wait_seconds", 0) or 0)
    lines = [
        f"🚦 <b>API Key 请求限流</b>: {ui.escape_html(name)}",
        "",
        "<b>当前有效配置</b>",
        f"• 限流开关: <code>{enabled_label}</code>（{_source_label(str(snap.get('enabled_source')))}）",
        f"• 并发上限: <code>{max_c}</code>（{_source_label(str(snap.get('max_concurrent_source')))}）",
        f"• 队列上限: <code>{snap.get('max_queue', 0)}</code>（{_source_label(str(snap.get('max_queue_source')))}）",
        f"• 最长等待: <code>{_fmt_duration(int(snap.get('queue_wait_seconds', 0)))}</code>（{_source_label(str(snap.get('queue_wait_source')))}）",
        "",
        "<b>实时状态</b>",
        f"• 在途请求: <code>{snap.get('in_flight', 0)} / {max_c}</code>",
        f"• 排队请求: <code>{snap.get('waiting', 0)} / {snap.get('max_queue', 0)}</code>",
        f"• 最久已等待: <code>{_fmt_duration(oldest)}</code>",
        "",
        "<i>并发满后进入 FIFO 队列；队列满、等待超时或客户端断开都会自动移出并返回/结束。</i>",
    ]
    if raw_limits:
        lines.append("")
        lines.append("<i>该 Key 存在单独覆盖；点「♻ 恢复默认」会删除 limits 覆盖。</i>")

    short = _short_of(name)
    toggle_label = "🔴 关闭限流" if snap.get("enabled", True) else "🟢 开启限流"
    rows = [
        [ui.btn(toggle_label, f"ak:lim_toggle:{_callback_payload(short, page)}"), ui.btn("♻ 恢复默认", f"ak:lim_reset:{_callback_payload(short, page)}")],
        [ui.btn("✏ 并发上限", f"ak:lim_edit:concurrent:{_callback_payload(short, page)}"),
         ui.btn("✏ 队列上限", f"ak:lim_edit:queue:{_callback_payload(short, page)}")],
        [ui.btn("✏ 最长等待", f"ak:lim_edit:wait:{_callback_payload(short, page)}"), ui.btn("🔄 刷新", f"ak:lim:{_callback_payload(short, page)}")],
        [ui.btn("◀ 返回 Key 详情", f"ak:view:{_callback_payload(short, page)}")],
    ]
    return "\n".join(lines), ui.inline_kb(rows)


def on_limit_view(chat_id: int, message_id: int, cb_id: str, short: str, page: int = 1) -> None:
    name = _name_of(short)
    if not name or _get_entry(name) is None:
        ui.answer_cb(cb_id, "未找到 Key")
        show(chat_id, message_id, page=page)
        return
    ui.answer_cb(cb_id)
    text, kb = _render_limit_detail(name, page=page)
    ui.edit(chat_id, message_id, text, reply_markup=kb)


def on_limit_toggle(chat_id: int, message_id: int, cb_id: str, short: str, page: int = 1) -> None:
    name = _name_of(short)
    if not name or _get_entry(name) is None:
        ui.answer_cb(cb_id, "未找到 Key")
        show(chat_id, message_id, page=page)
        return
    cur = apikey_limiter.key_snapshot(name).get("enabled", True)
    def _mutate(cfg):
        keys = cfg.setdefault("apiKeys", {})
        entry = keys.get(name)
        if isinstance(entry, str):
            entry = {
                "key": entry,
                "enabled": True,
                "allowedModels": [],
                "allowImages": False,
                "allowVideos": False,
            }
            keys[name] = entry
        if isinstance(entry, dict):
            entry.setdefault("limits", {})["enabled"] = not bool(cur)
    config.update(_mutate)
    ui.answer_cb(cb_id, "已切换")
    text, kb = _render_limit_detail(name, page=page)
    ui.edit(chat_id, message_id, text, reply_markup=kb)


def on_limit_reset(chat_id: int, message_id: int, cb_id: str, short: str, page: int = 1) -> None:
    name = _name_of(short)
    if not name or _get_entry(name) is None:
        ui.answer_cb(cb_id, "未找到 Key")
        show(chat_id, message_id, page=page)
        return
    def _mutate(cfg):
        entry = (cfg.setdefault("apiKeys", {}) or {}).get(name)
        if isinstance(entry, dict):
            entry.pop("limits", None)
    config.update(_mutate)
    ui.answer_cb(cb_id, "已恢复默认")
    text, kb = _render_limit_detail(name, page=page)
    ui.edit(chat_id, message_id, text, reply_markup=kb)


def on_limit_edit(chat_id: int, message_id: int, cb_id: str, field: str, short: str, page: int = 1) -> None:
    name = _name_of(short)
    if not name or _get_entry(name) is None:
        ui.answer_cb(cb_id, "未找到 Key")
        show(chat_id, message_id, page=page)
        return
    labels = {"concurrent": "并发上限", "queue": "队列上限", "wait": "最长等待"}
    if field not in labels:
        ui.answer_cb(cb_id, "字段无效")
        return
    states.set_state(chat_id, f"{_LIMIT_STATE_PREFIX}{field}", {"name": name, "short": short, "page": page})
    ui.answer_cb(cb_id)
    if field == "wait":
        hint = "请输入最长等待时间：\n支持 <code>1800</code>、<code>30m</code>、<code>1h</code>；<code>0</code> 表示不等待；<code>default</code> 表示继承默认。"
    elif field == "concurrent":
        hint = "请输入并发上限（整数 ≥0）：\n<code>0</code> 表示不限；<code>default</code> 表示继承默认。"
    else:
        hint = "请输入队列上限（整数 ≥0）：\n<code>0</code> 表示不排队；<code>default</code> 表示继承默认。"
    ui.edit(chat_id, message_id, hint, reply_markup=ui.inline_kb([[ui.btn("❌ 取消", f"ak:lim:{_callback_payload(short, page)}")]]))


def on_limit_input(chat_id: int, action: str, text: str) -> None:
    state = states.get_state(chat_id)
    data = (state or {}).get("data", {})
    name = data.get("name")
    short = data.get("short") or (_short_of(name) if name else "")
    page = int(data.get("page") or 1)
    field = action.split(":", 1)[1] if ":" in action else ""
    if not name or _get_entry(name) is None or field not in ("concurrent", "queue", "wait"):
        states.pop_state(chat_id)
        ui.send(chat_id, "⚠ 会话已过期，请重新操作。")
        return
    raw = (text or "").strip()
    try:
        if raw.lower() in ("default", "默认", "继承", ""):
            value = None
        elif field == "wait":
            value = _parse_duration(raw)
        else:
            value = int(raw)
            if value < 0:
                raise ValueError
    except Exception:
        ui.send(chat_id, "❌ 输入无效，请重新输入：")
        return

    key_map = {"concurrent": "maxConcurrent", "queue": "maxQueue", "wait": "queueWaitSeconds"}
    def _mutate(cfg):
        keys = cfg.setdefault("apiKeys", {})
        entry = keys.get(name)
        if isinstance(entry, str):
            entry = {
                "key": entry,
                "enabled": True,
                "allowedModels": [],
                "allowImages": False,
                "allowVideos": False,
            }
            keys[name] = entry
        if isinstance(entry, dict):
            limits = entry.setdefault("limits", {})
            k = key_map[field]
            if value is None:
                limits.pop(k, None)
                if not limits:
                    entry.pop("limits", None)
            else:
                limits[k] = value
    config.update(_mutate)
    states.pop_state(chat_id)
    label = "继承默认" if value is None else (_fmt_duration(value) if field == "wait" else ("不限" if field == "concurrent" and value == 0 else str(value)))
    ui.send_result(chat_id, f"✅ {ui.escape_html(name)} 的{ {'concurrent':'并发上限','queue':'队列上限','wait':'最长等待'}[field] }已更新为 <code>{ui.escape_html(label)}</code>", back_label="◀ 返回请求限流", back_callback=f"ak:lim:{_callback_payload(short, page)}")


# ─── 允许模型多选 ────────────────────────────────────────────────

_PERM_STATE = "ak_perm_editing"


def _configured_media_models() -> tuple[list[str], list[str]]:
    xai_cfg = config.get().get("xaiOAuth") or {}
    if not isinstance(xai_cfg, dict):
        return [], []

    def _clean(key: str) -> list[str]:
        raw = xai_cfg.get(key) or []
        if not isinstance(raw, list):
            return []
        out: list[str] = []
        for item in raw:
            model = str(item or "").strip()
            if model and model not in out:
                out.append(model)
        return out

    return _clean("imageModels"), _clean("videoModels")


def _available_permission_models() -> list[str]:
    """文本渠道模型与已配置的 Imagine 媒体模型的稳定并集。"""
    image_models, video_models = _configured_media_models()
    out: list[str] = []
    for model in [*registry.available_models(), *image_models, *video_models]:
        if model and model not in out:
            out.append(model)
    return out


def _permission_model_label(model: str) -> str:
    image_models, video_models = _configured_media_models()
    if model in image_models:
        return f"🖼 {model}"
    if model in video_models:
        return f"🎬 {model}"
    return model


def _render_perm_edit(name: str, models: list[str], checked: set[str]) -> tuple[str, dict]:
    lines = [
        f"🎯 <b>编辑允许模型</b>: {ui.escape_html(name)}",
        "",
        "点击下方模型切换勾选。🖼 为图片模型，🎬 为视频模型。",
        "清空 → 视为无限制；媒体接口仍需单独开启图片/视频权限。",
        f"当前已选: <b>{len(checked)}</b>" + ("（= 不限制）" if not checked else " 个"),
    ]

    rows: list[list[dict]] = []
    cur: list[dict] = []
    for idx, m in enumerate(models):
        mark = "☑" if m in checked else "☐"
        label = _permission_model_label(m)
        cur.append(ui.btn(f"{mark} {label}", f"ak:pt:{_short_of(name)}:{idx}"))
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


def on_perm_enter(chat_id: int, message_id: int, cb_id: str, short: str, page: int = 1) -> None:
    ui.answer_cb(cb_id)
    name = _name_of(short)
    entry = _get_entry(name) if name else None
    if entry is None:
        show(chat_id, message_id)
        return

    models = _available_permission_models()
    if not models:
        ui.edit(
            chat_id, message_id,
            "⚠ 当前无可用渠道/模型，请先添加 OAuth 或渠道。",
            reply_markup=ui.inline_kb([[ui.btn("◀ 返回详情", f"ak:view:{_callback_payload(short, page)}")]]),
        )
        return

    current = set(entry.get("allowedModels") or [])
    # 交集：仅保留仍然存在的模型
    checked = {m for m in current if m in models}
    states.set_state(chat_id, _PERM_STATE, {
        "name": name,
        "page": page,
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
    page = int(data.get("page") or 1)
    if _name_of(short) != name:
        ui.answer_cb(cb_id, "短码不匹配")
        return
    checked = list(data.get("checked") or [])

    def _mutate(cfg):
        keys = cfg.setdefault("apiKeys", {})
        entry = keys.get(name)
        if isinstance(entry, str):
            entry = {
                "key": entry,
                "enabled": True,
                "allowedModels": [],
                "allowImages": False,
                "allowVideos": False,
            }
            keys[name] = entry
        if not isinstance(entry, dict):
            return
        entry["allowedModels"] = checked
    config.update(_mutate)
    states.pop_state(chat_id)

    ui.answer_cb(cb_id, "已保存")
    # 回到详情页
    text, kb = _render_detail(name, page=page)
    if text:
        ui.edit(chat_id, message_id, text, reply_markup=kb)


def on_perm_cancel(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    state = states.get_state(chat_id) or {}
    data = state.get("data") or {}
    page = int(data.get("page") or 1)
    states.pop_state(chat_id)
    ui.answer_cb(cb_id, "已取消")
    name = _name_of(short)
    if not name:
        show(chat_id, message_id, page=page)
        return
    text, kb = _render_detail(name, page=page)
    if text:
        ui.edit(chat_id, message_id, text, reply_markup=kb)


# ─── API Key 排序 ────────────────────────────────────────────────


def _split_number_rows(n: int, max_cols: int = 6) -> list[list[int]]:
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


def _sort_state_data(chat_id: int) -> Optional[dict]:
    st = states.get_state(chat_id)
    if not st or st.get("action") != "ak_sort":
        return None
    return st.get("data") or {}


def _sort_selection_set(data: dict) -> set[int]:
    return {int(x) for x in (data.get("selected") or [])}


def _set_sort_state(chat_id: int, draft: list[str], *, page: int = 1,
                    selected: Optional[set[int]] = None) -> None:
    states.set_state(chat_id, "ak_sort", {
        "draft": list(draft),
        "page": max(1, int(page or 1)),
        "selected": sorted(selected or []),
    })


def _sort_item_line(idx: int, name: str) -> str:
    entry = _get_entry(name)
    if entry is None:
        return f"{idx}. <code>{ui.escape_html(name)}</code> ⚠ 已不存在"
    enabled = entry.get("enabled") is not False
    status = "enabled" if enabled else "disabled"
    try:
        s = log_db.tokens_for_apikey(name, since_ts=_month_start_ts())
        total = int(s.get("total", 0) or 0)
    except Exception:
        total = 0
    stat = f"本月 {total:,} 次" if total else "闲置"
    icon = "🟢" if enabled and total else ("⚪" if enabled else "⛔")
    return f"{idx}. {icon} <code>{ui.escape_html(name)}</code> <code>{status}</code> · {stat}"


def _sort_text_and_kb(draft: list[str], selected: set[int], page: int) -> tuple[str, dict]:
    lines = [
        "↕ <b>API Key 排序</b>",
        "",
        "当前 Key 顺序:",
    ]
    if not draft:
        lines.append("<i>当前没有 API Key。</i>")
    else:
        lines.extend(_sort_item_line(i, name) for i, name in enumerate(draft, start=1))
    lines.extend([
        "",
        "调整方式:",
        "先点下方序号勾选 API Key，再点置顶/置底/上移/下移。",
        "调整完成后记得点保存排序。",
        "返回时保留页码。",
    ])

    rows: list[list[dict]] = []
    for nums in _split_number_rows(len(draft)):
        row = []
        for n in nums:
            row.append(ui.btn(f"{n} ✅" if n in selected else str(n), f"ak:sort_sel:{n}"))
        rows.append(row)
    if draft:
        rows.append([
            ui.btn("🔝 置顶", "ak:sort_mv:top"),
            ui.btn("🔚 置底", "ak:sort_mv:bottom"),
            ui.btn("⬆ 上移", "ak:sort_mv:up"),
            ui.btn("⬇ 下移", "ak:sort_mv:down"),
        ])
    rows.append([ui.btn("还原", "ak:sort_reset"), ui.btn("保存排序", "ak:sort_save")])
    rows.append([ui.btn("◀ 返回 API Key 列表", _page_callback(page)), ui.btn("取消", "ak:sort_cancel")])
    return ui.truncate("\n".join(lines)), ui.inline_kb(rows)


def _show_sort(chat_id: int, message_id: int, cb_id: Optional[str] = None) -> None:
    data = _sort_state_data(chat_id)
    if cb_id is not None:
        ui.answer_cb(cb_id)
    if not data:
        show(chat_id, message_id)
        return
    draft = list(data.get("draft") or [])
    page = max(1, int(data.get("page") or 1))
    selected = _sort_selection_set(data)
    text, kb = _sort_text_and_kb(draft, selected, page)
    ui.edit(chat_id, message_id, text, reply_markup=kb)


def on_sort_start(chat_id: int, message_id: int, cb_id: str, page: int = 1) -> None:
    draft = _all_key_names()
    if not draft:
        ui.answer_cb(cb_id, "当前没有 API Key")
        return
    _set_sort_state(chat_id, draft, page=page)
    _show_sort(chat_id, message_id, cb_id)


def on_sort_select(chat_id: int, message_id: int, cb_id: str, idx_str: str) -> None:
    data = _sort_state_data(chat_id)
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
    selected = _sort_selection_set(data)
    if idx in selected:
        selected.remove(idx)
    else:
        selected.add(idx)
    _set_sort_state(chat_id, draft, page=data.get("page") or 1, selected=selected)
    _show_sort(chat_id, message_id, cb_id)


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


def on_sort_move(chat_id: int, message_id: int, cb_id: str, op: str) -> None:
    data = _sort_state_data(chat_id)
    if not data:
        ui.answer_cb(cb_id, "会话已失效")
        show(chat_id, message_id)
        return
    draft = list(data.get("draft") or [])
    selected = _sort_selection_set(data)
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
    _set_sort_state(chat_id, new_draft, page=data.get("page") or 1, selected=new_sel)
    _show_sort(chat_id, message_id, cb_id)


def on_sort_reset(chat_id: int, message_id: int, cb_id: str) -> None:
    data = _sort_state_data(chat_id) or {}
    page = max(1, int(data.get("page") or 1))
    _set_sort_state(chat_id, _all_key_names(), page=page)
    ui.answer_cb(cb_id, "已还原当前保存顺序")
    _show_sort(chat_id, message_id)


def _save_key_order(draft: list[str]) -> None:
    order = {name: i for i, name in enumerate(draft)}
    def _mutate(cfg):
        keys = cfg.get("apiKeys") or {}
        if not isinstance(keys, dict):
            return
        ordered = {name: keys[name] for name in draft if name in keys}
        rest = {name: entry for name, entry in keys.items() if name not in order}
        cfg["apiKeys"] = {**ordered, **rest}
    config.update(_mutate)


def on_sort_save(chat_id: int, message_id: int, cb_id: str) -> None:
    data = _sort_state_data(chat_id)
    if not data:
        ui.answer_cb(cb_id, "会话已失效")
        show(chat_id, message_id)
        return
    draft = list(data.get("draft") or [])
    page = max(1, int(data.get("page") or 1))
    _save_key_order(draft)
    states.pop_state(chat_id)
    ui.answer_cb(cb_id, "已保存")
    ui.edit(
        chat_id, message_id,
        "✅ 已保存 API Key 排序。",
        reply_markup=ui.inline_kb([
            [ui.btn("继续排序", f"ak:sort:{page}"), ui.btn("返回 API Key 列表", _page_callback(page))],
            [ui.btn("🏠 主菜单", "menu:main")],
        ]),
    )


def on_sort_cancel(chat_id: int, message_id: int, cb_id: str) -> None:
    data = _sort_state_data(chat_id) or {}
    page = max(1, int(data.get("page") or 1))
    states.pop_state(chat_id)
    show(chat_id, message_id, cb_id, page=page)


# ─── 路由分发 ─────────────────────────────────────────────────────

def handle_callback(chat_id: int, message_id: int, cb_id: str, data: str) -> bool:
    if data == "menu:apikey":
        show(chat_id, message_id, cb_id)
        return True
    if data.startswith("ak:page:"):
        page = _parse_page(data.split(":", 2)[2])
        show(chat_id, message_id, cb_id, page=page)
        return True
    if data.startswith("ak:sort_sel:"):
        on_sort_select(chat_id, message_id, cb_id, data.split(":", 2)[2])
        return True
    if data.startswith("ak:sort_mv:"):
        on_sort_move(chat_id, message_id, cb_id, data.split(":", 2)[2])
        return True
    if data == "ak:sort_reset":
        on_sort_reset(chat_id, message_id, cb_id)
        return True
    if data == "ak:sort_save":
        on_sort_save(chat_id, message_id, cb_id)
        return True
    if data == "ak:sort_cancel":
        on_sort_cancel(chat_id, message_id, cb_id)
        return True
    if data.startswith("ak:sort:"):
        on_sort_start(chat_id, message_id, cb_id, _parse_page(data.split(":", 2)[2]))
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
        short, page = _split_short_page(data.split(":", 2)[2])
        on_view(chat_id, message_id, cb_id, short, page)
        return True
    if data.startswith("ak:regen_exec:"):
        short, page = _split_short_page(data.split(":", 2)[2])
        on_regen_exec(chat_id, message_id, cb_id, short, page)
        return True
    if data.startswith("ak:regen:"):
        short, page = _split_short_page(data.split(":", 2)[2])
        on_regen_confirm(chat_id, message_id, cb_id, short, page)
        return True
    if data.startswith("ak:rekey:"):
        short, page = _split_short_page(data.split(":", 2)[2])
        on_rekey_enter(chat_id, message_id, cb_id, short, page)
        return True
    if data.startswith("ak:del_exec:"):
        short, page = _split_short_page(data.split(":", 2)[2])
        on_del_exec(chat_id, message_id, cb_id, short, page)
        return True
    if data.startswith("ak:del:"):
        short, page = _split_short_page(data.split(":", 2)[2])
        on_del_confirm(chat_id, message_id, cb_id, short, page)
        return True
    if data.startswith("ak:img:"):
        short, page = _split_short_page(data.split(":", 2)[2])
        on_images_toggle(chat_id, message_id, cb_id, short, page)
        return True
    if data.startswith("ak:vid:"):
        short, page = _split_short_page(data.split(":", 2)[2])
        on_videos_toggle(chat_id, message_id, cb_id, short, page)
        return True
    if data.startswith("ak:enabled:"):
        short, page = _split_short_page(data.split(":", 2)[2])
        on_key_enabled_toggle(chat_id, message_id, cb_id, short, page)
        return True

    # API Key 限流
    if data.startswith("ak:lim_edit:"):
        parts = data.split(":")
        if len(parts) >= 4:
            short, page = _split_short_page(":".join(parts[3:]))
            on_limit_edit(chat_id, message_id, cb_id, parts[2], short, page)
            return True
    if data.startswith("ak:lim_toggle:"):
        short, page = _split_short_page(data.split(":", 2)[2])
        on_limit_toggle(chat_id, message_id, cb_id, short, page)
        return True
    if data.startswith("ak:lim_reset:"):
        short, page = _split_short_page(data.split(":", 2)[2])
        on_limit_reset(chat_id, message_id, cb_id, short, page)
        return True
    if data.startswith("ak:lim:"):
        short, page = _split_short_page(data.split(":", 2)[2])
        on_limit_view(chat_id, message_id, cb_id, short, page)
        return True

    # 允许模型多选
    if data.startswith("ak:perm:"):
        short, page = _split_short_page(data.split(":", 2)[2])
        on_perm_enter(chat_id, message_id, cb_id, short, page)
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

    return False


def handle_text_state(chat_id: int, action: str, text: str) -> bool:
    """返回 True 表示本模块消费了该输入。"""
    if action.startswith(_LIMIT_STATE_PREFIX):
        on_limit_input(chat_id, action, text)
        return True
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
