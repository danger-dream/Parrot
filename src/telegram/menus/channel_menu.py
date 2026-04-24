"""渠道管理菜单（沿用 openai-proxy 的风格：健康图标 + 详情排版）。

callback_data 前缀：`ch:...`（渠道）；`chw:...`（添加向导）

状态机 action（添加向导）：
  - `ch_wiz_name`     步骤 1/5：输入名称
  - `ch_wiz_url`      步骤 2/5：输入 Base URL
  - `ch_wiz_protocol` 步骤 3/5：选择上游协议（按钮）
  - `ch_wiz_key`      步骤 4/5：输入 API Key
  - `ch_wiz_models`   步骤 5/5：输入模型列表（支持 `real:alias, ...`）
  - `ch_wiz_test`     最后：测试面板（所有协议统一走 probe）

状态机 action（编辑）：
  - `ch_edit_name:<short>`
  - `ch_edit_url:<short>`
  - `ch_edit_key:<short>`
  - `ch_edit_models:<short>`
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from ... import affinity, config, cooldown, log_db, probe, scorer, state_db
from ...channel import api_channel, registry
from ...channel.url_utils import (
    detect_suffix_protocol,
    split_base_url,
    validate_api_path_for_protocol,
)
from .. import states, ui
from . import main as main_menu


# 渠道协议取值与展示标签。新增 openai 家族时在此集中维护。
PROTOCOL_CHOICES: list[tuple[str, str]] = [
    ("anthropic",        "🅰 Anthropic (/v1/messages)"),
    ("openai-chat",      "🅞 OpenAI Chat (/v1/chat/completions)"),
    ("openai-responses", "🅞 OpenAI Responses (/v1/responses)"),
]

_PROTOCOL_LABEL = {p: label for p, label in PROTOCOL_CHOICES}


def _protocol_of(ch) -> str:
    return getattr(ch, "protocol", "anthropic")


_BJT = timezone(timedelta(hours=8))


def _month_start_ts() -> float:
    return datetime.now(_BJT).replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()


# ─── 异步同步桥 ──────────────────────────────────────────────────

def _run_sync(coro):
    """在当前线程内阻塞跑 async；返回结果或异常对象。

    用在不需要异步并发的辅助路径（如 OAuth refresh 内部）。
    长时间阻塞的任务（比如 probe）请用 _spawn_async_task。
    """
    try:
        return asyncio.run(coro)
    except Exception as exc:
        return exc


# 测试可以把 _SYNC_SPAWN 设为 True，让 _spawn_async_task 改为同步执行（便于断言）
_SYNC_SPAWN = False


_AUTO_DELETE_OK_AFTER_SECONDS = 8       # 测试成功：消息 8 秒后删，留点时间让用户瞥一眼
_AUTO_DELETE_FAIL_AFTER_SECONDS = 30    # 测试失败：30 秒后删，让用户看完错误原因


async def _schedule_delete_after(chat_id: int, message_id: int,
                                 delay: float = _AUTO_DELETE_OK_AFTER_SECONDS) -> None:
    """延迟 delay 秒后删除一条消息（清理测试进度消息用）。

    删除失败（消息已被用户删 / 超过 TG 48h 限制）静默忽略。
    """
    await asyncio.sleep(delay)
    try:
        ui.delete_message(chat_id, message_id)
    except Exception:
        pass


def _delete_delay(ok: bool) -> float:
    return _AUTO_DELETE_OK_AFTER_SECONDS if ok else _AUTO_DELETE_FAIL_AFTER_SECONDS


async def _finalize_and_delete(chat_id: int, message_id: int,
                               final_text: str, ok: bool) -> None:
    """测试结束后：追加"将自动删除"提示 → 延迟 → 删除。

    追加一行斜体说明让用户知道这条消息会自动消失，而不是"留在那里碍事"。
    """
    delay = _delete_delay(ok)
    reminder = f"\n\n<i>⏱ 本消息将在 {int(delay)} 秒后自动删除</i>"
    try:
        ui.edit(chat_id, message_id, final_text + reminder)
    except Exception:
        pass
    await _schedule_delete_after(chat_id, message_id, delay=delay)


def _spawn_async_task(coro_factory, name: str = "tg-task") -> None:
    """把一个 async 任务丢到独立 daemon 线程执行，不阻塞 polling 主循环。

    coro_factory 是返回 coroutine 的零参函数（不能直接传 coroutine，
    因为它会在新线程里被 asyncio.run 消费）。

    用例：probe 模型测试，最长 60s，期间不应让 TG bot 失去响应。

    测试场景：把 channel_menu._SYNC_SPAWN = True 让任务在当前线程内同步跑完，
    便于直接断言后续状态。
    """
    if _SYNC_SPAWN:
        try:
            asyncio.run(coro_factory())
        except Exception:
            import traceback
            traceback.print_exc()
        return
    def _runner():
        try:
            asyncio.run(coro_factory())
        except Exception:
            import traceback
            traceback.print_exc()
    t = threading.Thread(target=_runner, daemon=True, name=name)
    t.start()


# ─── 健康图标（风格同 openai-proxy） ─────────────────────────────

def _channel_health(ch) -> tuple[str, str]:
    """返回 (icon, short_status_text)。"""
    if not ch.enabled:
        return "⬛", "已禁用"

    key = ch.key
    # 冷却中？
    cd_entries = cooldown.active_entries()
    perm = [e for e in cd_entries if e["channel_key"] == key and e["cooldown_until"] == -1]
    temp = [e for e in cd_entries if e["channel_key"] == key and e["cooldown_until"] != -1]
    if perm:
        return "🔴", f"永久冷却 ({len(perm)}模型)"
    if temp:
        return "🟠", f"冷却中 ({len(temp)}模型)"

    # 看最近成功率
    worst = None
    for stat in scorer.snapshot():
        if stat["channel_key"] != key:
            continue
        recent = stat["recent_requests"]
        if recent <= 0:
            continue
        rate = (stat["recent_success_count"] / recent) * 100
        if worst is None or rate < worst:
            worst = rate

    if worst is None:
        return "⚪", "暂无数据"
    if worst >= 80:
        return "🟢", f"近期 {worst:.0f}%"
    if worst >= 50:
        return "🟡", f"近期 {worst:.0f}%"
    return "🔴", f"近期 {worst:.0f}%"


def _summary_text(ch, tps_v: Optional[float] = None, cache_phrase: str = "") -> str:
    """列表行上的简短摘要（success_rate · avg_first_byte · sum_requests · 本月 TPS · 缓存）。"""
    key = ch.key
    total_req = 0
    avg_fb: list[float] = []
    rate_str = "—"
    best = 0
    for stat in scorer.snapshot():
        if stat["channel_key"] != key:
            continue
        total_req += stat["total_requests"]
        if stat["avg_first_byte_ms"]:
            avg_fb.append(stat["avg_first_byte_ms"])
        if stat["recent_requests"] > 0:
            rate = (stat["recent_success_count"] / stat["recent_requests"]) * 100
            if rate > best:
                best = rate
                rate_str = f"{rate:.0f}%"
    fb_str = f"{int(sum(avg_fb) / len(avg_fb))}ms" if avg_fb else "—"
    core = f"{rate_str} · {fb_str} · {total_req} 次"
    if tps_v is not None:
        core += f" · ⚡ {ui.fmt_tps(tps_v)}"
    if cache_phrase:
        core += f" · {cache_phrase}"
    return core


def _mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 10:
        return key[0] + "***"
    return key[:6] + "***" + key[-4:]


# ─── 渠道列表 ─────────────────────────────────────────────────────

def _list_text_and_kb() -> tuple[str, dict]:
    chans = [ch for ch in registry.all_channels() if ch.type == "api"]
    total = len(chans)

    lines = [f"🔀 <b>渠道管理</b>", f"共 {total} 个"]
    if total == 0:
        lines.append("\n暂无渠道，点「➕ 添加渠道」创建。")

    # 本月 channel 级加权平均 TPS（按 channel 维度聚合 tokens/denom）
    month_ts = _month_start_ts()

    rows: list[list[dict]] = []
    current: list[dict] = []
    for ch in chans:
        icon, status = _channel_health(ch)
        try:
            ch_stats = log_db.tokens_for_channel(ch.key, since_ts=month_ts)
            ch_tps = ch_stats.get("avg_tps")
            ch_prompt = ui.prompt_total(ch_stats.get("input"), ch_stats.get("cache_creation"), ch_stats.get("cache_read"))
            ch_cache = (
                ui.fmt_cache_phrase(ch_stats.get("cache_read"), ch_prompt)
                if (ch_stats.get("cache_read") or 0) > 0 else ""
            )
        except Exception:
            ch_tps = None
            ch_cache = ""
        summary = _summary_text(ch, tps_v=ch_tps, cache_phrase=ch_cache)
        lines.append("")
        lines.append(f"{icon} <b>{ui.escape_html(ch.display_name)}</b> — {ui.escape_html(status)}")
        lines.append(f"  模型: {len(ch.models)} 个 · {ui.escape_html(summary)}")
        short = ui.register_code(ch.display_name)
        current.append(ui.btn(f"{icon} {ch.display_name}", f"ch:view:{short}"))
        if len(current) >= 2:
            rows.append(current)
            current = []
    if current:
        rows.append(current)

    rows.append([
        ui.btn("➕ 添加渠道", "chw:start"),
        ui.btn("🧹 清全部错误", "ch:clear_errors_all"),
    ])
    rows.append([
        ui.btn("🔗 清全部亲和", "ch:clear_affinity_all"),
    ])
    rows.append([ui.btn("◀ 返回主菜单", "menu:main")])

    text = ui.truncate("\n".join(lines))
    return text, ui.inline_kb(rows)


def show(chat_id: int, message_id: int, cb_id: Optional[str] = None) -> None:
    if cb_id is not None:
        ui.answer_cb(cb_id)
    text, kb = _list_text_and_kb()
    ui.edit(chat_id, message_id, text, reply_markup=kb)


def send_new(chat_id: int) -> None:
    text, kb = _list_text_and_kb()
    ui.send(chat_id, text, reply_markup=kb)


# ─── 渠道详情 ─────────────────────────────────────────────────────

def _channel_model_lines(ch) -> list[str]:
    lines = []
    now = int(time.time() * 1000)
    perfs = {s["model"]: s for s in scorer.snapshot() if s["channel_key"] == ch.key}
    cd_map = {e["model"]: e for e in cooldown.active_entries() if e["channel_key"] == ch.key}

    # 本月每个 model 的 TPS / 次数
    try:
        model_stats = log_db.channel_model_stats(ch.key, since_ts=_month_start_ts())
    except Exception:
        model_stats = []
    stats_by_model = {s["final_model"]: s for s in model_stats}

    for m in ch.models:
        alias = m.get("alias")
        real = m.get("real")
        line = f"  • <code>{ui.escape_html(alias)}</code>"
        if real != alias:
            line += f" → <code>{ui.escape_html(real)}</code>"
        perf = perfs.get(real)
        cd = cd_map.get(real)

        if cd:
            if cd["cooldown_until"] == -1:
                line += " 🔴 <b>永久冷却</b>"
            else:
                remaining = max(0, (cd["cooldown_until"] - now) // 1000)
                line += f" 🟠 冷却 {remaining}s"
        else:
            if perf and perf["recent_requests"] > 0:
                rate = (perf["recent_success_count"] / perf["recent_requests"]) * 100
                icon = "🟢" if rate >= 80 else ("🟡" if rate >= 50 else "🔴")
                line += f" {icon} {rate:.0f}%"
            else:
                line += " ⚪ 暂无数据"
        lines.append(line)

        if perf and perf["total_requests"] > 0:
            stats_line = (
                f"    请求 {perf['total_requests']} · "
                f"连接 {perf['avg_connect_ms']}ms · "
                f"首字 {perf['avg_first_byte_ms']}ms · "
                f"score {perf['score']}"
            )
            lines.append(stats_line)

        ms = stats_by_model.get(real)
        if ms:
            m_prompt = ui.prompt_total(ms.get("input"), ms.get("cache_creation"), ms.get("cache_read"))
            token_line = f"    ↑ {ui.fmt_tokens(m_prompt)} · ↓ {ui.fmt_tokens(ms.get('output'))}"
            if (ms.get("cache_read") or 0) > 0:
                token_line += f" · {ui.fmt_cache_phrase(ms.get('cache_read'), m_prompt)}"
            lines.append(token_line)
        if ms and ms.get("avg_tps") is not None:
            lines.append(
                f"    ⚡ TPS: 平均 {ui.fmt_tps(ms['avg_tps'])} · "
                f"峰值 {ui.fmt_tps(ms.get('max_tps'))} · "
                f"最低 {ui.fmt_tps(ms.get('min_tps'))}"
            )
    return lines


def _detail_text_and_kb(name: str) -> tuple[Optional[str], Optional[dict]]:
    ch = registry.get_channel(f"api:{name}")
    if ch is None or ch.type != "api":
        return None, None

    icon, status = _channel_health(ch)
    enabled = ch.enabled and not ch.disabled_reason
    protocol = _protocol_of(ch)

    api_path = getattr(ch, "api_path", None)
    # 展示完整 URL：apiPath 非空时拼完整，否则只给 baseUrl
    url_display = ch.base_url + api_path if api_path else ch.base_url
    lines = [
        f"{icon} <b>{ui.escape_html(ch.display_name)}</b>",
        "",
        f"🔗 URL: <code>{ui.escape_html(url_display)}</code>",
        f"🔑 Key: <code>{ui.escape_html(_mask_key(ch.api_key))}</code>",
    ]
    # 只在非 anthropic 时显示协议行，避免对现有 anthropic 渠道造成视觉噪声
    if protocol != "anthropic":
        lines.append(f"🔌 协议: <code>{ui.escape_html(_PROTOCOL_LABEL.get(protocol, protocol))}</code>")
    lines += [
        f"🎭 CC 伪装: <code>{'开启' if ch.cc_mimicry else '关闭'}</code>",
        f"⚡ 并发上限: <code>{getattr(ch, 'max_concurrent', 0) or '默认'}</code>",
        f"{'✅' if enabled else '⬛'} 状态: <code>{'enabled' if enabled else (ch.disabled_reason or 'disabled')}</code>",
        "",
        f"<b>📋 模型 ({len(ch.models)} 个)</b>",
    ]
    lines.extend(_channel_model_lines(ch))

    # 亲和绑定数
    bound = sum(1 for v in affinity.snapshot().values() if v["channel_key"] == ch.key)
    lines.append("")
    lines.append(f"🔗 亲和绑定: {bound} 个会话")

    short = ui.register_code(ch.display_name)
    toggle_label = "⬛ 禁用" if enabled else "✅ 启用"
    rows = [
        [ui.btn("🧪 测试模型", f"ch:test:{short}"), ui.btn("✏ 编辑", f"ch:edit:{short}")],
        [ui.btn("🧹 清错误", f"ch:clear_errors:{short}"),
         ui.btn("🔗 清亲和", f"ch:clear_affinity:{short}")],
        [ui.btn(toggle_label, f"ch:toggle:{short}"),
         ui.btn("🗑 删除", f"ch:del:{short}")],
        [ui.btn("◀ 返回列表", "menu:channel")],
    ]
    return ui.truncate("\n".join(lines)), ui.inline_kb(rows)


def on_view(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    name = ui.resolve_code(short)
    if not name:
        ui.answer_cb(cb_id, "短码已失效")
        show(chat_id, message_id)
        return
    ui.answer_cb(cb_id)
    text, kb = _detail_text_and_kb(name)
    if text is None:
        ui.edit(chat_id, message_id, f"⚠ 渠道 <code>{ui.escape_html(name)}</code> 不存在",
                reply_markup=ui.inline_kb([[ui.btn("◀ 返回列表", "menu:channel")]]))
        return
    ui.edit(chat_id, message_id, text, reply_markup=kb)


# ─── 启停 / 清错误 / 清亲和 / 删除 ───────────────────────────────

def on_toggle(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    name = ui.resolve_code(short)
    if not name:
        ui.answer_cb(cb_id, "短码已失效")
        return
    ch = registry.get_channel(f"api:{name}")
    if ch is None:
        ui.answer_cb(cb_id, "渠道不存在")
        return
    new_enabled = not (ch.enabled and not ch.disabled_reason)
    registry.update_api_channel(name, {"enabled": new_enabled})
    ui.answer_cb(cb_id, "已启用" if new_enabled else "已禁用")
    text, kb = _detail_text_and_kb(name)
    if text:
        ui.edit(chat_id, message_id, text, reply_markup=kb)


def on_clear_errors(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    name = ui.resolve_code(short)
    if not name:
        ui.answer_cb(cb_id, "短码已失效")
        return
    cooldown.clear(f"api:{name}", model=None)
    ui.answer_cb(cb_id, "已清除")
    text, kb = _detail_text_and_kb(name)
    if text:
        ui.edit(chat_id, message_id, text, reply_markup=kb)


def on_clear_affinity(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    name = ui.resolve_code(short)
    if not name:
        ui.answer_cb(cb_id, "短码已失效")
        return
    affinity.delete_by_channel(f"api:{name}")
    ui.answer_cb(cb_id, "已清空亲和")
    text, kb = _detail_text_and_kb(name)
    if text:
        ui.edit(chat_id, message_id, text, reply_markup=kb)


def on_clear_errors_all(chat_id: int, message_id: int, cb_id: str) -> None:
    cooldown.clear_all()
    ui.answer_cb(cb_id, "已全部清除")
    show(chat_id, message_id)


def on_clear_affinity_all(chat_id: int, message_id: int, cb_id: str) -> None:
    affinity.delete_all()
    ui.answer_cb(cb_id, "已全部清空")
    show(chat_id, message_id)


def on_delete_ask(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    name = ui.resolve_code(short)
    if not name:
        ui.answer_cb(cb_id, "短码已失效")
        return
    ch = registry.get_channel(f"api:{name}")
    if ch is None:
        ui.answer_cb(cb_id, "渠道不存在")
        return
    ui.answer_cb(cb_id)
    ui.edit(
        chat_id, message_id,
        "⚠ <b>确认删除渠道？</b>\n\n"
        f"• 名称: <code>{ui.escape_html(ch.display_name)}</code>\n"
        f"• URL: <code>{ui.escape_html((ch.base_url + getattr(ch, 'api_path', '')) if getattr(ch, 'api_path', None) else ch.base_url)}</code>\n"
        f"• 模型: {len(ch.models)} 个\n\n"
        "此操作会同时清除该渠道所有统计、冷却、亲和数据，不可恢复。",
        reply_markup=ui.inline_kb([[
            ui.btn("✅ 确认删除", f"ch:del_exec:{short}"),
            ui.btn("❌ 取消",     f"ch:view:{short}"),
        ]]),
    )


def on_delete_exec(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    name = ui.resolve_code(short)
    if not name:
        ui.answer_cb(cb_id, "短码已失效")
        show(chat_id, message_id)
        return
    ok = registry.delete_api_channel(name)
    if ok:
        ui.answer_cb(cb_id, "已删除")
        ui.edit(chat_id, message_id, f"✅ 已删除 <code>{ui.escape_html(name)}</code>")
        show(chat_id, message_id)
    else:
        ui.answer_cb(cb_id, "删除失败")


# ─── 添加向导 ─────────────────────────────────────────────────────

_WIZ_NAV = [ui.btn("❌ 取消", "chw:cancel")]


def wiz_start(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    states.set_state(chat_id, "ch_wiz_name", {})
    ui.edit(
        chat_id, message_id,
        "➕ <b>添加渠道（1/5）</b>\n\n请输入渠道名称（将显示在列表中；空格、中文均可）：",
        reply_markup=ui.inline_kb([_WIZ_NAV]),
    )


def wiz_cancel(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id, "已取消")
    states.pop_state(chat_id)
    show(chat_id, message_id)


def wiz_on_name_input(chat_id: int, text: str) -> None:
    name = (text or "").strip()
    if not name:
        ui.send(chat_id, "❌ 名称不能为空，请重新输入：")
        return
    if len(name) > 64:
        ui.send(chat_id, "❌ 名称过长（上限 64 字符），请重新输入：")
        return
    cfg = config.get()
    if any(c.get("name") == name for c in cfg.get("channels", [])):
        ui.send(chat_id, f"❌ 渠道名称 <code>{ui.escape_html(name)}</code> 已存在，请换一个：")
        return

    states.set_state(chat_id, "ch_wiz_url", {"name": name})
    ui.send(
        chat_id,
        "✅ 名称已设置\n\n"
        "➕ <b>添加渠道（2/5）</b>\n\n"
        "请输入上游 <b>Base URL</b>（需以 <code>http://</code> 或 <code>https://</code> 开头）\n\n"
        "<i>只需填上游域名或 API 根路径，代理会根据下一步所选协议自动追加对应子路径：</i>\n"
        "• Anthropic → <code>/v1/messages</code>\n"
        "• OpenAI Chat → <code>/v1/chat/completions</code>\n"
        "• OpenAI Responses → <code>/v1/responses</code>\n\n"
        "<i>如果上游接口路径非标准（比如智谱 Coding Plan 的 "
        "<code>/api/coding/paas/v4/chat/completions</code>），"
        "直接把<b>完整调用路径</b>贴进来即可，系统会自动识别并拆分。</i>\n\n"
        "示例：<code>https://api.example.com</code>",
        reply_markup=ui.inline_kb([_WIZ_NAV]),
    )


def wiz_on_url_input(chat_id: int, text: str) -> None:
    url = (text or "").strip().rstrip("/")
    if not (url.startswith("http://") or url.startswith("https://")):
        ui.send(chat_id, "❌ URL 需以 http:// 或 https:// 开头，请重新输入：")
        return
    state = states.get_state(chat_id)
    if not state:
        ui.send(chat_id, "❌ 会话过期，请重新添加")
        return
    data = state["data"]
    # 自动识别完整路径：末段命中 messages/completions/responses 则拆分
    try:
        split_base, split_path = split_base_url(url)
    except ValueError as exc:
        ui.send(chat_id, f"❌ URL 无效：{ui.escape_html(str(exc))}")
        return
    data["baseUrl"] = split_base
    if split_path:
        data["apiPath"] = split_path
    else:
        data.pop("apiPath", None)
    states.set_state(chat_id, "ch_wiz_protocol", data)
    _wiz_send_protocol_panel(chat_id)


def _wiz_send_protocol_panel(chat_id: int) -> None:
    rows = [[ui.btn(label, f"chw:proto:{proto}")] for proto, label in PROTOCOL_CHOICES]
    rows.append(_WIZ_NAV)
    state = states.get_state(chat_id) or {}
    data = state.get("data") or {}
    api_path = data.get("apiPath")
    head = "✅ URL 已设置\n\n"
    if api_path:
        # 提示已自动拆分，建议用户按 apiPath 末段对应的协议选
        detected = detect_suffix_protocol(api_path)
        detected_label = _PROTOCOL_LABEL.get(detected, "?") if detected else "?"
        head = (
            "✅ URL 已设置（检测到完整路径，已自动拆分）\n"
            f"     • baseUrl: <code>{ui.escape_html(data.get('baseUrl',''))}</code>\n"
            f"     • apiPath: <code>{ui.escape_html(api_path)}</code>\n"
            f"     • 建议协议: <b>{ui.escape_html(detected_label)}</b>\n\n"
        )
    ui.send(
        chat_id,
        head +
        "➕ <b>添加渠道（3/5）</b>\n\n"
        "请选择该渠道的上游协议：\n\n"
        "• <b>Anthropic</b> — 对接 Claude 风格 <code>/v1/messages</code>，支持 CC 伪装（默认）\n"
        "• <b>OpenAI Chat</b> — 对接 <code>/v1/chat/completions</code> 兼容上游（DeepSeek、智谱等）\n"
        "• <b>OpenAI Responses</b> — 对接 <code>/v1/responses</code>（gpt-5 / o 系列 / 新 Responses API）",
        reply_markup=ui.inline_kb(rows),
    )


def _wiz_proceed_to_key_step(chat_id: int, message_id: int, data: dict, protocol: str) -> None:
    """进入步骤 4（输入 API Key），公共逻辑。"""
    data["protocol"] = protocol
    states.set_state(chat_id, "ch_wiz_key", data)
    ui.edit(
        chat_id, message_id,
        f"✅ 协议：<code>{ui.escape_html(_PROTOCOL_LABEL[protocol])}</code>\n\n"
        "➕ <b>添加渠道（4/5）</b>\n\n请输入该渠道的 API Key：",
        reply_markup=ui.inline_kb([_WIZ_NAV]),
    )


def wiz_on_protocol_select(chat_id: int, message_id: int, cb_id: str, protocol: str) -> None:
    if protocol not in _PROTOCOL_LABEL:
        ui.answer_cb(cb_id, "无效协议")
        return
    state = states.get_state(chat_id)
    if not state or state.get("action") != "ch_wiz_protocol":
        ui.answer_cb(cb_id, "会话已过期")
        return
    data = state["data"]
    api_path = data.get("apiPath")
    detected = detect_suffix_protocol(api_path) if api_path else None
    # apiPath 识别的协议 != 用户选的协议 → 弹确认面板
    if api_path and detected and detected != protocol:
        ui.answer_cb(cb_id)
        detected_label = _PROTOCOL_LABEL.get(detected, detected)
        chosen_label = _PROTOCOL_LABEL.get(protocol, protocol)
        ui.edit(
            chat_id, message_id,
            "⚠ <b>协议与路径不匹配</b>\n\n"
            f"您选择的协议：<code>{ui.escape_html(chosen_label)}</code>\n"
            f"识别到的路径：<code>{ui.escape_html(api_path)}</code>\n"
            f"路径对应协议：<b>{ui.escape_html(detected_label)}</b>\n\n"
            "如何处理？",
            reply_markup=ui.inline_kb([
                [ui.btn(f"✅ 使用 {detected_label}（推荐）", f"chw:proto_adopt:{detected}")],
                [ui.btn(f"⚠ 坚持 {chosen_label}，清空自定义路径", f"chw:proto_force:{protocol}")],
                [ui.btn("◀ 返回修改 URL", "chw:back_to_url")],
            ]),
        )
        return
    ui.answer_cb(cb_id, _PROTOCOL_LABEL[protocol])
    _wiz_proceed_to_key_step(chat_id, message_id, data, protocol)


def wiz_proto_adopt(chat_id: int, message_id: int, cb_id: str, protocol: str) -> None:
    """冲突解决：采用 apiPath 对应的协议。"""
    if protocol not in _PROTOCOL_LABEL:
        ui.answer_cb(cb_id, "无效协议")
        return
    state = states.get_state(chat_id)
    if not state or state.get("action") != "ch_wiz_protocol":
        ui.answer_cb(cb_id, "会话已过期")
        return
    ui.answer_cb(cb_id, f"已采用 {_PROTOCOL_LABEL[protocol]}")
    _wiz_proceed_to_key_step(chat_id, message_id, state["data"], protocol)


def wiz_proto_force(chat_id: int, message_id: int, cb_id: str, protocol: str) -> None:
    """冲突解决：坚持用户选的协议，清空自动拆分的 apiPath。"""
    if protocol not in _PROTOCOL_LABEL:
        ui.answer_cb(cb_id, "无效协议")
        return
    state = states.get_state(chat_id)
    if not state or state.get("action") != "ch_wiz_protocol":
        ui.answer_cb(cb_id, "会话已过期")
        return
    data = state["data"]
    data.pop("apiPath", None)
    ui.answer_cb(cb_id, "已清空自定义路径")
    _wiz_proceed_to_key_step(chat_id, message_id, data, protocol)


def wiz_back_to_url(chat_id: int, message_id: int, cb_id: str) -> None:
    """冲突解决：返回步骤 2 重新输入 URL。"""
    ui.answer_cb(cb_id)
    state = states.get_state(chat_id)
    if not state:
        return
    data = state["data"]
    data.pop("baseUrl", None)
    data.pop("apiPath", None)
    states.set_state(chat_id, "ch_wiz_url", data)
    ui.edit(
        chat_id, message_id,
        "请重新输入 <b>Base URL</b>（需以 <code>http://</code> 或 <code>https://</code> 开头）：",
        reply_markup=ui.inline_kb([_WIZ_NAV]),
    )


def wiz_on_key_input(chat_id: int, text: str) -> None:
    key = (text or "").strip()
    if len(key) < 5:
        ui.send(chat_id, "❌ API Key 过短，请重新输入：")
        return
    state = states.get_state(chat_id)
    if not state:
        ui.send(chat_id, "❌ 会话过期，请重新添加")
        return
    data = state["data"]
    data["apiKey"] = key
    states.set_state(chat_id, "ch_wiz_models", data)
    ui.send(
        chat_id,
        "✅ API Key 已设置\n\n"
        "➕ <b>添加渠道（5/5）</b>\n\n"
        "请输入模型列表。格式 <code>真实名[:别名]</code>，以 ,/，/;/； 分隔。\n\n"
        "示例：\n"
        "<code>GLM-5:glm-5, GLM-5-Turbo:glm-5-turbo</code>\n"
        "<code>gpt-5.4; gpt-5.3-codex:codex</code>\n\n"
        "不写别名则别名=真实名；别名不可重复。",
        reply_markup=ui.inline_kb([_WIZ_NAV]),
    )


def wiz_on_models_input(chat_id: int, text: str) -> None:
    try:
        models = api_channel.parse_models_input(text or "")
    except ValueError as exc:
        ui.send(chat_id, f"❌ {ui.escape_html(str(exc))}\n请重新输入：")
        return
    state = states.get_state(chat_id)
    if not state:
        ui.send(chat_id, "❌ 会话过期，请重新添加")
        return
    data = state["data"]
    data["models"] = models
    data["test_results"] = {}   # real_model → (ok, elapsed_ms, reason)
    states.set_state(chat_id, "ch_wiz_test", data)
    _wiz_send_test_panel(chat_id, data)


def _wiz_test_kb(data: dict) -> dict:
    rows: list[list[dict]] = []
    # 每行放 1-2 个模型按钮
    current: list[dict] = []
    for i, m in enumerate(data["models"]):
        real = m["real"]
        status = data.get("test_results", {}).get(real)
        label = m["alias"] if m["alias"] == real else f"{m['alias']}({real})"
        if status is None:
            prefix = "🧪 "
        elif status[0]:
            prefix = "✅ "
        else:
            prefix = "❌ "
        current.append(ui.btn(f"{prefix}{label}", f"chw:test:{i}"))
        if len(current) >= 2:
            rows.append(current)
            current = []
    if current:
        rows.append(current)
    rows.append([
        ui.btn("🧪 测试全部模型", "chw:test_all"),
        ui.btn("⏭ 跳过测试", "chw:skip_test"),
    ])
    # 至少一个测试成功才允许保存
    any_ok = any(r[0] for r in data.get("test_results", {}).values())
    save_row = []
    if any_ok:
        save_row.append(ui.btn("💾 保存渠道", "chw:save"))
    save_row.append(ui.btn("◀ 返回上一步", "chw:back"))
    rows.append(save_row)
    rows.append([ui.btn("❌ 取消", "chw:cancel")])
    return ui.inline_kb(rows)


def _wiz_test_intro(data: dict) -> str:
    header = (
        "🧪 <b>渠道测试</b>\n\n"
        f"渠道: <code>{ui.escape_html(data['name'])}</code>\n"
        f"模型: {len(data['models'])} 个\n\n"
        "请选择模型进行联通性测试。至少有一个模型测试成功才能保存渠道。\n"
        "<i>（若跳过测试，全部模型默认标记为可用，由后台探测机制处理后续）</i>"
    )
    results = data.get("test_results") or {}
    if results:
        header += "\n\n<b>测试结果</b>:"
        for m in data["models"]:
            real = m["real"]
            r = results.get(real)
            if r is None:
                continue
            ok, elapsed, reason = r
            name = m["alias"]
            if ok:
                header += f"\n  ✅ <code>{ui.escape_html(name)}</code> — 耗时 {elapsed}ms"
            else:
                header += f"\n  ❌ <code>{ui.escape_html(name)}</code> — {ui.escape_html((reason or '')[:80])}"
    return header


def _wiz_send_test_panel(chat_id: int, data: dict) -> None:
    ui.send(chat_id, _wiz_test_intro(data), reply_markup=_wiz_test_kb(data))


def _wiz_refresh_test_panel(chat_id: int, msg_id: int, data: dict) -> None:
    ui.edit(chat_id, msg_id, _wiz_test_intro(data), reply_markup=_wiz_test_kb(data))


def wiz_back_to_models(chat_id: int, message_id: int, cb_id: str) -> None:
    """返回到步骤 4（重新输入模型列表）。"""
    ui.answer_cb(cb_id)
    state = states.get_state(chat_id)
    if not state or "data" not in state:
        wiz_cancel(chat_id, message_id, cb_id)
        return
    data = state["data"]
    # 清除测试结果
    data.pop("test_results", None)
    states.set_state(chat_id, "ch_wiz_models", data)
    ui.edit(
        chat_id, message_id,
        "请重新输入模型列表（格式同上）：",
        reply_markup=ui.inline_kb([_WIZ_NAV]),
    )


# ─── 测试：单个模型 / 全部 / 跳过 ─────────────────────────────────

def _make_temp_channel(data: dict):
    protocol = data.get("protocol") or "anthropic"
    entry = {
        "name": data["name"] + "__wiz",
        "type": "api",
        "baseUrl": data["baseUrl"],
        "apiKey": data["apiKey"],
        "protocol": protocol,
        "models": data["models"],
        "cc_mimicry": protocol == "anthropic",
        "enabled": True,
    }
    if data.get("apiPath"):
        entry["apiPath"] = data["apiPath"]
    # openai-* 协议走 OpenAIApiChannel；anthropic 走 ApiChannel
    if protocol == "anthropic":
        return api_channel.ApiChannel(entry)
    from ...openai.channel.api_channel import OpenAIApiChannel
    return OpenAIApiChannel(entry)


async def _probe_with_progress_async(chat_id: int, msg_id: int, header: str,
                                     ch, real_model: str) -> tuple[bool, int, Optional[str], str]:
    """在 async 上下文中跑 probe + 进度更新。

    完成后 edit 同一条消息显示结果。返回 (ok, elapsed_ms, reason, final_text)。
    final_text 供调用方在此基础上追加"将自动删除"提示。
    """
    state = {"text": header}

    async def progress_cb(line: str) -> None:
        state["text"] += f"\n{line}"
        ui.edit(chat_id, msg_id, state["text"])

    try:
        ok, elapsed, reason = await probe.probe_with_progress(
            ch, real_model,
            progress_cb=progress_cb,
            timeout_s=None,
            progress_interval=10,
        )
    except Exception as exc:
        state["text"] += f"\n[×] 测试异常：{ui.escape_html(str(exc))}"
        ui.edit(chat_id, msg_id, state["text"])
        return False, 0, str(exc), state["text"]

    if ok:
        state["text"] += f"\n[√] 模型测试成功，耗时: {elapsed}ms"
        # 手动测试成功 → 自动清除该 (渠道, 模型) 的冷却 / 永久禁用状态
        # 复用 probe recovery loop 的 cooldown.clear 路径，避免用户还要手动点"清错误"
        try:
            prev = cooldown.get_state(ch.key, real_model)
            if prev and (prev.get("cooldown_until") is not None
                         or int(prev.get("error_count", 0)) > 0):
                was_permanent = prev.get("cooldown_until") == -1
                cooldown.clear(ch.key, real_model)
                if was_permanent:
                    state["text"] += "\n[✓] 已自动解除永久冷却"
                else:
                    state["text"] += "\n[✓] 已自动清除冷却与失败计数"
        except Exception as exc:
            print(f"[channel_menu] auto-clear cooldown on test success failed: {exc}")
    else:
        state["text"] += f"\n[×] 模型测试失败，失败原因: {ui.escape_html(reason or '未知错误')}"
    ui.edit(chat_id, msg_id, state["text"])
    return ok, elapsed, reason, state["text"]


def _run_test_with_progress(chat_id: int, msg_id: int, header: str,
                            ch, real_model: str) -> tuple[bool, int, Optional[str]]:
    """同步包装：仅在添加向导的"逐个测试"路径中使用，因为后续要更新 test_results。

    长时间阻塞的"全部测试"和"已存在渠道测试全部"已改为后台线程模式，
    不再走这里。
    """
    return _run_sync(
        _probe_with_progress_async(chat_id, msg_id, header, ch, real_model)
    )


def wiz_test_single(chat_id: int, message_id: int, cb_id: str, idx_str: str) -> None:
    """后台线程跑单模型测试，TG polling 不阻塞。"""
    ui.answer_cb(cb_id, "测试已开始")
    state = states.get_state(chat_id)
    if not state or state.get("action") != "ch_wiz_test":
        ui.send(chat_id, "❌ 会话已过期，请重新添加")
        return
    data = state["data"]
    try:
        idx = int(idx_str)
        m = data["models"][idx]
    except (ValueError, IndexError):
        return

    ch = _make_temp_channel(data)
    real, alias = m["real"], m["alias"]

    header = (
        f"🧪 正在测试 [{ui.escape_html(data['name'])}] 渠道 "
        f"{ui.escape_html(alias)} 模型…\n（最长 60s，期间可继续操作）"
    )
    msg = ui.send(chat_id, header)
    if not msg or not msg.get("ok"):
        return
    progress_msg_id = msg["result"]["message_id"]

    async def _run():
        ok, elapsed, reason, final_text = await _probe_with_progress_async(
            chat_id, progress_msg_id, header, ch, real,
        )
        cur = states.get_state(chat_id)
        if cur and cur.get("action") == "ch_wiz_test":
            cur_data = cur["data"]
            cur_data.setdefault("test_results", {})[real] = (ok, elapsed, reason)
            states.set_state(chat_id, "ch_wiz_test", cur_data)
            _wiz_refresh_test_panel(chat_id, message_id, cur_data)
        await _finalize_and_delete(chat_id, progress_msg_id, final_text, ok)

    _spawn_async_task(_run, name=f"wiz-test-{chat_id}-{idx}")


def wiz_test_all(chat_id: int, message_id: int, cb_id: str) -> None:
    """后台线程批量测试所有模型，TG polling 不阻塞。"""
    ui.answer_cb(cb_id, "测试已开始")
    state = states.get_state(chat_id)
    if not state or state.get("action") != "ch_wiz_test":
        ui.send(chat_id, "❌ 会话已过期，请重新添加")
        return
    data = state["data"]
    ch = _make_temp_channel(data)

    msg = ui.send(
        chat_id,
        f"🧪 开始测试 [{ui.escape_html(data['name'])}] 全部 {len(data['models'])} 个模型…\n"
        "（每个模型最长 60s，期间可继续操作其他菜单）"
    )
    if not msg or not msg.get("ok"):
        return
    progress_msg_id = msg["result"]["message_id"]

    async def _run_all():
        accumulated = ""
        results: dict[str, tuple[bool, int, Optional[str]]] = {}
        for m in data["models"]:
            real, alias = m["real"], m["alias"]
            header_line = (f"{accumulated}\n\n" if accumulated else "") + (
                f"🧪 正在测试 [{ui.escape_html(data['name'])}] 渠道 "
                f"{ui.escape_html(alias)} 模型…"
            )
            ok, elapsed, reason, _ = await _probe_with_progress_async(
                chat_id, progress_msg_id, header_line, ch, real,
            )
            results[real] = (ok, elapsed, reason)
            accumulated = header_line + (
                f"\n[√] 模型测试成功，耗时: {elapsed}ms" if ok
                else f"\n[×] 模型测试失败，失败原因: {ui.escape_html(reason or '未知错误')}"
            )
        # 测试完成后回写状态机 + 刷新原测试面板
        cur = states.get_state(chat_id)
        if cur and cur.get("action") == "ch_wiz_test":
            cur_data = cur["data"]
            cur_data.setdefault("test_results", {}).update(results)
            states.set_state(chat_id, "ch_wiz_test", cur_data)
            _wiz_refresh_test_panel(chat_id, message_id, cur_data)
        if results:
            all_ok = all(v[0] for v in results.values())
            await _finalize_and_delete(chat_id, progress_msg_id, accumulated, all_ok)

    _spawn_async_task(_run_all, name=f"wiz-test-all-{chat_id}")


def wiz_skip_test(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id, "跳过测试，已保存")
    state = states.get_state(chat_id)
    if not state:
        return
    data = state["data"]
    protocol = data.get("protocol") or "anthropic"
    try:
        registry.add_api_channel({
            "name": data["name"],
            "baseUrl": data["baseUrl"],
            "apiPath": data.get("apiPath"),
            "apiKey": data["apiKey"],
            "protocol": protocol,
            "models": data["models"],
            "cc_mimicry": protocol == "anthropic",
            "enabled": True,
        })
    except Exception as exc:
        ui.send(chat_id, f"❌ 保存失败: <code>{ui.escape_html(str(exc))}</code>")
        return
    states.pop_state(chat_id)
    ui.edit(
        chat_id, message_id,
        f"✅ <b>渠道已保存（跳过测试）</b>\n\n"
        f"名称: <code>{ui.escape_html(data['name'])}</code>\n"
        f"协议: <code>{ui.escape_html(_PROTOCOL_LABEL[protocol])}</code>\n"
        "所有模型标记为「可用」，后台 probe 会持续验证真实可用性。",
        reply_markup=ui.inline_kb([
            [ui.btn("◀ 返回渠道列表", "menu:channel"),
             ui.btn("🏠 主菜单", "menu:main")],
        ]),
    )


def wiz_save(chat_id: int, message_id: int, cb_id: str) -> None:
    state = states.get_state(chat_id)
    if not state:
        ui.answer_cb(cb_id, "会话过期")
        return
    data = state["data"]
    protocol = data.get("protocol") or "anthropic"
    results = data.get("test_results") or {}
    any_ok = any(v[0] for v in results.values())
    if not any_ok:
        ui.answer_cb(cb_id, "需要至少一个模型测试成功", show_alert=True)
        return
    try:
        registry.add_api_channel({
            "name": data["name"],
            "baseUrl": data["baseUrl"],
            "apiPath": data.get("apiPath"),
            "apiKey": data["apiKey"],
            "protocol": protocol,
            "models": data["models"],
            "cc_mimicry": protocol == "anthropic",
            "enabled": True,
        })
    except Exception as exc:
        ui.send(chat_id, f"❌ 保存失败: <code>{ui.escape_html(str(exc))}</code>")
        ui.answer_cb(cb_id, "失败")
        return

    # 失败的模型：记入初始冷却（errorWindows[0]，默认 1 分钟）
    # 避免启用即被调度到；冷却期到后 probe 会自动重测
    for m in data["models"]:
        real = m["real"]
        r = results.get(real)
        if r and not r[0]:
            cooldown.record_error(f"api:{data['name']}", real, f"initial probe failed: {r[2]}")

    states.pop_state(chat_id)
    ui.answer_cb(cb_id, "已保存")
    ok_names = [m["alias"] for m in data["models"] if (results.get(m["real"]) or (False,))[0]]
    fail_names = [m["alias"] for m in data["models"] if not (results.get(m["real"]) or (False,))[0]]
    ok_display = ", ".join(ui.escape_html(n) for n in ok_names) or "-"
    fail_display = ", ".join(ui.escape_html(n) for n in fail_names)
    summary = (
        f"✅ <b>渠道已保存</b>: <code>{ui.escape_html(data['name'])}</code>\n\n"
        f"协议: <code>{ui.escape_html(_PROTOCOL_LABEL[protocol])}</code>\n"
        f"可用模型 ({len(ok_names)}): {ok_display}\n"
    )
    if fail_names:
        summary += f"不可用（已加入冷却） ({len(fail_names)}): {fail_display}"
    # edit 同一消息显示结果 + 导航；用户点击按钮返回列表，避免双消息
    ui.edit(chat_id, message_id, summary, reply_markup=ui.inline_kb([
        [ui.btn("◀ 返回渠道列表", "menu:channel"),
         ui.btn("🏠 主菜单", "menu:main")],
    ]))


# ─── 测试面板（已存在渠道；不影响 cooldown） ────────────────────

def on_test_panel(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    ui.answer_cb(cb_id)
    name = ui.resolve_code(short)
    ch = registry.get_channel(f"api:{name}") if name else None
    if ch is None:
        ui.edit(chat_id, message_id, "⚠ 渠道不存在",
                reply_markup=ui.inline_kb([[ui.btn("◀ 返回", "menu:channel")]]))
        return
    rows: list[list[dict]] = []
    current: list[dict] = []
    for i, m in enumerate(ch.models):
        label = m["alias"] if m["alias"] == m["real"] else f"{m['alias']}({m['real']})"
        current.append(ui.btn(f"🧪 {label}", f"ch:t1:{short}:{i}"))
        if len(current) >= 2:
            rows.append(current)
            current = []
    if current:
        rows.append(current)
    rows.append([ui.btn("🧪 测试全部", f"ch:tall:{short}")])
    rows.append([ui.btn("◀ 返回详情", f"ch:view:{short}")])
    ui.edit(
        chat_id, message_id,
        f"🧪 <b>测试 [{ui.escape_html(ch.display_name)}]</b>\n\n"
        f"模型: {len(ch.models)} 个\n<i>本次测试不会修改冷却状态，只反映联通性。</i>",
        reply_markup=ui.inline_kb(rows),
    )


def on_test_single(chat_id: int, message_id: int, cb_id: str, short: str, idx_str: str) -> None:
    """后台线程测单个模型，不阻塞 polling。"""
    ui.answer_cb(cb_id, "测试已开始")
    name = ui.resolve_code(short)
    ch = registry.get_channel(f"api:{name}") if name else None
    if ch is None:
        return
    try:
        idx = int(idx_str)
        m = ch.models[idx]
    except (ValueError, IndexError):
        return
    header = (
        f"🧪 正在测试 [{ui.escape_html(ch.display_name)}] 渠道 "
        f"{ui.escape_html(m['alias'])} 模型…\n（最长 60s，期间可继续操作）"
    )
    sent = ui.send(chat_id, header)
    if not sent or not sent.get("ok"):
        return
    progress_msg_id = sent["result"]["message_id"]

    async def _run():
        ok, _, _, final_text = await _probe_with_progress_async(
            chat_id, progress_msg_id, header, ch, m["real"],
        )
        await _finalize_and_delete(chat_id, progress_msg_id, final_text, ok)

    _spawn_async_task(_run, name=f"chtest-{chat_id}-{idx}")


def on_test_all(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    """后台线程批量测试已存在渠道的所有模型，不阻塞 polling。"""
    ui.answer_cb(cb_id, "测试已开始")
    name = ui.resolve_code(short)
    ch = registry.get_channel(f"api:{name}") if name else None
    if ch is None:
        return
    sent = ui.send(
        chat_id,
        f"🧪 开始测试 [{ui.escape_html(ch.display_name)}] 全部 {len(ch.models)} 个模型…\n"
        "（每个模型最长 60s，期间可继续操作其他菜单）"
    )
    if not sent or not sent.get("ok"):
        return
    progress_msg_id = sent["result"]["message_id"]

    async def _run_all():
        accumulated = ""
        all_ok = True
        for m in ch.models:
            header = (accumulated + "\n\n" if accumulated else "") + (
                f"🧪 正在测试 [{ui.escape_html(ch.display_name)}] 渠道 "
                f"{ui.escape_html(m['alias'])} 模型…"
            )
            ok, elapsed, reason, _ = await _probe_with_progress_async(
                chat_id, progress_msg_id, header, ch, m["real"],
            )
            if not ok:
                all_ok = False
            accumulated = header + (
                f"\n[√] 模型测试成功，耗时: {elapsed}ms" if ok
                else f"\n[×] 模型测试失败，失败原因: {ui.escape_html(reason or '未知错误')}"
            )
        if ch.models:
            await _finalize_and_delete(chat_id, progress_msg_id, accumulated, all_ok)

    _spawn_async_task(_run_all, name=f"chtest-all-{chat_id}")


# ─── 编辑（文本输入） ─────────────────────────────────────────────

def on_edit_menu(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    ui.answer_cb(cb_id)
    name = ui.resolve_code(short)
    ch = registry.get_channel(f"api:{name}") if name else None
    if ch is None:
        return
    cc_label = "🎭 切换 CC 伪装（当前: 开）" if ch.cc_mimicry else "🎭 切换 CC 伪装（当前: 关）"
    protocol = _protocol_of(ch)
    rows = [
        [ui.btn("✏ 名称",   f"ch:ename:{short}"),
         ui.btn("✏ URL",    f"ch:eurl:{short}")],
        [ui.btn("✏ API Key", f"ch:ekey:{short}"),
         ui.btn("✏ 模型列表", f"ch:emodels:{short}")],
        [ui.btn(f"⚡ 并发上限（当前: {getattr(ch, 'max_concurrent', 0) or '默认'}）",
                f"ch:emax:{short}")],
        [ui.btn(f"🔌 切换协议（当前: {_PROTOCOL_LABEL.get(protocol, protocol)}）",
                f"ch:eproto:{short}")],
    ]
    # openai-* 家族下 CC 伪装按钮无效（内部强制 False），隐藏以减少困惑
    if protocol == "anthropic":
        rows.append([ui.btn(cc_label, f"ch:ecc:{short}")])
    rows.append([ui.btn("◀ 返回详情", f"ch:view:{short}")])
    ui.edit(
        chat_id, message_id,
        f"✏ <b>编辑 [{ui.escape_html(ch.display_name)}]</b>\n\n选择要修改的字段：",
        reply_markup=ui.inline_kb(rows),
    )


def on_edit_protocol(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    ui.answer_cb(cb_id)
    name = ui.resolve_code(short)
    ch = registry.get_channel(f"api:{name}") if name else None
    if ch is None:
        return
    current = _protocol_of(ch)
    api_path = getattr(ch, "api_path", None)
    rows: list[list[dict]] = []
    for proto, label in PROTOCOL_CHOICES:
        marker = "● " if proto == current else ""
        rows.append([ui.btn(f"{marker}{label}", f"ch:seproto:{short}:{proto}")])
    rows.append([ui.btn("◀ 返回编辑", f"ch:edit:{short}")])
    extra = ""
    if api_path:
        extra = (
            f"\n<i>⚠ 当前渠道带有自定义 apiPath: <code>{ui.escape_html(api_path)}</code>。"
            "若切换到与该路径末段不匹配的协议，系统会拒绝保存；"
            "可在「✏ URL」里先把 baseUrl 改成无后缀的形式，再来切换协议。</i>"
        )
    ui.edit(
        chat_id, message_id,
        f"🔌 <b>切换协议 [{ui.escape_html(ch.display_name)}]</b>\n\n"
        f"当前：<code>{ui.escape_html(_PROTOCOL_LABEL.get(current, current))}</code>\n\n"
        "<i>切换到 OpenAI 家族会自动关闭 CC 伪装；切回 Anthropic 将恢复。\n"
        "注意：切换协议不自动更新 Base URL / API Key / 模型列表，请按需要另行修改。</i>"
        + extra,
        reply_markup=ui.inline_kb(rows),
    )


def on_edit_url_switch(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    """冲突解决：用新 URL + 切换协议。"""
    state = states.get_state(chat_id)
    if not state or state.get("action") != "ch_edit_url_confirm":
        ui.answer_cb(cb_id, "会话已过期")
        return
    data = state["data"]
    if data.get("short") != short:
        ui.answer_cb(cb_id, "短码不匹配")
        return
    name = ui.resolve_code(short)
    if not name:
        ui.answer_cb(cb_id, "短码已失效")
        return
    try:
        # 同时更新 baseUrl + apiPath + protocol；显式带 apiPath 让 registry 信任 UI
        registry.update_api_channel(name, {
            "baseUrl": data["new_base"],
            "apiPath": data["new_path"],
            "protocol": data["detected"],
        })
    except Exception as exc:
        ui.answer_cb(cb_id, "失败")
        ui.send(chat_id, f"❌ 更新失败: <code>{ui.escape_html(str(exc))}</code>")
        return
    states.pop_state(chat_id)
    ui.answer_cb(cb_id, "已更新并切换协议")
    ui.send_result(
        chat_id, "✅ URL 已更新，协议已自动切换",
        extra_rows=[
            [ui.btn("◀ 返回渠道详情", f"ch:view:{short}")],
            [ui.btn("📋 返回渠道列表", "menu:channel")],
        ],
        back_label="🏠 返回主菜单", back_callback="menu:main",
    )


def on_edit_url_basesonly(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    """冲突解决：只用 baseUrl，清空 apiPath 以适配当前协议。"""
    state = states.get_state(chat_id)
    if not state or state.get("action") != "ch_edit_url_confirm":
        ui.answer_cb(cb_id, "会话已过期")
        return
    data = state["data"]
    if data.get("short") != short:
        ui.answer_cb(cb_id, "短码不匹配")
        return
    name = ui.resolve_code(short)
    if not name:
        ui.answer_cb(cb_id, "短码已失效")
        return
    try:
        # 只留 baseUrl，清空 apiPath（显式传 None）
        registry.update_api_channel(name, {
            "baseUrl": data["new_base"],
            "apiPath": None,
        })
    except Exception as exc:
        ui.answer_cb(cb_id, "失败")
        ui.send(chat_id, f"❌ 更新失败: <code>{ui.escape_html(str(exc))}</code>")
        return
    states.pop_state(chat_id)
    ui.answer_cb(cb_id, "已保留协议，清空自定义路径")
    ui.send_result(
        chat_id, "✅ URL 已更新（只使用 baseUrl，协议未变）",
        extra_rows=[
            [ui.btn("◀ 返回渠道详情", f"ch:view:{short}")],
            [ui.btn("📋 返回渠道列表", "menu:channel")],
        ],
        back_label="🏠 返回主菜单", back_callback="menu:main",
    )


def on_set_protocol(chat_id: int, message_id: int, cb_id: str, short: str, protocol: str) -> None:
    name = ui.resolve_code(short)
    if not name:
        ui.answer_cb(cb_id, "短码已失效")
        return
    if protocol not in _PROTOCOL_LABEL:
        ui.answer_cb(cb_id, "无效协议")
        return
    try:
        registry.update_api_channel(name, {"protocol": protocol})
    except Exception as exc:
        ui.answer_cb(cb_id, "切换失败")
        ui.send(chat_id, f"❌ 切换失败: <code>{ui.escape_html(str(exc))}</code>")
        return
    ui.answer_cb(cb_id, f"已切换至 {_PROTOCOL_LABEL[protocol]}")
    on_edit_menu(chat_id, message_id, "-", short)


def _edit_prompt(chat_id: int, message_id: int, short: str, field: str, prompt: str) -> None:
    states.set_state(chat_id, f"ch_edit_{field}", {"short": short})
    ui.edit(chat_id, message_id, prompt,
            reply_markup=ui.inline_kb([[ui.btn("❌ 取消", f"ch:view:{short}")]]))


def on_edit_name(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    ui.answer_cb(cb_id)
    _edit_prompt(chat_id, message_id, short, "name", "请输入新的渠道名称：")


def on_edit_url(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    ui.answer_cb(cb_id)
    _edit_prompt(
        chat_id, message_id, short, "url",
        "请输入新的 Base URL（http:// 或 https://）：\n\n"
        "<i>如果上游接口路径非标准（比如智谱 Coding Plan 的 "
        "<code>/api/coding/paas/v4/chat/completions</code>），"
        "直接贴完整调用路径即可，系统会自动识别并拆分；"
        "否则系统会根据协议自动追加 <code>/v1/xxx</code>。</i>",
    )


def on_edit_key(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    ui.answer_cb(cb_id)
    _edit_prompt(chat_id, message_id, short, "key", "请输入新的 API Key：")


def on_edit_models(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    ui.answer_cb(cb_id)
    _edit_prompt(chat_id, message_id, short, "models",
                 "请输入新的模型列表（格式 <code>真实名[:别名]</code>，逗号/分号分隔）：")


def on_edit_max_concurrent(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    ui.answer_cb(cb_id)
    _edit_prompt(
        chat_id, message_id, short, "max",
        "请输入该渠道的并发上限（整数 ≥0）：\n"
        "• <code>0</code> = 使用全局默认（「⚙ 系统设置 → ⚡ 并发限制」里配的 defaultMaxConcurrent）\n"
        "• 正整数 = 该渠道同时允许最多 N 个在途请求，超出则排队\n\n"
        "例：<code>5</code>",
    )


def on_edit_cc_toggle(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    name = ui.resolve_code(short)
    ch = registry.get_channel(f"api:{name}") if name else None
    if ch is None:
        ui.answer_cb(cb_id, "渠道不存在")
        return
    try:
        registry.update_api_channel(name, {"cc_mimicry": not ch.cc_mimicry})
    except Exception as exc:
        ui.answer_cb(cb_id, "切换失败")
        ui.send(chat_id, f"❌ 切换失败: {ui.escape_html(str(exc))}")
        return
    ui.answer_cb(cb_id, "已切换")
    on_edit_menu(chat_id, message_id, "-", short)


def _do_edit(chat_id: int, short: str, field: str, value: Any) -> tuple[bool, str]:
    name = ui.resolve_code(short)
    if not name:
        return False, "短码已失效"
    try:
        patch = {field: value}
        if field == "name":
            patch = {"name": value}
        elif field == "baseUrl":
            patch = {"baseUrl": value}
        elif field == "apiKey":
            patch = {"apiKey": value}
        elif field == "models":
            patch = {"models": value}
        elif field == "maxConcurrent":
            patch = {"maxConcurrent": value}
        registry.update_api_channel(name, patch)
    except Exception as exc:
        return False, str(exc)
    return True, patch.get("name", name) if field == "name" else name


def handle_edit_text(chat_id: int, action: str, text: str) -> bool:
    state = states.get_state(chat_id)
    if state is None:
        return False
    short = (state.get("data") or {}).get("short", "")

    def _ok_result(msg: str, target_short: str) -> None:
        ui.send_result(
            chat_id, msg,
            extra_rows=[
                [ui.btn("◀ 返回渠道详情", f"ch:view:{target_short}")],
                [ui.btn("📋 返回渠道列表", "menu:channel")],
            ],
            back_label="🏠 返回主菜单", back_callback="menu:main",
        )

    if action == "ch_edit_name":
        new_name = (text or "").strip()
        if not new_name:
            ui.send(chat_id, "❌ 名称不能为空，请重新输入：")
            return True
        ok, result = _do_edit(chat_id, short, "name", new_name)
        if not ok:
            ui.send(chat_id, f"❌ {ui.escape_html(result)}")
            return True
        states.pop_state(chat_id)
        new_short = ui.register_code(new_name)   # 旧短码失效，新短码生成
        _ok_result(f"✅ 名称已改为 <code>{ui.escape_html(new_name)}</code>", new_short)
        return True
    if action == "ch_edit_url":
        url = (text or "").strip().rstrip("/")
        if not (url.startswith("http://") or url.startswith("https://")):
            ui.send(chat_id, "❌ URL 需以 http:// 或 https:// 开头，请重新输入：")
            return True
        # 先判断是否需要进入冲突解决：先 split，看识别出的协议与当前 channel 协议
        try:
            split_base, split_path = split_base_url(url)
        except ValueError as exc:
            ui.send(chat_id, f"❌ URL 无效：{ui.escape_html(str(exc))}")
            return True
        ch = registry.get_channel(f"api:{ui.resolve_code(short) or ''}")
        current_proto = _protocol_of(ch) if ch else "anthropic"
        detected = detect_suffix_protocol(split_path) if split_path else None
        if split_path and detected and detected != current_proto:
            # 冲突：记下候选 url + 两个分支信息，让用户按钮选
            states.set_state(chat_id, "ch_edit_url_confirm", {
                "short": short,
                "url": url,
                "new_base": split_base,
                "new_path": split_path,
                "detected": detected,
                "current_proto": current_proto,
            })
            current_label = _PROTOCOL_LABEL.get(current_proto, current_proto)
            detected_label = _PROTOCOL_LABEL.get(detected, detected)
            ui.send(
                chat_id,
                "⚠ <b>协议与新 URL 路径不匹配</b>\n\n"
                f"新 URL 路径：<code>{ui.escape_html(split_path)}</code>\n"
                f"路径对应协议：<b>{ui.escape_html(detected_label)}</b>\n"
                f"当前渠道协议：<code>{ui.escape_html(current_label)}</code>\n\n"
                "如何处理？",
                reply_markup=ui.inline_kb([
                    [ui.btn(f"✅ 更新 URL 并切换协议为 {detected_label}",
                            f"ch:eurl_switch:{short}")],
                    [ui.btn(f"⚠ 保持 {current_label}，只保留 baseUrl（清空路径）",
                            f"ch:eurl_basesonly:{short}")],
                    [ui.btn("❌ 取消", f"ch:edit:{short}")],
                ]),
            )
            return True
        # 无冲突：直接交给 registry（它会自动联动 apiPath）
        ok, result = _do_edit(chat_id, short, "baseUrl", url)
        if not ok:
            ui.send(chat_id, f"❌ {ui.escape_html(result)}")
            return True
        states.pop_state(chat_id)
        _ok_result("✅ URL 已更新", short)
        return True
    if action == "ch_edit_key":
        key = (text or "").strip()
        if len(key) < 5:
            ui.send(chat_id, "❌ API Key 过短，请重新输入：")
            return True
        ok, result = _do_edit(chat_id, short, "apiKey", key)
        if not ok:
            ui.send(chat_id, f"❌ {ui.escape_html(result)}")
            return True
        states.pop_state(chat_id)
        _ok_result("✅ API Key 已更新", short)
        return True
    if action == "ch_edit_max":
        try:
            v = int((text or "").strip())
            if v < 0:
                raise ValueError
        except ValueError:
            ui.send(chat_id, "❌ 需要非负整数，请重新输入：")
            return True
        ok, result = _do_edit(chat_id, short, "maxConcurrent", v)
        if not ok:
            ui.send(chat_id, f"❌ {ui.escape_html(result)}")
            return True
        states.pop_state(chat_id)
        label = "默认" if v == 0 else str(v)
        _ok_result(f"✅ 并发上限已更新为 <code>{label}</code>", short)
        return True
    if action == "ch_edit_models":
        try:
            models = api_channel.parse_models_input(text or "")
        except ValueError as exc:
            ui.send(chat_id, f"❌ {ui.escape_html(str(exc))}\n请重新输入：")
            return True
        ok, result = _do_edit(chat_id, short, "models", models)
        if not ok:
            ui.send(chat_id, f"❌ {ui.escape_html(result)}")
            return True
        states.pop_state(chat_id)
        _ok_result(f"✅ 模型列表已更新（{len(models)} 个）", short)
        return True
    return False


# ─── 路由分发 ─────────────────────────────────────────────────────

def handle_callback(chat_id: int, message_id: int, cb_id: str, data: str) -> bool:
    if data == "menu:channel":
        show(chat_id, message_id, cb_id)
        return True
    if data == "ch:clear_errors_all":
        on_clear_errors_all(chat_id, message_id, cb_id); return True
    if data == "ch:clear_affinity_all":
        on_clear_affinity_all(chat_id, message_id, cb_id); return True

    # 向导
    if data == "chw:start":  wiz_start(chat_id, message_id, cb_id); return True
    if data == "chw:cancel": wiz_cancel(chat_id, message_id, cb_id); return True
    if data == "chw:back":   wiz_back_to_models(chat_id, message_id, cb_id); return True
    if data == "chw:test_all": wiz_test_all(chat_id, message_id, cb_id); return True
    if data == "chw:skip_test": wiz_skip_test(chat_id, message_id, cb_id); return True
    if data == "chw:save":   wiz_save(chat_id, message_id, cb_id); return True
    if data.startswith("chw:test:"):
        wiz_test_single(chat_id, message_id, cb_id, data.split(":", 2)[2]); return True
    if data.startswith("chw:proto_adopt:"):
        wiz_proto_adopt(chat_id, message_id, cb_id, data.split(":", 2)[2]); return True
    if data.startswith("chw:proto_force:"):
        wiz_proto_force(chat_id, message_id, cb_id, data.split(":", 2)[2]); return True
    if data == "chw:back_to_url":
        wiz_back_to_url(chat_id, message_id, cb_id); return True
    if data.startswith("chw:proto:"):
        wiz_on_protocol_select(chat_id, message_id, cb_id, data.split(":", 2)[2]); return True

    # 渠道详情相关
    if data.startswith("ch:view:"):
        on_view(chat_id, message_id, cb_id, data.split(":", 2)[2]); return True
    if data.startswith("ch:toggle:"):
        on_toggle(chat_id, message_id, cb_id, data.split(":", 2)[2]); return True
    if data.startswith("ch:clear_errors:"):
        on_clear_errors(chat_id, message_id, cb_id, data.split(":", 2)[2]); return True
    if data.startswith("ch:clear_affinity:"):
        on_clear_affinity(chat_id, message_id, cb_id, data.split(":", 2)[2]); return True
    if data.startswith("ch:del_exec:"):
        on_delete_exec(chat_id, message_id, cb_id, data.split(":", 2)[2]); return True
    if data.startswith("ch:del:"):
        on_delete_ask(chat_id, message_id, cb_id, data.split(":", 2)[2]); return True

    # 测试（已存在渠道）
    if data.startswith("ch:test:"):
        on_test_panel(chat_id, message_id, cb_id, data.split(":", 2)[2]); return True
    if data.startswith("ch:t1:"):
        parts = data.split(":")
        if len(parts) >= 4:
            on_test_single(chat_id, message_id, cb_id, parts[2], parts[3]); return True
    if data.startswith("ch:tall:"):
        on_test_all(chat_id, message_id, cb_id, data.split(":", 2)[2]); return True

    # 编辑
    if data.startswith("ch:edit:"):
        on_edit_menu(chat_id, message_id, cb_id, data.split(":", 2)[2]); return True
    if data.startswith("ch:ename:"):
        on_edit_name(chat_id, message_id, cb_id, data.split(":", 2)[2]); return True
    if data.startswith("ch:eurl_switch:"):
        on_edit_url_switch(chat_id, message_id, cb_id, data.split(":", 2)[2]); return True
    if data.startswith("ch:eurl_basesonly:"):
        on_edit_url_basesonly(chat_id, message_id, cb_id, data.split(":", 2)[2]); return True
    if data.startswith("ch:eurl:"):
        on_edit_url(chat_id, message_id, cb_id, data.split(":", 2)[2]); return True
    if data.startswith("ch:ekey:"):
        on_edit_key(chat_id, message_id, cb_id, data.split(":", 2)[2]); return True
    if data.startswith("ch:emodels:"):
        on_edit_models(chat_id, message_id, cb_id, data.split(":", 2)[2]); return True
    if data.startswith("ch:ecc:"):
        on_edit_cc_toggle(chat_id, message_id, cb_id, data.split(":", 2)[2]); return True
    if data.startswith("ch:emax:"):
        on_edit_max_concurrent(chat_id, message_id, cb_id, data.split(":", 2)[2]); return True
    if data.startswith("ch:eproto:"):
        on_edit_protocol(chat_id, message_id, cb_id, data.split(":", 2)[2]); return True
    if data.startswith("ch:seproto:"):
        parts = data.split(":")
        if len(parts) >= 4:
            on_set_protocol(chat_id, message_id, cb_id, parts[2], parts[3]); return True

    return False


def handle_text_state(chat_id: int, action: str, text: str) -> bool:
    if action == "ch_wiz_name":
        wiz_on_name_input(chat_id, text); return True
    if action == "ch_wiz_url":
        wiz_on_url_input(chat_id, text); return True
    if action == "ch_wiz_key":
        wiz_on_key_input(chat_id, text); return True
    if action == "ch_wiz_models":
        wiz_on_models_input(chat_id, text); return True
    if action.startswith("ch_edit_"):
        return handle_edit_text(chat_id, action, text)
    return False
