"""Claude-only additions to the existing OAuth detail/confirmation menu."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .. import error_reporting, ui
from ...management_control.oauth.menu_bridge import control, telegram_context
from ...oauth import claude_reset as cr

WINDOW_LABELS = {
    "five_hour": "5 小时",
    "seven_day": "7 天",
    "seven_day_overage_included": "Fable / 周子限额 7 天",
    "seven_day_opus": "Opus 7 天",
    "seven_day_sonnet": "Sonnet 7 天",
    "seven_day_fable": "Fable 7 天",
    "seven_day_cowork": "Cowork 7 天",
    "seven_day_omelette": "Omelette 7 天",
    "seven_day_oauth_apps": "7 天（OAuth 应用）",
}

NEW_321_WINDOWS = frozenset({
    "seven_day_overage_included",
    "seven_day_cowork",
    "seven_day_omelette",
})

GRANT_KIND_LABELS = {
    "fable": "Fable 重置卡 · 退回周额度",
    "session": "5 小时卡",
    "weekly": "周额度卡",
    "full": "全量重置卡",
}

BUTTON_KIND_LABELS = {
    "fable": "Fable 重置卡·退回周额度",
    "session": "5小时重置卡",
    "weekly": "周额度重置卡",
    "full": "全量周额度重置卡",
}

REASON_LABELS = {
    "status_unknown": "官方未返回这项功能的状态",
    "ineligible": "官方判定当前没有资格",
    "surface": "当前入口不支持",
    "control": "账号不在这项功能的试验组里",
    "already_used": "本周次数已用完",
    "cooldown": "还在冷却期内",
    "no_grant": "没有可用的重置卡",
    "not_next_grant": "不是官方指定的下一张卡",
    "unknown_grant": "找不到官方指定的重置卡",
    "paused": "这张卡已被暂停",
    "expired": "重置卡已过期",
    "not_started": "重置卡尚未生效",
    "not_limited": "额度还没撞限，这张卡要撞限后才能用",
    "blocking": "有其他限制阻止使用",
    "fable_no_weekly_usage": "本周还没有可退回的 Fable 用量",
    "blocked_five_hour": "当前正受 5 小时限额阻止，需等其恢复后才能用",
    "blocked_seven_day": "当前正受 7 天周限额阻止",
    "blocked_opus": "当前正受 Opus 周限额阻止",
    "blocked_sonnet": "当前正受 Sonnet 周限额阻止",
    "blocked_other": "受此卡不负责清除的其它限额阻止",
    "cannot_clear": "这张卡无法清除当前触发的限额窗口",
    "needs_limit": "需要达到此卡对应的限额后才能使用",
    "query_cooldown": "查询处于限流冷却期，请稍后再试",
    "status_refresh_required": "状态需要重新刷新确认",
    "unavailable": "官方暂不可用",
    "organization_missing": "缺少组织信息，请刷新用量后重试",
    "organization_missing_or_changed": "组织信息缺失或已变化",
    "operation_in_progress": "上一次重置还在处理中",
    "allowance_changed_not_attributed": "额度有变化，但无法确认是这次重置造成的",
    "pending_requires_reconciliation": "上次结果未确认，需要先查询核对",
    "reset_unconfirmed": "官方没有确认结果",
    "stamp_indeterminate": "无法确认是否已执行",
    "auth_error": "账号认证已失效",
    "user": "账号被手动停用",
    "quota": "账号处于额度暂停",
    "mock_mode": "当前为模拟模式，未发出请求",
    "unknown_program": "未知的重置类型",
}

NEW_321_REASONS = frozenset({
    "fable_no_weekly_usage",
    "blocked_five_hour",
    "blocked_seven_day",
    "blocked_opus",
    "blocked_sonnet",
    "blocked_other",
    "cannot_clear",
    "needs_limit",
    "query_cooldown",
    "status_refresh_required",
})


def _esc(value):
    return ui.escape_html(str(value if value is not None else "未知"))


def _fmt_reason(value) -> str:
    if value is None or value == "":
        return "—"
    code = str(value)
    zh = REASON_LABELS.get(code)
    if zh:
        return f"{zh}（{code}）"
    if code.startswith("http_") and code[5:].isdigit():
        return f"官方返回 HTTP {code[5:]}（{code}）"
    return code


def _fmt_windows(items, *, gives_back_weekly: bool = False) -> str:
    values = [WINDOW_LABELS.get(str(x), str(x)) for x in (items or []) if x is not None and str(x)]
    base = "、".join(values) if values else "—"
    if gives_back_weekly:
        return "清 Fable 用量并退回 7 天周池" if base == "—" else f"{base}（清 Fable 用量并退回 7 天周池）"
    return base


def _is_v321_cedar(cedar: dict | None) -> bool:
    if not isinstance(cedar, dict):
        return False
    grants = [g for g in (cedar.get("grants") or []) if isinstance(g, dict)]
    if len(grants) > 1:
        return True
    if len(grants) == 1 and cedar.get("next_grant_id") is not None and grants[0].get("id") != cedar.get("next_grant_id"):
        return True
    if any(
        g.get("type") is not None
        or g.get("gives_back_weekly") is not None
        or g.get("unavailable_reason") is not None
        for g in grants
    ):
        return True
    if any(w in NEW_321_WINDOWS for w in (cedar.get("exhausted") or [])):
        return True
    if any(
        w in NEW_321_WINDOWS
        for g in grants
        for w in list(g.get("clears") or []) + list(g.get("blocking") or [])
    ):
        return True
    return False


def _missing_status(usage, program):
    observation = ((usage.get("claude_reset_queries") or {}).get(program) or {}) if isinstance(usage, dict) else {}
    state = observation.get("state")
    if state == "not_provided":
        return "已查询，官方未返回此功能状态（不等于 0 张或无资格）"
    if state == "invalid_response":
        return "官方返回格式无法识别，请刷新官方重置状态"
    if state == "error":
        code = observation.get("http_status")
        reason = "认证或权限被拒绝" if code in (401, 403) else "请求限流" if code == 429 else "请求失败"
        suffix = f"，HTTP {code}" if code else "，请检查网络后重试"
        return f"查询失败：{reason}{suffix}"
    return "尚未查询，请点击“官方额度重置/状态”或刷新额度"


def block(usage):
    cedar = usage.get("cedar_ember") if isinstance(usage, dict) else None
    juniper = usage.get("juniper_tide") if isinstance(usage, dict) else None
    lines = ["♻️ <b>Claude 官方额度重置</b>"]
    if not isinstance(cedar, dict):
        lines.append("周额度重置卡：" + _missing_status(usage, "cedar_ember"))
    else:
        grants = [g for g in cedar.get("grants", []) if isinstance(g, dict)]
        v321 = _is_v321_cedar(cedar)
        inelig = cedar.get("ineligible_reason")
        inelig_text = _fmt_reason(inelig) if (v321 or inelig in NEW_321_REASONS) else (inelig or "—")
        exhausted_text = _fmt_windows(cedar.get("exhausted")) if v321 else (", ".join(cedar.get("exhausted") or []) or "—")
        lines.append(f"周额度重置卡：{len(grants)} 张 · eligible=<code>{_esc(cedar.get('eligible'))}</code>")
        lines.append(f"下张卡：<code>{_esc(cedar.get('next_grant_id'))}</code> · 原因：{_esc(inelig_text)}")
        lines.append(f"已撞限={_esc(cedar.get('at_limit'))} · 耗尽窗口：{_esc(exhausted_text)}")
        status_for_card = {"cedar_ember": cedar}
        for grant in grants:
            card_reason = cr.grant_eligibility(status_for_card, grant)
            if v321:
                kind = cr.grant_kind(grant)
                kind_label = GRANT_KIND_LABELS.get(kind, GRANT_KIND_LABELS["full"])
                gid = grant.get("id")
                label = grant.get("label") or gid
                id_part = f" (<code>{_esc(gid)}</code>)" if grant.get("label") and gid and grant.get("label") != gid else ""
                detail = (
                    f"• {_esc(label)}{id_part} · <b>{_esc(kind_label)}</b>：剩余 <code>{_esc(grant.get('resets_left'))}/{_esc(grant.get('resets_total'))}</code>；"
                    f"可用={_esc(grant.get('usable_now'))} / 暂停={_esc(grant.get('paused'))}\n"
                    f"  有效期 {_esc(grant.get('starts_at'))} → {_esc(grant.get('ends_at'))}\n"
                    f"  重置 {_esc(_fmt_windows(grant.get('clears'), gives_back_weekly=bool(grant.get('gives_back_weekly'))))}；"
                    f"阻止 {_esc(_fmt_windows(grant.get('blocking')))}\n"
                    f"  {'须撞限才能用' if grant.get('use_requires_limit', True) else '随时可用模式'}；窗口用量(%)：{_esc(grant.get('percent_used') or '—')}"
                )
            else:
                detail = (
                    f"• {_esc(grant.get('label') or grant.get('id'))}：剩余 <code>{_esc(grant.get('resets_left'))}/{_esc(grant.get('resets_total'))}</code>；"
                    f"可用={_esc(grant.get('usable_now'))} / 暂停={_esc(grant.get('paused'))}\n"
                    f"  有效期 {_esc(grant.get('starts_at'))} → {_esc(grant.get('ends_at'))}\n"
                    f"  重置 {_esc(', '.join(grant.get('clears') or []))}；阻止 {_esc(', '.join(grant.get('blocking') or []) or '—')}\n"
                    f"  {'须撞限才能用' if grant.get('use_requires_limit', True) else '随时可用模式'}；窗口用量(%)：{_esc(grant.get('percent_used') or '—')}"
                )
            if card_reason:
                detail += f"\n  不可用原因：{_esc(_fmt_reason(card_reason))}"
            lines.append(detail)
        lines.append(f"冷却至：{_esc(cedar.get('cooldown_until') or '无')} · 周重置日不变：{_esc(cedar.get('weekly_resets_at'))}")
    if not isinstance(juniper, dict):
        lines.append("5h重置：" + _missing_status(usage, "juniper_tide"))
    else:
        j_inelig = juniper.get("ineligible_reason")
        j_reason = _fmt_reason(j_inelig) if j_inelig in NEW_321_REASONS else (j_inelig or "—")
        lines.append(f"5h重置：可用={_esc(juniper.get('available'))} · eligible={_esc(juniper.get('eligible'))} · 组={_esc(juniper.get('arm'))}\n"
                     f"每周 {_esc(juniper.get('resets_per_week', 1))} 次，<b>消耗周额度份额</b>；原因：{_esc(j_reason)}\n"
                     f"下次可用：{_esc(juniper.get('next_available_at'))} · 周重置日：{_esc(juniper.get('weekly_resets_at'))}")
    for program, observation in (usage.get("claude_reset_queries") or {}).items() if isinstance(usage, dict) else []:
        stamp, retry = observation.get("fetched_at"), observation.get("retry_at")
        def when(value):
            return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat()
        if stamp:
            lines.append(f"{_esc(program)} 数据时间：{_esc(when(stamp))}")
        if observation.get("state") == "error":
            lines.append("查询失败，保留上次成功数据；不代表零额度或无资格。")
        if retry:
            lines.append(f"查询限流，冷却至 {_esc(when(retry))}；到期不保证上游已解除限流。")
    return "\n".join(lines)


def cached_block(row):
    try:
        usage = json.loads((row or {}).get("raw_data") or "{}")
    except (ValueError, TypeError):
        usage = {}
    return block(usage)


def _payload(account_id, token, page, filter_key):
    from . import oauth_menu as menu
    short = ui.register_code(json.dumps({"account_id": account_id, "plan_token": token}))
    return menu._callback_payload(short, page, filter_key)


def _cedar_button_text(grant: dict, *, duplicate_label: bool = False) -> str:
    kind = cr.grant_kind(grant)
    prefix = BUTTON_KIND_LABELS.get(kind, "周额度重置卡")
    gid = str(grant.get("id") or "")
    label = str(grant.get("label") or gid)
    tag = f"{label}·{gid}" if duplicate_label and gid and label != gid else label
    return f"我已理解：{prefix}（{tag}），进入最终确认"


def ask(chat_id, message_id, cb_id, short, page=1, filter_key="all"):
    from . import oauth_menu as menu
    account_id = menu._account_key_from_short(short)
    if not account_id:
        ui.answer_cb(cb_id, "账号已失效")
        return
    ui.answer_cb(cb_id, "读取官方重置状态（不消耗）")
    ctx = telegram_context(chat_id)
    rows, status, reasons = [], {}, []
    v321_mode = False
    for program, label in (("cedar_ember", "周额度重置卡"), ("juniper_tide", "5h重置（消耗周额度）")):
        try:
            plan = control.plan_claude_reset(ctx, account_id, program)
            observation = dict(plan.get("status") or {})
            queries = observation.pop("claude_reset_queries", {})
            status.update(observation)
            status.setdefault("claude_reset_queries", {}).update(queries)
            cedar = status.get("cedar_ember") if program == "cedar_ember" else None
            if program == "cedar_ember" and _is_v321_cedar(cedar):
                v321_mode = True
            if plan.get("available"):
                if program == "cedar_ember":
                    cedar_grants = [
                        g for g in ((cedar or {}).get("grants") or [])
                        if isinstance(g, dict) and isinstance(g.get("id"), str)
                        and cr.GRANT_ID_RE.fullmatch(g["id"])
                        and cr.grant_eligibility(status, g) is None
                    ]
                    if v321_mode and cedar_grants:
                        primary_id = plan.get("grant_id")
                        label_counts: dict[str, int] = {}
                        for g in cedar_grants:
                            g_label = str(g.get("label") or g["id"])
                            label_counts[g_label] = label_counts.get(g_label, 0) + 1
                        plans_by_grant = {primary_id: plan} if primary_id else {}
                        for g in cedar_grants:
                            gid = g["id"]
                            if gid not in plans_by_grant:
                                extra = control.plan_claude_reset(ctx, account_id, program, grant_id=gid)
                                if extra.get("available"):
                                    plans_by_grant[gid] = extra
                        for g in cedar_grants:
                            gid = g["id"]
                            bound = plans_by_grant.get(gid)
                            if not bound:
                                continue
                            g_label = str(g.get("label") or gid)
                            payload = _payload(account_id, bound["plan_token"], page, filter_key)
                            btn_text = _cedar_button_text(g, duplicate_label=label_counts.get(g_label, 0) > 1)
                            rows.append([ui.btn(btn_text, f"oa:claude_reset_confirm:{payload}")])
                    else:
                        payload = _payload(account_id, plan["plan_token"], page, filter_key)
                        rows.append([ui.btn(f"我已理解：{label}，进入最终确认", f"oa:claude_reset_confirm:{payload}")])
                else:
                    payload = _payload(account_id, plan["plan_token"], page, filter_key)
                    rows.append([ui.btn(f"我已理解：{label}，进入最终确认", f"oa:claude_reset_confirm:{payload}")])
            else:
                r_code = plan.get("reason")
                r_text = _fmt_reason(r_code) if (v321_mode or r_code in NEW_321_REASONS) else r_code
                reasons.append(f"{label}不可执行：{_esc(r_text)}")
        except Exception as exc:
            status.setdefault("claude_reset_queries", {})[program] = {"state": "error"}
            reasons.append(error_reporting.report(exc, operation="Claude " + label + "查询") + "\n未发送消费请求。")
    cancel = menu._callback_payload(ui.register_code(account_id), page, filter_key)
    rows.append([ui.btn("🔄 刷新官方重置状态", f"oa:claude_reset_ask:{cancel}")])
    rows.append([ui.btn("❌ 取消，返回账号", f"oa:view:{cancel}")])
    cedar_bullet = (
        "• 重置卡按卡种独立选卡（支持 Fable 退回周额度卡、5小时卡、周额度卡、全量卡），<b>不改变每周自然重置日期</b>。\n"
        if v321_mode else
        "• 周卡清指定窗口用量，<b>不改变每周自然重置日期</b>。只使用服务端指定的 next_grant。\n"
    )
    text = (block(status) + "\n\n<b>官方重置说明（本页不会消费）</b>\n"
            + cedar_bullet +
            "• 5h重置会<b>消耗周额度份额</b>，并受每周次数限制，不是免费补充周额度。\n"
            "• 下一页还需最终确认；只有最终确认才发POST，不可撤销。\n"
            "• 成功/已使用不等于立即可用；会刷新真实usage，仍超限、失败或新限制到达时保留本地限制。\n"
            "• 与“清本地配额禁用”不同；不会解除手动停用或授权错误。\n"
            "• 周卡未决600秒内再次确认复用幂等ID；5h结果不确定时只查状态，不自动重复消费。\n" + "\n".join(reasons))
    ui.edit(chat_id, message_id, ui.truncate(text), reply_markup=ui.inline_kb(rows))


def _decode(short):
    value = json.loads(ui.resolve_code(short) or "null")
    if not isinstance(value, dict) or not value.get("account_id") or not value.get("plan_token"):
        raise ValueError("invalid_confirmation")
    return value["account_id"], value["plan_token"]


def confirm(chat_id, message_id, cb_id, short, page=1, filter_key="all"):
    from . import oauth_menu as menu
    try:
        account_id, token = _decode(short)
        final = control.confirm_claude_reset(telegram_context(chat_id), account_id, token)
    except Exception:
        ui.answer_cb(cb_id, "确认已失效或账号变化，请重新进入", show_alert=True)
        return
    ui.answer_cb(cb_id, "请最终确认")
    cedar = final["program"] == "cedar_ember"
    if cedar and final.get("v321_card"):
        kind = final.get("grant_kind") or ("fable" if final.get("gives_back_weekly") else "weekly")
        kind_label = GRANT_KIND_LABELS.get(kind, "周额度重置卡")
        grant_id = final.get("grant_id")
        grant_label = final.get("grant_label")
        card_ref = (
            f"{_esc(grant_label)}（<code>{_esc(grant_id)}</code>）"
            if grant_label and grant_label != grant_id
            else f"<code>{_esc(grant_id)}</code>"
        )
        if final.get("gives_back_weekly") or kind == "fable":
            action_line = (
                f"使用 <b>{_esc(kind_label)}</b> {card_ref}：清除 Fable 周子限额，"
                "并将本周 Fable 消耗的周额度退回 7 天周池；消耗一次卡额度，周自然重置日期不变。"
            )
        else:
            action_line = f"使用 <b>{_esc(kind_label)}</b> {card_ref}，消耗一次卡额度；周自然重置日期不变。"
    elif cedar:
        action_line = f"使用周重置卡 <code>{_esc(final.get('grant_id'))}</code>，消耗一次卡额度；周自然重置日期不变。"
    else:
        action_line = "重置5h会话额度，<b>消耗周额度份额</b>，受每周次数限制；不是补充周额度。"
    text = ("🚨 <b>最终确认：Claude 官方额度重置</b>\n\n"
            f"账号：<code>{_esc(account_id)}</code>\n" +
            action_line +
            "\n\n此操作不可撤销。成功后仅按新usage和模型scope判断可用性；不会强制启用。\n"
            "重复按钮不会再次执行；超时不猜测成功，不自动补发消费请求。")
    payload = _payload(account_id, final["plan_token"], page, filter_key)
    back = menu._callback_payload(ui.register_code(account_id), page, filter_key)
    ui.edit(chat_id, message_id, text, reply_markup=ui.inline_kb([
        [ui.btn("🚨 最终确认：执行官方重置", f"oa:claude_reset_execute:{payload}")],
        [ui.btn("❌ 取消", f"oa:view:{back}")],
    ]))


def execute(chat_id, message_id, cb_id, short, page=1, filter_key="all"):
    from . import oauth_menu as menu
    try:
        account_id, token = _decode(short)
        ui.answer_cb(cb_id, "正在执行官方重置...")
        result = control.execute_claude_reset(telegram_context(chat_id), account_id, token)
    except Exception:
        ui.send(chat_id, "⚠️ 确认已用/已失效，或请求未完成；不会自动重发。请重新打开官方重置状态核对。")
        return
    fields = [f"结果：<code>{_esc(result.get('result'))}</code>"]
    for name, label in (("reason", "服务端/安全门禁原因"), ("resets_left", "卡剩余次数"),
                        ("cleared", "实际清除窗口"), ("cooldown_until", "冷却至"),
                        ("next_available_at", "下次可用"), ("weekly_resets_at", "周重置日期（不改变）")):
        if name in result:
            val = result[name]
            if name == "reason" and val in NEW_321_REASONS:
                fields.append(f"{label}：{_esc(_fmt_reason(val))}")
            elif name == "cleared" and isinstance(val, list) and any(w in NEW_321_WINDOWS for w in val):
                fields.append(f"{label}：{_esc(_fmt_windows(val))} <code>{_esc(val)}</code>")
            else:
                fields.append(f"{label}：<code>{_esc(val)}</code>")
    action = (result.get("quota_action") or {}).get("action")
    if action:
        fields.append(f"本地额度评估：<code>{_esc(action)}</code>")
    fields.append("<i>返回reset/already_used不保证可用；未知/仍超限/刷新失败会保留限制。cleared中的专属窗口仅如实展示，不冒充已扩展路由策略。</i>")
    menu._edit_cached_detail(chat_id, message_id, account_id, page, filter_key,
                            prefix="♻️ <b>Claude 官方重置反馈</b>\n" + "\n".join(fields) + "\n\n",
                            refresh_quota=False)
