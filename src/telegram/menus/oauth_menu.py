"""OAuth 多账户管理菜单。

callback_data 前缀：`oa:...`

状态机 action：
  - `oa_login_code`：等待用户粘贴 PKCE 登录页返回的 code#state
  - `oa_set_json`：等待用户粘贴 OAuth JSON（access_token/refresh_token/...）

注意：本模块所有与 api.anthropic.com 的交互都通过 `oauth_manager` 中已 mockMode 保护
的入口。开发期请开 `config.oauth.mockMode=true` 避免触发风控。
"""

from __future__ import annotations

import asyncio
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from ... import affinity, config, cooldown, log_db, oauth_manager, state_db
from .. import states, ui
from . import main as main_menu


_BJT = timezone(timedelta(hours=8))


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

    lines = [f"{icon} <code>{ui.escape_html(email)}</code>{tag}"]

    # Token 过期时间（绝对 + 倒计时）
    expired = acc.get("expired")
    if expired:
        lines.append(
            f"  ⏳ Token 过期: <code>{_format_bjt(expired)}</code>"
            f" ({_format_remaining(expired)})"
        )

    # 用量（5h / 7d，列成两行，各带绝对重置时间）
    row = state_db.quota_load(email)
    if row:
        fh_util = row.get("five_hour_util")
        sd_util = row.get("seven_day_util")
        if fh_util is not None:
            reset = row.get("five_hour_reset")
            reset_str = _format_bjt(reset) if reset else "?"
            lines.append(
                f"  📊 5h 用量: <b>{fh_util:>4.0f}%</b> | 重置: <code>{reset_str}</code>"
            )
        if sd_util is not None:
            reset = row.get("seven_day_reset")
            reset_str = _format_bjt(reset) if reset else "?"
            lines.append(
                f"  📊 7d 用量: <b>{sd_util:>4.0f}%</b> | 重置: <code>{reset_str}</code>"
            )
        if fh_util is None and sd_util is None:
            lines.append("  📊 用量: <i>尚未获取</i>")
    else:
        lines.append("  📊 用量: <i>尚未获取</i>（请点账户详情手动刷新一次）")

    # 月度统计（本月 log_db 聚合）
    try:
        since_ts = _this_month_start_ts()
        ts = log_db.tokens_for_channel(f"oauth:{email}", since_ts=since_ts)
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

    return "\n".join(lines)


def _format_usage_block(email: str) -> str:
    row = state_db.quota_load(email)
    if not row:
        return "尚未获取用量（点「刷新用量」试试）"

    def _line(label: str, util, reset) -> Optional[str]:
        if util is None:
            return None
        line = f"{label}: {util:.0f}%"
        if reset:
            line += f" (重置: {_format_bjt(reset)})"
        return line

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

def _list_text_and_kb() -> tuple[str, dict]:
    accounts = oauth_manager.list_accounts()
    # 按访问节流刷新所有账户的 usage（quotaMonitor.enabled=True 时内部跳过）
    emails = [
        a.get("email") for a in accounts
        if a.get("email") and not a.get("disabled_reason")
    ]
    if emails:
        oauth_manager.ensure_quota_fresh_sync(emails)
    total = len(accounts)
    normal = sum(1 for a in accounts if a.get("enabled", True) and not a.get("disabled_reason"))
    quota_disabled = sum(1 for a in accounts if a.get("disabled_reason") == "quota")
    user_disabled = sum(1 for a in accounts if a.get("disabled_reason") == "user")
    auth_err = sum(1 for a in accounts if a.get("disabled_reason") == "auth_error")

    summary = (
        f"🔐 <b>OAuth 账户管理</b>\n"
        f"共 {total} 个账户 | 正常 {normal}"
        + (f" | 配额 {quota_disabled}" if quota_disabled else "")
        + (f" | 用户禁用 {user_disabled}" if user_disabled else "")
        + (f" | 认证失败 {auth_err}" if auth_err else "")
    )

    if not accounts:
        text = summary + "\n\n暂无账户，点击下方「➕ 新增账户」添加。"
    else:
        lines = [summary, ""]
        for i, acc in enumerate(accounts, 1):
            # 序号 + 账号多行块；序号前缀追加到块的第一行
            block = _format_account_block(acc)
            first, _, rest = block.partition("\n")
            lines.append(f"{i}. {first}")
            if rest:
                lines.append(rest)
            lines.append("")
        text = "\n".join(lines).rstrip()

    rows: list[list[dict]] = []
    for acc in accounts:
        email = acc.get("email", "?")
        short = ui.register_code(email)
        rows.append([ui.btn(f"  {email}  ", f"oa:view:{short}")])
    rows.append([
        ui.btn("➕ 新增账户", "oa:add"),
        ui.btn("🔄 刷新全部用量", "oa:refresh_all"),
    ])
    rows.append([ui.btn("◀ 返回主菜单", "menu:main")])
    return ui.truncate(text), ui.inline_kb(rows)


