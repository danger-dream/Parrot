"""Typed transport-neutral DTOs for channel management."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable


class ChannelProtocol(str, Enum):
    ANTHROPIC = "anthropic"
    OPENAI_CHAT = "openai-chat"
    OPENAI_RESPONSES = "openai-responses"


class CompatibilityMode(str, Enum):
    AUTO = "auto"
    FORCE = "force"


class ChannelHealth(str, Enum):
    DISABLED = "disabled"
    PERMANENT_COOLDOWN = "permanentCooldown"
    QUOTA_COOLDOWN = "quotaCooldown"
    COOLDOWN = "cooldown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


class ChannelSort(str, Enum):
    NAME = "name"
    ENABLED = "enabled"
    HEALTH = "health"
    PROTOCOL = "protocol"
    PROVIDER = "provider"
    MODEL_COUNT = "modelCount"
    ORDER = "order"


class SortDirection(str, Enum):
    ASC = "asc"
    DESC = "desc"


@dataclass(frozen=True, slots=True)
class ChannelModel(Mapping[str, str]):
    """Model pair; Mapping compatibility keeps the frozen Telegram renderer simple."""

    real: str
    alias: str

    def __getitem__(self, key: str) -> str:
        if key == "real":
            return self.real
        if key == "alias":
            return self.alias
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        yield "real"
        yield "alias"

    def __len__(self) -> int:
        return 2


@dataclass(frozen=True, slots=True)
class CompatibilityFeature:
    mode: CompatibilityMode = CompatibilityMode.AUTO
    models: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ChannelCompatibility:
    context_1m: CompatibilityFeature = CompatibilityFeature()
    fast: CompatibilityFeature = CompatibilityFeature()


@dataclass(frozen=True, slots=True)
class PerformanceView:
    model: str
    recent_requests: int
    recent_success_count: int
    total_requests: int
    avg_connect_ms: int | float | None
    avg_first_byte_ms: int | float | None
    score: int | float | None


@dataclass(frozen=True, slots=True)
class CooldownView:
    model: str
    error_count: int
    cooldown_until: int | None
    last_error_message: str | None
    quota: bool


@dataclass(frozen=True, slots=True)
class UsageView:
    status: str
    supported: bool
    stale: bool = False
    partial: bool = False
    source: str | None = None
    fetched_at: int | None = None
    error: str | None = None
    error_at: int | None = None
    snapshot: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ChannelView:
    id: str
    revision: str
    display_name: str
    base_url: str
    api_path: str | None
    protocol: ChannelProtocol
    provider_id: str | None
    provider_preset_id: str | None
    models: tuple[ChannelModel, ...]
    enabled: bool
    disabled_reason: str | None
    max_concurrent: int
    cc_mimicry: bool
    omit_temperature: bool
    omit_thinking: bool
    compatibility: ChannelCompatibility
    api_key_configured: bool
    api_key_masked_hint: str | None
    health: ChannelHealth
    recent_success_rate: float | None
    cooldown_count: int
    permanent_cooldown_count: int
    performance_by_model: Mapping[str, PerformanceView]
    cooldown_by_model: Mapping[str, CooldownView]
    affinity_count: int
    client_affinity_count: int
    provider_usage: UsageView

    @property
    def key(self) -> str:
        return self.id

    @property
    def type(self) -> str:
        return "api"

    @property
    def name(self) -> str:
        return self.display_name

    @property
    def context_1m_mode(self) -> str:
        return self.compatibility.context_1m.mode.value

    @property
    def context_1m_models(self) -> list[str]:
        return list(self.compatibility.context_1m.models)

    @property
    def fast_mode(self) -> str:
        return self.compatibility.fast.mode.value

    @property
    def fast_models(self) -> list[str]:
        return list(self.compatibility.fast.models)


@dataclass(frozen=True, slots=True)
class MonthlyStats:
    total: int = 0
    success_count: int = 0
    error_count: int = 0
    input: int = 0
    output: int = 0
    cache_creation: int = 0
    cache_read: int = 0
    avg_tps: float | None = None
    max_tps: float | None = None
    min_tps: float | None = None
    cost: str | None = None
    cost_ticks: int = 0
    costed_success: int = 0
    unpriced_success: int = 0


@dataclass(frozen=True, slots=True)
class ChannelDetail:
    channel: ChannelView
    month_stats: MonthlyStats
    model_stats: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True, slots=True)
class ChannelPage:
    items: tuple[ChannelView, ...]
    page: int
    page_size: int
    total: int
    has_next: bool
    order_revision: str


@dataclass(frozen=True, slots=True)
class ChannelListQuery:
    page: int = 1
    page_size: int = 50
    search: str | None = None
    enabled: bool | None = None
    protocol: ChannelProtocol | None = None
    provider_id: str | None = None
    health: ChannelHealth | None = None
    sort: ChannelSort = ChannelSort.ORDER
    direction: SortDirection = SortDirection.ASC


@dataclass(frozen=True, slots=True)
class ChannelCreateCommand:
    name: str
    base_url: str | None
    api_key: str
    protocol: ChannelProtocol
    models: tuple[ChannelModel, ...]
    max_concurrent: int = 0
    compatibility: ChannelCompatibility = ChannelCompatibility()
    cc_mimicry: bool | None = None
    omit_temperature: bool = False
    omit_thinking: bool = False
    provider_id: str | None = None
    provider_preset_id: str | None = None
    api_path: str | None = None
    enabled: bool = True
    initial_probe_results: Mapping[str, "ProbeResult"] = field(default_factory=dict)


_UNSET = object()


@dataclass(frozen=True, slots=True)
class ChannelUpdateCommand:
    name: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    protocol: ChannelProtocol | None = None
    models: tuple[ChannelModel, ...] | None = None
    max_concurrent: int | None = None
    cc_mimicry: bool | None = None
    omit_temperature: bool | None = None
    omit_thinking: bool | None = None
    enabled: bool | None = None
    api_path: str | None | object = _UNSET
    provider_id: str | None | object = _UNSET
    provider_preset_id: str | None | object = _UNSET
    compatibility: ChannelCompatibility | None = None


@dataclass(frozen=True, slots=True)
class ChannelMutationResult:
    channel: ChannelView
    load_balancing_initialized: bool


@dataclass(frozen=True, slots=True)
class DeleteResult:
    deleted: bool
    load_balancing_initialized: bool


@dataclass(frozen=True, slots=True)
class ActionResult:
    affected: int
    queued: bool | None = None


@dataclass(frozen=True, slots=True)
class ParsedChannelUrl:
    base_url: str
    api_path: str | None
    detected_protocol: ChannelProtocol | None


@dataclass(frozen=True, slots=True)
class ProviderPresetView:
    id: str
    display_name: str
    models_url_configured: bool
    models_auth: str
    models_parser: str
    protocols: Mapping[str, str]
    static_models: tuple[str, ...]
    cc_mimicry: bool
    usage_supported: bool


@dataclass(frozen=True, slots=True)
class ProviderBrandView:
    id: str
    display_name: str
    presets: tuple[ProviderPresetView, ...]


@dataclass(frozen=True, slots=True)
class ChannelCatalog:
    providers: tuple[ProviderBrandView, ...]
    protocols: tuple[ChannelProtocol, ...]
    compatibility_modes: tuple[CompatibilityMode, ...]
    features: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DiscoveryCommand:
    channel_id: str | None = None
    base_url: str | None = None
    api_path: str | None = None
    api_key: str | None = None
    protocol: ChannelProtocol | None = None
    provider_id: str | None = None
    provider_preset_id: str | None = None
    catalog_override: bool = False
    models_url: str | None = None
    models_auth: str = "bearer"
    models_parser: str = "openai-data-id"
    static_models: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    models: tuple[str, ...]
    source: str
    error: str | None
    retry_available: bool


@dataclass(frozen=True, slots=True)
class DraftProbeCommand:
    name: str
    base_url: str
    api_key: str
    protocol: ChannelProtocol
    model: str
    api_path: str | None = None
    provider_id: str | None = None
    provider_preset_id: str | None = None
    cc_mimicry: bool | None = None


@dataclass(frozen=True, slots=True)
class ProbeResult:
    ok: bool
    elapsed_ms: int
    reason: str | None
    cooldown_cleared: bool
    permanent_cooldown_cleared: bool


ProgressCallback = Callable[[str], Awaitable[None]]
