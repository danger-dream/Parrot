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
from .. import states, ui


_LIST_PAGE_SIZE = 6


_STATUS_ICON = {"success": "✅", "error": "❌", "pending": "⏳"}

# 入口协议 → 简短标签（anthropic 是默认，不加标签以避免每条日志都冗余显示）
_INGRESS_TAG = {"chat": "[chat]", "responses": "[rsp]"}


def _status_icon(row: dict) -> str:
    return _STATUS_ICON.get(row.get("status"), "?")


def _ingress_tag(row: dict) -> str:
    """若入口非 anthropic 则返回 `[chat]`/`[rsp]`，否则空串。"""
    return _INGRESS_TAG.get(row.get("ingress_protocol") or "", "")


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
        ts = ui.fmt_bjt_ts(r.get("created_at"), "%m-%d %H:%M:%S")
        icon = _status_icon(r)
        model = ui.escape_html(r.get("requested_model") or "?")
        key = ui.escape_html(r.get("api_key_name") or "?")
        ing_tag = _ingress_tag(r)
        line = f"\n<b>#{idx}</b> <code>{ts}</code> {icon} <b>{key}</b> → {model}"
        if ing_tag:
            line += f" <code>{ing_tag}</code>"

        # 通道 + 重试数
        if r.get("final_channel_key"):
            ch_short = ui.escape_html(r["final_channel_key"])
            line += f"\n  渠道: <code>{ch_short}</code>"
            if r.get("retry_count"):
                line += f"（重试 {r['retry_count']} 次）"
            if r.get("affinity_hit"):
                line += "  ★亲和"
        if r.get("status") == "success":
            inp = ui.prompt_total_from_row(r)
            cr = r.get("cache_read_tokens") or 0
            tok = f"↑ {ui.fmt_tokens(inp)} · ↓ {ui.fmt_tokens(r.get('output_tokens'))}"
            if cr > 0:
                tok += f" · {ui.fmt_cache_phrase_from_row(r)}"
            line += f"\n  Token: {tok}"

        timing_parts = []
        if r.get("connect_time_ms") is not None:
            timing_parts.append(f"连接 {ui.fmt_ms(r['connect_time_ms'])}")
        if r.get("is_stream") and r.get("first_token_time_ms") is not None:
            timing_parts.append(f"首字 {ui.fmt_ms(r['first_token_time_ms'])}")
        if r.get("total_time_ms") is not None:
            timing_parts.append(f"总 {ui.fmt_ms(r['total_time_ms'])}")
        tps_v = ui.calc_row_tps(r)
        if tps_v is not None:
            timing_parts.append(f"⚡ {ui.fmt_tps(tps_v)}")
        if timing_parts:
            line += "\n  耗时: " + " · ".join(timing_parts)

        if r.get("status") == "error" and r.get("error_message"):
            msg = r["error_message"]
            summary = _extract_error_summary(msg)[:120]
            # 用 ⚠ + 斜体行内显示，避免 <pre> 块级元素带来的大空行
            line += f"\n  ⚠ <i>{ui.escape_html(summary)}</i>"

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
        rid = r.get("request_id") or ""
        if not rid:
            continue
        short = ui.register_code(rid)
        cur.append(ui.btn(f"📄 #{idx}", f"logs:detail:{short}:{page}"))
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
    ui.edit(chat_id, message_id, ui.truncate(_render_list(rows, page=page, total=total, total_pages=total_pages)),
            reply_markup=_list_kb(rows, page=page, total_pages=total_pages))


def send_new(chat_id: int) -> None:
    rows, total, page, total_pages = _page_rows(1)
    ui.send(chat_id, ui.truncate(_render_list(rows, page=page, total=total, total_pages=total_pages)),
            reply_markup=_list_kb(rows, page=page, total_pages=total_pages))


def refresh(chat_id: int, message_id: int, cb_id: str, page: int = 1) -> None:
    show(chat_id, message_id, cb_id, page=page)


# ─── 详情 ─────────────────────────────────────────────────────────

