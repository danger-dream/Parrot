"""FastAPI adapter for load-balancing orders and affinity clear actions."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Path, Query, Request, Response

from src.management_auth import Capability
from src.management_control import ManagementContext, ManagementErrorCode
from src.management_control.load_balancing import (
    AffinityClearResult,
    LoadBalancingControl,
    LoadBalancingRecord,
    OrderRecord,
)

from ..dependencies import (
    ManagementRuntime,
    get_management_control_owner,
    get_management_runtime,
    require_capability,
)
from ..error_mapping import management_error_responses
from ..response_helpers import response_meta as _meta
from ._strict_query import reject_unknown_query_parameters
from ..schemas.load_balancing import (
    AffinityClearData,
    AffinityClearEnvelope,
    AffinityFamily,
    BulkOrderData,
    BulkOrderEnvelope,
    BulkReplaceOrdersRequest,
    LoadBalancingData,
    LoadBalancingEnvelope,
    OrderData,
    OrderEnvelope,
    ReplaceOrderRequest,
    UpdateLoadBalancingRequest,
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
_REORDER_ERRORS = (
    *_RESOURCE_ERRORS,
    ManagementErrorCode.CONFIRMATION_REQUIRED,
    ManagementErrorCode.REVISION_CONFLICT,
)


def get_load_balancing_control(
    runtime: Annotated[ManagementRuntime, Depends(get_management_runtime)],
) -> LoadBalancingControl:
    return get_management_control_owner(runtime).load_balancing


def _load_balancing(item: LoadBalancingRecord) -> LoadBalancingData:
    return LoadBalancingData(mode=item.mode, revision=item.revision)


def _order(item: OrderRecord) -> OrderData:
    return OrderData(
        modelId=item.model_id,
        order=list(item.order),
        source=item.source,
        revision=item.revision,
    )


def _affinity(item: AffinityClearResult) -> AffinityClearData:
    return AffinityClearData(
        family=item.family,
        fingerprintCount=item.fingerprint_count,
        clientCount=item.client_count,
        revision=item.revision,
    )


def _success(code: int, data: dict) -> dict[int, dict]:
    return {code: {"description": "Successful Response", "content": {"application/json": {"example": {"data": data, "meta": {"requestId": "request-example"}}}}}}


_LB_EXAMPLE = {"mode": "priority", "revision": "rev_example"}
_ORDER_EXAMPLE = {"modelId": None, "order": ["oauth:example", "api:backup"], "source": "channelDefault", "revision": "rev_example"}
_MODEL_ORDER_EXAMPLE = {"modelId": "claude-sonnet-4-5", "order": ["oauth:example"], "source": "modelOverride", "revision": "rev_example"}
_AFFINITY_EXAMPLE = {"family": None, "fingerprintCount": 2, "clientCount": 1, "revision": "rev_example"}


@router.get(
    "/load-balancing",
    operation_id="getLoadBalancing",
    tags=["management-load-balancing"],
    response_model=LoadBalancingEnvelope,
    responses={**_success(200, _LB_EXAMPLE), **management_error_responses(*_COMMON_ERRORS)},
)
def get_load_balancing(
    request: Request,
    context: ReadContext,
    control: Annotated[LoadBalancingControl, Depends(get_load_balancing_control)],
) -> LoadBalancingEnvelope:
    reject_unknown_query_parameters(request)
    return LoadBalancingEnvelope(
        data=_load_balancing(control.get(context)), meta=_meta(request)
    )


@router.patch(
    "/load-balancing",
    operation_id="updateLoadBalancingMode",
    tags=["management-load-balancing"],
    response_model=LoadBalancingEnvelope,
    responses={**_success(200, _LB_EXAMPLE), **management_error_responses(*_REORDER_ERRORS)},
)
def update_load_balancing_mode(
    body: UpdateLoadBalancingRequest,
    request: Request,
    context: WriteContext,
    control: Annotated[LoadBalancingControl, Depends(get_load_balancing_control)],
    if_match: IfMatch = None,
) -> LoadBalancingEnvelope:
    reject_unknown_query_parameters(request)
    result = control.update_mode(
        context, body.mode.value, expected_revision=if_match
    )
    return LoadBalancingEnvelope(data=_load_balancing(result), meta=_meta(request))


@router.get(
    "/load-balancing/channel-order",
    operation_id="getChannelOrder",
    tags=["management-load-balancing"],
    response_model=OrderEnvelope,
    responses={**_success(200, _ORDER_EXAMPLE), **management_error_responses(*_COMMON_ERRORS)},
)
def get_channel_order(
    request: Request,
    context: ReadContext,
    control: Annotated[LoadBalancingControl, Depends(get_load_balancing_control)],
) -> OrderEnvelope:
    reject_unknown_query_parameters(request)
    return OrderEnvelope(
        data=_order(control.get_channel_order(context)), meta=_meta(request)
    )


@router.put(
    "/load-balancing/channel-order",
    operation_id="replaceChannelOrder",
    tags=["management-load-balancing"],
    response_model=OrderEnvelope,
    responses={**_success(200, _ORDER_EXAMPLE), **management_error_responses(*_REORDER_ERRORS)},
)
def replace_channel_order(
    body: ReplaceOrderRequest,
    request: Request,
    context: WriteContext,
    control: Annotated[LoadBalancingControl, Depends(get_load_balancing_control)],
    if_match: IfMatch = None,
) -> OrderEnvelope:
    reject_unknown_query_parameters(request)
    result = control.replace_channel_order(
        context, body.order, expected_revision=if_match
    )
    return OrderEnvelope(data=_order(result), meta=_meta(request))


@router.get(
    "/load-balancing/model-orders",
    operation_id="listModelChannelOrders",
    tags=["management-load-balancing"],
    response_model=BulkOrderEnvelope,
    responses={**_success(200, {"orders": [_MODEL_ORDER_EXAMPLE], "revision": "rev_example"}),
               **management_error_responses(*_RESOURCE_ERRORS)},
)
def list_model_channel_orders(
    request: Request,
    context: ReadContext,
    control: Annotated[LoadBalancingControl, Depends(get_load_balancing_control)],
    source: Annotated[Literal["modelOverride", "channelDefault"] | None, Query()] = None,
) -> BulkOrderEnvelope:
    """Batch form of getModelChannelOrder (one request instead of one per model)."""
    reject_unknown_query_parameters(request, {"source"})
    records, revision = control.list_model_orders(context, source=source)
    return BulkOrderEnvelope(
        data=BulkOrderData(orders=[_order(item) for item in records], revision=revision),
        meta=_meta(request),
    )


@router.get(
    "/load-balancing/model-orders/{modelId:path}",
    operation_id="getModelChannelOrder",
    tags=["management-load-balancing"],
    response_model=OrderEnvelope,
    responses={**_success(200, _MODEL_ORDER_EXAMPLE), **management_error_responses(*_RESOURCE_ERRORS)},
)
def get_model_channel_order(
    model_id: Annotated[str, Path(alias="modelId", max_length=500)],
    request: Request,
    context: ReadContext,
    control: Annotated[LoadBalancingControl, Depends(get_load_balancing_control)],
) -> OrderEnvelope:
    reject_unknown_query_parameters(request)
    return OrderEnvelope(
        data=_order(control.get_model_order(context, model_id)), meta=_meta(request)
    )


@router.put(
    "/load-balancing/model-orders/{modelId:path}",
    operation_id="replaceModelChannelOrder",
    tags=["management-load-balancing"],
    response_model=OrderEnvelope,
    responses={**_success(200, _MODEL_ORDER_EXAMPLE), **management_error_responses(*_REORDER_ERRORS)},
)
def replace_model_channel_order(
    model_id: Annotated[str, Path(alias="modelId", max_length=500)],
    body: ReplaceOrderRequest,
    request: Request,
    context: WriteContext,
    control: Annotated[LoadBalancingControl, Depends(get_load_balancing_control)],
    if_match: IfMatch = None,
) -> OrderEnvelope:
    reject_unknown_query_parameters(request)
    result = control.replace_model_order(
        context, model_id, body.order, expected_revision=if_match
    )
    return OrderEnvelope(data=_order(result), meta=_meta(request))


@router.delete(
    "/load-balancing/model-orders/{modelId:path}",
    operation_id="deleteModelChannelOrder",
    tags=["management-load-balancing"],
    status_code=204,
    responses={204: {"description": "Model order deleted"}, **management_error_responses(*_REORDER_ERRORS)},
)
def delete_model_channel_order(
    model_id: Annotated[str, Path(alias="modelId", max_length=500)],
    request: Request,
    context: DestroyContext,
    control: Annotated[LoadBalancingControl, Depends(get_load_balancing_control)],
    if_match: IfMatch = None,
) -> Response:
    reject_unknown_query_parameters(request)
    control.delete_model_order(
        context, model_id, expected_revision=if_match
    )
    return Response(status_code=204)


@router.put(
    "/load-balancing/model-orders",
    operation_id="bulkReplaceModelChannelOrders",
    tags=["management-load-balancing"],
    response_model=BulkOrderEnvelope,
    responses={**_success(200, {"orders": [_MODEL_ORDER_EXAMPLE], "revision": "rev_example"}), **management_error_responses(*_REORDER_ERRORS)},
)
def bulk_replace_model_channel_orders(
    body: BulkReplaceOrdersRequest,
    request: Request,
    context: WriteContext,
    control: Annotated[LoadBalancingControl, Depends(get_load_balancing_control)],
    if_match: IfMatch = None,
) -> BulkOrderEnvelope:
    reject_unknown_query_parameters(request)
    results = control.bulk_replace_model_orders(
        context, body.modelIds, body.order, expected_revision=if_match
    )
    revision = results[0].revision if results else ""
    return BulkOrderEnvelope(
        data=BulkOrderData(orders=[_order(item) for item in results], revision=revision),
        meta=_meta(request),
    )


@router.post(
    "/affinity/actions/clear",
    operation_id="clearAllAffinity",
    tags=["management-load-balancing"],
    response_model=AffinityClearEnvelope,
    responses={**_success(200, _AFFINITY_EXAMPLE), **management_error_responses(*_COMMON_ERRORS)},
)
def clear_all_affinity(
    request: Request,
    context: DestroyContext,
    control: Annotated[LoadBalancingControl, Depends(get_load_balancing_control)],
) -> AffinityClearEnvelope:
    reject_unknown_query_parameters(request)
    return AffinityClearEnvelope(
        data=_affinity(control.clear_all_affinity(context)), meta=_meta(request)
    )


@router.post(
    "/affinity/families/{family}/actions/clear",
    operation_id="clearFamilyAffinity",
    tags=["management-load-balancing"],
    response_model=AffinityClearEnvelope,
    responses={**_success(200, {**_AFFINITY_EXAMPLE, "family": "anthropic"}), **management_error_responses(*_COMMON_ERRORS)},
)
def clear_family_affinity(
    family: AffinityFamily,
    request: Request,
    context: DestroyContext,
    control: Annotated[LoadBalancingControl, Depends(get_load_balancing_control)],
) -> AffinityClearEnvelope:
    reject_unknown_query_parameters(request)
    return AffinityClearEnvelope(
        data=_affinity(control.clear_family_affinity(context, family.value)),
        meta=_meta(request),
    )
