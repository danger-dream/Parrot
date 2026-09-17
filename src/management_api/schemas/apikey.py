"""Strict public schemas for downstream inference API key management."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, SecretStr, StringConstraints, model_validator

from src.management_control.apikey import (
    ApiKeyEnabledFilter,
    ApiKeyProvenance,
    ApiKeySort,
    ApiKeySource,
)

from .base import ResponseMeta, StrictSchema
from .mcp import McpToolName


Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")]
KeyId = Annotated[str, StringConstraints(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")]
ModelId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)]
SecretInput = Annotated[SecretStr, StringConstraints(min_length=8, max_length=256)]


class ApiKeyUsageData(StrictSchema):
    total: int = Field(ge=0)
    successCount: int = Field(ge=0)
    errorCount: int = Field(ge=0)
    inputTokens: int = Field(ge=0)
    outputTokens: int = Field(ge=0)
    cacheCreationTokens: int = Field(ge=0)
    cacheReadTokens: int = Field(ge=0)
    averageTps: float | None = None
    maximumTps: float | None = None
    minimumTps: float | None = None
    costTicks: int = Field(ge=0)
    actualCostTicks: int = Field(ge=0)
    estimatedCostTicks: int = Field(ge=0)
    costedSuccess: int = Field(ge=0)
    unpricedSuccess: int = Field(ge=0)


class ApiKeyModelUsageData(StrictSchema):
    model: str
    usage: ApiKeyUsageData


class ApiKeyLimiterData(StrictSchema):
    enabled: bool
    inFlight: int = Field(ge=0)
    maxConcurrent: int = Field(ge=0)
    maxQueue: int = Field(ge=0)
    queueWaitSeconds: int = Field(ge=0)
    waiting: int = Field(ge=0)
    oldestWaitSeconds: int = Field(ge=0)
    unlimited: bool
    enabledSource: Literal["key", "global"]
    maxConcurrentSource: Literal["key", "global"]
    maxQueueSource: Literal["key", "global"]
    queueWaitSource: Literal["key", "global"]


class ApiKeyLimitOverrideData(StrictSchema):
    enabled: bool | None = None
    maxConcurrent: int | None = Field(default=None, ge=0)
    maxQueue: int | None = Field(default=None, ge=0)
    queueWaitSeconds: int | None = Field(default=None, ge=0)


class ApiKeyData(StrictSchema):
    keyId: str
    name: str
    order: int = Field(ge=1)
    enabled: bool
    source: ApiKeyProvenance
    maskedHint: str
    allowImages: bool
    allowVideos: bool
    allowMcp: bool
    mcpTools: list[McpToolName]
    allowedModels: list[str]
    limitOverride: ApiKeyLimitOverrideData | None
    limiter: ApiKeyLimiterData
    monthStats: ApiKeyUsageData
    modelStats: list[ApiKeyModelUsageData]
    revision: str


class ApiKeyListData(StrictSchema):
    items: list[ApiKeyData]


class ApiKeyListMeta(ResponseMeta):
    page: int = Field(ge=1)
    pageSize: int = Field(ge=1, le=200)
    total: int = Field(ge=0)
    hasNext: bool
    revision: str


class ApiKeyListEnvelope(StrictSchema):
    data: ApiKeyListData
    meta: ApiKeyListMeta


class ApiKeyEnvelope(StrictSchema):
    data: ApiKeyData
    meta: ResponseMeta


class ApiKeyCreateRequest(StrictSchema):
    mode: ApiKeySource
    name: Name
    customSecret: SecretInput | None = Field(
        default=None,
        json_schema_extra={"writeOnly": True, "examples": ["<write-only>"]},
    )

    @model_validator(mode="after")
    def check_mode_secret(self):
        if self.mode is ApiKeySource.CUSTOM and self.customSecret is None:
            raise ValueError("customSecret is required in custom mode")
        if self.mode is ApiKeySource.GENERATED and self.customSecret is not None:
            raise ValueError("customSecret is not allowed in generated mode")
        return self


class ApiKeySecretData(StrictSchema):
    apiKey: ApiKeyData
    secret: str = Field(
        json_schema_extra={"examples": ["<one-time-secret>"]},
    )


class ApiKeySecretEnvelope(StrictSchema):
    data: ApiKeySecretData
    meta: ResponseMeta


class ApiKeyLimitOverridePatch(StrictSchema):
    enabled: bool | None = None
    maxConcurrent: int | None = Field(default=None, ge=0)
    maxQueue: int | None = Field(default=None, ge=0)
    queueWaitSeconds: int | None = Field(default=None, ge=0)


class ApiKeyUpdateRequest(StrictSchema):
    enabled: bool | None = None
    allowImages: bool | None = None
    allowVideos: bool | None = None
    allowMcp: bool | None = None
    mcpTools: list[McpToolName] | None = None
    allowedModels: list[ModelId] | None = None
    limitOverride: ApiKeyLimitOverridePatch | None = None

    @model_validator(mode="after")
    def require_change(self):
        if not self.model_fields_set:
            raise ValueError("at least one field is required")
        nullable = {"enabled", "allowImages", "allowVideos", "allowMcp", "mcpTools", "allowedModels"}
        for field in self.model_fields_set & nullable:
            if getattr(self, field) is None:
                raise ValueError(f"{field} must not be null")
        return self


class ApiKeyReplacementPlanData(StrictSchema):
    planId: str
    planToken: str = Field(
        json_schema_extra={"examples": ["<one-time-plan-token>"]},
    )
    keyId: str
    revision: str
    expiresAt: datetime
    impact: str


class ApiKeyReplacementPlanEnvelope(StrictSchema):
    data: ApiKeyReplacementPlanData
    meta: ResponseMeta


class ApiKeyRegenerateRequest(StrictSchema):
    planId: Annotated[str, StringConstraints(min_length=8, max_length=128)]
    planToken: Annotated[SecretStr, StringConstraints(min_length=8, max_length=256)] = Field(
        json_schema_extra={"writeOnly": True, "examples": ["<write-only>"]},
    )


class ApiKeyReplaceSecretRequest(StrictSchema):
    customSecret: SecretInput = Field(
        json_schema_extra={"writeOnly": True, "examples": ["<write-only>"]},
    )


class ApiKeyOrderRequest(StrictSchema):
    keyIds: list[KeyId] = Field(min_length=0, max_length=10_000)


class ApiKeyOrderData(StrictSchema):
    keyIds: list[str]
    revision: str


class ApiKeyOrderEnvelope(StrictSchema):
    data: ApiKeyOrderData
    meta: ResponseMeta


class ApiKeyLimiterEnvelope(StrictSchema):
    data: ApiKeyLimiterData
    meta: ResponseMeta


class ApiKeyStatsData(StrictSchema):
    keyId: str
    since: datetime
    overall: ApiKeyUsageData
    byModel: list[ApiKeyModelUsageData]
    revision: str


class ApiKeyStatsEnvelope(StrictSchema):
    data: ApiKeyStatsData
    meta: ResponseMeta


__all__ = [
    "ApiKeyCreateRequest",
    "ApiKeyData",
    "ApiKeyEnabledFilter",
    "ApiKeyEnvelope",
    "ApiKeyLimitOverridePatch",
    "ApiKeyLimiterEnvelope",
    "ApiKeyListEnvelope",
    "ApiKeyOrderEnvelope",
    "ApiKeyOrderRequest",
    "ApiKeyRegenerateRequest",
    "ApiKeyReplaceSecretRequest",
    "ApiKeyReplacementPlanEnvelope",
    "ApiKeySecretEnvelope",
    "ApiKeySort",
    "ApiKeyStatsEnvelope",
    "ApiKeyUpdateRequest",
    "KeyId",
]
