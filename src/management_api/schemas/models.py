"""Strict public schemas for the unified model center."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from src.management_control.models import ModelKind, ModelSourceType, ModelStatus

from .base import ResponseMeta, StrictSchema


class ModelOwnerData(StrictSchema):
    type: ModelSourceType
    id: str | None = None


class ModelIdentityData(StrictSchema):
    type: ModelKind
    modelId: str
    provider: str | None = None
    owner: ModelOwnerData | None = None


class ModelUsageData(StrictSchema):
    """TG model_center_usage: lifetime 累计用量 / 请求 / 成功率 / TPS."""
    requestCount: int
    successCount: int
    errorCount: int
    inputTokens: int
    outputTokens: int
    cacheCreationTokens: int
    cacheReadTokens: int
    promptTokens: int
    cacheReadRatio: float | None = None
    successRate: float | None = None
    averageTokensPerSecond: float | None = None
    maximumTokensPerSecond: float | None = None
    minimumTokensPerSecond: float | None = None


class ModelScheduleData(StrictSchema):
    """TG 「调度」: priority / order / smart; ``order`` = channel keys in scheduling order."""
    mode: str | None = None
    ownOrder: bool = False
    order: list[str] = Field(default_factory=list)


class OAuthSourceAccountData(StrictSchema):
    """Quota / balance / credits of the OAuth account behind a source (cached)."""
    statusTag: str | None = None
    plan: dict[str, Any] = Field(default_factory=dict)
    usageSections: list[dict[str, Any]] = Field(default_factory=list)
    usageFetchedAt: str | None = None
    cooldown: dict[str, Any] = Field(default_factory=dict)
    planType: str | None = None
    available: bool | None = None
    quotaLimited: bool | None = None
    invalid: bool | None = None
    disabledReason: str | None = None
    disabledUntil: str | None = None


class ModelSourceData(StrictSchema):
    type: ModelSourceType
    id: str
    label: str
    provider: str
    outboundModel: str
    sourceEnabled: bool
    containerEnabled: bool
    effectiveRoutable: bool
    enabledOverride: bool = False
    unavailableReason: str | None = None
    effectiveMetadata: dict[str, Any]
    valueSource: dict[str, str]
    constrainedBy: dict[str, list[str]]
    # ── Round 3 (getModel only; null in listModels) ──
    # Position among scheduled sources (TG numbering); null when out of scheduling.
    scheduleRank: int | None = None
    # TG order: scheduled sources in scheduling order, then the rest.
    displayOrder: int | None = None
    notScheduledReason: Literal["accountRestricted", "containerDisabled", "sourceDisabled",
                                "globalDisabled", "unavailable"] | None = None
    account: OAuthSourceAccountData | None = None
    channel: dict[str, Any] | None = None


class ModelData(StrictSchema):
    resourceKey: str
    identity: ModelIdentityData
    modelId: str
    aliases: list[str] = Field(default_factory=list)
    globalEnabled: bool | None = None
    visible: bool | None = None
    sourceCount: int
    commonMetadata: dict[str, Any] = Field(default_factory=dict)
    sources: list[ModelSourceData] = Field(default_factory=list)
    editable: bool
    revision: str
    # ── Round 3: TG list facts ──
    # null = available; otherwise TG's concise list state as a code.
    stateCode: str | None = None
    creditRates: dict[str, list[str]] = Field(default_factory=dict)
    # 容量/能力/价格 values shared by the scheduled sources (TG detail top block;
    # also the list's 压缩阈值 / 最大输出 …).
    sharedMetadata: dict[str, Any] = Field(default_factory=dict)
    usage: ModelUsageData | None = None
    usageLoading: bool = False
    schedule: ModelScheduleData | None = None


class ModelListMeta(ResponseMeta):
    page: int
    pageSize: int
    total: int
    hasNext: bool
    revision: str


class ModelListEnvelope(StrictSchema):
    data: list[ModelData]
    meta: ModelListMeta


class ModelEnvelope(StrictSchema):
    data: ModelData
    meta: ResponseMeta


class ModelSourceRequest(StrictSchema):
    type: ModelSourceType
    id: str | None = None

    @model_validator(mode="after")
    def validate_source(self) -> "ModelSourceRequest":
        if self.type is ModelSourceType.GLOBAL and self.id is not None:
            raise ValueError("global scope does not accept id")
        if self.type is not ModelSourceType.GLOBAL and not (self.id or "").strip():
            raise ValueError("oauth/api scope requires id")
        return self


class ModelFilterRequest(StrictSchema):
    type: list[ModelKind] = Field(default_factory=list)
    text: str | None = Field(default=None, max_length=300)
    sourceType: ModelSourceType | None = None
    sourceId: str | None = None
    status: list[ModelStatus] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_source(self) -> "ModelFilterRequest":
        if self.sourceType is None and self.sourceId is not None:
            raise ValueError("sourceId requires sourceType")
        if self.sourceType is ModelSourceType.GLOBAL and self.sourceId is not None:
            raise ValueError("global source does not accept sourceId")
        if self.sourceType in {ModelSourceType.OAUTH, ModelSourceType.API} and not (self.sourceId or "").strip():
            raise ValueError("oauth/api source requires sourceId")
        return self


class ModelSelectionRequest(StrictSchema):
    mode: Literal["ids", "filter"]
    modelIds: list[str] = Field(default_factory=list, max_length=10_000)
    filter: ModelFilterRequest | None = None
    excludedModelIds: list[str] = Field(default_factory=list, max_length=10_000)

    @model_validator(mode="after")
    def validate_mode(self) -> "ModelSelectionRequest":
        if self.mode == "ids":
            if not self.modelIds:
                raise ValueError("ids selection requires modelIds")
            if self.filter is not None or self.excludedModelIds:
                raise ValueError("ids selection does not accept filter or excludedModelIds")
        else:
            if self.filter is None:
                raise ValueError("filter selection requires filter")
            if self.modelIds:
                raise ValueError("filter selection does not accept modelIds")
        return self


class ModelStateTargetRequest(StrictSchema):
    enabled: bool | None = None
    visible: bool | None = None

    @model_validator(mode="after")
    def exactly_one(self) -> "ModelStateTargetRequest":
        if (self.enabled is None) == (self.visible is None):
            raise ValueError("target must contain exactly one of enabled or visible")
        return self


class ModelStateRequest(StrictSchema):
    scope: ModelSourceRequest
    selection: ModelSelectionRequest
    target: ModelStateTargetRequest


class ModelStateItemData(StrictSchema):
    modelId: str
    status: Literal["updated", "unchanged"]


class ModelStateData(StrictSchema):
    items: list[ModelStateItemData]
    revision: str


class ModelStateEnvelope(StrictSchema):
    data: ModelStateData
    meta: ResponseMeta


class RegistryPinData(StrictSchema):
    catalog: str = Field(min_length=1, max_length=300)
    name: str = Field(max_length=300)


class RegistryRuleData(StrictSchema):
    """merge: ``model`` becomes an alias of ``into``. assign: ``source``'s ``upstreamModel`` belongs to ``model``."""
    type: Literal["merge", "assign"]
    model: str = Field(min_length=1, max_length=300)
    into: str | None = Field(default=None, max_length=300)
    source: str | None = Field(default=None, max_length=300)
    upstreamModel: str | None = Field(default=None, max_length=300)
    pin: RegistryPinData | None = None

    @model_validator(mode="after")
    def _fields_for_type(self):
        if self.type == "merge" and not self.into:
            raise ValueError("merge requires into")
        if self.type == "assign" and not (self.source and self.upstreamModel):
            raise ValueError("assign requires source and upstreamModel")
        return self


