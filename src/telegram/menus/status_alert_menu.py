"""📡 故障订阅菜单。

入口：「⚙ 系统设置 → 📡 故障订阅」或主菜单 banner 点击。

功能：
- 总开关 + 三个 provider 各自开关 + 推送门槛 + 轮询间隔
- 展示当前活跃 incident
- 展示最近 N 条历史 incident（同步拉一次 statuspage）
"""

from __future__ import annotations

from typing import Optional

from ... import config, status_monitor
from .. import states, ui


_PROVIDERS_ORDER = ("claude", "openai", "cloudflare")
_IMPACT_OPTIONS = (
    ("none", "全部（含 none）"),
    ("minor", "minor 及以上（默认）"),
    ("major", "major 及以上"),
    ("critical", "仅 critical"),
)


def _cfg() -> dict:
    return config.get().get("statusMonitor") or {}


def _update_cfg(patch: dict) -> None:
    def _mut(c):
        sm = c.setdefault("statusMonitor", {})
        sm.update(patch)
    config.update(_mut)


def _format_active_block() -> str:
    snap = status_monitor.snapshot_active()
    lines: list[str] = ["<b>当前活跃事件</b>"]
    total = 0
    for p in _PROVIDERS_ORDER:
        incs = snap.get(p) or []
        if not incs:
            continue
        total += len(incs)
        lines.append("")
        lines.append(status_monitor._provider_tag(p))
        for i in incs[:5]:
            name = (i.get("name") or "").strip()
            impact = (i.get("impact") or "none").lower()
            status = (i.get("status") or "").lower()
            icon = status_monitor._IMPACT_ICON.get(impact, "📡")
            sicon = status_monitor._STATUS_ICON.get(status, "")
            short = i.get("shortlink") or ""
            lines.append(f"  {icon} {ui.escape_html(name)} · {sicon} <code>{status}</code>")
            if short:
                lines.append(f"  <code>{ui.escape_html(short)}</code>")
    if total == 0:
        lines.append("✅ 三家上游均无活跃事件")
    return "\n".join(lines)


def _main_text_and_kb() -> tuple[str, dict]:
    cfg = _cfg()
    enabled = bool(cfg.get("enabled", True))
    interval = int(cfg.get("intervalSeconds", 60) or 60)
    targets = set(cfg.get("targets") or _PROVIDERS_ORDER)
    min_impact = (cfg.get("minImpact") or "minor").lower()
    notif_evt = ((config.get().get("notifications") or {}).get("events") or {}).get("status_alert", True)
    muted_total = len(status_monitor.list_muted())

    lines = [
        "📡 <b>故障订阅</b>",
        "",
        f"总开关: <code>{'开' if enabled else '关'}</code>",
        f"通知事件 status_alert: <code>{'开' if notif_evt else '关'}</code>（通知设置里也能切）",
        f"轮询间隔: <code>{interval}s</code>",
        f"推送门槛: <code>{min_impact}</code>",
        f"已屏蔽事件: <code>{muted_total}</code>",
        "",
        _format_active_block(),
    ]
    text = "\n".join(lines)

    rows = [
        [ui.btn(f"{'🟢 已开启' if enabled else '🔴 已关闭'} · 切换", "stat:toggle_enabled")],
    ]
    rows.append([
        ui.btn(("✅ " if "claude" in targets else "⬛ ") + status_monitor._provider_icon("claude") + " Claude", "stat:toggle_tgt:claude"),
        ui.btn(("✅ " if "openai" in targets else "⬛ ") + status_monitor._provider_icon("openai") + " OpenAI", "stat:toggle_tgt:openai"),
        ui.btn(("✅ " if "cloudflare" in targets else "⬛ ") + "Cloudflare", "stat:toggle_tgt:cloudflare"),
    ])
    rows.append([
        ui.btn(f"⏱ 间隔: {interval}s", "stat:edit_interval"),
        ui.btn(f"🚦 门槛: {min_impact}", "stat:show_impact"),
    ])
    # 活跃事件：每条加一个屏蔽按钮
    snap = status_monitor.snapshot_active()
    for p_key in _PROVIDERS_ORDER:
        for inc in (snap.get(p_key) or [])[:5]:
            iid = inc.get("id") or ""
            name = (inc.get("name") or "").strip()[:30]
            if not iid:
                continue
            short = ui.register_code(f"stat_mute:{p_key}:{iid}|{name}")
            rows.append([ui.btn(
                f"🔕 屏蔽 [{status_monitor._provider_label(p_key)}] {name}",
                f"stat:mute:{short}",
            )])
    rows.append([
        ui.btn(f"🔕 已屏蔽列表（{muted_total}）", "stat:muted_list"),
        ui.btn("📜 查看历史", "stat:history"),
    ])
    rows.append([
        ui.btn("🔄 立即刷新", "stat:refresh"),
    ])
    rows.append([ui.btn("◀ 返回设置", "menu:settings"), ui.btn("🏠 主菜单", "menu:main")])
    return text, ui.inline_kb(rows)


