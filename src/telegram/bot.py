"""Telegram Bot 长轮询主循环 + 路由分发。

启动流程：
  1. init(token, admin_ids) — 保存配置
  2. start() — 注册命令菜单 + 起守护线程跑 _poll_loop
  3. _poll_loop 消费 getUpdates；每条 update 传给 _handle_update

路由：
  - Message 文本 → /start、/menu、/keys 等命令；或状态机输入
  - CallbackQuery → 按 callback_data 前缀分派到菜单模块

所有菜单模块通过 `handle_callback(...)` 消费 callback；返回 True 即结束分发。
"""

from __future__ import annotations

import asyncio
import threading
import time
import traceback
from dataclasses import dataclass
from typing import Callable, Optional

from .. import startup_timing
from ..async_owned import await_owned
from . import menu_cache, states, ui
from .menus import (
    apikey_menu, channel_menu, help_menu, image_menu, load_balancing_menu,
    logs_menu, mapping_menu, mcp_menu, media_logs_menu, model_center_menu,
    oauth_account_models_menu, oauth_menu, proxy_menu,
    search_menu, stats_menu, status_alert_menu, status_menu, system_menu, translation_menu, update_menu,
    xai_imagine_menu,
)
from .menus import main as main_menu


_offset = 0
_thread: Optional[threading.Thread] = None
_running = False
_run_generation = 0
_stop_event = threading.Event()
_stop_event.set()
_lifecycle_lock = threading.Lock()
_lifecycle_condition = threading.Condition(_lifecycle_lock)
_starting = False
_start_done_event = threading.Event()
_start_done_event.set()
_stopping = False
_stop_waiters = 0


@dataclass
class _StopCycle:
    completion: threading.Event
    start_done: threading.Event
    thread: Optional[threading.Thread]
    error: Optional[BaseException] = None


@dataclass(frozen=True)
class _StopTicket:
    cycle: _StopCycle
    owner: bool


_stop_cycle: Optional[_StopCycle] = None
_management_approval_handler: Optional[Callable[[str, int, bool], str]] = None


def configure_management_approval_handler(
    handler: Optional[Callable[[str, int, bool], str]],
) -> None:
    """Install the isolated ``mauth:`` callback bridge owned by composition."""
    global _management_approval_handler
    _management_approval_handler = handler


def send_management_approval(admin_ids: tuple[int, ...], notification) -> bool:
    """Render the only new TG surface without exposing browser credentials."""
    device = notification.device_summary or "未提供"
    text = (
        "🔐 <b>Management 登录批准</b>\n\n"
        f"客户端：<code>{ui.escape_html(notification.client_name)}</code>\n"
        f"来源：<code>{ui.escape_html(notification.source_address)}</code>\n"
        f"设备：<code>{ui.escape_html(device)}</code>\n"
        f"请求时间：<code>{notification.requested_at.isoformat()}</code>\n"
        f"过期时间：<code>{notification.expires_at.isoformat()}</code>"
    )
    keyboard = ui.inline_kb(
        [[
            ui.btn("✅ 批准", notification.approve_callback),
            ui.btn("❌ 拒绝", notification.deny_callback),
        ]]
    )
    sent_all = True
    for admin_id in admin_ids:
        result = ui.send(admin_id, text, reply_markup=keyboard)
        if not isinstance(result, dict) or not result.get("ok"):
            sent_all = False
    return sent_all


def _summarize_text(text: str) -> str:
    """日志里只保留安全摘要，避免把 token / JSON / API Key 打进 stdout。"""
    if not text:
        return "empty"
    if text.startswith("/"):
        return f"command={text.split(None, 1)[0]}"
    return f"text_len={len(text)}"


def _summarize_state(state: Optional[dict]) -> str:
    """状态机日志只输出 action 与 data 的键名，不输出敏感值。"""
    if not isinstance(state, dict):
        return repr(state)
    action = state.get("action")
    data = state.get("data")
    if not isinstance(data, dict):
        return f"action={action!r}"
    keys = sorted(str(k) for k in data.keys())
    shown = ", ".join(keys[:8])
    if len(keys) > 8:
        shown += ", ..."
    return f"action={action!r}, data_keys=[{shown}]"


# ─── 生命周期 ─────────────────────────────────────────────────────

def init(bot_token: str, admin_ids: list[int]) -> None:
    ui.configure(bot_token, admin_ids)


def is_configured() -> bool:
    return bool(ui.get_token())


