"""MCP 服务菜单：状态与总开关 + 调用日志。

callback_data 前缀：``mcp:...``

范围说明：本页只负责**服务开关**与**调用日志**。工具级开关和访问 Key 都在
API Key 菜单里（``menu:apikey``），这里不再重复一份，避免两处状态不一致。
"""

from __future__ import annotations

import json
from typing import Optional

from ...management_control.auxiliary.common import telegram_context
from ...management_control.mcp import DEFAULT_MCP_CONTROL
from ...mcp import catalog
from .. import ui


_CONTROL = DEFAULT_MCP_CONTROL

_PAGE_SIZE = 6
_STATUS_ICON = {
    "running": "⏳",
    "success": "✅",
    "error": "❌",
    "timeout": "⌛",
    "denied": "🚫",
}
_TOOL_LABELS = {
    "web_search": "网络搜索",
    "web_fetch": "网页抓取",
    "image_generate": "图片生成",
    "image_edit": "图片编辑",
    "video_generate": "视频生成",
    "video_status": "视频查询",
}


def _ctx(chat_id: int):
    """每次操作都绑定实际 Telegram 管理员身份，不缓存上下文。"""
    return telegram_context(chat_id)


def _tool_label(name: str) -> str:
    return _TOOL_LABELS.get(name, name)


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _fmt_bytes(value) -> str:
    size = _int(value)
    if size <= 0:
        return "0 B"
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / 1024 / 1024:.1f} MB"


def _fmt_ms(value) -> str:
    ms = _int(value)
    if ms <= 0:
        return "-"
    if ms < 1000:
        return f"{ms}ms"
    return f"{ms / 1000:.2f}s"


def _fmt_time(ts) -> str:
    try:
        import time as _time

        return _time.strftime("%m-%d %H:%M:%S", _time.localtime(float(ts)))
    except Exception:
        return "-"


# ─── 主页：状态与开关 ─────────────────────────────────────────────


def _main_text_and_kb(chat_id: int) -> tuple[str, dict]:
    cfg = _CONTROL.get(_ctx(chat_id))
    runtime = cfg.get("runtime") or {}
    enabled = bool(cfg.get("enabled"))
    tools = cfg.get("tools") or {}
    on_count = sum(1 for name in catalog.TOOL_NAMES if tools.get(name))

    status = "✅ 运行中" if enabled else "⛔ 已关闭"
    if enabled and not runtime.get("mounted"):
        status = "⚠ 装配失败"
    endpoint = f"{runtime.get('path') or '/mcp'}"
    lines = [
        "🔌 <b>MCP 服务</b>",
        "",
        f"状态：{status}",
        f"端点：<code>{ui.escape_html(endpoint)}</code>",
        f"工具：<b>{on_count}/{len(catalog.TOOL_NAMES)}</b> 已启用",
        "",
        "<i>工具开关与访问 Key 在「API Key 管理」中按 Key 配置。</i>",
    ]
    if runtime.get("reason"):
        lines.append(f"⚠ {ui.escape_html(str(runtime['reason']))}")

    toggle = "⛔ 关闭 MCP 服务" if enabled else "✅ 开启 MCP 服务"
    rows = [
        [ui.btn(toggle, "mcp:toggle")],
        [ui.btn("📋 调用日志", "mcp:logs:1")],
        [ui.btn("◀ 返回系统设置", "menu:settings")],
    ]
    return "\n".join(lines), ui.inline_kb(rows)


def show(chat_id: int, message_id: int, cb_id: Optional[str] = None) -> None:
    if cb_id is not None:
        ui.answer_cb(cb_id)
    text, kb = _main_text_and_kb(chat_id)
    ui.edit(chat_id, message_id, ui.truncate(text), reply_markup=kb)


def send_new(chat_id: int) -> None:
    text, kb = _main_text_and_kb(chat_id)
    ui.send(chat_id, ui.truncate(text), reply_markup=kb)


