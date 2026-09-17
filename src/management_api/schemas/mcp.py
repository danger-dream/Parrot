"""Strict MCP-management DTOs. No credential field exists in this domain."""
from __future__ import annotations

from typing import Literal

from pydantic import Field

from .base import StrictSchema
from .system import StrictRequestSchema

McpToolName = Literal[
    "web_search", "web_fetch",
    "image_generate", "image_edit",
    "video_generate", "video_status",
]


class McpToolSwitches(StrictRequestSchema):
    web_search: bool | None = None
    web_fetch: bool | None = None
    image_generate: bool | None = None
    image_edit: bool | None = None
    video_generate: bool | None = None
    video_status: bool | None = None


class McpSettingsPatch(StrictRequestSchema):
    enabled: bool | None = None
    mediaTtlSeconds: int | None = Field(
        None, ge=60, le=7 * 24 * 3600,
        description="媒体资源 URL 的有效期（秒）。",
    )
    maxRequestBodyBytes: int | None = Field(
        None, ge=1024 * 1024, le=256 * 1024 * 1024,
        description="MCP 端点请求体上限；图片编辑需传 data URL，故默认远高于 SDK 的 4 MiB。",
    )
    tools: McpToolSwitches | None = None


class McpToolSwitchRequest(StrictRequestSchema):
    toolName: McpToolName
    enabled: bool


class McpRuntimeData(StrictSchema):
    mounted: bool = Field(description="MCP 应用是否成功装配；失败不影响主服务")
    path: str
    mediaPath: str
    reason: str | None = None
    enabled: bool


class McpSettingsData(StrictSchema):
    enabled: bool
    mediaTtlSeconds: int
    maxRequestBodyBytes: int
    tools: dict[str, bool]
    revision: str
    runtime: McpRuntimeData


class McpCallLogData(StrictSchema):
    """One MCP tool call (dedicated MCP log).

    包含没有产生任何上游调用的调用（工具被禁用、Key 无权限、参数非法），
    这是它独立于 search_call_log / image_call_logs 的原因。
    """

    id: int
    callId: str
    createdAt: float
    apiKeyName: str | None = None
    clientName: str | None = None
    clientVersion: str | None = None
    protocolVersion: str | None = None
    toolName: str
    toolLabel: str
    params: dict | list | str | int | float | bool | None = None
    status: Literal["running", "success", "error", "timeout", "denied"]
    errorCode: str | None = None
    errorMessage: str | None = None
    elapsedMs: int | None = None
    sourceId: str | None = None
    sourceType: str | None = None
    model: str | None = None
    provider: str | None = None
    accountKey: str | None = None
    resultCount: int
    resultBytes: int
    mediaTokens: list[str]
    videoRequestId: str | None = None
    inputTokens: int
    outputTokens: int


class McpLogListData(StrictSchema):
    items: list[McpCallLogData]
    page: int
    pageSize: int
    period: str
    hasNext: bool


class McpToolStatData(StrictSchema):
    toolName: str
    label: str
    calls: int
    success: int
    failed: int
    avgElapsedMs: int
    lastAt: float


class McpStatsData(StrictSchema):
    period: str
    items: list[McpToolStatData]
