"""系统设置菜单。

callback_data 前缀：`sys:...`
状态机 action：`sys_*`（各编辑子项）
"""

from __future__ import annotations

import asyncio
import math
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

from ... import apikey_limiter, concurrency, config, load_balancing, log_db, network, network_monitor, state_db
from ...channel import registry
from .. import states, ui


_BJT = timezone(timedelta(hours=8))
_RETENTION_PLAN_TTL_SECONDS = 600
_retention_pending_lock = threading.Lock()
# code → {chat_id, kind, expires_at, ...}; callback 本身只带 8 位短码，实际计划
# 永远留在服务端，不接受客户端传来的路径、月份或删除范围。
_retention_pending: dict[str, dict] = {}


def _merge_proxy_stats_for_system(names, stats_map: dict[str, dict]) -> dict:
    """Merge backend proxy metrics by each metric's own non-NULL sample count."""

    merged = {
        "requests": 0, "successes": 0, "failures": 0,
        "input_tokens": 0, "output_tokens": 0,
        "cache_creation_tokens": 0, "cache_read_tokens": 0,
        "total_tokens": 0, "bytes_up": 0, "bytes_down": 0, "total_bytes": 0,
        "connect_sum_ms": 0, "connect_sample_count": 0,
        "first_byte_sum_ms": 0, "first_byte_sample_count": 0,
        "idle_sum_ms": 0, "idle_sample_count": 0,
        "total_sum_ms": 0, "total_sample_count": 0,
        "avg_connect_ms": 0, "avg_first_byte_ms": 0,
        "avg_idle_ms": 0, "avg_total_ms": 0,
    }
    for name in names:
        if name == "direct":
            continue
        ps = stats_map.get(name)
        if not ps:
            continue
        req = int(ps.get("requests") or 0)
        merged["requests"] += req
        merged["successes"] += int(ps.get("successes") or 0)
        merged["failures"] += int(ps.get("failures") or 0)
        for key in (
            "input_tokens", "output_tokens", "cache_creation_tokens",
            "cache_read_tokens", "total_tokens", "bytes_up", "bytes_down", "total_bytes",
        ):
            merged[key] += int(ps.get(key) or 0)
        for sum_key, count_key in (
            ("connect_sum_ms", "connect_sample_count"),
            ("first_byte_sum_ms", "first_byte_sample_count"),
            ("idle_sum_ms", "idle_sample_count"),
            ("total_sum_ms", "total_sample_count"),
        ):
            merged[sum_key] += int(ps.get(sum_key) or 0)
            merged[count_key] += int(ps.get(count_key) or 0)
    for avg_key, sum_key, count_key in (
        ("avg_connect_ms", "connect_sum_ms", "connect_sample_count"),
        ("avg_first_byte_ms", "first_byte_sum_ms", "first_byte_sample_count"),
        ("avg_idle_ms", "idle_sum_ms", "idle_sample_count"),
        ("avg_total_ms", "total_sum_ms", "total_sample_count"),
    ):
        count = int(merged[count_key] or 0)
        merged[avg_key] = round(merged[sum_key] / count) if count else 0
    return merged


# ─── 主菜单 ───────────────────────────────────────────────────────

