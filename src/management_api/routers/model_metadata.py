"""FastAPI adapter for inventory, metadata binding, sync and catalog."""

from __future__ import annotations

from typing import Annotated, Any, Mapping

from fastapi import APIRouter, Depends, Header, Path, Query, Request, Response, status

from src.management_auth import Capability
from src.management_control import ManagementContext, ManagementErrorCode
from src.management_control.mapping import (
    CatalogRecord,
    InventoryRecord,
    MappingControl,
    MetadataOverridePatch,
    MetadataRecord,
    MetadataSyncTarget,
)

from ..dependencies import (
    ManagementRuntime,
    get_management_control_owner,
    get_management_runtime,
    management_request_id,
    require_capability,
)
from ..error_mapping import management_error_responses
from ..response_helpers import response_meta as _meta, success_response as _success
from ._operations import operation_data as _operation
from ._strict_query import reject_unknown_query_parameters
from ..schemas.mapping import MappingListMeta
from ..schemas.model_metadata import (
    CatalogData,
    CatalogListEnvelope,
    CatalogSort,
    InventorySort,
    MetadataOperationEnvelope,
    MetadataScope,
    MetadataSort,
    MetadataSyncRequest,
    ModelInventoryData,
    ModelInventoryListEnvelope,
    ModelMetadataData,
    ModelMetadataEnvelope,
    ModelMetadataListEnvelope,
    MetadataPricing,
    MetadataValues,
    PatchMetadataOverridesRequest,
    PutMetadataBindingRequest,
)


router = APIRouter()
ReadContext = Annotated[ManagementContext, Depends(require_capability(Capability.READ))]
WriteContext = Annotated[ManagementContext, Depends(require_capability(Capability.WRITE))]
DestroyContext = Annotated[ManagementContext, Depends(require_capability(Capability.DESTRUCTIVE))]
IfMatch = Annotated[str | None, Header(alias="If-Match")]

_COMMON_ERRORS = (
    ManagementErrorCode.SESSION_REQUIRED,
    ManagementErrorCode.SESSION_EXPIRED,
    ManagementErrorCode.CAPABILITY_DENIED,
    ManagementErrorCode.ORIGIN_DENIED,
    ManagementErrorCode.VALIDATION_FAILED,
    ManagementErrorCode.SERVICE_NOT_READY,
)
_RESOURCE_ERRORS = (*_COMMON_ERRORS, ManagementErrorCode.RESOURCE_NOT_FOUND)
_MUTATION_ERRORS = (
    *_RESOURCE_ERRORS,
    ManagementErrorCode.CONFIRMATION_REQUIRED,
    ManagementErrorCode.REVISION_CONFLICT,
)


def get_metadata_control(
    runtime: Annotated[ManagementRuntime, Depends(get_management_runtime)],
) -> MappingControl:
    return get_management_control_owner(runtime).mapping


def _page_meta(request: Request, result) -> MappingListMeta:
    return MappingListMeta(
        requestId=management_request_id(request),
        page=result.page,
        pageSize=result.page_size,
        total=result.total,
        hasNext=result.has_next,
        revision=result.revision,
    )


def _pricing(raw: Mapping[str, Any]) -> MetadataPricing | None:
    cost = raw.get("cost")
    if not isinstance(cost, Mapping):
        return None
    return MetadataPricing(
        input=cost.get("input"),
        output=cost.get("output"),
        cacheRead=cost.get("cache_read") if "cache_read" in cost else cost.get("cacheRead"),
        cacheWrite=cost.get("cache_write") if "cache_write" in cost else cost.get("cacheWrite"),
        longContextInput=(cost.get("context_over_200k") or {}).get("input") if isinstance(cost.get("context_over_200k"), Mapping) else cost.get("longContextInput"),
        longContextOutput=(cost.get("context_over_200k") or {}).get("output") if isinstance(cost.get("context_over_200k"), Mapping) else cost.get("longContextOutput"),
    )


def _values(raw: Mapping[str, Any]) -> MetadataValues:
    limit = raw.get("limit") if isinstance(raw.get("limit"), Mapping) else {}
    modalities = raw.get("modalities") if isinstance(raw.get("modalities"), Mapping) else {}
    return MetadataValues(
        contextWindow=raw.get("contextWindow") if "contextWindow" in raw else limit.get("context"),
        contextWindowMaxMode=raw.get("contextWindowMaxMode"),
        maxInputTokens=raw.get("maxInputTokens") if "maxInputTokens" in raw else limit.get("input"),
        maxOutputTokens=raw.get("maxOutputTokens") if "maxOutputTokens" in raw else limit.get("output"),
        compactTriggerTokens=raw.get("compactTriggerTokens"),
        vision=raw.get("vision") if isinstance(raw.get("vision"), bool) else None,
        toolCall=raw.get("toolCall") if isinstance(raw.get("toolCall"), bool) else None,
        structuredOutput=raw.get("structuredOutput") if isinstance(raw.get("structuredOutput"), bool) else None,
        reasoningEfforts=list(raw.get("reasoningEfforts") or []),
        serviceTiers=list(raw.get("serviceTiers") or []),
        knowledgeCutoff=raw.get("knowledgeCutoff") or None,
        defaultReasoningEffort=raw.get("defaultReasoningEffort"),
        inputModalities=list(raw.get("inputModalities") or modalities.get("input") or []),
        outputModalities=list(raw.get("outputModalities") or modalities.get("output") or []),
        cost=_pricing(raw),
    )


