"""Zhipu TG adapter; account mutations/effects use the shared OAuth control."""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
import math
import secrets
import threading
import time

from ...management_control.oauth import OAuthProvider, JsonCredential, CreateOAuthAccountCommand, UpdateOAuthAccountCommand, CompleteOAuthLoginCommand
from ...management_control.oauth.contracts import revision
from ...oauth.zhipu.common import ZhipuError
from ...management_control.oauth.menu_bridge import control, telegram_context
from .. import error_reporting, states, ui

PREFIX = "oa:zh:"
_remote_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="zhipu-management")
_work_lock = threading.Lock()


def _submit_remote(run):
    return _remote_executor.submit(run)


def _current(chat_id, data):
    return (states.get_state(chat_id) or {}).get("data") is data


def _run_remote(chat_id, data, work, done, failed, *, abandoned=None):
    """Keep TG dispatch free; never let a late worker replace a newer menu."""
    with _work_lock:
        if not _current(chat_id, data) or data.get("busy"):
            return
        data["busy"] = True
    def run():
        try:
            if not _current(chat_id, data):
                return
            try:
                result = work()
            except Exception as exc:
                if _current(chat_id, data):
                    failed(exc)
                return
            if _current(chat_id, data):
                done(result)
            elif abandoned:
                abandoned(result)
        finally:
            data["busy"] = False
    return _submit_remote(run)


def model_route_available(account):
    from ...oauth.zhipu.runtime import model_route_available as available
    return available(account)


def parent():
    from . import oauth_menu
    return oauth_menu


def account_display(account):
    """Preserve custom names; never present the generated routing hash as a name."""
    subject = str(account.get("subject") or "")
    label = str(account.get("label") or "").strip()
    if label and label != subject:
        return label
    if account.get("email"):
        return str(account["email"])
    site = "智谱" if account.get("site") == "bigmodel" else "Z.ai"
    mode = "API Key" if account.get("credential_mode") == "api_key" else "OAuth"
    # A short non-secret account-id suffix disambiguates unnamed Key accounts.
    suffix = " · " + subject[-4:] if subject else ""
    return f"{site} {mode}{suffix}"


def provider_line(account, *, detail=False):
    site = "中国站" if account.get("site") == "bigmodel" else "国际站"
    mode = "API Key" if account.get("credential_mode") == "api_key" else "OAuth"
    scope = "团队" if account.get("plan_scope") == "team" else "个人"
    line = f"🏷️ 套餐: <code>{scope} Coding Plan</code> · {site}"
    if not detail:
        line += " · " + mode
        cards = _reset_card_lines(account.get("credential_mode"), account.get("zhipu_reset_status") or {})
        if cards:
            line += "\n" + "\n".join(cards)
    if mode == "OAuth":
        labels = {"available": "可用", "unknown": "未知", "unassigned": "未分配席位", "expired": "已过期", "unavailable": "无有效套餐"}
        if detail or account.get("entitlement") != "available":
            line += "\n📋 订阅: <code>" + labels.get(account.get("entitlement"), "未知") + "</code>"
        if detail and account.get("plan_scope") == "team":
            line += "\n🏢 组织: <code>" + ui.escape_html(str(account.get("organization_id") or "—")) + "</code>"
            line += " · 项目: <code>" + ui.escape_html(str(account.get("project_id") or "—")) + "</code>"
        if account.get("management_status") == "relogin_required":
            line += "\n⚠️ 管理登录已过期，请重新登录。"
        if not account.get("model_key"):
            line += "\n⚠️ 初始化未完成，可继续自动配置模型 Key。"
    if account.get("management_status") == "model_key_rejected":
        line += "\n⚠️ 模型 Key 认证失败，请更新凭据。"
    if detail:
        from ...model_state import state_snapshot
        selection = control.account_model_selection_snapshot(account)
        models = set(selection.get("models") or [])
        account_disabled = models & set(selection.get("disabled_models") or [])
        global_disabled = models & set(state_snapshot(control.config_snapshot())["disabledModels"])
        disabled = account_disabled | global_disabled
        # These are model switches, not a promise of quota/entitlement or live
        # availability. Account status is displayed separately by the parent.
        line += (f"\n🧬 模型目录: {len(models)} 个 · 已启用 {len(models - disabled)} · 禁用 {len(disabled)}"
                 f"（全局 {len(global_disabled)} · 账号 {len(account_disabled)}）")
    return line


def credential_lines(account):
    mode = "API Key" if account.get("credential_mode") == "api_key" else "OAuth"
    state = "已配置" if account.get("model_key") else "未配置"
    line = f"🔑 凭据: <code>{mode}</code> · 模型 Key {state}\n"
    if mode == "OAuth":
        if parent()._parse_iso(account.get("expired")) is not None:
            line += f"⏳ 登录到期: <code>{parent()._fmt_time_full(account['expired'])}</code>\n"
        if parent()._parse_iso(account.get("last_refresh")) is not None:
            line += f"🔄 更新: <code>{parent()._format_bjt(account['last_refresh'])}</code>\n"
    return line


def _number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def _count(value):
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def _card_time(value):
    try:
        return parent()._fmt_time_full(datetime.fromtimestamp(value / 1000, timezone.utc).isoformat())
    except (TypeError, ValueError, OverflowError):
        return "未知"


