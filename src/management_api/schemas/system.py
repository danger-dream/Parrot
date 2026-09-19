"""Strict camelCase schemas for P6 system settings and content blacklist."""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field

from .base import StrictSchema


class StrictRequestSchema(StrictSchema):
    """P6 body base: OpenAPI JSON scalar types are never coerced."""

    model_config = ConfigDict(
        extra="forbid", populate_by_name=True, strict=True,
    )


class RetryErrorsData(StrictSchema):
    openaiServerOverloaded: bool
    openaiServerError: bool
    claudeOverloaded: bool
    xaiUnavailable: bool


class RetryRecoveryData(StrictSchema):
    oauthRefresh: bool
    invalidEncryptedContent: bool
    claudeContext1mFallback: bool


class RetryTransientData(StrictSchema):
    enabled: bool
    maxExtraAttempts: int
    backoffSeconds: list[float]
    errors: RetryErrorsData


class RetrySettingsData(StrictSchema):
    transient: RetryTransientData
    recovery: RetryRecoveryData
    revision: str


class RetryErrorsPatch(StrictRequestSchema):
    openaiServerOverloaded: bool | None = None
    openaiServerError: bool | None = None
    claudeOverloaded: bool | None = None
    xaiUnavailable: bool | None = None


class RetryRecoveryPatch(StrictRequestSchema):
    oauthRefresh: bool | None = None
    invalidEncryptedContent: bool | None = None
    claudeContext1mFallback: bool | None = None


class RetryTransientPatch(StrictRequestSchema):
    enabled: bool | None = None
    maxExtraAttempts: int | None = Field(default=None, ge=1, le=5)
    backoffSeconds: list[float] | None = Field(default=None, min_length=1, max_length=5)
    errors: RetryErrorsPatch | None = None


class RetrySettingsPatch(StrictRequestSchema):
    transient: RetryTransientPatch | None = None
    recovery: RetryRecoveryPatch | None = None


class TimeoutSettingsData(StrictSchema):
    connect: int
    firstByte: int
    idle: int
    total: int
    revision: str


class TimeoutSettingsPatch(StrictRequestSchema):
    connect: int | None = Field(default=None, ge=1)
    firstByte: int | None = Field(default=None, ge=1)
    idle: int | None = Field(default=None, ge=1)
    total: int | None = Field(default=None, ge=1)


class ErrorCooldownSettingsData(StrictSchema):
    errorWindows: list[int]
    oauthGraceCount: int
    ladderMinIntervalSeconds: int
    permanentMinAgeSeconds: int
    revision: str


class ErrorCooldownSettingsPatch(StrictRequestSchema):
    errorWindows: list[int] | None = Field(default=None, min_length=1)
    oauthGraceCount: int | None = Field(default=None, ge=0, le=100)
    ladderMinIntervalSeconds: int | None = Field(default=None, ge=0, le=3600)
    permanentMinAgeSeconds: int | None = Field(default=None, ge=0, le=86400)


class ScoringSettingsData(StrictSchema):
    emaAlpha: float
    recentWindow: int
    errorPenaltyFactor: int
    explorationRate: float
    revision: str


class ScoringSettingsPatch(StrictRequestSchema):
    emaAlpha: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    recentWindow: int | None = Field(default=None, ge=1, le=1000)
    errorPenaltyFactor: int | None = Field(default=None, ge=0, le=100)
    explorationRate: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)


class AffinitySettingsData(StrictSchema):
    ttlMinutes: int
    revision: str


class AffinitySettingsPatch(StrictRequestSchema):
    ttlMinutes: int | None = Field(default=None, ge=1, le=1440)


class CchSettingsData(StrictSchema):
    mode: Literal["disabled", "dynamic"]
    revision: str


class CchSettingsPatch(StrictRequestSchema):
    mode: Literal["disabled", "dynamic"] | None = None


class ConcurrencySettingsData(StrictSchema):
    enabled: bool
    queueWaitSeconds: int
    defaultMaxConcurrent: int
    revision: str


class ConcurrencySettingsPatch(StrictRequestSchema):
    enabled: bool | None = None
    queueWaitSeconds: int | None = Field(default=None, ge=0)
    defaultMaxConcurrent: int | None = Field(default=None, ge=0)


class ApiKeyConcurrencySettingsData(StrictSchema):
    enabled: bool
    defaultMaxConcurrent: int
    defaultMaxQueue: int
    defaultQueueWaitSeconds: int
    revision: str


class ApiKeyConcurrencySettingsPatch(StrictRequestSchema):
    enabled: bool | None = None
    defaultMaxConcurrent: int | None = Field(default=None, ge=0)
    defaultMaxQueue: int | None = Field(default=None, ge=0)
    defaultQueueWaitSeconds: int | None = Field(default=None, ge=0)


class QuotaMonitorSettingsData(StrictSchema):
    enabled: bool
    intervalSeconds: int
    thresholdPercent: float
    revision: str


class QuotaMonitorSettingsPatch(StrictRequestSchema):
    enabled: bool | None = None
    intervalSeconds: int | None = Field(default=None, ge=10, le=86400)
    thresholdPercent: float | None = Field(default=None, ge=1, le=100, allow_inf_nan=False)


class NotificationEventsData(StrictSchema):
    channelPermanent: bool
    channelRecovered: bool
    quotaDisabled: bool
    quotaResumed: bool
    quotaCooldown: bool
    oauthRefreshed: bool
    oauthRefreshFailed: bool
    noChannels: bool
    openaiStoreSaveFailed: bool
    modelDegraded: bool
    statusAlert: bool
    appUpdate: bool
    networkMonitor: bool


class NotificationEventsPatch(StrictRequestSchema):
    channelPermanent: bool | None = None
    channelRecovered: bool | None = None
    quotaDisabled: bool | None = None
    quotaResumed: bool | None = None
    quotaCooldown: bool | None = None
    oauthRefreshed: bool | None = None
    oauthRefreshFailed: bool | None = None
    noChannels: bool | None = None
    openaiStoreSaveFailed: bool | None = None
    modelDegraded: bool | None = None
    statusAlert: bool | None = None
    appUpdate: bool | None = None
    networkMonitor: bool | None = None


class NotificationSettingsData(StrictSchema):
    enabled: bool
    events: NotificationEventsData
    revision: str


class NotificationSettingsPatch(StrictRequestSchema):
    enabled: bool | None = None
    events: NotificationEventsPatch | None = None


class OpenAiWebSocketSettingsData(StrictSchema):
    responsesUpstreamWsForOAuth: bool
    revision: str


class OpenAiWebSocketSettingsPatch(StrictRequestSchema):
    responsesUpstreamWsForOAuth: bool | None = None


class BlacklistTermRequest(StrictRequestSchema):
    term: str = Field(min_length=1, max_length=200)


class ChannelBlacklistData(StrictSchema):
    channelId: str
    terms: list[str]


class ContentBlacklistData(StrictSchema):
    default: list[str]
    byChannel: list[ChannelBlacklistData]
    revision: str
