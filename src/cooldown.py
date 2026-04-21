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


def _grace_count(channel_key: str) -> int:
    """OAuth 渠道的"宽容次数"：前 N 次失败不入冷却，只累计计数。

    避免单 OAuth 账号因偶发 timeout 就被冷却（导致所有 Claude 模型不可用）。
    第 N+1 次失败起按 errorWindows 阶梯。成功一次仍清零计数（沿用现有逻辑）。
    API 渠道不启用 grace（失败第一次就进冷却）。
    """
    if channel_key.startswith("oauth:"):
        return int(config.get().get("oauthGraceCount", 3))
    return 0


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


def _ladder_min_interval_ms() -> int:
    try:
        return int(config.get().get("cooldownLadderMinIntervalSeconds", 30)) * 1000
    except Exception:
        return 30_000


def _permanent_min_age_ms() -> int:
    try:
        return int(config.get().get("cooldownPermanentMinAgeSeconds", 300)) * 1000
    except Exception:
        return 300_000


def record_error(channel_key: str, model: str, message: str | None = None,
               *, cooldown_until: int | None = None) -> dict:
    """记一次失败，按阶梯推进 cooldown_until。返回更新后的状态。

    防爆发式冷却三道闸（2026-04-21 新增）：
      1. 已处于冷却期（非永久）：不推进阶梯，仅更新 last_error_message 和 count
         → 客户端在冷却期内重试不会把阶梯打穿
      2. 距上次 ladder 推进 < cooldownLadderMinIntervalSeconds：不推进
         → 并发 in-flight 请求一批次失败不会连推多档
      3. 首次推进到永久档时，若 first_error_at 距今 < cooldownPermanentMinAgeSeconds：
         回退到倒数第二档（而非永久） → 短时爆发式失败不会变永久；
         真正持续故障（跨越 5 分钟仍失败）才会永久

    调用方显式传 `cooldown_until`（如 429 解析出 reset ms）时，绕过三道闸直接落盘。

    若本次推进让该 (channel, model) **首次进入永久冷却**，触发"channel_permanent"事件通知。
    """
    windows = _windows()
    grace = _grace_count(channel_key)
    ladder_min_interval = _ladder_min_interval_ms()
    permanent_min_age = _permanent_min_age_ms()
    explicit_cooldown = cooldown_until
    just_became_permanent = False
    now = _now_ms()

    with _lock:
        state = _entries.get((channel_key, model)) or {
            "error_count": 0,
            "cooldown_until": None,
            "last_error_message": None,
            "first_error_at": None,
            "last_advance_at": 0,
        }
        prev_cd = state.get("cooldown_until")
        prev_count = int(state.get("error_count", 0))
        new_count = prev_count + 1

        # first_error_at：第一次失败时落地，之后只在成功清零后重置（由 clear 负责）
        first_error_at = state.get("first_error_at") or now
        last_advance_at = int(state.get("last_advance_at") or 0)

        # 已处于冷却期（非永久）
        already_cooling = (prev_cd is not None
                          and prev_cd != _INF
                          and prev_cd > now
                          and explicit_cooldown is None)
        # 距上次推进太近
        too_soon = (explicit_cooldown is None
                    and last_advance_at > 0
                    and (now - last_advance_at) < ladder_min_interval)

        if explicit_cooldown is not None:
            # 显式指定（429 reset）：直接使用，算一次推进
            cooldown_until = explicit_cooldown
            last_advance_at = now
        elif already_cooling or too_soon:
            # 不推进阶梯，保留原 cooldown_until
            cooldown_until = prev_cd
        elif new_count <= grace:
            # 仍在宽容期：只累计 error_count，不进冷却（也不发通知）
            cooldown_until = None
        else:
            # 已超出宽容期：用扣除 grace 后的次数索引 errorWindows
            ladder_idx = min(new_count - 1 - grace, len(windows) - 1)
            minutes = windows[ladder_idx]
            if minutes == 0:
                # 准备进永久：检查 first_error_at 年龄
                age_ms = now - first_error_at
                if age_ms < permanent_min_age:
                    # 时间窗不够长，回退到倒数第二档（若末位是 0 且只有这一档，用 15）
                    fallback_idx = len(windows) - 2 if len(windows) >= 2 else -1
                    fallback_min = windows[fallback_idx] if fallback_idx >= 0 else 0
                    if fallback_min <= 0:
                        fallback_min = 15  # 兜底
                    cooldown_until = now + fallback_min * 60 * 1000
                else:
                    cooldown_until = _INF
            else:
                cooldown_until = now + minutes * 60 * 1000
            last_advance_at = now

        # 检测"首次进入永久"
        if cooldown_until == _INF and prev_cd != _INF:
            just_became_permanent = True
        state["error_count"] = new_count
        state["cooldown_until"] = cooldown_until
        state["last_error_message"] = message
        state["first_error_at"] = first_error_at
        state["last_advance_at"] = last_advance_at
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
