"""Qoder Telegram adapter. Shared OAuthControl owns every business effect.

Callbacks carry only stable account codes or short nonces, never tokens.
"""
from __future__ import annotations

import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

from ...management_control import ManagementError
from ...management_control.oauth import OAuthProvider, CompleteOAuthLoginCommand
from ...management_control.oauth.account_mutations import OAuthReplaceRequired
from ...management_control.oauth.menu_bridge import control as oauth_control, telegram_context
from ...oauth.qoder import common as qd_common
from . import oauth_add_session as add_session
from .. import menu_cache, states, ui

_BJT = timezone(timedelta(hours=8))
_PREFIX = "oa:qd:"
_NAV = {"back_label": "◀ 上一步", "back_callback": "oa:add:qoder"}
_LOGIN_INTERVAL = 3.0
_login_jobs: dict[int, dict] = {}
_login_jobs_lock = threading.RLock()

_CHECKIN_STATUS = {
    "claimed": "今日已领取", "already_done": "今日已领取", "not_offered": "今日暂未开放",
    "inactive": "当前无签到活动", "unsupported": "不可用", "failed": "失败",
}
_UNSUPPORTED = {
    "device_initialization_required": "请先确认设备档案并重授权",
    "runtime_info_missing": "本机未找到 Qoder runtime-info（安装 Qoder CLI 或设置 QODER_RUNTIME_INFO）",
    "runtime_info_failed": "Qoder runtime-info 运行失败", "runtime_info_invalid": "Qoder runtime-info 输出无效",
    "device_token_missing": "账户缺少设备 Token，请重新登录",
}


def _parent():
    from . import oauth_menu
    return oauth_menu


def _date(value) -> str:
    try:
        if isinstance(value, (int, float)):
            dt = datetime.fromtimestamp(value / 1000 if value > 1e12 else value, _BJT)
        else:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(_BJT)
        return dt.strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OverflowError, OSError):
        return "未知"


def _amount(value) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    return "?"


def _error(exc) -> str:
    if isinstance(exc, ManagementError):
        field = next(iter(exc.fields or ()), None)
        detail = getattr(field, "message", "") if field is not None else ""
        return "❌ " + ui.escape_html(exc.code.value + (f"：{detail}" if detail else ""))
    return "❌ " + ui.escape_html(type(exc).__name__)


def provider_line(account, *, detail=False) -> str:
    try:
        route = qd_common.route_label(account)
    except ValueError:
        route = "Qoder"
    method = "PAT" if account.get("qoder_auth_method") == "pat" else "设备授权"
    chat = " · 设备 Token 对话" if account.get("qoder_device_chat") else ""
    return f"🏷️ 路线: <code>{ui.escape_html(route)}</code> · {method}{chat}"


def checkin_label(snapshot) -> str:
    if not snapshot.get("checkin_supported"):
        reason = _UNSUPPORTED.get(snapshot.get("checkin_unsupported_reason") or "", "不可用")
        return f"🎁 每日签到: {ui.escape_html(reason)}"
    record = snapshot.get("checkin") or {}
    if not record:
        return "🎁 每日签到: 尚未执行"
    today = datetime.now(_BJT).strftime("%Y-%m-%d")
    status = record.get("status") or "unknown"
    text = _CHECKIN_STATUS.get(status, status)
    if status in {"claimed", "already_done"} and record.get("business_date") != today:
        text = "上次已领取（今日未查询）"
    if status in {"claimed", "already_done"} and record.get("credit"):
        text += f" +{_amount(record['credit'])}"
    if status == "unsupported":
        text += "：" + _UNSUPPORTED.get(record.get("error_kind") or "", "")
    if status == "failed" and record.get("error_kind"):
        text += f"（{ui.escape_html(str(record['error_kind']))}）"
    return f"🎁 每日签到: {text} · {_date(record.get('at'))}"


def _plan_expiration(value) -> str:
    if not value or (isinstance(value, str) and value.startswith("9999-")):
        return ""
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 1e12 else value
        if seconds >= datetime(9999, 1, 1, tzinfo=timezone.utc).timestamp():
            return ""
    return _date(value)


