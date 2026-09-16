"""Public schemas for inventory, metadata bindings, catalog and sync."""

from __future__ import annotations

from enum import Enum

from pydantic import Field, ValidationInfo, field_validator, model_validator

from .base import ResponseMeta, StrictSchema
from .mapping import MappingListMeta
from .operations import ManagementOperationData


class InventorySort(str, Enum):
    MODEL_ID = "modelId"
    PROVIDER = "provider"
    CHANNEL_ID = "channelId"


class MetadataSort(str, Enum):
    MODEL_ID = "modelId"
    PROVIDER = "provider"
    SOURCE = "source"


class CatalogSort(str, Enum):
    NAME = "name"
    PROVIDER = "provider"
    MODEL_ID = "modelId"


class MetadataScope(str, Enum):
    GLOBAL = "global"
    OAUTH = "oauth"
    API = "api"


class MetadataSyncScope(str, Enum):
    # New isolated sync modes.
    ONE = "one"
    SELECTED = "selected"
    SOURCE = "source"
    FULL = "full"
    # Legacy request values remain accepted and are normalized by the router.
    PROVIDER = "provider"
    ACCOUNT = "account"
    CHANNEL = "channel"


class MetadataSourceType(str, Enum):
    OAUTH = "oauth"
    API = "api"


class ModelInventoryData(StrictSchema):
    modelId: str
    family: str
    provider: str
    channelId: str
    accountId: str | None
    outboundModel: str
    revision: str


class ModelInventoryListEnvelope(StrictSchema):
    data: list[ModelInventoryData]
    meta: MappingListMeta


class MetadataPricing(StrictSchema):
    input: float | None = None
    output: float | None = None
    cacheRead: float | None = None
    cacheWrite: float | None = None
    longContextInput: float | None = None
    longContextOutput: float | None = None


class MetadataValues(StrictSchema):
    contextWindow: int | None = None
    contextWindowMaxMode: int | None = None
    maxInputTokens: int | None = None
    maxOutputTokens: int | None = None
    compactTriggerTokens: int | None = None
    vision: bool | None = None
    toolCall: bool | None = None
    structuredOutput: bool | None = None
    reasoningEfforts: list[str] = Field(default_factory=list)
    serviceTiers: list[str] = Field(default_factory=list)
    knowledgeCutoff: str | None = None
    defaultReasoningEffort: str | None = None
    inputModalities: list[str] = Field(default_factory=list)
    outputModalities: list[str] = Field(default_factory=list)
    cost: MetadataPricing | None = None


class ModelMetadataData(StrictSchema):
    modelId: str
    target: str | None
    providerId: str | None
    catalogModelId: str | None
    scope: MetadataScope
    scopeId: str | None
    outboundModel: str | None
    source: str
    authority: str
    effective: MetadataValues
    raw: MetadataValues
    valueSource: dict[str, str] = Field(default_factory=dict)
    constrainedBy: dict[str, list[str]] = Field(default_factory=dict)
    commonOverride: dict[str, object] = Field(default_factory=dict)
    sourceOverride: dict[str, object] = Field(default_factory=dict)
    revision: str


class ModelMetadataEnvelope(StrictSchema):
    data: ModelMetadataData
    meta: ResponseMeta


class ModelMetadataListEnvelope(StrictSchema):
    data: list[ModelMetadataData]
    meta: MappingListMeta


class PutMetadataBindingRequest(StrictSchema):
    scope: MetadataScope
    targetModelId: str = Field(min_length=3, max_length=500)
    providerId: str = Field(min_length=1, max_length=200)
    accountId: str | None = Field(default=None, min_length=1, max_length=500)
    channelId: str | None = Field(default=None, min_length=1, max_length=500)
    outboundModel: str | None = Field(default=None, min_length=1, max_length=500)

    @field_validator("accountId", mode="before")
    @classmethod
    def reject_extra_account_selector(cls, value, info: ValidationInfo):
        scope = info.data.get("scope")
        if scope in {MetadataScope.GLOBAL, MetadataScope.API}:
            raise ValueError(f"{scope.value} scope does not accept accountId")
        return value

    @field_validator("channelId", mode="before")
    @classmethod
    def reject_extra_channel_selector(cls, value, info: ValidationInfo):
        scope = info.data.get("scope")
        if scope in {MetadataScope.GLOBAL, MetadataScope.OAUTH}:
            raise ValueError(f"{scope.value} scope does not accept channelId")
        return value

    @field_validator("outboundModel", mode="before")
    @classmethod
    def reject_global_outbound_selector(cls, value, info: ValidationInfo):
        if info.data.get("scope") is MetadataScope.GLOBAL:
            raise ValueError("global scope does not accept outboundModel")
        return value

    @model_validator(mode="after")
    def validate_scope_selector(self):
        if self.scope is MetadataScope.GLOBAL:
            if self.accountId is not None or self.channelId is not None or self.outboundModel is not None:
                raise ValueError("global scope does not accept scoped selectors")
        elif self.scope is MetadataScope.OAUTH:
            if not self.accountId or self.channelId is not None:
                raise ValueError("oauth scope requires only accountId")
            if not self.outboundModel:
                raise ValueError("oauth scope requires outboundModel")
        elif self.scope is MetadataScope.API:
            if not self.channelId or self.accountId is not None:
                raise ValueError("api scope requires only channelId")
            if not self.outboundModel:
                raise ValueError("api scope requires outboundModel")
        return self


