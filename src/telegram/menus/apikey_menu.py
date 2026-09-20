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
import secrets  # kept as the shared randomness module patched by frozen tests
from datetime import datetime, timezone
from typing import Optional

from ...management_auth import AuthMethod, ManagementPrincipal
from ...management_control import ManagementContext, ManagementError
from ...management_control.apikey import ApiKeyControl, ApiKeySource
from ...mcp.catalog import TOOL_NAMES as MCP_TOOL_ORDER
from .. import menu_cache, states, ui
from .sort_primitives import (
    move_bottom as _move_bottom,
    move_down as _move_down,
    move_top as _move_top,
    move_up as _move_up,
    split_number_rows as _split_number_rows,
)


_CONTROL = ApiKeyControl()


def _control_context(chat_id: int = 0) -> ManagementContext:
    return ManagementContext(
        request_id=f"telegram-apikey:{chat_id}",
        actor=ManagementPrincipal.administrator(
            subject_id=f"telegram:{chat_id}",
            auth_method=AuthMethod.TELEGRAM_ADMIN,
            issued_at=datetime.now(timezone.utc),
        ),
    )


def _entry_from_view(view) -> dict:
    entry = {
        "key": view.secret or "",
        "enabled": view.enabled,
        "allowedModels": list(view.allowed_models),
        "allowImages": view.allow_images,
        "allowVideos": view.allow_videos,
        "allowMcp": view.allow_mcp,
        "mcpTools": list(view.mcp_tools),
    }
    if view.limit_override is not None:
        limits = {}
        for key, value in (
            ("enabled", view.limit_override.enabled),
            ("maxConcurrent", view.limit_override.max_concurrent),
            ("maxQueue", view.limit_override.max_queue),
            ("queueWaitSeconds", view.limit_override.queue_wait_seconds),
        ):
            if value is not None:
                limits[key] = value
        if limits:
            entry["limits"] = limits
    return entry


def _month_start_ts() -> float:
    return menu_cache.month_start_ts()


def _key_month_stats(name: str) -> Optional[dict]:
    """只读共享本月快照；无数据或冷缓存返回 None。"""
    key = ("period", int(_month_start_ts()))
    snapshot = menu_cache.PERIOD_STATS.peek(key).value or {}
    stats = (snapshot.get("by_apikey") or {}).get(name)
    return stats if stats and stats.get("total", 0) > 0 else None


# ─── 工具 ─────────────────────────────────────────────────────────

def _get_entry(name: str) -> Optional[dict]:
    """读取兼容视图；冻结 TG 仍显示已保存 secret，API 永不调用此路径。"""
    if not name:
        return None
    try:
        view = _CONTROL.get_api_key(
            _control_context(), name, include_secret=True, include_stats=False,
        )
    except ManagementError:
        return None
    return _entry_from_view(view)


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
    snap = _limiter_dict(name)
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
    return [
        item.name for item in _CONTROL.snapshot_api_keys(_control_context())
    ]


def _all_api_key_values(exclude_name: Optional[str] = None) -> list[str]:
    return list(_CONTROL.existing_secret_values(
        _control_context(), exclude_key_id=exclude_name,
    ))


def _validate_custom_key(key: str, existing_keys: list[str]) -> Optional[str]:
    try:
        _CONTROL.validate_custom_secret(key)
    except ManagementError as exc:
        code = exc.fields[0].code if exc.fields else ""
        return {
            "TOO_SHORT": "key 太短，至少 8 个字符。",
            "TOO_LONG": "key 太长，最多 256 个字符。",
            "INVALID_CHARACTERS": "key 含非法字符。仅允许可见 ASCII 字母数字和 -_.~+/=，不允许空格、换行或控制字符。",
        }.get(code, "key 含非法字符。仅允许可见 ASCII 字母数字和 -_.~+/=，不允许空格、换行或控制字符。")
    if key in existing_keys:
        return "key 已被其他 key 使用，请换一个。"
    return None


def _create_api_key_entry(name: str, api_key: str, chat_id: int = 0) -> None:
    _CONTROL.create_api_key(
        _control_context(chat_id), name=name, mode=ApiKeySource.CUSTOM,
        custom_secret=api_key,
    )


def _set_api_key_value(name: str, api_key: str, chat_id: int = 0) -> bool:
    try:
        _CONTROL.replace_api_key_secret(
            _control_context(chat_id), name, custom_secret=api_key,
            if_match=None, require_revision=False, reset_runtime=False,
        )
    except ManagementError:
        return False
    return True


