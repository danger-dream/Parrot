"""Telegram Bot UI 工具与全局状态。

职责：
  - 维护 httpx 持久 Client 发 Bot API
  - send / edit / answer_cb 辅助
  - inline_kb 构造
  - admin 验证
  - callback_data 短码表（解决 name 过长的 64 字节限制）
  - HTML escape

所有菜单模块通过本模块提供的辅助函数操作。
"""

from __future__ import annotations

import errno
import hashlib
import math
import re
import threading
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Optional

import httpx

from .. import cache_display, network


# ─── 全局配置 ─────────────────────────────────────────────────────

_bot_token: str = ""
_admin_ids: set[int] = set()
_session: Optional[httpx.Client] = None
_session_lock = threading.Lock()


def configure(bot_token: str, admin_ids: list) -> None:
    """初始化 bot token 和 admin 白名单。

    admin_ids 容错：接受 int / 字符串数字 / 字符串混合。所有元素归一化为 int。
    （config.json 里如果误写成 ["123"] 而非 [123] 也能正常工作。）
    """
    global _bot_token, _admin_ids
    _bot_token = bot_token
    normalized: set[int] = set()
    for x in admin_ids or []:
        try:
            normalized.add(int(x))
        except (TypeError, ValueError):
            print(f"[tg] WARN: ignoring non-numeric adminId: {x!r}")
    _admin_ids = normalized


def get_token() -> str:
    return _bot_token


def admin_ids() -> set[int]:
    return set(_admin_ids)


def is_admin(chat_id) -> bool:
    """Admin 白名单判定。chat_id 接受 int / 字符串数字（防御性归一化）。"""
    if not _admin_ids:
        # 空白 admin 列表 = 不限（仅开发调试时使用；生产必须配）
        return True
    try:
        return int(chat_id) in _admin_ids
    except (TypeError, ValueError):
        return False


# ─── httpx 会话 ───────────────────────────────────────────────────

def _make_session() -> httpx.Client:
    return network.sync_client(
        timeout=httpx.Timeout(connect=10.0, read=50.0, write=10.0, pool=10.0),
        limits=httpx.Limits(max_connections=5, max_keepalive_connections=2, keepalive_expiry=30),
        http2=False,
        proxy_purpose="telegram",
    )


def rebuild_session() -> None:
    """重建 httpx 会话；不在 session 锁内执行可能阻塞的 close。"""
    global _session
    replacement = _make_session()
    with _session_lock:
        previous, _session = _session, replacement
    try:
        if previous is not None:
            previous.close()
    except Exception:
        pass


def _get_session() -> httpx.Client:
    global _session
    with _session_lock:
        if _session is None:
            _session = _make_session()
        return _session


def close_session() -> None:
    global _session
    with _session_lock:
        if _session is not None:
            try:
                _session.close()
            except Exception:
                pass
            _session = None


# ─── API 调用 ─────────────────────────────────────────────────────

_PARSE_ERR_MARKERS = (
    "can't parse entities",
    "can't find end of the entity",
    "unsupported start tag",
    "expected end tag",
    "unclosed",
    "unexpected end tag",
)
_MSG_NOT_MODIFIED = "message is not modified"


def _is_parse_error(desc: str) -> bool:
    d = (desc or "").lower()
    return any(marker in d for marker in _PARSE_ERR_MARKERS)


def _strip_html_tags(text: str) -> str:
    """剥离 HTML 标签，还原 &amp; &lt; &gt; &quot; &#39;。用于 parse 失败时的纯文本 fallback。"""
    import re
    out = re.sub(r"<[^>]+>", "", text or "")
    out = out.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'").replace("&amp;", "&")
    return out


def _exception_has_errno(exc: BaseException, target_errno: int) -> bool:
    """Inspect an exception's explicit cause/context chain without looping."""
    pending: list[BaseException] = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if getattr(current, "errno", None) == target_errno:
            return True
        for nested in (current.__cause__, current.__context__):
            if isinstance(nested, BaseException):
                pending.append(nested)
    return False


def _recover_eaddrnotavail(exc: BaseException) -> bool:
    if not _exception_has_errno(exc, errno.EADDRNOTAVAIL):
        return False
    network.invalidate_dns_cache("api.telegram.org")
    rebuild_session()
    return True


