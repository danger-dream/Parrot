"""Backup deletion controls shared by Web and Telegram update management."""
from __future__ import annotations

from typing import TYPE_CHECKING

from src.management_auth.principal import Capability

from ..context import ManagementContext
from ..errors import ManagementError, ManagementErrorCode
from .common import audit, invalid_field, require

if TYPE_CHECKING:
    from .updates import UpdateBackupClearResult


class UpdateBackupMixin:
    def delete_backup(self, context: ManagementContext, ref: str) -> None:
        require(context, Capability.UPDATE)
        target = str(ref or "").strip()
        if not target or "/" in target or "\\" in target or ".." in target or len(target) > 192:
            raise invalid_field("backupRef", "INVALID_BACKUP_REF", "backup reference is invalid")
        outcome = self._updates.delete_backup(target)
        if outcome == "not_found":
            raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
        if outcome in {"protected", "busy"}:
            audit(self._audit_sink, context, action="updates.backup.delete", target=target, result="rejected")
            raise ManagementError(
                ManagementErrorCode.STATE_CONFLICT,
                "backup is protected, ownership is unknown, or an update operation is in progress",
            )
        if outcome != "deleted":
            audit(self._audit_sink, context, action="updates.backup.delete", target=target, result="failed")
            raise ManagementError(
                ManagementErrorCode.DEPENDENCY_UNAVAILABLE,
                "failed to remove backup artifact or image tag",
                retryable=True,
            )
        audit(self._audit_sink, context, action="updates.backup.delete", target=target)

    def clear_backups(self, context: ManagementContext) -> UpdateBackupClearResult:
        from .updates import UpdateBackupClearResult

        require(context, Capability.UPDATE)
        outcome = self._updates.clear_backups()
        if outcome.get("busy"):
            audit(self._audit_sink, context, action="updates.backups.clear", target="backups", result="rejected")
            raise ManagementError(ManagementErrorCode.STATE_CONFLICT, "an update operation is in progress")
        if int(outcome.get("failed") or 0) > 0:
            audit(self._audit_sink, context, action="updates.backups.clear", target="backups", result="failed")
            raise ManagementError(
                ManagementErrorCode.DEPENDENCY_UNAVAILABLE,
                "one or more backup artifacts could not be removed",
                retryable=True,
            )
        audit(self._audit_sink, context, action="updates.backups.clear", target="backups")
        return UpdateBackupClearResult(
            deleted=int(outcome.get("deleted") or 0),
            kept_protected=bool(outcome.get("keptProtected")),
            kept_unowned=int(outcome.get("keptUnowned") or 0),
        )
