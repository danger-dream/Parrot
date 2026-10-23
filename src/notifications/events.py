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
    "account_paused": "已暂停调用此账号。",
    "account_resumed": "已恢复调用此账号。",
    "still_paused": "额度限制已解除，但账号仍因其他原因暂停。",
    "not_disabled": "账号仍保持启用。",
    "disable_failed": "暂停账号时保存失败，账号当前状态未确认。",
    "auth_refreshed": "",
    "model_paused": "已暂停受影响的模型，其他模型照常调度。",
    "model_resumed": "受影响的模型已恢复调度。",
    "credits_allowed": "后续请求会消耗积分，账号保持可用。",
    "benefit_saved": "权益已入账，不会自动使用。",
    "catalog_updated": "",
    "model_changed": "上游返回的模型名与发出的不一致。",
    "model_conflict": "多处上游信息报告的模型不一致，无法确定实际模型。",
    "route_blocked": "",
    "history_unavailable": "这次响应的历史没有保存，之后用 previous_response_id 续接可能失败。",
    "action_unknown": "结果未知，可能已经提交；不会自动重复提交。",
    "action_failed": "操作没有完成。",
    "saved": "账号已保存。",
    "not_saved": "账号未确认保存成功。",
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
