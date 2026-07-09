"""系统设置菜单。

callback_data 前缀：`sys:...`
状态机 action：`sys_*`（各编辑子项）
"""

from __future__ import annotations

import asyncio

from ... import apikey_limiter, concurrency, config, load_balancing, network, network_monitor, state_db
from ...channel import registry
from .. import states, ui


# ─── 主菜单 ───────────────────────────────────────────────────────

def _main_text_and_kb() -> tuple[str, dict]:
    cfg = config.get()
    t = cfg.get("timeouts") or {}
    sc = cfg.get("scoring") or {}
    aff = cfg.get("affinity") or {}
    ws_enabled = bool((cfg.get("openai") or {}).get("responsesUpstreamWsForOAuth", False))
    ws_mode_label = "开" if ws_enabled else "关"

    text = (
        "⚙ <b>系统设置</b>\n\n"
        f"超时: 连接 <code>{t.get('connect', 10)}s</code> | "
        f"首字 <code>{t.get('firstByte', 30)}s</code> | "
        f"空闲 <code>{t.get('idle', 30)}s</code> | "
        f"总 <code>{t.get('total', 600)}s</code>\n"
        f"错误阶梯: <code>{','.join(str(x) for x in (cfg.get('errorWindows') or []))}</code>\n"
        f"评分: α={sc.get('emaAlpha', 0.25)} · 窗口={sc.get('recentWindow', 50)} · "
        f"惩罚={sc.get('errorPenaltyFactor', 8)} · 探索={sc.get('explorationRate', 0.2)}\n"
        f"亲和: TTL={aff.get('ttlMinutes', 30)}min\n"
        f"调度: <code>{load_balancing.display_mode(cfg.get('channelSelection', 'smart'))}</code>\n"
        f"WS模式: HTTP→WS 上游转换 <code>{ws_mode_label}</code>\n"
    )
    bl = cfg.get("contentBlacklist") or {}
    bl_default_count = len((bl.get("default") or []))
    bl_by_ch_count = sum(len(v or []) for v in (bl.get("byChannel") or {}).values())
    # 翻译层状态
    tl_cfg = cfg.get("translation") or {}
    tl_enabled = bool(tl_cfg.get("enabled"))
    tl_model = tl_cfg.get("model") or ""
    tl_lang = tl_cfg.get("targetLanguage") or "English"
    if tl_enabled and tl_model:
        sys_flag = " · sys" if bool(tl_cfg.get("translateSystemMessages", False)) else ""
        tl_summary = f"开 · {tl_model} → {tl_lang}{sys_flag}"
    elif tl_enabled:
        tl_summary = "开 (未配置模型)"
    else:
        tl_summary = "关"

    text += f"黑名单: 默认 {bl_default_count} 条 · 渠道专属 {bl_by_ch_count} 条"
    text += f"\n翻译层: <code>{tl_summary}</code>"

    net = cfg.get("network") or {}
    dns_servers = (net.get("dns") or {}).get("servers") or ["8.8.8.8"]
    proxy_count = len(net.get("proxies") or {})
    default_route = (net.get("routing") or {}).get("default", "direct")
    text += (
        "\n"
        f"网络: DNS <code>{ui.escape_html(','.join(str(x) for x in dns_servers))}</code>"
        f" · 代理 <code>{proxy_count}</code>个"
        f" · 路由 <code>{ui.escape_html(str(default_route))}</code>"
    )

    kb = ui.inline_kb([
        [ui.btn("⏱ 超时设置", "sys:show:timeouts"),
         ui.btn("⛔ 错误阶梯", "sys:show:errwin")],
        [ui.btn("🎯 评分参数", "sys:show:scoring"),
         ui.btn("🔗 亲和参数", "sys:show:affinity")],
        [ui.btn("🔔 通知设置", "sys:show:notif"),
         ui.btn("📡 故障订阅", "menu:status_alert")],
        [ui.btn("🛡 首包黑名单", "sys:show:blacklist"),
         ui.btn("⚡ 渠道并发", "sys:show:concurrency")],
        [ui.btn("🔑 API Key 限流", "sys:show:aklim"),
         ui.btn("🌐 网络设置", "sys:show:network")],
        [ui.btn("🧬 WS模式", "sys:show:ws_mode"),
         ui.btn("🗣 翻译层", "tl:show")],
        [ui.btn("🆕 版本更新", "menu:update"),
         ui.btn("◀ 返回主菜单", "menu:main")],
    ])
    return text, kb


