"""Retention settings and actor-bound prepare/commit endpoints."""

from typing import Annotated

from fastapi import APIRouter, Body, Depends, Header, Path, Request, Response, status

from src.management_auth import Capability
from src.management_control import ManagementContext, ManagementErrorCode
from src.management_control.observability import RetentionMode

from ..dependencies import ManagementRuntime, get_management_runtime, require_capability
from ..error_mapping import management_error_responses
from ..schemas import DataEnvelope, ManagementOperationData
from ..schemas.observability import RetentionPlanCreate, RetentionPlanData, RetentionSettingsData, RetentionSettingsPatch
from ._observability import controls, meta, operation_data, reject_unknown_query


router = APIRouter(prefix="/logs/retention", tags=["management-retention"])
_READ_ERRORS = management_error_responses(
    ManagementErrorCode.SESSION_REQUIRED, ManagementErrorCode.SESSION_EXPIRED,
    ManagementErrorCode.CAPABILITY_DENIED, ManagementErrorCode.VALIDATION_FAILED,
    ManagementErrorCode.DEPENDENCY_UNAVAILABLE,
)
_WRITE_ERRORS = management_error_responses(
    ManagementErrorCode.SESSION_REQUIRED, ManagementErrorCode.SESSION_EXPIRED,
    ManagementErrorCode.CAPABILITY_DENIED, ManagementErrorCode.INVALID_REQUEST,
    ManagementErrorCode.CONFIRMATION_REQUIRED, ManagementErrorCode.VALIDATION_FAILED,
    ManagementErrorCode.RESOURCE_NOT_FOUND, ManagementErrorCode.RESOURCE_CONFLICT,
    ManagementErrorCode.REVISION_CONFLICT, ManagementErrorCode.STATE_CONFLICT,
    ManagementErrorCode.OPERATION_ALREADY_RUNNING, ManagementErrorCode.DEPENDENCY_UNAVAILABLE,
)


@router.get(
    "", operation_id="getLogRetentionSettings",
    response_model=DataEnvelope[RetentionSettingsData], responses=_READ_ERRORS,
)
def get_log_retention_settings(
    request: Request,
    context: Annotated[ManagementContext, Depends(require_capability(Capability.READ))],
) -> DataEnvelope[RetentionSettingsData]:
    reject_unknown_query(request, ())
    value = controls(request).retention.settings(context)
    return DataEnvelope(data=RetentionSettingsData.model_validate(value), meta=meta(request))


@router.patch(
    "", operation_id="updateLogRetentionSettings",
    response_model=DataEnvelope[RetentionSettingsData], responses=_WRITE_ERRORS,
)
def update_log_retention_settings(
    request: Request,
    context: Annotated[ManagementContext, Depends(require_capability(Capability.WRITE))],
    patch: Annotated[RetentionSettingsPatch, Body()],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> DataEnvelope[RetentionSettingsData]:
    reject_unknown_query(request, ())
    value = controls(request).retention.update_settings(
        context,
        mode=RetentionMode(patch.mode) if patch.mode else None,
        days=patch.days,
        log_store_bodies=patch.logStoreBodies,
        expected_revision=if_match,
        body_mode=RetentionMode(patch.bodyMode) if patch.bodyMode else None,
        body_days=patch.bodyDays,
    )
    return DataEnvelope(data=RetentionSettingsData.model_validate(value), meta=meta(request))


@router.post(
    "/plans", operation_id="createLogRetentionPlan",
    status_code=status.HTTP_201_CREATED,
    response_model=DataEnvelope[RetentionPlanData], responses=_WRITE_ERRORS,
)
def create_log_retention_plan(
    request: Request,
    context: Annotated[ManagementContext, Depends(require_capability(Capability.DESTRUCTIVE))],
    command: Annotated[RetentionPlanCreate, Body()],
) -> DataEnvelope[RetentionPlanData]:
    reject_unknown_query(request, ())
    value = controls(request).retention.create_plan(context, days=command.days, scope=command.scope)
    # The Control keeps its internal plan identifier as ``id``; the public v1
    # DTO and path contract consistently expose ``planId``.
    if "planId" not in value and "id" in value:
        value = {**value, "planId": value["id"]}
        value.pop("id", None)
    return DataEnvelope(data=RetentionPlanData.model_validate(value), meta=meta(request))


@router.post(
    "/plans/{planId}/commit", operation_id="commitLogRetentionPlan",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=DataEnvelope[ManagementOperationData], responses=_WRITE_ERRORS,
)
def commit_log_retention_plan(
    request: Request,
    context: Annotated[ManagementContext, Depends(require_capability(Capability.DESTRUCTIVE))],
    runtime: Annotated[ManagementRuntime, Depends(get_management_runtime)],
    plan_id: Annotated[str, Path(alias="planId", min_length=1, max_length=128)],
    if_match: Annotated[str, Header(alias="If-Match", min_length=1, max_length=128)],
) -> DataEnvelope[ManagementOperationData]:
    reject_unknown_query(request, ())
    operation = controls(request).retention.commit_plan(
        context, plan_id, expected_revision=if_match, operations=runtime.operations,
    )
    return DataEnvelope(data=operation_data(operation), meta=meta(request))


@router.delete(
    "/plans/{planId}", operation_id="cancelLogRetentionPlan",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={204: {"description": "Retention plan cancelled"}, **_WRITE_ERRORS},
)
def cancel_log_retention_plan(
    request: Request,
    context: Annotated[ManagementContext, Depends(require_capability(Capability.DESTRUCTIVE))],
    plan_id: Annotated[str, Path(alias="planId", min_length=1, max_length=128)],
) -> Response:
    reject_unknown_query(request, ())
    controls(request).retention.cancel_plan(context, plan_id)
    return Response(status_code=204)
