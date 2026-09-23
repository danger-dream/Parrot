"""Management API adapter for the transport-neutral OAuth control."""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Header, Path, Query, Request, Response, status

from src.management_auth import Capability
from src.management_control import ManagementContext, ManagementError, ManagementErrorCode
from src.management_control.oauth import (
    CompleteOAuthLoginCommand,
    CreateOAuthAccountCommand,
    OAuthAccountFilter,
    OAuthAccountSort,
    OAuthControl,
    OAuthImportDecision,
    OAuthProvider,
    OAuthReplaceRequired,
    PageSpec,
    UpdateOAuthAccountCommand,
)

from ..dependencies import (
    ManagementRuntime,
    get_management_runtime,
    require_capability,
)
from ..schemas.base import DataEnvelope
from ..schemas.oauth import (
    CommitOAuthImportRequest,
    CommitPlanRequest,
    CompleteOAuthLoginFlowRequest,
    CreateOAuthAccountRequest,
    InvalidOAuthDeletionPlanRequest,
    OAuthAccountDetailData,
    OAuthAccountListData,
    OAuthAccountListEnvelope,
    OAuthClearedCountData,
    OAuthDeletedCountData,
    OAuthDeletionPlanData,
    OAuthImportCandidateData,
    OAuthImportCommitData,
    OAuthImportPreviewData,
    OAuthImportProblemData,
    OAuthLoginFlowData,
    OAuthLoginPollData,
    PollOAuthLoginFlowRequest,
    OAuthMutationData,
    OAuthOperationEnvelope,
    OAuthQuotaResetPlanData,
    OAuthQuotaResetPlanRequest,
    OAuthRevisionData,
    PreviewOAuthImportRequest,
    ReorderOAuthAccountsRequest,
    StartOAuthLoginFlowRequest,
    UpdateOAuthAccountRequest,
)
from ..schemas.oauth_models import (
    OAuthModelListEnvelope,
    UpdateOAuthAccountModelSettingsRequest,
    UpdateOAuthAccountModelsRequest,
)
from ..schemas.oauth_settings import (
    OAuthSettingsData,
    QuotaMonitorData,
    TelegramOAuthPreferencesData,
    UpdateOAuthSettingsRequest,
    UpdateTelegramOAuthPreferencesRequest,
)
from .oauth_support import (
    ACCOUNT_EXAMPLE,
    OPERATION_EXAMPLE,
    credential,
    detail,
    get_oauth_control_dependency,
    identity_conflict_response,
    meta,
    models,
    operation_envelope,
    page_meta,
    responses,
    summary,
    StrictOAuthQueryRoute,
)


router = APIRouter(tags=["management-oauth"], route_class=StrictOAuthQueryRoute)

ReadContext = Annotated[ManagementContext, Depends(require_capability(Capability.READ))]
WriteContext = Annotated[ManagementContext, Depends(require_capability(Capability.WRITE))]
SecretContext = Annotated[ManagementContext, Depends(require_capability(Capability.SECRETS_WRITE))]
DestructiveContext = Annotated[ManagementContext, Depends(require_capability(Capability.DESTRUCTIVE))]
Control = Annotated[OAuthControl, Depends(get_oauth_control_dependency)]
Runtime = Annotated[ManagementRuntime, Depends(get_management_runtime)]


@router.get(
    "/oauth/accounts",
    operation_id="listOAuthAccounts",
    response_model=OAuthAccountListEnvelope,
    responses=responses(200, {"items": [ACCOUNT_EXAMPLE], "revision": "revision-example"}),
)
def list_oauth_accounts(
    request: Request,
    context: ReadContext,
    control: Control,
    filter: Annotated[OAuthAccountFilter, Query()] = OAuthAccountFilter.ALL,
    provider: Annotated[OAuthProvider | None, Query()] = None,
    enabled: Annotated[bool | None, Query()] = None,
    sort: Annotated[OAuthAccountSort, Query()] = OAuthAccountSort.CONFIGURED,
    page: Annotated[int, Query(ge=1)] = 1,
    pageSize: Annotated[int, Query(ge=1, le=200)] = 50,
) -> OAuthAccountListEnvelope:
    result = control.list_accounts(
        context,
        account_filter=filter,
        provider=provider,
        enabled=enabled,
        sort=sort,
        page=PageSpec(page, pageSize),
    )
    return OAuthAccountListEnvelope(
        data=OAuthAccountListData(
            items=[summary(item) for item in result.items], revision=result.revision,
        ),
        meta=page_meta(request, result.meta),
    )