def api(method: str, data: Optional[dict] = None) -> Optional[dict]:
    """调用一次 Bot API。

    失败行为：
      - 网络异常 / 无 token → 返回 None，打印日志
      - TG 返回 `ok=false` 且 description 指向解析错误 → 自动用**纯文本**（无 parse_mode）重发
      - TG 返回 `ok=false` 且 description 含 "message is not modified" → 视为成功，不打印噪音
      - 其他 TG 错误 → 打印描述，返回原始 json
    """
    if not _bot_token:
        return None
    url = f"https://api.telegram.org/bot{_bot_token}/{method}"
    try:
        session = _get_session()
        if data is None:
            resp = session.get(url)
        else:
            resp = session.post(url, json=data)
        result = resp.json()
    except Exception as exc:
        if _exception_has_errno(exc, errno.EADDRNOTAVAIL):
            try:
                _recover_eaddrnotavail(exc)
                print(f"[tg] api {method} failed: local address unavailable; DNS cache invalidated and session rebuilt")
            except Exception as recovery_exc:
                # Keep this log credential-safe: exception strings can contain request URLs.
                print(f"[tg] api {method} recovery failed: {type(recovery_exc).__name__}")
        else:
            print(f"[tg] api {method} failed: {exc}")
        return None

    if not isinstance(result, dict) or result.get("ok"):
        return result

    desc = str(result.get("description") or "")

    # editMessage 重编辑相同内容 → 吞掉（常见噪音）
    if _MSG_NOT_MODIFIED in desc.lower():
        return {"ok": True, "result": {"not_modified": True}}

    # HTML 解析失败 → 退化为纯文本重发
    if _is_parse_error(desc) and data and data.get("parse_mode") and data.get("text"):
        fallback = dict(data)
        fallback.pop("parse_mode", None)
        fallback["text"] = _strip_html_tags(fallback["text"])
        print(f"[tg] {method} parse error ({desc[:80]}); retry as plain text")
        try:
            resp2 = session.post(url, json=fallback)
            r2 = resp2.json()
            if isinstance(r2, dict) and r2.get("ok"):
                return r2
            # 重发仍失败，打印后返回原 result
            print(f"[tg] {method} plain-text retry also failed: {r2}")
        except Exception as exc:
            print(f"[tg] {method} plain-text retry error: {exc}")
        return result

    # 其他错误：打印但返回原始，让调用方决定
    print(f"[tg] {method} not ok: {desc[:200]}")
    return result


# ─── 消息发送辅助 ─────────────────────────────────────────────────

def send(chat_id: int, text: str,
         reply_markup: Optional[dict] = None,
         parse_mode: str = "HTML") -> Optional[dict]:
    data: dict[str, Any] = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        data["reply_markup"] = reply_markup
    return api("sendMessage", data)


def edit(chat_id: int, message_id: int, text: str,
         reply_markup: Optional[dict] = None,
         parse_mode: str = "HTML") -> Optional[dict]:
    data: dict[str, Any] = {
        "chat_id": chat_id, "message_id": message_id,
        "text": text, "parse_mode": parse_mode,
    }
    if reply_markup:
        data["reply_markup"] = reply_markup
    return api("editMessageText", data)


def answer_cb(callback_query_id: str, text: Optional[str] = None,
              show_alert: bool = False) -> Optional[dict]:
    data: dict[str, Any] = {"callback_query_id": callback_query_id}
    if text is not None:
        data["text"] = text
    if show_alert:
        data["show_alert"] = True
    return api("answerCallbackQuery", data)


def delete_message(chat_id: int, message_id: int) -> Optional[dict]:
    """删除一条消息。失败（如已被删除/超过 48h）静默忽略。"""
    return api("deleteMessage", {"chat_id": chat_id, "message_id": message_id})


def download_file(file_id: str, *, max_bytes: int = 10 * 1024 * 1024) -> tuple[bytes, str]:
    """下载 Telegram 文件到内存，返回 (content, file_path)。"""
    if not _bot_token:
        raise RuntimeError("telegram bot token is not configured")
    meta = api("getFile", {"file_id": file_id})
    if not isinstance(meta, dict) or not meta.get("ok"):
        raise RuntimeError("getFile failed")
    result = meta.get("result") or {}
    file_path = str(result.get("file_path") or "")
    if not file_path:
        raise RuntimeError("getFile returned empty file_path")
    file_size = result.get("file_size")
    try:
        if file_size is not None and int(file_size) > max_bytes:
            raise RuntimeError(f"file too large: {int(file_size)} bytes")
    except ValueError:
        pass

    url = f"https://api.telegram.org/file/bot{_bot_token}/{file_path}"
    session = _get_session()
    try:
        resp = session.get(url)
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        status = getattr(exc.response, "status_code", "?")
        raise RuntimeError(f"download file failed: HTTP {status}") from exc
    except httpx.RequestError as exc:
        raise RuntimeError(f"download file failed: {type(exc).__name__}") from exc
    content = resp.content
    if len(content) > max_bytes:
        raise RuntimeError(f"file too large: {len(content)} bytes")
    return content, file_path


def send_photo(chat_id: int, path: str, caption: str = "") -> Optional[dict]:
    """发送本地图片文件。用于图片日志的「查看图片」按钮。"""
    if not _bot_token:
        return None
    url = f"https://api.telegram.org/bot{_bot_token}/sendPhoto"
    try:
        session = _get_session()
        with open(path, "rb") as f:
            data: dict[str, Any] = {"chat_id": chat_id}
            if caption:
                data["caption"] = caption
                data["parse_mode"] = "HTML"
            resp = session.post(url, data=data, files={"photo": (path.rsplit("/", 1)[-1], f)})
        result = resp.json()
        if isinstance(result, dict) and result.get("ok"):
            return result
        print(f"[tg] sendPhoto not ok: {str(result)[:200]}")
        return result
    except Exception as exc:
        print(f"[tg] sendPhoto failed: {exc}")
        return None