class RegistryPendingData(StrictSchema):
    source: str
    upstreamModel: str
    name: str
    modelId: str
    reason: str
    options: list[str]


class RegistrySuggestionData(StrictSchema):
    key: str
    modelId: str
    intoModelId: str
    text: str
    pin: RegistryPinData | None = None


class ModelRegistryData(StrictSchema):
    enabled: bool
    revision: str
    pending: list[RegistryPendingData]
    suggestions: list[RegistrySuggestionData]
    conflicts: list[str]
    rules: list[RegistryRuleData]
    dismissed: list[str]
    defaultEfforts: dict[str, str]
    # Effort used by models without an entry in defaultEfforts.
    globalDefaultEffort: str | None = None


class ModelRegistryEnvelope(StrictSchema):
    data: ModelRegistryData
    meta: ResponseMeta


class ModelRegistryRulesRequest(StrictSchema):
    rules: list[RegistryRuleData] = Field(max_length=500)
    dismissed: list[str] = Field(default_factory=list, max_length=500)


class ModelDefaultEffortRequest(StrictSchema):
    effort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"] | None


class ModelRegistryRevisionData(StrictSchema):
    revision: str


class ModelRegistryRevisionEnvelope(StrictSchema):
    data: ModelRegistryRevisionData
    meta: ResponseMeta