def show(chat_id: int, message_id: int, cb_id: Optional[str] = None) -> None:
    if cb_id is not None:
        ui.answer_cb(cb_id)
    text, kb = _main_text_and_kb()
    ui.edit(chat_id, message_id, text, reply_markup=kb)


def send_new(chat_id: int) -> None:
    text, kb = _main_text_and_kb()
    ui.send(chat_id, text, reply_markup=kb)


# ─── 各操作 ────────────────────────────────────────────────────


def _toggle_enabled(chat_id: int, message_id: int, cb_id: str) -> None:
    cur = bool(_cfg().get("enabled", True))
    _update_cfg({"enabled": not cur})
    ui.answer_cb(cb_id, "已关闭" if cur else "已开启")
    show(chat_id, message_id)


def _toggle_target(chat_id: int, message_id: int, cb_id: str, provider: str) -> None:
    if provider not in status_monitor.TARGETS:
        ui.answer_cb(cb_id, "未知 provider")
        return
    cfg = _cfg()
    targets = list(cfg.get("targets") or list(_PROVIDERS_ORDER))
    if provider in targets:
        targets.remove(provider)
        # 同步清掉内存里该 provider 的活跃记录，避免 banner 还显示旧故障
        status_monitor.forget_provider(provider)
        msg = f"已移除 {provider}"
    else:
        targets.append(provider)
        msg = f"已添加 {provider}"
    _update_cfg({"targets": targets})
    ui.answer_cb(cb_id, msg)
    show(chat_id, message_id)


