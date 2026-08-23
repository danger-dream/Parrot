"""管理员通知抽象。

提供统一的"发给管理员"接口。默认实现只 print 到 stdout，
M6（TG Bot）实现后由 tgbot 注册真实 handler 替换。

**关键设计：notify() 永远不阻塞调用方**。
- handler 通常会做同步 HTTP（TG Bot API），最长可达 30-50s
- 调用方（async handler / 后台 loop）若被阻塞会卡住整个 event loop
- 因此 notify() 把 (text) 推入队列，由独立 daemon 线程消费 → handler

使用：
  - 服务内部任何需要告知运维的事件都调 `notifier.notify(text)`
  - 同步 / 异步上下文都安全
"""

from __future__ import annotations

import queue
import threading
from typing import Callable, Optional

_lock = threading.Lock()
_handler: Optional[Callable] = None

# 异步发送队列：notify() 入队 (text, auto_delete_seconds, reply_markup, meta)，worker 出队 → handler
_queue: "queue.Queue[tuple]" = queue.Queue(maxsize=1000)
_worker_thread: Optional[threading.Thread] = None
_worker_started = False


def escape_html(s) -> str:
    """对用户提供的字符串做 HTML 字符 escape，供通知文案中嵌入用户内容前调用。

    通知 handler 不会再对整段文本做 escape（否则 <b>/<code> 等标签也会被转义），
    所以**调用方负责** escape 任何来自用户/外部的字符串。
    """
    return (str(s).replace("&", "&amp;")
                  .replace("<", "&lt;")
                  .replace(">", "&gt;"))


_PROVIDER_BTN_EMOJI = {"claude": "🅰️", "anthropic": "🅰️", "openai": "🅾️", "xai": "𝕏", "cursor": "🖱️", "antigravity": "✨"}
_PROVIDER_CUSTOM_EMOJI = {
    "claude": "5872779796257184592",
    "anthropic": "5872779796257184592",
    "openai": "5861557411784957025",
    "xai": "5819115571463068721",
    "cursor": "6062261319426390107",
    "antigravity": "6077644693984779782",
}
_PROVIDER_CUSTOM_FALLBACK = {"claude": "🤖", "anthropic": "🤖", "openai": "🤖", "xai": "🐦", "cursor": "🖱️", "antigravity": "✨"}
_PROVIDER_LABEL = {"claude": "Claude", "anthropic": "Claude", "openai": "OpenAI", "xai": "Grok", "cursor": "Cursor", "antigravity": "Antigravity"}


def _provider_key(provider: str | None) -> str:
    p = str(provider or "").strip().lower()
    return "claude" if p == "anthropic" else p


def _telegram_ui_provider_table(name: str) -> dict:
    try:
        from . import config
        cfg = config.get().get("telegramUi") or {}
        table = cfg.get(name) or {}
        return table if isinstance(table, dict) else {}
    except Exception:
        return {}


def provider_btn_emoji(provider: str | None) -> str:
    """Plain-text/provider button emoji for notifications that cannot use rich entities."""
    p = _provider_key(provider)
    table = _telegram_ui_provider_table("providerBtnEmoji")
    return str(table.get(p) or _PROVIDER_BTN_EMOJI.get(p) or "✉")


def provider_custom_emoji_html(provider: str | None) -> str:
    """HTML custom emoji for notification/message body provider badges."""
    p = _provider_key(provider)
    table = _telegram_ui_provider_table("providerCustomEmoji")
    custom_id = str(table.get(p) or _PROVIDER_CUSTOM_EMOJI.get(p) or "").strip()
    if custom_id:
        fallback = _PROVIDER_CUSTOM_FALLBACK.get(p) or provider_btn_emoji(p) or "•"
        return f'<tg-emoji emoji-id="{escape_html(custom_id)}">{escape_html(fallback)}</tg-emoji>'
    return escape_html(provider_btn_emoji(p))


def provider_label(provider: str | None) -> str:
    p = _provider_key(provider)
    return _PROVIDER_LABEL.get(p, p or "OAuth")


def provider_tag(provider: str | None, *, rich: bool = True) -> str:
    p = _provider_key(provider)
    icon = provider_custom_emoji_html(p) if rich else provider_btn_emoji(p)
    label = provider_label(p)
    return f"{icon} {escape_html(label) if rich else label}" if label else icon


def _worker_loop() -> None:
    while True:
        try:
            item = _queue.get()
        except Exception:
            continue
        # 兼容多种入队格式：str / (text, auto_delete) / (text, auto_delete, reply_markup, meta)
        text = None
        auto_delete = None
        reply_markup = None
        meta = None
        if isinstance(item, tuple):
            if len(item) >= 4:
                text, auto_delete, reply_markup, meta = item[0], item[1], item[2], item[3]
            elif len(item) == 3:
                text, auto_delete, reply_markup = item
            else:
                text, auto_delete = item
        else:
            text = item
        try:
            with _lock:
                fn = _handler
            if fn is None:
                print(f"[notify] {text}")
            else:
                try:
                    # 按 handler 实际签名传参（用内省判断支持哪些 kwargs），
                    # 避免用 TypeError 兜底时把 handler 内部的 TypeError 误判为签名不匹配。
                    kwargs = {}
                    try:
                        import inspect
                        params = inspect.signature(fn).parameters
                        accepts_any_kw = any(
                            p.kind == inspect.Parameter.VAR_KEYWORD
                            for p in params.values()
                        )
                        if accepts_any_kw or "auto_delete_seconds" in params:
                            kwargs["auto_delete_seconds"] = auto_delete
                        if accepts_any_kw or "reply_markup" in params:
                            kwargs["reply_markup"] = reply_markup
                        if accepts_any_kw or "meta" in params:
                            kwargs["meta"] = meta
                    except (ValueError, TypeError):
                        # 拿不到签名（极少数 callable）→ 退化为只传文本
                        kwargs = {}
                    fn(text, **kwargs)
                except Exception as exc:
                    print(f"[notify] handler failed: {exc}")
                    print(f"[notify] (original message): {text}")
        finally:
            _queue.task_done()


