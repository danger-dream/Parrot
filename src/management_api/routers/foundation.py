"""P0 auth, discovery and Operation routes for Management API v1."""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Header, Path, Request, Response, status

from src.management_auth import (
    ApprovalError,
    AuthenticationRateLimited,
    Capability,
    SessionAuthenticationError,
)
from src.management_control import ManagementContext, ManagementError, ManagementErrorCode

from ._operations import operation_data as _operation_data
from ._strict_query import reject_unknown_query_parameters
from ..dependencies import (
    AuthenticatedSession,
    ManagementRuntime,
    get_authenticated_session,
    get_management_context,
    get_management_runtime,
    management_request_id,
    require_capability,
)
from ..discovery import build_discovery_snapshot, supported_capabilities
from ..error_mapping import management_error_responses
from ..response_helpers import response_meta as _meta
from ..session_cookie import clear_session_cookie, has_session_cookie, set_session_cookie
from ..schemas import (
    DataEnvelope,
    ManagementCapabilitiesData,
    ManagementKeyGrant,
    ManagementMetadataData,
    ManagementOperationData,
    SessionCredentialData,
    SessionGrant,
    SessionSummary,
    TelegramApprovalCreateRequest,
    TelegramApprovalCreatedData,
    TelegramApprovalStatusData,
)


router = APIRouter()

_SESSION_GRANT_EXAMPLES = {
    "managementKey": {
        "summary": "Exchange a configured management key",
        "value": {"grantType": "managementKey", "managementKey": "<write-only>"},
    },
    "browserCookie": {
        "summary": "Browser login that stores the session in an HttpOnly cookie",
        "value": {
            "grantType": "managementKey",
            "managementKey": "<write-only>",
            "sessionTransport": "cookie",
            "clientName": "Parrot WebUI",
            "deviceSummary": "Chrome on macOS",
        },
    },
    "telegramApproval": {
        "summary": "Exchange an approved Telegram challenge",
        "value": {
            "grantType": "telegramApproval",
            "approvalId": "approval-example",
            "exchangeSecret": "<write-only>",
        },
    },
}
_APPROVAL_CREATE_EXAMPLES = {
    "browser": {
        "summary": "Request browser login approval",
        "value": {"clientName": "admin-console", "deviceSummary": "desktop-browser"},
    }
}
_SESSION_EXAMPLE = {
    "data": {
        "sessionId": "session-example",
        "subjectId": "administrator",
        "authMethod": "managementKey",
        "roles": ["administrator"],
        "capabilities": ["management.read", "management.write"],
        "issuedAt": "2026-01-02T03:04:05Z",
        "expiresAt": "2026-02-01T03:04:05Z",
        "idleExpiresAt": "2026-01-05T03:04:05Z",
    },
    "meta": {"requestId": "request-example"},
}
_SESSION_CREATED_EXAMPLE = {
    "data": {
        "credential": "<one-time-credential>",
        "session": _SESSION_EXAMPLE["data"],
    },
    "meta": {"requestId": "request-example"},
}
_APPROVAL_CREATED_EXAMPLE = {
    "data": {
        "approvalId": "approval-example",
        "exchangeSecret": "<one-time-exchange-secret>",
        "expiresAt": "2026-01-02T03:07:05Z",
        "pollAfterSeconds": 2,
        "confirmCode": "7KQ2",
    },
    "meta": {"requestId": "request-example"},
}
_APPROVAL_STATUS_EXAMPLE = {
    "data": {
        "approvalId": "approval-example",
        "status": "pending",
        "expiresAt": "2026-01-02T03:07:05Z",
        "pollAfterSeconds": 2,
    },
    "meta": {"requestId": "request-example"},
}
_METADATA_EXAMPLE = {
    "data": {
        "apiVersion": "v1",
        "applicationVersion": "1.0.0",
        "features": [{"id": "management-auth", "actionCount": 5}],
        "supportedCapabilities": ["management.read", "management.write"],
        "principalCapabilities": ["management.read", "management.write"],
        "enums": [
            {
                "name": "AuthMethod",
                "values": ["managementKey", "telegramApproval", "telegramAdmin"],
            },
            {
                "name": "authMethod",
                "values": ["managementKey", "telegramApproval", "telegramAdmin"],
            },
        ],
        "documentationUrl": "/docs",
    },
    "meta": {"requestId": "request-example"},
}
_CAPABILITIES_EXAMPLE = {
    "data": {
        "supportedCapabilities": ["management.read", "management.write"],
        "principalCapabilities": ["management.read", "management.write"],
        "domains": [
            {
                "domain": "management-auth",
                "capabilities": ["management.read", "management.write"],
                "actions": ["createManagementSession"],
                "actionDetails": [
                    {
                        "operationId": "createManagementSession",
                        "method": "POST",
                        "path": "/api/management/v1/auth/sessions",
                    }
                ],
                "providers": [],
                "presets": [],
                "protocols": [],
                "modes": [],
                "features": [],
            }
        ],
    },
    "meta": {"requestId": "request-example"},
}
_OPERATION_EXAMPLE = {
    "data": {
        "id": "operation-example",
        "kind": "domain.refresh",
        "status": "queued",
        "progress": None,
        "createdAt": "2026-01-02T03:04:05Z",
        "startedAt": None,
        "finishedAt": None,
        "result": None,
        "error": None,
        "cancellable": True,
    },
    "meta": {"requestId": "request-example"},
}