def _device_lines(device) -> list[str]:
    statuses = {"bound": "已绑定", "initialization_required": "待初始化确认",
                "migration_required": "待迁移确认", "invalid_profile": "档案无效"}
    tickets = {"cached": "已缓存", "backoff": "等待重试", "not_cached": "未缓存", "unavailable": "不可用"}
    status = str(device.get("status") or "initialization_required")
    ticket = str(device.get("ticket_status") or "未申请")
    platform = device.get("platform_label")
    parts = [str(platform)] if platform else []
    parts.extend((statuses.get(status, status), "票据 " + tickets.get(ticket, ticket)))
    lines = ["🖥️ 设备: " + ui.escape_html(" · ".join(parts))]
    hardware = device.get("hardware") or {}
    if hardware:
        model = " · ".join(str(hardware[key]) for key in ("manufacturer", "model") if hardware.get(key))
        if model:
            lines.append("型号: " + ui.escape_html(model))
        for key, label in (("system", "系统"), ("architecture", "架构")):
            if hardware.get(key):
                lines.append(label + ": " + ui.escape_html(str(hardware[key])))
        components = [label + ": " + ui.escape_html(str(hardware[key]))
                      for key, label in (("motherboard", "主板"), ("cpu", "CPU")) if hardware.get(key)]
        if components:
            lines.append(" · ".join(components))
        capacity = []
        for key, label in (("memory_bytes", "内存"), ("available_memory_bytes", "可用内存"), ("storage_bytes", "磁盘")):
            if isinstance(hardware.get(key), (int, float)) and hardware[key] > 0:
                capacity.append(label + ": " + _amount(hardware[key] / 1024**3) + " GiB")
        if capacity:
            lines.append(" · ".join(capacity))
    profile_id = device.get("profile_fingerprint") or device.get("profile_id")
    if profile_id:
        lines.append("档案 ID: <code>" + ui.escape_html(str(profile_id)) + "</code>")
    return lines


def usage_block(account_id, *, detail=False, snapshot=None) -> str:
    snapshot = snapshot or oauth_control.qoder_snapshot(account_id)
    usage = snapshot.get("usage") or {}
    lines = []
    until = _plan_expiration(usage.get("until"))
    if usage.get("plan") or until:
        lines.append(f"📦 套餐: <code>{ui.escape_html(str(usage.get('plan') or '?'))}</code>"
                     + (f" · 到期 {until}" if until else ""))
    pools = usage.get("pools") or []
    for pool in pools:
        lines.append(f"💳 积分: 剩余 <code>{_amount(pool.get('remaining'))}</code>"
                     f" / {_amount(pool.get('total'))}（已用 {_amount(pool.get('used_percent'))}%）")
    if not pools and not usage.get("plan"):
        lines.append("💳 积分: 未取得（点「刷新额度」查询）")
    if detail and usage.get("fetched_at"):
        lines.append(f"🕒 额度更新于 {_date(usage['fetched_at'])}")
    device = snapshot.get("device") or {}
    if detail:
        phases = {"intent": "刷新结果未确认，需重授权（非已吊销结论）", "unknown": "刷新结果不明，禁止重放，请重授权",
                  "candidate": "已取得候选，仅重试保存", "store_unavailable": "安全存储不可用，禁止消费刷新凭据"}
        for pair, phase in (snapshot.get("refresh_status") or {}).items():
            if phase in phases:
                lines.append(ui.escape_html(f"{pair}: {phases[phase]}"))
        lines.extend(_device_lines(device))
        lines.append("🌐 代理: " + ui.escape_html(str(snapshot.get("proxy") or "继承现有规则")))
        lines.append(checkin_label(snapshot))
    elif snapshot.get("auto_checkin"):
        lines.append("🎁 自动签到已开")
    return "\n".join(lines)


def _nav_cb(kind, short, page, filter_key):
    return _PREFIX + kind + ":" + _parent()._callback_payload(short, page, filter_key)