def _startup_api_call(
    operation: str,
    call: Callable[[], object],
) -> tuple[bool, Optional[dict]]:
    """Run one required Bot API initialization call without logging credentials."""
    started_ns = startup_timing.now_ns()
    phase = "telegram.api." + operation.replace("(", "-").replace(")", "").lower()
    try:
        result = call()
    except Exception as exc:
        startup_timing.log(
            phase,
            started_ns=started_ns,
            status="error",
            error=type(exc).__name__,
        )
        print(
            f"[tg] bot startup failed ({operation}): {type(exc).__name__}. "
            "请检查 telegram.botToken 和服务器到 api.telegram.org 的网络后重启。"
        )
        return False, None
    if isinstance(result, dict) and result.get("ok") is True:
        startup_timing.log(phase, started_ns=started_ns)
        return True, result
    code = result.get("error_code") if isinstance(result, dict) else None
    if code in {401, 404}:
        reason = f"Bot Token 无效或已撤销（Telegram {code}）"
    elif code is not None:
        reason = f"Telegram API 拒绝请求（错误码 {code}）"
    else:
        reason = "未收到 Telegram API 的成功响应"
    startup_timing.log(
        phase,
        started_ns=started_ns,
        status="rejected",
        error_code=code,
    )
    print(
        f"[tg] bot startup failed ({operation}): {reason}。"
        "请检查 telegram.botToken 和服务器到 api.telegram.org 的网络后重启。"
    )
    return False, result if isinstance(result, dict) else None


def _close_failed_start_session() -> None:
    try:
        ui.close_session()
    except Exception as exc:
        print(f"[tg] failed-start session close failed: {type(exc).__name__}")


def start() -> bool:
    global _thread, _running, _run_generation, _stop_event
    global _starting, _start_done_event
    start_started_ns = startup_timing.now_ns()
    if not is_configured():
        startup_timing.log(
            "telegram.start-total", started_ns=start_started_ns, status="skipped",
        )
        print("[tg] not configured (empty token), skipping start")
        return False
    with _lifecycle_lock:
        if _running:
            startup_timing.log(
                "telegram.start-total", started_ns=start_started_ns,
                status="already-running",
            )
            return True
        if _starting or _stopping:
            startup_timing.log(
                "telegram.start-total", started_ns=start_started_ns, status="busy",
            )
            return False
        _starting = True
        start_done = threading.Event()
        _start_done_event = start_done
        _run_generation += 1
        generation = _run_generation
        stop_event = threading.Event()
        _stop_event = stop_event
        _running = True
        _thread = None
    launched = False
    try:
        if not _activate_start_generation(generation, stop_event):
            return False

        # Startup calls can be slow. They run without the lifecycle lock, while
        # stop can invalidate this generation and wait for their leases. Every
        # required response must be an explicit Bot API success before readiness.
        if not _drop_pending_updates():
            _close_failed_start_session()
            return False
        if not _poll_generation_active(generation, stop_event):
            return False

        # setMyCommands replaces the complete command list for the same scope;
        # deleting it first is a redundant Bot API round-trip.
        commands_set, _ = _startup_api_call("setMyCommands", lambda: ui.set_my_commands([
            {"command": "start",    "description": "打开管理面板"},
            {"command": "menu",     "description": "打开管理面板"},
            {"command": "stats",    "description": "统计汇总"},
            {"command": "logs",     "description": "最近日志"},
            {"command": "channels", "description": "渠道管理"},
            {"command": "oauth",    "description": "管理 OAuth 账户"},
            {"command": "keys",     "description": "管理 API Key"},
            {"command": "models",   "description": "模型中心"},
            {"command": "mapping",  "description": "模型中心（兼容命令）"},
            {"command": "loadbalancing", "description": "负载均衡"},
            {"command": "proxy",    "description": "代理管理 / 路由规则"},
            {"command": "settings", "description": "系统设置"},
            {"command": "help",     "description": "帮助"},
        ]))
        if not commands_set:
            _close_failed_start_session()
            return False
        if not _poll_generation_active(generation, stop_event):
            return False

        ui.install_notify_handler()
        with _lifecycle_lock:
            if not _poll_generation_active_locked(generation, stop_event):
                return False
            # Starting the scheduler and publishing/starting the poll owner are a
            # short atomic phase relative to stop; no network I/O occurs here.
            menu_cache.start()
            poll_thread = threading.Thread(
                target=lambda: _poll_loop(generation, stop_event),
                daemon=True,
                name="tg-bot-poll",
            )
            _thread = poll_thread
            poll_thread.start()
            launched = True
        print("[tg] bot started (polling ready)")
        return True
    finally:
        with _lifecycle_condition:
            _starting = False
            if not launched and generation == _run_generation:
                _running = False
                stop_event.set()
            start_done.set()
            _lifecycle_condition.notify_all()
        startup_timing.log(
            "telegram.start-total",
            started_ns=start_started_ns,
            status="ready" if launched else "failed",
        )


