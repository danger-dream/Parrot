"""Bucketed request statistics for charts (``GET /stats/timeseries``)."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from src.management_control.errors import ErrorField, ManagementError, ManagementErrorCode

from ._log_errors import map_historical_log_errors
from .common import revision_for


# Same fixed UTC+8 zone the existing today/month statistics use (stats._BJT).
_BJT = timezone(timedelta(hours=8))
TIMESERIES_TIMEZONE = "Asia/Shanghai"
MAX_BUCKETS = 800
TOTAL_KEY = "total"
OTHER_KEY = "__other__"
TOTAL_LABEL = "全部"
OTHER_LABEL = "其他"


class TimeseriesRange(str, Enum):
    ONE_HOUR = "1h"
    ONE_DAY = "24h"
    SEVEN_DAYS = "7d"
    THIRTY_DAYS = "30d"


class TimeseriesBucket(str, Enum):
    MINUTE = "minute"
    TEN_MINUTES = "10m"
    HOUR = "hour"
    DAY = "day"


class TimeseriesGroup(str, Enum):
    NONE = "none"
    MODEL = "model"
    CHANNEL = "channel"
    API_KEY = "apiKey"


RANGE_SECONDS = {
    TimeseriesRange.ONE_HOUR: 3600,
    TimeseriesRange.ONE_DAY: 86400,
    TimeseriesRange.SEVEN_DAYS: 7 * 86400,
    TimeseriesRange.THIRTY_DAYS: 30 * 86400,
}
BUCKET_SECONDS = {
    TimeseriesBucket.MINUTE: 60,
    TimeseriesBucket.TEN_MINUTES: 600,
    TimeseriesBucket.HOUR: 3600,
    TimeseriesBucket.DAY: 86400,
}
DEFAULT_BUCKET = {
    TimeseriesRange.ONE_HOUR: TimeseriesBucket.MINUTE,
    TimeseriesRange.ONE_DAY: TimeseriesBucket.HOUR,
    TimeseriesRange.SEVEN_DAYS: TimeseriesBucket.HOUR,
    TimeseriesRange.THIRTY_DAYS: TimeseriesBucket.DAY,
}
_SOURCE_GROUP = {
    TimeseriesGroup.NONE: None,
    TimeseriesGroup.MODEL: "model",
    TimeseriesGroup.CHANNEL: "channel",
    TimeseriesGroup.API_KEY: "apikey",
}
_ADDITIVE = (
    "total", "success_count", "error_count", "input_tokens", "output_tokens",
    "cache_read_tokens", "cache_creation_tokens", "cost_ticks", "unpriced_success", "hit_requests",
    "affinity_hits", "tps_num_tokens", "tps_denom_ms",
    "sum_first_token_ms", "cnt_first_token", "sum_total_ms", "cnt_total",
)


def window(
    now: float, range_value: TimeseriesRange, bucket: TimeseriesBucket,
) -> tuple[float, float, int, int]:
    """Return ``(start, end, bucket_seconds, count)``; the last bucket holds ``now``."""
    bucket_seconds = BUCKET_SECONDS[bucket]
    if bucket is TimeseriesBucket.DAY:
        current = datetime.fromtimestamp(now, tz=_BJT).replace(
            hour=0, minute=0, second=0, microsecond=0,
        ).timestamp()
    else:
        # UTC+8 is a whole-hour offset, so UTC minute/hour boundaries coincide.
        current = math.floor(now / bucket_seconds) * bucket_seconds
    count = math.ceil(RANGE_SECONDS[range_value] / bucket_seconds)
    end = float(current + bucket_seconds)
    return end - count * bucket_seconds, end, bucket_seconds, count


def _utc(timestamp: float) -> datetime:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc)


def _point(timestamp: float, raw: dict[str, int] | None) -> dict[str, Any]:
    raw = raw or {}

    def value(name: str) -> int:
        return int(raw.get(name) or 0)

    def average(total: str, samples: str) -> float | None:
        count = value(samples)
        return value(total) / count if count > 0 else None

    return {
        "t": _utc(timestamp),
        "total": value("total"),
        "successCount": value("success_count"),
        "errorCount": value("error_count"),
        "inputTokens": max(0, value("input_tokens")),
        "outputTokens": max(0, value("output_tokens")),
        "cacheReadTokens": max(0, value("cache_read_tokens")),
        "cacheCreationTokens": max(0, value("cache_creation_tokens")),
        "costTicks": max(0, value("cost_ticks")),
        "averageFirstTokenMilliseconds": average("sum_first_token_ms", "cnt_first_token"),
        "averageTotalMilliseconds": average("sum_total_ms", "cnt_total"),
    }


def _merge(target: dict[str, int], source: dict[str, int]) -> None:
    for name in _ADDITIVE:
        target[name] = int(target.get(name) or 0) + int(source.get(name) or 0)


def build_timeseries(
    source,
    *,
    now: float,
    range_value: TimeseriesRange | str,
    bucket: TimeseriesBucket | str | None,
    group: TimeseriesGroup | str,
    limit: int,
) -> dict[str, Any]:
    range_value = TimeseriesRange(range_value)
    bucket = TimeseriesBucket(bucket) if bucket else DEFAULT_BUCKET[range_value]
    group = TimeseriesGroup(group)
    if not 1 <= int(limit) <= 12:
        raise ManagementError(
            ManagementErrorCode.VALIDATION_FAILED,
            fields=(ErrorField("limit", "out_of_range", "limit must be between 1 and 12"),),
        )
    start, end, bucket_seconds, count = window(now, range_value, bucket)
    if count > MAX_BUCKETS:
        raise ManagementError(
            ManagementErrorCode.VALIDATION_FAILED,
            fields=(ErrorField(
                "bucket", "too_many_buckets",
                f"range/bucket combination yields {count} buckets; at most {MAX_BUCKETS} are allowed",
            ),),
        )
    with map_historical_log_errors():
        raw = source.bucketed_stats(
            start=start, bucket_seconds=bucket_seconds, count=count,
            group=_SOURCE_GROUP[group],
        )

    per_key: dict[str, dict[int, dict[str, int]]] = {}
    totals: dict[str, int] = {}
    for (index, key), point in raw.items():
        per_key.setdefault(key, {})[index] = point
        totals[key] = totals.get(key, 0) + int(point.get("total") or 0)

    series_specs: list[tuple[str, str, dict[int, dict[str, int]]]] = []
    if group is TimeseriesGroup.NONE:
        series_specs.append((TOTAL_KEY, TOTAL_LABEL, per_key.get(TOTAL_KEY, {})))
    else:
        ranked = sorted(totals, key=lambda key: (-totals[key], key))
        top, rest = ranked[: int(limit)], ranked[int(limit):]
        for key in top:
            series_specs.append((key, key, per_key[key]))
        if rest:
            other: dict[int, dict[str, int]] = {}
            for key in rest:
                for index, point in per_key[key].items():
                    _merge(other.setdefault(index, {}), point)
            series_specs.append((OTHER_KEY, OTHER_LABEL, other))

    series = [
        {
            "key": key,
            "label": label,
            "points": [
                _point(start + index * bucket_seconds, points.get(index))
                for index in range(count)
            ],
        }
        for key, label, points in series_specs
    ]
    data = {
        "range": range_value.value,
        "bucket": bucket.value,
        "bucketSeconds": bucket_seconds,
        "from": _utc(start),
        "to": _utc(end),
        "timezone": TIMESERIES_TIMEZONE,
        "series": series,
    }
    data["revision"] = revision_for(data)
    return data
