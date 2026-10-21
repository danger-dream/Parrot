"""Single notification contract/configuration directory; no application imports."""
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class Spec:
    title: str
    family: str
    legacy: str | None
    fields: tuple[str, ...]
    actions: tuple[str, ...] = ()
    severity: str = "warning"
    phase: str = "opened"
    push: bool = True
    repeat: int = 0
    auto_delete: int | None = None
    episodic: bool = False
    required: tuple[str, ...] = ()
    any_fields: tuple[str, ...] = ()  # at least one declared observation; empty quota lists mean unknown
    effects: tuple[str, ...] = ()
    confirmed_only: bool = False
    next_step: str = "请查看事件详情和当前对象状态。"
    labels: tuple[tuple[str, str], ...] = ()
    recovery_from: str | None = None


# facts are whitelist typed fields, never exception bodies or provider payloads.
FIELD_TYPES = {
    "metrics": list, "windows": list, "models": list, "added": list, "removed": list,
    "signals": list, "exclusions": list, "benefits": list, "stages": list,
    "expires_at": str, "reset_at": str, "retry_at": str, "pool": str,
    "error_code": str, "error_title": str, "error_reason": str, "advice": str,
    "retryable": bool, "auth_error": bool, "count": int, "threshold": (int, float),
    "model": str, "requested_model": str, "outbound_model": str, "actual_model": str,
    "api_key_name": str, "request_id": str, "response_id": str, "ingress": str,
    "operation": str, "result": str, "business_date": str, "previous": str,
    "inventory": list, "saved": bool,
}
CATALOG = {
    "account.auth.refreshed": Spec("授权已续期", "auth", "oauth_refreshed", ("expires_at",), ("auth",), "info", "updated", False, auto_delete=180, episodic=True),
    "account.auth.refresh_failed": Spec("授权续期未完成", "auth", "oauth_refresh_failed", ("error_code", "error_title", "error_reason", "advice", "retryable", "auth_error"), ("auth", "refresh_token", "reauthorize"), repeat=1800),
    "account.auth.recovered": Spec("授权问题已恢复", "auth", "oauth_refreshed", ("expires_at",), ("auth",), "info", "recovered"),
    "account.quota.blocked": Spec("额度达到暂停条件", "quota", "quota_disabled", ("metrics", "windows", "threshold", "reset_at"), ("refresh_usage", "benefits")),
    "account.quota.recovered": Spec("配额已恢复", "quota", "quota_resumed", ("metrics", "windows", "operation", "previous"), (), "info", "recovered"),
    "model.quota.blocked": Spec("模型额度已暂停", "model.quota", "quota_cooldown", ("metrics", "windows", "pool", "models", "count", "threshold", "reset_at", "error_code"), ("models", "refresh_usage")),
    "model.quota.recovered": Spec("模型额度已恢复", "model.quota", "quota_resumed", ("pool", "models", "count", "metrics"), ("models",), "info", "recovered"),
    "account.credits.started": Spec("套餐额度耗尽，将使用积分", "credits", "openai_credits_started", ("metrics",), ("stop_credits",)),
    "account.action.attention": Spec("账户操作尚未完成", "action", "workbuddy_action_failed", ("operation", "result", "business_date", "error_code"), ("records", "benefits"), repeat=86400),
    "account.benefit.granted": Spec("权益已领取", "benefit", None, ("benefits", "inventory", "operation", "result"), ("benefits", "records"), "info", episodic=True),
    "account.catalog.changed": Spec("模型目录已变化", "catalog", None, ("added", "removed"), ("models", "sync_models"), "info", episodic=True),
    "request.model.changed": Spec("上游报告模型发生变化", "request.model", "model_degraded", ("requested_model", "outbound_model", "actual_model", "api_key_name", "request_id"), ("request",), repeat=7200),
    "request.model.conflict": Spec("上游模型信息不一致", "request.model", "model_degraded", ("requested_model", "outbound_model", "signals", "api_key_name", "request_id"), ("request",), repeat=7200),
    "channel.model.frozen": Spec("渠道模型永久冻结", "channel.model", "channel_permanent", ("model", "count", "error_code"), ("models", "clear_cooldown"), "error"),
    "channel.model.recovered": Spec("渠道模型限制已解除", "channel.model", "channel_recovered", ("model", "previous"), ("models",), "info", "recovered"),
    "model.entitlement.blocked": Spec("当前套餐未包含模型", "entitlement", "plan_excluded", ("model", "error_code", "retry_at"), ("models",), repeat=86400),
    "model.entitlement.recovered": Spec("套餐模型的本地限制已解除", "entitlement", "channel_recovered", ("model", "previous"), ("models",), "info", "recovered"),
    "routing.unavailable": Spec("无可用渠道", "routing", "no_channels", ("ingress", "model", "exclusions", "api_key_name", "request_id"), ("routes",), "error", repeat=300),
    "system.responses_store.failed": Spec("Responses历史存储写入失败", "store", "openai_store_save_failed", ("model", "api_key_name", "response_id", "error_code"), ("storage",), "error", repeat=300),
    "operation.completed": Spec("操作已完成", "operation", None, ("operation", "result", "saved", "stages", "count"), ("models",), "info", "updated", episodic=True),
    "operation.attention": Spec("操作需要关注", "operation", None, ("operation", "result", "saved", "stages", "error_code", "error_title", "error_reason", "advice"), ("models", "sync_models", "refresh_usage", "benefits", "reauthorize"), episodic=True),
}