def reset_grant_notification(account_key, account, before, after):
    """Describe observed new cards, never mistake the full inventory for a grant."""
    lines = ["🎁 <b>智谱官方重置卡已领取</b>",
             "账户: <code>" + ui.escape_html(account_display(account)) + "</code>"]
    added_lines, totals = [], []
    now = time.time() * 1000
    if isinstance(after, dict):
        for name, title in (("five_hour", "5小时额度重置卡"), ("week", "周额度重置卡")):
            field = "available_" + name + "_resets"
            def expiries(snapshot):
                return Counter(card["expire_at"] for card in (snapshot or {}).get(field, [])
                               if _number(card.get("expire_at")) is not None and card["expire_at"] > now)
            current = expiries(after)
            totals.append(("5小时" if name == "five_hour" else "周") + f"卡 {sum(current.values())} 张")
            added = current - expiries(before) if isinstance(before, dict) else Counter()
            if added:
                added_lines.append(f"• {title}：<b>{sum(added.values())} 张</b>")
                for expiry, count in sorted(added.items()):
                    added_lines.append(f"  到期（北京时间）：{_card_time(expiry)}" + (f"（{count} 张）" if count > 1 else ""))
    if added_lines:
        lines.extend(["", "<b>本次查询确认新增</b>", *added_lines])
    else:
        lines.append("\n领取成功；本次新增卡种、张数尚未核实，请点「查看重置卡」。")
    if totals:
        lines.append("\n当前可用：" + " · ".join(totals))
    lines.append("\n已保留，未自动使用；使用时仍需你确认。")
    short = ui.register_code(account_key)
    keyboard = ui.inline_kb([
        [ui.btn("♻️ 查看重置卡", PREFIX + "reset:" + short),
         ui.btn("👤 查看账户", "oa:view:" + short + ":1:all")],
        [ui.btn("✖️ 关闭", PREFIX + "notice_close")],
    ])
    return "\n".join(lines), keyboard


def _action_state(status):
    return {"succeeded": "已完成", "not_submitted": "未提交", "unknown": "结果待核对",
            "key_pending": "已有 Key 待读取/保存", "pending": "处理中", "rejected": "上游拒绝",
            "not_granted": "暂未获得", "created_but_account_changed": "Key 已取得，但账号已变更"}.get(status, "待核对")


def _action_error(result):
    if not result.get("error_kind"):
        return ""
    from ...oauth.zhipu.diagnostics import FIELDS
    error = ZhipuError(result.get("error_stage") or "action", result["error_kind"],
        status=result.get("http_status") or 0, code=result.get("code"), timeout_phase=result.get("timeout_phase") or "",
        **{key: result[key] for key in FIELDS if key in result})
    return "\n" + parent()._oauth_error_html(error, provider="zhipu", operation="execute_action")


def _reset_card_lines(mode, cached, *, detail=False):
    if mode != "oauth":
        return []
    lines = []
    cards = cached.get("data")
    if isinstance(cards, dict):
        now = time.time() * 1000
        counts = [sum(card.get("expire_at", 0) > now for card in cards.get("available_" + name + "_resets", [])) for name in ("five_hour", "week")]
        lines.append(f"♻️ 重置卡: 5小时 {counts[0]} 张 · 周窗 {counts[1]} 张" + ("（上次查询结果）" if cached.get("error") else ""))
    if cached.get("error") and detail:
        lines.append("⚠️ 重置卡查询未完成，已有结果保留，可重新查询。")
    return lines


def extra_usage_lines(key, *, detail=False):
    """Only provider-specific extras; 5h/7d use the parent shared renderer.

    Coding Plan remaining is documented; usage/currentValue are not an
    authoritative total/used pair. Keep them in the raw snapshot, not the UI.
    """
    snap = control.zhipu_snapshot(key)
    lines = []
    for item in snap.get("limits") or []:
        if item.get("type") != "TIME_LIMIT" or item.get("unit") != 5 or item.get("number") != 1:
            continue
        remaining = _number(item.get("remaining"))
        if remaining is not None:
            lines.append(f"🛠 工具/月: 剩余 <b>{_count(remaining)}</b> 次")
    if detail:
        lines.extend(_reset_card_lines(snap.get("credential_mode"), snap.get("reset") or {}, detail=True))
        mcp = snap.get("mcp") or {}
        used, limit, remaining = (_number(mcp.get(field)) for field in ("used", "limit", "remaining"))
        if limit and used is not None:
            value = parent()._format_usage_value_bar_first_html(used / limit * 100)
            selected = remaining if parent()._usage_display_mode() == "remaining" else used
            counts = f"（{_count(selected)} / {_count(limit)}）" if selected is not None else ""
            lines.append(f"🛠 MCP/日: {value}{counts}")
        elif remaining is not None:
            lines.append(f"🛠 MCP/日: 剩余 <b>{_count(remaining)}</b>")
    stamp = _number(snap.get("fetched_at"))
    if stamp and time.time() * 1000 - stamp > 900000:
        lines.append("⚠️ 额度为旧快照，请刷新额度。")
    return lines


def _state(chat_id, action, **data):
    value = dict(data, nonce=secrets.token_hex(5))
    states.set_state(chat_id, action, value)
    return value


def discard_state(chat_id):
    state = states.get_state(chat_id)
    if not state or not state["action"].startswith("oa_zh_"):
        return
    data = state["data"]
    if data.get("action_cancellation") is not None:
        data["action_cancellation"].cancel()
    if data.get("flow_id"):
        try:
            control.cancel_login_flow(telegram_context(chat_id), data["flow_id"], data["flow_secret"])
        except Exception:
            pass
    states.pop_state_if_current(chat_id, data)


def before_command(chat_id, text):
    # Navigation commands are not credentials, names, or OAuth callbacks.
    if (text or "").startswith("/"):
        discard_state(chat_id)


def before_callback(chat_id, callback):
    # Leaving Zhipu also cancels its background login, so a late authorization
    # result cannot replace a newly opened menu. Internal confirmation/project
    # buttons retain their nonce-bound state until their handler consumes it.
    if not callback.startswith(PREFIX):
        discard_state(chat_id)


