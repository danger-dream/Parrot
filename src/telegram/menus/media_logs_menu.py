"""Unified GPT/Grok image and Grok video business-log menu.

callback_data prefix: ``media:...``
"""

from __future__ import annotations

import json
import os
from typing import Optional

from ... import config, media_db
from ...oauth_ids import account_key as make_account_key
from .. import ui


_PAGE_SIZE = 6
_STATUS_ICON = {
    "running": "⏳",
    "pending": "⏳",
    "success": "✅",
    "failed": "❌",
    "expired": "⌛",
    "cancelled": "⏹",
}
_ACTION_LABEL = {"generate": "生成", "edit": "编辑", "extend": "延长"}


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_ms(value) -> str:
    ms = _float(value)
    if ms is None or ms < 0:
        return "-"
    if ms < 1000:
        return f"{int(ms)}ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f}s"
    return f"{ms / 60_000:.1f}m"


def _fmt_bytes(value) -> str:
    size = max(0.0, _float(value) or 0.0)
    units = ("B", "KB", "MB", "GB", "TB")
    idx = 0
    while size >= 1024 and idx < len(units) - 1:
        size /= 1024
        idx += 1
    return f"{int(size)}B" if idx == 0 else f"{size:.1f}{units[idx]}"


def _fmt_cost_ticks(value) -> str:
    ticks = _int(value)
    if ticks <= 0:
        return "-"
    usd = ticks / 10_000_000_000
    if usd < 1:
        return f"${usd:.4f}".rstrip("0").rstrip(".")
    return f"${usd:.2f}"


def _fmt_seconds(value) -> str:
    seconds = _float(value)
    if seconds is None:
        return "-"
    return f"{int(seconds)}s" if seconds.is_integer() else f"{seconds:g}s"


def _provider_label(row: dict) -> str:
    return "Grok" if str(row.get("provider") or "openai") == "xai" else "GPT"


def _media_icon(row: dict) -> str:
    return "🎬" if row.get("media_type") == "video" else "🖼"


def _model(row: dict) -> str:
    return str(row.get("model") or row.get("tool_model") or row.get("main_model") or "?")


def _action(row: dict) -> str:
    return _ACTION_LABEL.get(str(row.get("action") or ""), str(row.get("action") or "?"))


def _status_icon(row: dict) -> str:
    return _STATUS_ICON.get(str(row.get("status") or ""), "❔")


def _progress_text(row: dict) -> str | None:
    progress = _float(row.get("progress"))
    if progress is None:
        return None
    return f"进度 {progress:g}%"


def _dimensions(row: dict) -> str | None:
    parts: list[str] = []
    if row.get("aspect_ratio"):
        parts.append(str(row["aspect_ratio"]))
    if row.get("resolution"):
        parts.append(str(row["resolution"]))
    if not parts and row.get("size"):
        parts.append(str(row["size"]))
    return " / ".join(parts) if parts else None


def _paths(row: dict) -> list[str]:
    try:
        values = json.loads(row.get("cache_paths") or "[]")
    except Exception:
        values = []
    if not isinstance(values, list):
        return []
    return [path for path in values if isinstance(path, str) and os.path.exists(path)]


def _row_metrics(row: dict) -> str:
    parts: list[str] = []
    if row.get("media_type") == "video":
        dims = _dimensions(row)
        if dims:
            parts.append(dims)
        seconds = _float(row.get("media_duration_seconds"))
        if seconds is not None:
            parts.append(_fmt_seconds(seconds))
        progress = _progress_text(row)
        if progress and row.get("status") in {"running", "pending"}:
            parts.append(progress)
    else:
        count = _int(row.get("image_count"))
        requested = max(1, _int(row.get("requested_count")) or 1)
        parts.append(f"{count} 张" if count > 0 else f"请求 {requested} 张")
        dims = _dimensions(row)
        if dims:
            parts.append(dims)

    if row.get("status") in {"running", "pending"}:
        parts.append(f"已等待 {media_db.seconds_since(row.get('created_at'))}s")
    elif row.get("duration_ms") is not None:
        parts.append(_fmt_ms(row.get("duration_ms")))
    cost = _fmt_cost_ticks(row.get("cost_usd_ticks"))
    if cost != "-":
        parts.append(cost)
    return " · ".join(parts) if parts else "-"


