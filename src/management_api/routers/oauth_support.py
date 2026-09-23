"""Schema conversion helpers for the OAuth Management API adapter."""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from src.management_control import ErrorField, ManagementError, ManagementErrorCode
from src.management_control.oauth import (
    JsonCredential,
    ManualCredential,
    OAuthControl,
    OAuthReplaceRequired,
    RefreshTokenCredential,
)
from src.management_control.oauth.contracts import public_value, sanitize_text
from src.management_control.operations import ManagementOperation

from ..dependencies import (
    ManagementRuntime,
    get_management_control_owner,
    get_management_runtime,
    management_request_id,
)
from ..error_mapping import error_response, management_error_responses
from ..schemas.base import ResponseMeta
from ..schemas.oauth import (
    JsonOAuthCredential,
    ManualOAuthCredential,
    OAuthAccountDetailData,
    OAuthAccountSummaryData,
    OAuthIdentityConflictEnvelope,
    OAuthLocalStatsData,
    OAuthOperationEnvelope,
    OAuthPageMeta,
    OAuthRequestMeta,
    OAuthRuntimeErrorData,
    OAuthUsageWindowData,
    RefreshTokenOAuthCredential,
)
from ..schemas.oauth_models import OAuthModelData, OAuthModelListData, OAuthModelListEnvelope
from ..schemas.operations import ManagementOperationData, OperationErrorData, OperationProgressData


COMMON_ERRORS = (
    ManagementErrorCode.SESSION_REQUIRED,
    ManagementErrorCode.SESSION_EXPIRED,
    ManagementErrorCode.ORIGIN_DENIED,
    ManagementErrorCode.CAPABILITY_DENIED,
    ManagementErrorCode.VALIDATION_FAILED,
    ManagementErrorCode.SERVICE_NOT_READY,
)
ACCOUNT_EXAMPLE = {
    "accountId": "openai:admin@example.invalid:workspace-example",
    "provider": "openai",
    "displayName": "Example account",
    "identity": "admin@example.invalid",
    "enabled": True,
    "disabledReason": None,
    "disabledUntil": None,
    "maxConcurrent": 2,
    "available": True,
    "quotaLimited": False,
    "invalid": False,
    "modelCount": 3,
    "disabledModelCount": 0,
    "credentialConfigured": True,
    "revision": "revision-example",
}
OPERATION_EXAMPLE = {
    "id": "op_example",
    "kind": "oauth.usage.refresh",
    "status": "queued",
    "progress": None,
    "createdAt": "2026-01-02T03:04:05Z",
    "startedAt": None,
    "finishedAt": None,
    "result": None,
    "error": None,
    "cancellable": False,
}


def responses(code: int, data: object, *extra: ManagementErrorCode) -> dict:
    if code == 204:
        success = {
            code: {
                "description": "Successful Response",
                "headers": {
                    "X-Request-Id": {
                        "description": "Stable request correlation identifier",
                        "schema": {"type": "string"},
                        "example": "request-example",
                    }
                },
            }
        }
    else:
        success = {
            code: {
                "description": "Successful Response",
                "content": {
                    "application/json": {
                        "example": {
                            "data": data,
                            "meta": {"requestId": "request-example"},
                        }
                    }
                },
            }
        }
    result = {
        **success,
        **management_error_responses(*COMMON_ERRORS, *extra),
    }
    if ManagementErrorCode.IDENTITY_CONFLICT in extra:
        result[409] = {
            "model": OAuthIdentityConflictEnvelope,
            "description": "Exact OAuth identity conflict with typed one-shot replace plan",
            "content": {
                "application/json": {
                    "example": {
                        "error": {
                            "code": "IDENTITY_CONFLICT",
                            "message": "IDENTITY_CONFLICT",
                            "fields": [{
                                "path": "accountId",
                                "code": "EXACT_IDENTITY",
                                "message": "openai:admin@example.invalid:workspace-example",
                            }],
                            "retryable": False,
                            "requestId": "request-example",
                            "operationId": None,
                        },
                        "conflict": {
                            "accountId": "openai:admin@example.invalid:workspace-example",
                            "replacePlanToken": "<one-time-write-only>",
                        },
                    }
                }
            },
        }
    return result


def identity_conflict_response(
    error: OAuthReplaceRequired, request: Request,
) -> JSONResponse:
    request_id = management_request_id(request)
    base = error_response(error, request_id=request_id)
    payload = json.loads(base.body)
    payload["conflict"] = {
        "accountId": error.account_id,
        "replacePlanToken": error.plan_token,
    }
    response = JSONResponse(status_code=409, content=payload)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Request-Id"] = request_id
    return response


class StrictOAuthQueryRoute(APIRoute):
    """Reject undeclared query keys before auth/control dependencies run."""

    def get_route_handler(self):
        original = super().get_route_handler()
        allowed = {str(field.alias) for field in self.dependant.query_params}

        async def strict_handler(request: Request):
            unknown = sorted(set(request.query_params) - allowed)
            if unknown:
                raise ManagementError(
                    ManagementErrorCode.VALIDATION_FAILED,
                    fields=tuple(
                        ErrorField(name, "extra_forbidden", "Unknown query parameter")
                        for name in unknown
                    ),
                )
            return await original(request)

        return strict_handler


