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
    next_step: str = ""
    labels: tuple[tuple[str, str], ...] = ()
    recovery_from: str | None = None
    show: tuple[str, ...] = ()


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
    "account.auth.refreshed": Spec("授权已续期", "auth", "oauth_refreshed", ("expires_at",), ("view",), "info", "updated", False, auto_delete=180, episodic=True),
    "account.auth.refresh_failed": Spec("授权续期失败", "auth", "oauth_refresh_failed", ("error_code", "error_title", "error_reason", "advice", "retryable", "auth_error"), ("view", "reauthorize", "refresh_token"), repeat=1800),
    "account.auth.recovered": Spec("授权已恢复", "auth", "oauth_refreshed", ("expires_at",), ("view",), "info", "recovered"),
    "account.quota.blocked": Spec("额度达到暂停线", "quota", "quota_disabled", ("metrics", "windows", "threshold", "reset_at"), ("view", "refresh_usage", "benefits")),
    "account.quota.recovered": Spec("额度已恢复", "quota", "quota_resumed", ("metrics", "windows", "operation", "previous"), ("view",), "info", "recovered"),
    "model.quota.blocked": Spec("模型额度已暂停", "model.quota", "quota_cooldown", ("metrics", "windows", "pool", "models", "count", "threshold", "reset_at", "error_code"), ("models", "refresh_usage", "view")),
    "model.quota.recovered": Spec("模型额度已恢复", "model.quota", "quota_resumed", ("pool", "models", "count", "metrics"), ("models", "view"), "info", "recovered"),
    "account.credits.started": Spec("套餐额度用完，开始消耗积分", "credits", "openai_credits_started", ("metrics",), ("view", "stop_credits")),
    "account.action.attention": Spec("账户操作未完成", "action", "workbuddy_action_failed", ("operation", "result", "business_date", "error_code"), ("records", "view", "benefits"), repeat=86400),
    "account.benefit.granted": Spec("权益已领取", "benefit", None, ("benefits", "inventory", "operation", "result"), ("view", "benefits", "records"), "info", episodic=True),
    "account.catalog.changed": Spec("模型目录有变化", "catalog", None, ("added", "removed"), ("models", "view"), "info", episodic=True),
    "request.model.changed": Spec("上游返回的模型与请求不同", "request.model", "model_degraded", ("requested_model", "outbound_model", "actual_model", "api_key_name", "request_id"), ("log", "model", "key", "view"), repeat=7200),
    "request.model.conflict": Spec("上游模型信息不一致", "request.model", "model_degraded", ("requested_model", "outbound_model", "signals", "api_key_name", "request_id"), ("log", "model", "key", "view"), repeat=7200),
    "channel.model.frozen": Spec("模型已永久冻结", "channel.model", "channel_permanent", ("model", "count", "error_code"), ("models", "view", "clear_cooldown"), "error"),
    "channel.model.recovered": Spec("模型限制已解除", "channel.model", "channel_recovered", ("model", "previous"), ("models", "view"), "info", "recovered"),
    "model.entitlement.blocked": Spec("套餐不含此模型", "entitlement", "plan_excluded", ("model", "error_code", "retry_at"), ("models", "view"), repeat=86400),
    "model.entitlement.recovered": Spec("套餐模型限制已解除", "entitlement", "channel_recovered", ("model", "previous"), ("models", "view"), "info", "recovered"),
    "routing.unavailable": Spec("无可用渠道", "routing", "no_channels", ("ingress", "model", "exclusions", "api_key_name", "request_id"), ("log", "model", "key"), "error", repeat=300),
    "system.responses_store.failed": Spec("Responses 历史保存失败", "store", "openai_store_save_failed", ("model", "api_key_name", "response_id", "error_code"), ("storage", "key"), "error", repeat=300),
    "operation.completed": Spec("操作已完成", "operation", None, ("operation", "result", "saved", "stages", "count"), ("view",), "info", "updated", episodic=True),
    "operation.attention": Spec("操作部分未完成", "operation", None, ("operation", "result", "saved", "stages", "error_code", "error_title", "error_reason", "advice"), ("view", "sync_models", "refresh_usage", "benefits", "reauthorize"), episodic=True),
}

