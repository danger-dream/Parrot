"""WorkBuddy HTTP adapters; shared controls own all account/action behavior."""
from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Header, Path, Query, Request, Response

from src.management_auth import Capability
from src.management_control import ManagementContext, ManagementErrorCode
from src.management_control.oauth import OAuthControl
from src.management_control.oauth.contracts import public_value

from ..dependencies import ManagementRuntime, get_management_runtime, require_capability
from ..schemas.base import DataEnvelope
from ..schemas.oauth import CommitPlanRequest, OAuthOperationEnvelope
from ..schemas.oauth_workbuddy import (WorkBuddyViewData, WorkBuddyPlanRequest, WorkBuddyPlanData,
    WorkBuddyRecordPageData, WorkBuddySettingsRequest, WorkBuddySettingsData, WorkBuddyPolicyData)
from .oauth_support import (StrictOAuthQueryRoute, get_oauth_control_dependency, responses, meta,
    operation_envelope, OPERATION_EXAMPLE)

router = APIRouter(route_class=StrictOAuthQueryRoute)
ReadContext = Annotated[ManagementContext, Depends(require_capability(Capability.READ))]
WriteContext = Annotated[ManagementContext, Depends(require_capability(Capability.WRITE))]
Control = Annotated[OAuthControl, Depends(get_oauth_control_dependency)]
Runtime = Annotated[ManagementRuntime, Depends(get_management_runtime)]
AccountId = Annotated[str, Path(min_length=1, max_length=1000)]


def public(value):
    return public_value(value, camel_case_keys=True)


@router.get("/oauth/accounts/{accountId}/workbuddy", operation_id="getOAuthWorkBuddyAccount",
    response_model=DataEnvelope[WorkBuddyViewData], responses=responses(200, {"snapshot": {}, "actions": []}))
def account_view(accountId: AccountId, request: Request, context: ReadContext, control: Control):
    return DataEnvelope(data=WorkBuddyViewData(**public(control.get_workbuddy(context, accountId))), meta=meta(request))


@router.post("/oauth/accounts/{accountId}/workbuddy/actions/refresh-status", operation_id="refreshOAuthWorkBuddyStatus",
    status_code=202, response_model=OAuthOperationEnvelope, responses=responses(202, OPERATION_EXAMPLE))
def refresh_status(accountId: AccountId, request: Request, context: WriteContext, control: Control, runtime: Runtime):
    return operation_envelope(control.refresh_workbuddy_status(context, accountId, runtime.operations), request)


@router.post("/oauth/accounts/{accountId}/workbuddy/action-plans", operation_id="planOAuthWorkBuddyAction",
    response_model=DataEnvelope[WorkBuddyPlanData], responses=responses(200, {"planToken": "wbaction_example.secret", "accountId": "workbuddy:cn:fixture:p", "action": "checkin", "businessDate": "2026-09-07", "expiresAt": "2026-09-07T10:00:00Z", "allowUnknown": False, "freeTrialConfirmed": False, "retryFailed": False}, ManagementErrorCode.INVALID_OPERATION_STATE))
async def plan_action(accountId: AccountId, body: Annotated[WorkBuddyPlanRequest, Body()], request: Request,
                      response: Response, context: WriteContext, control: Control):
    value = await asyncio.to_thread(control.plan_workbuddy_action, context, accountId, body.action.replace("-", "_"),
        allow_unknown=body.allowUnknown, free_trial_confirmed=body.freeTrialConfirmed, retry_failed=body.retryFailed)
    safe = public({key: item for key, item in value.items() if key != "plan_token"})
    safe["planToken"] = value["plan_token"]  # Explicit one-time capability response only.
    response.headers["Cache-Control"] = "no-store"
    return DataEnvelope(data=WorkBuddyPlanData(**safe), meta=meta(request))


@router.post("/oauth/accounts/{accountId}/workbuddy/actions/execute", operation_id="executeOAuthWorkBuddyAction",
    status_code=202, response_model=OAuthOperationEnvelope, responses=responses(202, OPERATION_EXAMPLE, ManagementErrorCode.REVISION_CONFLICT, ManagementErrorCode.INVALID_OPERATION_STATE))
def execute_action(accountId: AccountId, body: Annotated[CommitPlanRequest, Body()], request: Request,
                   context: WriteContext, control: Control, runtime: Runtime):
    return operation_envelope(control.execute_workbuddy_action(context, accountId, body.planToken.get_secret_value(), runtime.operations), request)


@router.get("/oauth/accounts/{accountId}/workbuddy/action-records", operation_id="listOAuthWorkBuddyActions",
    response_model=DataEnvelope[WorkBuddyRecordPageData], responses=responses(200, {"items": [], "page": 1, "pageSize": 20, "total": 0, "hasNext": False}))
def records(accountId: AccountId, request: Request, context: ReadContext, control: Control,
            page: Annotated[int, Query(ge=1)] = 1, pageSize: Annotated[int, Query(ge=1, le=100)] = 20):
    data = control.get_workbuddy_records(context, accountId, page=page, page_size=pageSize)
    return DataEnvelope(data=WorkBuddyRecordPageData(**public(data)), meta=meta(request))


@router.patch("/oauth/accounts/{accountId}/workbuddy/settings", operation_id="updateOAuthWorkBuddySettings",
    response_model=DataEnvelope[WorkBuddySettingsData], responses=responses(200, {"autoCheckin": False, "timezone": "Asia/Shanghai", "scheduledTime": "09:05", "scheduledTimes": ["09:05", "21:05"], "effectsEnabled": False, "revision": "fixture"}, ManagementErrorCode.REVISION_CONFLICT))
def settings(accountId: AccountId, body: Annotated[WorkBuddySettingsRequest, Body()], request: Request,
             context: WriteContext, control: Control,
             if_match: Annotated[str | None, Header(alias="If-Match")] = None):
    data = control.update_workbuddy_settings(context, accountId, auto_checkin=body.autoCheckin, expected_revision=if_match)
    return DataEnvelope(data=WorkBuddySettingsData(**public(data)), meta=meta(request))


@router.get("/oauth/workbuddy/settings", operation_id="getOAuthWorkBuddyPolicy",
    response_model=DataEnvelope[WorkBuddyPolicyData], responses=responses(200, {"clientProfile": "cli", "clientProfilesByRealm": {"cn": "cli", "global": "workbuddy-cli"}, "supportedClientProfilesByRealm": {"cn": ["cli"], "global": ["workbuddy-cli", "ide"]}, "clientRoutes": {"workbuddy-cli": "WorkBuddy CLI · www.workbuddy.ai", "ide": "CodeBuddy IDE · www.codebuddy.ai"}, "browserLoginRealms": ["cn", "global"], "importRealms": [], "effectsEnabled": False, "autoCheckinDefault": False, "autoCheckinTime": "09:05", "autoCheckinTimes": ["09:05", "21:05"], "timezone": "Asia/Shanghai", "autoTrial": False}))
def policy(request: Request, context: ReadContext, control: Control):
    return DataEnvelope(data=WorkBuddyPolicyData(**public(control.workbuddy_policy(context))), meta=meta(request))
