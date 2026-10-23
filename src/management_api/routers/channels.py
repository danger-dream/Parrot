"""Channels Management API adapter; all business work delegates to ChannelControl."""

from __future__ import annotations

from datetime import datetime, timezone
from threading import RLock
from typing import Annotated
from urllib.parse import unquote

from fastapi import APIRouter, Body, Depends, Header, Path, Query, Request, Response, status
from fastapi.routing import APIRoute
from starlette.routing import Match

from src.management_control import (
    ErrorField,
    ManagementContext,
    ManagementError,
    ManagementErrorCode,
)
from src.management_control.channels.model_facts import channel_model_facts
from src.management_control.channels import (
    ChannelCompatibility,
    ChannelControl,
    ChannelCreateCommand,
    ChannelHealth,
    ChannelListQuery,
    ChannelModel,
    ChannelProtocol,
    ChannelSort,
    ChannelUpdateCommand,
    CompatibilityFeature,
    DiscoveryCommand,
    DraftProbeCommand,
    ProbeResult,
    SortDirection,
)

from ..channels_security import redact_credential_text, strip_url_userinfo
from ..dependencies import (
    ManagementRuntime,
    get_management_context,
    get_management_control_owner,
    get_management_runtime,
    management_request_id,
)
from ..error_mapping import management_error_responses
from ..response_helpers import response_meta as _meta
from ..schemas.base import DataEnvelope
from src.management_control.channels.list_stats import month_stats_by_channel

from ..schemas.channels import (
    ChannelListItemData,
    month_stats_data,
    ActionResultData,
    CatalogProtocolEndpointData,
    ChannelActionEnvelope,
    ChannelCatalogData,
    ChannelCatalogEnvelope,
    ChannelCompatibilityData,
    ChannelCompatibilityEnvelope,
    ChannelCompatibilityInput,
    ChannelCreateRequest,
    ChannelData,
    ChannelDetailData,
    ChannelDetailEnvelope,
    ChannelDiscoveryRequest,
    ChannelListEnvelope,
    ChannelModelData,
    ChannelModelStatsData,
    ChannelMonthStatsData,
    ChannelOperationEnvelope,
    ChannelOrderData,
    ChannelOrderEnvelope,
    ChannelOrderRequest,
    ChannelPageMeta,
    ChannelPresetData,
    ChannelProviderData,
    ChannelRuntimeModelData,
    ChannelUpdateRequest,
    CompatibilityFeatureData,
    ExistingChannelDiscoveryRequest,
    ManualChannelCreateRequest,
    ProbeDraftRequest,
    ProbeExistingRequest,
    ProviderUsageData,
    ProviderUsageMetricData,
    ProviderUsageSnapshotData,
)
from ._operations import operation_data as _operation_data


_BIND_LOCK = RLock()


_LIST_QUERY_PARAMETERS = frozenset({
    "page",
    "pageSize",
    "search",
    "enabled",
    "protocol",
    "providerId",
    "health",
    "sort",
    "direction",
})


def _validate_query_parameters(request: Request) -> None:
    route = request.scope.get("route")
    allowed = (
        _LIST_QUERY_PARAMETERS
        if getattr(route, "operation_id", None) == "listChannels"
        else frozenset()
    )
    unknown = sorted(set(request.query_params) - allowed)
    if unknown:
        raise ManagementError(
            ManagementErrorCode.VALIDATION_FAILED,
            fields=tuple(
                ErrorField(name, "unknown", "Unknown query parameter")
                for name in unknown
            ),
        )


class ChannelResourceRoute(APIRoute):
    def matches(self, scope):
        # Match encoded IDs before ASGI decoding loses their segment boundary.
        # A catch-all would confuse a channel named "a/compatibility" with the
        # compatibility subresource of "a", or shadow that subresource entirely.
        raw_path = scope.get("raw_path", b"")
        if "channelId" in self.param_convertors and b"%2f" in raw_path.lower():
            matched, child = super().matches({
                **scope, "path": raw_path.decode("latin-1"),
            })
            if matched is not Match.NONE:
                child["path_params"]["channelId"] = unquote(child["path_params"]["channelId"])
                return matched, child
        return super().matches(scope)


router = APIRouter(
    dependencies=[Depends(_validate_query_parameters)], route_class=ChannelResourceRoute,
)