def error_text():
    return "⚠️ 操作未完成或确认已失效。若请求已经发出，结果可能未知；请先查询核对，不要重复消费。"


def _back(key=None):
    return ui.btn("◀ 返回账户" if key else "◀ 返回新增", "oa:view:" + ui.register_code(key) + ":1:all" if key else "oa:add")


def _cancel_flow(chat_id, flow):
    try:
        control.cancel_login_flow(telegram_context(chat_id), flow.flow_id, flow.flow_secret)
    except Exception:
        pass


def _login(chat_id, message_id, site):
    discard_state(chat_id)
    data = _state(chat_id, "oa_zh_login")
    ui.edit(chat_id, message_id, "🔄 正在准备授权链接…", reply_markup=ui.inline_kb([[_back()]]))
    def ready(flow):
        data.update(flow_id=flow.flow_id, flow_secret=flow.flow_secret, auth_url=flow.auth_url,
                    message_id=message_id, flow=flow)
        ui.edit(chat_id, message_id,
            "🌐 <b>智谱 / Z.ai OAuth 登录</b>\n\n请打开官方授权页面并完成授权。\n"
            "⏳ 正在等待网页授权，成功后自动保存账户，<b>无需粘贴回调地址</b>。\n\n"
            "链接 5 分钟内有效。授权后自动配置个人账户和专用模型 Key（不存在则创建），并获取额度与模型。",
            reply_markup=ui.inline_kb([[{"text": "打开授权页面", "url": flow.auth_url}],
                [ui.btn("取消登录", PREFIX + "cancel:" + data["nonce"])]]))
        _start_login_wait(chat_id, message_id, data)
    _run_remote(chat_id, data,
        lambda: control.start_login_flow(telegram_context(chat_id), OAuthProvider.ZHIPU, site=site), ready,
        lambda exc: _result(chat_id, message_id, None, error_reporting.report(exc, operation="智谱OAuth登录初始化")),
        abandoned=lambda flow: _cancel_flow(chat_id, flow))


def _start_login_wait(chat_id, message_id, data):
    if data.get("polling"):
        return
    data["polling"] = True
    def run():
        try:
            _wait_for_login(chat_id, message_id, data, data["flow"])
        finally:
            data["polling"] = False
    threading.Thread(target=run, daemon=True, name="zhipu-oauth-login").start()


def _wait_for_login(chat_id, message_id, data, flow):
    def current():
        return (states.get_state(chat_id) or {}).get("data") is data

    def authorized():
        if current():
            ui.edit(chat_id, message_id, "✅ 网页授权已完成，正在保存登录并获取项目、Key、额度及账户信息…",
                    reply_markup=ui.inline_kb([[_back()]]))

    read_retries = 0
    while current() and time.time() < flow.expires_at.timestamp():
        try:
            poll = control.poll_login_flow(telegram_context(chat_id), flow.flow_id, flow.flow_secret,
                                           on_authorized=authorized)
            if not current():
                return
            if poll.status == "completed" and poll.account_id:
                _login_saved(chat_id, message_id, data, poll)
                return
            if poll.status == "select_project":
                _show_projects(chat_id, data, poll, message_id=message_id)
                return
            read_retries = 0
            if poll.status != "pending":
                discard_state(chat_id)
                ui.edit(chat_id, message_id, "❌ 官方授权未完成或已过期，请重新发起登录。",
                        reply_markup=ui.inline_kb([[_back()]]))
                return
        except Exception as exc:
            if not current():
                return
            if getattr(exc, "retryable", False):
                read_retries += 1
                if read_retries <= 3:
                    ui.edit(chat_id, message_id, f"⚠️ 授权结果读取暂时失败，保留本次登录并重试（{read_retries}/3）…",
                        reply_markup=ui.inline_kb([[ui.btn("取消登录", PREFIX + "cancel:" + data["nonce"])]]))
                    time.sleep(min(2 ** read_retries, 8))
                    continue
                ui.edit(chat_id, message_id, error_reporting.report(exc, operation="智谱OAuth授权结果读取") +
                        "\n本次流程已保留，有效期内可继续读取，无需重新授权。",
                        reply_markup=ui.inline_kb([[ui.btn("🔄 继续读取", PREFIX + "retry_login:" + data["nonce"])],
                                                  [ui.btn("取消登录", PREFIX + "cancel:" + data["nonce"])]]))
                return
            discard_state(chat_id)
            ui.edit(chat_id, message_id, error_reporting.report(exc, operation="智谱OAuth授权结果查询"),
                    reply_markup=ui.inline_kb([[_back()]]))
            return
        # The control layer honors the interval supplied by the official server.
        time.sleep(1)
    if current():
        discard_state(chat_id)
        ui.edit(chat_id, message_id, "⌛ 登录链接已过期，请重新发起登录。",
                reply_markup=ui.inline_kb([[_back()]]))


def _login_saved(chat_id, message_id, data, poll):
    result = control.complete_login_flow(telegram_context(chat_id), data["flow_id"], data["flow_secret"],
                                        CompleteOAuthLoginCommand(completed=True))
    if not _current(chat_id, data):
        return
    states.pop_state_if_current(chat_id, data)
    _initialize_account(chat_id, message_id, result.account_id, result=result)


