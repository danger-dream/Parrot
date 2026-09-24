"""FastAPI adapter for proxy CRUD, group CRUD, probes and routing."""

from __future__ import annotations

from typing import Annotated, Any, Mapping

from fastapi import APIRouter, Depends, Header, Path, Query, Request, Response, status

from src.management_auth import Capability
from src.management_control import ManagementContext, ManagementErrorCode
from src.management_control.proxy import (
    ProxyControl,
    ProxyGroupRecord,
    ProxyRecord,
    ProxyRoutingRecord,
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
from ..schemas.proxy import (
    CreateProxyGroupRequest,
    CreateProxyRequest,
    ProxyData,
    ProxyEnvelope,
    ProxyGroupData,
    ProxyGroupEnvelope,
    ProxyGroupListEnvelope,
    ProxyGroupSort,
    ProxyListEnvelope,
    ProxyOperationEnvelope,
    ProxyRoutingData,
    ProxyRoutingEnvelope,
    ProxyRuntimeStats,
    ProxySort,
    ProxyType,
    UpdateProxyGroupRequest,
    UpdateProxyRequest,
    UpdateProxyRoutingRequest,
)


router = APIRouter()
ReadContext = Annotated[ManagementContext, Depends(require_capability(Capability.READ))]
WriteContext = Annotated[ManagementContext, Depends(require_capability(Capability.WRITE))]
SecretContext = Annotated[ManagementContext, Depends(require_capability(Capability.SECRETS_WRITE))]
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
    ManagementErrorCode.RESOURCE_CONFLICT,
    ManagementErrorCode.REVISION_CONFLICT,
)
_PROBE_ERRORS = (
    *_RESOURCE_ERRORS,
    ManagementErrorCode.DEPENDENCY_UNAVAILABLE,
    ManagementErrorCode.UPSTREAM_ERROR,
    ManagementErrorCode.UPSTREAM_TIMEOUT,
    ManagementErrorCode.STATE_CONFLICT,
)


def get_proxy_control(
    runtime: Annotated[ManagementRuntime, Depends(get_management_runtime)],
) -> ProxyControl:
    return get_management_control_owner(runtime).proxy


def _page_meta(request: Request, result) -> MappingListMeta:
    return MappingListMeta(
        requestId=management_request_id(request), page=result.page,
        pageSize=result.page_size, total=result.total, hasNext=result.has_next,
        revision=result.revision,
    )


def _stats(raw: Mapping[str, Any]) -> ProxyRuntimeStats:
    def value(*keys: str) -> int:
        return int(next((raw[key] for key in keys if key in raw), 0) or 0)

    return ProxyRuntimeStats(
        requests=value("requests"), successes=value("successes"), failures=value("failures"),
        inputTokens=value("input_tokens", "inputTokens"),
        outputTokens=value("output_tokens", "outputTokens"),
        cacheCreationTokens=value("cache_creation_tokens", "cacheCreationTokens"),
        cacheReadTokens=value("cache_read_tokens", "cacheReadTokens"),
        totalTokens=value("total_tokens", "totalTokens"),
        bytesUp=value("bytes_up", "bytesUp"), bytesDown=value("bytes_down", "bytesDown"),
        totalBytes=value("total_bytes", "totalBytes"),
        avgConnectMilliseconds=value("avg_connect_ms", "avgConnectMilliseconds"),
        avgFirstByteMilliseconds=value("avg_first_byte_ms", "avgFirstByteMilliseconds"),
        avgIdleMilliseconds=value("avg_idle_ms", "avgIdleMilliseconds"),
        avgTotalMilliseconds=value("avg_total_ms", "avgTotalMilliseconds"),
    )


def _proxy(item: ProxyRecord) -> ProxyData:
    return ProxyData(
        proxyId=item.proxy_id, name=item.name, type=item.type,
        maskedUrl=item.masked_url, server=item.server, port=item.port,
        cipher=item.cipher, runtimeStats=_stats(item.runtime_stats), revision=item.revision,
    )


def _group(item: ProxyGroupRecord) -> ProxyGroupData:
    return ProxyGroupData(
        groupId=item.group_id, name=item.name, members=list(item.members),
        runtimeStats=_stats(item.runtime_stats), revision=item.revision,
    )


