"""Multimedia log and cached-artifact endpoints."""

from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import Response, StreamingResponse

from src.management_auth import Capability
from src.management_control import ManagementContext, ManagementErrorCode
from src.management_control.observability.common import revision_for
from src.management_control.observability import (
    MediaAction,
    MediaLogQuery,
    MediaSort,
    MediaStatus,
    normalize_utc_range,
)

from ..dependencies import require_capability
from ..error_mapping import management_error_responses
from ..schemas import DataEnvelope
from ..schemas.base import StrictSchema
from ..schemas.observability import (
    MediaArtifactData,
    MediaLogData,
    MediaLogDetailData,
    RevisionedCollectionEnvelope,
    RevisionedPagedEnvelope,
    RevisionedPagedResponseMeta,
    RevisionedResponseMeta,
    Rfc3339UtcDateTime,
)
from ._observability import controls, meta, paged_meta, reject_unknown_query


router = APIRouter(prefix="/media-logs", tags=["management-media"])


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


_ERRORS = management_error_responses(
    ManagementErrorCode.SESSION_REQUIRED, ManagementErrorCode.SESSION_EXPIRED,
    ManagementErrorCode.CAPABILITY_DENIED, ManagementErrorCode.VALIDATION_FAILED,
    ManagementErrorCode.RESOURCE_NOT_FOUND, ManagementErrorCode.DEPENDENCY_UNAVAILABLE,
)


class MediaTopAccountData(StrictSchema):
    accountKey: str
    accountEmail: str | None = None
    provider: str
    total: int
    costUsdTicks: int


class MediaLogSummaryData(StrictSchema):
    """TG 多媒体日志汇总: GPT / Grok images, videos, outcomes, cost, OAuth top 3."""
    total: int
    openaiImages: int
    xaiImages: int
    videos: int
    successCount: int
    failedCount: int
    pendingCount: int
    expiredCount: int
    costUsdTicks: int
    topAccounts: list[MediaTopAccountData]


@router.get(
    "/summary", operation_id="getMediaLogSummary",
    response_model=DataEnvelope[MediaLogSummaryData], responses=_ERRORS,
)
def get_media_log_summary(
    request: Request,
    context: Annotated[ManagementContext, Depends(require_capability(Capability.READ))],
) -> DataEnvelope[MediaLogSummaryData]:
    reject_unknown_query(request, ())
    return DataEnvelope(data=MediaLogSummaryData(**controls(request).media.summary(context)), meta=meta(request))


@router.get(
    "", operation_id="listMediaLogs",
    response_model=RevisionedPagedEnvelope[MediaLogData], responses=_ERRORS,
)
def list_media_logs(
    request: Request,
    context: Annotated[ManagementContext, Depends(require_capability(Capability.READ))],
    status: Annotated[list[MediaStatus] | None, Query()] = None,
    provider: Annotated[list[str] | None, Query()] = None,
    model: Annotated[list[str] | None, Query()] = None,
    action: Annotated[list[MediaAction] | None, Query()] = None,
    started_at: Annotated[Rfc3339UtcDateTime | None, Query(alias="startedAt")] = None,
    ended_at: Annotated[Rfc3339UtcDateTime | None, Query(alias="endedAt")] = None,
    sort: Annotated[MediaSort, Query()] = MediaSort.CREATED_AT,
    descending: Annotated[bool, Query()] = True,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(alias="pageSize", ge=1, le=200)] = 50,
) -> RevisionedPagedEnvelope[MediaLogData]:
    reject_unknown_query(request, (
        "status", "provider", "model", "action", "startedAt", "endedAt",
        "sort", "descending", "page", "pageSize",
    ))
    started_at, ended_at = normalize_utc_range(started_at, ended_at)
    result = controls(request).media.list_logs(context, MediaLogQuery(
        statuses=tuple(status or ()), providers=tuple(provider or ()), models=tuple(model or ()),
        actions=tuple(action or ()), started_at=started_at, ended_at=ended_at,
        sort=sort, descending=descending, page=page, page_size=page_size,
    ))
    values = [_revisioned(MediaLogData, item) for item in result.items]
    return RevisionedPagedEnvelope(
        data=values, meta=_revisioned_page_meta(request, result, values),
    )


@router.get(
    "/{mediaLogId}/artifacts", operation_id="listMediaArtifacts",
    response_model=RevisionedCollectionEnvelope[MediaArtifactData], responses=_ERRORS,
)
def list_media_artifacts(
    request: Request,
    context: Annotated[ManagementContext, Depends(require_capability(Capability.READ))],
    media_log_id: Annotated[str, Path(alias="mediaLogId", min_length=1, max_length=128)],
) -> RevisionedCollectionEnvelope[MediaArtifactData]:
    reject_unknown_query(request, ())
    values = [
        _revisioned(MediaArtifactData, item)
        for item in controls(request).media.artifacts(context, media_log_id)
    ]
    public = [value.model_dump(mode="json", exclude={"revision"}) for value in values]
    return RevisionedCollectionEnvelope(
        data=values,
        meta=RevisionedResponseMeta(
            **meta(request), revision=revision_for({"mediaLogId": media_log_id, "data": public}),
        ),
    )


@router.get(
    "/{mediaLogId}/artifacts/{artifactId}", operation_id="downloadMediaArtifact",
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "Authenticated cached media artifact",
            "content": {"application/octet-stream": {"example": "<binary>"}},
        },
        **_ERRORS,
    },
)
def download_media_artifact(
    request: Request,
    context: Annotated[ManagementContext, Depends(require_capability(Capability.READ))],
    media_log_id: Annotated[str, Path(alias="mediaLogId", min_length=1, max_length=128)],
    artifact_id: Annotated[str, Path(alias="artifactId", min_length=1, max_length=128)],
) -> StreamingResponse:
    reject_unknown_query(request, ())
    try:
        artifact = controls(request).media.download(
            context, media_log_id, artifact_id, range_header=request.headers.get("range"),
        )
    except ValueError:
        size = next((item["sizeBytes"] for item in controls(request).media.artifacts(context, media_log_id)
                     if item["id"] == artifact_id), 0)
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}",
                                                  "Accept-Ranges": "bytes", "Cache-Control": "private, no-store"})
    filename = quote(artifact.filename, safe="")
    headers = {
        "Content-Length": str(artifact.size),
        "Content-Disposition": f"attachment; filename*=UTF-8''{filename}",
        "Cache-Control": "private, no-store",
        "Accept-Ranges": "bytes",
    }
    byte_range = getattr(artifact, "range", None)
    if byte_range is not None:
        start, end = byte_range
        headers["Content-Range"] = f"bytes {start}-{end}/{artifact.total_size}"
    return StreamingResponse(
        artifact.chunks, status_code=206 if byte_range is not None else 200,
        media_type=artifact.content_type, headers=headers,
    )


@router.get(
    "/{mediaLogId}", operation_id="getMediaLog",
    response_model=DataEnvelope[MediaLogDetailData], responses=_ERRORS,
)
def get_media_log(
    request: Request,
    context: Annotated[ManagementContext, Depends(require_capability(Capability.READ))],
    media_log_id: Annotated[str, Path(alias="mediaLogId", min_length=1, max_length=128)],
) -> DataEnvelope[MediaLogDetailData]:
    reject_unknown_query(request, ())
    value = controls(request).media.detail(context, media_log_id)
    return DataEnvelope(data=MediaLogDetailData.model_validate(value), meta=meta(request))