def refresh_usage(chat_id, message_id, key, page=1, filter_key="all"):
    """Resume incomplete setup; run quota IO outside Telegram dispatch."""
    account = control.account_snapshot(key) or {}
    if account.get("credential_mode") == "oauth" and not account.get("model_key"):
        _initialize_account(chat_id, message_id, key)
        return
    discard_state(chat_id)
    data = _state(chat_id, "oa_zh_work", key=key)
    ui.edit(chat_id, message_id, "🔄 正在读取已有 Key 和额度…",
            reply_markup=ui.inline_kb([[_back(key)]]))

    def render(text):
        parent()._edit_cached_detail(chat_id, message_id, key, page, filter_key,
                                    prefix=text + "\n\n", refresh_quota=False)
        states.pop_state_if_current(chat_id, data)

    def failed(exc):
        # ZhipuError retains only bounded, credential-free facts. Do not print
        # arbitrary transport errors, URLs, account IDs or response bodies.
        if isinstance(exc, ZhipuError):
            print(f"[zhipu] fetch_usage failed: stage={exc.stage} kind={exc.kind} phase={exc.timeout_phase or '-'} http={exc.status_code} code={exc.code}")
        else:
            print(f"[zhipu] fetch_usage failed: type={type(exc).__name__}")
        render(parent()._oauth_error_html(exc, provider="zhipu", operation="fetch_usage"))

    def done(result):
        if result.get("error") is not None:
            failed(result["error"])
            return
        usage = result.get("usage") or {}
        block = usage.get("zhipu") or {}
        text = "✅ 额度已更新" if block.get("windows") or block.get("limits") else "⚠️ 上游未返回可识别的额度数据"
        if block.get("errors"):
            text += "；订阅或 MCP 信息未完整取得，可稍后刷新。"
        action = (result.get("quota_action") or {}).get("action")
        if action in {"disabled", "still_over_quota"}:
            text += "\n🔒 额度达到阈值，保持配额暂停。"
        elif action == "resumed":
            text += "\n♻️ 额度已恢复，已解除配额暂停。"
        render(text)

    _run_remote(chat_id, data, lambda: control.refresh_usage_now(telegram_context(chat_id), key), done, failed)


def _initialize_account(chat_id, message_id, key, *, result=None):
    """One progress/result card; personal defaults are automatic, not a wizard."""
    discard_state(chat_id)
    data = _state(chat_id, "oa_zh_initializing", key=key)
    progress = "🔄 登录已保存，正在自动配置账户…"
    if message_id is None:
        message_id = parent()._message_id_from_response(ui.send(chat_id, progress))
    else:
        ui.edit(chat_id, message_id, progress, reply_markup=ui.inline_kb([[_back(key)]]))

    def work():
        outcome = result
        account = control.account_snapshot(key) or {}
        # Shared control already owns the entire post-save continuation. Only
        # an explicit retry starts it again; rendering never repeats exhausted IO.
        if outcome is None:
            outcome = control.initialize_zhipu_account(telegram_context(chat_id), key)
        future = (outcome.post_save or {}).get("model_sync_future")
        model_error = None
        if future is not None:
            try:
                model_result = future.result()  # Discovery owns its bounded network deadline.
                if (model_result.get("metadata_sync") or {}).get("status") == "failed":
                    model_error = RuntimeError("metadata_sync_failed")
            except Exception as exc:
                model_error = exc
        return outcome, model_error

    def render(outcome):
        saved, model_error = outcome
        effects = saved.post_save or {}
        current = control.account_snapshot(saved.account_id) or {}
        selection = control.account_model_selection_snapshot(current)
        count = len(selection.get("effective_models") or [])
        configured = bool(count and current.get("model_key") and model_route_available(current))
        text = ("✅ 账户已就绪" if current.get("enabled", True) else "⏸ 账户配置已完成，当前已暂停") if configured else "✅ 登录已保存"
        key_action = effects.get("key_action")
        if key_action and key_action.get("status") != "succeeded":
            text += "\n⚠️ " + _action_state(key_action["status"]) + "。" + _action_error(key_action)
            text += ("\n继续初始化只核对或读取已有 Key，不会重复创建。" if key_action["status"] in {"unknown", "key_pending", "pending"}
                     else "\n已保存的登录保留，可继续初始化。")
        elif effects.get("enrichment_error"):
            text += "\n" + parent()._oauth_error_html(effects["enrichment_error"], provider="zhipu", operation="initialize")
        elif not current.get("model_key"):
            text += "\n模型 Key 暂未取得，可重试初始化。"
        elif model_error or effects.get("model_sync_error"):
            text += "\n⚠️ 模型目录同步未完成。"
        if effects.get("usage_error"):
            text += "\n" + parent()._oauth_error_html(effects["usage_error"], provider="zhipu", operation="fetch_usage")
        if effects.get("reset_error"):
            text += "\n" + parent()._oauth_error_html(effects["reset_error"], provider="zhipu", operation="reset_status")
        parent()._edit_cached_detail(chat_id, message_id, saved.account_id, 1, "all",
                                    prefix=text + "\n\n", refresh_quota=False)
        states.pop_state_if_current(chat_id, data)

    def failed(exc):
        parent()._edit_cached_detail(chat_id, message_id, key, 1, "all",
            prefix="✅ 登录已保存\n" + parent()._oauth_error_html(exc, provider="zhipu", operation="initialize") + "\n\n",
            refresh_quota=False)
        states.pop_state_if_current(chat_id, data)

    _run_remote(chat_id, data, work, render, failed)