def _limiter_dict(name: str) -> dict:
    snap = _CONTROL.limiter_snapshot(_control_context(), name)
    return {
        "enabled": snap.enabled,
        "in_flight": snap.in_flight,
        "max_concurrent": snap.max_concurrent,
        "max_queue": snap.max_queue,
        "queue_wait_seconds": snap.queue_wait_seconds,
        "waiting": snap.waiting,
        "oldest_wait_seconds": snap.oldest_wait_seconds,
        "unlimited": snap.unlimited,
        "enabled_source": snap.enabled_source,
        "max_concurrent_source": snap.max_concurrent_source,
        "max_queue_source": snap.max_queue_source,
        "queue_wait_source": snap.queue_wait_source,
    }


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
    mcp: bool = False,
) -> str:
    """列表页用：单行简短权限串。协议入口不再按 Key 限制。"""
    m = "全部模型" if not allowed else f"{len(allowed)} 个模型"
    s = m
    if img:
        s += " · 🖼 图片"
    if video:
        s += " · 🎬 视频"
    if mcp:
        s += " · 🔌 MCP"
    if not enabled:
        s += " · ⛔ 停用"
    return s


def _render_list(page: int = 1, *, snapshot: dict | None = None,
                 history_totals: dict[str, int] | None = None) -> tuple[str, dict]:
    listed = _CONTROL.snapshot_api_keys(
        _control_context(), include_secret=True,
    )
    keys = {item.name: _entry_from_view(item) for item in listed}
    if not keys:
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

    # 空列表本身就是完整的零统计；不能让新装页面永久停在“初始化中”。
    period_ready = snapshot is not None or total == 0
    history_ready = history_totals is not None or total == 0
    empty_stats = {"total": 0, "success_count": 0, "error_count": 0, "input": 0,
                   "output": 0, "cache_creation": 0, "cache_read": 0, "avg_tps": None,
                   "max_tps": None, "min_tps": None}
    snapshot_by_key = (snapshot.get("by_apikey") or {}) if snapshot is not None else {}
    per: dict[str, dict] = {
        name: snapshot_by_key.get(name) or dict(empty_stats) for name in names
    }
    agg = {"total": 0, "input": 0, "output": 0, "cache_creation": 0, "cache_read": 0}
    for stats in per.values():
        for key in agg:
            agg[key] += int(stats.get(key, 0) or 0)
    active = sum(1 for n in names if per[n]["total"] > 0)
    disabled = sum(1 for n in names if isinstance(keys.get(n), dict) and keys[n].get("enabled") is False)
    idle = total - active
    agg_prompt = ui.prompt_total(agg["input"], agg["cache_creation"], agg["cache_read"])

    head = "🔑 <b>API Key 管理</b>\n" + f"共 {total} 个"
    if period_ready:
        head += f" · 活跃 {active}"
        head += f" · 停用 {disabled}" if disabled else ""
        head += f" · 闲置 {idle}" if idle else ""
        head += f" | 本月 {agg['total']:,} 次"
        if total_pages > 1:
            head += f" | 第 {page}/{total_pages} 页"
        if agg["total"] > 0:
            head += f" · ↑ {ui.fmt_tokens(agg_prompt)} · ↓ {ui.fmt_tokens(agg['output'])}"
    else:
        head += f" · 停用 {disabled}" if disabled else ""
        head += " | 统计初始化中"
        if total_pages > 1:
            head += f" | 第 {page}/{total_pages} 页"

    lines = [head, ""]
    for i, name in enumerate(page_names, start=start + 1):
        entry = keys.get(name)
        if isinstance(entry, str):
            key_str = entry
            allowed: list[str] = []
            img = False
            video = False
            mcp = False
            key_enabled = True
        else:
            entry = entry if isinstance(entry, dict) else {}
            key_str = entry.get("key", "")
            allowed = list(entry.get("allowedModels") or [])
            img = bool(entry.get("allowImages"))
            video = bool(entry.get("allowVideos"))
            mcp = bool(entry.get("allowMcp"))
            key_enabled = entry.get("enabled") is not False
        s = per[name]
        if not key_enabled:
            dot = "⛔"
        elif not period_ready:
            dot = "⏳"
        else:
            dot = "🟢" if s["total"] > 0 else "⚪"
        lines.append(f"{i}. {dot} <b>{ui.escape_html(name)}</b>")
        lines.append(f"Key: <code>{ui.escape_html(key_str)}</code>")
        lines.append(f"🏷️ {_perm_summary_short(allowed, img, video, key_enabled, mcp)}")
        lines.append(f"🚦 限流: <code>{ui.escape_html(_limit_brief(name))}</code>")
        if not period_ready:
            lines.append("💎 本月: <i>统计初始化中</i>")
        elif s["total"] > 0:
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
            lines.append(f"💵 {ui.fmt_cost(s, decimal_places=3)}")
        elif not history_ready:
            lines.append("💎 本月: <i>闲置</i>（历史统计初始化中）")
        else:
            hist = int(history_totals.get(name, 0) or 0)
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


