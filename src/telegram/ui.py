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

import hashlib
import threading
from typing import Any, Optional

import httpx


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
    return httpx.Client(
        timeout=httpx.Timeout(connect=10.0, read=50.0, write=10.0, pool=10.0),
        limits=httpx.Limits(max_connections=5, max_keepalive_connections=2, keepalive_expiry=30),
        http2=False,
    )


def rebuild_session() -> None:
    """连续失败后调用，重建 httpx 会话。"""
    global _session
    with _session_lock:
        try:
            if _session is not None:
                _session.close()
        except Exception:
            pass
        _session = _make_session()


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


def btn(text: str, callback_data: str) -> dict:
    # 自动保护：label 超长截断，callback_data 超长直接 assert（开发期暴露 bug）
    if callback_data and len(callback_data.encode("utf-8")) > BTN_CALLBACK_LIMIT:
        raise ValueError(
            f"callback_data too long ({len(callback_data)}B): {callback_data[:40]}... "
            f"— 用短码替代"
        )
    return {"text": _truncate_btn_label(text), "callback_data": callback_data}


def btn_url(text: str, url: str) -> dict:
    return {"text": text, "url": url}


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
    try:
        n = int(n or 0)
    except Exception:
        return "0"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def fmt_rate(num, denom) -> str:
    try:
        num = float(num or 0); denom = float(denom or 0)
    except Exception:
        return "N/A"
    if denom <= 0:
        return "N/A"
    return f"{num / denom * 100:.1f}%"


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


FAMILY_ICON = {"anthropic": "🅰", "openai": "🅾"}
FAMILY_LABEL = {"anthropic": "Anthropic", "openai": "OpenAI"}


def family_tag(family: Optional[str]) -> str:
    """统一的家族前缀标签，格式：🅰 Anthropic"""
    if not family:
        return ""
    return f"{FAMILY_ICON.get(family, '?')} {FAMILY_LABEL.get(family, family)}"


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

    def _handler(text: str, auto_delete_seconds: Optional[int] = None) -> None:
        full_text = text
        if auto_delete_seconds:
            full_text = (
                text + f"\n\n<i>⏱ 本消息将在 {int(auto_delete_seconds)} 秒后自动删除</i>"
            )
        for cid in list(_admin_ids):
            try:
                resp = send(cid, full_text)
                if auto_delete_seconds and resp and resp.get("ok"):
                    mid = (resp.get("result") or {}).get("message_id")
                    if mid:
                        _delayed_delete(cid, mid, int(auto_delete_seconds))
            except Exception:
                pass

    notifier.set_handler(_handler)