def _activate_start_generation(
    generation: int,
    stop_event: threading.Event,
) -> bool:
    """Atomically validate this start generation before activating Telegram I/O."""
    with _lifecycle_lock:
        if not _poll_generation_active_locked(generation, stop_event):
            return False
        # No network work is performed here. Taking the UI lock under the lifecycle
        # lock makes activation linearizable with _request_stop's generation change.
        ui.activate_session()
        return True


def _request_stop() -> _StopTicket:
    global _running, _run_generation, _stopping
    global _stop_cycle, _stop_waiters
    with _lifecycle_condition:
        if _stopping:
            assert _stop_cycle is not None
            _stop_waiters += 1
            return _StopTicket(_stop_cycle, False)

        _stopping = True
        _running = False
        _run_generation += 1
        _stop_event.set()
        cycle = _StopCycle(
            completion=threading.Event(),
            start_done=_start_done_event,
            thread=_thread,
        )
        _stop_cycle = cycle
        _stop_waiters = 1

    # New Telegram operations are rejected immediately. Existing leases are not
    # closed; they drain naturally before their retired clients are closed.
    try:
        ui.close_session()
    except BaseException as exc:
        cycle.error = exc
    return _StopTicket(cycle, True)


def _leave_stop_cycle(cycle: _StopCycle) -> None:
    global _stopping, _stop_cycle, _stop_waiters
    with _lifecycle_condition:
        _stop_waiters -= 1
        if _stop_waiters == 0 and _stop_cycle is cycle:
            _stop_cycle = None
            _stopping = False
            _lifecycle_condition.notify_all()


def _finish_stop(ticket: _StopTicket) -> None:
    cycle = ticket.cycle
    try:
        if ticket.owner:
            try:
                # The start owner may still be between publishing _starting and
                # activating UI. It must fully leave before shutdown can complete.
                cycle.start_done.wait()
                thread = cycle.thread
                if thread is not None and thread is not threading.current_thread():
                    join = getattr(thread, "join", None)
                    if callable(join):
                        join()
                ui.wait_session_idle()
                # Only the cycle owner closes the shared scheduler. Followers wait
                # for completion, so no late old stop can close a new generation.
                menu_cache.stop()
            except BaseException as exc:
                if cycle.error is None:
                    cycle.error = exc
            finally:
                cycle.completion.set()
        else:
            cycle.completion.wait()

        if cycle.error is not None:
            raise cycle.error
    finally:
        _leave_stop_cycle(cycle)


def stop() -> None:
    """Synchronously stop and drain Telegram work.

    Async owners must use ``await stop_async()`` so thread joins and the menu
    scheduler's final job cannot block their event loop.
    """
    _finish_stop(_request_stop())


async def stop_async() -> None:
    """Complete the owned stop before propagating caller cancellation."""
    ticket = _request_stop()
    await await_owned(asyncio.to_thread(_finish_stop, ticket))


def _drop_pending_updates() -> bool:
    """丢弃待处理 update；两个初始化调用都成功时才返回 True。

    实现：调 deleteWebhook(drop_pending_updates=True)。我们本来就没用
    webhook（用 polling），所以这条调用对功能无副作用。再用 offset=-1
    推进到队列尾；任一接口失败都不得继续宣告 polling ready。
    """
    global _offset
    deleted, _ = _startup_api_call(
        "deleteWebhook",
        lambda: ui.api("deleteWebhook", {"drop_pending_updates": True}),
    )
    if not deleted:
        return False
    fetched, result = _startup_api_call(
        "getUpdates(init)",
        lambda: ui.api("getUpdates", {"offset": -1, "limit": 1, "timeout": 0}),
    )
    if not fetched:
        return False
    updates = (result.get("result") or []) if result is not None else []
    if updates:
        _offset = updates[-1]["update_id"] + 1
    print(f"[tg] dropped pending updates, offset={_offset}")
    return True


# ─── 主循环 ───────────────────────────────────────────────────────