def _metadata(item: MetadataRecord) -> ModelMetadataData:
    return ModelMetadataData(
        modelId=item.model_id,
        target=item.target,
        providerId=item.provider_id,
        catalogModelId=item.catalog_model_id,
        scope=item.scope,
        scopeId=item.scope_id,
        outboundModel=item.outbound_model,
        source=item.source,
        authority=item.authority,
        effective=_values(item.effective),
        raw=_values(item.raw),
        valueSource=dict(item.value_source),
        constrainedBy={key: list(value) for key, value in item.constrained_by.items()},
        commonOverride=dict(item.common_override),
        sourceOverride=dict(item.source_override),
        revision=item.revision,
    )


def _inventory(item: InventoryRecord) -> ModelInventoryData:
    return ModelInventoryData(
        modelId=item.model_id,
        family=item.family,
        provider=item.provider,
        channelId=item.channel_id,
        accountId=item.account_id,
        outboundModel=item.outbound_model,
        revision=item.revision,
    )


def _catalog(item: CatalogRecord) -> CatalogData:
    return CatalogData(
        key=item.key,
        modelId=item.model_id,
        name=item.name,
        providerId=item.provider_id,
        providerName=item.provider_name,
        metadata=_values(item.metadata),
        revision=item.revision,
    )


_INVENTORY_EXAMPLE = {"modelId": "claude-sonnet-4-5", "family": "anthropic", "provider": "claude", "channelId": "oauth:claude:example", "accountId": "claude:example", "outboundModel": "claude-sonnet-4-5", "revision": "rev_example"}
_METADATA_EXAMPLE = {"modelId": "claude-sonnet-4-5", "target": "anthropic/claude-sonnet-4-5", "providerId": "anthropic", "catalogModelId": "claude-sonnet-4-5", "scope": "global", "scopeId": None, "outboundModel": None, "source": "manual", "authority": "models.dev", "effective": {"contextWindow": 200000, "reasoningEfforts": [], "inputModalities": [], "outputModalities": []}, "raw": {"contextWindow": 200000, "reasoningEfforts": [], "inputModalities": [], "outputModalities": []}, "revision": "rev_example"}
_CATALOG_EXAMPLE = {"key": "anthropic/claude-sonnet-4-5", "modelId": "claude-sonnet-4-5", "name": "Claude Sonnet 4.5", "providerId": "anthropic", "providerName": "Anthropic", "metadata": {"contextWindow": 200000, "reasoningEfforts": [], "inputModalities": [], "outputModalities": []}, "revision": "rev_example"}
_OPERATION_EXAMPLE = {"id": "op_example", "kind": "model_metadata.sync", "status": "queued", "progress": None, "createdAt": "2026-01-02T03:04:05Z", "startedAt": None, "finishedAt": None, "result": None, "error": None, "cancellable": False}


@router.get(
    "/models/inventory",
    operation_id="listModelInventory",
    tags=["management-model-metadata"],
    response_model=ModelInventoryListEnvelope,
    responses={**_success(200, [_INVENTORY_EXAMPLE]), **management_error_responses(*_COMMON_ERRORS)},
)
def list_model_inventory(
    request: Request,
    context: ReadContext,
    control: Annotated[MappingControl, Depends(get_metadata_control)],
    provider: Annotated[str | None, Query(max_length=200)] = None,
    family: Annotated[str | None, Query(pattern="^(anthropic|openai)$")] = None,
    query: Annotated[str | None, Query(max_length=300)] = None,
    sort: InventorySort = InventorySort.MODEL_ID,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(alias="pageSize", ge=1, le=200)] = 50,
) -> ModelInventoryListEnvelope:
    reject_unknown_query_parameters(
        request, {"provider", "family", "query", "sort", "page", "pageSize"}
    )
    result = control.list_inventory(
        context, provider=provider, family=family, query=query,
        sort=sort.value, page=page, page_size=page_size,
    )
    return ModelInventoryListEnvelope(
        data=[_inventory(item) for item in result.items], meta=_page_meta(request, result)
    )