@router.post(
    "/oauth/accounts",
    operation_id="createOAuthAccount",
    status_code=status.HTTP_201_CREATED,
    response_model=DataEnvelope[OAuthMutationData],
    responses=responses(201, {"accountId": "openai:example", "revision": "revision-example", "status": "created"}, ManagementErrorCode.IDENTITY_CONFLICT),
)
def create_oauth_account(
    body: Annotated[CreateOAuthAccountRequest, Body()],
    request: Request,
    context: SecretContext,
    control: Control,
) -> DataEnvelope[OAuthMutationData]:
    try:
        result = control.create_account(
            context,
            CreateOAuthAccountCommand(
                credential=credential(body.credential),
                replace_plan_token=body.replacePlanToken.get_secret_value() if body.replacePlanToken else None,
            ),
        )
    except OAuthReplaceRequired as error:
        return identity_conflict_response(error, request)
    return DataEnvelope(
        data=OAuthMutationData(accountId=result.account_id, revision=result.revision, status=result.status),
        meta=meta(request),
    )


@router.get(
    "/oauth/accounts/{accountId}",
    operation_id="getOAuthAccount",
    response_model=DataEnvelope[OAuthAccountDetailData],
    responses=responses(200, {"account": ACCOUNT_EXAMPLE, "usageWindows": [], "localStats": {"requestCount": 0, "inputTokens": 0, "outputTokens": 0, "costUsd": None}, "runtimeErrors": [], "credentialConfigured": True}, ManagementErrorCode.RESOURCE_NOT_FOUND),
)
def get_oauth_account(
    accountId: Annotated[str, Path(min_length=1, max_length=1000)],
    request: Request,
    context: ReadContext,
    control: Control,
) -> DataEnvelope[OAuthAccountDetailData]:
    result = control.get_account(context, accountId)
    return DataEnvelope(data=detail(result), meta=meta(request))


