"""最近日志菜单 + 单条详情（含重试链）。

callback_data：
  `menu:logs`                  — 显示最近日志第 1 页
  `logs:page:<page>`           — 显示指定页
  `logs:refresh:<page>`        — 刷新指定页
  `logs:detail:<short>:<page>` — 查看详情（short 是 request_id 短码，page 用于返回）
"""

from __future__ import annotations

import json
from typing import Optional

from ... import log_db
from .. import log_inspector, states, ui


_LIST_PAGE_SIZE = 6
_INSPECT_PAGE_SIZE = 6
_ITEM_PREVIEW_CHARS = 1500


_STATUS_ICON = {"success": "✅", "error": "❌", "pending": "⏳"}

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


def _page_rows(page: int) -> tuple[list[dict], int, int, int]:
    total = log_db.recent_logs_count()
    page, total_pages = _clamp_page(page, total)
    rows = log_db.recent_logs(_LIST_PAGE_SIZE, offset=(page - 1) * _LIST_PAGE_SIZE)
    return rows, total, page, total_pages


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


def _render_list(rows: list[dict], *, page: int = 1, total: int | None = None, total_pages: int | None = None) -> str:
    total = len(rows) if total is None else int(total or 0)
    total_pages = max(1, int(total_pages or 1))
    if not rows:
        return "📋 <b>最近日志</b>\n\n暂无记录。"
    lines = [
        f"📋 <b>最近日志 · 第 {page}/{total_pages} 页 · 共 {total} 条</b>",
        "<i>对照下方按钮的 #编号 点击查看详情</i>",
    ]
    for idx, r in enumerate(rows, 1):
        display_idx = _display_index(page, idx)
        key = ui.escape_html(r.get("api_key_name") or "?")
        prefix = f"\n<b>#{display_idx}</b> "
        # 列表首行比最近调用多一个 key →
        headline = ui.fmt_log_entry_headline(r, prefix=f"{prefix}<b>{key}</b> → ")
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
            return raw[:200]
    try:
        obj = json.loads(json_part)
    except Exception:
        return (raw[:200])
    err = obj.get("error") if isinstance(obj, dict) else None
    if isinstance(err, dict):
        msg = err.get("message", "")
        if msg:
            t = err.get("type") or ""
            summary = f"{t}: {msg}" if t else msg
            return (f"{prefix} — {summary}" if prefix else summary)[:200]
    return (f"{prefix} — {json_part[:150]}" if prefix else json_part[:200])


def _list_kb(rows: list[dict], *, page: int, total_pages: int) -> dict:
    """详情按钮 3 列紧凑排列；分页行参考 OAuth 菜单。"""
    rows_kb: list[list[dict]] = []
    cur: list[dict] = []
    for idx, r in enumerate(rows, 1):
        display_idx = _display_index(page, idx)
        rid = r.get("request_id") or ""
        if not rid:
            continue
        short = ui.register_code(rid)
        cur.append(ui.btn(f"📄 #{display_idx}", f"logs:detail:{short}:{page}"))
        if len(cur) >= 3:
            rows_kb.append(cur)
            cur = []
    if cur:
        rows_kb.append(cur)

    if total_pages > 1:
        nav: list[dict] = []
        if page > 1:
            nav.append(ui.btn("◀ 上一页", f"logs:page:{page - 1}"))
        nav.append(ui.btn(f"{page}/{total_pages}", f"logs:page:{page}"))
        if page < total_pages:
            nav.append(ui.btn("下一页 ▶", f"logs:page:{page + 1}"))
        rows_kb.append(nav)
    rows_kb.append([ui.btn("🔄 刷新", f"logs:refresh:{page}"),
                    ui.btn("◀ 返回主菜单", "menu:main")])
    return ui.inline_kb(rows_kb)


# ─── 列表入口 ─────────────────────────────────────────────────────

def show(chat_id: int, message_id: int, cb_id: Optional[str] = None, page: int = 1) -> None:
    if cb_id is not None:
        ui.answer_cb(cb_id)
    rows, total, page, total_pages = _page_rows(page)
    ui.edit(chat_id, message_id, ui.truncate(_maybe_suffix_status_banner(_render_list(rows, page=page, total=total, total_pages=total_pages))),
            reply_markup=_list_kb(rows, page=page, total_pages=total_pages))


def send_new(chat_id: int) -> None:
    rows, total, page, total_pages = _page_rows(1)
    ui.send(chat_id, ui.truncate(_maybe_suffix_status_banner(_render_list(rows, page=page, total=total, total_pages=total_pages))),
            reply_markup=_list_kb(rows, page=page, total_pages=total_pages))


def refresh(chat_id: int, message_id: int, cb_id: str, page: int = 1) -> None:
    show(chat_id, message_id, cb_id, page=page)