def _poll_generation_active_locked(
    generation: int,
    stop_event: threading.Event,
) -> bool:
    return (
        _running
        and not stop_event.is_set()
        and generation == _run_generation
        and stop_event is _stop_event
    )


def _poll_generation_active(
    generation: Optional[int],
    stop_event: Optional[threading.Event],
) -> bool:
    # Keep direct test invocation compatible; production threads always carry an
    # immutable generation/event pair.
    if generation is None or stop_event is None:
        return _running
    with _lifecycle_lock:
        return _poll_generation_active_locked(generation, stop_event)


def _poll_backoff(
    seconds: float,
    generation: Optional[int],
    stop_event: Optional[threading.Event],
) -> bool:
    if generation is None or stop_event is None:
        time.sleep(seconds)
    else:
        stop_event.wait(seconds)
    return _poll_generation_active(generation, stop_event)


def _poll_loop(
    generation: Optional[int] = None,
    stop_event: Optional[threading.Event] = None,
) -> None:
    global _offset
    fail_count = 0
    cleanup_counter = 0
    while _poll_generation_active(generation, stop_event):
        try:
            result = ui.api("getUpdates", {"offset": _offset, "timeout": 30})
            # A stop/restart may happen while long polling is blocked. Never act on
            # that old generation's response or failure after it returns.
            if not _poll_generation_active(generation, stop_event):
                return
            if not result or not result.get("ok"):
                fail_count += 1
                if fail_count >= 10 and fail_count % 10 == 0:
                    print(f"[tg] {fail_count} consecutive failures, rebuilding session")
                    ui.rebuild_session()
                if not _poll_backoff(min(5 * fail_count, 60), generation, stop_event):
                    return
                continue

            fail_count = 0
            for update in result.get("result", []):
                if not _poll_generation_active(generation, stop_event):
                    return
                _offset = update["update_id"] + 1
                try:
                    _handle_update(update)
                except Exception:
                    traceback.print_exc()
                    if not _poll_generation_active(generation, stop_event):
                        return
                    chat_id = _extract_chat_id(update)
                    if chat_id is not None:
                        try:
                            ui.send(chat_id, "❌ 内部错误，请稍后重试或联系管理员。")
                        except Exception:
                            pass

            if not _poll_generation_active(generation, stop_event):
                return
            cleanup_counter += 1
            if cleanup_counter >= 50:
                cleanup_counter = 0
                states.cleanup()
        except Exception:
            if not _poll_generation_active(generation, stop_event):
                return
            fail_count += 1
            if fail_count >= 10 and fail_count % 10 == 0:
                print(f"[tg] {fail_count} exceptions, rebuilding session")
                ui.rebuild_session()
            if not _poll_backoff(min(5 * fail_count, 60), generation, stop_event):
                return


# ─── 分发 ─────────────────────────────────────────────────────────

def _extract_chat_id(update: dict) -> Optional[int]:
    """从任意 update 中提取 chat_id（消息或回调）。失败返回 None。"""
    try:
        cb = update.get("callback_query")
        if cb:
            return cb["message"]["chat"]["id"]
        msg = update.get("message")
        if msg:
            return msg["chat"]["id"]
    except Exception:
        pass
    return None


def _handle_update(update: dict) -> None:
    # CallbackQuery
    cb = update.get("callback_query")
    if cb:
        _handle_callback(cb)
        return
    # Message
    msg = update.get("message")
    if msg:
        _handle_message(msg)


