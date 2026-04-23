"""OAuth 多账户管理菜单。

callback_data 前缀：`oa:...`

状态机 action（Claude）：
  - `oa_login_code`：等待用户粘贴 PKCE 登录页返回的 code#state
  - `oa_set_json` ：等待用户粘贴 OAuth JSON（access_token/refresh_token/...）
状态机 action（OpenAI）：
  - `oa_openai_code`：等待用户粘贴 Codex CLI 登录后的回调 URL
  - `oa_openai_rt`  ：等待用户粘贴 refresh_token 字符串

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

from ... import affinity, config, cooldown, log_db, oauth_manager, state_db
from ...oauth_ids import account_key as _account_key, split_account_key as _split_ak
from ...oauth import openai as openai_provider
from .. import states, ui
from . import main as main_menu


_BJT = timezone(timedelta(hours=8))




def _resolve_to_account_key(resolved):
    """short code 解析后可能是 account_key 或纯 email（历史/测试遗留）。
    纯 email 时回查 config 自动补 provider。"""
    if resolved is None:
        return None
    if ":" in resolved:
        return resolved
    for acc in oauth_manager.list_accounts():
        if acc.get("email") == resolved:
            return _account_key(acc)
    return resolved

# ─── 辅助：异步调用在线程里运行 ───────────────────────────────────

def _run_sync(coro):
    """在 TG 线程里阻塞跑一个 async 函数。"""
    try:
        return asyncio.run(coro)
    except Exception as exc:
        return exc


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
          💎 月度统计: ↑ 104.71M ↓ 1.78M · 缓存率 95.71%
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

    # provider 图标：claude 不显示（默认），openai 加 🅾 + plan
    prov = oauth_manager.provider_of(acc)
    provider_tag = ""
    if prov == "openai":
        plan = acc.get("plan_type") or ""
        provider_tag = f" 🅾 OpenAI{' · ' + ui.escape_html(plan) if plan else ''}"

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
        # OpenAI / Claude 都走同一条路径：点账户详情的"刷新用量"按钮。
        # 对 openai 来说，按钮会发一条最小 codex 探测请求拉响应头。
        lines.append("  📊 用量: <i>尚未获取</i>（请点账户详情手动刷新一次）")

    # 月度统计（本月 log_db 聚合）
    try:
        since_ts = _this_month_start_ts()
        ts = log_db.tokens_for_channel(f"oauth:{ak}", since_ts=since_ts)
    except Exception:
        ts = None
    if ts and ts["total"] > 0:
        prompt = ts["input"] + ts["cache_creation"] + ts["cache_read"]
        cache_rate = (ts["cache_read"] / prompt * 100) if prompt > 0 else 0
        lines.append(
            f"  💎 月度统计: ↑ {ui.fmt_tokens(prompt)} ↓ {ui.fmt_tokens(ts['output'])}"
            f" · 缓存率 {cache_rate:.2f}%"
        )
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


def _build_pagination_row(current: int, total_pages: int) -> list[dict]:
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
            btns.append(ui.btn("⬅ 上一页", f"oa:page:{current - 1}"))
        else:
            btns.append(ui.btn("◁ 上一页", "oa:page:noop"))
        btns.append(ui.btn(f"{current}/{total_pages}", "oa:page:noop"))
        if current < total_pages:
            btns.append(ui.btn("➡ 下一页", f"oa:page:{current + 1}"))
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
        page_btns.append(ui.btn("1", "oa:page:1"))
        if lo > 2:
            page_btns.append(ui.btn("…", "oa:page:noop"))
    for p in range(lo, hi + 1):
        if p == current:
            page_btns.append(ui.btn(f"[{p}]", "oa:page:noop"))
        else:
            page_btns.append(ui.btn(str(p), f"oa:page:{p}"))
    if hi < total_pages:
        if hi < total_pages - 1:
            page_btns.append(ui.btn("…", "oa:page:noop"))
        page_btns.append(ui.btn(str(total_pages), f"oa:page:{total_pages}"))
    return page_btns


def _list_text_and_kb(page: int = 1) -> tuple[str, dict]:
    accounts = oauth_manager.list_accounts()
    # 按访问节流刷新所有账户的 usage（quotaMonitor.enabled=True 时内部跳过）
    account_keys = [
        _account_key(a) for a in accounts
        if a.get("email") and not a.get("disabled_reason")
    ]
    if account_keys:
        oauth_manager.ensure_quota_fresh_sync(account_keys)
    total = len(accounts)
    normal = sum(1 for a in accounts if a.get("enabled", True) and not a.get("disabled_reason"))
    quota_disabled = sum(1 for a in accounts if a.get("disabled_reason") == "quota")
    user_disabled = sum(1 for a in accounts if a.get("disabled_reason") == "user")
    auth_err = sum(1 for a in accounts if a.get("disabled_reason") == "auth_error")

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
        f"共 {total} 个账户 | 正常 {normal}"
        + (f" | 配额 {quota_disabled}" if quota_disabled else "")
        + (f" | 用户禁用 {user_disabled}" if user_disabled else "")
        + (f" | 认证失败 {auth_err}" if auth_err else "")
        + (f" | ⚠ 冷却 {cooling_only}" if cooling_only else "")
        + (f" | 🔴 永久 {permanent}" if permanent else "")
        + page_info
    )

    if not accounts:
        text = summary + "\n\n暂无账户，点击下方「➕ 新增账户」添加。"
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
        for acc in page_accs[idx:idx + 2]:
            email = acc.get("email", "?")
            ak = _account_key(acc)
            short = ui.register_code(ak)
            prov = oauth_manager.provider_of(acc)
            tag = "🅾" if prov == "openai" else ("🅰" if prov == "claude" else "✉")
            row_btns.append(ui.btn(f"{tag} {email}", f"oa:view:{short}"))
        rows.append(row_btns)

    # 翻页
    pag_row = _build_pagination_row(page, total_pages)
    if pag_row:
        rows.append(pag_row)

    # 操作按钮（每页都有）
    rows.append([
        ui.btn("➕ 新增账户", "oa:add"),
        ui.btn("🔄 刷新全部用量", "oa:refresh_all"),
    ])
    # 只有存在 OAuth 账号的冷却条目时才显示"清除所有错误"（避免空操作按钮）
    if cd_keys_any:
        rows.append([ui.btn(f"🧹 清除所有账户错误（{len(cd_keys_any)} 个）", "oa:clear_all_errors")])
    rows.append([ui.btn("◀ 返回主菜单", "menu:main")])
    return ui.truncate(text), ui.inline_kb(rows)


def show(chat_id: int, message_id: int, cb_id: Optional[str] = None, page: int = 1) -> None:
    if cb_id is not None:
        ui.answer_cb(cb_id)
    text, kb = _list_text_and_kb(page=page)
    ui.edit(chat_id, message_id, text, reply_markup=kb)


def send_new(chat_id: int, page: int = 1) -> None:
    text, kb = _list_text_and_kb(page=page)
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
    inp_prompt = overall["input"] + overall["cache_creation"] + overall["cache_read"]
    out_tok = overall["output"]
    cache_rate = (overall["cache_read"] / inp_prompt * 100) if inp_prompt > 0 else 0

    lines = [
        "",
        "<b>⚡ 本月使用统计</b>",
        f"总体: {total} 次 · ✅ {succ} · ❌ {err}",
        f"↑ {ui.fmt_tokens(inp_prompt)} · ↓ {ui.fmt_tokens(out_tok)} · 缓存率 {cache_rate:.2f}%",
        f"平均 {ui.fmt_tps(overall.get('avg_tps'))} · "
        f"峰值 {ui.fmt_tps(overall.get('max_tps'))} · "
        f"最低 {ui.fmt_tps(overall.get('min_tps'))}",
    ]
    if by_model:
        lines.append("")
        lines.append("按模型:")
        for ms in by_model:
            model = ui.escape_html(ms.get("final_model") or "?")
            m_prompt = ms["input"] + ms["cache_creation"] + ms["cache_read"]
            lines.append(f"  • <code>{model}</code>")
            lines.append(
                f"    {ms['total']} 次 · ✅ {ms['success_count']} · ❌ {ms['error_count']}"
                f" · ↑ {ui.fmt_tokens(m_prompt)} ↓ {ui.fmt_tokens(ms['output'])}"
            )
            if ms.get("avg_tps") is not None:
                lines.append(
                    f"    ⚡ 平均 {ui.fmt_tps(ms.get('avg_tps'))} · "
                    f"峰值 {ui.fmt_tps(ms.get('max_tps'))} · "
                    f"最低 {ui.fmt_tps(ms.get('min_tps'))}"
                )
    return "\n".join(lines)


def _detail_text_and_kb(account_key: str) -> tuple[Optional[str], Optional[dict]]:
    acc = oauth_manager.get_account(account_key)
    if acc is None:
        return None, None
    email = acc.get("email", "?")

    if not acc.get("disabled_reason"):
        oauth_manager.ensure_quota_fresh_sync(account_key)

    icon = _status_icon(acc)
    reason = acc.get("disabled_reason") or "—"
    prov = oauth_manager.provider_of(acc)
    provider_line = ""
    if prov == "openai":
        plan = acc.get("plan_type") or "?"
        provider_line = (
            f"提供者: <code>🅾 OpenAI (Codex)</code> · 计划: <code>{ui.escape_html(plan)}</code>\n"
        )
    elif prov == "claude":
        provider_line = f"提供者: <code>🅰 Anthropic (Claude)</code>\n"
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

    rows = [
        [ui.btn("🔄 刷新 Token", f"oa:refresh_token:{short}"),
         ui.btn("📊 刷新用量",   f"oa:refresh_usage:{short}")],
        [ui.btn("🧹 清模型错误", f"oa:clear_errors:{short}"),
         ui.btn("🔗 清亲和绑定", f"oa:clear_affinity:{short}")],
        [ui.btn(f"⚡ 修改并发上限（当前: {max_cc_label}）", f"oa:emax:{short}")],
        [ui.btn(toggle_label,     f"oa:toggle:{short}"),
         ui.btn("🗑 删除",         f"oa:delete_ask:{short}")],
        [ui.btn("◀ 返回 OAuth 列表", "menu:oauth")],
    ]
    return ui.truncate(text), ui.inline_kb(rows)


def on_view(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    ak = _resolve_to_account_key(ui.resolve_code(short))
    if ak is None:
        ui.answer_cb(cb_id, "短码已失效，请返回重试")
        show(chat_id, message_id)
        return
    ui.answer_cb(cb_id)
    text, kb = _detail_text_and_kb(ak)
    if text is None:
        _, email = _split_ak(ak)
        ui.edit(chat_id, message_id,
                f"⚠ 账户 <code>{ui.escape_html(email)}</code> 已不存在",
                reply_markup=ui.inline_kb([[ui.btn("◀ 返回列表", "menu:oauth")]]))
        return
    ui.edit(chat_id, message_id, text, reply_markup=kb)


# ─── 刷新 Token ──────────────────────────────────────────────────

def on_refresh_token(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    ak = _resolve_to_account_key(ui.resolve_code(short))
    if ak is None:
        ui.answer_cb(cb_id, "短码已失效")
        return
    ui.answer_cb(cb_id, "刷新中...")

    result = _run_sync(oauth_manager.force_refresh(ak))
    if isinstance(result, Exception):
        ui.send(chat_id, f"❌ 刷新失败: <code>{ui.escape_html(str(result))}</code>")
        return

    _, email = _split_ak(ak)
    if oauth_manager.provider_of(ak) != "openai":
        usage_result = _run_sync(oauth_manager.fetch_usage(ak))
        if not isinstance(usage_result, Exception):
            state_db.quota_save(ak, oauth_manager.flatten_usage(usage_result), email=email)

    text, kb = _detail_text_and_kb(ak)
    if text:
        ui.edit(chat_id, message_id,
                "✅ Token 已刷新\n\n" + text,
                reply_markup=kb)


# ─── 刷新用量 ─────────────────────────────────────────────────────

def on_refresh_usage(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    ak = _resolve_to_account_key(ui.resolve_code(short))
    if ak is None:
        ui.answer_cb(cb_id, "短码已失效")
        return
    _, email = _split_ak(ak)
    if oauth_manager.provider_of(ak) == "openai":
        from ...channel import registry
        from ...channel.openai_oauth_channel import OpenAIOAuthChannel

        ui.answer_cb(cb_id, "刷新 Token 并发送探测请求...")
        tr = _run_sync(oauth_manager.force_refresh(ak))
        if isinstance(tr, Exception):
            ui.send(chat_id,
                    f"❌ 刷新 Token 失败: <code>{ui.escape_html(str(tr))}</code>")
            return
        ch = registry.get_channel(f"oauth:{ak}")
        if not isinstance(ch, OpenAIOAuthChannel):
            ui.send(chat_id, "❌ 账户未注册为 OpenAI OAuth 渠道")
            return
        pr = _run_sync(ch.probe_usage())
        if isinstance(pr, Exception):
            ui.send(chat_id, f"❌ 探测失败: <code>{ui.escape_html(str(pr))}</code>")
            return
        text, kb = _detail_text_and_kb(ak)
        if pr.get("ok"):
            head = "✅ 已刷新 Token 并更新用量（探测请求成功）"
        else:
            reason = pr.get("reason", "?")
            head = ("⚠ Token 已刷新，但用量探测未成功: "
                    f"<code>{ui.escape_html(str(reason))[:200]}</code>")
        if text:
            ui.edit(chat_id, message_id, head + "\n\n" + text, reply_markup=kb)
        return
    ui.answer_cb(cb_id, "拉取中...")

    usage_result = _run_sync(oauth_manager.fetch_usage(ak))
    if isinstance(usage_result, Exception):
        ui.send(chat_id, f"❌ 获取用量失败: <code>{ui.escape_html(str(usage_result))}</code>")
        return
    state_db.quota_save(ak, oauth_manager.flatten_usage(usage_result), email=email)

    text, kb = _detail_text_and_kb(ak)
    if text:
        ui.edit(chat_id, message_id, text, reply_markup=kb)


# ─── 清错误 / 清亲和 ─────────────────────────────────────────────

def on_clear_errors(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    ak = _resolve_to_account_key(ui.resolve_code(short))
    if ak is None:
        ui.answer_cb(cb_id, "短码已失效")
        return
    cooldown.clear(f"oauth:{ak}", model=None)
    ui.answer_cb(cb_id, "已清除该账号的所有模型冷却")
    text, kb = _detail_text_and_kb(ak)
    if text:
        ui.edit(chat_id, message_id, text, reply_markup=kb)


def on_clear_affinity(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    ak = _resolve_to_account_key(ui.resolve_code(short))
    if ak is None:
        ui.answer_cb(cb_id, "短码已失效")
        return
    affinity.delete_by_channel(f"oauth:{ak}")
    ui.answer_cb(cb_id, "已清亲和")
    text, kb = _detail_text_and_kb(ak)
    if text:
        ui.edit(chat_id, message_id, text, reply_markup=kb)


# ─── 启用 / 禁用 ──────────────────────────────────────────────────

def on_toggle(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    ak = _resolve_to_account_key(ui.resolve_code(short))
    if ak is None:
        ui.answer_cb(cb_id, "短码已失效")
        return
    acc = oauth_manager.get_account(ak)
    if acc is None:
        ui.answer_cb(cb_id, "账户不存在")
        show(chat_id, message_id)
        return

    enabled = acc.get("enabled", True) and not acc.get("disabled_reason")
    if enabled:
        oauth_manager.set_enabled(ak, False, reason="user")
        ui.answer_cb(cb_id, "已禁用")
    else:
        oauth_manager.set_enabled(ak, True)
        ui.answer_cb(cb_id, "已启用")

    text, kb = _detail_text_and_kb(ak)
    if text:
        ui.edit(chat_id, message_id, text, reply_markup=kb)


# ─── 删除（二次确认） ─────────────────────────────────────────────

def on_delete_ask(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    ak = _resolve_to_account_key(ui.resolve_code(short))
    if ak is None:
        ui.answer_cb(cb_id, "短码已失效")
        return
    _, email = _split_ak(ak)
    prov = oauth_manager.provider_of(ak)
    prov_tag = "🅾 OpenAI" if prov == "openai" else "🅰 Claude"
    ui.answer_cb(cb_id)
    ui.edit(
        chat_id, message_id,
        f"确认删除账户 <code>{ui.escape_html(email)}</code>（{prov_tag}）？\n"
        f"⚠ 该操作将清除此账户的所有统计与亲和绑定数据。",
        reply_markup=ui.inline_kb([[
            ui.btn("✅ 确认删除", f"oa:delete_exec:{short}"),
            ui.btn("❌ 取消",     f"oa:view:{short}"),
        ]]),
    )


def on_delete_exec(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    ak = _resolve_to_account_key(ui.resolve_code(short))
    if ak is None:
        ui.answer_cb(cb_id, "短码已失效")
        show(chat_id, message_id)
        return
    _, email = _split_ak(ak)
    try:
        oauth_manager.delete_account(ak)
    except Exception as exc:
        ui.answer_cb(cb_id, "删除失败")
        ui.send(chat_id, f"❌ 删除失败: <code>{ui.escape_html(str(exc))}</code>")
        return
    ui.answer_cb(cb_id, "已删除")
    ui.edit(chat_id, message_id, f"✅ 已删除 <code>{ui.escape_html(email)}</code>")
    show(chat_id, message_id)


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

def on_refresh_all(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id, "开始刷新...")
    from ...channel import registry
    from ...channel.openai_oauth_channel import OpenAIOAuthChannel

    accounts = oauth_manager.list_accounts()
    if not accounts:
        ui.send(chat_id, "❌ 当前无 OAuth 账户可刷新")
        return

    lines: list[str] = ["🔄 <b>批量刷新 OAuth 用量</b>", ""]
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
                lines[-1] = f"  ❌ Token 刷新失败: <code>{ui.escape_html(str(tr))[:120]}</code>"
                lines.append("")
                _flush()
                continue
            ch = registry.get_channel(f"oauth:{ak}")
            if not isinstance(ch, OpenAIOAuthChannel):
                lines[-1] = "  ❌ 渠道未注册（需重启或重新加载）"
                lines.append("")
                _flush()
                continue
            pr = _run_sync(ch.probe_usage())
            if isinstance(pr, Exception):
                lines[-1] = f"  ❌ 探测异常: <code>{ui.escape_html(str(pr))[:120]}</code>"
                lines.append("")
                _flush()
                continue
            if not pr.get("ok"):
                reason = pr.get("reason", "?")
                lines[-1] = f"  ❌ 探测失败: <code>{ui.escape_html(str(reason))[:120]}</code>"
                lines.append("")
                _flush()
                continue
            row = state_db.quota_load(ak) or {}
            usage = oauth_manager._synthesize_openai_usage_from_row(row)
        else:
            result = _run_sync(oauth_manager.fetch_usage(ak))
            if isinstance(result, Exception):
                lines[-1] = f"  ❌ 获取失败: <code>{ui.escape_html(str(result))[:120]}</code>"
                lines.append("")
                _flush()
                continue
            usage = result
            try:
                state_db.quota_save(ak, oauth_manager.flatten_usage(usage), email=email)
            except Exception as exc:
                print(f"[oauth_menu] quota_save failed for {ak}: {exc}")

        # ─ 写入进度 + 评估禁用/恢复 ─
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

    lines.append("📢 用量刷新完成，本消息 5 分钟后自动销毁。")
    _flush()

    # ─ 刷新原始 OAuth 列表面板，让用户无需离开再进来 ─
    try:
        list_text, list_kb = _list_text_and_kb()
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
    """第一步：选 provider。"""
    ui.answer_cb(cb_id)
    ui.edit(
        chat_id, message_id,
        "<b>新增 OAuth 账户</b>\n请选择类型：",
        reply_markup=ui.inline_kb([
            [ui.btn("🅰 Claude (Anthropic)",   "oa:add:claude")],
            [ui.btn("🅾 OpenAI (Codex / ChatGPT)", "oa:add:openai")],
            [ui.btn("◀ 返回列表", "menu:oauth")],
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
    )


def on_login_code_input(chat_id: int, text: str) -> None:
    state = states.pop_state(chat_id)
    if not state or state.get("action") != "oa_login_code":
        ui.send_result(chat_id, "❌ 登录会话已失效，请重新发起登录流程。",
                       back_label="◀ 返回 OAuth 列表", back_callback="menu:oauth")
        return
    data = state.get("data") or {}

    raw = (text or "").strip()
    if not raw:
        ui.send_result(chat_id, "❌ 内容为空。请重新发起登录流程。",
                       back_label="◀ 返回 OAuth 列表", back_callback="menu:oauth")
        return

    # 页面通常返回 code#state 形式
    code_part = raw.split("#", 1)[0].strip()
    if not code_part:
        ui.send_result(chat_id, "❌ code 无效，请重新发起登录流程。",
                       back_label="◀ 返回 OAuth 列表", back_callback="menu:oauth")
        return

    try:
        tok_resp = oauth_manager.exchange_code(
            code_part, data.get("code_verifier", ""), data.get("state", ""),
        )
    except Exception as exc:
        ui.send_result(chat_id,
                       f"❌ Token 换取失败: <code>{ui.escape_html(str(exc))}</code>",
                       back_label="◀ 返回 OAuth 列表", back_callback="menu:oauth")
        return

    # 获取 email（可选）
    email = ""
    try:
        profile = _run_sync(oauth_manager.fetch_profile(tok_resp.get("access_token", "")))
        if isinstance(profile, dict):
            email = (profile.get("account") or {}).get("email", "") or ""
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
    }
    try:
        oauth_manager.add_account(entry)
    except Exception as exc:
        ui.send_result(chat_id,
                       f"❌ 保存失败: <code>{ui.escape_html(str(exc))}</code>",
                       back_label="◀ 返回 OAuth 列表", back_callback="menu:oauth")
        return

    ui.send_result(
        chat_id,
        "✅ <b>OAuth 账户已添加</b>\n\n"
        f"Email: <code>{ui.escape_html(email)}</code>\n"
        f"过期: <code>{_format_bjt(new_expired)}</code>",
        back_label="◀ 返回 OAuth 列表", back_callback="menu:oauth",
    )


# ─── 手动设置 JSON ────────────────────────────────────────────────

def on_set_json_start(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    states.set_state(chat_id, "oa_set_json")
    ui.edit(
        chat_id, message_id,
        "请粘贴 OAuth JSON（需包含 <code>email / access_token / refresh_token / expired</code>）：",
    )


def on_set_json_input(chat_id: int, text: str) -> None:
    states.pop_state(chat_id)
    nav = {"back_label": "◀ 返回 OAuth 列表", "back_callback": "menu:oauth"}
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

    ui.send_result(chat_id, f"✅ 已添加 <code>{ui.escape_html(data['email'])}</code>", **nav)


# ─── OpenAI PKCE 登录 ──────────────────────────────────────────────
#
# 与 Claude 的 on_login_start 区别：
#   1. code_verifier 是 hex(64 随机字节)，非 base64url（OpenAI 特殊要求）
#   2. 登录 URL 必须带 id_token_add_organizations / codex_cli_simplified_flow
#   3. 回调 URL 是 http://localhost:1455/auth/callback?code=...&state=...；
#      这个端口我们不会监听，浏览器会显示"无法访问此网站"，用户把地址栏
#      的 URL 整段复制回来即可。我们正则抽 code 和 state。
#   4. 拿到 token 后解 id_token 得到 email / chatgpt_account_id / plan_type。


_OA_NAV_OPENAI = {"back_label": "◀ 返回 OAuth 列表", "back_callback": "menu:oauth"}


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
        [ui.btn("❌ 取消", "menu:oauth")],
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
            f"❌ Token 换取失败: <code>{ui.escape_html(str(exc))[:300]}</code>",
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
            f"❌ 刷新失败，refresh_token 可能无效: "
            f"<code>{ui.escape_html(str(exc))[:300]}</code>",
            **_OA_NAV_OPENAI,
        )
        return
    # refresh 响应里可能不带新的 refresh_token，回填用户输入的原 RT
    if not tok.get("refresh_token"):
        tok["refresh_token"] = rt_clean

    _finish_openai_add(chat_id, tok, source="rt")


def _finish_openai_add(chat_id: int, tok: dict, *, source: str) -> None:
    """共用保存路径：从 id_token 解 email 等 → add_account → 回报。"""
    id_token = tok.get("id_token", "") or ""
    if not id_token:
        ui.send_result(
            chat_id,
            "❌ token 响应缺少 <code>id_token</code>，无法识别账户。"
            "请检查授权是否带了 <code>openid</code> scope（默认即带）。",
            **_OA_NAV_OPENAI,
        )
        return
    try:
        claims = openai_provider.decode_id_token(id_token)
    except Exception as exc:
        ui.send_result(
            chat_id,
            f"❌ id_token 解码失败: <code>{ui.escape_html(str(exc))[:300]}</code>",
            **_OA_NAV_OPENAI,
        )
        return

    info = openai_provider.extract_user_info(claims)
    email = info.get("email") or ""
    if not email:
        # 兜底唯一名，避免 email 冲突（与 Claude 路径一致）
        email = f"unnamed-openai-{int(datetime.now().timestamp())}@local"

    expires_in = int(tok.get("expires_in", 28800))
    new_expired = (
        datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

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
        # OpenAI 专属 metadata
        "id_token": id_token,
        "chatgpt_account_id": info.get("chatgpt_account_id", ""),
        "organization_id": info.get("organization_id", ""),
        "plan_type": info.get("plan_type", ""),
    }
    try:
        oauth_manager.add_account(entry)
    except Exception as exc:
        ui.send_result(
            chat_id,
            f"❌ 保存失败: <code>{ui.escape_html(str(exc))}</code>",
            **_OA_NAV_OPENAI,
        )
        return

    plan_tag = f" · plan: <code>{ui.escape_html(info.get('plan_type') or '?')}</code>"
    ui.send_result(
        chat_id,
        "✅ <b>OpenAI OAuth 账户已添加</b>\n\n"
        f"Email: <code>{ui.escape_html(email)}</code>{plan_tag}\n"
        f"过期: <code>{_format_bjt(new_expired)}</code>\n"
        f"来源: <code>{source}</code>",
        **_OA_NAV_OPENAI,
    )


# ─── 路由分发 ─────────────────────────────────────────────────────

def on_clear_all_errors(chat_id: int, message_id: int, cb_id: str) -> None:
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
    show(chat_id, message_id)


def handle_callback(chat_id: int, message_id: int, cb_id: str, data: str) -> bool:
    if data == "menu:oauth":
        show(chat_id, message_id, cb_id)
        return True
    if data == "oa:refresh_all":
        on_refresh_all(chat_id, message_id, cb_id)
        return True
    if data.startswith("oa:page:"):
        page_str = data.split(":", 2)[2]
        if page_str == "noop":
            ui.answer_cb(cb_id, "当前页")
            return True
        try:
            page = int(page_str)
        except ValueError:
            page = 1
        show(chat_id, message_id, cb_id, page=page)
        return True
    if data == "oa:clear_all_errors":
        on_clear_all_errors(chat_id, message_id, cb_id)
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

    if data.startswith("oa:view:"):
        on_view(chat_id, message_id, cb_id, data.split(":", 2)[2])
        return True
    if data.startswith("oa:refresh_token:"):
        on_refresh_token(chat_id, message_id, cb_id, data.split(":", 2)[2])
        return True
    if data.startswith("oa:refresh_usage:"):
        on_refresh_usage(chat_id, message_id, cb_id, data.split(":", 2)[2])
        return True
    if data.startswith("oa:clear_errors:"):
        on_clear_errors(chat_id, message_id, cb_id, data.split(":", 2)[2])
        return True
    if data.startswith("oa:clear_affinity:"):
        on_clear_affinity(chat_id, message_id, cb_id, data.split(":", 2)[2])
        return True
    if data.startswith("oa:toggle:"):
        on_toggle(chat_id, message_id, cb_id, data.split(":", 2)[2])
        return True
    if data.startswith("oa:delete_ask:"):
        on_delete_ask(chat_id, message_id, cb_id, data.split(":", 2)[2])
        return True
    if data.startswith("oa:delete_exec:"):
        on_delete_exec(chat_id, message_id, cb_id, data.split(":", 2)[2])
        return True
    if data.startswith("oa:emax:"):
        on_edit_max_concurrent(chat_id, message_id, cb_id, data.split(":", 2)[2])
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
    if action == "oa_emax":
        on_edit_max_concurrent_input(chat_id, text)
        return True
    return False


# ─── 并发上限编辑 ─────────────────────────────────────────────────

def on_edit_max_concurrent(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    ak = _resolve_to_account_key(ui.resolve_code(short))
    if ak is None:
        ui.answer_cb(cb_id, "短码已失效")
        return
    ui.answer_cb(cb_id)
    states.set_state(chat_id, "oa_emax", {"account_key": ak, "short": short})
    ui.edit(
        chat_id, message_id,
        "请输入该 OAuth 账户的并发上限（整数 ≥0）：\n"
        "• <code>0</code> = 使用全局默认（「⚙ 系统设置 → ⚡ 并发限制」里配的 defaultMaxConcurrent）\n"
        "• 正整数 = 该账户同时允许最多 N 个在途请求，超出则排队\n\n"
        "例：<code>3</code>",
        reply_markup=ui.inline_kb([[ui.btn("❌ 取消", f"oa:view:{short}")]]),
    )


def on_edit_max_concurrent_input(chat_id: int, text: str) -> None:
    state = states.get_state(chat_id)
    data = (state.get("data") or {}) if state else {}
    ak = data.get("account_key")
    short = data.get("short", "")
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
        extra_rows=[[ui.btn("◀ 返回账户详情", f"oa:view:{short}")]],
        back_label="🏠 主菜单", back_callback="menu:main",
    )