def _routing(item: ProxyRoutingRecord) -> ProxyRoutingData:
    return ProxyRoutingData(
        default=item.default, directFallback=item.direct_fallback,
        functions=dict(item.functions), providers=dict(item.providers), accounts=dict(item.accounts),
        channels=dict(item.channels), models=dict(item.models), revision=item.revision,
    )


_STATS_EXAMPLE = {"requests": 2, "successes": 2, "failures": 0, "inputTokens": 0, "outputTokens": 0, "cacheCreationTokens": 0, "cacheReadTokens": 0, "totalTokens": 0, "bytesUp": 100, "bytesDown": 200, "totalBytes": 300, "avgConnectMilliseconds": 20, "avgFirstByteMilliseconds": 40, "avgIdleMilliseconds": 0, "avgTotalMilliseconds": 60}
_PROXY_EXAMPLE = {"proxyId": "edge-one", "name": "edge-one", "type": "socks5", "maskedUrl": "socks5://user:***@proxy.example.test:1080", "server": None, "port": None, "cipher": None, "runtimeStats": _STATS_EXAMPLE, "revision": "rev_example"}
_GROUP_EXAMPLE = {"groupId": "primary", "name": "primary", "members": ["edge-one", "direct"], "runtimeStats": _STATS_EXAMPLE, "revision": "rev_example"}
_ROUTING_EXAMPLE = {"default": "primary", "directFallback": False, "functions": {"telegram": "direct"}, "providers": {"workbuddy": "direct", "openai": "primary"}, "accounts": {}, "channels": {}, "models": {}, "revision": "rev_example"}
_OPERATION_EXAMPLE = {"id": "op_example", "kind": "proxy.test", "status": "queued", "progress": None, "createdAt": "2026-01-02T03:04:05Z", "startedAt": None, "finishedAt": None, "result": None, "error": None, "cancellable": False}


@router.get(
    "/proxies", operation_id="listProxies", tags=["management-proxy"],
    response_model=ProxyListEnvelope,
    responses={**_success(200, [_PROXY_EXAMPLE]), **management_error_responses(*_COMMON_ERRORS)},
)
def list_proxies(
    request: Request, context: ReadContext,
    control: Annotated[ProxyControl, Depends(get_proxy_control)],
    type_filter: Annotated[ProxyType | None, Query(alias="type")] = None,
    query: Annotated[str | None, Query(max_length=300)] = None,
    sort: ProxySort = ProxySort.NAME,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(alias="pageSize", ge=1, le=200)] = 50,
) -> ProxyListEnvelope:
    reject_unknown_query_parameters(
        request, {"type", "query", "sort", "page", "pageSize"}
    )
    result = control.list_proxies(
        context, type_filter=type_filter.value if type_filter else None,
        query=query, sort=sort.value, page=page, page_size=page_size,
    )
    return ProxyListEnvelope(
        data=[_proxy(item) for item in result.items], meta=_page_meta(request, result)
    )


@router.post(
    "/proxies", operation_id="createProxies", tags=["management-proxy"],
    status_code=status.HTTP_201_CREATED, response_model=ProxyEnvelope,
    responses={**_success(201, _PROXY_EXAMPLE), **management_error_responses(*_MUTATION_ERRORS)},
)
def create_proxies(
    body: CreateProxyRequest, request: Request, context: SecretContext,
    control: Annotated[ProxyControl, Depends(get_proxy_control)],
) -> ProxyEnvelope:
    reject_unknown_query_parameters(request)
    result = control.create_proxy(
        context, name=body.name, url=body.url.get_secret_value()
    )
    return ProxyEnvelope(data=_proxy(result), meta=_meta(request))


@router.get(
    "/proxies/{proxyId}", operation_id="getProxy", tags=["management-proxy"],
    response_model=ProxyEnvelope,
    responses={**_success(200, _PROXY_EXAMPLE), **management_error_responses(*_RESOURCE_ERRORS)},
)
def get_proxy(
    proxy_id: Annotated[str, Path(alias="proxyId", max_length=100)], request: Request,
    context: ReadContext, control: Annotated[ProxyControl, Depends(get_proxy_control)],
) -> ProxyEnvelope:
    reject_unknown_query_parameters(request)
    return ProxyEnvelope(data=_proxy(control.get_proxy(context, proxy_id)), meta=_meta(request))