def _projects(chat_id, message_id, key):
    discard_state(chat_id)
    data = _state(chat_id, "oa_zh_projects", key=key)
    ui.edit(chat_id, message_id, "🔄 正在获取可切换的项目…", reply_markup=ui.inline_kb([[_back(key)]]))
    def ready(flow):
        data.update(flow_id=flow.flow_id, flow_secret=flow.flow_secret)
        poll = control.poll_login_flow(telegram_context(chat_id), flow.flow_id, flow.flow_secret)
        if _current(chat_id, data):
            _show_projects(chat_id, data, poll, message_id=message_id)
    def failed(exc):
        ui.edit(chat_id, message_id, "✅ 账户已保存，项目查询失败不影响已保存的登录。\n" +
            error_reporting.report(exc, operation="智谱组织/项目查询"),
            reply_markup=ui.inline_kb([[ui.btn("🔄 重新查询项目", PREFIX + "projects:" + ui.register_code(key))], [_back(key)]]))
    _run_remote(chat_id, data, lambda: control.start_zhipu_project_selection(telegram_context(chat_id), key),
                ready, failed, abandoned=lambda flow: _cancel_flow(chat_id, flow))


def _show_projects(chat_id, data, poll, *, message_id=None):
    preview = poll.account_preview or {}
    data["choices"] = preview.get("choices") or []
    rows = [[ui.btn(str(choice.get("organization_name") or choice["organization_id"])[:30] + " / " +
                    str(choice.get("project_name") or choice["project_id"])[:30] +
                    (" · 团队" if choice["plan_scope"] == "team" else " · 个人"),
                    PREFIX + f"project:{data['nonce']}:{index}")] for index, choice in enumerate(data["choices"])]
    rows.append([_back(data.get("key"))] if data.get("key") else [ui.btn("取消", PREFIX + "cancel:" + data["nonce"])])
    label = ui.escape_html(str(preview.get("label") or "已授权用户"))
    text = f"✅ 已授权：<b>{label}</b>\n\n" + (
        "请选择目标组织/项目（席位及订阅状态将单独核验）：" if data["choices"] else
        "未查到可选择项目；账户已保存，可稍后重查或导入已有 Key，不需要重新登录。")
    if message_id is None:
        ui.send(chat_id, text, reply_markup=ui.inline_kb(rows))
    else:
        ui.edit(chat_id, message_id, text, reply_markup=ui.inline_kb(rows))


def _result(chat_id, message_id, key, text):
    ui.edit(chat_id, message_id, text, reply_markup=ui.inline_kb([[_back(key)]]))


def _show_initial_sync(chat_id, result):
    """Observe the save's existing work; never start a second initialization."""
    effects = result.post_save or {}
    if effects.get("model_sync_future") is None:
        ui.send(chat_id, "⚠️ 账户已保存，但模型同步未能启动；请在详情查看额度并重试模型同步。",
                reply_markup=ui.inline_kb([[_back(result.account_id)]]))
        return
    account = control.account_snapshot(result.account_id) or {}
    parent()._foreground_account_model_sync(chat_id, result.account_id,
        provider="zhipu", label=account_display(account), post_save=dict(effects))


def _reset_page(chat_id, message_id, key):
    account = control.account_snapshot(key)
    if account and account.get("credential_mode") == "api_key":
        # A button in an older TG message may survive after the entry is hidden.
        _result(chat_id, message_id, key, "此 API Key 账户不支持官方重置卡。")
        return
    discard_state(chat_id)
    data = _state(chat_id, "oa_zh_work", key=key)
    ui.edit(chat_id, message_id, "🔄 正在查询重置卡…", reply_markup=ui.inline_kb([[_back(key)]]))
    def ready(value):
        reset = value["reset"]
        short = ui.register_code(key)
        rows = [[ui.btn("立即尝试领取", PREFIX + "plan:" + short + ":opportunity")]]
        lines = ["♻️ 官方重置卡（查询不会标记历史已读）", "后台自动尝试领取；使用卡片仍需你确认。"]
        for name, title, kind in (("five_hour", "5小时", "FIVE_HOUR"), ("week", "周窗", "WEEK")):
            cards = reset["available_" + name + "_resets"]
            lines.append(f"{title}: {len(cards)} 张可用")
            for index, card in enumerate(cards):
                rows.append([ui.btn(f"使用{title}卡 {index+1} · 到期 {_card_time(card['expire_at'])}", PREFIX + f"plan:{short}:use:{kind}:{index}")])
        claim = next((row for row in reversed(value["actions"]) if row.get("action") == "opportunity"), None)
        if claim and claim.get("next_try_at", 0) > time.time() * 1000:
            lines.append("下次最早尝试：" + _card_time(claim["next_try_at"]) + "（不代表届时一定发卡）")
        for row in value["actions"][-5:]:
            title = {"create_key": "模型 Key", "opportunity": "领取重置卡", "use": "使用重置卡"}.get(row["action"], "操作")
            lines.append(ui.escape_html(title + "：" + _action_state(row["status"])))
        rows += [[ui.btn("重新查询", PREFIX + "reset:" + short)], [_back(key)]]
        ui.edit(chat_id, message_id, "\n".join(lines), reply_markup=ui.inline_kb(rows))
    _run_remote(chat_id, data, lambda: control.get_zhipu(telegram_context(chat_id), key, reset_status=True),
                ready, lambda exc: _result(chat_id, message_id, key, error_reporting.report(exc, operation="智谱重置卡查询")))


