"""Provider registration: identity, typed facts and capabilities; never sends or calls upstream."""
from dataclasses import dataclass
from typing import Callable
import os


@dataclass(frozen=True)
class Provider:
    name: str
    scopes: tuple[tuple[str, str], ...] = ()
    identity_fields: tuple[str, ...] = ("email", "nickname", "subject", "uid")
    capabilities: tuple[str, ...] = ("quota", "models", "auth", "records", "refresh_usage", "sync_models", "refresh_token", "reauthorize", "clear_cooldown")
    variant: Callable | None = None
    plan: Callable | None = None
    available: Callable | None = None
    facts: Callable | None = None
    login: Callable | None = None
    benefit_query: Callable | None = None
    benefit_actions: tuple = ()
    benefit_view: Callable | None = None
    records: Callable | None = None


REGISTRY = {}


def register(provider_id, provider):
    REGISTRY[provider_id] = provider


def _openai_plan(a):
    from ..oauth.plan_labels import openai_plan_label
    return openai_plan_label(a.get("plan_type"))


def _claude_plan(a):
    from ..oauth_manager import claude_plan_label
    return claude_plan_label(a)


def _zhipu_available(a):
    # API-key accounts have no renewable OAuth credential or benefits.
    result = {"quota", "models", "records", "refresh_usage", "sync_models", "clear_cooldown"}
    if a.get("credential_mode") == "oauth":
        result |= {"benefits", "reauthorize", "zhipu_use", "zhipu_claim"}
        if not (a.get("model_key") or a.get("project_key_id")) and a.get("organization_id") and a.get("project_id"):
            result.add("zhipu_create_key")
    return result


def _workbuddy_available(a):
    result = {"quota", "models", "auth", "records", "refresh_usage", "sync_models", "reauthorize", "benefits", "clear_cooldown"}
    if os.environ.get("PARROT_NO_REFRESH") != "1":
        result.add("refresh_token")
    if a.get("realm", "cn") == "cn" and a.get("disabled_reason") in {None, "", "quota"} and (a.get("enabled", True) or a.get("disabled_reason") == "quota"):
        result.add("workbuddy_checkin")
    return result


def _login(provider):
    def start(chat, mid, cb, account):
        from ..telegram.menus import oauth_menu
        from ..telegram import ui
        key = oauth_menu._account_key(account)
        return oauth_menu.on_relogin(chat, mid, cb, ui.register_code(key))
    return start


def _claude_execute(control, context, key, token):
    result = control.execute_claude_reset(context, key, token)
    outcome = result.get("result")
    return {"status": "succeeded" if outcome in {"reset", "already_used"} else "unknown" if outcome in {"pending", "unconfirmed"} else "failed"}