def get_oauth_control_dependency(
    runtime: Annotated[ManagementRuntime, Depends(get_management_runtime)],
) -> OAuthControl:
    """Return the flow/plan-preserving control owned by this runtime."""
    return get_management_control_owner(runtime).oauth


def meta(request: Request) -> ResponseMeta:
    return ResponseMeta(requestId=management_request_id(request))


def page_meta(request: Request, value) -> OAuthPageMeta:
    return OAuthPageMeta(
        requestId=management_request_id(request),
        page=value.page,
        pageSize=value.page_size,
        total=value.total,
        hasNext=value.has_next,
    )


def summary(value) -> OAuthAccountSummaryData:
    return OAuthAccountSummaryData(
        accountId=value.account_id,
        provider=value.provider,
        displayName=value.display_name,
        identity=value.identity,
        enabled=value.enabled,
        disabledReason=value.disabled_reason,
        disabledUntil=value.disabled_until,
        maxConcurrent=value.max_concurrent,
        available=value.available,
        quotaLimited=value.quota_limited,
        invalid=value.invalid,
        modelCount=value.model_count,
        disabledModelCount=value.disabled_model_count,
        credentialConfigured=value.credential_configured,
        revision=value.revision,
    )


def detail(value) -> OAuthAccountDetailData:
    return OAuthAccountDetailData(
        account=summary(value.account),
        workspaceId=value.workspace_id,
        workspaceName=value.workspace_name,
        planType=value.plan_type,
        expiresAt=value.expires_at,
        usageWindows=[
            OAuthUsageWindowData(
                name=item.name,
                usedPercent=item.used_percent,
                remainingPercent=item.remaining_percent,
                resetsAt=item.resets_at,
            )
            for item in value.usage_windows
        ],
        localStats=OAuthLocalStatsData(
            requestCount=value.local_stats.request_count,
            inputTokens=value.local_stats.input_tokens,
            outputTokens=value.local_stats.output_tokens,
            costUsd=value.local_stats.cost_usd,
        ),
        runtimeErrors=[
            OAuthRuntimeErrorData(
                modelId=item.model_id,
                message=sanitize_text(item.message) if item.message else None,
                cooldownUntil=item.cooldown_until,
                cooldownPermanent=item.cooldown_permanent,
            )
            for item in value.runtime_errors
        ],
        credentialConfigured=value.credential_configured,
        lastModelSync=value.last_model_sync,
        workbuddy=public_value(value.workbuddy, camel_case_keys=True) if value.workbuddy is not None else None,
        zhipu=public_value(value.zhipu, camel_case_keys=True) if value.zhipu is not None else None,
    )


def operation(value: ManagementOperation) -> ManagementOperationData:
    progress = None
    if value.progress is not None:
        progress = OperationProgressData(
            current=value.progress.current,
            total=value.progress.total,
            messageCode=value.progress.message_code,
        )
    error = None
    if value.error is not None:
        error = OperationErrorData(
            code=value.error.code,
            message=sanitize_text(value.error.message),
            retryable=value.error.retryable,
        )
    return ManagementOperationData(
        id=value.id,
        kind=value.kind,
        status=value.status,
        progress=progress,
        createdAt=value.created_at,
        startedAt=value.started_at,
        finishedAt=value.finished_at,
        result=public_value(value.result),
        error=error,
        cancellable=value.cancellable,
    )


def models(value, request: Request) -> OAuthModelListEnvelope:
    return OAuthModelListEnvelope(
        data=OAuthModelListData(
            items=[
                OAuthModelData(
                    modelId=item.model_id,
                    name=item.name,
                    disabled=item.disabled,
                    cooldownUntil=item.cooldown_until,
                    cooldownPermanent=item.cooldown_permanent,
                    metadataSource=item.metadata_source,
                    contextWindow=item.context_window,
                    maxContextWindow=item.max_context_window,
                    serviceTier=item.service_tier,
                    maxContextDefault=item.max_context_default,
                    maxInputTokens=item.max_input_tokens,
                    maxOutputTokens=item.max_output_tokens,
                    reasoningEfforts=list(item.reasoning_efforts),
                )
                for item in value.items
            ],
            revision=value.revision,
        ),
        meta=page_meta(request, value.meta),
    )


def credential(value):
    if isinstance(value, ManualOAuthCredential):
        return ManualCredential(
            provider=value.provider,
            email=value.email,
            access_token=value.accessToken.get_secret_value(),
            refresh_token=value.refreshToken.get_secret_value(),
            display_name=value.displayName,
            identity_subject=value.identitySubject,
            workspace_id=value.workspaceId,
            project_id=value.projectId,
            expires_at=value.expiresAt,
        )
    if isinstance(value, JsonOAuthCredential):
        return JsonCredential(
            provider=value.provider,
            payload=value.payload.get_secret_value(),
        )
    if isinstance(value, RefreshTokenOAuthCredential):
        return RefreshTokenCredential(
            provider=value.provider,
            refresh_token=value.refreshToken.get_secret_value(),
            email_hint=value.emailHint,
        )
    raise TypeError("unsupported OAuth credential schema")


def operation_envelope(
    value: ManagementOperation,
    request: Request,
) -> OAuthOperationEnvelope:
    return OAuthOperationEnvelope(
        data=operation(value),
        meta=OAuthRequestMeta(requestId=management_request_id(request)),
    )