def _current_account_top(limit: int = 3) -> list[dict]:
    active_keys = {
        make_account_key(account)
        for account in config.get().get("oauthAccounts", [])
        if isinstance(account, dict)
    }
    active_keys.discard("")

    current: list[dict] = []
    # Read past the first three because historical rows from removed accounts may
    # otherwise hide lower-ranked accounts that still exist in the configuration.
    for row in media_db.account_top(1000):
        if str(row.get("account_key") or "") in active_keys:
            current.append(row)
            if len(current) >= limit:
                break
    return current


def _page_info(page: int, total: int) -> tuple[int, int]:
    pages = max(1, (max(0, total) + _PAGE_SIZE - 1) // _PAGE_SIZE)
    try:
        normalized = int(page or 1)
    except (TypeError, ValueError):
        normalized = 1
    return max(1, min(normalized, pages)), pages


def _render_list(rows: list[dict], *, summary: dict, top: list[dict], page: int, pages: int) -> str:
    total = _int(summary.get("total"))
    lines = [
        f"🎞 <b>最近日志 · 多媒体 · 第 {page}/{pages} 页 · 共 {total} 条</b>",
        "",
        "<b>📊 汇总</b>",
        (
            f"🖼 GPT {_int(summary.get('openai_images'))} · "
            f"Grok {_int(summary.get('xai_images'))} · "
            f"🎬 视频 {_int(summary.get('videos'))}"
        ),
        (
            f"✅ {_int(summary.get('success_count'))} · "
            f"❌ {_int(summary.get('failed_count'))} · "
            f"⏳ {_int(summary.get('pending_count'))} · "
            f"⌛ {_int(summary.get('expired_count'))}"
        ),
    ]
    total_cost = _fmt_cost_ticks(summary.get("cost_usd_ticks"))
    if total_cost != "-":
        lines.append(f"💵 已记录费用 <code>{total_cost}</code>")

    if top:
        lines.extend(["", "<b>👤 OAuth Top 3</b>"])
        for idx, account in enumerate(top[:3], 1):
            name = account.get("account_email") or account.get("account_key") or "?"
            label = "Grok" if account.get("provider") == "xai" else "GPT"
            account_cost = _fmt_cost_ticks(account.get("cost_usd_ticks"))
            suffix = f" · {account_cost}" if account_cost != "-" else ""
            lines.append(
                f"{idx}. {label} <code>{ui.escape_html(name)}</code> · "
                f"{_int(account.get('total'))} 次{suffix}"
            )

    lines.extend(["", "<b>🕘 任务记录</b>"])
    if not rows:
        lines.append("暂无多媒体调用记录。")
        return "\n".join(lines)

    for idx, row in enumerate(rows, 1):
        display = (page - 1) * _PAGE_SIZE + idx
        lines.extend([
            (
                f"\n<b>#{display}</b> {_status_icon(row)} {_media_icon(row)} "
                f"{_provider_label(row)} · {_action(row)}"
            ),
            f"<code>{ui.escape_html(_model(row))}</code> · {ui.escape_html(_row_metrics(row))}",
            (
                f"<code>{media_db.fmt_bjt(row.get('created_at'))}</code> · "
                f"Key <code>{ui.escape_html(row.get('api_key_name') or '?')}</code>"
            ),
        ])
        if row.get("status") in {"failed", "expired", "cancelled"} and row.get("error_message"):
            lines.append(f"<i>{ui.escape_html(str(row['error_message'])[:100])}</i>")
    return "\n".join(lines)


def _list_kb(rows: list[dict], *, page: int, pages: int) -> dict:
    keyboard: list[list[dict]] = [[
        ui.btn("💬 请求日志", "menu:logs"),
        ui.btn("✅ 🎞 多媒体日志", f"media:page:{page}"),
    ]]
    details: list[dict] = []
    for idx, row in enumerate(rows, 1):
        short = ui.register_code(f"medialog:{row.get('id')}")
        display = (page - 1) * _PAGE_SIZE + idx
        details.append(ui.btn(f"📄 #{display}", f"media:detail:{short}:{page}"))
    for start in range(0, len(details), 3):
        keyboard.append(details[start:start + 3])
    keyboard.append([
        ui.btn("🏠 首页", "media:page:1"),
        ui.btn("◀ 上一页", f"media:page:{max(1, page - 1)}"),
        ui.btn(f"{page}/{pages}", f"media:page:{page}"),
        ui.btn("下一页 ▶", f"media:page:{min(pages, page + 1)}"),
    ])
    keyboard.append([
        ui.btn("🔄 刷新", f"media:refresh:{page}"),
        ui.btn("◀ 返回主菜单", "menu:main"),
    ])
    return ui.inline_kb(keyboard)


def _page_data(page: int) -> tuple[list[dict], dict, list[dict], int, int]:
    total = media_db.count()
    page, pages = _page_info(page, total)
    rows = media_db.recent(_PAGE_SIZE, offset=(page - 1) * _PAGE_SIZE)
    return rows, media_db.summary(), _current_account_top(3), page, pages


def show(chat_id: int, message_id: int, cb_id: Optional[str] = None, *, page: int = 1) -> None:
    if cb_id is not None:
        ui.answer_cb(cb_id)
    rows, summary, top, page, pages = _page_data(page)
    ui.edit(
        chat_id,
        message_id,
        ui.truncate(_render_list(rows, summary=summary, top=top, page=page, pages=pages)),
        reply_markup=_list_kb(rows, page=page, pages=pages),
    )


def send_new(chat_id: int, *, page: int = 1) -> None:
    rows, summary, top, page, pages = _page_data(page)
    ui.send(
        chat_id,
        ui.truncate(_render_list(rows, summary=summary, top=top, page=page, pages=pages)),
        reply_markup=_list_kb(rows, page=page, pages=pages),
    )


def _resolve_log(short: str) -> dict | None:
    full = ui.resolve_code(short) or ""
    if not full.startswith("medialog:"):
        return None
    try:
        log_id = int(full[len("medialog:"):])
    except (TypeError, ValueError):
        return None
    return media_db.get_log(log_id)


def _render_detail(row: dict) -> str:
    status = str(row.get("status") or "?")
    lines = [
        "🎞 <b>多媒体日志详情</b>",
        f"编号: <code>#{row.get('id')}</code>",
        f"状态: {_status_icon(row)} <code>{ui.escape_html(status)}</code>",
        (
            f"类型: {_media_icon(row)} <b>{_provider_label(row)}</b> · "
            f"{ui.escape_html(_action(row))}"
        ),
        f"模型: <code>{ui.escape_html(_model(row))}</code>",
        f"API Key: <code>{ui.escape_html(row.get('api_key_name') or '?')}</code>",
        (
            "OAuth: <code>"
            + ui.escape_html(row.get("account_email") or row.get("account_key") or "?")
            + "</code>"
        ),
        "",
        f"创建: <code>{media_db.fmt_bjt(row.get('created_at'))}</code>",
        f"更新: <code>{media_db.fmt_bjt(row.get('updated_at'))}</code>",
    ]
    if row.get("finished_at"):
        lines.append(f"完成: <code>{media_db.fmt_bjt(row.get('finished_at'))}</code>")

    dims = _dimensions(row)
    if dims:
        lines.append(f"尺寸: <code>{ui.escape_html(dims)}</code>")
    if row.get("media_type") == "image":
        lines.append(
            f"图片: 请求 <code>{max(1, _int(row.get('requested_count')) or 1)}</code> 张"
            f" · 返回 <code>{_int(row.get('image_count'))}</code> 张"
        )
        if _int(row.get("cached_images")):
            lines.append(
                f"缓存: <code>{_int(row.get('cached_images'))}</code> 张"
                f" · <code>{_fmt_bytes(row.get('image_bytes'))}</code>"
            )
    else:
        duration = _float(row.get("media_duration_seconds"))
        if duration is not None:
            lines.append(f"视频时长: <code>{_fmt_seconds(duration)}</code>")
        progress = _progress_text(row)
        if progress:
            lines.append(f"{progress}")
        cached_paths = _paths(row)
        if cached_paths:
            lines.append(
                f"缓存: <code>{len(cached_paths)}</code> 个"
                f" · <code>{_fmt_bytes(row.get('image_bytes'))}</code>"
            )

    if row.get("request_duration_ms") is not None:
        lines.append(f"创建请求耗时: <code>{_fmt_ms(row.get('request_duration_ms'))}</code>")
    if row.get("duration_ms") is not None:
        label = "最终生成耗时" if row.get("media_type") == "video" else "总耗时"
        lines.append(f"{label}: <code>{_fmt_ms(row.get('duration_ms'))}</code>")
    cost = _fmt_cost_ticks(row.get("cost_usd_ticks"))
    if cost != "-":
        lines.append(f"金额: <code>{cost}</code>")
    if row.get("http_status"):
        lines.append(f"HTTP 状态: <code>{_int(row.get('http_status'))}</code>")
    if row.get("upstream_status"):
        lines.append(f"上游状态: <code>{ui.escape_html(row.get('upstream_status'))}</code>")
    if row.get("last_polled_at"):
        lines.append(f"最后查询: <code>{media_db.fmt_bjt(row.get('last_polled_at'))}</code>")

    lines.extend([
        "",
        f"本地 ID: <code>{ui.escape_html(row.get('request_id') or '?')}</code>",
    ])
    if row.get("upstream_request_id"):
        lines.append(
            f"上游 request_id: <code>{ui.escape_html(row.get('upstream_request_id'))}</code>"
        )
    if row.get("prompt_preview"):
        lines.extend([
            "",
            "<b>提示词摘要</b>",
            f"<i>{ui.escape_html(row.get('prompt_preview'))}</i>",
        ])
    if row.get("error_message"):
        lines.extend([
            "",
            "<b>错误</b>",
            f"<code>{ui.escape_html(row.get('error_type') or 'error')}</code>",
            f"<i>{ui.escape_html(row.get('error_message'))}</i>",
        ])
    return "\n".join(lines)


def show_detail(
    chat_id: int,
    message_id: int,
    cb_id: str,
    short: str,
    *,
    page: int = 1,
) -> None:
    ui.answer_cb(cb_id)
    row = _resolve_log(short)
    if row is None:
        ui.edit(
            chat_id,
            message_id,
            "⚠ 多媒体日志已过期或不存在。",
            reply_markup=ui.inline_kb([[ui.btn("◀ 返回多媒体日志", f"media:page:{page}")]]),
        )
        return
    buttons: list[list[dict]] = []
    if _paths(row):
        label = "🎬 查看缓存视频" if row.get("media_type") == "video" else "🖼 查看缓存图片"
        buttons.append([ui.btn(label, f"media:view:{short}:{page}")])
    buttons.append([ui.btn(f"◀ 返回第 {page} 页", f"media:page:{page}")])
    ui.edit(
        chat_id,
        message_id,
        ui.truncate(_render_detail(row)),
        reply_markup=ui.inline_kb(buttons),
    )


def send_cached_media(chat_id: int, cb_id: str, short: str) -> None:
    row = _resolve_log(short)
    if row is None:
        ui.answer_cb(cb_id, "日志已过期或不存在")
        return
    paths = _paths(row)
    if not paths:
        ui.answer_cb(cb_id, "媒体缓存不存在或已清理", show_alert=True)
        return
    is_video = row.get("media_type") == "video"
    ui.answer_cb(cb_id, "正在发送视频…" if is_video else "正在发送图片…")
    caption = (
        f"{_media_icon(row)} 多媒体日志 #{row.get('id')} · "
        f"{_provider_label(row)} {_action(row)}\n"
        f"模型: <code>{ui.escape_html(_model(row))}</code>"
    )
    for path in paths[:1 if is_video else 5]:
        if is_video:
            ui.send_video(chat_id, path, caption=caption)
        else:
            ui.send_photo(chat_id, path, caption=caption)


def send_cached_images(chat_id: int, cb_id: str, short: str) -> None:
    """Backward-compatible helper retained for existing menu callers/tests."""
    send_cached_media(chat_id, cb_id, short)


def handle_callback(chat_id: int, message_id: int, cb_id: str, data: str) -> bool:
    if data == "media:logs":
        show(chat_id, message_id, cb_id, page=1)
        return True
    if data.startswith("media:page:") or data.startswith("media:refresh:"):
        try:
            page = int(data.rsplit(":", 1)[1])
        except (TypeError, ValueError):
            page = 1
        show(chat_id, message_id, cb_id, page=page)
        return True
    if data.startswith("media:detail:"):
        payload = data.split(":", 2)[2]
        short, _, page_text = payload.partition(":")
        try:
            page = int(page_text or 1)
        except (TypeError, ValueError):
            page = 1
        show_detail(chat_id, message_id, cb_id, short, page=page)
        return True
    if data.startswith("media:view:"):
        payload = data.split(":", 2)[2]
        short, _, _page_text = payload.partition(":")
        send_cached_media(chat_id, cb_id, short)
        return True
    return False