_CHANNEL_EXAMPLE = {
    "id": "api:example-channel",
    "revision": "chrev_example",
    "name": "example-channel",
    "baseUrl": "https://provider.example.test",
    "apiPath": "/v1/messages",
    "url": "https://provider.example.test/v1/messages",
    "apiKeyConfigured": True,
    "apiKeyMaskedHint": "exampl***hint",
    "protocol": "anthropic",
    "providerId": None,
    "providerPresetId": None,
    "models": [{"real": "model-real", "alias": "model-alias"}],
    "modelCount": 1,
    "enabled": True,
    "disabledReason": None,
    "maxConcurrent": 4,
    "ccMimicry": True,
    "omitTemperature": False,
    "omitThinking": False,
    "compatibility": {
        "revision": "chrev_example",
        "context1m": {"mode": "auto", "models": [], "allModels": True},
        "fast": {"mode": "force", "models": ["model-real"], "allModels": False},
    },
    "health": "unknown",
    "recentSuccessRate": None,
    "cooldownCount": 0,
    "affinityCount": 0,
    "clientAffinityCount": 0,
    "providerUsage": {
        "supported": False,
        "status": "unsupported",
        "stale": False,
        "partial": False,
        "source": None,
        "fetchedAt": None,
        "error": None,
        "errorAt": None,
        "snapshot": None,
    },
}
_OPERATION_EXAMPLE = {
    "data": {
        "id": "op_example",
        "kind": "channel.models.discover",
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
_ACTION_EXAMPLE = {
    "data": {"affected": 1, "queued": None},
    "meta": {"requestId": "request-example"},
}
_COMMON_ERRORS = (
    ManagementErrorCode.SESSION_REQUIRED,
    ManagementErrorCode.SESSION_EXPIRED,
    ManagementErrorCode.ORIGIN_DENIED,
    ManagementErrorCode.CAPABILITY_DENIED,
    ManagementErrorCode.RESOURCE_NOT_FOUND,
    ManagementErrorCode.REVISION_CONFLICT,
    ManagementErrorCode.RESOURCE_CONFLICT,
    ManagementErrorCode.VALIDATION_FAILED,
    ManagementErrorCode.UNSUPPORTED_VALUE,
    ManagementErrorCode.OPERATION_ALREADY_RUNNING,
    ManagementErrorCode.UPSTREAM_ERROR,
    ManagementErrorCode.DEPENDENCY_UNAVAILABLE,
    ManagementErrorCode.SERVICE_NOT_READY,
)


def _success(http_status: int, example: dict) -> dict[int, dict]:
    return {
        http_status: {
            "description": "Successful Response",
            "content": {"application/json": {"example": example}},
        }
    }


def _responses(http_status: int, example: dict, *extra: ManagementErrorCode) -> dict:
    codes = tuple(dict.fromkeys((*_COMMON_ERRORS, *extra)))
    return {**_success(http_status, example), **management_error_responses(*codes)}


def _no_content_responses() -> dict:
    return {
        204: {
            "description": "Channel deleted",
            "headers": {
                "X-Request-Id": {
                    "schema": {"type": "string"},
                    "example": "request-example",
                }
            },
        },
        **management_error_responses(*_COMMON_ERRORS),
    }


def get_channel_control(
    request: Request,
    runtime: Annotated[ManagementRuntime, Depends(get_management_runtime)],
) -> ChannelControl:
    current = getattr(request.app.state, "management_channel_control", None)
    owner = getattr(request.app.state, "management_channel_control_runtime", None)
    # Owner absence is the explicit composition/test injection seam. Controls
    # created here are replaced whenever the app's runtime owner changes.
    if isinstance(current, ChannelControl) and (owner is None or owner is runtime):
        return current
    with _BIND_LOCK:
        current = getattr(request.app.state, "management_channel_control", None)
        owner = getattr(request.app.state, "management_channel_control_runtime", None)
        if isinstance(current, ChannelControl) and (owner is None or owner is runtime):
            return current
        current = get_management_control_owner(runtime).channels
        request.app.state.management_channel_control = current
        request.app.state.management_channel_control_runtime = runtime
        return current


def _feature_data(feature, revision: str) -> CompatibilityFeatureData:
    del revision
    return CompatibilityFeatureData(
        mode=feature.mode,
        models=list(feature.models),
        allModels=not feature.models,
    )


def _compatibility_data(revision, compatibility) -> ChannelCompatibilityData:
    return ChannelCompatibilityData(
        revision=revision,
        context1m=_feature_data(compatibility.context_1m, revision),
        fast=_feature_data(compatibility.fast, revision),
    )


def _utc_time(value: object, *, milliseconds: bool = False) -> datetime | None:
    """Return only defensible absolute timestamps, normalized to UTC."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(
                    text[:-1] + "+00:00" if text.endswith("Z") else text
                )
            except ValueError:
                return None
        else:
            if milliseconds:
                if number <= 0:
                    return None
                seconds = number / 1000
            elif abs(number) >= 100_000_000_000:
                seconds = number / 1000
            elif abs(number) >= 1_000_000_000:
                seconds = number
            else:
                return None
            try:
                parsed = datetime.fromtimestamp(seconds, timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _usage_metric(raw: dict) -> ProviderUsageMetricData:
    return ProviderUsageMetricData(
        id=raw.get("id"),
        label=str(raw.get("label") or ""),
        kind=raw.get("kind"),
        group=raw.get("group"),
        unit=raw.get("unit"),
        currency=raw.get("currency"),
        value=raw.get("value"),
        used=raw.get("used"),
        total=raw.get("total"),
        remaining=raw.get("remaining"),
        usedPercent=raw.get("used_percent"),
        resetAt=_utc_time(raw.get("reset_at")),
        resetInSeconds=raw.get("reset_in_seconds"),
        status=raw.get("status"),
        startAt=_utc_time(raw.get("start_at")),
        endAt=_utc_time(raw.get("end_at")),
        distributionTotal=raw.get("distribution_total"),
    )


def _usage_data(usage) -> ProviderUsageData:
    snapshot = None
    if usage.snapshot:
        raw = usage.snapshot
        snapshot = ProviderUsageSnapshotData(
            version=int(raw.get("version") or 0),
            source=str(raw.get("source") or usage.source or ""),
            balances=[_usage_metric(item) for item in raw.get("balances", ())],
            windows=[_usage_metric(item) for item in raw.get("windows", ())],
            counters=[_usage_metric(item) for item in raw.get("counters", ())],
            notices=[str(item) for item in raw.get("notices", ())],
            partial=bool(raw.get("partial")),
        )
    return ProviderUsageData(
        supported=usage.supported,
        status=usage.status,
        stale=usage.stale,
        partial=usage.partial,
        source=usage.source,
        fetchedAt=_utc_time(usage.fetched_at, milliseconds=True),
        error=redact_credential_text(usage.error),
        errorAt=_utc_time(usage.error_at, milliseconds=True),
        snapshot=snapshot,
    )


def _channel_data(view) -> ChannelData:
    safe_base_url = strip_url_userinfo(view.base_url)
    return ChannelData(
        id=view.id,
        revision=view.revision,
        name=view.display_name,
        baseUrl=safe_base_url,
        apiPath=view.api_path,
        url=safe_base_url + (view.api_path or "") if safe_base_url else "",
        apiKeyConfigured=view.api_key_configured,
        apiKeyMaskedHint=view.api_key_masked_hint,
        protocol=view.protocol,
        providerId=view.provider_id,
        providerPresetId=view.provider_preset_id,
        models=[ChannelModelData(real=item.real, alias=item.alias) for item in view.models],
        modelCount=len(view.models),
        enabled=view.enabled,
        disabledReason=view.disabled_reason,
        maxConcurrent=view.max_concurrent,
        ccMimicry=view.cc_mimicry,
        omitTemperature=view.omit_temperature,
        omitThinking=view.omit_thinking,
        compatibility=_compatibility_data(view.revision, view.compatibility),
        health=view.health,
        recentSuccessRate=view.recent_success_rate,
        cooldownCount=view.cooldown_count,
        permanentCooldownCount=view.permanent_cooldown_count,
        affinityCount=view.affinity_count,
        clientAffinityCount=view.client_affinity_count,
        providerUsage=_usage_data(view.provider_usage),
    )


def _runtime_models(view) -> list[ChannelRuntimeModelData]:
    rows = []
    for model in view.models:
        perf = view.performance_by_model.get(model.real)
        cd = view.cooldown_by_model.get(model.real)
        cooldown_kind = None
        if cd is not None:
            cooldown_kind = "permanent" if cd.cooldown_until == -1 else "quota" if cd.quota else "temporary"
        rows.append(ChannelRuntimeModelData(
            real=model.real,
            alias=model.alias,
            recentRequests=perf.recent_requests if perf else 0,
            recentSuccessRate=(
                perf.recent_success_count / perf.recent_requests * 100
                if perf and perf.recent_requests else None
            ),
            totalRequests=perf.total_requests if perf else 0,
            averageConnectMilliseconds=perf.avg_connect_ms if perf else None,
            averageFirstByteMilliseconds=perf.avg_first_byte_ms if perf else None,
            score=perf.score if perf else None,
            cooldownUntil=(
                _utc_time(cd.cooldown_until, milliseconds=True)
                if cd is not None and cd.cooldown_until != -1 else None
            ),
            cooldownKind=cooldown_kind,
            errorCount=cd.error_count if cd else 0,
            **channel_model_facts(view.id, model.alias, model.real),
        ))
    return rows


def _model_stats(raw: dict) -> ChannelModelStatsData:
    return ChannelModelStatsData(
        finalModel=str(raw.get("final_model") or ""),
        total=int(raw.get("total") or 0),
        successCount=int(raw.get("success_count") or 0),
        errorCount=int(raw.get("error_count") or 0),
        inputTokens=int(raw.get("input") or 0),
        outputTokens=int(raw.get("output") or 0),
        cacheCreationTokens=int(raw.get("cache_creation") or 0),
        cacheReadTokens=int(raw.get("cache_read") or 0),
        averageTokensPerSecond=raw.get("avg_tps"),
        maximumTokensPerSecond=raw.get("max_tps"),
        minimumTokensPerSecond=raw.get("min_tps"),
        costTicks=int(raw.get("cost_ticks") or 0),
        actualCostTicks=int(raw.get("actual_cost_ticks") or 0),
        estimatedCostTicks=int(raw.get("estimated_cost_ticks") or 0),
        actualCostedSuccess=int(raw.get("actual_costed_success") or 0),
        estimatedCostedSuccess=int(raw.get("estimated_costed_success") or 0),
        costedSuccess=int(raw.get("costed_success") or 0),
        unpricedSuccess=int(raw.get("unpriced_success") or 0),
    )


def _detail_data(detail) -> ChannelDetailData:
    base = _channel_data(detail.channel).model_dump()
    month = detail.month_stats
    return ChannelDetailData(
        **base,
        monthStats=month_stats_data(month),
        modelStats=[_model_stats(item) for item in detail.model_stats],
        runtimeModels=_runtime_models(detail.channel),
    )


def _compatibility(value: ChannelCompatibilityInput) -> ChannelCompatibility:
    return ChannelCompatibility(
        context_1m=CompatibilityFeature(
            mode=value.context1m.mode, models=tuple(value.context1m.models)
        ),
        fast=CompatibilityFeature(mode=value.fast.mode, models=tuple(value.fast.models)),
    )


def _create_command(body) -> ChannelCreateCommand:
    manual = isinstance(body, ManualChannelCreateRequest)
    return ChannelCreateCommand(
        name=body.name,
        base_url=body.baseUrl if manual else None,
        api_path=body.apiPath if manual else None,
        api_key=body.apiKey.get_secret_value(),
        protocol=body.protocol,
        models=tuple(ChannelModel(real=item.real, alias=item.alias) for item in body.models),
        max_concurrent=body.maxConcurrent,
        compatibility=_compatibility(body.compatibility),
        cc_mimicry=body.ccMimicry,
        omit_temperature=body.omitTemperature,
        omit_thinking=body.omitThinking,
        provider_id=None if manual else body.providerId,
        provider_preset_id=None if manual else body.providerPresetId,
        enabled=body.enabled,
        initial_probe_results=_initial_probe_results(body),
    )


def _initial_probe_results(body) -> dict[str, ProbeResult]:
    models = {item.real for item in body.models}
    unknown = [item.model for item in body.initialProbeResults if item.model not in models]
    if unknown or len({item.model for item in body.initialProbeResults}) != len(body.initialProbeResults):
        raise ManagementError(ManagementErrorCode.VALIDATION_FAILED, fields=(ErrorField(
            "initialProbeResults", "UNKNOWN_OR_DUPLICATE_MODEL",
            "each result must name one of the channel's models (real id) once"),))
    return {
        item.model: ProbeResult(item.ok, 0, item.reason or ("PROBE_FAILED" if not item.ok else None), False, False)
        for item in body.initialProbeResults
    }


def _update_command(body: ChannelUpdateRequest) -> ChannelUpdateCommand:
    fields = body.model_fields_set
    kwargs = {
        "name": body.name,
        "base_url": body.baseUrl,
        "api_key": body.apiKey.get_secret_value() if body.apiKey else None,
        "protocol": body.protocol,
        "models": (
            tuple(ChannelModel(real=item.real, alias=item.alias) for item in body.models)
            if body.models is not None else None
        ),
        "max_concurrent": body.maxConcurrent,
        "compatibility": _compatibility(body.compatibility) if body.compatibility else None,
        "cc_mimicry": body.ccMimicry,
        "omit_temperature": body.omitTemperature,
        "omit_thinking": body.omitThinking,
        "enabled": body.enabled,
    }
    if "apiPath" in fields:
        kwargs["api_path"] = body.apiPath
    if "providerId" in fields:
        kwargs["provider_id"] = body.providerId
    if "providerPresetId" in fields:
        kwargs["provider_preset_id"] = body.providerPresetId
    return ChannelUpdateCommand(**kwargs)


@router.get(
    "/channels",
    operation_id="listChannels",
    tags=["management-channels"],
    response_model=ChannelListEnvelope,
    responses=_responses(200, {"data": [_CHANNEL_EXAMPLE], "meta": {
        "requestId": "request-example", "page": 1, "pageSize": 50,
        "total": 1, "hasNext": False, "orderRevision": "chorder_example",
    }}),
)
def list_channels(
    request: Request,
    control: Annotated[ChannelControl, Depends(get_channel_control)],
    context: Annotated[ManagementContext, Depends(get_management_context)],
    page: Annotated[int, Query(ge=1)] = 1,
    pageSize: Annotated[int, Query(ge=1, le=200)] = 50,
    search: Annotated[str | None, Query(max_length=256)] = None,
    enabled: Annotated[bool | None, Query()] = None,
    protocol: Annotated[ChannelProtocol | None, Query()] = None,
    providerId: Annotated[str | None, Query(max_length=120)] = None,
    health: Annotated[ChannelHealth | None, Query()] = None,
    sort: Annotated[ChannelSort, Query()] = ChannelSort.ORDER,
    direction: Annotated[SortDirection, Query()] = SortDirection.ASC,
) -> ChannelListEnvelope:
    result = control.list_channels(context, ChannelListQuery(
        page=page, page_size=pageSize, search=search, enabled=enabled,
        protocol=protocol, provider_id=providerId, health=health,
        sort=sort, direction=direction,
    ))
    month, loading = month_stats_by_channel(context)
    return ChannelListEnvelope(
        data=[ChannelListItemData(
            **_channel_data(item).model_dump(),
            monthStats=month_stats_data(month[item.id]) if item.id in month else None,
            monthStatsLoading=loading,
        ) for item in result.items],
        meta=ChannelPageMeta(
            requestId=management_request_id(request), page=result.page,
            pageSize=result.page_size, total=result.total, hasNext=result.has_next,
            orderRevision=result.order_revision,
        ),
    )


@router.post(
    "/channels",
    operation_id="createChannel",
    tags=["management-channels"],
    status_code=status.HTTP_201_CREATED,
    response_model=DataEnvelope[ChannelData],
    responses=_responses(201, {"data": _CHANNEL_EXAMPLE, "meta": {"requestId": "request-example"}}),
)
def create_channel(
    body: Annotated[ChannelCreateRequest, Body()],
    request: Request,
    control: Annotated[ChannelControl, Depends(get_channel_control)],
    context: Annotated[ManagementContext, Depends(get_management_context)],
) -> DataEnvelope[ChannelData]:
    result = control.create_channel(context, _create_command(body))
    return DataEnvelope(data=_channel_data(result.channel), meta=_meta(request))


@router.put(
    "/channels/order",
    operation_id="reorderChannels",
    tags=["management-channels"],
    response_model=ChannelOrderEnvelope,
    responses=_responses(200, {"data": {"revision": "chorder_example", "channelIds": ["api:example-channel"]}, "meta": {"requestId": "request-example"}}),
)
def reorder_channels(
    body: ChannelOrderRequest,
    request: Request,
    if_match: Annotated[str, Header(alias="If-Match", min_length=8, max_length=128)],
    control: Annotated[ChannelControl, Depends(get_channel_control)],
    context: Annotated[ManagementContext, Depends(get_management_context)],
) -> ChannelOrderEnvelope:
    revision = control.reorder_channels(
        context, tuple(body.channelIds), expected_revision=if_match
    )
    return ChannelOrderEnvelope(
        data=ChannelOrderData(revision=revision, channelIds=body.channelIds),
        meta=_meta(request),
    )


@router.post(
    "/channels/actions/clear-errors",
    operation_id="clearAllChannelErrors",
    tags=["management-channels"],
    response_model=ChannelActionEnvelope,
    responses=_responses(200, _ACTION_EXAMPLE),
)
def clear_all_channel_errors(
    request: Request,
    control: Annotated[ChannelControl, Depends(get_channel_control)],
    context: Annotated[ManagementContext, Depends(get_management_context)],
) -> ChannelActionEnvelope:
    result = control.clear_all_errors(context)
    return ChannelActionEnvelope(data=ActionResultData(affected=result.affected), meta=_meta(request))


@router.post(
    "/channels/actions/clear-affinity",
    operation_id="clearAllChannelAffinity",
    tags=["management-channels"],
    response_model=ChannelActionEnvelope,
    responses=_responses(200, _ACTION_EXAMPLE),
)
def clear_all_channel_affinity(
    request: Request,
    control: Annotated[ChannelControl, Depends(get_channel_control)],
    context: Annotated[ManagementContext, Depends(get_management_context)],
) -> ChannelActionEnvelope:
    result = control.clear_all_affinity(context)
    return ChannelActionEnvelope(data=ActionResultData(affected=result.affected), meta=_meta(request))


@router.get(
    "/channel-catalog",
    operation_id="getChannelCatalog",
    tags=["management-channels"],
    response_model=ChannelCatalogEnvelope,
    responses=_responses(200, {"data": {"providers": [], "protocols": ["anthropic", "openai-chat", "openai-responses"], "compatibilityModes": ["auto", "force"], "features": ["context1m", "fast"]}, "meta": {"requestId": "request-example"}}),
)
def get_channel_catalog(
    request: Request,
    control: Annotated[ChannelControl, Depends(get_channel_control)],
    context: Annotated[ManagementContext, Depends(get_management_context)],
) -> ChannelCatalogEnvelope:
    catalog = control.get_catalog(context)
    return ChannelCatalogEnvelope(
        data=ChannelCatalogData(
            providers=[ChannelProviderData(
                id=brand.id,
                name=brand.display_name,
                presets=[ChannelPresetData(
                    id=preset.id,
                    name=preset.display_name,
                    modelsUrlConfigured=preset.models_url_configured,
                    modelDiscoveryAuth=preset.models_auth,
                    modelDiscoveryParser=preset.models_parser,
                    protocols=[CatalogProtocolEndpointData(protocol=ChannelProtocol(key), endpoint=value) for key, value in preset.protocols.items()],
                    staticModels=list(preset.static_models),
                    ccMimicry=preset.cc_mimicry,
                    providerUsageSupported=preset.usage_supported,
                ) for preset in brand.presets],
            ) for brand in catalog.providers],
            protocols=list(catalog.protocols),
            compatibilityModes=list(catalog.compatibility_modes),
            features=list(catalog.features),
        ),
        meta=_meta(request),
    )


@router.post(
    "/channel-model-discoveries",
    operation_id="discoverChannelModels",
    tags=["management-channels"],
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ChannelOperationEnvelope,
    responses=_responses(202, _OPERATION_EXAMPLE),
)
async def discover_channel_models(
    body: Annotated[ChannelDiscoveryRequest, Body()],
    request: Request,
    control: Annotated[ChannelControl, Depends(get_channel_control)],
    context: Annotated[ManagementContext, Depends(get_management_context)],
) -> ChannelOperationEnvelope:
    command = (
        DiscoveryCommand(channel_id=body.channelId)
        if isinstance(body, ExistingChannelDiscoveryRequest)
        else DiscoveryCommand(
            base_url=body.baseUrl, api_path=body.apiPath,
            api_key=body.apiKey.get_secret_value(), protocol=body.protocol,
            provider_id=body.providerId,
            provider_preset_id=body.providerPresetId,
        )
    )
    operation = control.start_model_discovery(context, command)
    return ChannelOperationEnvelope(data=_operation_data(operation), meta=_meta(request))


@router.post(
    "/channel-drafts/probes",
    operation_id="probeChannelDraft",
    tags=["management-channels"],
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ChannelOperationEnvelope,
    responses=_responses(202, _OPERATION_EXAMPLE),
)
async def probe_channel_draft(
    body: ProbeDraftRequest,
    request: Request,
    control: Annotated[ChannelControl, Depends(get_channel_control)],
    context: Annotated[ManagementContext, Depends(get_management_context)],
) -> ChannelOperationEnvelope:
    operation = control.start_draft_probe(context, DraftProbeCommand(
        name=body.name, base_url=body.baseUrl, api_path=body.apiPath,
        api_key=body.apiKey.get_secret_value(), protocol=body.protocol,
        model=body.model, provider_id=body.providerId,
        provider_preset_id=body.providerPresetId, cc_mimicry=body.ccMimicry,
    ))
    return ChannelOperationEnvelope(data=_operation_data(operation), meta=_meta(request))


@router.get(
    "/channels/{channelId}",
    operation_id="getChannel",
    tags=["management-channels"],
    response_model=ChannelDetailEnvelope,
    responses=_responses(200, {"data": {**_CHANNEL_EXAMPLE, "monthStats": {"total": 0, "successCount": 0, "errorCount": 0, "inputTokens": 0, "outputTokens": 0, "cacheCreationTokens": 0, "cacheReadTokens": 0, "averageTokensPerSecond": None, "maximumTokensPerSecond": None, "minimumTokensPerSecond": None, "cost": None}, "modelStats": [], "runtimeModels": []}, "meta": {"requestId": "request-example"}}),
)
def get_channel(
    channelId: Annotated[str, Path(min_length=5, max_length=256)],
    request: Request,
    control: Annotated[ChannelControl, Depends(get_channel_control)],
    context: Annotated[ManagementContext, Depends(get_management_context)],
) -> ChannelDetailEnvelope:
    return ChannelDetailEnvelope(
        data=_detail_data(control.get_channel_detail(context, channelId)),
        meta=_meta(request),
    )


@router.patch(
    "/channels/{channelId}",
    operation_id="updateChannel",
    tags=["management-channels"],
    response_model=DataEnvelope[ChannelData],
    responses=_responses(200, {"data": _CHANNEL_EXAMPLE, "meta": {"requestId": "request-example"}}),
)
def update_channel(
    body: ChannelUpdateRequest,
    channelId: Annotated[str, Path(min_length=5, max_length=256)],
    request: Request,
    control: Annotated[ChannelControl, Depends(get_channel_control)],
    context: Annotated[ManagementContext, Depends(get_management_context)],
    if_match: Annotated[str | None, Header(alias="If-Match", min_length=8, max_length=128)] = None,
) -> DataEnvelope[ChannelData]:
    result = control.update_channel(
        context, channelId, _update_command(body), expected_revision=if_match
    )
    return DataEnvelope(data=_channel_data(result.channel), meta=_meta(request))


@router.delete(
    "/channels/{channelId}",
    operation_id="deleteChannel",
    tags=["management-channels"],
    status_code=status.HTTP_204_NO_CONTENT,
    responses=_no_content_responses(),
)
def delete_channel(
    channelId: Annotated[str, Path(min_length=5, max_length=256)],
    if_match: Annotated[str, Header(alias="If-Match", min_length=8, max_length=128)],
    request: Request,
    control: Annotated[ChannelControl, Depends(get_channel_control)],
    context: Annotated[ManagementContext, Depends(get_management_context)],
) -> Response:
    control.delete_channel(context, channelId, expected_revision=if_match)
    return Response(
        status_code=status.HTTP_204_NO_CONTENT,
        headers={"X-Request-Id": management_request_id(request)},
    )


@router.post(
    "/channels/{channelId}/diagnostic-probes",
    operation_id="probeExistingChannel",
    tags=["management-channels"],
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ChannelOperationEnvelope,
    responses=_responses(202, _OPERATION_EXAMPLE),
)
async def probe_existing_channel(
    body: ProbeExistingRequest,
    channelId: Annotated[str, Path(min_length=5, max_length=256)],
    request: Request,
    control: Annotated[ChannelControl, Depends(get_channel_control)],
    context: Annotated[ManagementContext, Depends(get_management_context)],
) -> ChannelOperationEnvelope:
    operation = control.start_existing_probe(context, channelId, body.model)
    return ChannelOperationEnvelope(data=_operation_data(operation), meta=_meta(request))


@router.post(
    "/channels/{channelId}/actions/refresh-usage",
    operation_id="refreshChannelProviderUsage",
    tags=["management-channels"],
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ChannelOperationEnvelope,
    responses=_responses(202, _OPERATION_EXAMPLE),
)
async def refresh_channel_provider_usage(
    channelId: Annotated[str, Path(min_length=5, max_length=256)],
    request: Request,
    control: Annotated[ChannelControl, Depends(get_channel_control)],
    context: Annotated[ManagementContext, Depends(get_management_context)],
) -> ChannelOperationEnvelope:
    operation = control.start_provider_usage_refresh(context, channelId)
    return ChannelOperationEnvelope(data=_operation_data(operation), meta=_meta(request))


@router.post(
    "/channels/{channelId}/actions/clear-errors",
    operation_id="clearChannelErrors",
    tags=["management-channels"],
    response_model=ChannelActionEnvelope,
    responses=_responses(200, _ACTION_EXAMPLE),
)
def clear_channel_errors(
    channelId: Annotated[str, Path(min_length=5, max_length=256)],
    request: Request,
    control: Annotated[ChannelControl, Depends(get_channel_control)],
    context: Annotated[ManagementContext, Depends(get_management_context)],
) -> ChannelActionEnvelope:
    result = control.clear_channel_errors(context, channelId)
    return ChannelActionEnvelope(data=ActionResultData(affected=result.affected), meta=_meta(request))


@router.post(
    "/channels/{channelId}/actions/clear-affinity",
    operation_id="clearChannelAffinity",
    tags=["management-channels"],
    response_model=ChannelActionEnvelope,
    responses=_responses(200, _ACTION_EXAMPLE),
)
def clear_channel_affinity(
    channelId: Annotated[str, Path(min_length=5, max_length=256)],
    request: Request,
    control: Annotated[ChannelControl, Depends(get_channel_control)],
    context: Annotated[ManagementContext, Depends(get_management_context)],
) -> ChannelActionEnvelope:
    result = control.clear_channel_affinity(context, channelId)
    return ChannelActionEnvelope(data=ActionResultData(affected=result.affected), meta=_meta(request))


@router.get(
    "/channels/{channelId}/compatibility",
    operation_id="getChannelCompatibility",
    tags=["management-channels"],
    response_model=ChannelCompatibilityEnvelope,
    responses=_responses(200, {"data": _CHANNEL_EXAMPLE["compatibility"], "meta": {"requestId": "request-example"}}),
)
def get_channel_compatibility(
    channelId: Annotated[str, Path(min_length=5, max_length=256)],
    request: Request,
    control: Annotated[ChannelControl, Depends(get_channel_control)],
    context: Annotated[ManagementContext, Depends(get_management_context)],
) -> ChannelCompatibilityEnvelope:
    revision, compatibility = control.get_compatibility(context, channelId)
    return ChannelCompatibilityEnvelope(
        data=_compatibility_data(revision, compatibility), meta=_meta(request)
    )


@router.patch(
    "/channels/{channelId}/compatibility",
    operation_id="updateChannelCompatibility",
    tags=["management-channels"],
    response_model=ChannelCompatibilityEnvelope,
    responses=_responses(200, {"data": _CHANNEL_EXAMPLE["compatibility"], "meta": {"requestId": "request-example"}}),
)
def update_channel_compatibility(
    body: ChannelCompatibilityInput,
    channelId: Annotated[str, Path(min_length=5, max_length=256)],
    request: Request,
    control: Annotated[ChannelControl, Depends(get_channel_control)],
    context: Annotated[ManagementContext, Depends(get_management_context)],
    if_match: Annotated[str | None, Header(alias="If-Match", min_length=8, max_length=128)] = None,
) -> ChannelCompatibilityEnvelope:
    result = control.update_compatibility(
        context, channelId, _compatibility(body), expected_revision=if_match
    )
    return ChannelCompatibilityEnvelope(
        data=_compatibility_data(
            result.channel.revision, result.channel.compatibility
        ), meta=_meta(request)
    )
