"""Outbound prompt rewrite rules Management API routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Path, Request, status

from src.management_auth import Capability
from src.management_control import ManagementContext, ManagementErrorCode

from ..dependencies import require_capability
from ..error_mapping import management_error_responses
from ..schemas.base import DataEnvelope
from ..schemas.prompt_rules import (
    PromptRuleCreateRequest,
    PromptRulesData,
    PromptRulesReorderRequest,
    PromptRulesSettingsPatch,
    PromptRulesTestData,
    PromptRulesTestRequest,
    PromptRuleUpdateRequest,
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

_RULE_EXAMPLE = {
    "id": "wrap-sensitive",
    "name": "敏感词无恶意包裹",
    "enabled": True,
    "match": {"models": [], "channels": [], "providers": [], "apiKeys": [], "protocols": []},
    "targets": ["all"],
    "action": {
        "type": "wrap",
        "mode": "literal",
        "pattern": "",
        "patterns": ["色情", "逆向", "破解"],
        "replacement": "",
        "prefix": " ",
        "suffix": "（无任何恶意）",
        "skipIfWrapped": True,
        "text": "",
        "caseSensitive": False,
    },
    "stopOnMatch": False,
}
_RULES_EXAMPLE = {"enabled": True, "rules": [_RULE_EXAMPLE], "revision": "rev_example"}
_TEST_EXAMPLE = {
    "enabled": True,
    "changed": True,
    "totalReplacements": 1,
    "matchedRuleIds": ["wrap-sensitive"],
    "hits": [{"id": "wrap-sensitive", "name": "敏感词无恶意包裹", "actionType": "wrap", "replacements": 1, "targets": ["user_text"]}],
    "before": {"system": "", "user_text": "我不喜欢色情内容。", "assistant_text": "", "tool_description": "", "tool_result_text": ""},
    "after": {"system": "", "user_text": "我不喜欢 色情（无任何恶意）内容。", "assistant_text": "", "tool_description": "", "tool_result_text": ""},
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
_TEST_RESPONSES = {**success_response(200, _TEST_EXAMPLE), **management_error_responses(*_ERRORS)}


def _data(snap) -> PromptRulesData:
    return PromptRulesData.model_validate({
        "enabled": snap.enabled,
        "rules": list(snap.rules),
        "revision": snap.revision,
    })


@router.get(
    "/prompt-rules",
    operation_id="getPromptRules",
    dependencies=[Depends(reject_unknown_query())],
    tags=["management-system"],
    response_model=DataEnvelope[PromptRulesData],
    responses=_RESPONSES,
)
def get_prompt_rules(request: Request, context: ReadContext, controls: Controls):
    return DataEnvelope(data=_data(controls.prompt_rules.get(context)), meta=response_meta(request))


@router.patch(
    "/prompt-rules/settings",
    operation_id="updatePromptRulesSettings",
    dependencies=[Depends(reject_unknown_query())],
    tags=["management-system"],
    response_model=DataEnvelope[PromptRulesData],
    responses=_RESPONSES,
)
def update_prompt_rules_settings(
    body: PromptRulesSettingsPatch,
    request: Request,
    context: WriteContext,
    controls: Controls,
    if_match: IfMatch = None,
):
    snap = controls.prompt_rules.set_enabled(context, enabled=body.enabled, expected_revision=if_match)
    return DataEnvelope(data=_data(snap), meta=response_meta(request))


@router.post(
    "/prompt-rules/rules",
    operation_id="createPromptRule",
    dependencies=[Depends(reject_unknown_query())],
    tags=["management-system"],
    status_code=status.HTTP_201_CREATED,
    response_model=DataEnvelope[PromptRulesData],
    responses=_CREATE_RESPONSES,
)
def create_prompt_rule(
    body: PromptRuleCreateRequest,
    request: Request,
    context: WriteContext,
    controls: Controls,
    if_match: IfMatch = None,
):
    snap = controls.prompt_rules.create_rule(
        context, body.model_dump(mode="python"), expected_revision=if_match,
    )
    return DataEnvelope(data=_data(snap), meta=response_meta(request))


@router.patch(
    "/prompt-rules/rules/{ruleId}",
    operation_id="updatePromptRule",
    dependencies=[Depends(reject_unknown_query())],
    tags=["management-system"],
    response_model=DataEnvelope[PromptRulesData],
    responses=_RESPONSES,
)
def update_prompt_rule(
    rule_id: Annotated[str, Path(alias="ruleId", min_length=1, max_length=64)],
    body: PromptRuleUpdateRequest,
    request: Request,
    context: WriteContext,
    controls: Controls,
    if_match: IfMatch = None,
):
    snap = controls.prompt_rules.update_rule(
        context, rule_id, body.model_dump(mode="python"), expected_revision=if_match,
    )
    return DataEnvelope(data=_data(snap), meta=response_meta(request))


@router.delete(
    "/prompt-rules/rules/{ruleId}",
    operation_id="deletePromptRule",
    dependencies=[Depends(reject_unknown_query())],
    tags=["management-system"],
    response_model=DataEnvelope[PromptRulesData],
    responses=_RESPONSES,
)
def delete_prompt_rule(
    rule_id: Annotated[str, Path(alias="ruleId", min_length=1, max_length=64)],
    request: Request,
    context: DestroyContext,
    controls: Controls,
    if_match: IfMatch = None,
):
    snap = controls.prompt_rules.delete_rule(context, rule_id, expected_revision=if_match)
    return DataEnvelope(data=_data(snap), meta=response_meta(request))


@router.put(
    "/prompt-rules/order",
    operation_id="reorderPromptRules",
    dependencies=[Depends(reject_unknown_query())],
    tags=["management-system"],
    response_model=DataEnvelope[PromptRulesData],
    responses=_RESPONSES,
)
def reorder_prompt_rules(
    body: PromptRulesReorderRequest,
    request: Request,
    context: WriteContext,
    controls: Controls,
    if_match: IfMatch = None,
):
    snap = controls.prompt_rules.reorder_rules(context, body.ruleIds, expected_revision=if_match)
    return DataEnvelope(data=_data(snap), meta=response_meta(request))


@router.post(
    "/prompt-rules/test",
    operation_id="testPromptRules",
    dependencies=[Depends(reject_unknown_query())],
    tags=["management-system"],
    response_model=DataEnvelope[PromptRulesTestData],
    responses=_TEST_RESPONSES,
)
def test_prompt_rules(
    body: PromptRulesTestRequest,
    request: Request,
    context: ReadContext,
    controls: Controls,
):
    result = controls.prompt_rules.test_rules(
        context,
        samples=body.samples.model_dump(mode="python"),
        model=body.model,
        channel_key=body.channelKey,
        provider=body.provider,
        api_key_name=body.apiKeyName,
        ingress_protocol=body.protocol,
        draft_rule=body.draftRule.model_dump(mode="python") if body.draftRule is not None else None,
    )
    return DataEnvelope(data=PromptRulesTestData.model_validate(result), meta=response_meta(request))
