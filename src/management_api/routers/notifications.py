"""Console notification inbox routes."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from src.management_auth import Capability
from src.management_control import ManagementContext, ManagementErrorCode

from ..dependencies import require_capability
from ..error_mapping import management_error_responses
from ..schemas.base import DataEnvelope
from ..schemas.notifications import (
    NotificationInboxData,
    NotificationInboxEnvelope,
    NotificationInboxMeta,
    NotificationInboxQuery,
    NotificationItemData,
    NotificationMarkReadRequest,
    NotificationUnreadData,
)
from .system_support import (
    SystemNetworkControls,
    get_bound_system_network_controls,
    reject_unknown_query,
    response_meta,
    success_response,
)


router = APIRouter()
ReadContext = Annotated[ManagementContext, Depends(require_capability(Capability.READ))]
Controls = Annotated[SystemNetworkControls, Depends(get_bound_system_network_controls)]
_ERRORS = (
    ManagementErrorCode.SESSION_REQUIRED, ManagementErrorCode.SESSION_EXPIRED,
    ManagementErrorCode.ORIGIN_DENIED, ManagementErrorCode.CAPABILITY_DENIED,
    ManagementErrorCode.VALIDATION_FAILED, ManagementErrorCode.DEPENDENCY_UNAVAILABLE,
    ManagementErrorCode.SERVICE_NOT_READY,
)
_ITEM_EXAMPLE = {
    "id": "inc:0123456789abcdef0123456789abcdef",
    "source": "event",
    "type": "account.auth.refresh_failed",
    "tone": "warning",
    "title": "Codex · 授权续期失败",
    "subject": "账号：someone@example.com",
    "summary": "授权已失效：refresh token 已被撤销",
    "lines": ["授权已失效：refresh token 已被撤销", "已暂停调用此账号。", "处理：点「重新授权」重新登录。"],
    "status": "active",
    "count": 3,
    "firstAt": "2026-01-02T03:04:05Z",
    "lastAt": "2026-01-02T04:04:05Z",
    "unread": True,
    "links": [{"kind": "account", "id": "codex:someone@example.com", "label": "账户详情"}],
}
_LIST_EXAMPLE = {
    "data": {"items": [_ITEM_EXAMPLE], "unreadCount": 1},
    "meta": {"requestId": "request-example", "page": 1, "pageSize": 30, "total": 1, "hasNext": False},
}


def _moment(value: float) -> datetime:
    return datetime.fromtimestamp(value, timezone.utc)


def _item(value: dict) -> NotificationItemData:
    return NotificationItemData(
        id=value["id"], source=value["source"], type=value["type"], tone=value["tone"],
        title=value["title"], subject=value["subject"], summary=value["summary"], lines=list(value["lines"]),
        status=value["status"], count=value["count"], firstAt=_moment(value["first_at"]),
        lastAt=_moment(value["last_at"]), unread=value["unread"], links=list(value["links"]),
    )


@router.get(
    "/notifications",
    operation_id="listNotifications",
    dependencies=[Depends(reject_unknown_query("view", "page", "pageSize"))],
    tags=["notifications"],
    response_model=NotificationInboxEnvelope,
    responses={
        200: {"description": "Successful Response", "content": {"application/json": {"example": _LIST_EXAMPLE}}},
        **management_error_responses(*_ERRORS),
    },
)
def list_notifications(
    request: Request,
    context: ReadContext,
    controls: Controls,
    filters: Annotated[NotificationInboxQuery, Query()],
) -> NotificationInboxEnvelope:
    value = controls.notifications.list(context, view=filters.view, page=filters.page, page_size=filters.pageSize)
    return NotificationInboxEnvelope(
        data=NotificationInboxData(items=[_item(item) for item in value.items], unreadCount=value.unread_count),
        meta=NotificationInboxMeta(
            requestId=response_meta(request).requestId,
            page=value.page,
            pageSize=value.page_size,
            total=value.total,
            hasNext=value.has_next,
        ),
    )


@router.post(
    "/notifications/actions/mark-read",
    operation_id="markNotificationsRead",
    dependencies=[Depends(reject_unknown_query())],
    tags=["notifications"],
    response_model=DataEnvelope[NotificationUnreadData],
    responses={**success_response(200, {"unreadCount": 0}), **management_error_responses(*_ERRORS)},
)
def mark_notifications_read(
    body: NotificationMarkReadRequest,
    request: Request,
    context: ReadContext,
    controls: Controls,
) -> DataEnvelope[NotificationUnreadData]:
    unread = controls.notifications.mark_read(context, ids=body.ids, everything=body.all)
    return DataEnvelope(data=NotificationUnreadData(unreadCount=unread), meta=response_meta(request))