def on_toggle(chat_id: int, message_id: int, cb_id: str) -> None:
    cfg = _CONTROL.get(_ctx(chat_id))
    target = not bool(cfg.get("enabled"))
    try:
        _CONTROL.patch(
            _ctx(chat_id), {"enabled": target}, expected_revision=cfg.get("revision"),
        )
    except Exception as exc:
        ui.answer_cb(cb_id, "切换失败", show_alert=True)
        ui.edit(chat_id, message_id,
                f"❌ 切换失败：{ui.escape_html(str(exc))}",
                reply_markup=ui.inline_kb([[ui.btn("◀ 返回", "mcp:show")]]))
        return
    ui.answer_cb(cb_id, "已开启" if target else "已关闭")
    show(chat_id, message_id)


# ─── 调用日志 ─────────────────────────────────────────────────────


def _log_page(chat_id: int, page: int) -> list[dict]:
    value = _CONTROL.logs(_ctx(chat_id), period="today", page=page, page_size=_PAGE_SIZE)
    return value.get("items") or []


def _render_list(rows: list[dict], *, page: int) -> str:
    lines = [
        "📋 <b>MCP 调用日志</b>（今日）",
        "",
    ]
    if not rows:
        lines.append("<i>今日暂无调用</i>")
        return "\n".join(lines)
    for idx, row in enumerate(rows, 1):
        display = (page - 1) * _PAGE_SIZE + idx
        icon = _STATUS_ICON.get(str(row.get("status")), "?")
        tool = _tool_label(str(row.get("toolName") or ""))
        key = str(row.get("apiKeyName") or "-")
        parts = [f"{icon} <b>#{display}</b> {ui.escape_html(tool)}"]
        parts.append(f"<code>{ui.escape_html(key)}</code>")
        if row.get("model"):
            parts.append(ui.escape_html(str(row["model"])))
        lines.append(" · ".join(parts))
        detail = f"   {_fmt_time(row.get('createdAt'))} · {_fmt_ms(row.get('elapsedMs'))}"
        if _int(row.get("resultCount")):
            detail += f" · {_int(row.get('resultCount'))} 项"
        if str(row.get("status")) != "success" and row.get("errorCode"):
            detail += f" · {ui.escape_html(str(row['errorCode']))}"
        lines.append(detail)
    return "\n".join(lines)


def _list_kb(rows: list[dict], *, page: int) -> dict:
    keyboard: list[list[dict]] = []
    if rows:
        details = []
        for idx, row in enumerate(rows, 1):
            short = ui.register_code(f"mcplog:{row.get('id')}")
            display = (page - 1) * _PAGE_SIZE + idx
            details.append(ui.btn(f"📄 #{display}", f"mcp:detail:{short}:{page}"))
        for start in range(0, len(details), 3):
            keyboard.append(details[start:start + 3])
    keyboard.append([
        ui.btn("🏠 首页", "mcp:logs:1"),
        ui.btn("◀ 上一页", f"mcp:logs:{max(1, page - 1)}"),
        ui.btn(f"{page}", f"mcp:logs:{page}"),
        ui.btn("下一页 ▶", f"mcp:logs:{page + 1}"),
    ])
    keyboard.append([
        ui.btn("🔄 刷新", f"mcp:logs:{page}"),
        ui.btn("🔌 MCP 服务", "mcp:show"),
    ])
    keyboard.append([ui.btn("◀ 返回系统设置", "menu:settings")])
    return ui.inline_kb(keyboard)


def show_logs(chat_id: int, message_id: int, cb_id: Optional[str] = None, *, page: int = 1) -> None:
    if cb_id is not None:
        ui.answer_cb(cb_id)
    page = max(1, int(page or 1))
    rows = _log_page(chat_id, page)
    ui.edit(chat_id, message_id, ui.truncate(_render_list(rows, page=page)),
            reply_markup=_list_kb(rows, page=page))


# ─── 日志详情 ─────────────────────────────────────────────────────


def _resolve_log(chat_id: int, short: str) -> dict | None:
    full = ui.resolve_code(short) or ""
    if not full.startswith("mcplog:"):
        return None
    try:
        log_id = int(full[len("mcplog:"):])
    except (TypeError, ValueError):
        return None
    value = _CONTROL.logs(_ctx(chat_id), period="today", page=1, page_size=200)
    return next((row for row in value.get("items") or [] if _int(row.get("id")) == log_id), None)