def _card_view(value):
    from datetime import datetime, timezone, timedelta
    lines, choices = [], []
    reset = value.get("reset") or {}
    for name, title, kind in (("five_hour", "5小时", "FIVE_HOUR"), ("week", "周", "WEEK")):
        cards = reset.get("available_" + name + "_resets")
        if cards is None:
            lines.append(title + "卡库存未取得")
            continue
        lines.append(title + "卡库存：" + str(len(cards)))
        if len(cards) > 30:
            lines.append("当前选择器显示前30张；使用后明确刷新库存，未自动选择其他卡。")
        for index, card in enumerate(cards[:30]):
            expiry = card.get("expire_at")
            when = datetime.fromtimestamp(expiry / 1000, timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M") if expiry else "未取得"
            choices.append((title + "卡 #" + str(index + 1) + " 到期 " + when, "benefit_plan", {"kind": "zhipu_use", "type": kind, "index": index}))
    for row in (value.get("actions") or [])[-5:]:
        lines.append(str(row.get("action", "操作")) + "：" + str(row.get("status", "unknown")))
    return lines, choices


def _claude_view(value):
    lines = []
    for name, title in (("cedar_ember", "周重置卡"), ("juniper_tide", "5h重置（消耗周额度）")):
        block = value.get(name)
        if not isinstance(block, dict):
            lines.append(title + "：尚未取得")
            continue
        for key, label in (("eligible", "资格"), ("available", "可用"), ("ineligible_reason", "不可用原因"),
                           ("at_limit", "达到限制"), ("cooldown_until", "冷却截止"), ("next_available_at", "下次可用"), ("next_grant_id", "当前权益编号")):
            if isinstance(block.get(key), (str, bool, int)):
                lines.append(title + " · " + label + "：" + str(block[key]))
        for grant in block.get("grants", [])[:8]:
            if isinstance(grant, dict):
                lines.append(title + " · 权益 " + str(grant.get("id", "未取得")) + "；剩余次数 " + str(grant.get("resets_left", "未取得")))
    return lines, []


def _workbuddy_view(value):
    block = value.get("workbuddy", value)
    lines = []
    for key, label in (("active", "活动开放"), ("today_checked_in", "今日已签到"), ("streak_days", "连续天数"), ("today_credit", "今日积分")):
        v = (block.get("checkin") or {}).get(key)
        lines.append(label + "：" + (str(v) if isinstance(v, (str, bool, int, float)) else "未取得"))
    for m in _credit_metrics({"workbuddy": block}):
        if "remaining" in m:
            lines.append(m["display_name"] + "：剩余 " + str(m["remaining"]))
    return lines, []


def _credit_metrics(usage):
    metrics = quota_metrics(usage)
    for field, title in (("credits", "当前积分"), ("personal_credits", "个人积分"), ("enterprise_credits", "企业积分")):
        block = (usage.get("workbuddy") or {}).get(field)
        if not isinstance(block, dict):
            continue
        metric = {"metric_id": field, "display_name": title, "kind": "credits", "unit": "积分"}
        if block.get("reliable") is True:
            for source, target in (("used", "used"), ("remaining", "remaining"), ("capacity", "total")):
                if type(block.get(source)) in (int, float):
                    metric[target] = block[source]
        metrics.append(metric)
    return metrics


def _zhipu_metrics(usage):
    result = quota_metrics(usage)
    block = usage.get("zhipu") or {}
    for index, row in enumerate(block.get("limits") or []):
        if row.get("type") != "TIME_LIMIT" or type(row.get("remaining")) not in (int, float):
            continue
        result.append({"metric_id": "tool_limit_" + str(index), "display_name": "工具调用额度", "kind": "requests",
                       "remaining": row["remaining"], "unit": "次"})
    mcp = block.get("mcp") or {}
    metric = {"metric_id": "mcp", "display_name": "MCP调用", "kind": "requests", "unit": "次"}
    for key, name in (("used", "used"), ("limit", "total"), ("remaining", "remaining")):
        if type(mcp.get(key)) in (int, float):
            metric[name] = mcp[key]
    if len(metric) > 4:
        result.append(metric)
    return result


def _antigravity_metrics(usage):
    result = quota_metrics(usage)
    block = usage.get("antigravity") or {}
    metric = {"metric_id": "credits", "display_name": "Credits", "kind": "credits", "unit": "积分"}
    if block.get("known") is True and type(block.get("credit_amount")) in (int, float):
        metric["remaining"] = block["credit_amount"]
    if block:
        result.append(metric)
    return result


register("claude", Provider("Claude", plan=_claude_plan, capabilities=Provider.capabilities + ("benefits", "claude_reset", "claude_session_reset"),
    login=_login("claude"), benefit_view=_claude_view, benefit_query=lambda c, ctx, key: c.refresh_claude_reset_status(ctx, key),
    benefit_actions=(("claude_reset", "预览周卡（自然周重置日期不变）", lambda c, ctx, key, p: c.plan_claude_reset(ctx, key, "cedar_ember"), _claude_execute,
                      lambda c, ctx, key, token: c.confirm_claude_reset(ctx, key, token)),
                     ("claude_session_reset", "预览5h重置（消耗周额度份额）", lambda c, ctx, key, p: c.plan_claude_reset(ctx, key, "juniper_tide"), _claude_execute,
                      lambda c, ctx, key, token: c.confirm_claude_reset(ctx, key, token)))))
register("openai", Provider("OpenAI", (("workspace_name", "工作区"), ("workspace_type", "工作区类型")), plan=_openai_plan,
    capabilities=Provider.capabilities + ("stop_credits",), login=_login("openai")))
register("xai", Provider("Grok", plan=lambda a: _xai_plan(a), login=_login("xai")))
register("cursor", Provider("Cursor", plan=lambda a: str(a.get("plan_type") or ""), login=_login("cursor")))
register("antigravity", Provider("Antigravity", (("project_name", "项目"),), facts=_antigravity_metrics, login=_login("antigravity")))
register("workbuddy", Provider("WorkBuddy", (("realm", "地区"), ("enterprise_name", "企业")), available=_workbuddy_available, facts=_credit_metrics,
    login=_login("workbuddy"), records=lambda c, ctx, key, a: c.get_workbuddy_records(ctx, key, page_size=10)["items"], benefit_view=_workbuddy_view, benefit_query=lambda c, ctx, key: c.refresh_workbuddy_status_now(ctx, key),
    benefit_actions=(("workbuddy_checkin", "预览签到（需确认）", lambda c, ctx, key, p: c.plan_workbuddy_action(ctx, key, "checkin"),
        lambda c, ctx, key, token: c.execute_workbuddy_action_now(ctx, key, token)),)))
register("zhipu", Provider("智谱 / Z.ai", (("plan_scope", "账户范围"), ("organization_name", "组织"), ("project_name", "项目")),
    variant=lambda a: "智谱 · BigModel 中国站" if a.get("site") == "bigmodel" else "Z.ai 国际站" if a.get("site") == "zai" else "智谱 / Z.ai",
    available=_zhipu_available, facts=_zhipu_metrics, login=_login("zhipu"), records=lambda c, ctx, key, a: c.get_zhipu(ctx, key)["actions"], benefit_view=_card_view, benefit_query=lambda c, ctx, key: c.get_zhipu(ctx, key, reset_status=True),
    benefit_actions=(("zhipu_use", "预览用卡", lambda c, ctx, key, p: c.plan_zhipu_action(ctx, key, "use", reset_type=p["type"], card_index=p["index"]),
        lambda c, ctx, key, token: c.execute_zhipu_action_now(ctx, key, token)),
        ("zhipu_create_key", "预览创建/核对专用Key（需确认）", lambda c, ctx, key, p: c.plan_zhipu_action(ctx, key, "create_key"),
        lambda c, ctx, key, token: c.execute_zhipu_action_now(ctx, key, token)),
        ("zhipu_claim", "预览领取权益", lambda c, ctx, key, p: c.plan_zhipu_action(ctx, key, "opportunity"),
        lambda c, ctx, key, token: c.execute_zhipu_action_now(ctx, key, token)))))


def _xai_plan(a):
    from ..oauth.plan_labels import xai_plan_label
    return xai_plan_label(a.get("plan_type"))


def capabilities(account):
    provider = REGISTRY.get(str(account.get("provider") or "claude"))
    if not provider:
        return set()
    result = set(provider.available(account) if provider.available else provider.capabilities)
    if os.environ.get("PARROT_NO_REFRESH") == "1":
        result -= {"refresh_token"}
    from ..management_control.oauth.account_orchestration import OAuthAccountOrchestrationControlMixin
    if not OAuthAccountOrchestrationControlMixin.account_authentication_failed(account):
        result.discard("reauthorize")
    if account.get("allowCredits") is not True:
        result.discard("stop_credits")
    return result


def quota_metrics(usage):
    """Common normalized usage only. Native extra metrics require a registered adapter."""
    out = []
    labels = {"five_hour": "5小时额度", "seven_day": "7天额度", "seven_day_fable": "Fable周额度", "thirty_day": "30天额度", "seven_day_sonnet": "Sonnet周额度", "seven_day_opus": "Opus周额度"}
    for key, label in labels.items():
        window = (usage or {}).get(key)
        if not isinstance(window, dict):
            continue
        used = window.get("utilization")
        if isinstance(used, (int, float)) and not isinstance(used, bool):
            metric = {"metric_id": key, "display_name": label, "kind": "percent", "used": used, "unit": "%"}
            if window.get("resets_at"):
                metric["reset_at"] = str(window["resets_at"])
                metric["reset_certainty"] = "upstream"
            out.append(metric)
    return out