def show(chat_id: int, message_id: int, cb_id: Optional[str] = None) -> None:
    if cb_id is not None:
        ui.answer_cb(cb_id)
    text, kb = _list_text_and_kb()
    ui.edit(chat_id, message_id, text, reply_markup=kb)


def send_new(chat_id: int) -> None:
    text, kb = _list_text_and_kb()
    ui.send(chat_id, text, reply_markup=kb)


# ─── 账户详情 ─────────────────────────────────────────────────────

def _format_month_stats_block(email: str) -> str:
    """本月使用统计：总体 + 按模型展开。无数据时返回空字符串。"""
    ck = f"oauth:{email}"
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


def _detail_text_and_kb(email: str) -> tuple[Optional[str], Optional[dict]]:
    acc = oauth_manager.get_account(email)
    if acc is None:
        return None, None

    # 详情页按访问节流刷新 usage（quotaMonitor.enabled=True 时内部跳过）
    if not acc.get("disabled_reason"):
        oauth_manager.ensure_quota_fresh_sync(email)

    icon = _status_icon(acc)
    reason = acc.get("disabled_reason") or "—"
    text = (
        f"{icon} <b>{ui.escape_html(email)}</b>\n\n"
        f"状态: <code>{ui.escape_html('enabled' if acc.get('enabled', True) and not acc.get('disabled_reason') else reason)}</code>\n"
        f"过期: <code>{_format_bjt(acc.get('expired'))}</code> ({_format_remaining(acc.get('expired'))})\n"
        f"上次刷新: <code>{_format_bjt(acc.get('last_refresh'))}</code>\n\n"
        f"<b>📊 使用量</b>\n{_format_usage_block(email)}"
    )
    month_block = _format_month_stats_block(email)
    if month_block:
        text += "\n" + month_block

    short = ui.register_code(email)
    enabled = acc.get("enabled", True) and not acc.get("disabled_reason")
    toggle_label = "🚫 禁用" if enabled else "✅ 启用"

    # 显示当前模型的冷却状态（让 admin 直观判断需不需要清错误）
    ck = f"oauth:{email}"
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
        [ui.btn(toggle_label,     f"oa:toggle:{short}"),
         ui.btn("🗑 删除",         f"oa:delete_ask:{short}")],
        [ui.btn("◀ 返回 OAuth 列表", "menu:oauth")],
    ]
    return ui.truncate(text), ui.inline_kb(rows)


