"""Upstream error handling rules Management API routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Path, Query, Request, status

from src.management_auth import Capability
from src.management_control import ManagementContext, ManagementErrorCode
from src.management_control.system.error_rules import STATS_WINDOW_SECONDS

from ..dependencies import require_capability
from ..error_mapping import management_error_responses
from ..schemas.base import DataEnvelope
from ..schemas.error_rules import (
    ErrorRuleInput,
    ErrorRulePresetPatch,
    ErrorRulesData,
    ErrorRulesRecentData,
    ErrorRulesReorderRequest,
    ErrorRulesSettingsPatch,
    ErrorRulesTestData,
    ErrorRulesTestRequest,
)
from .system_support import (
    SystemNetworkControls,
    get_bound_system_network_controls,
    reject_unknown_query,
    response_meta,
    success_response,
)

router = APIRouter()
ReadContext = Annotated[ManagementContext, Depends(require_capability(Capability.READ))]
WriteContext = Annotated[ManagementContext, Depends(require_capability(Capability.WRITE))]
DestroyContext = Annotated[ManagementContext, Depends(require_capability(Capability.DESTRUCTIVE))]
Controls = Annotated[SystemNetworkControls, Depends(get_bound_system_network_controls)]
IfMatch = Annotated[str | None, Header(alias="If-Match")]
RuleId = Annotated[str, Path(alias="ruleId", min_length=1, max_length=64)]
InvalidRuleKey = Annotated[str, Path(alias="key", pattern=r"^[0-9a-f]{16}$")]

_RULE_EXAMPLE = {
    "id": "rule-1a2b3c4d",
    "name": "MiniMax Token Plan 用尽",
    "enabled": True,
    "note": "",
    "when": {"kind": "group", "mode": "all", "items": [
        {"kind": "cond", "field": "provider", "op": "in", "path": "", "values": ["minimax"]},
        {"kind": "cond", "field": "path", "op": "ne", "path": "$.base_resp.status_code", "values": ["0"]},
        {"kind": "group", "mode": "any", "items": [
            {"kind": "cond", "field": "message", "op": "contains", "path": "", "values": ["Token Plan"]},
            {"kind": "cond", "field": "code", "op": "in", "path": "", "values": ["2067"]},
        ]},
    ]},
    "category": "quota_exhausted",
    "steps": [
        {"type": "retry", "delaySeconds": 2, "times": 1},
        {"type": "pause", "scope": "model", "mode": "usage_window", "seconds": 900, "pattern": "",
         "timezone": "+08:00", "boundaryHours": 5, "fallbackSeconds": 900},
        {"type": "notify", "repeatHours": 6},
    ],
    "final": "next",
    "thresholds": [{"count": 3, "mode": "consecutive", "windowMinutes": 10, "scope": "model",
                    "steps": [{"type": "pause", "scope": "account", "mode": "fixed", "seconds": 1800,
                               "pattern": "", "timezone": "+08:00", "boundaryHours": 0, "fallbackSeconds": 900}]}],
    "response": {"status": 429, "message": "MiniMax quota: {$.error.message}", "retryAfter": True},
    "group": "",
}
_RULES_EXAMPLE = {
    "enabled": True, "rules": [_RULE_EXAMPLE], "presets": [], "builtins": [], "categories": [],
    "providers": [{"id": "minimax", "channelCount": 1}], "channelProviders": {"api:MiniMax": "minimax"},
    "stats": {"rule-1a2b3c4d": {"count": 3, "lastAt": 1795180000.0}}, "statsWindowDays": 7,
    "revision": "rev_example",
}
_ERRORS = (
    ManagementErrorCode.SESSION_REQUIRED, ManagementErrorCode.SESSION_EXPIRED,
    ManagementErrorCode.ORIGIN_DENIED, ManagementErrorCode.CAPABILITY_DENIED,
    ManagementErrorCode.VALIDATION_FAILED, ManagementErrorCode.RESOURCE_NOT_FOUND,
    ManagementErrorCode.RESOURCE_CONFLICT, ManagementErrorCode.REVISION_CONFLICT,
    ManagementErrorCode.DEPENDENCY_UNAVAILABLE, ManagementErrorCode.SERVICE_NOT_READY,
)
_RESPONSES = {**success_response(200, _RULES_EXAMPLE), **management_error_responses(*_ERRORS)}
_CREATE_RESPONSES = {**success_response(201, _RULES_EXAMPLE), **management_error_responses(*_ERRORS)}
_READ_RESPONSES = management_error_responses(*_ERRORS)


def _data(snap, controls: SystemNetworkControls) -> ErrorRulesData:
    providers, channel_providers = controls.error_rules.providers()
    return ErrorRulesData.model_validate({
        "enabled": snap.enabled,
        "rules": list(snap.rules),
        "invalidRules": list(snap.invalid),
        "presets": list(snap.presets),
        "builtins": controls.error_rules.builtins(),
        "categories": controls.error_rules.categories(),
        "providers": providers,
        "channelProviders": channel_providers,
        "stats": controls.error_rules.stats(),
        "statsWindowDays": STATS_WINDOW_SECONDS // 86400,
        "revision": snap.revision,
    })


def _route(path: str, method: str, operation_id: str, *, status_code: int = 200, responses=None,
           model=DataEnvelope[ErrorRulesData]):
    return getattr(router, method)(
        path,
        operation_id=operation_id,
        dependencies=[Depends(reject_unknown_query())],
        tags=["management-system"],
        status_code=status_code,
        response_model=model,
        responses=responses or _RESPONSES,
    )


@_route("/error-rules", "get", "getErrorRules")
def get_error_rules(request: Request, context: ReadContext, controls: Controls):
    return DataEnvelope(data=_data(controls.error_rules.get(context), controls), meta=response_meta(request))


@_route("/error-rules/settings", "patch", "updateErrorRulesSettings")
def update_error_rules_settings(body: ErrorRulesSettingsPatch, request: Request, context: WriteContext,
                                controls: Controls, if_match: IfMatch = None):
    snap = controls.error_rules.set_enabled(context, enabled=body.enabled, expected_revision=if_match)
    return DataEnvelope(data=_data(snap, controls), meta=response_meta(request))


@_route("/error-rules/rules", "post", "createErrorRule", status_code=status.HTTP_201_CREATED,
        responses=_CREATE_RESPONSES)
def create_error_rule(body: ErrorRuleInput, request: Request, context: WriteContext, controls: Controls,
                      if_match: IfMatch = None):
    snap = controls.error_rules.create_rule(context, body.engine_payload(), expected_revision=if_match)
    return DataEnvelope(data=_data(snap, controls), meta=response_meta(request))


@_route("/error-rules/rules/{ruleId}", "patch", "updateErrorRule")
def update_error_rule(rule_id: RuleId, body: ErrorRuleInput, request: Request, context: WriteContext,
                      controls: Controls, if_match: IfMatch = None):
    snap = controls.error_rules.update_rule(context, rule_id, body.engine_payload(), expected_revision=if_match)
    return DataEnvelope(data=_data(snap, controls), meta=response_meta(request))


@_route("/error-rules/rules/{ruleId}", "delete", "deleteErrorRule")
def delete_error_rule(rule_id: RuleId, request: Request, context: DestroyContext, controls: Controls,
                      if_match: IfMatch = None):
    snap = controls.error_rules.delete_rule(context, rule_id, expected_revision=if_match)
    return DataEnvelope(data=_data(snap, controls), meta=response_meta(request))


@_route("/error-rules/invalid-rules/{key}", "delete", "deleteInvalidErrorRule")
def delete_invalid_error_rule(key: InvalidRuleKey, request: Request, context: DestroyContext, controls: Controls,
                              if_match: IfMatch = None):
    snap = controls.error_rules.delete_invalid_rule(context, key, expected_revision=if_match)
    return DataEnvelope(data=_data(snap, controls), meta=response_meta(request))


@_route("/error-rules/order", "put", "reorderErrorRules")
def reorder_error_rules(body: ErrorRulesReorderRequest, request: Request, context: WriteContext,
                        controls: Controls, if_match: IfMatch = None):
    snap = controls.error_rules.reorder_rules(context, body.ruleIds, expected_revision=if_match)
    return DataEnvelope(data=_data(snap, controls), meta=response_meta(request))


@_route("/error-rules/presets/{ruleId}", "patch", "updateErrorRulePreset")
def update_error_rule_preset(rule_id: RuleId, body: ErrorRulePresetPatch, request: Request,
                             context: WriteContext, controls: Controls, if_match: IfMatch = None):
    snap = controls.error_rules.set_preset_enabled(context, rule_id, enabled=body.enabled,
                                                   expected_revision=if_match)
    return DataEnvelope(data=_data(snap, controls), meta=response_meta(request))


@_route("/error-rules/test", "post", "testErrorRules", responses=_READ_RESPONSES,
        model=DataEnvelope[ErrorRulesTestData])
def test_error_rules(body: ErrorRulesTestRequest, request: Request, context: ReadContext, controls: Controls):
    result = controls.error_rules.test(
        context, channel_key=body.channelKey, provider=body.provider, model=body.model, phase=body.phase,
        status=body.status, body=body.body,
        draft_rule=body.draftRule.engine_payload() if body.draftRule is not None else None,
    )
    return DataEnvelope(data=ErrorRulesTestData.model_validate(result), meta=response_meta(request))


@router.get(
    "/error-rules/recent",
    operation_id="getRecentUpstreamErrors",
    dependencies=[Depends(reject_unknown_query("hours"))],
    tags=["management-system"],
    response_model=DataEnvelope[ErrorRulesRecentData],
    responses=_READ_RESPONSES,
)
def get_recent_upstream_errors(request: Request, context: ReadContext, controls: Controls,
                               hours: Annotated[int, Query(ge=1, le=168)] = 24):
    result = controls.error_rules.recent_errors(context, hours=hours)
    return DataEnvelope(data=ErrorRulesRecentData.model_validate(result), meta=response_meta(request))
