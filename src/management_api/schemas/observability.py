"""Typed Management API v1 schemas for P4 observability resources."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Annotated, Generic, Literal, TypeVar

from pydantic import BeforeValidator, ConfigDict, Field, JsonValue

from .base import ResponseMeta, StrictSchema


class PagedResponseMeta(ResponseMeta):
    page: int = Field(ge=1)
    pageSize: int = Field(ge=1, le=200)
    total: int = Field(ge=0)
    hasNext: bool


PageT = TypeVar("PageT")


_RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})$",
)


def _rfc3339_utc(value):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and _RFC3339_RE.fullmatch(value):
        text = value[:-1] + "+00:00" if value[-1:] in {"Z", "z"} else value
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError("invalid RFC 3339 timestamp") from exc
    else:
        raise ValueError("RFC 3339 timestamp with timezone offset is required")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("RFC 3339 timezone offset is required")
    return parsed.astimezone(timezone.utc)


Rfc3339UtcDateTime = Annotated[datetime, BeforeValidator(_rfc3339_utc)]


class PagedEnvelope(StrictSchema, Generic[PageT]):
    data: list[PageT]
    meta: PagedResponseMeta


class RevisionedResponseMeta(ResponseMeta):
    revision: str


class RevisionedPagedResponseMeta(PagedResponseMeta):
    revision: str


class RevisionedCollectionEnvelope(StrictSchema, Generic[PageT]):
    data: list[PageT]
    meta: RevisionedResponseMeta


class RevisionedPagedEnvelope(StrictSchema, Generic[PageT]):
    data: list[PageT]
    meta: RevisionedPagedResponseMeta


class ListenerSummary(StrictSchema):
    host: str
    port: int = Field(ge=0, le=65535)


class ResourceCounts(StrictSchema):
    channels: int = Field(ge=0)
    oauthAccounts: int = Field(ge=0)
    apiKeys: int = Field(ge=0)
    quotaHot: int = Field(ge=0)


class OverviewData(StrictSchema):
    version: str
    uptimeSeconds: int = Field(ge=0)
    listeners: ListenerSummary
    counts: ResourceCounts
    today: dict[str, JsonValue]
    lifetime: dict[str, JsonValue]
    activeAlerts: dict[str, JsonValue]
    revision: str
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{
        "version": "0.31.13", "uptimeSeconds": 42,
        "listeners": {"host": "127.0.0.1", "port": 22123},
        "counts": {"channels": 2, "oauthAccounts": 1, "apiKeys": 3, "quotaHot": 0},
        "today": {"total": 8}, "lifetime": {"total": 120},
        "activeAlerts": {}, "revision": "rev_example",
    }]})


class ChannelCooldownData(StrictSchema):
    model: str
    errorCount: int = Field(ge=0)
    state: Literal["active", "permanent"]
    until: datetime | None = None
    quota: bool
    message: str | None = None


class ChannelStatusData(StrictSchema):
    id: str
    name: str
    protocol: str
    type: str
    enabled: bool
    disabledReason: str | None = None
    health: Literal[
        "disabled", "permanentCooldown", "quotaCooldown", "cooldown",
        "healthy", "degraded", "unhealthy", "unknown",
    ]
    recentSuccessRate: float | None = Field(default=None, ge=0, le=100)
    cooldownCount: int = Field(ge=0)
    permanentCooldownCount: int = Field(ge=0)
    cooldowns: list[ChannelCooldownData]
    problemReasons: list[str]


class FastestChannelData(StrictSchema):
    channelId: str
    model: str
    successRate: float = Field(ge=0, le=1)
    score: float
    averageFirstByteMilliseconds: float | None = None


class QuotaMetricData(StrictSchema):
    window: str
    utilizationPercent: float = Field(ge=0)


class QuotaWarningData(StrictSchema):
    accountId: str
    provider: str
    metrics: list[QuotaMetricData]


class ChannelLimiterTotalsData(StrictSchema):
    inFlight: int = Field(default=0, ge=0)
    waiting: int = Field(default=0, ge=0)
    trackedChannels: int = Field(default=0, ge=0)


class ApiKeyLimiterTotalsData(StrictSchema):
    inFlight: int = Field(default=0, ge=0)
    waiting: int = Field(default=0, ge=0)
    trackedKeys: int = Field(default=0, ge=0)


class ChannelConcurrencyRowData(StrictSchema):
    channelKey: str
    inFlight: int = Field(ge=0)
    maxConcurrent: int = Field(ge=0)
    waiting: int = Field(ge=0)
    unlimited: bool


class ApiKeyConcurrencyRowData(StrictSchema):
    keyName: str
    enabled: bool
    inFlight: int = Field(ge=0)
    maxConcurrent: int = Field(ge=0)
    maxQueue: int = Field(ge=0)
    queueWaitSeconds: int = Field(ge=0)
    waiting: int = Field(ge=0)
    oldestWaitSeconds: int = Field(ge=0)
    unlimited: bool
    enabledSource: str
    maxConcurrentSource: str
    maxQueueSource: str
    queueWaitSource: str


class ConcurrencyData(StrictSchema):
    channelTotals: ChannelLimiterTotalsData
    channels: list[ChannelConcurrencyRowData]
    apiKeyTotals: ApiKeyLimiterTotalsData
    apiKeys: list[ApiKeyConcurrencyRowData]
    revision: str


class RuntimeStatusData(StrictSchema):
    channels: list[ChannelStatusData]
    problemChannels: list[ChannelStatusData]
    fastestByFamily: dict[str, list[FastestChannelData]]
    quotaWarnings: list[QuotaWarningData]
    concurrency: ConcurrencyData
    cooldownSummary: dict[str, int]
    affinitySummary: dict[str, int]
    database: dict[str, JsonValue]
    revision: str
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{
        "channels": [], "problemChannels": [],
        "fastestByFamily": {"anthropic": [], "openai": []}, "quotaWarnings": [],
        "concurrency": {
            "channelTotals": {}, "channels": [], "apiKeyTotals": {}, "apiKeys": [],
            "revision": "rev_concurrency",
        },
        "cooldownSummary": {"active": 0, "permanent": 0},
        "affinitySummary": {"server": 0, "client": 0},
        "database": {"status": "healthy"}, "revision": "rev_example",
    }]})


class BackgroundJobData(StrictSchema):
    id: str
    intervalSeconds: int | None = Field(default=None, ge=1)
    lastRunAt: datetime | None = None
    nextRunAt: datetime | None = None
    status: Literal["running", "succeeded", "failed", "unknown", "disabled"]
    error: str | None = None
    revision: str
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{
        "id": "walCheckpoint", "intervalSeconds": None, "lastRunAt": None,
        "nextRunAt": None, "status": "unknown", "error": None,
        "revision": "rev_example",
    }]})


class CooldownData(StrictSchema):
    channelId: str
    model: str
    errorCount: int = Field(ge=0)
    state: Literal["active", "permanent"]
    until: datetime | None = None
    message: str | None = None
    revision: str
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{
        "channelId": "api:example", "model": "example-model", "errorCount": 2,
        "state": "active", "until": "2026-01-02T03:04:05Z", "message": None,
        "revision": "rev_example",
    }]})


class StatsMetricData(StrictSchema):
    total: int = Field(default=0, ge=0)
    successCount: int = Field(default=0, ge=0)
    errorCount: int = Field(default=0, ge=0)
    pendingCount: int = Field(default=0, ge=0)
    inputTokens: int = Field(default=0, ge=0)
    outputTokens: int = Field(default=0, ge=0)
    cacheCreationTokens: int = Field(default=0, ge=0)
    cacheReadTokens: int = Field(default=0, ge=0)
    cacheHitRequests: int = Field(default=0, ge=0)
    cacheWriteRequests: int = Field(default=0, ge=0)
    totalRetries: int = Field(default=0, ge=0)
    retriedRequests: int = Field(default=0, ge=0)
    affinityHits: int = Field(default=0, ge=0)
    costTicks: int = Field(default=0, ge=0)
    actualCostTicks: int = Field(default=0, ge=0)
    estimatedCostTicks: int = Field(default=0, ge=0)
    actualCostedSuccess: int = Field(default=0, ge=0)
    estimatedCostedSuccess: int = Field(default=0, ge=0)
    costedSuccess: int = Field(default=0, ge=0)
    unpricedSuccess: int = Field(default=0, ge=0)
    averageConnectMilliseconds: float | None = Field(default=None, ge=0)
    averageFirstTokenMilliseconds: float | None = Field(default=None, ge=0)
    averageTotalMilliseconds: float | None = Field(default=None, ge=0)
    averageTokensPerSecond: float | None = Field(default=None, ge=0)
    maximumTokensPerSecond: float | None = Field(default=None, ge=0)
    minimumTokensPerSecond: float | None = Field(default=None, ge=0)
    serviceTierCounts: dict[str, int] = Field(default_factory=dict)


class StatsSummaryData(StrictSchema):
    period: Literal["today", "3d", "7d", "month", "lifetime"]
    overall: StatsMetricData
    families: dict[str, StatsMetricData]
    revision: str
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{
        "period": "today", "overall": {"total": 10, "successCount": 9},
        "families": {}, "revision": "rev_example",
    }]})


class StatsBreakdownData(StrictSchema):
    key: str
    metrics: StatsMetricData
    revision: str
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{
        "key": "example", "metrics": {"total": 10, "successCount": 9},
        "revision": "rev_example",
    }]})


class ModelStatsChannelData(StrictSchema):
    key: str
    count: int = Field(ge=0)
    type: str | None = None
    upstreamProtocol: str | None = None
    model_config = ConfigDict(extra="forbid")


class ModelStatsData(StrictSchema):
    modelId: str
    period: Literal["today", "3d", "7d", "month", "lifetime"]
    metrics: StatsMetricData
    channels: list[ModelStatsChannelData]
    revision: str
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{
        "modelId": "example-model", "period": "7d", "metrics": {"total": 4},
        "channels": [], "revision": "rev_example",
    }]})


class RecentCallData(StrictSchema):
    id: str
    status: str
    createdAt: datetime | None = None
    model: str | None = None
    channelId: str | None = None
    durationMilliseconds: float | None = None
    revision: str
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{
        "id": "request-example", "status": "success", "createdAt": "2026-01-02T03:04:05Z",
        "model": "example-model", "channelId": "api:example", "durationMilliseconds": 1200,
        "revision": "rev_example",
    }]})


class TelegramStatsPreferencesData(StrictSchema):
    byChannel: bool
    byModel: bool
    byApiKey: bool
    cacheMisses: bool
    recentCalls: bool
    revision: str
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{
        "byChannel": True, "byModel": True, "byApiKey": True,
        "cacheMisses": True, "recentCalls": True, "revision": "rev_example",
    }]})


class TelegramStatsPreferencesPatch(StrictSchema):
    byChannel: bool | None = None
    byModel: bool | None = None
    byApiKey: bool | None = None
    cacheMisses: bool | None = None
    recentCalls: bool | None = None
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{"byChannel": False}]})


class RequestLogBillingData(StrictSchema):
    costTicks: int = Field(ge=0)
    actualCostTicks: int = Field(ge=0)
    estimatedCostTicks: int = Field(ge=0)
    actualCostedSuccess: int = Field(ge=0)
    estimatedCostedSuccess: int = Field(ge=0)
    costedSuccess: int = Field(ge=0)
    unpricedSuccess: int = Field(ge=0)


class RequestLogData(StrictSchema):
    id: str
    status: str
    createdAt: datetime | None = None
    apiKeyName: str | None = None
    requestedModel: str | None = None
    finalModel: str | None = None
    channelId: str | None = None
    protocol: str | None = None
    transport: str | None = None
    retryCount: int = Field(ge=0)
    durationMilliseconds: float | None = None
    inputTokens: int = Field(ge=0)
    outputTokens: int = Field(ge=0)
    costTicks: int = Field(ge=0)
    billing: RequestLogBillingData
    error: str | None = None
    revision: str
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{
        "id": "request-example", "status": "success", "createdAt": "2026-01-02T03:04:05Z",
        "apiKeyName": "client", "requestedModel": "example", "finalModel": "example",
        "channelId": "api:example", "protocol": "anthropic", "transport": "http",
        "retryCount": 0, "durationMilliseconds": 850, "inputTokens": 20,
        "outputTokens": 30, "costTicks": 100,
        "billing": {
            "costTicks": 100, "actualCostTicks": 100, "estimatedCostTicks": 0,
            "actualCostedSuccess": 1, "estimatedCostedSuccess": 0,
            "costedSuccess": 1, "unpricedSuccess": 0,
        },
        "error": None, "revision": "rev_example",
    }]})


class FilterOptionData(StrictSchema):
    value: str
    count: int = Field(ge=0)


class RequestLogFilterOptionsData(StrictSchema):
    apiKeys: list[FilterOptionData]
    models: list[FilterOptionData]
    channels: list[FilterOptionData]
    statuses: list[FilterOptionData]
    protocols: list[FilterOptionData]
    revision: str
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{
        "apiKeys": [], "models": [], "channels": [],
        "statuses": [{"value": "success", "count": 3}], "protocols": [],
        "revision": "rev_example",
    }]})


class RequestLogDetailData(StrictSchema):
    id: str
    log: RequestLogData
    stages: list[dict[str, JsonValue]]
    attempts: list[dict[str, JsonValue]]
    localWebRounds: list[dict[str, JsonValue]]
    billingAttempts: list[dict[str, JsonValue]]
    requestBodyAvailable: bool
    responseBodyAvailable: bool
    requestHeadersAvailable: bool
    revision: str
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{
        "id": "request-example", "log": RequestLogData.model_json_schema().get("examples", [{}])[0],
        "stages": [], "attempts": [], "localWebRounds": [], "billingAttempts": [],
        "requestBodyAvailable": True, "responseBodyAvailable": True,
        "requestHeadersAvailable": True, "revision": "rev_example",
    }]})


class LogBodyKindCountData(StrictSchema):
    kind: str
    count: int = Field(ge=0)


class LogBodyPagedResponseMeta(PagedResponseMeta):
    revision: str
    kindCounts: list[LogBodyKindCountData] = Field(
        description="Counts after search and before itemKind filtering and page slicing",
    )


class LogBodyItemData(StrictSchema):
    revision: str
    id: str | None = None
    seq: int = Field(ge=1)
    kind: str
    title: str
    summary: str
    text: str
    raw: str
    size: int = Field(ge=0)
    meta: dict[str, JsonValue]
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{
        "id": "item_1", "seq": 1, "kind": "user", "title": "user",
        "summary": "input", "text": "hello", "raw": "hello", "size": 5, "meta": {},
        "revision": "rev_example",
    }]})


class LogBodyPagedEnvelope(StrictSchema):
    data: list[LogBodyItemData]
    meta: LogBodyPagedResponseMeta


class RawLogBodyData(StrictSchema):
    revision: str
    logId: str
    kind: Literal["request", "response"]
    body: JsonValue
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{
        "logId": "request-example", "kind": "request", "body": {"model": "example"},
        "revision": "rev_example",
    }]})


class MediaLogData(StrictSchema):
    id: str
    requestId: str
    status: Literal["running", "pending", "success", "failed", "expired", "cancelled"]
    provider: str
    model: str
    action: Literal["generate", "edit", "extend"]
    mediaType: str
    progress: float | None = None
    aspectRatio: str | None = None
    resolution: str | None = None
    durationSeconds: float | None = None
    durationMilliseconds: float | None = None
    costTicks: int = Field(ge=0)
    trafficBytes: int = Field(ge=0)
    cacheStatus: Literal["disabled", "cached", "failed"] | None = None
    cacheErrorClass: str | None = None
    createdAt: datetime | None = None
    finishedAt: datetime | None = None
    error: str | None = None
    revision: str
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{
        "id": "1", "requestId": "request-example", "status": "success", "provider": "openai",
        "model": "gpt-image", "action": "generate", "mediaType": "image", "progress": 100,
        "aspectRatio": "1:1", "resolution": "1024x1024", "durationSeconds": None,
        "durationMilliseconds": 1200, "costTicks": 100, "trafficBytes": 2048,
        "cacheStatus": "cached", "cacheErrorClass": None,
        "createdAt": "2026-01-02T03:04:05Z", "finishedAt": "2026-01-02T03:04:06Z",
        "error": None, "revision": "rev_example",
    }]})


class MediaLogDetailData(MediaLogData):
    accountId: str | None = None
    accountLabel: str | None = None
    upstreamRequestId: str | None = None
    upstreamStatus: str | None = None
    httpStatus: int | None = None
    promptPreview: str | None = None
    artifactCount: int = Field(ge=0)
    paths: list[str]


class MediaArtifactData(StrictSchema):
    id: str
    fileName: str
    contentType: str
    sizeBytes: int = Field(ge=0)
    mediaType: Literal["image", "video"]
    expiresAt: datetime | None = None
    revision: str
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{
        "id": "artifact_1_example", "fileName": "image.png", "contentType": "image/png",
        "sizeBytes": 2048, "mediaType": "image", "expiresAt": None,
        "revision": "rev_example",
    }]})


class RetentionCurrentData(StrictSchema):
    rows: int = Field(ge=0)


class RetentionSettingsData(StrictSchema):
    mode: Literal["forever", "days"]
    days: int | None = Field(default=None, ge=1)
    logStoreBodies: bool
    currentData: RetentionCurrentData
    busy: bool
    revision: str
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{
        "mode": "days", "days": 30, "logStoreBodies": True,
        "currentData": {"rows": 100}, "busy": False, "revision": "rev_example",
    }]})


class RetentionSettingsPatch(StrictSchema):
    mode: Literal["forever", "days"] | None = None
    days: int | None = Field(default=None, ge=1)
    logStoreBodies: bool | None = None
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{
        "mode": "days", "days": 30, "logStoreBodies": True,
    }]})


class RetentionPlanCreate(StrictSchema):
    days: int = Field(ge=1)
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{"days": 30}]})


class RetentionPlanData(StrictSchema):
    planId: str
    state: Literal["prepared", "committed", "cancelled"]
    days: int = Field(ge=1)
    cutoff: datetime
    expiresAt: datetime
    affectedRows: int = Field(ge=0)
    affectedFiles: int = Field(ge=0)
    affectedBytes: int = Field(ge=0)
    scannedRows: int = Field(ge=0)
    scannedFiles: int = Field(ge=0)
    scannedBytes: int = Field(ge=0)
    preflightOk: bool
    errors: list[str]
    revision: str
    operationId: str | None = None
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{
        "planId": "plan_example", "state": "prepared", "days": 30,
        "cutoff": "2026-01-02T03:04:05Z", "expiresAt": "2026-01-02T03:14:05Z",
        "affectedRows": 10, "affectedFiles": 1, "affectedBytes": 2048,
        "scannedRows": 100, "scannedFiles": 2, "scannedBytes": 4096,
        "preflightOk": True, "errors": [], "revision": "rev_example", "operationId": None,
    }]})