class MetadataOverrideCostValues(StrictSchema):
    input: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    output: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    cacheRead: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    cacheWrite: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    longContextInput: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    longContextOutput: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class MetadataOverrideValues(StrictSchema):
    contextWindow: int | None = Field(default=None, ge=1, le=2_147_483_647)
    maxInputTokens: int | None = Field(default=None, ge=1, le=2_147_483_647)
    maxOutputTokens: int | None = Field(default=None, ge=1, le=2_147_483_647)
    compactTriggerTokens: int | None = Field(default=None, ge=1, le=2_147_483_647)
    vision: bool | None = None
    toolCall: bool | None = None
    structuredOutput: bool | None = None
    reasoningEfforts: list[str] | None = Field(default=None, max_length=20)
    serviceTiers: list[str] | None = Field(default=None, max_length=20)
    knowledgeCutoff: str | None = Field(
        default=None, pattern=r"^\d{4}-\d{2}(?:-\d{2})?$",
    )
    cost: MetadataOverrideCostValues | None = None


class PatchMetadataOverridesRequest(StrictSchema):
    scope: MetadataScope
    accountId: str | None = Field(default=None, min_length=1, max_length=500)
    channelId: str | None = Field(default=None, min_length=1, max_length=500)
    outboundModel: str | None = Field(default=None, min_length=1, max_length=500)
    set: MetadataOverrideValues = Field(default_factory=MetadataOverrideValues)
    unset: list[str] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def validate_patch(self):
        if not self.set.model_fields_set and not self.unset:
            raise ValueError("set or unset must contain at least one field")
        if self.scope is MetadataScope.GLOBAL:
            if self.accountId is not None or self.channelId is not None or self.outboundModel is not None:
                raise ValueError("global scope does not accept scoped selectors")
        elif self.scope is MetadataScope.OAUTH:
            if not self.accountId or self.channelId is not None:
                raise ValueError("oauth scope requires only accountId")
        elif self.scope is MetadataScope.API:
            if not self.channelId or self.accountId is not None:
                raise ValueError("api scope requires only channelId")
        return self


class DeleteMetadataOverridesRequest(StrictSchema):
    scope: MetadataScope
    accountId: str | None = Field(default=None, min_length=1, max_length=500)
    channelId: str | None = Field(default=None, min_length=1, max_length=500)


class MetadataSourceRef(StrictSchema):
    type: MetadataSourceType
    id: str = Field(min_length=1, max_length=500)


class MetadataSyncTargetData(StrictSchema):
    modelId: str = Field(min_length=1, max_length=500)
    source: MetadataSourceRef | None = None


class MetadataSyncRequest(StrictSchema):
    mode: MetadataSyncScope | None = None
    targets: list[MetadataSyncTargetData] = Field(default_factory=list, max_length=10_000)
    source: MetadataSourceRef | None = None
    refreshCatalog: bool = True
    # Legacy selector shape remains parseable for old clients.
    scope: MetadataSyncScope | None = None
    providerId: str | None = Field(default=None, min_length=1, max_length=200)
    accountId: str | None = Field(default=None, min_length=1, max_length=500)
    channelId: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_selector(self):
        if self.mode is not None and self.scope is not None:
            raise ValueError("mode and legacy scope cannot both be supplied")
        selected = self.mode or self.scope or MetadataSyncScope.FULL
        if selected in {MetadataSyncScope.ONE, MetadataSyncScope.SELECTED, MetadataSyncScope.SOURCE}:
            if any((self.providerId, self.accountId, self.channelId)):
                raise ValueError("new sync modes do not accept legacy selectors")
            if selected is MetadataSyncScope.ONE and len(self.targets) != 1:
                raise ValueError("one sync requires exactly one target")
            if selected is MetadataSyncScope.SELECTED and not self.targets:
                raise ValueError("selected sync requires at least one target")
            if selected is MetadataSyncScope.SOURCE and (self.source is None or self.targets):
                raise ValueError("source sync requires source and no targets")
            if selected is not MetadataSyncScope.SOURCE and self.source is not None:
                raise ValueError("source selector is only valid for source mode")
            return self
        expected = {
            MetadataSyncScope.FULL: None,
            MetadataSyncScope.PROVIDER: "providerId",
            MetadataSyncScope.ACCOUNT: "accountId",
            MetadataSyncScope.CHANNEL: "channelId",
        }[selected]
        supplied = {
            "providerId": self.providerId,
            "accountId": self.accountId,
            "channelId": self.channelId,
        }
        if self.targets or self.source is not None:
            raise ValueError("legacy sync scope does not accept targets/source")
        if expected is None and any(supplied.values()):
            raise ValueError("full sync does not accept a selector")
        if expected is not None and not supplied[expected]:
            raise ValueError(f"{expected} is required for selected scope")
        if expected is not None and any(value for key, value in supplied.items() if key != expected):
            raise ValueError("only the selector matching scope is allowed")
        return self


class MetadataOperationEnvelope(StrictSchema):
    data: ManagementOperationData
    meta: ResponseMeta


class CatalogData(StrictSchema):
    key: str
    modelId: str
    name: str
    providerId: str
    providerName: str
    metadata: MetadataValues
    revision: str


class CatalogListEnvelope(StrictSchema):
    data: list[CatalogData]
    meta: MappingListMeta
