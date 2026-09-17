"""Management API adapter for downstream inference API keys."""

from __future__ import annotations

from threading import RLock
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Header, Path, Query, Request, Response, status

from src.management_auth import Capability
from src.management_control import ManagementContext, ManagementErrorCode
from src.management_control.apikey import (
    ApiKeyControl,
    ApiKeyEnabledFilter,
    ApiKeyLimiterSnapshot,
    ApiKeyModelUsage,
    ApiKeyProvenance,
    ApiKeySort,
    ApiKeySource,
    ApiKeyUsage,
    ApiKeyView,
)

from ._strict_query import reject_unknown_query_parameters
from ..dependencies import (
    ManagementRuntime,
    get_management_control_owner,
    get_management_runtime,
    management_request_id,
    require_capability,
)
from ..error_mapping import management_error_responses
from ..response_helpers import response_meta as _meta
from ..schemas.apikey import (
    ApiKeyCreateRequest,
    ApiKeyData,
    ApiKeyEnvelope,
    ApiKeyLimitOverrideData,
    ApiKeyLimiterData,
    ApiKeyLimiterEnvelope,
    ApiKeyListData,
    ApiKeyListEnvelope,
    ApiKeyListMeta,
    ApiKeyModelUsageData,
    ApiKeyOrderData,
    ApiKeyOrderEnvelope,
    ApiKeyOrderRequest,
    ApiKeyRegenerateRequest,
    ApiKeyReplaceSecretRequest,
    ApiKeyReplacementPlanData,
    ApiKeyReplacementPlanEnvelope,
    ApiKeySecretData,
    ApiKeySecretEnvelope,
    ApiKeyStatsData,
    ApiKeyStatsEnvelope,
    ApiKeyUpdateRequest,
    ApiKeyUsageData,
    KeyId,
)


router = APIRouter(tags=["api-keys"])
_BIND_LOCK = RLock()

_API_KEY_LIST_QUERY_PARAMETERS = frozenset(
    {"page", "pageSize", "enabled", "source", "name", "sort"}
)

_EMPTY_USAGE = {
    "total": 0,
    "successCount": 0,
    "errorCount": 0,
    "inputTokens": 0,
    "outputTokens": 0,
    "cacheCreationTokens": 0,
    "cacheReadTokens": 0,
    "averageTps": None,
    "maximumTps": None,
    "minimumTps": None,
    "costTicks": 0,
    "actualCostTicks": 0,
    "estimatedCostTicks": 0,
    "costedSuccess": 0,
    "unpricedSuccess": 0,
}
_LIMITER_EXAMPLE = {
    "enabled": True,
    "inFlight": 0,
    "maxConcurrent": 5,
    "maxQueue": 50,
    "queueWaitSeconds": 1800,
    "waiting": 0,
    "oldestWaitSeconds": 0,
    "unlimited": False,
    "enabledSource": "global",
    "maxConcurrentSource": "global",
    "maxQueueSource": "global",
    "queueWaitSource": "global",
}
_KEY_EXAMPLE = {
    "keyId": "console-client",
    "name": "console-client",
    "order": 1,
    "enabled": True,
    "source": "generated",
    "maskedHint": "ccp-…19af",
    "allowImages": False,
    "allowVideos": False,
    "allowMcp": False,
    "mcpTools": [],
    "allowedModels": [],
    "limitOverride": None,
    "limiter": _LIMITER_EXAMPLE,
    "monthStats": _EMPTY_USAGE,
    "modelStats": [],
    "revision": '"ak-example"',
}
_META_EXAMPLE = {"requestId": "request-example"}


def _success(http_status: int, data: object, *, meta: dict | None = None) -> dict[int, dict]:
    return {
        http_status: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {"data": data, "meta": meta or _META_EXAMPLE}
                }
            },
        }
    }


def _errors(*codes: ManagementErrorCode) -> dict[int, dict]:
    return management_error_responses(*codes)