def _render_cached_list(page: int) -> tuple[str, dict]:
    since = _month_start_ts()
    period = menu_cache.PERIOD_STATS.peek(("period", int(since)))
    history = menu_cache.HISTORY_TOTALS.peek("apikey-history")
    text, kb = _render_list(
        page=page,
        snapshot=period.value,
        history_totals=history.value,
    )
    return menu_cache.with_refreshing_notice(text, period, history), kb


def _list_stats_ready() -> bool:
    if not _all_key_names():
        return True
    since = _month_start_ts()
    return (
        menu_cache.PERIOD_STATS.peek(("period", int(since))).value is not None
        and menu_cache.HISTORY_TOTALS.peek("apikey-history").value is not None
    )


def _schedule_list_stats(
    chat_id: int, message_id: int, token: int, page: int, *,
    redraw_if_ready: bool = False,
) -> None:
    """补齐列表统计；完成后只更新仍停留在本页的消息。"""
    if not _all_key_names():
        return

    def redraw(_value=None, _error=None) -> None:
        if not menu_cache.is_current_view(chat_id, message_id, token):
            return
        text, kb = _render_cached_list(page)
        menu_cache.run_if_current(
            chat_id, message_id, token,
            lambda: ui.edit(chat_id, message_id, text, reply_markup=kb),
        )

    since = _month_start_ts()
    period = menu_cache.PERIOD_STATS.peek(("period", int(since)))
    if period.value is None:
        menu_cache.request_period_snapshot(
            since,
            subscriber=(chat_id, message_id, token, "apikey-period"),
            on_ready=redraw,
            interactive=True,
        )
    elif not period.fresh:
        menu_cache.request_period_snapshot(
            since,
            subscriber=(chat_id, message_id, token, "apikey-period"),
            on_ready=redraw,
            interactive=True,
        )
    history = menu_cache.HISTORY_TOTALS.peek("apikey-history")
    if history.value is None:
        menu_cache.request_apikey_history(
            subscriber=(chat_id, message_id, token, "apikey-history"),
            on_ready=redraw,
            interactive=True,
        )
    elif not history.fresh:
        menu_cache.request_apikey_history(
            subscriber=(chat_id, message_id, token, "apikey-history"),
            on_ready=redraw,
            interactive=True,
        )
    # 快照可能在首次渲染与这里登记订阅之间完成；此时 request() 不会再
    # 登记 waiter，必须主动重绘一次刚才确实以 loading 状态发出的页面。
    if redraw_if_ready and _list_stats_ready():
        redraw()


def show(chat_id: int, message_id: int, cb_id: Optional[str] = None, page: int = 1) -> None:
    if cb_id is not None:
        ui.answer_cb(cb_id)
    token = menu_cache.begin_view(chat_id, message_id)
    rendered_loading = not _list_stats_ready()
    text, kb = _render_cached_list(page)
    ui.edit(chat_id, message_id, text, reply_markup=kb)
    _schedule_list_stats(
        chat_id, message_id, token, page, redraw_if_ready=rendered_loading,
    )


def send_new(chat_id: int, page: int = 1) -> None:
    """命令入口：始终发送可操作页面；冷统计后台补齐并自动更新。"""
    rendered_loading = not _list_stats_ready()
    text, kb = _render_cached_list(page)
    response = ui.send(chat_id, text, reply_markup=kb)
    message = response.get("result") if isinstance(response, dict) and response.get("ok") else None
    if isinstance(message, dict) and message.get("message_id") and _all_key_names():
        since = _month_start_ts()
        period = menu_cache.PERIOD_STATS.peek(("period", int(since)))
        history = menu_cache.HISTORY_TOTALS.peek("apikey-history")
        if rendered_loading or not period.fresh or not history.fresh:
            message_id = int(message["message_id"])
            token = menu_cache.begin_view(chat_id, message_id)
            _schedule_list_stats(
                chat_id, message_id, token, page,
                redraw_if_ready=rendered_loading,
            )


