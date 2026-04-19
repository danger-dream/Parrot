"""系统设置菜单。

callback_data 前缀：`sys:...`
状态机 action：`sys_*`（各编辑子项）
"""

from __future__ import annotations

import json
import re
from typing import Any

from ... import config
from .. import states, ui
from . import main as main_menu


# ─── 主菜单 ───────────────────────────────────────────────────────

def _main_text_and_kb() -> tuple[str, dict]:
    cfg = config.get()
    t = cfg.get("timeouts") or {}
    sc = cfg.get("scoring") or {}
    aff = cfg.get("affinity") or {}
    qm = cfg.get("quotaMonitor") or {}

    text = (
        "⚙ <b>系统设置</b>\n\n"
        f"超时: 连接 <code>{t.get('connect', 10)}s</code> | "
        f"首字 <code>{t.get('firstByte', 30)}s</code> | "
        f"空闲 <code>{t.get('idle', 30)}s</code> | "
        f"总 <code>{t.get('total', 600)}s</code>\n"
        f"错误阶梯: <code>{','.join(str(x) for x in (cfg.get('errorWindows') or []))}</code>\n"
        f"评分: α={sc.get('emaAlpha', 0.25)} · 窗口={sc.get('recentWindow', 50)} · "
        f"惩罚={sc.get('errorPenaltyFactor', 8)} · 探索={sc.get('explorationRate', 0.2)}\n"
        f"亲和: TTL={aff.get('ttlMinutes', 30)}min · 打破阈值={aff.get('threshold', 3.0)}x\n"
        f"CCH: <code>{cfg.get('cchMode', 'disabled')}</code>"
        + (f" (<code>{cfg.get('cchStaticValue', '00000')}</code>)" if cfg.get('cchMode') == 'static' else "")
        + "\n"
        f"渠道选择: <code>{cfg.get('channelSelection', 'smart')}</code>\n"
        f"配额监控: <code>{'开' if qm.get('enabled') else '关'}</code>"
        f" · 间隔 {qm.get('intervalSeconds', 60)}s · 阈值 {qm.get('disableThresholdPercent', 95)}%\n"
    )
    bl = cfg.get("contentBlacklist") or {}
    bl_default_count = len((bl.get("default") or []))
    bl_by_ch_count = sum(len(v or []) for v in (bl.get("byChannel") or {}).values())
    text += f"黑名单: 默认 {bl_default_count} 条 · 渠道专属 {bl_by_ch_count} 条"

    kb = ui.inline_kb([
        [ui.btn("⏱ 超时设置", "sys:show:timeouts"),
         ui.btn("⛔ 错误阶梯", "sys:show:errwin")],
        [ui.btn("🎯 评分参数", "sys:show:scoring"),
         ui.btn("🔗 亲和参数", "sys:show:affinity")],
        [ui.btn("🎭 CCH 模式", "sys:show:cch"),
         ui.btn("🚦 选择模式", "sys:show:chsel")],
        [ui.btn("📈 配额监控", "sys:show:quota"),
         ui.btn("🔔 通知设置", "sys:show:notif")],
        [ui.btn("🛡 首包黑名单", "sys:show:blacklist")],
        [ui.btn("◀ 返回主菜单", "menu:main")],
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
    text = (
        "⛔ <b>错误冷却阶梯</b>\n\n"
        f"阶梯（分钟）: <code>{','.join(str(x) for x in win)}</code>\n"
        f"OAuth 宽容次数: <code>{grace}</code>\n\n"
        "<i>说明：</i>\n"
        "<i>• 每个 (渠道, 模型) 连续失败递进到下一阶梯；末位为 0 表示永久拉黑</i>\n"
        "<i>• 成功一次立即重置失败计数</i>\n"
        f"<i>• OAuth 渠道前 <b>{grace}</b> 次失败仅累计计数、不冷却（避免单账号偶发故障导致全部 Claude 模型不可用）</i>"
    )
    ui.edit(chat_id, message_id, text, reply_markup=ui.inline_kb([
        [ui.btn("✏ 修改阶梯", "sys:edit:errwin"),
         ui.btn("✏ OAuth 宽容次数", "sys:edit:oauth_grace")],
        [ui.btn("◀ 返回设置", "menu:settings")],
    ]))


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
    "threshold":  ("打破倍数",          "float", (1.0, 20.0)),
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

_CCH_MODES = ("disabled", "dynamic", "static")


def _show_cch(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    cfg = config.get()
    mode = cfg.get("cchMode", "disabled")
    static_val = cfg.get("cchStaticValue", "00000")
    text = (
        "🎭 <b>CCH 模式（Claude Code 伪装）</b>\n\n"
        f"当前模式: <code>{mode}</code>"
        + (f"\n静态值: <code>{static_val}</code>" if mode == "static" else "")
        + "\n\n"
        "<b>说明：</b>\n"
        "• <code>disabled</code>：不发送 CCH 头（生产默认）\n"
        "• <code>dynamic</code>：对每次请求 body 计算 xxhash64 → 5 位 hex\n"
        "• <code>static</code>：使用固定静态值（仅调试用）"
    )
    kb_rows = []
    for m in _CCH_MODES:
        label = f"{'✓ ' if m == mode else ''}{m}"
        kb_rows.append([ui.btn(label, f"sys:cch_set:{m}")])
    if mode == "static":
        kb_rows.append([ui.btn("✏ 修改静态值", "sys:edit:cch_static")])
    kb_rows.append([ui.btn("◀ 返回设置", "menu:settings")])
    ui.edit(chat_id, message_id, text, reply_markup=ui.inline_kb(kb_rows))


def _on_cch_set(chat_id: int, message_id: int, cb_id: str, mode: str) -> None:
    if mode not in _CCH_MODES:
        ui.answer_cb(cb_id, "无效模式")
        return
    config.update(lambda c: c.__setitem__("cchMode", mode))
    ui.answer_cb(cb_id, f"已切换到 {mode}")
    _show_cch(chat_id, message_id, "-")


def _edit_cch_static(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    states.set_state(chat_id, "sys_cch_static")
    ui.edit(
        chat_id, message_id,
        "请输入 CCH 静态值（5 位 0-9 a-f hex；如 <code>abcde</code>）：",
        reply_markup=ui.inline_kb([[ui.btn("❌ 取消", "sys:show:cch")]]),
    )


def _on_cch_static_input(chat_id: int, text: str) -> None:
    v = (text or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{5}", v):
        ui.send(chat_id, "❌ 需要正好 5 位 hex（0-9 a-f），请重新输入：")
        return
    config.update(lambda c: c.__setitem__("cchStaticValue", v))
    states.pop_state(chat_id)
    ui.send_result(
        chat_id,
        f"✅ CCH 静态值已更新为 <code>{v}</code>",
        back_label="◀ 返回 CCH 设置", back_callback="sys:show:cch",
    )


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
    if mode not in ("smart", "order"):
        ui.answer_cb(cb_id, "无效模式")
        return
    config.update(lambda c: c.__setitem__("channelSelection", mode))
    ui.answer_cb(cb_id, f"已切换到 {mode}")
    _show_chsel(chat_id, message_id, "-")


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


# ─── 路由 ─────────────────────────────────────────────────────────

def handle_callback(chat_id: int, message_id: int, cb_id: str, data: str) -> bool:
    if data == "menu:settings":
        show(chat_id, message_id, cb_id); return True

    if data == "sys:show:timeouts":  _show_timeouts(chat_id, message_id, cb_id); return True
    if data == "sys:edit:timeouts":  _edit_timeouts(chat_id, message_id, cb_id); return True
    if data == "sys:show:errwin":    _show_errwin(chat_id, message_id, cb_id); return True
    if data == "sys:edit:errwin":    _edit_errwin(chat_id, message_id, cb_id); return True
    if data == "sys:edit:oauth_grace": _edit_oauth_grace(chat_id, message_id, cb_id); return True
    if data == "sys:show:scoring":   _show_scoring(chat_id, message_id, cb_id); return True
    if data.startswith("sys:edit:scoring:"):
        _edit_scoring(chat_id, message_id, cb_id, data.split(":", 3)[3]); return True
    if data == "sys:show:affinity":  _show_affinity(chat_id, message_id, cb_id); return True
    if data.startswith("sys:edit:affinity:"):
        _edit_affinity(chat_id, message_id, cb_id, data.split(":", 3)[3]); return True
    if data == "sys:show:cch":       _show_cch(chat_id, message_id, cb_id); return True
    if data.startswith("sys:cch_set:"):
        _on_cch_set(chat_id, message_id, cb_id, data.split(":", 2)[2]); return True
    if data == "sys:edit:cch_static": _edit_cch_static(chat_id, message_id, cb_id); return True
    if data == "sys:show:chsel":     _show_chsel(chat_id, message_id, cb_id); return True
    if data.startswith("sys:chsel_set:"):
        _on_chsel_set(chat_id, message_id, cb_id, data.split(":", 2)[2]); return True

    # OAuth 配额监控
    if data == "sys:show:quota":          _show_quota(chat_id, message_id, cb_id); return True
    if data == "sys:quota_toggle":        _on_quota_toggle(chat_id, message_id, cb_id); return True
    if data == "sys:edit:quota_interval":   _edit_quota_interval(chat_id, message_id, cb_id); return True
    if data == "sys:edit:quota_threshold":  _edit_quota_threshold(chat_id, message_id, cb_id); return True

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

    return False


def handle_text_state(chat_id: int, action: str, text: str) -> bool:
    if action == "sys_timeouts":
        _on_timeouts_input(chat_id, text); return True
    if action == "sys_errwin":
        _on_errwin_input(chat_id, text); return True
    if action == "sys_oauth_grace":
        _on_oauth_grace_input(chat_id, text); return True
    if action.startswith("sys_scoring:"):
        _on_scoring_input(chat_id, action, text); return True
    if action.startswith("sys_affinity:"):
        _on_affinity_input(chat_id, action, text); return True
    if action == "sys_cch_static":
        _on_cch_static_input(chat_id, text); return True
    if action == "sys_bl_add_default":
        _on_bl_add_default_input(chat_id, text); return True
    if action == "sys_bl_add_ch":
        _on_bl_add_ch_input(chat_id, text); return True
    if action == "sys_quota_interval":
        _on_quota_interval_input(chat_id, text); return True
    if action == "sys_quota_threshold":
        _on_quota_threshold_input(chat_id, text); return True
    return False
