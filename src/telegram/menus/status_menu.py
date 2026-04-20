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

from ... import affinity, config, cooldown, log_db, oauth_manager, scorer, state_db
from ...oauth_ids import account_key as _account_key
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
    """统计 enabled / 冷却中 / 各类禁用的渠道数，按家族分组。

    返回结构：
      {
        "anthropic": {total, enabled, ...},
        "openai":    {total, enabled, ...},
        "total_all": 总数（合计；family=None 的渠道也算进来，用于单独兜底展示）,
      }
    """
    chs = registry.all_channels()
    cd_keys: set[str] = set()
    perm_keys: set[str] = set()
    for e in cooldown.active_entries():
        cd_keys.add(e["channel_key"])
        if e["cooldown_until"] == -1:
            perm_keys.add(e["channel_key"])

    def _new_bucket() -> dict:
        return {
            "total": 0, "enabled": 0, "user_disabled": 0,
            "quota_disabled": 0, "auth_err": 0, "cooling": 0, "permanent": 0,
        }

    buckets: dict[str, dict] = {"anthropic": _new_bucket(), "openai": _new_bucket()}
    for ch in chs:
        fam = ui.family_of(getattr(ch, "protocol", None)) or "anthropic"  # 未知归入 anthropic 兜底
        b = buckets[fam]
        b["total"] += 1
        if not ch.enabled:
            b["user_disabled"] += 1
            continue
        if ch.disabled_reason == "quota":
            b["quota_disabled"] += 1
            continue
        if ch.disabled_reason == "auth_error":
            b["auth_err"] += 1
            continue
        if ch.disabled_reason == "user":
            b["user_disabled"] += 1
            continue
        b["enabled"] += 1
        if ch.key in perm_keys:
            b["permanent"] += 1
        elif ch.key in cd_keys:
            b["cooling"] += 1
    buckets["total_all"] = len(chs)
    return buckets


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


def _fastest_channels_by_family(top_per_family: int = 5) -> dict:
    """按家族分组的 Top N 快渠道（enabled + 非冷却 + 近期成功率 >= 50%）。

    返回 {"anthropic": [...], "openai": [...]}
    每个元素为 (f"{channel_key}|{model}", {"rate":.., "avg_first_byte_ms":..., ...})
    """
    chs = registry.all_channels()
    ch_by_key = {ch.key: ch for ch in chs}
    enabled_keys = {ch.key for ch in chs if ch.enabled and not ch.disabled_reason}
    if not enabled_keys:
        return {"anthropic": [], "openai": []}
    cd_pairs: set[tuple[str, str]] = set()
    for e in cooldown.active_entries():
        cd_pairs.add((e["channel_key"], e["model"]))

    snapshot = scorer.snapshot()
    by_family: dict[str, list] = {"anthropic": [], "openai": []}
    for stat in snapshot:
        ck = stat["channel_key"]
        m = stat["model"]
        if ck not in enabled_keys:
            continue
        if (ck, m) in cd_pairs:
            continue
        if stat["recent_requests"] <= 0:
            continue
        rate = (stat["recent_success_count"] / stat["recent_requests"]) * 100
        if rate < 50:
            continue
        ch = ch_by_key.get(ck)
        fam = ui.family_of(getattr(ch, "protocol", None) if ch else None) or "anthropic"
        if fam not in by_family:
            continue
        by_family[fam].append((ck, m, rate, stat))

    result: dict = {}
    for fam, items in by_family.items():
        items.sort(key=lambda x: x[3]["score"])
        result[fam] = [
            (f"{ck}|{m}", {"rate": rate, **stat}) for ck, m, rate, stat in items[:top_per_family]
        ]
    return result


