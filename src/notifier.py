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

# 异步发送队列：notify() 入队 (text, auto_delete_seconds)，worker 出队 → handler
_queue: "queue.Queue[tuple[str, Optional[int]]]" = queue.Queue(maxsize=1000)
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


def _worker_loop() -> None:
    while True:
        try:
            item = _queue.get()
        except Exception:
            continue
        # 兼容旧入队格式（直接 str）
        if isinstance(item, tuple):
            text, auto_delete = item
        else:
            text, auto_delete = item, None
        try:
            with _lock:
                fn = _handler
            if fn is None:
                print(f"[notify] {text}")
            else:
                try:
                    # 优先调新签名（带 auto_delete_seconds）；老 handler 回退到单参
                    try:
                        fn(text, auto_delete_seconds=auto_delete)
                    except TypeError:
                        fn(text)
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


def notify(text: str, auto_delete_seconds: Optional[int] = None) -> None:
    """发送一条通知消息。**不阻塞**：把 text 推入队列，由 worker 线程异步发出。

    auto_delete_seconds: 若设置，handler 会在发送后 N 秒删除该消息（仅 TG handler 支持）。
    队列满（极端情况）→ 丢弃并打印警告，避免 notify 反过来阻塞调用方。
    """
    _ensure_worker()
    try:
        _queue.put_nowait((text, auto_delete_seconds))
    except queue.Full:
        print(f"[notify] queue full, dropping message: {text[:80]}")


def notify_event(event_key: str, text: str,
                 auto_delete_seconds: Optional[int] = None) -> None:
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
    notify(text, auto_delete_seconds=auto_delete_seconds)


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
                                 *, cooldown_seconds: int = _THROTTLE_DEFAULT_SEC) -> None:
    """节流版事件通知（async 版本）。

    `event_key` 决定 notify_event 的开关；`alert_key` 决定节流桶
    （同 alert_key 在 cooldown_seconds 内只发一次，哪怕 text 不同）。
    """
    if not _throttle_should_emit(alert_key, cooldown_seconds):
        return
    notify_event(event_key, text)


def throttled_notify_event_sync(event_key: str, alert_key: str, text: str,
                                *, cooldown_seconds: int = _THROTTLE_DEFAULT_SEC) -> None:
    """同 throttled_notify_event，但可从同步上下文调用。

    用在那些没法 await 的场景（如 sync 翻译器收尾、sync 的 Store save 回调）。
    """
    if not _throttle_should_emit(alert_key, cooldown_seconds):
        return
    notify_event(event_key, text)


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