# Scene contracts live beside the registry, never in provider-dependent templates.
_CONTRACTS = {
    "account.auth.refreshed": dict(required=("expires_at",), effects=("auth_refreshed",), confirmed_only=True,
        next_step="无需为本次成功续期操作；可查看当前授权状态。"),
    "account.auth.refresh_failed": dict(required=("error_code", "retryable", "auth_error"), effects=("account_paused", "not_disabled", "disable_failed"),
        next_step="请按上述分类建议处理；查看授权状态不会自动重试。"),
    "account.auth.recovered": dict(required=("expires_at",), effects=("auth_refreshed",), confirmed_only=True,
        next_step="此前授权问题已核实恢复；其他调度限制独立生效。"),
    "account.quota.blocked": dict(any_fields=("windows", "metrics"), effects=("account_paused",),
        next_step="可在账户详情查看额度；更新并检查可能按现有配额策略恢复调用。"),
    "account.quota.recovered": dict(any_fields=("windows", "metrics", "previous"), effects=("account_resumed", "still_paused"), confirmed_only=True,
        next_step="本次配额限制已解除；如仍不可调用，请查看当前其他限制。", labels=(("windows", "恢复检查窗口"),)),
    "model.quota.blocked": dict(required=("models",), effects=("model_paused",),
        next_step="请查看指定模型/额度池；重置时间不是调用一定成功的保证。"),
    "model.quota.recovered": dict(required=("models",), effects=("model_resumed",), confirmed_only=True,
        next_step="指定模型/池的本地限制已解除；其他限制见当前详情。"),
    "account.credits.started": dict(required=("metrics",), effects=("credits_allowed",), confirmed_only=True,
        next_step="不希望继续消耗积分时，可确认暂停积分续用。"),
    "account.action.attention": dict(required=("operation", "result", "error_code"), effects=("action_unknown", "action_failed"),
        next_step="先核对真实操作记录；结果未知不自动重提。"),
    "account.benefit.granted": dict(required=("operation", "result"), effects=("benefit_saved",), confirmed_only=True,
        next_step="请查看重置方式；消费权益必须再次确认。"),
    "account.catalog.changed": dict(any_fields=("added", "removed"), effects=("catalog_updated",), confirmed_only=True,
        next_step="可查看变化和当前模型目录；同步不会自动消费权益。"),
    "request.model.changed": dict(required=("requested_model", "outbound_model", "actual_model"), effects=("model_changed",), confirmed_only=True,
        next_step="查看关联请求与上游证据；不能仅据模型名推断能力下降。"),
    "request.model.conflict": dict(required=("requested_model", "outbound_model", "signals"), effects=("model_conflict",), confirmed_only=True,
        next_step="查看各来源证据；当前无法确定唯一实际模型。"),
    "channel.model.frozen": dict(required=("model", "error_code"), effects=("model_paused",),
        next_step="查看指定模型错误；清本地冷却需要确认，不保证上游可用。", labels=(("count", "累计失败次数"),)),
    "channel.model.recovered": dict(required=("model", "previous"), effects=("model_resumed",), confirmed_only=True,
        next_step="此模型的本地限制已解除；账户开关与上游可用性仍须分别查看。"),
    "model.entitlement.blocked": dict(required=("model", "error_code", "retry_at"), effects=("model_paused",),
        next_step="套餐开放前跳过此模型；其他渠道不受此项限制。"),
    "model.entitlement.recovered": dict(required=("model", "previous"), effects=("model_resumed",), confirmed_only=True,
        next_step="本地套餐排除限制已解除；不据此推断套餐已升级。"),
    "routing.unavailable": dict(required=("ingress", "model", "exclusions"), effects=("route_blocked",), confirmed_only=True,
        next_step="查看当时筛选证据与该模型当前渠道；不会自动修复。"),
    "system.responses_store.failed": dict(required=("error_code", "response_id"), effects=("history_unavailable",),
        next_step="请检查存储状态、权限与磁盘空间；不会自动删除或重建存储。"),
    "operation.completed": dict(required=("operation", "result"), effects=("saved", "not_disabled"),
        next_step="操作结果已记录；可查看精确对象的当前详情。", labels=(("count", "处理数量"),)),
    "operation.attention": dict(required=("operation", "result"), any_fields=("stages", "error_code"), effects=("saved", "not_saved", "not_disabled"),
        next_step="账户保存与各初始化阶段独立；仅重试尚未完成的阶段。", labels=(("count", "处理数量"),)),
}
for _name, _rules in _CONTRACTS.items():
    CATALOG[_name] = replace(CATALOG[_name], **_rules)