@router.patch(
    "/oauth/accounts/{accountId}",
    operation_id="updateOAuthAccount",
    response_model=DataEnvelope[OAuthAccountDetailData],
    responses=responses(200, {"account": ACCOUNT_EXAMPLE}, ManagementErrorCode.RESOURCE_NOT_FOUND, ManagementErrorCode.REVISION_CONFLICT),
)
def update_oauth_account(
    accountId: Annotated[str, Path(min_length=1, max_length=1000)],
    body: Annotated[UpdateOAuthAccountRequest, Body()],
    request: Request,
    context: WriteContext,
    control: Control,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> DataEnvelope[OAuthAccountDetailData]:
    result = control.update_account(
        context,
        accountId,
        UpdateOAuthAccountCommand(body.displayName, body.enabled, body.maxConcurrent),
        expected_revision=if_match,
    )
    return DataEnvelope(data=detail(result), meta=meta(request))


@router.delete(
    "/oauth/accounts/{accountId}",
    operation_id="deleteOAuthAccount",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=responses(204, None, ManagementErrorCode.RESOURCE_NOT_FOUND, ManagementErrorCode.REVISION_CONFLICT),
)
def delete_oauth_account(
    accountId: Annotated[str, Path(min_length=1, max_length=1000)],
    context: DestructiveContext,
    control: Control,
    if_match: Annotated[str, Header(alias="If-Match")],
) -> Response:
    control.delete_account(context, accountId, expected_revision=if_match)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put(
    "/oauth/account-order",
    operation_id="reorderOAuthAccounts",
    response_model=DataEnvelope[OAuthRevisionData],
    responses=responses(200, {"revision": "revision-example"}, ManagementErrorCode.RESOURCE_CONFLICT, ManagementErrorCode.REVISION_CONFLICT),
)
def reorder_oauth_accounts(
    body: Annotated[ReorderOAuthAccountsRequest, Body()],
    request: Request,
    context: WriteContext,
    control: Control,
    if_match: Annotated[str, Header(alias="If-Match")],
) -> DataEnvelope[OAuthRevisionData]:
    revision = control.reorder_accounts(context, body.accountIds, expected_revision=if_match)
    return DataEnvelope(data=OAuthRevisionData(revision=revision), meta=meta(request))


@router.post(
    "/oauth/login-flows",
    operation_id="startOAuthLoginFlow",
    status_code=status.HTTP_201_CREATED,
    response_model=DataEnvelope[OAuthLoginFlowData],
    responses=responses(201, {"flowId": "oflow_example", "flowSecret": "<one-time-secret>", "provider": "openai", "authUrl": "https://example.invalid/login", "instruction": None, "expiresAt": "2026-01-02T03:34:05Z"}, ManagementErrorCode.UPSTREAM_ERROR),
)
async def start_oauth_login_flow(
    body: Annotated[StartOAuthLoginFlowRequest, Body()],
    request: Request,
    context: SecretContext,
    control: Control,
) -> DataEnvelope[OAuthLoginFlowData]:
    result = await asyncio.to_thread(control.start_login_flow, context, body.provider,
                                     realm=body.realm, client_profile=body.clientProfile, **({"site": body.site} if body.site is not None else {}))
    return DataEnvelope(
        data=OAuthLoginFlowData(
            flowId=result.flow_id,
            flowSecret=result.flow_secret,
            provider=result.provider,
            authUrl=result.auth_url,
            instruction=result.instruction,
            expiresAt=result.expires_at,
        ),
        meta=meta(request),
    )


@router.post(
    "/oauth/login-flows/{flowId}/poll",
    operation_id="pollOAuthLoginFlow",
    response_model=DataEnvelope[OAuthLoginPollData],
    responses=responses(200, {"flowId": "wbflow_example", "status": "pending", "expiresAt": "2026-01-02T03:09:05Z"}, ManagementErrorCode.INVALID_OPERATION_STATE, ManagementErrorCode.UPSTREAM_ERROR),
)
async def poll_oauth_login_flow(
    flowId: Annotated[str, Path(min_length=1, max_length=200)],
    body: Annotated[PollOAuthLoginFlowRequest, Body()],
    request: Request, context: SecretContext, control: Control,
) -> DataEnvelope[OAuthLoginPollData]:
    result = await asyncio.to_thread(control.poll_login_flow, context, flowId, body.flowSecret.get_secret_value())
    return DataEnvelope(data=OAuthLoginPollData(flowId=result.flow_id, status=result.status,
                         expiresAt=result.expires_at, accountPreview=result.account_preview,
                         accountId=result.account_id, saveStatus=result.save_status, revision=result.revision), meta=meta(request))


@router.post(
    "/oauth/login-flows/{flowId}/cancel", operation_id="cancelOAuthLoginFlow",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=responses(204, None, ManagementErrorCode.INVALID_OPERATION_STATE),
)
async def cancel_oauth_login_flow(
    flowId: Annotated[str, Path(min_length=1, max_length=200)],
    body: Annotated[PollOAuthLoginFlowRequest, Body()],
    context: SecretContext, control: Control,
) -> Response:
    await asyncio.to_thread(control.cancel_login_flow, context, flowId, body.flowSecret.get_secret_value())
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/oauth/login-flows/{flowId}/complete",
    operation_id="completeOAuthLoginFlow",
    response_model=DataEnvelope[OAuthMutationData],
    responses=responses(200, {"accountId": "openai:example", "revision": "revision-example", "status": "created"}, ManagementErrorCode.IDENTITY_CONFLICT, ManagementErrorCode.STATE_CONFLICT, ManagementErrorCode.UPSTREAM_ERROR),
)
async def complete_oauth_login_flow(
    flowId: Annotated[str, Path(min_length=8, max_length=200)],
    body: Annotated[CompleteOAuthLoginFlowRequest, Body()],
    request: Request,
    context: SecretContext,
    control: Control,
) -> DataEnvelope[OAuthMutationData]:
    command = CompleteOAuthLoginCommand(
        code=body.code.get_secret_value() if body.code else None,
        state=body.state.get_secret_value() if body.state else None,
        callback_url=body.callbackUrl.get_secret_value() if body.callbackUrl else None,
        completed=body.completed,
        replace_plan_token=body.replacePlanToken.get_secret_value() if body.replacePlanToken else None,
    )
    try:
        result = await asyncio.to_thread(
            control.complete_login_flow,
            context,
            flowId,
            body.flowSecret.get_secret_value() if body.flowSecret else "",
            command,
        )
    except OAuthReplaceRequired as error:
        return identity_conflict_response(error, request)
    return DataEnvelope(
        data=OAuthMutationData(accountId=result.account_id, revision=result.revision, status=result.status),
        meta=meta(request),
    )


