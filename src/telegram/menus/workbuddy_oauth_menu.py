"""WorkBuddy Telegram adapter. Shared OAuthControl owns every business effect.

The list/detail fragments follow docs/workbuddy-ui-baseline.md; callbacks carry
only stable account codes or short nonces, never tokens or plan capabilities.
"""
from __future__ import annotations

import math
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

from ...management_control import ManagementError
from ...management_control.oauth import OAuthProvider
from ...management_control.oauth.menu_bridge import control as oauth_control, telegram_context
from .. import menu_cache, states, ui

_BJT = timezone(timedelta(hours=8))
_PREFIX = "oa:wb:"
_NAV = {"back_label": "◀ 返回新增账户", "back_callback": "oa:add"}
_LOGIN_INTERVAL = 5.0
_login_jobs: dict[int, dict] = {}
_login_jobs_lock = threading.RLock()


def _parent():
    from . import oauth_menu
    return oauth_menu


def _number(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
        return float(value)
    return None


def _num(value):
    # Presentation only: retain raw precision in billing/quota snapshots so a
    # positive sub-cent balance never becomes an exhausted account.
    value = _number(value)
    return f"{value:,.2f}" if value is not None else "未知"


def _date(value, *, short=False):
    try:
        dt = (datetime.fromtimestamp(float(value) / 1000, _BJT) if isinstance(value, (float, int))
              else datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(_BJT))
        return dt.strftime("%H:%M:%S" if short else "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OverflowError, OSError):
        return "未知"


def checkin_label(snapshot):
    checkin = snapshot.get("checkin") or {}
    observed = _number(checkin.get("observed_at"))
    if observed is None or datetime.fromtimestamp(observed / 1000, _BJT).date() != datetime.now(_BJT).date():
        return "今日状态未知"
    if checkin.get("active") is False:
        return "当前活动未开放"
    checked = checkin.get("today_checked_in")
    return "今日已签到" if checked is True else "今日未签到" if checked is False else "今日状态未知"


def provider_line(account, *, detail=False):
    region = "中国区" if account.get("realm") == "cn" else "国际区"
    scope = "企业" if account.get("enterprise_id") else "个人"
    selection = oauth_control.account_model_selection_snapshot(account)
    line = f"🏷️ 账户: {scope} · {region}"
    if detail:
        disabled = len(set(selection.get("models") or []) & set(selection.get("disabled_models") or []))
        line += f"\n🧬 模型目录: {len(selection.get('effective_models') or [])} 个可用模型 · 禁用 {disabled}"
    else:
        line += f" · 模型 {len(selection.get('models') or [])}"
    return line


def credit_line(credits, *, label="积分", reliable=False, suffix=""):
    credits = credits or {}
    capacity, remaining, used = (_number(credits.get(key)) for key in ("capacity", "remaining", "used"))
    if used is None and capacity is not None and remaining is not None and remaining <= capacity:
        used = capacity - remaining
    mode = oauth_control.config_snapshot().get("oauthUsageDisplayMode") or "used"
    selected, title = (remaining, "剩余") if mode == "remaining" else (used, "已用")
    if reliable and credits.get("reliable") is True and capacity and selected is not None:
        pct = min(100.0, max(0.0, selected * 100 / capacity))
        return f"🪙 {label}: {title}{ui.quota_progress_html(pct, width=10)} <b>{pct:.2f}%</b>（{_num(selected)} / {_num(capacity)}）{suffix}"
    if remaining is not None:
        total = "总量未知" if capacity is None else f"总量 {_num(capacity)}"
        return f"🪙 {label}: 剩余 {_num(remaining)}（{total}）{suffix}"
    if used is not None:
        return f"🪙 {label}: 已用 {_num(used)}（剩余未知）{suffix}"
    return f"🪙 {label}: 未知{suffix}"


def _empty_packages(snapshot):
    return snapshot.get("status") == "empty" and snapshot.get("complete") is True


def _package_page(snapshot, page=1):
    old = not _empty_packages(snapshot) and not (snapshot.get("complete") is True and (snapshot.get("credits") or {}).get("reliable")) and bool(snapshot.get("last_success_credits"))
    packages = (snapshot.get("last_success_packages") if old else snapshot.get("packages")) or []
    pages = max(1, math.ceil(len(packages) / 4))
    page = max(1, min(int(page or 1), pages))
    return packages, page, pages


def usage_block(account_id, *, detail=False, package_page=1, snapshot=None):
    snap = oauth_control.workbuddy_snapshot(account_id) if snapshot is None else snapshot
    credits = snap.get("credits") or {}
    complete = snap.get("complete") is True
    empty = _empty_packages(snap)
    old = not empty and not (complete and credits.get("reliable")) and bool(snap.get("last_success_credits"))
    if old:
        credits = snap["last_success_credits"]
    if not credits and not snap.get("fetched_at"):
        return "📊 用量: 尚未获取"
    stale = old or (_number(snap.get("fetched_at")) is not None and time.time() * 1000 - snap["fetched_at"] > 900000)
    suffix = "（旧快照）" if stale else "（数据不完整）" if not complete else ""
    label = "企业积分" if credits.get("scope") == "enterprise" else "积分"
    lines = [f"🪙 积分: 当前未查到有效资源包{suffix}" if empty else credit_line(credits, label=label, reliable=complete or old, suffix=suffix)]
    if detail:
        packages, selected_page, package_pages = _package_page(snap, package_page)
        personal = snap.get("personal_credits") or {}
        if label == "企业积分":
            lines.append(credit_line(personal, label="个人积分（独立）", reliable=personal.get("reliable") is True))
        package_credit = personal if label == "企业积分" else credits
        if not empty:
            lines.append(f"📦 资源包: {len(packages)} 个 · 剩余 {_num(package_credit.get('remaining'))} 积分" + ("（旧快照）" if old else ""))
        ends = [p["expires_at"] for p in packages if p.get("expires_at")]
        if ends:
            lines.append(f"📅 资源包最近到期: {_date(min(ends))}")
        if package_pages > 1:
            lines.append(f"积分包第 {selected_page}/{package_pages} 页")
        for index, item in enumerate(packages[(selected_page - 1) * 4:selected_page * 4], (selected_page - 1) * 4 + 1):
            lines.append(f"  {index}. <b>{ui.escape_html(str(item.get('name') or '未命名资源包')[:100])}</b>")
            lines.append(f"     总量 {_num(item.get('capacity'))} · 已用 {_num(item.get('used'))} · 剩余 {_num(item.get('remaining'))}")
            if item.get("cycle_end") and item.get("cycle_end") != item.get("expires_at"):
                lines.append(f"     本周期结束: {_date(item['cycle_end'])}")
            if item.get("expires_at"):
                lines.append(f"     资源包到期: {_date(item['expires_at'])}")
        if snap.get("realm") == "cn":
            lines.append("🎁 签到: " + checkin_label(snap))
        if snap.get("payment_type"):
            lines.append("🏷️ 计费类型: " + ui.escape_html(str(snap["payment_type"])))
        lines.extend(_query_errors(snap))
        stamp = snap.get("last_success_at") if old else snap.get("fetched_at")
        if stamp:
            lines.append(f"<i>{'上次成功' if old else '更新于'} {_date(stamp, short=not old)}</i>")
    return "\n".join(lines)


def _cb(kind, nav, page=1):
    short, account_page, filter_key = nav
    return f"{_PREFIX}{kind}:{short}:{account_page}:{filter_key}:{page}"


def _back(nav):
    short, page, filter_key = nav
    return ui.btn("◀ 返回账户", "oa:view:" + _parent()._callback_payload(short, page, filter_key))


def _resolve(nav):
    key = _parent()._account_key_from_short(nav[0])
    if not key or oauth_control.provider_of_snapshot(key) != "workbuddy":
        return None
    return key


def _error(exc):
    codes = {getattr(item, "code", "") for item in getattr(exc, "fields", ())}
    if "DISABLED" in codes:
        return "⚠️ 当前环境禁止上游网络请求，未提交签到或试用申请。"
    if "STATUS_UNKNOWN" in codes:
        return "⚠️ 今日活动状态未知，未提交。请先查询核对。"
    if "ACTIVITY_INACTIVE" in codes:
        return "ℹ️ 上游确认当前活动未开放，未提交。"
    if "ACCOUNT_PAUSED" in codes:
        return "⚠️ 账户已停用或认证失效，活动暂停。"
    code = getattr(getattr(exc, "code", None), "value", "UPSTREAM_ERROR")
    if code in {"REVISION_CONFLICT", "INVALID_OPERATION_STATE", "STATE_CONFLICT"}:
        return "⚠️ 确认已失效或账户发生变化，请返回页面重新确认。"
    return "⚠️ 查询或操作未能完成；若请求已发出，结果可能未知，请先查询记录核对，不要重复提交。"


def _query_error_detail(error):
    """Only normalized error kinds/numeric codes, never upstream text or URLs."""
    kind = error.get("kind")
    labels = {"network": "网络请求失败", "deadline": "查询超时", "timeout": "查询超时",
              "invalid_packages": "资源包格式异常（invalid_packages）", "invalid_envelope": "响应结构异常",
              "invalid_data": "返回数据格式异常", "unstable_pagination": "资源包分页数据变化",
              "repeated_page": "资源包分页重复", "count_mismatch": "资源包数量不一致",
              "incomplete_pages": "资源包分页不完整", "page_limit": "资源包查询达到分页上限"}
    reason = "登录凭据已失效，请重新登录" if error.get("auth_error") else labels.get(kind, "上游查询失败")
    details = []
    for field, label in (("http_status", "HTTP"), ("code", "code")):
        value = error.get(field)
        if isinstance(value, int) and not isinstance(value, bool) and (field == "code" or value > 0):
            details.append(f"{label} {value}")
    return reason + ("（" + "，".join(details) + "）" if details else "")


def _query_errors(snapshot):
    errors = snapshot.get("errors") or {}
    labels = {"credits": "积分/资源包", "pagination": "资源包分页", "enterprise": "企业积分",
              "checkin": "签到状态", "payment_type": "计费类型"}
    return [f"⚠️ {label}: {_query_error_detail(errors[key])}" for key, label in labels.items()
            if isinstance(errors.get(key), dict)]


def query_result_text(snapshot, *, error=None):
    """Shared read-only query feedback for account details and activity pages."""
    if error is not None:
        from ...oauth.workbuddy.common import WorkBuddyError
        if isinstance(error, WorkBuddyError):
            reason = _query_error_detail({"kind": error.kind, "http_status": error.status_code,
                                          "code": error.code, "auth_error": error.auth_error})
        elif isinstance(error, TimeoutError):
            reason = "查询超时"
        elif isinstance(error, ValueError):
            reason = "返回数据格式异常"
        elif isinstance(error, OSError):
            reason = "本地数据读取或保存失败"
        else:
            reason = "查询未能完成，请稍后重试"
        lines = ["❌ 查询失败：" + reason]
        if snapshot.get("fetched_at"):
            lines.append("页面数据未更新，以下仍为上次快照。")
    else:
        errors = _query_errors(snapshot)
        if errors:
            lines = ["⚠️ 查询部分失败", *errors]
        elif _empty_packages(snapshot):
            lines = ["✅ 查询完成：当前未查到有效资源包。"]
        elif snapshot.get("complete") is True and (snapshot.get("credits") or {}).get("reliable"):
            lines = ["✅ 已更新积分与活动" if snapshot.get("realm") == "cn" else "✅ 已更新积分与账户状态"]
        else:
            lines = ["⚠️ 查询完成，但积分数据不完整；未知值不视为零。"]
        if not _empty_packages(snapshot) and snapshot.get("last_success_credits"):
            lines.append("积分显示上次成功值（旧快照），不是本次余额。")
    lines.append("🕒 查询结束: " + _date(time.time() * 1000))
    lines.append("本次仅查询，未执行签到。" if snapshot.get("realm") == "cn" else "本次仅查询，未申请试用额度。")
    return "\n".join(lines)


def show_query_progress(chat_id, message_id):
    ui.edit(chat_id, message_id, "🔄 查询中…\n正在读取活动与余额，请稍候。", reply_markup=ui.inline_kb([]))


def _start_query_worker(worker):
    thread = threading.Thread(target=worker, daemon=True, name="workbuddy-status-ui")
    thread.start()
    return thread


def start_status_query(chat_id, message_id, key, render, *, control=None):
    """Keep vendor waits off polling; only the still-current page may be edited."""
    owner = control if control is not None else oauth_control
    token = menu_cache.begin_view(chat_id, message_id)
    show_query_progress(chat_id, message_id)

    def worker():
        try:
            snapshot = owner.refresh_workbuddy_status_now(telegram_context(chat_id), key)
            feedback = query_result_text(snapshot)
        except Exception as exc:
            try:
                snapshot = owner.workbuddy_snapshot(key)
            except Exception:
                snapshot = {}
            feedback = query_result_text(snapshot, error=exc)

        def finish():
            if owner.account_snapshot(key) is None:
                text, kb = None, None
            else:
                text, kb = render(feedback)
            if not text:
                text = "⚠️ 账户已不存在，请返回列表。"
                kb = ui.inline_kb([[ui.btn("◀ 返回列表", "menu:oauth")]])
            ui.edit(chat_id, message_id, text, reply_markup=kb)

        menu_cache.run_if_current(chat_id, message_id, token, finish)

    return _start_query_worker(worker)


def result_text(record):
    action = "签到" if record.get("action") == "checkin" else "试用申请"
    status = record.get("status")
    if status == "succeeded":
        text = f"✅ {action}已完成"
        if record.get("awarded_credits") is not None:
            text += f"；本次获得 {_num(record['awarded_credits'])} 分"
        if record.get("balance_updated") is False:
            text += "，余额待更新"
        return text
    if status == "already_done":
        return f"ℹ️ 上游确认{action}已完成，未重复提交。"
    if status in {"pending", "unknown"}:
        return "⚠️ 结果未知，可能已提交；仅查询核对，不自动重放请求。"
    if status in {"failed", "rejected"}:
        if record.get("http_status") == 401 or record.get("code") == 12153:
            return "❌ 上游拒绝了当前登录凭据；请重新登录后再试。此次未确认获得积分。"
        return "❌ 上游明确拒绝，未确认获得积分；再次尝试需要新的确认。"
    return "暂无执行记录"


def package_page_buttons(key, nav, page=1):
    _packages, page, pages = _package_page(oauth_control.workbuddy_snapshot(key), page)
    if pages <= 1:
        return []
    row = [ui.btn(f"◀ {page-1}", _cb("credits", nav, page-1))] if page > 1 else []
    row.append(ui.btn(f"📦 积分包 {page}/{pages}", "oa:wb:noop"))
    if page < pages:
        row.append(ui.btn(f"{page+1} ▶", _cb("credits", nav, page+1)))
    return [row]


def render_credits(key, nav, page=1):
    # Old sent buttons and inline package pagination return the complete account
    # detail, not a second-level credits page. Account-list navigation is kept.
    return _parent()._detail_text_and_kb(
        key, page=nav[1], filter_key=nav[2], refresh_quota=False,
        workbuddy_package_page=page,
    )


def render_activity(key, nav, *, chat_id=0, query_feedback=""):
    value = oauth_control.get_workbuddy(telegram_context(chat_id), key)
    snap, records = value["snapshot"], value["actions"]
    cn = snap.get("realm") == "cn"
    action = "checkin" if cn else "claim_trial"
    today = datetime.now(_BJT).strftime("%Y-%m-%d") if cn else "lifetime"
    prior = next((r for r in records if r.get("action") == action and r.get("business_date") == today), None)
    account = oauth_control.account_snapshot(key) or {}
    lines = ["🎁 <b>签到/领额度</b>", ""]
    if query_feedback:
        lines += [query_feedback, ""]
    lines.append(usage_block(key, snapshot=snap))
    if snap.get("payment_type"):
        lines.append("🏷️ 计费类型: " + ui.escape_html(str(snap["payment_type"])))
    if snap.get("fetched_at"):
        lines.append("<i>数据更新于 " + _date(snap["fetched_at"]) + "</i>")
    if not query_feedback:
        lines.extend(_query_errors(snap))
    lines += ["", ("签到: " + checkin_label(snap)) if cn else "申请试用额度：资格、额度及有效期以上游实际结果为准。"]
    if prior:
        lines += [result_text(prior)]
    if cn:
        lines += ["自动签到: " + ("开启" if snap.get("auto_checkin") else "关闭"), "每天北京时间 09:05 主轮、21:05 补漏；先查状态，已签不重复提交，未知仅核对。手动停用或认证失效时暂停。"]
    if account.get("disabled_reason") not in (None, "quota") or (not account.get("enabled", True) and account.get("disabled_reason") != "quota"):
        lines.append("⚠️ 账户已停用或认证失效，活动暂停。")
    rows = [[ui.btn("🔄 查询活动/余额", _cb("refresh_activity", nav)), ui.btn("📜 操作记录", _cb("records", nav))]]
    uncertain = prior and prior.get("status") in {"pending", "unknown"}
    done = (prior and prior.get("status") in {"succeeded", "already_done"}) or (cn and checkin_label(snap) == "今日已签到")
    if uncertain:
        rows.append([ui.btn("🔎 核对结果（不重试）", _cb("refresh_activity", nav))])
    elif not done:
        rows.append([ui.btn("🎁 立即签到" if cn else "🎁 申请试用额度", _cb("plan", nav))])
    if cn:
        rows.append([ui.btn("⏸ 关闭自动签到" if snap.get("auto_checkin") else "▶️ 开启自动签到", _cb("auto", nav))])
    rows.append([_back(nav)])
    return "\n".join(lines), ui.inline_kb(rows)


def render_records(key, nav, page=1, *, chat_id=0):
    data = oauth_control.get_workbuddy_records(telegram_context(chat_id), key, page=page, page_size=4)
    lines = ["📜 <b>活动操作记录</b>", ""]
    for record in data["items"]:
        lines += [f"{ui.escape_html(str(record.get('business_date') or '未知'))} · {'自动' if record.get('source') == 'auto' else '手动'}",
                  result_text(record), "更新: " + _date(record.get("updated_at")), ""]
    if not data["items"]:
        lines.append("暂无记录")
    buttons = []
    if page > 1:
        buttons.append(ui.btn("◀ 上一页", _cb("records", nav, page-1)))
    if data["has_next"]:
        buttons.append(ui.btn("下一页 ▶", _cb("records", nav, page+1)))
    rows = [buttons] if buttons else []
    rows += [[ui.btn("◀ 返回活动", _cb("activity", nav))], [_back(nav)]]
    return "\n".join(lines), ui.inline_kb(rows)


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
    if state and state["action"] == "oa_wb_login":
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
    if state and state["action"].startswith("oa_wb_"):
        states.pop_state(chat_id)


def _confirm(chat_id, message_id, *, text, data):
    data = _set_state(chat_id, "oa_wb_confirm", data)
    ui.edit(chat_id, message_id, text, reply_markup=ui.inline_kb([
        [ui.btn("✅ 确认执行", _PREFIX + "confirm:" + data["nonce"])],
        [ui.btn("❌ 取消", _PREFIX + "cancel:" + data["nonce"])]]))


def _plan_action(chat_id, message_id, key, nav, *, allow_unknown=False, trial_confirmed=False):
    account = oauth_control.account_snapshot(key) or {}
    cn = account.get("realm") == "cn"
    if not cn and not trial_confirmed:
        data = _set_state(chat_id, "oa_wb_terms", {"nav": nav, "key": key})
        ui.edit(chat_id, message_id,
            "🎁 <b>申请试用额度</b>\n请先核实账户当前官方试用条件。仅申请免费试用，不购买、不续费；额度与有效期以上游结果为准。\n确认资格与免费条件后，继续到最终确认页。",
            reply_markup=ui.inline_kb([[ui.btn("已核实免费条件，继续", _PREFIX + "terms:" + data["nonce"])],
                                      [ui.btn("❌ 取消", _PREFIX + "cancel:" + data["nonce"])]]))
        return
    try:
        records = oauth_control.get_workbuddy(telegram_context(chat_id), key)["actions"]
        today = datetime.now(_BJT).strftime("%Y-%m-%d") if cn else "lifetime"
        retry = any(r.get("business_date") == today and r.get("status") in {"failed", "rejected"} for r in records)
        plan = oauth_control.plan_workbuddy_action(telegram_context(chat_id), key, "checkin" if cn else "claim_trial",
            allow_unknown=allow_unknown, free_trial_confirmed=trial_confirmed, retry_failed=retry)
    except ManagementError as exc:
        if any(getattr(f, "code", "") == "STATUS_UNKNOWN" for f in exc.fields) and not allow_unknown:
            data = _set_state(chat_id, "oa_wb_unknown", {"nav": nav, "key": key})
            ui.edit(chat_id, message_id, "⚠️ 今日签到状态未知，不是未签到。建议先返回查询核对。\n如已通过官方页面核对，可明确接受重复提交风险后继续；已有结果未知的请求仍只对账，不重放。",
                reply_markup=ui.inline_kb([[ui.btn("已核对，接受风险后继续", _PREFIX + "unknown:" + data["nonce"])],
                                          [ui.btn("❌ 取消", _PREFIX + "cancel:" + data["nonce"])]]))
        else:
            ui.edit(chat_id, message_id, _error(exc), reply_markup=ui.inline_kb([[ui.btn("◀ 返回活动", _cb("activity", nav))]]))
        return
    prior = plan.get("prior_result") or {}
    if prior.get("status") in {"succeeded", "already_done", "pending", "unknown"} or (plan.get("observed_status") or {}).get("today_checked_in") is True:
        text = result_text(prior) if prior else "ℹ️ 今日已签到，未重复提交。"
        ui.edit(chat_id, message_id, text, reply_markup=ui.inline_kb([[ui.btn("🔎 查询核对", _cb("refresh_activity", nav))], [_back(nav)]]))
        return
    text = ("🎁 <b>确认签到</b>" if cn else "🎁 <b>确认申请试用额度</b>") + "\n账户: " + ui.escape_html(str(account.get("label") or account.get("nickname") or "WorkBuddy"))
    text += "\n日期: " + ui.escape_html(plan["business_date"]) + "\n请求发出后不能撤销；奖励只以上游明确结果为准。"
    if retry:
        text += "\n这是对上次明确拒绝的一次新尝试。"
    _confirm(chat_id, message_id, text=text, data={"kind": "action", "key": key, "nav": nav, "plan_token": plan["plan_token"]})


def _login_start(chat_id, message_id, *, realm="cn"):
    discard_state(chat_id)
    try:
        flow = oauth_control.start_login_flow(telegram_context(chat_id), OAuthProvider.WORKBUDDY,
            realm=realm, client_profile="ide" if realm == "global" else "cli")
    except Exception as exc:
        ui.edit(chat_id, message_id, _error(exc), reply_markup=ui.inline_kb([[ui.btn("◀ 返回新增", "oa:add")]]))
        return
    data = _set_state(chat_id, "oa_wb_login", {"flow_id": flow.flow_id, "flow_secret": flow.flow_secret,
        "realm": realm, "auth_url": flow.auth_url, "expires_at": flow.expires_at})
    region = "国际区" if realm == "global" else "中国区"
    login_hint = "使用 Google / GitHub 在官方页面登录并授权。\n" if realm == "global" else ""
    ui.edit(chat_id, message_id, f"🌐 <b>WorkBuddy {region}登录</b>\n{login_hint}在浏览器登录并授权后，后台每 5 秒自动检查，成功即保存并显示身份。同一身份重新登录只更新授权，保留原有设置。\n有效期 5 分钟。",
        reply_markup=ui.inline_kb([[{"text": "🌐 打开授权页面", "url": flow.auth_url}],
            [ui.btn("🔎 检查登录", _PREFIX + "poll:" + data["nonce"])],
            [ui.btn("❌ 取消", _PREFIX + "cancel:" + data["nonce"])]]))
    _login_poll(chat_id, message_id, data)


def _start_login_worker(worker):
    thread = threading.Thread(target=worker, daemon=True, name="workbuddy-login-ui")
    thread.start()
    return thread


def _login_feedback(data, poll=None, error=None):
    status = poll.status if poll is not None else "pending"
    if status == "completed":
        preview = poll.account_preview or {}
        text = "✅ 授权已保存。" if poll.save_status != "replaced" else "✅ 授权已更新，原有账户设置已保留。"
        text += "\n" + ui.escape_html(str(preview.get("label") or preview.get("nickname") or preview.get("uid") or "WorkBuddy"))
        region = "国际区" if preview.get("realm", data.get("realm")) == "global" else "中国区"
        text += f"\n区域: {region} · " + ("企业" if preview.get("enterprise_id") else "个人")
        text += "\n模型和额度以各自最新同步状态为准，后续同步失败不会回滚已保存授权。"
        return text, ui.inline_kb([[ui.btn("查看账户", "oa:view:" + ui.register_code(poll.account_id) + ":1")],
                                  [ui.btn("◀ 返回列表", "menu:oauth")]])
    if status in {"cancelled", "expired"}:
        return "ℹ️ 登录流程已取消或过期，请重新开始。", ui.inline_kb([[ui.btn("◀ 返回新增", "oa:add")]])
    text = "⌛ 等待浏览器授权，后台自动检查中；成功后直接保存。"
    if status == "identity_pending":
        text = "⌛ 已取得授权，正在自动确认账户身份；仅继续获取身份，不重复获取 Token。"
    if error is not None:
        text = _error(error) + "\n后台将继续检查，也可手动检查或取消。"
    rows = [[{"text": "🌐 打开授权页面", "url": data["auth_url"]}]] if data.get("auth_url") else []
    rows += [[ui.btn("🔎 检查登录", _PREFIX + "poll:" + data["nonce"])],
             [ui.btn("❌ 取消", _PREFIX + "cancel:" + data["nonce"])]]
    return text, ui.inline_kb(rows)


def _run_login_job(chat_id, job):
    data, owner = job["data"], job["owner"]
    last_render = None
    try:
        while not job["stop"].is_set() and _state(chat_id, data["nonce"], "oa_wb_login") is data:
            job["wake"].clear()
            poll, error = None, None
            try:
                poll = owner.poll_login_flow(telegram_context(chat_id), data["flow_id"], data["flow_secret"])
            except Exception as exc:
                error = exc
            terminal = poll is not None and poll.status in {"completed", "cancelled", "expired"}
            expired = not terminal and time.monotonic() >= job["deadline"]
            text, kb = _login_feedback(data, poll, error)
            if expired:
                text, kb = "ℹ️ 登录等待已到期，请重新开始。", ui.inline_kb([[ui.btn("◀ 返回新增", "oa:add")]])
            with _login_jobs_lock:
                message_id, token = job["message_id"], job["view_token"]
            render_key = (message_id, token, text)

            def render():
                if not job["stop"].is_set() and _state(chat_id, data["nonce"], "oa_wb_login") is data:
                    ui.edit(chat_id, message_id, text, reply_markup=kb)

            if render_key != last_render:
                try:
                    if menu_cache.run_if_current(chat_id, message_id, token, render):
                        last_render = render_key
                except Exception:
                    pass  # A Telegram delivery failure must not abort authorization.
            if terminal or expired:
                states.pop_state_if_current(chat_id, data)
                if expired:
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
        if job and job["data"] is data and not job["stop"].is_set():
            job.update(message_id=message_id, view_token=token)
            job["wake"].set()
            return
        if job:
            job["stop"].set()
            job["wake"].set()
        expires = data.get("expires_at")
        remaining = max(0, min(300, (expires - datetime.now(timezone.utc)).total_seconds())) if expires else 300
        job = {"data": data, "owner": oauth_control, "message_id": message_id, "view_token": token,
               "deadline": time.monotonic() + remaining, "wake": threading.Event(), "stop": threading.Event()}
        _login_jobs[chat_id] = job
    _start_login_worker(lambda: _run_login_job(chat_id, job))


def reject_removed_import(chat_id):
    """Retire old messages/input states without reading credentials or cancelling a new login."""
    state = states.get_state(chat_id)
    if state and state["action"] in {"oa_wb_import", "oa_wb_import_preview"}:
        states.pop_state(chat_id)
    ui.send(chat_id, "ℹ️ WorkBuddy JSON 导入已移除，中国区和国际区均请使用网页登录；已有账户不受影响。",
        reply_markup=ui.inline_kb([
            [ui.provider_button("WorkBuddy 中国区登录", "oa:wb:login", "workbuddy")],
            [ui.provider_button("WorkBuddy 国际区登录", "oa:wb:login:global", "workbuddy")],
            [ui.btn("◀ 返回新增账户", "oa:add")]]))


def handle_callback(chat_id, message_id, cb_id, callback):
    if not callback.startswith(_PREFIX):
        return False
    parts = callback[len(_PREFIX):].split(":")
    kind = parts[0]
    ui.answer_cb(cb_id, "查询中…") if kind in {"refresh_credits", "refresh_activity"} else ui.answer_cb(cb_id)
    if kind == "noop":
        return True
    if kind == "login":
        realm = "cn" if len(parts) == 1 else parts[1] if len(parts) == 2 else ""
        if realm in {"cn", "global"}:
            _login_start(chat_id, message_id, realm=realm)
        return True
    if kind == "import" or kind.startswith("import_"):
        reject_removed_import(chat_id)
        return True
    state_kinds = {"poll", "save", "replace", "cancel", "confirm", "terms", "unknown"}
    if kind in state_kinds:
        nonce = parts[1] if len(parts) == 2 else ""
        data = _state(chat_id, nonce, "oa_wb_login", "oa_wb_confirm", "oa_wb_terms", "oa_wb_unknown")
        if not data:
            ui.send_result(chat_id, "⚠️ 按钮已过期，请返回原页面重新开始。", **_NAV)
            return True
        state = states.get_state(chat_id)
        action = state["action"]
        if kind == "cancel":
            nav = data.get("nav")
            discard_state(chat_id)
            if nav and _resolve(nav):
                _show_activity(chat_id, message_id, _resolve(nav), nav)
            else:
                _parent().on_add_menu(chat_id, message_id, "")
        elif action == "oa_wb_login" and kind in {"poll", "save", "replace"}:
            # Old save/replace buttons now wake the same automatic flow.
            _login_poll(chat_id, message_id, data)
        elif kind in {"terms", "unknown"} and action == ("oa_wb_terms" if kind == "terms" else "oa_wb_unknown"):
            states.pop_state(chat_id)
            _plan_action(chat_id, message_id, data["key"], data["nav"], allow_unknown=kind == "unknown", trial_confirmed=kind == "terms")
        elif kind == "confirm" and action == "oa_wb_confirm":
            states.pop_state(chat_id)
            try:
                if data["kind"] == "auto":
                    oauth_control.update_workbuddy_settings(telegram_context(chat_id), data["key"], auto_checkin=data["enabled"], expected_revision=data["revision"])
                    text = "✅ 自动签到设置已保存。"
                else:
                    result = oauth_control.execute_workbuddy_action_now(telegram_context(chat_id), data["key"], data["plan_token"])
                    text = result_text(result)
            except Exception as exc:
                text = _error(exc)
            ui.edit(chat_id, message_id, text, reply_markup=ui.inline_kb([[ui.btn("◀ 返回活动", _cb("activity", data["nav"]))], [_back(data["nav"])]]))
        return True
    if len(parts) != 5:
        return True
    try:
        nav = (parts[1], max(1, int(parts[2])), _parent()._normalize_filter(parts[3]))
        page = max(1, int(parts[4]))
    except ValueError:
        return True
    key = _resolve(nav)
    if not key:
        ui.send_result(chat_id, "⚠️ 账户已不存在。", **_NAV)
        return True
    if kind in {"refresh_credits", "refresh_activity"}:
        def render_query(feedback):
            if kind == "refresh_activity":
                return render_activity(key, nav, chat_id=chat_id, query_feedback=feedback)
            text, kb = render_credits(key, nav, page)
            return (feedback + "\n\n" + text if text else None), kb
        start_status_query(chat_id, message_id, key, render_query)
        return True
    if kind in {"credits", "activity", "records"}:
        if kind == "credits":
            text, kb = render_credits(key, nav, page)
        elif kind == "records":
            text, kb = render_records(key, nav, page, chat_id=chat_id)
        else:
            text, kb = render_activity(key, nav, chat_id=chat_id)
        ui.edit(chat_id, message_id, text, reply_markup=kb)
    elif kind == "plan":
        _plan_action(chat_id, message_id, key, nav)
    elif kind == "auto":
        value = oauth_control.get_account(telegram_context(chat_id), key)
        enabled = not (oauth_control.account_snapshot(key) or {}).get("workbuddy_auto_checkin", False)
        _confirm(chat_id, message_id, text=("▶️ 确认开启自动签到？\n每天北京时间 09:05 主轮、21:05 补漏，仅中国区；先查状态，已签不重复提交，未知仅核对。停用或认证失效时暂停，不自动申请试用。禁 Token 刷新不影响签到。" if enabled else "⏸ 确认关闭自动签到？\n不会撤销已经发出的请求。"),
            data={"kind": "auto", "key": key, "nav": nav, "enabled": enabled, "revision": value.account.revision})
    return True


def _show_activity(chat_id, message_id, key, nav):
    text, kb = render_activity(key, nav, chat_id=chat_id)
    ui.edit(chat_id, message_id, text, reply_markup=kb)