def _success_response(http_status: int, example: dict) -> dict[int, dict]:
    return {
        http_status: {
            "description": "Successful Response",
            "content": {"application/json": {"example": example}},
        }
    }


def _no_content_response(description: str) -> dict[int, dict]:
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


def _session_summary(issued_or_verified) -> SessionSummary:
    principal = issued_or_verified.principal
    return SessionSummary(
        sessionId=principal.session_id or "",
        subjectId=principal.subject_id,
        authMethod=principal.auth_method,
        roles=sorted(principal.roles, key=lambda item: item.value),
        capabilities=sorted(principal.capabilities, key=lambda item: item.value),
        issuedAt=principal.issued_at,
        expiresAt=issued_or_verified.expires_at,
        idleExpiresAt=issued_or_verified.idle_expires_at,
    )


@router.post(
    "/auth/sessions",
    operation_id="createManagementSession",
    tags=["management-auth"],
    status_code=status.HTTP_201_CREATED,
    response_model=DataEnvelope[SessionCredentialData],
    responses={
        **_success_response(201, _SESSION_CREATED_EXAMPLE),
        **management_error_responses(
            ManagementErrorCode.AUTHENTICATION_FAILED,
            ManagementErrorCode.ORIGIN_DENIED,
            ManagementErrorCode.VALIDATION_FAILED,
            ManagementErrorCode.RATE_LIMITED,
            ManagementErrorCode.SERVICE_NOT_READY,
        ),
    },
)
async def create_management_session(
    grant: Annotated[SessionGrant, Body(openapi_examples=_SESSION_GRANT_EXAMPLES)],
    request: Request,
    response: Response,
    runtime: Annotated[ManagementRuntime, Depends(get_management_runtime)],
) -> DataEnvelope[SessionCredentialData]:
    reject_unknown_query_parameters(request)
    request_id = management_request_id(request)
    try:
        if isinstance(grant, ManagementKeyGrant):
            source = request.client.host if request.client else "unknown"
            device = {
                key: value
                for key, value in (
                    ("client_name", grant.clientName),
                    ("device_summary", grant.deviceSummary),
                )
                if value is not None
            }
            issued = runtime.sessions.create_from_management_key(
                grant.managementKey.get_secret_value(),
                source=source,
                request_id=request_id,
                **device,
            )
        else:
            issued = runtime.sessions.create_from_telegram_approval(
                approval_id=grant.approvalId,
                exchange_secret=grant.exchangeSecret.get_secret_value(),
                request_id=request_id,
            )
    except AuthenticationRateLimited as exc:
        raise ManagementError(
            ManagementErrorCode.RATE_LIMITED,
            retryable=True,
        ) from exc
    except SessionAuthenticationError as exc:
        raise ManagementError(ManagementErrorCode.AUTHENTICATION_FAILED) from exc
    response.headers["Cache-Control"] = "no-store"
    credential: str | None = issued.credential
    if grant.sessionTransport == "cookie":
        # Script-invisible: the credential exists only in the HttpOnly cookie.
        set_session_cookie(request, response, issued.credential, issued.expires_at)
        credential = None
    return DataEnvelope(
        data=SessionCredentialData(
            credential=credential,
            session=_session_summary(issued),
        ),
        meta=_meta(request),
    )


