"""Claude-only additions to the existing OAuth detail/confirmation menu."""
from __future__ import annotations

import json

from .. import ui
from ...management_control.oauth.menu_bridge import control, telegram_context


def _esc(value):
    return ui.escape_html(str(value if value is not None else "未知"))


def block(usage):
    cedar = usage.get("cedar_ember") if isinstance(usage, dict) else None
    juniper = usage.get("juniper_tide") if isinstance(usage, dict) else None
    lines = ["♻️ <b>Claude 官方额度重置</b>"]
    if not isinstance(cedar, dict):
        lines.append("周额度重置卡：状态未知（刷新官方重置状态查询）")
    else:
        grants = [g for g in cedar.get("grants", []) if isinstance(g, dict)]
        lines.append(f"周额度重置卡：{len(grants)} 张 · eligible=<code>{_esc(cedar.get('eligible'))}</code>")
        lines.append(f"下张卡：<code>{_esc(cedar.get('next_grant_id'))}</code> · 原因：{_esc(cedar.get('ineligible_reason') or '—')}")
        lines.append(f"已撞限={_esc(cedar.get('at_limit'))} · 耗尽窗口：{_esc(', '.join(cedar.get('exhausted') or []) or '—')}")
        for grant in grants:
            lines.append(f"• {_esc(grant.get('label') or grant.get('id'))}：剩余 <code>{_esc(grant.get('resets_left'))}/{_esc(grant.get('resets_total'))}</code>；"
                         f"可用={_esc(grant.get('usable_now'))} / 暂停={_esc(grant.get('paused'))}\n"
                         f"  有效期 {_esc(grant.get('starts_at'))} → {_esc(grant.get('ends_at'))}\n"
                         f"  重置 {_esc(', '.join(grant.get('clears') or []))}；阻止 {_esc(', '.join(grant.get('blocking') or []) or '—')}\n"
                         f"  {'须撞限才能用' if grant.get('use_requires_limit', True) else '随时可用模式'}；窗口用量(%)：{_esc(grant.get('percent_used') or '—')}")
        lines.append(f"冷却至：{_esc(cedar.get('cooldown_until') or '无')} · 周重置日不变：{_esc(cedar.get('weekly_resets_at'))}")
    if not isinstance(juniper, dict):
        lines.append("5h重置：状态未知")
    else:
        lines.append(f"5h重置：可用={_esc(juniper.get('available'))} · eligible={_esc(juniper.get('eligible'))} · 组={_esc(juniper.get('arm'))}\n"
                     f"每周 {_esc(juniper.get('resets_per_week', 1))} 次，<b>消耗周额度份额</b>；原因：{_esc(juniper.get('ineligible_reason') or '—')}\n"
                     f"下次可用：{_esc(juniper.get('next_available_at'))} · 周重置日：{_esc(juniper.get('weekly_resets_at'))}")
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


def ask(chat_id, message_id, cb_id, short, page=1, filter_key="all"):
    from . import oauth_menu as menu
    account_id = menu._account_key_from_short(short)
    if not account_id:
        ui.answer_cb(cb_id, "账号已失效")
        return
    ui.answer_cb(cb_id, "读取官方重置状态（不消耗）")
    rows, status, reasons = [], {}, []
    try:
        for program, label in (("cedar_ember", "周额度重置卡"), ("juniper_tide", "5h重置（消耗周额度）")):
            plan = control.plan_claude_reset(telegram_context(chat_id), account_id, program)
            status.update(plan.get("status") or {})
            if plan.get("available"):
                payload = _payload(account_id, plan["plan_token"], page, filter_key)
                rows.append([ui.btn(f"我已理解：{label}，进入最终确认", f"oa:claude_reset_confirm:{payload}")])
            else:
                reasons.append(f"{label}不可执行：{_esc(plan.get('reason'))}")
    except Exception:
        ui.send(chat_id, "⚠️ Claude 官方重置状态读取失败，未发送消费请求。请刷新后重试。")
        return
    cancel = menu._callback_payload(ui.register_code(account_id), page, filter_key)
    rows.append([ui.btn("🔄 刷新官方重置状态", f"oa:claude_reset_ask:{cancel}")])
    rows.append([ui.btn("❌ 取消，返回账号", f"oa:view:{cancel}")])
    text = (block(status) + "\n\n<b>官方重置说明（本页不会消费）</b>\n"
            "• 周卡清指定窗口用量，<b>不改变每周自然重置日期</b>。只使用服务端指定的 next_grant。\n"
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
    text = ("🚨 <b>最终确认：Claude 官方额度重置</b>\n\n"
            f"账号：<code>{_esc(account_id)}</code>\n" +
            (f"使用周重置卡 <code>{_esc(final.get('grant_id'))}</code>，消耗一次卡额度；周自然重置日期不变。" if cedar else
             "重置5h会话额度，<b>消耗周额度份额</b>，受每周次数限制；不是补充周额度。") +
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
            fields.append(f"{label}：<code>{_esc(result[name])}</code>")
    action = (result.get("quota_action") or {}).get("action")
    if action:
        fields.append(f"本地额度评估：<code>{_esc(action)}</code>")
    fields.append("<i>返回reset/already_used不保证可用；未知/仍超限/刷新失败会保留限制。cleared中的专属窗口仅如实展示，不冒充已扩展路由策略。</i>")
    menu._edit_cached_detail(chat_id, message_id, account_id, page, filter_key,
                            prefix="♻️ <b>Claude 官方重置反馈</b>\n" + "\n".join(fields) + "\n\n",
                            refresh_quota=False)