def _edit_interval(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    states.set_state(chat_id, "stat_interval")
    ui.edit(
        chat_id, message_id,
        "请输入轮询间隔（秒，≥10 推荐 30-300）：\n\n例：<code>60</code>",
        reply_markup=ui.inline_kb([[ui.btn("❌ 取消", "menu:status_alert")]]),
    )


def _on_interval_input(chat_id: int, text: str) -> None:
    try:
        v = int((text or "").strip())
        if v < 10:
            raise ValueError
    except ValueError:
        ui.send(chat_id, "❌ 需要 ≥10 的整数，请重新输入：")
        return
    _update_cfg({"intervalSeconds": v})
    states.pop_state(chat_id)
    ui.send(chat_id, f"✅ 轮询间隔已更新为 <code>{v}s</code>")
    send_new(chat_id)


def _show_impact(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    current = (_cfg().get("minImpact") or "minor").lower()
    rows = []
    for key, label in _IMPACT_OPTIONS:
        marker = "● " if key == current else ""
        rows.append([ui.btn(f"{marker}{label}", f"stat:set_impact:{key}")])
    rows.append([ui.btn("◀ 返回", "menu:status_alert")])
    ui.edit(
        chat_id, message_id,
        "选择推送门槛（低于该级别的事件不主动推，但仍记录进顶部 banner）：",
        reply_markup=ui.inline_kb(rows),
    )


def _set_impact(chat_id: int, message_id: int, cb_id: str, value: str) -> None:
    valid = {k for k, _ in _IMPACT_OPTIONS}
    if value not in valid:
        ui.answer_cb(cb_id, "无效值")
        return
    _update_cfg({"minImpact": value})
    ui.answer_cb(cb_id, f"门槛已切为 {value}")
    show(chat_id, message_id)


def _refresh(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id, "刷新中…")
    # 同步拉一次（短时间，可接受）
    try:
        for p in _cfg().get("targets") or list(_PROVIDERS_ORDER):
            if p in status_monitor.TARGETS:
                # 已 prime 过的 provider 走 push 模式；首次刷新仍只静默
                first = p not in status_monitor._initialized_providers
                status_monitor._process_provider(p, push=not first)
                status_monitor._initialized_providers.add(p)
    except Exception as exc:
        ui.send(chat_id, f"刷新失败: <code>{ui.escape_html(str(exc))}</code>")
        return
    show(chat_id, message_id)


def _history(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id, "拉取中…")
    lines = ["📜 <b>最近事件（各 provider 最多 5 条）</b>"]
    for p in _cfg().get("targets") or list(_PROVIDERS_ORDER):
        if p not in status_monitor.TARGETS:
            continue
        try:
            incs = status_monitor.list_recent_incidents(p, limit=5)
        except Exception as exc:
            incs = []
            lines.append(f"\n{status_monitor._provider_tag(p)} — 拉取失败: <code>{ui.escape_html(str(exc))}</code>")
            continue
        lines.append("")
        lines.append(status_monitor._provider_tag(p))
        if not incs:
            lines.append("  (空)")
            continue
        for i in incs:
            name = (i.get("name") or "").strip()
            impact = (i.get("impact") or "none").lower()
            status = (i.get("status") or "").lower()
            icon = status_monitor._IMPACT_ICON.get(impact, "📡")
            sicon = status_monitor._STATUS_ICON.get(status, "")
            created = i.get("created_at") or ""
            lines.append(
                f"  {icon} {ui.escape_html(name)}\n"
                f"    {sicon} <code>{status}</code> · {ui.escape_html(created)}"
            )
    ui.edit(
        chat_id, message_id,
        ui.truncate("\n".join(lines)),
        reply_markup=ui.inline_kb([[ui.btn("◀ 返回", "menu:status_alert")]]),
    )


def _mute_exec(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    payload = ui.resolve_code(short)
    if not payload or not payload.startswith("stat_mute:"):
        ui.answer_cb(cb_id, "短码失效")
        return
    body = payload[len("stat_mute:"):]
    spec, _, name = body.partition("|")
    provider, _, iid = spec.partition(":")
    if not provider or not iid:
        ui.answer_cb(cb_id, "解析失败")
        return
    status_monitor.mute_incident(provider, iid, name=name)
    ui.answer_cb(cb_id, f"已屏蔽: {name[:20]}")
    show(chat_id, message_id)


def _show_muted_list(chat_id: int, message_id: int, cb_id: Optional[str] = None) -> None:
    if cb_id is not None:
        ui.answer_cb(cb_id)
    rows_data = status_monitor.list_muted()
    if not rows_data:
        text = "🔕 <b>已屏蔽列表</b>\n\n(当前无屏蔽)"
        ui.edit(chat_id, message_id, text, reply_markup=ui.inline_kb([
            [ui.btn("◀ 返回", "menu:status_alert")],
        ]))
        return

    import datetime as _dt
    lines = ["🔕 <b>已屏蔽列表</b>", ""]
    kb_rows: list[list[dict]] = []
    for r in rows_data[:20]:
        prov = r["provider"]
        iid = r["incident_id"]
        name = r["name"] or "(unnamed)"
        ts = r["muted_at"]
        when = _dt.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
        lines.append(f"{status_monitor._provider_tag(prov)} {ui.escape_html(name[:60])}")
        lines.append(f"  <code>{ui.escape_html(iid)}</code> · 屏蔽于 {when}")
        short = ui.register_code(f"stat_unmute:{prov}:{iid}")
        kb_rows.append([ui.btn(f"✅ 解除 [{status_monitor._provider_label(prov)}] {name[:25]}",
                               f"stat:unmute:{short}")])
    if len(rows_data) > 20:
        lines.append(f"\n(仅显示前 20 条，共 {len(rows_data)} 条)")
    kb_rows.append([ui.btn("◀ 返回", "menu:status_alert")])
    ui.edit(chat_id, message_id, ui.truncate("\n".join(lines)), reply_markup=ui.inline_kb(kb_rows))


def _unmute_exec(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    payload = ui.resolve_code(short)
    if not payload or not payload.startswith("stat_unmute:"):
        ui.answer_cb(cb_id, "短码失效")
        return
    spec = payload[len("stat_unmute:"):]
    provider, _, iid = spec.partition(":")
    if not provider or not iid:
        ui.answer_cb(cb_id, "解析失败")
        return
    status_monitor.unmute_incident(provider, iid)
    ui.answer_cb(cb_id, "已解除")
    _show_muted_list(chat_id, message_id, cb_id=None)  # 已 answer 过；不再 answer


# ─── 路由 ─────────────────────────────────────────────────────


def handle_callback(chat_id: int, message_id: int, cb_id: str, data: str) -> bool:
    if data == "menu:status_alert":
        show(chat_id, message_id, cb_id); return True
    if data == "stat:toggle_enabled":
        _toggle_enabled(chat_id, message_id, cb_id); return True
    if data.startswith("stat:toggle_tgt:"):
        _toggle_target(chat_id, message_id, cb_id, data.split(":", 2)[2]); return True
    if data == "stat:edit_interval":
        _edit_interval(chat_id, message_id, cb_id); return True
    if data == "stat:show_impact":
        _show_impact(chat_id, message_id, cb_id); return True
    if data.startswith("stat:set_impact:"):
        _set_impact(chat_id, message_id, cb_id, data.split(":", 2)[2]); return True
    if data == "stat:refresh":
        _refresh(chat_id, message_id, cb_id); return True
    if data == "stat:history":
        _history(chat_id, message_id, cb_id); return True
    if data.startswith("stat:mute:"):
        _mute_exec(chat_id, message_id, cb_id, data.split(":", 2)[2]); return True
    if data == "stat:muted_list":
        _show_muted_list(chat_id, message_id, cb_id); return True
    if data.startswith("stat:unmute:"):
        _unmute_exec(chat_id, message_id, cb_id, data.split(":", 2)[2]); return True
    return False


def handle_text_state(chat_id: int, action: str, text: str) -> bool:
    if action == "stat_interval":
        _on_interval_input(chat_id, text); return True
    return False
