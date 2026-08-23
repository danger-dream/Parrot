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
状态机 action（xAI）：
  - `oa_xai_code`             ：等待用户粘贴 xAI / Grok OAuth 回调 URL
  - `oa_xai_rt`               ：等待用户粘贴 refresh_token 字符串
状态机 action（Antigravity）：
  - `oa_antigravity_code`     ：等待用户粘贴 Google / Antigravity 回调 URL
状态机 action（Cursor）：
  - `oa_cursor_login`         ：保存浏览器 PKCE 轮询会话，等待“已登录”按钮

注意：本模块所有 OAuth 远端交互都走 `oauth_manager` / `src.oauth.*`，已经有
mockMode 保护（`config.oauth.mockMode=true` 或 env DISABLE_OAUTH_NETWORK_CALLS=1）。
"""

from __future__ import annotations

import asyncio
import hashlib
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

from ... import (
    affinity, config, cooldown, cursor_reconcile, load_balancing, log_db,
    oauth_errors, oauth_manager, state_db,
)
from ...oauth_ids import account_key as _account_key, openai_account_identity_parts as _openai_identity_parts, openai_workspace_id as _openai_workspace_id, split_account_key as _split_ak
from ...oauth import antigravity as antigravity_provider, cursor as cursor_provider, openai as openai_provider, xai as xai_provider
from ...oauth.openai_import import OpenAIImportParseError, parse_openai_import_payload
from .. import menu_cache, states, ui
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


def _account_key_from_short(short: str) -> str | None:
    """Resolve an OAuth account code even after a process restart.

    ``ui.register_code`` is deterministic but its reverse map is in-memory.  Old
    Telegram messages survive restarts, so reconstruct the mapping from current
    account keys instead of falsely reporting that the account was deleted.
    """
    account_key = _resolve_to_account_key(ui.resolve_code(str(short or "")))
    if account_key and oauth_manager.get_account(account_key) is not None:
        return account_key
    wanted = str(short or "")
    for account in oauth_manager.list_accounts():
        candidate = oauth_manager.get_account_key(account)
        if ui.register_code(candidate) == wanted:
            return candidate
        email = str(account.get("email") or "")
        if email and ui.register_code(email) == wanted:
            return oauth_manager.get_account_key(account)
    return None


def _account_display(acc: dict) -> str:
    return str(acc.get("label") or acc.get("email") or "?")


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


def _overwrite_summary(entry: dict) -> str:
    provider = oauth_manager.provider_of(entry)
    label = str(entry.get("label") or entry.get("email") or "?")
    lines = [
        "检测到相同身份的 OAuth 账户。是否用本次登录结果覆盖凭据和最新资料？",
        "",
        f"Provider: <code>{ui.escape_html(provider)}</code>",
        f"账户: <code>{ui.escape_html(label)}</code>",
    ]
    if provider == "openai":
        workspace = entry.get("workspace_name") or entry.get("workspace_type") or entry.get("workspace_id")
        if workspace:
            lines.append(f"工作区: <code>{ui.escape_html(str(workspace))}</code>")
    if provider == "antigravity":
        project_id = str(entry.get("project_id") or entry.get("projectId") or "").strip()
        if project_id:
            lines.append(f"Project: <code>{ui.escape_html(project_id)}</code>")
    plan = entry.get("plan_type")
    if plan:
        lines.append(f"套餐: <code>{ui.escape_html(str(plan))}</code>")
    lines.extend(("", "取消或会话过期不会修改现有账户。"))
    return "\n".join(lines)


def _persist_new_or_stage_overwrite(
    chat_id: int, entry: dict, *, source: str, usage: dict | None = None,
    message_id: int | None = None,
) -> str:
    """Add a new identity immediately, or stage an exact-identity overwrite."""
    provider = oauth_manager.provider_of(entry)
    email = str(entry.get("email") or "")
    configured = oauth_manager.list_accounts()
    if provider == "openai" and not _openai_workspace_id(entry):
        raise ValueError("OpenAI token 缺少 workspace identity，无法安全判断账户")
    if provider in ("xai", "cursor"):
        incoming_subject = str(entry.get("subject") or entry.get("sub") or "")
        for account in configured:
            if oauth_manager.provider_of(account) != provider or str(account.get("email") or "") != email:
                continue
            old_subject = str(account.get("subject") or account.get("sub") or "")
            if bool(old_subject) != bool(incoming_subject):
                raise ValueError(f"{provider} legacy email fallback 会改变 canonical identity，请先移除或迁移旧账户")
    duplicate = oauth_manager.find_exact_identity(entry)
    if duplicate is None:
        added = oauth_manager.add_account_if_identity_absent(entry)
        if added.get("status") != "added":
            raise RuntimeError("账户在保存前已并发出现，请重新登录确认")
        return "added"
    target_key, _snapshot = duplicate
    nonce = secrets.token_urlsafe(12)
    states.set_state(chat_id, "oa_oauth_overwrite_confirm", {
        "nonce": nonce,
        "target_key": target_key,
        "provider": oauth_manager.provider_of(entry),
        "entry": entry,
        "source": source,
        "usage": usage,
    })
    markup = ui.inline_kb([[
        ui.btn("✅ 覆盖", f"oa:overwrite:confirm:{nonce}"),
        ui.btn("❌ 取消", f"oa:overwrite:cancel:{nonce}"),
    ]])
    text = _overwrite_summary(entry)
    if message_id is None:
        ui.send_result(chat_id, text, extra_rows=[[
            ui.btn("✅ 覆盖", f"oa:overwrite:confirm:{nonce}"),
            ui.btn("❌ 取消", f"oa:overwrite:cancel:{nonce}"),
        ]], back_label="◀ 返回新增账户", back_callback="oa:add")
    else:
        ui.edit(chat_id, message_id, text, reply_markup=markup)
    return "staged"


def _overwrite_state(chat_id: int, nonce: str, *, consume: bool) -> dict | None:
    state = states.get_state(chat_id)
    if not state or state.get("action") != "oa_oauth_overwrite_confirm":
        return None
    data = state.get("data") or {}
    if not secrets.compare_digest(str(data.get("nonce") or ""), str(nonce or "")):
        return None
    if consume:
        state = states.pop_state(chat_id)
        return (state or {}).get("data") or None
    return data


def on_oauth_overwrite_cancel(chat_id: int, message_id: int, cb_id: str, nonce: str) -> None:
    data = _overwrite_state(chat_id, nonce, consume=True)
    if data is None:
        ui.answer_cb(cb_id, "确认会话已失效，请重新登录", show_alert=True)
        return
    ui.answer_cb(cb_id, "已取消")
    ui.edit(chat_id, message_id, "✅ 已取消覆盖，现有账户未作任何修改。",
            reply_markup=ui.inline_kb([[ui.btn("◀ 返回新增账户", "oa:add")]]))


def on_oauth_overwrite_confirm(chat_id: int, message_id: int, cb_id: str, nonce: str) -> None:
    # Consume before touching config so double-click can never repeat a replace.
    data = _overwrite_state(chat_id, nonce, consume=True)
    if data is None:
        ui.answer_cb(cb_id, "确认会话已失效，请重新登录", show_alert=True)
        return
    entry = data.get("entry") or {}
    target_key = str(data.get("target_key") or "")
    result = oauth_manager.replace_exact_identity(target_key, entry)
    if result.get("status") != "replaced":
        ui.answer_cb(cb_id, "账户已变化，请重新登录", show_alert=True)
        ui.edit(chat_id, message_id, "❌ 目标账户已消失或身份发生变化；未写入本次登录结果。",
                reply_markup=ui.inline_kb([[ui.btn("◀ 重新登录", "oa:add")]]))
        return

    provider = str(data.get("provider") or oauth_manager.provider_of(entry))
    usage = data.get("usage")
    quota_note = ""
    if provider == "cursor" and isinstance(usage, dict):
        error = _save_usage_to_quota_cache(target_key, usage, email=entry.get("email") or "")
        if error is None:
            _evaluate_quota_action(target_key, usage)
        else:
            quota_note = "\n⚠️ 新额度快照保存失败，可稍后手动刷新。"
    elif provider == "openai":
        fetched = _fetch_and_save_usage_sync(target_key, email=entry.get("email") or "")
        if isinstance(fetched, Exception):
            quota_note = "\n⚠️ 新额度快照获取失败，可稍后手动刷新。"
        else:
            _evaluate_quota_action(target_key, fetched)
    ui.answer_cb(cb_id, "覆盖成功")
    ui.edit(
        chat_id, message_id,
        f"✅ {_provider_tag(provider)} <b>OAuth 账户已覆盖</b>\n\n"
        f"账户: <code>{ui.escape_html(str(entry.get('label') or entry.get('email') or '?'))}</code>\n"
        "账户主键、历史统计、优先级和用户设置均保持不变。" + quota_note,
        reply_markup=ui.inline_kb([[ui.btn("◀ 返回 OAuth 列表", "menu:oauth")]]),
    )


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
    try:
        raw = json.loads(row.get("raw_data") or "{}")
    except Exception:
        raw = {}
    if isinstance(raw, dict):
        ag = raw.get("antigravity")
        if isinstance(ag, dict) and ag.get("quota_supported"):
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
    if prov == "xai" and not _quota_cache_has_usage_signal(row):
        return True
    if prov == "antigravity" and not _quota_cache_has_usage_signal(row):
        return True
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


def _provider_tag(provider: str | None, *, full: bool = False, rich: bool = True) -> str:
    return ui.provider_tag(provider, full=full, rich=rich)


def _provider_label(provider: str | None, *, full: bool = False) -> str:
    return ui.provider_label(provider, full=full)


def _this_month_start_ts() -> float:
    """北京时间本月 00:00:00 的时间戳。"""
    return menu_cache.month_start_ts()


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


def _fmt_usd(v: float | int | None) -> str:
    return ui.fmt_usd(v)


def _fmt_credit_amount(v) -> str:
    if v is None:
        return "?"
    try:
        x = float(v)
    except (TypeError, ValueError):
        return "?"
    if abs(x - int(x)) < 1e-9:
        return f"{int(x):,}"
    return f"{x:,.2f}".rstrip("0").rstrip(".")


def _antigravity_raw_from_row(row: dict | None) -> dict:
    if not row or not row.get("raw_data"):
        return {}
    try:
        raw = json.loads(row.get("raw_data") or "{}")
    except Exception:
        return {}
    block = raw.get("antigravity") if isinstance(raw, dict) else None
    return block if isinstance(block, dict) else {}


def _antigravity_raw(account_key: str) -> dict:
    return _antigravity_raw_from_row(state_db.quota_load(account_key))


def _format_antigravity_credits_block(account_key: str, *, detail: bool = False) -> str:
    """Independent credits block. Never invent percent windows or currency."""
    row = state_db.quota_load(account_key)
    block = _antigravity_raw_from_row(row)
    acc = oauth_manager.get_account(account_key) or {}
    project_id = str(acc.get("project_id") or acc.get("projectId") or "").strip()
    if not block:
        lines = ["🚀 Antigravity · 未知", "Credits: <i>尚未获取</i>"]
        if detail and project_id:
            lines.append(f"Project: <code>{ui.escape_html(project_id)}</code>")
        return "\n".join(lines)

    tier = str(block.get("tier") or "").strip() or "未知"
    lines = [f"🚀 Antigravity · {ui.escape_html(tier)}"]
    if block.get("known"):
        amount = block.get("credit_amount")
        minimum = block.get("minimum_credit_amount")
        available = bool(block.get("available"))
        amount_txt = _fmt_credit_amount(amount)
        min_txt = _fmt_credit_amount(minimum)
        status = "可用" if available else "已耗尽"
        lines.append(f"Credits: {amount_txt}（最低 {min_txt}）· {status}")
    else:
        lines.append("Credits: <i>未知</i>")
    if detail:
        if project_id:
            lines.append(f"Project: <code>{ui.escape_html(project_id)}</code>")
        fetched = row.get("fetched_at") if row else None
        if fetched:
            try:
                fetched_iso = datetime.fromtimestamp(int(fetched) / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            except Exception:
                fetched_iso = ""
            if fetched_iso:
                lines.append(f"刷新: <code>{_fmt_time_full(fetched_iso)}</code>")
    return "\n".join(lines)


def _xai_raw_from_row(row: dict | None) -> dict:
    if not row or not row.get("raw_data"):
        return {}
    try:
        raw = json.loads(row.get("raw_data") or "{}")
    except Exception:
        return {}
    xai = raw.get("xai") if isinstance(raw, dict) else None
    return xai if isinstance(xai, dict) else {}


def _xai_raw(account_key: str) -> dict:
    return _xai_raw_from_row(state_db.quota_load(account_key))


def _xai_tier_label(xai: dict) -> str:
    user = xai.get("user") if isinstance(xai.get("user"), dict) else {}
    settings = xai.get("settings") if isinstance(xai.get("settings"), dict) else {}
    vals = []
    for v in (user.get("subscription_tier"), settings.get("subscription_tier_display")):
        s = str(v or "").strip()
        if s and s not in vals:
            vals.append(s)
    return " / ".join(vals)


def _xai_access_parts(xai: dict) -> list[str]:
    user = xai.get("user") if isinstance(xai.get("user"), dict) else {}
    settings = xai.get("settings") if isinstance(xai.get("settings"), dict) else {}
    blocked = bool(user.get("blocked") or user.get("user_blocked_reason") or user.get("team_blocked_reasons"))
    allow_access = settings.get("allow_access")
    has_code = user.get("has_grok_code_access")
    parts = []
    if blocked:
        parts.append("⚠ 账号受限")
    elif allow_access is False:
        parts.append("⚠ 访问受限")
    elif allow_access is True or has_code is True:
        parts.append("✅ 访问正常")
    if has_code is True:
        parts.append("Grok Code 可用")
    elif has_code is False:
        parts.append("Grok Code 不可用")
    return parts


def _format_xai_provider_line(account_key: str, *, detail: bool = False) -> str:
    xai = _xai_raw(account_key)
    if not xai or xai.get("source") == "unsupported":
        return ""
    lines: list[str] = []
    tier = _xai_tier_label(xai)
    access = _xai_access_parts(xai)
    if tier:
        if detail:
            lines.append(f"🏷 套餐: <code>{ui.escape_html(tier)}</code>")
        else:
            code_part = " · ✅ Grok Code 可用" if "Grok Code 可用" in access else ""
            lines.append(f"🏷 套餐: {ui.escape_html(tier)}{code_part}")
    if detail and access:
        icon = "⚠" if any(str(x).startswith("⚠") for x in access) else "✅"
        lines.append(f"{icon} 访问: <code>{ui.escape_html(' · '.join(access))}</code>")
    return "\n".join(lines) + ("\n" if lines and detail else "")


def _xai_used_remaining_percent(xai: dict, billing: dict) -> tuple[object, object]:
    """Map official Grok billing to used/remaining percent.

    A successful credits snapshot that has a billing window but no
    ``creditUsagePercent`` means unused quota (0% used / 100% remaining).
    Credits fetch failures stay unknown.
    """
    pct = billing.get("used_percent")
    remaining = billing.get("remaining_percent")
    errors = xai.get("errors") if isinstance(xai.get("errors"), dict) else {}
    if pct is None and not errors.get("credits"):
        if billing.get("period_type") or billing.get("period_start") or billing.get("period_end"):
            return 0.0, 100.0
    if pct is not None and remaining is None:
        try:
            remaining = 100.0 - float(pct)
        except (TypeError, ValueError):
            remaining = None
    return pct, remaining


def _format_xai_official_block(account_key: str, *, detail: bool = False) -> str:
    row = state_db.quota_load(account_key)
    xai = _xai_raw_from_row(row)
    if not xai or xai.get("source") == "unsupported":
        return "📊 官方额度: <i>尚未获取</i>" if not detail else "<b>📊 官方账单</b>\n尚未获取（点「刷新账单」拉取）"

    billing = xai.get("billing") if isinstance(xai.get("billing"), dict) else {}
    settings = xai.get("settings") if isinstance(xai.get("settings"), dict) else {}
    period_type = billing.get("period_type")
    pct, remaining_pct = _xai_used_remaining_percent(xai, billing)
    period_start = billing.get("period_start")
    period_end = billing.get("period_end")
    quota_label = "周额度" if period_type == "USAGE_PERIOD_TYPE_WEEKLY" else "官方额度"

    def _pct_text(value) -> str:
        try:
            return f"{float(value):.2f}%"
        except (TypeError, ValueError):
            return "?"

    if not detail:
        if pct is not None:
            reset = _format_bjt(period_end) if period_end else "?"
            return (
                f"📊 {quota_label}: 剩余 {_pct_text(remaining_pct)} · "
                f"已用 {_pct_text(pct)} · 重置 {ui.escape_html(reset)}"
            )
        return f"📊 {quota_label}: <i>上游未返回额度百分比</i>"

    lines = ["<b>📊 官方账单</b>"]
    if pct is not None:
        lines.append(
            f"{quota_label}: 剩余 <code>{_pct_text(remaining_pct)}</code>"
            f" · 已用 <code>{_pct_text(pct)}</code>"
        )
    else:
        lines.append(f"{quota_label}: <i>上游未返回额度百分比</i>")
    if period_start or period_end:
        lines.append(f"账期: <code>{_format_bjt(period_start)}</code> → <code>{_format_bjt(period_end)}</code>")
    if billing.get("on_demand_cap") is not None or billing.get("on_demand_used") is not None:
        lines.append(
            f"按需: 上限 <code>{_fmt_credit_amount(billing.get('on_demand_cap'))}</code>"
            f" · 已用 <code>{_fmt_credit_amount(billing.get('on_demand_used'))}</code>"
        )
    if billing.get("prepaid_balance") is not None:
        lines.append(f"预付余额: <code>{_fmt_credit_amount(billing.get('prepaid_balance'))}</code>")
    if billing.get("is_unified_billing_user") is not None:
        lines.append("统一计费: <code>是</code>" if billing.get("is_unified_billing_user") else "统一计费: <code>否</code>")
    if settings.get("default_model"):
        lines.append(f"默认模型: <code>{ui.escape_html(str(settings.get('default_model')))}</code>")
    comp_parts = []
    if settings.get("compaction_mode"):
        comp_parts.append(str(settings.get("compaction_mode")))
    if settings.get("flush_soft_threshold_tokens") is not None:
        comp_parts.append(f"flush {settings.get('flush_soft_threshold_tokens')} tokens")
    if comp_parts:
        lines.append(f"Compaction: <code>{ui.escape_html(' · '.join(comp_parts))}</code>")
    errors = xai.get("errors") if isinstance(xai.get("errors"), dict) else {}
    if errors:
        lines.append("⚠ 部分补充接口刷新失败，已保留主账单数据。")
    if row and row.get("fetched_at"):
        dt = datetime.fromtimestamp(int(row.get("fetched_at")) / 1000, tz=_BJT)
        lines.append(f"<i>更新于 {dt.strftime('%H:%M:%S')}</i>")
    return "\n".join(lines)


def _format_xai_spend_block(account_key: str, *, detail: bool = False,
                            month_stats: dict | None = None,
                            stats_loading: bool = False) -> str:
    """Grok/xAI OAuth 本地花费块。

    只展示 Parrot 本地上游尝试累计的金额与 Token，不做预算、进度条或
    百分比；不同结算来源在界面中统一合并为一个金额。
    """
    ck = f"oauth:{account_key}"
    since_ts = _this_month_start_ts()
    pricing_cfg = config.get().get("pricing", {})
    pricing_enabled = not isinstance(pricing_cfg, dict) or bool(
        pricing_cfg.get("enabled", True)
    )
    if month_stats is None:
        snapshot = menu_cache.PERIOD_STATS.peek(("period", int(since_ts))).value or {}
        month_stats = (snapshot.get("by_channel") or {}).get(ck)
    month = month_stats or {}
    prompt = ui.prompt_total(month.get("input") or 0, month.get("cache_creation") or 0, month.get("cache_read") or 0)
    output = int(month.get("output") or 0)
    cache_read = int(month.get("cache_read") or 0)

    money_line = (
        f"💵 本地计费: {ui.fmt_cost(month)}"
        if pricing_enabled
        else "💵 本地计费: 已关闭"
    )
    usage_line = f"💎 本地月度: ↑ {ui.fmt_tokens(prompt)} · ↓ {ui.fmt_tokens(output)}"
    if cache_read > 0:
        usage_line += f" · {ui.fmt_cache_phrase(cache_read, prompt)}"

    lines = ["<b>💵 Parrot 本地计费</b>"] if detail else []
    lines.append(usage_line)
    if month.get("avg_tps") is not None:
        lines.append(
            f"⚡️ TPS: 平均 {ui.fmt_tps(month.get('avg_tps'))} · "
            f"峰值 {ui.fmt_tps(month.get('max_tps'))} · "
            f"最低 {ui.fmt_tps(month.get('min_tps'))}"
        )
    lines.append(money_line)

    tier_counts = month.get("service_tier_counts") or {}
    if isinstance(tier_counts, dict) and tier_counts:
        parts = []
        for key in ("priority", "default"):
            n = int(tier_counts.get(key) or 0)
            if n > 0:
                parts.append(f"{key} {n} 次")
        for key in sorted(k for k in tier_counts.keys() if k not in {"priority", "default"}):
            try:
                n = int(tier_counts.get(key) or 0)
            except (TypeError, ValueError):
                n = 0
            if n > 0:
                parts.append(f"{ui.escape_html(str(key))} {n} 次")
        if parts:
            lines.append("🚀 服务层级: " + " · ".join(parts))
    return "\n".join(lines)

def _cursor_raw_from_row(row: dict | None) -> dict:
    if not isinstance(row, dict):
        return {}
    raw = row.get("raw_data")
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        payload = json.loads(raw)
    except Exception:
        return {}
    cursor = payload.get("cursor") if isinstance(payload, dict) else None
    return cursor if isinstance(cursor, dict) else {}


def _format_cursor_usage_block(account_key: str, *, detail: bool = False) -> str:
    row = state_db.quota_load(account_key)
    cursor = _cursor_raw_from_row(row)
    if not cursor:
        return "📊 Cursor 额度: <i>尚未获取</i>"

    def money(cents) -> str:
        try:
            return f"${float(cents) / 100:.2f}"
        except (TypeError, ValueError):
            return "—"

    used = cursor.get("total_spend_cents")
    limit = cursor.get("limit_cents")
    remaining = cursor.get("remaining_cents")
    total_util = cursor.get("total_utilization")
    auto_util = cursor.get("auto_percent_used")
    api_util = cursor.get("api_percent_used")
    reset = cursor.get("billing_cycle_end")

    lines: list[str] = []
    if limit is not None:
        if _usage_display_mode() == _USAGE_DISPLAY_REMAINING:
            lines.append(
                f"💳 包含额度: 剩余 <b>{money(remaining)}</b> / {money(limit)}"
                + (f" ({_usage_display_percent(total_util):.2f}%)" if total_util is not None else "")
            )
        else:
            lines.append(
                f"💳 包含额度: 已用 <b>{money(used)}</b> / {money(limit)}"
                + (f" ({_usage_display_percent(total_util):.2f}%)" if total_util is not None else "")
            )
    if auto_util is not None:
        lines.append(f"🧭 Cursor Models / Auto: {_format_usage_value_html(auto_util, decimals=2)}")
    if api_util is not None:
        lines.append(f"🧩 Other Models / API: {_format_usage_value_html(api_util, decimals=2)}")
    if reset:
        lines.append(f"🔄 周期重置: <code>{_fmt_time_full(reset)}</code>")
    status = cursor.get("subscription_status")
    if detail and status:
        lines.append(f"📋 订阅状态: <code>{ui.escape_html(status)}</code>")
    spend = cursor.get("spend_limit") if isinstance(cursor.get("spend_limit"), dict) else {}
    if detail and spend.get("limit_cents") is not None:
        lines.append(
            f"💰 额外消费上限: {money(spend.get('limit_cents'))} · "
            f"剩余 {money(spend.get('remaining_cents'))}"
        )
    if detail and row and row.get("fetched_at"):
        dt = datetime.fromtimestamp(row["fetched_at"] / 1000, tz=_BJT)
        lines.append(f"<i>更新于 {dt.strftime('%H:%M:%S')}</i>")
    return "\n".join(lines) if lines else "📊 Cursor 额度: <i>无有效数据</i>"


def _format_cursor_local_cost(stats: dict | None, *, model_row: bool = False) -> str:
    data = stats if isinstance(stats, dict) else {}
    actual = int(data.get("actual_costed_success") or 0)
    unpriced = int(data.get("unpriced_success") or 0)
    if actual > 0:
        amount = ui.fmt_cost(data)
        if unpriced > 0:
            return f"{amount}（官方已对账 {actual} 次 · 另 {unpriced} 次未计价）"
        return f"{amount}（Cursor 官方事件）"
    return "未计价" if model_row else "未计价（Cursor 官方账单见上方）"


def _has_local_usage_or_billing(stats: dict | None) -> bool:
    if not isinstance(stats, dict):
        return False
    for name in (
        "total", "input", "output", "cache_creation", "cache_read",
        "costed_success", "unpriced_success",
    ):
        try:
            if int(stats.get(name) or 0) > 0:
                return True
        except (TypeError, ValueError, OverflowError):
            continue
    return False


def _window_stats_cache_key(account_key: str, window_name: str) -> tuple:
    # key 表达业务窗口身份而不是滚动 since 时间。无有效 reset 时 since 会持续
    # 变化；若把 since 放进 key，预热结果会在渲染前跨桶并被静默漏掉。
    return "oauth-window", account_key, window_name


def _window_usage_detail(account_key: str, since_ts: float, indent: str,
                         window_name: str, stats: dict | None = None, *,
                         stats_loading: bool = False) -> Optional[str]:
    """某 OAuth 账号在 [since_ts, now] 窗口内、经 Parrot 的本地请求用量明细行。

    返回已缩进的单行文本；窗口内没有本地请求时返回 None（不堆空行）。

    口径提醒：上一行 5h/7d 百分比是上游账号「全局」配额用量；这里的
    tokens / 缓存 / 平均 TPS 只统计走 Parrot 的本地日志。账号若在别处也被
    使用，两者会对不上，属预期，不是 bug。
    """
    if stats is None:
        key = _window_stats_cache_key(account_key, window_name)
        cached = menu_cache.WINDOW_STATS.peek(key)
        stats = cached.value
    s = stats or {}
    if not _has_local_usage_or_billing(s):
        return None
    prompt = ui.prompt_total(s["input"], s["cache_creation"], s["cache_read"])
    parts = [f"↑{ui.fmt_tokens(prompt)} ↓{ui.fmt_tokens(s['output'])}"]
    if (s.get("cache_read") or 0) > 0:
        parts.append(ui.fmt_cache_phrase(s["cache_read"], prompt))
    if s.get("avg_tps") is not None:
        parts.append(f"均 {ui.fmt_tps(s.get('avg_tps'))}")
    if oauth_manager.provider_of(account_key) in {"claude", "openai"}:
        parts.append(ui.fmt_cost(s, decimal_places=3))
    return indent + " · ".join(parts)


def _format_account_block(acc: dict, *, month_snapshot: dict | None = None,
                          stats_loading: bool = False) -> str:
    """列表中每条 OAuth 账号的统一多行展示块。"""
    email = _account_display(acc)
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
    prov_tag = " " + _provider_tag(prov) if prov else ""
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
    elif prov == "xai":
        plan_line = _format_xai_provider_line(ak, detail=False)
        if plan_line:
            lines.extend(plan_line.splitlines())
    elif prov == "cursor":
        plan = str(acc.get("plan_type") or "Cursor")
        records = ((acc.get("cursor_model_catalog") or {}).get("models") or [])
        variants = sum(
            len(item.get("legacy_slugs") or []) for item in records if isinstance(item, dict)
        )
        lines.append(
            f"🏷️ 套餐: <code>{ui.escape_html(plan)}</code> · "
            f"模型 <code>{len(acc.get('models') or [])}</code> · 变体 <code>{variants}</code>"
        )
    elif prov == "antigravity":
        project_id = str(acc.get("project_id") or acc.get("projectId") or "").strip()
        if project_id:
            lines.append(f"🏷️ Project: <code>{ui.escape_html(project_id)}</code>")

    # 用量（5h / 7d）。Claude/OpenAI 百分比来自上游全局配额；Grok
    # 展示官方当前周期额度 + Parrot 本地累计金额/token。
    _now_ts = time.time()
    if prov == "cursor":
        lines.extend(_format_cursor_usage_block(ak, detail=False).splitlines())
    elif prov == "xai":
        lines.extend(_format_xai_official_block(ak, detail=False).splitlines())
        month_stats = ((month_snapshot or {}).get("by_channel") or {}).get(f"oauth:{ak}")
        lines.extend(_format_xai_spend_block(
            ak, detail=False, month_stats=month_stats, stats_loading=stats_loading,
        ).splitlines())
    elif prov == "antigravity":
        lines.extend(_format_antigravity_credits_block(ak, detail=False).splitlines())
    elif row:
        fh_util = row.get("five_hour_util")
        sd_util = row.get("seven_day_util")
        if fh_util is not None:
            reset = row.get("five_hour_reset")
            lines.append(f"📊 5h: {_format_usage_value_html(fh_util)} · 重置 <code>{_fmt_time_full(reset)}</code>")
            since_ts = _quota_window_since_ts(reset, 5 * 3600, now_ts=_now_ts)
            _d = _window_usage_detail(
                ak, since_ts, _USAGE_DETAIL_INDENT_LIST, "5h",
            )
            if _d:
                lines.append(_d)
        if sd_util is not None:
            reset = row.get("seven_day_reset")
            lines.append(f"📊 7d: {_format_usage_value_html(sd_util)} · 重置 <code>{_fmt_time_full(reset)}</code>")
            since_ts = _quota_window_since_ts(reset, 7 * 86400, now_ts=_now_ts)
            _d = _window_usage_detail(
                ak, since_ts, _USAGE_DETAIL_INDENT_LIST, "7d",
            )
            if _d:
                lines.append(_d)
        td_util = row.get("thirty_day_util")
        if td_util is not None:
            reset = row.get("thirty_day_reset")
            lines.append(f"📊 30d: {_format_usage_value_html(td_util)} · 重置 <code>{_fmt_time_full(reset)}</code>")
        if fh_util is None and sd_util is None and td_util is None:
            if prov == "xai":
                lines.append("📊 官方额度: <i>尚未获取</i>")
            else:
                lines.append("📊 用量: <i>尚未获取</i>")
    else:
        if prov == "xai":
            lines.append("📊 官方额度: <i>尚未获取</i>")
        else:
            lines.append("📊 用量: <i>尚未获取</i>")

    # 月度统计来自所有菜单共享的一次批量快照。
    ts = ((month_snapshot or {}).get("by_channel") or {}).get(f"oauth:{ak}")
    if prov != "xai" and _has_local_usage_or_billing(ts):
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
        if prov == "cursor":
            lines.append(f"💵 {_format_cursor_local_cost(ts)}")
        else:
            lines.append(f"💵 {ui.fmt_cost(ts)}")

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


def _format_usage_block(account_key: str, *, month_snapshot: dict | None = None,
                        stats_loading: bool = False) -> str:
    provider = oauth_manager.provider_of(account_key)
    if provider == "cursor":
        return _format_cursor_usage_block(account_key, detail=True)
    if provider == "antigravity":
        return _format_antigravity_credits_block(account_key, detail=True)
    if provider == "xai":
        month_stats = ((month_snapshot or {}).get("by_channel") or {}).get(f"oauth:{account_key}")
        return _format_xai_official_block(account_key, detail=True) + "\n\n" + _format_xai_spend_block(
            account_key, detail=True, month_stats=month_stats, stats_loading=stats_loading,
        )
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
                window_name = "5h" if util_k == "five_hour_util" else "7d"
                _d = _window_usage_detail(
                    account_key, since_ts, _USAGE_DETAIL_INDENT_BLOCK, window_name,
                )
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
    elif family == "xai":
        raw = (cfg.get("xaiOAuth") or {}).get("defaultModels") or []
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
    xai_models = _default_models_for_settings("xai")
    cfg = config.get()
    cursor_accounts = [
        acc for acc in cfg.get("oauthAccounts", [])
        if oauth_manager.provider_of(acc) == "cursor"
    ]
    xai_cfg = cfg.get("xaiOAuth") if isinstance(cfg.get("xaiOAuth"), dict) else {}
    xai_image_models = xai_cfg.get("imageModels") if isinstance(xai_cfg.get("imageModels"), list) else []
    xai_video_models = xai_cfg.get("videoModels") if isinstance(xai_cfg.get("videoModels"), list) else []
    images_cfg = cfg.get("images") if isinstance(cfg.get("images"), dict) else {}
    gpt_images_status = "✅ 已启用" if images_cfg.get("enabled", True) else "🚫 已停用"
    mode_label = _usage_display_label()
    quota_enabled, quota_interval, quota_threshold = _quota_monitor_values()
    quota_status = "✅ 已启用" if quota_enabled else "🚫 已停用"
    cch_enabled = _cch_enabled()
    cch_action = "关闭" if cch_enabled else "开启"

    text = "\n".join([
        "⚙️ <b>OAuth 账户设置</b>",
        "",
        "🧩 <b>OAuth 模型目录</b>",
        f"  {_provider_tag('claude')}  {len(anthropic_models)} 个 · "
        f"{_provider_tag('openai')}  {len(openai_models)} 个 · "
        f"{_provider_tag('xai')}  {len(xai_models)} 个",
        f"  {_provider_tag('cursor')} 按账号自动同步（{len(cursor_accounts)} 个账号）",
        "",
        "🎨 <b>媒体能力</b>",
        f"GPT / Codex 图片: {gpt_images_status}",
        f"Grok Imagine: 图片 <b>{len(xai_image_models)}</b> · 视频 <b>{len(xai_video_models)}</b>",
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
        [ui.btn("🧩 模型目录", "odm:show"),
         ui.btn("📈 配额监控", "oa:quota")],
        [ui.provider_button("GPT 图片", "img:show", "openai"),
         ui.provider_button("Grok 图片", "xim:show", "xai")],
        [ui.btn(f"📊 显示: {_usage_toggle_target_label()}", "oa:usage_mode:toggle"),
         ui.btn(f"🎭 CCH模式：{cch_action}", "oa:cch_toggle")],
        [ui.btn("🏠 返回主菜单", "menu:main"),
         ui.btn("◀ 返回OAuth账户", "menu:oauth")],
    ]
    return text, ui.inline_kb(rows)


def on_settings(chat_id: int, message_id: int, cb_id: Optional[str] = None) -> None:
    from . import oauth_defaults_menu
    oauth_defaults_menu.abandon_edit(chat_id)
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


def _list_text_and_kb(page: int = 1, filter_key: str = _FILTER_ALL, *,
                      month_snapshot: dict | None = None,
                      stats_loading: bool = False) -> tuple[str, dict]:
    accounts_all = oauth_manager.list_accounts()
    filter_key = _normalize_filter(filter_key)
    # 常用列表只读取本地状态与主动维护的月度快照。保留既有的本地配额
    # 阈值收敛（不访问网络）；远端用量仍只由显式刷新操作负责。
    account_keys = _refreshable_account_keys_for_ui(accounts_all)
    for account_key in account_keys:
        try:
            oauth_manager.evaluate_and_toggle_by_cached_quota(account_key)
        except Exception as exc:
            print(f"[oauth_menu] cached quota evaluate failed for {account_key}: {exc}")
    if account_keys:
        accounts_all = oauth_manager.list_accounts()
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
            block = _format_account_block(
                acc, month_snapshot=month_snapshot, stats_loading=stats_loading,
            )
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
            email = _account_display(acc)
            ak = _account_key(acc)
            short = ui.register_code(ak)
            provider = oauth_manager.provider_of(acc)
            num = start + offset + 1
            row_btns.append(ui.provider_button(
                f"{num}. {email}",
                f"oa:view:{_callback_payload(short, page, filter_key)}",
                provider,
            ))
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


def _oauth_window_specs(accounts: list[dict]) -> list[tuple[tuple, str, float]]:
    specs: list[tuple[tuple, str, float]] = []
    now_ts = time.time()
    for account in accounts:
        account_key = _account_key(account)
        row = state_db.quota_load(account_key)
        if not row:
            continue
        for window_name, util_key, reset_key, seconds in (
            ("5h", "five_hour_util", "five_hour_reset", 5 * 3600),
            ("7d", "seven_day_util", "seven_day_reset", 7 * 86400),
        ):
            if row.get(util_key) is None:
                continue
            since = _quota_window_since_ts(row.get(reset_key), seconds, now_ts=now_ts)
            specs.append((
                _window_stats_cache_key(account_key, window_name), account_key, since,
            ))
    return specs


def refresh_window_snapshots_now() -> bool:
    """由唯一统计调度线程同步刷新所有 OAuth 5h/7d 本地明细。"""
    ok = True
    for key, account_key, since in _oauth_window_specs(oauth_manager.list_accounts()):
        ok = menu_cache.WINDOW_STATS.refresh_now(
            key,
            lambda target=account_key, start=since: log_db.tokens_for_channel(
                f"oauth:{target}", since_ts=start,
            ),
        ) and ok
    return ok


def _request_window_snapshots(accounts: list[dict]) -> bool:
    """请求缺失/过期窗口快照，并返回旧页面所需快照是否均已存在。"""
    ready = True
    for key, account_key, since in _oauth_window_specs(accounts):
        cached = menu_cache.WINDOW_STATS.peek(key)
        if cached.value is None:
            ready = False
        if not cached.fresh:
            menu_cache.WINDOW_STATS.request(
                key,
                lambda target=account_key, start=since: log_db.tokens_for_channel(
                    f"oauth:{target}", since_ts=start,
                ),
            )
    return ready


def _render_cached_list(page: int, filter_key: str) -> tuple[str, dict]:
    since = _this_month_start_ts()
    period = menu_cache.PERIOD_STATS.peek(("period", int(since)))
    return _list_text_and_kb(
        page=page, filter_key=filter_key, month_snapshot=period.value,
    )


def _list_snapshot_ready() -> bool:
    since = _this_month_start_ts()
    if menu_cache.PERIOD_STATS.peek(("period", int(since))).value is None:
        return False
    # OAuth 列表原来显示的 5h/7d 本地 Token、缓存、TPS、金额是页面必需内容，
    # 不能因为快照尚未预热就静默删行。
    return _request_window_snapshots(oauth_manager.list_accounts())


def _converge_cached_quota_state() -> None:
    """保留旧列表进入时基于本地配额缓存立即收敛账号状态的行为。"""
    accounts = oauth_manager.list_accounts()
    for account_key in _refreshable_account_keys_for_ui(accounts):
        try:
            oauth_manager.evaluate_and_toggle_by_cached_quota(account_key)
        except Exception as exc:
            print(f"[oauth_menu] cached quota evaluate failed for {account_key}: {exc}")


def show(chat_id: int, message_id: int, cb_id: Optional[str] = None, page: int = 1, filter_key: str = _FILTER_ALL) -> None:
    # 这是本地状态收敛，不查统计库也不访问网络；即使统计快照还在预热，
    # 也不能丢掉旧版进入列表时立即禁用/恢复账号的语义。
    _converge_cached_quota_state()
    if not _list_snapshot_ready():
        if cb_id is not None:
            ui.answer_cb(cb_id, menu_cache.initialization_text())
        return
    if cb_id is not None:
        ui.answer_cb(cb_id)
    menu_cache.begin_view(chat_id, message_id)
    text, kb = _render_cached_list(page, filter_key)
    ui.edit(chat_id, message_id, text, reply_markup=kb)


def send_new(chat_id: int, page: int = 1, filter_key: str = _FILTER_ALL) -> None:
    _converge_cached_quota_state()
    if not _list_snapshot_ready():
        ui.send(chat_id, menu_cache.initialization_text())
        return
    text, kb = _render_cached_list(page, filter_key)
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
    tag = _provider_tag(prov)
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

def _format_month_stats_block(account_key: str, *,
                              month_snapshot: dict | None = None,
                              by_model: list[dict] | None = None,
                              stats_loading: bool = False) -> str:
    """本月使用统计：总体来自共享快照，模型明细来自后台对象缓存。"""
    ck = f"oauth:{account_key}"
    since_ts = _this_month_start_ts()
    if month_snapshot is None:
        month_snapshot = menu_cache.PERIOD_STATS.peek(
            ("period", int(since_ts))
        ).value
    overall = (((month_snapshot or {}).get("by_channel") or {}).get(ck))
    if not _has_local_usage_or_billing(overall):
        return ""
    is_cursor = oauth_manager.provider_of(account_key) == "cursor"
    model_loading = False
    if by_model is None:
        cached_models = menu_cache.DETAIL_STATS.peek(
            ("oauth-model", account_key, int(since_ts))
        )
        by_model = cached_models.value or []
        model_loading = cached_models.value is None

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
        (
            f"累计金额：{_format_cursor_local_cost(overall)}"
            if is_cursor else f"累计金额：{ui.fmt_cost(overall)}"
        ),
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
            lines.append(
                f"    累计金额：{_format_cursor_local_cost(ms, model_row=True)}"
                if is_cursor else f"    累计金额：{ui.fmt_cost(ms)}"
            )
    return "\n".join(lines)


def _detail_text_and_kb(account_key: str, page: int = 1, filter_key: str = _FILTER_ALL,
                        *, refresh_quota: bool = True,
                        reset_credit_count_override: int | None = None,
                        month_snapshot: dict | None = None,
                        model_stats: list[dict] | None = None,
                        stats_loading: bool = False) -> tuple[Optional[str], Optional[dict]]:
    acc = oauth_manager.get_account(account_key)
    if acc is None:
        return None, None
    email = _account_display(acc)

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
    elif prov == "xai":
        provider_line = _format_xai_provider_line(account_key, detail=True)
    elif prov == "antigravity":
        project_id = str(acc.get("project_id") or acc.get("projectId") or "").strip()
        provider_line = (
            f"🏷️ Project: <code>{ui.escape_html(project_id or '?')}</code>\n"
        )
    elif prov == "cursor":
        profile_name = str(acc.get("cursor_profile_name") or "").strip()
        cursor_model_ids = {
            str(item.get("id") or "") for item in _cursor_model_records(acc)
        }
        if not cursor_model_ids:
            cursor_model_ids = {
                str(model) for model in acc.get("models") or [] if str(model)
            }
        cursor_disabled = oauth_manager.cursor_disabled_models(acc) & cursor_model_ids
        cursor_available = max(0, len(cursor_model_ids) - len(cursor_disabled))
        disabled_suffix = f" · 禁用 {len(cursor_disabled)}" if cursor_disabled else ""
        provider_line = (
            (f"👤 姓名: <code>{ui.escape_html(profile_name)}</code>\n" if profile_name else "")
            + f"🏷️ 套餐: <code>{ui.escape_html(str(acc.get('plan_type') or 'Cursor'))}</code>\n"
            f"🧬 模型目录: <code>{cursor_available} 个可用模型{disabled_suffix}</code>\n"
            f"📚 元数据: <code>Cursor AvailableModels（账号专属）</code>\n"
        )
    else:
        provider_line = ""
    max_cc = int(acc.get("maxConcurrent", 0) or 0)
    max_cc_label = str(max_cc) if max_cc > 0 else "默认"
    prov_icon = _provider_tag(prov)
    text = (
        f"{icon} <b>{ui.escape_html(email)}</b> {prov_icon}\n\n"
        f"状态: <code>{ui.escape_html('enabled' if acc.get('enabled', True) and not acc.get('disabled_reason') else reason)}</code>\n"
        f"{provider_line}"
        f"⚡ 并发上限: <code>{max_cc_label}</code>\n"
        f"⏳ Token: <code>{_fmt_time_full(acc.get('expired'))}</code>\n"
        f"🔄 刷新: <code>{_format_bjt(acc.get('last_refresh'))}</code>\n\n"
        f"<b>📊 使用量</b>\n{_format_usage_block(account_key, month_snapshot=month_snapshot, stats_loading=stats_loading)}"
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

    month_block = _format_month_stats_block(
        account_key, month_snapshot=month_snapshot, by_model=model_stats,
        stats_loading=stats_loading,
    )
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
    usage_btn_label = (
        "📊 刷新 Cursor 用量" if prov == "cursor"
        else "📊 刷新账单" if prov == "xai"
        else "📊 刷新 Credits" if prov == "antigravity"
        else "📊 刷新用量/重置卡"
    )
    rows = [
        [ui.btn("🔄 刷新 Token", f"oa:refresh_token:{payload}"),
         ui.btn(usage_btn_label,   f"oa:refresh_usage:{payload}")],
    ]
    if prov == "cursor":
        rows.append([ui.provider_button(
            "Cursor 模型目录",
            f"oa:cursor_models:{short}:1",
            "cursor",
        )])
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


def _render_cached_detail(account_key: str, page: int, filter_key: str,
                          *, refresh_quota: bool = False) -> tuple[Optional[str], Optional[dict]]:
    since = _this_month_start_ts()
    period = menu_cache.PERIOD_STATS.peek(("period", int(since)))
    models = menu_cache.DETAIL_STATS.peek(("oauth-model", account_key, int(since)))
    return _detail_text_and_kb(
        account_key, page=page, filter_key=filter_key,
        refresh_quota=refresh_quota,
        month_snapshot=period.value,
        model_stats=models.value,
        stats_loading=period.value is None or models.value is None,
    )


def _queue_oauth_detail_stats(account_key: str) -> bool:
    """把缺失/过期的低频详情排入中央队列；返回当前是否已有完整值。"""
    since = _this_month_start_ts()
    period_key = ("period", int(since))
    if menu_cache.PERIOD_STATS.peek(period_key).value is None:
        return False

    ready = True
    model_key = ("oauth-model", account_key, int(since))
    model = menu_cache.DETAIL_STATS.peek(model_key)
    channel_stats = (
        (menu_cache.PERIOD_STATS.peek(period_key).value or {}).get("by_channel") or {}
    ).get(f"oauth:{account_key}")
    if model.value is None and not int((channel_stats or {}).get("total") or 0):
        # 本月无调用时，旧详情中的按模型统计完整结果就是空列表。
        menu_cache.DETAIL_STATS.store(model_key, [])
        model = menu_cache.DETAIL_STATS.peek(model_key)
    if model.value is None:
        ready = False
    if not model.fresh:
        menu_cache.DETAIL_STATS.request(
            model_key,
            lambda: log_db.channel_model_stats(f"oauth:{account_key}", since_ts=since),
        )

    account = oauth_manager.get_account(account_key)
    if not _request_window_snapshots([account] if account else []):
        ready = False
    return ready


def on_view(chat_id: int, message_id: int, cb_id: str, short: str, page: int = 1, filter_key: str = _FILTER_ALL) -> None:
    ak = _account_key_from_short(short)
    if ak is None or oauth_manager.get_account(ak) is None:
        ui.answer_cb(cb_id, "账户已不存在，请返回重试")
        return
    since = _this_month_start_ts()
    if menu_cache.PERIOD_STATS.peek(("period", int(since))).value is None:
        ui.answer_cb(cb_id, menu_cache.initialization_text())
        return
    # 旧详情中的按模型统计及 5h/7d 本地明细都是既有内容，不允许在
    # 冷快照时先渲染一个删减版页面。查询仍只排入中央串行调度器。
    if not _queue_oauth_detail_stats(ak):
        ui.answer_cb(cb_id, menu_cache.initialization_text())
        return
    ui.answer_cb(cb_id)
    menu_cache.begin_view(chat_id, message_id)
    text, kb = _render_cached_detail(ak, page, filter_key, refresh_quota=False)
    if text is not None:
        ui.edit(chat_id, message_id, text, reply_markup=kb)


# ─── 刷新 Token ──────────────────────────────────────────────────

def on_refresh_token(chat_id: int, message_id: int, cb_id: str, short: str, page: int = 1, filter_key: str = _FILTER_ALL) -> None:
    ak = _account_key_from_short(short)
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

    model_sync_note = ""
    if provider == "cursor":
        sync_result = _run_sync(oauth_manager.refresh_cursor_models(
            ak, force=True, min_interval_seconds=0, timeout_s=30.0,
        ))
        if isinstance(sync_result, dict) and sync_result.get("action") == "updated":
            model_sync_note = f" · 模型 {int(sync_result.get('models') or 0)} 个"
        elif isinstance(sync_result, dict) and sync_result.get("action") == "profile_updated":
            model_sync_note = " · 账号资料已同步（模型保留原目录）"
        elif isinstance(sync_result, (dict, Exception)):
            model_sync_note = " · 模型同步失败（保留原目录）"

    text, kb = _detail_text_and_kb(ak, page=page, filter_key=filter_key)
    if text:
        ui.edit(chat_id, message_id,
                f"✅ Token 已刷新{model_sync_note}\n\n" + text,
                reply_markup=kb)


# ─── 刷新用量 / 重置卡 ─────────────────────────────────────────────

def on_refresh_usage(chat_id: int, message_id: int, cb_id: str, short: str, page: int = 1, filter_key: str = _FILTER_ALL) -> None:
    ak = _account_key_from_short(short)
    if ak is None:
        ui.answer_cb(cb_id, "短码已失效")
        return
    email = _account_email(ak)
    provider = oauth_manager.provider_of(ak)
    if provider == "openai":
        ui.answer_cb(cb_id, "拉取 OpenAI 用量/重置卡...")
    elif provider == "xai":
        ui.answer_cb(cb_id, "拉取 Grok 官方账单...")
    elif provider == "cursor":
        ui.answer_cb(cb_id, "拉取 Cursor 额度和模型目录...")
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
    cursor_event_action = None
    if provider == "openai":
        metadata_action = _run_sync(oauth_manager.ensure_openai_metadata_fresh(
            ak, force=True, min_interval_seconds=0, timeout_s=5.0,
        ))
    elif provider == "cursor":
        metadata_action = _run_sync(oauth_manager.refresh_cursor_models(
            ak, force=True, min_interval_seconds=0, timeout_s=30.0,
        ))
        cursor_event_action = _run_sync(cursor_reconcile.sync_account(ak, force=True))

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
        if quota_action and quota_action.get("action") in ("disabled", "wham_limit_disabled"):
            hit = " / ".join(quota_action.get("hit_windows") or []) or "?"
            head += f"\n🔒 已自动标记为配额禁用（超限: <code>{ui.escape_html(hit)}</code>）"
        elif quota_action and quota_action.get("action") in ("still_over_quota", "wham_limit_keep_disabled"):
            hit = " / ".join(quota_action.get("hit_windows") or []) or "?"
            head += f"\n⚠ 仍处于配额禁用（超限: <code>{ui.escape_html(hit)}</code>）"
        elif quota_action and quota_action.get("action") == "resumed":
            head += "\n♻ 额度已恢复，已自动解除配额禁用"
        ui.edit(chat_id, message_id, head + "\n\n" + text, reply_markup=kb)
    elif provider == "cursor":
        head = "✅ 已更新 Cursor 套餐额度"
        if isinstance(metadata_action, dict) and metadata_action.get("action") == "updated":
            head += f"\n🧬 模型目录已同步: <code>{int(metadata_action.get('models') or 0)} 个</code>"
            if metadata_action.get("profile_updated"):
                head += "\n👤 账号邮箱与姓名已同步"
        elif isinstance(metadata_action, dict) and metadata_action.get("action") == "profile_updated":
            head += "\n👤 账号邮箱与姓名已同步"
            head += "\n⚠️ 模型目录本次同步失败，保留原目录"
        elif isinstance(metadata_action, dict) and metadata_action.get("action") in {"error", "timeout", "fetch_empty"}:
            head += "\n⚠️ 额度已更新，但模型目录本次同步失败，保留原目录"
        if isinstance(cursor_event_action, dict):
            matched = int(cursor_event_action.get("matched") or 0)
            refreshed = int(cursor_event_action.get("refreshed") or 0)
            pending = int(cursor_event_action.get("pending") or 0)
            if matched:
                head += f"\n🧾 官方缓存/金额事件已回填: <code>{matched} 条</code>"
            elif refreshed:
                head += f"\n🧾 官方事件已复核: <code>{refreshed} 条</code>"
            elif pending:
                head += "\n⏳ 官方事件尚未到达，后台会继续自动对账"
        if quota_action and quota_action.get("action") == "cursor_pool_cooldown":
            head += f"\n🟠 已按额度池冷却 <code>{int(quota_action.get('cooled_models') or 0)}</code> 个模型"
        elif quota_action and quota_action.get("action") == "cursor_pool_recovered":
            head += f"\n♻️ 已恢复 <code>{int(quota_action.get('recovered_models') or 0)}</code> 个模型"
        ui.edit(chat_id, message_id, head + "\n\n" + text, reply_markup=kb)
    elif provider == "xai":
        head = "✅ 已更新 Grok 官方账单"
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


# ─── Cursor 模型目录 / 单模型 Max Context 默认值 ──────────────────


_CURSOR_MODEL_PAGE_SIZE = 6
_CURSOR_MODEL_REF_SEP = "\x1f"
_CURSOR_DISABLE_ACTION = "oa_cursor_disable"


def _cursor_model_records(acc: dict) -> list[dict]:
    return [
        item for item in ((acc.get("cursor_model_catalog") or {}).get("models") or [])
        if isinstance(item, dict) and item.get("id")
    ]


def _cursor_model_ref(account_key: str, model_id: str) -> str:
    return ui.register_code(
        f"{account_key}{_CURSOR_MODEL_REF_SEP}{str(model_id or '').strip()}"
    )


def _resolve_cursor_model_ref(ref: str) -> tuple[str, dict, dict] | None:
    wanted = str(ref or "")
    raw = ui.resolve_code(wanted) or ""
    if _CURSOR_MODEL_REF_SEP not in raw:
        # Model detail buttons from an older process still carry a deterministic
        # hash. Rebuild the reverse mapping from the live Cursor catalogs.
        for account in oauth_manager.list_accounts():
            if oauth_manager.provider_of(account) != "cursor":
                continue
            account_key = oauth_manager.get_account_key(account)
            for item in _cursor_model_records(account):
                model_id = str(item.get("id") or "")
                if _cursor_model_ref(account_key, model_id) == wanted:
                    raw = f"{account_key}{_CURSOR_MODEL_REF_SEP}{model_id}"
                    break
            if _CURSOR_MODEL_REF_SEP in raw:
                break
    if _CURSOR_MODEL_REF_SEP not in raw:
        return None
    account_key, model_id = raw.split(_CURSOR_MODEL_REF_SEP, 1)
    account_key = _resolve_to_account_key(account_key) or ""
    acc = oauth_manager.get_account(account_key) if account_key else None
    if acc is None or oauth_manager.provider_of(acc) != "cursor":
        return None
    record = next((
        item for item in _cursor_model_records(acc)
        if str(item.get("id") or "") == model_id
    ), None)
    return (account_key, acc, record) if record is not None else None


def _cursor_model_page_payload(payload: str) -> tuple[str, int]:
    short, _, page_s = str(payload or "").partition(":")
    try:
        page = max(1, int(page_s or 1))
    except ValueError:
        page = 1
    return short, page


def _cursor_model_detail_payload(payload: str) -> tuple[str, int]:
    ref, _, page_s = str(payload or "").partition(":")
    try:
        page = max(1, int(page_s or 1))
    except ValueError:
        page = 1
    return ref, page


def _cursor_effort_text(item: dict) -> str:
    efforts = [
        str(value).strip() for value in item.get("reasoning_efforts") or []
        if str(value).strip()
    ]
    return "、".join(efforts)


def _cursor_disable_state(chat_id: int) -> dict | None:
    state = states.get_state(chat_id)
    if not state or state.get("action") != _CURSOR_DISABLE_ACTION:
        return None
    return state.get("data") or {}


def _parse_cursor_disable_payload(payload: str) -> tuple[str, str]:
    short, _sep, rest = str(payload or "").partition(":")
    return short, rest


def _load_cursor_disable_state(chat_id: int, short: str) -> dict | None:
    """Return the disable editor state, rebuilding it after a process restart.

    Draft selections live in memory.  After restart the Telegram message still
    carries the account short code, so we reconstruct from the persisted
    disabled-model set instead of treating the page as dead.
    """
    account_key = _account_key_from_short(short)
    acc = oauth_manager.get_account(account_key) if account_key else None
    if acc is None or oauth_manager.provider_of(acc) != "cursor":
        return None
    existing = _cursor_disable_state(chat_id)
    existing_key = _resolve_to_account_key((existing or {}).get("account_key"))
    if existing and existing_key == account_key:
        return existing
    available = {str(item.get("id") or "") for item in _cursor_model_records(acc)}
    selected = oauth_manager.cursor_disabled_models(acc) & available
    _set_cursor_disable_state(
        chat_id,
        account_key,
        page=max(1, int((existing or {}).get("page") or 1)),
        selected=selected,
    )
    return _cursor_disable_state(chat_id)


def _set_cursor_disable_state(
    chat_id: int,
    account_key: str,
    *,
    page: int,
    selected: set[str],
) -> None:
    states.set_state(chat_id, _CURSOR_DISABLE_ACTION, {
        "account_key": account_key,
        "page": max(1, int(page or 1)),
        "selected": sorted(selected),
    })


def _cursor_disable_text_and_kb(data: dict) -> tuple[str, dict] | None:
    account_key = _resolve_to_account_key(data.get("account_key"))
    acc = oauth_manager.get_account(account_key) if account_key else None
    if acc is None or oauth_manager.provider_of(acc) != "cursor":
        return None
    records = _cursor_model_records(acc)
    available = {str(item.get("id") or "") for item in records}
    selected = {
        str(model) for model in data.get("selected") or [] if str(model) in available
    }
    lines = [
        f"🚫 <b>{ui.escape_html(_account_display(acc))} · 批量禁用模型</b>",
        f"共 <b>{len(records)}</b> 个模型 · 将禁用 <b>{len(selected)}</b> 个",
        "",
        "点击序号切换状态，保存后立即从该 Cursor 账户的调度候选中移除。",
        "再次进入并取消选择即可恢复使用。",
    ]
    for index, item in enumerate(records, start=1):
        model_id = str(item.get("id") or "")
        name = str(item.get("name") or model_id)
        status = "🚫" if model_id in selected else "✅"
        lines.append(
            f"{index}. {status} <b>{ui.escape_html(name)}</b> · "
            f"<code>{ui.escape_html(model_id)}</code>"
        )

    short = ui.register_code(account_key)
    rows: list[list[dict]] = []
    for numbers in _split_number_rows(len(records)):
        rows.append([
            ui.btn(
                f"{number} {'🚫' if str(records[number - 1].get('id') or '') in selected else '✅'}",
                f"oa:cursor_dis_sel:{short}:{number}",
            )
            for number in numbers
        ])
    if records:
        rows.append([
            ui.btn("全选", f"oa:cursor_dis_all:{short}"),
            ui.btn("清空", f"oa:cursor_dis_clear:{short}"),
        ])
    rows.append([ui.btn("💾 保存禁用设置", f"oa:cursor_dis_save:{short}")])
    rows.append([ui.btn("◀ 取消并返回", f"oa:cursor_dis_cancel:{short}")])
    return ui.truncate("\n".join(lines)), ui.inline_kb(rows)


def _show_cursor_disable(
    chat_id: int, message_id: int, cb_id: str | None = None
) -> None:
    data = _cursor_disable_state(chat_id)
    rendered = _cursor_disable_text_and_kb(data or {}) if data else None
    if rendered is None:
        ui.answer_cb(cb_id or "", "会话已失效，请重新进入 Cursor 模型目录", show_alert=True)
        return
    if cb_id is not None:
        ui.answer_cb(cb_id)
    text, kb = rendered
    ui.edit(chat_id, message_id, text, reply_markup=kb)


def on_cursor_disable_start(
    chat_id: int, message_id: int, cb_id: str, payload: str
) -> None:
    short, page = _cursor_model_page_payload(payload)
    account_key = _account_key_from_short(short)
    acc = oauth_manager.get_account(account_key) if account_key else None
    if acc is None or oauth_manager.provider_of(acc) != "cursor":
        ui.answer_cb(cb_id, "Cursor 账户或短码已失效", show_alert=True)
        return
    available = {str(item.get("id") or "") for item in _cursor_model_records(acc)}
    selected = oauth_manager.cursor_disabled_models(acc) & available
    _set_cursor_disable_state(
        chat_id, account_key, page=page, selected=selected,
    )
    _show_cursor_disable(chat_id, message_id, cb_id)


def on_cursor_disable_select(
    chat_id: int, message_id: int, cb_id: str, payload: str
) -> None:
    short, index_text = _parse_cursor_disable_payload(payload)
    if not index_text and short.isdigit():
        data = _cursor_disable_state(chat_id)
        index_text = short
    else:
        data = _load_cursor_disable_state(chat_id, short)
    if not data:
        ui.answer_cb(cb_id, "会话已失效，请重新进入 Cursor 模型目录", show_alert=True)
        return
    account_key = _resolve_to_account_key(data.get("account_key"))
    acc = oauth_manager.get_account(account_key) if account_key else None
    records = _cursor_model_records(acc) if acc else []
    try:
        index = int(index_text)
    except ValueError:
        index = 0
    if index < 1 or index > len(records):
        ui.answer_cb(cb_id, "模型序号已失效", show_alert=True)
        return
    model_id = str(records[index - 1].get("id") or "")
    selected = {str(model) for model in data.get("selected") or []}
    if model_id in selected:
        selected.remove(model_id)
    else:
        selected.add(model_id)
    _set_cursor_disable_state(
        chat_id, account_key, page=int(data.get("page") or 1), selected=selected,
    )
    _show_cursor_disable(chat_id, message_id, cb_id)


def on_cursor_disable_set_all(
    chat_id: int, message_id: int, cb_id: str, *, selected_all: bool,
    payload: str = "",
) -> None:
    short, _rest = _parse_cursor_disable_payload(payload)
    data = _load_cursor_disable_state(chat_id, short) if short else _cursor_disable_state(chat_id)
    if not data:
        ui.answer_cb(cb_id, "会话已失效，请重新进入 Cursor 模型目录", show_alert=True)
        return
    account_key = _resolve_to_account_key(data.get("account_key"))
    acc = oauth_manager.get_account(account_key) if account_key else None
    if acc is None or oauth_manager.provider_of(acc) != "cursor":
        ui.answer_cb(cb_id, "Cursor 账户已不存在", show_alert=True)
        return
    selected = (
        {str(item.get("id") or "") for item in _cursor_model_records(acc)}
        if selected_all else set()
    )
    _set_cursor_disable_state(
        chat_id, account_key, page=int(data.get("page") or 1), selected=selected,
    )
    _show_cursor_disable(chat_id, message_id, cb_id)


def on_cursor_disable_save(
    chat_id: int, message_id: int, cb_id: str, payload: str = "",
) -> None:
    short, _rest = _parse_cursor_disable_payload(payload)
    data = _load_cursor_disable_state(chat_id, short) if short else _cursor_disable_state(chat_id)
    if not data:
        ui.answer_cb(cb_id, "会话已失效，请重新进入 Cursor 模型目录", show_alert=True)
        return
    account_key = _resolve_to_account_key(data.get("account_key"))
    page = max(1, int(data.get("page") or 1))
    try:
        saved = oauth_manager.set_cursor_disabled_models(
            account_key or "", data.get("selected") or [],
        )
    except ValueError as exc:
        ui.answer_cb(cb_id, str(exc), show_alert=True)
        return
    states.pop_state(chat_id)
    ui.answer_cb(cb_id, f"已禁用 {len(saved)} 个 Cursor 模型")
    short = ui.register_code(account_key)
    on_cursor_models(chat_id, message_id, "", f"{short}:{page}")


def on_cursor_disable_cancel(
    chat_id: int, message_id: int, cb_id: str, payload: str = "",
) -> None:
    short, _rest = _parse_cursor_disable_payload(payload)
    data = (_load_cursor_disable_state(chat_id, short) if short else _cursor_disable_state(chat_id)) or {}
    account_key = _resolve_to_account_key(data.get("account_key")) or _account_key_from_short(short)
    page = max(1, int(data.get("page") or 1))
    states.pop_state(chat_id)
    if not account_key:
        ui.answer_cb(cb_id, "会话已失效", show_alert=True)
        return
    ui.answer_cb(cb_id)
    short = ui.register_code(account_key)
    on_cursor_models(chat_id, message_id, "", f"{short}:{page}")


def on_cursor_models(chat_id: int, message_id: int, cb_id: str, payload: str) -> None:
    short, page = _cursor_model_page_payload(payload)
    account_key = _account_key_from_short(short)
    acc = oauth_manager.get_account(account_key) if account_key else None
    if acc is None or oauth_manager.provider_of(acc) != "cursor":
        ui.answer_cb(cb_id, "Cursor 账户或短码已失效")
        return
    records = _cursor_model_records(acc)
    disabled_models = oauth_manager.cursor_disabled_models(acc)
    disabled_count = sum(
        1 for item in records if str(item.get("id") or "") in disabled_models
    )
    total_pages = max(1, math.ceil(len(records) / _CURSOR_MODEL_PAGE_SIZE))
    page = min(page, total_pages)
    start = (page - 1) * _CURSOR_MODEL_PAGE_SIZE
    selected = records[start:start + _CURSOR_MODEL_PAGE_SIZE]
    lines = [
        f"🧬 <b>{ui.escape_html(_account_display(acc))} · Cursor 模型目录</b>",
        f"共 <b>{len(records)}</b> 个模型 · 已禁用 <b>{disabled_count}</b> 个 · 第 <b>{page}/{total_pages}</b> 页",
    ]
    for offset, item in enumerate(selected, start=1):
        display_index = start + offset
        model_id = str(item.get("id") or "")
        name = str(item.get("name") or model_id)
        context = int(item.get("context_window") or 0)
        max_context = int(item.get("context_window_max_mode") or context)
        default_max = (
            max_context > context > 0
            and oauth_manager.cursor_max_context_default(acc, model_id)
        )
        effective_context = max_context if default_max else context
        context_suffix = "（Max Context 默认）" if default_max else ""
        effort_text = _cursor_effort_text(item)
        disabled_suffix = " · 🚫 <b>已禁用</b>" if model_id in disabled_models else ""
        lines.extend([
            "",
            f"<b>{display_index}. {ui.escape_html(name)}</b> · <code>{ui.escape_html(model_id)}</code>{disabled_suffix}",
            f"上下文：<code>{ui.fmt_tokens(effective_context)}</code>{context_suffix} · 推理 {'✅' if item.get('reasoning') else '—'} · 图片上游 {'✅' if item.get('supports_images') else '—'}",
        ])
        if effort_text:
            lines.append(f"支持思考档位：{ui.escape_html(effort_text)}")

    rows: list[list[dict]] = []
    detail_buttons: list[dict] = []
    for offset, item in enumerate(selected, start=1):
        display_index = start + offset
        ref = _cursor_model_ref(account_key, str(item.get("id") or ""))
        detail_buttons.append(ui.btn(
            f"📄 #{display_index}", f"oa:cursor_model:{ref}:{page}",
        ))
        if len(detail_buttons) == 3:
            rows.append(detail_buttons)
            detail_buttons = []
    if detail_buttons:
        rows.append(detail_buttons)

    prev_page = max(1, page - 1)
    next_page = min(total_pages, page + 1)
    rows.append([
        ui.btn("🏠 首页", f"oa:cursor_models:{short}:1"),
        ui.btn("◀ 上一页", f"oa:cursor_models:{short}:{prev_page}"),
        ui.btn(f"{page}/{total_pages}", f"oa:cursor_models:{short}:{page}"),
        ui.btn("下一页 ▶", f"oa:cursor_models:{short}:{next_page}"),
    ])
    rows.append([
        ui.btn("🔄 刷新额度与模型", f"oa:refresh_usage:{short}:1"),
        ui.btn("🚫 批量禁用", f"oa:cursor_disable:{short}:{page}"),
    ])
    rows.append([ui.btn("◀ 返回账户详情", f"oa:view:{short}:1")])
    if cb_id:
        ui.answer_cb(cb_id)
    ui.edit(chat_id, message_id, ui.truncate("\n".join(lines)), reply_markup=ui.inline_kb(rows))


def on_cursor_model_detail(chat_id: int, message_id: int, cb_id: str, payload: str) -> None:
    ref, page = _cursor_model_detail_payload(payload)
    resolved = _resolve_cursor_model_ref(ref)
    if resolved is None:
        ui.answer_cb(cb_id, "Cursor 模型或短码已失效")
        return
    account_key, acc, item = resolved
    model_id = str(item.get("id") or "")
    name = str(item.get("name") or model_id)
    model_disabled = model_id in oauth_manager.cursor_disabled_models(acc)
    context = int(item.get("context_window") or 0)
    max_context = int(item.get("context_window_max_mode") or context)
    max_output = int(item.get("max_tokens") or 0)
    has_separate_max = max_context > context > 0
    default_max = oauth_manager.cursor_max_context_default(acc, model_id)
    variants = [value for value in item.get("variants") or [] if isinstance(value, dict)]
    supports_fast = any(bool(value.get("fast")) for value in variants)
    supports_thinking = any(bool(value.get("thinking")) for value in variants)
    effort_text = _cursor_effort_text(item)

    effective_context = max_context if has_separate_max and default_max else context
    lines = [
        f"🧬 <b>{ui.escape_html(name)}</b>",
        f"账户：<code>{ui.escape_html(_account_display(acc))}</code>",
        f"模型：<code>{ui.escape_html(model_id)}</code>",
        f"使用状态：{'🚫 已禁用' if model_disabled else '✅ 已启用'}",
        "",
    ]
    if has_separate_max:
        lines.extend([
            f"默认上下文：<code>{ui.fmt_tokens(effective_context)}</code>（Max Context {'已开启' if default_max else '已关闭'}）",
            f"普通上下文：<code>{ui.fmt_tokens(context)}</code>",
            f"Max Context：<code>{ui.fmt_tokens(max_context)}</code>",
        ])
    else:
        lines.append(f"上下文：<code>{ui.fmt_tokens(context)}</code>")
        if context >= 1_000_000:
            lines.append("<i>该模型默认即为最大上下文。</i>")
    if max_output:
        lines.append(f"最大输出：<code>{ui.fmt_tokens(max_output)}</code>")
    lines.extend([
        f"推理：{'✅' if item.get('reasoning') else '—'}",
        f"图片上游：{'✅' if item.get('supports_images') else '—'}",
        f"Thinking：{'✅' if supports_thinking else '—'}",
        f"Fast：{'✅' if supports_fast else '—'}",
    ])
    if effort_text:
        lines.append(f"支持思考档位：{ui.escape_html(effort_text)}")
    lines.append("")
    rows: list[list[dict]] = []
    if has_separate_max:
        lines.extend([
            f"Max Context 默认：<b>{'已开启' if default_max else '已关闭'}</b>",
            "<i>下游显式 true/false 优先；下游未指定时采用这里保存的默认值。</i>",
        ])
        action = "关闭" if default_max else "开启"
        rows.append([ui.provider_button(
            f"{'🟢' if default_max else '⬛'} 点击{action} Max Context",
            f"oa:cursor_maxctx:{ref}:{page}",
            "cursor",
        )])
    else:
        lines.append("<i>该模型没有可单独切换的 Max Context 档位。</i>")

    account_short = ui.register_code(account_key)
    rows.append([ui.btn(f"◀ 返回第 {page} 页", f"oa:cursor_models:{account_short}:{page}")])
    if cb_id:
        ui.answer_cb(cb_id)
    ui.edit(chat_id, message_id, ui.truncate("\n".join(lines)), reply_markup=ui.inline_kb(rows))


def on_cursor_max_context_toggle(chat_id: int, message_id: int, cb_id: str, payload: str) -> None:
    ref, page = _cursor_model_detail_payload(payload)
    resolved = _resolve_cursor_model_ref(ref)
    if resolved is None:
        ui.answer_cb(cb_id, "Cursor 模型或短码已失效")
        return
    account_key, acc, item = resolved
    model_id = str(item.get("id") or "")
    current = oauth_manager.cursor_max_context_default(acc, model_id)
    try:
        saved = oauth_manager.set_cursor_max_context_default(
            account_key, model_id, not current,
        )
    except ValueError as exc:
        ui.answer_cb(cb_id, str(exc), show_alert=True)
        return
    ui.answer_cb(cb_id, f"Max Context 默认已{'开启' if saved else '关闭'}")
    on_cursor_model_detail(chat_id, message_id, "", f"{ref}:{page}")


# ─── 清错误 / 清亲和 ─────────────────────────────────────────────

def on_clear_errors(chat_id: int, message_id: int, cb_id: str, short: str, page: int = 1, filter_key: str = _FILTER_ALL) -> None:
    ak = _account_key_from_short(short)
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
    ak = _account_key_from_short(short)
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
            f"♻️ {ui.provider_custom_emoji_html('openai')} <b>OpenAI 官方额度重置说明</b>\n\n"
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
        f"🚨 {ui.provider_custom_emoji_html('openai')} <b>最终确认：消耗 1 次 OpenAI 官方重置</b>\n\n"
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
            prefix = f"♻️ {ui.provider_custom_emoji_html('openai')} <b>OpenAI 官方额度重置已执行</b>\n"
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
            elif action in (
                "still_over_quota", "disabled",
                "wham_limit_keep_disabled", "wham_limit_disabled",
            ):
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
    elif action == "already_enabled":
        ui.answer_cb(cb_id, "账号已启用")
    elif action == "cleared_runtime_state":
        ui.answer_cb(cb_id, "已清理本地配额/冷却状态")
    elif action == "reset_failed":
        ui.answer_cb(cb_id, "重置失败，账号保持禁用")
    elif action == "state_conflict":
        ui.answer_cb(cb_id, "账号状态已变化，未自动启用")
    elif action == "invalid_state":
        ui.answer_cb(cb_id, "账号状态无效，未自动启用")
    elif action == "noop_user":
        ui.answer_cb(cb_id, "手动禁用不自动重置")
    elif action == "noop_auth_error":
        ui.answer_cb(cb_id, "auth_error 需重新登录")
    else:
        ui.answer_cb(cb_id, "无需重置")
    text, kb = _detail_text_and_kb(ak, page=page, filter_key=filter_key, refresh_quota=False)
    if text:
        if action == "reset":
            prefix = (
                "♻️ <b>已清理本地配额禁用</b>\n"
                "已清除该账号的 quota 禁用、模型冷却和本地 quota 缓存；"
                "下一次真实请求/刷新会重新采样。\n\n"
            )
            ui.edit(chat_id, message_id, prefix + text, reply_markup=kb)
        elif action == "reset_failed":
            if result.get("required_state_cleared"):
                detail = "本地阻断已清，但账号启用未能持久化；账号仍保持禁用。"
            else:
                detail = "至少一项本地配额/冷却状态未能持久化清除；账号仍保持禁用。"
            ui.edit(
                chat_id,
                message_id,
                f"⚠️ <b>本地配额重置失败</b>\n{detail}\n\n" + text,
                reply_markup=kb,
            )
        else:
            ui.edit(chat_id, message_id, text, reply_markup=kb)


def on_clear_affinity(chat_id: int, message_id: int, cb_id: str, short: str, page: int = 1, filter_key: str = _FILTER_ALL) -> None:
    ak = _account_key_from_short(short)
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
    ak = _account_key_from_short(short)
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
    ak = _account_key_from_short(short)
    if ak is None:
        ui.answer_cb(cb_id, "短码已失效")
        return
    acc = oauth_manager.get_account(ak)
    email = (acc or {}).get("email") or _account_email(ak)
    prov = oauth_manager.provider_of(ak)
    prov_tag = _provider_tag(prov)
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
    ak = _account_key_from_short(short)
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
                            show_transition_hint: bool = True,
                            view_token: int | None = None) -> None:
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
            if view_token is None:
                ui.edit(chat_id, progress_mid, text)
            else:
                menu_cache.run_if_current(
                    chat_id, progress_mid, view_token,
                    lambda: ui.edit(chat_id, progress_mid, text),
                )
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
            text, kb = _render_cached_list(page, filter_key)
        if text and target_mid != -1:
            edited = True
            if view_token is None:
                ui.edit(chat_id, target_mid, text, reply_markup=kb)
            else:
                edited = menu_cache.run_if_current(
                    chat_id, target_mid, view_token,
                    lambda: ui.edit(chat_id, target_mid, text, reply_markup=kb),
                )
            if edited and view_token is not None:
                if final_detail_account_key is None:
                    _start_oauth_list_stats_refreshes(
                        chat_id, target_mid, page, filter_key, view_token,
                    )
                else:
                    _start_oauth_detail_stats_refreshes(
                        chat_id, target_mid, final_detail_account_key,
                        page, filter_key, view_token,
                    )
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
                              background: bool = True,
                              view_token: int | None = None) -> None:
    if not background:
        _run_oauth_update_panel(
            chat_id, progress_mid, account_keys,
            page=page, filter_key=filter_key,
            final_target_message_id=final_target_message_id,
            final_detail_account_key=final_detail_account_key,
            delete_progress_later=delete_progress_later,
            send_fallback_summary=send_fallback_summary,
            show_transition_hint=show_transition_hint,
            view_token=view_token,
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
            "view_token": view_token,
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
        prov_tag = _provider_tag(provider)
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
            if quota_action and quota_action.get("action") in ("disabled", "wham_limit_disabled"):
                hit = " / ".join(quota_action.get("hit_windows") or []) or "?"
                lines.append(f"  🔒 触发自动禁用（超限窗口: <code>{ui.escape_html(hit)}</code>）")
            elif quota_action and quota_action.get("action") in ("still_over_quota", "wham_limit_keep_disabled"):
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
        f"<b>新增 OAuth 账户</b>\n请选择类型：\n\n{_provider_tag('claude')}、{_provider_tag('openai')}、{_provider_tag('xai')}、{_provider_tag('cursor')}、{_provider_tag('antigravity')}",
        reply_markup=ui.inline_kb([
            [ui.provider_button("Claude 登录获取 Token", "oa:login", "claude")],
            [ui.provider_button("Claude 手动设置 JSON", "oa:set_json", "claude")],
            [ui.provider_button("OpenAI 登录获取 Token", "oa:login:openai", "openai")],
            [ui.provider_button("OpenAI 粘贴 refresh_token", "oa:set_rt:openai", "openai")],
            [ui.provider_button("Grok 登录获取 Token", "oa:login:xai", "xai")],
            [ui.provider_button("Grok 粘贴 refresh_token", "oa:set_rt:xai", "xai")],
            [ui.provider_button("Cursor 登录", "oa:login:cursor", "cursor")],
            [ui.provider_button("Antigravity 登录获取 Token", "oa:login:antigravity", "antigravity")],
            [ui.provider_button("OpenAI 导入 Sub2API 文件", "oa:import:sub2api", "openai")],
            [ui.provider_button("OpenAI 导入 CPA 文件", "oa:import:cpa", "openai")],
            [ui.btn("◀ 返回列表", "menu:oauth")],
            [ui.btn("🏠 返回主菜单", "menu:main")],
        ]),
    )


def on_add_claude(chat_id: int, message_id: int, cb_id: str) -> None:
    """Claude 子菜单（原 on_add_menu 内容）。"""
    ui.answer_cb(cb_id)
    ui.edit(
        chat_id, message_id,
        f"<b>新增</b> {_provider_tag('claude', full=True)} <b>OAuth 账户</b>\n请选择方式：",
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
        f"<b>新增</b> {_provider_tag('openai')} <b>OAuth 账户</b>\n请选择方式：\n\n"
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
        f"请在浏览器中打开以下链接完成 {_provider_tag('claude')} 账号登录：\n\n"
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
        email = f"unnamed-{secrets.token_hex(8)}@local"

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
        save_action = _persist_new_or_stage_overwrite(
            chat_id, entry, source="Claude OAuth 登录",
        )
    except Exception as exc:
        ui.send_result(chat_id,
                       f"❌ 保存失败: <code>{ui.escape_html(str(exc))}</code>",
                       **nav)
        return
    if save_action == "staged":
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
        f"✅ {ui.provider_custom_emoji_html('claude')} <b>Anthropic OAuth 账户已添加</b>\n\n"
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
        f"请粘贴 {_provider_tag('claude')} OAuth JSON（需包含 <code>email / access_token / refresh_token / expired</code>）：",
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
        save_action = _persist_new_or_stage_overwrite(
            chat_id, entry, source="Claude 手动 JSON",
        )
    except Exception as exc:
        ui.send_result(chat_id,
                       f"❌ 保存失败: <code>{ui.escape_html(str(exc))}</code>",
                       **nav)
        return
    if save_action == "staged":
        return

    lb_hint = (
        "\n已加入负载均衡优先级队列末尾，如需调整请进入「负载均衡」。"
        if load_balancing.is_initialized() else ""
    )
    ui.send_result(chat_id, f"✅ 已添加 <code>{ui.escape_html(data['email'])}</code>{lb_hint}", **nav)


# ─── Cursor 浏览器 PKCE 登录（按钮轮询）────────────────────────────


_OA_NAV_CURSOR = {"back_label": "◀ 返回新增账户", "back_callback": "oa:add"}


def on_login_cursor_start(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    try:
        params = cursor_provider.generate_login()
    except Exception as exc:
        ui.send_result(
            chat_id,
            _oauth_error_html(exc, provider="cursor", operation="exchange_code"),
            **_OA_NAV_CURSOR,
        )
        return
    states.set_state(chat_id, "oa_cursor_login", {
        "uuid": params.uuid,
        "verifier": params.verifier,
        "created_at": time.time(),
        "login_url": params.login_url,
    })
    text = (
        f"请在浏览器打开以下链接登录 {_provider_tag('cursor', full=True)}：\n\n"
        f"<a href=\"{ui.escape_html(params.login_url)}\">📱 点此打开 Cursor 登录页</a>\n\n"
        "完成登录后回到这里点击 <b>✅ 已登录</b>。Parrot 会自动获取并保存 "
        "Access Token、Refresh Token、套餐额度和当前账号可用模型。\n\n"
        "👇 长按也可复制登录地址：\n"
        f"<code>{ui.escape_html(params.login_url)}</code>\n\n"
        "<i>如果仍提示未登录，请等待几秒后再次点击；登录会话 15 分钟内有效。</i>"
    )
    ui.edit(
        chat_id,
        message_id,
        text,
        reply_markup=ui.inline_kb([
            [ui.provider_url_button("打开 Cursor 登录页", params.login_url, "cursor")],
            [ui.btn("✅ 已登录", "oa:login:cursor:done"), ui.btn("❌ 取消", "oa:add")],
        ]),
    )


def on_login_cursor_done(chat_id: int, message_id: int, cb_id: str) -> None:
    state = states.get_state(chat_id)
    if not state or state.get("action") != "oa_cursor_login":
        ui.answer_cb(cb_id, "登录会话已失效，请重新发起", show_alert=True)
        return
    data = state.get("data") or {}
    if time.time() - float(data.get("created_at") or 0) > 15 * 60:
        states.pop_state(chat_id)
        ui.answer_cb(cb_id, "登录会话已过期，请重新生成", show_alert=True)
        ui.edit(
            chat_id, message_id,
            "❌ Cursor 登录会话已过期，请重新发起登录。",
            reply_markup=ui.inline_kb([[ui.btn("◀ 返回新增账户", "oa:add")]]),
        )
        return

    try:
        tokens = cursor_provider.poll_login_once(
            str(data.get("uuid") or ""),
            str(data.get("verifier") or ""),
        )
    except cursor_provider.CursorAuthPending:
        ui.answer_cb(cb_id, "Cursor 尚未确认登录，请完成浏览器登录后再点", show_alert=True)
        return
    except Exception as exc:
        ui.answer_cb(cb_id, "获取 Cursor Token 失败", show_alert=True)
        ui.edit(
            chat_id,
            message_id,
            _oauth_error_html(exc, provider="cursor", operation="exchange_code"),
            reply_markup=ui.inline_kb([[ui.btn("◀ 重新登录", "oa:login:cursor"), ui.btn("❌ 取消", "oa:add")]]),
        )
        return

    ui.answer_cb(cb_id, "登录成功，正在同步额度和模型...")
    try:
        subject = cursor_provider.subject_from_access_token(tokens.access_token)
        if not subject:
            raise ValueError("Cursor access token 缺少稳定 subject")
        catalog = cursor_provider.fetch_model_catalog_sync(tokens.access_token)
        records = catalog.get("models") if isinstance(catalog, dict) else None
        models = [
            str(item.get("id") or "") for item in records or []
            if isinstance(item, dict) and item.get("id")
        ]
        if not models:
            raise ValueError("Cursor AvailableModels 未返回可用模型")
    except Exception as exc:
        ui.edit(
            chat_id,
            message_id,
            f"❌ Cursor 登录已完成，但模型同步失败。\n"
            f"{_oauth_error_html(exc, provider='cursor', operation='fetch_models')}",
            reply_markup=ui.inline_kb([[ui.btn("🔄 重试获取", "oa:login:cursor:done"), ui.btn("❌ 取消", "oa:add")]]),
        )
        return

    profile: dict = {}
    profile_error: Exception | None = None
    try:
        profile = cursor_provider.fetch_profile_sync(
            tokens.access_token, account_key=f"cursor:{subject}",
        )
    except Exception as exc:
        # Profile is display metadata; keep login usable when cursor.com has a
        # temporary failure and retry on the next model/profile sync.
        profile_error = exc

    usage: dict | None = None
    usage_error: Exception | None = None
    try:
        usage = cursor_provider.fetch_usage_sync(tokens.access_token)
    except Exception as exc:
        usage_error = exc

    cursor_usage = (usage or {}).get("cursor") if isinstance(usage, dict) else {}
    cursor_usage = cursor_usage if isinstance(cursor_usage, dict) else {}
    profile_email = str(profile.get("email") or "").strip()
    email = profile_email or cursor_provider.account_label(subject)
    plan = str(cursor_usage.get("plan_name") or cursor_usage.get("individual_membership_type") or "Cursor")
    short_subject = hashlib.sha256(subject.encode("utf-8")).hexdigest()[:8]
    label = email if profile_email else (
        f"Cursor {plan} · {short_subject}" if plan and plan != "Cursor" else f"Cursor · {short_subject}"
    )
    expired = datetime.fromtimestamp(tokens.expires_at_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry = {
        "email": email,
        "label": label,
        "provider": "cursor",
        "type": "cursor",
        "subject": subject,
        "sub": subject,
        "cursor_profile_name": str(profile.get("name") or "").strip(),
        "cursor_profile_id": str(profile.get("id") or "").strip(),
        "cursor_email_verified": (
            profile.get("email_verified") if isinstance(profile.get("email_verified"), bool) else None
        ),
        "access_token": tokens.access_token,
        "refresh_token": tokens.refresh_token,
        "expired": expired,
        "last_refresh": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "enabled": True,
        "disabled_reason": None,
        "disabled_until": None,
        "models": list(dict.fromkeys(models)),
        "cursor_model_catalog": catalog,
        "last_model_sync": str(catalog.get("fetched_at") or ""),
        "plan_type": plan,
        "subscription_status": cursor_usage.get("subscription_status") or "",
        "billing_cycle_start": cursor_usage.get("billing_cycle_start") or "",
        "billing_cycle_end": cursor_usage.get("billing_cycle_end") or "",
    }
    account_key = _account_key(entry)
    existed = False  # duplicate returns above and is rendered by the confirmation callback
    try:
        save_action = _persist_new_or_stage_overwrite(
            chat_id, entry, source="Cursor 浏览器登录", usage=usage,
            message_id=message_id,
        )
        if save_action == "staged":
            return
        if usage is not None:
            save_error = _save_usage_to_quota_cache(account_key, usage, email=email)
            if save_error is not None:
                usage_error = save_error
            else:
                _evaluate_quota_action(account_key, usage)
    except Exception as exc:
        ui.edit(
            chat_id,
            message_id,
            f"❌ 保存 Cursor 账户失败：<code>{ui.escape_html(str(exc))[:500]}</code>",
            reply_markup=ui.inline_kb([[ui.btn("◀ 返回新增账户", "oa:add")]]),
        )
        return

    states.pop_state(chat_id)
    raw_total = cursor_usage.get("total_spend_cents")
    raw_limit = cursor_usage.get("limit_cents")
    raw_remaining = cursor_usage.get("remaining_cents")
    quota_line = "额度: <code>暂未获取，稍后可在账户详情刷新</code>"
    if raw_limit is not None:
        quota_line = (
            f"额度: <code>已用 ${float(raw_total or 0) / 100:.2f} / ${float(raw_limit) / 100:.2f}"
            + (f" · 剩余 ${float(raw_remaining) / 100:.2f}" if raw_remaining is not None else "")
            + "</code>"
        )
    if usage_error is not None:
        quota_line += "\n⚠️ 用量接口本次未完整返回，Token 和模型已保存。"
    profile_line = ""
    profile_name = str(profile.get("name") or "").strip()
    if profile_name:
        profile_line = f"姓名: <code>{ui.escape_html(profile_name)}</code>\n"
    if profile_error is not None:
        profile_line += "⚠️ 账号资料本次未获取，暂用内部标识；下次模型同步会重试。\n"
    title = "Cursor OAuth 账户已更新" if existed else "Cursor OAuth 账户已添加"
    lb_hint = (
        "\n已加入 OpenAI 家族负载均衡队列末尾，可在负载均衡菜单调整优先级。"
        if not existed and load_balancing.is_initialized() else ""
    )
    ui.edit(
        chat_id,
        message_id,
        f"✅ {_provider_tag('cursor')} <b>{title}</b>\n\n"
        f"账户: <code>{ui.escape_html(label)}</code>\n"
        f"{profile_line}"
        f"套餐: <code>{ui.escape_html(plan)}</code>\n"
        f"模型: <code>{len(models)} 个 canonical 模型</code>\n"
        f"原生变体: <code>{sum(len(item.get('legacy_slugs') or []) for item in records or [] if isinstance(item, dict))} 个</code>\n"
        f"{quota_line}\n"
        f"Token: <code>{_fmt_time_full(expired)}</code>{lb_hint}",
        reply_markup=ui.inline_kb([[ui.btn("◀ 返回 OAuth 列表", "menu:oauth")]]),
    )


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
        f"请在浏览器打开以下链接登录 {_provider_tag('openai')}（ChatGPT）账号：\n\n"
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
    """Compatibility helper for non-interactive callers; never overwrite a duplicate.

    Production Telegram paths stage duplicates through
    ``_persist_new_or_stage_overwrite``.  Keeping this helper fail-closed prevents
    old call sites/tests from reviving token-validity based skip/replace behavior.
    """
    if oauth_manager.find_exact_identity(entry) is not None:
        return "duplicate", "需要用户确认覆盖"
    oauth_manager.add_account(entry)
    return "added", "新增"


def _finish_openai_add(chat_id: int, tok: dict, *, source: str) -> None:
    """共用保存路径：从 token 解 email/workspace → 按重复策略保存 → 回报。"""
    try:
        entry, meta = _openai_token_to_entry(tok)
        action = _persist_new_or_stage_overwrite(
            chat_id, entry, source=source,
        )
        if action == "staged":
            return
        action_msg = "新增"
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
        "added": f"✅ {ui.provider_custom_emoji_html('openai')} <b>OpenAI OAuth 账户已添加</b>",
        "replaced": f"✅ {ui.provider_custom_emoji_html('openai')} <b>OpenAI OAuth 账户已更新</b>",
        "skipped": f"✅ {ui.provider_custom_emoji_html('openai')} <b>OpenAI OAuth 账户已存在</b>",
    }.get(action, f"✅ {ui.provider_custom_emoji_html('openai')} <b>OpenAI OAuth 账户已处理</b>")
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


# ─── xAI / Grok OAuth 登录 ───────────────────────────────────────

_OA_NAV_XAI = {"back_label": "◀ 返回新增账户", "back_callback": "oa:add"}


def _build_xai_login_text_and_kb(url: str) -> tuple[str, dict]:
    redirect = xai_provider.redirect_uri()
    text = (
        f"请在浏览器打开以下链接登录 {_provider_tag('xai', full=True)} 账号：\n\n"
        f"<a href=\"{ui.escape_html(url)}\">📱 点此打开登录页</a>\n\n"
        "👇 长按下方地址可复制：\n"
        f"<code>{ui.escape_html(url)}</code>\n\n"
        f"登录后浏览器会跳到 <code>{ui.escape_html(redirect)}?code=...&amp;state=...</code>"
        "（页面显示无法访问属正常，Parrot 不会监听这个端口）。\n"
        "请把 <b>地址栏里整段 URL</b> 复制发给我即可。\n\n"
        "<i>（登录会话 30 分钟内有效）</i>"
    )
    kb = ui.inline_kb([
        [ui.btn("🔄 重新生成登录地址", "oa:login:xai:regen")],
        [ui.btn("❌ 取消", "oa:add")],
    ])
    return text, kb


def on_login_xai_start(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    verifier, challenge = xai_provider.pkce_generate()
    state = secrets.token_urlsafe(32)
    try:
        discovery = xai_provider.discover_sync()
        authorization_endpoint = discovery.get("authorization_endpoint") or xai_provider.authorization_url()
        token_endpoint = discovery.get("token_endpoint") or xai_provider.token_url()
        url = xai_provider.build_login_url(
            challenge,
            state,
            authorization_endpoint=authorization_endpoint,
        )
    except Exception as exc:
        ui.send_result(
            chat_id,
            _oauth_error_html(exc, provider="xai", operation="discovery"),
            **_OA_NAV_XAI,
        )
        return

    states.set_state(chat_id, "oa_xai_code", {
        "code_verifier": verifier,
        "state": state,
        "token_endpoint": token_endpoint,
        "redirect_uri": xai_provider.redirect_uri(),
    })

    text, kb = _build_xai_login_text_and_kb(url)
    ui.edit(chat_id, message_id, text, reply_markup=kb)


def on_login_xai_regen(chat_id: int, message_id: int, cb_id: str) -> None:
    on_login_xai_start(chat_id, message_id, cb_id)


def on_login_xai_code_input(chat_id: int, text: str) -> None:
    state = states.pop_state(chat_id)
    if not state or state.get("action") != "oa_xai_code":
        ui.send_result(chat_id, "❌ 登录会话已失效，请重新发起登录流程。", **_OA_NAV_XAI)
        return
    data = state.get("data") or {}
    code, recv_state = _extract_openai_code_and_state(text)
    if not code:
        ui.send_result(chat_id, "❌ 没有抽到 code，请重新发起登录流程。", **_OA_NAV_XAI)
        return
    orig_state = data.get("state", "")
    if recv_state and orig_state and recv_state != orig_state:
        ui.send_result(
            chat_id,
            f"❌ state 不匹配：收到 <code>{ui.escape_html(recv_state[:16])}...</code>，"
            f"期望 <code>{ui.escape_html(orig_state[:16])}...</code>。"
            "可能是会话错乱，请重新发起登录流程。",
            **_OA_NAV_XAI,
        )
        return
    try:
        tok = xai_provider.exchange_code_sync(
            code,
            data.get("code_verifier", ""),
            redirect_uri=data.get("redirect_uri") or xai_provider.redirect_uri(),
            token_endpoint=data.get("token_endpoint") or xai_provider.token_url(),
        )
    except Exception as exc:
        ui.send_result(
            chat_id,
            _oauth_error_html(exc, provider="xai", operation="exchange_code"),
            **_OA_NAV_XAI,
        )
        return

    _finish_xai_add(chat_id, tok, source="login")


def on_set_rt_xai_start(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    states.set_state(chat_id, "oa_xai_rt")
    ui.edit(
        chat_id, message_id,
        f"请粘贴 {_provider_tag('xai', full=True)} <b>refresh_token</b>（纯字符串即可，代理会立即刷新一次 "
        "token 并从 id_token 解出 email / subject）：",
        reply_markup=ui.inline_kb([[ui.btn("❌ 取消", "oa:add")]]),
    )


def on_set_rt_xai_input(chat_id: int, text: str) -> None:
    states.pop_state(chat_id)
    rt = (text or "").strip()
    m = re.search(r"([A-Za-z0-9_\-\.]{20,})", rt)
    rt_clean = m.group(1) if m else rt
    if not rt_clean or len(rt_clean) < 20:
        ui.send_result(chat_id, "❌ refresh_token 过短或无法识别，请重新粘贴。", **_OA_NAV_XAI)
        return
    try:
        tok = xai_provider.refresh_sync(rt_clean)
    except Exception as exc:
        ui.send_result(
            chat_id,
            _oauth_error_html(exc, provider="xai", operation="refresh_token"),
            **_OA_NAV_XAI,
        )
        return
    if not tok.get("refresh_token"):
        tok["refresh_token"] = rt_clean
    _finish_xai_add(chat_id, tok, source="rt")


def _xai_token_to_entry(tok: dict, *, fallback_email: str = "") -> tuple[dict, dict]:
    id_token = tok.get("id_token", "") or ""
    info: dict = {}
    if id_token:
        try:
            info = xai_provider.extract_user_info(xai_provider.decode_id_token(id_token))
        except Exception as exc:
            raise ValueError(f"id_token 解码失败: {exc}") from exc

    email = tok.get("email") or info.get("email") or fallback_email or ""
    subject = tok.get("subject") or tok.get("sub") or info.get("subject") or ""
    if not email:
        email = f"unnamed-xai-{int(datetime.now().timestamp())}@local"

    expires_in = int(tok.get("expires_in", 3600) or 3600)
    new_expired = (
        datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry = {
        "email": email,
        "provider": "xai",
        "access_token": tok.get("access_token", ""),
        "refresh_token": tok.get("refresh_token", ""),
        "expired": new_expired,
        "last_refresh": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "type": "xai",
        "enabled": True,
        "disabled_reason": None,
        "disabled_until": None,
        "models": [],
        "id_token": id_token,
        "subject": subject,
        "sub": subject,
        "base_url": tok.get("base_url") or tok.get("baseUrl") or xai_provider.api_base_url(),
        "token_endpoint": tok.get("token_endpoint") or xai_provider.token_url(),
        "redirect_uri": tok.get("redirect_uri") or xai_provider.redirect_uri(),
    }
    meta = {"email": email, "subject": subject, "expired": new_expired}
    return entry, meta


def _finish_xai_add(chat_id: int, tok: dict, *, source: str) -> None:
    try:
        entry, meta = _xai_token_to_entry(tok)
        ak = _account_key(entry)
        save_action = _persist_new_or_stage_overwrite(
            chat_id, entry, source=source,
        )
        if save_action == "staged":
            return
        existed = False
    except Exception as exc:
        ui.send_result(
            chat_id,
            f"❌ 保存失败: <code>{ui.escape_html(str(exc))[:500]}</code>",
            **_OA_NAV_XAI,
        )
        return

    title = (f"✅ {ui.provider_custom_emoji_html('xai')} <b>Grok OAuth 账户已更新</b>" if existed
             else f"✅ {ui.provider_custom_emoji_html('xai')} <b>Grok OAuth 账户已添加</b>")
    lb_hint = (
        "\n已加入负载均衡优先级队列末尾，如需调整请进入「负载均衡」。"
        if not existed and load_balancing.is_initialized() else ""
    )
    subject_line = f"Subject: <code>{ui.escape_html(meta.get('subject') or '')}</code>\n" if meta.get("subject") else ""
    ui.send_result(
        chat_id,
        f"{title}\n\n"
        f"Email: <code>{ui.escape_html(meta.get('email') or entry.get('email') or '')}</code>\n"
        f"{subject_line}"
        f"过期: <code>{_fmt_time_full(meta.get('expired'))}</code>\n"
        "额度: <code>xAI 暂无已确认的零成本 quota 端点</code>\n"
        f"来源: <code>{ui.escape_html(source)}</code>{lb_hint}",
        **_OA_NAV_XAI,
    )


# ─── Antigravity / Google OAuth 登录 ───────────────────────────────

_OA_NAV_ANTIGRAVITY = {"back_label": "◀ 返回新增账户", "back_callback": "oa:add"}


def _build_antigravity_login_text_and_kb(url: str) -> tuple[str, dict]:
    redirect = antigravity_provider.redirect_uri()
    text = (
        f"请在浏览器打开以下链接登录 {_provider_tag('antigravity', full=True)} 账号：\n\n"
        f"<a href=\"{ui.escape_html(url)}\">📱 点此打开登录页</a>\n\n"
        "👇 长按下方地址可复制：\n"
        f"<code>{ui.escape_html(url)}</code>\n\n"
        f"登录后浏览器会跳到 <code>{ui.escape_html(redirect)}?code=...&amp;state=...</code>"
        "（页面显示无法访问属正常，Parrot 不会监听这个端口）。\n"
        "请把 <b>地址栏里整段 URL</b> 复制发给我即可。\n\n"
        "<i>（登录会话 30 分钟内有效）</i>"
    )
    kb = ui.inline_kb([
        [ui.btn("🔄 重新生成登录地址", "oa:login:antigravity:regen")],
        [ui.btn("❌ 取消", "oa:add")],
    ])
    return text, kb


def on_login_antigravity_start(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    state = secrets.token_urlsafe(32)
    url = antigravity_provider.build_login_url(state)
    states.set_state(chat_id, "oa_antigravity_code", {
        "state": state,
        "token_endpoint": antigravity_provider.token_url(),
        "redirect_uri": antigravity_provider.redirect_uri(),
    })
    text, kb = _build_antigravity_login_text_and_kb(url)
    ui.edit(chat_id, message_id, text, reply_markup=kb)


def on_login_antigravity_regen(chat_id: int, message_id: int, cb_id: str) -> None:
    on_login_antigravity_start(chat_id, message_id, cb_id)


def on_login_antigravity_code_input(chat_id: int, text: str) -> None:
    state = states.pop_state(chat_id)
    if not state or state.get("action") != "oa_antigravity_code":
        ui.send_result(chat_id, "❌ 登录会话已失效，请重新发起登录流程。", **_OA_NAV_ANTIGRAVITY)
        return
    data = state.get("data") or {}
    try:
        parsed = antigravity_provider.parse_callback_url(text)
    except Exception as exc:
        ui.send_result(
            chat_id,
            f"❌ 没有抽到 code：<code>{ui.escape_html(str(exc))[:300]}</code>",
            **_OA_NAV_ANTIGRAVITY,
        )
        return
    code = parsed.get("code") or ""
    recv_state = parsed.get("state") or ""
    orig_state = data.get("state", "")
    if recv_state and orig_state and recv_state != orig_state:
        ui.send_result(
            chat_id,
            f"❌ state 不匹配：收到 <code>{ui.escape_html(recv_state[:16])}...</code>，"
            f"期望 <code>{ui.escape_html(orig_state[:16])}...</code>。"
            "可能是会话错乱，请重新发起登录流程。",
            **_OA_NAV_ANTIGRAVITY,
        )
        return
    try:
        tok = antigravity_provider.complete_login_sync(
            code,
            redirect_uri=data.get("redirect_uri") or antigravity_provider.redirect_uri(),
            token_endpoint=data.get("token_endpoint") or antigravity_provider.token_url(),
        )
    except Exception as exc:
        ui.send_result(
            chat_id,
            _oauth_error_html(exc, provider="antigravity", operation="exchange_code"),
            **_OA_NAV_ANTIGRAVITY,
        )
        return
    _finish_antigravity_add(chat_id, tok, source="login")


def _antigravity_token_to_entry(tok: dict) -> tuple[dict, dict]:
    email = str(tok.get("email") or "").strip()
    project_id = str(tok.get("project_id") or tok.get("projectId") or "").strip()
    if not email:
        raise ValueError("Antigravity 登录结果缺少 email")
    if not project_id:
        raise ValueError("Antigravity 登录结果缺少 project_id")
    expires_in = int(tok.get("expires_in", 3600) or 3600)
    new_expired = (
        datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry = {
        "email": email,
        "provider": "antigravity",
        "access_token": tok.get("access_token", ""),
        "refresh_token": tok.get("refresh_token", ""),
        "expired": new_expired,
        "last_refresh": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "type": "antigravity",
        "enabled": True,
        "disabled_reason": None,
        "disabled_until": None,
        "models": [],
        "project_id": project_id,
        "base_url": tok.get("base_url") or tok.get("baseUrl") or antigravity_provider.api_base_url(),
        "token_endpoint": tok.get("token_endpoint") or antigravity_provider.token_url(),
        "redirect_uri": tok.get("redirect_uri") or antigravity_provider.redirect_uri(),
    }
    meta = {"email": email, "project_id": project_id, "expired": new_expired}
    return entry, meta


def _finish_antigravity_add(chat_id: int, tok: dict, *, source: str) -> None:
    try:
        entry, meta = _antigravity_token_to_entry(tok)
        save_action = _persist_new_or_stage_overwrite(
            chat_id, entry, source=source,
        )
        if save_action == "staged":
            return
        existed = False
    except Exception as exc:
        ui.send_result(
            chat_id,
            f"❌ 保存失败: <code>{ui.escape_html(str(exc))[:500]}</code>",
            **_OA_NAV_ANTIGRAVITY,
        )
        return

    title = (
        f"✅ {ui.provider_custom_emoji_html('antigravity')} <b>Antigravity OAuth 账户已更新</b>"
        if existed else
        f"✅ {ui.provider_custom_emoji_html('antigravity')} <b>Antigravity OAuth 账户已添加</b>"
    )
    lb_hint = (
        "\n已加入负载均衡优先级队列末尾，如需调整请进入「负载均衡」。"
        if not existed and load_balancing.is_initialized() else ""
    )
    ui.send_result(
        chat_id,
        f"{title}\n\n"
        f"Email: <code>{ui.escape_html(meta.get('email') or entry.get('email') or '')}</code>\n"
        f"Project: <code>{ui.escape_html(meta.get('project_id') or '')}</code>\n"
        f"过期: <code>{_fmt_time_full(meta.get('expired'))}</code>\n"
        "额度: <code>Credits 将在账户详情中刷新</code>\n"
        f"来源: <code>{ui.escape_html(source)}</code>{lb_hint}",
        **_OA_NAV_ANTIGRAVITY,
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
        f"<b>导入</b> {_provider_tag('openai')} <b>{label} 账户</b>\n\n"
        "请上传 <code>.zip</code> / <code>.json</code> 文件，或直接粘贴 JSON 文本。\n\n"
        "我会只提取 <code>email</code> 与 <code>refresh_token</code>，随后复用"
        f"「{ui.provider_tag('openai')} 粘贴 refresh_token」逻辑刷新并导入。\n\n"
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
    lines = [f"<b>导入</b> {_provider_tag('openai')} <b>{label} 账户</b>", "", "当前识别到："]
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


def _stage_openai_import_candidates(items: list[dict]) -> dict:
    """Refresh/classify candidates without persistent writes."""
    staged = {"new": [], "duplicate": [], "failed": []}
    seen: set[str] = set()
    for item in items:
        hint, rt = str(item.get("email") or "").strip(), str(item.get("refresh_token") or "").strip()
        if not rt:
            staged["failed"].append((hint or "?", "缺少 refresh_token")); continue
        entry, meta, error = _refresh_openai_rt_to_entry(rt, email_hint=hint)
        if entry is None:
            staged["failed"].append((hint or "?", _format_import_error(error))); continue
        if not _openai_workspace_id(entry):
            staged["failed"].append((entry.get("email") or hint or "?", "token 缺少 canonical workspace identity")); continue
        key = _account_key(entry)
        if key in seen:
            staged["failed"].append((entry.get("email") or hint or "?", "同批 canonical identity 重复，已去重")); continue
        seen.add(key)
        record = {"account_key": key, "entry": entry, "meta": meta}
        staged["duplicate" if oauth_manager.find_exact_identity(entry) else "new"].append(record)
    return staged


def _commit_staged_openai_import(staged: dict) -> dict:
    result = {"added": [], "replaced": [], "failed": list(staged.get("failed") or [])}
    for record in staged.get("new") or []:
        entry = record["entry"]
        added = oauth_manager.add_account_if_identity_absent(entry)
        if added.get("status") != "added":
            result["failed"].append((entry.get("email") or "?", "账户已并发出现，请重新导入确认")); continue
        result["added"].append(entry.get("email") or "?")
        usage = _fetch_and_save_usage_sync(record["account_key"], email=entry.get("email") or "")
        if not isinstance(usage, Exception):
            _evaluate_quota_action(record["account_key"], usage)
    for record in staged.get("duplicate") or []:
        entry = record["entry"]
        replacement = oauth_manager.replace_exact_identity(record["account_key"], entry)
        if replacement.get("status") != "replaced":
            result["failed"].append((entry.get("email") or "?", "目标账户已变化，请重新导入确认")); continue
        result["replaced"].append(entry.get("email") or "?")
        usage = _fetch_and_save_usage_sync(record["account_key"], email=entry.get("email") or "")
        if not isinstance(usage, Exception):
            _evaluate_quota_action(record["account_key"], usage)
    return result


def _render_openai_import_result(chat_id: int, message_id: int, label: str, result: dict) -> None:
    failed = result.get("failed") or []
    lines = [f"<b>导入</b> {_provider_tag('openai')} <b>{label} 账户完成</b>", "",
             f"✅ 新增 {len(result.get('added') or [])} 个", f"🔁 覆盖 {len(result.get('replaced') or [])} 个",
             f"❌ 失败 {len(failed)} 个"]
    lines += [f"• <code>{ui.escape_html(str(e))}</code>：{ui.escape_html(str(m))}" for e, m in failed[:20]]
    ui.edit(chat_id, message_id, "\n".join(lines), reply_markup=ui.inline_kb([[ui.btn("◀ 返回 OAuth 列表", "menu:oauth")]]))


def on_import_openai_exec(chat_id: int, message_id: int, cb_id: str) -> None:
    state = states.pop_state(chat_id)
    if not state or state.get("action") != "oa_openai_import_confirm":
        ui.answer_cb(cb_id, "导入会话已失效", show_alert=True); return
    data = state.get("data") or {}; kind = data.get("kind", ""); items = list(data.get("items") or [])
    label = _OPENAI_IMPORT_LABELS.get(kind, kind.upper())
    if not items:
        ui.answer_cb(cb_id, "没有可导入账号", show_alert=True); return
    ui.answer_cb(cb_id, "正在解析候选…")
    staged = _stage_openai_import_candidates(items)
    if staged["duplicate"]:
        nonce = secrets.token_urlsafe(12)
        states.set_state(chat_id, "oa_openai_import_overwrite_confirm", {"nonce": nonce, "kind": kind, "staged": staged})
        ui.edit(chat_id, message_id,
                f"<b>{label} 导入二次确认</b>\n\n新增 <b>{len(staged['new'])}</b>、覆盖 <b>{len(staged['duplicate'])}</b>、失败 <b>{len(staged['failed'])}</b>。\n取消将整批零写入。是否提交？",
                reply_markup=ui.inline_kb([[ui.btn("✅ 提交", f"oa:import_overwrite:confirm:{nonce}"), ui.btn("❌ 取消", f"oa:import_overwrite:cancel:{nonce}")]]))
        return
    _render_openai_import_result(chat_id, message_id, label, _commit_staged_openai_import(staged))


def on_import_openai_overwrite(chat_id: int, message_id: int, cb_id: str, nonce: str, *, confirm: bool) -> None:
    state = states.get_state(chat_id)
    data = (state.get("data") or {}) if state and state.get("action") == "oa_openai_import_overwrite_confirm" else {}
    if not data or not secrets.compare_digest(str(data.get("nonce") or ""), str(nonce or "")):
        ui.answer_cb(cb_id, "导入确认已失效，请重新导入", show_alert=True); return
    states.pop_state(chat_id)
    if not confirm:
        ui.answer_cb(cb_id, "已取消，整批未写入")
        ui.edit(chat_id, message_id, "✅ 已取消导入，所有候选均未写入。", reply_markup=ui.inline_kb([[ui.btn("◀ 返回新增账户", "oa:add")]])); return
    result = _commit_staged_openai_import(data.get("staged") or {})
    label = _OPENAI_IMPORT_LABELS.get(data.get("kind", ""), str(data.get("kind") or "").upper())
    ui.answer_cb(cb_id, "导入完成")
    _render_openai_import_result(chat_id, message_id, label, result)


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
    ak = _account_key_from_short(short)
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
    if data == "oa:login:cursor":
        on_login_cursor_start(chat_id, message_id, cb_id)
        return True
    if data == "oa:login:cursor:done":
        on_login_cursor_done(chat_id, message_id, cb_id)
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
    if data == "oa:login:xai":
        on_login_xai_start(chat_id, message_id, cb_id)
        return True
    if data == "oa:login:xai:regen":
        on_login_xai_regen(chat_id, message_id, cb_id)
        return True
    if data == "oa:set_rt:xai":
        on_set_rt_xai_start(chat_id, message_id, cb_id)
        return True
    if data == "oa:login:antigravity":
        on_login_antigravity_start(chat_id, message_id, cb_id)
        return True
    if data == "oa:login:antigravity:regen":
        on_login_antigravity_regen(chat_id, message_id, cb_id)
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
    if data.startswith("oa:import_overwrite:confirm:"):
        on_import_openai_overwrite(chat_id, message_id, cb_id, data.rsplit(":", 1)[-1], confirm=True)
        return True
    if data.startswith("oa:import_overwrite:cancel:"):
        on_import_openai_overwrite(chat_id, message_id, cb_id, data.rsplit(":", 1)[-1], confirm=False)
        return True
    if data.startswith("oa:overwrite:confirm:"):
        on_oauth_overwrite_confirm(chat_id, message_id, cb_id, data.rsplit(":", 1)[-1])
        return True
    if data.startswith("oa:overwrite:cancel:"):
        on_oauth_overwrite_cancel(chat_id, message_id, cb_id, data.rsplit(":", 1)[-1])
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
    if data.startswith("oa:cursor_models:"):
        on_cursor_models(chat_id, message_id, cb_id, data.split(":", 2)[2])
        return True
    if data.startswith("oa:cursor_disable:"):
        on_cursor_disable_start(chat_id, message_id, cb_id, data.split(":", 2)[2])
        return True
    if data.startswith("oa:cursor_dis_sel:"):
        on_cursor_disable_select(chat_id, message_id, cb_id, data.split(":", 2)[2])
        return True
    if data == "oa:cursor_dis_all" or data.startswith("oa:cursor_dis_all:"):
        payload = data.split(":", 2)[2] if data.startswith("oa:cursor_dis_all:") else ""
        on_cursor_disable_set_all(
            chat_id, message_id, cb_id, selected_all=True, payload=payload,
        )
        return True
    if data == "oa:cursor_dis_clear" or data.startswith("oa:cursor_dis_clear:"):
        payload = data.split(":", 2)[2] if data.startswith("oa:cursor_dis_clear:") else ""
        on_cursor_disable_set_all(
            chat_id, message_id, cb_id, selected_all=False, payload=payload,
        )
        return True
    if data == "oa:cursor_dis_save" or data.startswith("oa:cursor_dis_save:"):
        payload = data.split(":", 2)[2] if data.startswith("oa:cursor_dis_save:") else ""
        on_cursor_disable_save(chat_id, message_id, cb_id, payload)
        return True
    if data == "oa:cursor_dis_cancel" or data.startswith("oa:cursor_dis_cancel:"):
        payload = data.split(":", 2)[2] if data.startswith("oa:cursor_dis_cancel:") else ""
        on_cursor_disable_cancel(chat_id, message_id, cb_id, payload)
        return True
    if data.startswith("oa:cursor_model:"):
        on_cursor_model_detail(chat_id, message_id, cb_id, data.split(":", 2)[2])
        return True
    if data.startswith("oa:cursor_maxctx:"):
        on_cursor_max_context_toggle(chat_id, message_id, cb_id, data.split(":", 2)[2])
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
    if action == "oa_xai_code":
        on_login_xai_code_input(chat_id, text)
        return True
    if action == "oa_xai_rt":
        on_set_rt_xai_input(chat_id, text)
        return True
    if action == "oa_antigravity_code":
        on_login_antigravity_code_input(chat_id, text)
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
    ak = _account_key_from_short(short)
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