def _plan_action(chat_id, message_id, key, action, *, reset_type=None, card_index=0):
    """Continue on the same card rather than sending users back to find a button."""
    account = control.account_snapshot(key) or {}
    discard_state(chat_id)
    data = _state(chat_id, "oa_zh_confirm", key=key, remote_action=action)
    ui.edit(chat_id, message_id, "🔄 正在准备确认…", reply_markup=ui.inline_kb([[_back(key)]]))
    def ready(plan):
        data["plan_token"] = plan["plan_token"]
        label = ui.escape_html(account_display(account))
        if action == "create_key":
            text = (f"🔑 <b>{label} · 创建模型 Key</b>\n\n"
                    "当前项目没有已有的 zcode-api-key。\n确认后将在该项目创建 Key，并自动继续获取额度和模型。")
            confirm_label = "创建并继续"
            if plan.get("resume_only"):
                text = (f"🔑 <b>{label} · 继续配置模型 Key</b>\n\n"
                        "此前操作需要核对或继续读取。只查询并保存已有 Key，不会再次提交创建请求。")
                confirm_label = "查询并继续"
        else:
            title = "领取重置卡" if action == "opportunity" else "使用重置卡"
            text = f"♻️ <b>{label} · {title}</b>\n\n提交后无法撤销。"
            confirm_label = "确认" + title
        text += "\n组织/项目: <code>" + ui.escape_html(str(plan.get("organization_id") or "个人"))
        text += " / " + ui.escape_html(str(plan.get("project_id") or "个人")) + "</code>"
        if plan.get("expire_at"):
            text += f"\n重置卡: {'5小时' if plan['reset_type'] == 'FIVE_HOUR' else '周窗'} #{plan['card_index']+1}，到期 {_card_time(plan['expire_at'])}"
        ui.edit(chat_id, message_id, text, reply_markup=ui.inline_kb([
            [ui.btn(confirm_label, PREFIX + "confirm:" + data["nonce"]),
             ui.btn("取消", PREFIX + "cancel_action:" + data["nonce"])]]))
    _run_remote(chat_id, data, lambda: control.plan_zhipu_action(telegram_context(chat_id), key, action,
        reset_type=reset_type, card_index=card_index), ready,
        lambda exc: _result(chat_id, message_id, key, parent()._oauth_error_html(exc, provider="zhipu", operation="prepare_action")))


