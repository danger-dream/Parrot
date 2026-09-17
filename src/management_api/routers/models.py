"""FastAPI adapter for unified model-center query and persistent state."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Path, Query, Request

from src.management_auth import Capability
from src.management_control import ManagementContext, ManagementErrorCode
from src.management_control.models import (
    ModelCenterControl,
    ModelFilters,
    ModelKind,
    ModelSelection,
    ModelSelectionMode,
    ModelSourceRef,
    ModelSourceType,
    ModelStateField,
    ModelStateTarget,
    ModelStatus,
    ModelView,
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
from ..schemas.models import (
    ModelData,
    ModelEnvelope,
    ModelIdentityData,
    ModelListEnvelope,
    ModelListMeta,
    ModelOwnerData,
    ModelSourceData,
    ModelStateData,
    ModelStateEnvelope,
    ModelStateItemData,
    ModelStateRequest,
)
from ._strict_query import reject_unknown_query_parameters


router = APIRouter(tags=["management-models"])
ReadContext = Annotated[ManagementContext, Depends(require_capability(Capability.READ))]
WriteContext = Annotated[ManagementContext, Depends(require_capability(Capability.WRITE))]
IfMatch = Annotated[str | None, Header(alias="If-Match")]

_COMMON_ERRORS = (
    ManagementErrorCode.SESSION_REQUIRED,
    ManagementErrorCode.SESSION_EXPIRED,
    ManagementErrorCode.CAPABILITY_DENIED,
    ManagementErrorCode.ORIGIN_DENIED,
    ManagementErrorCode.VALIDATION_FAILED,
    ManagementErrorCode.RESOURCE_NOT_FOUND,
    ManagementErrorCode.SERVICE_NOT_READY,
)
_MUTATION_ERRORS = (
    *_COMMON_ERRORS,
    ManagementErrorCode.CONFIRMATION_REQUIRED,
    ManagementErrorCode.REVISION_CONFLICT,
    ManagementErrorCode.RESOURCE_CONFLICT,
    ManagementErrorCode.UNSUPPORTED_VALUE,
    ManagementErrorCode.DEPENDENCY_UNAVAILABLE,
)


def get_model_control(
    runtime: Annotated[ManagementRuntime, Depends(get_management_runtime)],
) -> ModelCenterControl:
    return get_management_control_owner(runtime).models


def _source_ref(source_type: ModelSourceType | None, source_id: str | None) -> ModelSourceRef | None:
    if source_type is None:
        if source_id is not None:
            raise ModelCenterControl._validation(
                "sourceId", "REQUIRES_SOURCE_TYPE", "sourceId requires sourceType",
            )
        return None
    if source_type is ModelSourceType.GLOBAL and source_id is not None:
        raise ModelCenterControl._validation(
            "sourceId", "NOT_ALLOWED", "global source does not accept sourceId",
        )
    return ModelSourceRef(source_type, (source_id or "").strip())


def _filters(
    *,
    kinds: list[ModelKind] | None,
    text: str | None,
    source_type: ModelSourceType | None,
    source_id: str | None,
    statuses: list[ModelStatus] | None,
) -> ModelFilters:
    return ModelFilters(
        kinds=tuple(kinds or ()), text=text,
        source=_source_ref(source_type, source_id),
        statuses=tuple(statuses or ()),
    )


def _model(item: ModelView) -> ModelData:
    identity = item.identity
    owner = identity.owner
    return ModelData(
        resourceKey=item.resource_key,
        identity=ModelIdentityData(
            type=identity.kind, modelId=identity.model_id,
            provider=identity.provider,
            owner=None if owner is None else ModelOwnerData(type=owner.type, id=owner.id),
        ),
        modelId=item.model_id,
        aliases=list(item.aliases),
        globalEnabled=item.global_enabled,
        visible=item.visible,
        sourceCount=len(item.sources),
        commonMetadata=dict(item.common_metadata),
        sources=[
            ModelSourceData(
                type=source.type, id=source.id, label=source.label,
                provider=source.provider, outboundModel=source.outbound_model,
                sourceEnabled=source.source_enabled,
                containerEnabled=source.container_enabled,
                effectiveRoutable=source.effective_routable,
                unavailableReason=source.unavailable_reason,
                effectiveMetadata=dict(source.effective_metadata),
                valueSource=dict(source.value_source),
                constrainedBy={key: list(value) for key, value in source.constrained_by.items()},
            )
            for source in item.sources
        ],
        editable=item.editable,
        revision=item.revision,
    )


_MODEL_EXAMPLE = {
    "resourceKey": "mdl_example",
    "identity": {"type": "chat", "modelId": "gpt-example"},
    "modelId": "gpt-example",
    "aliases": ["assistant-latest"],
    "globalEnabled": True,
    "visible": True,
    "sourceCount": 1,
    "commonMetadata": {},
    "sources": [],
    "editable": True,
    "revision": "rev_example",
}


@router.get(
    "/models",
    operation_id="listModels",
    response_model=ModelListEnvelope,
    responses={**_success(200, [_MODEL_EXAMPLE]), **management_error_responses(*_COMMON_ERRORS)},
)
def list_models(
    request: Request,
    context: ReadContext,
    control: Annotated[ModelCenterControl, Depends(get_model_control)],
    types: Annotated[list[ModelKind] | None, Query(alias="type")] = None,
    text: Annotated[str | None, Query(max_length=300)] = None,
    source_type: Annotated[ModelSourceType | None, Query(alias="sourceType")] = None,
    source_id: Annotated[str | None, Query(alias="sourceId", max_length=500)] = None,
    statuses: Annotated[list[ModelStatus] | None, Query(alias="status")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(alias="pageSize", ge=1, le=200)] = 50,
) -> ModelListEnvelope:
    reject_unknown_query_parameters(
        request, {"type", "text", "sourceType", "sourceId", "status", "page", "pageSize"},
    )
    result = control.list_models(
        context,
        filters=_filters(
            kinds=types, text=text, source_type=source_type,
            source_id=source_id, statuses=statuses,
        ),
        page=page,
        page_size=page_size,
    )
    return ModelListEnvelope(
        data=[_model(item) for item in result.items],
        meta=ModelListMeta(
            requestId=management_request_id(request), page=result.page,
            pageSize=result.page_size, total=result.total, hasNext=result.has_next,
            revision=result.revision,
        ),
    )


@router.patch(
    "/models/actions/state",
    operation_id="setModelState",
    response_model=ModelStateEnvelope,
    responses={**_success(200, {"items": [], "revision": "rev_example"}),
               **management_error_responses(*_MUTATION_ERRORS)},
)
def set_model_state(
    body: ModelStateRequest,
    request: Request,
    context: WriteContext,
    control: Annotated[ModelCenterControl, Depends(get_model_control)],
    if_match: IfMatch = None,
) -> ModelStateEnvelope:
    reject_unknown_query_parameters(request)
    scope = None
    if body.scope.type is not ModelSourceType.GLOBAL:
        scope = ModelSourceRef(body.scope.type, str(body.scope.id))
    request_filter = body.selection.filter
    filters = None
    if request_filter is not None:
        filters = _filters(
            kinds=request_filter.type,
            text=request_filter.text,
            source_type=request_filter.sourceType,
            source_id=request_filter.sourceId,
            statuses=request_filter.status,
        )
    selection = ModelSelection(
        mode=ModelSelectionMode(body.selection.mode),
        model_ids=tuple(body.selection.modelIds),
        filters=filters,
        excluded_model_ids=tuple(body.selection.excludedModelIds),
    )
    if body.target.enabled is not None:
        target = ModelStateTarget(ModelStateField.ENABLED, body.target.enabled)
    else:
        target = ModelStateTarget(ModelStateField.VISIBLE, bool(body.target.visible))
    result = control.set_state(
        context, scope=scope, selection=selection, target=target,
        expected_revision=if_match,
    )
    return ModelStateEnvelope(
        data=ModelStateData(
            items=[ModelStateItemData(modelId=item.model_id, status=item.status) for item in result.items],
            revision=result.revision,
        ),
        meta=_meta(request),
    )


@router.get(
    "/models/{resource_key}",
    operation_id="getModel",
    response_model=ModelEnvelope,
    responses={**_success(200, _MODEL_EXAMPLE), **management_error_responses(*_COMMON_ERRORS)},
)
def get_model(
    resource_key: Annotated[str, Path(min_length=1, max_length=200)],
    request: Request,
    context: ReadContext,
    control: Annotated[ModelCenterControl, Depends(get_model_control)],
) -> ModelEnvelope:
    reject_unknown_query_parameters(request)
    return ModelEnvelope(data=_model(control.get_model(context, resource_key)), meta=_meta(request))
