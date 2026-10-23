"""Translation Management API routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, Header, Request, Response, status

from src.management_auth.principal import Capability
from src.management_control import ManagementContext, ManagementErrorCode
from src.management_control.auxiliary import AuxiliaryControls

from ..dependencies import require_capability
from ..error_mapping import management_error_responses
from ..schemas.base import DataEnvelope
from ..schemas.operations import ManagementOperationData
from ..schemas.auxiliary_translation import (
    TranslationCacheData,
    TranslationLanguageData,
    TranslationLanguagesData,
    TranslationModelOverrideSchema,
    TranslationScopeSchema,
    TranslationSettingsData,
    TranslationSettingsPatch,
    TranslationTestRequest,
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
        "enabled": False,
        "model": "gpt-4.1-mini",
        "fallbackModel": "",
        "targetLanguage": "English",
        "timeoutSeconds": 10,
        "maxHistoryMessages": 20,
        "cacheTtlDays": 3,
        "cachePreloadCount": 100,
        "failureAlertThreshold": 10,
        "memoryCacheMaxMb": 100,
        "memoryCacheTtlSeconds": 7200,
        "translateSystemMessages": False,
        "scope": {"models": [], "channels": []},
        "modelOverrides": {},
        "prompt": "",
        "revision": "rev_example",
    },
    "meta": {"requestId": "request-example"},
}
_CACHE_EXAMPLE = {
    "data": {
        "entries": 12,
        "memoryEntries": 4,
        "memoryBytes": 4096,
        "hits": 8,
        "misses": 2,
        "revision": "rev_example",
    },
    "meta": {"requestId": "request-example"},
}
_LANGUAGES_EXAMPLE = {
    "data": {"items": [{"id": "English", "displayName": "🇬🇧 English"}], "total": 14},
    "meta": {"requestId": "request-example"},
}
_OPERATION_EXAMPLE = {
    "data": {
        "id": "op_example",
        "kind": "translation.test",
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
_ERRORS = (
    ManagementErrorCode.SESSION_REQUIRED,
    ManagementErrorCode.SESSION_EXPIRED,
    ManagementErrorCode.ORIGIN_DENIED,
    ManagementErrorCode.CAPABILITY_DENIED,
    ManagementErrorCode.VALIDATION_FAILED,
    ManagementErrorCode.REVISION_CONFLICT,
    ManagementErrorCode.SERVICE_NOT_READY,
)


def _settings(value) -> TranslationSettingsData:
    return TranslationSettingsData(
        enabled=value.enabled,
        model=value.model,
        fallbackModel=value.fallback_model,
        targetLanguage=value.target_language,
        timeoutSeconds=value.timeout_seconds,
        maxHistoryMessages=value.max_history_messages,
        cacheTtlDays=value.cache_ttl_days,
        cachePreloadCount=value.cache_preload_count,
        failureAlertThreshold=value.failure_alert_threshold,
        memoryCacheMaxMb=value.memory_cache_max_mb,
        memoryCacheTtlSeconds=value.memory_cache_ttl_seconds,
        translateSystemMessages=value.translate_system_messages,
        scope=TranslationScopeSchema(
            models=list(value.scope.models),
            channels=list(value.scope.channels),
        ),
        modelOverrides={
            model: TranslationModelOverrideSchema(body=override.get("body", {}))
            for model, override in value.model_overrides.items()
        },
        prompt=value.prompt,
        revision=value.revision,
        defaultPrompt=getattr(value, "default_prompt", "") or "",
    )


@router.get(
    "/translation",
    operation_id="getTranslationSettings",
    dependencies=[Depends(reject_unknown_query())],
    tags=["translation"],
    response_model=DataEnvelope[TranslationSettingsData],
    responses={**success_response(200, _SETTINGS_EXAMPLE), **management_error_responses(*_ERRORS)},
)
def get_translation_settings(
    request: Request,
    controls: Annotated[AuxiliaryControls, Depends(get_bound_auxiliary_controls)],
    context: Annotated[ManagementContext, Depends(require_capability(Capability.READ))],
) -> DataEnvelope[TranslationSettingsData]:
    return DataEnvelope(data=_settings(controls.translation.get_settings(context)), meta=response_meta(request))


@router.patch(
    "/translation",
    operation_id="updateTranslationSettings",
    dependencies=[Depends(reject_unknown_query())],
    tags=["translation"],
    response_model=DataEnvelope[TranslationSettingsData],
    responses={**success_response(200, _SETTINGS_EXAMPLE), **management_error_responses(*_ERRORS)},
)
def update_translation_settings(
    body: Annotated[
        TranslationSettingsPatch,
        Body(openapi_examples={"settings": {"value": {"enabled": True, "model": "gpt-4.1-mini"}}}),
    ],
    request: Request,
    controls: Annotated[AuxiliaryControls, Depends(get_bound_auxiliary_controls)],
    context: Annotated[ManagementContext, Depends(require_capability(Capability.WRITE))],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> DataEnvelope[TranslationSettingsData]:
    value = controls.translation.update_settings(
        context,
        body.model_dump(exclude_unset=True),
        expected_revision=if_match,
    )
    return DataEnvelope(data=_settings(value), meta=response_meta(request))


@router.get(
    "/translation/cache",
    operation_id="getTranslationCacheStats",
    dependencies=[Depends(reject_unknown_query())],
    tags=["translation"],
    response_model=DataEnvelope[TranslationCacheData],
    responses={**success_response(200, _CACHE_EXAMPLE), **management_error_responses(*_ERRORS)},
)
def get_translation_cache_stats(
    request: Request,
    controls: Annotated[AuxiliaryControls, Depends(get_bound_auxiliary_controls)],
    context: Annotated[ManagementContext, Depends(require_capability(Capability.READ))],
) -> DataEnvelope[TranslationCacheData]:
    value = controls.translation.cache_stats(context)
    return DataEnvelope(
        data=TranslationCacheData(
            entries=value.entries,
            memoryEntries=value.memory_entries,
            memoryBytes=value.memory_bytes,
            hits=value.hits,
            misses=value.misses,
            revision=value.revision,
        ),
        meta=response_meta(request),
    )


@router.delete(
    "/translation/cache",
    operation_id="clearTranslationCache",
    dependencies=[Depends(reject_unknown_query())],
    tags=["translation"],
    status_code=status.HTTP_204_NO_CONTENT,
    responses={**no_content_response("Translation cache cleared"), **management_error_responses(*_ERRORS)},
)
def clear_translation_cache(
    controls: Annotated[AuxiliaryControls, Depends(get_bound_auxiliary_controls)],
    context: Annotated[ManagementContext, Depends(require_capability(Capability.WRITE))],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Response:
    controls.translation.clear_cache(context, expected_revision=if_match)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/translation/actions/test",
    operation_id="testTranslation",
    dependencies=[Depends(reject_unknown_query())],
    tags=["translation"],
    status_code=status.HTTP_202_ACCEPTED,
    response_model=DataEnvelope[ManagementOperationData],
    responses={**success_response(202, _OPERATION_EXAMPLE), **management_error_responses(*_ERRORS)},
)
def test_translation(
    body: Annotated[
        TranslationTestRequest,
        Body(openapi_examples={"sample": {"value": {"text": "Translate this sentence."}}}),
    ],
    request: Request,
    controls: Annotated[AuxiliaryControls, Depends(get_bound_auxiliary_controls)],
    context: Annotated[ManagementContext, Depends(require_capability(Capability.WRITE))],
) -> DataEnvelope[ManagementOperationData]:
    operation = controls.translation.test_translation(context, body.text)
    return DataEnvelope(data=operation_data(operation), meta=response_meta(request))


@router.get(
    "/translation/languages",
    operation_id="listTranslationLanguages",
    dependencies=[Depends(reject_unknown_query())],
    tags=["translation"],
    response_model=DataEnvelope[TranslationLanguagesData],
    responses={**success_response(200, _LANGUAGES_EXAMPLE), **management_error_responses(*_ERRORS)},
)
def list_translation_languages(
    request: Request,
    controls: Annotated[AuxiliaryControls, Depends(get_bound_auxiliary_controls)],
    context: Annotated[ManagementContext, Depends(require_capability(Capability.READ))],
) -> DataEnvelope[TranslationLanguagesData]:
    values = controls.translation.list_languages(context)
    return DataEnvelope(
        data=TranslationLanguagesData(
            items=[TranslationLanguageData(id=item.id, displayName=item.display_name) for item in values],
            total=len(values),
        ),
        meta=response_meta(request),
    )
