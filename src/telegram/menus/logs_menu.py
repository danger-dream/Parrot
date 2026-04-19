"""最近日志菜单 + 单条详情（含重试链）。

callback_data：
  `menu:logs`           — 显示最近 20 条
  `logs:refresh`        — 刷新列表
  `logs:detail:<short>` — 查看详情（short 是 request_id 前 8 字符的短码）
"""

from __future__ import annotations

import json
from typing import Optional

from ... import log_db
from .. import states, ui


_LIST_LIMIT = 8


_STATUS_ICON = {"success": "✅", "error": "❌", "pending": "⏳"}


def _status_icon(row: dict) -> str:
    return _STATUS_ICON.get(row.get("status"), "?")


def _render_list(rows: list[dict]) -> str:
    if not rows:
        return "📋 <b>最近日志</b>\n\n暂无记录。"
    lines = [f"📋 <b>最近 {len(rows)} 条日志</b>",
             "<i>对照下方按钮的 #编号 点击查看详情</i>"]
    for idx, r in enumerate(rows, 1):
        ts = ui.fmt_bjt_ts(r.get("created_at"), "%m-%d %H:%M:%S")
        icon = _status_icon(r)
        model = ui.escape_html(r.get("requested_model") or "?")
        key = ui.escape_html(r.get("api_key_name") or "?")
        line = f"\n<b>#{idx}</b> <code>{ts}</code> {icon} <b>{key}</b> → {model}"

        # 通道 + 重试数
        if r.get("final_channel_key"):
            ch_short = ui.escape_html(r["final_channel_key"])
            line += f"\n  渠道: <code>{ch_short}</code>"
            if r.get("retry_count"):
                line += f"（重试 {r['retry_count']} 次）"
            if r.get("affinity_hit"):
                line += "  ★亲和"
        detail_parts = []
        if r.get("status") == "success":
            inp = (r.get("input_tokens") or 0) + (r.get("cache_creation_tokens") or 0) + (r.get("cache_read_tokens") or 0)
            cr = r.get("cache_read_tokens") or 0
            tok = f"↑{ui.fmt_tokens(inp)} ↓{ui.fmt_tokens(r.get('output_tokens'))}"
            if cr > 0:
                tok += f" 缓存{ui.fmt_tokens(cr)}"
            detail_parts.append(tok)
        if r.get("connect_time_ms") is not None:
            detail_parts.append(f"连接 {ui.fmt_ms(r['connect_time_ms'])}")
        if r.get("is_stream") and r.get("first_token_time_ms") is not None:
            detail_parts.append(f"首字 {ui.fmt_ms(r['first_token_time_ms'])}")
        if r.get("total_time_ms") is not None:
            detail_parts.append(f"总 {ui.fmt_ms(r['total_time_ms'])}")
        if detail_parts:
            line += "\n  " + " · ".join(detail_parts)

        if r.get("status") == "error" and r.get("error_message"):
            msg = r["error_message"]
            summary = _extract_error_summary(msg)[:120]
            line += f"\n  <pre>{ui.escape_html(summary)}</pre>"

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


def _list_kb(rows: list[dict]) -> dict:
    """按钮区只做"详情入口"，文案与列表正文不重复。

    每条日志一个 `📄 #<序号> <时间>` 按钮（2 列），让用户对照列表里的
    "#1 / #2 / ..." 编号点击查看完整详情。
    """
    rows_kb: list[list[dict]] = []
    cur: list[dict] = []
    # 与列表正文 _LIST_LIMIT 保持一致，避免"文字 N 条 vs 按钮 M 条"的不一致
    for idx, r in enumerate(rows[:_LIST_LIMIT], 1):
        rid = r.get("request_id") or ""
        if not rid:
            continue
        short = ui.register_code(rid)
        ts = ui.fmt_bjt_ts(r.get("created_at"), "%H:%M:%S")
        cur.append(ui.btn(f"📄 #{idx} {ts}", f"logs:detail:{short}"))
        if len(cur) >= 2:
            rows_kb.append(cur)
            cur = []
    if cur:
        rows_kb.append(cur)
    rows_kb.append([ui.btn("🔄 刷新", "logs:refresh"),
                    ui.btn("◀ 返回主菜单", "menu:main")])
    return ui.inline_kb(rows_kb)