@router.get(
    "/auth/session",
    operation_id="getCurrentManagementSession",
    tags=["management-auth"],
    response_model=DataEnvelope[SessionSummary],
    responses={
        **_success_response(200, _SESSION_EXAMPLE),
        **management_error_responses(
            ManagementErrorCode.SESSION_REQUIRED,
            ManagementErrorCode.SESSION_EXPIRED,
            ManagementErrorCode.ORIGIN_DENIED,
            ManagementErrorCode.VALIDATION_FAILED,
            ManagementErrorCode.SERVICE_NOT_READY,
        ),
    },
)
async def get_current_management_session(
    request: Request,
    response: Response,
    authenticated: Annotated[AuthenticatedSession, Depends(get_authenticated_session)],
) -> DataEnvelope[SessionSummary]:
    reject_unknown_query_parameters(request)
    response.headers["Cache-Control"] = "no-store"
    return DataEnvelope(data=_session_summary(authenticated.verified), meta=_meta(request))


@router.delete(
    "/auth/session",
    operation_id="revokeCurrentManagementSession",
    tags=["management-auth"],
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        **_no_content_response("Management session revoked"),
        **management_error_responses(
            ManagementErrorCode.SESSION_REQUIRED,
            ManagementErrorCode.SESSION_EXPIRED,
            ManagementErrorCode.ORIGIN_DENIED,
            ManagementErrorCode.VALIDATION_FAILED,
            ManagementErrorCode.SERVICE_NOT_READY,
        ),
    },
)
async def revoke_current_management_session(
    request: Request,
    authenticated: Annotated[AuthenticatedSession, Depends(get_authenticated_session)],
    runtime: Annotated[ManagementRuntime, Depends(get_management_runtime)],
) -> Response:
    reject_unknown_query_parameters(request)
    runtime.sessions.revoke_current(
        authenticated.credential,
        request_id=management_request_id(request),
    )
    response = Response(status_code=status.HTTP_204_NO_CONTENT, headers={"Cache-Control": "no-store"})
    if has_session_cookie(request):
        clear_session_cookie(request, response)
    return response


@router.post(
    "/auth/telegram-approvals",
    operation_id="createTelegramApproval",
    tags=["management-auth"],
    status_code=status.HTTP_201_CREATED,
    response_model=DataEnvelope[TelegramApprovalCreatedData],
    responses={
        **_success_response(201, _APPROVAL_CREATED_EXAMPLE),
        **management_error_responses(
            ManagementErrorCode.ORIGIN_DENIED,
            ManagementErrorCode.VALIDATION_FAILED,
            ManagementErrorCode.RATE_LIMITED,
            ManagementErrorCode.SERVICE_NOT_READY,
            ManagementErrorCode.DEPENDENCY_UNAVAILABLE,
        ),
    },
)
async def create_telegram_approval(
    body: Annotated[
        TelegramApprovalCreateRequest,
        Body(openapi_examples=_APPROVAL_CREATE_EXAMPLES),
    ],
    request: Request,
    response: Response,
    runtime: Annotated[ManagementRuntime, Depends(get_management_runtime)],
) -> DataEnvelope[TelegramApprovalCreatedData]:
    reject_unknown_query_parameters(request)
    request_id = management_request_id(request)
    try:
        issued = await asyncio.to_thread(
            runtime.approvals.create,
            client_name=body.clientName,
            source_address=request.client.host if request.client else "unknown",
            device_summary=body.deviceSummary,
            request_id=request_id,
        )
    except AuthenticationRateLimited as exc:
        raise ManagementError(ManagementErrorCode.RATE_LIMITED, retryable=True) from exc
    except ApprovalError as exc:
        code = (
            ManagementErrorCode.RATE_LIMITED
            if exc.reason == "capacity"
            else ManagementErrorCode.DEPENDENCY_UNAVAILABLE
        )
        raise ManagementError(code, retryable=True) from exc
    response.headers["Cache-Control"] = "no-store"
    return DataEnvelope(
        data=TelegramApprovalCreatedData(
            approvalId=issued.approval_id,
            exchangeSecret=issued.exchange_secret,
            expiresAt=issued.expires_at,
            pollAfterSeconds=issued.poll_after_seconds,
            confirmCode=issued.confirm_code,
        ),
        meta=_meta(request),
    )