def _handle_callback(cb: dict) -> None:
    chat_id = cb["message"]["chat"]["id"]
    msg_id = cb["message"]["message_id"]
    cb_id = cb["id"]
    data = cb.get("data", "") or ""
    print(f"[tg] cb from {chat_id}: data={data!r}")    # DEBUG

    # Management login approvals are deliberately isolated from all legacy menu
    # dispatch and authorization.  The service validates callback_query.from.id
    # against the current non-empty configured admin allow-list.
    if data.startswith("mauth:"):
        parts = data.split(":", 2)
        actor_id = (cb.get("from") or {}).get("id")
        if len(parts) != 3 or parts[1] not in {"a", "d"} or actor_id is None:
            ui.answer_cb(cb_id, "无效的登录批准请求", show_alert=True)
            return
        if _management_approval_handler is None:
            ui.answer_cb(cb_id, "Management 登录暂不可用", show_alert=True)
            return
        try:
            result = _management_approval_handler(parts[2], int(actor_id), parts[1] == "a")
        except Exception:
            ui.answer_cb(cb_id, "登录批准失败或已失效", show_alert=True)
            return
        messages = {
            "approved": "✅ 已批准登录",
            "denied": "❌ 已拒绝登录",
            "expired": "登录批准已过期",
            "consumed": "登录批准已使用",
            "alreadyDecided": "登录批准已处理，不能重复决定",
        }
        ui.answer_cb(cb_id, messages.get(result, "登录批准状态未变更"), show_alert=True)
        return

    if not ui.is_admin(chat_id):
        ui.answer_cb(cb_id, "⛔ 无权限")
        return

    # Revoke only model-center input before any early navigation return.
    model_center_menu.before_callback(chat_id, data)
    search_menu.before_callback(chat_id, data)
    # 任意新 callback 都让该消息此前的后台统计更新失效，防止旧页面覆盖新菜单。
    menu_cache.begin_view(chat_id, msg_id)

    # 主菜单
    if data == "menu:main":
        main_menu.handle_back(chat_id, msg_id, cb_id)
        return

    # 状态总览
    if status_menu.handle_callback(chat_id, msg_id, cb_id, data):
        return

    # 帮助
    if help_menu.handle_callback(chat_id, msg_id, cb_id, data):
        return

    # 统一模型中心（必须先于旧模型页兼容 handler）
    if model_center_menu.handle_callback(chat_id, msg_id, cb_id, data):
        return

    # OAuth 管理菜单
    if oauth_menu.handle_callback(chat_id, msg_id, cb_id, data):
        return
    if oauth_account_models_menu.handle_callback(chat_id, msg_id, cb_id, data):
        return

    # GPT/Codex 图片生成设置
    if image_menu.handle_callback(chat_id, msg_id, cb_id, data):
        return

    # Grok Imagine 图片 / 视频设置
    if xai_imagine_menu.handle_callback(chat_id, msg_id, cb_id, data):
        return

    # 渠道管理菜单
    if channel_menu.handle_callback(chat_id, msg_id, cb_id, data):
        return

    # 统计菜单
    if stats_menu.handle_callback(chat_id, msg_id, cb_id, data):
        return

    # 负载均衡菜单
    if load_balancing_menu.handle_callback(chat_id, msg_id, cb_id, data):
        return

    # 普通请求日志 / 多媒体业务日志
    if media_logs_menu.handle_callback(chat_id, msg_id, cb_id, data):
        return
    if logs_menu.handle_callback(chat_id, msg_id, cb_id, data):
        return

    # 故障订阅菜单
    if status_alert_menu.handle_callback(chat_id, msg_id, cb_id, data):
        return

    # 版本更新菜单
    if update_menu.handle_callback(chat_id, msg_id, cb_id, data):
        return

    # 翻译层菜单
    if translation_menu.handle_callback(chat_id, msg_id, cb_id, data):
        return

    # 搜索工具与系统设置菜单
    if search_menu.handle_callback(chat_id, msg_id, cb_id, data):
        return
    if mcp_menu.handle_callback(chat_id, msg_id, cb_id, data):
        return
    if proxy_menu.handle_callback(chat_id, msg_id, cb_id, data):
        print(f"[tg] handled by proxy_menu ({data})")
        return
    if system_menu.handle_callback(chat_id, msg_id, cb_id, data):
        return

    # API Key 菜单
    if apikey_menu.handle_callback(chat_id, msg_id, cb_id, data):
        return

    # 模型映射菜单
    if mapping_menu.handle_callback(chat_id, msg_id, cb_id, data):
        return

    if data.startswith("odm:"):
        if str((states.get_state(chat_id) or {}).get("action") or "").startswith("odm_"):
            states.pop_state(chat_id)
        ui.answer_cb(cb_id, "OAuth 备用模型已退役，请到模型中心同步上游模型。")
        return

    # 未知 callback
    ui.answer_cb(cb_id, "未知操作")


