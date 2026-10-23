"""Statistics and Telegram statistics-preference endpoints."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Header, Path, Query, Request

from src.management_auth import Capability
from src.management_control import ManagementContext, ManagementErrorCode
from src.management_control.observability import StatsBreakdownQuery, StatsDimension, StatsPeriod, StatsSort
from src.management_control.observability import TimeseriesBucket, TimeseriesGroup, TimeseriesRange
from src.management_control.observability.common import revision_for

from ..dependencies import require_capability
from ..error_mapping import management_error_responses
from ..schemas import DataEnvelope
from ..schemas.observability import (
    CacheMissSampleData,
    ModelStatsData,
    RecentCallData,
    RecentOutcomeData,
    RevisionedPagedEnvelope,
    RevisionedPagedResponseMeta,
    StatsBreakdownData,
    StatsSummaryData,
    TelegramStatsPreferencesData,
    TelegramStatsPreferencesPatch,
)
from ..schemas.webui import OutcomeTimelineData, StatsCompareData, StatsTimeseriesData
from ._observability import controls, meta, paged_meta, reject_unknown_query


router = APIRouter(tags=["management-stats"])


def _revisioned(model_type, value):
    raw = dict(value)
    supplied = raw.pop("revision", None)
    public = {
        name: raw[name]
        for name in model_type.model_fields
        if name != "revision" and name in raw
    }
    public["revision"] = supplied or revision_for(public)
    return model_type.model_validate(public)


def _revisioned_page_meta(request: Request, result, values) -> RevisionedPagedResponseMeta:
    base = paged_meta(request, result).model_dump()
    snapshot = {
        "data": [value.model_dump(mode="json", exclude={"revision"}) for value in values],
        "page": result.page,
        "pageSize": result.page_size,
        "total": result.total,
        "hasNext": result.has_next,
    }
    return RevisionedPagedResponseMeta(**base, revision=revision_for(snapshot))


_READ_ERRORS = management_error_responses(
    ManagementErrorCode.SESSION_REQUIRED, ManagementErrorCode.SESSION_EXPIRED,
    ManagementErrorCode.CAPABILITY_DENIED, ManagementErrorCode.VALIDATION_FAILED,
    ManagementErrorCode.RESOURCE_NOT_FOUND, ManagementErrorCode.DEPENDENCY_UNAVAILABLE,
)
_WRITE_ERRORS = management_error_responses(
    ManagementErrorCode.SESSION_REQUIRED, ManagementErrorCode.SESSION_EXPIRED,
    ManagementErrorCode.CAPABILITY_DENIED, ManagementErrorCode.VALIDATION_FAILED,
    ManagementErrorCode.REVISION_CONFLICT, ManagementErrorCode.DEPENDENCY_UNAVAILABLE,
)


@router.get(
    "/stats/summary", operation_id="getStatsSummary",
    response_model=DataEnvelope[StatsSummaryData], responses=_READ_ERRORS,
)
def get_stats_summary(
    request: Request,
    context: Annotated[ManagementContext, Depends(require_capability(Capability.READ))],
    period: Annotated[StatsPeriod, Query()] = StatsPeriod.TODAY,
) -> DataEnvelope[StatsSummaryData]:
    reject_unknown_query(request, ("period",))
    value = controls(request).stats.summary(context, period)
    return DataEnvelope(data=StatsSummaryData.model_validate(value), meta=meta(request))


@router.get(
    "/stats/compare", operation_id="getStatsCompare",
    response_model=DataEnvelope[StatsCompareData], responses=_READ_ERRORS,
)
def get_stats_compare(
    request: Request,
    context: Annotated[ManagementContext, Depends(require_capability(Capability.READ))],
    period: Annotated[StatsPeriod | None, Query()] = None,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: Annotated[datetime | None, Query()] = None,
    model: Annotated[str | None, Query(min_length=1, max_length=500)] = None,
    channel: Annotated[str | None, Query(min_length=1, max_length=500)] = None,
    api_key: Annotated[str | None, Query(alias="apiKey", min_length=1, max_length=500)] = None,
) -> DataEnvelope[StatsCompareData]:
    """Totals for a window and the window right before it covering the same elapsed time.

    ``period`` (today / 3d / 7d / month) or ``from`` + ``to``; ``model``, ``channel`` and
    ``apiKey`` narrow both windows with the same keys the request log uses.
    """
    reject_unknown_query(request, ("period", "from", "to", "model", "channel", "apiKey"))
    value = controls(request).stats.compare(
        context,
        period=period.value if period is not None else None,
        start=from_.timestamp() if from_ is not None else None,
        end=to.timestamp() if to is not None else None,
        filters={"model": model, "channel": channel, "apiKey": api_key},
    )
    return DataEnvelope(data=StatsCompareData.model_validate(value), meta=meta(request))


@router.get(
    "/stats/timeseries", operation_id="getStatsTimeseries",
    response_model=DataEnvelope[StatsTimeseriesData], responses=_READ_ERRORS,
)
def get_stats_timeseries(
    request: Request,
    context: Annotated[ManagementContext, Depends(require_capability(Capability.READ))],
    range_value: Annotated[TimeseriesRange, Query(alias="range")],
    bucket: Annotated[TimeseriesBucket | None, Query()] = None,
    group_by: Annotated[TimeseriesGroup, Query(alias="groupBy")] = TimeseriesGroup.NONE,
    limit: Annotated[int, Query(ge=1, le=12)] = 6,
) -> DataEnvelope[StatsTimeseriesData]:
    reject_unknown_query(request, ("range", "bucket", "groupBy", "limit"))
    value = controls(request).stats.timeseries(
        context, range_value=range_value, bucket=bucket, group=group_by, limit=limit,
    )
    return DataEnvelope(data=StatsTimeseriesData.model_validate(value), meta=meta(request))


@router.get(
    "/stats/breakdown", operation_id="getStatsBreakdown",
    response_model=RevisionedPagedEnvelope[StatsBreakdownData], responses=_READ_ERRORS,
)
def get_stats_breakdown(
    request: Request,
    context: Annotated[ManagementContext, Depends(require_capability(Capability.READ))],
    dimension: Annotated[StatsDimension, Query()],
    period: Annotated[StatsPeriod, Query()] = StatsPeriod.TODAY,
    sort: Annotated[StatsSort, Query()] = StatsSort.TOTAL,
    descending: Annotated[bool, Query()] = True,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(alias="pageSize", ge=1, le=200)] = 50,
    family: Annotated[str | None, Query(pattern="^(anthropic|openai)$")] = None,
    channel: Annotated[str | None, Query(min_length=1, max_length=500)] = None,
) -> RevisionedPagedEnvelope[StatsBreakdownData]:
    reject_unknown_query(request, ("dimension", "period", "sort", "descending", "page", "pageSize",
                                   "family", "channel"))
    result = controls(request).stats.breakdown(context, StatsBreakdownQuery(
        dimension=dimension, period=period, sort=sort, descending=descending,
        page=page, page_size=page_size, family=family, channel=channel,
    ))
    values = [_revisioned(StatsBreakdownData, item) for item in result.items]
    return RevisionedPagedEnvelope(
        data=values, meta=_revisioned_page_meta(request, result, values),
    )


@router.get(
    "/stats/recent-outcomes", operation_id="listRecentOutcomes",
    response_model=DataEnvelope[list[RecentOutcomeData]], responses=_READ_ERRORS,
)
def list_recent_outcomes(
    request: Request,
    context: Annotated[ManagementContext, Depends(require_capability(Capability.READ))],
    dimension: Annotated[StatsDimension, Query()],
    limit: Annotated[int, Query(ge=1, le=60)] = 20,
) -> DataEnvelope[list[RecentOutcomeData]]:
    reject_unknown_query(request, ("dimension", "limit"))
    items = controls(request).stats.recent_outcomes(context, dimension=dimension, limit=limit)
    return DataEnvelope(data=[RecentOutcomeData.model_validate(item) for item in items], meta=meta(request))


@router.get(
    "/stats/outcome-timeline", operation_id="getOutcomeTimeline",
    response_model=DataEnvelope[OutcomeTimelineData], responses=_READ_ERRORS,
)
def get_outcome_timeline(
    request: Request,
    context: Annotated[ManagementContext, Depends(require_capability(Capability.READ))],
    dimension: Annotated[StatsDimension, Query()],
    bucketMinutes: Annotated[int, Query(ge=5, le=360)] = 60,
    buckets: Annotated[int, Query(ge=1, le=96)] = 24,
) -> DataEnvelope[OutcomeTimelineData]:
    reject_unknown_query(request, ("dimension", "bucketMinutes", "buckets"))
    data = controls(request).stats.outcome_timeline(
        context, dimension=dimension, bucket_minutes=bucketMinutes, buckets=buckets,
    )
    return DataEnvelope(data=OutcomeTimelineData.model_validate(data), meta=meta(request))


@router.get(
    "/stats/models/{modelId:path}", operation_id="getModelStats",
    response_model=DataEnvelope[ModelStatsData], responses=_READ_ERRORS,
)
def get_model_stats(
    request: Request,
    context: Annotated[ManagementContext, Depends(require_capability(Capability.READ))],
    model_id: Annotated[str, Path(alias="modelId", min_length=1, max_length=256)],
    period: Annotated[StatsPeriod, Query()] = StatsPeriod.TODAY,
) -> DataEnvelope[ModelStatsData]:
    reject_unknown_query(request, ("period",))
    value = controls(request).stats.model_stats(context, model_id, period)
    return DataEnvelope(data=ModelStatsData.model_validate(value), meta=meta(request))


@router.get(
    "/stats/cache-misses", operation_id="listCacheMissSamples",
    response_model=DataEnvelope[list[CacheMissSampleData]], responses=_READ_ERRORS,
)
def list_cache_miss_samples(
    request: Request,
    context: Annotated[ManagementContext, Depends(require_capability(Capability.READ))],
    period: StatsPeriod = StatsPeriod.TODAY,
) -> DataEnvelope[list[CacheMissSampleData]]:
    """TG「最近未命中样本」: up to 3 latest successful requests without cache read."""
    reject_unknown_query(request, ("period",))
    rows = controls(request).stats.cache_misses(context, period)
    return DataEnvelope(data=[CacheMissSampleData(
        id=str(row.get("request_id") or ""),
        createdAt=row.get("created_at"),
        model=row.get("requested_model") or row.get("final_model"),
        apiKeyName=row.get("api_key_name"),
        channelId=row.get("final_channel_key"),
        inputTokens=int(row.get("input_tokens") or 0),
        cacheCreationTokens=int(row.get("cache_creation_tokens") or 0),
        outputTokens=int(row.get("output_tokens") or 0),
        messageCount=int(row.get("msg_count") or 0),
        toolCount=int(row.get("tool_count") or 0),
    ) for row in rows], meta=meta(request))


@router.get(
    "/stats/recent-calls", operation_id="listRecentCalls",
    response_model=RevisionedPagedEnvelope[RecentCallData], responses=_READ_ERRORS,
)
def list_recent_calls(
    request: Request,
    context: Annotated[ManagementContext, Depends(require_capability(Capability.READ))],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(alias="pageSize", ge=1, le=200)] = 50,
) -> RevisionedPagedEnvelope[RecentCallData]:
    reject_unknown_query(request, ("page", "pageSize"))
    result = controls(request).stats.recent_calls(context, page=page, page_size=page_size)
    values = []
    for row in result.items:
        values.append(_revisioned(RecentCallData, {
            "id": str(row.get("request_id") or row.get("id") or ""),
            "status": str(row.get("status") or "unknown"),
            "createdAt": row.get("created_at"),
            "model": row.get("requested_model") or row.get("final_model"),
            "channelId": row.get("final_channel_key"),
            "durationMilliseconds": row.get("duration_ms") or row.get("total_time_ms"),
        }))
    return RevisionedPagedEnvelope(
        data=values, meta=_revisioned_page_meta(request, result, values),
    )


@router.get(
    "/preferences/telegram/stats", operation_id="getTelegramStatsPreferences",
    response_model=DataEnvelope[TelegramStatsPreferencesData], responses=_READ_ERRORS,
)
def get_telegram_stats_preferences(
    request: Request,
    context: Annotated[ManagementContext, Depends(require_capability(Capability.READ))],
) -> DataEnvelope[TelegramStatsPreferencesData]:
    reject_unknown_query(request, ())
    value = controls(request).stats.get_preferences(context)
    return DataEnvelope(data=TelegramStatsPreferencesData.model_validate(value), meta=meta(request))


@router.patch(
    "/preferences/telegram/stats", operation_id="updateTelegramStatsPreferences",
    response_model=DataEnvelope[TelegramStatsPreferencesData], responses=_WRITE_ERRORS,
)
def update_telegram_stats_preferences(
    request: Request,
    context: Annotated[ManagementContext, Depends(require_capability(Capability.WRITE))],
    patch: Annotated[TelegramStatsPreferencesPatch, Body()],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> DataEnvelope[TelegramStatsPreferencesData]:
    reject_unknown_query(request, ())
    value = controls(request).stats.update_preferences(
        context, patch.model_dump(exclude_none=True), expected_revision=if_match,
    )
    return DataEnvelope(data=TelegramStatsPreferencesData.model_validate(value), meta=meta(request))
