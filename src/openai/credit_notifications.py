"""Account-scoped reminders when included Codex quota gives way to credits."""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field

from .. import notifier


@dataclass
class _State:
    generation: str
    windows: dict[str, tuple[int, float, float | None]] = field(default_factory=dict)
    gate: tuple[int, bool] = (0, False)
    sent: bool = False


_lock = threading.RLock()
_states: dict[str, _State] = {}


def forget(account_key: str) -> None:
    with _lock:
        _states.pop(account_key, None)


def _number(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def observe(account_key: str, *, usage: dict | None = None,
            snapshot: dict | None = None) -> None:
    # Notification failure must never affect routing, quota state or a response.
    try:
        from .. import oauth_manager as manager
        with manager.account_generation_guard(None):
            _observe(account_key, usage=usage, snapshot=snapshot)
    except Exception as exc:
        print(f"[oauth] credit notification failed: {type(exc).__name__}")


def _observe(account_key: str, *, usage: dict | None, snapshot: dict | None) -> None:
    from .. import oauth_manager as manager

    account = manager.get_account(account_key)
    if not account or manager.provider_of(account) != "openai":
        return
    account_key = manager._account_key(account)
    generation = manager.account_state_key(account)
    now = time.time()
    block = (usage or {}).get("openai") or {}
    windows = {}
    if snapshot is not None:
        observed = manager._ms_timestamp(snapshot.get("fetched_at")) or int(now * 1000)
        for name in ("primary", "secondary"):
            used = _number(snapshot.get(f"{name}_used_pct"))
            reset = _number(snapshot.get(f"{name}_reset_at"))
            after = _number(snapshot.get(f"{name}_reset_sec"))
            if reset is None and after is not None:
                reset = observed / 1000 + after
            if used is not None:
                windows[name] = (observed, used, reset)
    else:
        observed = manager._ms_timestamp(block.get("quota_observed_at")) or int(now * 1000)
        limits = next((item for item in block.get("rate_limits") or []
                       if item.get("limit_id") == "codex"), {})
        for name in ("primary", "secondary"):
            window = limits.get(name) or {}
            used = _number(window.get("used_percent"))
            if used is not None:
                windows[name] = (observed, used, _number(window.get("reset_at")))

    def exhausted(window):
        _at, used, reset = window
        return used >= 100 and (reset is None or reset > now)

    with _lock:
        state = _states.get(account_key)
        if state is None or state.generation != generation:
            state = _states[account_key] = _State(generation)
        if not state.gate[1] and not any(exhausted(window) for window in state.windows.values()):
            state.sent = False
        for name, window in windows.items():
            if window[0] >= state.windows.get(name, (0, 0, None))[0]:
                state.windows[name] = window
        if snapshot is None and block.get("source") == "wham_usage" and observed >= state.gate[0]:
            if block.get("allowed") is False or block.get("limit_reached") is True:
                # Known exhausted windows own their reset; a window-less refusal
                # needs explicit recovery, not an unrelated credit-only sample.
                state.gate = (observed, not any(window[1] >= 100 for window in windows.values()))
            elif block.get("allowed") is True or block.get("limit_reached") is False:
                state.gate = (observed, False)
        if state.gate[1] and len(state.windows) == 2 and all(
                at > state.gate[0] and used < 100 for at, used, _reset in state.windows.values()):
            state.gate = (max(window[0] for window in state.windows.values()), False)
        hits = [name for name, window in state.windows.items() if exhausted(window)]
        if not hits and not state.gate[1]:
            state.sent = False
            return
        if state.sent or not account.get("enabled", True) or account.get("disabled_reason"):
            return
        if not manager.openai_credits_usable(account_key, usage, snapshot=snapshot):
            return
        notifications = manager.config.get().get("notifications") or {}
        if not notifications.get("enabled", True) or not (notifications.get("events") or {}).get("openai_credits_started", True):
            return
        label = manager.openai_plan_workspace_label(account)
        lines = [
            "💳 <b>OpenAI 套餐额度已耗尽，将使用积分继续</b>",
            f"账号: <code>{notifier.escape_html(manager.account_key_to_email(account_key))}</code>",
            f"{notifier.provider_custom_emoji_html('openai')} {notifier.escape_html(label)}",
            "已开启「允许使用积分」，后续请求可能消耗积分余额；账号保持可用。",
            "如不希望消耗积分，请在该账户详情关闭「允许使用积分」。",
        ]
        if notifier.notify_event("openai_credits_started", "\n".join(lines)):
            state.sent = True