for _recovery, _opening in {
    "account.auth.recovered": "account.auth.refresh_failed", "account.quota.recovered": "account.quota.blocked",
    "model.quota.recovered": "model.quota.blocked", "channel.model.recovered": "channel.model.frozen",
    "model.entitlement.recovered": "model.entitlement.blocked",
}.items():
    CATALOG[_recovery] = replace(CATALOG[_recovery], recovery_from=_opening)

# Retained system events are deliberately not routed through the new core.
LEGACY_SYSTEM = {"status_alert": "上游官网状态", "app_update": "应用更新", "network_monitor": "网络健康",
                 "model_registry": "模型命名变化"}
LEGACY_LABELS = {}
for spec in CATALOG.values():
    if spec.legacy:
        LEGACY_LABELS.setdefault(spec.legacy, spec.title)
LEGACY_LABELS.update(LEGACY_SYSTEM)


def public_key(name):
    parts = name.replace(".", "_").split("_")
    return parts[0] + "".join(p.title() for p in parts[1:])


def settings_entries():
    return tuple((public_key(key), key) for key in (*LEGACY_LABELS, *CATALOG))


def default_events():
    # Existing installations historically defaulted every legacy key to enabled.
    # Do NOT inject canonical defaults: their presence would override old settings.
    return {key: True for key in LEGACY_LABELS}


def enabled(event_type, notifications):
    spec = CATALOG[event_type]
    flags = notifications.get("events") or {}
    if not notifications.get("enabled", True):
        return False
    if event_type in flags:
        return flags[event_type] is True
    if spec.legacy:
        return flags.get(spec.legacy, True) is True
    return spec.push


def register(event_type, spec):
    if (event_type in CATALOG or any(field not in FIELD_TYPES for field in spec.fields)
            or not (spec.required or spec.any_fields) or not spec.effects
            or (set(spec.required) | set(spec.any_fields)) - set(spec.fields)):
        raise ValueError("invalid or duplicate notification contract")
    CATALOG[event_type] = spec
