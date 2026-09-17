"""Independent Management API search entry points (never inference auth)."""
from __future__ import annotations

from typing import Annotated
from threading import RLock
from fastapi import APIRouter, Depends, Header, Path, Request, Response
from src.management_auth import Capability
from src.management_control import ManagementContext, ManagementErrorCode
from src.management_control.search import SearchControl
from ..dependencies import ManagementRuntime, get_management_runtime, management_request_id, require_capability
from ..error_mapping import management_error_responses
from ..response_helpers import response_meta
from ..schemas.base import DataEnvelope
from ..schemas.operations import ManagementOperationData
from ..schemas.search import (
    SearchAccountData, SearchBackendCreate, SearchBackendPatch, SearchPriorityRequest,
    SearchSettingsData, SearchSettingsPatch, SearchTestRequest,
)
from ._operations import operation_data
from .auxiliary_support import reject_unknown_query

def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(tags=["management-search"], dependencies=[Depends(reject_unknown_query()), Depends(_no_store)], responses=management_error_responses(
    ManagementErrorCode.SESSION_REQUIRED, ManagementErrorCode.SESSION_EXPIRED,
    ManagementErrorCode.ORIGIN_DENIED, ManagementErrorCode.CAPABILITY_DENIED,
    ManagementErrorCode.RESOURCE_NOT_FOUND, ManagementErrorCode.RESOURCE_CONFLICT,
    ManagementErrorCode.REVISION_CONFLICT, ManagementErrorCode.STATE_CONFLICT,
    ManagementErrorCode.CONFIRMATION_REQUIRED,
    ManagementErrorCode.VALIDATION_FAILED, ManagementErrorCode.SERVICE_NOT_READY,
    ManagementErrorCode.DEPENDENCY_UNAVAILABLE, ManagementErrorCode.OPERATION_ALREADY_RUNNING,
))


_control_lock = RLock()


def get_search_control(request: Request, runtime: Annotated[ManagementRuntime, Depends(get_management_runtime)]) -> SearchControl:
    # Concurrent first probes must share the same idempotency ledger/owner.
    with _control_lock:
        current = getattr(request.app.state, "management_search_control", None)
        owner = getattr(request.app.state, "management_search_runtime", None)
        if isinstance(current, SearchControl) and (owner is None or owner is runtime):
            return current
        current = SearchControl(audit_sink=runtime.audit_sink, operations=runtime.operations)
        request.app.state.management_search_control = current
        request.app.state.management_search_runtime = runtime
        return current


Control = Annotated[SearchControl, Depends(get_search_control)]
ReadContext = Annotated[ManagementContext, Depends(require_capability(Capability.READ))]
WriteContext = Annotated[ManagementContext, Depends(require_capability(Capability.WRITE))]
Revision = Annotated[str | None, Header(alias="If-Match")]


def _response(request, value):
    return DataEnvelope(data=SearchSettingsData(**value), meta=response_meta(request))


@router.get("/search", operation_id="getSearchSettings", response_model=DataEnvelope[SearchSettingsData])
def get_search(request: Request, control: Control, context: ReadContext):
    return _response(request, control.get(context))


@router.patch("/search", operation_id="updateSearchSettings", response_model=DataEnvelope[SearchSettingsData])
def patch_search(body: SearchSettingsPatch, request: Request, control: Control, context: WriteContext, if_match: Revision = None):
    return _response(request, control.patch(context, body.model_dump(exclude_unset=True), expected_revision=if_match))


@router.post("/search/backends", operation_id="createSearchBackend", response_model=DataEnvelope[SearchSettingsData], status_code=201)
def create_backend(body: SearchBackendCreate, request: Request, control: Control, context: WriteContext, if_match: Revision = None):
    return _response(request, control.add_backend(context, body.model_dump(exclude_unset=True), expected_revision=if_match))


@router.patch("/search/backends/{backendId}", operation_id="updateSearchBackend", response_model=DataEnvelope[SearchSettingsData])
def patch_backend(backend_id: Annotated[str, Path(alias="backendId")], body: SearchBackendPatch, request: Request, control: Control, context: WriteContext, if_match: Revision = None):
    # Secret fields additionally require management.secrets.write inside Control.
    return _response(request, control.patch_backend(context, backend_id, body.model_dump(exclude_unset=True), expected_revision=if_match))


@router.delete("/search/backends/{backendId}", operation_id="deleteSearchBackend", status_code=204)
def delete_backend(backend_id: Annotated[str, Path(alias="backendId")], request: Request, control: Control, context: WriteContext,
                   if_match: Annotated[str, Header(alias="If-Match", min_length=8, max_length=128)]):
    control.delete_backend(context, backend_id, expected_revision=if_match)
    return Response(status_code=204, headers={"X-Request-Id": management_request_id(request), "Cache-Control": "no-store"})


@router.get("/search/backends/{backendId}/accounts", operation_id="getSearchBackendAccounts", response_model=DataEnvelope[list[SearchAccountData]])
def get_accounts(backend_id: Annotated[str, Path(alias="backendId")], request: Request, control: Control, context: ReadContext):
    return DataEnvelope(data=[SearchAccountData(**v) for v in control.accounts(context, backend_id)], meta=response_meta(request))


@router.put("/search/priority", operation_id="updateSearchPriority", response_model=DataEnvelope[SearchSettingsData])
def priority(body: SearchPriorityRequest, request: Request, control: Control, context: WriteContext, if_match: Revision = None):
    return _response(request, control.priority(context, body.backendIds, expected_revision=if_match))


@router.post("/search/test", operation_id="testSearchBackend", response_model=DataEnvelope[ManagementOperationData], status_code=202)
def test_backend(body: SearchTestRequest, request: Request, control: Control, context: WriteContext):
    """Explicit single-source probe; poll the ordinary Management Operation.

    Idempotency-Key replays the same operation, never silently probes a fallback.
    Readiness/verified flags are not a live health check and are not changed here.
    """
    item = control.start_test(context, body.backendId, operation=body.operation, query=body.query, url=body.url)
    return DataEnvelope(data=operation_data(item), meta=response_meta(request))