@router.get(
    "/model-metadata",
    operation_id="listModelMetadata",
    tags=["management-model-metadata"],
    response_model=ModelMetadataListEnvelope,
    responses={**_success(200, [_METADATA_EXAMPLE]), **management_error_responses(*_RESOURCE_ERRORS)},
)
def list_model_metadata(
    request: Request,
    context: ReadContext,
    control: Annotated[MappingControl, Depends(get_metadata_control)],
    scope: MetadataScope | None = None,
    scope_id: Annotated[str | None, Query(alias="scopeId", min_length=1, max_length=500)] = None,
    query: Annotated[str | None, Query(max_length=300)] = None,
    sort: MetadataSort = MetadataSort.MODEL_ID,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(alias="pageSize", ge=1, le=200)] = 50,
) -> ModelMetadataListEnvelope:
    reject_unknown_query_parameters(
        request, {"scope", "scopeId", "query", "sort", "page", "pageSize"}
    )
    result = control.list_metadata(
        context, scope=scope.value if scope else None, scope_id=scope_id,
        query=query, sort=sort.value, page=page, page_size=page_size,
    )
    return ModelMetadataListEnvelope(
        data=[_metadata(item) for item in result.items], meta=_page_meta(request, result)
    )


@router.post(
    "/model-metadata/actions/sync",
    operation_id="syncModelMetadata",
    tags=["management-model-metadata"],
    status_code=status.HTTP_202_ACCEPTED,
    response_model=MetadataOperationEnvelope,
    responses={**_success(202, _OPERATION_EXAMPLE), **management_error_responses(*_RESOURCE_ERRORS, ManagementErrorCode.OPERATION_ALREADY_RUNNING, ManagementErrorCode.DEPENDENCY_UNAVAILABLE, ManagementErrorCode.STATE_CONFLICT)},
)
def sync_model_metadata(
    body: MetadataSyncRequest,
    request: Request,
    context: WriteContext,
    control: Annotated[MappingControl, Depends(get_metadata_control)],
    if_match: IfMatch = None,
) -> MetadataOperationEnvelope:
    reject_unknown_query_parameters(request)
    selected = body.mode or body.scope
    mode = (selected.value if selected is not None else "full")
    targets = tuple(
        MetadataSyncTarget(model_id=item.modelId, source=item.source)
        for item in body.targets
    )
    operation = control.start_metadata_sync(
        context,
        mode=mode,
        targets=targets,
        source=body.source,
        refresh_catalog=body.refreshCatalog,
        expected_revision=if_match,
        scope=mode if mode in {"provider", "account", "channel"} else None,
        provider_id=body.providerId,
        account_id=body.accountId,
        channel_id=body.channelId,
    )
    return MetadataOperationEnvelope(data=_operation(operation), meta=_meta(request))


@router.patch(
    "/model-metadata/{modelId:path}/overrides",
    operation_id="patchModelMetadataOverrides",
    tags=["management-model-metadata"],
    response_model=ModelMetadataEnvelope,
    responses={**_success(200, _METADATA_EXAMPLE), **management_error_responses(*_MUTATION_ERRORS)},
)
def patch_model_metadata_overrides(
    model_id: Annotated[str, Path(alias="modelId", max_length=500)],
    body: PatchMetadataOverridesRequest,
    request: Request,
    context: WriteContext,
    control: Annotated[MappingControl, Depends(get_metadata_control)],
    if_match: IfMatch = None,
) -> ModelMetadataEnvelope:
    reject_unknown_query_parameters(request)
    item = control.patch_metadata_overrides(
        context,
        model_id,
        scope=body.scope.value,
        account_id=body.accountId,
        channel_id=body.channelId,
        outbound_model=body.outboundModel,
        patch=MetadataOverridePatch(
            set_fields=body.set.model_dump(exclude_unset=True),
            unset_fields=tuple(body.unset),
        ),
        expected_revision=if_match,
    )
    return ModelMetadataEnvelope(data=_metadata(item), meta=_meta(request))


@router.delete(
    "/model-metadata/{modelId:path}/overrides",
    operation_id="deleteModelMetadataOverrides",
    tags=["management-model-metadata"],
    status_code=204,
    responses={204: {"description": "Override layer deleted"}, **management_error_responses(*_MUTATION_ERRORS)},
)
def delete_model_metadata_overrides(
    model_id: Annotated[str, Path(alias="modelId", max_length=500)],
    request: Request,
    context: DestroyContext,
    control: Annotated[MappingControl, Depends(get_metadata_control)],
    scope: MetadataScope = MetadataScope.GLOBAL,
    account_id: Annotated[str | None, Query(alias="accountId", min_length=1, max_length=500)] = None,
    channel_id: Annotated[str | None, Query(alias="channelId", min_length=1, max_length=500)] = None,
    if_match: IfMatch = None,
) -> Response:
    reject_unknown_query_parameters(request, {"scope", "accountId", "channelId"})
    control.delete_metadata_overrides(
        context,
        model_id,
        scope=scope.value,
        account_id=account_id,
        channel_id=channel_id,
        expected_revision=if_match,
    )
    return Response(status_code=204)


