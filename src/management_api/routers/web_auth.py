"""WebUI auth additions: public login options and the signed-in device list."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request, Response, status

from src.management_auth import Capability
from src.management_control import ManagementContext, ManagementError, ManagementErrorCode

from ..dependencies import (
    AuthenticatedSession,
    ManagementRuntime,
    get_authenticated_session,
    get_management_control_owner,
    get_management_runtime,
    management_request_id,
    require_capability,
)
from ..error_mapping import management_error_responses
from ..response_helpers import response_meta as _meta
from ..schemas.auth import (
    LoginOptionsData,
    ManagementKeyLoginOption,
    RevokeOtherSessionsData,
    SessionDeviceData,
    TelegramApprovalLoginOption,
)
from ..schemas.base import DataEnvelope
from ..session_cookie import clear_session_cookie, has_session_cookie
from ._strict_query import reject_unknown_query_parameters


router = APIRouter(tags=["management-auth"])

_SESSION_ERRORS = (
    ManagementErrorCode.SESSION_REQUIRED,
    ManagementErrorCode.SESSION_EXPIRED,
    ManagementErrorCode.ORIGIN_DENIED,
    ManagementErrorCode.CAPABILITY_DENIED,
    ManagementErrorCode.VALIDATION_FAILED,
    ManagementErrorCode.SERVICE_NOT_READY,
)
_DEVICE_EXAMPLE = {
    "sessionId": "ms_example",
    "authMethod": "telegramApproval",
    "clientName": "Parrot WebUI",
    "deviceSummary": "Chrome on macOS",
    "sourceAddress": "203.0.113.7",
    "issuedAt": "2026-01-02T03:04:05Z",
    "lastSeenAt": "2026-01-02T03:09:05Z",
    "expiresAt": "2026-02-01T03:04:05Z",
    "idleExpiresAt": "2026-01-05T03:09:05Z",
    "current": True,
}


def _ok(http_status: int, data) -> dict[int, dict]:
    return {
        http_status: {
            "description": "Successful Response",
            "content": {"application/json": {"example": {
                "data": data, "meta": {"requestId": "request-example"},
            }}},
        }
    }


def _no_content(description: str) -> dict[int, dict]:
    return {
        204: {
            "description": description,
            "headers": {
                "X-Request-Id": {
                    "description": "Stable request correlation identifier",
                    "schema": {"type": "string"},
                    "example": "request-example",
                }
            },
        }
    }


@router.get(
    "/auth/login-options",
    operation_id="getManagementLoginOptions",
    response_model=DataEnvelope[LoginOptionsData],
    responses={
        **_ok(200, {
            "webUiEnabled": True,
            "telegramApproval": {"available": True, "ttlSeconds": 180},
            "managementKey": {"available": True},
        }),
        **management_error_responses(
            ManagementErrorCode.ORIGIN_DENIED,
            ManagementErrorCode.VALIDATION_FAILED,
            ManagementErrorCode.SERVICE_NOT_READY,
        ),
    },
)
def get_management_login_options(
    request: Request,
    response: Response,
    runtime: Annotated[ManagementRuntime, Depends(get_management_runtime)],
) -> DataEnvelope[LoginOptionsData]:
    """Unauthenticated, non-secret facts the login page needs before sign-in."""
    reject_unknown_query_parameters(request)
    approvals = runtime.approvals
    web_ui_enabled = get_management_control_owner(runtime).system.settings.web_ui_enabled()
    response.headers["Cache-Control"] = "no-store"
    return DataEnvelope(
        data=LoginOptionsData(
            webUiEnabled=web_ui_enabled,
            telegramApproval=TelegramApprovalLoginOption(
                available=approvals.available(),
                ttlSeconds=approvals.ttl_seconds,
            ),
            # The fixed management key is mandatory for the control plane; when
            # the runtime is up, key exchange is always possible.
            managementKey=ManagementKeyLoginOption(available=True),
        ),
        meta=_meta(request),
    )


@router.get(
    "/auth/sessions",
    operation_id="listManagementSessions",
    response_model=DataEnvelope[list[SessionDeviceData]],
    responses={**_ok(200, [_DEVICE_EXAMPLE]), **management_error_responses(*_SESSION_ERRORS)},
)
def list_management_sessions(
    request: Request,
    response: Response,
    context: Annotated[ManagementContext, Depends(require_capability(Capability.READ))],
    authenticated: Annotated[AuthenticatedSession, Depends(get_authenticated_session)],
    runtime: Annotated[ManagementRuntime, Depends(get_management_runtime)],
) -> DataEnvelope[list[SessionDeviceData]]:
    reject_unknown_query_parameters(request)
    current = authenticated.verified.principal.session_id
    devices = runtime.sessions.list_devices(
        context.actor, request_id=management_request_id(request),
    )
    response.headers["Cache-Control"] = "no-store"
    return DataEnvelope(
        data=[
            SessionDeviceData(
                sessionId=item.session_id,
                authMethod=item.auth_method,
                clientName=item.client_name,
                deviceSummary=item.device_summary,
                sourceAddress=item.source_address,
                issuedAt=item.issued_at,
                lastSeenAt=item.last_seen_at,
                expiresAt=item.expires_at,
                idleExpiresAt=item.idle_expires_at,
                current=item.session_id == current,
            )
            for item in devices
        ],
        meta=_meta(request),
    )


@router.delete(
    "/auth/sessions/{sessionId}",
    operation_id="revokeManagementSession",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        **_no_content("Management session revoked"),
        **management_error_responses(*_SESSION_ERRORS, ManagementErrorCode.RESOURCE_NOT_FOUND),
    },
)
def revoke_management_session(
    request: Request,
    session_id: Annotated[str, Path(alias="sessionId", min_length=4, max_length=128)],
    context: Annotated[ManagementContext, Depends(require_capability(Capability.WRITE))],
    authenticated: Annotated[AuthenticatedSession, Depends(get_authenticated_session)],
    runtime: Annotated[ManagementRuntime, Depends(get_management_runtime)],
) -> Response:
    reject_unknown_query_parameters(request)
    revoked = runtime.sessions.revoke_session(
        context.actor, session_id, request_id=management_request_id(request),
    )
    if not revoked:
        raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
    response = Response(
        status_code=status.HTTP_204_NO_CONTENT, headers={"Cache-Control": "no-store"},
    )
    if (
        session_id == authenticated.verified.principal.session_id
        and has_session_cookie(request)
    ):
        clear_session_cookie(request, response)
    return response


@router.post(
    "/auth/sessions/actions/revoke-others",
    operation_id="revokeOtherManagementSessions",
    response_model=DataEnvelope[RevokeOtherSessionsData],
    responses={**_ok(200, {"revoked": 2}), **management_error_responses(*_SESSION_ERRORS)},
)
def revoke_other_management_sessions(
    request: Request,
    response: Response,
    context: Annotated[ManagementContext, Depends(require_capability(Capability.WRITE))],
    runtime: Annotated[ManagementRuntime, Depends(get_management_runtime)],
) -> DataEnvelope[RevokeOtherSessionsData]:
    reject_unknown_query_parameters(request)
    count = runtime.sessions.revoke_others(
        context.actor, request_id=management_request_id(request),
    )
    response.headers["Cache-Control"] = "no-store"
    return DataEnvelope(data=RevokeOtherSessionsData(revoked=count), meta=_meta(request))
