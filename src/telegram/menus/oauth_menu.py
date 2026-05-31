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
import re
import secrets
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
    if name:
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


def _fetch_and_save_usage_sync(ak: str, *, email: str | None = None):
    """同步拉 usage 并写 quota cache；返回 usage 或 Exception。"""
    usage = _run_sync(oauth_manager.fetch_usage(ak))
    if isinstance(usage, Exception):
        return usage
    try:
        state_db.quota_save(
            ak, oauth_manager.flatten_usage(usage),
            email=email if email is not None else _account_email(ak),
        )
    except Exception as exc:
        return exc
    return usage


def _evaluate_quota_action(ak: str, usage: dict) -> dict | None:
    try:
        return oauth_manager.evaluate_and_toggle_by_usage(ak, usage)
    except Exception as exc:
        print(f"[oauth_menu] quota evaluate failed for {ak}: {exc}")
        return None


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
    dt = _parse_iso(iso_str)
    if dt is None:
        return "?"
    delta = (dt - datetime.now(timezone.utc)).total_seconds()
    if delta <= 0:
        return "已过期"
    hours = int(delta // 3600)
    minutes = int((delta % 3600) // 60)
    if hours > 0:
        return f"剩 {hours}h {minutes}m"
    return f"剩 {minutes}m"


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


def _format_account_block(acc: dict) -> str:
    """列表中每条 OAuth 账号的多行展示块。

    示例：
        ✅ marlenaplocheroei79@gmail.com
          ⏳ Token 过期: 2026-04-19 12:57:39 (剩 1h 7m)
          📊 5h 用量:  17% | 重置: 2026-04-19 14:00:00
          📊 7d 用量:  26% | 重置: 2026-04-25 08:00:00
          💎 月度统计: ↑ 104.7M · ↓ 1.8M · 缓存 99.8M (95.7%)
    """
    email = acc.get("email", "?")
    ak = _account_key(acc)
    icon = _status_icon(acc)
    reason = acc.get("disabled_reason")
    tag = ""
    if reason == "user":
        tag = " [用户禁用]"
    elif reason == "quota":
        du = acc.get("disabled_until")
        tag = f" [配额禁用 · 预计 {_format_bjt(du)}]"
    elif reason == "auth_error":
        tag = " [认证失败]"

    # provider 图标 + 套餐标签
    prov = oauth_manager.provider_of(acc)
    provider_tag = ""
    if prov == "openai":
        plan = acc.get("plan_type") or ""
        sub_exp = acc.get("subscription_expires_at") or ""
        suffix = f" · {ui.escape_html(plan)}" if plan else ""
        if sub_exp:
            suffix += f" · sub 到 {_format_bjt(sub_exp)}"
        workspace = _openai_workspace_label(acc)
        if workspace:
            suffix += f" · {workspace}"
        provider_tag = f" 🅾 OpenAI{suffix}"
    elif prov == "claude":
        cl_label = oauth_manager.claude_plan_label(acc)
        if cl_label:
            provider_tag = f" 🅰 {ui.escape_html(cl_label)}"

    lines = [f"{icon} <code>{ui.escape_html(email)}</code>{provider_tag}{tag}"]

    # Token 过期时间（绝对 + 倒计时）
    expired = acc.get("expired")
    if expired:
        lines.append(
            f"  ⏳ Token 过期: <code>{_format_bjt(expired)}</code>"
            f" ({_format_remaining(expired)})"
        )

    # 用量（5h / 7d，列成两行，各带绝对重置时间）
    row = state_db.quota_load(ak)
    if row:
        fh_util = row.get("five_hour_util")
        sd_util = row.get("seven_day_util")
        if fh_util is not None:
            reset = row.get("five_hour_reset")
            reset_str = _format_reset_text(reset)
            lines.append(
                f"  📊 5h 用量: <b>{fh_util:>4.0f}%</b> | 重置: <code>{reset_str}</code>"
            )
        if sd_util is not None:
            reset = row.get("seven_day_reset")
            reset_str = _format_reset_text(reset)
            lines.append(
                f"  📊 7d 用量: <b>{sd_util:>4.0f}%</b> | 重置: <code>{reset_str}</code>"
            )
        if fh_util is None and sd_util is None:
            lines.append("  📊 用量: <i>尚未获取</i>")
    else:
        # OpenAI / Claude 都走同一条路径：点账户详情的“刷新用量”按钮。
        # OpenAI 主动刷新走 wham/usage；业务响应头仍会实时补充 x-codex-*。
        lines.append("  📊 用量: <i>尚未获取</i>（请点账户详情手动刷新一次）")

    # 月度统计（本月 log_db 聚合）
    try:
        since_ts = _this_month_start_ts()
        ts = log_db.tokens_for_channel(f"oauth:{ak}", since_ts=since_ts)
    except Exception:
        ts = None
    if ts and ts["total"] > 0:
        prompt = ui.prompt_total(ts["input"], ts["cache_creation"], ts["cache_read"])
        stat_line = f"  💎 月度统计: ↑ {ui.fmt_tokens(prompt)} · ↓ {ui.fmt_tokens(ts['output'])}"
        if (ts.get("cache_read") or 0) > 0:
            stat_line += f" · {ui.fmt_cache_phrase(ts['cache_read'], prompt)}"
        lines.append(stat_line)
        if ts.get("avg_tps") is not None:
            lines.append(
                f"  ⚡ 本月 TPS: 平均 {ui.fmt_tps(ts.get('avg_tps'))} · "
                f"峰值 {ui.fmt_tps(ts.get('max_tps'))} · "
                f"最低 {ui.fmt_tps(ts.get('min_tps'))}"
            )

    # 冷却状态（该账号下所有模型聚合）：简短提示；详情在详情页展开
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
        lines.append("  ⚠ " + " · ".join(parts))

    return "\n".join(lines)


def _format_usage_block(account_key: str) -> str:
    row = state_db.quota_load(account_key)
    if not row:
        return "尚未获取用量（点「刷新用量」试试）"

    def _line(label: str, util, reset) -> Optional[str]:
        if util is None:
            return None
        return f"{label}: {util:.0f}% (重置: {_format_reset_text(reset)})"

    out = []
    for label, util_k, reset_k in (
        ("⏱ 5h", "five_hour_util", "five_hour_reset"),
        ("📅 7d", "seven_day_util", "seven_day_reset"),
        ("🤖 Sonnet 7d", "sonnet_util", "sonnet_reset"),
        ("🧠 Opus 7d", "opus_util", "opus_reset"),
    ):
        line = _line(label, row.get(util_k), row.get(reset_k))
        if line:
            out.append(line)

    # OpenAI Codex 原始窗口（primary/secondary）：用量值与上方 5h/7d 一致，但展示
    # 原始窗口时长（分钟）让 admin 能看到源数据（例如 primary=10080min=7d，secondary=300min=5h）。
    codex_primary_pct = row.get("codex_primary_used_pct")
    codex_primary_win = row.get("codex_primary_window_min")
    codex_secondary_pct = row.get("codex_secondary_used_pct")
    codex_secondary_win = row.get("codex_secondary_window_min")
    if codex_primary_pct is not None or codex_secondary_pct is not None:
        out.append("")
        out.append("Codex 原始窗口:")
        if codex_primary_pct is not None and codex_primary_win:
            out.append(f"  primary ({codex_primary_win}min): {codex_primary_pct:.0f}%")
        if codex_secondary_pct is not None and codex_secondary_win:
            out.append(f"  secondary ({codex_secondary_win}min): {codex_secondary_pct:.0f}%")

    ex_used = row.get("extra_used")
    ex_limit = row.get("extra_limit")
    ex_util = row.get("extra_util")
    if ex_limit and ex_limit > 0:
        out.append(f"💰 额外: ${ex_used or 0:.2f} / ${ex_limit:.2f} ({ex_util or 0:.1f}%)")

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
    # 按访问节流刷新所有账户的 usage（quotaMonitor.enabled=True 时内部跳过）
    account_keys = [
        _account_key(a) for a in accounts_all
        if a.get("email") and not a.get("disabled_reason")
    ]
    if account_keys:
        oauth_manager.ensure_quota_fresh_sync(account_keys)
        # 如果缓存已经显示 >= quotaMonitor 阈值，立即收敛账号状态，
        # 不等 600s 后台监控下一轮。
        for ak in account_keys:
            try:
                oauth_manager.evaluate_and_toggle_by_cached_quota(ak)
            except Exception as exc:
                print(f"[oauth_menu] cached quota evaluate failed for {ak}: {exc}")
        accounts_all = oauth_manager.list_accounts()
    total_all = len(accounts_all)
    normal = sum(1 for a in accounts_all if a.get("enabled", True) and not a.get("disabled_reason"))
    quota_disabled = sum(1 for a in accounts_all if a.get("disabled_reason") == "quota")
    user_disabled = sum(1 for a in accounts_all if a.get("disabled_reason") == "user")
    auth_err = sum(1 for a in accounts_all if a.get("disabled_reason") == "auth_error")
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

    # 翻页
    pag_row = _build_pagination_row(page, total_pages, filter_key)
    if pag_row:
        rows.append(pag_row)

    # 过滤按钮
    rows.append([
        ui.btn(f"全部{'√' if filter_key == _FILTER_ALL else ''}", _page_callback(1, _FILTER_ALL)),
        ui.btn(f"可用{'√' if filter_key == _FILTER_AVAILABLE else ''}", _page_callback(1, _FILTER_AVAILABLE)),
        ui.btn(f"限额{'√' if filter_key == _FILTER_QUOTA else ''}", _page_callback(1, _FILTER_QUOTA)),
        ui.btn(f"失效{'√' if filter_key == _FILTER_INVALID else ''}", _page_callback(1, _FILTER_INVALID)),
    ])

    # 操作按钮（每页都有）
    rows.append([
        ui.btn("➕ 新增账户", "oa:add"),
        ui.btn("🔄 刷新全部用量", f"oa:refresh_all:{page}" if filter_key == _FILTER_ALL else f"oa:refresh_all:{page}:{filter_key}"),
    ])
    # 只有存在 OAuth 账号的冷却条目时才显示"清除所有错误"（避免空操作按钮）
    if cd_keys_any:
        clear_cb = f"oa:clear_all_errors:{page}" if filter_key == _FILTER_ALL else f"oa:clear_all_errors:{page}:{filter_key}"
        rows.append([ui.btn(f"🧹 清除所有账户错误（{len(cd_keys_any)} 个）", clear_cb)])
    rows.append([ui.btn("🖼 图片生成", "img:show"), ui.btn("🧨 移除失效", "oa:invalid:list")])
    rows.append([ui.btn("🧩 OAuth 默认模型", "odm:show"), ui.btn("◀ 返回主菜单", "menu:main")])
    return ui.truncate(_maybe_suffix_status_banner(text)), ui.inline_kb(rows)


def show(chat_id: int, message_id: int, cb_id: Optional[str] = None, page: int = 1, filter_key: str = _FILTER_ALL) -> None:
    if cb_id is not None:
        ui.answer_cb(cb_id)
    text, kb = _list_text_and_kb(page=page, filter_key=filter_key)
    ui.edit(chat_id, message_id, text, reply_markup=kb)


def send_new(chat_id: int, page: int = 1, filter_key: str = _FILTER_ALL) -> None:
    text, kb = _list_text_and_kb(page=page, filter_key=filter_key)
    ui.send(chat_id, text, reply_markup=kb)


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


def _detail_text_and_kb(account_key: str, page: int = 1, filter_key: str = _FILTER_ALL) -> tuple[Optional[str], Optional[dict]]:
    acc = oauth_manager.get_account(account_key)
    if acc is None:
        return None, None
    email = acc.get("email", "?")

    if not acc.get("disabled_reason"):
        oauth_manager.ensure_quota_fresh_sync(account_key)
        try:
            oauth_manager.evaluate_and_toggle_by_cached_quota(account_key)
        except Exception as exc:
            print(f"[oauth_menu] cached quota evaluate failed for {account_key}: {exc}")
        acc = oauth_manager.get_account(account_key) or acc

    icon = _status_icon(acc)
    reason = acc.get("disabled_reason") or "—"
    prov = oauth_manager.provider_of(acc)
    provider_line = ""
    if prov == "openai":
        plan = acc.get("plan_type") or "?"
        sub_exp = acc.get("subscription_expires_at") or ""
        sub_line = (
            f" · 订阅到: <code>{_format_bjt(sub_exp)}</code>" if sub_exp else ""
        )
        provider_line = (
            f"提供者: <code>🅾 OpenAI (Codex)</code> · 计划: <code>{ui.escape_html(plan)}</code>{sub_line}\n"
        )
        workspace = _openai_workspace_label(acc, force=True)
        if workspace and _openai_same_email_count(acc) > 1:
            provider_line += f"工作区: <code>{workspace}</code>\n"
    elif prov == "claude":
        cl_label = oauth_manager.claude_plan_label(acc)
        cl_suffix = f" · {ui.escape_html(cl_label)}" if cl_label else ""
        provider_line = f"提供者: <code>🅰 Anthropic (Claude){cl_suffix}</code>\n"
        # 订阅信息
        sub_status = acc.get("subscription_status") or ""
        billing = acc.get("billing_type") or ""
        sub_created = acc.get("subscription_created_at") or ""
        if sub_status or billing:
            sub_parts = [s for s in (sub_status, billing) if s]
            provider_line += f"订阅: <code>{ui.escape_html(' · '.join(sub_parts))}</code>\n"
        if sub_created:
            provider_line += f"订阅开始: <code>{_format_bjt(sub_created)}</code>\n"
    max_cc = int(acc.get("maxConcurrent", 0) or 0)
    max_cc_label = str(max_cc) if max_cc > 0 else "默认"
    text = (
        f"{icon} <b>{ui.escape_html(email)}</b>\n\n"
        f"状态: <code>{ui.escape_html('enabled' if acc.get('enabled', True) and not acc.get('disabled_reason') else reason)}</code>\n"
        f"{provider_line}"
        f"⚡ 并发上限: <code>{max_cc_label}</code>\n"
        f"过期: <code>{_format_bjt(acc.get('expired'))}</code> ({_format_remaining(acc.get('expired'))})\n"
        f"上次刷新: <code>{_format_bjt(acc.get('last_refresh'))}</code>\n\n"
        f"<b>📊 使用量</b>\n{_format_usage_block(account_key)}"
    )
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
         ui.btn("📊 刷新用量",   f"oa:refresh_usage:{payload}")],
        [ui.btn("🧹 清模型错误", f"oa:clear_errors:{payload}"),
         ui.btn("🔗 清亲和绑定", f"oa:clear_affinity:{payload}")],
        [ui.btn(f"⚡ 修改并发上限（当前: {max_cc_label}）", f"oa:emax:{payload}")],
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


# ─── 刷新用量 ─────────────────────────────────────────────────────

def on_refresh_usage(chat_id: int, message_id: int, cb_id: str, short: str, page: int = 1, filter_key: str = _FILTER_ALL) -> None:
    ak = _resolve_to_account_key(ui.resolve_code(short))
    if ak is None:
        ui.answer_cb(cb_id, "短码已失效")
        return
    email = _account_email(ak)
    provider = oauth_manager.provider_of(ak)
    if provider == "openai":
        ui.answer_cb(cb_id, "刷新 Token 并拉取额度...")
        tr = _run_sync(oauth_manager.force_refresh(ak))
        if isinstance(tr, Exception):
            ui.send(chat_id, _oauth_error_html(
                tr, provider="openai", operation="refresh_token",
            ))
            return
    else:
        ui.answer_cb(cb_id, "拉取中...")

    usage_result = _fetch_and_save_usage_sync(ak, email=email)
    if isinstance(usage_result, Exception):
        ui.send(chat_id, _oauth_error_html(
            usage_result, provider=provider, operation="fetch_usage",
        ))
        return
    quota_action = _evaluate_quota_action(ak, usage_result)

    text, kb = _detail_text_and_kb(ak, page=page, filter_key=filter_key)
    if not text:
        return
    if provider == "openai":
        head = "✅ 已刷新 Token 并更新用量（wham/usage）"
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


# ─── 刷新全部用量 ─────────────────────────────────────────────────
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
    ui.answer_cb(cb_id, "开始刷新...")
    accounts = oauth_manager.list_accounts()
    if not accounts:
        ui.send(chat_id, "❌ 当前无 OAuth 账户可刷新")
        return

    lines: list[str] = ["🔄 <b>批量刷新 OAuth 用量</b>", ""]
    success_count = 0
    fail_count = 0
    resp = ui.send(chat_id, "\n".join(lines + ["⌛ 初始化..."]))
    if not resp or not resp.get("ok"):
        ui.send(chat_id, "❌ 无法创建进度消息")
        return
    progress_mid = (resp.get("result") or {}).get("message_id")
    if progress_mid is None:
        # 测试 / 无真实 TG 响应时走纯 send 摘要模式（不 edit、不自删）
        progress_mid = -1

    def _flush() -> None:
        if progress_mid == -1:
            return  # 无真实消息 id，不做中间态刷新（避免测试里刷屏）
        try:
            ui.edit(chat_id, progress_mid, "\n".join(lines))
        except Exception:
            pass

    def _labels_for(usage: dict) -> str:
        utils = oauth_manager.extract_utils_percent(usage)
        tags = ["5h", "7d", "sonnet", "opus"]
        parts = [f"{t} {u:.0f}%" for t, u in zip(tags, utils) if u is not None]
        return " / ".join(parts) if parts else "无数据"

    for idx, acc in enumerate(accounts, 1):
        email = acc.get("email")
        if not email:
            continue
        ak = _account_key(acc)
        prov = oauth_manager.provider_of(acc)
        prov_tag = "🅾 OpenAI" if prov == "openai" else "🅰 Claude"
        ek = ui.escape_html(email)

        lines.append(f"<b>{idx}. {ek}</b> · {prov_tag}")
        lines.append(f"  ⌛ 正在刷新用量...")
        _flush()

        usage = None
        # ─ 拉 usage ─
        if prov == "openai":
            tr = _run_sync(oauth_manager.force_refresh(ak))
            if isinstance(tr, Exception):
                fail_count += 1
                _replace_last_with_oauth_error(
                    lines, tr, provider="openai", operation="refresh_token",
                )
                lines.append("")
                _flush()
                continue

        result = _fetch_and_save_usage_sync(ak, email=email)
        if isinstance(result, Exception):
            fail_count += 1
            _replace_last_with_oauth_error(
                lines, result, provider=prov, operation="fetch_usage",
            )
            lines.append("")
            _flush()
            continue
        usage = result

        # ─ 写入进度 + 评估禁用/恢复 ─
        success_count += 1
        usage_str = _labels_for(usage)
        lines[-1] = f"  ✅ 刷新成功: {usage_str}"

        try:
            res = oauth_manager.evaluate_and_toggle_by_usage(ak, usage)
        except Exception as exc:
            lines.append(f"  ⚠ 状态评估异常: <code>{ui.escape_html(str(exc))[:120]}</code>")
            lines.append("")
            _flush()
            continue

        action = res.get("action")
        if action == "disabled":
            hit = " / ".join(res.get("hit_windows") or []) or "?"
            lines.append(f"  🔒 触发自动禁用（超限窗口: <code>{hit}</code>）")
        elif action == "still_over_quota":
            hit = " / ".join(res.get("hit_windows") or []) or "?"
            lines.append(f"  ⚠ 仍未恢复，维持禁用（超限: <code>{hit}</code>）")
        elif action == "resumed":
            lines.append("  ♻ 额度已恢复，已自动解除禁用")
        elif action == "noop_user":
            lines.append("  🚫 手动禁用中（不自动恢复）")
        elif action == "noop_auth_error":
            lines.append("  ⚠ auth_error（不自动恢复，需重新登录）")
        elif action == "disable_failed":
            lines.append("  ❌ 自动禁用写入失败，见 systemd 日志")
        elif action == "resume_failed":
            lines.append("  ❌ 自动解禁写入失败，见 systemd 日志")
        # "kept_enabled" / "noop_missing" 不追加额外行

        lines.append("")
        _flush()

    lines.append(
        f"📢 用量刷新完成：成功 {success_count} 个，失败 {fail_count} 个。"
    )
    lines.append("本消息 5 分钟后自动销毁。")
    _flush()

    # ─ 刷新原始 OAuth 列表面板，让用户无需离开再进来 ─
    try:
        list_text, list_kb = _list_text_and_kb(page=page, filter_key=filter_key)
        if list_text:
            ui.edit(chat_id, message_id, list_text, reply_markup=list_kb)
    except Exception:
        pass

    if progress_mid != -1:
        import threading as _t
        def _delete_later():
            try:
                ui.delete_message(chat_id, progress_mid)
            except Exception:
                pass
        _t.Timer(300.0, _delete_later).start()
    else:
        # 降级路径：无法 edit 时用一条摘要消息兜底（保留老测试可见性）
        ui.send(chat_id, "\n".join(lines))


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
    _cl_plan_line = f"\n🅰 Claude · {ui.escape_html(_cl_plan)}" if _cl_plan else ""
    _cl_sub_line = ""
    if claude_plan_info.get("subscription_status"):
        _cl_sub_line = f"\n订阅: {ui.escape_html(claude_plan_info.get('subscription_status', ''))}"
        if claude_plan_info.get("billing_type"):
            _cl_sub_line += f" · {ui.escape_html(claude_plan_info['billing_type'])}"
    ui.send_result(
        chat_id,
        "✅ <b>OAuth 账户已添加</b>\n\n"
        f"Email: <code>{ui.escape_html(email)}</code>{_cl_plan_line}{_cl_sub_line}\n"
        f"过期: <code>{_format_bjt(new_expired)}</code>{lb_hint}",
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
            for label, util in zip(("5h", "7d"), oauth_manager.extract_utils_percent(usage_result)[:2]):
                if util is not None:
                    parts.append(f"{label} {util:.0f}%")
            quota_note = "\n额度: <code>" + ui.escape_html(" / ".join(parts) or "已获取") + "</code>"

    plan = meta.get("plan_type") or "?"
    plan_tag = f" · plan: <code>{ui.escape_html(plan)}</code>"
    if meta.get("subscription_expires_at"):
        plan_tag += f" · sub: <code>{_format_bjt(meta.get('subscription_expires_at'))}</code>"
    workspace_line = ""
    if meta.get("workspace_name") or meta.get("workspace_type") or meta.get("workspace_id"):
        label = meta.get("workspace_name") or meta.get("workspace_type") or "workspace"
        workspace_line = f"工作区: <code>{ui.escape_html(label)}</code>\n"
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
        f"Email: <code>{ui.escape_html(meta.get('email') or entry.get('email') or '')}</code>{plan_tag}\n"
        f"{workspace_line}"
        f"过期: <code>{_format_bjt(meta.get('expired'))}</code>{quota_note}\n"
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
