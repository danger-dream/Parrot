"""Qoder HTTP adapters; shared controls own all account/action behavior."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, Header, Path, Request
from starlette.concurrency import run_in_threadpool

from src.management_auth import Capability
from src.management_control import ManagementContext, ManagementErrorCode
from src.management_control.oauth import OAuthControl
from src.management_control.oauth.upstream import call_upstream
from src.management_control.oauth.contracts import public_value

from ..dependencies import require_capability
from ..schemas.base import DataEnvelope
from ..schemas.oauth import OAuthLoginFlowData
from ..schemas.oauth_qoder import QoderCheckinData, QoderSettingsData, QoderSettingsRequest, QoderViewData, QoderDeviceRequest, QoderDeviceRegenerateRequest
from .oauth_support import StrictOAuthQueryRoute, get_oauth_control_dependency, meta, responses

router = APIRouter(route_class=StrictOAuthQueryRoute)
ReadContext = Annotated[ManagementContext, Depends(require_capability(Capability.READ))]
WriteContext = Annotated[ManagementContext, Depends(require_capability(Capability.WRITE))]
SecretContext = Annotated[ManagementContext, Depends(require_capability(Capability.SECRETS_WRITE))]
Control = Annotated[OAuthControl, Depends(get_oauth_control_dependency)]
AccountId = Annotated[str, Path(min_length=1, max_length=1000)]

VIEW_EXAMPLE = {"realm": "global", "uid": "fixture", "email": "q@example.com", "nickname": None, "label": "q@example.com",
                "route": "Qoder 国际 · qoder.com", "authMethod": "device", "deviceChat": False, "reloginRequired": False,
                "autoCheckin": False, "checkinSupported": True, "checkinUnsupportedReason": None, "checkin": {}, "usage": {}}


def public(value):
    return public_value(value, camel_case_keys=True)


def flow_data(flow):
    return OAuthLoginFlowData(flowId=flow.flow_id, flowSecret=flow.flow_secret, provider=flow.provider,
                             authUrl=flow.auth_url, instruction=flow.instruction, expiresAt=flow.expires_at,
                             devicePreview=flow.device_preview)


@router.post("/oauth/login-flows/{flowId}/qoder-device/regenerate", operation_id="regenerateOAuthQoderDevice",
             response_model=DataEnvelope[OAuthLoginFlowData], responses=responses(200, {}))
def regenerate_device(flowId: Annotated[str, Path(min_length=1, max_length=200)],
                      body: QoderDeviceRegenerateRequest, request: Request, context: SecretContext, control: Control):
    result = call_upstream(control.regenerate_qoder_device, context, flowId, body.flowSecret.get_secret_value(), body.revision,
                                             platform=body.platform)
    return DataEnvelope(data=flow_data(result), meta=meta(request))


@router.post("/oauth/login-flows/{flowId}/qoder-device/confirm", operation_id="confirmOAuthQoderDevice",
             response_model=DataEnvelope[OAuthLoginFlowData], responses=responses(200, {}))
async def confirm_device(flowId: Annotated[str, Path(min_length=1, max_length=200)],
                         body: QoderDeviceRequest, request: Request, context: SecretContext, control: Control):
    result = await run_in_threadpool(call_upstream, control.confirm_qoder_device, context, flowId, body.flowSecret.get_secret_value(),
                                     body.revision, pat=body.pat.get_secret_value() if body.pat else None)
    return DataEnvelope(data=flow_data(result), meta=meta(request))


@router.get("/oauth/accounts/{accountId}/qoder", operation_id="getOAuthQoderAccount",
    response_model=DataEnvelope[QoderViewData], responses=responses(200, VIEW_EXAMPLE))
def account_view(accountId: AccountId, request: Request, context: ReadContext, control: Control):
    return DataEnvelope(data=QoderViewData(**public(control.get_qoder(context, accountId))), meta=meta(request))


@router.post("/oauth/accounts/{accountId}/qoder/actions/refresh-status", operation_id="refreshOAuthQoderStatus",
    response_model=DataEnvelope[QoderViewData], responses=responses(200, VIEW_EXAMPLE, ManagementErrorCode.UPSTREAM_ERROR))
async def refresh_status(accountId: AccountId, request: Request, context: WriteContext, control: Control):
    value = await run_in_threadpool(call_upstream, control.refresh_qoder_status_now, context, accountId)
    return DataEnvelope(data=QoderViewData(**public(value)), meta=meta(request))


@router.post("/oauth/accounts/{accountId}/qoder/actions/checkin", operation_id="checkinOAuthQoderAccount",
    response_model=DataEnvelope[QoderCheckinData],
    responses=responses(200, {"businessDate": "2026-10-20", "status": "claimed", "source": "manual", "at": 1792500000.0, "realm": "global", "credit": 100},
                        ManagementErrorCode.INVALID_OPERATION_STATE))
async def checkin(accountId: AccountId, request: Request, context: WriteContext, control: Control):
    value = await run_in_threadpool(call_upstream, control.checkin_qoder_now, context, accountId)
    return DataEnvelope(data=QoderCheckinData(**public(value)), meta=meta(request))


@router.patch("/oauth/accounts/{accountId}/qoder/settings", operation_id="updateOAuthQoderSettings",
    response_model=DataEnvelope[QoderSettingsData],
    responses=responses(200, {"autoCheckin": True, "timezone": "Asia/Shanghai", "revision": "fixture"}, ManagementErrorCode.REVISION_CONFLICT))
def settings(accountId: AccountId, body: Annotated[QoderSettingsRequest, Body()], request: Request,
             context: WriteContext, control: Control,
             if_match: Annotated[str | None, Header(alias="If-Match")] = None):
    data = control.update_qoder_settings(context, accountId, auto_checkin=body.autoCheckin, expected_revision=if_match)
    return DataEnvelope(data=QoderSettingsData(**public(data)), meta=meta(request))
