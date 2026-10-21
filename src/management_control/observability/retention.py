"""Actor-bound retention policy, prepare/commit plans and asynchronous execution."""

from __future__ import annotations

import copy
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable

from src import config as config_module
from src import log_body_retention as log_body_module
from src import log_db as log_db_module
from src.management_auth import Capability
from src.management_control.context import AuditSink, ManagementContext, audit_record
from src.management_control.errors import ErrorField, ManagementError, ManagementErrorCode
from src.management_control.operations import ManagementOperation, OperationStore

from .common import require, revision_for


class RetentionMode(str, Enum):
    FOREVER = "forever"
    DAYS = "days"


class RetentionScope(str, Enum):
    REQUESTS = "requests"
    BODIES = "bodies"


def _utc(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc) if value else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _public_compaction(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "month": str(row.get("month") or ""),
        "action": str(row.get("action") or "skipped"),
        "compacted": bool(row.get("compacted")),
        "reason": str(row.get("reason") or ""),
        "beforeBytes": int(row.get("before_bytes") or 0),
        "afterBytes": int(row.get("after_bytes") or 0),
        "at": _utc(row.get("at")),
    }


def public_body_cleanup(status: dict[str, Any]) -> dict[str, Any]:
    """Stable API projection of the background body-retention runner state."""

    return {
        "running": bool(status.get("running")),
        "automatic": bool(status.get("automatic")),
        "month": status.get("month") or None,
        "startedAt": _utc(status.get("started_at")),
        "finishedAt": _utc(status.get("finished_at")),
        "purgedRequests": int(status.get("purged_requests") or 0),
        "purgedMcpResults": int(status.get("purged_mcp") or 0),
        "purgedBytes": int(status.get("purged_bytes") or 0),
        "skippedActive": int(status.get("skipped_active") or 0),
        "errors": [str(item) for item in status.get("errors") or []],
        "stopped": str(status.get("stopped") or ""),
        "compaction": [_public_compaction(row) for row in status.get("last_compaction") or []],
    }


class PlanState(str, Enum):
    PREPARED = "prepared"
    COMMITTED = "committed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class RetentionPlan:
    id: str
    actor_subject_id: str
    session_id: str | None
    revision: str
    created_at: datetime
    expires_at: datetime
    state: PlanState
    raw_plan: dict[str, Any]
    operation_id: str | None = None
    commit_idempotency_key: str | None = None