def detail_buttons(account, short, page, filter_key) -> list[list[dict]]:
    auto = account.get("qoder_auto_checkin") is True
    return [[ui.btn("🎁 立即签到", _nav_cb("checkin", short, page, filter_key)),
             ui.btn(("✅" if auto else "⬜") + " 每日自动签到", _nav_cb("auto", short, page, filter_key))],
            [ui.provider_button("设备档案 / 重授权", _nav_cb("device", short, page, filter_key), "qoder")]]


# ---- sign-in ---------------------------------------------------------------------

def _set_state(chat_id, action, data):
    data = dict(data, nonce=secrets.token_hex(6))
    states.set_state(chat_id, action, data)
    return data


def _state(chat_id, nonce, *actions):
    state = states.get_state(chat_id)
    if not state or state["action"] not in actions or state["data"].get("nonce") != nonce:
        return None
    return state["data"]


def discard_state(chat_id):
    state = states.get_state(chat_id)
    if state and state["action"] in {"oa_qd_login", "oa_qd_device", "oa_qd_pat"}:
        data = state["data"]
        with _login_jobs_lock:
            job = _login_jobs.get(chat_id)
            if job and job["data"] is data:
                job["stop"].set()
                job["wake"].set()
        try:
            oauth_control.cancel_login_flow(telegram_context(chat_id), data["flow_id"], data["flow_secret"])
        except ManagementError:
            pass
    if state and state["action"].startswith("oa_qd_"):
        states.pop_state(chat_id)


def show_add(chat_id, message_id, cb_id=""):
    add_session.clear_input(chat_id)
    if cb_id:
        ui.answer_cb(cb_id)
    ui.edit(chat_id, message_id,
        f"{ui.provider_custom_emoji_html('qoder')} <b>新增 Qoder OAuth 账户</b>\n"
        "国际区（qoder.com）：Google / GitHub / 邮箱账户，可用设备授权或 PAT。\n"
        "中国区（qoder.cn）：阿里云 / 手机号账户，仅设备授权。\n"
        "最好不要多个账户共用出口，可在设置代理中指定；不同代理名称不等于不同出口。\n"
        "浏览器授权使用浏览器自己的网络。",
        reply_markup=ui.inline_kb([
            [ui.btn("🌐 国际区 · 浏览器授权", _PREFIX + "login:global")],
            [ui.btn("🔑 国际区 · PAT", _PREFIX + "pat")],
            [ui.btn("🇨🇳 中国区 · 浏览器授权", _PREFIX + "login:cn")],
            [ui.btn("⚙ 设置代理", "oa:add:proxy"), ui.btn("◀ 上一步", "oa:add")],
        ]))


def _pat_prompt(chat_id, message_id, *, relogin=None):
    from .qoder_device_menu import prepare
    return prepare(chat_id, message_id, method="pat", relogin=relogin)


def on_pat_input(chat_id, text):
    from .qoder_device_menu import pat_input
    return pat_input(chat_id, text)


def _login_start(chat_id, message_id, *, realm="global", pat=None, relogin=None):
    from .qoder_device_menu import prepare
    return prepare(chat_id, message_id, realm=realm, relogin=relogin)


def _login_started(chat_id, message_id, flow, *, realm, relogin=None):
    parent = _parent()
    data = _set_state(chat_id, "oa_qd_login", {"flow_id": flow.flow_id, "flow_secret": flow.flow_secret, "realm": realm,
        "auth_url": flow.auth_url, "expires_at": flow.expires_at, "relogin": relogin})
    if flow.auth_url:
        region = "国际区" if realm == "global" else "中国区"
        ui.edit(chat_id, message_id,
            f"{ui.provider_custom_emoji_html('qoder')} <b>Qoder {region}登录</b>\n路线: {ui.escape_html(qd_common.route_label({'realm': realm}))}\n"
            "打开授权页面登录并授权此设备；后台每 3 秒自动检查，成功即保存并显示身份。同一身份重新登录只更新授权，保留原有设置。\n"
            f"截止时间: {flow.expires_at.isoformat()}（从准备档案起计时）。"
            + parent._login_hint(relogin),
            reply_markup=ui.inline_kb([[ui.provider_url_button("打开授权页面", flow.auth_url, "qoder")],
                [ui.btn("🔎 检查登录", _PREFIX + "poll:" + data["nonce"])],
                [ui.btn("❌ 取消", _PREFIX + "cancel:" + data["nonce"])]]))
    _login_poll(chat_id, message_id, data)


