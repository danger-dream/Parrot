"""📊 状态总览（运维一眼诊断页）。

合并自 openai-proxy 的 status.js + cc-proxy 的最近调用：
  - 服务运行时长 / 选路模式 / 亲和绑定数
  - 渠道总览（可用 / 禁用 / 冷却 / quota / auth_error）
  - OAuth 配额预警（≥80% 高亮）
  - ⚡ 最快渠道 Top 5（按评分升序）
  - ⚠ 问题渠道清单（含原因）
  - 今日请求快照
"""

from __future__ import annotations

import time
from typing import Optional

from ... import affinity, config, cooldown, log_db, scorer, state_db
from ...channel import registry
from .. import ui


_SERVICE_START_TS = time.time()


def _fmt_uptime(secs: float) -> str:
    s = int(max(0, secs))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}分{s % 60}秒"
    if s < 86400:
        return f"{s // 3600}小时{(s % 3600) // 60}分"
    return f"{s // 86400}天{(s % 86400) // 3600}小时"


def _channel_overview() -> dict:
    """统计 enabled / 冷却中 / 各类禁用的渠道数。"""
    chs = registry.all_channels()
    cd_keys: set[str] = set()
    perm_keys: set[str] = set()
    for e in cooldown.active_entries():
        cd_keys.add(e["channel_key"])
        if e["cooldown_until"] == -1:
            perm_keys.add(e["channel_key"])

    enabled = 0
    user_disabled = 0
    quota_disabled = 0
    auth_err = 0
    cooling_only = 0   # enabled 但全部模型在冷却的渠道数
    permanent = 0
    for ch in chs:
        if not ch.enabled:
            user_disabled += 1
            continue
        if ch.disabled_reason == "quota":
            quota_disabled += 1
            continue
        if ch.disabled_reason == "auth_error":
            auth_err += 1
            continue
        if ch.disabled_reason == "user":
            user_disabled += 1
            continue
        enabled += 1
        if ch.key in perm_keys:
            permanent += 1
        elif ch.key in cd_keys:
            cooling_only += 1
    return {
        "total": len(chs),
        "enabled": enabled,
        "user_disabled": user_disabled,
        "quota_disabled": quota_disabled,
        "auth_err": auth_err,
        "cooling": cooling_only,
        "permanent": permanent,
    }