def _mark_sensitive(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def get_api_key_control(
    request: Request,
    runtime: Annotated[ManagementRuntime, Depends(get_management_runtime)],
) -> ApiKeyControl:
    current = getattr(request.app.state, "management_apikey_control", None)
    owner = getattr(request.app.state, "management_apikey_control_runtime", None)
    # An explicitly injected control (owner absent) is a supported composition
    # and test seam. Automatically-created controls are replaced with runtime.
    if isinstance(current, ApiKeyControl) and (owner is None or owner is runtime):
        return current
    with _BIND_LOCK:
        current = getattr(request.app.state, "management_apikey_control", None)
        owner = getattr(request.app.state, "management_apikey_control_runtime", None)
        if isinstance(current, ApiKeyControl) and (owner is None or owner is runtime):
            return current
        current = get_management_control_owner(runtime).api_keys
        request.app.state.management_apikey_control = current
        request.app.state.management_apikey_control_runtime = runtime
        return current


def _usage(value: ApiKeyUsage) -> ApiKeyUsageData:
    return ApiKeyUsageData(
        total=value.total,
        successCount=value.success_count,
        errorCount=value.error_count,
        inputTokens=value.input_tokens,
        outputTokens=value.output_tokens,
        cacheCreationTokens=value.cache_creation_tokens,
        cacheReadTokens=value.cache_read_tokens,
        averageTps=value.avg_tps,
        maximumTps=value.max_tps,
        minimumTps=value.min_tps,
        costTicks=value.cost_ticks,
        actualCostTicks=value.actual_cost_ticks,
        estimatedCostTicks=value.estimated_cost_ticks,
        costedSuccess=value.costed_success,
        unpricedSuccess=value.unpriced_success,
    )


def _model_usage(value: ApiKeyModelUsage) -> ApiKeyModelUsageData:
    return ApiKeyModelUsageData(model=value.model, usage=_usage(value.usage))


def _limiter(value: ApiKeyLimiterSnapshot) -> ApiKeyLimiterData:
    return ApiKeyLimiterData(
        enabled=value.enabled,
        inFlight=value.in_flight,
        maxConcurrent=value.max_concurrent,
        maxQueue=value.max_queue,
        queueWaitSeconds=value.queue_wait_seconds,
        waiting=value.waiting,
        oldestWaitSeconds=value.oldest_wait_seconds,
        unlimited=value.unlimited,
        enabledSource=value.enabled_source,
        maxConcurrentSource=value.max_concurrent_source,
        maxQueueSource=value.max_queue_source,
        queueWaitSource=value.queue_wait_source,
    )


def _key(value: ApiKeyView) -> ApiKeyData:
    override = None
    if value.limit_override is not None:
        override = ApiKeyLimitOverrideData(
            enabled=value.limit_override.enabled,
            maxConcurrent=value.limit_override.max_concurrent,
            maxQueue=value.limit_override.max_queue,
            queueWaitSeconds=value.limit_override.queue_wait_seconds,
        )
    return ApiKeyData(
        keyId=value.key_id,
        name=value.name,
        order=value.order,
        enabled=value.enabled,
        source=value.source,
        maskedHint=value.masked_hint,
        allowImages=value.allow_images,
        allowVideos=value.allow_videos,
        allowMcp=value.allow_mcp,
        mcpTools=list(value.mcp_tools),
        allowedModels=list(value.allowed_models),
        limitOverride=override,
        limiter=_limiter(value.limiter),
        monthStats=_usage(value.month_stats),
        modelStats=[_model_usage(item) for item in value.model_stats],
        revision=value.revision,
    )


_COMMON_READ_ERRORS = (
    ManagementErrorCode.SESSION_REQUIRED,
    ManagementErrorCode.SESSION_EXPIRED,
    ManagementErrorCode.CAPABILITY_DENIED,
    ManagementErrorCode.ORIGIN_DENIED,
    ManagementErrorCode.VALIDATION_FAILED,
    ManagementErrorCode.DEPENDENCY_UNAVAILABLE,
    ManagementErrorCode.SERVICE_NOT_READY,
)
_COMMON_WRITE_ERRORS = (*_COMMON_READ_ERRORS, ManagementErrorCode.REVISION_CONFLICT)


@router.get(
    "/api-keys",
    operation_id="listApiKeys",
    response_model=ApiKeyListEnvelope,
    responses={
        **_success(
            200,
            {"items": [_KEY_EXAMPLE]},
            meta={
                "requestId": "request-example",
                "page": 1,
                "pageSize": 50,
                "total": 1,
                "hasNext": False,
                "revision": '"aks-example"',
            },
        ),
        **_errors(*_COMMON_READ_ERRORS),
    },
)
def list_api_keys(
    request: Request,
    context: Annotated[ManagementContext, Depends(require_capability(Capability.READ))],
    control: Annotated[ApiKeyControl, Depends(get_api_key_control)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(alias="pageSize", ge=1, le=200)] = 50,
    enabled: ApiKeyEnabledFilter = ApiKeyEnabledFilter.ALL,
    source: ApiKeyProvenance | None = None,
    name: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    sort: ApiKeySort = ApiKeySort.ORDER_ASC,
) -> ApiKeyListEnvelope:
    reject_unknown_query_parameters(request, _API_KEY_LIST_QUERY_PARAMETERS)
    result = control.list_api_keys(
        context,
        page=page,
        page_size=page_size,
        enabled=enabled,
        source=source,
        name_contains=name,
        sort=sort,
    )
    return ApiKeyListEnvelope(
        data=ApiKeyListData(items=[_key(item) for item in result.items]),
        meta=ApiKeyListMeta(
            requestId=management_request_id(request),
            page=result.page,
            pageSize=result.page_size,
            total=result.total,
            hasNext=result.has_next,
            revision=result.revision,
        ),
    )


@router.post(
    "/api-keys",
    operation_id="createApiKey",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiKeySecretEnvelope,
    responses={
        **_success(201, {"apiKey": _KEY_EXAMPLE, "secret": "<one-time-secret>"}),
        **_errors(
            ManagementErrorCode.SESSION_REQUIRED,
            ManagementErrorCode.SESSION_EXPIRED,
            ManagementErrorCode.CAPABILITY_DENIED,
            ManagementErrorCode.ORIGIN_DENIED,
            ManagementErrorCode.VALIDATION_FAILED,
            ManagementErrorCode.RESOURCE_CONFLICT,
            ManagementErrorCode.SERVICE_NOT_READY,
        ),
    },
)
def create_api_key(
    body: Annotated[
        ApiKeyCreateRequest,
        Body(openapi_examples={
            "generated": {"value": {"mode": "generated", "name": "console-client"}},
            "custom": {"value": {"mode": "custom", "name": "console-client", "customSecret": "<write-only>"}},
        }),
    ],
    request: Request,
    response: Response,
    context: Annotated[ManagementContext, Depends(require_capability(Capability.SECRETS_WRITE))],
    control: Annotated[ApiKeyControl, Depends(get_api_key_control)],
) -> ApiKeySecretEnvelope:
    reject_unknown_query_parameters(request)
    result = control.create_api_key(
        context,
        name=body.name,
        mode=body.mode,
        custom_secret=body.customSecret.get_secret_value() if body.customSecret is not None else None,
    )
    _mark_sensitive(response)
    return ApiKeySecretEnvelope(
        data=ApiKeySecretData(apiKey=_key(result.api_key), secret=result.secret),
        meta=_meta(request),
    )


@router.get(
    "/api-keys/{keyId}",
    operation_id="getApiKey",
    response_model=ApiKeyEnvelope,
    responses={
        **_success(200, _KEY_EXAMPLE),
        **_errors(*_COMMON_READ_ERRORS, ManagementErrorCode.RESOURCE_NOT_FOUND),
    },
)
def get_api_key(
    request: Request,
    key_id: Annotated[KeyId, Path(alias="keyId")],
    context: Annotated[ManagementContext, Depends(require_capability(Capability.READ))],
    control: Annotated[ApiKeyControl, Depends(get_api_key_control)],
) -> ApiKeyEnvelope:
    reject_unknown_query_parameters(request)
    return ApiKeyEnvelope(data=_key(control.get_api_key(context, key_id)), meta=_meta(request))


@router.patch(
    "/api-keys/{keyId}",
    operation_id="updateApiKey",
    response_model=ApiKeyEnvelope,
    responses={
        **_success(200, _KEY_EXAMPLE),
        **_errors(*_COMMON_WRITE_ERRORS, ManagementErrorCode.RESOURCE_NOT_FOUND),
    },
)
def update_api_key(
    body: Annotated[ApiKeyUpdateRequest, Body(openapi_examples={
        "permissions": {"value": {"enabled": True, "allowImages": False, "allowVideos": False, "allowedModels": ["model-a"]}},
        "limits": {"value": {"limitOverride": {"maxConcurrent": 3, "maxQueue": 10, "queueWaitSeconds": 60}}},
    })],
    request: Request,
    key_id: Annotated[KeyId, Path(alias="keyId")],
    context: Annotated[ManagementContext, Depends(require_capability(Capability.WRITE))],
    control: Annotated[ApiKeyControl, Depends(get_api_key_control)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> ApiKeyEnvelope:
    reject_unknown_query_parameters(request)
    changes = {}
    fields = body.model_fields_set
    if "enabled" in fields:
        changes["enabled"] = body.enabled
    if "allowImages" in fields:
        changes["allow_images"] = body.allowImages
    if "allowVideos" in fields:
        changes["allow_videos"] = body.allowVideos
    if "allowMcp" in fields:
        changes["allow_mcp"] = body.allowMcp
    if "mcpTools" in fields:
        changes["mcp_tools"] = body.mcpTools
    if "allowedModels" in fields:
        changes["allowed_models"] = body.allowedModels
    if "limitOverride" in fields:
        changes["limit_override"] = (
            None
            if body.limitOverride is None
            else {
                {
                    "enabled": "enabled",
                    "maxConcurrent": "max_concurrent",
                    "maxQueue": "max_queue",
                    "queueWaitSeconds": "queue_wait_seconds",
                }[field]: getattr(body.limitOverride, field)
                for field in body.limitOverride.model_fields_set
            }
        )
    result = control.update_api_key(context, key_id, changes=changes, if_match=if_match)
    return ApiKeyEnvelope(data=_key(result), meta=_meta(request))


@router.delete(
    "/api-keys/{keyId}",
    operation_id="deleteApiKey",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,  # Older FastAPI infers NoneType from the return annotation.
    responses={
        204: {"description": "API key deleted", "headers": {"X-Request-Id": {"schema": {"type": "string"}, "example": "request-example"}}},
        **_errors(
            ManagementErrorCode.SESSION_REQUIRED,
            ManagementErrorCode.SESSION_EXPIRED,
            ManagementErrorCode.CAPABILITY_DENIED,
            ManagementErrorCode.ORIGIN_DENIED,
            ManagementErrorCode.VALIDATION_FAILED,
            ManagementErrorCode.CONFIRMATION_REQUIRED,
            ManagementErrorCode.RESOURCE_NOT_FOUND,
            ManagementErrorCode.REVISION_CONFLICT,
            ManagementErrorCode.SERVICE_NOT_READY,
        ),
    },
)
def delete_api_key(
    response: Response,
    request: Request,
    key_id: Annotated[KeyId, Path(alias="keyId")],
    context: Annotated[ManagementContext, Depends(require_capability(Capability.DESTRUCTIVE))],
    control: Annotated[ApiKeyControl, Depends(get_api_key_control)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> None:
    reject_unknown_query_parameters(request)
    control.delete_api_key(context, key_id, if_match=if_match)
    response.headers["X-Request-Id"] = management_request_id(request)


@router.post(
    "/api-keys/{keyId}/actions/generate-replacement-plan",
    operation_id="planApiKeyRegeneration",
    response_model=ApiKeyReplacementPlanEnvelope,
    responses={
        **_success(200, {
            "planId": "akplan_example",
            "planToken": "<one-time-plan-token>",
            "keyId": "console-client",
            "revision": '"ak-example"',
            "expiresAt": "2026-01-02T03:09:05Z",
            "impact": "Existing clients using the current secret will fail immediately after commit",
        }),
        **_errors(*_COMMON_WRITE_ERRORS, ManagementErrorCode.RESOURCE_NOT_FOUND),
    },
)
def plan_api_key_regeneration(
    request: Request,
    response: Response,
    key_id: Annotated[KeyId, Path(alias="keyId")],
    context: Annotated[ManagementContext, Depends(require_capability(Capability.SECRETS_WRITE))],
    control: Annotated[ApiKeyControl, Depends(get_api_key_control)],
) -> ApiKeyReplacementPlanEnvelope:
    reject_unknown_query_parameters(request)
    plan = control.plan_regeneration(context, key_id)
    _mark_sensitive(response)
    return ApiKeyReplacementPlanEnvelope(
        data=ApiKeyReplacementPlanData(
            planId=plan.plan_id,
            planToken=plan.plan_token,
            keyId=plan.key_id,
            revision=plan.revision,
            expiresAt=plan.expires_at,
            impact=plan.impact,
        ),
        meta=_meta(request),
    )


@router.post(
    "/api-keys/{keyId}/actions/generate-replacement",
    operation_id="regenerateApiKey",
    response_model=ApiKeySecretEnvelope,
    responses={
        **_success(200, {"apiKey": _KEY_EXAMPLE, "secret": "<one-time-secret>"}),
        **_errors(
            *_COMMON_WRITE_ERRORS,
            ManagementErrorCode.CONFIRMATION_REQUIRED,
            ManagementErrorCode.INVALID_OPERATION_STATE,
            ManagementErrorCode.RESOURCE_NOT_FOUND,
        ),
    },
)
def regenerate_api_key(
    body: Annotated[ApiKeyRegenerateRequest, Body(openapi_examples={
        "commit": {"value": {"planId": "akplan_example", "planToken": "<write-only>"}}
    })],
    request: Request,
    response: Response,
    key_id: Annotated[KeyId, Path(alias="keyId")],
    context: Annotated[ManagementContext, Depends(require_capability(Capability.SECRETS_WRITE))],
    control: Annotated[ApiKeyControl, Depends(get_api_key_control)],
) -> ApiKeySecretEnvelope:
    reject_unknown_query_parameters(request)
    result = control.regenerate_api_key(
        context,
        key_id,
        plan_id=body.planId,
        plan_token=body.planToken.get_secret_value(),
    )
    _mark_sensitive(response)
    return ApiKeySecretEnvelope(data=ApiKeySecretData(apiKey=_key(result.api_key), secret=result.secret), meta=_meta(request))


@router.put(
    "/api-keys/{keyId}/secret",
    operation_id="replaceApiKeySecret",
    response_model=ApiKeySecretEnvelope,
    responses={
        **_success(200, {"apiKey": _KEY_EXAMPLE, "secret": "<one-time-secret>"}),
        **_errors(
            *_COMMON_WRITE_ERRORS,
            ManagementErrorCode.CONFIRMATION_REQUIRED,
            ManagementErrorCode.RESOURCE_CONFLICT,
            ManagementErrorCode.RESOURCE_NOT_FOUND,
        ),
    },
)
def replace_api_key_secret(
    body: Annotated[ApiKeyReplaceSecretRequest, Body(openapi_examples={
        "replace": {"value": {"customSecret": "<write-only>"}}
    })],
    request: Request,
    response: Response,
    key_id: Annotated[KeyId, Path(alias="keyId")],
    context: Annotated[ManagementContext, Depends(require_capability(Capability.SECRETS_WRITE))],
    control: Annotated[ApiKeyControl, Depends(get_api_key_control)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> ApiKeySecretEnvelope:
    reject_unknown_query_parameters(request)
    result = control.replace_api_key_secret(
        context,
        key_id,
        custom_secret=body.customSecret.get_secret_value(),
        if_match=if_match,
    )
    _mark_sensitive(response)
    return ApiKeySecretEnvelope(data=ApiKeySecretData(apiKey=_key(result.api_key), secret=result.secret), meta=_meta(request))


@router.put(
    "/api-keys/order",
    operation_id="reorderApiKeys",
    response_model=ApiKeyOrderEnvelope,
    responses={
        **_success(200, {"keyIds": ["console-client"], "revision": '"aks-example"'}),
        **_errors(*_COMMON_WRITE_ERRORS, ManagementErrorCode.CONFIRMATION_REQUIRED),
    },
)
def reorder_api_keys(
    body: Annotated[ApiKeyOrderRequest, Body(openapi_examples={
        "completeOrder": {"value": {"keyIds": ["console-client", "automation"]}}
    })],
    request: Request,
    context: Annotated[ManagementContext, Depends(require_capability(Capability.WRITE))],
    control: Annotated[ApiKeyControl, Depends(get_api_key_control)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> ApiKeyOrderEnvelope:
    reject_unknown_query_parameters(request)
    revision = control.reorder_api_keys(context, body.keyIds, if_match=if_match)
    return ApiKeyOrderEnvelope(data=ApiKeyOrderData(keyIds=body.keyIds, revision=revision), meta=_meta(request))


@router.post(
    "/api-keys/{keyId}/actions/reset-limiter",
    operation_id="resetApiKeyLimiter",
    response_model=ApiKeyLimiterEnvelope,
    responses={
        **_success(200, _LIMITER_EXAMPLE),
        **_errors(*_COMMON_WRITE_ERRORS, ManagementErrorCode.RESOURCE_NOT_FOUND),
    },
)
def reset_api_key_limiter(
    request: Request,
    key_id: Annotated[KeyId, Path(alias="keyId")],
    context: Annotated[ManagementContext, Depends(require_capability(Capability.WRITE))],
    control: Annotated[ApiKeyControl, Depends(get_api_key_control)],
) -> ApiKeyLimiterEnvelope:
    reject_unknown_query_parameters(request)
    result = control.reset_api_key_limiter(context, key_id)
    return ApiKeyLimiterEnvelope(data=_limiter(result), meta=_meta(request))


@router.get(
    "/api-keys/{keyId}/stats",
    operation_id="getApiKeyStats",
    response_model=ApiKeyStatsEnvelope,
    responses={
        **_success(200, {
            "keyId": "console-client",
            "since": "2026-01-01T00:00:00Z",
            "overall": _EMPTY_USAGE,
            "byModel": [],
            "revision": '"ak-example"',
        }),
        **_errors(*_COMMON_READ_ERRORS, ManagementErrorCode.RESOURCE_NOT_FOUND),
    },
)
def get_api_key_stats(
    request: Request,
    key_id: Annotated[KeyId, Path(alias="keyId")],
    context: Annotated[ManagementContext, Depends(require_capability(Capability.READ))],
    control: Annotated[ApiKeyControl, Depends(get_api_key_control)],
) -> ApiKeyStatsEnvelope:
    reject_unknown_query_parameters(request)
    result = control.get_api_key_stats(context, key_id)
    return ApiKeyStatsEnvelope(
        data=ApiKeyStatsData(
            keyId=result.key_id,
            since=result.since,
            overall=_usage(result.overall),
            byModel=[_model_usage(item) for item in result.by_model],
            revision=result.revision,
        ),
        meta=_meta(request),
    )


__all__ = ["get_api_key_control", "router"]
