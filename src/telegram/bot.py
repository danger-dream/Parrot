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

import threading
import time
import traceback
from typing import Optional

from . import states, ui
from .menus import (
    apikey_menu, channel_menu, help_menu, logs_menu, mapping_menu,
    oauth_menu, stats_menu, status_menu, system_menu,
)
from .menus import main as main_menu


_offset = 0
_thread: Optional[threading.Thread] = None
_running = False


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


def start() -> None:
    global _thread, _running
    if not is_configured():
        print("[tg] not configured (empty token), skipping start")
        return
    if _running:
        return

    # 启动时丢弃 TG 服务端积压的 pending updates，避免历史消息被重新回放
    # （否则 bot 重启后，用户之前发的所有 /start 会全部"重新执行"一遍）。
    # deleteWebhook + drop_pending_updates=True 对 polling 模式也有效：
    # 它会清空 update 队列；下一次 getUpdates 只能拿到本次启动后到达的消息。
    _drop_pending_updates()

    # 同步命令菜单：必须先 delete 再 set，且必须串行（同步 httpx 自然保证）。
    # 否则可能出现两种坏情况：
    #   1. 旧菜单（之前部署/BotFather 手动设过的）残留
    #   2. 并发触发时 delete 晚于 set 到达，反而把新菜单清空
    ui.delete_my_commands()
    ui.set_my_commands([
        {"command": "start",    "description": "打开管理面板"},
        {"command": "menu",     "description": "打开管理面板"},
        {"command": "status",   "description": "状态总览"},
        {"command": "stats",    "description": "统计汇总"},
        {"command": "logs",     "description": "最近日志"},
        {"command": "channels", "description": "渠道管理"},
        {"command": "oauth",    "description": "管理 OAuth 账户"},
        {"command": "keys",     "description": "管理 API Key"},
        {"command": "settings", "description": "系统设置"},
        {"command": "mapping",  "description": "模型映射 / 默认模型"},
        {"command": "help",     "description": "帮助"},
    ])

    # 安装 notifier 钩子（把服务事件转发给 admin）
    ui.install_notify_handler()

    _running = True
    _thread = threading.Thread(target=_poll_loop, daemon=True, name="tg-bot-poll")
    _thread.start()
    print("[tg] bot started (polling)")


def stop() -> None:
    global _running
    _running = False
    ui.close_session()


def _drop_pending_updates() -> None:
    """丢弃 TG 服务端的所有未处理 update。

    实现：调 deleteWebhook(drop_pending_updates=True)。
    我们本来就没用 webhook（用 polling），所以这条调用对功能无副作用，
    它的语义就是"清空 update 队列"。同时把 _offset 标记为 1，
    避免下一轮 polling 重新尝试 offset=0。
    """
    global _offset
    try:
        ui.api("deleteWebhook", {"drop_pending_updates": True})
        # 再用 offset=-1 取一次最新 update_id，把 _offset 推进到队列尾
        # （deleteWebhook 已清空，这里多数返回空；保险起见兜底处理一次）
        result = ui.api("getUpdates", {"offset": -1, "limit": 1, "timeout": 0})
        if result and result.get("ok"):
            updates = result.get("result") or []
            if updates:
                _offset = updates[-1]["update_id"] + 1
        print(f"[tg] dropped pending updates, offset={_offset}")
    except Exception as exc:
        print(f"[tg] drop pending updates failed: {exc}")


# ─── 主循环 ───────────────────────────────────────────────────────