def _login_feedback(data, poll=None, error=None):
    parent = _parent()
    status = poll.status if poll is not None else "pending"
    if status == "completed":
        preview = poll.account_preview or {}
        text = "✅ 授权已保存。" if poll.save_status != "replaced" else "✅ 授权已更新，原有账户设置已保留。"
        text += "\n" + ui.escape_html(str(preview.get("label") or preview.get("email") or preview.get("uid") or "Qoder"))
        text += "\n路线: " + ui.escape_html(qd_common.route_label({"realm": preview.get("realm") or data.get("realm")}))
        if preview.get("qoder_device_chat"):
            text += "\nQoder CN 未取得 job token；已验证设备 Token 的模型访问，使用设备 Token 对话。"
        text += "\n模型目录和额度以各自最新同步状态为准。"
        return text, ui.inline_kb([[ui.provider_button("查看账户", "oa:view:" + ui.register_code(poll.account_id) + ":1", "qoder")],
                                  [ui.btn("◀ 返回列表", "menu:oauth")]])
    back = [[ui.btn(parent._login_nav(data.get("relogin"), provider="qoder")["back_label"], parent._login_cancel(data.get("relogin"), provider="qoder"))]]
    if status in {"cancelled", "expired"}:
        return "ℹ️ 登录流程已取消或过期，请重新开始。", ui.inline_kb(back)
    text = "⌛ 等待浏览器授权，后台自动检查中；成功后直接保存。"
    if status == "identity_pending":
        text = "⌛ 已取得设备授权，正在换取对话凭据并确认身份；不会重复轮询授权。"
    if error is not None:
        text = _error(error) + "\n后台将继续检查，也可手动检查或取消。"
    rows = [[ui.provider_url_button("打开授权页面", data["auth_url"], "qoder")]] if data.get("auth_url") else []
    rows += [[ui.btn("🔎 检查登录", _PREFIX + "poll:" + data["nonce"])],
             [ui.btn("❌ 取消", _PREFIX + "cancel:" + data["nonce"])]]
    return text, ui.inline_kb(rows)