def send_video(chat_id: int, path: str, caption: str = "") -> Optional[dict]:
    """发送本地视频文件。用于多媒体日志的「查看视频」按钮。"""
    if not _bot_token:
        return None
    url = f"https://api.telegram.org/bot{_bot_token}/sendVideo"
    try:
        session = _get_session()
        with open(path, "rb") as f:
            data: dict[str, Any] = {
                "chat_id": chat_id,
                "supports_streaming": "true",
            }
            if caption:
                data["caption"] = caption
                data["parse_mode"] = "HTML"
            resp = session.post(
                url,
                data=data,
                files={"video": (path.rsplit("/", 1)[-1], f, "video/mp4")},
            )
        result = resp.json()
        if isinstance(result, dict) and result.get("ok"):
            return result
        print(f"[tg] sendVideo not ok: {str(result)[:200]}")
        return result
    except Exception as exc:
        print(f"[tg] sendVideo failed: {exc}")
        return None


def send_document_bytes(chat_id: int, data_bytes: bytes, *, filename: str,
                        caption: str = "", content_type: str = "text/plain; charset=utf-8") -> Optional[dict]:
    """发送内存中的文件。用于超长日志导出等场景。"""
    if not _bot_token:
        return None
    url = f"https://api.telegram.org/bot{_bot_token}/sendDocument"
    try:
        session = _get_session()
        data: dict[str, Any] = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption
            data["parse_mode"] = "HTML"
        resp = session.post(
            url, data=data,
            files={"document": (filename, data_bytes, content_type)},
        )
        result = resp.json()
        if isinstance(result, dict) and result.get("ok"):
            return result
        print(f"[tg] sendDocument not ok: {str(result)[:200]}")
        return result
    except Exception as exc:
        print(f"[tg] sendDocument failed: {exc}")
        return None


def send_document_text(chat_id: int, text: str, *, filename: str, caption: str = "") -> Optional[dict]:
    return send_document_bytes(
        chat_id, (text or "").encode("utf-8", errors="replace"),
        filename=filename, caption=caption, content_type="text/plain; charset=utf-8",
    )


def set_my_commands(commands: list[dict]) -> Optional[dict]:
    return api("setMyCommands", {"commands": commands})


def delete_my_commands() -> Optional[dict]:
    """清空 Bot 当前的命令菜单。

    在 setMyCommands 之前调用，避免老菜单残留或与新菜单合并产生不一致。
    """
    return api("deleteMyCommands", {})


# ─── 内联键盘构造 ─────────────────────────────────────────────────

def inline_kb(rows: list[list[dict]]) -> dict:
    """`rows` 是 [[{"text": ..., "callback_data": ...}, ...], ...]。"""
    return {"inline_keyboard": rows}


BTN_LABEL_LIMIT = 60       # Telegram 单按钮 text 上限（实际 64，留余量）
BTN_CALLBACK_LIMIT = 64    # Telegram callback_data 上限


def _truncate_btn_label(label: str, limit: int = BTN_LABEL_LIMIT) -> str:
    if len(label) <= limit:
        return label
    return label[:limit - 1] + "…"


def btn(
    text: str,
    callback_data: str,
    *,
    icon_custom_emoji_id: str | None = None,
) -> dict:
    # 自动保护：label 超长截断，callback_data 超长直接 assert（开发期暴露 bug）
    if callback_data and len(callback_data.encode("utf-8")) > BTN_CALLBACK_LIMIT:
        raise ValueError(
            f"callback_data too long ({len(callback_data)}B): {callback_data[:40]}... "
            f"— 用短码替代"
        )
    button = {"text": _truncate_btn_label(text), "callback_data": callback_data}
    custom_id = str(icon_custom_emoji_id or "").strip()
    if custom_id:
        button["icon_custom_emoji_id"] = custom_id
    return button


def btn_url(
    text: str,
    url: str,
    *,
    icon_custom_emoji_id: str | None = None,
) -> dict:
    button = {"text": _truncate_btn_label(text), "url": url}
    custom_id = str(icon_custom_emoji_id or "").strip()
    if custom_id:
        button["icon_custom_emoji_id"] = custom_id
    return button


# ─── 通用导航 / 确认按钮 ─────────────────────────────────────────

def back_to_main_row() -> list[dict]:
    """统一的"◀ 返回主菜单"按钮行。"""
    return [btn("◀ 返回主菜单", "menu:main")]


def nav_row(back_label: str, back_callback: str) -> list[dict]:
    """统一的"返回 + 主菜单"双按钮行（用于较深的菜单页）。"""
    return [btn(back_label, back_callback), btn("🏠 主菜单", "menu:main")]


def confirm_kb(confirm_callback: str, cancel_callback: str = "menu:main",
               confirm_label: str = "✅ 确认", cancel_label: str = "❌ 取消") -> dict:
    """二次确认按钮：确认 / 取消。"""
    return inline_kb([[btn(confirm_label, confirm_callback), btn(cancel_label, cancel_callback)]])


# ─── 状态机输入完成的"成果消息"统一发送 ─────────────────────────
#
# 状态机输入完成时，回复一条带导航的"成果消息"——避免"send 成果 + send 主菜单"
# 这种双消息累积。调用方传 text 和"操作目标"的返回 callback；不再自动 send 主菜单。