# ─── 详情 ─────────────────────────────────────────────────────────

def _render_detail(detail: dict) -> str:
    log = detail.get("log") or {}
    chain = detail.get("retry_chain") or []
    proxy_chain = detail.get("proxy_chain") or []

    rid = log.get("request_id") or "?"
    created = ui.fmt_bjt_ts(log.get("created_at"), "%Y-%m-%d %H:%M:%S")
    icon = _status_icon(log)
    status = log.get("status") or "?"

    lines = [
        f"📋 <b>日志详情</b>",
        f"ID: <code>{ui.escape_html(rid)}</code>",
        f"时间: <code>{created}</code>",
        f"状态: <code>{ui.escape_html(status)}</code>"
        + (f" ({log.get('http_status')})" if log.get("http_status") else "")
        + f" {icon}",
        f"客户端: <code>{ui.escape_html(log.get('client_ip') or '?')}</code>"
        f" / Key <code>{ui.escape_html(log.get('api_key_name') or '?')}</code>",
        f"请求模型: <code>{ui.escape_html(log.get('requested_model') or '?')}</code>",
    ]
    if log.get("final_channel_key"):
        final_ch = ui.channel_display_name(log["final_channel_key"], with_family=True)
        lines.append(
            f"最终渠道: <code>{ui.escape_html(final_ch)}</code>"
            f" / <code>{ui.escape_html(log.get('final_model') or '?')}</code>"
        )
    if log.get("proxy_name"):
        lines.append(f"出站代理: 🔀 <code>{ui.escape_html(log['proxy_name'])}</code>")
    # 协议（入口 + 上游）：老日志可能为空，非空才显示避免噪音
    ingress = log.get("ingress_protocol")
    upstream_proto = log.get("upstream_protocol")
    if ingress or upstream_proto:
        lines.append(
            f"协议: 入口 <code>{ui.escape_html(ingress or '?')}</code>"
            f" → 上游 <code>{ui.escape_html(upstream_proto or '?')}</code>"
            + (f" / <code>{ui.escape_html(log.get('upstream_transport'))}</code>" if log.get("upstream_transport") else "")
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
    effort = log.get("reasoning_effort")
    if effort:
        lines.append(f"思考强度：🧠 {ui.escape_html(effort)}")

    # Tokens
    if status == "success":
        inp = ui.prompt_total_from_row(log)
        lines.append("")
        lines.append("<b>Tokens</b>")
        token_line = f"↑ {ui.fmt_tokens(inp)} | ↓ {ui.fmt_tokens(log.get('output_tokens'))}"
        if (log.get("cache_read_tokens") or 0) > 0:
            token_line += f" | {ui.fmt_cache_phrase_from_row(log)}"
        lines.append(token_line)
    # 耗时
    lines.append("")
    lines.append("<b>耗时</b>")
    lines.append(
        f"连接 {ui.fmt_ms(log.get('connect_time_ms'))} · "
        f"首字 {ui.fmt_ms(log.get('first_token_time_ms'))} · "
        f"总 {ui.fmt_ms(log.get('total_time_ms'))}"
    )
    tps_v = ui.calc_row_tps(log)
    if tps_v is not None:
        lines.append(f"⚡ 生成速度: {ui.fmt_tps(tps_v)}")

    # 代理链（同一渠道尝试内的出站代理切换原因）
    if proxy_chain:
        lines.append("")
        lines.append(f"<b>代理链 ({len(proxy_chain)} 次尝试)</b>")
        for p in proxy_chain:
            order = p.get("attempt_order") or "?"
            pname = ui.escape_html(p.get("proxy_name") or "?")
            oc = p.get("outcome") or "?"
            mark = "✅" if oc in ("success", "connected") else "❌"
            lines.append(f"  {mark} <b>{order}.</b> 🔀 <code>{pname}</code> — {ui.escape_html(oc)}")
            timing = []
            if p.get("connect_ms") is not None:
                timing.append(f"连接 {ui.fmt_ms(p['connect_ms'])}")
            if p.get("started_at") and p.get("ended_at"):
                dur = (p["ended_at"] - p["started_at"]) * 1000
                timing.append(f"耗时 {ui.fmt_ms(dur)}")
            byte_sum = int(p.get("bytes_up") or 0) + int(p.get("bytes_down") or 0)
            if byte_sum:
                timing.append(f"流量 {_fmt_bytes(byte_sum)}")
            if timing:
                lines.append(f"     · {' · '.join(timing)}")
            if p.get("error_detail"):
                lines.append(f"     ⚠ <i>{ui.escape_html(_extract_error_summary(p['error_detail'])[:180])}</i>")

    # 重试链
    lines.append("")
    lines.append(f"<b>重试链 ({len(chain)} 次尝试)</b>")
    if not chain:
        lines.append("  (无记录)")
    for c in chain:
        order = c.get("attempt_order") or "?"
        ch = ui.escape_html(ui.channel_display_name(c.get("channel_key") or "?", with_family=True))
        model = ui.escape_html(c.get("model") or "?")
        oc = c.get("outcome") or "?"
        mark = "✅" if oc == "success" else "❌"
        proxy_tag = ""
        if c.get("proxy_name"):
            proxy_tag = f" 🔀 {ui.escape_html(c['proxy_name'])}"
        lines.append(f"  {mark} <b>{order}.</b> <code>{ch}</code> / <code>{model}</code>{proxy_tag} — {ui.escape_html(oc)}")
        timing = []
        if c.get("connect_ms") is not None:
            timing.append(f"连接 {ui.fmt_ms(c['connect_ms'])}")
        if c.get("first_byte_ms") is not None:
            timing.append(f"首字 {ui.fmt_ms(c['first_byte_ms'])}")
        if c.get("started_at") and c.get("ended_at"):
            dur = (c["ended_at"] - c["started_at"]) * 1000
            timing.append(f"耗时 {ui.fmt_ms(dur)}")
        if timing:
            lines.append(f"     · {' · '.join(timing)}")
        if c.get("error_detail"):
            lines.append(f"     ⚠ <i>{ui.escape_html(_extract_error_summary(c['error_detail'])[:180])}</i>")

    # 错误信息（整体）
    if status == "error" and log.get("error_message"):
        lines.append("")
        lines.append("<b>错误信息</b>")
        lines.append(f"<i>{ui.escape_html(_extract_error_summary(log['error_message'])[:300])}</i>")

    return "\n".join(lines)


def show_detail(chat_id: int, message_id: int, cb_id: str, short: str, page: int = 1) -> None:
    ui.answer_cb(cb_id)
    rid = ui.resolve_code(short)
    if not rid:
        ui.edit(chat_id, message_id, "⚠ 日志已过期或未找到",
                reply_markup=ui.inline_kb([[ui.btn("◀ 返回列表", f"logs:page:{page}")]]))
        return
    try:
        detail = log_db.log_detail(rid)
    except Exception as exc:
        ui.edit(chat_id, message_id, f"❌ 查询失败: <code>{ui.escape_html(str(exc))}</code>",
                reply_markup=ui.inline_kb([[ui.btn("◀ 返回列表", f"logs:page:{page}")]]))
        return
    if not detail or not detail.get("log"):
        ui.edit(chat_id, message_id, f"⚠ 未找到 <code>{ui.escape_html(rid)}</code>",
                reply_markup=ui.inline_kb([[ui.btn("◀ 返回列表", f"logs:page:{page}")]]))
        return
    body_short = ui.register_code("logbody:" + rid)
    resp_short = ui.register_code("logresp:" + rid)
    ui.edit(
        chat_id, message_id, ui.truncate(_render_detail(detail)),
        reply_markup=ui.inline_kb([
            [ui.btn("📨 请求 Body", f"logs:body:{body_short}:{page}"),
             ui.btn("📬 响应", f"logs:response:{resp_short}:{page}")],
            [ui.btn(f"◀ 返回第 {page} 页", f"logs:page:{page}")],
        ]),
    )



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


def _detail_back_cb(rid: str, page: int | None) -> str:
    return f"logs:detail:{ui.register_code(rid)}:{int(page or 1)}"


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
    lp = int(state.get("lp") or 1)

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

    rows.append([ui.btn("◀ 返回详情", _detail_back_cb(rid, lp))])
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


def _initial_state(rid: str, kind: str, *, list_page: int = 1) -> dict:
    return {"r": rid, "k": kind, "s": None, "p": None, "o": "original", "q": "", "f": "", "lp": int(list_page or 1)}


def _show_body_inspector(chat_id: int, message_id: int, cb_id: str, payload: str, kind: str) -> None:
    short, _, page_s = (payload or "").partition(":")
    try:
        list_page = int(page_s or 1)
    except Exception:
        list_page = 1
    full = ui.resolve_code(short)
    prefix = "logbody:" if kind == "request" else "logresp:"
    if not full or not full.startswith(prefix):
        ui.answer_cb(cb_id, "短码已失效")
        return
    rid = full[len(prefix):]
    ui.answer_cb(cb_id, "加载中...")
    _show_inspector(chat_id, message_id, None, _initial_state(rid, kind, list_page=list_page))


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
    if data.startswith("logs:detail:"):
        payload = data.split(":", 2)[2]
        short, _, page_s = payload.partition(":")
        try:
            page = int(page_s or 1)
        except Exception:
            page = 1
        show_detail(chat_id, message_id, cb_id, short, page=page); return True
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