# ─── 列表入口 ─────────────────────────────────────────────────────

def show(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    rows = log_db.recent_logs(_LIST_LIMIT)
    ui.edit(chat_id, message_id, ui.truncate(_render_list(rows)),
            reply_markup=_list_kb(rows))


def send_new(chat_id: int) -> None:
    rows = log_db.recent_logs(_LIST_LIMIT)
    ui.send(chat_id, ui.truncate(_render_list(rows)), reply_markup=_list_kb(rows))


def refresh(chat_id: int, message_id: int, cb_id: str) -> None:
    show(chat_id, message_id, cb_id)


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
        inp = (log.get("input_tokens") or 0) + (log.get("cache_creation_tokens") or 0) + (log.get("cache_read_tokens") or 0)
        lines.append("")
        lines.append("<b>Tokens</b>")
        lines.append(
            f"↑ {ui.fmt_tokens(inp)} | ↓ {ui.fmt_tokens(log.get('output_tokens'))} | "
            f"cache {ui.fmt_tokens(log.get('cache_read_tokens'))} "
            f"(读 {ui.fmt_tokens(log.get('cache_read_tokens'))} / 写 {ui.fmt_tokens(log.get('cache_creation_tokens'))})"
        )
    # 耗时
    lines.append("")
    lines.append("<b>耗时</b>")
    lines.append(
        f"连接 {ui.fmt_ms(log.get('connect_time_ms'))} · "
        f"首字 {ui.fmt_ms(log.get('first_token_time_ms'))} · "
        f"总 {ui.fmt_ms(log.get('total_time_ms'))}"
    )

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
            lines.append(f"     ⚠ <pre>{ui.escape_html(_extract_error_summary(c['error_detail'])[:180])}</pre>")

    # 错误信息（整体）
    if status == "error" and log.get("error_message"):
        lines.append("")
        lines.append("<b>错误信息</b>")
        lines.append(f"<pre>{ui.escape_html(_extract_error_summary(log['error_message'])[:300])}</pre>")

    return "\n".join(lines)


def show_detail(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    ui.answer_cb(cb_id)
    rid = ui.resolve_code(short)
    if not rid:
        ui.edit(chat_id, message_id, "⚠ 日志已过期或未找到",
                reply_markup=ui.inline_kb([[ui.btn("◀ 返回列表", "menu:logs")]]))
        return
    try:
        detail = log_db.log_detail(rid)
    except Exception as exc:
        ui.edit(chat_id, message_id, f"❌ 查询失败: <code>{ui.escape_html(str(exc))}</code>",
                reply_markup=ui.inline_kb([[ui.btn("◀ 返回列表", "menu:logs")]]))
        return
    if not detail or not detail.get("log"):
        ui.edit(chat_id, message_id, f"⚠ 未找到 <code>{ui.escape_html(rid)}</code>",
                reply_markup=ui.inline_kb([[ui.btn("◀ 返回列表", "menu:logs")]]))
        return
    body_short = ui.register_code("logbody:" + rid)
    resp_short = ui.register_code("logresp:" + rid)
    ui.edit(
        chat_id, message_id, ui.truncate(_render_detail(detail)),
        reply_markup=ui.inline_kb([
            [ui.btn("📨 查看请求 body", f"logs:body:{body_short}"),
             ui.btn("📬 查看响应", f"logs:response:{resp_short}")],
            [ui.btn("◀ 返回列表", "menu:logs")],
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
        show(chat_id, message_id, cb_id); return True
    if data == "logs:refresh":
        refresh(chat_id, message_id, cb_id); return True
    if data.startswith("logs:detail:"):
        short = data.split(":", 2)[2]
        show_detail(chat_id, message_id, cb_id, short); return True
    if data.startswith("logs:body:"):
        show_request_body(chat_id, message_id, cb_id, data.split(":", 2)[2]); return True
    if data.startswith("logs:response:"):
        show_response_body(chat_id, message_id, cb_id, data.split(":", 2)[2]); return True
    return False