def _render_detail(detail: dict) -> str:
    log = detail.get("log") or {}
    chain = detail.get("retry_chain") or []

    rid = log.get("request_id") or "?"
    created = ui.fmt_bjt_ts(log.get("created_at"), "%Y-%m-%d %H:%M:%S")
    icon = _status_icon(log)
    status = log.get("status") or "?"

    lines = [
        f"📋 <b>日志详情</b> {icon}",
        f"ID: <code>{ui.escape_html(rid)}</code>",
        f"时间: <code>{created}</code>",
        f"状态: <code>{ui.escape_html(status)}</code>"
        + (f" ({log.get('http_status')})" if log.get("http_status") else ""),
        f"客户端: <code>{ui.escape_html(log.get('client_ip') or '?')}</code>"
        f" / Key <code>{ui.escape_html(log.get('api_key_name') or '?')}</code>",
        f"请求模型: <code>{ui.escape_html(log.get('requested_model') or '?')}</code>",
    ]
    if log.get("final_channel_key"):
        lines.append(
            f"最终渠道: <code>{ui.escape_html(log['final_channel_key'])}</code>"
            f" / <code>{ui.escape_html(log.get('final_model') or '?')}</code>"
        )
    # 协议（入口 + 上游）：老日志可能为空，非空才显示避免噪音
    ingress = log.get("ingress_protocol")
    upstream_proto = log.get("upstream_protocol")
    if ingress or upstream_proto:
        lines.append(
            f"协议: 入口 <code>{ui.escape_html(ingress or '?')}</code>"
            f" → 上游 <code>{ui.escape_html(upstream_proto or '?')}</code>"
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

    # 重试链
    lines.append("")
    lines.append(f"<b>重试链 ({len(chain)} 次尝试)</b>")
    if not chain:
        lines.append("  (无记录)")
    for c in chain:
        order = c.get("attempt_order") or "?"
        ch = ui.escape_html(c.get("channel_key") or "?")
        model = ui.escape_html(c.get("model") or "?")
        oc = c.get("outcome") or "?"
        mark = "✅" if oc == "success" else "❌"
        lines.append(f"  {mark} <b>{order}.</b> <code>{ch}</code> / <code>{model}</code> — {ui.escape_html(oc)}")
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
            [ui.btn("📨 查看请求 body", f"logs:body:{body_short}"),
             ui.btn("📬 查看响应", f"logs:response:{resp_short}")],
            [ui.btn(f"◀ 返回第 {page} 页", f"logs:page:{page}")],
        ]),
    )


def _chunk_for_tg(text: str, chunk_size: int = 3900) -> list[str]:
    """把长文本切成多条（每条 <= TG 单消息上限）。"""
    if not text:
        return [""]
    parts: list[str] = []
    for i in range(0, len(text), chunk_size):
        parts.append(text[i:i + chunk_size])
    return parts


def _send_body(chat_id: int, rid: str, kind: str) -> None:
    """kind: 'request' → detail.request_body；'response' → detail.response_body。"""
    try:
        detail = log_db.log_detail(rid)
    except Exception as exc:
        ui.send(chat_id, f"❌ 查询失败: <code>{ui.escape_html(str(exc))}</code>")
        return
    data = (detail or {}).get("detail") or {}
    if kind == "request":
        raw = data.get("request_body")
        label = "Request Body"
    else:
        raw = data.get("response_body")
        label = "Response"
    if not raw:
        ui.send(chat_id, f"(空 {label})")
        return
    # request_body 存的是 JSON；response_body 可能是 JSON 或 SSE 文本
    text = str(raw)
    # 尝试美化 JSON
    try:
        obj = json.loads(text)
        pretty = json.dumps(obj, indent=2, ensure_ascii=False)
        if len(pretty) <= 30000:  # 过大时保留原样
            text = pretty
    except Exception:
        pass
    chunks = _chunk_for_tg(text)
    ui.send(chat_id, f"📄 <b>{label}</b> (<code>{ui.escape_html(rid[:8])}</code>) — {len(chunks)} 段")
    for i, c in enumerate(chunks, 1):
        suffix = f"\n\n<i>[{i}/{len(chunks)}]</i>" if len(chunks) > 1 else ""
        # 用 <pre> 保持格式
        ui.send(chat_id, f"<pre>{ui.escape_html(c)}</pre>{suffix}")


def show_request_body(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    full = ui.resolve_code(short)
    if not full or not full.startswith("logbody:"):
        ui.answer_cb(cb_id, "短码已失效")
        return
    rid = full[len("logbody:"):]
    ui.answer_cb(cb_id, "加载中...")
    _send_body(chat_id, rid, "request")


def show_response_body(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    full = ui.resolve_code(short)
    if not full or not full.startswith("logresp:"):
        ui.answer_cb(cb_id, "短码已失效")
        return
    rid = full[len("logresp:"):]
    ui.answer_cb(cb_id, "加载中...")
    _send_body(chat_id, rid, "response")


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
    return False