def handle_callback(chat_id, message_id, cb_id, callback):
    if not callback.startswith(PREFIX):
        return False
    ui.answer_cb(cb_id)
    parts = callback[len(PREFIX):].split(":")
    kind = parts[0]
    if kind == "notice_close":
        ui.delete_message(chat_id, message_id)
        return True
    if kind == "add":
        discard_state(chat_id)
        rows = []
        for site, label in (("bigmodel", "BigModel 中国站"), ("zai", "Z.ai 国际站")):
            rows.append([ui.btn(label + " OAuth 登录", PREFIX + "login:" + site)])
            rows.append([ui.btn(label + " API Key", PREFIX + "key:" + site)])
        rows.extend([[ui.btn("导入账户 JSON（两种模式）", PREFIX + "import")], [_back()]])
        ui.edit(chat_id, message_id, "🦜 智谱 / Z.ai Coding Plan\n个人和已有团队；不支持 Start Plan/off-peak。", reply_markup=ui.inline_kb(rows))
        return True
    if kind == "login" and len(parts) == 2 and parts[1] in {"bigmodel", "zai"}:
        _login(chat_id, message_id, parts[1])
        return True
    if kind in {"key", "import"}:
        discard_state(chat_id)
        _state(chat_id, "oa_zh_input", site=parts[1] if len(parts) == 2 else None, key_only=kind == "key")
        ui.edit(chat_id, message_id, "请发送 Coding Plan API Key；若为团队请使用 JSON 导入并指定组织/项目。" if kind == "key" else
                '发送账户 JSON。必填 site=bigmodel|zai、credential_mode=api_key|oauth；Key模式填 model_key；OAuth填 subject/access_token/zcode_token。团队另填 plan_scope=team、organization_id/project_id。', reply_markup=ui.inline_kb([[_back()]]))
        return True
    if kind == "name_skip":
        state = states.get_state(chat_id)
        data = state["data"] if state and state["action"] == "oa_zh_name" else {}
        if len(parts) == 2 and data.get("nonce") == parts[1]:
            states.pop_state_if_current(chat_id, data)
            _result(chat_id, message_id, data["key"], "✅ 保留现有名称，可随时在账户详情修改。")
        else:
            _result(chat_id, message_id, None, "按钮已过期。")
        return True
    if kind in {"cancel", "cancel_action", "project", "confirm", "replace", "retry_login"}:
        state = states.get_state(chat_id)
        data = state["data"] if state and state["action"].startswith("oa_zh_") else {}
        if len(parts) < 2 or data.get("nonce") != parts[1]:
            _result(chat_id, message_id, None, "按钮已过期。")
            return True
        if kind == "retry_login":
            if data.get("polling"):
                return True
            if state["action"] != "oa_zh_login" or not data.get("flow"):
                _result(chat_id, message_id, None, "登录已失效，请重新发起登录。")
                return True
            ui.edit(chat_id, message_id, "🔄 正在继续读取本次授权结果…")
            _start_login_wait(chat_id, message_id, data)
            return True
        if kind == "cancel":
            discard_state(chat_id)
            parent().on_add_menu(chat_id, message_id, "")
            return True
        if kind == "cancel_action":
            cancellation = data.get("action_cancellation")
            cancelled = cancellation is None or cancellation.cancel()
            states.pop_state_if_current(chat_id, data)
            _result(chat_id, message_id, data.get("key"),
                "已取消本次操作，未提交新的创建或消费请求。" if cancelled else
                "已停止等待。操作已进入提交阶段，无法撤销；结果会保留，请勿重复提交。")
            return True
        if data.get("busy"):
            return True
        def failed(exc):
            _result(chat_id, message_id, data.get("key"), error_reporting.report(exc, operation="智谱操作") + "\n已保存的账户不会因本次失败丢失。")
        def saved(result):
            states.pop_state_if_current(chat_id, data)
            _initialize_account(chat_id, message_id, result.account_id, result=result)
        if kind == "project" and len(parts) == 3:
            try:
                choice = data["choices"][int(parts[2])]
            except (KeyError, ValueError, IndexError):
                failed(ValueError("invalid_project"))
                return True
            ui.edit(chat_id, message_id, "🔄 正在绑定所选项目，随后独立查询订阅和已有 Key…")
            _run_remote(chat_id, data, lambda: control.select_zhipu_project(telegram_context(chat_id), data["flow_id"], data["flow_secret"],
                organization_id=choice["organization_id"], project_id=choice["project_id"]), saved, failed)
        elif kind == "confirm":
            from ...oauth.zhipu.actions import ActionCancellation
            cancellation = ActionCancellation()
            data["action_cancellation"] = cancellation
            def progress(stage):
                if not _current(chat_id, data):
                    return
                text = {"key_list": "🔄 正在检查已有 Key，尚未提交创建请求…",
                        "checking": "🔄 正在核对操作条件，尚未提交…",
                        "submitted": "🔄 已进入提交阶段，正在等待上游结果…",
                        "key_copy": "🔄 正在读取并保存模型 Key…"}.get(stage, "🔄 正在处理…")
                text += "\n提交前可以取消；提交后只能停止等待，不能撤销。"
                ui.edit(chat_id, message_id, text, reply_markup=ui.inline_kb([
                    [ui.btn("停止等待" if cancellation.submitted else "取消操作", PREFIX + "cancel_action:" + data["nonce"])],
                    [_back(data["key"])]]))
            def done(result):
                states.pop_state_if_current(chat_id, data)
                if result["status"] == "succeeded":
                    if result.get("action") == "create_key":
                        summary = result.get("initialization") or {}
                        text = "✅ 模型 Key 已保存。"
                        current = control.account_snapshot(summary.get("account_id", data["key"])) or {}
                        complete = (summary.get("models", 0) > 0 and summary.get("usage_ready")
                                    and summary.get("reset_ready") and not summary.get("errors"))
                        if complete:
                            text += "\n✅ 账户已就绪。" if current.get("enabled", True) and model_route_available(current) else "\n⏸ 账户配置已完成，当前不可调度。"
                        else:
                            text += "\n⚠️ 部分初始化未完成，账号与 Key 已保留，可继续初始化。"
                        parent()._edit_cached_detail(chat_id, message_id, summary.get("account_id", data["key"]), 1, "all",
                                                    prefix=text + "\n\n", refresh_quota=False)
                        return
                    text = "✅ 已确认完成。"
                    if result.get("action") == "use":
                        quota_status = result.get("quota_status")
                        text = "✅ 重置卡已使用。" + (
                            "额度已刷新，已恢复账号。" if quota_status == "resumed" else
                            "额度已刷新，账号保持启用。" if quota_status == "kept_enabled" else
                            "⚠️ 额度/调度恢复尚未确认（" + str(quota_status or "unknown") + "）；请查询额度，不要再次消费。")
                elif result["status"] == "not_submitted":
                    if result.get("error_kind") == "cancelled":
                        text = "已取消，未提交创建或消费请求。"
                    else:
                        text = "⚠️ 创建/消费请求未提交。" + _action_error(result)
                    rows = [[_back(data["key"])]]
                    if result.get("action") == "create_key" and result.get("error_kind") != "cancelled":
                        rows.insert(0, [ui.btn("重新确认创建", PREFIX + "plan:" + ui.register_code(data["key"]) + ":create_key")])
                    ui.edit(chat_id, message_id, text, reply_markup=ui.inline_kb(rows))
                    return
                else:
                    text = "⚠️ " + _action_state(result["status"]) + "。" + _action_error(result)
                    if result.get("action") == "create_key" and result["status"] in {"unknown", "key_pending"}:
                        text += "\n只读取已有 Key 并继续初始化，不会重复创建。"
                        ui.edit(chat_id, message_id, text, reply_markup=ui.inline_kb([
                            [ui.btn("继续读取 Key" if result["status"] == "key_pending" else "查询创建结果",
                                    PREFIX + "plan:" + ui.register_code(data["key"]) + ":create_key")], [_back(data["key"])]]))
                        return
                    if result["status"] == "unknown":
                        text += "\n请查询核对，不要再次消费。"
                _result(chat_id, message_id, data["key"], text)
            progress("checking")
            _run_remote(chat_id, data, lambda: control.execute_zhipu_action_now(telegram_context(chat_id), data["key"],
                data["plan_token"], cancellation=cancellation, on_stage=progress), done, failed)
        elif kind == "replace":
            ui.edit(chat_id, message_id, "🔄 正在更新账户并同步额度、模型及元数据…")
            _run_remote(chat_id, data, lambda: control.create_account(telegram_context(chat_id),
                CreateOAuthAccountCommand(JsonCredential(OAuthProvider.ZHIPU, data["payload"]), data["plan_token"])), saved, failed)
        return True
    if len(parts) < 2:
        return True
    key = parent()._account_key_from_short(parts[1])
    account = control.account_snapshot(key) if key else None
    if not account or control.provider_of_snapshot(account) != "zhipu":
        return True
    if kind == "name":
        data = _state(chat_id, "oa_zh_name", key=key, generation=account.get("generationId"))
        ui.edit(chat_id, message_id, "✏️ 当前名称: <code>" + ui.escape_html(account_display(account)) + "</code>\n\n请发送新名称（1–200 个字），仅修改名称，不改变凭据和调用设置。",
            reply_markup=ui.inline_kb([[ui.btn("取消", PREFIX + "name_skip:" + data["nonce"])]]))
    elif kind == "reset":
        _reset_page(chat_id, message_id, key)
    elif kind == "initialize":
        _initialize_account(chat_id, message_id, key)
    elif kind == "projects":
        _projects(chat_id, message_id, key)
    elif kind == "refresh":
        discard_state(chat_id)
        data = _state(chat_id, "oa_zh_work", key=key)
        ui.edit(chat_id, message_id, "🔄 正在刷新连接/解析已有 Key…", reply_markup=ui.inline_kb([[_back(key)]]))
        _run_remote(chat_id, data, lambda: control.refresh_token(telegram_context(chat_id), key),
            lambda result: _result(chat_id, message_id, key, "✅ 签名缓存已失效" + ("，重新解析已有模型 Key。OAuth 未续期。" if account["credential_mode"] == "oauth" else "；API Key 无 OAuth 续期，下一请求重新握手。")),
            lambda exc: _result(chat_id, message_id, key, "⚠️ 未能重新解析模型 Key。管理登录可能需重登；不会清除已有模型 Key。"))
    elif kind == "plan" and len(parts) >= 3:
        _plan_action(chat_id, message_id, key, parts[2], reset_type=parts[3] if len(parts) > 3 else None,
                     card_index=int(parts[4]) if len(parts) > 4 else 0)
    return True