def _main_text_and_kb() -> tuple[str, dict]:
    cfg = config.get()
    t = cfg.get("timeouts") or {}
    sc = cfg.get("scoring") or {}
    aff = cfg.get("affinity") or {}
    retention = log_db.retention_policy(cfg)
    retention_label = (
        "全部保留"
        if retention["mode"] == "forever"
        else f"按天留存（{retention['days']} 天）"
    )
    ws_enabled = bool((cfg.get("openai") or {}).get("responsesUpstreamWsForOAuth", False))
    ws_mode_label = "开" if ws_enabled else "关"

    retry_transient = ((cfg.get("retry") or {}).get("transient") or {})
    retry_enabled = bool(retry_transient.get("enabled", True))
    try:
        retry_extra = max(0, int(retry_transient.get("maxExtraAttempts", 2)))
    except (TypeError, ValueError):
        retry_extra = 2
    retry_summary = (
        f"候选按序故障转移 · 瞬时额外 {retry_extra} 次 · 仅首包前"
        if retry_enabled and retry_extra > 0
        else "候选按序故障转移 · 瞬时加试关闭"
    )

    text = (
        "⚙ <b>系统设置</b>\n\n"
        f"超时: 连接 <code>{t.get('connect', 10)}s</code> | "
        f"首字 <code>{t.get('firstByte', 30)}s</code> | "
        f"空闲 <code>{t.get('idle', 30)}s</code> | "
        f"总 <code>{t.get('total', 600)}s</code>\n"
        f"重试: {retry_summary}\n"
        f"错误阶梯: <code>{','.join(str(x) for x in (cfg.get('errorWindows') or []))}</code>\n"
        f"评分: α={sc.get('emaAlpha', 0.25)} · 窗口={sc.get('recentWindow', 50)} · "
        f"惩罚={sc.get('errorPenaltyFactor', 8)} · 探索={sc.get('explorationRate', 0.2)}\n"
        f"亲和: TTL={aff.get('ttlMinutes', 30)}min\n"
        f"调度: <code>{load_balancing.display_mode(cfg.get('channelSelection', 'smart'))}</code>\n"
        f"WS模式: HTTP→WS 上游转换 <code>{ws_mode_label}</code>\n"
        f"请求日志留存: <code>{retention_label}</code>\n"
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
        [ui.btn("🗃 数据留存", "sys:show:retention"),
         ui.btn("🆕 版本更新", "menu:update")],
        [ui.btn("🔁 重试设置", "sys:show:retry"),
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


# ─── 重试设置 ───────────────────────────────────────────────────────

_RETRY_TRANSIENT_EVENTS = [
    ("openaiServerOverloaded", "OpenAI overload", "OpenAI <code>server_is_overloaded</code>"),
    ("openaiServerError", "OpenAI server_error", "OpenAI <code>server_error</code>"),
    ("claudeOverloaded", "Claude overload", "Claude <code>overloaded_error / HTTP 529</code>"),
    ("xaiUnavailable", "xAI unavailable", "xAI <code>HTTP 503 / UNAVAILABLE</code>"),
]
_RETRY_RECOVERY_EVENTS = [
    ("oauthRefresh", "OAuth 刷新", "OAuth 401/403：每个报错账号刷新后，原账号加试 1 次"),
    ("invalidEncryptedContent", "EC 修复", "无效 encrypted_content：全请求移除后加试 1 次"),
    ("claudeContext1mFallback", "1M 回退", "Claude 1M 权限不足：当前候选回退普通上下文加试 1 次"),
]


def _retry_sections() -> tuple[dict, dict]:
    retry = config.get().get("retry") or {}
    if not isinstance(retry, dict):
        retry = {}
    transient = retry.get("transient") or {}
    recovery = retry.get("recovery") or {}
    return (
        transient if isinstance(transient, dict) else {},
        recovery if isinstance(recovery, dict) else {},
    )


def _fmt_retry_delay(value) -> str:
    try:
        return f"{float(value):.2f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return "0"


def _retry_menu_text_kb() -> tuple[str, dict]:
    transient, recovery = _retry_sections()
    enabled = bool(transient.get("enabled", True))
    try:
        extra = max(0, int(transient.get("maxExtraAttempts", 2)))
    except (TypeError, ValueError):
        extra = 2
    delays = transient.get("backoffSeconds") or [0.75, 1.75]
    if not isinstance(delays, (list, tuple)):
        delays = [0.75, 1.75]
    delay_text = " → ".join(f"{_fmt_retry_delay(v)}s" for v in delays) or "0.75s → 1.75s"
    error_flags = transient.get("errors") or {}
    if not isinstance(error_flags, dict):
        error_flags = {}

    lines = [
        "🔁 <b>重试设置</b>",
        "",
        "生效边界: <code>仅下游首个可见内容前</code>",
        "候选故障转移: <code>✅ 已启用</code> · 数量随当次候选动态变化",
        f"瞬时额外机会: <code>全请求共享 {extra} 次</code>",
        f"退避: <code>{delay_text}</code>（各加 0–0.25s 抖动）",
        "",
        "<i>当前账号 / 渠道 → 命中瞬时错误时原候选加试 → 仍失败则继续后续账号 / 渠道 → 全部候选耗尽才返回错误</i>",
        "",
        "<b>核心故障转移（次数动态）</b>",
        "✅ 账号 / 渠道：按调度顺序尝试当次全部可用候选",
        "✅ 代理路由：连接失败时按代理组顺序继续尝试",
        "",
        "<b>同候选瞬时加试（共享次数 + 退避）</b>",
    ]
    for key, _button_label, description in _RETRY_TRANSIENT_EVENTS:
        selected = bool(error_flags.get(key, True))
        if selected and enabled and extra > 0:
            mark = "✅"
        elif selected:
            mark = "⏸"
        else:
            mark = "🚫"
        lines.append(f"{mark} {description}")

    lines.extend(["", "<b>鉴权与请求修复（独立防循环）</b>"])
    for key, _button_label, description in _RETRY_RECOVERY_EVENTS:
        lines.append(f"{'✅' if recovery.get(key, True) else '🚫'} {description}")
    lines.extend([
        "",
        "<i>“额外机会”只限制瞬时错误的原候选加试，不会截断后续账号 / 渠道。"
        "OAuth 刷新若仍失败会继续后续候选；EC 仍无效则按请求错误返回；并发排队不计作重试。"
        "退避项少于额外机会时，后续加试沿用最后一项。</i>",
    ])

    rows: list[list[dict]] = [
        [ui.btn("✅ 瞬时加试：开启" if enabled else "🚫 瞬时加试：关闭", "sys:retry:toggle_transient"),
         ui.btn("ℹ 候选切换：全部", "sys:retry:failover_info")],
        [ui.btn(f"✏ 额外机会：{extra}", "sys:retry:edit_attempts"),
         ui.btn("✏ 退避：" + ",".join(_fmt_retry_delay(v) for v in delays), "sys:retry:edit_backoff")],
    ]
    for offset in range(0, len(_RETRY_TRANSIENT_EVENTS), 2):
        row = []
        for key, label, _description in _RETRY_TRANSIENT_EVENTS[offset:offset + 2]:
            row.append(ui.btn(
                f"{'☑' if error_flags.get(key, True) else '☐'} {label}",
                f"sys:retry:toggle_error:{key}",
            ))
        rows.append(row)
    for offset in range(0, len(_RETRY_RECOVERY_EVENTS), 2):
        row = []
        for key, label, _description in _RETRY_RECOVERY_EVENTS[offset:offset + 2]:
            row.append(ui.btn(
                f"{'☑' if recovery.get(key, True) else '☐'} {label}",
                f"sys:retry:toggle_recovery:{key}",
            ))
        rows.append(row)
    rows.append([ui.btn("◀ 返回系统设置", "menu:settings")])
    return "\n".join(lines), ui.inline_kb(rows)


def _show_retry(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    text, kb = _retry_menu_text_kb()
    ui.edit(chat_id, message_id, text, reply_markup=kb)


def _toggle_retry_transient(chat_id: int, message_id: int, cb_id: str) -> None:
    transient, _ = _retry_sections()
    new_value = not bool(transient.get("enabled", True))
    config.update(
        lambda c: c.setdefault("retry", {}).setdefault("transient", {}).__setitem__("enabled", new_value)
    )
    ui.answer_cb(cb_id, "瞬时加试已开启" if new_value else "瞬时加试已关闭")
    _show_retry(chat_id, message_id, "-")


def _toggle_retry_item(chat_id: int, message_id: int, cb_id: str, *, group: str, key: str) -> None:
    valid = (
        {item[0] for item in _RETRY_TRANSIENT_EVENTS}
        if group == "errors"
        else {item[0] for item in _RETRY_RECOVERY_EVENTS}
    )
    if key not in valid:
        ui.answer_cb(cb_id, "未知重试项", show_alert=True)
        return
    transient, recovery = _retry_sections()
    current = (
        bool((transient.get("errors") or {}).get(key, True))
        if group == "errors"
        else bool(recovery.get(key, True))
    )
    new_value = not current

    def _mutate(c):
        retry = c.setdefault("retry", {})
        if group == "errors":
            retry.setdefault("transient", {}).setdefault("errors", {})[key] = new_value
        else:
            retry.setdefault("recovery", {})[key] = new_value

    config.update(_mutate)
    ui.answer_cb(cb_id, "已开启" if new_value else "已关闭")
    _show_retry(chat_id, message_id, "-")


def _edit_retry_attempts(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    transient, _ = _retry_sections()
    current = transient.get("maxExtraAttempts", 2)
    states.set_state(chat_id, "sys_retry_attempts")
    ui.edit(
        chat_id,
        message_id,
        "请输入全请求共享的瞬时错误额外机会数：\n\n"
        "• 范围 <code>1–5</code>\n"
        "• 不会按账号／渠道数量倍增\n"
        "• 设为关闭请使用菜单中的总开关\n\n"
        f"当前：<code>{ui.escape_html(str(current))}</code>",
        reply_markup=ui.inline_kb([[ui.btn("❌ 取消", "sys:show:retry")]]),
    )


def _on_retry_attempts_input(chat_id: int, text: str) -> None:
    try:
        value = int((text or "").strip())
    except (TypeError, ValueError):
        ui.send(chat_id, "❌ 请输入 1–5 的整数：")
        return
    if value < 1 or value > 5:
        ui.send(chat_id, "❌ 额外机会需在 1–5 之间，请重新输入：")
        return
    config.update(
        lambda c: c.setdefault("retry", {}).setdefault("transient", {}).__setitem__("maxExtraAttempts", value)
    )
    states.pop_state(chat_id)
    ui.send_result(
        chat_id,
        f"✅ 瞬时错误额外机会已更新为 <code>{value}</code> 次（全请求共享）",
        back_label="◀ 返回重试设置",
        back_callback="sys:show:retry",
    )


def _edit_retry_backoff(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    transient, _ = _retry_sections()
    current = transient.get("backoffSeconds") or [0.75, 1.75]
    states.set_state(chat_id, "sys_retry_backoff")
    ui.edit(
        chat_id,
        message_id,
        "请输入瞬时加试退避秒数，以逗号分隔：\n\n"
        "• 1–5 项，每项范围 <code>0–60</code> 秒\n"
        "• 每次实际等待会另加 0–0.25 秒抖动\n"
        "• 项数不足时，后续加试沿用最后一项\n\n"
        f"当前：<code>{ui.escape_html(','.join(_fmt_retry_delay(v) for v in current))}</code>\n"
        "示例：<code>0.75,1.75</code>",
        reply_markup=ui.inline_kb([[ui.btn("❌ 取消", "sys:show:retry")]]),
    )


def _on_retry_backoff_input(chat_id: int, text: str) -> None:
    raw = [part for part in re.split(r"[,，;；\s]+", (text or "").strip()) if part]
    if not raw or len(raw) > 5:
        ui.send(chat_id, "❌ 请输入 1–5 个退避秒数：")
        return
    values: list[float] = []
    try:
        for part in raw:
            value = float(part)
            if not math.isfinite(value) or value < 0 or value > 60:
                raise ValueError
            values.append(round(value, 3))
    except (TypeError, ValueError):
        ui.send(chat_id, "❌ 每项必须是 0–60 之间的数字，请重新输入：")
        return
    config.update(
        lambda c: c.setdefault("retry", {}).setdefault("transient", {}).__setitem__("backoffSeconds", values)
    )
    states.pop_state(chat_id)
    formatted = ",".join(_fmt_retry_delay(v) for v in values)
    ui.send_result(
        chat_id,
        f"✅ 瞬时加试退避已更新为 <code>{formatted}</code> 秒",
        back_label="◀ 返回重试设置",
        back_callback="sys:show:retry",
    )


# ─── 请求日志数据留存 ───────────────────────────────────────────────


def _fmt_retention_bytes(value) -> str:
    try:
        size = max(0, int(value or 0))
    except (TypeError, ValueError):
        return "0 B"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return "0 B"


def _fmt_retention_time(value) -> str:
    try:
        return datetime.fromtimestamp(float(value), tz=_BJT).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "时间不可表示"



def _purge_expired_retention_pending() -> None:
    now = time.time()
    with _retention_pending_lock:
        expired = [code for code, value in _retention_pending.items()
                   if float(value.get("expires_at") or 0) <= now]
        for code in expired:
            _retention_pending.pop(code, None)


def _register_retention_pending(chat_id: int, kind: str, **payload) -> str:
    _purge_expired_retention_pending()
    # ui.register_code 只解决 callback 64-byte 限制；真正计划和权限绑定都在本字典里。
    code = ui.register_code(f"retention:{kind}:{secrets.token_hex(16)}")
    data = {
        "chat_id": int(chat_id),
        "kind": kind,
        "expires_at": time.time() + _RETENTION_PLAN_TTL_SECONDS,
        **payload,
    }
    with _retention_pending_lock:
        _retention_pending[code] = data
    return code


def _get_retention_pending(code: str, chat_id: int, kind: str) -> dict | None:
    _purge_expired_retention_pending()
    with _retention_pending_lock:
        item = _retention_pending.get(code)
        if not item or item.get("kind") != kind or int(item.get("chat_id") or -1) != int(chat_id):
            return None
        return dict(item)


def _pop_retention_pending(code: str, chat_id: int, kind: str | None = None) -> dict | None:
    _purge_expired_retention_pending()
    with _retention_pending_lock:
        item = _retention_pending.get(code)
        if not item or int(item.get("chat_id") or -1) != int(chat_id):
            return None
        if kind is not None and item.get("kind") != kind:
            return None
        _retention_pending.pop(code, None)
        return dict(item)


def _retention_menu_text_kb() -> tuple[str, dict]:
    policy = log_db.retention_policy()
    if policy["mode"] == "days":
        policy_lines = [
            "当前留存模式：<code>按天留存</code>",
            f"保留天数：<code>{int(policy['days'])} 天</code>",
        ]
    else:
        policy_lines = ["当前留存模式：<code>全部保留</code>"]
    lines = [
        "🗃 <b>请求日志数据留存</b>",
        "",
        *policy_lines,
        "",
        "<b>清理范围</b>",
        "• 请求摘要与其统计来源",
        "• 原始 request / response 内容",
        "• 重试、代理、本地 Web 调用明细",
        "",
        "<b>不会影响</b>",
        "• state.db 运行状态",
        "• 图片日志、图片缓存、翻译缓存",
        "",
        "<i>按天留存最少 1 天、无业务上限。整月过期库会删除文件；边界月会精确删除旧记录并压缩数据库以释放磁盘。</i>",
    ]
    if log_db.retention_cleanup_busy():
        lines.extend(["", "⏳ <i>日志留存清理正在执行，完成前不能切换策略。</i>"])
    rows: list[list[dict]] = []
    if policy["mode"] == "days":
        rows.append([
            ui.btn("⏰ 修改保留天数", "sys:retention:days"),
            ui.btn("♾ 切换为全部保留", "sys:retention:forever"),
        ])
    else:
        rows.append([ui.btn("⏰ 设置按天数留存", "sys:retention:days")])
    rows.append([ui.btn("◀ 返回系统设置", "menu:settings")])
    return "\n".join(lines), ui.inline_kb(rows)


def _show_retention(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    text, kb = _retention_menu_text_kb()
    ui.edit(chat_id, message_id, text, reply_markup=kb)


def _edit_retention_days(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    if log_db.retention_cleanup_busy():
        ui.edit(chat_id, message_id, "⏳ 日志留存清理正在执行，完成后再修改策略。",
                reply_markup=ui.inline_kb([[ui.btn("◀ 返回数据留存", "sys:show:retention")]]))
        return
    policy = log_db.retention_policy()
    current = (
        f"当前为按天留存，保留 <code>{int(policy['days'])}</code> 天。请输入新的保留天数："
        if policy["mode"] == "days"
        else "当前为全部保留。请输入要保留的天数："
    )
    states.set_state(chat_id, "sys_retention_days")
    ui.edit(
        chat_id,
        message_id,
        f"{current}\n\n"
        "• 必须是正整数\n"
        "• 最少 <code>1</code> 天\n"
        "• 不设业务最大值\n\n"
        "示例：<code>7</code>",
        reply_markup=ui.inline_kb([[ui.btn("❌ 取消", "sys:retention:cancel_input")]]),
    )


def _on_retention_days_input(chat_id: int, text: str) -> None:
    try:
        days = int((text or "").strip())
    except (TypeError, ValueError):
        ui.send(chat_id, "❌ 请输入大于等于 1 的整数天数：")
        return
    if days < 1:
        ui.send(chat_id, "❌ 最少保留 1 天，请重新输入：")
        return
    states.pop_state(chat_id)
    current = log_db.retention_policy()
    if current["mode"] == "days":
        old_days = int(current["days"])
        if days == old_days:
            ui.send(
                chat_id,
                f"ℹ️ 当前已经是按天留存 <code>{days}</code> 天，未修改设置。",
                reply_markup=ui.inline_kb([[ui.btn("🗃 返回数据留存", "sys:show:retention")]]),
            )
            return
        if days > old_days:
            # 延长保留期不会扩大删除范围，因此直接保存，不走删除预览。
            result = log_db.extend_retention_days(days)
            if result.get("ok"):
                ui.send(
                    chat_id,
                    "✅ <b>按天留存天数已修改</b>\n\n"
                    f"留存模式：按天留存\n"
                    f"保留天数：<code>{old_days}</code> 天 → <code>{days}</code> 天\n\n"
                    "本次只是延长保留时间，不会发起即时清理；此前已删除的历史日志无法恢复。",
                    reply_markup=ui.inline_kb([[ui.btn("🗃 返回数据留存", "sys:show:retention")]]),
                )
            else:
                ui.send(
                    chat_id,
                    f"⚠️ 修改保留天数失败：<code>{ui.escape_html(str(result.get('reason') or '未知错误'))}</code>",
                    reply_markup=ui.inline_kb([[ui.btn("🗃 返回数据留存", "sys:show:retention")]]),
                )
            return
        transition = f"即将把按天留存从 {old_days} 天缩短为 {days} 天"
    else:
        transition = "即将从全部保留切换为按天留存"

    # 首次启用或缩短留存期会扩大删除范围，必须先展示警告、扫描清单，再二次确认。
    code = _register_retention_pending(chat_id, "first", days=days)
    ui.send(
        chat_id,
        f"⚠️ <b>{transition}</b>\n\n"
        f"新的保留天数：<code>{days}</code> 天。\n"
        "最终保存后，系统会自动清理早于留存临界时间的请求日志。\n\n"
        "将移除请求摘要、原始 request / response、重试、代理和本地 Web 明细；"
        "对应历史统计与日志详情也将不再可查。\n\n"
        "继续后只会扫描并展示清理清单，<b>不会保存设置，也不会删除数据</b>。",
        reply_markup=ui.inline_kb([
            [ui.btn("✅ 继续扫描清理清单", f"sys:retention:scan:{code}")],
            [ui.btn("❌ 取消", f"sys:retention:cancel:{code}")],
        ]),
    )


def _retention_item_lines(item: dict, ordinal: int) -> list[str]:
    month = ui.escape_html(str(item.get("month") or "?"))
    bundle = _fmt_retention_bytes(item.get("bundle_bytes"))
    total = int(item.get("total_requests") or 0)
    expired = int(item.get("expired_requests") or 0)
    if item.get("action") == "delete_file":
        return [
            f"<b>{ordinal}. {month}.db</b> · <code>{bundle}</code>",
            f"   整库删除 · {expired:,} 条请求",
        ]
    return [
        f"<b>{ordinal}. {month}.db</b> · 当前 <code>{bundle}</code>",
        f"   删除 {expired:,} / {total:,} 条过期请求及关联明细",
        "   随后压缩该月数据库以释放空间",
    ]


def _render_retention_plan(chat_id: int, message_id: int, code: str, page: int) -> None:
    entry = _get_retention_pending(code, chat_id, "plan")
    if entry is None:
        ui.edit(chat_id, message_id, "⚠️ 清理预览已过期，请重新设置保留天数。",
                reply_markup=ui.inline_kb([[ui.btn("◀ 返回数据留存", "sys:show:retention")]]))
        return
    plan = entry.get("plan") or {}
    items = list(plan.get("items") or [])
    page_size = 6
    page_count = max(1, (len(items) + page_size - 1) // page_size)
    page = max(0, min(int(page), page_count - 1))
    start = page * page_size
    visible = items[start:start + page_size]
    days = int(plan.get("days") or 0)
    lines = [
        "🔎 <b>数据清理预览</b>",
        "",
        "<i>尚未保存策略，尚未删除任何数据。</i>",
        f"新策略：保留最近 <code>{days}</code> 天",
        f"清理临界：<code>{_fmt_retention_time(plan.get('cutoff'))}</code>（北京时间）",
        f"已扫描：{int(plan.get('scanned_months') or 0)} 个数据库 · {_fmt_retention_bytes(plan.get('scanned_bytes'))}",
        "",
    ]
    if not items:
        lines.append("✅ 当前没有早于临界时间的请求日志，无需删除历史数据。")
    else:
        lines.append(f"本次涉及 {len(items)} 个数据库（第 {page + 1}/{page_count} 页）：")
        lines.append("")
        for ordinal, item in enumerate(visible, start=start + 1):
            lines.extend(_retention_item_lines(item, ordinal))
            lines.append("")
    preflight = plan.get("preflight") or {}
    if items and any(item.get("action") == "trim_and_vacuum" for item in items):
        if preflight.get("ok"):
            lines.extend([
                "<b>压缩空间预检：通过</b>",
                f"可用：<code>{_fmt_retention_bytes(preflight.get('effective_available_bytes'))}</code> · "
                f"需要至少：<code>{_fmt_retention_bytes(preflight.get('required_bytes'))}</code>",
            ])
        else:
            lines.extend([
                "⚠️ <b>压缩空间预检未通过</b>",
                ui.escape_html(str(preflight.get("reason") or "未知原因")),
            ])
    lines.extend([
        "",
        "⚠️ 删除不可恢复；历史统计和日志详情将不再可查。",
    ])

    rows: list[list[dict]] = []
    nav: list[dict] = []
    if page > 0:
        nav.append(ui.btn("◀ 上一页", f"sys:retention:plan:{code}:{page - 1}"))
    if page < page_count - 1:
        nav.append(ui.btn("下一页 ▶", f"sys:retention:plan:{code}:{page + 1}"))
    if nav:
        rows.append(nav)
    if page == page_count - 1 and bool(preflight.get("ok")):
        label = "✅ 确认保存策略" if not items else "🗑 确认保存并执行清理"
        rows.append([ui.btn(label, f"sys:retention:commit:{code}")])
    rows.append([ui.btn("❌ 取消", f"sys:retention:cancel:{code}")])
    ui.edit(chat_id, message_id, "\n".join(lines), reply_markup=ui.inline_kb(rows))


def _scan_retention(chat_id: int, message_id: int, cb_id: str, code: str) -> None:
    entry = _pop_retention_pending(code, chat_id, "first")
    if entry is None:
        ui.answer_cb(cb_id, "确认已过期，请重新输入天数", show_alert=True)
        return
    ui.answer_cb(cb_id, "正在扫描…")
    ui.edit(chat_id, message_id, "🔎 <b>正在扫描所有月度请求日志…</b>\n\n不会读取或展示请求正文。")
    try:
        plan = log_db.plan_retention(int(entry["days"]))
    except Exception as exc:
        ui.edit(chat_id, message_id, f"❌ 扫描失败：<code>{ui.escape_html(str(exc))}</code>",
                reply_markup=ui.inline_kb([[ui.btn("◀ 返回数据留存", "sys:show:retention")]]))
        return
    if plan.get("errors"):
        details = "\n".join(f"• {ui.escape_html(str(item))}" for item in plan["errors"][:8])
        ui.edit(chat_id, message_id, "❌ <b>扫描未完成，已拒绝生成删除计划</b>\n\n" + details,
                reply_markup=ui.inline_kb([[ui.btn("◀ 返回数据留存", "sys:show:retention")]]))
        return
    plan_code = _register_retention_pending(chat_id, "plan", plan=plan)
    _render_retention_plan(chat_id, message_id, plan_code, 0)


def _retention_progress_text(days: int, event: dict) -> str:
    item = event.get("item") or {}
    month = ui.escape_html(str(item.get("month") or "?"))
    index = int(event.get("index") or 0)
    total = int(event.get("total") or 0)
    phase = event.get("phase")
    lines = [
        "🧹 <b>正在执行请求日志清理</b>",
        f"策略：保留最近 <code>{days}</code> 天",
        "",
        f"[{index}/{total}] <code>{month}.db</code>",
    ]
    if phase == "item_start":
        lines.append("正在准备…")
    elif phase == "trim_delete":
        lines.append("正在删除过期请求及关联原始数据…")
    elif phase == "trim_vacuum":
        lines.append("正在压缩数据库以释放空间，可能需要一些时间…")
    elif phase == "item_done":
        result = event.get("result") or {}
        if result.get("ok"):
            lines.append("✅ 本项完成")
        else:
            lines.append("⚠️ 本项未完整完成，正在记录结果…")
    return "\n".join(lines)


def _commit_retention(chat_id: int, message_id: int, cb_id: str, code: str) -> None:
    entry = _pop_retention_pending(code, chat_id, "plan")
    if entry is None:
        ui.answer_cb(cb_id, "清理预览已过期，请重新扫描", show_alert=True)
        return
    plan = entry.get("plan") or {}
    days = int(plan.get("days") or 0)
    ui.answer_cb(cb_id, "开始执行清理…")
    busy_kb = ui.inline_kb([[ui.btn("⏳ 正在清理…", "sys:retention:noop")]])
    ui.edit(chat_id, message_id, "🧹 <b>正在验证清理计划并保存策略…</b>", reply_markup=busy_kb)

    def _progress(event: dict) -> None:
        ui.edit(chat_id, message_id, _retention_progress_text(days, event), reply_markup=busy_kb)

    result = log_db.apply_retention_plan(plan, activate_policy=True, progress=_progress)
    if result.get("ok"):
        text = (
            "✅ <b>数据留存策略已生效</b>\n\n"
            f"当前策略：仅保留最近 <code>{int(result.get('days') or days)}</code> 天\n"
            f"整库删除：<code>{int(result.get('full_months_deleted') or 0)}</code> 个\n"
            f"已删除历史请求：<code>{int(result.get('deleted_requests') or 0):,}</code> 条\n"
            f"实际释放磁盘：<code>{_fmt_retention_bytes(result.get('actual_free_bytes'))}</code>"
        )
    else:
        errors = result.get("errors") or []
        reason = result.get("reason") or "；".join(str(x) for x in errors) or "未知错误"
        if result.get("config_saved"):
            text = (
                "⚠️ <b>数据留存策略已保存，但清理未完整完成</b>\n\n"
                f"当前策略：仅保留最近 <code>{int(result.get('days') or days)}</code> 天\n"
                f"已删除历史请求：<code>{int(result.get('deleted_requests') or 0):,}</code> 条\n"
                f"原因：<code>{ui.escape_html(str(reason))}</code>\n\n"
                "系统会在后续维护周期继续检查；也可重新进入此菜单扫描。"
            )
        else:
            text = (
                "❌ <b>未保存数据留存策略，未执行删除</b>\n\n"
                f"原因：<code>{ui.escape_html(str(reason))}</code>"
            )
    ui.edit(chat_id, message_id, text, reply_markup=ui.inline_kb([
        [ui.btn("🗃 返回数据留存", "sys:show:retention")],
        [ui.btn("◀ 返回系统设置", "menu:settings")],
    ]))


def _set_retention_forever(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    result = log_db.set_retention_forever()
    if not result.get("ok"):
        ui.edit(chat_id, message_id, f"⚠️ {ui.escape_html(str(result.get('reason') or '切换失败'))}",
                reply_markup=ui.inline_kb([[ui.btn("◀ 返回数据留存", "sys:show:retention")]]))
        return
    ui.edit(chat_id, message_id, "✅ 已切换为 <b>全部保留</b>。\n\n不会删除已有请求日志。",
            reply_markup=ui.inline_kb([[ui.btn("◀ 返回数据留存", "sys:show:retention")]]))


def _cancel_retention(chat_id: int, message_id: int, cb_id: str, code: str | None = None) -> None:
    states.pop_state(chat_id)
    if code:
        _pop_retention_pending(code, chat_id)
    ui.answer_cb(cb_id, "已取消")
    text, kb = _retention_menu_text_kb()
    ui.edit(chat_id, message_id, text, reply_markup=kb)


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
    ("quota_cooldown",        "🟠 配额冷却"),
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

    def _merge_stats(names):
        return _merge_proxy_stats_for_system(names, pstats_by_name)

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
        [ui.provider_button(
            "OpenAI " + ("关" if core.get("openai") else "开"),
            "sys:mon:core_toggle:openai",
            "openai",
        )],
        [ui.provider_button(
            "Claude " + ("关" if core.get("claude") else "开"),
            "sys:mon:core_toggle:claude",
            "claude",
        )],
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
            # API 渠道无法锁定具体 Provider；展示它兼容的协议家族 rich tag。
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

    # 请求日志留存：两次确认分别使用 first / plan 短期服务端状态。
    if data == "sys:show:retention":
        _show_retention(chat_id, message_id, cb_id); return True
    if data == "sys:retention:days":
        _edit_retention_days(chat_id, message_id, cb_id); return True
    if data == "sys:retention:forever":
        _set_retention_forever(chat_id, message_id, cb_id); return True
    if data == "sys:retention:cancel_input":
        _cancel_retention(chat_id, message_id, cb_id); return True
    if data == "sys:retention:noop":
        ui.answer_cb(cb_id, "清理正在执行，请稍候"); return True
    if data.startswith("sys:retention:scan:"):
        _scan_retention(chat_id, message_id, cb_id, data.split(":", 3)[3]); return True
    if data.startswith("sys:retention:plan:"):
        parts = data.split(":")
        if len(parts) != 5:
            ui.answer_cb(cb_id, "无效的分页请求"); return True
        try:
            page = int(parts[4])
        except ValueError:
            ui.answer_cb(cb_id, "无效的页码"); return True
        ui.answer_cb(cb_id)
        _render_retention_plan(chat_id, message_id, parts[3], page); return True
    if data.startswith("sys:retention:commit:"):
        _commit_retention(chat_id, message_id, cb_id, data.split(":", 3)[3]); return True
    if data.startswith("sys:retention:cancel:"):
        _cancel_retention(chat_id, message_id, cb_id, data.split(":", 3)[3]); return True

    # 重试设置：核心候选/代理故障转移始终保留，仅配置首包前加试和独立恢复分支。
    if data == "sys:show:retry":
        _show_retry(chat_id, message_id, cb_id); return True
    if data == "sys:retry:toggle_transient":
        _toggle_retry_transient(chat_id, message_id, cb_id); return True
    if data == "sys:retry:failover_info":
        ui.answer_cb(
            cb_id,
            "账号／渠道会按调度顺序尝试当次全部可用候选；代理组按路由顺序处理连接失败。"
            "这些核心故障转移不会被瞬时加试总开关截断。",
            show_alert=True,
        )
        return True
    if data == "sys:retry:edit_attempts":
        _edit_retry_attempts(chat_id, message_id, cb_id); return True
    if data == "sys:retry:edit_backoff":
        _edit_retry_backoff(chat_id, message_id, cb_id); return True
    if data.startswith("sys:retry:toggle_error:"):
        _toggle_retry_item(
            chat_id, message_id, cb_id,
            group="errors", key=data.split(":", 3)[3],
        ); return True
    if data.startswith("sys:retry:toggle_recovery:"):
        _toggle_retry_item(
            chat_id, message_id, cb_id,
            group="recovery", key=data.split(":", 3)[3],
        ); return True

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
    if action == "sys_retention_days":
        _on_retention_days_input(chat_id, text); return True
    if action == "sys_retry_attempts":
        _on_retry_attempts_input(chat_id, text); return True
    if action == "sys_retry_backoff":
        _on_retry_backoff_input(chat_id, text); return True
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
    lines.append("<i>提示：每个渠道的 <code>maxConcurrent</code> 在「📡 渠道管理」"
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