def _quota_warnings(threshold_pct: float = 80.0) -> list[str]:
    """OAuth 账户用量 >= threshold 的告警条目（按 provider 读不同的 util 字段）。

    Anthropic 账户的指标维度：5h / 7d / Sonnet / Opus
    OpenAI   账户的指标维度：5h / 7d（normalize_codex_snapshot 写入通用列）
                           + codex_primary / codex_secondary（codex 专属，更精细）
    """
    out: list[str] = []
    cfg = config.get()
    account_keys = [
        _account_key(a) for a in cfg.get("oauthAccounts", [])
        if a.get("email") and not a.get("disabled_reason")
    ]
    if account_keys:
        oauth_manager.ensure_quota_fresh_sync(account_keys)
    for acc in cfg.get("oauthAccounts", []):
        email = acc.get("email")
        if not email:
            continue
        if acc.get("disabled_reason"):
            continue
        ak = _account_key(acc)
        row = state_db.quota_load(ak)
        if not row:
            continue

        provider = oauth_manager.provider_of(acc)
        if provider == "openai":
            # OpenAI OAuth 没有 sonnet/opus；使用 five_hour / seven_day（通用）
            # + codex_primary / codex_secondary（codex 专属）
            utils = {
                "5h": row.get("five_hour_util"),
                "7d": row.get("seven_day_util"),
                "Primary": row.get("codex_primary_used_pct"),
                "Secondary": row.get("codex_secondary_used_pct"),
            }
            family_prefix = "🅾 "
        else:
            # Anthropic: 5h / 7d / Sonnet / Opus（原行为）
            utils = {
                "5h": row.get("five_hour_util"),
                "7d": row.get("seven_day_util"),
                "Sonnet": row.get("sonnet_util"),
                "Opus": row.get("opus_util"),
            }
            family_prefix = "🅰 "
        hot = [(k, v) for k, v in utils.items() if v is not None and v >= threshold_pct]
        if hot:
            parts = " | ".join(f"{k} {v:.0f}%" for k, v in hot)
            out.append(f"⚠ {family_prefix}<code>{ui.escape_html(email)}</code> — {parts}")
    return out


def _today_snapshot_by_family() -> dict:
    """今日请求按家族分组的快照。

    返回 {"anthropic": {...}, "openai": {...}, "total": total_all}。
    每家族有 total/succ/err/avg_first/avg_total/avg_tps。
    """
    from datetime import datetime, timedelta, timezone
    bjt = timezone(timedelta(hours=8))
    today = datetime.now(bjt).replace(hour=0, minute=0, second=0, microsecond=0)
    since = today.timestamp()

    def _snap(fam: str | None) -> dict:
        try:
            r = log_db.stats_summary(since_ts=since, family=fam, summary_top_limit=0)
            o = r.get("overall") or {}
            return {
                "total": int(o.get("total") or 0),
                "succ": int(o.get("success_count") or 0),
                "err": int(o.get("error_count") or 0),
                "avg_first": o.get("avg_first_token_ms"),
                "avg_total": o.get("avg_total_ms"),
                "avg_tps": o.get("avg_tps"),
            }
        except Exception:
            return {"total": 0, "succ": 0, "err": 0,
                    "avg_first": None, "avg_total": None, "avg_tps": None}

    return {
        "anthropic": _snap("anthropic"),
        "openai": _snap("openai"),
        "total_all": _snap(None)["total"],
    }


def _month_tps_by_channel_model() -> dict:
    """本月按 (channel_key, model) 的平均 TPS lookup；供"最快渠道"补充展示。"""
    from datetime import datetime, timedelta, timezone
    bjt = timezone(timedelta(hours=8))
    month_start = datetime.now(bjt).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    try:
        return log_db.tps_by_channel_model(since_ts=month_start.timestamp())
    except Exception:
        return {}


# ─── 渲染 ─────────────────────────────────────────────────────────

def _fmt_channel_bucket(bucket: dict) -> str:
    """单家族一行：共 N · ✅ 可用 N · ⚠ 冷却 N · 🔴 永久 N ..."""
    parts = [f"共 {bucket['total']} · ✅ 可用 {bucket['enabled']}"]
    if bucket["cooling"]:
        parts.append(f"⚠ 冷却 {bucket['cooling']}")
    if bucket["permanent"]:
        parts.append(f"🔴 永久 {bucket['permanent']}")
    if bucket["user_disabled"]:
        parts.append(f"🚫 用户 {bucket['user_disabled']}")
    if bucket["quota_disabled"]:
        parts.append(f"🔒 配额 {bucket['quota_disabled']}")
    if bucket["auth_err"]:
        parts.append(f"❌ 认证 {bucket['auth_err']}")
    return " · ".join(parts)