def handle_text(chat_id, action, text):
    if action == "oa_zh_callback":
        state = states.get_state(chat_id)
        if not state or state["action"] != action:
            return True
        data = state["data"]
        if data.get("callback_received"):
            ui.send(chat_id, "授权已接收，正在初始化账户，请稍候。")
            return True
        def done(poll):
            data["callback_received"] = True
            if poll.status == "completed":
                _login_saved(chat_id, data.get("message_id"), data, poll)
            else:
                _show_projects(chat_id, data, poll)
        def failed(exc):
            if isinstance(exc, ValueError) and str(exc) == "invalid_callback":
                ui.send(chat_id, "❌ 回调地址不匹配或缺少参数。请复制本次授权跳转后的完整 127.0.0.1 地址，保留 code/authCode 和 state。")
            else:
                discard_state(chat_id)
                ui.send(chat_id, error_reporting.report(exc, operation="智谱OAuth回调处理") + "\n此次登录已结束，请重新发起登录。",
                        reply_markup=ui.inline_kb([[_back()]]))
        _run_remote(chat_id, data, lambda: control.submit_zhipu_callback(telegram_context(chat_id), data["flow_id"], data["flow_secret"], text.strip()), done, failed)
        return True
    if action == "oa_zh_name":
        state = states.get_state(chat_id)
        if not state or state["action"] != action:
            return True
        data = state["data"]
        name = text.strip()
        if not name or len(name) > 200 or any(ord(char) < 32 or ord(char) == 127 for char in name):
            ui.send(chat_id, "❌ 请输入 1–200 个字的单行名称。")
            return True
        current = control.account_snapshot(data["key"])
        if not current or current.get("generationId") != data.get("generation"):
            states.pop_state_if_current(chat_id, data)
            ui.send(chat_id, "⚠️ 账户已变更，请重新打开账户详情。")
            return True
        try:
            control.update_account(telegram_context(chat_id), data["key"],
                UpdateOAuthAccountCommand(display_name=name), expected_revision=revision(current))
            states.pop_state_if_current(chat_id, data)
            ui.send(chat_id, "✅ 账户名称已更新为 <code>" + ui.escape_html(name) + "</code>。",
                    reply_markup=ui.inline_kb([[_back(data["key"])]]))
        except Exception:
            ui.send(chat_id, "⚠️ 名称未保存，请重新打开账户详情后修改。")
        return True
    if action != "oa_zh_input":
        return False
    state = states.get_state(chat_id)
    data = state["data"] if state else {}
    if not state or data.get("busy"):
        return True
    payload = json.dumps({"site": data["site"], "credential_mode": "api_key", "model_key": text.strip()}) if data.get("key_only") else text
    progress_id = parent()._message_id_from_response(ui.send(chat_id, "🔄 正在保存账户并同步额度、模型及元数据…"))
    def saved(result):
        states.pop_state_if_current(chat_id, data)
        saved = control.account_snapshot(result.account_id) or {}
        if saved.get("credential_mode") == "oauth":
            _initialize_account(chat_id, progress_id, result.account_id, result=result)
            return
        _show_initial_sync(chat_id, result)
        if saved.get("credential_mode") == "api_key" and saved.get("label") in (None, "", saved.get("subject")):
            named = _state(chat_id, "oa_zh_name", key=result.account_id, generation=saved.get("generationId"))
            ui.send(chat_id, "✅ API Key 已导入。\n\n请发送账户名称，例如“智谱老号”或“智谱新号”；也可以跳过，稍后在详情修改。",
                reply_markup=ui.inline_kb([[ui.btn("跳过命名", PREFIX + "name_skip:" + named["nonce"])]]))
        else:
            ui.send(chat_id, "✅ 已保存账户。", reply_markup=ui.inline_kb([[_back(result.account_id)]]))
    def failed(exc):
        from ...management_control.oauth.account_mutations import OAuthReplaceRequired
        if isinstance(exc, OAuthReplaceRequired):
            replacement = _state(chat_id, "oa_zh_replace", payload=payload, plan_token=exc.plan_token)
            ui.send(chat_id, "账户已存在，是否确认替换凭据？", reply_markup=ui.inline_kb([[ui.btn("确认替换", PREFIX + "replace:" + replacement["nonce"])], [_back()]]))
        else:
            ui.send(chat_id, "❌ 无法保存，请检查凭据模式和必填字段；未创建任何远端 Key。", reply_markup=ui.inline_kb([[_back()]]))
    _run_remote(chat_id, data, lambda: control.create_account(telegram_context(chat_id),
        CreateOAuthAccountCommand(JsonCredential(OAuthProvider.ZHIPU, payload))), saved, failed)
    return True