# ─── 详情视图 ─────────────────────────────────────────────────────

def _render_detail(name: str, page: int = 1, *,
                   overall: dict | None = None,
                   by_model: list[dict] | None = None,
                   stats_loading: bool | None = None) -> tuple[Optional[str], Optional[dict]]:
    entry = _get_entry(name)
    if entry is None:
        return None, None
    key_str = entry.get("key", "")
    allowed = list(entry.get("allowedModels") or [])
    img = bool(entry.get("allowImages"))
    video = bool(entry.get("allowVideos"))
    mcp_enabled = bool(entry.get("allowMcp"))
    mcp_selected = [str(item) for item in (entry.get("mcpTools") or []) if isinstance(item, str)]
    key_enabled = entry.get("enabled") is not False

    since_ts = _month_start_ts()
    period_read = menu_cache.PERIOD_STATS.peek(("period", int(since_ts)))
    if stats_loading is None:
        stats_loading = period_read.value is None
    if overall is None and not stats_loading:
        period = period_read.value or {}
        overall = (period.get("by_apikey") or {}).get(name)
    s = overall or {"total": 0, "success_count": 0, "error_count": 0, "input": 0,
                    "output": 0, "cache_creation": 0, "cache_read": 0, "avg_tps": None,
                    "max_tps": None, "min_tps": None}
    active = not stats_loading and s["total"] > 0
    dot = "⏳ 统计初始化中" if stats_loading else ("🟢 活跃" if active else "⚪ 闲置")

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
    if stats_loading:
        lines.append("<i>统计初始化中，管理功能可正常使用。</i>")
    elif active:
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
        lines.append(f"💵 {ui.fmt_cost(s, decimal_places=3)}")
        model_loading = False
        if by_model is None:
            cached_models = menu_cache.DETAIL_STATS.peek(
                ("apikey-model", name, int(since_ts))
            )
            by_model = cached_models.value or []
            model_loading = cached_models.value is None
        if model_loading:
            lines.append("")
            lines.append("按模型: <i>统计初始化中</i>")
        elif by_model:
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
                lines.append(f"    💵 {ui.fmt_cost(mrow, decimal_places=3)}")
            if len(by_model) > 8:
                lines.append(f"  <i>… 其余 {len(by_model) - 8} 个模型未展开</i>")
    else:
        lines.append("<i>本月暂无调用</i>")

    short = _short_of(name)
    payload = _callback_payload(short, page)
    img_label = "🖼 禁用图片接口" if entry.get("allowImages") else "🖼 允许图片接口"
    video_label = "🎬 禁用视频接口" if entry.get("allowVideos") else "🎬 允许视频接口"
    mcp_label = "🔌 禁用 MCP" if mcp_enabled else "🔌 启用 MCP"
    enabled_label = "⛔ 停用 API Key" if key_enabled else "✅ 启用 API Key"
    rows = [
        [ui.btn("🔁 重新生成 key", f"ak:regen:{payload}"),
         ui.btn("✏ 自定义新 key", f"ak:rekey:{payload}")],
        [ui.btn("🎯 编辑允许模型", f"ak:perm:{payload}"),
         ui.btn("🚦 请求限流", f"ak:lim:{payload}")],
        [ui.btn(img_label, f"ak:img:{payload}"),
         ui.btn(video_label, f"ak:vid:{payload}")],
        [ui.btn(mcp_label, f"ak:mcp:{payload}"),
         ui.btn(f"🛠 MCP 工具（{_mcp_tools_label(mcp_selected)}）", f"ak:mt:{payload}")],
        [ui.btn("🗑 删除", f"ak:del:{payload}"),
         ui.btn(enabled_label, f"ak:enabled:{payload}")],
        [ui.btn("◀ 返回列表", _page_callback(page)),
         ui.btn("🏠 主菜单", "menu:main")],
    ]
    return ui.truncate("\n".join(lines)), ui.inline_kb(rows)


