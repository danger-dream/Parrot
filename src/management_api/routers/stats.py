"""Statistics and Telegram statistics-preference endpoints."""

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
    ModelStatsData,
    RecentCallData,
    RevisionedPagedEnvelope,
    RevisionedPagedResponseMeta,
    StatsBreakdownData,
    StatsSummaryData,
    TelegramStatsPreferencesData,
    TelegramStatsPreferencesPatch,
)
from ..schemas.webui import StatsTimeseriesData
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
) -> RevisionedPagedEnvelope[StatsBreakdownData]:
    reject_unknown_query(request, ("dimension", "period", "sort", "descending", "page", "pageSize"))
    result = controls(request).stats.breakdown(context, StatsBreakdownQuery(
        dimension=dimension, period=period, sort=sort, descending=descending,
        page=page, page_size=page_size,
    ))
    values = [_revisioned(StatsBreakdownData, item) for item in result.items]
    return RevisionedPagedEnvelope(
        data=values, meta=_revisioned_page_meta(request, result, values),
    )


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