@router.post(
    "/oauth/imports/preview",
    operation_id="previewOAuthImport",
    response_model=DataEnvelope[OAuthImportPreviewData],
    responses=responses(200, {"importId": "oimport_example", "importSecret": "<one-time-secret>", "candidates": [], "errors": [], "expiresAt": "2026-01-02T03:14:05Z"}),
)
def preview_oauth_import(
    body: Annotated[PreviewOAuthImportRequest, Body()],
    request: Request,
    context: SecretContext,
    control: Control,
) -> DataEnvelope[OAuthImportPreviewData]:
    result = control.preview_import(
        context,
        format=body.format,
        payload=body.parser_payload(),
        filename=body.filename or "",
    )
    return DataEnvelope(
        data=OAuthImportPreviewData(
            importId=result.import_id,
            importSecret=result.import_secret,
            candidates=[
                OAuthImportCandidateData(
                    candidateId=item.candidate_id,
                    provider=item.provider,
                    identity=item.identity,
                    displayName=item.display_name,
                    conflictAccountId=item.conflict_account_id,
                )
                for item in result.candidates
            ],
            errors=[OAuthImportProblemData(index=item.index, code=item.code, message=item.message) for item in result.errors],
            expiresAt=result.expires_at,
        ),
        meta=meta(request),
    )


@router.post(
    "/oauth/imports/{importId}/commit",
    operation_id="commitOAuthImport",
    response_model=DataEnvelope[OAuthImportCommitData],
    responses=responses(200, {"added": [], "replaced": [], "skipped": []}, ManagementErrorCode.INVALID_OPERATION_STATE, ManagementErrorCode.STATE_CONFLICT),
)
def commit_oauth_import(
    importId: Annotated[str, Path(min_length=8, max_length=200)],
    body: Annotated[CommitOAuthImportRequest, Body()],
    request: Request,
    context: SecretContext,
    control: Control,
) -> DataEnvelope[OAuthImportCommitData]:
    result = control.commit_import(
        context,
        importId,
        body.importSecret.get_secret_value(),
        [OAuthImportDecision(item.candidateId, item.action) for item in body.decisions],
    )
    return DataEnvelope(
        data=OAuthImportCommitData(added=list(result.added), replaced=list(result.replaced), skipped=list(result.skipped)),
        meta=meta(request),
    )