def _detail_snapshots(name: str) -> tuple[object, dict | None, object]:
    since = _month_start_ts()
    period = menu_cache.PERIOD_STATS.peek(("period", int(since)))
    overall = (
        (period.value.get("by_apikey") or {}).get(name)
        if period.value is not None else None
    )
    models = menu_cache.DETAIL_STATS.peek(("apikey-model", name, int(since)))
    return period, overall, models


def _detail_stats_ready(name: str) -> bool:
    period, overall, models = _detail_snapshots(name)
    if period.value is None:
        return False
    if int((overall or {}).get("total") or 0) == 0:
        return True
    return models.value is not None


def _schedule_detail_stats(
    chat_id: int, message_id: int, token: int, name: str, page: int, *,
    redraw_if_ready: bool = False,
) -> None:
    since = _month_start_ts()
    model_key = ("apikey-model", name, int(since))

    def redraw(_value=None, error=None) -> None:
        if not menu_cache.is_current_view(chat_id, message_id, token):
            return
        period, overall, models = _detail_snapshots(name)
        text, kb = _render_detail(
            name, page=page, overall=overall, by_model=models.value,
            stats_loading=period.value is None,
        )
        if text is not None:
            menu_cache.run_if_current(
                chat_id, message_id, token,
                lambda: ui.edit(chat_id, message_id, text, reply_markup=kb),
            )
        if error is None:
            subscribe()

    def subscribe() -> None:
        period, overall, models = _detail_snapshots(name)
        if period.value is None:
            read = menu_cache.request_period_snapshot(
                since,
                subscriber=(chat_id, message_id, token, "apikey-detail-period"),
                on_ready=redraw,
                interactive=True,
            )
            if read.fresh and read.value is not None:
                redraw(read.value, None)
            return
        if models.value is None and not int((overall or {}).get("total") or 0):
            menu_cache.DETAIL_STATS.store(model_key, [])
            redraw([], None)
            return
        if models.value is None:
            read = menu_cache.DETAIL_STATS.request(
                model_key,
                lambda: _CONTROL.load_model_stats(
                    _control_context(chat_id), name, since_ts=since,
                ),
                subscriber=(chat_id, message_id, token, "apikey-detail-models"),
                on_ready=redraw,
                interactive=True,
            )
            if read.fresh and read.value is not None:
                redraw(read.value, None)
        elif not models.fresh:
            menu_cache.DETAIL_STATS.request(
                model_key,
                lambda: _CONTROL.load_model_stats(
                    _control_context(chat_id), name, since_ts=since,
                ),
            )

    subscribe()
    if redraw_if_ready and _detail_stats_ready(name):
        redraw()


def _edit_cached_detail(
    chat_id: int, message_id: int, name: str, page: int = 1, *,
    start_view: bool = False,
) -> bool:
    """重绘详情；只有导航或冷统计才接管页面令牌和订阅。"""
    period, overall, models = _detail_snapshots(name)
    cache_loading = not _detail_stats_ready(name)
    text, kb = _render_detail(
        name, page=page, overall=overall, by_model=models.value,
        stats_loading=period.value is None,
    )
    if text is None:
        return False
    rendered_loading = cache_loading and "统计初始化中" in text
    token = None
    if start_view or rendered_loading:
        token = menu_cache.current_view_token(chat_id, message_id)
        if token is None:
            token = menu_cache.begin_view(chat_id, message_id)
    ui.edit(chat_id, message_id, text, reply_markup=kb)
    if token is not None:
        _schedule_detail_stats(
            chat_id, message_id, token, name, page,
            redraw_if_ready=rendered_loading,
        )
    return True


