"""HTTP adapter for the same confirmed controls used by Telegram."""
from __future__ import annotations

import asyncio
from typing import Annotated
from fastapi import APIRouter, Body, Depends, Path, Request, Response
from src.management_auth import Capability
from src.management_control import ManagementContext
from src.management_control.oauth import OAuthControl
from src.management_control.oauth.contracts import public_value
from ..dependencies import require_capability
from ..schemas.base import DataEnvelope
from ..schemas.oauth import CommitPlanRequest, OAuthMutationData, OAuthLoginFlowData
from ..schemas.oauth_zhipu import ZhipuProjectRequest, ZhipuPlanRequest, ZhipuPlanData, ZhipuViewData, ZhipuActionData
from .oauth_support import StrictOAuthQueryRoute, get_oauth_control_dependency, responses, meta

router = APIRouter(route_class=StrictOAuthQueryRoute)
ReadContext = Annotated[ManagementContext, Depends(require_capability(Capability.READ))]
WriteContext = Annotated[ManagementContext, Depends(require_capability(Capability.WRITE))]
SecretContext = Annotated[ManagementContext, Depends(require_capability(Capability.SECRETS_WRITE))]
Control = Annotated[OAuthControl, Depends(get_oauth_control_dependency)]
AccountId = Annotated[str, Path(min_length=1, max_length=1000)]


@router.get("/oauth/accounts/{accountId}/zhipu", operation_id="getOAuthZhipuAccount",
            response_model=DataEnvelope[ZhipuViewData], responses=responses(200, {"snapshot": {}, "actions": []}))
def view(accountId: AccountId, request: Request, context: ReadContext, control: Control):
    return DataEnvelope(data=ZhipuViewData(**public_value(control.get_zhipu(context, accountId), camel_case_keys=True)), meta=meta(request))


@router.get("/oauth/accounts/{accountId}/zhipu/reset-status", operation_id="getOAuthZhipuResetStatus",
            response_model=DataEnvelope[ZhipuViewData], responses=responses(200, {"snapshot": {}, "actions": [], "reset": {}}))
async def reset_status(accountId: AccountId, request: Request, context: ReadContext, control: Control):
    value = await asyncio.to_thread(control.get_zhipu, context, accountId, reset_status=True)
    return DataEnvelope(data=ZhipuViewData(**public_value(value, camel_case_keys=True)), meta=meta(request))


@router.post("/oauth/accounts/{accountId}/zhipu/project-flows", operation_id="startOAuthZhipuProjectSelection",
             response_model=DataEnvelope[OAuthLoginFlowData], responses=responses(200, {"flowId": "zhflow_example", "flowSecret": "fixture", "provider": "zhipu", "authUrl": None, "instruction": None, "expiresAt": "2026-09-23T10:00:00Z"}))
async def start_projects(accountId: AccountId, request: Request, response: Response, context: SecretContext, control: Control):
    result = await asyncio.to_thread(control.start_zhipu_project_selection, context, accountId)
    response.headers["Cache-Control"] = "no-store"
    return DataEnvelope(data=OAuthLoginFlowData(flowId=result.flow_id, flowSecret=result.flow_secret,
        provider=result.provider, authUrl=None, instruction=result.instruction, expiresAt=result.expires_at), meta=meta(request))


@router.post("/oauth/login-flows/{flowId}/zhipu/project", operation_id="selectOAuthZhipuProject",
             response_model=DataEnvelope[OAuthMutationData], responses=responses(200, {"accountId": "zhipu:fixture", "revision": "fixture", "status": "created"}))
async def select_project(flowId: str, body: Annotated[ZhipuProjectRequest, Body()], request: Request, context: SecretContext, control: Control):
    result = await asyncio.to_thread(control.select_zhipu_project, context, flowId, body.flowSecret.get_secret_value(),
                                    organization_id=body.organizationId, project_id=body.projectId)
    return DataEnvelope(data=OAuthMutationData(accountId=result.account_id, revision=result.revision, status=result.status), meta=meta(request))


@router.post("/oauth/accounts/{accountId}/zhipu/action-plans", operation_id="planOAuthZhipuAction",
             response_model=DataEnvelope[ZhipuPlanData], responses=responses(200, {"planToken": "zhaction_example.secret", "accountId": "zhipu:fixture", "action": "use", "expiresAt": "2026-09-23T10:00:00Z", "cardIndex": 0}))
async def plan_action(accountId: AccountId, body: Annotated[ZhipuPlanRequest, Body()], request: Request, response: Response, context: WriteContext, control: Control):
    result = await asyncio.to_thread(control.plan_zhipu_action, context, accountId, body.action, reset_type=body.resetType, card_index=body.cardIndex)
    safe = public_value({k: v for k, v in result.items() if k != "plan_token"}, camel_case_keys=True)
    safe["planToken"] = result["plan_token"]
    response.headers["Cache-Control"] = "no-store"
    return DataEnvelope(data=ZhipuPlanData(**safe), meta=meta(request))


@router.post("/oauth/accounts/{accountId}/zhipu/actions/execute", operation_id="executeOAuthZhipuAction",
             response_model=DataEnvelope[ZhipuActionData], responses=responses(200, {"action": "use", "status": "unknown", "createdAt": 1}))
async def execute_action(accountId: AccountId, body: Annotated[CommitPlanRequest, Body()], request: Request, context: WriteContext, control: Control):
    result = await asyncio.to_thread(control.execute_zhipu_action_now, context, accountId, body.planToken.get_secret_value())
    return DataEnvelope(data=ZhipuActionData(**public_value(result, camel_case_keys=True)), meta=meta(request))
