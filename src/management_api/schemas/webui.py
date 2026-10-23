"""Schemas added for the browser WebUI: time series and live event payloads."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import ConfigDict, Field

from .base import StrictSchema


class TimeseriesPointData(StrictSchema):
    t: datetime
    total: int = Field(ge=0)
    successCount: int = Field(ge=0)
    errorCount: int = Field(ge=0)
    inputTokens: int = Field(ge=0)
    outputTokens: int = Field(ge=0)
    cacheReadTokens: int = Field(ge=0)
    cacheCreationTokens: int = Field(ge=0)
    costTicks: int = Field(ge=0)
    averageFirstTokenMilliseconds: float | None = None
    averageTotalMilliseconds: float | None = None


class TimeseriesSeriesData(StrictSchema):
    key: str
    label: str
    points: list[TimeseriesPointData]


class StatsTimeseriesData(StrictSchema):
    range: Literal["1h", "24h", "7d", "30d"]
    bucket: Literal["minute", "10m", "hour", "day"]
    bucketSeconds: int = Field(ge=60)
    from_: datetime = Field(alias="from")
    to: datetime
    timezone: str
    series: list[TimeseriesSeriesData]
    revision: str
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        serialize_by_alias=True,
        json_schema_extra={"examples": [{
            "range": "24h", "bucket": "hour", "bucketSeconds": 3600,
            "from": "2026-01-01T04:00:00Z", "to": "2026-01-02T04:00:00Z",
            "timezone": "Asia/Shanghai",
            "series": [{
                "key": "total", "label": "全部",
                "points": [{
                    "t": "2026-01-01T04:00:00Z", "total": 3, "successCount": 3,
                    "errorCount": 0, "inputTokens": 120, "outputTokens": 80,
                    "cacheReadTokens": 0, "cacheCreationTokens": 0,
                    "costTicks": 1500000, "averageFirstTokenMilliseconds": 420.0,
                    "averageTotalMilliseconds": 1800.0,
                }],
            }],
            "revision": "rev_example",
        }]},
    )


class CompareMetricsData(StrictSchema):
    total: int = Field(ge=0)
    successCount: int = Field(ge=0)
    errorCount: int = Field(ge=0)
    inputTokens: int = Field(ge=0)
    outputTokens: int = Field(ge=0)
    cacheReadTokens: int = Field(ge=0)
    cacheCreationTokens: int = Field(ge=0)
    costTicks: int = Field(ge=0)
    unpricedSuccess: int = Field(ge=0)
    cacheHitRequests: int = Field(ge=0)
    # Client-cancelled requests; with success and error they leave only in-flight ones out of total.
    cancelledCount: int = Field(default=0, ge=0)
    affinityHits: int = Field(ge=0)
    averageFirstTokenMilliseconds: float | None = None
    averageTotalMilliseconds: float | None = None
    averageTokensPerSecond: float | None = None


class ComparePointData(CompareMetricsData):
    t: datetime


_COMPARE_METRICS_EXAMPLE = {
    "total": 461, "successCount": 445, "errorCount": 16, "inputTokens": 21000000,
    "outputTokens": 319000, "cacheReadTokens": 19000000, "cacheCreationTokens": 0,
    "costTicks": 1500000, "unpricedSuccess": 0, "cacheHitRequests": 400, "affinityHits": 300,
    "averageFirstTokenMilliseconds": 420.0, "averageTotalMilliseconds": 1800.0, "averageTokensPerSecond": 75.7,
}


class OutcomeBucketData(StrictSchema):
    total: int = Field(ge=0)
    errorCount: int = Field(ge=0)
    cancelledCount: int = Field(ge=0)


class OutcomeTimelineItemData(StrictSchema):
    key: str
    # Oldest first, one per bucket of the timeline (empty buckets included).
    buckets: list[OutcomeBucketData]
    total: int = Field(ge=0)
    errorCount: int = Field(ge=0)
    cancelledCount: int = Field(ge=0)
    lastAt: datetime | None = None


class OutcomeTimelineData(StrictSchema):
    """Finished requests per channel / API key / model in fixed buckets; the last bucket is the current one."""
    from_: datetime = Field(alias="from")
    bucketSeconds: int = Field(ge=60)
    items: list[OutcomeTimelineItemData]


class StatsCompareData(StrictSchema):
    """This window, the window before it covering the same elapsed time, and this window's sparkline."""
    from_: datetime = Field(alias="from")
    to: datetime
    previousFrom: datetime
    previousTo: datetime
    # False when the logs do not reach back to previousFrom (retention or a new install).
    previousComplete: bool
    bucketSeconds: int = Field(ge=60)
    current: CompareMetricsData
    previous: CompareMetricsData
    points: list[ComparePointData]
    revision: str
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        serialize_by_alias=True,
        json_schema_extra={"examples": [{
            "from": "2026-01-01T16:00:00Z", "to": "2026-01-02T07:00:00Z",
            "previousFrom": "2026-12-31T16:00:00Z", "previousTo": "2026-01-01T07:00:00Z",
            "previousComplete": True, "bucketSeconds": 3600,
            "current": _COMPARE_METRICS_EXAMPLE,
            "previous": {**_COMPARE_METRICS_EXAMPLE, "total": 49},
            "points": [{"t": "2026-01-01T16:00:00Z", **_COMPARE_METRICS_EXAMPLE}],
            "revision": "rev_example",
        }]},
    )


class LiveRequestStartedData(StrictSchema):
    id: str
    createdAt: datetime | None = None
    apiKeyName: str | None = None
    requestedModel: str | None = None
    protocol: str | None = None
    isStream: bool
    clientIp: str | None = None
    reasoningEffort: str | None = None
    fastMode: bool | None = None


class LiveRequestUpdatedData(StrictSchema):
    id: str
    channelId: str | None = None
    model: str | None = None
    retryCount: int | None = Field(default=None, ge=0)
    # Round 3: outbound proxy selected for this attempt (TG 出站代理).
    proxyName: str | None = None


class LiveHelloData(StrictSchema):
    serverTime: datetime
    version: str
    sessionExpiresAt: datetime
