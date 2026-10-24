"""Schemas for the console notification inbox."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from .base import ResponseMeta, StrictSchema


NotificationTone = Literal["info", "warning", "error", "critical", "recovered"]
NotificationStatus = Literal["active", "recovered", "ended"]
NotificationLinkKind = Literal["account", "channel", "model", "request", "apikey", "page"]


class NotificationInboxQuery(StrictSchema):
    view: Literal["unread", "read"] = "unread"
    page: int = Field(default=1, ge=1)
    pageSize: int = Field(default=30, ge=1, le=100)


class NotificationLinkData(StrictSchema):
    kind: NotificationLinkKind
    id: str
    label: str


class NotificationItemData(StrictSchema):
    id: str
    source: Literal["event", "legacy"]
    type: str
    tone: NotificationTone
    title: str
    subject: str
    summary: str
    lines: list[str]
    status: NotificationStatus | None
    count: int
    firstAt: datetime
    lastAt: datetime
    unread: bool
    links: list[NotificationLinkData]


class NotificationInboxData(StrictSchema):
    items: list[NotificationItemData]
    unreadCount: int


class NotificationInboxMeta(ResponseMeta):
    page: int
    pageSize: int
    total: int
    hasNext: bool


class NotificationInboxEnvelope(StrictSchema):
    data: NotificationInboxData
    meta: NotificationInboxMeta


class NotificationMarkReadRequest(StrictSchema):
    ids: list[str] = Field(default_factory=list, max_length=200)
    all: bool = False


class NotificationUnreadData(StrictSchema):
    unreadCount: int
