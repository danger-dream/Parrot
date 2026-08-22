"""最近日志菜单 + 单条详情（含重试链）。

callback_data：
  `menu:logs`                  — 显示最近日志第 1 页
  `logs:page:<page>`           — 显示指定页
  `logs:refresh:<page>`        — 刷新指定页
  `logs:query:<state>`         — 打开查询方式二级菜单
  `logs:queryclear:<state>`    — 清空全部查询条件并返回日志列表
  `logs:detail:<short>:<state>` — 查看详情（state 用于返回列表）
  `logs:dpage:<short>:<n>:<state>` — 查看完整详情的第 n 页
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Optional

from ... import config, log_db, oauth_manager
from .. import log_inspector, states, ui


_LIST_PAGE_SIZE = 6
_INSPECT_PAGE_SIZE = 6
_ITEM_PREVIEW_CHARS = 1500


_STATUS_ICON = {"success": "✅", "error": "❌", "cancelled": "⏹", "pending": "⏳"}

def _retry_chain_mark(outcome: str) -> str:
    if outcome == "success":
        return "✅"
    if outcome == "local_web_tool_round":
        return "🔎"
    if outcome == "local_web_tool_limit":
        return "🧭"
    if outcome in ("candidate_guard", "guard_error", "request_invalid"):
        return "🚫"
    if outcome in ("queue_wait", "pending"):
        return "⏳"
    return "❌"


def _attempt_outbound_model(attempt: dict) -> str:
    raw = attempt.get("binding_json")
    if not isinstance(raw, str) or not raw:
        return ""
    try:
        payload = json.loads(raw)
    except Exception:
        return ""
    dispatch = payload.get("dispatch") if isinstance(payload, dict) else None
    value = dispatch.get("outbound_model_id") if isinstance(dispatch, dict) else None
    return str(value or "").strip()


def _retry_chain_label(outcome: str) -> str:
    labels = {
        "success": "成功",
        "local_web_tool_round": "本地搜索轮",
        "local_web_tool_limit": "搜索预算用尽",
        "candidate_guard": "候选不兼容",
        "guard_error": "请求拦截",
        "request_invalid": "请求无效",
    }
    return labels.get(outcome, outcome or "?")


# 入口协议 → 简短标签（anthropic 是默认，不加标签以避免每条日志都冗余显示）
_INGRESS_TAG = {"chat": "[chat]", "responses": "[rsp]", "responses_ws": "[rsp]"}


def _status_icon(row: dict) -> str:
    return _STATUS_ICON.get(row.get("status"), "?")


def _ingress_tag(row: dict) -> str:
    """若入口非 anthropic 则返回 `[chat]`/`[rsp]`，否则空串。"""
    return _INGRESS_TAG.get(row.get("ingress_protocol") or "", "")


def _transport_tag(row: dict) -> str:
    """Transport marker for entries that share the same protocol tag."""
    if (row.get("ingress_protocol") or "") == "responses_ws":
        return "WS"
    if (row.get("ingress_protocol") or "") == "responses" and (row.get("upstream_transport") or "").lower() == "ws":
        return "↑WS"
    return ""


def _fmt_bytes(n) -> str:
    try:
        n = int(n or 0)
    except Exception:
        n = 0
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f}KB"
    return f"{n / (1024 * 1024):.1f}MB"


def _clamp_page(page: int, total: int) -> tuple[int, int]:
    total_pages = max(1, (max(0, int(total or 0)) + _LIST_PAGE_SIZE - 1) // _LIST_PAGE_SIZE)
    try:
        p = int(page or 1)
    except (TypeError, ValueError):
        p = 1
    return max(1, min(p, total_pages)), total_pages


def _norm_values(values) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for v in values or []:
        s = str(v or "").strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _list_state(page: int = 1, *, api_keys=None, models=None, channels=None) -> dict:
    return {
        "p": max(1, int(page or 1)),
        "a": _norm_values(api_keys),
        "m": _norm_values(models),
        "c": _norm_values(channels),
    }


def _normalize_list_state(state: dict | None, *, page: int | None = None) -> dict:
    st = dict(state or {})
    try:
        p = int(page if page is not None else (st.get("p") or 1))
    except Exception:
        p = 1
    return _list_state(
        p,
        api_keys=st.get("a") or [],
        models=st.get("m") or [],
        channels=st.get("c") or [],
    )


def _list_state_code(state: dict) -> str:
    payload = json.dumps(_normalize_list_state(state), ensure_ascii=False, separators=(",", ":"))
    return ui.register_code("loglist:" + payload)


def _resolve_list_state(short: str) -> dict | None:
    full = ui.resolve_code(short)
    if not full or not full.startswith("loglist:"):
        return None
    try:
        obj = json.loads(full[len("loglist:"):])
    except Exception:
        return None
    return _normalize_list_state(obj if isinstance(obj, dict) else {})


def _list_cb(state: dict, *, page: int | None = None) -> str:
    st = _normalize_list_state(state, page=page)
    return f"logs:list:{_list_state_code(st)}"


def _active_filters(state: dict) -> dict:
    st = _normalize_list_state(state)
    return {"api_keys": st["a"], "models": st["m"], "channel_keys": st["c"]}


def _page_rows(state: dict) -> tuple[list[dict], int, int, int, dict]:
    st = _normalize_list_state(state)
    filters = _active_filters(st)
    total = log_db.recent_logs_count(**filters)
    page, total_pages = _clamp_page(st["p"], total)
    st["p"] = page
    rows = log_db.recent_logs(_LIST_PAGE_SIZE, offset=(page - 1) * _LIST_PAGE_SIZE, **filters)
    return rows, total, page, total_pages, st


def _maybe_suffix_status_banner(text: str) -> str:
    """在文本底部追加 banner：上游故障 + 新版本可用（任一存在即拼到末尾）。"""
    extras: list[str] = []
    try:
        from ... import status_monitor
        line = status_monitor.get_active_summary()
        if line:
            extras.append(line)
    except Exception:
        pass
    try:
        from ... import update_checker
        line = update_checker.get_update_banner()
        if line:
            extras.append(line)
    except Exception:
        pass
    if not extras:
        return text
    return text + "\n\n" + "\n".join(extras)


def _display_index(page: int, idx: int) -> int:
    try:
        p = max(1, int(page or 1))
    except (TypeError, ValueError):
        p = 1
    return (p - 1) * _LIST_PAGE_SIZE + int(idx)


def _option_label(kind: str, value: str) -> str:
    if kind == "channel":
        name = ui.channel_display_name(value, with_family=False)
        if str(value or "").startswith("oauth:"):
            return f"🔐 {name}"
        return f"📡 {name}"
    return value


def _button_label(text: str, *, max_len: int = 18) -> str:
    text = str(text or "?")
    return text if len(text) <= max_len else text[:max_len - 1] + "…"


def _filter_summary(kind: str, values: list[str]) -> str:
    vals = _norm_values(values)
    if not vals:
        return "全部"
    first = _option_label(kind, vals[0])
    if len(vals) == 1:
        return _button_label(first, max_len=16)
    return f"{_button_label(first, max_len=12)} + {len(vals) - 1}"


def _api_key_options() -> list[str]:
    opts = []
    try:
        opts.extend((config.get().get("apiKeys") or {}).keys())
    except Exception:
        pass
    opts.extend(log_db.recent_log_values("apikey"))
    return _norm_values(opts)


def _model_options() -> list[str]:
    # parrot-test* 是内部/历史测试模型（如上下文溢出探针），不展示给用户筛选。
    return [m for m in _norm_values(log_db.recent_log_values("model"))
            if not m.startswith("parrot-test")]


def _channel_options() -> list[str]:
    """渠道筛选只展示当前配置里的 API 渠道 + OAuth 账号。

    历史日志里会出现 compact-rescue、__tmp_context_* 等内部/临时 channel key，
    这些不属于用户可选渠道，不能混进筛选菜单。
    """
    opts: list[str] = []
    try:
        for ch in config.get().get("channels") or []:
            name = str(ch.get("name") or "").strip()
            if name:
                opts.append(f"api:{name}")
    except Exception:
        pass
    try:
        for acc in oauth_manager.list_accounts():
            ak = oauth_manager._account_key(acc)
            if ak:
                opts.append(f"oauth:{ak}")
    except Exception:
        pass
    return _norm_values(opts)


def _filter_options(kind: str) -> list[str]:
    if kind == "apikey":
        return _api_key_options()
    if kind == "model":
        return _model_options()
    if kind == "channel":
        return _channel_options()
    return []


def _filter_field(kind: str) -> str:
    return {"apikey": "a", "model": "m", "channel": "c"}.get(kind, "")


def _filter_title(kind: str) -> str:
    return {
        "apikey": "🔑 按 API KEY 账号筛选日志",
        "model": "🤖 按模型筛选日志",
        "channel": "📡 按渠道筛选日志",
    }.get(kind, "筛选日志")


def _filter_current_label(kind: str, values: list[str]) -> str:
    name = {"apikey": "账号", "model": "模型", "channel": "渠道"}.get(kind, "筛选")
    return f"当前{name}: {_filter_summary(kind, values)}"


def _render_list(rows: list[dict], *, page: int = 1, total: int | None = None, total_pages: int | None = None) -> str:
    total = len(rows) if total is None else int(total or 0)
    total_pages = max(1, int(total_pages or 1))
    if not rows:
        return "📋 <b>最近日志 · 请求日志</b>\n\n暂无记录。"
    lines = [
        f"📋 <b>最近日志 · 请求日志 · 第 {page}/{total_pages} 页 · 共 {total} 条</b>",
        "<i>对照下方按钮的 #编号 点击查看详情</i>",
    ]
    for idx, r in enumerate(rows, 1):
        display_idx = _display_index(page, idx)
        prefix = f"\n<b>#{display_idx}</b> "
        headline = ui.fmt_log_entry_headline(r, prefix=prefix)
        body = ui.fmt_log_entry_body(r)
        line = headline
        if body:
            line += "\n" + body
        lines.append(line)
    return "\n".join(lines)


def _extract_error_summary(raw: str) -> str:
    """从错误文本中提取简洁摘要（HTTP 5xx 的 JSON 尝试解包）。"""
    if not raw:
        return "未知错误"
    prefix = ""
    json_part = raw
    if raw.startswith("HTTP "):
        colon_idx = raw.find(": ")
        if colon_idx > 0:
            prefix = raw[:colon_idx]
            json_part = raw[colon_idx + 2:]
        else:
            return raw
    try:
        obj = json.loads(json_part)
    except Exception:
        return raw
    err = obj.get("error") if isinstance(obj, dict) else None
    if isinstance(err, dict):
        msg = err.get("message", "")
        if msg:
            t = err.get("type") or ""
            summary = f"{t}: {msg}" if t else msg
            return f"{prefix} — {summary}" if prefix else summary
    return f"{prefix} — {json_part}" if prefix else json_part


def _list_kb(rows: list[dict], *, state: dict, page: int, total_pages: int) -> dict:
    """详情按钮 3 列紧凑排列 + 状态保持分页/筛选。"""
    state = _normalize_list_state(state, page=page)
    state_code = _list_state_code(state)
    rows_kb: list[list[dict]] = [[
        ui.btn("✅ 💬 请求日志", _list_cb(state, page=page)),
        ui.btn("🎞 多媒体日志", "media:logs"),
    ]]
    cur: list[dict] = []
    for idx, r in enumerate(rows, 1):
        display_idx = _display_index(page, idx)
        rid = r.get("request_id") or ""
        if not rid:
            continue
        short = ui.register_code(rid)
        cur.append(ui.btn(f"📄 #{display_idx}", f"logs:detail:{short}:{state_code}"))
        if len(cur) >= 3:
            rows_kb.append(cur)
            cur = []
    if cur:
        rows_kb.append(cur)

    prev_page = max(1, page - 1)
    next_page = min(total_pages, page + 1)
    rows_kb.append([
        ui.btn("🏠 首页", _list_cb(state, page=1)),
        ui.btn("◀ 上一页", _list_cb(state, page=prev_page)),
        ui.btn(f"{page}/{total_pages}", _list_cb(state, page=page)),
        ui.btn("下一页 ▶", _list_cb(state, page=next_page)),
    ])
    rows_kb.append([
        ui.btn("🔄 刷新", _list_cb(state, page=page)),
        ui.btn("🔎 查询", f"logs:query:{state_code}"),
        ui.btn("◀ 返回主菜单", "menu:main"),
    ])
    return ui.inline_kb(rows_kb)


# ─── 列表入口 ─────────────────────────────────────────────────────

def show(chat_id: int, message_id: int, cb_id: Optional[str] = None,
         page: int = 1, state: dict | None = None) -> None:
    if cb_id is not None:
        ui.answer_cb(cb_id)
    st = _normalize_list_state(state, page=page if state is None else None)
    rows, total, page, total_pages, st = _page_rows(st)
    ui.edit(chat_id, message_id, ui.truncate(_maybe_suffix_status_banner(_render_list(rows, page=page, total=total, total_pages=total_pages))),
            reply_markup=_list_kb(rows, state=st, page=page, total_pages=total_pages))


def send_new(chat_id: int) -> None:
    st = _list_state(1)
    rows, total, page, total_pages, st = _page_rows(st)
    ui.send(chat_id, ui.truncate(_maybe_suffix_status_banner(_render_list(rows, page=page, total=total, total_pages=total_pages))),
            reply_markup=_list_kb(rows, state=st, page=page, total_pages=total_pages))


def refresh(chat_id: int, message_id: int, cb_id: str,
            page: int = 1, state: dict | None = None) -> None:
    show(chat_id, message_id, cb_id, page=page, state=state)


def _render_query_menu_text(state: dict) -> str:
    st = _normalize_list_state(state)
    return (
        "🔎 <b>查询日志</b>\n\n"
        "请选择查询方式。当前条件：\n"
        f"🔑 账号：{ui.escape_html(_filter_summary('apikey', st['a']))}\n"
        f"🤖 模型：{ui.escape_html(_filter_summary('model', st['m']))}\n"
        f"📡 渠道：{ui.escape_html(_filter_summary('channel', st['c']))}"
    )


def _query_menu_kb(state: dict) -> dict:
    st = _normalize_list_state(state)
    state_code = _list_state_code(st)
    return ui.inline_kb([
        [
            ui.btn("🔑 账号", f"logs:filter:apikey:{state_code}"),
            ui.btn("🤖 模型", f"logs:filter:model:{state_code}"),
            ui.btn("📡 渠道", f"logs:filter:channel:{state_code}"),
        ],
        [ui.btn("🧹 清空查询条件", f"logs:queryclear:{state_code}")],
        [ui.btn("◀ 返回日志", _list_cb(st))],
    ])


def _clear_query_filters(chat_id: int, message_id: int, cb_id: str, state_short: str) -> None:
    if not _resolve_list_state(state_short):
        ui.answer_cb(cb_id, "查询状态已失效")
        return
    show(chat_id, message_id, cb_id, state=_list_state(1))


def _show_query_menu(chat_id: int, message_id: int, cb_id: str | None, state: dict) -> None:
    if cb_id is not None:
        ui.answer_cb(cb_id)
    st = _normalize_list_state(state)
    ui.edit(chat_id, message_id, _render_query_menu_text(st), reply_markup=_query_menu_kb(st))


def _filter_state_code(kind: str, base: dict, draft: dict) -> str:
    payload = json.dumps({
        "k": kind,
        "base": _normalize_list_state(base),
        "draft": _normalize_list_state(draft),
    }, ensure_ascii=False, separators=(",", ":"))
    return ui.register_code("logfilter:" + payload)


def _resolve_filter_state(short: str) -> dict | None:
    full = ui.resolve_code(short)
    if not full or not full.startswith("logfilter:"):
        return None
    try:
        obj = json.loads(full[len("logfilter:"):])
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    kind = str(obj.get("k") or "")
    if kind not in {"apikey", "model", "channel"}:
        return None
    return {
        "k": kind,
        "base": _normalize_list_state(obj.get("base") or {}),
        "draft": _normalize_list_state(obj.get("draft") or {}),
    }


def _render_filter_menu_text(kind: str, draft: dict) -> str:
    field = _filter_field(kind)
    return (
        f"{_filter_title(kind)}\n\n"
        f"{_filter_current_label(kind, draft.get(field) or [])}"
    )


def _filter_menu_kb(kind: str, base: dict, draft: dict) -> dict:
    field = _filter_field(kind)
    selected = set(_norm_values(draft.get(field) or []))
    options = _filter_options(kind)
    rows: list[list[dict]] = []
    buttons: list[dict] = []
    for value in options[:90]:
        label = _button_label(_option_label(kind, value), max_len=24 if kind == "channel" else 16)
        mark = "✅ " if value in selected else ""
        code = _filter_state_code(kind, base, draft)
        v_code = ui.register_code(value)
        callback = f"logs:ftoggle:{kind}:{v_code}:{code}"
        if kind == "channel" and ui.channel_provider(value):
            buttons.append(ui.btn(
                mark + label,
                callback,
                icon_custom_emoji_id=ui.channel_provider_custom_emoji_id(value),
            ))
        else:
            buttons.append(ui.btn(mark + label, callback))
    _append_button_grid(rows, buttons, cols=2 if kind == "channel" else 3)
    code = _filter_state_code(kind, base, draft)
    rows.append([
        ui.btn("取消", f"logs:faction:{kind}:cancel:{code}"),
        ui.btn("全选", f"logs:faction:{kind}:all:{code}"),
        ui.btn("反选", f"logs:faction:{kind}:invert:{code}"),
        ui.btn("确认", f"logs:faction:{kind}:confirm:{code}"),
    ])
    return ui.inline_kb(rows)


def _show_filter_menu(chat_id: int, message_id: int, cb_id: str | None,
                      kind: str, base: dict, draft: dict | None = None) -> None:
    if cb_id is not None:
        ui.answer_cb(cb_id)
    draft = _normalize_list_state(draft or base)
    ui.edit(
        chat_id, message_id,
        _render_filter_menu_text(kind, draft),
        reply_markup=_filter_menu_kb(kind, _normalize_list_state(base), draft),
    )


def _toggle_filter_value(chat_id: int, message_id: int, cb_id: str,
                         kind: str, value_short: str, state_short: str) -> None:
    st = _resolve_filter_state(state_short)
    value = ui.resolve_code(value_short)
    if not st or not value:
        ui.answer_cb(cb_id, "筛选状态已失效")
        return
    draft = _normalize_list_state(st["draft"])
    field = _filter_field(kind)
    values = _norm_values(draft.get(field) or [])
    if value in values:
        values = [v for v in values if v != value]
    else:
        values.append(value)
    draft[field] = values
    _show_filter_menu(chat_id, message_id, cb_id, kind, st["base"], draft)


def _filter_action(chat_id: int, message_id: int, cb_id: str,
                   kind: str, action: str, state_short: str) -> None:
    st = _resolve_filter_state(state_short)
    if not st:
        ui.answer_cb(cb_id, "筛选状态已失效")
        return
    base = _normalize_list_state(st["base"])
    draft = _normalize_list_state(st["draft"])
    field = _filter_field(kind)
    if action == "cancel":
        show(chat_id, message_id, cb_id, state=base)
        return
    if action == "confirm":
        draft["p"] = 1
        show(chat_id, message_id, cb_id, state=draft)
        return
    options = _filter_options(kind)
    if action == "all":
        draft[field] = options
    elif action == "invert":
        selected = set(_norm_values(draft.get(field) or []))
        draft[field] = [v for v in options if v not in selected]
    _show_filter_menu(chat_id, message_id, cb_id, kind, base, draft)


# ─── 详情 ─────────────────────────────────────────────────────────

def _detail_inline(value: object, fallback: str = "?") -> str:
    text = str(value if value not in (None, "") else fallback)
    return " ".join(text.splitlines())


def _detail_metric_line(row: dict, *, request: bool = False,
                        first_applicable: bool = True) -> str | None:
    keys = (
        ("连接", "connect_time_ms" if request else "connect_ms"),
        ("首字", "first_token_time_ms" if request else "first_byte_ms"),
        ("空闲", "idle_time_ms" if request else "idle_ms"),
        ("总计", "total_time_ms" if request else "total_ms"),
    )
    parts = []
    for label, key in keys:
        if label == "首字" and not first_applicable:
            continue
        value = row.get(key)
        if value is not None:
            parts.append(f"{label} {ui.fmt_ms(value)}")
    return " · ".join(parts) if parts else None


def _detail_stage_line(row: dict) -> str | None:
    stage_keys = [
        ("DNS", "dns_ms"),
        ("TCP", "tcp_ms"),
        ("代理 TCP", "proxy_tcp_ms"),
        ("代理隧道", "proxy_tunnel_ms"),
    ]
    # Current round snapshots expose target_tls_ms and keep tls_ms as a
    # compatibility alias.  Show one reliable phase, never duplicate it.
    if row.get("target_tls_ms") is not None:
        stage_keys.append(("目标 TLS", "target_tls_ms"))
    else:
        stage_keys.append(("TLS", "tls_ms"))
    stage_keys.extend([
        ("WS 握手", "ws_handshake_ms"),
        ("请求上传", "request_upload_ms"),
        ("等待响应头", "response_headers_wait_ms"),
        ("头后首 Body", "response_body_first_byte_wait_ms"),
    ])
    parts = [
        f"{label} {ui.fmt_ms(row.get(key))}"
        for label, key in stage_keys
        if row.get(key) is not None
    ]
    return " · ".join(parts) if parts else None


def _append_detail_text(lines: list[str], raw: object, *, indent: str = "", italic: bool = False) -> None:
    """Append all text without loss; chunks are escaped and independently tagged."""
    text = str(raw or "")
    physical_lines = text.splitlines() or [""]
    for physical in physical_lines:
        chunks = [physical[i:i + 500] for i in range(0, len(physical), 500)] or [""]
        for chunk in chunks:
            escaped = ui.escape_html(chunk)
            lines.append(f"{indent}<i>{escaped}</i>" if italic else f"{indent}{escaped}")


def _append_round_detail(lines: list[str], p: dict, *, nested: bool = True) -> None:
    indent = "     " if nested else "  "
    order = p.get("attempt_order") or "?"
    pname = ui.escape_html(_detail_inline(p.get("proxy_name") or "direct"))
    outcome = _detail_inline(p.get("outcome"))
    mark = "✅" if outcome in ("success", "connected") else "❌"
    lines.append(f"{indent}{mark} <b>轮次 {order}</b> · <code>{pname}</code> — {ui.escape_html(outcome)}")
    identity = []
    if p.get("round_id"):
        identity.append(f"ID <code>{ui.escape_html(_detail_inline(p['round_id']))}</code>")
    if p.get("transport"):
        identity.append(f"传输 <code>{ui.escape_html(_detail_inline(p['transport']))}</code>")
    if p.get("request_mode"):
        identity.append(f"模式 <code>{ui.escape_html(_detail_inline(p['request_mode']))}</code>")
    if identity:
        lines.append(f"{indent}   · " + " · ".join(identity))
    metrics = _detail_metric_line(
        p,
        first_applicable=p.get("request_mode") != "http_non_stream",
    )
    if metrics:
        lines.append(f"{indent}   · 业务计时: {metrics}")
    stages = _detail_stage_line(p)
    if stages:
        lines.append(f"{indent}   · 可靠阶段（可能重叠，不相加）: {stages}")
    byte_sum = int(p.get("bytes_up") or 0) + int(p.get("bytes_down") or 0)
    if byte_sum:
        lines.append(f"{indent}   · 流量 {_fmt_bytes(byte_sum)}")
    if p.get("error_detail"):
        _append_detail_text(
            lines,
            _extract_error_summary(str(p["error_detail"])),
            indent=f"{indent}   ⚠ ",
            italic=True,
        )


_BILLING_COMPONENTS = (
    ("input_tokens", "input_per_token", "输入"),
    ("output_tokens", "output_per_token", "输出"),
    ("cache_creation_tokens", "cache_write_per_token", "缓存写入"),
    ("cache_read_tokens", "cache_read_per_token", "缓存读取"),
)
_ANTHROPIC_TTL_BILLING_COMPONENTS = (
    ("input_tokens", "input_per_token", "输入"),
    ("output_tokens", "output_per_token", "输出"),
    ("cache_creation_5m_tokens", "cache_write_5m_per_token", "缓存写入 5m"),
    ("cache_creation_1h_tokens", "cache_write_1h_per_token", "缓存写入 1h"),
    ("cache_read_tokens", "cache_read_per_token", "缓存读取"),
)


def _fmt_price_per_million(price_per_token: object) -> str | None:
    try:
        value = Decimal(str(price_per_token)) * Decimal(1_000_000)
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not value.is_finite() or value < 0:
        return None
    text = format(value, ",f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return f"${text}/M"


def _attempt_cost_formula(attempt: dict) -> str | None:
    if str(attempt.get("cost_source") or "") != "estimated":
        return None
    try:
        snapshot = json.loads(attempt.get("pricing_snapshot_json") or "")
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(snapshot, dict):
        return None

    split_known = (
        attempt.get("cache_creation_5m_tokens") is not None
        and attempt.get("cache_creation_1h_tokens") is not None
    )
    components = (
        _ANTHROPIC_TTL_BILLING_COMPONENTS if split_known else _BILLING_COMPONENTS
    )
    terms: list[str] = []
    for token_key, price_key, label in components:
        try:
            tokens = int(attempt.get(token_key) or 0)
        except (TypeError, ValueError, OverflowError):
            return None
        price = _fmt_price_per_million(snapshot.get(price_key))
        if tokens > 0 and price is not None and price != "$0/M":
            terms.append(f"{label} {tokens:,} × {price}")
    return " + ".join(terms) or None


def _billing_detail_lines(cost_metrics: dict, attempts: list[dict]) -> list[str]:
    total = ui.fmt_cost(cost_metrics, decimal_places=3)
    priced = [
        row for row in attempts
        if isinstance(row, dict)
        and row.get("cost_ticks") is not None
        and str(row.get("cost_source") or "") in {"actual", "estimated"}
    ]
    if len(priced) == 1:
        formula = _attempt_cost_formula(priced[0])
        cursor_official = bool(priced[0].get("cursor_event_key"))
        suffix = "（Cursor 官方事件）" if cursor_official else ""
        return [f"💵 {total}{suffix}" + (f" = {formula}" if formula else "")]

    lines = [f"💵 {total}"]
    for index, row in enumerate(priced, start=1):
        try:
            ticks = max(0, int(row.get("cost_ticks") or 0))
        except (TypeError, ValueError, OverflowError):
            continue
        amount = ui.fmt_usd(
            Decimal(ticks) / Decimal(10_000_000_000), decimal_places=3,
        )
        order = row.get("attempt_order") or index
        line = f"  尝试 {order}: {amount}"
        formula = _attempt_cost_formula(row)
        if formula:
            line += f" = {formula}"
        lines.append(line)
    return lines


def _render_detail(detail: dict) -> str:
    log = detail.get("log") or {}
    chain = detail.get("retry_chain") or []
    proxy_chain = detail.get("proxy_chain") or []
    local_web_log = detail.get("local_web_log") or []
    billing_attempts = detail.get("billing_attempts") or []

    rid = _detail_inline(log.get("request_id"))
    created = ui.fmt_bjt_ts(log.get("created_at"), "%Y-%m-%d %H:%M:%S")
    status = _detail_inline(log.get("status"))
    lines = [
        "📋 <b>日志详情</b>",
        f"ID: <code>{ui.escape_html(rid)}</code>",
        f"时间: <code>{created}</code>",
        f"状态: <code>{ui.escape_html(status)}</code>"
        + (f" ({log.get('http_status')})" if log.get("http_status") else "")
        + f" {_status_icon(log)}",
        f"客户端: <code>{ui.escape_html(_detail_inline(log.get('client_ip')))}</code>"
        f" / Key <code>{ui.escape_html(_detail_inline(log.get('api_key_name')))}</code>",
        f"请求模型: <code>{ui.escape_html(_detail_inline(log.get('requested_model')))}</code>",
    ]
    if log.get("final_channel_key"):
        channel_key = log["final_channel_key"]
        final_ch = ui.channel_display_name(channel_key, with_family=False)
        provider_icon = ui.channel_provider_custom_emoji_html(channel_key)
        prefix = f"{provider_icon} " if provider_icon else ""
        lines.append(
            f"最终渠道: {prefix}<code>{ui.escape_html(_detail_inline(final_ch))}</code>"
            f" / <code>{ui.escape_html(_detail_inline(log.get('final_model')))}</code>"
        )
    if log.get("proxy_name"):
        lines.append(f"出站代理: 🔀 <code>{ui.escape_html(_detail_inline(log['proxy_name']))}</code>")
    ingress = log.get("ingress_protocol")
    upstream_proto = log.get("upstream_protocol")
    if ingress or upstream_proto:
        lines.append(
            f"协议: 入口 <code>{ui.escape_html(_detail_inline(ingress))}</code>"
            f" → 上游 <code>{ui.escape_html(_detail_inline(upstream_proto))}</code>"
            + (f" / <code>{ui.escape_html(_detail_inline(log.get('upstream_transport')))}</code>" if log.get("upstream_transport") else "")
        )
    flags = []
    if log.get("is_stream"):
        flags.append("stream")
    if log.get("affinity_hit"):
        flags.append("亲和命中")
    if log.get("retry_count"):
        flags.append(f"重试 {log['retry_count']} 次")
    if flags:
        lines.append(" · ".join(flags))
    if log.get("reasoning_effort"):
        lines.append(f"思考强度：🧠 {ui.escape_html(_detail_inline(log['reasoning_effort']))}")
    fast_badge = ui.log_fast_mode_badge(log)
    if fast_badge:
        lines.append(f"模式：{fast_badge}")

    cost_row = dict(log)
    stored_detail = detail.get("detail") or {}
    if isinstance(stored_detail, dict):
        cost_row["response_body"] = stored_detail.get("response_body")
    cost_metrics = log_db.cost_for_log(cost_row)

    if status == "success":
        lines.extend(["", "<b>Tokens</b>"])
        token_line = f"↑ {ui.fmt_tokens(ui.prompt_total_from_row(log))} | ↓ {ui.fmt_tokens(log.get('output_tokens'))}"
        if (log.get("cache_read_tokens") or 0) > 0:
            token_line += f" | {ui.fmt_cache_phrase_from_row(log)}"
        lines.append(token_line)
        if log.get("cursor_event_reconciled"):
            lines.append("来源: <code>Cursor 官方 usage event</code>")
        lines.extend(["", "<b>计费</b>"])
        lines.extend(_billing_detail_lines(cost_metrics, billing_attempts))
    elif status in ("error", "cancelled") and (
        int(cost_metrics.get("costed_success") or 0) > 0
        or int(cost_metrics.get("unpriced_success") or 0) > 0
    ):
        lines.extend(["", "<b>计费</b>"])
        lines.extend(_billing_detail_lines(cost_metrics, billing_attempts))

    lines.extend(["", "<b>请求总览</b>"])
    if log.get("final_round_id"):
        lines.append(f"最终轮次: <code>{ui.escape_html(_detail_inline(log['final_round_id']))}</code>")
    request_first_applicable = bool(log.get("is_stream") or log.get("upstream_transport") == "ws")
    request_metrics = _detail_metric_line(log, request=True, first_applicable=request_first_applicable)
    lines.append(f"业务计时: {request_metrics or '无可靠样本'}")
    if log.get("request_elapsed_ms") is not None:
        lines.append(f"请求全程（外层）: {ui.fmt_ms(log.get('request_elapsed_ms'))}")
    request_stages = _detail_stage_line(log)
    if request_stages:
        lines.append(f"可靠阶段（可能重叠，不相加）: {request_stages}")
    tps_v = ui.calc_row_tps(log)
    if tps_v is not None:
        lines.append(f"⚡ 生成速度: {ui.fmt_tps(tps_v)}")

    rounds_by_attempt: dict[str, list[dict]] = {}
    unassigned_rounds: list[dict] = []
    for item in proxy_chain:
        retry_id = item.get("retry_attempt_id")
        if retry_id is None:
            unassigned_rounds.append(item)
        else:
            rounds_by_attempt.setdefault(str(retry_id), []).append(item)

    lines.extend(["", f"<b>执行链 ({len(chain)} 次渠道尝试 / {len(proxy_chain)} 个上游轮次)</b>"])
    if not chain:
        lines.append("  (无渠道尝试记录)")
    for c in chain:
        order = c.get("attempt_order") or "?"
        channel_key = c.get("channel_key") or "?"
        ch = ui.escape_html(_detail_inline(
            ui.channel_display_name(channel_key, with_family=False)
        ))
        provider_icon = ui.channel_provider_custom_emoji_html(channel_key)
        provider_prefix = f"{provider_icon} " if provider_icon else ""
        model_raw = _detail_inline(c.get("model"))
        outbound_model = _attempt_outbound_model(c)
        model = ui.escape_html(
            f"{model_raw} → {outbound_model}"
            if outbound_model and outbound_model != model_raw else model_raw
        )
        outcome = _detail_inline(c.get("outcome"))
        mark = _retry_chain_mark(outcome)
        label = ui.escape_html(_retry_chain_label(outcome))
        proxy_tag = f" 🔀 {ui.escape_html(_detail_inline(c['proxy_name']))}" if c.get("proxy_name") else ""
        lines.append(
            f"  {mark} <b>尝试 {order}.</b> {provider_prefix}<code>{ch}</code>"
            f" / <code>{model}</code>{proxy_tag} — {label}"
        )
        if c.get("final_round_id"):
            lines.append(f"     · 终止轮次 <code>{ui.escape_html(_detail_inline(c['final_round_id']))}</code>")
        child_rounds = rounds_by_attempt.pop(str(c.get("id")), [])
        final_mode = child_rounds[-1].get("request_mode") if child_rounds else None
        attempt_metrics = _detail_metric_line(c, first_applicable=final_mode != "http_non_stream")
        if attempt_metrics:
            lines.append(f"     · 终止轮摘要: {attempt_metrics}")
        if c.get("attempt_elapsed_ms") is not None:
            lines.append(f"     · 渠道尝试全程（外层）: {ui.fmt_ms(c.get('attempt_elapsed_ms'))}")
        attempt_stages = _detail_stage_line(c)
        if attempt_stages:
            lines.append(f"     · 可靠阶段（可能重叠，不相加）: {attempt_stages}")
        if c.get("error_detail"):
            _append_detail_text(lines, _extract_error_summary(str(c["error_detail"])), indent="     ⚠ ", italic=True)
        for p in child_rounds:
            _append_round_detail(lines, p)

    remaining_rounds = unassigned_rounds + [p for values in rounds_by_attempt.values() for p in values]
    if remaining_rounds:
        lines.extend(["", f"<b>未关联渠道尝试的上游轮次 ({len(remaining_rounds)} 个)</b>"])
        for p in remaining_rounds:
            _append_round_detail(lines, p, nested=False)

    if local_web_log:
        lines.extend(["", f"<b>搜索日志 ({len(local_web_log)} 次)</b>"])
        total_bytes = 0
        total_results = 0
        for idx, item in enumerate(local_web_log, 1):
            local_status = _detail_inline(item.get("status"))
            mark = "✅" if local_status == "success" else ("⏳" if local_status == "running" else "❌")
            count = int(item.get("result_count") or 0)
            byte_count = int(item.get("content_bytes") or 0)
            total_results += count
            total_bytes += byte_count
            head = f"  {mark} <b>{idx}.</b> <code>{ui.escape_html(_detail_inline(item.get('tool_name')))}</code> · round {item.get('round_no') or '?'}"
            if count:
                head += f" · 返回 {count} 条"
            if byte_count:
                head += f" · {_fmt_bytes(byte_count)}"
            lines.append(head)
            if item.get("query"):
                _append_detail_text(lines, item["query"], indent="     查: ", italic=True)
            if item.get("url"):
                _append_detail_text(lines, item["url"], indent="     URL: ")
            local_timing = []
            if item.get("started_at") and item.get("ended_at"):
                local_timing.append(f"耗时 {ui.fmt_ms((item['ended_at'] - item['started_at']) * 1000)}")
            if item.get("content_chars"):
                local_timing.append(f"字符 {ui.fmt_tokens(item.get('content_chars'))}")
            if local_timing:
                lines.append(f"     · {' · '.join(local_timing)}")
            if item.get("error_message"):
                _append_detail_text(lines, item["error_message"], indent="     ⚠ ", italic=True)
        summary = []
        if total_results:
            summary.append(f"共 {total_results} 条")
        if total_bytes:
            summary.append(f"共 {_fmt_bytes(total_bytes)}")
        if summary:
            lines.append("  合计: " + " · ".join(summary))

    if status in ("error", "cancelled") and log.get("error_message"):
        title = "客户端取消" if status == "cancelled" else "最终错误"
        lines.extend(["", f"<b>{title}</b>"])
        _append_detail_text(lines, _extract_error_summary(str(log["error_message"])), italic=True)

    return "\n".join(lines)


def _render_detail_pages(detail: dict, limit: int = 3600) -> list[str]:
    """Paginate by complete escaped lines; no content is discarded or HTML tag cut."""
    lines = _render_detail(detail).splitlines()
    pages: list[list[str]] = [[]]
    page_len = 0
    for line in lines:
        added = len(line) + (1 if pages[-1] else 0)
        if pages[-1] and page_len + added > limit:
            pages.append([])
            page_len = 0
            added = len(line)
        pages[-1].append(line)
        page_len += added
    return ["\n".join(page) for page in pages if page] or [""]


def show_detail(chat_id: int, message_id: int, cb_id: str, short: str,
                page: int = 1, list_state: dict | None = None,
                detail_page: int = 1) -> None:
    ui.answer_cb(cb_id)
    list_state = _normalize_list_state(list_state, page=page if list_state is None else None)
    list_code = _list_state_code(list_state)
    rid = ui.resolve_code(short)
    if not rid:
        ui.edit(chat_id, message_id, "⚠ 日志已过期或未找到",
                reply_markup=ui.inline_kb([[ui.btn("◀ 返回列表", _list_cb(list_state))]]))
        return
    try:
        detail = log_db.log_detail(rid)
    except Exception as exc:
        ui.edit(chat_id, message_id, f"❌ 查询失败: <code>{ui.escape_html(str(exc))}</code>",
                reply_markup=ui.inline_kb([[ui.btn("◀ 返回列表", _list_cb(list_state))]]))
        return
    if not detail or not detail.get("log"):
        ui.edit(chat_id, message_id, f"⚠ 未找到 <code>{ui.escape_html(rid)}</code>",
                reply_markup=ui.inline_kb([[ui.btn("◀ 返回列表", _list_cb(list_state))]]))
        return
    detail_pages = _render_detail_pages(detail)
    detail_page = max(1, min(int(detail_page or 1), len(detail_pages)))
    text = detail_pages[detail_page - 1]
    if len(detail_pages) > 1:
        text += f"\n\n<i>详情第 {detail_page}/{len(detail_pages)} 页</i>"
    rows: list[list[dict]] = []
    if config.get().get("logStoreBodies", True) is not False:
        stored_detail = detail.get("detail") or {}
        body_row: list[dict] = []
        if isinstance(stored_detail, dict) and stored_detail.get("request_body") is not None:
            body_short = ui.register_code("logbody:" + rid)
            body_row.append(ui.btn(
                "📨 请求 Body", f"logs:body:{body_short}:{list_code}",
            ))
        if isinstance(stored_detail, dict) and stored_detail.get("response_body") is not None:
            resp_short = ui.register_code("logresp:" + rid)
            body_row.append(ui.btn(
                "📬 响应", f"logs:response:{resp_short}:{list_code}",
            ))
        if body_row:
            rows.append(body_row)
    if len(detail_pages) > 1:
        nav = []
        if detail_page > 1:
            nav.append(ui.btn("◀ 上一页", f"logs:dpage:{short}:{detail_page - 1}:{list_code}"))
        if detail_page < len(detail_pages):
            nav.append(ui.btn("下一页 ▶", f"logs:dpage:{short}:{detail_page + 1}:{list_code}"))
        rows.append(nav)
    rows.append([ui.btn(f"◀ 返回第 {list_state['p']} 页", _list_cb(list_state))])
    ui.edit(chat_id, message_id, text, reply_markup=ui.inline_kb(rows))



def _chunk_for_tg(text: str, chunk_size: int = 3200) -> list[str]:
    """把长文本切成多条。调用方仍需自行 escape。"""
    if not text:
        return [""]
    return [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)]


def _state_code(state: dict) -> str:
    payload = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
    return ui.register_code("loginspect:" + payload)


def _resolve_state(short: str) -> dict | None:
    full = ui.resolve_code(short)
    if not full or not full.startswith("loginspect:"):
        return None
    try:
        obj = json.loads(full[len("loginspect:"):])
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _detail_back_cb(rid: str, list_state: dict | None) -> str:
    st = _normalize_list_state(list_state or {})
    return f"logs:detail:{ui.register_code(rid)}:{_list_state_code(st)}"


def _kind_label(kind: str) -> str:
    return "请求 Body" if kind == "request" else "响应"


def _kind_icon(kind: str) -> str:
    return "📨" if kind == "request" else "📬"


def _raw_for_kind(detail: dict, kind: str):
    data = (detail or {}).get("detail") or {}
    if kind == "request":
        return data.get("request_body")
    return data.get("response_body")


def _items_for_state(state: dict) -> tuple[str, list[dict], str]:
    rid = str(state.get("r") or "")
    kind = "request" if state.get("k") == "request" else "response"
    detail = log_db.log_detail(rid)
    raw = _raw_for_kind(detail, kind)
    if not raw:
        return rid, [], ""
    if kind == "request":
        items = log_inspector.parse_request_body(raw)
    else:
        items = log_inspector.parse_response_body(raw)
    return rid, items, str(raw)


def _query_filtered_items(items: list[dict], state: dict) -> list[dict]:
    return log_inspector.filter_items(items, str(state.get("q") or ""))


def _kind_filter(state: dict) -> str:
    return str(state.get("f") or "")


def _apply_kind_filter(items: list[dict], state: dict) -> list[dict]:
    f = _kind_filter(state)
    if not f:
        return list(items)
    return [it for it in items if str(it.get("kind") or "") == f]


def _filtered_sorted_items(items: list[dict], state: dict) -> list[dict]:
    filtered = _apply_kind_filter(_query_filtered_items(items, state), state)
    return log_inspector.sort_items(filtered, str(state.get("o") or "original"))


def _kind_counts(items: list[dict]) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for it in items:
        k = str(it.get("kind") or "message")
        counts[k] = counts.get(k, 0) + 1
    return list(counts.items())


def _page_count(total: int) -> int:
    return max(1, (max(0, int(total or 0)) + _INSPECT_PAGE_SIZE - 1) // _INSPECT_PAGE_SIZE)


def _page_for_index(index0: int) -> int:
    return max(1, (max(0, index0) // _INSPECT_PAGE_SIZE) + 1)


def _page_slice(items: list[dict], page: int) -> list[dict]:
    start = (max(1, int(page or 1)) - 1) * _INSPECT_PAGE_SIZE
    return items[start:start + _INSPECT_PAGE_SIZE]


def _append_button_grid(rows: list[list[dict]], buttons: list[dict], *, cols: int) -> None:
    for i in range(0, len(buttons), max(1, int(cols or 1))):
        rows.append(buttons[i:i + max(1, int(cols or 1))])


def _seq_for_page(items: list[dict], page: int) -> int | None:
    rows = _page_slice(items, page)
    if not rows:
        return None
    # 翻页时默认选中该页最后一条“真实消息”；若该页只有 usage/params 等元信息才退回最后一条。
    selected = log_inspector.selected_item(rows, None) or rows[-1]
    try:
        return int(selected.get("seq") or 0)
    except Exception:
        return None


def _render_item_text(*, rid: str, kind: str, all_count: int, shown_count: int,
                      selected: dict, page: int, total_pages: int, sort_key: str,
                      query: str, type_filter: str = "") -> str:
    seq = int(selected.get("seq") or 0)
    size = log_inspector.fmt_size(int(selected.get("size") or 0))
    kind_name = str(selected.get("kind") or "message")
    kind_name_display = log_inspector.kind_label(kind_name)
    summary = log_inspector.summary_label(str(selected.get("summary") or ""))
    text = str(selected.get("text") or selected.get("raw") or "")
    is_long = len(text) > _ITEM_PREVIEW_CHARS
    preview = text[:_ITEM_PREVIEW_CHARS]

    lines = [
        f"{_kind_icon(kind)} <b>{_kind_label(kind)}</b> · <code>{ui.escape_html(rid[:8])}</code>",
        f"消息: <b>#{seq}</b> / <code>{shown_count}</code>" + (f"（原始 {all_count}）" if shown_count != all_count else ""),
        f"列表: 第 <code>{page}/{total_pages}</code> 页 · 排序 <code>{ui.escape_html(log_inspector.SORT_LABELS.get(sort_key, sort_key))}</code>",
    ]
    if query:
        lines.append(f"搜索: <code>{ui.escape_html(query)}</code>")
    if type_filter:
        lines.append(f"类型: <code>{ui.escape_html(log_inspector.kind_label(type_filter))}</code>")
    lines.extend([
        "",
        f"<b>{ui.escape_html(kind_name_display)}</b> · <code>{ui.escape_html(size)}</code>",
    ])
    if summary:
        lines.append(f"<i>{ui.escape_html(summary)}</i>")
    lines.append("")
    lines.append(f"<pre>{ui.escape_html(preview)}</pre>")
    if is_long:
        lines.append(f"\n<i>已截断：优先显示前 {_ITEM_PREVIEW_CHARS} 字符，完整内容请点下方按钮。</i>")
    return ui.truncate("\n".join(lines), 3900)


def _empty_inspector_text(rid: str, kind: str, query: str = "") -> str:
    if query:
        return (
            f"{_kind_icon(kind)} <b>{_kind_label(kind)}</b> · <code>{ui.escape_html(rid[:8])}</code>\n\n"
            f"🔎 搜索 <code>{ui.escape_html(query)}</code> 没有命中。"
        )
    return f"{_kind_icon(kind)} <b>{_kind_label(kind)}</b> · <code>{ui.escape_html(rid[:8])}</code>\n\n暂无可显示内容。"


def _inspector_kb(*, state: dict, rid: str, items: list[dict], selected_seq: int | None,
                  page: int, total_pages: int, all_count: int,
                  type_counts: list[tuple[str, int]] | None = None,
                  base_count: int | None = None) -> dict:
    rows: list[list[dict]] = []
    kind = "request" if state.get("k") == "request" else "response"
    sort_key = str(state.get("o") or "original")
    query = str(state.get("q") or "")
    current_filter = _kind_filter(state)
    list_state = _normalize_list_state(state.get("ls") or {"p": int(state.get("lp") or 1)})

    item_buttons: list[dict] = []
    for item in _page_slice(items, page):
        seq = int(item.get("seq") or 0)
        next_state = dict(state)
        next_state.update({"s": seq, "p": page})
        item_buttons.append(ui.btn(log_inspector.button_label(item, selected=(seq == selected_seq), compact=True), f"logs:ins:{_state_code(next_state)}"))
    _append_button_grid(rows, item_buttons, cols=2)

    def _page_state(target_page: int) -> dict:
        target_page = max(1, min(int(target_page or 1), total_pages))
        st = dict(state)
        st.update({"p": target_page, "s": _seq_for_page(items, target_page)})
        return st

    # 固定紧凑分页：始终给首页 / 上一页 / 当前页 / 下一页 / 尾页，方便长日志快速跳转。
    first_page = 1
    prev_page = max(1, page - 1)
    next_page = min(total_pages, page + 1)
    last_page = total_pages
    rows.append([
        ui.btn("⏮ 首页", f"logs:ins:{_state_code(_page_state(first_page))}"),
        ui.btn("◀", f"logs:ins:{_state_code(_page_state(prev_page))}"),
        ui.btn(f"{page}/{total_pages}", f"logs:ins:{_state_code(state)}"),
        ui.btn("▶", f"logs:ins:{_state_code(_page_state(next_page))}"),
        ui.btn("尾页 ⏭", f"logs:ins:{_state_code(_page_state(last_page))}"),
    ])

    sort_state = dict(state)
    sort_state["o"] = log_inspector.next_sort(sort_key)
    sort_state["p"] = None
    rows.append([
        ui.btn(f"排序:{log_inspector.SORT_LABELS.get(sort_key, sort_key)}", f"logs:ins:{_state_code(sort_state)}"),
        ui.btn("🔎 搜索", f"logs:search:{_state_code(state)}"),
    ])

    # 类型统计过滤按钮：基于“搜索后、类型过滤前”的集合统计。
    counts = list(type_counts or [])
    if counts:
        total_base = int(base_count if base_count is not None else sum(n for _, n in counts))
        filter_buttons: list[dict] = []
        all_state = dict(state)
        all_state.update({"f": "", "s": None, "p": None})
        filter_buttons.append(ui.btn(("✅" if not current_filter else "") + f"全部 {total_base}", f"logs:ins:{_state_code(all_state)}"))
        for k, n in counts:
            st = dict(state)
            st.update({"f": k, "s": None, "p": None})
            label = ("✅" if current_filter == k else "") + f"{log_inspector.kind_short_label(k)} {n}"
            filter_buttons.append(ui.btn(label, f"logs:ins:{_state_code(st)}"))
        _append_button_grid(rows, filter_buttons, cols=3)

    extra: list[dict] = []
    if query:
        clear_state = dict(state)
        clear_state.update({"q": "", "p": None, "s": None})
        extra.append(ui.btn("清除搜索", f"logs:ins:{_state_code(clear_state)}"))
    selected = log_inspector.selected_item(items, selected_seq)
    if selected and len(str(selected.get("text") or selected.get("raw") or "")) > _ITEM_PREVIEW_CHARS:
        full_state = dict(state)
        full_state.update({"s": int(selected.get("seq") or 0), "p": page})
        extra.append(ui.btn("📜 查看完整内容", f"logs:full:{_state_code(full_state)}"))
    if extra:
        rows.append(extra)

    rows.append([ui.btn("◀ 返回详情", _detail_back_cb(rid, list_state))])
    return ui.inline_kb(rows)


def _show_inspector(chat_id: int, message_id: int, cb_id: str | None, state: dict) -> None:
    if cb_id is not None:
        ui.answer_cb(cb_id)
    kind = "request" if state.get("k") == "request" else "response"
    try:
        rid, all_items, _raw = _items_for_state(state)
    except Exception as exc:
        ui.edit(chat_id, message_id, f"❌ 解析失败: <code>{ui.escape_html(str(exc))}</code>")
        return

    sort_key = str(state.get("o") or "original")
    query = str(state.get("q") or "")
    type_filter = _kind_filter(state)
    query_items = _query_filtered_items(all_items, state)
    type_counts = _kind_counts(query_items)
    items = log_inspector.sort_items(_apply_kind_filter(query_items, state), sort_key)
    if not items:
        ui.edit(
            chat_id, message_id, _empty_inspector_text(rid, kind, query),
            reply_markup=_inspector_kb(state=state, rid=rid, items=[], selected_seq=None, page=1,
                                       total_pages=1, all_count=len(all_items),
                                       type_counts=type_counts, base_count=len(query_items)),
        )
        return

    selected = log_inspector.selected_item(items, state.get("s")) or items[-1]
    selected_seq = int(selected.get("seq") or 0)
    idx = next((i for i, it in enumerate(items) if int(it.get("seq") or 0) == selected_seq), len(items) - 1)
    if state.get("p") is None:
        page = _page_for_index(idx)
    else:
        try:
            page = int(state.get("p") or 1)
        except Exception:
            page = _page_for_index(idx)
    total_pages = _page_count(len(items))
    page = max(1, min(page, total_pages))
    state = dict(state)
    state.update({"s": selected_seq, "p": page, "o": sort_key, "q": query, "f": type_filter})
    text = _render_item_text(
        rid=rid, kind=kind, all_count=len(all_items), shown_count=len(items), selected=selected,
        page=page, total_pages=total_pages, sort_key=sort_key, query=query, type_filter=type_filter,
    )
    ui.edit(
        chat_id, message_id, text,
        reply_markup=_inspector_kb(state=state, rid=rid, items=items, selected_seq=selected_seq,
                                   page=page, total_pages=total_pages, all_count=len(all_items),
                                   type_counts=type_counts, base_count=len(query_items)),
    )


def _initial_state(rid: str, kind: str, *, list_state: dict | None = None, list_page: int = 1) -> dict:
    st = _normalize_list_state(list_state or {"p": int(list_page or 1)})
    return {"r": rid, "k": kind, "s": None, "p": None, "o": "original", "q": "", "f": "", "ls": st, "lp": st["p"]}


def _show_body_inspector(chat_id: int, message_id: int, cb_id: str, payload: str, kind: str) -> None:
    short, _, state_s = (payload or "").partition(":")
    list_state = _resolve_list_state(state_s) or _list_state(1)
    full = ui.resolve_code(short)
    prefix = "logbody:" if kind == "request" else "logresp:"
    if not full or not full.startswith(prefix):
        ui.answer_cb(cb_id, "短码已失效")
        return
    rid = full[len(prefix):]
    ui.answer_cb(cb_id, "加载中...")
    _show_inspector(chat_id, message_id, None, _initial_state(rid, kind, list_state=list_state))


def show_request_body(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    _show_body_inspector(chat_id, message_id, cb_id, short, "request")


def show_response_body(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    _show_body_inspector(chat_id, message_id, cb_id, short, "response")


def _send_full_item(chat_id: int, cb_id: str, state_short: str) -> None:
    state = _resolve_state(state_short)
    if not state:
        ui.answer_cb(cb_id, "短码已失效")
        return
    try:
        rid, all_items, _raw = _items_for_state(state)
        items = _filtered_sorted_items(all_items, state)
        selected = log_inspector.selected_item(items, state.get("s"))
    except Exception as exc:
        ui.answer_cb(cb_id, "读取失败")
        ui.send(chat_id, f"❌ 读取失败: <code>{ui.escape_html(str(exc))}</code>")
        return
    if not selected:
        ui.answer_cb(cb_id, "没有内容")
        return
    text = str(selected.get("text") or selected.get("raw") or "")
    seq = int(selected.get("seq") or 0)
    kind = str(selected.get("kind") or "message")
    label = _kind_label("request" if state.get("k") == "request" else "response")
    title = f"{label} #{seq} {log_inspector.kind_label(kind)} ({rid[:8]})"
    ui.answer_cb(cb_id, "输出完整内容...")
    if len(text) <= 12_000:
        chunks = _chunk_for_tg(text, 3200)
        ui.send(chat_id, f"📜 <b>{ui.escape_html(title)}</b> — {len(chunks)} 段")
        for i, c in enumerate(chunks, 1):
            suffix = f"\n\n<i>[{i}/{len(chunks)}]</i>" if len(chunks) > 1 else ""
            ui.send(chat_id, f"<pre>{ui.escape_html(c)}</pre>{suffix}")
        return
    filename = f"parrot-log-{rid[:8]}-{state.get('k')}-item-{seq}.txt"
    caption = f"📜 <b>{ui.escape_html(title)}</b> · {log_inspector.fmt_size(len(text.encode('utf-8', errors='replace')))}"
    if hasattr(ui, "send_document_text"):
        ui.send_document_text(chat_id, text, filename=filename, caption=caption)
    else:
        chunks = _chunk_for_tg(text[:12_000], 3200)
        ui.send(chat_id, f"📜 <b>{ui.escape_html(title)}</b> — 内容过长，先输出前 {len(chunks)} 段")
        for i, c in enumerate(chunks, 1):
            ui.send(chat_id, f"<pre>{ui.escape_html(c)}</pre>\n\n<i>[{i}/{len(chunks)}]</i>")


def _begin_search(chat_id: int, message_id: int, cb_id: str, state_short: str) -> None:
    state = _resolve_state(state_short)
    if not state:
        ui.answer_cb(cb_id, "短码已失效")
        return
    states.set_state(chat_id, "logs_search", {"state": state, "message_id": message_id})
    ui.answer_cb(cb_id, "请输入搜索关键词")
    ui.send(chat_id, "🔎 请输入搜索关键词。\n发送 <code>/cancel</code> 取消；发送 <code>/clear</code> 清除搜索。")


def handle_text_state(chat_id: int, action: str, text: str) -> bool:
    if action != "logs_search":
        return False
    st = states.pop_state(chat_id) or {}
    data = st.get("data") or {}
    state = dict(data.get("state") or {})
    message_id = data.get("message_id")
    if not state or not message_id:
        ui.send(chat_id, "⚠ 搜索状态已失效，请重新打开日志详情。")
        return True
    q = (text or "").strip()
    if q == "/cancel":
        ui.send(chat_id, "已取消搜索。")
        return True
    if q == "/clear":
        q = ""
    state.update({"q": q, "s": None, "p": None})
    _show_inspector(chat_id, int(message_id), None, state)
    return True


# ─── 路由 ─────────────────────────────────────────────────────────

def handle_callback(chat_id: int, message_id: int, cb_id: str, data: str) -> bool:
    if data == "menu:logs":
        show(chat_id, message_id, cb_id, page=1); return True
    if data.startswith("logs:list:"):
        state = _resolve_list_state(data.split(":", 2)[2]) or _list_state(1)
        show(chat_id, message_id, cb_id, state=state); return True
    if data.startswith("logs:page:"):
        try:
            page = int(data.split(":", 2)[2])
        except Exception:
            page = 1
        show(chat_id, message_id, cb_id, page=page); return True
    if data.startswith("logs:refresh"):
        parts = data.split(":", 2)
        try:
            page = int(parts[2]) if len(parts) > 2 else 1
        except Exception:
            page = 1
        refresh(chat_id, message_id, cb_id, page=page); return True
    if data.startswith("logs:queryclear:"):
        _clear_query_filters(chat_id, message_id, cb_id, data.split(":", 2)[2]); return True
    if data.startswith("logs:query:"):
        state = _resolve_list_state(data.split(":", 2)[2])
        if not state:
            ui.answer_cb(cb_id, "查询状态已失效")
            return True
        _show_query_menu(chat_id, message_id, cb_id, state); return True
    if data.startswith("logs:filter:"):
        payload = data.split(":", 2)[2]
        kind, _, short = payload.partition(":")
        state = _resolve_list_state(short)
        if not state or kind not in {"apikey", "model", "channel"}:
            ui.answer_cb(cb_id, "筛选状态已失效")
            return True
        _show_filter_menu(chat_id, message_id, cb_id, kind, state); return True
    if data.startswith("logs:ftoggle:"):
        payload = data.split(":", 2)[2]
        parts = payload.split(":", 2)
        if len(parts) != 3:
            ui.answer_cb(cb_id, "筛选状态已失效")
            return True
        kind, value_short, state_short = parts
        _toggle_filter_value(chat_id, message_id, cb_id, kind, value_short, state_short); return True
    if data.startswith("logs:faction:"):
        payload = data.split(":", 2)[2]
        parts = payload.split(":", 2)
        if len(parts) != 3:
            ui.answer_cb(cb_id, "筛选状态已失效")
            return True
        kind, action, state_short = parts
        _filter_action(chat_id, message_id, cb_id, kind, action, state_short); return True
    if data.startswith("logs:dpage:"):
        payload = data.split(":", 2)[2]
        parts = payload.split(":", 2)
        if len(parts) != 3:
            ui.answer_cb(cb_id, "详情页状态已失效")
            return True
        short, detail_page_s, state_s = parts
        try:
            detail_page = int(detail_page_s)
        except Exception:
            detail_page = 1
        list_state = _resolve_list_state(state_s) or _list_state(1)
        show_detail(
            chat_id, message_id, cb_id, short,
            list_state=list_state, detail_page=detail_page,
        )
        return True
    if data.startswith("logs:detail:"):
        payload = data.split(":", 2)[2]
        short, _, state_s = payload.partition(":")
        list_state = _resolve_list_state(state_s)
        page = 1
        if list_state is None:
            try:
                page = int(state_s or 1)
            except Exception:
                page = 1
        show_detail(chat_id, message_id, cb_id, short, page=page, list_state=list_state); return True
    if data.startswith("logs:body:"):
        show_request_body(chat_id, message_id, cb_id, data.split(":", 2)[2]); return True
    if data.startswith("logs:response:"):
        show_response_body(chat_id, message_id, cb_id, data.split(":", 2)[2]); return True
    if data.startswith("logs:ins:"):
        state = _resolve_state(data.split(":", 2)[2])
        if not state:
            ui.answer_cb(cb_id, "短码已失效")
            return True
        _show_inspector(chat_id, message_id, cb_id, state); return True
    if data.startswith("logs:full:"):
        _send_full_item(chat_id, cb_id, data.split(":", 2)[2]); return True
    if data.startswith("logs:search:"):
        _begin_search(chat_id, message_id, cb_id, data.split(":", 2)[2]); return True
    return False
