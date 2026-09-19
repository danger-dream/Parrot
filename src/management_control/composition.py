"""Lifecycle-owned composition of controls shared by production adapters."""

from __future__ import annotations

from dataclasses import dataclass

from src.management_auth import ManagementStateStore

from .apikey import ApiKeyControl
from .auxiliary import (
    AntigravityMediaControl,
    AuxiliaryControls,
    ImageControl,
    VideoControl,
    StatusAlertControl,
    TranslationControl,
    UpdateControl,
    XaiMediaControl,
)
from .channels import ChannelControl
from .context import AuditSink
from .load_balancing import LoadBalancingControl
from .mapping import MappingControl
from .models import ModelCenterControl
from .network import NetworkControl
from .oauth import OAuthBackend, OAuthControl
from .observability import (
    LogsControl,
    MediaControl,
    RetentionControl,
    StatsControl,
    StatusControl,
)
from .operations import OperationRegistry, OperationStore
from .proxy import ProxyControl
from .system import ContentBlacklistControl, SettingsControl, TelegramRetentionAdapter
from .system.runtime import SystemRuntimeControl


@dataclass(slots=True)
class ObservabilityControls:
    status: StatusControl
    stats: StatsControl
    logs: LogsControl
    media: MediaControl
    retention: RetentionControl


@dataclass(slots=True)
class SystemNetworkControls:
    settings: SettingsControl
    blacklist: ContentBlacklistControl
    network: NetworkControl


@dataclass(frozen=True, slots=True)
class ManagementControls:
    """The one Control owner for a single ManagementRuntime lifecycle."""

    audit_sink: AuditSink
    operations: OperationStore
    operation_registry: OperationRegistry
    oauth: OAuthControl
    channels: ChannelControl
    api_keys: ApiKeyControl
    mapping: MappingControl
    models: ModelCenterControl
    load_balancing: LoadBalancingControl
    proxy: ProxyControl
    observability: ObservabilityControls
    system: SystemNetworkControls
    system_runtime: SystemRuntimeControl
    telegram_retention: TelegramRetentionAdapter
    auxiliary: AuxiliaryControls


def build_management_controls(
    *,
    audit_sink: AuditSink,
    operations: OperationStore,
    operation_registry: OperationRegistry,
    state_store: ManagementStateStore,
) -> ManagementControls:
    """Construct and fully bind one lifecycle's production Control graph."""

    auxiliary = AuxiliaryControls(
        translation=TranslationControl(audit_sink=audit_sink),
        status_alerts=StatusAlertControl(audit_sink=audit_sink),
        updates=UpdateControl(audit_sink=audit_sink),
        images=ImageControl(audit_sink=audit_sink),
        videos=VideoControl(audit_sink=audit_sink),
        xai_media=XaiMediaControl(audit_sink=audit_sink),
        antigravity_media=AntigravityMediaControl(audit_sink=audit_sink),
    )
    auxiliary.bind_operations(operations, operation_registry)
    retention = RetentionControl(audit_sink=audit_sink)
    settings = SettingsControl(audit_sink=audit_sink)
    oauth = OAuthControl(
        backend=OAuthBackend(settings_control=settings), audit_sink=audit_sink,
    )
    channels = ChannelControl(
        operation_registry=operation_registry,
        operation_store=operations,
        audit_sink=audit_sink,
    )
    mapping = MappingControl(
        audit_sink=audit_sink,
        operation_store=operations,
    )
    models = ModelCenterControl(
        mapping=mapping,
        oauth=oauth,
        channels=channels,
        images=auxiliary.images,
        videos=auxiliary.videos,
        xai_media=auxiliary.xai_media,
        antigravity_media=auxiliary.antigravity_media,
        operations=operations,
        audit_sink=audit_sink,
    )
    return ManagementControls(
        audit_sink=audit_sink,
        operations=operations,
        operation_registry=operation_registry,
        oauth=oauth,
        channels=channels,
        api_keys=ApiKeyControl(audit_sink=audit_sink, provenance_store=state_store),
        mapping=mapping,
        models=models,
        load_balancing=LoadBalancingControl(audit_sink=audit_sink),
        proxy=ProxyControl(
            audit_sink=audit_sink,
            operation_store=operations,
        ),
        observability=ObservabilityControls(
            status=StatusControl(),
            stats=StatsControl(audit_sink=audit_sink),
            logs=LogsControl(),
            media=MediaControl(),
            retention=retention,
        ),
        system=SystemNetworkControls(
            settings=settings,
            blacklist=ContentBlacklistControl(audit_sink=audit_sink),
            network=NetworkControl(
                operations=operations,
                audit_sink=audit_sink,
            ),
        ),
        system_runtime=SystemRuntimeControl(audit_sink=audit_sink),
        telegram_retention=TelegramRetentionAdapter(retention),
        auxiliary=auxiliary,
    )
