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


class OverviewTelegramBot(StrictSchema):
    configured: bool


class OverviewUpdate(StrictSchema):
    available: bool
    latestVersion: str | None = None


class OverviewData(StrictSchema):
    version: str
    uptimeSeconds: int = Field(ge=0)
    listeners: ListenerSummary
    counts: ResourceCounts
    today: dict[str, JsonValue]
    lifetime: dict[str, JsonValue]
    activeAlerts: dict[str, JsonValue]
    publicIp: str | None = None
    telegramBot: OverviewTelegramBot = OverviewTelegramBot(configured=False)
    update: OverviewUpdate = OverviewUpdate(available=False)
    revision: str
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{
        "version": "0.31.13", "uptimeSeconds": 42,
        "listeners": {"host": "127.0.0.1", "port": 22123},
        "counts": {"channels": 2, "oauthAccounts": 1, "apiKeys": 3, "quotaHot": 0},
        "today": {"total": 8}, "lifetime": {"total": 120},
        "activeAlerts": {}, "publicIp": "203.0.113.10",
        "telegramBot": {"configured": True},
        "update": {"available": False, "latestVersion": None},
        "revision": "rev_example",
    }]})


class ChannelCooldownData(StrictSchema):
    model: str
    errorCount: int = Field(ge=0)
    state: Literal["active", "permanent"]
    until: datetime | None = None
    quota: bool
    message: str | None = None


class WeakModelData(StrictSchema):
    model: str
    successRate: float = Field(ge=0, le=100)
    recentRequests: int = Field(ge=0)
    recentSuccessCount: int = Field(ge=0)


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
    # Models still in use whose recent success rate is below 80 %, lowest first.
    weakModels: list[WeakModelData] = Field(default_factory=list)


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
    # Round 3: TG-derived values (null when the denominator is 0).
    promptTokens: int = Field(default=0, ge=0)
    cacheReadRatio: float | None = None
    successRate: float | None = None
    cacheHitRate: float | None = None
    cacheWriteRate: float | None = None
    retriedRequestRate: float | None = None
    affinityHitRate: float | None = None


class StatsTopRowData(StrictSchema):
    key: str
    metrics: StatsMetricData


class StatsModelChannelShareData(StrictSchema):
    key: str
    type: str | None = None
    count: int = Field(ge=0)


class StatsTopModelRowData(StatsTopRowData):
    channels: list[StatsModelChannelShareData] = Field(default_factory=list)


class StatsFamilyTopsData(StrictSchema):
    channels: list[StatsTopRowData]
    models: list[StatsTopModelRowData]


class StatsTopApiKeyRowData(StatsTopRowData):
    familyTotals: dict[str, int] = Field(default_factory=dict)


class StatsSummaryData(StrictSchema):
    period: Literal["today", "3d", "7d", "month", "lifetime"]
    overall: StatsMetricData
    families: dict[str, StatsMetricData]
    familyTops: dict[str, StatsFamilyTopsData] = Field(default_factory=dict)
    topApiKeys: list[StatsTopApiKeyRowData] = Field(default_factory=list)
    revision: str
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{
        "period": "today", "overall": {"total": 10, "successCount": 9},
        "families": {}, "revision": "rev_example",
    }]})


class StatsBreakdownData(StrictSchema):
    key: str
    metrics: StatsMetricData
    # Round 3 (dimension=model): TG 「所属」 channels (top 5 by count).
    channels: list["StatsModelChannelShareData"] | None = None
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


class CacheMissSampleData(StrictSchema):
    """One TG「最近未命中样本」 row: a successful request without cache read."""
    id: str
    createdAt: datetime | None = None
    model: str | None = None
    apiKeyName: str | None = None
    channelId: str | None = None
    inputTokens: int
    cacheCreationTokens: int
    outputTokens: int
    messageCount: int
    toolCount: int


class RecentOutcomeData(StrictSchema):
    """Last finished outcomes (oldest first) of one channel / API key / model within 7 days."""
    key: str
    outcomes: list[Literal["success", "error", "cancelled"]]
    successCount: int
    errorCount: int
    lastAt: datetime | None = None


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


class RequestLogModelConflictData(StrictSchema):
    httpHeader: list[str]
    eventHeaders: list[str]
    bodyModels: list[str]