@router.get(
    "/oauth/invalid-accounts",
    operation_id="listInvalidOAuthAccounts",
    response_model=OAuthAccountListEnvelope,
    responses=responses(200, {"items": [ACCOUNT_EXAMPLE], "revision": "revision-example"}),
)
def list_invalid_oauth_accounts(
    request: Request,
    context: ReadContext,
    control: Control,
    page: Annotated[int, Query(ge=1)] = 1,
    pageSize: Annotated[int, Query(ge=1, le=200)] = 50,
) -> OAuthAccountListEnvelope:
    result = control.list_invalid_accounts(context, page=PageSpec(page, pageSize))
    return OAuthAccountListEnvelope(
        data=OAuthAccountListData(
            items=[summary(item) for item in result.items], revision=result.revision,
        ),
        meta=page_meta(request, result.meta),
    )


@router.post(
    "/oauth/invalid-accounts/delete-plan",
    operation_id="planInvalidOAuthAccountDeletion",
    response_model=DataEnvelope[OAuthDeletionPlanData],
    responses=responses(200, {"planToken": "<one-time>", "accountIds": [], "expiresAt": "2026-01-02T03:14:05Z", "revision": "revision-example"}),
)
def plan_invalid_oauth_account_deletion(
    body: Annotated[InvalidOAuthDeletionPlanRequest, Body()],
    request: Request,
    context: DestructiveContext,
    control: Control,
) -> DataEnvelope[OAuthDeletionPlanData]:
    if body.all == (body.accountIds is not None):
        raise ManagementError(ManagementErrorCode.VALIDATION_FAILED)
    result = control.plan_invalid_deletion(context, None if body.all else body.accountIds)
    return DataEnvelope(
        data=OAuthDeletionPlanData(
            planToken=result.plan_token,
            accountIds=list(result.account_ids),
            expiresAt=result.expires_at,
            revision=result.revision,
        ),
        meta=meta(request),
    )


@router.post(
    "/oauth/invalid-accounts/delete",
    operation_id="deleteInvalidOAuthAccounts",
    response_model=DataEnvelope[OAuthDeletedCountData],
    responses=responses(200, {"deleted": 2}, ManagementErrorCode.INVALID_OPERATION_STATE, ManagementErrorCode.REVISION_CONFLICT, ManagementErrorCode.STATE_CONFLICT),
)
def delete_invalid_oauth_accounts(
    body: Annotated[CommitPlanRequest, Body()],
    request: Request,
    context: DestructiveContext,
    control: Control,
) -> DataEnvelope[OAuthDeletedCountData]:
    deleted = control.delete_invalid_accounts(context, body.planToken.get_secret_value())
    return DataEnvelope(data=OAuthDeletedCountData(deleted=deleted), meta=meta(request))


@router.post(
    "/oauth/accounts/{accountId}/actions/refresh-token",
    operation_id="refreshOAuthToken",
    response_model=DataEnvelope[OAuthMutationData],
    responses=responses(200, {"accountId": "openai:example", "revision": "revision-example", "status": "refreshed"}, ManagementErrorCode.RESOURCE_NOT_FOUND, ManagementErrorCode.UPSTREAM_ERROR),
)
def refresh_oauth_token(
    accountId: Annotated[str, Path(min_length=1, max_length=1000)],
    request: Request,
    context: WriteContext,
    control: Control,
) -> DataEnvelope[OAuthMutationData]:
    result = control.refresh_token(context, accountId)
    return DataEnvelope(
        data=OAuthMutationData(accountId=result.account_id, revision=result.revision, status=result.status),
        meta=meta(request),
    )


@router.post(
    "/oauth/accounts/{accountId}/actions/refresh-usage",
    operation_id="refreshOAuthUsage",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=OAuthOperationEnvelope,
    responses=responses(202, OPERATION_EXAMPLE, ManagementErrorCode.RESOURCE_NOT_FOUND, ManagementErrorCode.OPERATION_ALREADY_RUNNING),
)
def refresh_oauth_usage(
    accountId: Annotated[str, Path(min_length=1, max_length=1000)],
    request: Request,
    context: WriteContext,
    control: Control,
    runtime: Runtime,
) -> OAuthOperationEnvelope:
    return operation_envelope(control.refresh_usage(context, accountId, runtime.operations), request)