# Scene contracts live beside the registry, never in provider-dependent templates.
# ``show`` is the reading order of the pushed body; the request origin (Key, ingress)
# is rendered beside the subject and request/response ids are reached by buttons.
_CONTRACTS = {
    "account.auth.refreshed": dict(required=("expires_at",), effects=("auth_refreshed",), confirmed_only=True),
    "account.auth.refresh_failed": dict(required=("error_code", "retryable", "auth_error"), effects=("account_paused", "not_disabled", "disable_failed"),
        show=("error_title", "error_reason", "error_code"), next_step="后台会继续重试；持续失败时请重新授权。"),
    "account.auth.recovered": dict(required=("expires_at",), effects=("auth_refreshed",), confirmed_only=True),
    "account.quota.blocked": dict(any_fields=("windows", "metrics"), effects=("account_paused",),
        show=("metrics", "windows", "threshold", "reset_at"), next_step="额度重置后会自动恢复，也可点「更新额度」立即检查。"),
    "account.quota.recovered": dict(any_fields=("windows", "metrics", "previous"), effects=("account_resumed", "still_paused"), confirmed_only=True,
        show=("metrics", "windows", "operation"), labels=(("windows", "恢复检查窗口"),)),
    "model.quota.blocked": dict(required=("models",), effects=("model_paused",),
        show=("models", "pool", "metrics", "windows", "threshold", "reset_at", "error_code"), next_step="额度重置后会自动恢复，也可点「更新额度」立即检查。"),
    "model.quota.recovered": dict(required=("models",), effects=("model_resumed",), confirmed_only=True,
        show=("models", "pool")),
    "account.credits.started": dict(required=("metrics",), effects=("credits_allowed",), confirmed_only=True,
        next_step="不想消耗积分，点「暂停积分续用」。"),
    "account.action.attention": dict(required=("operation", "result", "error_code"), effects=("action_unknown", "action_failed"),
        next_step="先在「操作记录」核对是否已提交，再决定是否重试。"),
    "account.benefit.granted": dict(required=("operation", "result"), effects=("benefit_saved",), confirmed_only=True,
        show=("benefits", "inventory", "result")),
    "account.catalog.changed": dict(any_fields=("added", "removed"), effects=("catalog_updated",), confirmed_only=True),
    "request.model.changed": dict(required=("requested_model", "outbound_model", "actual_model"), effects=("model_changed",), confirmed_only=True,
        show=("requested_model", "outbound_model", "actual_model"), next_step="点「调用日志」查看这次请求的上游返回。"),
    "request.model.conflict": dict(required=("requested_model", "outbound_model", "signals"), effects=("model_conflict",), confirmed_only=True,
        show=("requested_model", "outbound_model", "signals"), next_step="点「调用日志」查看这次请求的上游返回。"),
    "channel.model.frozen": dict(required=("model", "error_code"), effects=("model_paused",),
        show=("model", "count", "error_code"), next_step="确认上游恢复后，点「清除冷却」解除冻结。", labels=(("count", "累计失败次数"),)),
    "channel.model.recovered": dict(required=("model", "previous"), effects=("model_resumed",), confirmed_only=True),
    "model.entitlement.blocked": dict(required=("model", "error_code", "retry_at"), effects=("model_paused",),
        show=("model", "retry_at", "error_code"), next_step="套餐开通此模型前会一直跳过它，其他渠道不受影响。"),
    "model.entitlement.recovered": dict(required=("model", "previous"), effects=("model_resumed",), confirmed_only=True),
    "routing.unavailable": dict(required=("ingress", "model", "exclusions"), effects=("route_blocked",), confirmed_only=True,
        show=("exclusions",)),
    "system.responses_store.failed": dict(required=("error_code", "response_id"), effects=("history_unavailable",),
        show=("model", "response_id", "error_code"), next_step="检查磁盘空间和存储文件权限，可点「存储状态」查看。"),
    "operation.completed": dict(required=("operation", "result"), effects=("saved", "not_disabled"),
        show=("operation", "result", "stages", "count"), labels=(("count", "处理数量"),)),
    "operation.attention": dict(required=("operation", "result"), any_fields=("stages", "error_code"), effects=("saved", "not_saved", "not_disabled"),
        show=("operation", "result", "stages", "error_title", "error_reason", "error_code"),
        next_step="可用下方按钮只重试未完成的阶段。", labels=(("count", "处理数量"),)),
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
                 "model_registry": "模型命名变化", "translation_failed": "翻译层连续失败"}
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
