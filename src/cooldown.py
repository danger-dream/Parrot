"""错误阶梯冷却：`(channel_key, model)` 独立计数。

阶梯默认 [1, 3, 5, 10, 15, 0] 分钟；`0` = 永久拉黑（cooldown_until = -1）。
连续失败 → 递进下一阶；成功一次 → 计数清零。

内存 + state.db 双层。init() 启动时从 state.db 恢复。
"""

from __future__ import annotations

import threading
import time
from typing import Optional

from . import config, notifier, state_db


_INF = -1  # state.db 中用 -1 表示永久

_lock = threading.Lock()
_entries: dict[tuple[str, str], dict] = {}  # (channel_key, model) -> state
_initialized = False


def init() -> None:
    global _initialized
    if _initialized:
        return
    rows = state_db.error_load_all()
    with _lock:
        _entries.clear()
        for row in rows:
            key = (row["channel_key"], row["model"])
            _entries[key] = {
                "error_count": int(row["error_count"] or 0),
                "cooldown_until": row["cooldown_until"],
                "last_error_message": row["last_error_message"],
            }
    _initialized = True
    print(f"[cooldown] loaded {len(rows)} entries from state.db")


def _windows() -> list[int]:
    cfg = config.get()
    w = cfg.get("errorWindows") or [1, 3, 5, 10, 15, 0]
    return [int(x) for x in w]


def _now_ms() -> int:
    return int(time.time() * 1000)


def get_state(channel_key: str, model: str) -> Optional[dict]:
    """返回当前冷却状态。不判断是否过期，仅按内存结构返回。"""
    with _lock:
        v = _entries.get((channel_key, model))
        return dict(v) if v else None


def is_blocked(channel_key: str, model: str) -> bool:
    """(channel, model) 是否处于冷却中（永久或未过期的 cooldown_until）。"""
    state = get_state(channel_key, model)
    if not state:
        return False
    cd = state.get("cooldown_until")
    if cd is None:
        return False
    if cd == _INF:
        return True
    return cd > _now_ms()


def record_error(channel_key: str, model: str, message: str | None = None) -> dict:
    """记一次失败，按阶梯推进 cooldown_until。返回更新后的状态。

    若本次推进让该 (channel, model) **首次进入永久冷却**，触发"channel_permanent"事件通知。
    """
    windows = _windows()
    just_became_permanent = False
    with _lock:
        state = _entries.get((channel_key, model)) or {
            "error_count": 0,
            "cooldown_until": None,
            "last_error_message": None,
        }
        prev_cd = state.get("cooldown_until")
        prev_count = int(state.get("error_count", 0))
        idx = min(prev_count, len(windows) - 1)
        minutes = windows[idx]
        new_count = prev_count + 1
        if minutes == 0:
            cooldown_until: Optional[int] = _INF
        else:
            cooldown_until = _now_ms() + minutes * 60 * 1000
        # 检测"首次进入永久"
        if cooldown_until == _INF and prev_cd != _INF:
            just_became_permanent = True
        state["error_count"] = new_count
        state["cooldown_until"] = cooldown_until
        state["last_error_message"] = message
        _entries[(channel_key, model)] = state
        result = dict(state)

    state_db.error_save(channel_key, model, new_count, cooldown_until, message)

    if just_became_permanent:
        ek = notifier.escape_html
        notifier.notify_event(
            "channel_permanent",
            "🔴 <b>渠道永久冻结</b>\n"
            f"渠道: <code>{ek(channel_key)}</code> ({ek(model)})\n"
            f"累计失败 {new_count} 次\n"
            f"最后错误: <code>{ek((message or '?')[:200])}</code>\n"
            "如需恢复请到「🔀 渠道管理」点「🧹 清错误」"
        )
    return result


def _was_actively_blocked(state: dict, now: int) -> bool:
    """判断 entry 当前是否真的处于"在冷却"（永久 / cooldown_until > now）。"""
    cd = state.get("cooldown_until")
    if cd is None:
        return False
    return cd == _INF or cd > now


def clear(channel_key: str, model: Optional[str] = None) -> None:
    """清除冷却。model=None 清该 channel 下所有模型。

    对每个清除前真的在冷却的条目，触发"channel_recovered"事件通知（避免每次成功
    都报一次"恢复"——只有从"被锁中"变成"未锁"才算恢复）。
    """
    now = _now_ms()
    recovered: list[tuple[str, str, bool]] = []   # (ck, model, was_permanent)
    with _lock:
        if model is None:
            keys = [k for k in _entries if k[0] == channel_key]
        else:
            keys = [(channel_key, model)] if (channel_key, model) in _entries else []
        for k in keys:
            entry = _entries.get(k)
            if entry and _was_actively_blocked(entry, now):
                was_perm = entry.get("cooldown_until") == _INF
                recovered.append((k[0], k[1], was_perm))
            _entries.pop(k, None)
    state_db.error_delete(channel_key, model)

    ek = notifier.escape_html
    for ck, mdl, was_perm in recovered:
        tag = "永久冻结" if was_perm else "冷却"
        notifier.notify_event(
            "channel_recovered",
            f"✅ <b>渠道恢复</b>（从{tag}中）\n"
            f"渠道: <code>{ek(ck)}</code> ({ek(mdl)})",
        )


def clear_all() -> None:
    with _lock:
        _entries.clear()
    state_db.error_delete(None, None)


def rename_channel(old_key: str, new_key: str) -> None:
    if old_key == new_key:
        return
    with _lock:
        old_items = [(k, v) for k, v in _entries.items() if k[0] == old_key]
        for (_, model), state in old_items:
            _entries.pop((old_key, model), None)
            _entries[(new_key, model)] = state
    state_db.error_rename_channel(old_key, new_key)


def active_entries(now_ms: Optional[int] = None) -> list[dict]:
    """当前仍在冷却期的条目（供 probe recovery 循环用）。"""
    now = now_ms if now_ms is not None else _now_ms()
    out: list[dict] = []
    with _lock:
        for (ck, m), state in _entries.items():
            cd = state.get("cooldown_until")
            if cd is None:
                continue
            if cd != _INF and cd <= now:
                continue
            out.append({
                "channel_key": ck,
                "model": m,
                "error_count": int(state.get("error_count", 0)),
                "cooldown_until": cd,
                "last_error_message": state.get("last_error_message"),
            })
    return out


def snapshot() -> list[dict]:
    """调试/TG 展示用（包括已过期的条目）。"""
    now = _now_ms()
    with _lock:
        out = []
        for (ck, m), state in _entries.items():
            cd = state.get("cooldown_until")
            if cd == _INF:
                remaining = "permanent"
            elif cd is None:
                remaining = None
            elif cd > now:
                remaining = f"{max(0, (cd - now) // 1000)}s"
            else:
                remaining = "expired"
            out.append({
                "channel_key": ck,
                "model": m,
                "error_count": int(state.get("error_count", 0)),
                "cooldown_until": cd,
                "remaining": remaining,
                "last_error_message": state.get("last_error_message"),
            })
        return out