def on_view(chat_id: int, message_id: int, cb_id: str, short: str, page: int = 1) -> None:
    name = _name_of(short)
    if not name:
        ui.answer_cb(cb_id, "API Key 不存在")
        return
    ui.answer_cb(cb_id)
    if not _edit_cached_detail(
        chat_id, message_id, name, page, start_view=True,
    ):
        ui.answer_cb(cb_id, "API Key 不存在")


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
    try:
        _CONTROL.validate_name(name)
    except ManagementError:
        ui.send(chat_id, "❌ 名称无效。允许字符：字母、数字、<code>_ - .</code>；长度 1-64。请重新输入：")
        return
    if _get_entry(name) is not None:
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
    if _get_entry(name) is not None:
        ui.answer_cb(cb_id, "名称已存在")
        states.set_state(chat_id, "ak_add_name")
        ui.edit(chat_id, message_id, f"❌ 名称 <code>{ui.escape_html(name)}</code> 已存在，请重新输入名称：")
        return

    result = _CONTROL.create_api_key(
        _control_context(chat_id), name=name, mode=ApiKeySource.GENERATED,
    )
    api_key = result.secret
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
    if _get_entry(name) is not None:
        ui.send(chat_id, f"❌ 名称 <code>{ui.escape_html(name)}</code> 已存在，请重新添加。")
        states.pop_state(chat_id)
        return
    api_key = text or ""
    err = _validate_custom_key(api_key, _all_api_key_values())
    if err:
        ui.send(chat_id, f"❌ {ui.escape_html(err)}\n请重新输入：")
        return

    _create_api_key_entry(name, api_key, chat_id)
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
    result = _CONTROL.regenerate_api_key(
        _control_context(chat_id), name, require_plan=False, reset_runtime=False,
    )
    api_key = result.secret
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
    _set_api_key_value(name, api_key, chat_id)
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

    _CONTROL.delete_api_key(
        _control_context(chat_id), name, if_match=None,
        require_confirmation=False, missing_ok=True,
    )

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

    _CONTROL.update_api_key(
        _control_context(chat_id), name,
        changes={"allow_images": not bool(entry.get("allowImages", False))},
    )
    ui.answer_cb(cb_id, "已切换")
    _edit_cached_detail(chat_id, message_id, name, page)


def on_videos_toggle(chat_id: int, message_id: int, cb_id: str, short: str, page: int = 1) -> None:
    name = _name_of(short)
    entry = _get_entry(name) if name else None
    if entry is None:
        ui.answer_cb(cb_id, "未找到 Key")
        show(chat_id, message_id, page=page)
        return

    _CONTROL.update_api_key(
        _control_context(chat_id), name,
        changes={"allow_videos": not bool(entry.get("allowVideos", False))},
    )
    ui.answer_cb(cb_id, "已切换")
    _edit_cached_detail(chat_id, message_id, name, page)


def on_key_enabled_toggle(chat_id: int, message_id: int, cb_id: str, short: str, page: int = 1) -> None:
    name = _name_of(short)
    entry = _get_entry(name) if name else None
    if entry is None:
        ui.answer_cb(cb_id, "未找到 Key")
        show(chat_id, message_id, page=page)
        return

    _CONTROL.update_api_key(
        _control_context(chat_id), name,
        changes={"enabled": not (entry.get("enabled") is not False)},
    )
    ui.answer_cb(cb_id, "已切换")
    _edit_cached_detail(chat_id, message_id, name, page)


# ─── MCP 权限与工具选择 ───────────────────────────────────────────

_MCP_STATE = "ak_mcp_editing"

# 工具名到界面文案。名称本身来自 catalog（单一真相源），这里只做展示翻译。
MCP_TOOL_LABELS = {
    "web_search": "网络搜索",
    "web_fetch": "网页抓取",
    "image_generate": "图片生成",
    "image_edit": "图片编辑",
    "video_generate": "视频生成",
    "video_status": "视频查询",
}


def _mcp_tools_label(selected: list[str]) -> str:
    """按钮上的简短状态：未选择 = 跟随全局开关。"""
    return "跟随全局" if not selected else f"{len(selected)}/{len(MCP_TOOL_ORDER)}"


def on_mcp_toggle(chat_id: int, message_id: int, cb_id: str, short: str, page: int = 1) -> None:
    """启用/禁用该 Key 的 MCP 访问。"""
    name = _name_of(short)
    entry = _get_entry(name) if name else None
    if entry is None:
        ui.answer_cb(cb_id, "未找到 Key")
        show(chat_id, message_id, page=page)
        return

    _CONTROL.update_api_key(
        _control_context(chat_id), name,
        changes={"allow_mcp": not bool(entry.get("allowMcp", False))},
    )
    ui.answer_cb(cb_id, "已切换")
    _edit_cached_detail(chat_id, message_id, name, page)


