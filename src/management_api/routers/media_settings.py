"""GPT image and xAI media settings Management API routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Path, Query, Request

from src.management_auth.principal import Capability
from src.management_control import ManagementContext, ManagementError, ManagementErrorCode
from src.management_control.auxiliary import AuxiliaryControls
from src.management_control.models import ModelOwnerRef, ModelSourceType

from ..dependencies import require_capability
from ..error_mapping import management_error_responses
from ..schemas.base import DataEnvelope
from ..schemas.auxiliary_media import (
    AntigravityMediaAccountOverrideData,
    AntigravityMediaModelAddRequest,
    AntigravityMediaModelRenameRequest,
    AntigravityMediaSettingsData,
    AntigravityMediaSettingsPatch,
    ImageAccountStateData,
    ImageAccountStatePatch,
    ImageSettingsData,
    ImageSettingsPatch,
    VideoSettingsPatch, MediaSourceData, MediaSourcesData, MediaSourcePatch,
    MediaKind,
    MediaModelAddRequest,
    MediaModelMutationData,
    MediaModelRenameRequest,
    MediaOwnerData,
    MediaOwnerType,
    XaiMediaSettingsData,
    XaiMediaSettingsPatch,
)
from .auxiliary_support import (
    get_bound_auxiliary_controls,
    reject_unknown_query,
    response_meta,
    success_response,
)


router = APIRouter()

_IMAGE_EXAMPLE = {
    "data": {
        "enabled": True,
        "cacheEnabled": False,
        "models": {"openai": ["gpt-image-2", "gpt-image-2.5"]},
        "requestTimeoutSeconds": 180,
        "jobTtlSeconds": None,
        "cachePath": "images",
        "cacheRetentionDays": 30,
        "cacheMaxBytes": 1073741824,
        "revision": "rev_example",
    },
    "meta": {"requestId": "request-example"},
}
_ACCOUNT_EXAMPLE = {
    "data": {
        "accountId": "openai:account@example.com",
        "email": "account@example.com",
        "oauthEnabled": True,
        "imageEnabled": True,
        "imageCooldownUntil": None,
        "missingAccountId": False,
        "revision": "rev_example",
    },
    "meta": {"requestId": "request-example"},
}
_XAI_EXAMPLE = {
    "data": {
        "imageModels": ["grok-imagine-image"],
        "videoModels": ["grok-imagine-video"],
        "jobTtlSeconds": 10800,
        "requestTimeoutSeconds": 180,
        "revision": "rev_example",
    },
    "meta": {"requestId": "request-example"},
}
_MEDIA_MUTATION_EXAMPLE = {
    "data": {
        "provider": "xai", "kind": "image",
        "owner": {"type": "global", "id": None},
        "modelId": "grok-imagine-image", "models": ["grok-imagine-image"],
        "status": "added", "revision": "rev_example",
    },
    "meta": {"requestId": "request-example"},
}
_ANTIGRAVITY_EXAMPLE = {
    "data": {
        "imageModels": ["gemini-3.1-flash-image"],
        "accountOverrides": [{
            "accountId": "antigravity:account@example.com:project",
            "imageModels": ["account-image"], "editable": False,
        }],
        "revision": "rev_example",
    },
    "meta": {"requestId": "request-example"},
}
_ERRORS = (
    ManagementErrorCode.SESSION_REQUIRED,
    ManagementErrorCode.SESSION_EXPIRED,
    ManagementErrorCode.ORIGIN_DENIED,
    ManagementErrorCode.CAPABILITY_DENIED,
    ManagementErrorCode.RESOURCE_NOT_FOUND,
    ManagementErrorCode.RESOURCE_CONFLICT,
    ManagementErrorCode.CONFIRMATION_REQUIRED,
    ManagementErrorCode.REVISION_CONFLICT,
    ManagementErrorCode.UNSUPPORTED_VALUE,
    ManagementErrorCode.VALIDATION_FAILED,
    ManagementErrorCode.SERVICE_NOT_READY,
)


def _image(value) -> ImageSettingsData:
    return ImageSettingsData(
        enabled=value.enabled,
        cacheEnabled=value.cache_enabled,
        models=value.models,
        defaultModel=value.default_model,
        requestTimeoutSeconds=value.request_timeout_seconds,
        jobTtlSeconds=value.job_ttl_seconds,
        cachePath=value.cache_path,
        cacheRetentionDays=value.cache_retention_days,
        cacheMaxBytes=value.cache_max_bytes,
        revision=value.revision,
    )


def _account(value) -> ImageAccountStateData:
    return ImageAccountStateData(
        accountId=value.account_id,
        email=value.email,
        oauthEnabled=value.oauth_enabled,
        imageEnabled=value.image_enabled,
        imageCooldownUntil=value.image_cooldown_until,
        missingAccountId=value.missing_account_id,
        independentEnabled=value.independent_enabled,
        independentAllowed=value.independent_allowed,
        effectiveAvailable=value.effective_available,
        unavailableReason=value.unavailable_reason,
        revision=value.revision,
    )


def _xai(value) -> XaiMediaSettingsData:
    return XaiMediaSettingsData(
        imageModels=list(value.image_models),
        videoModels=list(value.video_models),
        jobTtlSeconds=value.job_ttl_seconds,
        requestTimeoutSeconds=value.request_timeout_seconds,
        revision=value.revision,
    )


def _owner(value: MediaOwnerData) -> ModelOwnerRef:
    return ModelOwnerRef(ModelSourceType(value.type.value), value.id)


def _owner_query(owner_type: MediaOwnerType, owner_id: str | None) -> ModelOwnerRef:
    if (
        (owner_type is MediaOwnerType.GLOBAL and owner_id is not None)
        or (owner_type is MediaOwnerType.OAUTH and owner_id is None)
    ):
        raise ManagementError(ManagementErrorCode.VALIDATION_FAILED)
    return ModelOwnerRef(ModelSourceType(owner_type.value), owner_id)


def _mutation(value) -> MediaModelMutationData:
    return MediaModelMutationData(
        provider=value.provider,
        kind=value.kind.value,
        owner=MediaOwnerData(type=value.owner.type.value, id=value.owner.id),
        modelId=value.model_id,
        models=list(value.models),
        status=value.status,
        revision=value.revision,
    )


def _antigravity(value) -> AntigravityMediaSettingsData:
    return AntigravityMediaSettingsData(
        imageModels=list(value.image_models),
        accountOverrides=[
            AntigravityMediaAccountOverrideData(
                accountId=account_id, imageModels=list(models), editable=False,
            )
            for account_id, models in value.account_overrides
        ],
        revision=value.revision,
    )


@router.get(
    "/images/settings",
    operation_id="getImageSettings",
    dependencies=[Depends(reject_unknown_query())],
    tags=["images"],
    response_model=DataEnvelope[ImageSettingsData],
    responses={**success_response(200, _IMAGE_EXAMPLE), **management_error_responses(*_ERRORS)},
)
def get_image_settings(
    request: Request,
    controls: Annotated[AuxiliaryControls, Depends(get_bound_auxiliary_controls)],
    context: Annotated[ManagementContext, Depends(require_capability(Capability.READ))],
) -> DataEnvelope[ImageSettingsData]:
    return DataEnvelope(data=_image(controls.images.get_settings(context)), meta=response_meta(request))


@router.patch(
    "/images/settings",
    operation_id="updateImageSettings",
    dependencies=[Depends(reject_unknown_query())],
    tags=["images"],
    response_model=DataEnvelope[ImageSettingsData],
    responses={**success_response(200, _IMAGE_EXAMPLE), **management_error_responses(*_ERRORS)},
)
def update_image_settings(
    body: ImageSettingsPatch,
    request: Request,
    controls: Annotated[AuxiliaryControls, Depends(get_bound_auxiliary_controls)],
    context: Annotated[ManagementContext, Depends(require_capability(Capability.WRITE))],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> DataEnvelope[ImageSettingsData]:
    value = controls.images.update_settings(
        context,
        body.model_dump(exclude_unset=True),
        expected_revision=if_match,
    )
    return DataEnvelope(data=_image(value), meta=response_meta(request))


@router.get(
    "/images/accounts/{accountId}",
    operation_id="getImageAccountState",
    dependencies=[Depends(reject_unknown_query())],
    tags=["images"],
    response_model=DataEnvelope[ImageAccountStateData],
    responses={**success_response(200, _ACCOUNT_EXAMPLE), **management_error_responses(*_ERRORS)},
)
def get_image_account_state(
    account_id: Annotated[str, Path(alias="accountId", min_length=1, max_length=512)],
    request: Request,
    controls: Annotated[AuxiliaryControls, Depends(get_bound_auxiliary_controls)],
    context: Annotated[ManagementContext, Depends(require_capability(Capability.READ))],
) -> DataEnvelope[ImageAccountStateData]:
    return DataEnvelope(data=_account(controls.images.get_account(context, account_id)), meta=response_meta(request))


@router.patch(
    "/images/accounts/{accountId}",
    operation_id="updateImageAccountState",
    dependencies=[Depends(reject_unknown_query())],
    tags=["images"],
    response_model=DataEnvelope[ImageAccountStateData],
    responses={**success_response(200, _ACCOUNT_EXAMPLE), **management_error_responses(*_ERRORS)},
)
def update_image_account_state(
    account_id: Annotated[str, Path(alias="accountId", min_length=1, max_length=512)],
    body: ImageAccountStatePatch,
    request: Request,
    controls: Annotated[AuxiliaryControls, Depends(get_bound_auxiliary_controls)],
    context: Annotated[ManagementContext, Depends(require_capability(Capability.WRITE))],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> DataEnvelope[ImageAccountStateData]:
    value = controls.images.update_account(
        context,
        account_id,
        enabled=body.enabled,
        independent_enabled=body.independentEnabled,
        expected_revision=if_match,
    )
    return DataEnvelope(data=_account(value), meta=response_meta(request))


@router.get(
    "/xai/media-settings",
    operation_id="getXaiMediaSettings",
    dependencies=[Depends(reject_unknown_query())],
    tags=["xai-media"],
    response_model=DataEnvelope[XaiMediaSettingsData],
    responses={**success_response(200, _XAI_EXAMPLE), **management_error_responses(*_ERRORS)},
)
def get_xai_media_settings(
    request: Request,
    controls: Annotated[AuxiliaryControls, Depends(get_bound_auxiliary_controls)],
    context: Annotated[ManagementContext, Depends(require_capability(Capability.READ))],
) -> DataEnvelope[XaiMediaSettingsData]:
    return DataEnvelope(data=_xai(controls.xai_media.get_settings(context)), meta=response_meta(request))


@router.patch(
    "/xai/media-settings",
    operation_id="updateXaiMediaSettings",
    dependencies=[Depends(reject_unknown_query())],
    tags=["xai-media"],
    response_model=DataEnvelope[XaiMediaSettingsData],
    responses={**success_response(200, _XAI_EXAMPLE), **management_error_responses(*_ERRORS)},
)
def update_xai_media_settings(
    body: XaiMediaSettingsPatch,
    request: Request,
    controls: Annotated[AuxiliaryControls, Depends(get_bound_auxiliary_controls)],
    context: Annotated[ManagementContext, Depends(require_capability(Capability.WRITE))],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> DataEnvelope[XaiMediaSettingsData]:
    value = controls.xai_media.update_settings(
        context,
        body.model_dump(exclude_unset=True),
        expected_revision=if_match,
    )
    return DataEnvelope(data=_xai(value), meta=response_meta(request))


@router.post(
    "/xai/media-models/{kind}",
    operation_id="addXaiMediaModel",
    dependencies=[Depends(reject_unknown_query())],
    tags=["xai-media"],
    response_model=DataEnvelope[MediaModelMutationData],
    responses={**success_response(200, _MEDIA_MUTATION_EXAMPLE), **management_error_responses(*_ERRORS)},
)
def add_xai_media_model(
    kind: MediaKind,
    body: MediaModelAddRequest,
    request: Request,
    controls: Annotated[AuxiliaryControls, Depends(get_bound_auxiliary_controls)],
    context: Annotated[ManagementContext, Depends(require_capability(Capability.WRITE))],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> DataEnvelope[MediaModelMutationData]:
    value = controls.xai_media.add_model(
        context, kind=kind.value, model_id=body.modelId,
        expected_revision=if_match,
    )
    return DataEnvelope(data=_mutation(value), meta=response_meta(request))


@router.patch(
    "/xai/media-models/{kind}/{modelId:path}",
    operation_id="renameXaiMediaModel",
    dependencies=[Depends(reject_unknown_query())],
    tags=["xai-media"],
    response_model=DataEnvelope[MediaModelMutationData],
    responses={**success_response(200, _MEDIA_MUTATION_EXAMPLE), **management_error_responses(*_ERRORS)},
)
def rename_xai_media_model(
    kind: MediaKind,
    model_id: Annotated[str, Path(alias="modelId", min_length=1, max_length=128)],
    body: MediaModelRenameRequest,
    request: Request,
    controls: Annotated[AuxiliaryControls, Depends(get_bound_auxiliary_controls)],
    context: Annotated[ManagementContext, Depends(require_capability(Capability.WRITE))],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> DataEnvelope[MediaModelMutationData]:
    value = controls.xai_media.rename_model(
        context, kind=kind.value, old_model_id=model_id,
        new_model_id=body.newModelId, expected_revision=if_match,
    )
    return DataEnvelope(data=_mutation(value), meta=response_meta(request))


@router.delete(
    "/xai/media-models/{kind}/{modelId:path}",
    operation_id="removeXaiMediaModel",
    dependencies=[Depends(reject_unknown_query())],
    tags=["xai-media"],
    response_model=DataEnvelope[MediaModelMutationData],
    responses={**success_response(200, _MEDIA_MUTATION_EXAMPLE), **management_error_responses(*_ERRORS)},
)
def remove_xai_media_model(
    kind: MediaKind,
    model_id: Annotated[str, Path(alias="modelId", min_length=1, max_length=128)],
    request: Request,
    controls: Annotated[AuxiliaryControls, Depends(get_bound_auxiliary_controls)],
    context: Annotated[ManagementContext, Depends(require_capability(Capability.DESTRUCTIVE))],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> DataEnvelope[MediaModelMutationData]:
    value = controls.xai_media.remove_model(
        context, kind=kind.value, model_id=model_id,
        expected_revision=if_match,
    )
    return DataEnvelope(data=_mutation(value), meta=response_meta(request))


# AG image management routes retired; stored settings and media history remain.


@router.get('/videos/settings', operation_id='getVideoSettings', tags=['videos'],
    dependencies=[Depends(reject_unknown_query())], response_model=DataEnvelope[ImageSettingsData],
    responses={**success_response(200, _IMAGE_EXAMPLE), **management_error_responses(*_ERRORS)})
def get_video_settings(request: Request,
    controls: Annotated[AuxiliaryControls, Depends(get_bound_auxiliary_controls)],
    context: Annotated[ManagementContext, Depends(require_capability(Capability.READ))]):
    return DataEnvelope(data=_image(controls.videos.get_settings(context)), meta=response_meta(request))


@router.patch('/videos/settings', operation_id='updateVideoSettings', tags=['videos'],
    dependencies=[Depends(reject_unknown_query())], response_model=DataEnvelope[ImageSettingsData],
    responses={**success_response(200, _IMAGE_EXAMPLE), **management_error_responses(*_ERRORS)})
def update_video_settings(body: VideoSettingsPatch, request: Request,
    controls: Annotated[AuxiliaryControls, Depends(get_bound_auxiliary_controls)],
    context: Annotated[ManagementContext, Depends(require_capability(Capability.WRITE))],
    if_match: Annotated[str | None, Header(alias='If-Match')] = None):
    value = controls.videos.update_settings(context, body.model_dump(exclude_unset=True), expected_revision=if_match)
    return DataEnvelope(data=_image(value), meta=response_meta(request))


_SOURCE_EXAMPLE = {'data': {'sourceId': 'oauth:xai:example', 'label': 'account@example.com',
    'provider': 'xai', 'enabled': True, 'effectiveAvailable': True, 'canEnable': True,
    'unavailableReason': None, 'revision': 'rev_example'}, 'meta': {'requestId': 'request-example'}}


def _source(value):
    return MediaSourceData(sourceId=value['source_id'], label=value['label'], provider=value['provider'],
        enabled=value['enabled'], effectiveAvailable=value['effective_available'], canEnable=value['can_enable'],
        unavailableReason=value['unavailable_reason'], revision=value['revision'])


@router.get('/media/{kind}/sources', operation_id='listMediaSources', tags=['media'],
    dependencies=[Depends(reject_unknown_query())], response_model=DataEnvelope[MediaSourcesData],
    responses={**success_response(200, {'data': {'sources': [_SOURCE_EXAMPLE['data']]}, 'meta': _SOURCE_EXAMPLE['meta']}), **management_error_responses(*_ERRORS)})
def list_media_sources(kind: MediaKind, request: Request,
    controls: Annotated[AuxiliaryControls, Depends(get_bound_auxiliary_controls)],
    context: Annotated[ManagementContext, Depends(require_capability(Capability.READ))]):
    control = controls.images if kind is MediaKind.IMAGE else controls.videos
    return DataEnvelope(data=MediaSourcesData(sources=[_source(item) for item in control.list_sources(context)]), meta=response_meta(request))


@router.patch('/media/{kind}/sources/{sourceId:path}', operation_id='updateMediaSource', tags=['media'],
    dependencies=[Depends(reject_unknown_query())], response_model=DataEnvelope[MediaSourceData],
    responses={**success_response(200, _SOURCE_EXAMPLE), **management_error_responses(*_ERRORS)})
def update_media_source(kind: MediaKind, source_id: Annotated[str, Path(alias='sourceId', min_length=1, max_length=512)],
    body: MediaSourcePatch, request: Request,
    controls: Annotated[AuxiliaryControls, Depends(get_bound_auxiliary_controls)],
    context: Annotated[ManagementContext, Depends(require_capability(Capability.WRITE))],
    if_match: Annotated[str | None, Header(alias='If-Match')] = None):
    control = controls.images if kind is MediaKind.IMAGE else controls.videos
    value = control.update_source(context, source_id, enabled=body.enabled, expected_revision=if_match)
    return DataEnvelope(data=_source(value), meta=response_meta(request))