def _fmt_today_family(snap: dict) -> str:
    """单家族今日一行：100 次 · ✅ 97% · 首字 890ms · 总 4.2s · ⚡ 42 t/s"""
    total = snap.get("total") or 0
    if total == 0:
        return "暂无请求"
    succ = snap.get("succ") or 0
    err = snap.get("err") or 0
    parts = [f"{total} 次 ({ui.fmt_rate(succ, total)})"]
    if err:
        parts.append(f"❌ {err}")
    timing = []
    if snap.get("avg_first") is not None:
        timing.append(f"首字 {ui.fmt_ms(snap['avg_first'])}")
    if snap.get("avg_total") is not None:
        timing.append(f"总 {ui.fmt_ms(snap['avg_total'])}")
    if snap.get("avg_tps") is not None:
        timing.append(f"⚡ {ui.fmt_tps(snap['avg_tps'])}")
    if timing:
        parts.append(" · ".join(timing))
    return " · ".join(parts)


def _render_fastest_family(fam: str, items: list, tps_map: dict) -> list[str]:
    """单家族 Top3 渲染：每条名称一行 + 数据一行（解决长邮箱折行）。"""
    if not items:
        return []
    tag = ui.family_tag(fam)
    out = [f"<b>⚡ 最快渠道 ({tag}):</b>"]
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
    for idx, (key, info) in enumerate(items):
        medal = medals[idx] if idx < len(medals) else f"{idx + 1}."
        ck, m = key.split("|", 1)
        short = ck.split(":", 1)[1] if ":" in ck else ck
        ico = "🔐" if ck.startswith("oauth:") else "🔀"
        # 名称一行
        out.append(
            f"{medal} {ico} <code>{ui.escape_html(short)}</code> ({ui.escape_html(m)})"
        )
        # 数据一行
        tps = tps_map.get((ck, m))
        tps_part = f" · ⚡ {ui.fmt_tps(tps)}" if tps is not None else ""
        out.append(
            f"   首字 {ui.fmt_ms(info['avg_first_byte_ms'])} · "
            f"成功率 {info['rate']:.0f}% · {info['recent_requests']} 次{tps_part}"
        )
    return out


def _compose() -> tuple[str, dict]:
    cfg = config.get()
    uptime = _fmt_uptime(time.time() - _SERVICE_START_TS)
    mode = cfg.get("channelSelection", "smart")

    overview = _channel_overview()
    today = _today_snapshot_by_family()
    fastest_by_fam = _fastest_channels_by_family(top_per_family=5)
    problems = _problem_channels()
    quota_warn = _quota_warnings(80.0)

    # 月度 TPS 映射（只查一次）
    any_fastest = bool(fastest_by_fam.get("anthropic")) or bool(fastest_by_fam.get("openai"))
    tps_map = _month_tps_by_channel_model() if any_fastest else {}

    sep = "─" * 18
    lines = [
        "📊 <b>状态总览</b>",
        sep,
        f"🕐 运行: <code>{uptime}</code> · ⚙ 选路: <code>{mode}</code> · 🔗 亲和: <code>{affinity.count()}</code>",
    ]

    # 渠道：双家族各一行
    lines.append("")
    lines.append("<b>渠道:</b>")
    for fam in ("anthropic", "openai"):
        b = overview.get(fam) or {}
        if b.get("total", 0) == 0:
            continue
        tag = ui.family_tag(fam)
        lines.append(f"{tag}: {_fmt_channel_bucket(b)}")

    # 今日请求：双家族各一段
    lines.append("")
    lines.append("<b>今日请求:</b>")
    anth_today = today.get("anthropic") or {}
    oai_today = today.get("openai") or {}
    if (anth_today.get("total") or 0) > 0 or (oai_today.get("total") or 0) > 0:
        # 名称一行 + 数据一行，防长行折行（跟长邮箱渠道一致的处理原则）
        if (anth_today.get("total") or 0) > 0:
            lines.append(f"🅰 Anthropic:")
            lines.append(f"  {_fmt_today_family(anth_today)}")
        if (oai_today.get("total") or 0) > 0:
            lines.append(f"🅾 OpenAI:")
            lines.append(f"  {_fmt_today_family(oai_today)}")
    else:
        lines.append("暂无请求")

    # 最快渠道：按家族各 Top3
    for fam in ("anthropic", "openai"):
        items = fastest_by_fam.get(fam) or []
        if items:
            lines.append("")
            lines += _render_fastest_family(fam, items, tps_map)

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
