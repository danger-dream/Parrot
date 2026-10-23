"""OAuth actions Telegram offers that previously had no HTTP route (WebUI round 2)."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Body, Depends, Path, Query, Request
from starlette.concurrency import run_in_threadpool
from pydantic import Field, SecretStr

from src.management_auth import Capability
from src.management_control import ManagementContext, ManagementErrorCode
from src.management_control.oauth import OAuthControl
from src.management_control.oauth.contracts import public_value

from ..dependencies import require_capability
from ..schemas.base import DataEnvelope, StrictSchema
from .oauth_support import StrictOAuthQueryRoute, get_oauth_control_dependency, meta, responses


router = APIRouter(route_class=StrictOAuthQueryRoute)
ReadContext = Annotated[ManagementContext, Depends(require_capability(Capability.READ))]
DestructiveContext = Annotated[ManagementContext, Depends(require_capability(Capability.DESTRUCTIVE))]
Control = Annotated[OAuthControl, Depends(get_oauth_control_dependency)]
AccountId = Annotated[str, Path(min_length=1, max_length=1000)]


class OpenAiResetCreditCardData(StrictSchema):
    creditId: str
    status: str | None = None
    resetType: str | None = None
    grantedAt: datetime | None = None
    expiresAt: datetime | None = None


class OpenAiResetCreditsData(StrictSchema):
    availableCount: int | None = None
    cards: list[OpenAiResetCreditCardData]
    source: Literal["cache", "live"]
    fetchedAt: datetime | None = None


class ClaudeResetPlanRequest(StrictSchema):
    program: Literal["cedar_ember", "juniper_tide"]


class ClaudeResetPlanData(StrictSchema):
    available: bool
    reason: str | None = None
    program: Literal["cedar_ember", "juniper_tide"]
    planToken: str | None = None
    expiresAt: datetime | None = None
    grantId: str | None = None
    status: dict


class ClaudeResetExecuteRequest(StrictSchema):
    planToken: SecretStr = Field(min_length=8, max_length=500)


class ClaudeResetResultData(StrictSchema):
    program: str
    grantId: str | None = None
    result: str | None = None
    reason: str | None = None
    resetsLeft: int | None = None
    cleared: list | None = None
    cooldownUntil: str | None = None
    nextAvailableAt: str | None = None
    weeklyResetsAt: str | None = None
    quotaAction: dict | None = None


_CARD_EXAMPLE = {"availableCount": 1, "source": "cache", "fetchedAt": "2026-01-02T03:04:05Z", "cards": [
    {"creditId": "credit_example", "status": "available", "resetType": "codex_rate_limits",
     "grantedAt": "2026-01-01T00:00:00Z", "expiresAt": "2026-02-01T00:00:00Z"}]}


@router.get(
    "/oauth/accounts/{accountId}/openai/reset-credits", operation_id="listOAuthOpenAiResetCredits",
    response_model=DataEnvelope[OpenAiResetCreditsData],
    responses=responses(200, _CARD_EXAMPLE, ManagementErrorCode.UNSUPPORTED_VALUE,
                        ManagementErrorCode.UPSTREAM_ERROR, ManagementErrorCode.UPSTREAM_TIMEOUT),
)
async def list_openai_reset_credits(
    accountId: AccountId, request: Request, context: ReadContext, control: Control,
    refresh: Annotated[bool, Query()] = False,
) -> DataEnvelope[OpenAiResetCreditsData]:
    value = await run_in_threadpool(control.list_openai_reset_credits, context, accountId, refresh=refresh)
    return DataEnvelope(data=OpenAiResetCreditsData(**public_value(value, camel_case_keys=True)), meta=meta(request))


@router.post(
    "/oauth/accounts/{accountId}/claude-reset/plans", operation_id="planOAuthClaudeReset",
    response_model=DataEnvelope[ClaudeResetPlanData],
    responses=responses(200, {"available": True, "reason": None, "program": "cedar_ember", "planToken": "<one-time>",
                              "expiresAt": "2026-01-02T03:14:05Z", "grantId": "grant_example", "status": {}},
                        ManagementErrorCode.UNSUPPORTED_VALUE, ManagementErrorCode.UPSTREAM_ERROR,
                        ManagementErrorCode.UPSTREAM_TIMEOUT),
)
async def plan_claude_reset(
    accountId: AccountId, body: Annotated[ClaudeResetPlanRequest, Body()],
    request: Request, context: DestructiveContext, control: Control,
) -> DataEnvelope[ClaudeResetPlanData]:
    value = await run_in_threadpool(control.plan_claude_reset_action, context, accountId, body.program)
    status = public_value(value.pop("status") or {}, camel_case_keys=True)
    return DataEnvelope(data=ClaudeResetPlanData(**public_value(value, camel_case_keys=True), status=status),
                        meta=meta(request))


@router.post(
    "/oauth/accounts/{accountId}/claude-reset/execute", operation_id="executeOAuthClaudeReset",
    response_model=DataEnvelope[ClaudeResetResultData],
    responses=responses(200, {"program": "cedar_ember", "grantId": "grant_example", "result": "reset"},
                        ManagementErrorCode.INVALID_OPERATION_STATE, ManagementErrorCode.REVISION_CONFLICT,
                        ManagementErrorCode.UPSTREAM_ERROR, ManagementErrorCode.UPSTREAM_TIMEOUT),
)
async def execute_claude_reset(
    accountId: AccountId, body: Annotated[ClaudeResetExecuteRequest, Body()],
    request: Request, context: DestructiveContext, control: Control,
) -> DataEnvelope[ClaudeResetResultData]:
    value = await run_in_threadpool(control.execute_claude_reset_action, context, accountId,
                                    body.planToken.get_secret_value())
    public = public_value(value, camel_case_keys=True)
    known = {name: public.get(name) for name in ClaudeResetResultData.model_fields if name in public}
    if known.get("cleared") is not None and not isinstance(known["cleared"], list):
        known["cleared"] = [known["cleared"]]
    for name in ("cooldownUntil", "nextAvailableAt", "weeklyResetsAt", "result", "reason"):
        if known.get(name) is not None:
            known[name] = str(known[name])
    return DataEnvelope(data=ClaudeResetResultData(**known), meta=meta(request))