@router.patch(
    "/proxies/{proxyId}", operation_id="updateProxy", tags=["management-proxy"],
    response_model=ProxyEnvelope,
    responses={**_success(200, _PROXY_EXAMPLE), **management_error_responses(*_MUTATION_ERRORS)},
)
def update_proxy(
    proxy_id: Annotated[str, Path(alias="proxyId", max_length=100)], body: UpdateProxyRequest,
    request: Request, context: WriteContext,
    control: Annotated[ProxyControl, Depends(get_proxy_control)], if_match: IfMatch = None,
) -> ProxyEnvelope:
    reject_unknown_query_parameters(request)
    result = control.update_proxy(
        context, proxy_id, name=body.name,
        url=body.url.get_secret_value() if body.url else None,
        expected_revision=if_match,
    )
    return ProxyEnvelope(data=_proxy(result), meta=_meta(request))


@router.delete(
    "/proxies/{proxyId}", operation_id="deleteProxy", tags=["management-proxy"],
    status_code=204,
    responses={204: {"description": "Unreferenced proxy deleted"}, **management_error_responses(*_MUTATION_ERRORS)},
)
def delete_proxy(
    proxy_id: Annotated[str, Path(alias="proxyId", max_length=100)], request: Request,
    context: DestroyContext,
    control: Annotated[ProxyControl, Depends(get_proxy_control)], if_match: IfMatch = None,
) -> Response:
    reject_unknown_query_parameters(request)
    control.delete_proxy(context, proxy_id, expected_revision=if_match)
    return Response(status_code=204)


@router.post(
    "/proxies/{proxyId}/actions/test", operation_id="testProxy", tags=["management-proxy"],
    status_code=202, response_model=ProxyOperationEnvelope,
    responses={**_success(202, _OPERATION_EXAMPLE), **management_error_responses(*_PROBE_ERRORS)},
)
def test_proxy(
    proxy_id: Annotated[str, Path(alias="proxyId", max_length=100)], request: Request,
    context: WriteContext, control: Annotated[ProxyControl, Depends(get_proxy_control)],
) -> ProxyOperationEnvelope:
    reject_unknown_query_parameters(request)
    return ProxyOperationEnvelope(
        data=_operation(control.start_proxy_test(context, proxy_id)), meta=_meta(request)
    )


@router.get(
    "/proxy-groups", operation_id="listProxyGroups", tags=["management-proxy"],
    response_model=ProxyGroupListEnvelope,
    responses={**_success(200, [_GROUP_EXAMPLE]), **management_error_responses(*_COMMON_ERRORS)},
)
def list_proxy_groups(
    request: Request, context: ReadContext,
    control: Annotated[ProxyControl, Depends(get_proxy_control)],
    query: Annotated[str | None, Query(max_length=300)] = None,
    sort: ProxyGroupSort = ProxyGroupSort.NAME,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(alias="pageSize", ge=1, le=200)] = 50,
) -> ProxyGroupListEnvelope:
    reject_unknown_query_parameters(request, {"query", "sort", "page", "pageSize"})
    result = control.list_groups(
        context, query=query, sort=sort.value, page=page, page_size=page_size
    )
    return ProxyGroupListEnvelope(
        data=[_group(item) for item in result.items], meta=_page_meta(request, result)
    )


@router.post(
    "/proxy-groups", operation_id="createProxyGroups", tags=["management-proxy"],
    status_code=201, response_model=ProxyGroupEnvelope,
    responses={**_success(201, _GROUP_EXAMPLE), **management_error_responses(*_MUTATION_ERRORS)},
)
def create_proxy_groups(
    body: CreateProxyGroupRequest, request: Request, context: WriteContext,
    control: Annotated[ProxyControl, Depends(get_proxy_control)],
) -> ProxyGroupEnvelope:
    reject_unknown_query_parameters(request)
    return ProxyGroupEnvelope(
        data=_group(control.create_group(context, name=body.name, members=body.members)),
        meta=_meta(request),
    )


