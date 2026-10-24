"""Console notification inbox use cases over the recorded notification store."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from src.management_control.context import AuditSink, ManagementContext
from src.management_control.dependency_mapping import dependency_result
from src.management_control.models.common import DomainControl
from src.notifications import inbox as inbox_module


@dataclass(frozen=True, slots=True)
class NotificationInboxPage:
    items: tuple[dict[str, Any], ...]
    total: int
    unread_count: int
    page: int
    page_size: int

    @property
    def has_next(self) -> bool:
        return self.page * self.page_size < self.total


class NotificationInboxControl(DomainControl):
    def __init__(self, *, inbox=inbox_module, audit_sink: AuditSink | None = None) -> None:
        super().__init__(audit_sink=audit_sink)
        self.inbox = inbox

    def list(self, context: ManagementContext, *, view: str, page: int, page_size: int) -> NotificationInboxPage:
        items, total, unread = dependency_result(lambda: self.inbox.listing(view, page, page_size))
        return NotificationInboxPage(tuple(items), total, unread, page, page_size)

    def mark_read(self, context: ManagementContext, *, ids: Iterable[str] = (), everything: bool = False) -> int:
        dependency_result(lambda: self.inbox.mark_read(tuple(ids), everything=everything))
        return dependency_result(lambda: self.inbox.listing("unread", 1, 1))[2]
