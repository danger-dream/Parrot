"""Versioned, credential-free event contracts. Unknown fields fail closed."""
from dataclasses import asdict, dataclass, field
import math
import time
import uuid
from .catalog import CATALOG, FIELD_TYPES


@dataclass(frozen=True)
class Effect:
    status: str  # confirmed / unchanged / failed / unknown
    code: str    # a registered local effect, not arbitrary prose


EFFECTS = {
    "account_paused": "Parrot 已暂停此账号调用。",
    "account_resumed": "Parrot 已解除此前配额暂停，恢复调用此账号。",
    "still_paused": "本次限制已解除，但账户仍被其他条件暂停。",
    "not_disabled": "Parrot 未因此自动禁用账号；授权可用性以当前状态为准。",
    "disable_failed": "禁用状态保存失败，当前本地状态未确认。",
    "auth_refreshed": "授权已续期；未改变其他调度限制。",
    "model_paused": "仅暂停本账户/渠道的指定模型或额度池；其他模型不受此项限制。",
    "model_resumed": "已解除指定模型/池的本地限制；其他限制仍按当前状态执行。",
    "credits_allowed": "已允许使用积分，后续请求可能消耗积分余额；账号保持可用。",
    "benefit_saved": "领取已获上游确认；权益未自动使用，使用仍需确认。",
    "catalog_updated": "账户模型目录已更新；具体可路由模型见详情。",
    "model_changed": "上游报告的模型与出站模型不同，不据此判断能力降级。",
    "model_conflict": "上游模型信号相互矛盾，无法据此确定实际模型或是否降级。",
    "route_blocked": "该入口请求当前没有可用渠道；不代表某一家服务账号故障。",
    "history_unavailable": "本地历史保存未完成；后续 previous_response_id 续接或 fallback 可能无法展开历史。",
    "action_unknown": "结果未知，可能已提交；请核对记录，不自动重复提交。",
    "action_failed": "本次操作未完成；请查看记录及当前限制。",
    "saved": "账户已保存；各初始化阶段结果见下方/详情。",
    "not_saved": "本次未确认账户保存成功；未因通知创建远端资源。",
}


@dataclass(frozen=True)
class Subject:
    kind: str
    exact_id: str
    generation: str
    provider: str
    service: str
    short_id: str
    label: str = ""
    identity: str = ""
    scopes: tuple[tuple[str, str], ...] = ()
    plan: str = ""


COMPLETED_STAGES = {"succeeded", "skipped", "not_modified"}
STAGE_STATUSES = COMPLETED_STAGES | {"failed", "unknown", "running", "queued", "pending", "partial_failed", "not_submitted", "already_done"}
METRIC_KINDS = {"percent", "credits", "requests", "tokens", "concurrency", "entitlement"}
SCOPE_KINDS = {"account", "model", "pool", "route", "operation", "system"}
RESET_CERTAINTIES = {"upstream", "estimated", "unknown"}


_LIST_SCHEMAS = {
    "metrics": {"metric_id": str, "display_name": str, "kind": str, "scope_kind": str,
                "scope_ref": str, "used": (int, float), "remaining": (int, float),
                "total": (int, float), "threshold": (int, float), "unit": str,
                "reset_at": str, "reset_certainty": str, "observed_at": (int, float), "source": str},
    "signals": {"source": str, "models": list},
    "exclusions": {"subject": str, "reason": str, "detail": str},
    "benefits": {"name": str, "count": int, "expires_at": str},
    "inventory": {"name": str, "count": int},
    "stages": {"name": str, "status": str, "count": int},
}


def _check(value, typ):
    if not isinstance(value, typ) or (isinstance(value, bool) and typ is not bool):
        raise ValueError("invalid notification fact type")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite notification fact")
    if isinstance(value, str) and len(value) > 2048:
        raise ValueError("notification fact too long")


