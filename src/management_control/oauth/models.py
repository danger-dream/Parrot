"""Typed transport-neutral models for OAuth management controls."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Mapping, Sequence


class OAuthProvider(str, Enum):
    CLAUDE = "claude"
    CURSOR = "cursor"
    OPENAI = "openai"
    XAI = "xai"
    ANTIGRAVITY = "antigravity"
    WORKBUDDY = "workbuddy"


class OAuthAccountFilter(str, Enum):
    ALL = "all"
    AVAILABLE = "available"
    QUOTA = "quota"
    INVALID = "invalid"


class OAuthAccountSort(str, Enum):
    CONFIGURED = "configured"
    DISPLAY_NAME = "displayName"
    PROVIDER = "provider"
    STATUS = "status"


class OAuthCredentialKind(str, Enum):
    MANUAL = "manual"
    JSON = "json"
    REFRESH_TOKEN = "refreshToken"


class OAuthUsageDisplayMode(str, Enum):
    USED = "used"
    REMAINING = "remaining"


class CchMode(str, Enum):
    DISABLED = "disabled"
    DYNAMIC = "dynamic"


@dataclass(frozen=True, slots=True)
class PageSpec:
    page: int = 1
    page_size: int = 50


@dataclass(frozen=True, slots=True)
class PageMeta:
    page: int
    page_size: int
    total: int
    has_next: bool


@dataclass(frozen=True, slots=True)
class OAuthAccountSummary:
    account_id: str
    provider: OAuthProvider
    display_name: str
    identity: str
    enabled: bool
    disabled_reason: str | None
    disabled_until: datetime | None
    max_concurrent: int
    available: bool
    quota_limited: bool
    invalid: bool
    model_count: int
    disabled_model_count: int
    credential_configured: bool
    revision: str


@dataclass(frozen=True, slots=True)
class OAuthAccountPage:
    items: tuple[OAuthAccountSummary, ...]
    meta: PageMeta
    revision: str


@dataclass(frozen=True, slots=True)
class OAuthUsageWindow:
    name: str
    used_percent: float | None
    remaining_percent: float | None
    resets_at: datetime | None


@dataclass(frozen=True, slots=True)
class OAuthLocalStats:
    request_count: int
    input_tokens: int
    output_tokens: int
    cost_usd: float | None


@dataclass(frozen=True, slots=True)
class OAuthRuntimeError:
    model_id: str | None
    message: str | None
    cooldown_until: datetime | None
    cooldown_permanent: bool


@dataclass(frozen=True, slots=True)
class OAuthAccountDetail:
    account: OAuthAccountSummary
    workspace_id: str | None
    workspace_name: str | None
    plan_type: str | None
    expires_at: datetime | None
    usage_windows: tuple[OAuthUsageWindow, ...]
    local_stats: OAuthLocalStats
    runtime_errors: tuple[OAuthRuntimeError, ...]
    credential_configured: bool
    last_model_sync: datetime | None
    workbuddy: dict | None = None


@dataclass(frozen=True, slots=True)
class ManualCredential:
    provider: OAuthProvider
    email: str
    access_token: str
    refresh_token: str
    display_name: str | None = None
    identity_subject: str | None = None
    workspace_id: str | None = None
    project_id: str | None = None
    expires_at: str | None = None


@dataclass(frozen=True, slots=True)
class JsonCredential:
    provider: OAuthProvider
    payload: str


@dataclass(frozen=True, slots=True)
class RefreshTokenCredential:
    provider: OAuthProvider
    refresh_token: str
    email_hint: str | None = None


OAuthCredential = ManualCredential | JsonCredential | RefreshTokenCredential


@dataclass(frozen=True, slots=True)
class CreateOAuthAccountCommand:
    credential: OAuthCredential
    replace_plan_token: str | None = None


@dataclass(frozen=True, slots=True)
class UpdateOAuthAccountCommand:
    display_name: str | None = None
    enabled: bool | None = None
    max_concurrent: int | None = None


@dataclass(frozen=True, slots=True)
class OAuthMutationResult:
    account_id: str
    revision: str
    status: str


@dataclass(frozen=True, slots=True)
class OAuthLoginFlow:
    flow_id: str
    flow_secret: str = field(repr=False)
    provider: OAuthProvider
    auth_url: str | None
    instruction: str | None
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class OAuthLoginPoll:
    flow_id: str
    status: str
    expires_at: datetime
    account_preview: dict | None = None
    account_id: str | None = None
    save_status: str | None = None
    revision: str | None = None


@dataclass(frozen=True, slots=True)
class CompleteOAuthLoginCommand:
    code: str | None = None
    state: str | None = None
    callback_url: str | None = None
    completed: bool | None = None
    replace_plan_token: str | None = None


@dataclass(frozen=True, slots=True)
class OAuthImportCandidate:
    candidate_id: str
    provider: OAuthProvider
    identity: str
    display_name: str
    conflict_account_id: str | None


@dataclass(frozen=True, slots=True)
class OAuthImportProblem:
    index: int | None
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class OAuthImportPreview:
    import_id: str
    import_secret: str = field(repr=False)
    candidates: tuple[OAuthImportCandidate, ...]
    errors: tuple[OAuthImportProblem, ...]
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class OAuthImportDecision:
    candidate_id: str
    action: str


@dataclass(frozen=True, slots=True)
class OAuthImportCommitResult:
    added: tuple[str, ...]
    replaced: tuple[str, ...]
    skipped: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OAuthDeletionPlan:
    plan_token: str
    account_ids: tuple[str, ...]
    expires_at: datetime
    revision: str


@dataclass(frozen=True, slots=True)
class OAuthQuotaResetPlan:
    plan_token: str
    account_id: str
    provider: OAuthProvider
    credit_count: int | None
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class OAuthModel:
    model_id: str
    name: str
    disabled: bool
    cooldown_until: datetime | None
    cooldown_permanent: bool
    metadata_source: str | None
    context_window: int | None
    max_context_window: int | None
    service_tier: str | None
    max_context_default: bool | None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    reasoning_efforts: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class OAuthModelPage:
    items: tuple[OAuthModel, ...]
    meta: PageMeta
    revision: str


@dataclass(frozen=True, slots=True)
class OAuthSettings:
    quota_monitor_enabled: bool
    quota_monitor_interval_seconds: int
    quota_monitor_threshold_percent: float
    cch_mode: CchMode
    revision: str


@dataclass(frozen=True, slots=True)
class TelegramOAuthPreferences:
    usage_display_mode: OAuthUsageDisplayMode
    quota_progress_bar: bool
    revision: str


@dataclass(frozen=True, slots=True)
class OperationHandle:
    operation_id: str
    kind: str


JsonObject = Mapping[str, object]
StringSequence = Sequence[str]