def _ensure_worker() -> None:
    global _worker_thread, _worker_started
    if _worker_started:
        return
    with _lock:
        if _worker_started:
            return
        _worker_thread = threading.Thread(
            target=_worker_loop, daemon=True, name="notifier-worker",
        )
        _worker_thread.start()
        _worker_started = True


def set_handler(fn: Optional[Callable[[str], None]]) -> None:
    """由 tgbot 在启动时注册实际的通知函数。fn 可以是阻塞的——它在 worker 线程跑。"""
    global _handler
    with _lock:
        _handler = fn
    _ensure_worker()


def notify(text: str, auto_delete_seconds: Optional[int] = None,
           reply_markup: Optional[dict] = None, meta: Optional[dict] = None) -> None:
    """发送一条通知消息。**不阻塞**：把 text 推入队列，由 worker 线程异步发出。

    auto_delete_seconds: 若设置，handler 会在发送后 N 秒删除该消息（仅 TG handler 支持）。
    reply_markup: 可选 inline 键盘（仅 TG handler 支持），用于带按钮的交互通知。
    meta: 可选元信息，handler 可据此回填 message_id（如自更新流程需记住通知消息）。
    队列满（极端情况）→ 丢弃并打印警告，避免 notify 反过来阻塞调用方。
    """
    _ensure_worker()
    try:
        _queue.put_nowait((text, auto_delete_seconds, reply_markup, meta))
    except queue.Full:
        print(f"[notify] queue full, dropping message: {text[:80]}")


def notify_event(event_key: str, text: str,
                 auto_delete_seconds: Optional[int] = None,
                 reply_markup: Optional[dict] = None,
                 meta: Optional[dict] = None) -> None:
    """事件级通知：受 config.notifications.enabled 总开关 + events[event_key] 单独开关控制。

    任一关闭则跳过（仍打印到 stdout，便于排查）。配置不存在时按"开"处理（向前兼容）。
    """
    try:
        from . import config
        cfg = config.get()
        notif = cfg.get("notifications") or {}
        if not notif.get("enabled", True):
            print(f"[notify:{event_key}:disabled] {text}")
            return
        events = notif.get("events") or {}
        if event_key in events and not events[event_key]:
            print(f"[notify:{event_key}:off] {text}")
            return
    except Exception as exc:
        print(f"[notify_event] config check failed ({exc}), sending anyway")
    notify(text, auto_delete_seconds=auto_delete_seconds,
           reply_markup=reply_markup, meta=meta)


# ─── 异步节流通知（同 event_key N 秒内仅触发一次） ─────────────────
#
# 用于像 "no_channels:<model>" 这种"频繁重复但不需要每次都通知"的场景。
# 与 notify_event 正交：先节流判断，再走 notify_event。

import asyncio as _asyncio
import time as _t

# 节流桶由 sync 与 async 两个入口共享，用普通 threading.Lock 保证两边安全。
_throttle_last_sent: dict[str, float] = {}
_throttle_lock_sync = threading.Lock()
_throttle_lock = _asyncio.Lock()   # 兼容旧调用（async）
_THROTTLE_DEFAULT_SEC = 300


def _throttle_should_emit(alert_key: str, cooldown_seconds: int) -> bool:
    """线程安全：判断是否已过冷却；若是则更新时间戳并返回 True。"""
    with _throttle_lock_sync:
        now = _t.time()
        last = _throttle_last_sent.get(alert_key, 0)
        if now - last < cooldown_seconds:
            return False
        _throttle_last_sent[alert_key] = now
        return True


async def throttled_notify_event(event_key: str, alert_key: str, text: str,
                                 *, cooldown_seconds: int = _THROTTLE_DEFAULT_SEC,
                                 reply_markup: Optional[dict] = None) -> None:
    """节流版事件通知（async 版本）。

    `event_key` 决定 notify_event 的开关；`alert_key` 决定节流桶
    （同 alert_key 在 cooldown_seconds 内只发一次，哪怕 text 不同）。
    """
    if not _throttle_should_emit(alert_key, cooldown_seconds):
        return
    notify_event(event_key, text, reply_markup=reply_markup)


def throttled_notify_event_sync(event_key: str, alert_key: str, text: str,
                                *, cooldown_seconds: int = _THROTTLE_DEFAULT_SEC,
                                reply_markup: Optional[dict] = None) -> bool:
    """同 throttled_notify_event，但可从同步上下文调用。

    用在那些没法 await 的场景（如 sync 翻译器收尾、sync 的 Store save 回调）。
    返回是否实际进入发送队列，供同步调用方避免重复日志。
    """
    if not _throttle_should_emit(alert_key, cooldown_seconds):
        return False
    notify_event(event_key, text, reply_markup=reply_markup)
    return True


def wait_drain(timeout: float = 5.0) -> bool:
    """等待 queue 中所有消息被 worker 处理完毕。仅供测试 / 关停场景使用。

    返回 True = 全部处理完；False = 超时。
    """
    import time as _t
    deadline = _t.time() + timeout
    while _t.time() < deadline:
        if _queue.unfinished_tasks == 0:
            return True
        _t.sleep(0.02)
    return False