def _run_login_job(chat_id, job):
    data, owner = job["data"], job["owner"]
    last_render = None
    try:
        while not job["stop"].is_set() and _state(chat_id, data["nonce"], "oa_qd_login") is data:
            job["wake"].clear()
            poll, error = None, None
            try:
                if data.pop("replace_confirmed", False):
                    owner.complete_login_flow(telegram_context(chat_id), data["flow_id"], data["flow_secret"],
                        CompleteOAuthLoginCommand(completed=True, replace_plan_token=data["replace_plan_token"]))
                    data.pop("replace_plan_token", None)
                poll = owner.poll_login_flow(telegram_context(chat_id), data["flow_id"], data["flow_secret"])
            except OAuthReplaceRequired as exc:
                data["replace_plan_token"] = exc.plan_token
                job["awaiting_confirm"] = True

                def confirm():
                    if _state(chat_id, data["nonce"], "oa_qd_login") is data:
                        ui.edit(chat_id, job["message_id"], "账户已存在。是否覆盖凭据并将本次选择保存为该账户代理？取消不修改账户。",
                            reply_markup=ui.inline_kb([
                                [ui.btn("✅ 确认覆盖", _PREFIX + "replace:" + data["nonce"])],
                                [ui.btn("❌ 取消", _PREFIX + "cancel:" + data["nonce"])]]))
                menu_cache.run_if_current(chat_id, job["message_id"], job["view_token"], confirm)
                break
            except Exception as exc:
                error = exc
            rejected_target = getattr(getattr(error, "code", None), "value", "") in {
                "IDENTITY_CONFLICT", "RESOURCE_CONFLICT", "REVISION_CONFLICT", "RESOURCE_NOT_FOUND",
            }
            terminal = rejected_target or (poll is not None and poll.status in {"completed", "cancelled", "expired"})
            expired = not terminal and time.monotonic() >= job["deadline"]
            text, kb = _login_feedback(data, poll, error)
            if rejected_target:
                text = "❌ 登录身份不匹配或原账户已变化；未修改或新增账户。请返回原账户重新登录。"
                kb = ui.inline_kb([[ui.btn("◀ 返回账户", _parent()._login_cancel(data.get("relogin"), provider="qoder"))]])
            if expired:
                text, kb = _login_feedback(data, type("Poll", (), {"status": "expired"})())
            with _login_jobs_lock:
                message_id, token = job["message_id"], job["view_token"]
            render_key = (message_id, token, text)

            def render():
                if not job["stop"].is_set() and _state(chat_id, data["nonce"], "oa_qd_login") is data:
                    ui.edit(chat_id, message_id, text, reply_markup=kb)

            if render_key != last_render:
                try:
                    if menu_cache.run_if_current(chat_id, message_id, token, render):
                        last_render = render_key
                except Exception:
                    pass  # A Telegram delivery failure must not abort authorization.
            if terminal or expired:
                states.pop_state_if_current(chat_id, data)
                if expired or rejected_target:
                    try:
                        owner.cancel_login_flow(telegram_context(chat_id), data["flow_id"], data["flow_secret"])
                    except ManagementError:
                        pass
                break
            job["wake"].wait(min(_LOGIN_INTERVAL, max(0, job["deadline"] - time.monotonic())))
    finally:
        with _login_jobs_lock:
            if _login_jobs.get(chat_id) is job:
                _login_jobs.pop(chat_id, None)


def _login_poll(chat_id, message_id, data):
    """Manual checks wake the one bounded worker; the TG poll loop never waits."""
    token = menu_cache.begin_view(chat_id, message_id)
    with _login_jobs_lock:
        job = _login_jobs.get(chat_id)
        if job and job["data"] is data and not job["stop"].is_set() and not job.get("awaiting_confirm"):
            job.update(message_id=message_id, view_token=token)
            job["wake"].set()
            return
        if job:
            job["stop"].set()
            job["wake"].set()
        expires = data.get("expires_at")
        remaining = max(0, min(900, (expires - datetime.now(timezone.utc)).total_seconds())) if expires else 900
        job = {"data": data, "owner": oauth_control, "message_id": message_id, "view_token": token,
               "deadline": time.monotonic() + remaining, "wake": threading.Event(), "stop": threading.Event()}
        _login_jobs[chat_id] = job
    threading.Thread(target=lambda: _run_login_job(chat_id, job), daemon=True, name="qoder-login-ui").start()


def start_relogin(chat_id, message_id, account, relogin):
    if account.get("qoder_auth_method") == "pat":
        return _pat_prompt(chat_id, message_id, relogin=relogin)
    return _login_start(chat_id, message_id, realm=account.get("realm") or "global", relogin=relogin)


# ---- account actions ---------------------------------------------------------------

def _checkin_text(record) -> str:
    status = record.get("status") or "unknown"
    if status in {"claimed", "already_done"}:
        credit = f" +{_amount(record['credit'])}" if record.get("credit") else ""
        return ("✅ 已领取今日积分" if status == "claimed" else "ℹ️ 今日积分已领取，未重复领取") + credit
    if status == "not_offered":
        return "ℹ️ 签到活动已列出，但今日积分尚未开放领取；稍后再试。"
    if status == "inactive":
        return "ℹ️ 当前账户没有可领取的签到活动。"
    if status == "unsupported":
        return "⚠️ 无法签到：" + _UNSUPPORTED.get(record.get("error_kind") or "", "当前账户不支持")
    return "❌ 签到失败" + (f"（{ui.escape_html(str(record.get('error_kind')))}）" if record.get("error_kind") else "")