class RetentionControl:
    def __init__(
        self,
        *,
        log_db=log_db_module,
        config=config_module,
        body=log_body_module,
        audit_sink: AuditSink | None = None,
        now: Callable[[], float] = time.time,
        ttl_seconds: int = 600,
        max_plans: int = 256,
        start_worker: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        self.log_db = log_db
        self.config = config
        self.body = body
        self.audit_sink = audit_sink
        self._now = now
        self.ttl_seconds = ttl_seconds
        self.max_plans = max_plans
        self._plans: OrderedDict[str, RetentionPlan] = OrderedDict()
        self._idempotent_creates: dict[tuple[str, str | None, str], str] = {}
        self._expired_ids: OrderedDict[str, tuple[str, str | None]] = OrderedDict()
        self._lock = threading.RLock()
        self._start_worker = start_worker

    def _audit(self, context: ManagementContext, action: str, target: str, result: str) -> None:
        if self.audit_sink is not None:
            self.audit_sink.record(audit_record(context, action=action, target=target, result=result))

    def _visible(self, context: ManagementContext, plan: RetentionPlan) -> bool:
        if plan.session_id is not None:
            return plan.session_id == context.actor.session_id
        return plan.actor_subject_id == context.actor.subject_id

    def _purge(self) -> None:
        now = self._now()
        expired = [key for key, plan in self._plans.items() if plan.expires_at.timestamp() <= now]
        for key in expired:
            plan = self._plans.pop(key)
            self._expired_ids[key] = (plan.actor_subject_id, plan.session_id)
        while len(self._expired_ids) > self.max_plans:
            self._expired_ids.popitem(last=False)
        live_ids = set(self._plans)
        for key, plan_id in tuple(self._idempotent_creates.items()):
            if plan_id not in live_ids:
                self._idempotent_creates.pop(key, None)
        while len(self._plans) >= self.max_plans:
            self._plans.popitem(last=False)

    def _get(self, context: ManagementContext, plan_id: str) -> RetentionPlan:
        self._purge()
        plan = self._plans.get(plan_id)
        if plan is None:
            owner = self._expired_ids.get(plan_id)
            if owner is not None and owner == (context.actor.subject_id, context.actor.session_id):
                raise ManagementError(ManagementErrorCode.STATE_CONFLICT, "Retention plan has expired")
            raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
        if not self._visible(context, plan):
            raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
        return plan

    def settings(self, context: ManagementContext) -> dict[str, Any]:
        require(context)
        cfg = self.config.get()
        policy = self.log_db.retention_policy(cfg)
        try:
            rows = int(self.log_db.management_logs_count())
        except log_db_module.HistoricalLogError as exc:
            raise ManagementError(
                ManagementErrorCode.DEPENDENCY_UNAVAILABLE, retryable=True,
            ) from exc
        except Exception:
            rows = 0
        body_policy = self.body.body_retention_policy(cfg)
        data = {
            "mode": str(policy.get("mode") or "forever"),
            "days": policy.get("days"),
            "logStoreBodies": cfg.get("logStoreBodies", False) is not False,
            "currentData": {"rows": rows},
            "busy": bool(self.log_db.retention_cleanup_busy()),
            "bodyMode": str(body_policy.get("mode") or "forever"),
            "bodyDays": body_policy.get("days"),
            "bodyCleanup": public_body_cleanup(self.body.body_retention_status()),
        }
        data["revision"] = revision_for({
            "mode": data["mode"], "days": data["days"],
            "logStoreBodies": data["logStoreBodies"],
            "bodyMode": data["bodyMode"], "bodyDays": data["bodyDays"],
        })
        return data

    def update_settings(
        self,
        context: ManagementContext,
        *,
        mode: RetentionMode | None,
        days: int | None,
        log_store_bodies: bool | None,
        expected_revision: str | None,
        body_mode: RetentionMode | None = None,
        body_days: int | None = None,
    ) -> dict[str, Any]:
        require(context, Capability.WRITE)
        current = self.settings(context)
        if expected_revision is not None and expected_revision != current["revision"]:
            raise ManagementError(ManagementErrorCode.REVISION_CONFLICT)
        if (mode is None and days is None and log_store_bodies is None
                and body_mode is None and body_days is None):
            raise ManagementError(
                ManagementErrorCode.VALIDATION_FAILED,
                fields=(ErrorField(path="body", code="empty_patch", message="At least one field is required"),),
            )
        target_mode = RetentionMode(mode or current["mode"])
        if target_mode is RetentionMode.FOREVER and days is not None:
            raise ManagementError(
                ManagementErrorCode.VALIDATION_FAILED,
                fields=(ErrorField(path="days", code="field_not_allowed", message="days is not used in forever mode"),),
            )
        if mode is None and days is not None and current["mode"] == RetentionMode.FOREVER.value:
            raise ManagementError(
                ManagementErrorCode.VALIDATION_FAILED,
                fields=(ErrorField(path="days", code="mode_required", message="mode=days is required"),),
            )
        target_days = days if days is not None else current["days"]
        if target_mode is RetentionMode.DAYS and (
            isinstance(target_days, bool) or not isinstance(target_days, int) or target_days < 1
        ):
            raise ManagementError(
                ManagementErrorCode.VALIDATION_FAILED,
                fields=(ErrorField(path="days", code="greater_than_equal", message="days must be at least 1"),),
            )

        current_mode = RetentionMode(current["mode"])
        current_days = current["days"]
        policy_changed = target_mode is not current_mode or (
            target_mode is RetentionMode.DAYS and target_days != current_days
        )
        dangerous = (
            current_mode is RetentionMode.FOREVER and target_mode is RetentionMode.DAYS
        ) or (
            current_mode is RetentionMode.DAYS and target_mode is RetentionMode.DAYS
            and int(target_days) < int(current_days)
        )
        # Detect the whole patch before writing logStoreBodies: a destructive
        # policy transition must be prepared and committed through a plan.
        if dangerous:
            self._audit(context, "retention.settings.update", "logs.retention", "confirmation_required")
            raise ManagementError(
                ManagementErrorCode.CONFIRMATION_REQUIRED,
                fields=(ErrorField(
                    path="mode" if current_mode is RetentionMode.FOREVER else "days",
                    code="retention_plan_required",
                    message="Create and commit a retention plan for this policy change",
                ),),
            )

        body_target = self._body_patch_target(context, current, body_mode, body_days)

        body_changed = (
            log_store_bodies is not None
            and bool(log_store_bodies) != bool(current["logStoreBodies"])
        )
        if policy_changed or body_changed:
            try:
                result = self.log_db.update_retention_settings(
                    mode=target_mode.value if policy_changed else None,
                    days=target_days if policy_changed and target_mode is RetentionMode.DAYS else None,
                    log_store_bodies=bool(log_store_bodies) if body_changed else None,
                    expected_policy={"mode": current_mode.value, "days": current_days},
                    expected_log_store_bodies=bool(current["logStoreBodies"]),
                )
            except Exception as exc:
                self._audit(context, "retention.settings.update", "logs.retention", "failed")
                raise ManagementError(
                    ManagementErrorCode.DEPENDENCY_UNAVAILABLE, retryable=True,
                ) from exc
            if not isinstance(result, dict) or not result.get("ok"):
                self._audit(context, "retention.settings.update", "logs.retention", "failed")
                error = result.get("error") if isinstance(result, dict) else None
                if error == "revision_conflict":
                    raise ManagementError(ManagementErrorCode.REVISION_CONFLICT)
                if error == "plan_required":
                    raise ManagementError(
                        ManagementErrorCode.CONFIRMATION_REQUIRED,
                        fields=(ErrorField(
                            path="mode" if current_mode is RetentionMode.FOREVER else "days",
                            code="retention_plan_required",
                            message="Create and commit a retention plan for this policy change",
                        ),),
                    )
                if error == "persist_failed":
                    raise ManagementError(
                        ManagementErrorCode.DEPENDENCY_UNAVAILABLE, retryable=True,
                    )
                raise ManagementError(ManagementErrorCode.STATE_CONFLICT)
        if body_target is not None:
            self._save_body_policy(context, current, body_target)
        self._audit(
            context, "retention.settings.update", "logs.retention",
            "succeeded" if policy_changed or body_changed or body_target is not None else "noop",
        )
        return self.settings(context)

    def _body_patch_target(
        self,
        context: ManagementContext,
        current: dict[str, Any],
        body_mode: RetentionMode | None,
        body_days: int | None,
    ) -> dict[str, Any] | None:
        """Validate the body-retention half of a patch before anything is written."""

        if body_mode is None and body_days is None:
            return None
        current_mode = RetentionMode(current["bodyMode"])
        target_mode = RetentionMode(body_mode or current_mode)
        if target_mode is RetentionMode.FOREVER and body_days is not None:
            raise ManagementError(
                ManagementErrorCode.VALIDATION_FAILED,
                fields=(ErrorField(path="bodyDays", code="field_not_allowed", message="bodyDays is not used in forever mode"),),
            )
        if body_mode is None and current_mode is RetentionMode.FOREVER:
            raise ManagementError(
                ManagementErrorCode.VALIDATION_FAILED,
                fields=(ErrorField(path="bodyDays", code="mode_required", message="bodyMode=days is required"),),
            )
        target_days = body_days if body_days is not None else current["bodyDays"]
        if target_mode is RetentionMode.DAYS and (
            isinstance(target_days, bool) or not isinstance(target_days, int) or target_days < 1
        ):
            raise ManagementError(
                ManagementErrorCode.VALIDATION_FAILED,
                fields=(ErrorField(path="bodyDays", code="greater_than_equal", message="bodyDays must be at least 1"),),
            )
        if target_mode is RetentionMode.FOREVER:
            target = {"mode": RetentionMode.FOREVER.value, "days": None}
        else:
            target = {"mode": RetentionMode.DAYS.value, "days": int(target_days)}
        before = {"mode": current_mode.value, "days": current["bodyDays"]}
        if target == before:
            return None
        dangerous = target["mode"] == RetentionMode.DAYS.value and (
            current_mode is RetentionMode.FOREVER or int(target["days"]) < int(current["bodyDays"])
        )
        if dangerous:
            self._audit(context, "retention.settings.update", "logs.retention", "confirmation_required")
            raise ManagementError(
                ManagementErrorCode.CONFIRMATION_REQUIRED,
                fields=(ErrorField(
                    path="bodyMode" if current_mode is RetentionMode.FOREVER else "bodyDays",
                    code="retention_plan_required",
                    message="Create and commit a bodies-scope retention plan for this policy change",
                ),),
            )
        return target

    def _save_body_policy(self, context: ManagementContext, current: dict[str, Any], target: dict[str, Any]) -> None:
        try:
            result = self.body.update_body_retention_settings(
                mode=target["mode"], days=target["days"],
                expected_policy={"mode": current["bodyMode"], "days": current["bodyDays"]},
            )
        except Exception as exc:
            self._audit(context, "retention.settings.update", "logs.retention", "failed")
            raise ManagementError(ManagementErrorCode.DEPENDENCY_UNAVAILABLE, retryable=True) from exc
        if result.get("ok"):
            return
        self._audit(context, "retention.settings.update", "logs.retention", "failed")
        error = result.get("error")
        if error == "revision_conflict":
            raise ManagementError(ManagementErrorCode.REVISION_CONFLICT)
        if error == "plan_required":
            raise ManagementError(ManagementErrorCode.CONFIRMATION_REQUIRED)
        if error == "persist_failed":
            raise ManagementError(ManagementErrorCode.DEPENDENCY_UNAVAILABLE, retryable=True)
        raise ManagementError(ManagementErrorCode.STATE_CONFLICT)

    def _scan_retention_plan(self, days: int, scope: RetentionScope = RetentionScope.REQUESTS) -> dict[str, Any]:
        if scope is RetentionScope.BODIES:
            return self.body.plan_body_retention(days)
        return self.log_db.plan_retention(days)

    def _apply_retention_plan(
        self,
        plan: dict[str, Any],
        *,
        progress: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        if plan.get("scope") == RetentionScope.BODIES.value:
            # Saves the policy, then hands the batched purge to the single
            # background runner; progress is exposed through settings.bodyCleanup.
            return self.body.apply_body_retention_plan(plan, activate_policy=True)
        return self.log_db.apply_retention_plan(
            plan, activate_policy=True, progress=progress,
        )

    def sync_settings(self, context: ManagementContext) -> dict[str, Any]:
        """Return the frozen synchronous adapter view without API-only fields."""

        require(context, Capability.READ)
        cfg = self.config.get()
        policy = self.log_db.retention_policy(cfg)
        return {
            "mode": policy["mode"],
            "days": policy.get("days"),
            "logStoreBodies": cfg.get("logStoreBodies", False) is not False,
        }

    def sync_set_log_store_bodies(self, context: ManagementContext, value: bool) -> None:
        require(context, Capability.WRITE)
        # Preserve the Telegram timing/error contract: a raw persistence error
        # escapes before the adapter emits its success answer and redraw.
        self.config.update(lambda cfg: cfg.__setitem__("logStoreBodies", value))

    def sync_extend_days(self, context: ManagementContext, days: int) -> dict[str, Any]:
        require(context, Capability.WRITE)
        return self.log_db.extend_retention_days(days)

    def sync_set_forever(self, context: ManagementContext) -> dict[str, Any]:
        require(context, Capability.WRITE)
        return self.log_db.set_retention_forever()

    def sync_create_plan(self, context: ManagementContext, days: int) -> dict[str, Any]:
        require(context, Capability.WRITE)
        # Keep raw timestamps for the frozen renderer's fallback semantics.
        return self._scan_retention_plan(days)

    def sync_body_settings(self, context: ManagementContext) -> dict[str, Any]:
        require(context, Capability.READ)
        policy = self.body.body_retention_policy(self.config.get())
        return {
            "mode": policy["mode"],
            "days": policy.get("days"),
            "status": self.body.body_retention_status(),
        }

    def sync_update_body_policy(self, context: ManagementContext, *, mode: str, days: int | None) -> dict[str, Any]:
        """Non-destructive body policy change (forever or longer days)."""

        require(context, Capability.WRITE)
        return self.body.update_body_retention_settings(mode=mode, days=days)

    def sync_create_body_plan(self, context: ManagementContext, days: int) -> dict[str, Any]:
        require(context, Capability.WRITE)
        return self._scan_retention_plan(days, RetentionScope.BODIES)

    def sync_commit_body_plan(self, context: ManagementContext, plan: dict[str, Any]) -> dict[str, Any]:
        require(context, Capability.DESTRUCTIVE)
        return self.body.apply_body_retention_plan(plan, activate_policy=True)

    def sync_commit_plan(
        self,
        context: ManagementContext,
        plan: dict[str, Any],
        *,
        progress: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        require(context, Capability.DESTRUCTIVE)
        # Deliberately synchronous and call-local: the Telegram menu controls
        # progress ordering and retains the original exception point.
        return self._apply_retention_plan(plan, progress=progress)

    @staticmethod
    def _public_plan(plan: RetentionPlan) -> dict[str, Any]:
        raw = plan.raw_plan
        items = list(raw.get("items") or [])
        return {
            "id": plan.id,
            "state": plan.state.value,
            "days": int(raw.get("days") or 0),
            "cutoff": datetime.fromtimestamp(float(raw.get("cutoff") or 0), tz=timezone.utc),
            "expiresAt": plan.expires_at,
            "scope": str(raw.get("scope") or RetentionScope.REQUESTS.value),
            "affectedRows": sum(
                int(item.get("expired_requests") or 0) + int(item.get("expired_mcp") or 0)
                for item in items
            ),
            "affectedFiles": len(items),
            "affectedBytes": sum(int(item.get("bundle_bytes") or 0) for item in items),
            "scannedRows": int(raw.get("scanned_requests") or 0),
            "scannedFiles": int(raw.get("scanned_months") or 0),
            "scannedBytes": int(raw.get("scanned_bytes") or 0),
            "preflightOk": bool((raw.get("preflight") or {}).get("ok")),
            "errors": [str(item) for item in raw.get("errors") or []],
            "revision": plan.revision,
            "operationId": plan.operation_id,
        }

    def create_plan(
        self,
        context: ManagementContext,
        *,
        days: int,
        scope: RetentionScope | str = RetentionScope.REQUESTS,
    ) -> dict[str, Any]:
        require(context, Capability.DESTRUCTIVE)
        scope = RetentionScope(scope)
        if isinstance(days, bool) or not isinstance(days, int) or days < 1:
            raise ManagementError(
                ManagementErrorCode.VALIDATION_FAILED,
                fields=(ErrorField(path="days", code="greater_than_equal", message="days must be at least 1"),),
            )
        idempotency = context.idempotency_key
        create_key = (context.actor.subject_id, context.actor.session_id, idempotency or "")
        with self._lock:
            self._purge()
            if idempotency and create_key in self._idempotent_creates:
                existing = self._get(context, self._idempotent_creates[create_key])
                if (int(existing.raw_plan.get("days") or 0) != days
                        or str(existing.raw_plan.get("scope") or "requests") != scope.value):
                    raise ManagementError(
                        ManagementErrorCode.RESOURCE_CONFLICT,
                        "Idempotency key was already used with different retention days",
                    )
                return self._public_plan(existing)
        try:
            raw = self._scan_retention_plan(days, scope)
        except Exception as exc:
            raise ManagementError(ManagementErrorCode.DEPENDENCY_UNAVAILABLE, retryable=True) from exc
        if raw.get("errors") or not bool((raw.get("preflight") or {}).get("ok")):
            raise ManagementError(ManagementErrorCode.STATE_CONFLICT)
        now = self._now()
        revision = revision_for({"policy": raw.get("base_policy"), "signature": raw.get("signature")})
        plan = RetentionPlan(
            id="plan_" + secrets.token_urlsafe(18),
            actor_subject_id=context.actor.subject_id,
            session_id=context.actor.session_id,
            revision=revision,
            created_at=datetime.fromtimestamp(now, tz=timezone.utc),
            expires_at=datetime.fromtimestamp(now + self.ttl_seconds, tz=timezone.utc),
            state=PlanState.PREPARED,
            raw_plan=copy.deepcopy(raw),
        )
        with self._lock:
            self._purge()
            # The scan is intentionally outside the lock.  Re-check the
            # actor/session/idempotency tuple before publishing so concurrent
            # equal payloads converge and differing payloads conflict.
            if idempotency and create_key in self._idempotent_creates:
                existing = self._get(context, self._idempotent_creates[create_key])
                if (int(existing.raw_plan.get("days") or 0) != days
                        or str(existing.raw_plan.get("scope") or "requests") != scope.value):
                    raise ManagementError(
                        ManagementErrorCode.RESOURCE_CONFLICT,
                        "Idempotency key was already used with different retention days",
                    )
                return self._public_plan(existing)
            self._plans[plan.id] = plan
            if idempotency:
                self._idempotent_creates[create_key] = plan.id
        self._audit(context, "retention.plan.create", plan.id, "prepared")
        return self._public_plan(plan)

    def commit_plan(
        self,
        context: ManagementContext,
        plan_id: str,
        *,
        expected_revision: str,
        operations: OperationStore,
    ) -> ManagementOperation:
        require(context, Capability.DESTRUCTIVE)
        if not expected_revision:
            raise ManagementError(ManagementErrorCode.INVALID_REQUEST)
        with self._lock:
            plan = self._get(context, plan_id)
            if plan.revision != expected_revision:
                raise ManagementError(ManagementErrorCode.REVISION_CONFLICT)
            if plan.state is PlanState.COMMITTED:
                if context.idempotency_key and context.idempotency_key == plan.commit_idempotency_key and plan.operation_id:
                    return operations.get(context, plan.operation_id)
                raise ManagementError(ManagementErrorCode.STATE_CONFLICT)
            if plan.state is not PlanState.PREPARED:
                raise ManagementError(ManagementErrorCode.STATE_CONFLICT)
            operation = operations.create(context, kind="logs.retention.commit", cancellable=False)
            committed = replace(
                plan,
                state=PlanState.COMMITTED,
                operation_id=operation.id,
                commit_idempotency_key=context.idempotency_key,
            )
            self._plans[plan_id] = committed

        def worker() -> None:
            try:
                operations.mark_running(operation.id)
                total = max(1, len(committed.raw_plan.get("items") or []))

                def progress(event: dict[str, Any]) -> None:
                    current = min(total, max(0, int(event.get("index") or 0)))
                    try:
                        operations.update_progress(
                            operation.id,
                            current=current,
                            total=total,
                            message_code="retention." + str(event.get("phase") or "running"),
                        )
                    except ManagementError:
                        pass

                result = self._apply_retention_plan(
                    copy.deepcopy(committed.raw_plan), progress=progress,
                )
                if not result.get("ok"):
                    operations.fail(
                        operation.id,
                        code=ManagementErrorCode.STATE_CONFLICT,
                        message=ManagementErrorCode.STATE_CONFLICT.value,
                    )
                    self._audit(context, "retention.plan.commit", plan_id, "failed")
                    return
                public_result = {
                    "deletedRows": int(result.get("deleted_requests") or 0),
                    "affectedFiles": len(result.get("items") or []),
                    "logicalBytesRemoved": int(result.get("logical_bytes_removed") or 0),
                    "actualFreeBytes": int(result.get("actual_free_bytes") or 0),
                    "days": int(result.get("days") or 0),
                    "policyActivated": bool(result.get("config_saved")),
                }
                if result.get("scope") == RetentionScope.BODIES.value:
                    # Body deletion runs in the background runner; its progress and
                    # compaction outcome are reported by settings.bodyCleanup.
                    public_result["scope"] = RetentionScope.BODIES.value
                    public_result["affectedFiles"] = len(committed.raw_plan.get("items") or [])
                    public_result["backgroundCleanupStarted"] = bool(result.get("started"))
                else:
                    not_compacted = [
                        {"month": str(row.get("month") or ""),
                         "reason": str(row.get("compaction_reason") or "")}
                        for row in result.get("items") or []
                        if row.get("action") == "trim_and_vacuum" and not row.get("compacted")
                        and row.get("compaction_reason")
                    ]
                    if not_compacted:
                        public_result["notCompacted"] = not_compacted
                operations.succeed(operation.id, public_result)
                self._audit(context, "retention.plan.commit", plan_id, "succeeded")
            except Exception:
                try:
                    operations.fail(
                        operation.id,
                        code=ManagementErrorCode.DEPENDENCY_UNAVAILABLE,
                        message=ManagementErrorCode.DEPENDENCY_UNAVAILABLE.value,
                        retryable=True,
                    )
                except ManagementError:
                    pass
                self._audit(context, "retention.plan.commit", plan_id, "failed")

        try:
            if self._start_worker is not None:
                self._start_worker(worker)
            else:
                operations.submit(operation.id, worker)
        except Exception as exc:
            operations.fail_if_active(
                operation.id,
                code=ManagementErrorCode.DEPENDENCY_UNAVAILABLE,
                retryable=True,
            )
            self._audit(context, "retention.plan.commit", plan_id, "failed")
            raise ManagementError(
                ManagementErrorCode.DEPENDENCY_UNAVAILABLE,
                retryable=True,
                operation_id=operation.id,
            ) from exc
        return operation

    def cancel_plan(self, context: ManagementContext, plan_id: str) -> None:
        require(context, Capability.DESTRUCTIVE)
        with self._lock:
            plan = self._get(context, plan_id)
            if plan.state is not PlanState.PREPARED:
                raise ManagementError(ManagementErrorCode.STATE_CONFLICT)
            self._plans[plan_id] = replace(plan, state=PlanState.CANCELLED)
        self._audit(context, "retention.plan.cancel", plan_id, "cancelled")


DEFAULT_RETENTION_CONTROL = RetentionControl()
