"""Independent Management API MCP entry points (never inference auth)."""
from __future__ import annotations

from enum import Enum
from threading import RLock
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request, Response

from src.management_auth import Capability
from src.management_control import ManagementContext, ManagementErrorCode
from src.management_control.mcp import MCPControl

from ..dependencies import (
    ManagementRuntime,
    get_management_runtime,
    management_request_id,
    require_capability,
)
from ..error_mapping import management_error_responses
from ..response_helpers import response_meta
from ..schemas.base import DataEnvelope
from ..schemas.mcp import (
    McpCallLogData,
    McpLogListData,
    McpSettingsData,
    McpSettingsPatch,
    McpStatsData,
    McpToolStatData,
    McpToolSwitchRequest,
)
from .auxiliary_support import reject_unknown_query


class McpLogPeriod(str, Enum):
    TODAY = "today"
    MONTH = "month"


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


_NO_QUERY = [Depends(reject_unknown_query()), Depends(_no_store)]
router = APIRouter(tags=["management-mcp"], responses=management_error_responses(
    ManagementErrorCode.SESSION_REQUIRED, ManagementErrorCode.SESSION_EXPIRED,
    ManagementErrorCode.ORIGIN_DENIED, ManagementErrorCode.CAPABILITY_DENIED,
    ManagementErrorCode.RESOURCE_NOT_FOUND, ManagementErrorCode.RESOURCE_CONFLICT,
    ManagementErrorCode.REVISION_CONFLICT, ManagementErrorCode.STATE_CONFLICT,
    ManagementErrorCode.CONFIRMATION_REQUIRED,
    ManagementErrorCode.VALIDATION_FAILED, ManagementErrorCode.SERVICE_NOT_READY,
    ManagementErrorCode.DEPENDENCY_UNAVAILABLE, ManagementErrorCode.OPERATION_ALREADY_RUNNING,
))


_control_lock = RLock()


def get_mcp_control(request: Request, runtime: Annotated[ManagementRuntime, Depends(get_management_runtime)]) -> MCPControl:
    # Concurrent first probes must share the same idempotency ledger/owner.
    with _control_lock:
        current = getattr(request.app.state, "management_mcp_control", None)
        owner = getattr(request.app.state, "management_mcp_runtime", None)
        if isinstance(current, MCPControl) and (owner is None or owner is runtime):
            return current
        current = MCPControl(audit_sink=runtime.audit_sink, operations=runtime.operations)
        request.app.state.management_mcp_control = current
        request.app.state.management_mcp_runtime = runtime
        return current


Control = Annotated[MCPControl, Depends(get_mcp_control)]
ReadContext = Annotated[ManagementContext, Depends(require_capability(Capability.READ))]
WriteContext = Annotated[ManagementContext, Depends(require_capability(Capability.WRITE))]
Revision = Annotated[str | None, Header(alias="If-Match")]


def _response(request, value):
    return DataEnvelope(data=McpSettingsData(**value), meta=response_meta(request))


@router.get("/mcp", operation_id="getMcpSettings", dependencies=_NO_QUERY,
            response_model=DataEnvelope[McpSettingsData])
def get_mcp(request: Request, control: Control, context: ReadContext):
    return _response(request, control.get(context))


@router.patch("/mcp", operation_id="updateMcpSettings", dependencies=_NO_QUERY,
              response_model=DataEnvelope[McpSettingsData])
def patch_mcp(body: McpSettingsPatch, request: Request, control: Control,
              context: WriteContext, if_match: Revision = None):
    values = body.model_dump(exclude_unset=True)
    return _response(request, control.patch(context, values, expected_revision=if_match))


@router.put("/mcp/tools", operation_id="updateMcpTool", dependencies=_NO_QUERY,
            response_model=DataEnvelope[McpSettingsData])
def put_mcp_tool(body: McpToolSwitchRequest, request: Request, control: Control,
                 context: WriteContext, if_match: Revision = None):
    """Toggle one tool. Kept separate so a single switch is an atomic operation."""
    return _response(request, control.set_tool(
        context, body.toolName, body.enabled, expected_revision=if_match))


@router.get(
    "/mcp/logs", operation_id="listMcpLogs",
    dependencies=[Depends(reject_unknown_query(
        "period", "apiKeyName", "toolName", "page", "pageSize")), Depends(_no_store)],
    response_model=DataEnvelope[McpLogListData],
)
def list_mcp_logs(
    request: Request,
    control: Control,
    context: ReadContext,
    period: McpLogPeriod = McpLogPeriod.TODAY,
    apiKeyName: str | None = None,
    toolName: str | None = None,
    page: int = Query(1, ge=1, le=10_000),
    pageSize: int = Query(50, ge=1, le=200),
):
    """MCP tool calls, including denials that never reached an upstream."""
    value = control.logs(context, period=period.value, api_key_name=apiKeyName,
                         tool_name=toolName, page=page, page_size=pageSize)
    return DataEnvelope(data=McpLogListData(**value), meta=response_meta(request))


@router.get(
    "/mcp/stats", operation_id="getMcpStats",
    dependencies=[Depends(reject_unknown_query("period")), Depends(_no_store)],
    response_model=DataEnvelope[McpStatsData],
)
def get_mcp_stats(
    request: Request,
    control: Control,
    context: ReadContext,
    period: McpLogPeriod = McpLogPeriod.MONTH,
):
    """Per-tool call counts, outcomes and average latency."""
    value = control.stats(context, period=period.value)
    return DataEnvelope(data=McpStatsData(**value), meta=response_meta(request))
