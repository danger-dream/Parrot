"""Status-alert settings, incident query and refresh control."""

from __future__ import annotations

from src.safe_diagnostics import diagnose

import copy
from dataclasses import dataclass
from typing import Any, Callable, Literal, Protocol

from src import status_monitor
from src.management_auth.principal import Capability

from ..context import AuditSink, ManagementContext
from ..errors import ManagementError, ManagementErrorCode
from ..operations import ManagementOperation, OperationRegistry, OperationStore
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


STATUS_PROVIDERS = ("claude", "openai", "cloudflare")
STATUS_IMPACTS = ("none", "minor", "major", "critical")


@dataclass(frozen=True, slots=True)
class StatusAlertSettings:
    enabled: bool
    interval_seconds: int
    targets: tuple[str, ...]
    min_impact: str
    notification_enabled: bool
    revision: str


@dataclass(frozen=True, slots=True)
class StatusIncident:
    id: str
    provider: str
    name: str
    impact: str
    status: str
    created_at: str | None
    updated_at: str | None
    shortlink: str | None
    muted: bool
    active: bool
    muted_at: str | None
    revision: str


@dataclass(frozen=True, slots=True)
class StatusIncidentPage:
    items: tuple[StatusIncident, ...]
    page: int
    page_size: int
    total: int
    has_next: bool


class StatusGateway(Protocol):
    def snapshot_active(self) -> dict[str, list[dict[str, Any]]]: ...
    def list_muted(self) -> list[dict[str, Any]]: ...
    def forget_provider(self, provider: str) -> None: ...
    def refresh_provider(self, provider: str, *, raise_on_error: bool = False) -> None: ...
    def list_recent(self, provider: str, limit: int, *, raise_on_error: bool = False) -> list[dict[str, Any]]: ...
    def mute(self, provider: str, incident_id: str, name: str = "") -> None: ...
    def unmute(self, provider: str, incident_id: str) -> None: ...
    def provider_tag(self, provider: str) -> str: ...
    def provider_label(self, provider: str) -> str: ...
    def impact_icon(self, impact: str) -> str: ...
    def status_icon(self, status: str) -> str: ...


class ModuleStatusGateway:
    def snapshot_active(self) -> dict[str, list[dict[str, Any]]]:
        return status_monitor.snapshot_active()

    def list_muted(self) -> list[dict[str, Any]]:
        return status_monitor.list_muted()

    def forget_provider(self, provider: str) -> None:
        status_monitor.forget_provider(provider)

    def refresh_provider(self, provider: str, *, raise_on_error: bool = False) -> None:
        first = provider not in status_monitor._initialized_providers
        if raise_on_error:
            snapshot = status_monitor._process_provider(provider, push=not first, raise_on_error=True)
        else:
            snapshot = status_monitor._process_provider(provider, push=not first)
        if snapshot is None:
            raise RuntimeError("Status refresh failed")
        status_monitor._initialized_providers.add(provider)

    def list_recent(self, provider: str, limit: int, *, raise_on_error: bool = False) -> list[dict[str, Any]]:
        if raise_on_error:
            return status_monitor.list_recent_incidents(provider, limit=limit, raise_on_error=True)
        return status_monitor.list_recent_incidents(provider, limit=limit)

    def mute(self, provider: str, incident_id: str, name: str = "") -> None:
        status_monitor.mute_incident(provider, incident_id, name=name)

    def unmute(self, provider: str, incident_id: str) -> None:
        status_monitor.unmute_incident(provider, incident_id)

    def provider_tag(self, provider: str) -> str:
        return status_monitor._provider_tag(provider)

    def provider_label(self, provider: str) -> str:
        return status_monitor._provider_label(provider)

    def impact_icon(self, impact: str) -> str:
        return status_monitor._IMPACT_ICON.get(impact, "📡")

    def status_icon(self, status: str) -> str:
        return status_monitor._STATUS_ICON.get(status, "")


Scheduler = Callable[[Callable[[], None], str], None]