def _problem_channels() -> list[str]:
    """问题渠道（含原因），用于"⚠ 问题渠道"区。"""
    out: list[str] = []
    chs = registry.all_channels()
    cd_map: dict[str, list[dict]] = {}
    for e in cooldown.active_entries():
        cd_map.setdefault(e["channel_key"], []).append(e)

    for ch in chs:
        short = ui.escape_html(ch.display_name)
        icon = "🔐" if ch.type == "oauth" else "🔀"
        if not ch.enabled or ch.disabled_reason == "user":
            out.append(f"• {icon} {short} — 手动禁用")
            continue
        if ch.disabled_reason == "quota":
            out.append(f"• {icon} {short} — 配额禁用")
            continue
        if ch.disabled_reason == "auth_error":
            out.append(f"• {icon} {short} — 认证失败（refresh_token 失效）")
            continue
        # enabled 渠道但有冷却模型
        entries = cd_map.get(ch.key, [])
        if not entries:
            continue
        for e in entries:
            model = ui.escape_html(e["model"])
            ec = int(e.get("error_count") or 0)
            if e["cooldown_until"] == -1:
                out.append(f"• {icon} {short} ({model}) — 永久冷却 · 累计失败 {ec} 次")
            else:
                remaining = max(0, (e["cooldown_until"] - int(time.time() * 1000)) // 1000)
                out.append(
                    f"• {icon} {short} ({model}) — 冷却中 剩 {remaining}s · 累计失败 {ec} 次"
                )
    return out


def _fastest_channels(top_n: int = 5) -> list[tuple[str, dict]]:
    """按 scorer 分数升序取 Top N（仅在 enabled + 非冷却 + 近期成功率 >= 50%）。"""
    chs = registry.all_channels()
    enabled_keys = {ch.key for ch in chs if ch.enabled and not ch.disabled_reason}
    if not enabled_keys:
        return []
    cd_pairs: set[tuple[str, str]] = set()
    for e in cooldown.active_entries():
        cd_pairs.add((e["channel_key"], e["model"]))

    snapshot = scorer.snapshot()
    qualified = []
    for s in snapshot:
        if s["channel_key"] not in enabled_keys:
            continue
        if (s["channel_key"], s["model"]) in cd_pairs:
            continue
        if s["recent_requests"] <= 0:
            continue
        rate = (s["recent_success_count"] / s["recent_requests"]) * 100
        if rate < 50:
            continue
        qualified.append((s["channel_key"], s["model"], rate, s))
    qualified.sort(key=lambda x: x[3]["score"])
    return [(f"{ck}|{m}", {"rate": rate, **stat}) for ck, m, rate, stat in qualified[:top_n]]


def _quota_warnings(threshold_pct: float = 80.0) -> list[str]:
    """OAuth 账户用量 >= threshold 的告警条目。"""
    out: list[str] = []
    cfg = config.get()
    for acc in cfg.get("oauthAccounts", []):
        email = acc.get("email")
        if not email:
            continue
        # 已被禁用的不需告警（已在问题渠道里）
        if acc.get("disabled_reason"):
            continue
        row = state_db.quota_load(email)
        if not row:
            continue
        utils = {
            "5h": row.get("five_hour_util"),
            "7d": row.get("seven_day_util"),
            "Sonnet": row.get("sonnet_util"),
            "Opus": row.get("opus_util"),
        }
        hot = [(k, v) for k, v in utils.items() if v is not None and v >= threshold_pct]
        if hot:
            parts = " | ".join(f"{k} {v:.0f}%" for k, v in hot)
            out.append(f"⚠ <code>{ui.escape_html(email)}</code> — {parts}")
    return out


def _today_snapshot() -> dict:
    """今日请求总数、成功率、平均响应。"""
    from datetime import datetime, timedelta, timezone
    bjt = timezone(timedelta(hours=8))
    today = datetime.now(bjt).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        result = log_db.stats_summary(since_ts=today.timestamp(), summary_top_limit=0)
    except Exception:
        return {"total": 0, "succ": 0, "avg_total": None}
    o = result.get("overall") or {}
    return {
        "total": int(o.get("total") or 0),
        "succ": int(o.get("success_count") or 0),
        "err": int(o.get("error_count") or 0),
        "avg_total": o.get("avg_total_ms"),
        "avg_first": o.get("avg_first_token_ms"),
    }


# ─── 渲染 ─────────────────────────────────────────────────────────

def _compose() -> tuple[str, dict]:
    cfg = config.get()
    uptime = _fmt_uptime(time.time() - _SERVICE_START_TS)
    mode = cfg.get("channelSelection", "smart")

    overview = _channel_overview()
    today = _today_snapshot()
    fastest = _fastest_channels()
    problems = _problem_channels()
    quota_warn = _quota_warnings(80.0)

    sep = "─" * 18
    lines = [
        "📊 <b>状态总览</b>",
        sep,
        f"🕐 运行: <code>{uptime}</code> · ⚙ 选路: <code>{mode}</code> · 🔗 亲和: <code>{affinity.count()}</code>",
        "",
        "<b>渠道:</b>",
        f"共 {overview['total']} · ✅ 可用 {overview['enabled']}"
        + (f" · ⚠ 冷却 {overview['cooling']}" if overview['cooling'] else "")
        + (f" · 🔴 永久 {overview['permanent']}" if overview['permanent'] else "")
        + (f" · 🚫 用户 {overview['user_disabled']}" if overview['user_disabled'] else "")
        + (f" · 🔒 配额 {overview['quota_disabled']}" if overview['quota_disabled'] else "")
        + (f" · ❌ 认证失败 {overview['auth_err']}" if overview['auth_err'] else ""),
    ]

    # 今日请求
    lines += [
        "",
        "<b>今日:</b>",
        f"共 {today['total']} 次 · ✅ {today['succ']} ({ui.fmt_rate(today['succ'], today['total'])})"
        + (f" · ❌ {today['err']}" if today['err'] else ""),
    ]
    if today["avg_total"] or today["avg_first"]:
        lines.append(f"首字 {ui.fmt_ms(today['avg_first'])} · 总 {ui.fmt_ms(today['avg_total'])}")

    # 最快渠道
    if fastest:
        lines += ["", "<b>⚡ 最快渠道:</b>"]
        medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
        for idx, (key, info) in enumerate(fastest):
            medal = medals[idx] if idx < len(medals) else f"{idx + 1}."
            ck, m = key.split("|", 1)
            short = ck.split(":", 1)[1] if ":" in ck else ck
            ico = "🔐" if ck.startswith("oauth:") else "🔀"
            lines.append(
                f"{medal} {ico} <code>{ui.escape_html(short)}</code> "
                f"({ui.escape_html(m)})"
            )
            lines.append(
                f"   首字 {ui.fmt_ms(info['avg_first_byte_ms'])} · "
                f"成功率 {info['rate']:.0f}% · {info['recent_requests']} 次"
            )

    # 配额预警
    if quota_warn:
        lines += ["", "<b>📈 配额预警 (≥80%):</b>"]
        lines += quota_warn

    # 问题渠道
    if problems:
        lines += ["", f"<b>⚠ 问题渠道 ({len(problems)}):</b>"]
        lines += problems[:8]
        if len(problems) > 8:
            lines.append(f"... 还有 {len(problems) - 8} 个")
    elif not quota_warn:
        lines += ["", "✅ 所有渠道运行正常"]

    text = ui.truncate("\n".join(lines))
    kb = ui.inline_kb([
        [ui.btn("🔄 刷新", "menu:status")],
        ui.back_to_main_row(),
    ])
    return text, kb


# ─── 入口 ─────────────────────────────────────────────────────────

def show(chat_id: int, message_id: int, cb_id: Optional[str] = None) -> None:
    if cb_id is not None:
        ui.answer_cb(cb_id)
    text, kb = _compose()
    ui.edit(chat_id, message_id, text, reply_markup=kb)


def send_new(chat_id: int) -> None:
    text, kb = _compose()
    ui.send(chat_id, text, reply_markup=kb)


def handle_callback(chat_id: int, message_id: int, cb_id: str, data: str) -> bool:
    if data == "menu:status":
        show(chat_id, message_id, cb_id)
        return True
    return False
