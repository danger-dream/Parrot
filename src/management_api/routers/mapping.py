"""FastAPI adapter for mappings, ingress defaults and compression model."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Path, Query, Request, Response, status

from src.management_auth import Capability
from src.management_control import ManagementContext, ManagementErrorCode
from src.management_control.mapping import MappingControl, MappingRecord

from ..dependencies import (
    ManagementRuntime,
    get_management_control_owner,
    get_management_runtime,
    management_request_id,
    require_capability,
)
from ..error_mapping import management_error_responses
from ..response_helpers import response_meta as _meta, success_response as _success
from ._strict_query import reject_unknown_query_parameters
from ..schemas.mapping import (
    CompressionModelData,
    CompressionModelEnvelope,
    Ingress,
    IngressDefaultData,
    IngressDefaultEnvelope,
    MappingData,
    MappingEnvelope,
    MappingListEnvelope,
    MappingListMeta,
    MappingSort,
    PutCompressionModelRequest,
    PutIngressDefaultRequest,
    PutMappingRequest,
    UpdateMappingRequest,
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
    ManagementErrorCode.RESOURCE_CONFLICT,
    ManagementErrorCode.UNSUPPORTED_VALUE,
)


def get_mapping_control(
    runtime: Annotated[ManagementRuntime, Depends(get_management_runtime)],
) -> MappingControl:
    return get_management_control_owner(runtime).mapping


def _mapping(item: MappingRecord) -> MappingData:
    return MappingData(
        alias=item.alias,
        realModel=item.real_model,
        sourceLine=item.source_line,
        revision=item.revision,
    )


_MAPPING_EXAMPLE = {
    "alias": "assistant-latest",
    "realModel": "claude-sonnet-4-5",
    "sourceLine": "global",
    "revision": "rev_example",
}
_DEFAULT_EXAMPLE = {
    "ingress": "anthropic",
    "modelId": "claude-sonnet-4-5",
    "revision": "rev_example",
}
_COMPRESSION_EXAMPLE = {"modelId": "claude-sonnet-4-5", "revision": "rev_example"}


@router.get(
    "/model-mappings",
    operation_id="listModelMappings",
    tags=["management-model-mapping"],
    response_model=MappingListEnvelope,
    responses={**_success(200, [_MAPPING_EXAMPLE]), **management_error_responses(*_COMMON_ERRORS)},
)
def list_model_mappings(
    request: Request,
    context: ReadContext,
    control: Annotated[MappingControl, Depends(get_mapping_control)],
    query: Annotated[str | None, Query(max_length=300)] = None,
    sort: MappingSort = MappingSort.ALIAS,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(alias="pageSize", ge=1, le=200)] = 50,
) -> MappingListEnvelope:
    reject_unknown_query_parameters(request, {"query", "sort", "page", "pageSize"})
    result = control.list_mappings(
        context,
        query=query,
        sort=sort.value,
        page=page,
        page_size=page_size,
    )
    return MappingListEnvelope(
        data=[_mapping(item) for item in result.items],
        meta=MappingListMeta(
            requestId=management_request_id(request),
            page=result.page,
            pageSize=result.page_size,
            total=result.total,
            hasNext=result.has_next,
            revision=result.revision,
        ),
    )


@router.put(
    "/model-mappings/{alias:path}",
    operation_id="putModelMapping",
    tags=["management-model-mapping"],
    response_model=MappingEnvelope,
    responses={**_success(200, _MAPPING_EXAMPLE), **management_error_responses(*_MUTATION_ERRORS)},
)
def put_model_mapping(
    alias: Annotated[str, Path(min_length=1, max_length=300)],
    body: PutMappingRequest,
    request: Request,
    context: WriteContext,
    control: Annotated[MappingControl, Depends(get_mapping_control)],
    if_match: IfMatch = None,
) -> MappingEnvelope:
    reject_unknown_query_parameters(request)
    item = control.put_mapping(
        context, alias, body.realModel, expected_revision=if_match
    )
    return MappingEnvelope(data=_mapping(item), meta=_meta(request))


@router.patch(
    "/model-mappings/{alias:path}",
    operation_id="updateModelMapping",
    tags=["management-model-mapping"],
    response_model=MappingEnvelope,
    responses={**_success(200, _MAPPING_EXAMPLE), **management_error_responses(*_MUTATION_ERRORS)},
)
def update_model_mapping(
    alias: Annotated[str, Path(min_length=1, max_length=300)],
    body: UpdateMappingRequest,
    request: Request,
    context: WriteContext,
    control: Annotated[MappingControl, Depends(get_mapping_control)],
    if_match: IfMatch = None,
) -> MappingEnvelope:
    reject_unknown_query_parameters(request)
    item = control.update_mapping(
        context,
        alias,
        new_alias=body.alias,
        real_model=body.realModel,
        expected_revision=if_match,
    )
    return MappingEnvelope(data=_mapping(item), meta=_meta(request))


@router.delete(
    "/model-mappings/{alias:path}",
    operation_id="deleteModelMapping",
    tags=["management-model-mapping"],
    status_code=status.HTTP_204_NO_CONTENT,
    responses={204: {"description": "Mapping deleted"}, **management_error_responses(*_MUTATION_ERRORS)},
)
def delete_model_mapping(
    alias: Annotated[str, Path(min_length=1, max_length=300)],
    request: Request,
    context: DestroyContext,
    control: Annotated[MappingControl, Depends(get_mapping_control)],
    if_match: IfMatch = None,
) -> Response:
    reject_unknown_query_parameters(request)
    control.delete_mapping(context, alias, expected_revision=if_match)
    return Response(status_code=204)


@router.get(
    "/ingress-default-models/{ingress}",
    operation_id="getIngressDefaultModel",
    tags=["management-model-mapping"],
    response_model=IngressDefaultEnvelope,
    responses={**_success(200, _DEFAULT_EXAMPLE), **management_error_responses(*_COMMON_ERRORS)},
)
def get_ingress_default_model(
    ingress: Ingress,
    request: Request,
    context: ReadContext,
    control: Annotated[MappingControl, Depends(get_mapping_control)],
) -> IngressDefaultEnvelope:
    reject_unknown_query_parameters(request)
    item = control.get_ingress_default(context, ingress.value)
    return IngressDefaultEnvelope(
        data=IngressDefaultData(
            ingress=ingress, modelId=item.model_id, revision=item.revision
        ),
        meta=_meta(request),
    )


@router.put(
    "/ingress-default-models/{ingress}",
    operation_id="putIngressDefaultModel",
    tags=["management-model-mapping"],
    response_model=IngressDefaultEnvelope,
    responses={**_success(200, _DEFAULT_EXAMPLE), **management_error_responses(*_MUTATION_ERRORS)},
)
def put_ingress_default_model(
    ingress: Ingress,
    body: PutIngressDefaultRequest,
    request: Request,
    context: WriteContext,
    control: Annotated[MappingControl, Depends(get_mapping_control)],
    if_match: IfMatch = None,
) -> IngressDefaultEnvelope:
    reject_unknown_query_parameters(request)
    item = control.put_ingress_default(
        context, ingress.value, body.modelId, expected_revision=if_match
    )
    return IngressDefaultEnvelope(
        data=IngressDefaultData(
            ingress=ingress, modelId=item.model_id, revision=item.revision
        ),
        meta=_meta(request),
    )


@router.delete(
    "/ingress-default-models/{ingress}",
    operation_id="deleteIngressDefaultModel",
    tags=["management-model-mapping"],
    status_code=204,
    responses={204: {"description": "Ingress default deleted"}, **management_error_responses(*_MUTATION_ERRORS)},
)
def delete_ingress_default_model(
    ingress: Ingress,
    request: Request,
    context: DestroyContext,
    control: Annotated[MappingControl, Depends(get_mapping_control)],
    if_match: IfMatch = None,
) -> Response:
    reject_unknown_query_parameters(request)
    control.delete_ingress_default(
        context, ingress.value, expected_revision=if_match
    )
    return Response(status_code=204)


@router.get(
    "/compression-model",
    operation_id="getCompressionModel",
    tags=["management-model-metadata"],
    response_model=CompressionModelEnvelope,
    responses={**_success(200, _COMPRESSION_EXAMPLE), **management_error_responses(*_COMMON_ERRORS)},
)
def get_compression_model(
    request: Request,
    context: ReadContext,
    control: Annotated[MappingControl, Depends(get_mapping_control)],
) -> CompressionModelEnvelope:
    reject_unknown_query_parameters(request)
    model_id, revision = control.get_compression(context)
    return CompressionModelEnvelope(
        data=CompressionModelData(modelId=model_id, revision=revision),
        meta=_meta(request),
    )


@router.put(
    "/compression-model",
    operation_id="putCompressionModel",
    tags=["management-model-metadata"],
    response_model=CompressionModelEnvelope,
    responses={**_success(200, _COMPRESSION_EXAMPLE), **management_error_responses(*_MUTATION_ERRORS)},
)
def put_compression_model(
    body: PutCompressionModelRequest,
    request: Request,
    context: WriteContext,
    control: Annotated[MappingControl, Depends(get_mapping_control)],
    if_match: IfMatch = None,
) -> CompressionModelEnvelope:
    reject_unknown_query_parameters(request)
    model_id, revision = control.put_compression(
        context, body.modelId, expected_revision=if_match
    )
    return CompressionModelEnvelope(
        data=CompressionModelData(modelId=model_id, revision=revision),
        meta=_meta(request),
    )


@router.delete(
    "/compression-model",
    operation_id="deleteCompressionModel",
    tags=["management-model-metadata"],
    status_code=204,
    responses={204: {"description": "Compression model cleared"}, **management_error_responses(*_MUTATION_ERRORS)},
)
def delete_compression_model(
    request: Request,
    context: DestroyContext,
    control: Annotated[MappingControl, Depends(get_mapping_control)],
    if_match: IfMatch = None,
) -> Response:
    reject_unknown_query_parameters(request)
    control.delete_compression(context, expected_revision=if_match)
    return Response(status_code=204)