def send_result(chat_id: int, text: str,
                back_label: str = "◀ 返回主菜单",
                back_callback: str = "menu:main",
                extra_rows: Optional[list] = None) -> Optional[dict]:
    """发送一条带导航按钮的"操作结果"消息（替代 send + main_menu.show 双消息）。

    extra_rows 是额外的按钮行（在导航之前），形如 [[btn(...), btn(...)], ...]。
    """
    rows: list = list(extra_rows or [])
    rows.append([btn(back_label, back_callback)])
    return send(chat_id, text, reply_markup=inline_kb(rows))


# ─── callback_data 短码表 ────────────────────────────────────────

_code_lock = threading.Lock()
_code_to_name: dict[str, str] = {}


def register_code(name: str) -> str:
    """把 name（任意字符串）映射到 8 位 hex 短码，供 callback_data 使用。

    稳定映射：sha1(name)[:8]。
    """
    short = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    with _code_lock:
        _code_to_name[short] = name
    return short


def resolve_code(short: str) -> Optional[str]:
    with _code_lock:
        return _code_to_name.get(short)


# ─── HTML 工具 ────────────────────────────────────────────────────

def escape_html(s: Any) -> str:
    if s is None:
        return ""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# ─── 长消息截断 ───────────────────────────────────────────────────

TG_MSG_LIMIT = 4096


def truncate(text: str, limit: int = 3900, suffix: str = "\n\n... (已截断)") -> str:
    if len(text) <= limit:
        return text
    return text[: limit - len(suffix)] + suffix


# ─── 数值格式化 ───────────────────────────────────────────────────

def fmt_tokens(n) -> str:
    """1234567 → 1.2M；1234 → 1.2K；else → 原样。"""
    return cache_display.fmt_tokens(n)


def fmt_rate(num, denom) -> str:
    return cache_display.fmt_rate(num, denom)


_QUOTA_PROGRESS_WIDTH = 10
_QUOTA_PROGRESS_FILLED = "█"
_QUOTA_PROGRESS_EMPTY = "░"


def quota_progress_enabled() -> bool:
    """Whether quota percentages include the shared monochrome progress bar."""
    from .. import config

    return bool(config.get().get("quotaProgressBar", True))


