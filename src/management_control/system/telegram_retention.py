"""Frozen Telegram retention adapter over the shared RetentionControl."""

from __future__ import annotations

from typing import Any, Callable

from src.management_control.context import ManagementContext
from src.management_control.observability.retention import (
    DEFAULT_RETENTION_CONTROL,
    RetentionControl,
)


class TelegramRetentionAdapter:
    """Thin synchronous adapter preserving the v0.31.13 Telegram lifecycle.

    The Telegram menu continues to own its eight-character pending code, exact
    600-second TTL, progress rendering, and exception timing.  All retention
    business calls are delegated to the lifecycle-owned ``RetentionControl``;
    this adapter owns no config/log-database write path or plan application.
    """

    def __init__(self, control: RetentionControl = DEFAULT_RETENTION_CONTROL) -> None:
        self.control = control

    def settings(self, context: ManagementContext) -> dict[str, Any]:
        return self.control.sync_settings(context)

    def set_log_store_bodies(self, context: ManagementContext, value: bool) -> None:
        self.control.sync_set_log_store_bodies(context, value)

    def extend_days(self, context: ManagementContext, days: int) -> dict[str, Any]:
        return self.control.sync_extend_days(context, days)

    def set_forever(self, context: ManagementContext) -> dict[str, Any]:
        return self.control.sync_set_forever(context)

    def create_plan(self, context: ManagementContext, days: int) -> dict[str, Any]:
        return self.control.sync_create_plan(context, days)

    def commit_plan(
        self,
        context: ManagementContext,
        plan: dict[str, Any],
        *,
        progress: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        return self.control.sync_commit_plan(context, plan, progress=progress)

    # 正文保留（logBodyRetention）：只清理完整正文，元数据永久保留。
    def body_settings(self, context: ManagementContext) -> dict[str, Any]:
        return self.control.sync_body_settings(context)

    def update_body_policy(self, context: ManagementContext, *, mode: str, days: int | None) -> dict[str, Any]:
        return self.control.sync_update_body_policy(context, mode=mode, days=days)

    def create_body_plan(self, context: ManagementContext, days: int) -> dict[str, Any]:
        return self.control.sync_create_body_plan(context, days)

    def commit_body_plan(self, context: ManagementContext, plan: dict[str, Any]) -> dict[str, Any]:
        return self.control.sync_commit_body_plan(context, plan)


DEFAULT_TELEGRAM_RETENTION_ADAPTER = TelegramRetentionAdapter(DEFAULT_RETENTION_CONTROL)
