"""Typed transport-neutral DTOs for system settings and content blacklist."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RetryErrors:
    openaiServerOverloaded: bool
    openaiServerError: bool
    claudeOverloaded: bool
    xaiUnavailable: bool


@dataclass(frozen=True, slots=True)
class RetryRecovery:
    oauthRefresh: bool
    invalidEncryptedContent: bool
    claudeContext1mFallback: bool


@dataclass(frozen=True, slots=True)
class RetryTransient:
    enabled: bool
    maxExtraAttempts: int
    backoffSeconds: tuple[float, ...]
    errors: RetryErrors


@dataclass(frozen=True, slots=True)
class RetrySettings:
    transient: RetryTransient
    recovery: RetryRecovery
    revision: str


@dataclass(frozen=True, slots=True)
class TimeoutSettings:
    connect: int
    firstByte: int
    idle: int
    total: int
    revision: str


@dataclass(frozen=True, slots=True)
class ErrorCooldownSettings:
    errorWindows: tuple[int, ...]
    oauthGraceCount: int
    ladderMinIntervalSeconds: int
    permanentMinAgeSeconds: int
    revision: str


@dataclass(frozen=True, slots=True)
class ScoringSettings:
    emaAlpha: float
    recentWindow: int
    errorPenaltyFactor: int
    explorationRate: float
    revision: str


@dataclass(frozen=True, slots=True)
class AffinitySettings:
    ttlMinutes: int
    revision: str


@dataclass(frozen=True, slots=True)
class CchSettings:
    mode: str
    revision: str


@dataclass(frozen=True, slots=True)
class ConcurrencySettings:
    enabled: bool
    queueWaitSeconds: int
    defaultMaxConcurrent: int
    revision: str


@dataclass(frozen=True, slots=True)
class ApiKeyConcurrencySettings:
    enabled: bool
    defaultMaxConcurrent: int
    defaultMaxQueue: int
    defaultQueueWaitSeconds: int
    revision: str


@dataclass(frozen=True, slots=True)
class QuotaMonitorSettings:
    enabled: bool
    intervalSeconds: int
    thresholdPercent: float
    revision: str


@dataclass(frozen=True, slots=True)
class NotificationEvents:
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


@dataclass(frozen=True, slots=True)
class NotificationSettings:
    enabled: bool
    events: NotificationEvents
    revision: str


@dataclass(frozen=True, slots=True)
class OpenAiWebSocketSettings:
    responsesUpstreamWsForOAuth: bool
    revision: str


@dataclass(frozen=True, slots=True)
class ChannelBlacklist:
    channelId: str
    terms: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ContentBlacklist:
    default: tuple[str, ...]
    byChannel: tuple[ChannelBlacklist, ...]
    revision: str
