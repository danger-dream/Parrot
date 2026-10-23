"""Application update Management API routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, Header, Path, Query, Request, Response, status

from src.management_auth.principal import Capability
from src.management_control import ManagementContext, ManagementErrorCode
from src.management_control.auxiliary import AuxiliaryControls

from ..dependencies import require_capability
from ..error_mapping import management_error_responses
from ..schemas.base import DataEnvelope
from ..schemas.operations import ManagementOperationData
from ..schemas.auxiliary_updates import (
    ActivateStagedUpdateRequest,
    StageUpdateOperationData,
    UpdateBackupData,
    UpdateBackupListData,
    UpdateBackupListEnvelope,
    UpdateBackupQuery,
    UpdateCheckData,
    UpdateFailureLogData,
    UpdatePageMeta,
    UpdateSettingsData,
    UpdateSettingsPatch,
    UpdateActivationPlanData,
    UpdateStateData,
)
from .auxiliary_support import (
    get_bound_auxiliary_controls,
    no_content_response,
    operation_data,
    reject_unknown_query,
    response_meta,
    success_response,
)


router = APIRouter()

_SETTINGS_EXAMPLE = {
    "data": {
        "enabled": True,
        "includePrerelease": False,
        "autoUpdate": False,
        "intervalSeconds": 3600,
        "ignoredVersions": [],
        "revision": "rev_example",
    },
    "meta": {"requestId": "request-example"},
}
_CHECK_EXAMPLE = {
    "data": {
        "currentVersion": "0.31.13",
        "candidateVersion": "0.32.0",
        "candidateName": "Parrot 0.32.0",
        "changelog": "Release notes",
        "publishedAt": "2026-01-02T03:04:05Z",
        "prerelease": False,
        "releaseUrl": "https://example.invalid/releases/0.32.0",
        "newer": True,
        "ignored": False,
        "revision": "rev_example",
    },
    "meta": {"requestId": "request-example"},
}
_BACKUPS_EXAMPLE = {
    "data": {
        "items": [{
            "ref": "backup-20260102",
            "version": "0.31.13",
            "targetVersion": "0.32.0",
            "mode": "docker",
            "createdAt": "2026-01-02T03:04:05Z",
            "revision": "rev_example",
        }]
    },
    "meta": {"requestId": "request-example", "page": 1, "pageSize": 50, "total": 1, "hasNext": False},
}
_FAILURE_LOG_EXAMPLE = {
    "data": {"content": "health verification failed", "revision": "rev_example"},
    "meta": {"requestId": "request-example"},
}
_OPERATION_EXAMPLE = {
    "data": {
        "id": "op_example",
        "kind": "updates.stage",
        "status": "queued",
        "progress": None,
        "createdAt": "2026-01-02T03:04:05Z",
        "startedAt": None,
        "finishedAt": None,
        "result": None,
        "error": None,
        "cancellable": False,
    },
    "meta": {"requestId": "request-example"},
}
_STAGE_OPERATION_EXAMPLE = {
    "data": {
        **_OPERATION_EXAMPLE["data"],
        "activationPlanToken": "<one-time-stage-plan>",
    },
    "meta": _OPERATION_EXAMPLE["meta"],
}
_ERRORS = (
    ManagementErrorCode.INVALID_REQUEST,
    ManagementErrorCode.INVALID_OPERATION_STATE,
    ManagementErrorCode.SESSION_REQUIRED,
    ManagementErrorCode.SESSION_EXPIRED,
    ManagementErrorCode.ORIGIN_DENIED,
    ManagementErrorCode.CAPABILITY_DENIED,
    ManagementErrorCode.RESOURCE_NOT_FOUND,
    ManagementErrorCode.REVISION_CONFLICT,
    ManagementErrorCode.STATE_CONFLICT,
    ManagementErrorCode.VALIDATION_FAILED,
    ManagementErrorCode.OPERATION_ALREADY_RUNNING,
    ManagementErrorCode.UPSTREAM_ERROR,
    ManagementErrorCode.SERVICE_NOT_READY,
    ManagementErrorCode.DEPENDENCY_UNAVAILABLE,
)


def _settings(value) -> UpdateSettingsData:
    return UpdateSettingsData(
        enabled=value.enabled,
        includePrerelease=value.include_prerelease,
        autoUpdate=value.auto_update,
        intervalSeconds=value.interval_seconds,
        ignoredVersions=list(value.ignored_versions),
        revision=value.revision,
    )


def _check(value) -> UpdateCheckData:
    return UpdateCheckData(
        currentVersion=value.current_version,
        candidateVersion=value.candidate_version,
        candidateName=value.candidate_name,
        changelog=value.changelog,
        publishedAt=value.published_at,
        prerelease=value.prerelease,
        releaseUrl=value.release_url,
        newer=value.newer,
        ignored=value.ignored,
        revision=value.revision,
    )


@router.get(
    "/updates/settings",
    operation_id="getSettings",
    dependencies=[Depends(reject_unknown_query())],
    tags=["updates"],
    response_model=DataEnvelope[UpdateSettingsData],
    responses={**success_response(200, _SETTINGS_EXAMPLE), **management_error_responses(*_ERRORS)},
)
def get_update_settings(
    request: Request,
    controls: Annotated[AuxiliaryControls, Depends(get_bound_auxiliary_controls)],
    context: Annotated[ManagementContext, Depends(require_capability(Capability.READ))],
) -> DataEnvelope[UpdateSettingsData]:
    return DataEnvelope(data=_settings(controls.updates.get_settings(context)), meta=response_meta(request))


@router.patch(
    "/updates/settings",
    operation_id="updateSettings",
    dependencies=[Depends(reject_unknown_query())],
    tags=["updates"],
    response_model=DataEnvelope[UpdateSettingsData],
    responses={**success_response(200, _SETTINGS_EXAMPLE), **management_error_responses(*_ERRORS)},
)
def update_settings(
    body: UpdateSettingsPatch,
    request: Request,
    controls: Annotated[AuxiliaryControls, Depends(get_bound_auxiliary_controls)],
    context: Annotated[ManagementContext, Depends(require_capability(Capability.WRITE))],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> DataEnvelope[UpdateSettingsData]:
    value = controls.updates.update_settings(
        context,
        body.model_dump(exclude_unset=True),
        expected_revision=if_match,
    )
    return DataEnvelope(data=_settings(value), meta=response_meta(request))


@router.post(
    "/updates/actions/check",
    operation_id="checkForUpdates",
    dependencies=[Depends(reject_unknown_query())],
    tags=["updates"],
    response_model=DataEnvelope[UpdateCheckData],
    responses={**success_response(200, _CHECK_EXAMPLE), **management_error_responses(*_ERRORS)},
)
def check_for_updates(
    request: Request,
    controls: Annotated[AuxiliaryControls, Depends(get_bound_auxiliary_controls)],
    context: Annotated[ManagementContext, Depends(require_capability(Capability.WRITE))],
) -> DataEnvelope[UpdateCheckData]:
    return DataEnvelope(data=_check(controls.updates.check(context)), meta=response_meta(request))


@router.put(
    "/updates/ignored-versions/{version}",
    operation_id="ignoreUpdateVersion",
    dependencies=[Depends(reject_unknown_query())],
    tags=["updates"],
    response_model=DataEnvelope[UpdateSettingsData],
    responses={**success_response(200, _SETTINGS_EXAMPLE), **management_error_responses(*_ERRORS)},
)
def ignore_update_version(
    version: Annotated[str, Path(min_length=1, max_length=128)],
    request: Request,
    controls: Annotated[AuxiliaryControls, Depends(get_bound_auxiliary_controls)],
    context: Annotated[ManagementContext, Depends(require_capability(Capability.WRITE))],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> DataEnvelope[UpdateSettingsData]:
    value = controls.updates.ignore_version(context, version, expected_revision=if_match)
    return DataEnvelope(data=_settings(value), meta=response_meta(request))


@router.delete(
    "/updates/ignored-versions/{version}",
    operation_id="unignoreUpdateVersion",
    dependencies=[Depends(reject_unknown_query())],
    tags=["updates"],
    status_code=status.HTTP_204_NO_CONTENT,
    responses={**no_content_response("Update version unignored"), **management_error_responses(*_ERRORS)},
)
def unignore_update_version(
    version: Annotated[str, Path(min_length=1, max_length=128)],
    controls: Annotated[AuxiliaryControls, Depends(get_bound_auxiliary_controls)],
    context: Annotated[ManagementContext, Depends(require_capability(Capability.WRITE))],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Response:
    controls.updates.unignore_version(context, version, expected_revision=if_match)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/updates/backups",
    operation_id="listUpdateBackups",
    dependencies=[Depends(reject_unknown_query("mode", "sort", "page", "pageSize"))],
    tags=["updates"],
    response_model=UpdateBackupListEnvelope,
    responses={**success_response(200, _BACKUPS_EXAMPLE), **management_error_responses(*_ERRORS)},
)
def list_update_backups(
    request: Request,
    controls: Annotated[AuxiliaryControls, Depends(get_bound_auxiliary_controls)],
    context: Annotated[ManagementContext, Depends(require_capability(Capability.READ))],
    filters: Annotated[UpdateBackupQuery, Query()],
) -> UpdateBackupListEnvelope:
    value = controls.updates.list_backups(
        context,
        mode=filters.mode,
        sort=filters.sort,
        page=filters.page,
        page_size=filters.pageSize,
    )
    items = [
        UpdateBackupData(
            ref=item.ref,
            version=item.version,
            targetVersion=item.target_version,
            mode=item.mode,
            createdAt=item.created_at,
            revision=item.revision,
        )
        for item in value.items
    ]
    return UpdateBackupListEnvelope(
        data=UpdateBackupListData(items=items),
        meta=UpdatePageMeta(
            requestId=response_meta(request).requestId,
            page=value.page,
            pageSize=value.page_size,
            total=value.total,
            hasNext=value.has_next,
        ),
    )


@router.get(
    "/updates/failure-log",
    operation_id="getUpdateFailureLog",
    dependencies=[Depends(reject_unknown_query())],
    tags=["updates"],
    response_model=DataEnvelope[UpdateFailureLogData],
    responses={**success_response(200, _FAILURE_LOG_EXAMPLE), **management_error_responses(*_ERRORS)},
)
def get_update_failure_log(
    request: Request,
    controls: Annotated[AuxiliaryControls, Depends(get_bound_auxiliary_controls)],
    context: Annotated[ManagementContext, Depends(require_capability(Capability.LOG_BODY_READ))],
) -> DataEnvelope[UpdateFailureLogData]:
    value = controls.updates.failure_log(context)
    return DataEnvelope(
        data=UpdateFailureLogData(content=value.content, revision=value.revision),
        meta=response_meta(request),
    )


@router.post(
    "/updates/{version}/actions/stage",
    operation_id="stageUpdate",
    dependencies=[Depends(reject_unknown_query())],
    tags=["updates"],
    status_code=status.HTTP_202_ACCEPTED,
    response_model=DataEnvelope[StageUpdateOperationData],
    responses={**success_response(202, _STAGE_OPERATION_EXAMPLE), **management_error_responses(*_ERRORS)},
)
def stage_update(
    version: Annotated[str, Path(min_length=1, max_length=128)],
    request: Request,
    controls: Annotated[AuxiliaryControls, Depends(get_bound_auxiliary_controls)],
    context: Annotated[ManagementContext, Depends(require_capability(Capability.UPDATE))],
) -> DataEnvelope[StageUpdateOperationData]:
    submission = controls.updates.stage_update(context, version)
    operation = operation_data(submission.operation)
    data = StageUpdateOperationData(
        **operation.model_dump(),
        activationPlanToken=submission.activation_plan_token,
    )
    return DataEnvelope(data=data, meta=response_meta(request))


@router.post(
    "/updates/staged/actions/restart",
    operation_id="activateStagedUpdate",
    dependencies=[Depends(reject_unknown_query())],
    tags=["updates"],
    status_code=status.HTTP_202_ACCEPTED,
    response_model=DataEnvelope[ManagementOperationData],
    responses={**success_response(202, _OPERATION_EXAMPLE), **management_error_responses(*_ERRORS)},
)
def activate_staged_update(
    body: Annotated[
        ActivateStagedUpdateRequest,
        Body(openapi_examples={"confirmation": {"value": {"planToken": "<one-time-stage-plan>"}}}),
    ],
    request: Request,
    controls: Annotated[AuxiliaryControls, Depends(get_bound_auxiliary_controls)],
    context: Annotated[ManagementContext, Depends(require_capability(Capability.UPDATE))],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> DataEnvelope[ManagementOperationData]:
    operation = controls.updates.activate_staged(
        context,
        plan_token=body.planToken,
        expected_revision=if_match,
    )
    return DataEnvelope(data=operation_data(operation), meta=response_meta(request))


@router.delete(
    "/updates/staged",
    operation_id="cancelStagedUpdate",
    dependencies=[Depends(reject_unknown_query())],
    tags=["updates"],
    status_code=status.HTTP_204_NO_CONTENT,
    responses={**no_content_response("Staged update cancelled"), **management_error_responses(*_ERRORS)},
)
def cancel_staged_update(
    controls: Annotated[AuxiliaryControls, Depends(get_bound_auxiliary_controls)],
    context: Annotated[ManagementContext, Depends(require_capability(Capability.UPDATE))],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Response:
    controls.updates.cancel_staged(context, expected_revision=if_match)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


_STATE_EXAMPLE = {
    "stage": "staged", "mode": "git", "fromVersion": "0.35.0", "toVersion": None,
    "targetVersion": "v0.36.0", "message": None, "repository": "danger-dream/Parrot",
    "currentVersion": "0.35.0", "busy": False, "activationAvailable": True, "revision": "rev_example",
}


@router.get(
    "/updates/state",
    operation_id="getUpdateState",
    dependencies=[Depends(reject_unknown_query())],
    tags=["updates"],
    response_model=DataEnvelope[UpdateStateData],
    responses={**success_response(200, {"data": _STATE_EXAMPLE, "meta": {"requestId": "request-example"}}), **management_error_responses(*_ERRORS)},
)
def get_update_state(
    request: Request,
    controls: Annotated[AuxiliaryControls, Depends(get_bound_auxiliary_controls)],
    context: Annotated[ManagementContext, Depends(require_capability(Capability.READ))],
) -> DataEnvelope[UpdateStateData]:
    value = controls.updates.state_view(context)
    return DataEnvelope(data=UpdateStateData(
        stage=value["stage"], mode=value["mode"], fromVersion=value["from_version"],
        toVersion=value["to_version"], targetVersion=value["target_version"], message=value["message"],
        repository=value["repository"], currentVersion=value["current_version"], busy=value["busy"],
        activationAvailable=value["activation_available"], revision=value["revision"],
    ), meta=response_meta(request))


@router.get(
    "/updates/check",
    operation_id="getCachedUpdateCheck",
    dependencies=[Depends(reject_unknown_query())],
    tags=["updates"],
    response_model=DataEnvelope[UpdateCheckData],
    responses={**success_response(200, _CHECK_EXAMPLE), **management_error_responses(*_ERRORS)},
)
def get_cached_update_check(
    request: Request,
    controls: Annotated[AuxiliaryControls, Depends(get_bound_auxiliary_controls)],
    context: Annotated[ManagementContext, Depends(require_capability(Capability.READ))],
) -> DataEnvelope[UpdateCheckData]:
    """Last check result (with release notes) without contacting GitHub."""
    return DataEnvelope(data=_check(controls.updates.cached_check(context)), meta=response_meta(request))


@router.post(
    "/updates/staged/actions/activation-plan",
    operation_id="createUpdateActivationPlan",
    dependencies=[Depends(reject_unknown_query())],
    tags=["updates"],
    response_model=DataEnvelope[UpdateActivationPlanData],
    responses={**success_response(200, {"data": {"planToken": "<one-time-stage-plan>", "targetVersion": "v0.36.0",
                                                "revision": "rev_example", "expiresAt": "2026-01-02T03:14:05Z"},
                                       "meta": {"requestId": "request-example"}}),
               **management_error_responses(*_ERRORS)},
)
def create_update_activation_plan(
    request: Request,
    controls: Annotated[AuxiliaryControls, Depends(get_bound_auxiliary_controls)],
    context: Annotated[ManagementContext, Depends(require_capability(Capability.UPDATE))],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> DataEnvelope[UpdateActivationPlanData]:
    value = controls.updates.issue_activation_plan(context, expected_revision=if_match)
    return DataEnvelope(data=UpdateActivationPlanData(
        planToken=value["plan_token"], targetVersion=value["target_version"],
        revision=value["revision"], expiresAt=value["expires_at"],
    ), meta=response_meta(request))