@router.get(
    "/auth/telegram-approvals/{approvalId}",
    operation_id="getTelegramApproval",
    tags=["management-auth"],
    response_model=DataEnvelope[TelegramApprovalStatusData],
    responses={
        **_success_response(200, _APPROVAL_STATUS_EXAMPLE),
        **management_error_responses(
            ManagementErrorCode.AUTHENTICATION_FAILED,
            ManagementErrorCode.ORIGIN_DENIED,
            ManagementErrorCode.VALIDATION_FAILED,
            ManagementErrorCode.SERVICE_NOT_READY,
        ),
    },
)
async def get_telegram_approval(
    approvalId: Annotated[str, Path(min_length=8, max_length=128)],
    request: Request,
    response: Response,
    runtime: Annotated[ManagementRuntime, Depends(get_management_runtime)],
    authorization: Annotated[str | None, Header()] = None,
) -> DataEnvelope[TelegramApprovalStatusData]:
    reject_unknown_query_parameters(request)
    if authorization is None or not authorization.startswith("Approval "):
        raise ManagementError(ManagementErrorCode.AUTHENTICATION_FAILED)
    exchange_secret = authorization[len("Approval ") :]
    try:
        view = runtime.approvals.get(approvalId, exchange_secret)
    except ApprovalError as exc:
        raise ManagementError(ManagementErrorCode.AUTHENTICATION_FAILED) from exc
    response.headers["Cache-Control"] = "no-store"
    return DataEnvelope(
        data=TelegramApprovalStatusData(
            approvalId=view.approval_id,
            status=view.status.value,
            expiresAt=view.expires_at,
            pollAfterSeconds=view.poll_after_seconds,
        ),
        meta=_meta(request),
    )


@router.get(
    "/meta",
    operation_id="getManagementMetadata",
    tags=["management-discovery"],
    response_model=DataEnvelope[ManagementMetadataData],
    responses={
        **_success_response(200, _METADATA_EXAMPLE),
        **management_error_responses(
            ManagementErrorCode.INVALID_REQUEST,
            ManagementErrorCode.SESSION_REQUIRED,
            ManagementErrorCode.SESSION_EXPIRED,
            ManagementErrorCode.ORIGIN_DENIED,
            ManagementErrorCode.CAPABILITY_DENIED,
            ManagementErrorCode.VALIDATION_FAILED,
            ManagementErrorCode.SERVICE_NOT_READY,
        ),
    },
)
async def get_management_metadata(
    request: Request,
    runtime: Annotated[ManagementRuntime, Depends(get_management_runtime)],
    context: Annotated[ManagementContext, Depends(require_capability(Capability.READ))],
) -> DataEnvelope[ManagementMetadataData]:
    reject_unknown_query_parameters(request)
    principal_capabilities = sorted(
        context.actor.capabilities, key=lambda item: item.value
    )
    snapshot = build_discovery_snapshot(
        request.app.openapi(), principal_capabilities=principal_capabilities
    )
    return DataEnvelope(
        data=ManagementMetadataData(
            apiVersion="v1",
            applicationVersion=runtime.application_version,
            features=list(snapshot.features),
            supportedCapabilities=supported_capabilities(),
            principalCapabilities=principal_capabilities,
            enums=list(snapshot.enums),
            documentationUrl=runtime.documentation_url,
        ),
        meta=_meta(request),
    )


