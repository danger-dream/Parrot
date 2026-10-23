"""Application update settings and staged update control."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import secrets
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol

from src import __version__, update_checker, updater
from src.management_auth.principal import Capability

from ..context import AuditSink, ManagementContext
from ..errors import ManagementError, ManagementErrorCode
from ..operations import ManagementOperation, OperationRegistry, OperationStore
from .update_state import UpdateStateMixin
from .common import (
    ConfigGateway,
    ModuleConfigGateway,
    audit,
    ensure_revision,
    invalid_field,
    require,
    revision_for,
    rfc3339_utc,
    string_list,
)
from .update_failure_log import sanitize_update_failure_log


STAGE_IDLE = "idle"
STAGE_BACKING_UP = "backing_up"
STAGE_PULLING = "pulling"
STAGE_STAGED = "staged"
STAGE_RESTARTING = "restarting"
STAGE_VERIFYING = "verifying"
STAGE_SUCCESS = "success"
STAGE_FAILED = "failed"
STAGE_ROLLED_BACK = "rolled_back"
_ACTIVE_STAGES = {STAGE_BACKING_UP, STAGE_PULLING, STAGE_RESTARTING, STAGE_VERIFYING}
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")


@dataclass(frozen=True, slots=True)
class UpdateSettings:
    enabled: bool
    include_prerelease: bool
    auto_update: bool
    interval_seconds: int
    ignored_versions: tuple[str, ...]
    revision: str


@dataclass(frozen=True, slots=True)
class UpdateState:
    stage: str
    mode: str
    from_version: str | None
    to_version: str | None
    target_version: str | None
    message: str | None
    revision: str


@dataclass(frozen=True, slots=True)
class UpdateCheckResult:
    current_version: str
    candidate_version: str | None
    candidate_name: str | None
    changelog: str | None
    published_at: str | None
    prerelease: bool
    release_url: str | None
    newer: bool
    ignored: bool
    revision: str


@dataclass(frozen=True, slots=True)
class UpdateBackup:
    ref: str
    version: str
    target_version: str
    mode: str
    created_at: str | None
    revision: str


@dataclass(frozen=True, slots=True)
class UpdateBackupPage:
    items: tuple[UpdateBackup, ...]
    page: int
    page_size: int
    total: int
    has_next: bool


@dataclass(frozen=True, slots=True)
class UpdateFailureLog:
    content: str
    revision: str


@dataclass(frozen=True, slots=True)
class StagedUpdateSubmission:
    operation: ManagementOperation
    activation_plan_token: str | None


@dataclass(slots=True)
class _ActivationPlan:
    actor_key: str
    target_version: str
    expires_at: datetime
    revision: str | None = None
    client_revision: str | None = None
    ready: bool = False
    consumed: bool = False


class UpdateGateway(Protocol):
    @property
    def current_version(self) -> str: ...
    def cached_release(self) -> dict[str, Any]: ...
    def is_newer(self, version: str | None) -> bool: ...
    def check_latest(self) -> None: ...
    def force_refresh(self) -> None: ...
    def add_ignored(self, version: str) -> None: ...
    def remove_ignored(self, version: str) -> None: ...
    def clear_ignored(self) -> None: ...
    def mode(self) -> str: ...
    def state(self) -> dict[str, Any]: ...
    def busy(self) -> bool: ...
    def backups(self) -> list[dict[str, Any]]: ...
    def failure_log(self) -> str: ...
    def set_progress(self, callback: Callable[[str, str], None] | None) -> None: ...
    def stage(self, version: str, *, chat_id: int | None = None, notify_msg_id: int | None = None) -> tuple[bool, str]: ...
    def save_state(self, **fields: Any) -> None: ...
    def activate(self) -> tuple[bool, str]: ...
    def cancel(self) -> tuple[bool, str]: ...
    def activation_is_terminal(self) -> bool: ...


class ModuleUpdateGateway:
    @property
    def current_version(self) -> str:
        return __version__

    def cached_release(self) -> dict[str, Any]:
        return update_checker.get_cached() or {}

    def is_newer(self, version: str | None) -> bool:
        return update_checker._has_newer(version)

    def check_latest(self) -> None:
        update_checker._check_once(push=False, raise_on_error=True, manual=True)

    def force_refresh(self) -> None:
        update_checker.force_refresh_sync()

    def add_ignored(self, version: str) -> None:
        update_checker.add_ignored(version)

    def remove_ignored(self, version: str) -> None:
        update_checker.remove_ignored(version)

    def clear_ignored(self) -> None:
        update_checker.clear_ignored()

    def mode(self) -> str:
        return updater.get_mode()

    def state(self) -> dict[str, Any]:
        return updater.load_state()

    def busy(self) -> bool:
        return updater.is_busy()

    def backups(self) -> list[dict[str, Any]]:
        return updater.list_backups()

    def failure_log(self) -> str:
        return updater.get_update_log()

    def set_progress(self, callback: Callable[[str, str], None] | None) -> None:
        updater.set_progress_callback(callback)

    def stage(self, version: str, *, chat_id: int | None = None, notify_msg_id: int | None = None) -> tuple[bool, str]:
        return updater.stage_update(version, chat_id=chat_id, notify_msg_id=notify_msg_id)

    def save_state(self, **fields: Any) -> None:
        updater.save_state(**fields)

    def activate(self) -> tuple[bool, str]:
        return updater.confirm_restart()

    def cancel(self) -> tuple[bool, str]:
        return updater.cancel_staged()

    def activation_is_terminal(self) -> bool:
        # The production restart only becomes terminal after this process exits and
        # the new process completes health verification/rollback.
        return False


Scheduler = Callable[[Callable[[], None], str], None]
Clock = Callable[[], datetime]


def _actor_key(context: ManagementContext) -> str:
    return context.actor.session_id or context.actor.subject_id


class UpdateControl(UpdateStateMixin):
    STAGE_KIND = "updates.stage"
    ACTIVATE_KIND = "updates.activate"

    def __init__(
        self,
        *,
        config_gateway: ConfigGateway | None = None,
        update_gateway: UpdateGateway | None = None,
        audit_sink: AuditSink | None = None,
        scheduler: Scheduler | None = None,
        clock: Clock | None = None,
        plan_ttl_seconds: int = 600,
    ) -> None:
        self._config = config_gateway or ModuleConfigGateway()
        self._updates = update_gateway or ModuleUpdateGateway()
        self._audit_sink = audit_sink
        self._scheduler = scheduler
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._plan_ttl = timedelta(seconds=plan_ttl_seconds)
        self._operation_store: OperationStore | None = None
        self._operation_registry: OperationRegistry | None = None
        self._bound_registries: set[int] = set()
        self._plans: dict[str, _ActivationPlan] = {}
        self._active_plan_digest: str | None = None
        self._idempotency: OrderedDict[tuple[str, str, str], tuple[str, str]] = OrderedDict()
        self._lock = threading.RLock()

    @property
    def current_version(self) -> str:
        return self._updates.current_version

    def bind_operations(self, store: OperationStore, registry_: OperationRegistry) -> None:
        identity = id(registry_)
        if identity not in self._bound_registries:
            registry_.register(self.STAGE_KIND, self._start_stage)
            registry_.register(self.ACTIVATE_KIND, self._start_activate)
            self._bound_registries.add(identity)
        self._operation_store = store
        self._operation_registry = registry_

    @staticmethod
    def _effective(root: dict[str, Any]) -> dict[str, Any]:
        raw = root.get("updateChecker") or {}
        return {
            "enabled": bool(raw.get("enabled", True)),
            "includePrerelease": bool(raw.get("includePrerelease", True)),
            "autoUpdate": bool(raw.get("autoUpdate", False)),
            "intervalSeconds": int(raw.get("intervalSeconds", 3600) or 3600),
            "ignoredVersions": string_list(raw.get("ignoredVersions")),
        }

    @staticmethod
    def _settings_dto(value: dict[str, Any]) -> UpdateSettings:
        stable = {
            "enabled": bool(value["enabled"]),
            "includePrerelease": bool(value["includePrerelease"]),
            "autoUpdate": bool(value["autoUpdate"]),
            "intervalSeconds": int(value["intervalSeconds"]),
            "ignoredVersions": string_list(value["ignoredVersions"]),
        }
        return UpdateSettings(
            enabled=stable["enabled"],
            include_prerelease=stable["includePrerelease"],
            auto_update=stable["autoUpdate"],
            interval_seconds=stable["intervalSeconds"],
            ignored_versions=tuple(stable["ignoredVersions"]),
            revision=revision_for(stable),
        )

    def get_settings(self, context: ManagementContext) -> UpdateSettings:
        require(context, Capability.READ)
        return self._settings_dto(self._effective(self._config.get()))

    @staticmethod
    def _validate_version(version: str, path: str = "version") -> str:
        normalized = str(version or "").strip()
        if not _VERSION_RE.fullmatch(normalized):
            raise invalid_field(path, "INVALID_VERSION", "version contains unsupported characters")
        return normalized

    def update_settings(
        self,
        context: ManagementContext,
        patch: dict[str, Any],
        *,
        expected_revision: str | None = None,
    ) -> UpdateSettings:
        require(context, Capability.WRITE)
        value = copy.deepcopy(patch)
        if "intervalSeconds" in value:
            interval = value["intervalSeconds"]
            if not isinstance(interval, int) or isinstance(interval, bool) or not 300 <= interval <= 604800:
                raise invalid_field("intervalSeconds", "OUT_OF_RANGE", "must be between 300 and 604800")
        if "ignoredVersions" in value:
            versions = string_list(value["ignoredVersions"])
            value["ignoredVersions"] = [self._validate_version(item, "ignoredVersions") for item in versions]

        def mutate(root: dict[str, Any]) -> None:
            current = self._settings_dto(self._effective(root))
            ensure_revision(expected_revision, current.revision)
            root.setdefault("updateChecker", {}).update(copy.deepcopy(value))

        self._config.update(mutate)
        audit(self._audit_sink, context, action="updates.settings.update", target="updates")
        return self._settings_dto(self._effective(self._config.get()))

    def check(self, context: ManagementContext) -> UpdateCheckResult:
        require(context, Capability.WRITE)
        try:
            self._updates.check_latest()
        except Exception as exc:
            audit(self._audit_sink, context, action="updates.check", target="updates", result="failed")
            raise ManagementError(ManagementErrorCode.UPSTREAM_ERROR, retryable=True) from exc
        result = self.cached_check(context)
        audit(self._audit_sink, context, action="updates.check", target="updates")
        return result

    def refresh_direct(self, context: ManagementContext) -> None:
        require(context, Capability.WRITE)
        self._updates.force_refresh()
        audit(self._audit_sink, context, action="updates.check", target="updates")

    def set_settings_direct(self, context: ManagementContext, patch: dict[str, Any]) -> None:
        require(context, Capability.WRITE)

        def mutate(root: dict[str, Any]) -> None:
            root.setdefault("updateChecker", {}).update(copy.deepcopy(patch))

        self._config.update(mutate)
        audit(self._audit_sink, context, action="updates.settings.update", target="updates")

    def repository(self, context: ManagementContext) -> str:
        require(context, Capability.READ)
        raw = self._config.get().get("updateChecker") or {}
        return str(raw.get("repo") or "danger-dream/Parrot")

    def cached_check(self, context: ManagementContext) -> UpdateCheckResult:
        require(context, Capability.READ)
        cached = self._updates.cached_release()
        candidate = cached.get("latest_version")
        settings = self.get_settings(context)
        stable = {
            "currentVersion": self._updates.current_version,
            "candidateVersion": candidate,
            "candidateName": cached.get("latest_name"),
            "changelog": cached.get("latest_body"),
            "publishedAt": rfc3339_utc(cached.get("latest_published_at")),
            "prerelease": bool(cached.get("latest_prerelease")),
            "releaseUrl": cached.get("latest_url"),
            "newer": self._updates.is_newer(candidate),
            "ignored": bool(candidate and candidate in settings.ignored_versions),
        }
        return UpdateCheckResult(
            current_version=stable["currentVersion"],
            candidate_version=stable["candidateVersion"],
            candidate_name=stable["candidateName"],
            changelog=stable["changelog"],
            published_at=stable["publishedAt"],
            prerelease=stable["prerelease"],
            release_url=stable["releaseUrl"],
            newer=stable["newer"],
            ignored=stable["ignored"],
            revision=revision_for(stable),
        )

    def ignore_version(
        self,
        context: ManagementContext,
        version: str,
        *,
        expected_revision: str | None = None,
    ) -> UpdateSettings:
        require(context, Capability.WRITE)
        version = self._validate_version(version)
        ensure_revision(expected_revision, self.get_settings(context).revision)
        self._updates.add_ignored(version)
        audit(self._audit_sink, context, action="updates.version.ignore", target=version)
        return self._settings_dto(self._effective(self._config.get()))

    def unignore_version(
        self,
        context: ManagementContext,
        version: str,
        *,
        expected_revision: str | None = None,
    ) -> UpdateSettings:
        require(context, Capability.WRITE)
        version = self._validate_version(version)
        ensure_revision(expected_revision, self.get_settings(context).revision)
        if version not in self.get_settings(context).ignored_versions:
            raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
        self._updates.remove_ignored(version)
        audit(self._audit_sink, context, action="updates.version.unignore", target=version)
        return self._settings_dto(self._effective(self._config.get()))

    def ignore_direct(self, context: ManagementContext, version: str) -> None:
        require(context, Capability.WRITE)
        self._updates.add_ignored(version)
        audit(self._audit_sink, context, action="updates.version.ignore", target=version)

    def unignore_direct(self, context: ManagementContext, version: str) -> None:
        require(context, Capability.WRITE)
        self._updates.remove_ignored(version)
        audit(self._audit_sink, context, action="updates.version.unignore", target=version)

    def clear_ignored_direct(self, context: ManagementContext) -> None:
        require(context, Capability.WRITE)
        self._updates.clear_ignored()
        audit(self._audit_sink, context, action="updates.versions.clear", target="ignored-versions")

    @staticmethod
    def _backup(row: dict[str, Any]) -> UpdateBackup:
        stable = {
            "ref": str(row.get("ref") or ""),
            "version": str(row.get("version") or ""),
            "targetVersion": str(row.get("target_tag") or ""),
            "mode": str(row.get("mode") or ""),
            "createdAt": rfc3339_utc(row.get("ts"), compact=True),
        }
        return UpdateBackup(
            ref=stable["ref"],
            version=stable["version"],
            target_version=stable["targetVersion"],
            mode=stable["mode"],
            created_at=stable["createdAt"],
            revision=revision_for(stable),
        )

    def list_backups(
        self,
        context: ManagementContext,
        *,
        mode: str | None = None,
        sort: str = "createdAtDesc",
        page: int = 1,
        page_size: int = 50,
    ) -> UpdateBackupPage:
        require(context, Capability.READ)
        if mode is not None and mode not in {"docker", "systemd", "bare", "src"}:
            raise invalid_field("mode", "UNKNOWN_MODE", "unsupported update mode")
        rows = [self._backup(row) for row in self._updates.backups()]
        if mode is not None:
            rows = [row for row in rows if row.mode == mode]
        rows.sort(key=lambda row: (row.created_at or "", row.ref), reverse=sort == "createdAtDesc")
        total = len(rows)
        start = (page - 1) * page_size
        return UpdateBackupPage(
            items=tuple(rows[start : start + page_size]),
            page=page,
            page_size=page_size,
            total=total,
            has_next=start + page_size < total,
        )

    def backups_raw(self, context: ManagementContext) -> list[dict[str, Any]]:
        require(context, Capability.READ)
        return self._updates.backups()

    def failure_log(self, context: ManagementContext) -> UpdateFailureLog:
        require(context, Capability.LOG_BODY_READ)
        content = sanitize_update_failure_log(self._updates.failure_log())
        audit(self._audit_sink, context, action="updates.failure-log.read", target="update-failure-log")
        return UpdateFailureLog(content=content, revision=revision_for({"content": content}))

    def failure_log_raw(self, context: ManagementContext) -> str:
        require(context, Capability.READ)
        return self._updates.failure_log()

    def state_raw(self, context: ManagementContext) -> dict[str, Any]:
        require(context, Capability.READ)
        return self._updates.state()

    def state(self, context: ManagementContext) -> UpdateState:
        require(context, Capability.READ)
        row = self._updates.state()
        stable = {
            "stage": str(row.get("stage") or STAGE_IDLE),
            "mode": str(row.get("mode") or self._updates.mode()),
            "fromVersion": row.get("from_version"),
            "toVersion": row.get("to_version"),
            "targetVersion": row.get("target_tag"),
            "message": row.get("message"),
        }
        return UpdateState(
            stage=stable["stage"],
            mode=stable["mode"],
            from_version=stable["fromVersion"],
            to_version=stable["toVersion"],
            target_version=stable["targetVersion"],
            message=stable["message"],
            revision=revision_for(stable),
        )

    def is_newer(self, context: ManagementContext, version: str | None) -> bool:
        require(context, Capability.READ)
        return self._updates.is_newer(version)

    def is_busy(self, context: ManagementContext) -> bool:
        require(context, Capability.READ)
        return self._updates.busy()

    def cached_release(self, context: ManagementContext) -> dict[str, Any]:
        require(context, Capability.READ)
        return self._updates.cached_release()

    def mode(self, context: ManagementContext) -> str:
        require(context, Capability.READ)
        return self._updates.mode()

    def stage_direct(
        self,
        context: ManagementContext,
        version: str,
        *,
        progress: Callable[[str, str], None],
        chat_id: int,
        notify_msg_id: int,
    ) -> tuple[bool, str]:
        require(context, Capability.UPDATE)
        with self._lock:
            self._updates.set_progress(progress)
            try:
                return self._updates.stage(version, chat_id=chat_id, notify_msg_id=notify_msg_id)
            finally:
                self._updates.set_progress(None)

    def activate_direct(
        self,
        context: ManagementContext,
        *,
        chat_id: int,
        notify_msg_id: int,
        before_activate: Callable[[], None],
    ) -> tuple[bool, str]:
        require(context, Capability.UPDATE)
        with self._lock:
            active_digest = self._active_plan_digest
            self._updates.save_state(chat_id=chat_id, notify_msg_id=notify_msg_id)
            before_activate()
            result = self._updates.activate()
            if result[0]:
                self._retire_plan(active_digest)
            return result

    def cancel_direct(self, context: ManagementContext) -> tuple[bool, str]:
        require(context, Capability.UPDATE)
        with self._lock:
            active_digest = self._active_plan_digest
            result = self._updates.cancel()
            self._retire_plan_after_cancel(active_digest, ok=result[0])
            return result

    def _idempotent_existing(
        self,
        context: ManagementContext,
        *,
        kind: str,
        fingerprint: str,
    ) -> ManagementOperation | None:
        if not context.idempotency_key:
            raise invalid_field("Idempotency-Key", "REQUIRED", "Idempotency-Key header is required")
        key = (_actor_key(context), kind, context.idempotency_key)
        with self._lock:
            existing = self._idempotency.get(key)
        if existing is None:
            return None
        previous_fingerprint, operation_id = existing
        if previous_fingerprint != fingerprint:
            raise ManagementError(ManagementErrorCode.STATE_CONFLICT)
        if self._operation_store is None:
            raise ManagementError(ManagementErrorCode.SERVICE_NOT_READY, retryable=True)
        return self._operation_store.get(context, operation_id)

    def _remember_idempotency(
        self,
        context: ManagementContext,
        *,
        kind: str,
        fingerprint: str,
        operation_id: str,
    ) -> None:
        key = (_actor_key(context), kind, context.idempotency_key or "")
        with self._lock:
            self._idempotency[key] = (fingerprint, operation_id)
            self._idempotency.move_to_end(key)
            while len(self._idempotency) > 500:
                self._idempotency.popitem(last=False)

    def stage_update(self, context: ManagementContext, version: str) -> StagedUpdateSubmission:
        require(context, Capability.UPDATE)
        version = self._validate_version(version)
        fingerprint = hashlib.sha256(version.encode()).hexdigest()
        with self._lock:
            existing = self._idempotent_existing(context, kind=self.STAGE_KIND, fingerprint=fingerprint)
            if existing is not None:
                return StagedUpdateSubmission(operation=existing, activation_plan_token=None)
            if self._updates.busy():
                raise ManagementError(ManagementErrorCode.OPERATION_ALREADY_RUNNING, retryable=True)
            if self._operation_registry is None:
                raise ManagementError(ManagementErrorCode.SERVICE_NOT_READY, retryable=True)
            token, digest = self._new_plan(
                actor_key=_actor_key(context),
                target_version=version,
            )
            try:
                operation = self._operation_registry.create(
                    context,
                    kind=self.STAGE_KIND,
                    payload={
                        "version": version,
                        "actorKey": _actor_key(context),
                        "planDigest": digest,
                    },
                    cancellable=False,
                )
            except Exception:
                self._plans.pop(digest, None)
                raise
            self._remember_idempotency(
                context,
                kind=self.STAGE_KIND,
                fingerprint=fingerprint,
                operation_id=operation.id,
            )
            return StagedUpdateSubmission(operation=operation, activation_plan_token=token)

    def _new_plan(self, *, actor_key: str, target_version: str) -> tuple[str, str]:
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        self._plans[digest] = _ActivationPlan(
            actor_key=actor_key,
            target_version=target_version,
            expires_at=self._clock() + self._plan_ttl,
        )
        return token, digest

    def _retire_plan(self, digest: str | None) -> None:
        """Make one stage identity permanently unusable without removing history."""
        if digest is None:
            return
        plan = self._plans.get(digest)
        if plan is not None:
            plan.consumed = True
        if self._active_plan_digest == digest:
            self._active_plan_digest = None

    def _retire_plan_after_cancel(self, digest: str | None, *, ok: bool) -> None:
        """Retire a plan whenever cancel has actually left the staged state."""
        if ok:
            self._retire_plan(digest)
            return
        try:
            post_state = self._state_without_auth()
        except Exception:
            return
        if post_state.stage != STAGE_STAGED:
            self._retire_plan(digest)

    def _start_stage(self, operation_id: str, context: ManagementContext, payload: Any) -> None:
        store = self._operation_store
        if store is None:
            raise ManagementError(ManagementErrorCode.SERVICE_NOT_READY, retryable=True)

        def fail() -> None:
            with self._lock:
                digest = str(payload["planDigest"])
                self._plans.pop(digest, None)
                if self._active_plan_digest == digest:
                    self._active_plan_digest = None
            store.fail(
                operation_id,
                code=ManagementErrorCode.DEPENDENCY_UNAVAILABLE,
                message=ManagementErrorCode.DEPENDENCY_UNAVAILABLE.value,
                retryable=True,
            )

        def run() -> None:
            store.mark_running(operation_id)
            progress_index = {
                STAGE_BACKING_UP: 1,
                STAGE_PULLING: 2,
                STAGE_STAGED: 3,
            }

            def progress(stage: str, _message: str) -> None:
                current = progress_index.get(stage)
                if current is not None:
                    store.update_progress(
                        operation_id,
                        current=current,
                        total=3,
                        message_code=f"UPDATE_{stage.upper()}",
                    )

            try:
                # The updater owns one process-wide progress callback.  Keep callback
                # install, stage, cleanup, and authoritative state confirmation under
                # the same lock used by the Telegram direct adapter.
                with self._lock:
                    self._updates.set_progress(progress)
                    try:
                        ok, _detail = self._updates.stage(str(payload["version"]))
                    except Exception:
                        ok = False
                    finally:
                        self._updates.set_progress(None)
                    if not ok:
                        raise ManagementError(ManagementErrorCode.DEPENDENCY_UNAVAILABLE)
                    state = self._state_without_auth()
                    digest = str(payload["planDigest"])
                    plan = self._plans.get(digest)
                    if plan is None or plan.actor_key != str(payload["actorKey"]):
                        raise ManagementError(ManagementErrorCode.STATE_CONFLICT)
                    if state.stage != STAGE_STAGED or state.target_version != plan.target_version:
                        raise ManagementError(ManagementErrorCode.INVALID_OPERATION_STATE)
                    self._retire_plan(self._active_plan_digest)
                    plan.revision = state.revision
                    plan.client_revision = state.revision
                    plan.expires_at = self._clock() + self._plan_ttl
                    plan.ready = True
                    self._active_plan_digest = digest
                    expires_at = rfc3339_utc(plan.expires_at)
                    store.succeed(
                        operation_id,
                        {
                            "stagedVersion": payload["version"],
                            "expectedRevision": state.revision,
                            "expiresAt": expires_at,
                        },
                    )
            except Exception:
                fail()

        if self._scheduler is not None:
            self._scheduler(run, "management-update-stage")
        else:
            store.submit(operation_id, run)
        audit(self._audit_sink, context, action="updates.stage", target=operation_id, result="queued")

    def _state_without_auth(self) -> UpdateState:
        row = self._updates.state()
        stable = {
            "stage": str(row.get("stage") or STAGE_IDLE),
            "mode": str(row.get("mode") or self._updates.mode()),
            "fromVersion": row.get("from_version"),
            "toVersion": row.get("to_version"),
            "targetVersion": row.get("target_tag"),
            "message": row.get("message"),
        }
        return UpdateState(
            stage=stable["stage"],
            mode=stable["mode"],
            from_version=stable["fromVersion"],
            to_version=stable["toVersion"],
            target_version=stable["targetVersion"],
            message=stable["message"],
            revision=revision_for(stable),
        )

    def activate_staged(
        self,
        context: ManagementContext,
        *,
        plan_token: str,
        expected_revision: str | None,
    ) -> ManagementOperation:
        require(context, Capability.UPDATE)
        if not expected_revision:
            raise invalid_field("If-Match", "REQUIRED", "If-Match header is required")
        if not context.idempotency_key:
            raise invalid_field("Idempotency-Key", "REQUIRED", "Idempotency-Key header is required")
        if self._operation_registry is None:
            raise ManagementError(ManagementErrorCode.SERVICE_NOT_READY, retryable=True)
        digest = hashlib.sha256(str(plan_token or "").encode()).hexdigest()
        idempotency_key = (_actor_key(context), self.ACTIVATE_KIND, context.idempotency_key)
        with self._lock:
            if idempotency_key in self._idempotency:
                raise ManagementError(ManagementErrorCode.STATE_CONFLICT)
            plan = self._plans.get(digest)
            if plan is None or plan.consumed:
                raise ManagementError(ManagementErrorCode.STATE_CONFLICT)
            if self._clock() >= plan.expires_at:
                plan.consumed = True
                raise ManagementError(ManagementErrorCode.STATE_CONFLICT)
            if plan.actor_key != _actor_key(context):
                raise ManagementError(ManagementErrorCode.STATE_CONFLICT)
            if not plan.ready or plan.revision is None:
                raise ManagementError(ManagementErrorCode.INVALID_OPERATION_STATE)
            if self._active_plan_digest != digest:
                plan.consumed = True
                raise ManagementError(ManagementErrorCode.STATE_CONFLICT)
            current = self._state_without_auth()
            if current.stage != STAGE_STAGED or current.target_version != plan.target_version:
                raise ManagementError(ManagementErrorCode.INVALID_OPERATION_STATE)
            # A synchronous restart guard may update only the staged message/revision.
            # The original response revision remains valid solely with this still-live,
            # actor-bound plan; the plan itself always tracks authoritative state.
            if expected_revision not in {current.revision, plan.client_revision}:
                ensure_revision(expected_revision, current.revision)
            ensure_revision(plan.revision, current.revision)
            plan.consumed = True
            try:
                operation = self._operation_registry.create(
                    context,
                    kind=self.ACTIVATE_KIND,
                    payload={"targetVersion": plan.target_version, "planDigest": digest},
                    cancellable=False,
                )
            except Exception:
                plan.consumed = False
                raise
            self._idempotency[idempotency_key] = (digest, operation.id)
            self._idempotency.move_to_end(idempotency_key)
            while len(self._idempotency) > 500:
                self._idempotency.popitem(last=False)
        audit(self._audit_sink, context, action="updates.activate", target=operation.id, result="queued")
        return operation

    def _rearm_failed_activation(self, digest: str, target_version: str) -> bool:
        """Restore only the same still-staged plan after a synchronous rejection."""
        plan = self._plans.get(digest)
        if plan is None or self._active_plan_digest != digest or not plan.consumed:
            return False
        try:
            state = self._state_without_auth()
        except Exception:
            self._retire_plan(digest)
            return False
        if state.stage != STAGE_STAGED or state.target_version != target_version:
            self._retire_plan(digest)
            return False
        plan.revision = state.revision
        plan.expires_at = self._clock() + self._plan_ttl
        plan.ready = True
        plan.consumed = False
        return True

    def _start_activate(self, operation_id: str, _context: ManagementContext, payload: Any) -> None:
        store = self._operation_store
        if store is None:
            raise ManagementError(ManagementErrorCode.SERVICE_NOT_READY, retryable=True)
        digest = str(payload["planDigest"])
        target_version = str(payload["targetVersion"])

        def finish_success() -> None:
            try:
                store.update_progress(
                    operation_id,
                    current=2,
                    total=2,
                    message_code="UPDATE_HEALTH_VERIFIED",
                )
                store.succeed(operation_id, {"activated": True})
            except ManagementError:
                # Orderly owner shutdown may already have interrupted the record.
                return

        def run() -> None:
            store.mark_running(operation_id)
            store.update_progress(
                operation_id,
                current=1,
                total=2,
                message_code="UPDATE_RESTARTING",
            )
            with self._lock:
                plan = self._plans.get(digest)
                if plan is None or self._active_plan_digest != digest or not plan.consumed:
                    store.fail_if_active(
                        operation_id,
                        code=ManagementErrorCode.INVALID_OPERATION_STATE,
                        retryable=False,
                    )
                    return
                try:
                    ok, _detail = self._updates.activate()
                except Exception:
                    ok = False
                if not ok:
                    retryable = self._rearm_failed_activation(digest, target_version)
                    try:
                        store.fail(
                            operation_id,
                            code=ManagementErrorCode.DEPENDENCY_UNAVAILABLE,
                            message=ManagementErrorCode.DEPENDENCY_UNAVAILABLE.value,
                            retryable=retryable,
                        )
                    except ManagementError:
                        self._retire_plan(digest)
                    return
                # Restart acceptance is irreversible for this plan, but is not success.
                self._retire_plan(digest)
            if self._updates.activation_is_terminal():
                finish_success()
            # Production acceptance is not completion. Release this worker and
            # let owner shutdown interrupt the Operation; the new process's
            # health/rollback and a new Session are the recovery authority.

        if self._scheduler is not None:
            self._scheduler(run, "management-update-activate")
        else:
            store.submit(operation_id, run)

    def cancel_staged(
        self,
        context: ManagementContext,
        *,
        expected_revision: str | None = None,
    ) -> None:
        require(context, Capability.UPDATE)
        with self._lock:
            current = self._state_without_auth()
            ensure_revision(expected_revision, current.revision)
            if current.stage != STAGE_STAGED:
                raise ManagementError(ManagementErrorCode.INVALID_OPERATION_STATE)
            active_digest = self._active_plan_digest
            ok, _detail = self._updates.cancel()
            self._retire_plan_after_cancel(active_digest, ok=ok)
            if not ok:
                raise ManagementError(ManagementErrorCode.DEPENDENCY_UNAVAILABLE, retryable=True)
        audit(self._audit_sink, context, action="updates.staged.cancel", target="staged-update")