def _render_mcp_tools_edit(name: str, checked: set[str]) -> tuple[str, dict]:
    lines = [
        f"🛠 <b>MCP 工具授权</b>: {ui.escape_html(name)}",
        "",
        "点击工具名切换勾选。未勾选任何工具 = 跟随全局开关（服务端开了就能用）。",
        "勾选后只允许选中的工具；服务端全局关闭的工具无法在此放开。",
        f"当前已选: <b>{len(checked)}</b>" + ("（= 跟随全局）" if not checked else " 个"),
    ]
    rows: list[list[dict]] = []
    for idx, tool in enumerate(MCP_TOOL_ORDER):
        mark = "☑" if tool in checked else "☐"
        label = MCP_TOOL_LABELS.get(tool, tool)
        rows.append([ui.btn(f"{mark} {label}", f"ak:mtg:{_short_of(name)}:{idx}")])
    short = _short_of(name)
    save_label = f"✅ 保存（{len(checked)} 个）" if checked else "✅ 保存（跟随全局）"
    rows.append([
        ui.btn(save_label, f"ak:mts:{short}"),
        ui.btn("🚫 清空(=跟随全局)", f"ak:mtc:{short}"),
    ])
    rows.append([ui.btn("❌ 取消", f"ak:mtx:{short}")])
    return "\n".join(lines), ui.inline_kb(rows)


