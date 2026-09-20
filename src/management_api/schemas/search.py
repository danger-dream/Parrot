"""Strict search-management DTOs. Keys exist only in write requests."""
from __future__ import annotations

from typing import Annotated, Literal
from pydantic import Field
from .base import StrictSchema
from .system import StrictRequestSchema

Mode = Literal["managed", "passthrough", "disabled"]
BackendType = Literal["anysearch", "tavily", "exa", "brave", "openai", "xai", "anthropic"]
BackendId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
Key = Annotated[str, Field(min_length=1, max_length=8192, json_schema_extra={"writeOnly": True})]


class SearchSettingsPatch(StrictRequestSchema):
    functionMode: Mode | None = None
    hostedMode: Mode | None = None
    maxAttempts: int | None = Field(None, ge=1, le=10)
    timeoutSeconds: float | None = Field(None, ge=0.1, le=600, allow_inf_nan=False, description="Deadline for one complete backend call; default 10 seconds. OAuth native search may require longer")
    maxResults: int | None = Field(None, ge=1, le=20)
    maxToolRounds: int | None = Field(None, ge=1, le=1000)
    maxFetchChars: int | None = Field(None, ge=1, le=10_000_000)
    minQueryChars: int | None = Field(None, ge=1, le=1000)
    maxFetchUrlChars: int | None = Field(None, ge=1, le=100_000)
    maxConcurrentToolCalls: int | None = Field(None, ge=0, le=1000)
    requireKnownUrlForFetch: bool | None = None
    language: str | None = Field(None, max_length=64)
    country: str | None = Field(None, max_length=64)
    freshness: Literal["", "day", "week", "month", "year"] | None = None


class SearchBackendPatch(StrictRequestSchema):
    name: str | None = Field(None, min_length=1, max_length=200)
    enabled: bool | None = None
    endpoint: str | None = Field(None, max_length=2048)
    model: str | None = Field(None, max_length=200, description="OAuth search backends only; HTTP key backends accept only the empty compatibility value")
    accountIds: list[str] | None = Field(None, description="Complete public account keys, including workspace/subject; empty selects all eligible accounts")
    allowDisabledAccounts: bool | None = None
    apiKeys: list[Key] | None = Field(None, max_length=100, repr=False, json_schema_extra={"writeOnly": True})
    addApiKeys: list[Key] | None = Field(None, max_length=100, repr=False, json_schema_extra={"writeOnly": True})
    removeKeyIndices: list[int] | None = None


class SearchBackendCreate(SearchBackendPatch):
    type: BackendType
    id: BackendId | None = None


class SearchPriorityRequest(StrictRequestSchema):
    backendIds: list[BackendId]


class SearchTestRequest(StrictRequestSchema):
    backendId: BackendId
    operation: Literal["search", "extract"] = "search"
    query: str | None = Field(None, min_length=1, max_length=10000)
    url: str | None = Field(None, min_length=1, max_length=100000)


class SearchBackendData(StrictSchema):
    id: str
    type: BackendType
    name: str
    enabled: bool
    available: bool = Field(description="Configuration readiness only; not a live health probe")
    reason: str
    keyCount: int = Field(description="Number of configured keys; values are never returned")
    accountCount: int
    verified: bool = Field(description="Implementation was previously exercised, not current-account health; Anthropic remains unverified")
    endpoint: str
    model: str
    accountIds: list[str]
    allowDisabledAccounts: bool


class SearchSettingsData(StrictSchema):
    functionMode: Mode
    hostedMode: Mode
    maxAttempts: int
    timeoutSeconds: float
    maxResults: int
    maxToolRounds: int
    maxFetchChars: int
    minQueryChars: int
    maxFetchUrlChars: int
    maxConcurrentToolCalls: int
    requireKnownUrlForFetch: bool
    language: str
    country: str
    freshness: str
    backends: list[SearchBackendData]
    revision: str


class SearchAccountData(StrictSchema):
    id: str
    name: str
    enabled: bool
    credentialConfigured: bool


class SearchCallLogData(StrictSchema):
    """One real upstream call of a search source (dedicated search log)."""

    id: int
    callId: str
    attemptNo: int
    origin: str
    requestId: str | None = None
    roundNo: int
    sourceId: str
    sourceType: str
    sourceName: str
    operation: Literal["search", "x_search", "extract"]
    credentialKind: Literal["api_key", "oauth", ""]
    credentialLabel: str
    accountKey: str
    credentialIndex: int | None = None
    model: str
    query: str | None = None
    url: str | None = None
    startedAt: float
    endedAt: float | None = None
    status: Literal["running", "success", "error"]
    errorCode: str | None = None
    elapsedMs: int | None = None
    resultCount: int
    contentChars: int
    inputTokens: int
    outputTokens: int
    cacheCreationTokens: int
    cacheReadTokens: int
    usageObserved: bool
    pricingModel: str | None = None
    costSource: Literal["actual", "estimated", "unpriced"]
    costUsd: float
    settledAt: float | None = None


class SearchSourceStatsData(StrictSchema):
    """Aggregated per-source search statistics."""

    sourceId: str
    sourceType: str
    sourceName: str
    attempts: int
    success: int
    failed: int
    running: int
    averageMs: int | None = None
    resultCount: int
    inputTokens: int
    outputTokens: int
    cacheCreationTokens: int
    cacheReadTokens: int
    costUsd: float
    usageObserved: int
    lastAt: float