@router.get(
    "/proxy-groups/{groupId}", operation_id="getProxyGroup", tags=["management-proxy"],
    response_model=ProxyGroupEnvelope,
    responses={**_success(200, _GROUP_EXAMPLE), **management_error_responses(*_RESOURCE_ERRORS)},
)
def get_proxy_group(
    group_id: Annotated[str, Path(alias="groupId", max_length=100)], request: Request,
    context: ReadContext, control: Annotated[ProxyControl, Depends(get_proxy_control)],
) -> ProxyGroupEnvelope:
    reject_unknown_query_parameters(request)
    return ProxyGroupEnvelope(
        data=_group(control.get_group(context, group_id)), meta=_meta(request)
    )


@router.patch(
    "/proxy-groups/{groupId}", operation_id="updateProxyGroup", tags=["management-proxy"],
    response_model=ProxyGroupEnvelope,
    responses={**_success(200, _GROUP_EXAMPLE), **management_error_responses(*_MUTATION_ERRORS)},
)
def update_proxy_group(
    group_id: Annotated[str, Path(alias="groupId", max_length=100)], body: UpdateProxyGroupRequest,
    request: Request, context: WriteContext,
    control: Annotated[ProxyControl, Depends(get_proxy_control)], if_match: IfMatch = None,
) -> ProxyGroupEnvelope:
    reject_unknown_query_parameters(request)
    result = control.update_group(
        context, group_id, name=body.name, members=body.members,
        expected_revision=if_match,
    )
    return ProxyGroupEnvelope(data=_group(result), meta=_meta(request))


@router.delete(
    "/proxy-groups/{groupId}", operation_id="deleteProxyGroup", tags=["management-proxy"],
    status_code=204,
    responses={204: {"description": "Unreferenced proxy group deleted"}, **management_error_responses(*_MUTATION_ERRORS)},
)
def delete_proxy_group(
    group_id: Annotated[str, Path(alias="groupId", max_length=100)], request: Request,
    context: DestroyContext,
    control: Annotated[ProxyControl, Depends(get_proxy_control)], if_match: IfMatch = None,
) -> Response:
    reject_unknown_query_parameters(request)
    control.delete_group(context, group_id, expected_revision=if_match)
    return Response(status_code=204)


@router.post(
    "/proxy-groups/{groupId}/actions/test", operation_id="testProxyGroup", tags=["management-proxy"],
    status_code=202, response_model=ProxyOperationEnvelope,
    responses={**_success(202, {**_OPERATION_EXAMPLE, "kind": "proxy_group.test"}), **management_error_responses(*_PROBE_ERRORS)},
)
def test_proxy_group(
    group_id: Annotated[str, Path(alias="groupId", max_length=100)], request: Request,
    context: WriteContext, control: Annotated[ProxyControl, Depends(get_proxy_control)],
) -> ProxyOperationEnvelope:
    reject_unknown_query_parameters(request)
    return ProxyOperationEnvelope(
        data=_operation(control.start_group_test(context, group_id)), meta=_meta(request)
    )


@router.get(
    "/proxy-routing", operation_id="getProxyRouting", tags=["management-proxy"],
    response_model=ProxyRoutingEnvelope,
    responses={**_success(200, _ROUTING_EXAMPLE), **management_error_responses(*_COMMON_ERRORS)},
)
def get_proxy_routing(
    request: Request, context: ReadContext,
    control: Annotated[ProxyControl, Depends(get_proxy_control)],
) -> ProxyRoutingEnvelope:
    reject_unknown_query_parameters(request)
    return ProxyRoutingEnvelope(
        data=_routing(control.get_routing(context)), meta=_meta(request)
    )


@router.patch(
    "/proxy-routing", operation_id="updateProxyRouting", tags=["management-proxy"],
    response_model=ProxyRoutingEnvelope,
    responses={**_success(200, _ROUTING_EXAMPLE), **management_error_responses(*_MUTATION_ERRORS)},
)
def update_proxy_routing(
    body: UpdateProxyRoutingRequest, request: Request, context: WriteContext,
    control: Annotated[ProxyControl, Depends(get_proxy_control)], if_match: IfMatch = None,
) -> ProxyRoutingEnvelope:
    reject_unknown_query_parameters(request)
    patch = body.model_dump(exclude_unset=True)
    result = control.update_routing(context, patch, expected_revision=if_match)
    return ProxyRoutingEnvelope(data=_routing(result), meta=_meta(request))
