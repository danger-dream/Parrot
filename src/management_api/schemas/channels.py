"""Strict camelCase schemas for the Channels Management API."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Union

from pydantic import AfterValidator, Field, SecretStr, model_validator

from src.management_control.channels import (
    ChannelHealth,
    ChannelProtocol,
    CompatibilityMode,
)

from ..channels_security import require_url_without_userinfo
from .base import DataEnvelope, ResponseMeta, StrictSchema
from .operations import ManagementOperationData


ChannelInputUrl = Annotated[
    str,
    Field(min_length=8, max_length=4096),
    AfterValidator(require_url_without_userinfo),
]


class ChannelModelInput(StrictSchema):
    real: str = Field(min_length=1, max_length=256)
    alias: str = Field(min_length=1, max_length=256)


class CompatibilityFeatureInput(StrictSchema):
    mode: CompatibilityMode = CompatibilityMode.AUTO
    models: list[str] = Field(default_factory=list, max_length=2000)


class ChannelCompatibilityInput(StrictSchema):
    context1m: CompatibilityFeatureInput = Field(default_factory=CompatibilityFeatureInput)
    fast: CompatibilityFeatureInput = Field(default_factory=CompatibilityFeatureInput)


class InitialProbeResultInput(StrictSchema):
    """One wizard probe outcome (probeChannelDraft) carried into createChannel."""
    model: str = Field(min_length=1, max_length=256)
    ok: bool
    reason: str | None = Field(default=None, max_length=500)


class _ChannelCreateBase(StrictSchema):
    name: str = Field(min_length=1, max_length=64)
    apiKey: SecretStr = Field(
        min_length=5,
        max_length=4096,
        json_schema_extra={"writeOnly": True},
    )
    protocol: ChannelProtocol
    models: list[ChannelModelInput] = Field(min_length=1, max_length=2000)
    maxConcurrent: int = Field(default=0, ge=0, le=100000)
    compatibility: ChannelCompatibilityInput = Field(default_factory=ChannelCompatibilityInput)
    ccMimicry: bool | None = None
    omitTemperature: bool = False
    omitThinking: bool = False
    enabled: bool = True
    # Telegram wizard parity: models whose initial probe failed are saved but
    # put into cooldown ("不可用（已加入冷却）") on the newly created channel.
    initialProbeResults: list[InitialProbeResultInput] = Field(default_factory=list, max_length=2000)


class ManualChannelCreateRequest(_ChannelCreateBase):
    mode: Literal["manual"]
    baseUrl: ChannelInputUrl
    apiPath: str | None = Field(default=None, max_length=4096)


class PresetChannelCreateRequest(_ChannelCreateBase):
    mode: Literal["preset"]
    providerId: str = Field(min_length=1, max_length=120)
    providerPresetId: str = Field(min_length=1, max_length=120)


ChannelCreateRequest = Annotated[
    Union[ManualChannelCreateRequest, PresetChannelCreateRequest],
    Field(discriminator="mode"),
]


class ChannelUpdateRequest(StrictSchema):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    baseUrl: ChannelInputUrl | None = None
    apiPath: str | None = Field(default=None, max_length=4096)
    apiKey: SecretStr | None = Field(
        default=None,
        min_length=5,
        max_length=4096,
        json_schema_extra={"writeOnly": True},
    )
    protocol: ChannelProtocol | None = None
    models: list[ChannelModelInput] | None = Field(default=None, min_length=1, max_length=2000)
    maxConcurrent: int | None = Field(default=None, ge=0, le=100000)
    compatibility: ChannelCompatibilityInput | None = None
    ccMimicry: bool | None = None
    omitTemperature: bool | None = None
    omitThinking: bool | None = None
    enabled: bool | None = None
    providerId: str | None = Field(default=None, max_length=120)
    providerPresetId: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def require_patch(self):
        if not self.model_fields_set:
            raise ValueError("at least one channel field is required")
        return self


class ChannelModelData(StrictSchema):
    real: str
    alias: str


class CompatibilityFeatureData(StrictSchema):
    mode: CompatibilityMode
    models: list[str]
    allModels: bool


class ChannelCompatibilityData(StrictSchema):
    revision: str
    context1m: CompatibilityFeatureData
    fast: CompatibilityFeatureData


class ProviderUsageMetricData(StrictSchema):
    id: str | None = None
    label: str
    kind: str | None = None
    group: str | None = None
    unit: str | None = None
    currency: str | None = None
    value: str | None = None
    used: str | None = None
    total: str | None = None
    remaining: str | None = None
    usedPercent: float | None = None
    resetAt: datetime | None = None
    resetInSeconds: float | None = None
    status: str | None = None
    startAt: datetime | None = None
    endAt: datetime | None = None
    distributionTotal: str | None = None


class ProviderUsageSnapshotData(StrictSchema):
    version: int
    source: str
    balances: list[ProviderUsageMetricData]
    windows: list[ProviderUsageMetricData]
    counters: list[ProviderUsageMetricData]
    notices: list[str]
    partial: bool


class ProviderUsageData(StrictSchema):
    supported: bool
    status: str
    stale: bool
    partial: bool
    source: str | None = None
    fetchedAt: datetime | None = None
    error: str | None = None
    errorAt: datetime | None = None
    snapshot: ProviderUsageSnapshotData | None = None


class ChannelRuntimeModelData(StrictSchema):
    real: str
    alias: str
    recentRequests: int
    recentSuccessRate: float | None = None
    totalRequests: int
    averageConnectMilliseconds: float | None = None
    averageFirstByteMilliseconds: float | None = None
    score: float | None = None
    cooldownUntil: datetime | None = None
    cooldownKind: Literal["permanent", "quota", "temporary"] | None = None
    errorCount: int


class ChannelData(StrictSchema):
    id: str
    revision: str
    name: str
    baseUrl: str
    apiPath: str | None = None
    url: str
    apiKeyConfigured: bool
    apiKeyMaskedHint: str | None = None
    protocol: ChannelProtocol
    providerId: str | None = None
    providerPresetId: str | None = None
    models: list[ChannelModelData]
    modelCount: int
    enabled: bool
    disabledReason: str | None = None
    maxConcurrent: int
    ccMimicry: bool
    omitTemperature: bool
    omitThinking: bool
    compatibility: ChannelCompatibilityData
    health: ChannelHealth
    recentSuccessRate: float | None = None
    cooldownCount: int
    permanentCooldownCount: int = 0
    affinityCount: int
    clientAffinityCount: int
    providerUsage: ProviderUsageData


class ChannelMonthStatsData(StrictSchema):
    total: int
    successCount: int
    errorCount: int
    inputTokens: int
    outputTokens: int
    cacheCreationTokens: int
    cacheReadTokens: int
    averageTokensPerSecond: float | None = None
    maximumTokensPerSecond: float | None = None
    minimumTokensPerSecond: float | None = None
    cost: str | None = None
    # Round 3: TG 💎 月度 line inputs.
    promptTokens: int = 0
    successRate: float | None = None
    cacheReadRatio: float | None = None
    costTicks: int = 0
    costedSuccess: int = 0
    unpricedSuccess: int = 0


def month_stats_data(raw) -> "ChannelMonthStatsData":
    """Build the month DTO from a tokens_for_channel-shaped dict (round 3)."""
    get = (lambda key: raw.get(key)) if isinstance(raw, dict) else (lambda key: getattr(raw, key, None))
    number = lambda key: int(get(key) or 0)
    prompt = number("input") + number("cache_creation") + number("cache_read")
    total = number("total")
    cost = get("cost")
    if cost is None and isinstance(raw, dict) and number("costed_success"):
        from decimal import Decimal
        cost = format(Decimal(number("cost_ticks")) / Decimal(10_000_000_000), "f")  # model_pricing.TICKS_PER_USD
    return ChannelMonthStatsData(
        total=total, successCount=number("success_count"), errorCount=number("error_count"),
        inputTokens=number("input"), outputTokens=number("output"),
        cacheCreationTokens=number("cache_creation"), cacheReadTokens=number("cache_read"),
        averageTokensPerSecond=get("avg_tps"), maximumTokensPerSecond=get("max_tps"),
        minimumTokensPerSecond=get("min_tps"), cost=cost, promptTokens=prompt,
        successRate=number("success_count") / total if total else None,
        cacheReadRatio=number("cache_read") / prompt if prompt else None,
        costTicks=number("cost_ticks"), costedSuccess=number("costed_success"),
        unpricedSuccess=number("unpriced_success"),
    )


class ChannelModelStatsData(StrictSchema):
    finalModel: str
    total: int
    successCount: int
    errorCount: int
    inputTokens: int
    outputTokens: int
    cacheCreationTokens: int
    cacheReadTokens: int
    averageTokensPerSecond: float | None = None
    maximumTokensPerSecond: float | None = None
    minimumTokensPerSecond: float | None = None
    costTicks: int
    actualCostTicks: int
    estimatedCostTicks: int
    actualCostedSuccess: int
    estimatedCostedSuccess: int
    costedSuccess: int
    unpricedSuccess: int


class ChannelListItemData(ChannelData):
    # Round 3: TG 「💎 Parrot 月度」 per list row (null while the shared month
    # snapshot is first computed → monthStatsLoading=true).
    monthStats: ChannelMonthStatsData | None = None
    monthStatsLoading: bool = False


class ChannelDetailData(ChannelData):
    monthStats: ChannelMonthStatsData
    modelStats: list[ChannelModelStatsData]
    runtimeModels: list[ChannelRuntimeModelData]


class ChannelPageMeta(ResponseMeta):
    page: int
    pageSize: int
    total: int
    hasNext: bool
    orderRevision: str


class ChannelListEnvelope(StrictSchema):
    data: list[ChannelListItemData]
    meta: ChannelPageMeta


class ChannelOrderRequest(StrictSchema):
    channelIds: list[str] = Field(max_length=10000)


class ChannelOrderData(StrictSchema):
    revision: str
    channelIds: list[str]


class ActionResultData(StrictSchema):
    affected: int
    queued: bool | None = None


class ProbeExistingRequest(StrictSchema):
    model: str = Field(min_length=1, max_length=256)


class ProbeDraftRequest(StrictSchema):
    name: str = Field(default="management-draft", min_length=1, max_length=64)
    baseUrl: ChannelInputUrl
    apiPath: str | None = Field(default=None, max_length=4096)
    apiKey: SecretStr = Field(
        min_length=5, max_length=4096, json_schema_extra={"writeOnly": True}
    )
    protocol: ChannelProtocol
    model: str = Field(min_length=1, max_length=256)
    providerId: str | None = Field(default=None, max_length=120)
    providerPresetId: str | None = Field(default=None, max_length=120)
    ccMimicry: bool | None = None


class ExistingChannelDiscoveryRequest(StrictSchema):
    source: Literal["existing"]
    channelId: str = Field(min_length=5, max_length=256)


class DraftChannelDiscoveryRequest(StrictSchema):
    source: Literal["draft"]
    baseUrl: ChannelInputUrl
    apiPath: str | None = Field(default=None, max_length=4096)
    apiKey: SecretStr = Field(
        min_length=5, max_length=4096, json_schema_extra={"writeOnly": True}
    )
    protocol: ChannelProtocol
    providerId: str | None = Field(default=None, max_length=120)
    providerPresetId: str | None = Field(default=None, max_length=120)


ChannelDiscoveryRequest = Annotated[
    Union[ExistingChannelDiscoveryRequest, DraftChannelDiscoveryRequest],
    Field(discriminator="source"),
]


class CatalogProtocolEndpointData(StrictSchema):
    protocol: ChannelProtocol
    endpoint: str


class ChannelPresetData(StrictSchema):
    id: str
    name: str
    modelsUrlConfigured: bool
    modelDiscoveryAuth: str
    modelDiscoveryParser: str
    protocols: list[CatalogProtocolEndpointData]
    staticModels: list[str]
    ccMimicry: bool
    providerUsageSupported: bool


class ChannelProviderData(StrictSchema):
    id: str
    name: str
    presets: list[ChannelPresetData]


class ChannelCatalogData(StrictSchema):
    providers: list[ChannelProviderData]
    protocols: list[ChannelProtocol]
    compatibilityModes: list[CompatibilityMode]
    features: list[str]


ChannelEnvelope = DataEnvelope[ChannelData]
ChannelDetailEnvelope = DataEnvelope[ChannelDetailData]
ChannelCompatibilityEnvelope = DataEnvelope[ChannelCompatibilityData]
ChannelOrderEnvelope = DataEnvelope[ChannelOrderData]
ChannelCatalogEnvelope = DataEnvelope[ChannelCatalogData]
ChannelActionEnvelope = DataEnvelope[ActionResultData]
ChannelOperationEnvelope = DataEnvelope[ManagementOperationData]