def _run_checkin(chat_id, message_id, key, page, filter_key):
    try:
        result = oauth_control.checkin_qoder_now(telegram_context(chat_id), key)
        prefix = _checkin_text(result)
    except Exception as exc:
        prefix = _error(exc)
    _parent()._edit_cached_detail(chat_id, message_id, key, page, filter_key, prefix=prefix + "\n\n")


def handle_text(chat_id, action, text) -> bool:
    if action == "oa_qd_pat":
        on_pat_input(chat_id, text)
        return True
    return False


def handle_callback(chat_id, message_id, cb_id, callback):
    if callback == "oa:add:qoder":
        show_add(chat_id, message_id, cb_id)
        return True
    if not callback.startswith(_PREFIX):
        return False
    parts = callback[len(_PREFIX):].split(":")
    kind = parts[0]
    if kind in {"device_confirm", "device_regenerate", "device_cancel", "device_platform"}:
        from .qoder_device_menu import handle
        ui.answer_cb(cb_id)
        if kind == "device_platform":
            handle(chat_id, message_id, kind, parts[1] if len(parts) == 3 else "",
                   platform=parts[2] if len(parts) == 3 else None)
        else:
            handle(chat_id, message_id, kind, parts[1] if len(parts) == 2 else "")
        return True
    if kind == "login" and len(parts) == 2 and parts[1] in {"global", "cn"}:
        ui.answer_cb(cb_id)
        _login_start(chat_id, message_id, realm=parts[1])
        return True
    if kind == "pat" and len(parts) == 1:
        ui.answer_cb(cb_id)
        _pat_prompt(chat_id, message_id)
        return True
    if kind in {"poll", "replace", "cancel"}:
        ui.answer_cb(cb_id)
        data = _state(chat_id, parts[1] if len(parts) == 2 else "", "oa_qd_login")
        if not data:
            ui.send_result(chat_id, "⚠️ 按钮已过期，请返回原页面重新开始。", **_NAV)
            return True
        if kind == "cancel":
            discard_state(chat_id)
            if data.get("relogin"):
                _parent()._return_from_relogin(chat_id, message_id, "", data["relogin"]["payload"])
            else:
                show_add(chat_id, message_id)
            return True
        if data.get("replace_plan_token"):
            if kind != "replace":
                return True
            data["replace_confirmed"] = True
        _login_poll(chat_id, message_id, data)
        return True
    if kind in {"checkin", "auto", "device"}:
        short, page, filter_key = _parent()._split_short_page_filter(":".join(parts[1:]))
        key = _parent()._account_key_from_short(short)
        if not key:
            ui.answer_cb(cb_id, "账户已不存在")
            return True
        if kind == "device":
            ui.answer_cb(cb_id)
            account = oauth_control.account_snapshot(key) or {}
            start_relogin(chat_id, message_id, account,
                          {"target_key": key, "payload": _parent()._callback_payload(short, page, filter_key)})
            return True
        if kind == "checkin":
            ui.answer_cb(cb_id, "签到中…")
            threading.Thread(target=_run_checkin, args=(chat_id, message_id, key, page, filter_key),
                             daemon=True, name="qoder-checkin-ui").start()
            return True
        ui.answer_cb(cb_id)
        account = oauth_control.account_snapshot(key) or {}
        enabled = account.get("qoder_auto_checkin") is not True
        try:
            revision = oauth_control.get_account(telegram_context(chat_id), key).account.revision
            oauth_control.update_qoder_settings(telegram_context(chat_id), key, auto_checkin=enabled, expected_revision=revision)
            prefix = ("✅ 已开启每日自动签到：每个北京日领取一次，之后每 30 分钟核对是否有新活动；暂停或认证失效时跳过。"
                      if enabled else "⏸ 已关闭每日自动签到。")
        except Exception as exc:
            prefix = _error(exc)
        _parent()._edit_cached_detail(chat_id, message_id, key, page, filter_key, prefix=prefix + "\n\n")
        return True
    ui.answer_cb(cb_id)
    return True
