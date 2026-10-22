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
    bucket: Literal["minute", "hour", "day"]
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


class LiveHelloData(StrictSchema):
    serverTime: datetime
    version: str
    sessionExpiresAt: datetime