def _poll_loop() -> None:
    global _offset
    fail_count = 0
    cleanup_counter = 0
    while _running:
        try:
            result = ui.api("getUpdates", {"offset": _offset, "timeout": 30})
            if not result or not result.get("ok"):
                fail_count += 1
                if fail_count >= 10 and fail_count % 10 == 0:
                    print(f"[tg] {fail_count} consecutive failures, rebuilding session")
                    ui.rebuild_session()
                time.sleep(min(5 * fail_count, 60))
                continue

            fail_count = 0
            for update in result.get("result", []):
                _offset = update["update_id"] + 1
                try:
                    _handle_update(update)
                except Exception:
                    traceback.print_exc()
                    # 兜底：给用户回一条消息，避免"无响应"假象
                    chat_id = _extract_chat_id(update)
                    if chat_id is not None:
                        try:
                            ui.send(chat_id, "❌ 内部错误，请稍后重试或联系管理员。")
                        except Exception:
                            pass

            cleanup_counter += 1
            if cleanup_counter >= 50:
                cleanup_counter = 0
                states.cleanup()
        except Exception:
            fail_count += 1
            if fail_count >= 10 and fail_count % 10 == 0:
                print(f"[tg] {fail_count} exceptions, rebuilding session")
                ui.rebuild_session()
            time.sleep(min(5 * fail_count, 60))


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

    if not ui.is_admin(chat_id):
        ui.answer_cb(cb_id, "⛔ 无权限")
        return

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

    # OAuth 管理菜单
    if oauth_menu.handle_callback(chat_id, msg_id, cb_id, data):
        return

    # 渠道管理菜单
    if channel_menu.handle_callback(chat_id, msg_id, cb_id, data):
        return

    # 统计菜单
    if stats_menu.handle_callback(chat_id, msg_id, cb_id, data):
        return

    # 日志菜单
    if logs_menu.handle_callback(chat_id, msg_id, cb_id, data):
        return

    # 系统设置菜单
    if system_menu.handle_callback(chat_id, msg_id, cb_id, data):
        return

    # API Key 菜单
    if apikey_menu.handle_callback(chat_id, msg_id, cb_id, data):
        return

    # 模型映射菜单
    if mapping_menu.handle_callback(chat_id, msg_id, cb_id, data):
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

    # 状态机输入
    state = states.get_state(chat_id)
    print(f"[tg] state for {chat_id}: {_summarize_state(state)}")        # DEBUG
    if state:
        action = state.get("action", "")
        if apikey_menu.handle_text_state(chat_id, action, text):
            print(f"[tg] handled by apikey_menu (action={action})")  # DEBUG
            return
        if oauth_menu.handle_text_state(chat_id, action, text):
            print(f"[tg] handled by oauth_menu (action={action})")
            return
        if channel_menu.handle_text_state(chat_id, action, text):
            print(f"[tg] handled by channel_menu (action={action})")
            return
        if system_menu.handle_text_state(chat_id, action, text):
            print(f"[tg] handled by system_menu (action={action})")
            return
        if mapping_menu.handle_text_state(chat_id, action, text):
            print(f"[tg] handled by mapping_menu (action={action})")
            return
        print(f"[tg] state action={action!r} not consumed by any menu")  # DEBUG
        # 未来其他菜单也在此分派

    # 命令：直接渲染对应菜单（用 send_new 而非 edit）
    if text.startswith("/start"):
        main_menu.on_start_command(chat_id); return
    if text.startswith("/menu"):
        main_menu.on_menu_command(chat_id); return
    if text.startswith("/status"):
        status_menu.send_new(chat_id); return
    if text.startswith("/stats"):
        stats_menu.send_new(chat_id); return
    if text.startswith("/logs"):
        logs_menu.send_new(chat_id); return
    if text.startswith("/channels"):
        channel_menu.send_new(chat_id); return
    if text.startswith("/oauth"):
        oauth_menu.send_new(chat_id); return
    if text.startswith("/keys"):
        apikey_menu.send_new(chat_id); return
    if text.startswith("/settings"):
        system_menu.send_new(chat_id); return
    if text.startswith("/mapping"):
        mapping_menu.send_new(chat_id); return
    if text.startswith("/help"):
        help_menu.send_new(chat_id); return

    # 其他文本：提示用 /menu
    ui.send(chat_id, "未识别的输入。发送 /menu 打开管理面板。")