def show(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    text, kb = _main_text_and_kb()
    ui.edit(chat_id, message_id, text, reply_markup=kb)


def send_new(chat_id: int) -> None:
    text, kb = _main_text_and_kb()
    ui.send(chat_id, text, reply_markup=kb)


# ─── 超时设置 ─────────────────────────────────────────────────────

def _show_timeouts(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    t = config.get().get("timeouts") or {}
    text = (
        "⏱ <b>超时设置</b>\n\n"
        f"连接最大时长: <code>{t.get('connect', 10)}s</code>\n"
        f"首字最大时长: <code>{t.get('firstByte', 30)}s</code>\n"
        f"空闲最大时长: <code>{t.get('idle', 30)}s</code>\n"
        f"总请求最大时长: <code>{t.get('total', 600)}s</code>"
    )
    ui.edit(chat_id, message_id, text, reply_markup=ui.inline_kb([
        [ui.btn("✏ 修改", "sys:edit:timeouts")],
        [ui.btn("◀ 返回设置", "menu:settings")],
    ]))


def _edit_timeouts(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    states.set_state(chat_id, "sys_timeouts")
    ui.edit(
        chat_id, message_id,
        "请输入超时配置，格式：<code>&lt;连接&gt;,&lt;首字&gt;,&lt;空闲&gt;,&lt;总&gt;</code>\n"
        "单位: 秒；均需为正整数。\n\n"
        "例: <code>10,30,30,600</code>",
        reply_markup=ui.inline_kb([[ui.btn("❌ 取消", "sys:show:timeouts")]]),
    )


def _on_timeouts_input(chat_id: int, text: str) -> None:
    parts = [p.strip() for p in (text or "").split(",")]
    if len(parts) != 4:
        ui.send(chat_id, "❌ 需要 4 个数字（连接,首字,空闲,总），请重新输入：")
        return
    try:
        c, fb, idle, total = [int(p) for p in parts]
    except ValueError:
        ui.send(chat_id, "❌ 非法数字，请重新输入：")
        return
    if any(x <= 0 for x in (c, fb, idle, total)):
        ui.send(chat_id, "❌ 所有值必须为正整数，请重新输入：")
        return

    def _m(cfg):
        cfg.setdefault("timeouts", {}).update({
            "connect": c, "firstByte": fb, "idle": idle, "total": total,
        })
    config.update(_m)
    states.pop_state(chat_id)
    ui.send_result(
        chat_id,
        f"✅ 已更新：连接 {c}s · 首字 {fb}s · 空闲 {idle}s · 总 {total}s",
        back_label="◀ 返回系统设置", back_callback="menu:settings",
    )


# ─── 错误阶梯 ─────────────────────────────────────────────────────

def _show_errwin(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    cfg = config.get()
    win = cfg.get("errorWindows") or []
    grace = int(cfg.get("oauthGraceCount", 3))
    ladder_interval = int(cfg.get("cooldownLadderMinIntervalSeconds", 30))
    perm_min_age = int(cfg.get("cooldownPermanentMinAgeSeconds", 300))
    text = (
        "⛔ <b>错误冷却阶梯</b>\n\n"
        f"阶梯（分钟）: <code>{','.join(str(x) for x in win)}</code>\n"
        f"OAuth 宽容次数: <code>{grace}</code>\n"
        f"阶梯推进最小间隔: <code>{ladder_interval}s</code>\n"
        f"永久冷却最小累计: <code>{perm_min_age}s</code>\n\n"
        "<i>说明：</i>\n"
        "<i>• 每个 (渠道, 模型) 连续失败递进到下一阶梯；末位为 0 表示永久拉黑</i>\n"
        "<i>• 成功一次立即重置失败计数</i>\n"
        f"<i>• OAuth 渠道前 <b>{grace}</b> 次失败仅累计计数、不冷却（避免单账号偶发故障导致全部 Claude 模型不可用）</i>\n"
        f"<i>• 两次阶梯推进至少间隔 <b>{ladder_interval}s</b>（挡住客户端秒级重试把渠道打穿）</i>\n"
        f"<i>• 从首次失败起持续 &lt; <b>{perm_min_age}s</b> 时，即使推到末位档也不进永久，回退到倒数第二档（挡住短时爆发式失败）</i>"
    )
    ui.edit(chat_id, message_id, text, reply_markup=ui.inline_kb([
        [ui.btn("✏ 修改阶梯", "sys:edit:errwin"),
         ui.btn("✏ OAuth 宽容次数", "sys:edit:oauth_grace")],
        [ui.btn("✏ 推进最小间隔", "sys:edit:ladder_interval"),
         ui.btn("✏ 永久最小累计", "sys:edit:perm_min_age")],
        [ui.btn("◀ 返回设置", "menu:settings")],
    ]))


def _edit_ladder_interval(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    states.set_state(chat_id, "sys_ladder_interval")
    ui.edit(
        chat_id, message_id,
        "请输入阶梯推进最小间隔（秒，非负整数）：\n\n"
        "<i>两次阶梯推进（cooldown_until 前进一格）必须间隔 ≥ 该秒数；期间的失败仅累计计数、不推进。</i>\n"
        "<i>推荐 30；设 0 关闭该保护。</i>",
        reply_markup=ui.inline_kb([[ui.btn("❌ 取消", "sys:show:errwin")]]),
    )


def _on_ladder_interval_input(chat_id: int, text: str) -> None:
    try:
        v = int((text or "").strip())
    except ValueError:
        ui.send(chat_id, "❌ 非法数字，请重新输入：")
        return
    if v < 0 or v > 3600:
        ui.send(chat_id, "❌ 范围 0-3600，请重新输入：")
        return
    config.update(lambda c: c.__setitem__("cooldownLadderMinIntervalSeconds", v))
    states.pop_state(chat_id)
    ui.send_result(
        chat_id, f"✅ 阶梯推进最小间隔已更新为 <code>{v}s</code>",
        back_label="◀ 返回错误阶梯", back_callback="sys:show:errwin",
    )


def _edit_perm_min_age(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    states.set_state(chat_id, "sys_perm_min_age")
    ui.edit(
        chat_id, message_id,
        "请输入进入永久冷却所需的最小累计失败时长（秒，非负整数）：\n\n"
        "<i>从首次失败起持续 &lt; 该秒数时，即使阶梯推到末位 0 也不进永久，</i>"
        "<i>回退到倒数第二档（默认 errorWindows 末位前一个 = 15min）。</i>\n"
        "<i>推荐 300；设 0 关闭该保护。</i>",
        reply_markup=ui.inline_kb([[ui.btn("❌ 取消", "sys:show:errwin")]]),
    )


def _on_perm_min_age_input(chat_id: int, text: str) -> None:
    try:
        v = int((text or "").strip())
    except ValueError:
        ui.send(chat_id, "❌ 非法数字，请重新输入：")
        return
    if v < 0 or v > 86400:
        ui.send(chat_id, "❌ 范围 0-86400，请重新输入：")
        return
    config.update(lambda c: c.__setitem__("cooldownPermanentMinAgeSeconds", v))
    states.pop_state(chat_id)
    ui.send_result(
        chat_id, f"✅ 永久冷却最小累计已更新为 <code>{v}s</code>",
        back_label="◀ 返回错误阶梯", back_callback="sys:show:errwin",
    )


def _edit_errwin(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    states.set_state(chat_id, "sys_errwin")
    ui.edit(
        chat_id, message_id,
        "请输入新的错误阶梯（非负整数，以逗号分隔；末位可用 0 表示永久）。\n\n"
        "例: <code>1,3,5,10,15,0</code>",
        reply_markup=ui.inline_kb([[ui.btn("❌ 取消", "sys:show:errwin")]]),
    )


def _edit_oauth_grace(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    states.set_state(chat_id, "sys_oauth_grace")
    ui.edit(
        chat_id, message_id,
        "请输入新的 OAuth 宽容次数（非负整数）：\n\n"
        "<i>示例：3 = 前 3 次失败仅累计不冷却，第 4 次起按错误阶梯进入冷却。</i>\n"
        "<i>设 0 = 关闭宽容（与 API 渠道相同，第 1 次失败立即冷却）。</i>",
        reply_markup=ui.inline_kb([[ui.btn("❌ 取消", "sys:show:errwin")]]),
    )


def _on_oauth_grace_input(chat_id: int, text: str) -> None:
    try:
        v = int((text or "").strip())
    except ValueError:
        ui.send(chat_id, "❌ 非法数字，请重新输入：")
        return
    if v < 0 or v > 100:
        ui.send(chat_id, "❌ 范围 0-100，请重新输入：")
        return
    config.update(lambda c: c.__setitem__("oauthGraceCount", v))
    states.pop_state(chat_id)
    ui.send_result(
        chat_id, f"✅ OAuth 宽容次数已更新为 <code>{v}</code>",
        back_label="◀ 返回错误阶梯", back_callback="sys:show:errwin",
    )


def _on_errwin_input(chat_id: int, text: str) -> None:
    parts = [p.strip() for p in (text or "").split(",") if p.strip()]
    if not parts:
        ui.send(chat_id, "❌ 至少要有一个数字，请重新输入：")
        return
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        ui.send(chat_id, "❌ 非法数字，请重新输入：")
        return
    if any(n < 0 for n in nums):
        ui.send(chat_id, "❌ 数字不能为负，请重新输入：")
        return
    config.update(lambda c: c.__setitem__("errorWindows", nums))
    states.pop_state(chat_id)
    ui.send_result(
        chat_id,
        f"✅ 错误阶梯已更新：<code>{','.join(str(n) for n in nums)}</code>",
        back_label="◀ 返回系统设置", back_callback="menu:settings",
    )


# ─── 评分参数 ─────────────────────────────────────────────────────

_SCORING_FIELDS = {
    "emaAlpha":           ("EMA 平滑系数", "float", (0.0, 1.0)),
    "recentWindow":       ("滑动窗口大小", "int",   (1, 1000)),
    "errorPenaltyFactor": ("失败率惩罚倍数", "int", (0, 100)),
    "explorationRate":    ("探索率", "float", (0.0, 1.0)),
}


def _show_scoring(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    sc = config.get().get("scoring") or {}
    lines = ["🎯 <b>评分参数</b>", ""]
    rows: list[list[dict]] = []
    for k, (label, _kind, _rng) in _SCORING_FIELDS.items():
        cur = sc.get(k, "-")
        lines.append(f"{label}: <code>{cur}</code> (<code>{k}</code>)")
        rows.append([ui.btn(f"✏ 修改 {label}", f"sys:edit:scoring:{k}")])
    rows.append([ui.btn("◀ 返回设置", "menu:settings")])
    ui.edit(chat_id, message_id, "\n".join(lines), reply_markup=ui.inline_kb(rows))


def _edit_scoring(chat_id: int, message_id: int, cb_id: str, field: str) -> None:
    ui.answer_cb(cb_id)
    if field not in _SCORING_FIELDS:
        ui.send(chat_id, "❌ 未知字段")
        return
    label, kind, rng = _SCORING_FIELDS[field]
    states.set_state(chat_id, f"sys_scoring:{field}")
    ui.edit(
        chat_id, message_id,
        f"请输入 {label}（<code>{field}</code>），{kind} 类型，范围 {rng[0]}..{rng[1]}：",
        reply_markup=ui.inline_kb([[ui.btn("❌ 取消", "sys:show:scoring")]]),
    )


def _on_scoring_input(chat_id: int, action: str, text: str) -> None:
    field = action.split(":", 1)[1]
    if field not in _SCORING_FIELDS:
        ui.send(chat_id, "❌ 会话异常，请重新进入设置")
        states.pop_state(chat_id)
        return
    label, kind, rng = _SCORING_FIELDS[field]
    try:
        v = int(text.strip()) if kind == "int" else float(text.strip())
    except Exception:
        ui.send(chat_id, f"❌ 非法数字，请重新输入 {label}：")
        return
    if v < rng[0] or v > rng[1]:
        ui.send(chat_id, f"❌ 超出范围 [{rng[0]}, {rng[1]}]，请重新输入：")
        return
    config.update(lambda c: c.setdefault("scoring", {}).__setitem__(field, v))
    states.pop_state(chat_id)
    ui.send_result(
        chat_id,
        f"✅ 评分参数 {label} 已更新为 <code>{v}</code>",
        back_label="◀ 返回评分参数", back_callback="sys:show:scoring",
    )


# ─── 亲和参数 ─────────────────────────────────────────────────────

_AFFINITY_FIELDS = {
    "ttlMinutes": ("绑定 TTL（分钟）", "int",   (1, 1440)),
}


def _show_affinity(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    a = config.get().get("affinity") or {}
    lines = ["🔗 <b>亲和绑定参数</b>", ""]
    rows: list[list[dict]] = []
    for k, (label, _kind, _rng) in _AFFINITY_FIELDS.items():
        lines.append(f"{label}: <code>{a.get(k, '-')}</code> (<code>{k}</code>)")
        rows.append([ui.btn(f"✏ 修改 {label}", f"sys:edit:affinity:{k}")])
    rows.append([ui.btn("◀ 返回设置", "menu:settings")])
    ui.edit(chat_id, message_id, "\n".join(lines), reply_markup=ui.inline_kb(rows))


def _edit_affinity(chat_id: int, message_id: int, cb_id: str, field: str) -> None:
    ui.answer_cb(cb_id)
    if field not in _AFFINITY_FIELDS:
        return
    label, kind, rng = _AFFINITY_FIELDS[field]
    states.set_state(chat_id, f"sys_affinity:{field}")
    ui.edit(
        chat_id, message_id,
        f"请输入 {label}（<code>{field}</code>），{kind} 类型，范围 {rng[0]}..{rng[1]}：",
        reply_markup=ui.inline_kb([[ui.btn("❌ 取消", "sys:show:affinity")]]),
    )


def _on_affinity_input(chat_id: int, action: str, text: str) -> None:
    field = action.split(":", 1)[1]
    if field not in _AFFINITY_FIELDS:
        states.pop_state(chat_id); return
    label, kind, rng = _AFFINITY_FIELDS[field]
    try:
        v = int(text.strip()) if kind == "int" else float(text.strip())
    except Exception:
        ui.send(chat_id, f"❌ 非法数字，请重新输入 {label}：")
        return
    if v < rng[0] or v > rng[1]:
        ui.send(chat_id, f"❌ 超出范围 [{rng[0]}, {rng[1]}]，请重新输入：")
        return
    config.update(lambda c: c.setdefault("affinity", {}).__setitem__(field, v))
    states.pop_state(chat_id)
    ui.send_result(
        chat_id,
        f"✅ 亲和参数 {label} 已更新为 <code>{v}</code>",
        back_label="◀ 返回亲和参数", back_callback="sys:show:affinity",
    )


# ─── CCH 模式 ─────────────────────────────────────────────────────

_CCH_MODES = ("disabled", "dynamic")


def _show_cch(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    cfg = config.get()
    mode = cfg.get("cchMode", "disabled")
    text = (
        "🎭 <b>CCH 模式（Claude Code 伪装）</b>\n\n"
        f"当前模式: <code>{mode if mode in _CCH_MODES else 'disabled'}</code>"
        + "\n\n"
        "<b>说明：</b>\n"
        "• <code>disabled</code>：不发送 CCH 头\n"
        "• <code>dynamic</code>：对每次请求 body 计算 xxhash64 → 5 位 hex"
    )
    kb_rows = []
    for m in _CCH_MODES:
        label = f"{'✓ ' if m == mode else ''}{m}"
        kb_rows.append([ui.btn(label, f"sys:cch_set:{m}")])
    kb_rows.append([ui.btn("◀ 返回设置", "menu:settings")])
    ui.edit(chat_id, message_id, text, reply_markup=ui.inline_kb(kb_rows))


def _on_cch_set(chat_id: int, message_id: int, cb_id: str, mode: str) -> None:
    if mode not in _CCH_MODES:
        ui.answer_cb(cb_id, "无效模式")
        return
    config.update(lambda c: c.__setitem__("cchMode", mode))
    ui.answer_cb(cb_id, f"已切换到 {mode}")
    _show_cch(chat_id, message_id, "-")


# ─── 渠道选择模式 ────────────────────────────────────────────────

def _show_chsel(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    mode = config.get().get("channelSelection", "smart")
    text = (
        "🚦 <b>渠道选择模式</b>\n\n"
        f"当前: <code>{mode}</code>\n\n"
        "<b>说明：</b>\n"
        "• <code>smart</code>：按滑动窗口评分 + 20% 探索率排序\n"
        "• <code>order</code>：按 config 中渠道定义顺序（适合强制固定优先级）"
    )
    rows = []
    for m in ("smart", "order"):
        label = f"{'✓ ' if m == mode else ''}{m}"
        rows.append([ui.btn(label, f"sys:chsel_set:{m}")])
    rows.append([ui.btn("◀ 返回设置", "menu:settings")])
    ui.edit(chat_id, message_id, text, reply_markup=ui.inline_kb(rows))


def _on_chsel_set(chat_id: int, message_id: int, cb_id: str, mode: str) -> None:
    from . import load_balancing_menu
    if mode not in ("smart", "order", "priority"):
        ui.answer_cb(cb_id, "无效模式")
        return
    try:
        load_balancing.set_mode(mode)
    except Exception as exc:
        ui.answer_cb(cb_id, "切换失败")
        ui.send(chat_id, f"❌ 切换失败: <code>{ui.escape_html(str(exc))}</code>")
        return
    ui.answer_cb(cb_id, f"已切换到 {load_balancing.display_mode(mode)}")
    load_balancing_menu.show(chat_id, message_id)


# ─── OAuth 配额监控 ──────────────────────────────────────────────

def _show_quota(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    qm = config.get().get("quotaMonitor") or {}
    enabled = bool(qm.get("enabled", False))
    interval = int(qm.get("intervalSeconds", 60))
    threshold = float(qm.get("disableThresholdPercent", 95))
    text = (
        "📈 <b>OAuth 配额监控</b>\n\n"
        f"状态: <code>{'✅ 已启用' if enabled else '🚫 已停用'}</code>\n"
        f"检查间隔: <code>{interval}s</code>\n"
        f"禁用阈值: <code>{threshold:.0f}%</code>\n\n"
        "<b>说明：</b>\n"
        "• 启用后，每 N 秒拉一次每个 OAuth 账号的 usage\n"
        "• 任一指标（5h / 7d / Sonnet 7d / Opus 7d）≥ 阈值则自动禁用账号\n"
        "• resets_at 过后 + 全部指标 &lt; 阈值 → 自动恢复\n\n"
        "<i>⚠ 频繁请求 /api/oauth/usage 可能被 Anthropic 风控盯上。"
        "默认关闭；若需开启建议保持 ≥60s 间隔。</i>"
    )
    toggle_label = "🚫 停用" if enabled else "✅ 启用"
    kb_rows = [
        [ui.btn(toggle_label, "sys:quota_toggle")],
        [ui.btn("✏ 修改间隔（秒）", "sys:edit:quota_interval"),
         ui.btn("✏ 修改阈值（%）", "sys:edit:quota_threshold")],
        [ui.btn("◀ 返回设置", "menu:settings")],
    ]
    ui.edit(chat_id, message_id, text, reply_markup=ui.inline_kb(kb_rows))


def _on_quota_toggle(chat_id: int, message_id: int, cb_id: str) -> None:
    cur = bool((config.get().get("quotaMonitor") or {}).get("enabled", False))
    new_val = not cur
    config.update(lambda c: c.setdefault("quotaMonitor", {}).__setitem__("enabled", new_val))
    ui.answer_cb(cb_id, "已启用" if new_val else "已停用")
    _show_quota(chat_id, message_id, "-")


def _edit_quota_interval(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    states.set_state(chat_id, "sys_quota_interval")
    ui.edit(
        chat_id, message_id,
        "请输入配额监控间隔（秒，正整数，建议 ≥ 30）：",
        reply_markup=ui.inline_kb([[ui.btn("❌ 取消", "sys:show:quota")]]),
    )


def _on_quota_interval_input(chat_id: int, text: str) -> None:
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
    ui.send_result(
        chat_id, f"✅ 配额监控间隔已更新为 <code>{v}s</code>",
        back_label="◀ 返回配额监控", back_callback="sys:show:quota",
    )


def _edit_quota_threshold(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    states.set_state(chat_id, "sys_quota_threshold")
    ui.edit(
        chat_id, message_id,
        "请输入禁用阈值（百分比，1-100）：\n"
        "<i>任一指标到达阈值即禁用该账号。常见值：90 / 95 / 99</i>",
        reply_markup=ui.inline_kb([[ui.btn("❌ 取消", "sys:show:quota")]]),
    )


def _on_quota_threshold_input(chat_id: int, text: str) -> None:
    try:
        v = float((text or "").strip().rstrip("%"))
    except ValueError:
        ui.send(chat_id, "❌ 非法数字，请重新输入（如 95）：")
        return
    if v < 1 or v > 100:
        ui.send(chat_id, "❌ 阈值需在 1-100 之间，请重新输入：")
        return

    def _m(c):
        qm = c.setdefault("quotaMonitor", {})
        qm["disableThresholdPercent"] = v
        # resumeThreshold 未单独 UI 暴露，跟禁用阈值保持一致
        qm["resumeThresholdPercent"] = v
    config.update(_m)
    states.pop_state(chat_id)
    ui.send_result(
        chat_id, f"✅ 配额禁用阈值已更新为 <code>{v:.0f}%</code>",
        back_label="◀ 返回配额监控", back_callback="sys:show:quota",
    )


# ─── 通知设置 ────────────────────────────────────────────────────

# 事件 key → 显示名（顺序即菜单按钮顺序）
_NOTIF_EVENTS = [
    ("channel_permanent",     "🔴 渠道永久冻结"),
    ("channel_recovered",     "✅ 渠道恢复"),
    ("quota_disabled",        "⚠ 配额禁用"),
    ("quota_resumed",         "✅ 配额恢复"),
    ("oauth_refreshed",       "🔄 OAuth Token 刷新成功"),
    ("oauth_refresh_failed",  "❌ OAuth Token 刷新失败"),
    ("no_channels",           "🚨 无可用渠道告警"),
    ("openai_store_save_failed", "❌ OpenAI Store 写入失败"),
    ("network_monitor",     "🌐 网络检测失败/恢复"),
]


def _show_notif(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    notif = config.get().get("notifications") or {}
    enabled = bool(notif.get("enabled", True))
    events = notif.get("events") or {}

    text_lines = [
        "🔔 <b>通知设置</b>",
        "",
        f"总开关: <code>{'✅ 已启用' if enabled else '🚫 已停用'}</code>",
        "",
        "<b>事件分类：</b>",
    ]
    for key, label in _NOTIF_EVENTS:
        on = events.get(key, True)  # 缺省视为开
        text_lines.append(f"  {'✅' if on else '🚫'} {label}")
    text_lines.append("")
    text_lines.append("<i>点下方按钮切换。总开关关闭时所有事件都不发。</i>")

    rows: list[list[dict]] = [
        [ui.btn("🚫 关闭总开关" if enabled else "✅ 开启总开关", "sys:notif_toggle_main")],
    ]
    for key, label in _NOTIF_EVENTS:
        on = events.get(key, True)
        mark = "☑" if on else "☐"
        rows.append([ui.btn(f"{mark} {label}", f"sys:notif_toggle:{key}")])
    rows.append([ui.btn("◀ 返回设置", "menu:settings")])
    ui.edit(chat_id, message_id, "\n".join(text_lines), reply_markup=ui.inline_kb(rows))


def _on_notif_toggle_main(chat_id: int, message_id: int, cb_id: str) -> None:
    cur = bool((config.get().get("notifications") or {}).get("enabled", True))
    new_val = not cur
    config.update(lambda c: c.setdefault("notifications", {}).__setitem__("enabled", new_val))
    ui.answer_cb(cb_id, "已开启" if new_val else "已关闭")
    _show_notif(chat_id, message_id, "-")


def _on_notif_toggle_event(chat_id: int, message_id: int, cb_id: str, event_key: str) -> None:
    valid_keys = {k for k, _ in _NOTIF_EVENTS}
    if event_key not in valid_keys:
        ui.answer_cb(cb_id, "未知事件")
        return
    notif = config.get().get("notifications") or {}
    events = notif.get("events") or {}
    cur = bool(events.get(event_key, True))
    new_val = not cur

    def _m(c):
        n = c.setdefault("notifications", {})
        ev = n.setdefault("events", {})
        ev[event_key] = new_val
    config.update(_m)
    ui.answer_cb(cb_id, "已开" if new_val else "已关")
    _show_notif(chat_id, message_id, "-")


# ─── 首包黑名单 ───────────────────────────────────────────────────

def _show_blacklist(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    bl = config.get().get("contentBlacklist") or {}
    defaults = list(bl.get("default") or [])
    by_ch = bl.get("byChannel") or {}

    lines = ["🛡 <b>首包文本黑名单</b>", "", "<b>默认（对所有渠道生效）</b>:"]
    if defaults:
        for kw in defaults:
            lines.append(f"  • <code>{ui.escape_html(kw)}</code>")
    else:
        lines.append("  (无)")

    lines.append("")
    lines.append("<b>按渠道</b>:")
    if by_ch:
        for ch_name, words in by_ch.items():
            if not words:
                continue
            lines.append(f"  • <code>{ui.escape_html(ch_name)}</code>: "
                         + ", ".join(f"<code>{ui.escape_html(w)}</code>" for w in words))
    else:
        lines.append("  (无)")

    rows = [
        [ui.btn("➕ 添加默认", "sys:bl_add_default"),
         ui.btn("🗑 删除默认", "sys:bl_del_default")],
        [ui.btn("➕ 添加渠道专属", "sys:bl_add_ch")],
        [ui.btn("◀ 返回设置", "menu:settings")],
    ]
    ui.edit(chat_id, message_id, ui.truncate("\n".join(lines)), reply_markup=ui.inline_kb(rows))


def _bl_add_default(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    states.set_state(chat_id, "sys_bl_add_default")
    ui.edit(
        chat_id, message_id,
        "请输入要添加到默认黑名单的关键词（整条文本，大小写敏感）：",
        reply_markup=ui.inline_kb([[ui.btn("❌ 取消", "sys:show:blacklist")]]),
    )


def _on_bl_add_default_input(chat_id: int, text: str) -> None:
    kw = (text or "").strip()
    if not kw:
        ui.send(chat_id, "❌ 空关键词，请重新输入：")
        return
    if len(kw) > 200:
        ui.send(chat_id, "❌ 关键词过长（上限 200），请重新输入：")
        return
    def _m(c):
        bl = c.setdefault("contentBlacklist", {})
        arr = bl.setdefault("default", [])
        if kw not in arr:
            arr.append(kw)
    config.update(_m)
    states.pop_state(chat_id)
    ui.send_result(
        chat_id,
        f"✅ 已添加默认黑名单关键词: <code>{ui.escape_html(kw)}</code>",
        back_label="◀ 返回黑名单", back_callback="sys:show:blacklist",
    )


def _bl_del_default(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    defaults = list((config.get().get("contentBlacklist") or {}).get("default") or [])
    if not defaults:
        ui.edit(chat_id, message_id, "(无默认黑名单可删除)",
                reply_markup=ui.inline_kb([[ui.btn("◀ 返回", "sys:show:blacklist")]]))
        return
    rows = []
    for kw in defaults:
        short = ui.register_code("bl:d:" + kw)
        rows.append([ui.btn(f"🗑 {kw[:32]}", f"sys:bl_del_exec:{short}")])
    rows.append([ui.btn("◀ 返回", "sys:show:blacklist")])
    ui.edit(chat_id, message_id, "选择要删除的关键词：", reply_markup=ui.inline_kb(rows))


def _bl_del_exec(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    full = ui.resolve_code(short)
    if not full or not full.startswith("bl:d:"):
        ui.answer_cb(cb_id, "短码已失效")
        return
    kw = full[5:]
    def _m(c):
        arr = (c.setdefault("contentBlacklist", {})).setdefault("default", [])
        if kw in arr:
            arr.remove(kw)
    config.update(_m)
    ui.answer_cb(cb_id, "已删除")
    _show_blacklist(chat_id, message_id, "-")


def _bl_add_ch(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    states.set_state(chat_id, "sys_bl_add_ch")
    ui.edit(
        chat_id, message_id,
        "请输入 <code>渠道名=关键词</code> 格式，如：\n"
        "<code>智谱Coding Plan Max=content_policy_violation</code>",
        reply_markup=ui.inline_kb([[ui.btn("❌ 取消", "sys:show:blacklist")]]),
    )


def _on_bl_add_ch_input(chat_id: int, text: str) -> None:
    raw = (text or "").strip()
    if "=" not in raw:
        ui.send(chat_id, "❌ 格式错误：应为 <code>渠道名=关键词</code>，请重新输入：")
        return
    ch_name, kw = raw.split("=", 1)
    ch_name = ch_name.strip(); kw = kw.strip()
    if not ch_name or not kw:
        ui.send(chat_id, "❌ 渠道名或关键词为空，请重新输入：")
        return

    def _m(c):
        bl = c.setdefault("contentBlacklist", {})
        by_ch = bl.setdefault("byChannel", {})
        arr = by_ch.setdefault(ch_name, [])
        if kw not in arr:
            arr.append(kw)
    config.update(_m)
    states.pop_state(chat_id)
    ui.send_result(
        chat_id,
        f"✅ 已为渠道 <code>{ui.escape_html(ch_name)}</code> 添加关键词 "
        f"<code>{ui.escape_html(kw)}</code>",
        back_label="◀ 返回黑名单", back_callback="sys:show:blacklist",
    )


# ─── 网络设置 ───────────────────────────────────────────────────

def _network_summary() -> tuple[list[str], dict, dict, list[str]]:
    cfg = config.get()
    net = cfg.get("network") or {}
    dns_cfg = net.get("dns") or {}
    s5_cfg = net.get("socks5") or {}
    servers = list(dns_cfg.get("servers") or ["8.8.8.8"])

    # New proxy system info
    proxies = net.get("proxies") or {}
    groups = net.get("groups") or {}
    routing = net.get("routing") or {}
    default_route = routing.get("default", "direct")

    proxy_count = len(proxies)
    group_count = len(groups)
    rule_count = sum(1 for k in routing if k not in ("default",))
    rule_count += sum(len(v) for v in routing.values() if isinstance(v, dict))

    lines = [
        "🌐 <b>网络设置</b>",
        "",
        f"DNS: <code>{ui.escape_html(', '.join(str(x) for x in servers))}</code>",
        f"DNS 缓存: <code>{int(dns_cfg.get('cacheTtlSeconds', 300) or 0)}s</code>",
        "",
        f"🔀 代理: <code>{proxy_count}</code> 个",
    ]
    # Top proxy stats. Show all proxy rows so group totals are not truncated.
    from ... import log_db
    pstats = log_db.proxy_stats(limit=1000)
    pstats_by_name = {p["proxy_name"]: p for p in pstats}

    def _fmt_ms(ms):
        ms = int(ms or 0)
        return f"{ms / 1000:.1f}s" if ms >= 10000 else f"{ms}ms"

    def _fmt_bytes(n):
        n = int(n or 0)
        if n < 1024:
            return f"{n}B"
        if n < 1048576:
            return f"{n / 1024:.1f}KB"
        if n < 1073741824:
            return f"{n / 1048576:.1f}MB"
        return f"{n / 1073741824:.1f}GB"

    def _empty_stats():
        return {
            "requests": 0, "successes": 0, "failures": 0,
            "input_tokens": 0, "output_tokens": 0,
            "cache_creation_tokens": 0, "cache_read_tokens": 0,
            "total_tokens": 0, "bytes_up": 0, "bytes_down": 0, "total_bytes": 0,
            "avg_connect_ms": 0, "avg_first_byte_ms": 0, "avg_total_ms": 0,
        }

    def _merge_stats(names):
        merged = _empty_stats()
        weighted = {"connect": 0, "first": 0, "total": 0}
        count = 0
        for name in names:
            if name == "direct":
                continue
            ps = pstats_by_name.get(name)
            if not ps:
                continue
            req = int(ps.get("requests") or 0)
            merged["requests"] += req
            merged["successes"] += int(ps.get("successes") or 0)
            merged["failures"] += int(ps.get("failures") or 0)
            for k in ("input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens", "total_tokens", "bytes_up", "bytes_down", "total_bytes"):
                merged[k] += int(ps.get(k) or 0)
            weighted["connect"] += int(ps.get("avg_connect_ms") or 0) * req
            weighted["first"] += int(ps.get("avg_first_byte_ms") or 0) * req
            weighted["total"] += int(ps.get("avg_total_ms") or 0) * req
            count += req
        if count:
            merged["avg_connect_ms"] = round(weighted["connect"] / count)
            merged["avg_first_byte_ms"] = round(weighted["first"] / count)
            merged["avg_total_ms"] = round(weighted["total"] / count)
        return merged

    def _append_stat_lines(prefix: str, ps: dict) -> None:
        tok = int(ps.get("total_tokens") or 0)
        traffic = int(ps.get("total_bytes") or 0)
        lines.append(
            f"{prefix}📊 <code>{int(ps.get('requests') or 0)}</code>次"
            f" · ✅<code>{int(ps.get('successes') or 0)}</code> / ❌<code>{int(ps.get('failures') or 0)}</code>"
        )
        lines.append(
            f"{prefix}🧮 <code>{ui.fmt_tokens(tok)}</code> tok"
            f" · 📦 <code>{_fmt_bytes(traffic)}</code>"
        )
        lines.append(
            f"{prefix}⏱ 连接 <code>{_fmt_ms(ps.get('avg_connect_ms'))}</code>"
            f" · 首字 <code>{_fmt_ms(ps.get('avg_first_byte_ms'))}</code>"
            f" · 总耗时 <code>{_fmt_ms(ps.get('avg_total_ms'))}</code>"
        )

    if pstats:
        for ps in pstats[:5]:
            lines.append(f"  • <code>{ui.escape_html(ps['proxy_name'])}</code>")
            _append_stat_lines("    ", ps)
    lines.append("")
    lines.append(f"📋 代理组: <code>{group_count}</code> 个")
    for gname, members in list(groups.items())[:5]:
        merged = _merge_stats(members)
        member_text = " → ".join(str(m) for m in members)
        lines.append(f"  • <code>{ui.escape_html(gname)}</code>  <code>{ui.escape_html(member_text)}</code>")
        if merged["requests"] > 0:
            lines.append("    组内总计：")
            _append_stat_lines("      ", merged)
    lines.append("")
    lines.append(f"🎯 默认路由: <code>{ui.escape_html(str(default_route))}</code>")
    lines.append(f"📝 路由规则: <code>{rule_count}</code> 条")
    if not proxies and not groups:
        lines.append("")
        lines.append("<i>未配置代理，所有出站请求直连。</i>")
    return lines, dns_cfg, s5_cfg, servers


def _show_network(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    lines, _dns_cfg, s5_cfg, _servers = _network_summary()
    s5_url = str(s5_cfg.get("url") or "").strip()
    s5_enabled = bool(s5_cfg.get("enabled")) and bool(s5_url)
    rows = [
        [ui.btn("✏ 修改 DNS", "sys:net:edit_dns"),
         ui.btn("🔄 同步系统 DNS", "sys:net:sync_dns"),
         ui.btn("📦 DNS 缓存", "sys:net:dns_cache")],
        [ui.btn("🔀 代理管理", "px:show"),
         ui.btn("📋 代理组", "px:groups"),
         ui.btn("🎯 路由规则", "px:routing")],
        [ui.btn("🩺 网络检测", "sys:mon:show"),
         ui.btn("🏠 返回主菜单", "menu:main"),
         ui.btn("◀ 返回设置", "menu:settings")],
    ]
    ui.edit(chat_id, message_id, "\n".join(lines), reply_markup=ui.inline_kb(rows))


def _show_dns_cache(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    try:
        entries = network.dns_cache_entries()
    except Exception as exc:
        ui.edit(
            chat_id, message_id,
            f"❌ 读取 DNS 缓存失败：<code>{ui.escape_html(exc)}</code>",
            reply_markup=ui.inline_kb([
                [ui.btn("🏠 返回主菜单", "menu:main"),
                 ui.btn("◀ 返回网络设置", "sys:show:network")],
            ]),
        )
        return
    ttl = 0
    try:
        ttl = int((config.get().get("network", {}).get("dns", {}) or {}).get("cacheTtlSeconds", 300) or 0)
    except Exception:
        ttl = 0
    lines = [
        "📦 <b>DNS 缓存</b>",
        "",
        f"全局 TTL: <code>{ttl}s</code> · 当前条目: <code>{len(entries)}</code>",
        "",
    ]
    if not entries:
        lines.append("<i>当前缓存为空。</i>")
    else:
        # 限制显示数量避免消息超长
        shown = entries[:50]
        for ent in shown:
            host = ui.escape_html(ent["host"])
            ips_str = ui.escape_html(", ".join(ent["ips"]))
            lines.append(f"• <code>{host}</code> → <code>{ips_str}</code>")
            lines.append(f"   剩余 <code>{ent['ttl_remaining_seconds']}s</code>")
        if len(entries) > len(shown):
            lines.append(f"<i>... 还有 {len(entries) - len(shown)} 条未显示</i>")
    rows = [
        [ui.btn("🧹 清除全部缓存", "sys:net:dns_cache_clear"),
         ui.btn("🔄 刷新", "sys:net:dns_cache")],
        [ui.btn("🏠 返回主菜单", "menu:main"),
         ui.btn("◀ 返回网络设置", "sys:show:network")],
    ]
    ui.edit(chat_id, message_id, ui.truncate("\n".join(lines)), reply_markup=ui.inline_kb(rows))


def _clear_dns_cache(chat_id: int, message_id: int, cb_id: str) -> None:
    try:
        network.clear_dns_cache()
    except Exception as exc:
        ui.answer_cb(cb_id, "清除失败", show_alert=True)
        ui.send(chat_id, f"❌ 清除 DNS 缓存失败：<code>{ui.escape_html(exc)}</code>")
        return
    ui.answer_cb(cb_id, "已清除")
    _show_dns_cache(chat_id, message_id, cb_id)



def _edit_dns(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    servers = network.dns_servers()
    states.set_state(chat_id, "sys_net_dns")
    ui.edit(
        chat_id, message_id,
        "修改 DNS\n\n"
        f"当前 DNS 为：<code>{ui.escape_html(', '.join(servers))}</code>\n"
        "请输入您的 DNS\n"
        "支持单个或多个，多个用逗号分隔。DNS 服务器如果填写域名，服务器域名本身会直接用系统 DNS 解析，避免套娃。\n\n"
        "例：\n"
        "<code>1.1.1.1</code> 或 <code>1.1.1.1,8.8.8.8</code>\n"
        "<code>dot://1.1.1.1:853?hostname=cloudflare-dns.com</code>\n"
        "<code>https://dns.google/dns-query</code>",
        reply_markup=ui.inline_kb([
            [ui.btn("🏠 返回主菜单", "menu:main"), ui.btn("❌ 取消", "sys:show:network")],
        ]),
    )


def _on_dns_input(chat_id: int, text: str) -> None:
    try:
        servers = network.parse_dns_input(text)
    except ValueError as exc:
        ui.send(chat_id, f"❌ {ui.escape_html(exc)}，请重新输入：")
        return

    progress = ui.send(chat_id, "正在检测 DNS 网络访问情况：\n\n请稍候...")
    msg_id = ((progress or {}).get("result") or {}).get("message_id")
    try:
        test = network.test_dns_servers(servers)
    except Exception as exc:
        ui.send(chat_id, f"❌ DNS 检测异常：<code>{ui.escape_html(exc)}</code>")
        return

    state_data = network.dumps_state({"servers": servers, "test": test})
    states.set_state(chat_id, "sys_net_dns_confirm", state_data)
    ok = bool(test.get("ok"))
    text_out = network.dns_test_text(test) + "\n\n" + (
        "是否立即保存？" if ok else "是否仍然保存？"
    )
    rows = [
        [ui.btn("✅ 保存", "sys:net:dns_save")],
    ]
    if not ok:
        rows = [[ui.btn("⚠️ 强制保存", "sys:net:dns_save_force")]]
    rows.append([ui.btn("❌ 取消", "sys:show:network")])
    if msg_id:
        ui.edit(chat_id, msg_id, ui.escape_html(text_out), reply_markup=ui.inline_kb(rows), parse_mode="HTML")
    else:
        ui.send(chat_id, ui.escape_html(text_out), reply_markup=ui.inline_kb(rows))


def _save_dns_confirm(chat_id: int, message_id: int, cb_id: str, *, force: bool) -> None:
    st = states.get_state(chat_id) or {}
    data = st.get("data") or {}
    servers = data.get("servers") or []
    test = data.get("test") or {}
    if st.get("action") != "sys_net_dns_confirm" or not servers:
        ui.answer_cb(cb_id, "确认状态已过期")
        return
    if not test.get("ok") and not force:
        ui.answer_cb(cb_id, "检测未通过，请用强制保存或取消", show_alert=True)
        return
    try:
        network.save_dns_servers(list(servers))
    except Exception as exc:
        ui.answer_cb(cb_id, "保存失败", show_alert=True)
        ui.send(chat_id, f"❌ DNS 保存失败：<code>{ui.escape_html(exc)}</code>")
        return
    states.pop_state(chat_id)
    ui.answer_cb(cb_id, "已保存")
    ui.edit(
        chat_id, message_id,
        f"✅ DNS 已保存为：<code>{ui.escape_html(', '.join(str(x) for x in servers))}</code>",
        reply_markup=ui.inline_kb([[ui.btn("◀ 返回网络设置", "sys:show:network")]]),
    )
    if force:
        warn = network.failure_warning("dns", test)
        if warn:
            ui.send(chat_id, ui.escape_html(warn))


def _sync_dns(chat_id: int, message_id: int, cb_id: str) -> None:
    try:
        servers = network.sync_system_dns_now()
    except Exception as exc:
        ui.answer_cb(cb_id, "同步失败", show_alert=True)
        ui.send(chat_id, f"❌ 同步系统 DNS 失败：<code>{ui.escape_html(exc)}</code>")
        return
    ui.answer_cb(cb_id, "已同步")
    ui.edit(
        chat_id, message_id,
        f"✅ 已同步系统 DNS：<code>{ui.escape_html(', '.join(servers))}</code>",
        reply_markup=ui.inline_kb([[ui.btn("◀ 返回网络设置", "sys:show:network")]]),
    )


def _edit_socks5(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    s5 = network.socks5_cfg()
    cur = str(s5.get("url") or "").strip()
    states.set_state(chat_id, "sys_net_socks5")
    ui.edit(
        chat_id, message_id,
        "设置 SOCKS5 代理\n\n"
        f"当前代理：<code>{ui.escape_html(network.mask_url(cur)) if cur else '未设置'}</code>\n"
        "请输入 SOCKS5 地址\n\n"
        "支持：\n"
        "<code>socks5://127.0.0.1:1080</code>\n"
        "<code>tcp://127.0.0.1:1080</code>\n"
        "<code>127.0.0.1:1080</code>\n"
        "<code>socks5://user:pass@proxy.example.com:1080</code>",
        reply_markup=ui.inline_kb([
            [ui.btn("🏠 返回主菜单", "menu:main"), ui.btn("❌ 取消", "sys:show:network")],
        ]),
    )


def _on_socks5_input(chat_id: int, text: str) -> None:
    try:
        norm = network.normalize_socks5_url(text)
    except ValueError as exc:
        ui.send(chat_id, f"❌ {ui.escape_html(exc)}，请重新输入：")
        return
    progress = ui.send(chat_id, "正在检测 SOCKS5 网络访问情况：\n\n请稍候...")
    msg_id = ((progress or {}).get("result") or {}).get("message_id")
    try:
        test = asyncio.run(network.test_socks5(norm.url))
    except Exception as exc:
        ui.send(chat_id, f"❌ SOCKS5 检测异常：<code>{ui.escape_html(exc)}</code>")
        return
    state_data = network.dumps_state({"url": norm.url, "test": test})
    states.set_state(chat_id, "sys_net_socks5_confirm", state_data)
    ok = bool(test.get("ok"))
    text_out = network.socks5_test_text(test) + "\n\n" + (
        "是否立即保存并启用？" if ok else "是否仍然保存并启用？"
    )
    rows = [[ui.btn("✅ 保存并启用", "sys:net:socks5_save")]]
    if not ok:
        rows = [[ui.btn("⚠️ 强制保存并启用", "sys:net:socks5_save_force")]]
    rows.append([ui.btn("❌ 取消", "sys:show:network")])
    if msg_id:
        ui.edit(chat_id, msg_id, ui.escape_html(text_out), reply_markup=ui.inline_kb(rows))
    else:
        ui.send(chat_id, ui.escape_html(text_out), reply_markup=ui.inline_kb(rows))


def _save_socks5_confirm(chat_id: int, message_id: int, cb_id: str, *, force: bool) -> None:
    st = states.get_state(chat_id) or {}
    data = st.get("data") or {}
    url = str(data.get("url") or "")
    test = data.get("test") or {}
    if st.get("action") != "sys_net_socks5_confirm" or not url:
        ui.answer_cb(cb_id, "确认状态已过期")
        return
    if not test.get("ok") and not force:
        ui.answer_cb(cb_id, "检测未通过，请用强制保存或取消", show_alert=True)
        return
    try:
        saved = network.save_socks5(url, enabled=True)
    except Exception as exc:
        ui.answer_cb(cb_id, "保存失败", show_alert=True)
        ui.send(chat_id, f"❌ SOCKS5 保存失败：<code>{ui.escape_html(exc)}</code>")
        return
    states.pop_state(chat_id)
    ui.answer_cb(cb_id, "已保存并启用")
    ui.edit(
        chat_id, message_id,
        f"✅ SOCKS5 已保存并启用：<code>{ui.escape_html(network.mask_url(saved))}</code>",
        reply_markup=ui.inline_kb([[ui.btn("◀ 返回网络设置", "sys:show:network")]]),
    )
    if force:
        warn = network.failure_warning("socks5", test)
        if warn:
            ui.send(chat_id, ui.escape_html(warn))


def _toggle_socks5(chat_id: int, message_id: int, cb_id: str) -> None:
    s5 = network.socks5_cfg()
    url = str(s5.get("url") or "").strip()
    enabled = bool(s5.get("enabled")) and bool(url)
    if not enabled and not url:
        ui.answer_cb(cb_id, "请先设置 SOCKS5 地址", show_alert=True)
        _edit_socks5(chat_id, message_id, "-")
        return
    network.set_socks5_enabled(not enabled)
    ui.answer_cb(cb_id, "已启用" if not enabled else "已关闭")
    _show_network(chat_id, message_id, "-")


# ─── 网络检测 ───────────────────────────────────────────────────

def _mon_cfg() -> dict:
    return network_monitor.cfg()


def _mon_on(v: bool) -> str:
    return "✅ 开" if v else "🚫 关"


def _mon_core_label(key: str, *, rich: bool = False) -> str:
    if key == "openai":
        return ui.provider_tag("openai") if rich else "OpenAI"
    if key == "claude":
        return ui.provider_tag("claude") if rich else "Claude"
    if key == "cloudflare":
        return "Cloudflare"
    return ui.escape_html(key) if rich else key


def _mon_last_lines(limit: int = 12) -> list[str]:
    rows = state_db.network_check_load_all()
    if not rows:
        return ["<i>暂无检测记录。</i>"]
    out: list[str] = []
    for r in rows[:limit]:
        ok = bool(r.get("ok"))
        icon = "✅" if ok else "❌"
        label = ui.escape_html(r.get("label") or r.get("key") or "?")
        detail = ui.escape_html(r.get("detail") or "")
        lat = r.get("latency_ms")
        lat_s = f" · {lat}ms" if lat is not None else ""
        ts = int((r.get("checked_at") or 0) / 1000)
        ts_s = ui.fmt_bjt_ts(ts, "%H:%M:%S") if ts else "?"
        line = f"{icon} {label}{lat_s} · {ts_s}"
        if detail and not ok:
            line += f"\n  <code>{detail[:160]}</code>"
        out.append(line)
    if len(rows) > limit:
        out.append(f"<i>... 还有 {len(rows) - limit} 条</i>")
    return out


def _show_monitor(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    c = _mon_cfg()
    ch_cfg = c.get("channels") or {}
    core = c.get("core") or {}
    failures = network_monitor.active_failures()
    lines = [
        "🩺 <b>网络检测</b>",
        "",
        f"总开关: <code>{_mon_on(bool(c.get('enabled', True)))}</code>",
        f"检测间隔: <code>{int(c.get('intervalSeconds', 60))}s</code>（最小 5s）",
        f"当前异常: <code>{len(failures)}</code> 项",
        "",
        f"DNS 检测: <code>{_mon_on(bool(c.get('dns')))}</code>",
        f"SOCKS5 检测: <code>{_mon_on(bool(c.get('socks5')))}</code>",
        f"渠道连接性: <code>{_mon_on(bool(ch_cfg.get('enabled')))}</code> · 已选 {len(network_monitor.enabled_channel_keys())} 个",
        "核心上游: " + " · ".join(
            f"{_mon_core_label(k, rich=True)} {'✅' if core.get(k) else '🚫'}"
            for k in ("openai", "claude", "cloudflare")
        ),
        "",
        "<b>最近状态</b>",
        *_mon_last_lines(),
    ]
    rows = [
        [ui.btn("🔴 关闭总开关" if c.get("enabled", True) else "🟢 开启总开关", "sys:mon:toggle:enabled")],
        [ui.btn("✏ 修改间隔", "sys:mon:edit_interval"),
         ui.btn("▶ 立即检测", "sys:mon:run_now")],
        [ui.btn("DNS " + ("关" if c.get("dns") else "开"), "sys:mon:toggle:dns"),
         ui.btn("SOCKS5 " + ("关" if c.get("socks5") else "开"), "sys:mon:toggle:socks5")],
        [ui.btn("核心上游", "sys:mon:core"),
         ui.btn("渠道检测", "sys:mon:channels")],
        [ui.btn("◀ 返回网络设置", "sys:show:network")],
    ]
    ui.edit(chat_id, message_id, ui.truncate("\n".join(lines)), reply_markup=ui.inline_kb(rows))


def _mon_toggle(chat_id: int, message_id: int, cb_id: str, key: str) -> None:
    c = _mon_cfg()
    if key == "enabled":
        cur = bool(c.get("enabled", True))
        network_monitor.update_settings(lambda m: m.__setitem__("enabled", not cur))
    elif key in ("dns", "socks5"):
        cur = bool(c.get(key, False))
        network_monitor.update_settings(lambda m: m.__setitem__(key, not cur))
    else:
        ui.answer_cb(cb_id, "未知开关")
        return
    ui.answer_cb(cb_id, "已切换")
    _show_monitor(chat_id, message_id, "-")


def _edit_monitor_interval(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    states.set_state(chat_id, "sys_mon_interval")
    cur = int(_mon_cfg().get("intervalSeconds", 60))
    ui.edit(
        chat_id, message_id,
        f"当前检测间隔：<code>{cur}s</code>\n\n请输入新的检测间隔（秒，至少 5）：",
        reply_markup=ui.inline_kb([[ui.btn("❌ 取消", "sys:mon:show")]]),
    )


def _on_monitor_interval_input(chat_id: int, text: str) -> None:
    try:
        v = int((text or "").strip())
    except ValueError:
        ui.send(chat_id, "❌ 请输入整数秒数：")
        return
    if v < 5:
        ui.send(chat_id, "❌ 检测间隔至少 5 秒，请重新输入：")
        return
    network_monitor.update_settings(lambda m: m.__setitem__("intervalSeconds", v))
    states.pop_state(chat_id)
    ui.send_result(chat_id, f"✅ 网络检测间隔已更新为 <code>{v}s</code>", back_label="◀ 返回网络检测", back_callback="sys:mon:show")


def _show_monitor_core(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    core = _mon_cfg().get("core") or {}
    lines = ["🧭 <b>核心上游检测</b>", ""]
    for k in ("openai", "claude", "cloudflare"):
        lines.append(f"{_mon_core_label(k, rich=True)}: <code>{_mon_on(bool(core.get(k)))}</code>")
    rows = [
        [ui.btn(ui.provider_icon("openai") + " OpenAI " + ("关" if core.get("openai") else "开"), "sys:mon:core_toggle:openai")],
        [ui.btn(ui.provider_icon("claude") + " Claude " + ("关" if core.get("claude") else "开"), "sys:mon:core_toggle:claude")],
        [ui.btn("Cloudflare " + ("关" if core.get("cloudflare") else "开"), "sys:mon:core_toggle:cloudflare")],
        [ui.btn("◀ 返回网络检测", "sys:mon:show")],
    ]
    ui.edit(chat_id, message_id, "\n".join(lines), reply_markup=ui.inline_kb(rows))


def _mon_core_toggle(chat_id: int, message_id: int, cb_id: str, key: str) -> None:
    if key not in ("openai", "claude", "cloudflare"):
        ui.answer_cb(cb_id, "未知核心上游")
        return
    cur = bool((_mon_cfg().get("core") or {}).get(key))
    def _m(mon: dict) -> None:
        mon.setdefault("core", {})[key] = not cur
    network_monitor.update_settings(_m)
    ui.answer_cb(cb_id, "已切换")
    _show_monitor_core(chat_id, message_id, "-")


def _show_monitor_channels(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    c = _mon_cfg()
    ch_cfg = c.get("channels") or {}
    total_on = bool(ch_cfg.get("enabled", False))
    by_key = ch_cfg.get("byKey") or {}
    # 只显示 API 类型渠道；OAuth 走专门的 OAuth 状态监控，不在网络检测里
    channels = [ch for ch in registry.all_channels() if getattr(ch, "type", "") == "api"]
    lines = [
        "🔌 <b>渠道连接性检测</b>",
        "",
        f"总开关: <code>{_mon_on(total_on)}</code>",
        "<i>仅做 API 渠道 TCP 连接性检测（不含 OAuth），不消耗 token。</i>",
        "",
    ]
    rows = [[ui.btn("🔴 关闭渠道检测" if total_on else "🟢 开启渠道检测", "sys:mon:channels_toggle")]]
    if channels:
        for ch in channels[:30]:
            on = bool(by_key.get(ch.key, False))
            label = ui.escape_html(getattr(ch, "display_name", ch.key))
            # 家族 badge: · 🅰 Anthropic / 🅾 OpenAI
            try:
                fam = load_balancing.family_for_channel(ch)
            except Exception:
                fam = ""
            fam_tag = ui.family_tag(fam) if fam else ""
            # 第二行展示该渠道的探测 URL，便于一眼判断打的是哪个上游
            try:
                probe_url = network_monitor._channel_probe_url(ch)
            except Exception:
                probe_url = ""
            head = f"{'✅' if on else '🚫'} {label}"
            if fam_tag:
                head += f" · {fam_tag}"
            lines.append(head)
            if probe_url:
                lines.append(f"   <code>{ui.escape_html(probe_url)}</code>")
            code = ui.register_code("monch:" + ch.key)
            rows.append([ui.btn(("☑ " if on else "☐ ") + getattr(ch, "display_name", ch.key)[:42], f"sys:mon:ch_toggle:{code}")])
        if len(channels) > 30:
            lines.append(f"<i>... 还有 {len(channels) - 30} 个渠道未显示</i>")
    else:
        lines.append("<i>暂无渠道。</i>")
    rows.append([ui.btn("◀ 返回网络检测", "sys:mon:show")])
    ui.edit(chat_id, message_id, ui.truncate("\n".join(lines)), reply_markup=ui.inline_kb(rows))


def _mon_channels_toggle(chat_id: int, message_id: int, cb_id: str) -> None:
    cur = bool((_mon_cfg().get("channels") or {}).get("enabled", False))
    def _m(mon: dict) -> None:
        mon.setdefault("channels", {"enabled": False, "byKey": {}})["enabled"] = not cur
    network_monitor.update_settings(_m)
    ui.answer_cb(cb_id, "已切换")
    _show_monitor_channels(chat_id, message_id, "-")


def _mon_channel_toggle(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    full = ui.resolve_code(short)
    if not full or not full.startswith("monch:"):
        ui.answer_cb(cb_id, "短码已失效")
        return
    key = full[len("monch:"):]
    cur = network_monitor.channel_enabled(key)
    network_monitor.set_channel_enabled(key, not cur)
    ui.answer_cb(cb_id, "已切换")
    _show_monitor_channels(chat_id, message_id, "-")


def _run_monitor_now(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id, "开始检测")
    ui.edit(chat_id, message_id, "🩺 正在执行网络检测，请稍候...")
    try:
        results = asyncio.run(network_monitor.run_once(save=True))
    except Exception as exc:
        ui.edit(chat_id, message_id, f"❌ 网络检测异常：<code>{ui.escape_html(exc)}</code>", reply_markup=ui.inline_kb([[ui.btn("◀ 返回网络检测", "sys:mon:show")]]))
        return
    ui.edit(chat_id, message_id, ui.truncate(network_monitor.format_results(results)), reply_markup=ui.inline_kb([[ui.btn("◀ 返回网络检测", "sys:mon:show")]]))


# ─── 路由 ─────────────────────────────────────────────────────────

def handle_callback(chat_id: int, message_id: int, cb_id: str, data: str) -> bool:
    if data == "menu:settings":
        show(chat_id, message_id, cb_id); return True

    if data == "sys:show:timeouts":  _show_timeouts(chat_id, message_id, cb_id); return True
    if data == "sys:edit:timeouts":  _edit_timeouts(chat_id, message_id, cb_id); return True
    if data == "sys:show:errwin":    _show_errwin(chat_id, message_id, cb_id); return True
    if data == "sys:edit:errwin":    _edit_errwin(chat_id, message_id, cb_id); return True
    if data == "sys:edit:oauth_grace": _edit_oauth_grace(chat_id, message_id, cb_id); return True
    if data == "sys:edit:ladder_interval": _edit_ladder_interval(chat_id, message_id, cb_id); return True
    if data == "sys:edit:perm_min_age": _edit_perm_min_age(chat_id, message_id, cb_id); return True
    if data == "sys:show:scoring":   _show_scoring(chat_id, message_id, cb_id); return True
    if data.startswith("sys:edit:scoring:"):
        _edit_scoring(chat_id, message_id, cb_id, data.split(":", 3)[3]); return True
    if data == "sys:show:affinity":  _show_affinity(chat_id, message_id, cb_id); return True
    if data.startswith("sys:edit:affinity:"):
        _edit_affinity(chat_id, message_id, cb_id, data.split(":", 3)[3]); return True
    # 旧入口保留为跳转，渠道选择模式已迁移到「负载均衡」。
    if data == "sys:show:chsel":
        ui.answer_cb(cb_id, "已迁移到负载均衡")
        from . import load_balancing_menu
        load_balancing_menu.show(chat_id, message_id)
        return True
    if data.startswith("sys:chsel_set:"):
        ui.answer_cb(cb_id, "已迁移到负载均衡")
        return True

    # 网络设置
    if data == "sys:show:network":        _show_network(chat_id, message_id, cb_id); return True
    if data == "sys:net:edit_dns":        _edit_dns(chat_id, message_id, cb_id); return True
    if data == "sys:net:sync_dns":        _sync_dns(chat_id, message_id, cb_id); return True
    if data == "sys:net:dns_save":        _save_dns_confirm(chat_id, message_id, cb_id, force=False); return True
    if data == "sys:net:dns_save_force":  _save_dns_confirm(chat_id, message_id, cb_id, force=True); return True
    if data == "sys:net:edit_socks5":     _edit_socks5(chat_id, message_id, cb_id); return True
    if data == "sys:net:socks5_save":     _save_socks5_confirm(chat_id, message_id, cb_id, force=False); return True
    if data == "sys:net:socks5_save_force": _save_socks5_confirm(chat_id, message_id, cb_id, force=True); return True
    if data == "sys:net:toggle_socks5":   _toggle_socks5(chat_id, message_id, cb_id); return True
    if data == "sys:net:dns_cache":       _show_dns_cache(chat_id, message_id, cb_id); return True
    if data == "sys:net:dns_cache_clear": _clear_dns_cache(chat_id, message_id, cb_id); return True

    # 网络检测
    if data == "sys:mon:show":            _show_monitor(chat_id, message_id, cb_id); return True
    if data.startswith("sys:mon:toggle:"):
        _mon_toggle(chat_id, message_id, cb_id, data.split(":", 3)[3]); return True
    if data == "sys:mon:edit_interval":   _edit_monitor_interval(chat_id, message_id, cb_id); return True
    if data == "sys:mon:run_now":         _run_monitor_now(chat_id, message_id, cb_id); return True
    if data == "sys:mon:core":            _show_monitor_core(chat_id, message_id, cb_id); return True
    if data.startswith("sys:mon:core_toggle:"):
        _mon_core_toggle(chat_id, message_id, cb_id, data.split(":", 3)[3]); return True
    if data == "sys:mon:channels":        _show_monitor_channels(chat_id, message_id, cb_id); return True
    if data == "sys:mon:channels_toggle": _mon_channels_toggle(chat_id, message_id, cb_id); return True
    if data.startswith("sys:mon:ch_toggle:"):
        _mon_channel_toggle(chat_id, message_id, cb_id, data.split(":", 3)[3]); return True

    # 通知设置
    if data == "sys:show:notif":          _show_notif(chat_id, message_id, cb_id); return True
    if data == "sys:notif_toggle_main":   _on_notif_toggle_main(chat_id, message_id, cb_id); return True
    if data.startswith("sys:notif_toggle:"):
        _on_notif_toggle_event(chat_id, message_id, cb_id, data.split(":", 2)[2]); return True

    # 黑名单
    if data == "sys:show:blacklist": _show_blacklist(chat_id, message_id, cb_id); return True
    if data == "sys:bl_add_default": _bl_add_default(chat_id, message_id, cb_id); return True
    if data == "sys:bl_del_default": _bl_del_default(chat_id, message_id, cb_id); return True
    if data.startswith("sys:bl_del_exec:"):
        _bl_del_exec(chat_id, message_id, cb_id, data.split(":", 2)[2]); return True
    if data == "sys:bl_add_ch":      _bl_add_ch(chat_id, message_id, cb_id); return True

    # 并发限制
    if data == "sys:show:concurrency":        _show_concurrency(chat_id, message_id, cb_id); return True
    if data == "sys:cc_toggle":               _on_cc_toggle(chat_id, message_id, cb_id); return True
    if data == "sys:edit:cc_queue_wait":      _edit_cc_queue_wait(chat_id, message_id, cb_id); return True
    if data == "sys:edit:cc_default_max":     _edit_cc_default_max(chat_id, message_id, cb_id); return True

    # API Key 限流
    if data == "sys:show:aklim":              _show_aklim(chat_id, message_id, cb_id); return True
    if data == "sys:aklim_toggle":            _on_aklim_toggle(chat_id, message_id, cb_id); return True
    if data == "sys:edit:aklim_max":          _edit_aklim(chat_id, message_id, cb_id, "max"); return True
    if data == "sys:edit:aklim_queue":        _edit_aklim(chat_id, message_id, cb_id, "queue"); return True
    if data == "sys:edit:aklim_wait":         _edit_aklim(chat_id, message_id, cb_id, "wait"); return True

    # WS 模式
    if data == "sys:show:ws_mode":            _show_ws_mode(chat_id, message_id, cb_id); return True
    if data == "sys:ws_mode:toggle":          _on_ws_mode_toggle(chat_id, message_id, cb_id); return True

    return False


def handle_text_state(chat_id: int, action: str, text: str) -> bool:
    if action == "sys_timeouts":
        _on_timeouts_input(chat_id, text); return True
    if action == "sys_errwin":
        _on_errwin_input(chat_id, text); return True
    if action == "sys_oauth_grace":
        _on_oauth_grace_input(chat_id, text); return True
    if action == "sys_ladder_interval":
        _on_ladder_interval_input(chat_id, text); return True
    if action == "sys_perm_min_age":
        _on_perm_min_age_input(chat_id, text); return True
    if action.startswith("sys_scoring:"):
        _on_scoring_input(chat_id, action, text); return True
    if action.startswith("sys_affinity:"):
        _on_affinity_input(chat_id, action, text); return True
    if action == "sys_bl_add_default":
        _on_bl_add_default_input(chat_id, text); return True
    if action == "sys_bl_add_ch":
        _on_bl_add_ch_input(chat_id, text); return True
    if action == "sys_net_dns":
        _on_dns_input(chat_id, text); return True
    if action == "sys_net_socks5":
        _on_socks5_input(chat_id, text); return True
    if action == "sys_mon_interval":
        _on_monitor_interval_input(chat_id, text); return True
    if action == "sys_cc_queue_wait":
        _on_cc_queue_wait_input(chat_id, text); return True
    if action == "sys_cc_default_max":
        _on_cc_default_max_input(chat_id, text); return True
    if action.startswith("sys_aklim_"):
        _on_aklim_input(chat_id, action, text); return True
    return False


# ─── 并发限制 ─────────────────────────────────────────────────────

def _show_concurrency(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    cfg = config.get()
    cc_cfg = cfg.get("concurrency") or {}
    enabled = bool(cc_cfg.get("enabled", True))
    queue_wait = int(cc_cfg.get("queueWaitSeconds", 30))
    default_max = int(cc_cfg.get("defaultMaxConcurrent", 0))

    totals = concurrency.totals()
    snap = concurrency.snapshot()

    lines = [
        "⚡ <b>渠道并发限制</b>",
        "",
        f"总开关: <code>{'开' if enabled else '关'}</code>",
        f"队列等待: <code>{queue_wait}s</code>（全满时最长排队，超时返回 429）",
        f"默认上限: <code>{default_max if default_max > 0 else '不限'}</code>"
        " （渠道未配 <code>maxConcurrent</code> 时用这个）",
        "",
        f"当前在途: <b>{totals['in_flight']}</b> · 排队中: <b>{totals['waiting']}</b>"
        f" · 追踪渠道: {totals['tracked_channels']}",
    ]

    if snap:
        lines.append("")
        lines.append("📊 <b>渠道分布</b>")
        for row in snap[:20]:  # 最多展示 20 条，避免超长
            ck = row["channel_key"]
            inf = row["in_flight"]
            mx = row["max_concurrent"]
            wt = row["waiting"]
            if row["unlimited"]:
                usage = f"{inf}/∞"
            else:
                usage = f"{inf}/{mx}"
                if inf >= mx and mx > 0:
                    usage = "🔴 " + usage
                elif mx > 0 and inf >= mx * 0.8:
                    usage = "🟡 " + usage
            wait_s = f" · 排队 {wt}" if wt > 0 else ""
            lines.append(f"  <code>{ui.escape_html(ck)}</code> · {usage}{wait_s}")
        if len(snap) > 20:
            lines.append(f"  <i>...（还有 {len(snap) - 20} 个未列出）</i>")
    else:
        lines.append("")
        lines.append("<i>暂无活跃渠道记录（服务启动后还没处理过请求）。</i>")

    lines.append("")
    lines.append("<i>提示：每个渠道的 <code>maxConcurrent</code> 在「🔀 渠道管理」"
                 "或「🔐 管理 OAuth」对应渠道的详情页设置。</i>")

    kb = ui.inline_kb([
        [ui.btn(
            "🔴 关闭并发限制" if enabled else "🟢 开启并发限制",
            "sys:cc_toggle",
        )],
        [ui.btn("✏ 修改队列等待", "sys:edit:cc_queue_wait"),
         ui.btn("✏ 修改默认上限", "sys:edit:cc_default_max")],
        [ui.btn("🔄 刷新", "sys:show:concurrency")],
        [ui.btn("◀ 返回设置", "menu:settings")],
    ])
    ui.edit(chat_id, message_id, "\n".join(lines), reply_markup=kb)


def _on_cc_toggle(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id, "已切换")
    cfg = config.get()
    cur = bool((cfg.get("concurrency") or {}).get("enabled", True))
    new_val = not cur
    def _mut(c):
        c.setdefault("concurrency", {})["enabled"] = new_val
    config.update(_mut)
    _show_concurrency(chat_id, message_id, "")


def _edit_cc_queue_wait(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    states.set_state(chat_id, "sys_cc_queue_wait")
    ui.edit(
        chat_id, message_id,
        "请输入队列等待时长（秒，整数 ≥0；0 表示不排队直接 429）：\n"
        "例：<code>30</code>",
        reply_markup=ui.inline_kb([[ui.btn("❌ 取消", "sys:show:concurrency")]]),
    )


def _on_cc_queue_wait_input(chat_id: int, text: str) -> None:
    try:
        v = int((text or "").strip())
        if v < 0:
            raise ValueError
    except ValueError:
        ui.send(chat_id, "❌ 需要非负整数，请重新输入：")
        return
    def _mut(c):
        c.setdefault("concurrency", {})["queueWaitSeconds"] = v
    config.update(_mut)
    states.pop_state(chat_id)
    ui.send(chat_id, f"✅ 队列等待已更新为 <code>{v}s</code>")
    send_new(chat_id)


def _edit_cc_default_max(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    states.set_state(chat_id, "sys_cc_default_max")
    ui.edit(
        chat_id, message_id,
        "请输入默认最大并发数（整数 ≥0；0 表示不限制）：\n"
        "此值在各渠道未设置 <code>maxConcurrent</code> 时生效。\n\n"
        "例：<code>5</code>",
        reply_markup=ui.inline_kb([[ui.btn("❌ 取消", "sys:show:concurrency")]]),
    )


def _on_cc_default_max_input(chat_id: int, text: str) -> None:
    try:
        v = int((text or "").strip())
        if v < 0:
            raise ValueError
    except ValueError:
        ui.send(chat_id, "❌ 需要非负整数，请重新输入：")
        return
    def _mut(c):
        c.setdefault("concurrency", {})["defaultMaxConcurrent"] = v
    config.update(_mut)
    states.pop_state(chat_id)
    label = "不限" if v == 0 else str(v)
    ui.send(chat_id, f"✅ 默认最大并发数已更新为 <code>{label}</code>")
    send_new(chat_id)


# ─── API Key 限流默认值 ───────────────────────────────────────────


def _fmt_aklim_duration(seconds: int) -> str:
    seconds = max(0, int(seconds or 0))
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds >= 60 and seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def _parse_aklim_duration(text: str) -> int:
    raw = (text or "").strip().lower()
    if not raw:
        raise ValueError
    mult = 1
    if raw.endswith("s"):
        raw = raw[:-1].strip(); mult = 1
    elif raw.endswith("m"):
        raw = raw[:-1].strip(); mult = 60
    elif raw.endswith("h"):
        raw = raw[:-1].strip(); mult = 3600
    v = int(raw) * mult
    if v < 0:
        raise ValueError
    return v


def _show_aklim(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    cfg = config.get()
    ak_cfg = cfg.get("apiKeyConcurrency") or {}
    enabled = bool(ak_cfg.get("enabled", True))
    default_max = int(ak_cfg.get("defaultMaxConcurrent", 5))
    default_queue = int(ak_cfg.get("defaultMaxQueue", 50))
    default_wait = int(ak_cfg.get("defaultQueueWaitSeconds", 1800))
    totals = apikey_limiter.totals()
    snap = apikey_limiter.snapshot()
    lines = [
        "🔑 <b>API Key 默认限流</b>",
        "",
        f"总开关: <code>{'开' if enabled else '关'}</code>",
        f"默认并发上限: <code>{default_max if default_max > 0 else '不限'}</code>",
        f"默认队列上限: <code>{default_queue}</code>",
        f"默认最长等待: <code>{_fmt_aklim_duration(default_wait)}</code>",
        "",
        f"实时汇总: 在途 <b>{totals['in_flight']}</b> · 排队 <b>{totals['waiting']}</b> · 追踪 Key {totals['tracked_keys']}",
    ]
    interesting = [r for r in snap if r.get("in_flight", 0) > 0 or r.get("waiting", 0) > 0]
    if interesting:
        lines.append("")
        lines.append("活跃 Key:")
        for row in interesting[:12]:
            max_c = "∞" if row.get("unlimited") else str(row.get("max_concurrent", 0))
            icon = "🔴" if (not row.get("unlimited") and row.get("max_concurrent", 0) > 0 and row.get("in_flight", 0) >= row.get("max_concurrent", 0)) else "🟢"
            lines.append(
                f"  {icon} <code>{ui.escape_html(row.get('key_name', ''))}</code> · "
                f"{row.get('in_flight', 0)}/{max_c} 在途 · "
                f"{row.get('waiting', 0)}/{row.get('max_queue', 0)} 排队"
            )
        if len(interesting) > 12:
            lines.append(f"  <i>...还有 {len(interesting) - 12} 个未列出</i>")
    else:
        lines.append("")
        lines.append("<i>暂无排队或在途中的 API Key 请求。</i>")

    kb = ui.inline_kb([
        [ui.btn("🔴 关闭 Key 限流" if enabled else "🟢 开启 Key 限流", "sys:aklim_toggle")],
        [ui.btn("✏ 默认并发", "sys:edit:aklim_max"), ui.btn("✏ 默认队列", "sys:edit:aklim_queue")],
        [ui.btn("✏ 默认等待", "sys:edit:aklim_wait"), ui.btn("🔄 刷新", "sys:show:aklim")],
        [ui.btn("◀ 返回设置", "menu:settings")],
    ])
    ui.edit(chat_id, message_id, "\n".join(lines), reply_markup=kb)


def _on_aklim_toggle(chat_id: int, message_id: int, cb_id: str) -> None:
    cfg = config.get()
    cur = bool((cfg.get("apiKeyConcurrency") or {}).get("enabled", True))
    def _mut(c):
        c.setdefault("apiKeyConcurrency", {})["enabled"] = not cur
    config.update(_mut)
    ui.answer_cb(cb_id, "已切换")
    _show_aklim(chat_id, message_id, "")


def _edit_aklim(chat_id: int, message_id: int, cb_id: str, field: str) -> None:
    ui.answer_cb(cb_id)
    states.set_state(chat_id, f"sys_aklim_{field}")
    if field == "wait":
        text = "请输入默认最长等待时间：\n支持 <code>1800</code>、<code>30m</code>、<code>1h</code>；<code>0</code> 表示不等待。"
    elif field == "max":
        text = "请输入默认并发上限（整数 ≥0）：\n<code>0</code> 表示不限。"
    else:
        text = "请输入默认队列上限（整数 ≥0）：\n<code>0</code> 表示不排队。"
    ui.edit(chat_id, message_id, text, reply_markup=ui.inline_kb([[ui.btn("❌ 取消", "sys:show:aklim")]]))


def _on_aklim_input(chat_id: int, action: str, text: str) -> None:
    field = action.removeprefix("sys_aklim_")
    try:
        if field == "wait":
            value = _parse_aklim_duration(text)
        else:
            value = int((text or "").strip())
            if value < 0:
                raise ValueError
    except Exception:
        ui.send(chat_id, "❌ 需要非负整数；等待时间可带 s/m/h，请重新输入：")
        return
    key_map = {"max": "defaultMaxConcurrent", "queue": "defaultMaxQueue", "wait": "defaultQueueWaitSeconds"}
    if field not in key_map:
        states.pop_state(chat_id); return
    def _mut(c):
        c.setdefault("apiKeyConcurrency", {})[key_map[field]] = value
    config.update(_mut)
    states.pop_state(chat_id)
    label = _fmt_aklim_duration(value) if field == "wait" else ("不限" if field == "max" and value == 0 else str(value))
    ui.send(chat_id, f"✅ API Key 默认限流已更新为 <code>{ui.escape_html(label)}</code>")
    send_new(chat_id)


def _ws_mode_enabled() -> bool:
    return bool((config.get().get("openai") or {}).get("responsesUpstreamWsForOAuth", False))


def _show_ws_mode(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    enabled = _ws_mode_enabled()
    lines = [
        "🔌 <b>WS 模式</b>",
        "",
        "当前状态：",
        "• 下游 WebSocket <code>/v1/responses</code>：已支持，默认可用",
        f"• HTTP Responses 转上游 WS：<code>{'开启' if enabled else '关闭'}</code>",
        "",
        "说明：",
        "开启后，当客户端仍使用 HTTP/SSE <code>/v1/responses</code> 调用 Parrot 时，",
        "如果选中的上游渠道是 <b>OpenAI OAuth</b> 账号，Parrot 会优先使用 WebSocket 连接 OpenAI Codex 上游。",
        "",
        "普通 OpenAI API 渠道不受影响。",
        "Anthropic / ChatCompletions / images 不受影响。",
    ]
    ui.edit(chat_id, message_id, "\n".join(lines), reply_markup=ui.inline_kb([
        [ui.btn("🔴 关闭 HTTP→WS 上游转换" if enabled else "🟢 开启 HTTP→WS 上游转换", "sys:ws_mode:toggle")],
        [ui.btn("◀ 返回设置", "menu:settings")],
    ]))


def _on_ws_mode_toggle(chat_id: int, message_id: int, cb_id: str) -> None:
    cur = _ws_mode_enabled()
    new_enabled = not cur
    def _mut(c):
        openai_cfg = c.setdefault("openai", {})
        openai_cfg["responsesUpstreamWsForOAuth"] = new_enabled
        openai_cfg.pop("responsesUpstreamTransport", None)
        openai_cfg.pop("responsesUpstreamWs", None)
    config.update(_mut)
    ui.answer_cb(cb_id, "已切换")
    _show_ws_mode(chat_id, message_id, "")
