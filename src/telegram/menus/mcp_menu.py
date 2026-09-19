"""MCP 服务菜单：服务状态与开关 + 调用日志（列表 / 详情 / 结果内容）。

callback_data 前缀：``mcp:...``

范围说明：本页只负责**服务开关**、**接入说明**与**调用日志**。工具级开关和
访问 Key 都在 API Key 菜单里（``menu:apikey``），这里不再重复一份，避免两处
状态不一致。

日志展示与「最近日志 · 多媒体日志」保持同一套结构：汇总 + 逐条多行摘要 +
分页 + 详情页。列表刻意把**查询词/提示词、来源、条数、状态**直接写出来，
因为这些正是排查"模型到底搜了什么、为什么答错"所需要的信息，不应藏在详情页里。
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
_RESULT_PAGE_CHARS = 3200

_STATUS_ICON = {
    "running": "⏳",
    "success": "✅",
    "error": "❌",
    "timeout": "⌛",
    "denied": "🚫",
}
_TOOL_ICON = {
    "web_search": "🔎",
    "web_fetch": "📄",
    "image_generate": "🖼",
    "image_edit": "🎨",
    "video_generate": "🎬",
    "video_status": "📽",
}
_TOOL_LABELS = {
    "web_search": "网络搜索",
    "web_fetch": "网页抓取",
    "image_generate": "图片生成",
    "image_edit": "图片编辑",
    "video_generate": "视频生成",
    "video_status": "视频查询",
}

# 参数里最该被看见的那一个（决定"它搜了什么/画了什么"）。
_SUBJECT_PARAMS = {
    "web_search": ("query", "查询"),
    "web_fetch": ("url", "URL"),
    "image_generate": ("prompt", "提示词"),
    "image_edit": ("prompt", "提示词"),
    "video_generate": ("prompt", "提示词"),
    "video_status": ("request_id", "任务"),
}


def _ctx(chat_id: int):
    """每次操作都绑定实际 Telegram 管理员身份，不缓存上下文。"""
    return telegram_context(chat_id)


def _tool_label(name: str) -> str:
    return _TOOL_LABELS.get(name, name or "?")


def _tool_icon(name: str) -> str:
    return _TOOL_ICON.get(name, "🛠")


def _status_icon(status: str) -> str:
    return _STATUS_ICON.get(status, "❔")


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
    if ms < 60_000:
        return f"{ms / 1000:.2f}s"
    return f"{ms / 60_000:.1f}m"


def _fmt_time(ts) -> str:
    return ui.fmt_bjt_ts(float(ts or 0))


def _clip(text: str, limit: int) -> str:
    value = str(text or "").replace("\n", " ").strip()
    return value if len(value) <= limit else value[:limit] + "…"


def _subject(row: dict) -> tuple[str, str]:
    """返回 (标签, 值)：该次调用最该被看到的那一个输入。"""
    tool = str(row.get("toolName") or "")
    field, label = _SUBJECT_PARAMS.get(tool, ("prompt", "输入"))
    params = row.get("params")
    if isinstance(params, dict):
        for key in (field, "query", "url", "prompt", "request_id"):
            value = params.get(key)
            if isinstance(value, (str, int, float)) and str(value).strip():
                return label, str(value)
    return label, ""


# ─── 主页：状态、接入说明与概览 ───────────────────────────────────


def _install_block(tool_count: int) -> list[str]:
    """接入说明。用 <pre> 保住 JSON 缩进，方便直接复制。"""
    example = (
        '{\n'
        '  "mcpServers": {\n'
        '    "parrot": {\n'
        '      "type": "http",\n'
        '      "url": "https://你的域名/mcp",\n'
        '      "headers": {\n'
        '        "Authorization": "Bearer <Parrot API Key>"\n'
        '      }\n'
        '    }\n'
        '  }\n'
        '}'
    )
    lines = [
        "<b>📖 接入方式</b>",
        "MCP 客户端（Claude Code / Cursor 等）配置：",
        f"<pre>{ui.escape_html(example)}</pre>",
        "不支持 HTTP 的客户端用 <code>mcp-remote</code> 桥接：",
        "<pre>npx -y mcp-remote https://你的域名/mcp \\\n"
        "  --header \"Authorization: Bearer &lt;Key&gt;\"</pre>",
    ]
    if tool_count:
        lines.append("<i>先用「API Key 管理」给某个 Key 开启 MCP，再填入该 Key。</i>")
    return lines


def _main_text_and_kb(chat_id: int) -> tuple[str, dict]:
    cfg = _CONTROL.get(_ctx(chat_id))
    runtime = cfg.get("runtime") or {}
    enabled = bool(cfg.get("enabled"))
    tools = cfg.get("tools") or {}
    on_count = sum(1 for name in catalog.TOOL_NAMES if tools.get(name))
    total_tools = len(catalog.TOOL_NAMES)

    status = "运行中" if enabled else "已关闭"
    status_icon = _status_icon("success") if enabled else "⛔"
    if enabled and not runtime.get("mounted"):
        status = "装配失败"
        status_icon = "⚠"
    endpoint = str(runtime.get("path") or "/mcp")

    lines = [
        "🔌 <b>MCP 服务</b>",
        "",
        f"状态：{status_icon} <b>{status}</b>"
        f"    工具：<b>{on_count}/{total_tools}</b> 已启用",
        f"端点：<code>{ui.escape_html(endpoint)}</code>",
    ]
    if runtime.get("reason"):
        lines.append(f"⚠ {ui.escape_html(str(runtime['reason']))}")

    # 概览：直接回答"今天有没有人在用、用得怎么样"。
    try:
        summary = _CONTROL.summary(_ctx(chat_id), period="today")
    except Exception:
        summary = None
    if summary is not None:
        lines.extend(["", "<b>📊 今日调用</b>"])
        if not _int(summary.get("calls")):
            lines.append("<i>今天还没有调用。</i>")
        else:
            bits = [f"共 <b>{_int(summary.get('calls'))}</b> 次"]
            bits.append(f"✅ {_int(summary.get('success'))}")
            if _int(summary.get("failed")):
                bits.append(f"❌ {_int(summary.get('failed'))}")
            if _int(summary.get("timeout")):
                bits.append(f"⌛ {_int(summary.get('timeout'))}")
            if _int(summary.get("denied")):
                bits.append(f"🚫 {_int(summary.get('denied'))}")
            if _int(summary.get("running")):
                bits.append(f"⏳ {_int(summary.get('running'))}")
            lines.append(" · ".join(bits))
            if _int(summary.get("avgElapsedMs")):
                lines.append(f"⏱ 平均耗时 {_fmt_ms(summary.get('avgElapsedMs'))}")
            by_tool = [t for t in (summary.get("byTool") or []) if _int(t.get("calls"))]
            if by_tool:
                lines.append(" · ".join(
                f"{_tool_icon(t.get('toolName'))} {_int(t.get('calls'))}"
                for t in by_tool[:6]
            ))

    lines.extend(["", *_install_block(on_count)])

    toggle = f"{_status_icon('success')} 已开启：MCP" if enabled else "⛔ 已关闭：MCP"
    rows = [
        [ui.btn(toggle, "mcp:toggle"), ui.btn("📋 调用日志", "mcp:logs:1")],
        [ui.btn("◀ 返回系统设置", "menu:settings"), ui.btn("🏠 返回主菜单", "menu:main")],
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
                reply_markup=ui.inline_kb([
                    [ui.btn("◀ 返回 MCP 服务", "mcp:show"), ui.btn("🏠 主菜单", "menu:main")],
                ]))
        return
    ui.answer_cb(cb_id, "已开启 MCP" if target else "已关闭 MCP")
    show(chat_id, message_id)


# ─── 调用日志：列表 ───────────────────────────────────────────────


def _log_page(chat_id: int, page: int) -> tuple[list[dict], dict | None]:
    value = _CONTROL.logs(_ctx(chat_id), period="today", page=page, page_size=_PAGE_SIZE)
    items = value.get("items") or []
    try:
        summary = _CONTROL.summary(_ctx(chat_id), period="today")
    except Exception:
        summary = None
    return items, summary


def _item_lines(row: dict, display: int) -> list[str]:
    """一条记录的多行摘要：工具 / 输入 / 来源与结果 / Key 与时间。"""
    tool = str(row.get("toolName") or "")
    icon = _status_icon(str(row.get("status")))
    lines = [f"\n<b>#{display}</b> {icon} {_tool_icon(tool)} {ui.escape_html(_tool_label(tool))}"]

    label, value = _subject(row)
    if value:
        lines.append(f"{label} <code>{ui.escape_html(_clip(value, 80))}</code>")

    metrics: list[str] = []
    source = str(row.get("sourceId") or row.get("provider") or row.get("model") or "")
    if source:
        metrics.append(f"<code>{ui.escape_html(_clip(source, 40))}</code>")
    if _int(row.get("resultCount")):
        metrics.append(f"{_int(row.get('resultCount'))} 项")
    if _int(row.get("resultBytes")):
        metrics.append(_fmt_bytes(row.get("resultBytes")))
    if row.get("elapsedMs") is not None:
        metrics.append(_fmt_ms(row.get("elapsedMs")))
    if metrics:
        lines.append(" · ".join(metrics))

    tail = (
        f"Key <code>{ui.escape_html(str(row.get('apiKeyName') or '?'))}</code>"
        f" · <code>{_fmt_time(row.get('createdAt'))}</code>"
    )
    lines.append(tail)

    if str(row.get("status")) not in ("success", "running") and row.get("errorCode"):
        detail = str(row.get("errorMessage") or row.get("errorCode") or "")
        lines.append(f"<i>{ui.escape_html(_clip(detail, 90))}</i>")
    return lines


def _render_list(rows: list[dict], *, page: int, pages: int, summary: dict | None) -> str:
    total = _int((summary or {}).get("calls"))
    lines = [f"📋 <b>MCP 调用日志</b>（今日）· 第 {page}/{pages} 页 · 共 {total} 条"]

    if summary is not None and total:
        lines.extend(["", "<b>📊 汇总</b>"])
        bits = [f"共 <b>{total}</b> 次", f"✅ {_int(summary.get('success'))}"]
        if _int(summary.get("failed")):
            bits.append(f"❌ {_int(summary.get('failed'))}")
        if _int(summary.get("timeout")):
            bits.append(f"⌛ {_int(summary.get('timeout'))}")
        if _int(summary.get("denied")):
            bits.append(f"🚫 {_int(summary.get('denied'))}")
        if _int(summary.get("running")):
            bits.append(f"⏳ {_int(summary.get('running'))}")
        lines.append(" · ".join(bits))
        if _int(summary.get("avgElapsedMs")):
            lines.append(f"⏱ 平均耗时 {_fmt_ms(summary.get('avgElapsedMs'))}")
        by_tool = [t for t in (summary.get("byTool") or []) if _int(t.get("calls"))]
        if by_tool:
            lines.append(" · ".join(
                f"{_tool_icon(t.get('toolName'))} {ui.escape_html(str(t.get('label') or _tool_label(t.get('toolName'))))} "
                f"{_int(t.get('calls'))}"
                for t in by_tool[:6]
            ))

    lines.extend(["", "<b>🕘 调用记录</b>"])
    if not rows:
        lines.append("今日暂无调用。")
        return "\n".join(lines)

    for idx, row in enumerate(rows, 1):
        display = (page - 1) * _PAGE_SIZE + idx
        lines.extend(_item_lines(row, display))
    return "\n".join(lines)


def _list_kb(rows: list[dict], *, page: int, pages: int) -> dict:
    keyboard: list[list[dict]] = [[
        ui.btn("🔌 MCP 服务", "mcp:show"),
        ui.btn("📋 调用日志", f"mcp:logs:{page}"),
    ]]
    details: list[dict] = []
    for idx, row in enumerate(rows, 1):
        short = ui.register_code(f"mcplog:{row.get('callId')}")
        display = (page - 1) * _PAGE_SIZE + idx
        details.append(ui.btn(f"📄 #{display}", f"mcp:detail:{short}:{page}"))
    for start in range(0, len(details), 3):
        keyboard.append(details[start:start + 3])
    keyboard.append([
        ui.btn("🏠 首页", "mcp:logs:1"),
        ui.btn("◀ 上一页", f"mcp:logs:{max(1, page - 1)}"),
        ui.btn(f"{page}/{pages}", f"mcp:logs:{page}"),
        ui.btn("下一页 ▶", f"mcp:logs:{min(pages, page + 1)}"),
    ])
    keyboard.append([
        ui.btn("🔄 刷新", f"mcp:logs:{page}"),
        ui.btn("◀ 返回系统设置", "menu:settings"),
        ui.btn("🏠 返回主菜单", "menu:main"),
    ])
    return ui.inline_kb(keyboard)


def show_logs(chat_id: int, message_id: int, cb_id: Optional[str] = None, *, page: int = 1) -> None:
    if cb_id is not None:
        ui.answer_cb(cb_id)
    page = max(1, int(page or 1))
    rows, summary = _log_page(chat_id, page)
    normalized_page, pages = _page_info(page, _int((summary or {}).get("calls")))
    ui.edit(
        chat_id, message_id,
        ui.truncate(_render_list(rows, page=normalized_page, pages=pages, summary=summary)),
        reply_markup=_list_kb(rows, page=normalized_page, pages=pages),
    )


def _page_info(page: int, total: int) -> tuple[int, int]:
    pages = max(1, (max(0, total) + _PAGE_SIZE - 1) // _PAGE_SIZE)
    return max(1, min(int(page or 1), pages)), pages


# ─── 调用日志：详情 ───────────────────────────────────────────────


def _resolve_log(chat_id: int, short: str) -> dict | None:
    full = ui.resolve_code(short) or ""
    if not full.startswith("mcplog:"):
        return None
    call_id = full[len("mcplog:"):]
    if not call_id:
        return None
    return _CONTROL.log_entry(_ctx(chat_id), call_id, period="today")


def _render_detail(row: dict) -> str:
    tool = str(row.get("toolName") or "")
    status = str(row.get("status") or "")
    lines = [
        f"{_status_icon(status)} <b>MCP 调用详情</b>",
        f"工具：{_tool_icon(tool)} <b>{ui.escape_html(_tool_label(tool))}</b>",
        f"状态：<code>{ui.escape_html(status or '?')}</code>",
        f"Key：<code>{ui.escape_html(str(row.get('apiKeyName') or '?'))}</code>",
        f"时间：<code>{_fmt_time(row.get('createdAt'))}</code>",
        f"耗时：<code>{_fmt_ms(row.get('elapsedMs'))}</code>",
    ]
    if row.get("clientName"):
        client = str(row["clientName"])
        if row.get("clientVersion"):
            client += f" {row['clientVersion']}"
        lines.append(f"客户端：{ui.escape_html(client)}")
    if row.get("protocolVersion"):
        lines.append(f"协议：<code>{ui.escape_html(str(row['protocolVersion']))}</code>")

    lines.append("")
    if row.get("sourceId") or row.get("provider"):
        lines.append(
            "来源：<code>"
            + ui.escape_html(str(row.get("sourceId") or row.get("provider")))
            + "</code>"
        )
    if row.get("model"):
        lines.append(f"模型：<code>{ui.escape_html(str(row['model']))}</code>")
    if row.get("accountKey"):
        lines.append(f"账户：<code>{ui.escape_html(_clip(str(row['accountKey']), 60))}</code>")

    result_bits: list[str] = []
    if _int(row.get("resultCount")):
        result_bits.append(f"{_int(row.get('resultCount'))} 项")
    if _int(row.get("resultBytes")):
        result_bits.append(_fmt_bytes(row.get("resultBytes")))
    if result_bits:
        lines.append("结果：" + " · ".join(result_bits))
    if _int(row.get("inputTokens")) or _int(row.get("outputTokens")):
        lines.append(
            f"Token：<code>{_int(row.get('inputTokens'))}/{_int(row.get('outputTokens'))}</code>"
        )
    if row.get("videoRequestId"):
        lines.append(f"视频任务：<code>{ui.escape_html(str(row['videoRequestId']))}</code>")
    tokens = row.get("mediaTokens") or []
    if tokens:
        lines.append(f"媒体资源：{len(tokens)} 个")

    if row.get("errorCode") or row.get("errorMessage"):
        lines.extend([
            "",
            f"⛔ <b>{ui.escape_html(str(row.get('errorCode') or 'error'))}</b>",
        ])
        if row.get("errorMessage"):
            lines.append(f"<i>{ui.escape_html(str(row['errorMessage']))}</i>")

    params = row.get("params")
    if params not in (None, {}, []):
        lines.extend(["", "<b>📝 参数</b>"])
        try:
            text = json.dumps(params, ensure_ascii=False, indent=2)
        except Exception:
            text = str(params)
        lines.append(f"<pre>{ui.escape_html(text[:1500])}</pre>")
    return "\n".join(lines)


def show_detail(chat_id: int, message_id: int, cb_id: str, short: str, *, page: int = 1) -> None:
    ui.answer_cb(cb_id)
    row = _resolve_log(chat_id, short)
    if row is None:
        ui.edit(chat_id, message_id, "⚠ 日志已过期或不存在（仅保留今日）。",
                reply_markup=ui.inline_kb([
                    [ui.btn(f"◀ 返回第 {page} 页", f"mcp:logs:{page}"),
                     ui.btn("🏠 主菜单", "menu:main")],
                ]))
        return
    buttons: list[list[dict]] = []
    call_id = str(row.get("callId") or "")
    if call_id:
        body_short = ui.register_code(f"mcpbody:{call_id}")
        buttons.append([ui.btn("📨 查看结果", f"mcp:result:{body_short}:{page}")])
    buttons.append([
        ui.btn(f"◀ 返回第 {page} 页", f"mcp:logs:{page}"),
        ui.btn("🏠 返回主菜单", "menu:main"),
    ])
    ui.edit(chat_id, message_id, ui.truncate(_render_detail(row)),
            reply_markup=ui.inline_kb(buttons))


# ─── 调用结果内容 ─────────────────────────────────────────────────


def _chunk_pages(text: str, limit: int = _RESULT_PAGE_CHARS) -> list[str]:
    """Bound each page after HTML escaping; retain every original character."""
    body = text or ""
    pages: list[str] = []
    start = size = 0
    for index, char in enumerate(body):
        escaped_size = len(ui.escape_html(char))
        if size and size + escaped_size > limit:
            pages.append(body[start:index])
            start, size = index, 0
        size += escaped_size
    pages.append(body[start:])
    return pages


def _pretty_result(body: str) -> str:
    """把留存的结果正文排成人看的形状。

    搜索结果与抓取正文的差异只在结构：前者是 ``results`` 列表，后者是
    ``content`` 长文本。原始 JSON 一行铺开几乎不可读，因此这里按条目展开；
    解析失败时原样返回，绝不因为排版丢内容。
    """
    text = (body or "").strip()
    if not text.startswith("{") and not text.startswith("["):
        return text
    try:
        data = json.loads(text)
    except Exception:
        return text
    if not isinstance(data, dict):
        return text

    parts: list[str] = []
    query = data.get("query") or data.get("url")
    if query:
        parts.append(f"查询：{query}")
    if data.get("answer"):
        parts.append(f"答案：{data['answer']}")
    warnings = data.get("warnings")
    if isinstance(warnings, list) and warnings:
        parts.append("提示：" + "；".join(str(w) for w in warnings))

    results = data.get("results")
    if isinstance(results, list) and results:
        parts.append(f"结果 {len(results)} 条：")
        for idx, item in enumerate(results, 1):
            if not isinstance(item, dict):
                parts.append(f"{idx}. {item}")
                continue
            title = str(item.get("title") or item.get("name") or "").strip()
            url = str(item.get("url") or item.get("link") or "").strip()
            parts.append(f"\n{idx}. {title or '(无标题)'}")
            if url:
                parts.append(f"   {url}")
            snippet = str(item.get("snippet") or item.get("content") or item.get("description") or "").strip()
            if snippet:
                parts.append(f"   {snippet}")
        if data.get("truncated"):
            parts.append("\n（上游标记结果已截断）")
        return "\n".join(parts)

    content = data.get("content")
    if isinstance(content, str) and content.strip():
        return content
    # 其余工具（图片/视频）没有可展开的结构，回退到格式化 JSON。
    try:
        return json.dumps(data, ensure_ascii=False, indent=2)
    except Exception:
        return text


def show_result(
    chat_id: int, message_id: int, cb_id: str, short: str, *,
    page: int = 1, result_page: int = 1,
) -> None:
    """显示该次调用留存的结果内容（搜索结果 / 抓取正文等）。"""
    ui.answer_cb(cb_id)
    full = ui.resolve_code(short) or ""
    call_id = full[len("mcpbody:"):] if full.startswith("mcpbody:") else ""
    back = ui.inline_kb([
        [ui.btn(f"◀ 返回详情", f"mcp:detail:{ui.register_code('mcplog:' + call_id)}:{page}")],
    ])
    if not call_id:
        ui.edit(chat_id, message_id, "⚠ 日志已过期或不存在。", reply_markup=back)
        return
    try:
        payload = _CONTROL.result_body(_ctx(chat_id), call_id)
    except Exception:
        payload = None
    if not payload or not str(payload.get("body") or "").strip():
        ui.edit(
            chat_id, message_id,
            "⚠ <b>这条记录没有留存结果内容</b>\n\n"
            "<i>可能原因：调用失败（没有结果可存），或「数据留存 → 保存完整请求」"
            "处于关闭状态，或记录已被留存策略清理。</i>",
            reply_markup=back,
        )
        return

    body = _pretty_result(str(payload["body"]))
    pages = _chunk_pages(body)
    idx = max(1, min(int(result_page or 1), len(pages)))
    text = (
        f"📨 <b>调用结果</b> · 第 {idx}/{len(pages)} 页\n"
        f"<i>共 {len(body)} 字符</i>\n\n"
        f"<pre>{ui.escape_html(pages[idx - 1])}</pre>"
    )
    nav: list[dict] = []
    if idx > 1:
        nav.append(ui.btn("◀ 上页", f"mcp:result:{short}:{page}:{idx - 1}"))
    if idx < len(pages):
        nav.append(ui.btn("下页 ▶", f"mcp:result:{short}:{page}:{idx + 1}"))
    rows = ([nav] if nav else [])
    rows.append([ui.btn("◀ 返回详情", f"mcp:detail:{ui.register_code('mcplog:' + call_id)}:{page}")])
    # _chunk_pages already budgets escaped content, leaving room for header/tags.
    ui.edit(chat_id, message_id, text, reply_markup=ui.inline_kb(rows))


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
        elif action == "result":
            show_result(chat_id, message_id, cb_id, parts[2],
                        page=int(parts[3]) if len(parts) > 3 else 1,
                        result_page=int(parts[4]) if len(parts) > 4 else 1)
        else:
            return False
    except Exception:
        ui.answer_cb(cb_id, "操作失败")
        return True
    return True