def _handle_message(msg: dict) -> None:
    chat_id = msg["chat"]["id"]
    text = msg.get("text", "") or ""
    print(f"[tg] msg from {chat_id}: {_summarize_text(text)}")   # DEBUG

    if not ui.is_admin(chat_id):
        ui.send(
            chat_id,
            f"⛔ 无权限。你的 Chat ID: <code>{chat_id}</code>\n"
            "请联系管理员将此 ID 加入 <code>config.telegram.adminIds</code>",
        )
        return

    # Commands leave MC input; /cancel is consumed by its own editor.
    # Other menus retain their historical command/input semantics.
    model_center_menu.before_command(chat_id, text)
    search_menu.before_command(chat_id, text)
    # 状态机输入
    state = states.get_state(chat_id)
    print(f"[tg] state for {chat_id}: {_summarize_state(state)}")        # DEBUG
    if state:
        action = state.get("action", "")
        if msg.get("document") and oauth_menu.handle_document_state(chat_id, action, msg):
            print(f"[tg] handled document by oauth_menu (action={action})")
            return
        if model_center_menu.handle_text_state(chat_id, action, text):
            print(f"[tg] handled by model_center_menu (action={action})")
            return
        if apikey_menu.handle_text_state(chat_id, action, text):
            print(f"[tg] handled by apikey_menu (action={action})")  # DEBUG
            return
        if oauth_menu.handle_text_state(chat_id, action, text):
            print(f"[tg] handled by oauth_menu (action={action})")
            return
        if search_menu.handle_text_state(chat_id, action, text):
            return
        if image_menu.handle_text_state(chat_id, action, text):
            print(f"[tg] handled by image_menu (action={action})")
            return
        if xai_imagine_menu.handle_text_state(chat_id, action, text):
            print(f"[tg] handled by xai_imagine_menu (action={action})")
            return
        if channel_menu.handle_text_state(chat_id, action, text):
            print(f"[tg] handled by channel_menu (action={action})")
            return
        if load_balancing_menu.handle_text_state(chat_id, action, text):
            print(f"[tg] handled by load_balancing_menu (action={action})")
            return
        if logs_menu.handle_text_state(chat_id, action, text):
            print(f"[tg] handled by logs_menu (action={action})")
            return
        if status_alert_menu.handle_text_state(chat_id, action, text):
            return
        if update_menu.handle_text_state(chat_id, action, text):
            return
        if proxy_menu.handle_text_state(chat_id, action, text):
            print(f"[tg] handled by proxy_menu (action={action})")
            return
        if translation_menu.handle_text_state(chat_id, action, text):
            print(f"[tg] handled by translation_menu (action={action})")
            return
        if system_menu.handle_text_state(chat_id, action, text):
            print(f"[tg] handled by system_menu (action={action})")
            return
        if mapping_menu.handle_text_state(chat_id, action, text):
            print(f"[tg] handled by mapping_menu (action={action})")
            return
        if action.startswith("odm_"):
            states.pop_state(chat_id)
            ui.send(chat_id, "OAuth 备用模型已退役，请到模型中心同步上游模型。")
            return
        print(f"[tg] state action={action!r} not consumed by any menu")  # DEBUG
        # 未来其他菜单也在此分派

    # 命令：直接渲染对应菜单（用 send_new 而非 edit）
    if text.startswith("/start"):
        main_menu.on_start_command(chat_id); return
    if text.startswith("/menu"):
        main_menu.on_menu_command(chat_id); return
    if text.startswith("/status"):
        ui.send(chat_id, "状态总览已从菜单移除。发送 /menu 打开管理面板。"); return
    if text.startswith("/stats"):
        stats_menu.send_new(chat_id); return
    if text.startswith("/logs"):
        logs_menu.send_new(chat_id); return
    if text.startswith("/channels"):
        channel_menu.send_new(chat_id); return
    # Narrow legacy command must precede the broad /oauth prefix.
    if text.startswith("/oauth_defaults"):
        ui.send(chat_id, "OAuth 备用模型已退役，请到模型中心同步上游模型。"); return
    if text.startswith("/oauth"):
        oauth_menu.send_new(chat_id); return
    if text.startswith("/keys"):
        apikey_menu.send_new(chat_id); return
    if text.startswith("/settings"):
        system_menu.send_new(chat_id); return
    if text.startswith("/models") or text.startswith("/mapping"):
        model_center_menu.send_new(chat_id); return
    if text.startswith("/loadbalancing"):
        load_balancing_menu.send_new(chat_id); return
    if text.startswith("/proxy") or text.startswith("/proxies"):
        ui.send(chat_id, "🔀 代理管理", reply_markup=ui.inline_kb([[ui.btn("打开代理管理", "px:show")]])); return
    if text.startswith("/help"):
        help_menu.send_new(chat_id); return

    # 其他文本：提示用 /menu
    ui.send(chat_id, "未识别的输入。发送 /menu 打开管理面板。")