def on_mcp_tools_enter(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    ui.answer_cb(cb_id)
    name = _name_of(short)
    entry = _get_entry(name) if name else None
    if entry is None:
        ui.answer_cb(cb_id, "未找到 Key")
        return
    checked = {str(item) for item in (entry.get("mcpTools") or []) if isinstance(item, str)}
    states.set_state(chat_id, _MCP_STATE, {"name": name, "checked": sorted(checked)})
    text, kb = _render_mcp_tools_edit(name, checked)
    ui.edit(chat_id, message_id, text, reply_markup=kb)


def _resume_mcp_state(chat_id: int, short: str) -> dict | None:
    state = states.get_state(chat_id)
    if not state or state.get("action") != _MCP_STATE:
        return None
    data = state.get("data") or {}
    if data.get("name") != _name_of(short):
        return None
    return data


def on_mcp_tool_toggle(chat_id: int, message_id: int, cb_id: str, short: str, idx_str: str) -> None:
    data = _resume_mcp_state(chat_id, short)
    if data is None:
        ui.answer_cb(cb_id, "会话已过期")
        return
    try:
        tool = MCP_TOOL_ORDER[int(idx_str)]
    except (ValueError, IndexError):
        ui.answer_cb(cb_id, "索引无效")
        return
    checked = set(data.get("checked") or [])
    if tool in checked:
        checked.discard(tool)
    else:
        checked.add(tool)
    data["checked"] = sorted(checked)
    states.set_state(chat_id, _MCP_STATE, data)
    ui.answer_cb(cb_id)
    text, kb = _render_mcp_tools_edit(data["name"], checked)
    ui.edit(chat_id, message_id, text, reply_markup=kb)


def on_mcp_tools_clear(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    data = _resume_mcp_state(chat_id, short)
    if data is None:
        ui.answer_cb(cb_id, "会话已过期")
        return
    data["checked"] = []
    states.set_state(chat_id, _MCP_STATE, data)
    ui.answer_cb(cb_id, "已清空（= 跟随全局）")
    text, kb = _render_mcp_tools_edit(data["name"], set())
    ui.edit(chat_id, message_id, text, reply_markup=kb)


def on_mcp_tools_save(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    data = _resume_mcp_state(chat_id, short)
    if data is None:
        ui.answer_cb(cb_id, "会话已过期")
        return
    name = data["name"]
    checked = sorted(set(data.get("checked") or []))
    states.pop_state(chat_id)
    try:
        _CONTROL.update_api_key(
            _control_context(chat_id), name, changes={"mcp_tools": checked},
        )
    except Exception as exc:
        ui.answer_cb(cb_id, "保存失败")
        ui.edit(chat_id, message_id,
                f"❌ 保存失败：{ui.escape_html(str(exc))}",
                reply_markup=ui.inline_kb([[ui.btn("◀ 返回详情", f"ak:view:{_callback_payload(short)}")]]))
        return
    ui.answer_cb(cb_id, "已保存")
    _edit_cached_detail(chat_id, message_id, name)


def on_mcp_tools_cancel(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    states.pop_state(chat_id)
    ui.answer_cb(cb_id)
    name = _name_of(short)
    if not name:
        return
    _edit_cached_detail(chat_id, message_id, name)


# ─── API Key 限流设置 ─────────────────────────────────────────────

_LIMIT_STATE_PREFIX = "ak_limit_edit:"


def _render_limit_detail(name: str, page: int = 1) -> tuple[str, dict]:
    entry = _get_entry(name) or {}
    raw_limits = entry.get("limits") if isinstance(entry.get("limits"), dict) else {}
    snap = _limiter_dict(name)
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
    cur = _limiter_dict(name).get("enabled", True)
    _CONTROL.update_api_key(
        _control_context(chat_id), name,
        changes={"limit_override": {"enabled": not bool(cur)}},
    )
    ui.answer_cb(cb_id, "已切换")
    text, kb = _render_limit_detail(name, page=page)
    ui.edit(chat_id, message_id, text, reply_markup=kb)


def on_limit_reset(chat_id: int, message_id: int, cb_id: str, short: str, page: int = 1) -> None:
    name = _name_of(short)
    if not name or _get_entry(name) is None:
        ui.answer_cb(cb_id, "未找到 Key")
        show(chat_id, message_id, page=page)
        return
    _CONTROL.update_api_key(
        _control_context(chat_id), name, changes={"limit_override": None},
    )
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

    key_map = {"concurrent": "max_concurrent", "queue": "max_queue", "wait": "queue_wait_seconds"}
    _CONTROL.update_api_key(
        _control_context(chat_id), name,
        changes={"limit_override": {key_map[field]: value}},
    )
    states.pop_state(chat_id)
    label = "继承默认" if value is None else (_fmt_duration(value) if field == "wait" else ("不限" if field == "concurrent" and value == 0 else str(value)))
    ui.send_result(chat_id, f"✅ {ui.escape_html(name)} 的{ {'concurrent':'并发上限','queue':'队列上限','wait':'最长等待'}[field] }已更新为 <code>{ui.escape_html(label)}</code>", back_label="◀ 返回请求限流", back_callback=f"ak:lim:{_callback_payload(short, page)}")


# ─── 允许模型多选 ────────────────────────────────────────────────

_PERM_STATE = "ak_perm_editing"


def _configured_media_models() -> tuple[list[str], list[str]]:
    images, videos = _CONTROL.configured_media_models(_control_context())
    return list(images), list(videos)


def _available_permission_models() -> list[str]:
    """文本渠道模型与已配置的 Imagine 媒体模型的稳定并集。"""
    return list(_CONTROL.available_permission_models(_control_context()))


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

    _CONTROL.update_api_key(
        _control_context(chat_id), name, changes={"allowed_models": checked},
    )
    states.pop_state(chat_id)

    ui.answer_cb(cb_id, "已保存")
    # 回到详情页
    _edit_cached_detail(chat_id, message_id, name, page)


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
    _edit_cached_detail(chat_id, message_id, name, page)


# ─── API Key 排序 ────────────────────────────────────────────────


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
    stats = _key_month_stats(name)
    total = int((stats or {}).get("total", 0) or 0)
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


def _save_key_order(draft: list[str], chat_id: int = 0) -> None:
    _CONTROL.reorder_api_keys(
        _control_context(chat_id), draft, if_match=None,
        require_revision=False, require_complete=False,
    )


def on_sort_save(chat_id: int, message_id: int, cb_id: str) -> None:
    data = _sort_state_data(chat_id)
    if not data:
        ui.answer_cb(cb_id, "会话已失效")
        show(chat_id, message_id)
        return
    draft = list(data.get("draft") or [])
    page = max(1, int(data.get("page") or 1))
    _save_key_order(draft, chat_id)
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

    # MCP 权限与工具选择
    if data.startswith("ak:mcp:"):
        short, page = _split_short_page(data.split(":", 2)[2])
        on_mcp_toggle(chat_id, message_id, cb_id, short, page)
        return True
    if data.startswith("ak:mtg:"):
        parts = data.split(":")
        if len(parts) >= 4:
            on_mcp_tool_toggle(chat_id, message_id, cb_id, parts[2], parts[3])
            return True
    if data.startswith("ak:mtc:"):
        on_mcp_tools_clear(chat_id, message_id, cb_id, data.split(":", 2)[2])
        return True
    if data.startswith("ak:mts:"):
        on_mcp_tools_save(chat_id, message_id, cb_id, data.split(":", 2)[2])
        return True
    if data.startswith("ak:mtx:"):
        on_mcp_tools_cancel(chat_id, message_id, cb_id, data.split(":", 2)[2])
        return True
    if data.startswith("ak:mt:"):
        short, _page = _split_short_page(data.split(":", 2)[2])
        on_mcp_tools_enter(chat_id, message_id, cb_id, short)
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