def _render_detail(row: dict) -> str:
    icon = _STATUS_ICON.get(str(row.get("status")), "?")
    lines = [
        f"{icon} <b>MCP 调用 #{_int(row.get('id'))}</b>",
        "",
        f"🛠 工具：{ui.escape_html(_tool_label(str(row.get('toolName') or '')))}",
        f"🔑 Key：<code>{ui.escape_html(str(row.get('apiKeyName') or '-'))}</code>",
        f"🕒 时间：{_fmt_time(row.get('createdAt'))}",
        f"⏱ 耗时：{_fmt_ms(row.get('elapsedMs'))}",
        f"📊 状态：{ui.escape_html(str(row.get('status') or ''))}",
    ]
    if row.get("clientName"):
        client = str(row["clientName"])
        if row.get("clientVersion"):
            client += f" {row['clientVersion']}"
        lines.append(f"📱 客户端：{ui.escape_html(client)}")
    if row.get("protocolVersion"):
        lines.append(f"📡 协议：<code>{ui.escape_html(str(row['protocolVersion']))}</code>")
    if row.get("sourceId") or row.get("model"):
        target = str(row.get("sourceId") or row.get("model") or "")
        lines.append(f"🎯 来源：<code>{ui.escape_html(target)}</code>")
    if row.get("provider"):
        lines.append(f"🏷 提供方：{ui.escape_html(str(row['provider']))}")
    result_bits = []
    if _int(row.get("resultCount")):
        result_bits.append(f"{_int(row.get('resultCount'))} 项")
    if _int(row.get("resultBytes")):
        result_bits.append(_fmt_bytes(row.get("resultBytes")))
    if result_bits:
        lines.append("📦 结果：" + " · ".join(result_bits))
    if row.get("videoRequestId"):
        lines.append(f"🎬 视频任务：<code>{ui.escape_html(str(row['videoRequestId']))}</code>")
    tokens = row.get("mediaTokens") or []
    if tokens:
        lines.append(f"🖼 媒体：{len(tokens)} 个资源")
    if row.get("errorCode") or row.get("errorMessage"):
        lines.append("")
        lines.append(f"⛔ <b>{ui.escape_html(str(row.get('errorCode') or 'error'))}</b>")
        if row.get("errorMessage"):
            lines.append(ui.escape_html(str(row["errorMessage"])))
    params = row.get("params")
    if params not in (None, {}, []):
        lines.append("")
        lines.append("📝 参数：")
        try:
            text = json.dumps(params, ensure_ascii=False, indent=2)
        except Exception:
            text = str(params)
        lines.append(f"<pre>{ui.escape_html(text[:1200])}</pre>")
    return "\n".join(lines)


def show_detail(chat_id: int, message_id: int, cb_id: str, short: str, *, page: int = 1) -> None:
    ui.answer_cb(cb_id)
    row = _resolve_log(chat_id, short)
    if row is None:
        ui.edit(chat_id, message_id, "⚠ 日志已过期或不存在（仅保留今日）。",
                reply_markup=ui.inline_kb([[ui.btn("◀ 返回日志", f"mcp:logs:{page}")]]))
        return
    ui.edit(chat_id, message_id, ui.truncate(_render_detail(row)),
            reply_markup=ui.inline_kb([[ui.btn(f"◀ 返回第 {page} 页", f"mcp:logs:{page}")]]))


# ─── 回调分发 ─────────────────────────────────────────────────────


def handle_callback(chat_id: int, message_id: int, cb_id: str, data: str) -> bool:
    if not data.startswith("mcp:"):
        return False
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    try:
        if action == "show":
            show(chat_id, message_id, cb_id)
        elif action == "toggle":
            on_toggle(chat_id, message_id, cb_id)
        elif action == "logs":
            show_logs(chat_id, message_id, cb_id,
                      page=int(parts[2]) if len(parts) > 2 else 1)
        elif action == "detail":
            show_detail(chat_id, message_id, cb_id, parts[2],
                        page=int(parts[3]) if len(parts) > 3 else 1)
        else:
            return False
    except Exception:
        ui.answer_cb(cb_id, "操作失败")
        return True
    return True