class RequestLogSafetyReviewData(StrictSchema):
    flagged: bool
    reason: str | None = None


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
    # ── Round 3: every fact of the Telegram list card (ui.fmt_log_entry_*) ──
    httpStatus: int | None = None
    channelName: str | None = None
    channelProvider: str | None = None
    channelType: str | None = None
    ingressProtocol: str | None = None
    upstreamProtocol: str | None = None
    websocket: Literal["ingress", "upstream"] | None = None
    isStream: bool = False
    cacheCreationTokens: int = Field(default=0, ge=0)
    cacheReadTokens: int = Field(default=0, ge=0)
    promptTokens: int = Field(default=0, ge=0)
    cacheReadRatio: float | None = None
    connectMilliseconds: float | None = None
    firstTokenMilliseconds: float | None = None
    idleMilliseconds: float | None = None
    tokensPerSecond: float | None = None
    reasoningEffort: str | None = None
    serviceTier: str | None = None
    speedMode: Literal["ultrafast", "fast", "tier"] | None = None
    affinityHit: bool = False
    proxyName: str | None = None
    proxyBytesUp: int | None = None
    proxyBytesDown: int | None = None
    localSearchCount: int = Field(default=0, ge=0)
    usageObserved: bool | None = None
    errorSummary: str | None = None
    upstreamActualModel: str | None = None
    modelSignalConflict: RequestLogModelConflictData | None = None
    safetyReview: RequestLogSafetyReviewData | None = None
    # ── detail only (getRequestLog): TG 「请求总览 / 客户端」 ──
    clientIp: str | None = None
    finalRoundId: str | None = None
    requestElapsedMilliseconds: float | None = None
    stageMilliseconds: dict[str, float | None] | None = None
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
    # Set when the bodies were removed by logBodyRetention (distinct from
    # "never stored"); metadata, attempts and billing remain available.
    bodyPurgedAt: datetime | None = None
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
    trafficBytes: int | None = Field(default=None, ge=0)
    generatedCount: int | None = Field(default=None, ge=0)
    outputSizes: list[str] | None = None
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


class RetentionCompactionData(StrictSchema):
    month: str
    action: Literal["compacted", "skipped", "not_needed", "failed"]
    compacted: bool
    reason: str
    beforeBytes: int = Field(ge=0)
    afterBytes: int = Field(ge=0)
    at: datetime | None = None


class RetentionBodyCleanupData(StrictSchema):
    """Background body-retention runner state (in-memory; resets on restart)."""

    running: bool = False
    automatic: bool = False
    month: str | None = None
    startedAt: datetime | None = None
    finishedAt: datetime | None = None
    purgedRequests: int = Field(default=0, ge=0)
    purgedMcpResults: int = Field(default=0, ge=0)
    purgedBytes: int = Field(default=0, ge=0)
    skippedActive: int = Field(default=0, ge=0)
    errors: list[str] = Field(default_factory=list)
    stopped: str = ""
    compaction: list[RetentionCompactionData] = Field(default_factory=list)


class RetentionSettingsData(StrictSchema):
    mode: Literal["forever", "days"]
    days: int | None = Field(default=None, ge=1)
    logStoreBodies: bool
    currentData: RetentionCurrentData
    busy: bool
    # Body-only retention: purges request/response bodies and MCP results;
    # request summaries, tokens, costs, retries and proxy metadata stay forever.
    bodyMode: Literal["forever", "days"] = "forever"
    bodyDays: int | None = Field(default=None, ge=1)
    bodyCleanup: RetentionBodyCleanupData = Field(default_factory=RetentionBodyCleanupData)
    revision: str
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{
        "mode": "forever", "days": None, "logStoreBodies": True,
        "currentData": {"rows": 100}, "busy": False,
        "bodyMode": "days", "bodyDays": 7,
        "bodyCleanup": {"running": False, "purgedRequests": 0, "compaction": []},
        "revision": "rev_example",
    }]})


class RetentionSettingsPatch(StrictSchema):
    mode: Literal["forever", "days"] | None = None
    days: int | None = Field(default=None, ge=1)
    logStoreBodies: bool | None = None
    # Enabling or shortening body retention requires a scope=bodies plan.
    bodyMode: Literal["forever", "days"] | None = None
    bodyDays: int | None = Field(default=None, ge=1)
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{
        "mode": "days", "days": 30, "logStoreBodies": True,
    }]})


class RetentionPlanCreate(StrictSchema):
    days: int = Field(ge=1)
    # requests = delete whole expired rows (logRetention); bodies = purge only
    # full request/response bodies and MCP results (logBodyRetention).
    scope: Literal["requests", "bodies"] = "requests"
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{"days": 30}]})


class RetentionPlanItemData(StrictSchema):
    month: str
    database: str
    action: Literal["delete_file", "trim_and_vacuum", "purge_bodies"]
    totalRows: int = Field(ge=0)
    deletedRows: int = Field(ge=0)
    deletedCallRows: int = Field(ge=0)
    currentBytes: int = Field(ge=0)
    releasableBytes: int | None = Field(default=None, ge=0)
    willCompact: bool
    compactionRequiredBytes: int | None = Field(default=None, ge=0)
    note: str | None = None
    active: bool


class RetentionPlanData(StrictSchema):
    planId: str
    state: Literal["prepared", "committed", "cancelled"]
    days: int = Field(ge=1)
    scope: Literal["requests", "bodies"] = "requests"
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
    items: list[RetentionPlanItemData] = Field(default_factory=list)
    revision: str
    operationId: str | None = None
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{
        "planId": "plan_example", "state": "prepared", "days": 30,
        "cutoff": "2026-01-02T03:04:05Z", "expiresAt": "2026-01-02T03:14:05Z",
        "affectedRows": 10, "affectedFiles": 1, "affectedBytes": 2048,
        "scannedRows": 100, "scannedFiles": 2, "scannedBytes": 4096,
        "preflightOk": True, "errors": [], "revision": "rev_example", "operationId": None,
    }]})


StatsBreakdownData.model_rebuild()