@router.get(
    "/capabilities",
    operation_id="getManagementCapabilities",
    tags=["management-discovery"],
    response_model=DataEnvelope[ManagementCapabilitiesData],
    responses={
        **_success_response(200, _CAPABILITIES_EXAMPLE),
        **management_error_responses(
            ManagementErrorCode.INVALID_REQUEST,
            ManagementErrorCode.SESSION_REQUIRED,
            ManagementErrorCode.SESSION_EXPIRED,
            ManagementErrorCode.ORIGIN_DENIED,
            ManagementErrorCode.CAPABILITY_DENIED,
            ManagementErrorCode.VALIDATION_FAILED,
            ManagementErrorCode.SERVICE_NOT_READY,
        ),
    },
)
async def get_management_capabilities(
    request: Request,
    runtime: Annotated[ManagementRuntime, Depends(get_management_runtime)],
    context: Annotated[ManagementContext, Depends(require_capability(Capability.READ))],
) -> DataEnvelope[ManagementCapabilitiesData]:
    reject_unknown_query_parameters(request)
    principal_capabilities = sorted(
        context.actor.capabilities, key=lambda item: item.value
    )
    channel_catalog = runtime.control_owner().channels.get_catalog(context)
    snapshot = build_discovery_snapshot(
        request.app.openapi(),
        channel_catalog=channel_catalog,
        principal_capabilities=principal_capabilities,
    )
    return DataEnvelope(
        data=ManagementCapabilitiesData(
            supportedCapabilities=supported_capabilities(),
            principalCapabilities=principal_capabilities,
            domains=list(snapshot.domains),
        ),
        meta=_meta(request),
    )


@router.get(
    "/operations/{operationId}",
    operation_id="getManagementOperation",
    tags=["management-operations"],
    response_model=DataEnvelope[ManagementOperationData],
    responses={
        **_success_response(200, _OPERATION_EXAMPLE),
        **management_error_responses(
            ManagementErrorCode.INVALID_REQUEST,
            ManagementErrorCode.SESSION_REQUIRED,
            ManagementErrorCode.SESSION_EXPIRED,
            ManagementErrorCode.ORIGIN_DENIED,
            ManagementErrorCode.CAPABILITY_DENIED,
            ManagementErrorCode.OPERATION_NOT_FOUND,
            ManagementErrorCode.VALIDATION_FAILED,
            ManagementErrorCode.SERVICE_NOT_READY,
        ),
    },
)
async def get_management_operation(
    operationId: Annotated[str, Path(min_length=4, max_length=128)],
    request: Request,
    runtime: Annotated[ManagementRuntime, Depends(get_management_runtime)],
    context: Annotated[ManagementContext, Depends(get_management_context)],
) -> DataEnvelope[ManagementOperationData]:
    reject_unknown_query_parameters(request)
    operation = runtime.operations.get(context, operationId)
    return DataEnvelope(data=_operation_data(operation), meta=_meta(request))


@router.delete(
    "/operations/{operationId}",
    operation_id="cancelManagementOperation",
    tags=["management-operations"],
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        **_no_content_response("Management operation cancelled"),
        **management_error_responses(
            ManagementErrorCode.INVALID_REQUEST,
            ManagementErrorCode.INVALID_OPERATION_STATE,
            ManagementErrorCode.SESSION_REQUIRED,
            ManagementErrorCode.SESSION_EXPIRED,
            ManagementErrorCode.ORIGIN_DENIED,
            ManagementErrorCode.CAPABILITY_DENIED,
            ManagementErrorCode.OPERATION_NOT_FOUND,
            ManagementErrorCode.VALIDATION_FAILED,
            ManagementErrorCode.SERVICE_NOT_READY,
        ),
    },
)
async def cancel_management_operation(
    operationId: Annotated[str, Path(min_length=4, max_length=128)],
    request: Request,
    runtime: Annotated[ManagementRuntime, Depends(get_management_runtime)],
    context: Annotated[ManagementContext, Depends(get_management_context)],
) -> Response:
    reject_unknown_query_parameters(request)
    runtime.operations.cancel(context, operationId)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