def on_view(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    email = ui.resolve_code(short)
    if email is None:
        ui.answer_cb(cb_id, "短码已失效，请返回重试")
        show(chat_id, message_id)
        return
    ui.answer_cb(cb_id)
    text, kb = _detail_text_and_kb(email)
    if text is None:
        ui.edit(chat_id, message_id,
                f"⚠ 账户 <code>{ui.escape_html(email)}</code> 已不存在",
                reply_markup=ui.inline_kb([[ui.btn("◀ 返回列表", "menu:oauth")]]))
        return
    ui.edit(chat_id, message_id, text, reply_markup=kb)


# ─── 刷新 Token ──────────────────────────────────────────────────

def on_refresh_token(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    email = ui.resolve_code(short)
    if email is None:
        ui.answer_cb(cb_id, "短码已失效")
        return
    ui.answer_cb(cb_id, "刷新中...")

    result = _run_sync(oauth_manager.force_refresh(email))
    if isinstance(result, Exception):
        ui.send(chat_id, f"❌ 刷新失败: <code>{ui.escape_html(str(result))}</code>")
        return

    # 顺便刷新用量写入缓存
    usage_result = _run_sync(oauth_manager.fetch_usage(email))
    if not isinstance(usage_result, Exception):
        state_db.quota_save(email, oauth_manager.flatten_usage(usage_result))

    # 重渲染详情
    text, kb = _detail_text_and_kb(email)
    if text:
        ui.edit(chat_id, message_id,
                "✅ Token 已刷新\n\n" + text,
                reply_markup=kb)


# ─── 刷新用量 ─────────────────────────────────────────────────────

def on_refresh_usage(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    email = ui.resolve_code(short)
    if email is None:
        ui.answer_cb(cb_id, "短码已失效")
        return
    ui.answer_cb(cb_id, "拉取中...")

    usage_result = _run_sync(oauth_manager.fetch_usage(email))
    if isinstance(usage_result, Exception):
        ui.send(chat_id, f"❌ 获取用量失败: <code>{ui.escape_html(str(usage_result))}</code>")
        return
    state_db.quota_save(email, oauth_manager.flatten_usage(usage_result))

    text, kb = _detail_text_and_kb(email)
    if text:
        ui.edit(chat_id, message_id, text, reply_markup=kb)


# ─── 清错误 / 清亲和 ─────────────────────────────────────────────

def on_clear_errors(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    email = ui.resolve_code(short)
    if email is None:
        ui.answer_cb(cb_id, "短码已失效")
        return
    cooldown.clear(f"oauth:{email}", model=None)
    ui.answer_cb(cb_id, "已清除该账号的所有模型冷却")
    text, kb = _detail_text_and_kb(email)
    if text:
        ui.edit(chat_id, message_id, text, reply_markup=kb)


def on_clear_affinity(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    email = ui.resolve_code(short)
    if email is None:
        ui.answer_cb(cb_id, "短码已失效")
        return
    affinity.delete_by_channel(f"oauth:{email}")
    ui.answer_cb(cb_id, "已清亲和")
    text, kb = _detail_text_and_kb(email)
    if text:
        ui.edit(chat_id, message_id, text, reply_markup=kb)


# ─── 启用 / 禁用 ──────────────────────────────────────────────────

def on_toggle(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    email = ui.resolve_code(short)
    if email is None:
        ui.answer_cb(cb_id, "短码已失效")
        return
    acc = oauth_manager.get_account(email)
    if acc is None:
        ui.answer_cb(cb_id, "账户不存在")
        show(chat_id, message_id)
        return

    enabled = acc.get("enabled", True) and not acc.get("disabled_reason")
    if enabled:
        oauth_manager.set_enabled(email, False, reason="user")
        ui.answer_cb(cb_id, "已禁用")
    else:
        oauth_manager.set_enabled(email, True)
        ui.answer_cb(cb_id, "已启用")

    text, kb = _detail_text_and_kb(email)
    if text:
        ui.edit(chat_id, message_id, text, reply_markup=kb)


# ─── 删除（二次确认） ─────────────────────────────────────────────

def on_delete_ask(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    email = ui.resolve_code(short)
    if email is None:
        ui.answer_cb(cb_id, "短码已失效")
        return
    ui.answer_cb(cb_id)
    ui.edit(
        chat_id, message_id,
        f"确认删除账户 <code>{ui.escape_html(email)}</code>？\n"
        f"⚠ 该操作将清除此账户的所有统计与亲和绑定数据。",
        reply_markup=ui.inline_kb([[
            ui.btn("✅ 确认删除", f"oa:delete_exec:{short}"),
            ui.btn("❌ 取消",     f"oa:view:{short}"),
        ]]),
    )


def on_delete_exec(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    email = ui.resolve_code(short)
    if email is None:
        ui.answer_cb(cb_id, "短码已失效")
        show(chat_id, message_id)
        return
    try:
        oauth_manager.delete_account(email)
    except Exception as exc:
        ui.answer_cb(cb_id, "删除失败")
        ui.send(chat_id, f"❌ 删除失败: <code>{ui.escape_html(str(exc))}</code>")
        return
    ui.answer_cb(cb_id, "已删除")
    ui.edit(chat_id, message_id, f"✅ 已删除 <code>{ui.escape_html(email)}</code>")
    show(chat_id, message_id)


# ─── 刷新全部用量 ─────────────────────────────────────────────────

def on_refresh_all(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id, "拉取中...")
    accounts = oauth_manager.list_accounts()
    ok = 0
    fail = 0
    for acc in accounts:
        email = acc.get("email")
        if not email:
            continue
        result = _run_sync(oauth_manager.fetch_usage(email))
        if isinstance(result, Exception):
            fail += 1
            continue
        state_db.quota_save(email, oauth_manager.flatten_usage(result))
        ok += 1
    show(chat_id, message_id)
    ui.send(chat_id, f"✅ 刷新完成：成功 {ok}，失败 {fail}")


# ─── 新增账户：入口 ──────────────────────────────────────────────

def on_add_menu(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    ui.edit(
        chat_id, message_id,
        "<b>新增 OAuth 账户</b>\n请选择方式：",
        reply_markup=ui.inline_kb([
            [ui.btn("🌐 登录获取 Token", "oa:login")],
            [ui.btn("📝 手动设置 JSON",  "oa:set_json")],
            [ui.btn("◀ 返回列表", "menu:oauth")],
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


# ─── 路由分发 ─────────────────────────────────────────────────────

def handle_callback(chat_id: int, message_id: int, cb_id: str, data: str) -> bool:
    if data == "menu:oauth":
        show(chat_id, message_id, cb_id)
        return True
    if data == "oa:refresh_all":
        on_refresh_all(chat_id, message_id, cb_id)
        return True
    if data == "oa:add":
        on_add_menu(chat_id, message_id, cb_id)
        return True
    if data == "oa:login":
        on_login_start(chat_id, message_id, cb_id)
        return True
    if data == "oa:set_json":
        on_set_json_start(chat_id, message_id, cb_id)
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
    return False


def handle_text_state(chat_id: int, action: str, text: str) -> bool:
    if action == "oa_login_code":
        on_login_code_input(chat_id, text)
        return True
    if action == "oa_set_json":
        on_set_json_input(chat_id, text)
        return True
    return False
