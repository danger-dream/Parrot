"""Typed transport-neutral contracts for downstream API key management."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class ApiKeySource(str, Enum):
    """Creation/replacement mode for newly observed secret material."""

    GENERATED = "generated"
    CUSTOM = "custom"


class ApiKeyProvenance(str, Enum):
    """Persisted source; old entries without evidence remain explicitly unknown."""

    GENERATED = "generated"
    CUSTOM = "custom"
    UNKNOWN = "unknown"


class ApiKeySort(str, Enum):
    ORDER_ASC = "orderAsc"
    ORDER_DESC = "orderDesc"
    NAME_ASC = "nameAsc"
    NAME_DESC = "nameDesc"
    MONTH_CALLS_DESC = "monthCallsDesc"


class ApiKeyEnabledFilter(str, Enum):
    ALL = "all"
    ENABLED = "enabled"
    DISABLED = "disabled"


@dataclass(frozen=True, slots=True)
class ApiKeyUsage:
    total: int = 0
    success_count: int = 0
    error_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    avg_tps: float | None = None
    max_tps: float | None = None
    min_tps: float | None = None
    cost_ticks: int = 0
    actual_cost_ticks: int = 0
    estimated_cost_ticks: int = 0
    costed_success: int = 0
    unpriced_success: int = 0


@dataclass(frozen=True, slots=True)
class ApiKeyModelUsage:
    model: str
    usage: ApiKeyUsage


@dataclass(frozen=True, slots=True)
class ApiKeyLimiterSnapshot:
    enabled: bool
    in_flight: int
    max_concurrent: int
    max_queue: int
    queue_wait_seconds: int
    waiting: int
    oldest_wait_seconds: int
    unlimited: bool
    enabled_source: str
    max_concurrent_source: str
    max_queue_source: str
    queue_wait_source: str


@dataclass(frozen=True, slots=True)
class ApiKeyLimitOverride:
    enabled: bool | None = None
    max_concurrent: int | None = None
    max_queue: int | None = None
    queue_wait_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class ApiKeyView:
    key_id: str
    name: str
    order: int
    enabled: bool
    source: ApiKeyProvenance
    masked_hint: str
    allow_images: bool
    allow_videos: bool
    # MCP 服务访问权限。默认关闭：既有 Key 不会被顺带开放。
    allow_mcp: bool
    # 该 Key 选用的 MCP 工具；空 = 跟随全局 mcp.tools。
    mcp_tools: tuple[str, ...]
    allowed_models: tuple[str, ...]
    limit_override: ApiKeyLimitOverride | None
    limiter: ApiKeyLimiterSnapshot
    month_stats: ApiKeyUsage
    model_stats: tuple[ApiKeyModelUsage, ...]
    revision: str
    # Compatibility-only privileged material. Management API adapters must never
    # request or serialize it; the frozen Telegram v0.31.13 renderer still does.
    secret: str | None = None


@dataclass(frozen=True, slots=True)
class ApiKeyPage:
    items: tuple[ApiKeyView, ...]
    page: int
    page_size: int
    total: int
    has_next: bool
    revision: str


@dataclass(frozen=True, slots=True)
class ApiKeySecretResult:
    api_key: ApiKeyView
    secret: str


@dataclass(frozen=True, slots=True)
class ApiKeyStats:
    key_id: str
    since: datetime
    overall: ApiKeyUsage
    by_model: tuple[ApiKeyModelUsage, ...]
    revision: str


@dataclass(frozen=True, slots=True)
class ApiKeyReplacementPlan:
    plan_id: str
    plan_token: str
    key_id: str
    revision: str
    expires_at: datetime
    impact: str
