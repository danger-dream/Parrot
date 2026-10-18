"""Telegram add-page selection, separate from the one-shot text input state."""
from __future__ import annotations

from dataclasses import dataclass
from functools import wraps
import secrets
import threading
import time

from ...oauth import add_context
from .. import states, ui

_TTL = 600
_lock = threading.RLock()


@dataclass
class Session:
    nonce: str
    touched: float
    target: str | None = None
    attempt: add_context.AddAttempt | None = None


_sessions: dict[int, Session] = {}
_expired: set[int] = set()  # non-secret tombstones; old buttons must not silently inherit


def cleanup():
    attempts = []
    with _lock:
        for chat_id, session in list(_sessions.items()):
            if time.time() - session.touched >= _TTL:
                if session.attempt:
                    attempts.append(session.attempt)
                del _sessions[chat_id]
                _expired.add(chat_id)
    for attempt in attempts:
        attempt.cancel()


def get(chat_id, *, create=True):
    cleanup()
    with _lock:
        session = _sessions.get(chat_id)
        if session is None and create:
            session = Session(secrets.token_hex(5), time.time())
            _sessions[chat_id] = session
            _expired.discard(chat_id)
        if session is not None and create:
            session.touched = time.time()
        return session


def cancel_attempt(chat_id):
    with _lock:
        session = _sessions.get(chat_id)
        attempt = session.attempt if session else None
        if session:
            session.attempt = None
    if attempt:
        attempt.cancel()


def discard(chat_id):
    cancel_attempt(chat_id)
    with _lock:
        _sessions.pop(chat_id, None)
        _expired.discard(chat_id)


def clear_all():
    with _lock:
        attempts = [s.attempt for s in _sessions.values() if s.attempt]
        _sessions.clear()
        _expired.clear()
    for attempt in attempts:
        attempt.cancel()


def clear_input(chat_id):
    from . import workbuddy_oauth_menu, zhipu_oauth_menu
    cancel_attempt(chat_id)
    workbuddy_oauth_menu.discard_state(chat_id)
    zhipu_oauth_menu.discard_state(chat_id)
    states.pop_state(chat_id)


def begin(chat_id, provider):
    clear_input(chat_id)
    session = get(chat_id)
    with _lock:
        attempt = add_context.AddAttempt(target=session.target, provider=provider)
        session.attempt = attempt
    return attempt


def start(provider):
    """Bind before even discovery/device initialization; snapshot once per login."""
    def decorate(fn):
        @wraps(fn)
        def wrapped(chat_id, *args, **kwargs):
            if kwargs.get("relogin"):
                # Relogin never reads the add-page selection.
                with add_context.bind(None):
                    return fn(chat_id, *args, **kwargs)
            cleanup()
            if chat_id in _expired:
                ui.send_result(chat_id, "新增会话已过期，请重新打开新增页选择代理。",
                               back_label="◀ 返回新增", back_callback="oa:add")
                return
            attempt = add_context.current() or begin(chat_id, provider)
            with add_context.bind(attempt):
                return fn(chat_id, *args, **kwargs)
        return wrapped
    return decorate


def resume(fn):
    @wraps(fn)
    def wrapped(chat_id, *args, **kwargs):
        state = states.get_state(chat_id)
        data = (state or {}).get("data") or {}
        ref = None if data.get("relogin") else data.get("_oauth_add_attempt")
        with _lock:
            session = _sessions.get(chat_id)
            attempt = session.attempt if ref and session else None
        if ref and (attempt is None or attempt.nonce != ref):
            ui.send_result(chat_id, "新增会话已取消或过期，请重新开始。",
                           back_label="◀ 返回新增", back_callback="oa:add")
            return True
        with add_context.bind(attempt):
            if attempt is not None:
                try:
                    attempt.check()
                except ValueError:
                    ui.send_result(chat_id, "新增会话已取消或过期，请重新开始。",
                                   back_label="◀ 返回新增", back_callback="oa:add")
                    return True
            return fn(chat_id, *args, **kwargs)
    return wrapped


def nav(provider=""):
    attempt = add_context.current()
    provider = provider or (attempt.provider if attempt else "")
    callback = {"claude": "oa:add:claude", "openai": "oa:add:openai", "xai": "oa:add:xai",
                "workbuddy": "oa:add:workbuddy", "zhipu": "oa:zh:add"}.get(provider, "oa:add")
    return {"back_label": "◀ 返回新增账户" if callback == "oa:add" else "◀ 上一步", "back_callback": callback}


def description(chat_id):
    target = get(chat_id).target
    return "继承现有规则" if target is None else "直连" if target == "direct" else ui.escape_html(target)


def show_picker(chat_id, message_id, cb_id):
    from .proxy_menu import _all_targets
    clear_input(chat_id)
    session = get(chat_id)
    rows = [[ui.btn("继承现有规则", f"oa:add_proxy:{session.nonce}:inherit")]]
    rows += [[ui.btn("🌐 直连" if name == "direct" else label,
                     f"oa:add_proxy:{session.nonce}:" + ui.register_code(name))]
             for name, label in _all_targets()]
    rows.append([ui.btn("◀ 上一步", "oa:add")])
    ui.answer_cb(cb_id)
    ui.edit(chat_id, message_id, "<b>本次新增代理</b>：" + description(chat_id) +
            "\n仅影响本次新增的服务端请求；浏览器出口不受控制。\n显式选择在成功保存时成为账户代理，继承不新增专属规则。",
            reply_markup=ui.inline_kb(rows))


def select(chat_id, message_id, cb_id, payload):
    from .proxy_menu import _all_targets
    session = get(chat_id, create=False)
    nonce, _, code = payload.partition(":")
    if session is None or not secrets.compare_digest(session.nonce, nonce):
        ui.answer_cb(cb_id, "选择已过期，请重新打开新增页", show_alert=True)
        return
    target = None if code == "inherit" else ui.resolve_code(code)
    if code != "inherit" and target not in {name for name, _ in _all_targets()}:
        ui.answer_cb(cb_id, "代理已不存在，请重新选择", show_alert=True)
        return
    clear_input(chat_id)
    with _lock:
        session.target = target
    from .oauth_menu import on_add_menu
    on_add_menu(chat_id, message_id, cb_id)


def before_callback(chat_id, callback):
    cleanup()
    if chat_id not in _sessions and chat_id not in _expired:
        return
    if callback.startswith("menu:") or callback.startswith(("oa:view:", "oa:relogin:")):
        state = states.get_state(chat_id)
        if state and state["action"].startswith("oa_"):
            clear_input(chat_id)
        discard(chat_id)


def before_command(chat_id, text):
    if (chat_id in _sessions or chat_id in _expired) and (text or "").startswith("/"):
        state = states.get_state(chat_id)
        if state and state["action"].startswith("oa_"):
            clear_input(chat_id)
        discard(chat_id)