def quota_progress_bar(percent, *, width: int = _QUOTA_PROGRESS_WIDTH) -> str:
    """Render a clamped monochrome bar for an already display-oriented percent."""
    try:
        pct = float(percent)
    except (TypeError, ValueError, OverflowError):
        pct = 0.0
    if not math.isfinite(pct):
        pct = 0.0
    pct = max(0.0, min(100.0, pct))
    width = max(1, int(width))
    filled = int((pct * width + 50) // 100)
    filled = max(0, min(width, filled))
    return _QUOTA_PROGRESS_FILLED * filled + _QUOTA_PROGRESS_EMPTY * (width - filled)


def quota_progress_html(percent, *, width: int = _QUOTA_PROGRESS_WIDTH) -> str:
    """Return one HTML progress-bar suffix, or an empty string when disabled."""
    if not quota_progress_enabled():
        return ""
    return f" <code>{quota_progress_bar(percent, width=width)}</code>"


def prompt_total(input_tokens=0, cache_creation=0, cache_read=0) -> int:
    return cache_display.prompt_total(input_tokens, cache_creation, cache_read)


def prompt_total_from_row(row: dict, *, aggregate: bool = False) -> int:
    return cache_display.prompt_total_from_row(row, aggregate=aggregate)


def fmt_cache_read(cache_read, prompt) -> str:
    """读缓存展示：`51.7K (60.8%)`。"""
    return cache_display.cache_read_label(cache_read, prompt)


def fmt_cache_phrase(cache_read, prompt) -> str:
    """完整读缓存短语：`缓存 51.7K (60.8%)`。"""
    return cache_display.cache_read_phrase(cache_read, prompt)


def fmt_cache_phrase_from_row(row: dict, *, aggregate: bool = False) -> str:
    return cache_display.cache_read_phrase_from_row(row, aggregate=aggregate)


def fmt_cost(
    metrics: dict | None,
    *,
    show_source: bool = True,
    decimal_places: int = 2,
) -> str:
    """Format the combined USD amount without exposing settlement sources."""
    from .. import config

    pricing_cfg = config.get().get("pricing", {})
    if isinstance(pricing_cfg, dict) and not bool(pricing_cfg.get("enabled", True)):
        return "已关闭"
    data = metrics if isinstance(metrics, dict) else {}

    def nonnegative_int(name: str) -> int:
        try:
            return max(0, int(data.get(name) or 0))
        except (TypeError, ValueError, OverflowError):
            return 0

    # ``cost_ticks`` is the authoritative sum of provider-reported and
    # token-priced amounts.  Keep ``show_source`` for call-site compatibility,
    # but every Telegram surface now intentionally renders only that total.
    _ = show_source
    if "cost_ticks" in data:
        ticks = nonnegative_int("cost_ticks")
    else:
        ticks = nonnegative_int("actual_cost_ticks") + nonnegative_int(
            "estimated_cost_ticks"
        )
    return fmt_usd(
        Decimal(ticks) / Decimal(10_000_000_000),
        decimal_places=decimal_places,
    )


def fmt_usd(value, *, decimal_places: int = 2) -> str:
    """Format a USD value using decimal half-up rounding."""
    places = max(0, int(decimal_places))
    try:
        amount = Decimal(str(value or 0))
    except (InvalidOperation, TypeError, ValueError):
        amount = Decimal(0)
    if not amount.is_finite() or amount < 0:
        amount = Decimal(0)
    try:
        amount = amount.quantize(
            Decimal(1).scaleb(-places),
            rounding=ROUND_HALF_UP,
        )
    except InvalidOperation:
        amount = Decimal(0)
    return f"${amount:,.{places}f}"


def cost_metrics_from_row(row: dict | None) -> dict:
    """Return one request's immutable/fallback cost metrics."""
    from .. import log_db

    return log_db.cost_for_log(row)


def fmt_cost_from_row(row: dict | None) -> str:
    """Format one request row's combined cost amount."""

    return fmt_cost(cost_metrics_from_row(row))


def fmt_ms(ms) -> str:
    if ms is None:
        return "-"
    try:
        ms = float(ms)
    except Exception:
        return "-"
    if ms < 1000:
        return f"{int(ms)}ms"
    return f"{ms / 1000:.1f}s"


def fmt_tps(v) -> str:
    """生成速度格式化：42.3 → '42.3 t/s'；None → '—'。"""
    if v is None:
        return "—"
    try:
        v = float(v)
    except Exception:
        return "—"
    if v >= 1000:
        return f"{v / 1000:.1f}K t/s"
    if v >= 100:
        return f"{v:.0f} t/s"
    return f"{v:.1f} t/s"


def calc_row_tps(row: dict) -> Optional[float]:
    """单条日志的生成速度（t/s）。口径与 log_db._TPS_* 一致：
    stream 有首字 → (total-first) 作分母；非 stream → total。成功才算。"""
    if not row or row.get("status") != "success":
        return None
    out = row.get("output_tokens") or 0
    total = row.get("total_time_ms")
    first = row.get("first_token_time_ms")
    if out <= 0 or total is None or total <= 0:
        return None
    if row.get("is_stream") and first is not None and total > first:
        return out * 1000.0 / (total - first)
    if not row.get("is_stream"):
        return out * 1000.0 / total
    return None


def fmt_bjt_ts(ts: float, pattern: str = "%m-%d %H:%M:%S") -> str:
    """Unix 秒级时间戳 → 北京时间字符串。"""
    from datetime import datetime, timedelta, timezone
    if not ts:
        return "?"
    return datetime.fromtimestamp(ts, tz=timezone(timedelta(hours=8))).strftime(pattern)


# ─── 家族识别 / 展示 ─────────────────────────────────────────────
# upstream_protocol / channel.protocol → 家族（"anthropic" / "openai" / None）
# 用于状态总览 / 统计汇总的家族分段展示。

ANTHROPIC_PROTOCOLS = frozenset({"anthropic"})
OPENAI_PROTOCOLS = frozenset({"openai-chat", "openai-responses"})


def family_of(protocol: Optional[str]) -> Optional[str]:
    if not protocol:
        return None
    if protocol in ANTHROPIC_PROTOCOLS:
        return "anthropic"
    if protocol in OPENAI_PROTOCOLS:
        return "openai"
    return None


FAMILY_ICON = {"anthropic": "🅰️", "openai": "🅾️"}
FAMILY_LABEL = {"anthropic": "Anthropic", "openai": "OpenAI、Grok、Cursor、Antigravity"}
FAMILY_PROVIDER_LABELS = {
    "anthropic": (("claude", "Anthropic"),),
    "openai": (
        ("openai", "OpenAI"),
        ("xai", "Grok"),
        ("cursor", "Cursor"),
        ("antigravity", "Antigravity"),
    ),
}

PROVIDER_BTN_EMOJI = {"claude": "🅰️", "anthropic": "🅰️", "openai": "🅾️", "xai": "𝕏", "cursor": "🖱️", "antigravity": "✨"}
PROVIDER_CUSTOM_EMOJI = {
    "claude": "5872779796257184592",
    "anthropic": "5872779796257184592",
    "openai": "5861557411784957025",
    "xai": "5819115571463068721",
    "cursor": "6062261319426390107",
    "antigravity": "6077644693984779782",
}
PROVIDER_CUSTOM_FALLBACK = {"claude": "🤖", "anthropic": "🤖", "openai": "🤖", "xai": "🐦", "cursor": "🖱️", "antigravity": "✨"}
PROVIDER_LABEL = {"claude": "Claude", "anthropic": "Claude", "openai": "OpenAI", "xai": "Grok", "cursor": "Cursor", "antigravity": "Antigravity"}
PROVIDER_FULL_LABEL = {"claude": "Anthropic Claude", "anthropic": "Anthropic Claude", "openai": "OpenAI", "xai": "xAI Grok", "cursor": "Cursor OAuth", "antigravity": "Antigravity"}


def _provider_key(provider: str | None) -> str:
    p = str(provider or "").strip().lower()
    return "claude" if p == "anthropic" else p


def _telegram_ui_provider_table(name: str) -> dict:
    try:
        from .. import config
        cfg = config.get().get("telegramUi") or {}
        table = cfg.get(name) or {}
        return table if isinstance(table, dict) else {}
    except Exception:
        return {}


def provider_btn_emoji(provider: str | None) -> str:
    p = _provider_key(provider)
    table = _telegram_ui_provider_table("providerBtnEmoji")
    return str(table.get(p) or PROVIDER_BTN_EMOJI.get(p) or "✉")


def provider_icon(provider: str | None) -> str:
    """Plain-text/provider button emoji. Safe for inline keyboards and code blocks."""
    return provider_btn_emoji(provider)


def provider_custom_emoji_id(provider: str | None) -> str:
    p = _provider_key(provider)
    table = _telegram_ui_provider_table("providerCustomEmoji")
    return str(table.get(p) or PROVIDER_CUSTOM_EMOJI.get(p) or "").strip()


def provider_custom_emoji_html(provider: str | None) -> str:
    p = _provider_key(provider)
    custom_id = provider_custom_emoji_id(p)
    if custom_id:
        fallback = PROVIDER_CUSTOM_FALLBACK.get(p) or provider_btn_emoji(p) or "•"
        return f'<tg-emoji emoji-id="{escape_html(custom_id)}">{escape_html(fallback)}</tg-emoji>'
    return escape_html(provider_btn_emoji(p))


def provider_label(provider: str | None, *, full: bool = False) -> str:
    table = PROVIDER_FULL_LABEL if full else PROVIDER_LABEL
    p = _provider_key(provider)
    return table.get(p, p or "OAuth")


def provider_tag(provider: str | None, *, full: bool = False, rich: bool = True) -> str:
    p = _provider_key(provider)
    icon = provider_custom_emoji_html(p) if rich else provider_btn_emoji(p)
    label = provider_label(p, full=full)
    return f"{icon} {escape_html(label) if rich else label}" if label else icon


_STATUS_BUTTON_PREFIXES = (
    "✅", "❌", "⬛", "🟢", "🔴", "⚪️", "⚪", "⚫", "⬜", "☑️", "✔️",
)
_LEADING_DECORATIVE_EMOJI_RE = re.compile(
    r"^(?:[\u2300-\u23FF\u2600-\u27BF\u2B00-\u2BFF\U0001F300-\U0001FAFF]"
    r"[\uFE0F\u200D]*)+\s*"
)


def _provider_button_text(text: str) -> str:
    """Brand custom icon already occupies the button icon slot.

    Decorative leading emoji would stack a second icon; keep status/toggle marks.
    """
    raw = str(text or "")
    stripped = raw.lstrip()
    if any(stripped.startswith(prefix) for prefix in _STATUS_BUTTON_PREFIXES):
        return raw
    rest = _LEADING_DECORATIVE_EMOJI_RE.sub("", stripped, count=1).strip()
    return rest or raw


def provider_button(text: str, callback_data: str, provider: str | None) -> dict:
    """Inline button with the provider's real Telegram custom emoji icon."""
    return btn(
        _provider_button_text(text),
        callback_data,
        icon_custom_emoji_id=provider_custom_emoji_id(provider),
    )


def provider_url_button(text: str, url: str, provider: str | None) -> dict:
    return btn_url(
        _provider_button_text(text),
        url,
        icon_custom_emoji_id=provider_custom_emoji_id(provider),
    )


def family_label(family: str | None) -> str:
    pairs = FAMILY_PROVIDER_LABELS.get(str(family or ""), ())
    if not pairs:
        return str(family or "")
    return "、".join(label for _provider, label in pairs)


def family_tag(
    family: str | None,
    *,
    rich: bool = True,
    suffix: str = "",
) -> str:
    pairs = FAMILY_PROVIDER_LABELS.get(str(family or ""), ())
    if not pairs:
        raw = str(family or "")
        return (escape_html(raw) if rich else raw) + suffix
    parts = []
    for provider, label in pairs:
        icon = (
            provider_custom_emoji_html(provider)
            if rich else provider_btn_emoji(provider)
        )
        parts.append(f"{icon} {escape_html(label) if rich else label}")
    return "、".join(parts) + suffix


def family_button(
    family: str,
    callback_data: str,
    *,
    suffix: str = "",
) -> dict:
    pairs = FAMILY_PROVIDER_LABELS.get(family, ())
    provider = pairs[0][0] if pairs else None
    return provider_button(family_label(family) + suffix, callback_data, provider)


def channel_provider(channel_key: str | None) -> str:
    key = str(channel_key or "")
    if not key.startswith("oauth:"):
        return ""
    try:
        from ..oauth_ids import provider_from_channel_key
        return str(provider_from_channel_key(key) or "")
    except Exception:
        return ""


def channel_provider_custom_emoji_id(channel_key: str | None) -> str:
    provider = channel_provider(channel_key)
    return provider_custom_emoji_id(provider) if provider else ""


def channel_provider_custom_emoji_html(channel_key: str | None) -> str:
    provider = channel_provider(channel_key)
    return provider_custom_emoji_html(provider) if provider else ""


def channel_display_name(channel_key: Any, *, with_family: bool = True) -> str:
    """Human-facing channel name for TG UI.

    Internal channel keys may contain OpenAI workspace ids. Never show those raw
    keys in user-facing menus/logs; resolve OAuth channels back to email and use
    only a short provider tag for disambiguation.
    """
    key = str(channel_key or "?")
    if key.startswith("oauth:"):
        account_key = key[len("oauth:"):]
        try:
            from .. import oauth_manager
            acc = oauth_manager.get_account(account_key)
            if acc is not None:
                provider = oauth_manager.provider_of(acc)
                name = str(
                    (acc.get("label") or acc.get("email") or "?")
                    if provider == "cursor" else (acc.get("email") or "?")
                )
                if provider == "openai":
                    same_email_count = sum(
                        1 for item in oauth_manager.list_accounts()
                        if oauth_manager.provider_of(item) == "openai"
                        and str(item.get("email") or "") == name
                    )
                    if same_email_count > 1:
                        workspace = str(
                            acc.get("workspace_name")
                            or acc.get("workspace_type")
                            or acc.get("plan_type")
                            or "workspace"
                        )
                        name = f"{name} · {workspace}"
            else:
                name = oauth_manager.account_key_to_email(account_key) or "?"
                provider = oauth_manager.provider_of(account_key)
        except Exception:
            # Last resort for legacy Claude keys. For OpenAI workspace ids this
            # may still be opaque, but normal runtime should resolve via config.
            provider, _, ident = account_key.partition(":")
            name = ident or account_key
        if provider:
            if with_family:
                name += f" · {provider_tag(provider, rich=False)}"
            else:
                # Logs and compact stats still need the provider label because
                # the same email can exist as OpenAI and xAI/Grok accounts.
                name += f" · {provider_label(provider)}"
        return name
    if key.startswith("api:"):
        return key.split(":", 1)[1]
    if ":" in key:
        return key.split(":", 1)[1]
    return key


# ─── 通知钩子：把 notifier.notify 转发到管理员 ──────────────────

def install_notify_handler() -> None:
    """把 notifier 的 handler 指向"向所有 admin 发消息"。

    重要：handler 不再对整段 text 做 escape——通知文案本身含 HTML 标签
    （<b>/<code>/<i> 等），escape 会让它们显示成字面值。**调用方** 负责对嵌入
    通知文案的用户字符串做 `notifier.escape_html(...)`。

    auto_delete_seconds: 在文案末尾追加倒计时提示，并起 daemon 线程延迟删除。
    """
    import threading
    import time as _time
    from .. import notifier

    def _delayed_delete(chat_id: int, msg_id: int, delay: int) -> None:
        def _runner():
            _time.sleep(delay)
            try:
                delete_message(chat_id, msg_id)
            except Exception:
                pass
        threading.Thread(
            target=_runner, daemon=True, name=f"notif-delete-{chat_id}",
        ).start()

    def _handler(text: str, auto_delete_seconds: Optional[int] = None,
                 reply_markup: Optional[dict] = None,
                 meta: Optional[dict] = None) -> None:
        full_text = text
        if auto_delete_seconds:
            full_text = (
                text + f"\n\n<i>⏱ 本消息将在 {int(auto_delete_seconds)} 秒后自动删除</i>"
            )
        first_msg_id = None
        for cid in list(_admin_ids):
            try:
                resp = send(cid, full_text, reply_markup=reply_markup)
                if resp and resp.get("ok"):
                    mid = (resp.get("result") or {}).get("message_id")
                    if first_msg_id is None:
                        first_msg_id = mid
                    if auto_delete_seconds and mid:
                        _delayed_delete(cid, mid, int(auto_delete_seconds))
            except Exception:
                pass
        # 回填首个 admin 的 message_id（供自更新流程 edit 同一条通知）
        if meta is not None and isinstance(meta, dict):
            cb = meta.get("on_sent")
            if callable(cb) and first_msg_id is not None:
                try:
                    cb(list(_admin_ids)[0] if _admin_ids else None, first_msg_id)
                except Exception:
                    pass

    notifier.set_handler(_handler)


# ─── 日志条目共用渲染 ──────────────────────────────────────────────

_LOG_STATUS_ICON = {"success": "✅", "error": "❌", "cancelled": "⏹", "pending": "⏳"}
_LOG_INGRESS_TAG = {"chat": "[chat]", "responses": "[response]", "responses_ws": "[response]"}


def _log_transport_tag(row: dict) -> str:
    if (row.get("ingress_protocol") or "") == "responses_ws":
        return "WS"
    if (row.get("ingress_protocol") or "") == "responses" and (row.get("upstream_transport") or "").lower() == "ws":
        return "↑WS"
    return ""


def _log_model_display(r: dict) -> str:
    requested = str(r.get("requested_model") or "?")
    final = str(r.get("final_model") or "").strip()
    if final and final != requested:
        return f"{escape_html(requested)} → {escape_html(final)}"
    return escape_html(final or requested)


def log_fast_mode_enabled(r: dict) -> bool:
    try:
        return bool(int(r.get("fast_mode") or 0))
    except Exception:
        return bool(r.get("fast_mode"))


def log_fast_mode_badge(r: dict) -> str:
    return "⚡ Fast" if log_fast_mode_enabled(r) else ""


def fmt_log_entry_body(r: dict, *, separate_billing: bool = False) -> str:
    """渲染日志条目的 body 部分。

    列表首行只放编号/时间/Key/状态，模型、渠道、Token、耗时、代理分行展示，
    避免长模型名把 Telegram 单行撑爆。最近日志列表保留紧凑金额；嵌入其他
    统计页面时可将金额独立成行。
    """
    lines: list[str] = []

    # 模型 + 入口协议 + 思考强度
    model_line = f"  模型: <code>{_log_model_display(r)}</code>"
    ing_tag = _LOG_INGRESS_TAG.get(r.get("ingress_protocol") or "", "")
    if ing_tag:
        model_line += f" <code>{ing_tag}</code>"
    effort = r.get("reasoning_effort")
    if effort:
        model_line += f" · 🧠 {escape_html(effort)}"
    fast_badge = log_fast_mode_badge(r)
    if fast_badge:
        model_line += f" · {fast_badge}"
    lines.append(model_line)

    # 渠道
    if r.get("final_channel_key"):
        channel_key = r["final_channel_key"]
        ch_short = escape_html(channel_display_name(channel_key, with_family=False))
        provider_icon = channel_provider_custom_emoji_html(channel_key)
        prefix = f"{provider_icon} " if provider_icon else ""
        ch_line = f"  渠道: {prefix}<code>{ch_short}</code>"
        if r.get("retry_count"):
            ch_line += f"（重试 {r['retry_count']} 次）"
        if r.get("affinity_hit"):
            ch_line += " · ★亲和"
        lines.append(ch_line)

    # Token / billing. Failed attempts can still be billed, so surface an
    # immutable actual/estimated/unpriced fact even when request_log has no
    # final-response Token summary.
    row_status = r.get("status")
    cost_metrics = (
        cost_metrics_from_row(r)
        if row_status in ("success", "error", "cancelled") else {}
    )
    if row_status == "success":
        inp = prompt_total_from_row(r)
        cr = r.get("cache_read_tokens") or 0
        tok = f"↑ {fmt_tokens(inp)} · ↓ {fmt_tokens(r.get('output_tokens'))}"
        if cr > 0:
            tok += f" · {fmt_cache_phrase_from_row(r)}"
        cost_text = fmt_cost(cost_metrics, show_source=False, decimal_places=3)
        if separate_billing:
            lines.append(f"  Token: {tok}")
            lines.append(f"  金额: {cost_text}")
        else:
            lines.append(f"  Token: {tok} · {cost_text}")
    elif row_status in ("error", "cancelled") and (
        int(cost_metrics.get("costed_success") or 0) > 0
        or int(cost_metrics.get("unpriced_success") or 0) > 0
    ):
        lines.append(f"  计费: {fmt_cost(cost_metrics, show_source=False, decimal_places=3)}")

    # 耗时
    timing_parts: list[str] = []
    if r.get("connect_time_ms") is not None:
        timing_parts.append(f"连接 {fmt_ms(r['connect_time_ms'])}")
    if r.get("is_stream") and r.get("first_token_time_ms") is not None:
        timing_parts.append(f"首字 {fmt_ms(r['first_token_time_ms'])}")
    if r.get("total_time_ms") is not None:
        timing_parts.append(f"总 {fmt_ms(r['total_time_ms'])}")
    tps_v = calc_row_tps(r)
    if tps_v is not None:
        timing_parts.append(f"⚡ {fmt_tps(tps_v)}")
    if (r.get("retry_count") or 0) > 0 and not r.get("final_channel_key"):
        timing_parts.append(f"重试 {r['retry_count']} 次")
    if timing_parts:
        lines.append("  耗时: " + " · ".join(timing_parts))

    # 传输协议 / 出站代理 / 本地搜索。常规 response 已在模型行展示，这里只显示特殊 WS。
    transport = _log_transport_tag(r)
    proxy_name = r.get("proxy_name")
    try:
        search_count = int(r.get("local_web_count") or 0)
    except Exception:
        search_count = 0
    network_parts: list[str] = []
    if transport in {"WS", "↑WS"}:
        network_parts.append(f"传输协议: <b>{transport}</b>")
    if proxy_name:
        network_parts.append(f"出站代理: {escape_html(proxy_name)}")
    if network_parts:
        lines.append("  " + " · ".join(network_parts))
    if search_count:
        lines.append(f"  搜索: {search_count} 次")

    # 错误 / 客户端取消
    if r.get("status") in ("error", "cancelled") and r.get("error_message"):
        err_short = escape_html(str(r["error_message"])[:120])
        marker = "⏹" if r.get("status") == "cancelled" else "⚠"
        lines.append(f"  {marker} <i>{err_short}</i>")

    return "\n".join(lines)


def fmt_log_entry_headline(r: dict, *, prefix: str = "") -> str:
    """渲染日志条目首行：#编号 [时间] Key 状态。

    模型等长字段放到 body 分行，避免 Telegram 列表单行过长换行。
    """
    ts = fmt_bjt_ts(r.get("created_at"), "%m-%d %H:%M:%S")
    icon = _LOG_STATUS_ICON.get(r.get("status"), "?")
    key = escape_html(r.get("api_key_name") or "?")
    return f"{prefix}<code>[{ts}]</code> <b>{key}</b> {icon}"
