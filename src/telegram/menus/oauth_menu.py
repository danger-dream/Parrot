"""OAuth 多账户管理菜单。

callback_data 前缀：`oa:...`

状态机 action（Claude）：
  - `oa_login_code`：等待用户粘贴 PKCE 登录页返回的 code#state
  - `oa_set_json` ：等待用户粘贴 OAuth JSON（access_token/refresh_token/...）
状态机 action（OpenAI）：
  - `oa_openai_code`          ：等待用户粘贴 Codex CLI 登录后的回调 URL
  - `oa_openai_rt`            ：等待用户粘贴 refresh_token 字符串
  - `oa_openai_import`        ：等待用户上传/粘贴 Sub2API / CPA 导出内容
  - `oa_openai_import_confirm`：等待用户确认批量导入

注意：本模块所有 OAuth 远端交互都走 `oauth_manager` / `src.oauth.*`，已经有
mockMode 保护（`config.oauth.mockMode=true` 或 env DISABLE_OAUTH_NETWORK_CALLS=1）。
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import secrets
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import parse_qs, urlparse

from ... import affinity, config, cooldown, load_balancing, log_db, oauth_errors, oauth_manager, state_db
from ...oauth_ids import account_key as _account_key, openai_account_identity_parts as _openai_identity_parts, openai_workspace_id as _openai_workspace_id, split_account_key as _split_ak
from ...oauth import openai as openai_provider
from ...oauth.openai_import import OpenAIImportParseError, parse_openai_import_payload
from .. import states, ui
from . import main as main_menu


_BJT = timezone(timedelta(hours=8))




def _resolve_to_account_key(resolved):
    """short code 解析后可能是 account_key 或纯 email（历史/测试遗留）。
    纯 email 时回查 config 自动补 provider。"""
    if resolved is None:
        return None
    if ":" in resolved:
        try:
            return oauth_manager.resolve_account_key(resolved)
        except oauth_manager.AmbiguousOAuthAccountKey:
            return None
    try:
        return oauth_manager.resolve_account_key(resolved)
    except oauth_manager.AmbiguousOAuthAccountKey:
        return None


def _account_email(account_key: str) -> str:
    acc = oauth_manager.get_account(account_key)
    if acc is not None:
        return str(acc.get("email") or "")
    return oauth_manager.account_key_to_email(account_key)


def _openai_same_email_count(acc: dict) -> int:
    """OpenAI 同邮箱 workspace 数；只用于决定 UI 是否需要消歧标签。"""
    if oauth_manager.provider_of(acc) != "openai":
        return 0
    email = str(acc.get("email") or "")
    return sum(
        1 for item in oauth_manager.list_accounts()
        if oauth_manager.provider_of(item) == "openai"
        and str(item.get("email") or "") == email
    )


def _openai_workspace_label(acc: dict, *, html: bool = True, force: bool = False) -> str:
    """Human-facing OpenAI disambiguation label.

    只在同邮箱多个 workspace 时补一个极短标签。默认不展示内部 workspace id。
    """
    if oauth_manager.provider_of(acc) != "openai":
        return ""
    if not force and _openai_same_email_count(acc) <= 1:
        return ""
    name = str(acc.get("workspace_name") or "").strip()
    wtype = str(acc.get("workspace_type") or "").strip()
    plan = str(acc.get("plan_type") or "").strip()
    # OpenAI may return Team workspaces with a generic name of "Personal".
    # Keep the account type visible instead of showing a misleading Personal
    # workspace label. Curated names such as SP/AU/UK/IN/AU2 still win.
    if name and not (name.lower() == "personal" and "team" in f"{wtype} {plan}".lower()):
        text = name
    elif wtype:
        text = wtype
    else:
        text = "workspace"
    return ui.escape_html(text) if html else text

# ─── 辅助：异步调用在线程里运行 ───────────────────────────────────

def _run_sync(coro):
    """在 TG 线程里阻塞跑一个 async 函数。"""
    try:
        return asyncio.run(coro)
    except Exception as exc:
        return exc


def _oauth_error_html(exc, *, provider: str, operation: str, indent: str = "") -> str:
    """OAuth 错误的用户友好 HTML 文案；禁止把 raw httpx 异常直出到 TG。"""
    return oauth_errors.format_oauth_error_html(
        exc, provider=provider, operation=operation, indent=indent,
    )


def _replace_last_with_oauth_error(
    lines: list[str], exc, *, provider: str, operation: str, indent: str = "  "
) -> None:
    lines[-1:] = _oauth_error_html(
        exc, provider=provider, operation=operation, indent=indent,
    ).splitlines()


def _save_usage_to_quota_cache(ak: str, usage: dict, *, email: str | None = None):
    try:
        state_db.quota_save(
            ak, oauth_manager.flatten_usage(usage),
            email=email if email is not None else _account_email(ak),
        )
    except Exception as exc:
        return exc
    return None


def _fetch_and_save_usage_result_sync(ak: str, *, email: str | None = None, on_stage=None) -> dict:
    """同步拉 usage；OpenAI 再拉 reset-card 明细；写入同一份 quota cache。

    on_stage(stage, payload) 用于进度面板：
      - usage_start / usage_done
      - reset_start / reset_done / reset_error
    """
    provider = oauth_manager.provider_of(ak)
    details = None
    detail_error = None

    def _stage(name: str, payload=None) -> None:
        if on_stage is None:
            return
        try:
            on_stage(name, payload)
        except Exception:
            pass

    _stage("usage_start")
    usage = _run_sync(oauth_manager.fetch_usage(ak))
    if isinstance(usage, Exception):
        return {"error": usage}
    save_err = _save_usage_to_quota_cache(ak, usage, email=email)
    if save_err is not None:
        return {"error": save_err}
    _stage("usage_done", usage)

    if provider == "openai":
        count = _openai_reset_credit_count_from_usage(usage)
        if count is None or count > 0:
            _stage("reset_start", usage)
            details = _run_sync(oauth_manager.fetch_openai_rate_limit_reset_credits(ak))
            if isinstance(details, Exception):
                detail_error = details
                old_details = _openai_reset_credit_details_from_row(state_db.quota_load(ak))
                if old_details is not None:
                    usage = oauth_manager.attach_openai_reset_credit_details_to_usage(
                        usage, old_details, sync_available_count=False,
                    )
                    save_err = _save_usage_to_quota_cache(ak, usage, email=email)
                    if save_err is not None:
                        return {"error": save_err}
                _stage("reset_error", detail_error)
            elif isinstance(details, dict):
                usage = oauth_manager.attach_openai_reset_credit_details_to_usage(usage, details)
                save_err = _save_usage_to_quota_cache(ak, usage, email=email)
                if save_err is not None:
                    return {"error": save_err}
                _stage("reset_done", details)
        else:
            _stage("reset_done", {"available_count": count, "data": []})

    return {"usage": usage, "reset_credit_details": details, "reset_credit_error": detail_error}


def _fetch_and_save_usage_sync(ak: str, *, email: str | None = None):
    """同步拉 usage 并写 quota cache；返回 usage 或 Exception。"""
    result = _fetch_and_save_usage_result_sync(ak, email=email)
    return result.get("error") or result.get("usage")


def _evaluate_quota_action(ak: str, usage: dict) -> dict | None:
    try:
        return oauth_manager.evaluate_and_toggle_by_usage(ak, usage)
    except Exception as exc:
        print(f"[oauth_menu] quota evaluate failed for {ak}: {exc}")
        return None


def _openai_reset_credit_count_from_usage(usage: dict | None) -> int | None:
    if not isinstance(usage, dict):
        return None
    summary = (usage.get("openai") or {}).get("rate_limit_reset_credits")
    if not isinstance(summary, dict):
        summary = usage.get("rate_limit_reset_credits")
    if not isinstance(summary, dict):
        return None
    try:
        return int(summary.get("available_count"))
    except (TypeError, ValueError):
        return None


def _openai_reset_credit_count_from_row(row: dict | None) -> int | None:
    if not row:
        return None
    raw = row.get("raw_data")
    if not raw:
        return None
    try:
        usage = json.loads(raw)
    except Exception:
        return None
    return _openai_reset_credit_count_from_usage(usage)


def _openai_reset_credit_details_from_usage(usage: dict | None) -> dict | None:
    if not isinstance(usage, dict):
        return None
    openai = usage.get("openai")
    details = openai.get("rate_limit_reset_credit_details") if isinstance(openai, dict) else None
    if not isinstance(details, dict):
        details = usage.get("rate_limit_reset_credit_details")
    return details if isinstance(details, dict) else None


def _openai_reset_credit_details_from_row(row: dict | None) -> dict | None:
    if not row:
        return None
    raw = row.get("raw_data")
    if not raw:
        return None
    try:
        usage = json.loads(raw)
    except Exception:
        return None
    return _openai_reset_credit_details_from_usage(usage)


def _openai_reset_credit_label_from_row(row: dict | None, *, show_zero: bool = True) -> str:
    count = _openai_reset_credit_count_from_row(row)
    if count is None:
        return "未获取" if show_zero else ""
    if count <= 0 and not show_zero:
        return ""
    return f"{count} 次"


def _openai_reset_credit_count_from_details(details: dict | None) -> int | None:
    if not isinstance(details, dict):
        return None
    try:
        return int(details.get("available_count"))
    except (TypeError, ValueError):
        return None


def _fetch_openai_reset_credit_details_for_ui(account_key: str,
                                              *, cached_count: int | None = None):
    """Fetch reset-card details for detail page; skip known-zero accounts."""
    if cached_count is not None and cached_count <= 0:
        return None
    return _run_sync(oauth_manager.fetch_openai_rate_limit_reset_credits(account_key))


def _reset_credit_type_label(reset_type: str | None) -> str:
    mapping = {
        "codex_rate_limits": "Codex 额度重置",
        "codexRateLimits": "Codex 额度重置",
    }
    raw = str(reset_type or "").strip()
    return mapping.get(raw, raw or "重置卡")


def _reset_credit_status_label(status: str | None) -> str:
    mapping = {
        "available": "可用",
        "redeeming": "兑换中",
        "redeemed": "已使用",
    }
    raw = str(status or "").strip()
    return mapping.get(raw, raw or "未知状态")


def _format_reset_credit_cards_block(details, *, cached_count: int | None = None,
                                     available_count_override: int | None = None) -> str:
    if details is None:
        display_count = available_count_override if available_count_override is not None else cached_count
        if display_count is None or display_count <= 0:
            return ""
        return (
            "<b>♻️ 官方重置卡</b>\n"
            f"当前可用 <code>{display_count} 次</code>；"
            "卡片明细尚未缓存，正在后台刷新。也可以点「刷新用量/重置卡」立即拉取。"
        )
    if isinstance(details, Exception):
        display_count = available_count_override if available_count_override is not None else cached_count
        if display_count is None or display_count <= 0:
            return ""
        return "<b>♻️ 官方重置卡</b>\n" + _oauth_error_html(
            details, provider="openai", operation="rate_limit_reset_credit_details",
            indent="",
        )
    if not isinstance(details, dict):
        return ""

    available_count = _openai_reset_credit_count_from_details(details)
    display_count = available_count_override if available_count_override is not None else available_count
    data = details.get("data")
    if not isinstance(data, list):
        data = []
    if (display_count is None or display_count <= 0) and not data:
        return ""

    lines = ["<b>♻️ 官方重置卡</b>"]
    # 刚消耗 reset credit 后，wham/usage 的 available_count 是本次重绘的权威值。
    # 如果卡片明细端点存在短暂同步延迟并返回了更大的旧 count，宁可提示稍后刷新，
    # 不展示可能已经被消耗掉的旧卡片，避免 TG 详情页残留误导。
    if (
        available_count_override is not None
        and available_count is not None
        and available_count > available_count_override
    ):
        if available_count_override <= 0:
            return ""
        lines.append(
            f"当前可用 <code>{available_count_override} 次</code>；"
            "卡片明细仍在同步，稍后刷新可查看最新列表。"
        )
        return "\n".join(lines)

    if not data:
        count_text = "未获取" if display_count is None else f"{display_count} 次"
        lines.append(f"当前可用 <code>{ui.escape_html(count_text)}</code>，上游未返回卡片明细。")
        return "\n".join(lines)

    for idx, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            continue
        status = ui.escape_html(_reset_credit_status_label(item.get("status")))
        reset_type = ui.escape_html(_reset_credit_type_label(item.get("reset_type")))
        lines.append(f"{idx}. {status} · {reset_type}")
        granted_at = item.get("granted_at")
        expires_at = item.get("expires_at")
        if granted_at:
            lines.append(f"   发放: <code>{_format_bjt(str(granted_at))}</code>")
        if expires_at:
            lines.append(f"   过期: <code>{_fmt_time_full(str(expires_at))}</code>")
        else:
            lines.append("   过期: <code>上游未返回</code>")

    footer_count = display_count if display_count is not None else available_count
    if footer_count is not None and footer_count > len(data):
        lines.append(f"仅展示前 <code>{len(data)}</code> 张；当前共 <code>{footer_count}</code> 张可用。")
    return "\n".join(lines)



_BACKGROUND_REFRESH_LOCK = threading.Lock()
_BACKGROUND_REFRESH_INFLIGHT: set[str] = set()
_METADATA_REFRESH_LOCK = threading.Lock()
_METADATA_REFRESH_INFLIGHT: set[str] = set()


def _access_refresh_throttle_seconds_for_ui() -> int:
    qm = config.get().get("quotaMonitor") or {}
    try:
        return int(qm.get("accessRefreshThrottleSeconds", 180))
    except Exception:
        return 180


def _quota_monitor_enabled_for_ui() -> bool:
    qm = config.get().get("quotaMonitor") or {}
    return bool(qm.get("enabled", False))


def _quota_cache_is_stale_for_ui(row: dict | None) -> bool:
    if not row:
        return True
    try:
        fetched_at_ms = int(row.get("fetched_at") or 0)
    except Exception:
        fetched_at_ms = 0
    if fetched_at_ms <= 0:
        return True
    age_s = (state_db.now_ms() - fetched_at_ms) / 1000.0
    return age_s >= _access_refresh_throttle_seconds_for_ui()


def _quota_cache_has_usage_signal(row: dict | None) -> bool:
    if not row:
        return False
    for key in (
        "five_hour_util", "seven_day_util", "thirty_day_util",
        "sonnet_util", "opus_util", "extra_util",
    ):
        if row.get(key) is not None:
            return True
    return False


def _should_refresh_account_for_ui(acc: dict | None) -> bool:
    if not acc or not acc.get("email"):
        return False
    # 用户手动禁用 / auth_error 不做自动远端刷新；quota 禁用需要刷新，
    # 否则缺 cache 时会一直显示“尚未获取”，也无法自动恢复。
    return acc.get("disabled_reason") not in ("user", "auth_error")


def _refreshable_account_keys_for_ui(accounts: list[dict]) -> list[str]:
    return [_account_key(a) for a in accounts if _should_refresh_account_for_ui(a)]


def _needs_initial_oauth_cache_sync_for_ui(account_key: str, *, include_details: bool = False) -> bool:
    # 首拉只解决“完全没有有效 usage cache”的空白页问题。已有 quota
    # 窗口数据时，即使 OpenAI reset-card 明细缺失，也不能同步覆盖旧快照；
    # 否则会把响应头实时采样的 85%/98% 等配额状态改写成 mock/新 usage，
    # 影响禁用判断。缺失的 reset-card 明细交给后台刷新或手动刷新按钮。
    row = state_db.quota_load(account_key)
    return not _quota_cache_has_usage_signal(row)


def _needs_oauth_cache_refresh_for_ui(account_key: str) -> bool:
    row = state_db.quota_load(account_key)
    prov = oauth_manager.provider_of(account_key)
    if row is None:
        return True

    stale = _quota_cache_is_stale_for_ui(row)
    if prov == "openai":
        count = _openai_reset_credit_count_from_row(row)
        details = _openai_reset_credit_details_from_row(row)
        # 旧缓存或 quotaMonitor 刷出的 usage 可能只有次数，没有卡片明细。
        # OpenAI 页进入时后台补齐 usage + reset-card，UI 当前仍只读缓存。
        if count is None:
            return True
        if count > 0 and details is None:
            return True
        if stale:
            return True

    if _quota_monitor_enabled_for_ui():
        return False
    return stale


def _schedule_openai_metadata_for_ui(account_keys: list[str] | str, *, force: bool = False) -> None:
    if isinstance(account_keys, str):
        keys = [account_keys]
    else:
        keys = list(account_keys or [])
    keys = [k for k in keys if k and oauth_manager.provider_of(k) == "openai"]
    if not keys:
        return
    with _METADATA_REFRESH_LOCK:
        pending = [k for k in keys if force or k not in _METADATA_REFRESH_INFLIGHT]
        for k in pending:
            _METADATA_REFRESH_INFLIGHT.add(k)
    if not pending:
        return

    def _worker() -> None:
        try:
            oauth_manager.ensure_openai_metadata_fresh_sync(
                pending, force=force, min_interval_seconds=3600, timeout_s=5.0,
            )
        except Exception as exc:
            print(f"[oauth_menu] background openai metadata refresh failed: {exc}")
        finally:
            with _METADATA_REFRESH_LOCK:
                for k in pending:
                    _METADATA_REFRESH_INFLIGHT.discard(k)

    threading.Thread(target=_worker, daemon=True).start()


def _schedule_oauth_cache_refresh_for_ui(account_keys: list[str] | str, *, force: bool = False) -> None:
    if isinstance(account_keys, str):
        keys = [account_keys]
    else:
        keys = list(account_keys or [])
    pending: list[str] = []
    with _BACKGROUND_REFRESH_LOCK:
        for ak in keys:
            if not ak:
                continue
            if not force and not _needs_oauth_cache_refresh_for_ui(ak):
                continue
            if ak in _BACKGROUND_REFRESH_INFLIGHT:
                continue
            _BACKGROUND_REFRESH_INFLIGHT.add(ak)
            pending.append(ak)
    if not pending:
        return

    def _worker() -> None:
        for ak in pending:
            try:
                email = _account_email(ak)
                result = _fetch_and_save_usage_result_sync(ak, email=email)
                if result.get("error") is not None:
                    print(f"[oauth_menu] background quota refresh failed for {ak}: {result.get('error')}")
                    continue
                usage = result.get("usage")
                if isinstance(usage, dict):
                    _evaluate_quota_action(ak, usage)
            except Exception as exc:
                print(f"[oauth_menu] background quota refresh error for {ak}: {exc}")
            finally:
                with _BACKGROUND_REFRESH_LOCK:
                    _BACKGROUND_REFRESH_INFLIGHT.discard(ak)

    threading.Thread(target=_worker, daemon=True).start()


# ─── 时间 / 用量格式化 ────────────────────────────────────────────

def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _format_bjt(iso_str: Optional[str]) -> str:
    dt = _parse_iso(iso_str)
    if dt is None:
        return "?"
    return dt.astimezone(_BJT).strftime("%Y-%m-%d %H:%M:%S")


def _format_reset_text(iso_str: Optional[str]) -> str:
    """配额窗口重置时间的展示文案。"""
    if not iso_str:
        return "上游未返回"
    return _format_bjt(iso_str)


def _format_remaining(iso_str: Optional[str]) -> str:
    """人类可读剩余时间：剩 3天11小时 / 剩 5小时23分 / 剩 4分钟 / 已过期。"""
    dt = _parse_iso(iso_str)
    if dt is None:
        return "?"
    delta = (dt - datetime.now(timezone.utc)).total_seconds()
    if delta <= 0:
        return "已过期"
    days = int(delta // 86400)
    hours = int((delta % 86400) // 3600)
    minutes = int((delta % 3600) // 60)
    if days > 0:
        return f"剩 {days}天{hours}小时"
    if hours > 0:
        return f"剩 {hours}小时{minutes}分"
    return f"剩 {minutes}分钟"


def _fmt_time_full(iso_str: Optional[str]) -> str:
    """RemainingTimeFormatFull: 2026-06-10 07:09:01（剩 3天11小时）"""
    dt = _parse_iso(iso_str)
    if dt is None:
        return "?"
    time_str = dt.astimezone(_BJT).strftime("%Y-%m-%d %H:%M:%S")
    remaining = _format_remaining(iso_str)
    return f"{time_str}（{remaining}）"


def _status_icon(acc: dict) -> str:
    """账户状态 icon。"""
    reason = acc.get("disabled_reason")
    if reason == "user":
        return "🚫"
    if reason == "quota":
        return "🔒"
    if reason == "auth_error":
        return "⚠"
    if not acc.get("enabled", True):
        return "🔕"
    return "✅"


def _this_month_start_ts() -> float:
    """北京时间本月 00:00:00 的时间戳。"""
    now = datetime.now(_BJT)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return month_start.timestamp()


# 用量明细行缩进：让明细对齐上一行 5h/7d 的数字（emoji 占位用空格补齐）。
# 列表页主行是 "  📊 5h"，详情页主行是 "⏱ 5h"，两处 emoji 前缀宽度不同，
# 缩进常量分开调；TG 比例字体下 emoji 非整数字宽，最终以真机为准微调。
# 明细行缩进：HTML parse_mode 会吃掉行首普通空格，必须用 NBSP(U+00A0) 才稳定缩进。
# 列表页主行是 "  📊 5h"，明细行对齐到「5」需要 NBSP×9（真机校准，emoji 宽度非整数倍）。
_USAGE_DETAIL_INDENT_LIST = "\u00a0" * 7
# 详情页主行是 "⏱ 5h"（无前导空格、emoji 不同），单独校准。
_USAGE_DETAIL_INDENT_BLOCK = "\u00a0" * 7

_USAGE_DISPLAY_USED = "used"
_USAGE_DISPLAY_REMAINING = "remaining"


def _usage_display_mode() -> str:
    """OAuth 用量展示口径。配置只影响 UI 展示，不改变 quota cache 存储。"""
    mode = str(config.get().get("oauthUsageDisplayMode") or _USAGE_DISPLAY_USED).strip().lower()
    return _USAGE_DISPLAY_REMAINING if mode == _USAGE_DISPLAY_REMAINING else _USAGE_DISPLAY_USED


def _usage_display_label(mode: str | None = None) -> str:
    mode = _usage_display_mode() if mode is None else mode
    return "剩余用量" if mode == _USAGE_DISPLAY_REMAINING else "已使用量"


def _usage_display_percent(util) -> float:
    """把 state_db 中的 used% 转成当前 UI 模式下应展示的百分比。"""
    try:
        used = float(util)
    except (TypeError, ValueError):
        return 0.0
    used = max(0.0, min(100.0, used))
    if _usage_display_mode() == _USAGE_DISPLAY_REMAINING:
        return max(0.0, 100.0 - used)
    return used


def _format_usage_value_html(util, *, decimals: int = 0) -> str:
    pct = _usage_display_percent(util)
    label = "剩余" if _usage_display_mode() == _USAGE_DISPLAY_REMAINING else "已用"
    return f"{label} <b>{pct:.{decimals}f}%</b>"


def _format_usage_value_text(util, *, decimals: int = 0) -> str:
    pct = _usage_display_percent(util)
    label = "剩余" if _usage_display_mode() == _USAGE_DISPLAY_REMAINING else "已用"
    return f"{label} {pct:.{decimals}f}%"


def _quota_window_since_ts(reset_iso: str | None, window_seconds: int, *, now_ts: float | None = None) -> float:
    """Return current quota window start timestamp for local usage details.

    Upstream 5h/7d quota is windowed by its next reset time, so the local detail
    should count from `reset_at - window`. If reset is absent/invalid/stale, fall
    back to the old rolling window (`now - window`) so the UI still has data.
    """
    now_ts = time.time() if now_ts is None else now_ts
    fallback = now_ts - window_seconds
    if not reset_iso:
        return fallback
    try:
        dt = datetime.fromisoformat(str(reset_iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        reset_ts = dt.timestamp()
    except Exception:
        return fallback
    # Stale reset means the cache is old/odd. Future reset beyond one window plus
    # a small grace is also suspicious. In both cases keep the old rolling window.
    if reset_ts <= now_ts - 60:
        return fallback
    if reset_ts > now_ts + window_seconds + 300:
        return fallback
    return reset_ts - window_seconds


def _window_usage_detail(account_key: str, since_ts: float, indent: str) -> Optional[str]:
    """某 OAuth 账号在 [since_ts, now] 窗口内、经 Parrot 的本地请求用量明细行。

    返回已缩进的单行文本；窗口内没有本地请求时返回 None（不堆空行）。

    口径提醒：上一行 5h/7d 百分比是上游账号「全局」配额用量；这里的
    tokens / 缓存 / 平均 TPS 只统计走 Parrot 的本地日志。账号若在别处也被
    使用，两者会对不上，属预期，不是 bug。
    """
    try:
        s = log_db.tokens_for_channel(f"oauth:{account_key}", since_ts=since_ts)
    except Exception:
        return None
    if not s or (s.get("total") or 0) <= 0:
        return None
    prompt = ui.prompt_total(s["input"], s["cache_creation"], s["cache_read"])
    parts = [f"↑{ui.fmt_tokens(prompt)} ↓{ui.fmt_tokens(s['output'])}"]
    if (s.get("cache_read") or 0) > 0:
        parts.append(ui.fmt_cache_phrase(s["cache_read"], prompt))
    if s.get("avg_tps") is not None:
        parts.append(f"均 {ui.fmt_tps(s.get('avg_tps'))}")
    return indent + " · ".join(parts)


def _format_account_block(acc: dict) -> str:
    """列表中每条 OAuth 账号的统一多行展示块。"""
    email = acc.get("email", "?")
    ak = _account_key(acc)
    icon = _status_icon(acc)
    reason = acc.get("disabled_reason")
    prov = oauth_manager.provider_of(acc)

    # 状态 tag
    tag = ""
    if reason == "user":
        tag = " [用户禁用]"
    elif reason == "quota":
        du = acc.get("disabled_until")
        tag = f" [配额禁用 · 预计 {_format_bjt(du)}]"
    elif reason == "auth_error":
        tag = " [认证失败]"

    # 第一行：icon + email + provider
    prov_tag = " 🅾️ OpenAI" if prov == "openai" else (" 🅰️ Claude" if prov == "claude" else "")
    lines = [f"{icon} <code>{ui.escape_html(email)}</code>{prov_tag}{tag}"]
    row = state_db.quota_load(ak)

    # 套餐行
    if prov == "openai":
        plan = acc.get("plan_type") or ""
        workspace = _openai_workspace_label(acc)
        ws_suffix = f"（{ui.escape_html(workspace)}）" if workspace else ""
        plan_parts = []
        if plan:
            plan_parts.append(f"套餐: <code>{ui.escape_html(plan)}{ws_suffix}</code>")
        reset_label = _openai_reset_credit_label_from_row(row, show_zero=False)
        if reset_label:
            plan_parts.append(f"♻️ 官方重置次数: <code>{reset_label}</code>")
        if plan_parts:
            lines.append("🏷 " + " · ".join(plan_parts))
        sub_exp = acc.get("subscription_expires_at") or ""
        if sub_exp:
            lines.append(f"📅 到期: <code>{_fmt_time_full(sub_exp)}</code>")
    elif prov == "claude":
        cl_label = oauth_manager.claude_plan_label(acc)
        if cl_label:
            lines.append(f"🏷️ 套餐: <code>{ui.escape_html(cl_label)}</code>")

    # 用量（5h / 7d）。百分比来自上游全局配额；其下明细行是「走 Parrot 的
    # 本地请求」在该窗口内的 tokens/缓存/平均 TPS（口径不同，仅本地流量）。
    _now_ts = time.time()
    if row:
        fh_util = row.get("five_hour_util")
        sd_util = row.get("seven_day_util")
        if fh_util is not None:
            reset = row.get("five_hour_reset")
            lines.append(f"📊 5h: {_format_usage_value_html(fh_util)} · 重置 <code>{_fmt_time_full(reset)}</code>")
            since_ts = _quota_window_since_ts(reset, 5 * 3600, now_ts=_now_ts)
            _d = _window_usage_detail(ak, since_ts, _USAGE_DETAIL_INDENT_LIST)
            if _d:
                lines.append(_d)
        if sd_util is not None:
            reset = row.get("seven_day_reset")
            lines.append(f"📊 7d: {_format_usage_value_html(sd_util)} · 重置 <code>{_fmt_time_full(reset)}</code>")
            since_ts = _quota_window_since_ts(reset, 7 * 86400, now_ts=_now_ts)
            _d = _window_usage_detail(ak, since_ts, _USAGE_DETAIL_INDENT_LIST)
            if _d:
                lines.append(_d)
        td_util = row.get("thirty_day_util")
        if td_util is not None:
            reset = row.get("thirty_day_reset")
            lines.append(f"📊 30d: {_format_usage_value_html(td_util)} · 重置 <code>{_fmt_time_full(reset)}</code>")
        if fh_util is None and sd_util is None and td_util is None:
            lines.append("📊 用量: <i>尚未获取</i>")
    else:
        lines.append("📊 用量: <i>尚未获取</i>")

    # 月度统计
    try:
        since_ts = _this_month_start_ts()
        ts = log_db.tokens_for_channel(f"oauth:{ak}", since_ts=since_ts)
    except Exception:
        ts = None
    if ts and ts["total"] > 0:
        prompt = ui.prompt_total(ts["input"], ts["cache_creation"], ts["cache_read"])
        stat_line = f"💎 月度: ↑ {ui.fmt_tokens(prompt)} · ↓ {ui.fmt_tokens(ts['output'])}"
        if (ts.get("cache_read") or 0) > 0:
            stat_line += f" · {ui.fmt_cache_phrase(ts['cache_read'], prompt)}"
        lines.append(stat_line)
        if ts.get("avg_tps") is not None:
            lines.append(
                f"⚡ TPS: 平均 {ui.fmt_tps(ts.get('avg_tps'))} · "
                f"峰值 {ui.fmt_tps(ts.get('max_tps'))} · "
                f"最低 {ui.fmt_tps(ts.get('min_tps'))}"
            )

    # 冷却状态
    from ... import cooldown as _cd
    ck = f"oauth:{ak}"
    cds = [e for e in _cd.active_entries() if e.get("channel_key") == ck]
    if cds:
        perm_n = sum(1 for e in cds if e.get("cooldown_until") == -1)
        cool_n = len(cds) - perm_n
        parts = []
        if perm_n:
            parts.append(f"🔴 永久冻结 {perm_n} 个模型")
        if cool_n:
            parts.append(f"🟠 冷却 {cool_n} 个模型")
        lines.append("⚠ " + " · ".join(parts))

    return "\n".join(lines)


def _format_usage_block(account_key: str) -> str:
    row = state_db.quota_load(account_key)
    if not row:
        return "尚未获取用量（点「刷新用量/重置卡」试试）"

    def _line(label: str, util, reset) -> Optional[str]:
        if util is None:
            return None
        return f"{label}: {_format_usage_value_text(util)} (重置: {_format_reset_text(reset)})"

    out = []
    _now_ts = time.time()
    _detail_window_seconds = {
        "five_hour_util": 5 * 3600,
        "seven_day_util": 7 * 86400,
    }
    for label, util_k, reset_k in (
        ("⏱ 5h", "five_hour_util", "five_hour_reset"),
        ("📅 7d", "seven_day_util", "seven_day_reset"),
        ("📆 30d", "thirty_day_util", "thirty_day_reset"),
        ("🤖 Sonnet 7d", "sonnet_util", "sonnet_reset"),
        ("🧠 Opus 7d", "opus_util", "opus_reset"),
    ):
        line = _line(label, row.get(util_k), row.get(reset_k))
        if line:
            out.append(line)
            if util_k in _detail_window_seconds:
                since_ts = _quota_window_since_ts(
                    row.get(reset_k), _detail_window_seconds[util_k], now_ts=_now_ts,
                )
                _d = _window_usage_detail(account_key, since_ts, _USAGE_DETAIL_INDENT_BLOCK)
                if _d:
                    out.append(_d)

    ex_used = row.get("extra_used")
    ex_limit = row.get("extra_limit")
    ex_util = row.get("extra_util")
    if ex_limit and ex_limit > 0:
        if _usage_display_mode() == _USAGE_DISPLAY_REMAINING:
            remaining = max(0.0, float(ex_limit) - float(ex_used or 0))
            out.append(f"💰 额外: 剩余 ${remaining:.2f} / ${ex_limit:.2f} ({_usage_display_percent(ex_util):.1f}%)")
        else:
            out.append(f"💰 额外: 已用 ${ex_used or 0:.2f} / ${ex_limit:.2f} ({_usage_display_percent(ex_util):.1f}%)")

    fetched = row.get("fetched_at")
    if fetched:
        dt = datetime.fromtimestamp(fetched / 1000, tz=_BJT)
        out.append(f"\n<i>更新于 {dt.strftime('%H:%M:%S')}</i>")
    return "\n".join(out) if out else "(无数据)"


# ─── 列表视图 ─────────────────────────────────────────────────────

_PAGE_SIZE = 4  # 每页显示账户数

_FILTER_ALL = "all"
_FILTER_AVAILABLE = "available"
_FILTER_QUOTA = "quota"
_FILTER_INVALID = "invalid"
_FILTER_LABELS = {
    _FILTER_ALL: "全部",
    _FILTER_AVAILABLE: "可用",
    _FILTER_QUOTA: "限额",
    _FILTER_INVALID: "失效",
}


def _default_models_for_settings(family: str) -> list[str]:
    cfg = config.get()
    if family == "openai":
        raw = (cfg.get("openaiOAuth") or {}).get("defaultModels") or []
    else:
        raw = cfg.get("oauthDefaultModels") or []
    if not isinstance(raw, list):
        return []
    return [str(x) for x in raw if str(x).strip()]


def _quota_monitor_values() -> tuple[bool, int, float]:
    qm = config.get().get("quotaMonitor") or {}
    enabled = bool(qm.get("enabled", False))
    interval = int(qm.get("intervalSeconds", 60) or 60)
    threshold = float(qm.get("disableThresholdPercent", 95) or 95)
    return enabled, interval, threshold


def _cch_enabled() -> bool:
    # 新 UI 只保留 disabled / dynamic；历史 static 不再作为可选模式展示。
    return str(config.get().get("cchMode", "disabled")) == "dynamic"


def _cch_status_label() -> str:
    return "✅ 已启用" if _cch_enabled() else "🚫 已关闭"


def _usage_toggle_target_label() -> str:
    target = _USAGE_DISPLAY_USED if _usage_display_mode() == _USAGE_DISPLAY_REMAINING else _USAGE_DISPLAY_REMAINING
    return _usage_display_label(target)


def _settings_text_and_kb() -> tuple[str, dict]:
    anthropic_models = _default_models_for_settings("anthropic")
    openai_models = _default_models_for_settings("openai")
    mode_label = _usage_display_label()
    quota_enabled, quota_interval, quota_threshold = _quota_monitor_values()
    quota_status = "✅ 已启用" if quota_enabled else "🚫 已停用"
    cch_enabled = _cch_enabled()
    cch_action = "关闭" if cch_enabled else "开启"

    def _models_line(models: list[str]) -> str:
        if not models:
            return "<i>(空)</i>"
        return ui.escape_html(", ".join(models))

    text = "\n".join([
        "⚙️ <b>OAuth 账户设置</b>",
        "",
        f"🅰️ <b>Anthropic 可用模型</b> ({len(anthropic_models)}):",
        _models_line(anthropic_models),
        "",
        f"🅾️ <b>OpenAI 可用模型</b>({len(openai_models)}):",
        _models_line(openai_models),
        "",
        "🎭 <b>CCH 模式（Claude Code 伪装）</b>",
        f"当前模式: {_cch_status_label()}",
        "",
        "📊 <b>用量显示模式</b>",
        f"当前模式: {mode_label}",
        "",
        "📈 <b>OAuth 配额监控</b>",
        f"状态: {quota_status}",
        f"检查间隔: <code>{quota_interval}s</code>",
        f"禁用阈值: <code>{quota_threshold:.0f}%</code>",
    ])
    rows = [
        [ui.btn("✏ 修改Anthropic模型", "odm:edit:anthropic"),
         ui.btn("✏ 修改OpenAI模型", "odm:edit:openai")],
        [ui.btn("🖼 图片生成设置", "img:show"),
         ui.btn("📈 配额监控", "oa:quota")],
        [ui.btn(f"🎭 CCH模式：{cch_action}", "oa:cch_toggle"),
         ui.btn(f"📊 显示: {_usage_toggle_target_label()}", "oa:usage_mode:toggle")],
        [ui.btn("🏠 返回主菜单", "menu:main"),
         ui.btn("◀ 返回OAuth账户", "menu:oauth")],
    ]
    return text, ui.inline_kb(rows)


def on_settings(chat_id: int, message_id: int, cb_id: Optional[str] = None) -> None:
    if cb_id is not None:
        ui.answer_cb(cb_id)
    text, kb = _settings_text_and_kb()
    ui.edit(chat_id, message_id, text, reply_markup=kb)


def on_toggle_usage_display_mode(chat_id: int, message_id: int, cb_id: str) -> None:
    old_mode = _usage_display_mode()
    new_mode = _USAGE_DISPLAY_REMAINING if old_mode == _USAGE_DISPLAY_USED else _USAGE_DISPLAY_USED

    def _mutate(cfg: dict) -> None:
        cfg["oauthUsageDisplayMode"] = new_mode

    config.update(_mutate)
    ui.answer_cb(cb_id, f"已切换为{_usage_display_label(new_mode)}")
    text, kb = _settings_text_and_kb()
    ui.edit(chat_id, message_id, text, reply_markup=kb)


def on_toggle_cch_mode(chat_id: int, message_id: int, cb_id: str) -> None:
    new_mode = "disabled" if _cch_enabled() else "dynamic"
    config.update(lambda c: c.__setitem__("cchMode", new_mode))
    ui.answer_cb(cb_id, "CCH 已开启" if new_mode == "dynamic" else "CCH 已关闭")
    text, kb = _settings_text_and_kb()
    ui.edit(chat_id, message_id, text, reply_markup=kb)


def _quota_menu_text_and_kb() -> tuple[str, dict]:
    enabled, interval, threshold = _quota_monitor_values()
    status = "✅ 已启用" if enabled else "🚫 已停用"
    toggle_label = "🚫 停用" if enabled else "✅ 启用"
    text = "\n".join([
        "📈 <b>OAuth 配额监控</b>",
        "",
        f"状态: <b>{status}</b>",
        f"检查间隔: <code>{interval}s</code>",
        f"禁用阈值: <code>{threshold:.0f}%</code>",
        "",
        "<b>说明：</b>",
        "• 启用后，每 N 秒拉一次每个 OAuth 账号的 usage",
        "• 任一指标（5h / 7d / Sonnet 7d / Opus 7d）≥ 阈值则自动禁用账号",
        "• resets_at 过后 + 全部指标 &lt; 阈值 → 自动恢复",
        "",
        "<i>⚠ 频繁请求 /api/oauth/usage 可能被 Anthropic 风控盯上；建议保持 ≥60s 间隔。</i>",
    ])
    rows = [
        [ui.btn(toggle_label, "oa:quota_toggle")],
        [ui.btn("✏ 修改间隔（秒）", "oa:edit:quota_interval"),
         ui.btn("✏ 修改阈值（%）", "oa:edit:quota_threshold")],
        [ui.btn("🏠 返回主菜单", "menu:main"),
         ui.btn("◀ 返回账户设置", "oa:settings")],
    ]
    return text, ui.inline_kb(rows)


def on_quota_menu(chat_id: int, message_id: int, cb_id: Optional[str] = None) -> None:
    if cb_id is not None:
        ui.answer_cb(cb_id)
    text, kb = _quota_menu_text_and_kb()
    ui.edit(chat_id, message_id, text, reply_markup=kb)


def on_quota_toggle(chat_id: int, message_id: int, cb_id: str) -> None:
    cur = bool((config.get().get("quotaMonitor") or {}).get("enabled", False))
    new_val = not cur
    config.update(lambda c: c.setdefault("quotaMonitor", {}).__setitem__("enabled", new_val))
    ui.answer_cb(cb_id, "已启用" if new_val else "已停用")
    text, kb = _quota_menu_text_and_kb()
    ui.edit(chat_id, message_id, text, reply_markup=kb)


def on_edit_quota_interval(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    states.set_state(chat_id, "oa_quota_interval")
    ui.edit(
        chat_id, message_id,
        "请输入配额监控间隔（秒，正整数，建议 ≥ 60）：",
        reply_markup=ui.inline_kb([[ui.btn("❌ 取消", "oa:quota")]]),
    )


def on_quota_interval_input(chat_id: int, text: str) -> None:
    try:
        v = int((text or "").strip())
    except ValueError:
        ui.send(chat_id, "❌ 非法数字，请重新输入：")
        return
    if v < 10:
        ui.send(chat_id, "❌ 间隔不能小于 10 秒，请重新输入（建议 ≥ 60s 避免被风控）：")
        return
    if v > 86400:
        ui.send(chat_id, "❌ 间隔不能超过 86400 秒（1 天），请重新输入：")
        return
    config.update(lambda c: c.setdefault("quotaMonitor", {}).__setitem__("intervalSeconds", v))
    states.pop_state(chat_id)
    ui.send(
        chat_id, f"✅ 配额监控间隔已更新为 <code>{v}s</code>",
        reply_markup=ui.inline_kb([[ui.btn("🏠 返回主菜单", "menu:main"), ui.btn("◀ 返回账户设置", "oa:settings")]]),
    )


def on_edit_quota_threshold(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    states.set_state(chat_id, "oa_quota_threshold")
    ui.edit(
        chat_id, message_id,
        "请输入禁用阈值（百分比，1-100）：\n"
        "<i>任一指标到达阈值即禁用该账号。常见值：90 / 95 / 98 / 99</i>",
        reply_markup=ui.inline_kb([[ui.btn("❌ 取消", "oa:quota")]]),
    )


def on_quota_threshold_input(chat_id: int, text: str) -> None:
    try:
        v = float((text or "").strip().rstrip("%"))
    except ValueError:
        ui.send(chat_id, "❌ 非法数字，请重新输入（如 98）：")
        return
    if v < 1 or v > 100:
        ui.send(chat_id, "❌ 阈值需在 1-100 之间，请重新输入：")
        return

    def _mutate(cfg: dict) -> None:
        qm = cfg.setdefault("quotaMonitor", {})
        qm["disableThresholdPercent"] = v
        qm["resumeThresholdPercent"] = v

    config.update(_mutate)
    states.pop_state(chat_id)
    ui.send(
        chat_id, f"✅ 配额禁用阈值已更新为 <code>{v:.0f}%</code>",
        reply_markup=ui.inline_kb([[ui.btn("🏠 返回主菜单", "menu:main"), ui.btn("◀ 返回账户设置", "oa:settings")]]),
    )


def _normalize_filter(value: str | None) -> str:
    key = (value or "").strip().lower()
    return key if key in _FILTER_LABELS else _FILTER_ALL


def _filter_account(acc: dict, filter_key: str) -> bool:
    filter_key = _normalize_filter(filter_key)
    if filter_key == _FILTER_AVAILABLE:
        return bool(acc.get("enabled", True)) and not acc.get("disabled_reason")
    if filter_key == _FILTER_QUOTA:
        return acc.get("disabled_reason") == "quota"
    if filter_key == _FILTER_INVALID:
        return acc.get("disabled_reason") == "auth_error"
    return True


def _page_callback(page: int, filter_key: str = _FILTER_ALL) -> str:
    page = max(1, int(page or 1))
    filter_key = _normalize_filter(filter_key)
    if filter_key == _FILTER_ALL:
        return f"oa:page:{page}"
    return f"oa:page:{page}:{filter_key}"


def _parse_page_filter(payload: str, default_page: int = 1, default_filter: str = _FILTER_ALL) -> tuple[int, str]:
    raw = (payload or "").strip()
    filter_key = _normalize_filter(default_filter)
    if not raw or raw == "noop":
        return default_page, filter_key

    if ":" in raw:
        left, _, maybe_filter = raw.rpartition(":")
        if maybe_filter in _FILTER_LABELS:
            filter_key = maybe_filter
            raw = left

    try:
        page = int(raw)
    except (TypeError, ValueError):
        page = default_page
    return max(1, page), filter_key


def _build_pagination_row(current: int, total_pages: int, filter_key: str = _FILTER_ALL) -> list[dict]:
    """构建翻页按钮行。

    • 总页数 ≤ 10：⬅ 上一页 / ➡ 下一页
    • 总页数 > 10：页码按钮组，当前页加 [] 标记
    """
    if total_pages <= 1:
        return []

    if total_pages <= 10:
        # 上一页 / 下一页 样式（首末页按钮保留但显示为禁用态）
        btns: list[dict] = []
        if current > 1:
            btns.append(ui.btn("⬅ 上一页", _page_callback(current - 1, filter_key)))
        else:
            btns.append(ui.btn("◁ 上一页", "oa:page:noop"))
        btns.append(ui.btn(f"{current}/{total_pages}", "oa:page:noop"))
        if current < total_pages:
            btns.append(ui.btn("➡ 下一页", _page_callback(current + 1, filter_key)))
        else:
            btns.append(ui.btn("下一页 ▷", "oa:page:noop"))
        return btns

    # 页码按钮组：显示当前页附近的窗口（最多 5 个）
    window = 2
    lo = max(1, current - window)
    hi = min(total_pages, current + window)
    # 补齐到至少 5 个
    if hi - lo + 1 < 5:
        if lo == 1:
            hi = min(total_pages, lo + 4)
        else:
            lo = max(1, hi - 4)

    page_btns: list[dict] = []
    if lo > 1:
        page_btns.append(ui.btn("1", _page_callback(1, filter_key)))
        if lo > 2:
            page_btns.append(ui.btn("…", "oa:page:noop"))
    for p in range(lo, hi + 1):
        if p == current:
            page_btns.append(ui.btn(f"[{p}]", "oa:page:noop"))
        else:
            page_btns.append(ui.btn(str(p), _page_callback(p, filter_key)))
    if hi < total_pages:
        if hi < total_pages - 1:
            page_btns.append(ui.btn("…", "oa:page:noop"))
        page_btns.append(ui.btn(str(total_pages), _page_callback(total_pages, filter_key)))
    return page_btns


def _split_short_page_filter(payload: str, default_page: int = 1, default_filter: str = _FILTER_ALL) -> tuple[str, int, str]:
    """解析带可选页码/过滤条件的 callback payload。

    新格式：<short>:<page>:<filter>；旧格式：<short>:<page> / <short>。
    """
    raw = (payload or "").strip()
    filter_key = _normalize_filter(default_filter)
    if ":" not in raw:
        return raw, default_page, filter_key

    head = raw
    maybe_head, _, maybe_filter = raw.rpartition(":")
    if maybe_filter in _FILTER_LABELS:
        filter_key = maybe_filter
        head = maybe_head

    if ":" not in head:
        return head, default_page, filter_key
    short, _, page_s = head.rpartition(":")
    try:
        page = int(page_s)
    except ValueError:
        return raw, default_page, filter_key
    if page < 1:
        page = default_page
    return short, page, filter_key


def _split_short_page(payload: str, default_page: int = 1) -> tuple[str, int]:
    short, page, _ = _split_short_page_filter(payload, default_page=default_page)
    return short, page


def _callback_payload(short: str, page: int, filter_key: str = _FILTER_ALL) -> str:
    try:
        p = int(page or 1)
    except (TypeError, ValueError):
        p = 1
    filter_key = _normalize_filter(filter_key)
    if filter_key == _FILTER_ALL:
        return f"{short}:{max(1, p)}"
    return f"{short}:{max(1, p)}:{filter_key}"


def _maybe_suffix_status_banner(text: str) -> str:
    """在文本底部追加 banner：上游故障 + 新版本可用（任一存在即拼到末尾）。"""
    extras: list[str] = []
    try:
        from ... import status_monitor
        line = status_monitor.get_active_summary()
        if line:
            extras.append(line)
    except Exception:
        pass
    try:
        from ... import update_checker
        line = update_checker.get_update_banner()
        if line:
            extras.append(line)
    except Exception:
        pass
    if not extras:
        return text
    return text + "\n\n" + "\n".join(extras)


def _list_text_and_kb(page: int = 1, filter_key: str = _FILTER_ALL) -> tuple[str, dict]:
    accounts_all = oauth_manager.list_accounts()
    filter_key = _normalize_filter(filter_key)
    # 页面只读本地缓存；缓存过期走后台刷新。完全缺 OpenAI usage cache
    # 的场景由 show/on_view 先切到进度面板，避免在这里卡住 callback。
    account_keys = _refreshable_account_keys_for_ui(accounts_all)
    if account_keys:
        # 如果缓存已经显示 >= quotaMonitor 阈值，立即收敛账号状态，
        # 不等 600s 后台监控下一轮。页面渲染只读缓存，不等待远端刷新。
        for ak in account_keys:
            try:
                oauth_manager.evaluate_and_toggle_by_cached_quota(ak)
            except Exception as exc:
                print(f"[oauth_menu] cached quota evaluate failed for {ak}: {exc}")
        accounts_all = oauth_manager.list_accounts()
        account_keys = _refreshable_account_keys_for_ui(accounts_all)
        _schedule_oauth_cache_refresh_for_ui(account_keys)
        openai_keys = [
            ak for ak in account_keys
            if oauth_manager.provider_of(ak) == "openai"
        ]
        if openai_keys:
            _schedule_openai_metadata_for_ui(openai_keys)
    total_all = len(accounts_all)
    normal = sum(1 for a in accounts_all if a.get("enabled", True) and not a.get("disabled_reason"))
    quota_disabled = sum(1 for a in accounts_all if a.get("disabled_reason") == "quota")
    user_disabled = sum(1 for a in accounts_all if a.get("disabled_reason") == "user")
    auth_err = sum(1 for a in accounts_all if a.get("disabled_reason") == "auth_error")
    # 只有总账户数超过一页时才需要筛选；一页内全部看得见，强制回到全部视图。
    show_filter_row = total_all > _PAGE_SIZE
    if not show_filter_row:
        filter_key = _FILTER_ALL
        accounts = list(accounts_all)
    else:
        accounts = [a for a in accounts_all if _filter_account(a, filter_key)]
    total = len(accounts)

    # 冷却统计：按 oauth:email 聚合；一个账号只要有任何模型处于冷却，就计数一次
    from ... import cooldown as _cd
    cd_keys_any: set[str] = set()
    cd_keys_perm: set[str] = set()
    for e in _cd.active_entries():
        ck = e.get("channel_key", "")
        if not ck.startswith("oauth:"):
            continue
        cd_keys_any.add(ck)
        if e.get("cooldown_until") == -1:
            cd_keys_perm.add(ck)
    cooling_only = len(cd_keys_any - cd_keys_perm)
    permanent = len(cd_keys_perm)

    import math
    total_pages = max(1, math.ceil(total / _PAGE_SIZE)) if total else 1
    page = max(1, min(page, total_pages))
    page_info = f" | 第 {page}/{total_pages} 页" if total_pages > 1 else ""

    summary = (
        f"🔐 <b>OAuth 账户管理</b>\n"
        f"共 {total_all} 个账户 | 正常 {normal}"
        + (f" | 配额 {quota_disabled}" if quota_disabled else "")
        + (f" | 用户禁用 {user_disabled}" if user_disabled else "")
        + (f" | 认证失败 {auth_err}" if auth_err else "")
        + (f" | ⚠ 冷却 {cooling_only}" if cooling_only else "")
        + (f" | 🔴 永久 {permanent}" if permanent else "")
        + page_info
    )
    if filter_key != _FILTER_ALL:
        summary += f"\n当前过滤: <b>{_FILTER_LABELS.get(filter_key, '全部')}</b>"

    if not accounts:
        empty_hint = "暂无账户，点击下方「➕ 新增账户」添加。" if not accounts_all else "当前过滤条件下暂无账户。"
        text = summary + f"\n\n{empty_hint}"
    else:
        start = (page - 1) * _PAGE_SIZE
        end = min(start + _PAGE_SIZE, total)
        page_accounts = accounts[start:end]
        lines = [summary, ""]
        for i, acc in enumerate(page_accounts, start=start + 1):
            # 序号 + 账号多行块；序号前缀追加到块的第一行
            block = _format_account_block(acc)
            first, _, rest = block.partition("\n")
            lines.append(f"{i}. {first}")
            if rest:
                lines.append(rest)
            lines.append("")
        text = "\n".join(lines).rstrip()

    # ── 按钮区 ──
    rows: list[list[dict]] = []

    # 当前页账户按钮（每行 2 个，图标在邮箱前面）
    start = (page - 1) * _PAGE_SIZE
    end = min(start + _PAGE_SIZE, total)
    page_accs = accounts[start:end]
    for idx in range(0, len(page_accs), 2):
        row_btns: list[dict] = []
        for offset, acc in enumerate(page_accs[idx:idx + 2], start=idx):
            email = acc.get("email", "?")
            ak = _account_key(acc)
            short = ui.register_code(ak)
            prov = oauth_manager.provider_of(acc)
            tag = "🅾" if prov == "openai" else ("🅰" if prov == "claude" else "✉")
            num = start + offset + 1
            row_btns.append(ui.btn(f"{num}. {tag} {email}", f"oa:view:{_callback_payload(short, page, filter_key)}"))
        rows.append(row_btns)

    # 翻页/排序：当前列表只有一页时不显示。
    if total_pages > 1:
        pag_row = _build_pagination_row(page, total_pages, filter_key)
        if accounts_all:
            pag_row.append(ui.btn("↕ 排序", f"oa:sort:{page}:{filter_key}"))
        if pag_row:
            rows.append(pag_row)

    # 过滤按钮：总账户数超过一页时才显示。
    if show_filter_row:
        rows.append([
            ui.btn(f"全部{'√' if filter_key == _FILTER_ALL else ''}", _page_callback(1, _FILTER_ALL)),
            ui.btn(f"可用{'√' if filter_key == _FILTER_AVAILABLE else ''}", _page_callback(1, _FILTER_AVAILABLE)),
            ui.btn(f"限额{'√' if filter_key == _FILTER_QUOTA else ''}", _page_callback(1, _FILTER_QUOTA)),
            ui.btn(f"失效{'√' if filter_key == _FILTER_INVALID else ''}", _page_callback(1, _FILTER_INVALID)),
        ])

    # 操作按钮（每页都有）
    refresh_cb = f"oa:refresh_all:{page}" if filter_key == _FILTER_ALL else f"oa:refresh_all:{page}:{filter_key}"
    rows.append([
        ui.btn("➕ 新增账户", "oa:add"),
        ui.btn("🧨 移除失效", "oa:invalid:list"),
        ui.btn("🔄 刷新用量/重置卡", refresh_cb),
    ])
    rows.append([
        ui.btn("⚙️ 账户设置", "oa:settings"),
        ui.btn("◀ 返回主菜单", "menu:main"),
    ])
    return ui.truncate(_maybe_suffix_status_banner(text)), ui.inline_kb(rows)


def show(chat_id: int, message_id: int, cb_id: Optional[str] = None, page: int = 1, filter_key: str = _FILTER_ALL) -> None:
    if cb_id is not None:
        ui.answer_cb(cb_id)
    accounts = oauth_manager.list_accounts()
    initial_keys = _initial_update_keys_for_ui(accounts)
    if initial_keys:
        # 初次没有 usage cache 时，先把当前面板切成进度面板，后台逐账号更新；
        # 这样 callback 立刻返回，不会因为账号多/接口慢导致 TG 超时。
        keys = _refreshable_account_keys_for_ui(accounts) or initial_keys
        initial_items = [
            _progress_account_block(ak, idx, "  等待更新...")
            for idx, ak in enumerate(keys, 1)
        ]
        ui.edit(chat_id, message_id, _build_oauth_update_panel(initial_items))
        _start_oauth_update_panel(
            chat_id, message_id, keys, page=page, filter_key=filter_key,
            final_target_message_id=message_id, background=True,
        )
        return
    text, kb = _list_text_and_kb(page=page, filter_key=filter_key)
    ui.edit(chat_id, message_id, text, reply_markup=kb)


def send_new(chat_id: int, page: int = 1, filter_key: str = _FILTER_ALL) -> None:
    accounts = oauth_manager.list_accounts()
    initial_keys = _initial_update_keys_for_ui(accounts)
    if initial_keys:
        keys = _refreshable_account_keys_for_ui(accounts) or initial_keys
        initial_items = [
            _progress_account_block(ak, idx, "  等待更新...")
            for idx, ak in enumerate(keys, 1)
        ]
        resp = ui.send(chat_id, _build_oauth_update_panel(initial_items))
        mid = (resp.get("result") or {}).get("message_id") if resp and resp.get("ok") else None
        if mid is not None:
            _start_oauth_update_panel(
                chat_id, int(mid), keys, page=page, filter_key=filter_key,
                final_target_message_id=int(mid), background=True,
            )
            return
    text, kb = _list_text_and_kb(page=page, filter_key=filter_key)
    ui.send(chat_id, text, reply_markup=kb)


# ─── 账户排序 ─────────────────────────────────────────────────────

def _all_account_keys() -> list[str]:
    return [_account_key(acc) for acc in oauth_manager.list_accounts()]


def _split_number_rows(n: int, max_cols: int = 6) -> list[list[int]]:
    if n <= 0:
        return []
    rows_count = math.ceil(n / max_cols)
    base = n // rows_count
    extra = n % rows_count
    rows: list[list[int]] = []
    cur = 1
    for r in range(rows_count):
        size = base + (1 if r < extra else 0)
        rows.append(list(range(cur, cur + size)))
        cur += size
    return rows


def _sort_state_data(chat_id: int) -> Optional[dict]:
    st = states.get_state(chat_id)
    if not st or st.get("action") != "oa_sort":
        return None
    return st.get("data") or {}


def _sort_selection_set(data: dict) -> set[int]:
    return {int(x) for x in (data.get("selected") or [])}


def _set_sort_state(chat_id: int, draft: list[str], *, page: int = 1,
                    filter_key: str = _FILTER_ALL,
                    selected: Optional[set[int]] = None) -> None:
    states.set_state(chat_id, "oa_sort", {
        "draft": list(draft),
        "page": max(1, int(page or 1)),
        "filter_key": _normalize_filter(filter_key),
        "selected": sorted(selected or []),
    })


def _sort_item_line(idx: int, account_key: str) -> str:
    acc = oauth_manager.get_account(account_key)
    if acc is None:
        return f"{idx}. <code>{ui.escape_html(account_key)}</code> ⚠ 已不存在"
    email = str(acc.get("email") or oauth_manager.account_key_to_email(account_key) or "?")
    prov = oauth_manager.provider_of(acc)
    tag = "🅾" if prov == "openai" else ("🅰" if prov == "claude" else "✉")
    status = "enabled" if acc.get("enabled", True) and not acc.get("disabled_reason") else (acc.get("disabled_reason") or "disabled")
    suffix = _openai_workspace_label(acc, force=True) if prov == "openai" else ""
    suffix_text = f" · {suffix}" if suffix else ""
    return (
        f"{idx}. {tag} <code>{ui.escape_html(email)}</code>{suffix_text} "
        f"<code>{ui.escape_html(status)}</code>"
    )


def _sort_text_and_kb(draft: list[str], selected: set[int], page: int, filter_key: str) -> tuple[str, dict]:
    filter_key = _normalize_filter(filter_key)
    lines = [
        "↕ <b>OAuth 账户排序</b>",
        "",
        "当前账户顺序:",
    ]
    if not draft:
        lines.append("<i>当前没有 OAuth 账户。</i>")
    else:
        lines.extend(_sort_item_line(i, ak) for i, ak in enumerate(draft, start=1))
    lines.extend([
        "",
        "调整方式:",
        "先点下方序号勾选账户，再点置顶/置底/上移/下移。",
        "调整完成后记得点保存排序。",
        "返回时保留过滤条件和页码。",
    ])

    rows: list[list[dict]] = []
    for nums in _split_number_rows(len(draft)):
        row = []
        for n in nums:
            label = f"{n} ✅" if n in selected else str(n)
            row.append(ui.btn(label, f"oa:sort_sel:{n}"))
        rows.append(row)
    if draft:
        rows.append([
            ui.btn("🔝 置顶", "oa:sort_mv:top"),
            ui.btn("🔚 置底", "oa:sort_mv:bottom"),
            ui.btn("⬆ 上移", "oa:sort_mv:up"),
            ui.btn("⬇ 下移", "oa:sort_mv:down"),
        ])
    rows.append([ui.btn("还原", "oa:sort_reset"), ui.btn("保存排序", "oa:sort_save")])
    rows.append([
        ui.btn("◀ 返回 OAuth 列表", _page_callback(page, filter_key)),
        ui.btn("取消", "oa:sort_cancel"),
    ])
    return ui.truncate("\n".join(lines)), ui.inline_kb(rows)


def _show_sort(chat_id: int, message_id: int, cb_id: Optional[str] = None) -> None:
    data = _sort_state_data(chat_id)
    if cb_id is not None:
        ui.answer_cb(cb_id)
    if not data:
        show(chat_id, message_id)
        return
    draft = list(data.get("draft") or [])
    page = max(1, int(data.get("page") or 1))
    filter_key = _normalize_filter(data.get("filter_key") or _FILTER_ALL)
    selected = _sort_selection_set(data)
    text, kb = _sort_text_and_kb(draft, selected, page, filter_key)
    ui.edit(chat_id, message_id, text, reply_markup=kb)


def on_sort_start(chat_id: int, message_id: int, cb_id: str,
                  page: int = 1, filter_key: str = _FILTER_ALL) -> None:
    draft = _all_account_keys()
    if not draft:
        ui.answer_cb(cb_id, "当前没有账户")
        return
    _set_sort_state(chat_id, draft, page=page, filter_key=filter_key)
    _show_sort(chat_id, message_id, cb_id)


def on_sort_select(chat_id: int, message_id: int, cb_id: str, idx_str: str) -> None:
    data = _sort_state_data(chat_id)
    if not data:
        ui.answer_cb(cb_id, "会话已失效")
        show(chat_id, message_id)
        return
    draft = list(data.get("draft") or [])
    try:
        idx = int(idx_str)
    except ValueError:
        ui.answer_cb(cb_id, "无效序号")
        return
    if idx < 1 or idx > len(draft):
        ui.answer_cb(cb_id, "序号越界")
        return
    selected = _sort_selection_set(data)
    if idx in selected:
        selected.remove(idx)
    else:
        selected.add(idx)
    _set_sort_state(
        chat_id, draft,
        page=data.get("page") or 1,
        filter_key=data.get("filter_key") or _FILTER_ALL,
        selected=selected,
    )
    _show_sort(chat_id, message_id, cb_id)


def _move_top(draft: list[str], selected: set[int]) -> list[str]:
    idxs = [i - 1 for i in sorted(selected)]
    chosen = [draft[i] for i in idxs]
    rest = [x for i, x in enumerate(draft) if i not in idxs]
    return chosen + rest


def _move_bottom(draft: list[str], selected: set[int]) -> list[str]:
    idxs = [i - 1 for i in sorted(selected)]
    chosen = [draft[i] for i in idxs]
    rest = [x for i, x in enumerate(draft) if i not in idxs]
    return rest + chosen


def _move_up(draft: list[str], selected: set[int]) -> tuple[list[str], set[int]]:
    arr = list(draft)
    sel = {i - 1 for i in selected}
    for i in range(1, len(arr)):
        if i in sel and (i - 1) not in sel:
            arr[i - 1], arr[i] = arr[i], arr[i - 1]
            sel.remove(i)
            sel.add(i - 1)
    return arr, {i + 1 for i in sel}


def _move_down(draft: list[str], selected: set[int]) -> tuple[list[str], set[int]]:
    arr = list(draft)
    sel = {i - 1 for i in selected}
    for i in range(len(arr) - 2, -1, -1):
        if i in sel and (i + 1) not in sel:
            arr[i + 1], arr[i] = arr[i], arr[i + 1]
            sel.remove(i)
            sel.add(i + 1)
    return arr, {i + 1 for i in sel}


def on_sort_move(chat_id: int, message_id: int, cb_id: str, op: str) -> None:
    data = _sort_state_data(chat_id)
    if not data:
        ui.answer_cb(cb_id, "会话已失效")
        show(chat_id, message_id)
        return
    draft = list(data.get("draft") or [])
    selected = _sort_selection_set(data)
    if not selected:
        ui.answer_cb(cb_id, "请先勾选序号")
        return
    if op == "top":
        new_draft = _move_top(draft, selected)
        new_sel = set(range(1, len(selected) + 1))
    elif op == "bottom":
        new_draft = _move_bottom(draft, selected)
        start = len(new_draft) - len(selected) + 1
        new_sel = set(range(start, len(new_draft) + 1))
    elif op == "up":
        new_draft, new_sel = _move_up(draft, selected)
    elif op == "down":
        new_draft, new_sel = _move_down(draft, selected)
    else:
        ui.answer_cb(cb_id, "未知移动操作")
        return
    _set_sort_state(
        chat_id, new_draft,
        page=data.get("page") or 1,
        filter_key=data.get("filter_key") or _FILTER_ALL,
        selected=new_sel,
    )
    _show_sort(chat_id, message_id, cb_id)


def on_sort_reset(chat_id: int, message_id: int, cb_id: str) -> None:
    data = _sort_state_data(chat_id) or {}
    page = max(1, int(data.get("page") or 1))
    filter_key = _normalize_filter(data.get("filter_key") or _FILTER_ALL)
    _set_sort_state(chat_id, _all_account_keys(), page=page, filter_key=filter_key)
    ui.answer_cb(cb_id, "已还原当前保存顺序")
    _show_sort(chat_id, message_id)


def _save_account_order(draft: list[str]) -> None:
    order = {ak: i for i, ak in enumerate(draft)}

    def _mutate(cfg):
        accounts = list(cfg.get("oauthAccounts") or [])
        ordered = [a for a in accounts if _account_key(a) in order]
        ordered.sort(key=lambda a: order.get(_account_key(a), 10**9))
        rest = [a for a in accounts if _account_key(a) not in order]
        cfg["oauthAccounts"] = ordered + rest

    config.update(_mutate)


def on_sort_save(chat_id: int, message_id: int, cb_id: str) -> None:
    data = _sort_state_data(chat_id)
    if not data:
        ui.answer_cb(cb_id, "会话已失效")
        show(chat_id, message_id)
        return
    draft = list(data.get("draft") or [])
    page = max(1, int(data.get("page") or 1))
    filter_key = _normalize_filter(data.get("filter_key") or _FILTER_ALL)
    _save_account_order(draft)
    states.pop_state(chat_id)
    ui.answer_cb(cb_id, "已保存")
    ui.edit(
        chat_id, message_id,
        "✅ 已保存 OAuth 账户排序。",
        reply_markup=ui.inline_kb([
            [ui.btn("继续排序", f"oa:sort:{page}:{filter_key}"), ui.btn("返回 OAuth 列表", _page_callback(page, filter_key))],
            [ui.btn("🏠 主菜单", "menu:main")],
        ]),
    )


def on_sort_cancel(chat_id: int, message_id: int, cb_id: str) -> None:
    data = _sort_state_data(chat_id) or {}
    page = max(1, int(data.get("page") or 1))
    filter_key = _normalize_filter(data.get("filter_key") or _FILTER_ALL)
    states.pop_state(chat_id)
    show(chat_id, message_id, cb_id, page=page, filter_key=filter_key)


# ─── 账户详情 ─────────────────────────────────────────────────────

def _format_month_stats_block(account_key: str) -> str:
    """本月使用统计：总体 + 按模型展开。无数据时返回空字符串。"""
    ck = f"oauth:{account_key}"
    since_ts = _this_month_start_ts()
    try:
        overall = log_db.tokens_for_channel(ck, since_ts=since_ts)
    except Exception:
        return ""
    if not overall or overall.get("total", 0) <= 0:
        return ""
    try:
        by_model = log_db.channel_model_stats(ck, since_ts=since_ts)
    except Exception:
        by_model = []

    total = overall["total"]
    succ = overall["success_count"]
    err = overall["error_count"]
    inp_prompt = ui.prompt_total(overall["input"], overall["cache_creation"], overall["cache_read"])
    out_tok = overall["output"]
    token_line = f"↑ {ui.fmt_tokens(inp_prompt)} · ↓ {ui.fmt_tokens(out_tok)}"
    if (overall.get("cache_read") or 0) > 0:
        token_line += f" · {ui.fmt_cache_phrase(overall['cache_read'], inp_prompt)}"

    lines = [
        "",
        "<b>⚡ 本月使用统计</b>",
        f"总体: {total} 次 · ✅ {succ} · ❌ {err}",
        token_line,
        f"平均 {ui.fmt_tps(overall.get('avg_tps'))} · "
        f"峰值 {ui.fmt_tps(overall.get('max_tps'))} · "
        f"最低 {ui.fmt_tps(overall.get('min_tps'))}",
    ]
    if by_model:
        lines.append("")
        lines.append("按模型:")
        for ms in by_model:
            model = ui.escape_html(ms.get("final_model") or "?")
            m_prompt = ui.prompt_total(ms["input"], ms["cache_creation"], ms["cache_read"])
            model_line = (
                f"    {ms['total']} 次 · ✅ {ms['success_count']} · ❌ {ms['error_count']}"
                f" · ↑ {ui.fmt_tokens(m_prompt)} · ↓ {ui.fmt_tokens(ms['output'])}"
            )
            if (ms.get("cache_read") or 0) > 0:
                model_line += f" · {ui.fmt_cache_phrase(ms['cache_read'], m_prompt)}"
            lines.append(f"  • <code>{model}</code>")
            lines.append(model_line)
            if ms.get("avg_tps") is not None:
                lines.append(
                    f"    ⚡ 平均 {ui.fmt_tps(ms.get('avg_tps'))} · "
                    f"峰值 {ui.fmt_tps(ms.get('max_tps'))} · "
                    f"最低 {ui.fmt_tps(ms.get('min_tps'))}"
                )
    return "\n".join(lines)


def _detail_text_and_kb(account_key: str, page: int = 1, filter_key: str = _FILTER_ALL,
                        *, refresh_quota: bool = True,
                        reset_credit_count_override: int | None = None) -> tuple[Optional[str], Optional[dict]]:
    acc = oauth_manager.get_account(account_key)
    if acc is None:
        return None, None
    email = acc.get("email", "?")

    if refresh_quota and _should_refresh_account_for_ui(acc):
        try:
            oauth_manager.evaluate_and_toggle_by_cached_quota(account_key)
        except Exception as exc:
            print(f"[oauth_menu] cached quota evaluate failed for {account_key}: {exc}")
        acc = oauth_manager.get_account(account_key) or acc
        if _should_refresh_account_for_ui(acc):
            _schedule_oauth_cache_refresh_for_ui(account_key)
    if oauth_manager.provider_of(acc) == "openai":
        _schedule_openai_metadata_for_ui(account_key)
        acc = oauth_manager.get_account(account_key) or acc

    icon = _status_icon(acc)
    reason = acc.get("disabled_reason") or "—"
    prov = oauth_manager.provider_of(acc)
    quota_row = state_db.quota_load(account_key)
    reset_credit_cached_count = _openai_reset_credit_count_from_row(quota_row) if prov == "openai" else None
    reset_credit_effective_count = (
        reset_credit_count_override
        if reset_credit_count_override is not None else reset_credit_cached_count
    )
    reset_credit_details = _openai_reset_credit_details_from_row(quota_row) if prov == "openai" else None
    provider_line = ""
    if prov == "openai":
        plan = acc.get("plan_type") or "?"
        workspace = _openai_workspace_label(acc, force=True)
        ws_suffix = f"（{ui.escape_html(workspace)}）" if workspace and _openai_same_email_count(acc) > 1 else ""
        provider_line = f"🏷️ 套餐: <code>{ui.escape_html(plan)}{ws_suffix}</code>\n"
        detail_count = _openai_reset_credit_count_from_details(reset_credit_details)
        if reset_credit_effective_count is not None:
            reset_label = f"{reset_credit_effective_count} 次"
        elif detail_count is not None:
            reset_label = f"{detail_count} 次"
        else:
            reset_label = _openai_reset_credit_label_from_row(quota_row, show_zero=True)
        provider_line += f"♻️ 官方重置次数: <code>{ui.escape_html(reset_label)}</code>\n"
        sub_exp = acc.get("subscription_expires_at") or ""
        if sub_exp:
            provider_line += f"📅 到期: <code>{_fmt_time_full(sub_exp)}</code>\n"
    elif prov == "claude":
        cl_label = oauth_manager.claude_plan_label(acc)
        provider_line = f"🏷️ 套餐: <code>{ui.escape_html(cl_label or '?')}</code>\n"
        sub_status = acc.get("subscription_status") or ""
        billing = acc.get("billing_type") or ""
        sub_created = acc.get("subscription_created_at") or ""
        if sub_status or billing:
            sub_parts = [s for s in (sub_status, billing) if s]
            provider_line += f"📋 订阅: <code>{ui.escape_html(' · '.join(sub_parts))}</code>\n"
        if sub_created:
            provider_line += f"📅 开始: <code>{_format_bjt(sub_created)}</code>\n"
    else:
        provider_line = ""
    max_cc = int(acc.get("maxConcurrent", 0) or 0)
    max_cc_label = str(max_cc) if max_cc > 0 else "默认"
    prov_icon = "🅾️ OpenAI" if prov == "openai" else ("🅰️ Claude" if prov == "claude" else prov)
    text = (
        f"{icon} <b>{ui.escape_html(email)}</b> {prov_icon}\n\n"
        f"状态: <code>{ui.escape_html('enabled' if acc.get('enabled', True) and not acc.get('disabled_reason') else reason)}</code>\n"
        f"{provider_line}"
        f"⚡ 并发上限: <code>{max_cc_label}</code>\n"
        f"⏳ Token: <code>{_fmt_time_full(acc.get('expired'))}</code>\n"
        f"🔄 刷新: <code>{_format_bjt(acc.get('last_refresh'))}</code>\n\n"
        f"<b>📊 使用量</b>\n{_format_usage_block(account_key)}"
    )
    reset_cards_block = (
        _format_reset_credit_cards_block(
            reset_credit_details,
            cached_count=reset_credit_effective_count,
            available_count_override=reset_credit_effective_count,
        ) if prov == "openai" else ""
    )
    if reset_cards_block:
        text += "\n\n" + reset_cards_block

    month_block = _format_month_stats_block(account_key)
    if month_block:
        text += "\n" + month_block

    short = ui.register_code(account_key)
    enabled = acc.get("enabled", True) and not acc.get("disabled_reason")
    toggle_label = "🚫 禁用" if enabled else "✅ 启用"

    # 显示当前模型的冷却状态
    ck = f"oauth:{account_key}"
    cd_models = [e for e in cooldown.active_entries() if e["channel_key"] == ck]
    if cd_models:
        text += "\n\n<b>⚠ 冷却中的模型：</b>\n"
        now_ms = int(__import__('time').time() * 1000)
        for e in cd_models:
            mdl = ui.escape_html(e["model"])
            cu = e.get("cooldown_until")
            if cu == -1:
                text += f"  🔴 <code>{mdl}</code> — 永久冻结"
            else:
                rem = max(0, (cu - now_ms) // 1000)
                text += f"  🟠 <code>{mdl}</code> — 剩 {rem}s"
            text += f" (累计失败 {e['error_count']} 次)\n"

    payload = _callback_payload(short, page, filter_key)
    rows = [
        [ui.btn("🔄 刷新 Token", f"oa:refresh_token:{payload}"),
         ui.btn("📊 刷新用量/重置卡",   f"oa:refresh_usage:{payload}")],
    ]
    if prov == "openai":
        rows.append([
            ui.btn("⚡ 并发上限", f"oa:emax:{payload}"),
            ui.btn("♻️ 重置次数", f"oa:reset_quota_ask:{payload}"),
        ])
    elif acc.get("disabled_reason") == "quota":
        rows.append([ui.btn("♻️ 清本地配额禁用", f"oa:reset_quota:{payload}")])
    rows += [
        [ui.btn("🧹 清模型错误", f"oa:clear_errors:{payload}"),
         ui.btn("🔗 清亲和绑定", f"oa:clear_affinity:{payload}")],
    ]
    if prov != "openai":
        rows.append([ui.btn("⚡ 并发上限", f"oa:emax:{payload}")])
    rows += [
        [ui.btn(toggle_label,     f"oa:toggle:{payload}"),
         ui.btn("🗑 删除",         f"oa:delete_ask:{payload}")],
        [ui.btn("◀ 返回 OAuth 列表", _page_callback(max(1, int(page or 1)), filter_key))],
    ]
    return ui.truncate(text), ui.inline_kb(rows)


def on_view(chat_id: int, message_id: int, cb_id: str, short: str, page: int = 1, filter_key: str = _FILTER_ALL) -> None:
    ak = _resolve_to_account_key(ui.resolve_code(short))
    if ak is None:
        ui.answer_cb(cb_id, "短码已失效，请返回重试")
        show(chat_id, message_id, page=page, filter_key=filter_key)
        return
    ui.answer_cb(cb_id)
    acc = oauth_manager.get_account(ak)
    if acc and _should_refresh_account_for_ui(acc) and oauth_manager.provider_of(acc) == "openai" and _needs_initial_oauth_cache_sync_for_ui(ak):
        ui.edit(chat_id, message_id, _build_oauth_update_panel([
            _progress_account_block(ak, 1, "  等待更新...")
        ]))
        _start_oauth_update_panel(
            chat_id, message_id, [ak], page=page, filter_key=filter_key,
            final_target_message_id=message_id, final_detail_account_key=ak, background=True,
        )
        return
    text, kb = _detail_text_and_kb(ak, page=page, filter_key=filter_key)
    if text is None:
        _, email = _split_ak(ak)
        ui.edit(chat_id, message_id,
                f"⚠ 账户 <code>{ui.escape_html(email)}</code> 已不存在",
                reply_markup=ui.inline_kb([[ui.btn("◀ 返回列表", _page_callback(max(1, int(page or 1)), filter_key))]]))
        return
    ui.edit(chat_id, message_id, text, reply_markup=kb)


# ─── 刷新 Token ──────────────────────────────────────────────────

def on_refresh_token(chat_id: int, message_id: int, cb_id: str, short: str, page: int = 1, filter_key: str = _FILTER_ALL) -> None:
    ak = _resolve_to_account_key(ui.resolve_code(short))
    if ak is None:
        ui.answer_cb(cb_id, "短码已失效")
        return
    ui.answer_cb(cb_id, "刷新中...")

    provider = oauth_manager.provider_of(ak)
    result = _run_sync(oauth_manager.force_refresh(ak))
    if isinstance(result, Exception):
        ui.send(chat_id, _oauth_error_html(
            result, provider=provider, operation="refresh_token",
        ))
        return

    email = _account_email(ak)
    usage_result = _fetch_and_save_usage_sync(ak, email=email)
    if isinstance(usage_result, Exception):
        print(f"[oauth_menu] usage fetch after token refresh failed for {ak}: {usage_result}")
    else:
        _evaluate_quota_action(ak, usage_result)

    text, kb = _detail_text_and_kb(ak, page=page, filter_key=filter_key)
    if text:
        ui.edit(chat_id, message_id,
                "✅ Token 已刷新\n\n" + text,
                reply_markup=kb)


# ─── 刷新用量 / 重置卡 ─────────────────────────────────────────────

def on_refresh_usage(chat_id: int, message_id: int, cb_id: str, short: str, page: int = 1, filter_key: str = _FILTER_ALL) -> None:
    ak = _resolve_to_account_key(ui.resolve_code(short))
    if ak is None:
        ui.answer_cb(cb_id, "短码已失效")
        return
    email = _account_email(ak)
    provider = oauth_manager.provider_of(ak)
    if provider == "openai":
        ui.answer_cb(cb_id, "拉取 OpenAI 用量/重置卡...")
    else:
        ui.answer_cb(cb_id, "拉取中...")

    refresh_result = _fetch_and_save_usage_result_sync(ak, email=email)
    if refresh_result.get("error") is not None:
        err = refresh_result["error"]
        ui.send(chat_id, _oauth_error_html(
            err, provider=provider, operation="fetch_usage",
        ))
        return
    usage_result = refresh_result.get("usage")
    if not isinstance(usage_result, dict):
        ui.send(chat_id, "❌ 用量刷新失败：上游未返回有效数据")
        return
    quota_action = _evaluate_quota_action(ak, usage_result)
    metadata_action = None
    if provider == "openai":
        metadata_action = _run_sync(oauth_manager.ensure_openai_metadata_fresh(
            ak, force=True, min_interval_seconds=0, timeout_s=5.0,
        ))

    text, kb = _detail_text_and_kb(ak, page=page, filter_key=filter_key, refresh_quota=False)
    if not text:
        return
    if provider == "openai":
        head = "✅ 已更新用量（wham/usage）"
        reset_credit_count = _openai_reset_credit_count_from_usage(usage_result)
        if reset_credit_count is not None:
            head += f"\n♻️ 重置次数: <code>{reset_credit_count}</code>"
        if refresh_result.get("reset_credit_error") is not None:
            head += "\n⚠️ 重置次数刷新失败，已更新用量；稍后可再点「刷新用量/重置卡」。"
        if isinstance(metadata_action, dict) and metadata_action.get("action") == "updated":
            fields = metadata_action.get("fields") or {}
            if fields.get("plan_type"):
                head += f"\n🏷 套餐信息已刷新: <code>{ui.escape_html(fields.get('plan_type'))}</code>"
        if quota_action and quota_action.get("action") == "disabled":
            hit = " / ".join(quota_action.get("hit_windows") or []) or "?"
            head += f"\n🔒 已自动标记为配额禁用（超限: <code>{ui.escape_html(hit)}</code>）"
        elif quota_action and quota_action.get("action") == "still_over_quota":
            hit = " / ".join(quota_action.get("hit_windows") or []) or "?"
            head += f"\n⚠ 仍处于配额禁用（超限: <code>{ui.escape_html(hit)}</code>）"
        elif quota_action and quota_action.get("action") == "resumed":
            head += "\n♻ 额度已恢复，已自动解除配额禁用"
        ui.edit(chat_id, message_id, head + "\n\n" + text, reply_markup=kb)
    else:
        ui.edit(chat_id, message_id, text, reply_markup=kb)


# ─── 清错误 / 清亲和 ─────────────────────────────────────────────

def on_clear_errors(chat_id: int, message_id: int, cb_id: str, short: str, page: int = 1, filter_key: str = _FILTER_ALL) -> None:
    ak = _resolve_to_account_key(ui.resolve_code(short))
    if ak is None:
        ui.answer_cb(cb_id, "短码已失效")
        return
    cooldown.clear(f"oauth:{ak}", model=None)
    ui.answer_cb(cb_id, "已清除该账号的所有模型冷却")
    text, kb = _detail_text_and_kb(ak, page=page, filter_key=filter_key)
    if text:
        ui.edit(chat_id, message_id, text, reply_markup=kb)


def on_reset_quota_ask(chat_id: int, message_id: int, cb_id: str, short: str,
                       page: int = 1, filter_key: str = _FILTER_ALL) -> None:
    ak = _resolve_to_account_key(ui.resolve_code(short))
    if ak is None:
        ui.answer_cb(cb_id)
        return
    if oauth_manager.provider_of(ak) != "openai":
        ui.answer_cb(cb_id)
        return

    email = _account_email(ak)
    usage_result = _fetch_and_save_usage_sync(ak, email=email)
    if isinstance(usage_result, Exception):
        ui.answer_cb(cb_id)
        return
    _evaluate_quota_action(ak, usage_result)
    count = _openai_reset_credit_count_from_usage(usage_result)
    payload = _callback_payload(short, page, filter_key)
    if count is None or count <= 0:
        ui.answer_cb(cb_id)
        return
    else:
        unit = "次" if count == 1 else "次"
        body = (
            "♻️ <b>OpenAI 官方额度重置说明</b>\n\n"
            f"当前可用官方重置次数: <code>{count}</code> {unit}\n\n"
            "这一步 <b>不会消耗</b> 重置次数，只是进入最终确认页。\n\n"
            "请确认你理解：\n"
            "• 这是 OpenAI/Codex 官方 banked reset credit，不是 Parrot 本地清缓存。\n"
            "• 真正执行后会立刻向 OpenAI 发送 consume 请求，并消耗 <code>1</code> 次官方重置。\n"
            "• 该操作不可撤销；如果 OpenAI 判断没有可重置窗口，可能返回 no_credit / nothing_to_reset。\n"
            "• 成功后 Parrot 会立即刷新最新额度；只有 fresh usage 证明低于阈值，才会解除 quota 禁用并清理模型 cooldown。\n"
            "• 如果刷新失败或额度仍超限，本地 quota 限制会保留，避免误放行。\n"
            "• 不会清除 user 手动禁用或 auth_error；这些必须手动启用或重新登录。"
        )
        # OpenAI 官方要求同一次逻辑 reset 重试时复用同一个 idempotency key。
        # 先绑定到“最终确认页”按钮，最终执行按钮继续复用，避免 TG 重投/双击消耗多次。
        reset_idem = str(uuid.uuid4())
        confirm_short = ui.register_code(f"{ak}|{reset_idem}|confirm")
        confirm_payload = _callback_payload(confirm_short, page, filter_key)
        rows = [
            [ui.btn("我已理解，进入最终确认（不消耗）", f"oa:reset_quota_confirm:{confirm_payload}")],
            [ui.btn("❌ 取消", f"oa:view:{payload}")],
        ]
    ui.edit(chat_id, message_id, body, reply_markup=ui.inline_kb(rows))


def _parse_openai_reset_payload(short: str) -> tuple[str | None, str | None, str | None]:
    resolved = ui.resolve_code(short)
    if not isinstance(resolved, str):
        return None, None, None
    parts = resolved.split("|")
    ak = _resolve_to_account_key(parts[0])
    reset_idem = parts[1] if len(parts) >= 2 and parts[1] else None
    stage = parts[2] if len(parts) >= 3 and parts[2] else None
    return ak, reset_idem, stage


def on_reset_quota_confirm(chat_id: int, message_id: int, cb_id: str, short: str,
                           page: int = 1, filter_key: str = _FILTER_ALL) -> None:
    ak, reset_idem, stage = _parse_openai_reset_payload(short)
    if ak is None or not reset_idem or stage != "confirm":
        ui.answer_cb(cb_id, "确认信息已失效，请重新进入")
        return
    if oauth_manager.provider_of(ak) != "openai":
        ui.answer_cb(cb_id, "仅 OpenAI 有官方重置次数")
        return

    ui.answer_cb(cb_id, "请做最终确认")
    email = _account_email(ak)
    final_short = ui.register_code(f"{ak}|{reset_idem}|execute")
    final_payload = _callback_payload(final_short, page, filter_key)
    cancel_payload = _callback_payload(ui.register_code(ak), page, filter_key)
    reset_label = _openai_reset_credit_label_from_row(state_db.quota_load(ak), show_zero=True)
    body = (
        "🚨 <b>最终确认：消耗 1 次 OpenAI 官方重置</b>\n\n"
        f"账号: <code>{ui.escape_html(email or ak)}</code>\n"
        f"当前可用官方重置次数: <code>{ui.escape_html(reset_label)}</code>\n\n"
        "点击下面的最终确认后，会立即调用 OpenAI 官方接口：\n"
        "<code>rate-limit-reset-credits/consume</code>\n\n"
        "结果与影响：\n"
        "• 会消耗该账号 <code>1</code> 次官方 Codex reset credit。\n"
        "• OpenAI 会重置当前符合条件的 Codex rate-limit 使用窗口。\n"
        "• 成功后 Parrot 会立即刷新最新额度；只有 fresh usage 证明低于阈值，才自动解除 quota 禁用并清理模型 cooldown。\n"
        "• 如果刷新失败或额度仍超限，本地 quota 限制会保留，避免误放行。\n"
        "• 操作不可撤销；请确认这是你当前真正要重置的账号。\n\n"
        "防误触保护：同一个最终确认按钮重复投递会复用同一个幂等 ID，不会因为 TG 重投而消耗多次。"
    )
    rows = [
        [ui.btn("🚨 最终确认：消耗 1 次官方重置", f"oa:reset_quota:{final_payload}")],
        [ui.btn("❌ 取消，返回账户详情", f"oa:view:{cancel_payload}")],
    ]
    ui.edit(chat_id, message_id, body, reply_markup=ui.inline_kb(rows))


def on_reset_quota(chat_id: int, message_id: int, cb_id: str, short: str, page: int = 1, filter_key: str = _FILTER_ALL) -> None:
    resolved = ui.resolve_code(short)
    ak, reset_idem, reset_stage = _parse_openai_reset_payload(short)
    if ak is None:
        ak = _resolve_to_account_key(resolved)
    if ak is None:
        ui.answer_cb(cb_id, "短码已失效")
        return

    provider = oauth_manager.provider_of(ak)
    if provider == "openai":
        if not reset_idem or reset_stage != "execute":
            ui.answer_cb(cb_id, "需要先完成二次确认")
            text, kb = _detail_text_and_kb(ak, page=page, filter_key=filter_key, refresh_quota=False)
            if text:
                ui.edit(chat_id, message_id,
                        "⚠️ <b>未执行重置</b>\nOpenAI 官方额度重置必须经过说明页和最终确认页，不能从旧按钮或直达回调直接执行。\n\n" + text,
                        reply_markup=kb)
            return
        ui.answer_cb(cb_id, "正在调用 OpenAI 官方重置...")
        result = _run_sync(oauth_manager.redeem_openai_rate_limit_reset_credit(ak, idempotency_key=reset_idem))
        if isinstance(result, Exception):
            ui.send(chat_id, _oauth_error_html(
                result, provider="openai", operation="rate_limit_reset_credit",
            ))
            return
        outcome = result.get("outcome")
        reset_credit_count_override = None
        if outcome in ("reset", "alreadyRedeemed") and result.get("available_count") is not None:
            try:
                reset_credit_count_override = int(result.get("available_count"))
            except (TypeError, ValueError):
                reset_credit_count_override = None
        text, kb = _detail_text_and_kb(
            ak, page=page, filter_key=filter_key, refresh_quota=False,
            reset_credit_count_override=reset_credit_count_override,
        )
        if not text:
            return
        if outcome in ("reset", "alreadyRedeemed"):
            prefix = "♻️ <b>OpenAI 官方额度重置已执行</b>\n"
            if result.get("available_count") is not None:
                prefix += f"剩余官方重置次数: <code>{result.get('available_count')}</code>\n"
            quota_action = result.get("quota_action") or {}
            action = quota_action.get("action")
            if result.get("refresh_error"):
                prefix += "⚠️ 最新额度刷新失败，未自动解除本地 quota 限制；请稍后点「刷新用量/重置卡」重新确认。\n"
            elif action == "resumed":
                prefix += "✅ 已刷新最新额度，确认低于阈值；已自动解除 quota 禁用并清理模型冷却。\n"
            elif action == "kept_enabled":
                prefix += "✅ 已刷新最新额度，账号保持可用；已清理相关模型冷却。\n"
            elif action in ("still_over_quota", "disabled"):
                hit = " / ".join(quota_action.get("hit_windows") or []) or "?"
                prefix += f"⚠️ 已刷新最新额度，但仍超限（<code>{ui.escape_html(hit)}</code>）；本地 quota 限制已保留。\n"
            elif action == "quota_unknown_keep_disabled":
                prefix += "⚠️ 最新额度没有有效窗口数据，未自动解除本地 quota 限制。\n"
            else:
                prefix += "ℹ️ 已刷新最新额度；未触发自动解禁动作。\n"
            prefix += "\n"
            ui.edit(chat_id, message_id, prefix + text, reply_markup=kb)
        elif outcome == "nothingToReset":
            ui.edit(chat_id, message_id,
                    "ℹ️ OpenAI 返回: 当前没有符合条件的使用窗口需要重置。\n\n" + text,
                    reply_markup=kb)
        elif outcome == "noCredit":
            ui.edit(chat_id, message_id,
                    "⚠️ OpenAI 返回: 当前没有可用的官方重置次数。\n\n" + text,
                    reply_markup=kb)
        else:
            ui.edit(chat_id, message_id,
                    f"⚠️ OpenAI 重置返回未知结果: <code>{ui.escape_html(str(outcome))}</code>\n\n" + text,
                    reply_markup=kb)
        return

    result = oauth_manager.reset_quota(ak)
    action = result.get("action")
    if action == "reset":
        ui.answer_cb(cb_id, "已清本地配额禁用")
    elif action == "cleared_runtime_state":
        ui.answer_cb(cb_id, "已清理本地配额/冷却状态")
    elif action == "noop_user":
        ui.answer_cb(cb_id, "手动禁用不自动重置")
    elif action == "noop_auth_error":
        ui.answer_cb(cb_id, "auth_error 需重新登录")
    else:
        ui.answer_cb(cb_id, "无需重置")
    text, kb = _detail_text_and_kb(ak, page=page, filter_key=filter_key, refresh_quota=False)
    if text:
        prefix = "♻️ <b>已清理本地配额禁用</b>\n"
        if action == "reset":
            prefix += "已清除该账号的 quota 禁用、模型冷却和本地 quota 缓存；下一次真实请求/刷新会重新采样。\n\n"
            ui.edit(chat_id, message_id, prefix + text, reply_markup=kb)
        else:
            ui.edit(chat_id, message_id, text, reply_markup=kb)


def on_clear_affinity(chat_id: int, message_id: int, cb_id: str, short: str, page: int = 1, filter_key: str = _FILTER_ALL) -> None:
    ak = _resolve_to_account_key(ui.resolve_code(short))
    if ak is None:
        ui.answer_cb(cb_id, "短码已失效")
        return
    ch_key = f"oauth:{ak}"
    affinity.delete_by_channel(ch_key)
    affinity.client_delete_by_channel(ch_key)
    ui.answer_cb(cb_id, "已清亲和")
    text, kb = _detail_text_and_kb(ak, page=page, filter_key=filter_key)
    if text:
        ui.edit(chat_id, message_id, text, reply_markup=kb)


# ─── 启用 / 禁用 ──────────────────────────────────────────────────

def on_toggle(chat_id: int, message_id: int, cb_id: str, short: str, page: int = 1, filter_key: str = _FILTER_ALL) -> None:
    ak = _resolve_to_account_key(ui.resolve_code(short))
    if ak is None:
        ui.answer_cb(cb_id, "短码已失效")
        return
    acc = oauth_manager.get_account(ak)
    if acc is None:
        ui.answer_cb(cb_id, "账户不存在")
        show(chat_id, message_id, page=page, filter_key=filter_key)
        return

    enabled = acc.get("enabled", True) and not acc.get("disabled_reason")
    if enabled:
        oauth_manager.set_enabled(ak, False, reason="user")
        ui.answer_cb(cb_id, "已禁用")
    else:
        oauth_manager.set_enabled(ak, True)
        ui.answer_cb(cb_id, "已启用")

    text, kb = _detail_text_and_kb(ak, page=page, filter_key=filter_key)
    if text:
        ui.edit(chat_id, message_id, text, reply_markup=kb)


# ─── 删除（二次确认） ─────────────────────────────────────────────

def on_delete_ask(chat_id: int, message_id: int, cb_id: str, short: str, page: int = 1, filter_key: str = _FILTER_ALL) -> None:
    ak = _resolve_to_account_key(ui.resolve_code(short))
    if ak is None:
        ui.answer_cb(cb_id, "短码已失效")
        return
    acc = oauth_manager.get_account(ak)
    email = (acc or {}).get("email") or _account_email(ak)
    prov = oauth_manager.provider_of(ak)
    prov_tag = "🅾 OpenAI" if prov == "openai" else "🅰 Claude"
    ui.answer_cb(cb_id)
    ui.edit(
        chat_id, message_id,
        f"确认删除账户 <code>{ui.escape_html(email)}</code>（{prov_tag}）？\n"
        f"⚠ 该操作将清除此账户的所有统计与亲和绑定数据。",
        reply_markup=ui.inline_kb([[
            ui.btn("✅ 确认删除", f"oa:delete_exec:{_callback_payload(short, page, filter_key)}"),
            ui.btn("❌ 取消",     f"oa:view:{_callback_payload(short, page, filter_key)}"),
        ]]),
    )


def on_delete_exec(chat_id: int, message_id: int, cb_id: str, short: str, page: int = 1, filter_key: str = _FILTER_ALL) -> None:
    ak = _resolve_to_account_key(ui.resolve_code(short))
    if ak is None:
        ui.answer_cb(cb_id, "短码已失效")
        show(chat_id, message_id, page=page, filter_key=filter_key)
        return
    email = _account_email(ak)
    try:
        oauth_manager.delete_account(ak)
    except Exception as exc:
        ui.answer_cb(cb_id, "删除失败")
        ui.send(chat_id, f"❌ 删除失败: <code>{ui.escape_html(str(exc))}</code>")
        return
    ui.answer_cb(cb_id, "已删除")
    extra = ""
    if load_balancing.is_initialized():
        extra = "\n已从负载均衡优先级队列中移除。"
    ui.edit(chat_id, message_id, f"✅ 已删除 <code>{ui.escape_html(email)}</code>{extra}")
    show(chat_id, message_id, page=page, filter_key=filter_key)


def _initial_update_keys_for_ui(accounts: list[dict]) -> list[str]:
    keys: list[str] = []
    for acc in accounts:
        if not _should_refresh_account_for_ui(acc):
            continue
        ak = _account_key(acc)
        # 当前卡死问题来自 OpenAI 的 wham/reset-card 慢接口；只有完全没有
        # OpenAI usage cache 时才切进度面板。已有缓存则直接显示旧值、后台刷新。
        if oauth_manager.provider_of(acc) == "openai" and _needs_initial_oauth_cache_sync_for_ui(ak):
            keys.append(ak)
    return keys


def _progress_account_block(account_key: str, idx: int, status_line: str | None = None) -> str:
    acc = oauth_manager.get_account(account_key) or {"email": _account_email(account_key)}
    block = _format_account_block(acc)
    first, _, rest = block.partition("\n")
    lines = [f"{idx}. {first}"]
    if rest:
        lines.append(rest)
    if status_line:
        lines.append(status_line)
    return "\n".join(lines)


def _build_oauth_update_panel(items: list[str], *, done: bool = False,
                              success_count: int = 0, fail_count: int = 0,
                              final_hint: str = "") -> str:
    title = "✅ <b>OAuth账户信息更新完成</b>" if done else "🔄 <b>正在更新OAuth账户信息</b>"
    lines = [title, ""]
    lines.extend(items or ["暂无需要更新的账户。"])
    if done:
        lines.extend(["", f"📢 用量刷新完成 / 重置卡刷新完成：成功 {success_count} 个，失败 {fail_count} 个。"])
        if final_hint:
            lines.append(final_hint)
    return ui.truncate("\n".join(lines))


def _run_oauth_update_panel(chat_id: int, progress_mid: int, account_keys: list[str],
                            *, page: int = 1, filter_key: str = _FILTER_ALL,
                            final_target_message_id: int | None = None,
                            final_detail_account_key: str | None = None,
                            delete_progress_later: bool = False,
                            send_fallback_summary: bool = False,
                            show_transition_hint: bool = True) -> None:
    account_keys = [ak for ak in account_keys if ak]
    items = [
        _progress_account_block(ak, idx, "  等待更新...")
        for idx, ak in enumerate(account_keys, 1)
    ]
    success_count = 0
    fail_count = 0

    def _flush(done: bool = False, final_hint: str = "") -> None:
        text = _build_oauth_update_panel(
            items, done=done, success_count=success_count,
            fail_count=fail_count, final_hint=final_hint,
        )
        if progress_mid == -1:
            return
        try:
            ui.edit(chat_id, progress_mid, text)
        except Exception:
            pass

    def _set_item(idx0: int, ak: str, status_line: str | None) -> None:
        items[idx0] = _progress_account_block(ak, idx0 + 1, status_line)
        _flush()

    _flush()
    for idx0, ak in enumerate(account_keys):
        provider = oauth_manager.provider_of(ak)
        acquired = False
        with _BACKGROUND_REFRESH_LOCK:
            if ak not in _BACKGROUND_REFRESH_INFLIGHT:
                _BACKGROUND_REFRESH_INFLIGHT.add(ak)
                acquired = True
        if not acquired:
            _set_item(idx0, ak, "  ⌛ 已有更新任务进行中，等待结果...")
            deadline = time.time() + 120
            while time.time() < deadline:
                with _BACKGROUND_REFRESH_LOCK:
                    busy = ak in _BACKGROUND_REFRESH_INFLIGHT
                if not busy:
                    break
                time.sleep(0.5)
            if _quota_cache_has_usage_signal(state_db.quota_load(ak)):
                success_count += 1
                _set_item(idx0, ak, "  ✅ 刷新成功")
            else:
                fail_count += 1
                _set_item(idx0, ak, "  ⚠ 等待已有更新任务超时，稍后可重试")
            continue

        def _stage(stage: str, payload=None) -> None:
            if stage == "usage_start":
                _set_item(idx0, ak, "  ⌛ 正在更新用量数据...")
            elif stage == "usage_done":
                if provider == "openai":
                    _set_item(idx0, ak, "  ⌛ 正在更新重置卡数据...")
                else:
                    _set_item(idx0, ak, "  ✅ 刷新成功")
            elif stage == "reset_start":
                _set_item(idx0, ak, "  ⌛ 正在更新重置卡数据...")
            elif stage == "reset_done":
                _set_item(idx0, ak, "  ✅ 刷新成功")
            elif stage == "reset_error":
                _set_item(idx0, ak, "  ⚠ 重置卡明细更新失败，已保留用量结果")

        try:
            result = _fetch_and_save_usage_result_sync(ak, email=_account_email(ak), on_stage=_stage)
            if result.get("error") is not None:
                fail_count += 1
                err = _oauth_error_html(result["error"], provider=provider, operation="fetch_usage", indent="  ")
                items[idx0] = _progress_account_block(ak, idx0 + 1) + "\n" + err
                _flush()
                continue
            usage = result.get("usage")
            if isinstance(usage, dict):
                _evaluate_quota_action(ak, usage)
            success_count += 1
            if result.get("reset_credit_error") is not None and provider == "openai":
                _set_item(idx0, ak, "  ⚠ 重置卡明细更新失败，已保留用量结果")
            else:
                _set_item(idx0, ak, "  ✅ 刷新成功")
        except Exception as exc:
            fail_count += 1
            items[idx0] = _progress_account_block(ak, idx0 + 1, f"  ❌ 更新异常: <code>{ui.escape_html(str(exc))[:120]}</code>")
            _flush()
        finally:
            with _BACKGROUND_REFRESH_LOCK:
                _BACKGROUND_REFRESH_INFLIGHT.discard(ak)

    final_hint = ""
    if show_transition_hint:
        final_hint = "正在打开 OAuth 列表..." if final_detail_account_key is None else "正在打开账户详情..."
    _flush(done=True, final_hint=final_hint)

    target_mid = final_target_message_id or progress_mid
    try:
        if final_detail_account_key:
            text, kb = _detail_text_and_kb(
                final_detail_account_key, page=page, filter_key=filter_key,
                refresh_quota=False,
            )
        else:
            text, kb = _list_text_and_kb(page=page, filter_key=filter_key)
        if text and target_mid != -1:
            ui.edit(chat_id, target_mid, text, reply_markup=kb)
    except Exception as exc:
        print(f"[oauth_menu] final panel render failed: {exc}")

    if send_fallback_summary:
        ui.send(chat_id, _build_oauth_update_panel(
            items, done=True, success_count=success_count,
            fail_count=fail_count, final_hint="",
        ))

    if delete_progress_later and progress_mid != -1:
        def _delete_later():
            try:
                ui.delete_message(chat_id, progress_mid)
            except Exception:
                pass
        threading.Timer(300.0, _delete_later).start()


def _start_oauth_update_panel(chat_id: int, progress_mid: int, account_keys: list[str],
                              *, page: int = 1, filter_key: str = _FILTER_ALL,
                              final_target_message_id: int | None = None,
                              final_detail_account_key: str | None = None,
                              delete_progress_later: bool = False,
                              send_fallback_summary: bool = False,
                              show_transition_hint: bool = True,
                              background: bool = True) -> None:
    if not background:
        _run_oauth_update_panel(
            chat_id, progress_mid, account_keys,
            page=page, filter_key=filter_key,
            final_target_message_id=final_target_message_id,
            final_detail_account_key=final_detail_account_key,
            delete_progress_later=delete_progress_later,
            send_fallback_summary=send_fallback_summary,
            show_transition_hint=show_transition_hint,
        )
        return
    threading.Thread(
        target=_run_oauth_update_panel,
        args=(chat_id, progress_mid, account_keys),
        kwargs={
            "page": page,
            "filter_key": filter_key,
            "final_target_message_id": final_target_message_id,
            "final_detail_account_key": final_detail_account_key,
            "delete_progress_later": delete_progress_later,
            "send_fallback_summary": send_fallback_summary,
            "show_transition_hint": show_transition_hint,
        },
        daemon=True,
    ).start()


def _refresh_all_usage_summary(usage: dict | None, *, provider: str,
                               reset_credit_error=None) -> str:
    if not isinstance(usage, dict):
        return "无数据"
    utils = oauth_manager.extract_utils_percent(usage)
    tags = ["5h", "7d", "30d", "sonnet", "opus"]
    parts = []
    for tag, util in zip(tags, utils):
        if util is None:
            continue
        try:
            parts.append(f"{tag} {float(util):.0f}%")
        except (TypeError, ValueError):
            continue
    if provider == "openai":
        reset_count = _openai_reset_credit_count_from_usage(usage)
        if reset_count is not None:
            parts.append(f"重置次数 {reset_count}")
        elif reset_credit_error is not None:
            parts.append("重置次数 获取失败")
    return " / ".join(parts) if parts else "无数据"


def _run_refresh_all_legacy_panel(chat_id: int, progress_mid: int, account_keys: list[str],
                                  *, page: int = 1, filter_key: str = _FILTER_ALL,
                                  final_target_message_id: int | None = None,
                                  delete_progress_later: bool = False,
                                  send_fallback_summary: bool = False) -> None:
    """Old concise refresh-all progress format, with OpenAI reset-credit count."""
    lines: list[str] = ["🔄 <b>批量刷新 OAuth 用量</b>", ""]
    success_count = 0
    fail_count = 0

    def _flush() -> None:
        if progress_mid == -1:
            return
        try:
            ui.edit(chat_id, progress_mid, ui.truncate("\n".join(lines)))
        except Exception:
            pass

    _flush()
    for idx, ak in enumerate([k for k in account_keys if k], 1):
        provider = oauth_manager.provider_of(ak)
        email = _account_email(ak)
        prov_tag = "🅾 OpenAI" if provider == "openai" else ("🅰 Claude" if provider == "claude" else ui.escape_html(provider or "OAuth"))
        ek = ui.escape_html(email or ak)

        lines.append(f"<b>{idx}. {ek}</b> · {prov_tag}")
        lines.append("  ⌛ 正在刷新用量...")
        _flush()

        def _stage(stage: str, payload=None) -> None:
            if stage == "reset_start":
                lines[-1] = "  ⌛ 正在刷新重置次数..."
                _flush()

        acquired = False
        with _BACKGROUND_REFRESH_LOCK:
            if ak not in _BACKGROUND_REFRESH_INFLIGHT:
                _BACKGROUND_REFRESH_INFLIGHT.add(ak)
                acquired = True
        if not acquired:
            lines[-1] = "  ⌛ 已有刷新任务进行中，等待结果..."
            _flush()
            deadline = time.time() + 120
            while time.time() < deadline:
                with _BACKGROUND_REFRESH_LOCK:
                    busy = ak in _BACKGROUND_REFRESH_INFLIGHT
                if not busy:
                    break
                time.sleep(0.5)
            row = state_db.quota_load(ak)
            if _quota_cache_has_usage_signal(row):
                success_count += 1
                usage = None
                try:
                    usage = oauth_manager.usage_from_quota_row(row) if row else None
                except Exception:
                    usage = None
                lines[-1] = f"  ✅ 刷新成功: {_refresh_all_usage_summary(usage, provider=provider)}"
            else:
                fail_count += 1
                lines[-1] = "  ⚠ 等待已有刷新任务超时，稍后可重试"
            lines.append("")
            _flush()
            continue

        try:
            result = _fetch_and_save_usage_result_sync(ak, email=email, on_stage=_stage)
            if result.get("error") is not None:
                fail_count += 1
                _replace_last_with_oauth_error(
                    lines, result["error"], provider=provider, operation="fetch_usage",
                )
                lines.append("")
                _flush()
                continue

            usage = result.get("usage")
            success_count += 1
            lines[-1] = (
                "  ✅ 刷新成功: "
                + _refresh_all_usage_summary(
                    usage, provider=provider,
                    reset_credit_error=result.get("reset_credit_error"),
                )
            )
            if result.get("reset_credit_error") is not None and provider == "openai":
                lines.append("  ⚠ 重置次数刷新失败，已保留用量结果")

            if isinstance(usage, dict):
                quota_action = _evaluate_quota_action(ak, usage)
            else:
                quota_action = None
            if quota_action and quota_action.get("action") == "disabled":
                hit = " / ".join(quota_action.get("hit_windows") or []) or "?"
                lines.append(f"  🔒 触发自动禁用（超限窗口: <code>{ui.escape_html(hit)}</code>）")
            elif quota_action and quota_action.get("action") == "still_over_quota":
                hit = " / ".join(quota_action.get("hit_windows") or []) or "?"
                lines.append(f"  ⚠ 仍未恢复，维持禁用（超限: <code>{ui.escape_html(hit)}</code>）")
            elif quota_action and quota_action.get("action") == "resumed":
                lines.append("  ♻ 额度已恢复，已自动解除禁用")
            elif quota_action and quota_action.get("action") == "noop_user":
                lines.append("  🚫 手动禁用中（不自动恢复）")
            elif quota_action and quota_action.get("action") == "noop_auth_error":
                lines.append("  ⚠ auth_error（不自动恢复，需重新登录）")
            elif quota_action and quota_action.get("action") == "disable_failed":
                lines.append("  ❌ 自动禁用写入失败，见 systemd 日志")
            elif quota_action and quota_action.get("action") == "resume_failed":
                lines.append("  ❌ 自动解禁写入失败，见 systemd 日志")
        except Exception as exc:
            fail_count += 1
            lines[-1] = f"  ❌ 刷新异常: <code>{ui.escape_html(str(exc))[:120]}</code>"
        finally:
            with _BACKGROUND_REFRESH_LOCK:
                _BACKGROUND_REFRESH_INFLIGHT.discard(ak)

        lines.append("")
        _flush()

    lines.append(f"📢 用量刷新完成：成功 {success_count} 个，失败 {fail_count} 个。")
    lines.append("本消息 5 分钟后自动销毁。")
    _flush()

    try:
        list_text, list_kb = _list_text_and_kb(page=page, filter_key=filter_key)
        target_mid = final_target_message_id or progress_mid
        if list_text and target_mid != -1:
            ui.edit(chat_id, target_mid, list_text, reply_markup=list_kb)
    except Exception as exc:
        print(f"[oauth_menu] final list render failed: {exc}")

    if send_fallback_summary:
        ui.send(chat_id, ui.truncate("\n".join(lines)))

    if delete_progress_later and progress_mid != -1:
        def _delete_later():
            try:
                ui.delete_message(chat_id, progress_mid)
            except Exception:
                pass
        threading.Timer(300.0, _delete_later).start()


# ─── 刷新全部用量 / 重置卡 ─────────────────────────────────────────────
#
# 交互：不覆盖原 OAuth 面板，而是新发一条「进度消息」追加式展示：
#   ⌛ 正在刷新 xxx 账户用量...
#   ✅ 刷新成功: 5h 12% / 7d 45%
#   🔒 触发自动禁用（超限窗口: 5h）
#   ...
#   📢 用量刷新完成，本消息 5 分钟后自动销毁。
#
# 副作用：每账户拉完 usage 后调 `evaluate_and_toggle_by_usage`：
#   • 任一窗口 util ≥ 阈值 → 按「撞哪个窗口锁哪个窗口」触发/维持 quota 禁用
#   • 全部窗口可用 & 当前是 quota 禁用 → 自动解除（因额度触发的禁用才解）
#   • user/auth_error 禁用 → 永远不动
#
# 5 分钟后后台 Timer 删除进度消息（失败静默）。

def on_refresh_all(chat_id: int, message_id: int, cb_id: str, page: int = 1, filter_key: str = _FILTER_ALL) -> None:
    ui.answer_cb(cb_id, "开始刷新用量/重置卡...")
    accounts = oauth_manager.list_accounts()
    account_keys = _refreshable_account_keys_for_ui(accounts)
    if not account_keys:
        ui.send(chat_id, "❌ 当前无可刷新的 OAuth 账户")
        return

    resp = ui.send(chat_id, "🔄 <b>批量刷新 OAuth 用量</b>\n\n⌛ 初始化...")
    if not resp or not resp.get("ok"):
        ui.send(chat_id, "❌ 无法创建进度消息")
        return
    progress_mid = (resp.get("result") or {}).get("message_id")
    if progress_mid is None:
        progress_mid = -1

    # 真实 TG 有 message_id：后台刷新，避免 callback 长时间占住。
    # 测试/降级无 message_id：同步跑完并追加一条最终摘要，保持可断言。
    if progress_mid != -1:
        threading.Thread(
            target=_run_refresh_all_legacy_panel,
            args=(chat_id, int(progress_mid), account_keys),
            kwargs={
                "page": page,
                "filter_key": filter_key,
                "final_target_message_id": message_id,
                "delete_progress_later": True,
                "send_fallback_summary": False,
            },
            daemon=True,
        ).start()
        return

    _run_refresh_all_legacy_panel(
        chat_id, int(progress_mid), account_keys,
        page=page, filter_key=filter_key,
        final_target_message_id=message_id,
        delete_progress_later=True,
        send_fallback_summary=(progress_mid == -1),
    )


# ─── 新增账户：入口 ──────────────────────────────────────────────

def on_add_menu(chat_id: int, message_id: int, cb_id: str) -> None:
    """新增 OAuth 账户：把常用登录/导入入口扁平化到一级。"""
    # 这里也是所有新增流程的「取消」落点，进入时清掉等待输入状态，避免后续文本误触发旧流程。
    states.pop_state(chat_id)
    ui.answer_cb(cb_id)
    ui.edit(
        chat_id, message_id,
        "<b>新增 OAuth 账户</b>\n请选择类型：",
        reply_markup=ui.inline_kb([
            [ui.btn("🟣 Claude 登录获取 Token", "oa:login")],
            [ui.btn("📄 Claude 手动设置 JSON", "oa:set_json")],
            [ui.btn("🅾 OpenAI 登录获取 Token", "oa:login:openai")],
            [ui.btn("🔑 OpenAI 粘贴 refresh_token", "oa:set_rt:openai")],
            [ui.btn("📦 OpenAI 导入 Sub2API 文件", "oa:import:sub2api")],
            [ui.btn("🗂 OpenAI 导入 CPA 文件", "oa:import:cpa")],
            [ui.btn("◀ 返回列表", "menu:oauth")],
            [ui.btn("🏠 返回主菜单", "menu:main")],
        ]),
    )


def on_add_claude(chat_id: int, message_id: int, cb_id: str) -> None:
    """Claude 子菜单（原 on_add_menu 内容）。"""
    ui.answer_cb(cb_id)
    ui.edit(
        chat_id, message_id,
        "<b>新增 Claude OAuth 账户</b>\n请选择方式：",
        reply_markup=ui.inline_kb([
            [ui.btn("🌐 登录获取 Token", "oa:login")],
            [ui.btn("📝 手动设置 JSON",  "oa:set_json")],
            [ui.btn("◀ 上一步", "oa:add")],
        ]),
    )


def on_add_openai(chat_id: int, message_id: int, cb_id: str) -> None:
    """OpenAI 子菜单。"""
    ui.answer_cb(cb_id)
    ui.edit(
        chat_id, message_id,
        "<b>新增 OpenAI OAuth 账户</b>\n请选择方式：\n\n"
        "<i>登录获取：浏览器打开 Codex CLI 授权页，登录后页面会重定向到一个"
        "本地 URL（通常显示「无法访问此网站」），把地址栏里整段 URL 复制回来即可。</i>\n"
        "<i>手动粘 RT：已经有 refresh_token 时直接粘字符串，代理会自动刷新"
        "并从 id_token 解出 email 等账户信息。</i>",
        reply_markup=ui.inline_kb([
            [ui.btn("🌐 登录获取 Token", "oa:login:openai")],
            [ui.btn("📝 粘贴 refresh_token", "oa:set_rt:openai")],
            [ui.btn("◀ 上一步", "oa:add")],
        ]),
    )


# ─── PKCE 登录流程 ────────────────────────────────────────────────

def on_login_start(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    code_verifier, code_challenge = oauth_manager.pkce_generate()
    state = secrets.token_urlsafe(32)
    url = oauth_manager.build_login_url(code_challenge, state)

    states.set_state(chat_id, "oa_login_code", {
        "code_verifier": code_verifier, "state": state,
    })

    ui.edit(
        chat_id, message_id,
        "请在浏览器中打开以下链接完成 Claude 账号登录：\n\n"
        f"<a href=\"{ui.escape_html(url)}\">点此打开登录页</a>\n\n"
        "登录后页面会显示一个 <b>authorization code</b>（通常形如 <code>abc#state</code>），"
        "请复制并发送给我。\n\n"
        "<i>（登录会话 10 分钟内有效）</i>",
        reply_markup=ui.inline_kb([[ui.btn("❌ 取消", "oa:add")]]),
    )


def on_login_code_input(chat_id: int, text: str) -> None:
    state = states.pop_state(chat_id)
    nav = {"back_label": "◀ 返回新增账户", "back_callback": "oa:add"}
    if not state or state.get("action") != "oa_login_code":
        ui.send_result(chat_id, "❌ 登录会话已失效，请重新发起登录流程。", **nav)
        return
    data = state.get("data") or {}

    raw = (text or "").strip()
    if not raw:
        ui.send_result(chat_id, "❌ 内容为空。请重新发起登录流程。", **nav)
        return

    # 页面通常返回 code#state 形式
    code_part = raw.split("#", 1)[0].strip()
    if not code_part:
        ui.send_result(chat_id, "❌ code 无效，请重新发起登录流程。", **nav)
        return

    try:
        tok_resp = oauth_manager.exchange_code(
            code_part, data.get("code_verifier", ""), data.get("state", ""),
        )
    except Exception as exc:
        ui.send_result(chat_id,
                       _oauth_error_html(exc, provider="claude", operation="exchange_code"),
                       **nav)
        return

    # 获取 email + 套餐信息（可选）
    email = ""
    claude_plan_info = {}
    try:
        profile = _run_sync(oauth_manager.fetch_profile(tok_resp.get("access_token", "")))
        if isinstance(profile, dict):
            email = (profile.get("account") or {}).get("email", "") or ""
            claude_plan_info = oauth_manager.extract_claude_plan_info(profile)
    except Exception:
        pass

    if not email:
        # 给用户一个兜底的唯一名
        email = f"unnamed-{int(datetime.now().timestamp())}@local"

    expires_in = int(tok_resp.get("expires_in", 28800))
    new_expired = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry = {
        "email": email,
        "access_token": tok_resp.get("access_token", ""),
        "refresh_token": tok_resp.get("refresh_token", ""),
        "expired": new_expired,
        "last_refresh": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "type": "claude",
        "enabled": True,
        "disabled_reason": None,
        "disabled_until": None,
        "models": [],
        # §9-1：存登录响应里的 scope，供后续 refresh 带真实 scope
        "scopes": tok_resp.get("scope", "") or "",
        **claude_plan_info,
    }
    try:
        oauth_manager.add_account(entry)
    except Exception as exc:
        ui.send_result(chat_id,
                       f"❌ 保存失败: <code>{ui.escape_html(str(exc))}</code>",
                       **nav)
        return

    lb_hint = (
        "\n\n已加入负载均衡优先级队列末尾，如需调整请进入「负载均衡」。"
        if load_balancing.is_initialized() else ""
    )
    _cl_plan = oauth_manager.claude_plan_label(entry)
    _cl_sub_parts = []
    if claude_plan_info.get("subscription_status"):
        _cl_sub_parts.append(claude_plan_info["subscription_status"])
    if claude_plan_info.get("billing_type"):
        _cl_sub_parts.append(claude_plan_info["billing_type"])
    _cl_sub_line = f"\n订阅信息: <code>{ui.escape_html(' · '.join(_cl_sub_parts))}</code>" if _cl_sub_parts else ""
    _cl_created = claude_plan_info.get("subscription_created_at") or ""
    _cl_created_line = f"\n开始时间: <code>{_format_bjt(_cl_created)}</code>" if _cl_created else ""
    ui.send_result(
        chat_id,
        "✅ <b>Anthropic OAuth 账户已添加</b>\n\n"
        f"Email: <code>{ui.escape_html(email)}</code>\n"
        f"套餐计划: <code>{ui.escape_html(_cl_plan or '?')}</code>{_cl_sub_line}{_cl_created_line}\n"
        f"过期: <code>{_fmt_time_full(new_expired)}</code>\n"
        f"来源: <code>Claude OAuth 登录</code>{lb_hint}",
        back_label="◀ 返回 OAuth 列表", back_callback="menu:oauth",
    )


# ─── 手动设置 JSON ────────────────────────────────────────────────

def on_set_json_start(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    states.set_state(chat_id, "oa_set_json")
    ui.edit(
        chat_id, message_id,
        "请粘贴 OAuth JSON（需包含 <code>email / access_token / refresh_token / expired</code>）：",
        reply_markup=ui.inline_kb([[ui.btn("❌ 取消", "oa:add")]]),
    )


def on_set_json_input(chat_id: int, text: str) -> None:
    states.pop_state(chat_id)
    nav = {"back_label": "◀ 返回新增账户", "back_callback": "oa:add"}
    try:
        data = json.loads((text or "").strip())
    except Exception as exc:
        ui.send_result(chat_id,
                       f"❌ JSON 解析失败: <code>{ui.escape_html(str(exc))}</code>",
                       **nav)
        return
    if not isinstance(data, dict):
        ui.send_result(chat_id,
                       "❌ 需要一个 JSON 对象（含 email / access_token / refresh_token）",
                       **nav)
        return

    for k in ("email", "access_token", "refresh_token"):
        if not data.get(k):
            ui.send_result(chat_id, f"❌ 缺少必填字段: <code>{k}</code>", **nav)
            return

    entry = {
        "email": data["email"],
        "access_token": data["access_token"],
        "refresh_token": data["refresh_token"],
        "expired": data.get("expired", ""),
        "last_refresh": data.get("last_refresh",
                                 datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
        "type": data.get("type", "claude"),
        "enabled": True,
        "disabled_reason": None,
        "disabled_until": None,
        "models": list(data.get("models") or []),
    }
    try:
        oauth_manager.add_account(entry)
    except Exception as exc:
        ui.send_result(chat_id,
                       f"❌ 保存失败: <code>{ui.escape_html(str(exc))}</code>",
                       **nav)
        return

    lb_hint = (
        "\n已加入负载均衡优先级队列末尾，如需调整请进入「负载均衡」。"
        if load_balancing.is_initialized() else ""
    )
    ui.send_result(chat_id, f"✅ 已添加 <code>{ui.escape_html(data['email'])}</code>{lb_hint}", **nav)


# ─── OpenAI PKCE 登录 ──────────────────────────────────────────────
#
# 与 Claude 的 on_login_start 区别：
#   1. code_verifier 是 hex(64 随机字节)，非 base64url（OpenAI 特殊要求）
#   2. 登录 URL 必须带 id_token_add_organizations / codex_cli_simplified_flow
#   3. 回调 URL 是 http://localhost:1455/auth/callback?code=...&state=...；
#      这个端口我们不会监听，浏览器会显示"无法访问此网站"，用户把地址栏
#      的 URL 整段复制回来即可。我们正则抽 code 和 state。
#   4. 拿到 token 后解 id_token 得到 email / chatgpt_account_id / plan_type。


_OA_NAV_OPENAI = {"back_label": "◀ 返回新增账户", "back_callback": "oa:add"}


def _build_openai_login_text_and_kb(url: str) -> tuple[str, dict]:
    """构建 OpenAI 登录页的文本和键盘（复用于首次生成和重新生成）。"""
    text = (
        "请在浏览器打开以下链接登录 OpenAI / ChatGPT 账号：\n\n"
        f"<a href=\"{ui.escape_html(url)}\">📱 点此打开登录页</a>\n\n"
        "👇 长按下方地址可复制（推荐用隐私浏览器打开）：\n"
        f"<code>{ui.escape_html(url)}</code>\n\n"
        "登录后浏览器会跳到 <code>http://localhost:1455/auth/callback?code=...&amp;state=...</code>"
        "（页面显示「无法访问此网站」属正常，代理不会监听这个端口）。\n"
        "请把 <b>地址栏里整段 URL</b> 复制发给我即可。\n\n"
        "<i>（登录会话 30 分钟内有效）</i>"
    )
    kb = ui.inline_kb([
        [ui.btn("🔄 重新生成登录地址", "oa:login:openai:regen")],
        [ui.btn("❌ 取消", "oa:add")],
    ])
    return text, kb


def on_login_openai_start(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    verifier, challenge = openai_provider.pkce_generate()
    state = secrets.token_urlsafe(32)
    url = openai_provider.build_login_url(challenge, state)

    states.set_state(chat_id, "oa_openai_code", {
        "code_verifier": verifier, "state": state,
    })

    text, kb = _build_openai_login_text_and_kb(url)
    ui.edit(chat_id, message_id, text, reply_markup=kb)


def on_login_openai_regen(chat_id: int, message_id: int, cb_id: str) -> None:
    """重新生成 PKCE + 登录 URL，覆盖旧状态。"""
    on_login_openai_start(chat_id, message_id, cb_id)


def _extract_openai_code_and_state(text: str) -> tuple[str, str]:
    """从用户粘贴的内容里抽 code/state。

    支持三种形式：
      - 完整 URL：http://localhost:1455/auth/callback?code=xxx&state=yyy
      - 纯查询串：code=xxx&state=yyy
      - 单独 code#state（兼容 Claude 那条路径的习惯）
    """
    raw = (text or "").strip()
    if not raw:
        return "", ""
    # 情况 1: URL
    if raw.startswith("http://") or raw.startswith("https://"):
        try:
            parsed = urlparse(raw)
            q = parse_qs(parsed.query)
            return (q.get("code", [""])[0].strip(),
                    q.get("state", [""])[0].strip())
        except Exception:
            return "", ""
    # 情况 2: 查询串
    if "=" in raw and "code" in raw:
        q = parse_qs(raw.lstrip("?"))
        code = q.get("code", [""])[0].strip()
        st = q.get("state", [""])[0].strip()
        if code:
            return code, st
    # 情况 3: code#state
    if "#" in raw:
        code, _, st = raw.partition("#")
        return code.strip(), st.strip()
    # 情况 4: 只有 code
    return raw, ""


def on_login_openai_code_input(chat_id: int, text: str) -> None:
    state = states.pop_state(chat_id)
    if not state or state.get("action") != "oa_openai_code":
        ui.send_result(chat_id, "❌ 登录会话已失效，请重新发起登录流程。",
                       **_OA_NAV_OPENAI)
        return
    data = state.get("data") or {}

    code, recv_state = _extract_openai_code_and_state(text)
    if not code:
        ui.send_result(chat_id, "❌ 没有抽到 code，请重新发起登录流程。",
                       **_OA_NAV_OPENAI)
        return
    # state 一致性校验（粘整段 URL 才能拿到；少数客户端不回显 state，放行警告）
    orig_state = data.get("state", "")
    if recv_state and orig_state and recv_state != orig_state:
        ui.send_result(
            chat_id,
            f"❌ state 不匹配：收到 <code>{ui.escape_html(recv_state[:16])}...</code>，"
            f"期望 <code>{ui.escape_html(orig_state[:16])}...</code>。"
            "可能是会话错乱，请重新发起登录流程。",
            **_OA_NAV_OPENAI,
        )
        return

    verifier = data.get("code_verifier", "")
    try:
        tok = openai_provider.exchange_code_sync(code, verifier)
    except Exception as exc:
        ui.send_result(
            chat_id,
            _oauth_error_html(exc, provider="openai", operation="exchange_code"),
            **_OA_NAV_OPENAI,
        )
        return

    _finish_openai_add(chat_id, tok, source="login")


def on_set_rt_openai_start(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    states.set_state(chat_id, "oa_openai_rt")
    ui.edit(
        chat_id, message_id,
        "请粘贴 <b>refresh_token</b>（纯字符串即可，代理会立即用它刷新一次 "
        "token 并从 id_token 解出 email 等账户信息）：",
        reply_markup=ui.inline_kb([[ui.btn("❌ 取消", "oa:add")]]),
    )


def on_set_rt_openai_input(chat_id: int, text: str) -> None:
    states.pop_state(chat_id)
    rt = (text or "").strip()
    # 宽松清洗：用户可能贴了 "refresh_token: xxx" 这类前缀
    m = re.search(r"([A-Za-z0-9_\-\.]{20,})", rt)
    rt_clean = m.group(1) if m else rt
    if not rt_clean or len(rt_clean) < 20:
        ui.send_result(chat_id,
                       "❌ refresh_token 过短或无法识别，请重新粘贴。",
                       **_OA_NAV_OPENAI)
        return
    try:
        tok = openai_provider.refresh_sync(rt_clean)
    except Exception as exc:
        ui.send_result(
            chat_id,
            _oauth_error_html(exc, provider="openai", operation="refresh_token"),
            **_OA_NAV_OPENAI,
        )
        return
    # refresh 响应里可能不带新的 refresh_token，回填用户输入的原 RT
    if not tok.get("refresh_token"):
        tok["refresh_token"] = rt_clean

    _finish_openai_add(chat_id, tok, source="rt")


def _openai_token_to_entry(tok: dict, *, fallback_email: str = "") -> tuple[dict, dict]:
    """token response → Parrot oauthAccounts entry。"""
    id_token = tok.get("id_token", "") or ""
    if not id_token:
        raise ValueError("token 响应缺少 id_token，无法识别账户")
    try:
        claims = openai_provider.decode_id_token(id_token)
    except Exception as exc:
        raise ValueError(f"id_token 解码失败: {exc}") from exc

    info = openai_provider.extract_user_info(claims)
    email = info.get("email") or tok.get("email") or fallback_email or ""
    if not email:
        email = f"unnamed-openai-{int(datetime.now().timestamp())}@local"

    expires_in = int(tok.get("expires_in", 28800))
    new_expired = (
        datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    workspace_id = tok.get("workspace_id") or info.get("workspace_id") or info.get("chatgpt_account_id", "")
    chatgpt_account_id = tok.get("chatgpt_account_id") or workspace_id or info.get("chatgpt_account_id", "")
    entry = {
        "email": email,
        "provider": "openai",
        "access_token": tok.get("access_token", ""),
        "refresh_token": tok.get("refresh_token", ""),
        "expired": new_expired,
        "last_refresh": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "type": "openai",
        "enabled": True,
        "disabled_reason": None,
        "disabled_until": None,
        "models": [],
        "id_token": id_token,
        "chatgpt_account_id": chatgpt_account_id,
        "workspace_id": workspace_id,
        "workspace_name": tok.get("workspace_name") or info.get("workspace_name", ""),
        "workspace_type": tok.get("workspace_type") or info.get("workspace_type", ""),
        "organization_id": tok.get("organization_id") or info.get("organization_id", ""),
        "plan_type": tok.get("plan_type") or info.get("plan_type", ""),
        "subscription_expires_at": tok.get("subscription_expires_at", ""),
    }
    meta = {
        "email": email,
        "expired": new_expired,
        "plan_type": entry.get("plan_type", ""),
        "subscription_expires_at": entry.get("subscription_expires_at", ""),
        "workspace_id": entry.get("workspace_id", ""),
        "workspace_name": entry.get("workspace_name", ""),
        "workspace_type": entry.get("workspace_type", ""),
    }
    return entry, meta



def _refresh_openai_rt_to_entry(refresh_token: str, *, email_hint: str = "",
                                workspace_id: str = "",
                                org_id: str = "") -> tuple[dict | None, dict | None, Exception | None]:
    """refresh_token → entry；失败时返回异常。"""
    try:
        kwargs = {"email": email_hint or None}
        if workspace_id:
            kwargs["workspace_id"] = workspace_id
        if org_id:
            kwargs["org_id"] = org_id
        tok = openai_provider.refresh_sync(refresh_token, **kwargs)
        if not tok.get("refresh_token"):
            tok["refresh_token"] = refresh_token
        entry, meta = _openai_token_to_entry(tok, fallback_email=email_hint)
        return entry, meta, None
    except Exception as exc:
        return None, None, exc


def _find_openai_account_by_email(email: str) -> dict | None:
    email = (email or "").strip()
    if not email:
        return None
    matches = [
        acc for acc in oauth_manager.list_accounts()
        if oauth_manager.provider_of(acc) == "openai" and acc.get("email") == email
    ]
    return matches[0] if len(matches) == 1 else None


def _find_openai_account_by_identity(entry: dict) -> dict | None:
    """Find an existing OpenAI account by the full email+workspace identity."""
    email, workspace_id, chatgpt_account_id = _openai_identity_parts(entry)
    if not (workspace_id or chatgpt_account_id):
        return None
    for acc in oauth_manager.list_accounts():
        if oauth_manager.provider_of(acc) != "openai":
            continue
        acc_email, acc_workspace_id, acc_chatgpt_account_id = _openai_identity_parts(acc)
        if (
            acc_email == email
            and acc_workspace_id == workspace_id
            and acc_chatgpt_account_id == chatgpt_account_id
        ):
            return acc
    return None


def _find_openai_existing_for_entry(entry: dict) -> dict | None:
    """Find existing OpenAI account for an incoming token entry.

    If the token exposes a workspace identity, email is display-only and must not
    be used as a duplicate key; same email can legitimately have Personal + Team
    workspaces. Email fallback is only for legacy/metadata-poor tokens that have
    no workspace/chatgpt account id.
    """
    if _openai_workspace_id(entry):
        return _find_openai_account_by_identity(entry)
    return _find_openai_account_by_email(entry.get("email", ""))


def _upsert_openai_account_entry(entry: dict, *, preserve_existing_settings: bool = True) -> bool:
    """写入 OpenAI 账号。返回 True 表示替换既有账号，False 表示新增。"""
    target = _find_openai_existing_for_entry(entry)
    if target is None:
        oauth_manager.add_account(entry)
        return False
    target_key = _account_key(target)
    replaced = False
    appended = False

    def mutate(cfg):
        nonlocal replaced, appended
        accounts = cfg.setdefault("oauthAccounts", [])
        for acc in accounts:
            if _account_key(acc) != target_key:
                continue
            keep_models = acc.get("models")
            keep_max = acc.get("maxConcurrent")
            # 替换已有账号时，保留账号的手动启停/配额禁用状态；token/metadata 更新。
            keep_enabled = acc.get("enabled")
            keep_disabled_reason = acc.get("disabled_reason")
            keep_disabled_until = acc.get("disabled_until")
            acc.update(entry)
            if preserve_existing_settings:
                if keep_models is not None:
                    acc["models"] = keep_models
                if keep_max is not None:
                    acc["maxConcurrent"] = keep_max
                if keep_enabled is not None:
                    acc["enabled"] = keep_enabled
                acc["disabled_reason"] = keep_disabled_reason
                acc["disabled_until"] = keep_disabled_until
            replaced = True
            return
        accounts.append(entry)
        appended = True

    config.update(mutate)
    if appended:
        load_balancing.sync_channel_added(f"oauth:{_account_key(entry)}", "openai")
    return replaced


def _save_openai_entry_with_duplicate_policy(entry: dict) -> tuple[str, str]:
    """保存 OpenAI 账号；重复账号按 token 有效性决策。

    规则：同 workspace 已存在时更新；否则同 email 仅在唯一时作为旧数据兼容：
      - 现有有效、新 token 有效：保留现有（并写回刷新后的 token），跳过新 token
      - 现有无效、新 token 有效：用新 token 替换现有账号
      - 不存在：新增
    """
    email = entry.get("email", "")
    existing = _find_openai_existing_for_entry(entry)
    if not existing:
        oauth_manager.add_account(entry)
        return "added", "新增"

    existing_entry, _, existing_err = _refresh_openai_rt_to_entry(
        existing.get("refresh_token", ""),
        email_hint=email,
        workspace_id=_openai_workspace_id(existing),
        org_id=existing.get("organization_id") or "",
    )
    if existing_entry is not None:
        _upsert_openai_account_entry(existing_entry, preserve_existing_settings=True)
        return "skipped", "现有 token 有效，已保留现有账号"

    _upsert_openai_account_entry(entry, preserve_existing_settings=True)
    return "replaced", "现有 token 无效，已用新 token 替换"


def _finish_openai_add(chat_id: int, tok: dict, *, source: str) -> None:
    """共用保存路径：从 token 解 email/workspace → 按重复策略保存 → 回报。"""
    try:
        entry, meta = _openai_token_to_entry(tok)
        action, action_msg = _save_openai_entry_with_duplicate_policy(entry)
    except Exception as exc:
        ui.send_result(
            chat_id,
            f"❌ 保存失败: <code>{ui.escape_html(str(exc))[:500]}</code>",
            **_OA_NAV_OPENAI,
        )
        return

    quota_note = ""
    saved_acc = _find_openai_existing_for_entry(entry)
    if saved_acc is not None:
        saved_ak = _account_key(saved_acc)
        usage_result = _fetch_and_save_usage_sync(saved_ak, email=saved_acc.get("email") or entry.get("email") or "")
        if isinstance(usage_result, Exception):
            quota_note = "\n额度: <code>未获取成功，稍后可手动刷新</code>"
            print(f"[oauth_menu] openai usage fetch after add failed for {saved_ak}: {usage_result}")
        else:
            _evaluate_quota_action(saved_ak, usage_result)
            parts = []
            for label, util in zip(("5h", "7d", "30d"), oauth_manager.extract_utils_percent(usage_result)[:3]):
                if util is not None:
                    parts.append(f"{label} {util:.0f}%")
            quota_note = "\n额度: <code>" + ui.escape_html(" / ".join(parts) or "已获取") + "</code>"

    plan = meta.get("plan_type") or "?"
    workspace = meta.get("workspace_name") or meta.get("workspace_type") or ""
    ws_line = f"工作区: <code>{ui.escape_html(workspace)}</code>\n" if workspace else ""
    sub_exp = meta.get("subscription_expires_at") or ""
    sub_line = f"订阅到期时间: <code>{_format_bjt(sub_exp)}</code>\n" if sub_exp else ""
    title = {
        "added": "✅ <b>OpenAI OAuth 账户已添加</b>",
        "replaced": "✅ <b>OpenAI OAuth 账户已更新</b>",
        "skipped": "✅ <b>OpenAI OAuth 账户已存在</b>",
    }.get(action, "✅ <b>OpenAI OAuth 账户已处理</b>")
    lb_hint = (
        "\n已加入负载均衡优先级队列末尾，如需调整请进入「负载均衡」。"
        if action == "added" and load_balancing.is_initialized() else ""
    )
    ui.send_result(
        chat_id,
        f"{title}\n\n"
        f"Email: <code>{ui.escape_html(meta.get('email') or entry.get('email') or '')}</code>\n"
        f"套餐计划: <code>{ui.escape_html(plan)}</code>\n"
        f"{sub_line}"
        f"{ws_line}"
        f"过期: <code>{_fmt_time_full(meta.get('expired'))}</code>{quota_note}\n"
        f"处理: <code>{ui.escape_html(action_msg)}</code>\n"
        f"来源: <code>{source}</code>{lb_hint}",
        **_OA_NAV_OPENAI,
    )


# ─── OpenAI 批量导入（Sub2API / CPA）───────────────────────────────

_OPENAI_IMPORT_LABELS = {
    "sub2api": "Sub2API",
    "cpa": "CPA",
}


def on_import_openai_start(chat_id: int, message_id: int, cb_id: str, kind: str) -> None:
    kind = (kind or "").strip().lower()
    label = _OPENAI_IMPORT_LABELS.get(kind)
    if not label:
        ui.answer_cb(cb_id, "未知导入类型")
        return
    ui.answer_cb(cb_id)
    states.set_state(chat_id, "oa_openai_import", {"kind": kind})
    ui.edit(
        chat_id, message_id,
        f"<b>导入 {label} 账户</b>\n\n"
        "请上传 <code>.zip</code> / <code>.json</code> 文件，或直接粘贴 JSON 文本。\n\n"
        "我会只提取 <code>email</code> 与 <code>refresh_token</code>，随后复用"
        "「OpenAI 粘贴 refresh_token」逻辑刷新并导入。\n\n"
        "<i>导入前会先展示识别到的邮箱列表，请确认后再写入配置。</i>",
        reply_markup=ui.inline_kb([[ui.btn("❌ 取消", "oa:import_cancel")]]),
    )


def _parse_openai_import_or_report(chat_id: int, kind: str, payload, *, filename: str = "") -> list[dict] | None:
    try:
        candidates = parse_openai_import_payload(kind, payload, filename=filename)
    except OpenAIImportParseError as exc:
        ui.send_result(
            chat_id,
            f"❌ 解析失败: <code>{ui.escape_html(str(exc))[:800]}</code>",
            back_label="◀ 返回新增账户", back_callback="oa:add",
        )
        return None
    except Exception as exc:
        ui.send_result(
            chat_id,
            f"❌ 解析失败: <code>{ui.escape_html(str(exc))[:800]}</code>",
            back_label="◀ 返回新增账户", back_callback="oa:add",
        )
        return None
    return [c.as_state_item() for c in candidates]


def _show_openai_import_preview(chat_id: int, kind: str, items: list[dict]) -> None:
    label = _OPENAI_IMPORT_LABELS.get(kind, kind.upper())
    states.set_state(chat_id, "oa_openai_import_confirm", {"kind": kind, "items": items})
    lines = [f"<b>导入 {label} 账户</b>", "", "当前识别到："]
    max_show = 30
    for i, item in enumerate(items[:max_show], 1):
        email = item.get("email") or "?"
        source = item.get("source") or ""
        suffix = f" <i>({ui.escape_html(source)})</i>" if source and len(items) <= 10 else ""
        lines.append(f"{i}. <code>{ui.escape_html(email)}</code>{suffix}")
    if len(items) > max_show:
        lines.append(f"... 另有 {len(items) - max_show} 个")
    lines.extend(["", "是否立即导入？"])
    ui.send(
        chat_id,
        "\n".join(lines),
        reply_markup=ui.inline_kb([
            [ui.btn("❌ 取消", "oa:import_cancel"), ui.btn("✅ 导入", "oa:import_exec")],
        ]),
    )


def on_import_openai_text_input(chat_id: int, text: str) -> None:
    state = states.get_state(chat_id)
    data = (state.get("data") or {}) if state else {}
    kind = data.get("kind", "")
    if kind not in _OPENAI_IMPORT_LABELS:
        states.pop_state(chat_id)
        ui.send_result(chat_id, "❌ 导入会话已失效，请重新开始。", **_OA_NAV_OPENAI)
        return
    items = _parse_openai_import_or_report(chat_id, kind, text or "", filename="pasted-json")
    if items is None:
        return
    _show_openai_import_preview(chat_id, kind, items)


def on_import_openai_document_input(chat_id: int, msg: dict) -> None:
    state = states.get_state(chat_id)
    data = (state.get("data") or {}) if state else {}
    kind = data.get("kind", "")
    if kind not in _OPENAI_IMPORT_LABELS:
        states.pop_state(chat_id)
        ui.send_result(chat_id, "❌ 导入会话已失效，请重新开始。", **_OA_NAV_OPENAI)
        return
    doc = msg.get("document") or {}
    file_id = doc.get("file_id") or ""
    filename = doc.get("file_name") or ""
    if not file_id:
        ui.send_result(chat_id, "❌ 没有拿到文件 ID，请重新上传。", **_OA_NAV_OPENAI)
        return
    try:
        payload, tg_path = ui.download_file(file_id, max_bytes=10 * 1024 * 1024)
    except Exception as exc:
        ui.send_result(
            chat_id,
            f"❌ 文件下载失败: <code>{ui.escape_html(str(exc))[:500]}</code>",
            **_OA_NAV_OPENAI,
        )
        return
    if not filename:
        filename = tg_path.rsplit("/", 1)[-1] or "uploaded-file"
    items = _parse_openai_import_or_report(chat_id, kind, payload, filename=filename)
    if items is None:
        return
    _show_openai_import_preview(chat_id, kind, items)


def on_import_openai_cancel(chat_id: int, message_id: int, cb_id: str) -> None:
    states.pop_state(chat_id)
    on_add_menu(chat_id, message_id, cb_id)


def _format_import_error(exc: Exception | None) -> str:
    if exc is None:
        return "未知错误"
    return str(exc)[:240]


def _import_candidate_with_policy(item: dict) -> tuple[str, str, str]:
    """导入单个候选；返回 (status, email, message)。

    新 token 保存/替换成功后会 best-effort 拉一次 wham/usage 写 quota cache；
    失败不影响账号导入，只把提示拼进 message。
    """
    email_hint = str(item.get("email") or "").strip()
    rt = str(item.get("refresh_token") or "").strip()
    if not rt:
        return "failed", email_hint or "?", "缺少 refresh_token"

    entry, meta, import_err = _refresh_openai_rt_to_entry(rt, email_hint=email_hint)
    if entry is not None:
        action, msg = _save_openai_entry_with_duplicate_policy(entry)
        workspace = meta.get("workspace_name") or meta.get("workspace_type") or "workspace"
        plan = meta.get("plan_type") or "?"
        quota_msg = ""
        saved = _find_openai_existing_for_entry(entry)
        if saved is not None:
            ak = _account_key(saved)
            usage = _fetch_and_save_usage_sync(ak, email=saved.get("email") or entry.get("email") or email_hint)
            if isinstance(usage, Exception):
                quota_msg = "；额度未获取成功"
                print(f"[oauth_menu] openai import usage fetch failed for {ak}: {usage}")
            else:
                _evaluate_quota_action(ak, usage)
                quota_msg = "；额度已获取"
        return action, meta.get("email") or entry.get("email") or email_hint, f"{workspace} / {plan}；{msg}{quota_msg}"

    # 新 token 无效时，还没有可信 workspace identity，只能做 legacy 兜底：
    # 同邮箱恰好一个账号时，验证现有 token；多 workspace 时 _find_openai_account_by_email
    # 会返回 None，避免 Personal/Team 串号。
    existing = _find_openai_account_by_email(email_hint)
    if existing is not None:
        existing_entry, _, existing_err = _refresh_openai_rt_to_entry(
            existing.get("refresh_token", ""),
            email_hint=email_hint,
            workspace_id=_openai_workspace_id(existing),
            org_id=existing.get("organization_id") or "",
        )
        if existing_entry is not None:
            _upsert_openai_account_entry(existing_entry, preserve_existing_settings=True)
            quota_msg = ""
            refreshed = _find_openai_existing_for_entry(existing_entry)
            if refreshed is not None:
                ak = _account_key(refreshed)
                usage = _fetch_and_save_usage_sync(ak, email=refreshed.get("email") or email_hint)
                if isinstance(usage, Exception):
                    quota_msg = "；额度未获取成功"
                    print(f"[oauth_menu] openai import existing usage fetch failed for {ak}: {usage}")
                else:
                    _evaluate_quota_action(ak, usage)
                    quota_msg = "；额度已获取"
            return "skipped", email_hint, "导入 token 无效，现有 token 有效，已保留现有账号" + quota_msg
        return "failed", email_hint, (
            "导入 token 与现有 token 均无效；"
            f"导入错误: {_format_import_error(import_err)}；现有错误: {_format_import_error(existing_err)}"
        )

    return "failed", email_hint or "?", _format_import_error(import_err)


def on_import_openai_exec(chat_id: int, message_id: int, cb_id: str) -> None:
    state = states.pop_state(chat_id)
    if not state or state.get("action") != "oa_openai_import_confirm":
        ui.answer_cb(cb_id, "导入会话已失效", show_alert=True)
        return
    data = state.get("data") or {}
    kind = data.get("kind", "")
    label = _OPENAI_IMPORT_LABELS.get(kind, kind.upper())
    items = list(data.get("items") or [])
    if not items:
        ui.answer_cb(cb_id, "没有可导入账号", show_alert=True)
        return

    ui.answer_cb(cb_id, "开始导入…")
    ui.edit(chat_id, message_id, f"正在导入 {label} 账户，共 {len(items)} 个，请稍等…")

    buckets = {"added": [], "replaced": [], "skipped": [], "failed": []}
    for item in items:
        status, email, msg = _import_candidate_with_policy(item)
        buckets.setdefault(status, []).append((email, msg))

    lines = [f"<b>导入 {label} 账户完成</b>", ""]
    if buckets["added"]:
        lines.append(f"✅ 新增 {len(buckets['added'])} 个")
        for email, _ in buckets["added"][:20]:
            lines.append(f"• <code>{ui.escape_html(email)}</code>")
        if len(buckets["added"]) > 20:
            lines.append(f"• ... 另有 {len(buckets['added']) - 20} 个")
        lines.append("")
    if buckets["replaced"]:
        lines.append(f"🔁 替换 {len(buckets['replaced'])} 个（现有 token 无效，已用导入 token）")
        for email, _ in buckets["replaced"][:20]:
            lines.append(f"• <code>{ui.escape_html(email)}</code>")
        if len(buckets["replaced"]) > 20:
            lines.append(f"• ... 另有 {len(buckets['replaced']) - 20} 个")
        lines.append("")
    if buckets["skipped"]:
        lines.append(f"⚠️ 跳过 {len(buckets['skipped'])} 个")
        for email, msg in buckets["skipped"][:20]:
            lines.append(f"• <code>{ui.escape_html(email)}</code>：{ui.escape_html(msg)}")
        if len(buckets["skipped"]) > 20:
            lines.append(f"• ... 另有 {len(buckets['skipped']) - 20} 个")
        lines.append("")
    if buckets["failed"]:
        lines.append(f"❌ 失败 {len(buckets['failed'])} 个")
        for email, msg in buckets["failed"][:20]:
            lines.append(f"• <code>{ui.escape_html(email)}</code>：{ui.escape_html(msg)}")
        if len(buckets["failed"]) > 20:
            lines.append(f"• ... 另有 {len(buckets['failed']) - 20} 个")
        lines.append("")
    if not any(buckets.values()):
        lines.append("没有账号被处理。")

    ui.edit(
        chat_id, message_id,
        "\n".join(lines).rstrip(),
        reply_markup=ui.inline_kb([[ui.btn("◀ 返回 OAuth 列表", "menu:oauth")]]),
    )


# ─── 移除失效账户 ─────────────────────────────────────────────────

def _invalid_accounts() -> list[dict]:
    return [
        a for a in oauth_manager.list_accounts()
        if a.get("email") and a.get("disabled_reason") == "auth_error"
    ]


def _invalid_select_token(account_key: str) -> str:
    return ui.register_code(account_key)


def _invalid_account_keys() -> list[str]:
    return [_account_key(a) for a in _invalid_accounts()]


def _render_invalid_remove(chat_id: int, message_id: int, *, selected: set[str] | None = None) -> None:
    selected = set(selected or set())
    invalid = _invalid_accounts()
    selected &= {_account_key(a) for a in invalid}

    lines = [
        "<b>移除失效账户</b>",
        "",
        "请在下方选择要移除的失效账户",
    ]
    rows: list[list[dict]] = []
    if not invalid:
        lines.append("")
        lines.append("当前没有认证失效账户。")
    else:
        for idx, acc in enumerate(invalid, 1):
            ak = _account_key(acc)
            email = acc.get("email") or "?"
            mark = "✅ " if ak in selected else "☐ "
            lines.append(f"{idx}. <code>{ui.escape_html(email)}</code>")
            rows.append([ui.btn(f"{mark}{email}", f"oa:invalid:toggle:{_invalid_select_token(ak)}")])

    rows.append([
        ui.btn("全部移除", "oa:invalid:remove_all"),
        ui.btn("移除选中", "oa:invalid:remove_selected"),
    ])
    rows.append([
        ui.btn("返回主菜单", "menu:main"),
        ui.btn("返回账户管理", "menu:oauth"),
    ])
    states.set_state(chat_id, "oa_invalid_remove", {"selected": sorted(selected)})
    ui.edit(chat_id, message_id, "\n".join(lines), reply_markup=ui.inline_kb(rows))


def on_invalid_remove_start(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    _render_invalid_remove(chat_id, message_id, selected=set())


def on_invalid_remove_toggle(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    ak = _resolve_to_account_key(ui.resolve_code(short))
    if ak is None:
        ui.answer_cb(cb_id, "短码已失效")
        return
    if ak not in set(_invalid_account_keys()):
        ui.answer_cb(cb_id, "该账户已不在失效列表")
        _render_invalid_remove(chat_id, message_id, selected=set())
        return
    state = states.get_state(chat_id) or {}
    selected = set((state.get("data") or {}).get("selected") or [])
    if ak in selected:
        selected.remove(ak)
        ui.answer_cb(cb_id, "已取消选择")
    else:
        selected.add(ak)
        ui.answer_cb(cb_id, "已选择")
    _render_invalid_remove(chat_id, message_id, selected=selected)


def _delete_accounts_by_keys(keys: list[str]) -> tuple[int, list[str]]:
    deleted = 0
    failed: list[str] = []
    for ak in keys:
        acc = oauth_manager.get_account(ak)
        email = (acc or {}).get("email") or _split_ak(ak)[1]
        try:
            oauth_manager.delete_account(ak)
            deleted += 1
        except Exception as exc:
            failed.append(f"{email}: {exc}")
    return deleted, failed


def on_invalid_remove_exec(chat_id: int, message_id: int, cb_id: str, *, all_items: bool) -> None:
    invalid_keys = _invalid_account_keys()
    if all_items:
        targets = invalid_keys
    else:
        state = states.get_state(chat_id) or {}
        selected = set((state.get("data") or {}).get("selected") or [])
        valid = set(invalid_keys)
        targets = [ak for ak in invalid_keys if ak in selected and ak in valid]

    if not targets:
        ui.answer_cb(cb_id, "没有选择可移除的失效账户", show_alert=True)
        return

    ui.answer_cb(cb_id, "正在移除…")
    deleted, failed = _delete_accounts_by_keys(targets)
    states.pop_state(chat_id)

    lines = ["<b>移除失效账户完成</b>", "", f"✅ 已移除 {deleted} 个"]
    if failed:
        lines.append(f"❌ 失败 {len(failed)} 个")
        for item in failed[:20]:
            lines.append(f"• <code>{ui.escape_html(item)}</code>")
        if len(failed) > 20:
            lines.append(f"• ... 另有 {len(failed) - 20} 个")
    ui.edit(
        chat_id, message_id,
        "\n".join(lines),
        reply_markup=ui.inline_kb([
            [ui.btn("返回主菜单", "menu:main"), ui.btn("返回账户管理", "menu:oauth")],
        ]),
    )


# ─── 路由分发 ─────────────────────────────────────────────────────

def on_clear_all_errors(chat_id: int, message_id: int, cb_id: str, page: int = 1, filter_key: str = _FILTER_ALL) -> None:
    """清除所有 OAuth 账户的模型冷却（按 oauth: 前缀批量 clear）。"""
    from ... import cooldown as _cd
    cd_keys = sorted({
        e["channel_key"] for e in _cd.active_entries()
        if e.get("channel_key", "").startswith("oauth:")
    })
    cleared = 0
    for ck in cd_keys:
        _cd.clear(ck, model=None)
        cleared += 1
    ui.answer_cb(cb_id, f"已清除 {cleared} 个账户的冷却")
    show(chat_id, message_id, page=page, filter_key=filter_key)


def handle_callback(chat_id: int, message_id: int, cb_id: str, data: str) -> bool:
    if data == "menu:oauth":
        show(chat_id, message_id, cb_id)
        return True
    if data == "oa:settings":
        on_settings(chat_id, message_id, cb_id)
        return True
    if data == "oa:usage_mode:toggle":
        on_toggle_usage_display_mode(chat_id, message_id, cb_id)
        return True
    if data == "oa:cch_toggle":
        on_toggle_cch_mode(chat_id, message_id, cb_id)
        return True
    if data == "oa:quota":
        on_quota_menu(chat_id, message_id, cb_id)
        return True
    if data == "oa:quota_toggle":
        on_quota_toggle(chat_id, message_id, cb_id)
        return True
    if data == "oa:edit:quota_interval":
        on_edit_quota_interval(chat_id, message_id, cb_id)
        return True
    if data == "oa:edit:quota_threshold":
        on_edit_quota_threshold(chat_id, message_id, cb_id)
        return True
    if data == "oa:refresh_all" or data.startswith("oa:refresh_all:"):
        page, filter_key = _parse_page_filter(data[len("oa:refresh_all:"):] if data.startswith("oa:refresh_all:") else "")
        on_refresh_all(chat_id, message_id, cb_id, page=page, filter_key=filter_key)
        return True
    if data.startswith("oa:page:"):
        payload = data.split(":", 2)[2]
        if payload == "noop":
            ui.answer_cb(cb_id, "当前页")
            return True
        page, filter_key = _parse_page_filter(payload)
        show(chat_id, message_id, cb_id, page=page, filter_key=filter_key)
        return True
    if data.startswith("oa:sort:"):
        page, filter_key = _parse_page_filter(data.split(":", 2)[2])
        on_sort_start(chat_id, message_id, cb_id, page=page, filter_key=filter_key)
        return True
    if data.startswith("oa:sort_sel:"):
        on_sort_select(chat_id, message_id, cb_id, data.split(":", 2)[2])
        return True
    if data.startswith("oa:sort_mv:"):
        on_sort_move(chat_id, message_id, cb_id, data.split(":", 2)[2])
        return True
    if data == "oa:sort_reset":
        on_sort_reset(chat_id, message_id, cb_id)
        return True
    if data == "oa:sort_save":
        on_sort_save(chat_id, message_id, cb_id)
        return True
    if data == "oa:sort_cancel":
        on_sort_cancel(chat_id, message_id, cb_id)
        return True
    if data == "oa:clear_all_errors" or data.startswith("oa:clear_all_errors:"):
        page, filter_key = _parse_page_filter(data[len("oa:clear_all_errors:"):] if data.startswith("oa:clear_all_errors:") else "")
        on_clear_all_errors(chat_id, message_id, cb_id, page=page, filter_key=filter_key)
        return True
    if data == "oa:add":
        on_add_menu(chat_id, message_id, cb_id)
        return True
    if data == "oa:add:claude":
        on_add_claude(chat_id, message_id, cb_id)
        return True
    if data == "oa:add:openai":
        on_add_openai(chat_id, message_id, cb_id)
        return True
    if data == "oa:login":
        on_login_start(chat_id, message_id, cb_id)
        return True
    if data == "oa:set_json":
        on_set_json_start(chat_id, message_id, cb_id)
        return True
    if data == "oa:login:openai":
        on_login_openai_start(chat_id, message_id, cb_id)
        return True
    if data == "oa:login:openai:regen":
        on_login_openai_regen(chat_id, message_id, cb_id)
        return True
    if data == "oa:set_rt:openai":
        on_set_rt_openai_start(chat_id, message_id, cb_id)
        return True
    if data.startswith("oa:import:"):
        on_import_openai_start(chat_id, message_id, cb_id, data.rsplit(":", 1)[-1])
        return True
    if data == "oa:import_cancel":
        on_import_openai_cancel(chat_id, message_id, cb_id)
        return True
    if data == "oa:import_exec":
        on_import_openai_exec(chat_id, message_id, cb_id)
        return True
    if data == "oa:invalid:list":
        on_invalid_remove_start(chat_id, message_id, cb_id)
        return True
    if data.startswith("oa:invalid:toggle:"):
        on_invalid_remove_toggle(chat_id, message_id, cb_id, data.split(":", 3)[3])
        return True
    if data == "oa:invalid:remove_all":
        on_invalid_remove_exec(chat_id, message_id, cb_id, all_items=True)
        return True
    if data == "oa:invalid:remove_selected":
        on_invalid_remove_exec(chat_id, message_id, cb_id, all_items=False)
        return True

    if data.startswith("oa:view:"):
        short, page, filter_key = _split_short_page_filter(data.split(":", 2)[2])
        on_view(chat_id, message_id, cb_id, short, page=page, filter_key=filter_key)
        return True
    if data.startswith("oa:refresh_token:"):
        short, page, filter_key = _split_short_page_filter(data.split(":", 2)[2])
        on_refresh_token(chat_id, message_id, cb_id, short, page=page, filter_key=filter_key)
        return True
    if data.startswith("oa:refresh_usage:"):
        short, page, filter_key = _split_short_page_filter(data.split(":", 2)[2])
        on_refresh_usage(chat_id, message_id, cb_id, short, page=page, filter_key=filter_key)
        return True
    if data.startswith("oa:clear_errors:"):
        short, page, filter_key = _split_short_page_filter(data.split(":", 2)[2])
        on_clear_errors(chat_id, message_id, cb_id, short, page=page, filter_key=filter_key)
        return True
    if data.startswith("oa:reset_quota_ask:"):
        short, page, filter_key = _split_short_page_filter(data.split(":", 2)[2])
        on_reset_quota_ask(chat_id, message_id, cb_id, short, page=page, filter_key=filter_key)
        return True
    if data.startswith("oa:reset_quota_confirm:"):
        short, page, filter_key = _split_short_page_filter(data.split(":", 2)[2])
        on_reset_quota_confirm(chat_id, message_id, cb_id, short, page=page, filter_key=filter_key)
        return True
    if data.startswith("oa:reset_quota:"):
        short, page, filter_key = _split_short_page_filter(data.split(":", 2)[2])
        on_reset_quota(chat_id, message_id, cb_id, short, page=page, filter_key=filter_key)
        return True
    if data.startswith("oa:clear_affinity:"):
        short, page, filter_key = _split_short_page_filter(data.split(":", 2)[2])
        on_clear_affinity(chat_id, message_id, cb_id, short, page=page, filter_key=filter_key)
        return True
    if data.startswith("oa:toggle:"):
        short, page, filter_key = _split_short_page_filter(data.split(":", 2)[2])
        on_toggle(chat_id, message_id, cb_id, short, page=page, filter_key=filter_key)
        return True
    if data.startswith("oa:delete_ask:"):
        short, page, filter_key = _split_short_page_filter(data.split(":", 2)[2])
        on_delete_ask(chat_id, message_id, cb_id, short, page=page, filter_key=filter_key)
        return True
    if data.startswith("oa:delete_exec:"):
        short, page, filter_key = _split_short_page_filter(data.split(":", 2)[2])
        on_delete_exec(chat_id, message_id, cb_id, short, page=page, filter_key=filter_key)
        return True
    if data.startswith("oa:emax:"):
        short, page, filter_key = _split_short_page_filter(data.split(":", 2)[2])
        on_edit_max_concurrent(chat_id, message_id, cb_id, short, page=page, filter_key=filter_key)
        return True
    return False


def handle_text_state(chat_id: int, action: str, text: str) -> bool:
    if action == "oa_login_code":
        on_login_code_input(chat_id, text)
        return True
    if action == "oa_set_json":
        on_set_json_input(chat_id, text)
        return True
    if action == "oa_openai_code":
        on_login_openai_code_input(chat_id, text)
        return True
    if action == "oa_openai_rt":
        on_set_rt_openai_input(chat_id, text)
        return True
    if action == "oa_openai_import":
        on_import_openai_text_input(chat_id, text)
        return True
    if action == "oa_emax":
        on_edit_max_concurrent_input(chat_id, text)
        return True
    if action == "oa_quota_interval":
        on_quota_interval_input(chat_id, text)
        return True
    if action == "oa_quota_threshold":
        on_quota_threshold_input(chat_id, text)
        return True
    return False


def handle_document_state(chat_id: int, action: str, msg: dict) -> bool:
    if action == "oa_openai_import":
        on_import_openai_document_input(chat_id, msg)
        return True
    return False


# ─── 并发上限编辑 ─────────────────────────────────────────────────

def on_edit_max_concurrent(chat_id: int, message_id: int, cb_id: str, short: str, page: int = 1, filter_key: str = _FILTER_ALL) -> None:
    ak = _resolve_to_account_key(ui.resolve_code(short))
    if ak is None:
        ui.answer_cb(cb_id, "短码已失效")
        return
    ui.answer_cb(cb_id)
    states.set_state(chat_id, "oa_emax", {"account_key": ak, "short": short, "page": page, "filter_key": _normalize_filter(filter_key)})
    ui.edit(
        chat_id, message_id,
        "请输入该 OAuth 账户的并发上限（整数 ≥0）：\n"
        "• <code>0</code> = 使用全局默认（「⚙ 系统设置 → ⚡ 并发限制」里配的 defaultMaxConcurrent）\n"
        "• 正整数 = 该账户同时允许最多 N 个在途请求，超出则排队\n\n"
        "例：<code>3</code>",
        reply_markup=ui.inline_kb([[ui.btn("❌ 取消", f"oa:view:{_callback_payload(short, page, filter_key)}")]]),
    )


def on_edit_max_concurrent_input(chat_id: int, text: str) -> None:
    state = states.get_state(chat_id)
    data = (state.get("data") or {}) if state else {}
    ak = data.get("account_key")
    short = data.get("short", "")
    page = int(data.get("page") or 1)
    filter_key = _normalize_filter(data.get("filter_key"))
    if not ak:
        ui.send(chat_id, "❌ 状态已失效，请重新进入编辑")
        states.pop_state(chat_id)
        return
    try:
        v = int((text or "").strip())
        if v < 0:
            raise ValueError
    except ValueError:
        ui.send(chat_id, "❌ 需要非负整数，请重新输入：")
        return
    try:
        oauth_manager.update_max_concurrent(ak, v)
    except Exception as exc:
        ui.send(chat_id, f"❌ 失败: <code>{ui.escape_html(str(exc))}</code>")
        return
    states.pop_state(chat_id)
    label = "默认" if v == 0 else str(v)
    ui.send_result(
        chat_id, f"✅ 并发上限已更新为 <code>{label}</code>",
        extra_rows=[[ui.btn("◀ 返回账户详情", f"oa:view:{_callback_payload(short, page, filter_key)}")]],
        back_label="🏠 主菜单", back_callback="menu:main",
    )