@router.post(
    "/oauth/actions/refresh-usage",
    operation_id="refreshAllOAuthUsage",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=OAuthOperationEnvelope,
    responses=responses(202, OPERATION_EXAMPLE, ManagementErrorCode.OPERATION_ALREADY_RUNNING),
)
def refresh_all_oauth_usage(
    request: Request,
    context: WriteContext,
    control: Control,
    runtime: Runtime,
) -> OAuthOperationEnvelope:
    return operation_envelope(control.refresh_all_usage(context, runtime.operations), request)


@router.post(
    "/oauth/accounts/{accountId}/actions/reset-quota-plan",
    operation_id="planOAuthQuotaReset",
    response_model=DataEnvelope[OAuthQuotaResetPlanData],
    responses=responses(200, {"planToken": "<one-time>", "accountId": "openai:example", "provider": "openai", "creditCount": 1, "expiresAt": "2026-01-02T03:14:05Z"}, ManagementErrorCode.RESOURCE_NOT_FOUND, ManagementErrorCode.UNSUPPORTED_VALUE),
)
def plan_oauth_quota_reset(
    accountId: Annotated[str, Path(min_length=1, max_length=1000)],
    body: Annotated[OAuthQuotaResetPlanRequest, Body()],
    request: Request,
    context: DestructiveContext,
    control: Control,
) -> DataEnvelope[OAuthQuotaResetPlanData]:
    result = control.plan_quota_reset(context, accountId)
    return DataEnvelope(
        data=OAuthQuotaResetPlanData(
            planToken=result.plan_token,
            accountId=result.account_id,
            provider=result.provider,
            creditCount=result.credit_count,
            expiresAt=result.expires_at,
        ),
        meta=meta(request),
    )


@router.post(
    "/oauth/accounts/{accountId}/actions/reset-quota",
    operation_id="resetOAuthQuota",
    response_model=DataEnvelope[OAuthMutationData],
    responses=responses(200, {"accountId": "openai:example", "revision": "revision-example", "status": "reset"}, ManagementErrorCode.INVALID_OPERATION_STATE, ManagementErrorCode.REVISION_CONFLICT),
)
def reset_oauth_quota(
    accountId: Annotated[str, Path(min_length=1, max_length=1000)],
    body: Annotated[CommitPlanRequest, Body()],
    request: Request,
    context: DestructiveContext,
    control: Control,
) -> DataEnvelope[OAuthMutationData]:
    result = control.reset_quota(
        context, accountId, body.planToken.get_secret_value(),
    )
    return DataEnvelope(
        data=OAuthMutationData(accountId=result.account_id, revision=result.revision, status=result.status),
        meta=meta(request),
    )


@router.post(
    "/oauth/accounts/{accountId}/actions/clear-errors",
    operation_id="clearOAuthAccountErrors",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=responses(204, None, ManagementErrorCode.RESOURCE_NOT_FOUND),
)
def clear_oauth_account_errors(
    accountId: Annotated[str, Path(min_length=1, max_length=1000)],
    context: WriteContext,
    control: Control,
) -> Response:
    control.clear_errors(context, accountId)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/oauth/accounts/{accountId}/actions/clear-affinity",
    operation_id="clearOAuthAccountAffinity",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=responses(204, None, ManagementErrorCode.RESOURCE_NOT_FOUND),
)
def clear_oauth_account_affinity(
    accountId: Annotated[str, Path(min_length=1, max_length=1000)],
    context: WriteContext,
    control: Control,
) -> Response:
    control.clear_affinity(context, accountId)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/oauth/actions/clear-errors",
    operation_id="clearAllOAuthErrors",
    response_model=DataEnvelope[OAuthClearedCountData],
    responses=responses(200, {"cleared": 2}),
)
def clear_all_oauth_errors(
    request: Request,
    context: DestructiveContext,
    control: Control,
) -> DataEnvelope[OAuthClearedCountData]:
    count = control.clear_all_errors(context)
    return DataEnvelope(data=OAuthClearedCountData(cleared=count), meta=meta(request))