@router.put(
    "/model-metadata/{modelId:path}/binding",
    operation_id="putModelMetadataBinding",
    tags=["management-model-metadata"],
    response_model=ModelMetadataEnvelope,
    responses={**_success(200, _METADATA_EXAMPLE), **management_error_responses(*_MUTATION_ERRORS)},
)
def put_model_metadata_binding(
    model_id: Annotated[str, Path(alias="modelId", max_length=500)],
    body: PutMetadataBindingRequest,
    request: Request,
    context: WriteContext,
    control: Annotated[MappingControl, Depends(get_metadata_control)],
    if_match: IfMatch = None,
) -> ModelMetadataEnvelope:
    reject_unknown_query_parameters(request)
    item = control.put_binding(
        context, model_id,
        scope=body.scope.value,
        target_model_id=body.targetModelId,
        provider_id=body.providerId,
        account_id=body.accountId,
        channel_id=body.channelId,
        outbound_model=body.outboundModel,
        expected_revision=if_match,
    )
    return ModelMetadataEnvelope(data=_metadata(item), meta=_meta(request))


@router.delete(
    "/model-metadata/{modelId:path}/binding",
    operation_id="deleteModelMetadataBinding",
    tags=["management-model-metadata"],
    status_code=204,
    responses={204: {"description": "Binding deleted"}, **management_error_responses(*_MUTATION_ERRORS)},
)
def delete_model_metadata_binding(
    model_id: Annotated[str, Path(alias="modelId", max_length=500)],
    request: Request,
    context: DestroyContext,
    control: Annotated[MappingControl, Depends(get_metadata_control)],
    scope: MetadataScope = MetadataScope.GLOBAL,
    account_id: Annotated[str | None, Query(alias="accountId", min_length=1, max_length=500)] = None,
    channel_id: Annotated[str | None, Query(alias="channelId", min_length=1, max_length=500)] = None,
    if_match: IfMatch = None,
) -> Response:
    reject_unknown_query_parameters(request, {"scope", "accountId", "channelId"})
    control.delete_binding_control(
        context, model_id,
        scope=scope.value,
        account_id=account_id,
        channel_id=channel_id,
        expected_revision=if_match,
    )
    return Response(status_code=204)


@router.get(
    "/model-metadata/{modelId:path}",
    operation_id="getModelMetadata",
    tags=["management-model-metadata"],
    response_model=ModelMetadataEnvelope,
    responses={**_success(200, _METADATA_EXAMPLE), **management_error_responses(*_RESOURCE_ERRORS)},
)
def get_model_metadata(
    model_id: Annotated[str, Path(alias="modelId", max_length=500)],
    request: Request,
    context: ReadContext,
    control: Annotated[MappingControl, Depends(get_metadata_control)],
    scope_id: Annotated[str | None, Query(alias="scopeId", min_length=1, max_length=500)] = None,
) -> ModelMetadataEnvelope:
    reject_unknown_query_parameters(request, {"scopeId"})
    return ModelMetadataEnvelope(
        data=_metadata(control.get_metadata(context, model_id, scope_id=scope_id)),
        meta=_meta(request),
    )


@router.get(
    "/model-catalog",
    operation_id="searchModelCatalog",
    tags=["management-model-metadata"],
    response_model=CatalogListEnvelope,
    responses={**_success(200, [_CATALOG_EXAMPLE]), **management_error_responses(*_COMMON_ERRORS)},
)
def search_model_catalog(
    request: Request,
    context: ReadContext,
    control: Annotated[MappingControl, Depends(get_metadata_control)],
    provider: Annotated[str | None, Query(max_length=200)] = None,
    query: Annotated[str | None, Query(max_length=300)] = None,
    sort: CatalogSort = CatalogSort.NAME,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(alias="pageSize", ge=1, le=200)] = 50,
) -> CatalogListEnvelope:
    reject_unknown_query_parameters(
        request, {"provider", "query", "sort", "page", "pageSize"}
    )
    result = control.search_catalog(
        context, provider=provider, query=query, sort=sort.value,
        page=page, page_size=page_size,
    )
    return CatalogListEnvelope(
        data=[_catalog(item) for item in result.items], meta=_page_meta(request, result)
    )