class StatusAlertControl:
    REFRESH_KIND = "status-alerts.refresh"

    def __init__(
        self,
        *,
        config_gateway: ConfigGateway | None = None,
        status_gateway: StatusGateway | None = None,
        audit_sink: AuditSink | None = None,
        scheduler: Scheduler | None = None,
    ) -> None:
        self._config = config_gateway or ModuleConfigGateway()
        self._status = status_gateway or ModuleStatusGateway()
        self._audit_sink = audit_sink
        self._scheduler = scheduler
        self._operation_store: OperationStore | None = None
        self._operation_registry: OperationRegistry | None = None
        self._bound_registries: set[int] = set()

    def bind_operations(self, store: OperationStore, registry_: OperationRegistry) -> None:
        identity = id(registry_)
        if identity not in self._bound_registries:
            registry_.register(self.REFRESH_KIND, self._start_refresh)
            self._bound_registries.add(identity)
        self._operation_store = store
        self._operation_registry = registry_

    @staticmethod
    def _effective(root: dict[str, Any]) -> dict[str, Any]:
        raw = root.get("statusMonitor") or {}
        targets = raw.get("targets", list(STATUS_PROVIDERS))
        notifications = root.get("notifications") or {}
        events = notifications.get("events") or {}
        return {
            "enabled": bool(raw.get("enabled", True)),
            "intervalSeconds": int(raw.get("intervalSeconds", 60) or 60),
            "targets": string_list(targets),
            "minImpact": str(raw.get("minImpact") or "minor").lower(),
            "notificationEnabled": bool(events.get("status_alert", True)),
        }

    @staticmethod
    def _dto(value: dict[str, Any]) -> StatusAlertSettings:
        stable = {
            "enabled": bool(value["enabled"]),
            "intervalSeconds": int(value["intervalSeconds"]),
            "targets": string_list(value["targets"]),
            "minImpact": str(value["minImpact"]),
            "notificationEnabled": bool(value["notificationEnabled"]),
        }
        return StatusAlertSettings(
            enabled=stable["enabled"],
            interval_seconds=stable["intervalSeconds"],
            targets=tuple(stable["targets"]),
            min_impact=stable["minImpact"],
            notification_enabled=stable["notificationEnabled"],
            revision=revision_for(stable),
        )

    def get_settings(self, context: ManagementContext) -> StatusAlertSettings:
        require(context, Capability.READ)
        return self._dto(self._effective(self._config.get()))

    def _write_settings(
        self,
        context: ManagementContext,
        patch: dict[str, Any],
        *,
        expected_revision: str | None,
    ) -> None:
        value = copy.deepcopy(patch)
        if "intervalSeconds" in value:
            interval = value["intervalSeconds"]
            if not isinstance(interval, int) or isinstance(interval, bool) or interval < 10:
                raise invalid_field("intervalSeconds", "OUT_OF_RANGE", "must be at least 10")
        if "targets" in value:
            targets = string_list(value["targets"])
            unknown = [item for item in targets if item not in STATUS_PROVIDERS]
            if unknown:
                raise invalid_field("targets", "UNKNOWN_PROVIDER", f"unsupported provider: {unknown[0]}")
            value["targets"] = targets
        if "minImpact" in value and value["minImpact"] not in STATUS_IMPACTS:
            raise invalid_field("minImpact", "UNKNOWN_IMPACT", "unsupported impact")
        removed: list[str] = []

        def mutate(root: dict[str, Any]) -> None:
            current = self._dto(self._effective(root))
            ensure_revision(expected_revision, current.revision)
            previous = set(current.targets)
            section = root.setdefault("statusMonitor", {})
            section.update(copy.deepcopy(value))
            if "targets" in value:
                removed.extend(sorted(previous - set(value["targets"])))

        self._config.update(mutate)
        for provider in removed:
            self._status.forget_provider(provider)
        audit(self._audit_sink, context, action="status-alerts.settings.update", target="status-alerts")

    def update_settings(
        self,
        context: ManagementContext,
        patch: dict[str, Any],
        *,
        expected_revision: str | None = None,
    ) -> StatusAlertSettings:
        require(context, Capability.WRITE)
        self._write_settings(context, patch, expected_revision=expected_revision)
        return self._dto(self._effective(self._config.get()))

    def update_settings_direct(self, context: ManagementContext, patch: dict[str, Any]) -> None:
        """TG compatibility write without a post-commit DTO read-back."""
        require(context, Capability.WRITE)
        self._write_settings(context, patch, expected_revision=None)

    def _toggle_target(self, context: ManagementContext, provider: str) -> bool:
        if provider not in STATUS_PROVIDERS:
            raise invalid_field("provider", "UNKNOWN_PROVIDER", "unsupported provider")
        current = self.get_settings(context)
        targets = list(current.targets)
        removed = provider in targets
        if removed:
            targets.remove(provider)
            self._status.forget_provider(provider)
        else:
            targets.append(provider)

        def mutate(root: dict[str, Any]) -> None:
            root.setdefault("statusMonitor", {})["targets"] = targets

        self._config.update(mutate)
        audit(self._audit_sink, context, action="status-alerts.target.toggle", target=provider)
        return removed

    def toggle_target(self, context: ManagementContext, provider: str) -> tuple[StatusAlertSettings, bool]:
        """Toggle a target and return the Management DTO plus removal flag."""
        require(context, Capability.WRITE)
        removed = self._toggle_target(context, provider)
        return self.get_settings(context), removed

    def toggle_target_direct(self, context: ManagementContext, provider: str) -> bool:
        """TG compatibility toggle without a post-commit DTO read-back."""
        require(context, Capability.WRITE)
        return self._toggle_target(context, provider)

    def snapshot_active(self, context: ManagementContext) -> dict[str, list[dict[str, Any]]]:
        require(context, Capability.READ)
        return self._status.snapshot_active()

    def list_muted_raw(self, context: ManagementContext) -> list[dict[str, Any]]:
        require(context, Capability.READ)
        return self._status.list_muted()

    def provider_tag(self, context: ManagementContext, provider: str) -> str:
        require(context, Capability.READ)
        return self._status.provider_tag(provider)

    def provider_label(self, context: ManagementContext, provider: str) -> str:
        require(context, Capability.READ)
        return self._status.provider_label(provider)

    def impact_icon(self, context: ManagementContext, impact: str) -> str:
        require(context, Capability.READ)
        return self._status.impact_icon(impact)

    def status_icon(self, context: ManagementContext, status: str) -> str:
        require(context, Capability.READ)
        return self._status.status_icon(status)

    def refresh_direct(self, context: ManagementContext) -> int:
        require(context, Capability.WRITE)
        targets = self.get_settings(context).targets
        failure: Exception | None = None
        for provider in targets:
            if provider in STATUS_PROVIDERS:
                try:
                    self._status.refresh_provider(provider)
                except Exception as exc:
                    # TG refresh remains best effort across all selected targets.
                    # Report failure after the pass without consuming failed prime
                    # or replacing that provider's last valid snapshot.
                    if failure is None:
                        failure = exc
        if failure is not None:
            audit(self._audit_sink, context, action="status-alerts.refresh", target="status-alerts", result="failed")
            raise failure
        audit(self._audit_sink, context, action="status-alerts.refresh", target="status-alerts")
        return len(targets)

    def recent_direct(
        self,
        context: ManagementContext,
        provider: str,
        *,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        require(context, Capability.READ)
        return self._status.list_recent(provider, limit)

    def recent_by_provider(
        self,
        context: ManagementContext,
        *,
        limit: int = 5,
    ) -> list[tuple[str, list[dict[str, Any]] | Exception]]:
        require(context, Capability.READ)
        rows: list[tuple[str, list[dict[str, Any]] | Exception]] = []
        for provider in self.get_settings(context).targets:
            if provider not in STATUS_PROVIDERS:
                continue
            try:
                rows.append((provider, self._status.list_recent(provider, limit)))
            except Exception as exc:
                rows.append((provider, exc))
        return rows

    @staticmethod
    def _incident_id(row: dict[str, Any]) -> str:
        return str(row.get("id") or row.get("incident_id") or "")

    @classmethod
    def _mute_index(cls, rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
        result: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            provider = str(row.get("provider") or "")
            incident_id = cls._incident_id(row)
            if provider and incident_id:
                result[(provider, incident_id)] = row
        return result

    @classmethod
    def _incident_revision(
        cls,
        provider: str,
        row: dict[str, Any],
        muted_row: dict[str, Any] | None,
    ) -> str:
        # Active/history differ only by presentation, while a muted record is
        # the local authority once the notification relationship exists.  Use
        # that authority as the canonical source so all views and mutations
        # hash the same resource without dropping real feed or mute changes.
        source = muted_row if muted_row is not None else row
        return revision_for({
            "id": cls._incident_id(source),
            "provider": provider,
            "name": str(source.get("name") or ""),
            "impact": str(source.get("impact") or "none").lower(),
            "status": str(source.get("status") or "").lower(),
            "createdAt": rfc3339_utc(source.get("created_at")),
            "updatedAt": rfc3339_utc(source.get("updated_at")),
            "shortlink": source.get("shortlink"),
            "muted": muted_row is not None,
            "mutedAt": rfc3339_utc(muted_row.get("muted_at")) if muted_row is not None else None,
        })

    @classmethod
    def _incident(
        cls,
        provider: str,
        row: dict[str, Any],
        *,
        muted_row: dict[str, Any] | None,
        active: bool,
    ) -> StatusIncident:
        incident_id = cls._incident_id(row)
        stable = {
            "id": incident_id,
            "provider": provider,
            "name": str(row.get("name") or ""),
            "impact": str(row.get("impact") or "none").lower(),
            "status": str(row.get("status") or "").lower(),
            "createdAt": rfc3339_utc(row.get("created_at")),
            "updatedAt": rfc3339_utc(row.get("updated_at")),
            "shortlink": row.get("shortlink"),
            "muted": muted_row is not None,
            "active": active,
            "mutedAt": rfc3339_utc(muted_row.get("muted_at")) if muted_row is not None else None,
        }
        return StatusIncident(
            id=stable["id"],
            provider=stable["provider"],
            name=stable["name"],
            impact=stable["impact"],
            status=stable["status"],
            created_at=stable["createdAt"],
            updated_at=stable["updatedAt"],
            shortlink=stable["shortlink"],
            muted=stable["muted"],
            active=stable["active"],
            muted_at=stable["mutedAt"],
            revision=cls._incident_revision(provider, row, muted_row),
        )

    @diagnose('status', 'query', 'request')
    def list_incidents(
        self,
        context: ManagementContext,
        *,
        view: Literal["active", "history", "muted"],
        provider: str | None,
        impact: str | None,
        sort: Literal["createdAtAsc", "createdAtDesc"],
        page: int,
        page_size: int,
    ) -> StatusIncidentPage:
        require(context, Capability.READ)
        if provider is not None and provider not in STATUS_PROVIDERS:
            raise invalid_field("provider", "UNKNOWN_PROVIDER", "unsupported provider")
        providers = [provider] if provider else list(STATUS_PROVIDERS)
        muted_rows = self._status.list_muted()
        muted_by_incident = self._mute_index(muted_rows)
        rows: list[StatusIncident] = []
        if view == "active":
            snapshot = self._status.snapshot_active()
            for item_provider in providers:
                rows.extend(
                    self._incident(
                        item_provider,
                        item,
                        muted_row=muted_by_incident.get((item_provider, self._incident_id(item))),
                        active=True,
                    )
                    for item in snapshot.get(item_provider, [])
                )
        elif view == "muted":
            for item in muted_rows:
                item_provider = str(item.get("provider") or "")
                if item_provider in providers:
                    rows.append(self._incident(item_provider, item, muted_row=item, active=False))
        else:
            for item_provider in providers:
                try:
                    incidents = self._status.list_recent(item_provider, 200, raise_on_error=True)
                except Exception as exc:
                    raise ManagementError(ManagementErrorCode.UPSTREAM_ERROR, retryable=True) from exc
                rows.extend(
                    self._incident(
                        item_provider,
                        item,
                        muted_row=muted_by_incident.get((item_provider, self._incident_id(item))),
                        active=False,
                    )
                    for item in incidents
                )
        if impact is not None:
            rows = [item for item in rows if item.impact == impact]
        reverse = sort == "createdAtDesc"
        rows.sort(key=lambda item: (item.created_at or "", item.provider, item.id), reverse=reverse)
        total = len(rows)
        start = (page - 1) * page_size
        selected = tuple(rows[start : start + page_size])
        return StatusIncidentPage(
            items=selected,
            page=page,
            page_size=page_size,
            total=total,
            has_next=start + page_size < total,
        )

    def _find_incident(
        self,
        incident_id: str,
        *,
        muted_rows: list[dict[str, Any]] | None = None,
    ) -> tuple[str, dict[str, Any]] | None:
        current_mutes = muted_rows if muted_rows is not None else self._status.list_muted()
        for row in current_mutes:
            if self._incident_id(row) == incident_id:
                return str(row.get("provider") or ""), row
        for provider in STATUS_PROVIDERS:
            for row in self._status.list_recent(provider, 200):
                if self._incident_id(row) == incident_id:
                    return provider, row
        for provider, rows in self._status.snapshot_active().items():
            for row in rows:
                if self._incident_id(row) == incident_id:
                    return provider, row
        return None

    def mute_incident(
        self,
        context: ManagementContext,
        incident_id: str,
        *,
        expected_revision: str | None = None,
    ) -> StatusIncident:
        require(context, Capability.WRITE)
        muted_rows = self._status.list_muted()
        found = self._find_incident(incident_id, muted_rows=muted_rows)
        if found is None:
            raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
        provider, row = found
        muted_row = self._mute_index(muted_rows).get((provider, incident_id))
        current_revision = self._incident_revision(provider, row, muted_row)
        ensure_revision(expected_revision, current_revision)
        self._status.mute(provider, incident_id, str(row.get("name") or ""))
        audit(self._audit_sink, context, action="status-alerts.incident.mute", target=incident_id)
        muted_row = next(
            (
                item for item in self._status.list_muted()
                if str(item.get("provider") or "") == provider
                and str(item.get("incident_id") or item.get("id") or "") == incident_id
            ),
            None,
        )
        if muted_row is None:
            raise ManagementError(ManagementErrorCode.STATE_CONFLICT)
        return self._incident(provider, muted_row, muted_row=muted_row, active=False)

    def mute_direct(self, context: ManagementContext, provider: str, incident_id: str, name: str) -> None:
        require(context, Capability.WRITE)
        self._status.mute(provider, incident_id, name)
        audit(self._audit_sink, context, action="status-alerts.incident.mute", target=incident_id)

    def unmute_incident(
        self,
        context: ManagementContext,
        incident_id: str,
        *,
        expected_revision: str | None = None,
    ) -> None:
        require(context, Capability.WRITE)
        found = next(
            (
                row for row in self._status.list_muted()
                if str(row.get("incident_id") or row.get("id") or "") == incident_id
            ),
            None,
        )
        if found is None:
            raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
        provider = str(found.get("provider") or "")
        ensure_revision(
            expected_revision,
            self._incident_revision(provider, found, found),
        )
        self._status.unmute(provider, incident_id)
        audit(self._audit_sink, context, action="status-alerts.incident.unmute", target=incident_id)

    def unmute_direct(self, context: ManagementContext, provider: str, incident_id: str) -> None:
        require(context, Capability.WRITE)
        self._status.unmute(provider, incident_id)
        audit(self._audit_sink, context, action="status-alerts.incident.unmute", target=incident_id)

    def refresh(self, context: ManagementContext) -> ManagementOperation:
        require(context, Capability.WRITE)
        if self._operation_registry is None:
            raise ManagementError(ManagementErrorCode.SERVICE_NOT_READY, retryable=True)
        return self._operation_registry.create(
            context,
            kind=self.REFRESH_KIND,
            payload={"targets": list(self.get_settings(context).targets)},
            cancellable=False,
        )

    def _start_refresh(self, operation_id: str, context: ManagementContext, payload: Any) -> None:
        store = self._operation_store
        if store is None:
            raise ManagementError(ManagementErrorCode.SERVICE_NOT_READY, retryable=True)

        def run() -> None:
            store.mark_running(operation_id)
            targets = [item for item in payload["targets"] if item in STATUS_PROVIDERS]
            try:
                for index, provider in enumerate(targets, start=1):
                    self._status.refresh_provider(provider, raise_on_error=True)
                    store.update_progress(
                        operation_id,
                        current=index,
                        total=len(targets),
                        message_code="STATUS_PROVIDER_REFRESHED",
                    )
                store.succeed(operation_id, {"refreshedProviders": targets})
            except Exception:
                store.fail(
                    operation_id,
                    code=ManagementErrorCode.UPSTREAM_ERROR,
                    message=ManagementErrorCode.UPSTREAM_ERROR.value,
                    retryable=True,
                )

        if self._scheduler is not None:
            self._scheduler(run, "management-status-alert-refresh")
        else:
            store.submit(operation_id, run)
        audit(self._audit_sink, context, action="status-alerts.refresh", target=operation_id, result="queued")