def validate_facts(event_type, facts):
    spec = CATALOG[event_type]
    if set(facts) - set(spec.fields):
        raise ValueError("unregistered notification fact")
    for name, value in facts.items():
        _check(value, FIELD_TYPES[name])
        if isinstance(value, list):
            if len(value) > 300:
                raise ValueError("too many notification facts")
            for item in value:
                schema = _LIST_SCHEMAS.get(name)
                if schema:
                    if not isinstance(item, dict) or set(item) - set(schema):
                        raise ValueError("unregistered structured fact")
                    for key, val in item.items():
                        _check(val, schema[key])
                        if isinstance(val, list):
                            for text in val:
                                _check(text, str)
                else:
                    _check(item, str)

    for metric in facts.get("metrics", []):
        if metric.get("kind") not in METRIC_KINDS:
            raise ValueError("unregistered metric kind")
        if "scope_kind" in metric and metric["scope_kind"] not in SCOPE_KINDS:
            raise ValueError("unregistered metric scope")
        if "reset_certainty" in metric and metric["reset_certainty"] not in RESET_CERTAINTIES:
            raise ValueError("unregistered reset certainty")
    for stage in facts.get("stages", []):
        if not stage.get("name") or stage.get("status") not in STAGE_STATUSES:
            raise ValueError("unregistered operation stage")
        if event_type == "operation.completed" and stage["status"] not in COMPLETED_STAGES:
            raise ValueError("operation completion contradicts stage")
    if event_type == "operation.completed" and facts.get("saved") is False:
        raise ValueError("operation completion contradicts save")
    for name in spec.required:
        if name not in facts or (isinstance(facts[name], str) and not facts[name].strip()):
            raise ValueError("missing required notification fact")
    if spec.any_fields and not any(name in facts for name in spec.any_fields):
        raise ValueError("missing scene observations")
    if "error_code" in facts:
        import re
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", facts["error_code"]):
            raise ValueError("unsafe error classification")
    if event_type == "request.model.changed" and facts["outbound_model"].casefold() == facts["actual_model"].casefold():
        raise ValueError("no observed model change")
    if event_type == "request.model.conflict" and len({m.casefold() for group in facts["signals"] for m in group.get("models", []) if m.strip()}) < 2:
        raise ValueError("no conflicting model evidence")
    if event_type == "account.catalog.changed" and not (facts.get("added") or facts.get("removed")):
        raise ValueError("empty model change")
    if event_type.startswith("model.quota.") and not facts["models"]:
        raise ValueError("missing affected models")


@dataclass(frozen=True)
class NotificationEvent:
    event_type: str
    subject: Subject
    effect: Effect
    facts: dict = field(default_factory=dict)
    scope: str = "account"
    scope_ref: str = ""
    dimension: str = ""
    source: str = "local"
    version: int = 0
    correlation_ref: str = ""
    occurred_at: float = field(default_factory=time.time)
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    severity: str = ""
    schema_version: int = 1

    def record(self):
        validate_facts(self.event_type, self.facts)
        if self.effect.status not in {"confirmed", "unchanged", "failed", "unknown"} or self.effect.code not in EFFECTS:
            raise ValueError("unregistered notification effect")
        spec = CATALOG[self.event_type]
        if self.effect.code not in spec.effects or (spec.confirmed_only and self.effect.status != "confirmed"):
            raise ValueError("effect contradicts notification scene")
        allowed_status = {
            "account_paused": {"confirmed", "failed", "unknown"}, "model_paused": {"confirmed", "failed", "unknown"},
            "not_disabled": {"confirmed", "unchanged"}, "disable_failed": {"failed", "unknown"},
            "history_unavailable": {"failed", "unknown"}, "action_unknown": {"unknown"},
            "action_failed": {"failed"}, "not_saved": {"failed", "unknown"},
        }.get(self.effect.code, {"confirmed"})
        if self.effect.status not in allowed_status:
            raise ValueError("effect status contradicts outcome")
        if spec.phase == "recovered" and self.effect.status != "confirmed":
            raise ValueError("recovery is not confirmed")
        if self.event_type == "operation.completed" and self.effect.status not in {"confirmed", "unchanged"}:
            raise ValueError("operation completion unconfirmed")
        if not self.subject.service or self.subject.kind not in {"account", "channel", "route", "system", "operation"}:
            raise ValueError("invalid notification subject")
        if self.severity and self.severity not in {"info", "warning", "error", "critical"}:
            raise ValueError("invalid notification severity")
        result = asdict(self)
        result["subject"]["scopes"] = [list(pair) for pair in self.subject.scopes]
        result["severity"] = self.severity or CATALOG[self.event_type].severity
        result["phase"] = CATALOG[self.event_type].phase
        return result