@router.get(
    "/oauth/accounts/{accountId}/models",
    operation_id="listOAuthAccountModels",
    response_model=OAuthModelListEnvelope,
    responses=responses(200, {"items": [], "revision": "revision-example"}, ManagementErrorCode.RESOURCE_NOT_FOUND),
)
def list_oauth_accountmodels(
    accountId: Annotated[str, Path(min_length=1, max_length=1000)],
    request: Request,
    context: ReadContext,
    control: Control,
    page: Annotated[int, Query(ge=1)] = 1,
    pageSize: Annotated[int, Query(ge=1, le=200)] = 50,
) -> OAuthModelListEnvelope:
    return models(control.list_models(context, accountId, page=PageSpec(page, pageSize)), request)


@router.patch(
    "/oauth/accounts/{accountId}/models",
    operation_id="updateOAuthAccountModels",
    response_model=OAuthModelListEnvelope,
    responses=responses(200, {"items": [], "revision": "revision-example"}, ManagementErrorCode.RESOURCE_NOT_FOUND, ManagementErrorCode.REVISION_CONFLICT),
)
def update_oauth_accountmodels(
    accountId: Annotated[str, Path(min_length=1, max_length=1000)],
    body: Annotated[UpdateOAuthAccountModelsRequest, Body()],
    request: Request,
    context: WriteContext,
    control: Control,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> OAuthModelListEnvelope:
    result = control.update_models(
        context,
        accountId,
        model_ids=body.modelIds,
        disabled=body.disabled,
        expected_revision=if_match,
    )
    return models(result, request)


@router.patch(
    "/oauth/accounts/{accountId}/models/settings",
    operation_id="updateOAuthAccountModelSettings",
    response_model=OAuthModelListEnvelope,
    responses=responses(200, {"items": [], "revision": "revision-example"}, ManagementErrorCode.RESOURCE_NOT_FOUND, ManagementErrorCode.REVISION_CONFLICT, ManagementErrorCode.UNSUPPORTED_VALUE),
)
def update_oauth_account_model_settings(
    accountId: Annotated[str, Path(min_length=1, max_length=1000)],
    body: Annotated[UpdateOAuthAccountModelSettingsRequest, Body()],
    request: Request,
    context: WriteContext,
    control: Control,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> OAuthModelListEnvelope:
    result = control.update_model_settings(
        context,
        accountId,
        model_id=body.modelId,
        max_context_default=body.maxContextDefault,
        expected_revision=if_match,
    )
    return models(result, request)


@router.post(
    "/oauth/accounts/{accountId}/models/actions/sync",
    operation_id="syncOAuthAccountModels",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=OAuthOperationEnvelope,
    responses=responses(202, OPERATION_EXAMPLE, ManagementErrorCode.RESOURCE_NOT_FOUND, ManagementErrorCode.OPERATION_ALREADY_RUNNING),
)
def sync_oauth_accountmodels(
    accountId: Annotated[str, Path(min_length=1, max_length=1000)],
    request: Request,
    context: WriteContext,
    control: Control,
    runtime: Runtime,
) -> OAuthOperationEnvelope:
    return operation_envelope(control.sync_models(context, accountId, runtime.operations), request)


@router.get(
    "/oauth/settings",
    operation_id="getOAuthSettings",
    response_model=DataEnvelope[OAuthSettingsData],
    responses=responses(200, {"quotaMonitor": {"enabled": False, "intervalSeconds": 60, "thresholdPercent": 95}, "cchMode": "disabled", "antigravityTlsFingerprintEnabled": False, "revision": "revision-example"}),
)
def get_oauth_settings(
    request: Request,
    context: ReadContext,
    control: Control,
) -> DataEnvelope[OAuthSettingsData]:
    result = control.get_settings(context)
    return DataEnvelope(
        data=OAuthSettingsData(
            quotaMonitor=QuotaMonitorData(
                enabled=result.quota_monitor_enabled,
                intervalSeconds=result.quota_monitor_interval_seconds,
                thresholdPercent=result.quota_monitor_threshold_percent,
            ),
            cchMode=result.cch_mode,
            antigravityTlsFingerprintEnabled=result.antigravity_tls_fingerprint_enabled,
            revision=result.revision,
        ),
        meta=meta(request),
    )


@router.patch(
    "/oauth/settings",
    operation_id="updateOAuthSettings",
    response_model=DataEnvelope[OAuthSettingsData],
    responses=responses(200, {"quotaMonitor": {"enabled": True, "intervalSeconds": 120, "thresholdPercent": 90}, "cchMode": "dynamic", "antigravityTlsFingerprintEnabled": True, "revision": "revision-example"}, ManagementErrorCode.REVISION_CONFLICT),
)
def update_oauth_settings(
    body: Annotated[UpdateOAuthSettingsRequest, Body()],
    request: Request,
    context: WriteContext,
    control: Control,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> DataEnvelope[OAuthSettingsData]:
    quota = body.quotaMonitor
    result = control.update_settings(
        context,
        quota_enabled=quota.enabled if quota else None,
        interval_seconds=quota.intervalSeconds if quota else None,
        threshold_percent=quota.thresholdPercent if quota else None,
        cch_mode=body.cchMode,
        antigravity_tls_fingerprint_enabled=body.antigravityTlsFingerprintEnabled,
        expected_revision=if_match,
    )
    return DataEnvelope(
        data=OAuthSettingsData(
            quotaMonitor=QuotaMonitorData(
                enabled=result.quota_monitor_enabled,
                intervalSeconds=result.quota_monitor_interval_seconds,
                thresholdPercent=result.quota_monitor_threshold_percent,
            ),
            cchMode=result.cch_mode,
            antigravityTlsFingerprintEnabled=result.antigravity_tls_fingerprint_enabled,
            revision=result.revision,
        ),
        meta=meta(request),
    )


@router.get(
    "/preferences/telegram/oauth",
    operation_id="getTelegramOAuthPreferences",
    response_model=DataEnvelope[TelegramOAuthPreferencesData],
    responses=responses(200, {"usageDisplayMode": "used", "quotaProgressBar": True, "revision": "revision-example"}),
)
def get_telegram_oauth_preferences(
    request: Request,
    context: ReadContext,
    control: Control,
) -> DataEnvelope[TelegramOAuthPreferencesData]:
    result = control.get_telegram_preferences(context)
    return DataEnvelope(
        data=TelegramOAuthPreferencesData(
            usageDisplayMode=result.usage_display_mode,
            quotaProgressBar=result.quota_progress_bar,
            revision=result.revision,
        ),
        meta=meta(request),
    )


@router.patch(
    "/preferences/telegram/oauth",
    operation_id="updateTelegramOAuthPreferences",
    response_model=DataEnvelope[TelegramOAuthPreferencesData],
    responses=responses(200, {"usageDisplayMode": "remaining", "quotaProgressBar": False, "revision": "revision-example"}, ManagementErrorCode.REVISION_CONFLICT),
)
def update_telegram_oauth_preferences(
    body: Annotated[UpdateTelegramOAuthPreferencesRequest, Body()],
    request: Request,
    context: WriteContext,
    control: Control,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> DataEnvelope[TelegramOAuthPreferencesData]:
    result = control.update_telegram_preferences(
        context,
        usage_display_mode=body.usageDisplayMode,
        quota_progress_bar=body.quotaProgressBar,
        expected_revision=if_match,
    )
    return DataEnvelope(
        data=TelegramOAuthPreferencesData(
            usageDisplayMode=result.usage_display_mode,
            quotaProgressBar=result.quota_progress_bar,
            revision=result.revision,
        ),
        meta=meta(request),
    )


from .oauth_workbuddy import router as workbuddy_router
router.include_router(workbuddy_router)
from .oauth_zhipu import router as zhipu_router
router.include_router(zhipu_router)
