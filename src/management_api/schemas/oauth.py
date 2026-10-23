"""Strict OAuth account, login, import, and action API schemas."""

from __future__ import annotations

import base64
import binascii
from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator

from src.management_control.oauth.models import (
    OAuthAccountFilter,
    OAuthAccountSort,
    OAuthProvider,
)

from .base import ErrorDetailSchema, StrictSchema
from .operations import ManagementOperationData


class OAuthPageMeta(StrictSchema):
    requestId: str
    page: int
    pageSize: int
    total: int
    hasNext: bool


class OAuthLocalUsageStatsData(StrictSchema):
    """Field names follow StatsMetricData."""
    total: int
    successCount: int
    errorCount: int
    inputTokens: int
    outputTokens: int
    cacheCreationTokens: int
    cacheReadTokens: int
    promptTokens: int
    cacheReadRatio: float | None = None
    averageTokensPerSecond: float | None = None
    maximumTokensPerSecond: float | None = None
    minimumTokensPerSecond: float | None = None
    costTicks: int
    actualCostTicks: int
    estimatedCostTicks: int
    actualCostedSuccess: int
    costedSuccess: int
    unpricedSuccess: int


class OAuthLocalPeriodData(StrictSchema):
    """TG 「💎 本地自然月 / 套餐本期 / 本周 / 账单本期」 local statistics."""
    source: str
    label: str
    moneyLabel: str
    since: datetime
    stats: OAuthLocalUsageStatsData | None = None
    statsLoading: bool = False


class OAuthWindowStatsData(StrictSchema):
    """Local usage inside the current upstream 5h / 7d window (TG detail lines)."""
    since: datetime
    stats: OAuthLocalUsageStatsData | None = None
    statsLoading: bool = False


class OAuthWeeklyProjectionData(StrictSchema):
    tokens: int
    costTicks: int | None = None


class OAuthCooldownSummaryData(StrictSchema):
    accountPaused: bool
    quotaPausedModels: int
    permanentModels: int
    coolingModels: int


class OAuthAccessData(StrictSchema):
    state: Literal["ok", "limited", "blocked"] | None = None
    grokCode: bool | None = None


class OAuthCreditObservationData(StrictSchema):
    observedAt: datetime | None = None
    source: Literal["official_usage", "response_sample"] | None = None
    stale: bool = False


class OAuthPlanFactsData(StrictSchema):
    """Provider-specific plan line facts (TG 🏷️ 套餐 / 订阅 / 到期 …); others null."""
    label: str | None = None
    status: str | None = None
    billingType: str | None = None
    startedAt: datetime | None = None
    expiresAt: datetime | None = None
    tier: str | None = None
    access: OAuthAccessData | None = None
    textModelCount: int | None = None
    imageModelCount: int | None = None
    variantCount: int | None = None
    workspaceLabel: str | None = None
    resetCreditCount: int | None = None
    credits: OAuthCreditObservationData | None = None
    globalDisabledModelCount: int | None = None
    organizationId: str | None = None
    projectId: str | None = None
    site: str | None = None
    credentialMode: str | None = None
    entitlement: str | None = None
    managementStatus: str | None = None
    setupIncomplete: bool | None = None
    region: str | None = None
    accountType: str | None = None
    authMethod: str | None = None
    deviceChat: bool | None = None


class OAuthAccountCardData(StrictSchema):
    statusTag: Literal["userDisabled", "quotaDisabled", "authFailed", "providerRestricted"] | None = None
    plan: OAuthPlanFactsData
    usageSections: list["OAuthUsageSectionData"]
    usageFetchedAt: datetime | None = None
    localPeriod: OAuthLocalPeriodData
    windowStats: dict[str, OAuthWindowStatsData]
    weeklyProjection: OAuthWeeklyProjectionData | None = None
    cooldown: OAuthCooldownSummaryData


class OAuthAccountSummaryData(StrictSchema):
    accountId: str
    provider: OAuthProvider
    displayName: str
    identity: str
    enabled: bool
    disabledReason: str | None = None
    disabledUntil: datetime | None = None
    maxConcurrent: int
    available: bool
    quotaLimited: bool
    invalid: bool
    modelCount: int
    disabledModelCount: int
    credentialConfigured: bool = True
    revision: str
    # Round 3: TG list-card facts (null for Telegram-internal reads only).
    card: OAuthAccountCardData | None = None


class OAuthAccountListData(StrictSchema):
    items: list[OAuthAccountSummaryData]
    revision: str


class OAuthAccountListEnvelope(StrictSchema):
    data: OAuthAccountListData
    meta: OAuthPageMeta


class OAuthUsageWindowData(StrictSchema):
    name: str
    usedPercent: float | None = None
    remainingPercent: float | None = None
    resetsAt: datetime | None = None


class OAuthLocalStatsData(StrictSchema):
    requestCount: int
    inputTokens: int
    outputTokens: int
    costUsd: float | None = None
    # Round 3: TG honest period (套餐本期 / 账单本期 / 本周 / 本地自然月) and the
    # full StatsMetricData-named figures; requestCount/inputTokens/... now use it.
    periodLabel: str | None = None
    periodSource: str | None = None
    moneyLabel: str | None = None
    since: datetime | None = None
    total: int | None = None
    successCount: int | None = None
    errorCount: int | None = None
    cacheCreationTokens: int | None = None
    cacheReadTokens: int | None = None
    promptTokens: int | None = None
    cacheReadRatio: float | None = None
    averageTokensPerSecond: float | None = None
    maximumTokensPerSecond: float | None = None
    minimumTokensPerSecond: float | None = None
    costTicks: int | None = None
    actualCostTicks: int | None = None
    estimatedCostTicks: int | None = None
    actualCostedSuccess: int | None = None
    costedSuccess: int | None = None
    unpricedSuccess: int | None = None


class OAuthRuntimeErrorData(StrictSchema):
    modelId: str | None = None
    message: str | None = None
    cooldownUntil: datetime | None = None
    cooldownPermanent: bool
    errorCount: int = 0
    # fault | quotaPause (Cursor pool, not a failure) | quotaPauseHidden
    # (paused model hidden/disabled on the account) | accountRestriction (11140)
    kind: str = "fault"
    pool: Literal["cursor_models", "other_models", "unknown"] | None = None


class OAuthUsageItemData(StrictSchema):
    label: str
    usedPercent: float | None = None
    remainingPercent: float | None = None
    used: float | None = None
    limit: float | None = None
    remaining: float | None = None
    unit: Literal["usd", "credits", "count"] | None = None
    resetsAt: datetime | None = None
    note: str | None = None
    # Round 3 (windows section): local Parrot usage inside this upstream window.
    local: "OAuthLocalUsageStatsData | None" = None


class OAuthUsageSectionData(StrictSchema):
    id: str
    title: str
    items: list[OAuthUsageItemData]


class OAuthAccountDetailData(StrictSchema):
    account: OAuthAccountSummaryData
    workspaceId: str | None = None
    workspaceName: str | None = None
    planType: str | None = None
    expiresAt: datetime | None = None
    usageWindows: list[OAuthUsageWindowData]
    localStats: OAuthLocalStatsData
    runtimeErrors: list[OAuthRuntimeErrorData]
    credentialConfigured: bool
    lastModelSync: datetime | None = None
    workbuddy: dict | None = None
    zhipu: dict | None = None
    qoder: dict | None = None
    usageSections: list[OAuthUsageSectionData] = Field(default_factory=list)
    usageFetchedAt: datetime | None = None
    allowCredits: bool | None = None
    # Round 3: TG detail facts (null for Telegram-internal reads).
    plan: OAuthPlanFactsData | None = None
    weeklyProjection: "OAuthWeeklyProjectionData | None" = None
    windowStats: "dict[str, OAuthWindowStatsData] | None" = None
    lastRefreshAt: datetime | None = None


# WorkBuddy credentials are acquired only through its browser login flow.
ImportedOAuthProvider = Literal[
    OAuthProvider.CLAUDE, OAuthProvider.OPENAI, OAuthProvider.XAI,
    OAuthProvider.CURSOR, OAuthProvider.ANTIGRAVITY,
]


class ManualOAuthCredential(StrictSchema):
    kind: Literal["manual"]
    provider: ImportedOAuthProvider
    email: str = Field(default="", max_length=320)
    accessToken: SecretStr = Field(min_length=1, json_schema_extra={"writeOnly": True})
    refreshToken: SecretStr = Field(min_length=1, json_schema_extra={"writeOnly": True})
    displayName: str | None = Field(default=None, max_length=200)
    identitySubject: str | None = Field(default=None, max_length=500)
    workspaceId: str | None = Field(default=None, max_length=500)
    projectId: str | None = Field(default=None, max_length=500)
    expiresAt: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def validate_identity(self):
        if not self.email.strip():
            raise ValueError("email is required for this provider")
        return self


class JsonOAuthCredential(StrictSchema):
    kind: Literal["json"]
    provider: Literal[OAuthProvider.CLAUDE, OAuthProvider.OPENAI, OAuthProvider.XAI,
                      OAuthProvider.CURSOR, OAuthProvider.ANTIGRAVITY, OAuthProvider.ZHIPU]
    payload: SecretStr = Field(min_length=2, max_length=200_000, json_schema_extra={"writeOnly": True})


class RefreshTokenOAuthCredential(StrictSchema):
    kind: Literal["refreshToken"]
    provider: ImportedOAuthProvider
    refreshToken: SecretStr = Field(min_length=20, json_schema_extra={"writeOnly": True})
    emailHint: str | None = Field(default=None, max_length=320)


OAuthCredentialRequest = Annotated[
    ManualOAuthCredential | JsonOAuthCredential | RefreshTokenOAuthCredential,
    Field(discriminator="kind"),
]


class CreateOAuthAccountRequest(StrictSchema):
    credential: OAuthCredentialRequest
    replacePlanToken: SecretStr | None = Field(default=None, json_schema_extra={"writeOnly": True})


class UpdateOAuthAccountRequest(StrictSchema):
    displayName: str | None = Field(default=None, min_length=1, max_length=200)
    enabled: bool | None = None
    maxConcurrent: int | None = Field(default=None, ge=0, le=10_000)
    # OpenAI only: TG's 「允许使用积分」 (config field ``allowCredits``).
    allowCredits: bool | None = None


class ReorderOAuthAccountsRequest(StrictSchema):
    accountIds: list[str] = Field(min_length=0, max_length=10_000)


class OAuthRevisionData(StrictSchema):
    revision: str


class OAuthMutationData(StrictSchema):
    accountId: str
    revision: str
    status: str


class OAuthQuotaResetData(OAuthMutationData):
    """Reset receipt; upstream consumption and local recovery are separate facts."""
    upstreamOutcome: str | None = None
    localAction: str | None = None


class OAuthReplaceConflictData(StrictSchema):
    accountId: str
    replacePlanToken: str


class OAuthIdentityConflictEnvelope(StrictSchema):
    error: ErrorDetailSchema
    conflict: OAuthReplaceConflictData


class StartOAuthLoginFlowRequest(StrictSchema):
    provider: OAuthProvider
    realm: Literal["cn", "global"] | None = None
    clientProfile: Literal["cli", "ide", "workbuddy-cli"] | None = None
    site: Literal["bigmodel", "zai"] | None = None
    loginMethod: Literal["device", "pat"] = "device"
    proxyTarget: str | None = Field(default=None, min_length=1, max_length=200)
    reloginAccountId: str | None = Field(default=None, min_length=1, max_length=1000)
    # Deprecated for Qoder: submit PAT only to the confirmed device flow.
    pat: SecretStr | None = Field(default=None, min_length=4, max_length=4096)

    @model_validator(mode="after")
    def validate_provider_fields(self):
        if self.provider is not OAuthProvider.QODER and self.loginMethod != "device":
            raise ValueError("loginMethod=pat is Qoder-only")
        if self.proxyTarget is not None and self.reloginAccountId is not None:
            raise ValueError("proxyTarget applies to new accounts only; a relogin keeps the account's routing")
        if self.provider is OAuthProvider.ZHIPU and self.site is None:
            raise ValueError("Zhipu site is required")
        if self.provider is not OAuthProvider.ZHIPU and self.site is not None:
            raise ValueError("site is Zhipu-only")
        if self.provider not in {OAuthProvider.WORKBUDDY, OAuthProvider.QODER} and self.realm is not None:
            raise ValueError("realm is a WorkBuddy/Qoder login option")
        if self.provider is not OAuthProvider.WORKBUDDY and self.clientProfile is not None:
            raise ValueError("clientProfile is a WorkBuddy-only login option")
        if self.pat is not None and (self.provider is not OAuthProvider.QODER or self.realm == "cn"):
            raise ValueError("pat is a Qoder international login option")
        return self


class OAuthLoginFlowData(StrictSchema):
    devicePreview: dict | None = None
    flowId: str
    flowSecret: str
    provider: OAuthProvider
    authUrl: str | None = None
    instruction: str | None = None
    expiresAt: datetime


class PollOAuthLoginFlowRequest(StrictSchema):
    flowSecret: SecretStr = Field(min_length=16, max_length=200, json_schema_extra={"writeOnly": True})


class OAuthLoginPollData(StrictSchema):
    flowId: str
    status: Literal["preparing", "confirmed", "pending", "identity_pending", "select_project", "ready", "completed", "cancelled", "expired", "failed"]
    expiresAt: datetime
    accountPreview: dict | None = None
    accountId: str | None = None
    saveStatus: Literal["created", "replaced"] | None = None
    revision: str | None = None


class CompleteOAuthLoginFlowRequest(StrictSchema):
    flowSecret: SecretStr | None = Field(
        default=None,
        min_length=16,
        max_length=200,
        json_schema_extra={"writeOnly": True},
    )
    code: SecretStr | None = Field(default=None, json_schema_extra={"writeOnly": True})
    state: SecretStr | None = Field(default=None, json_schema_extra={"writeOnly": True})
    callbackUrl: SecretStr | None = Field(default=None, json_schema_extra={"writeOnly": True})
    completed: bool | None = None
    replacePlanToken: SecretStr | None = Field(default=None, json_schema_extra={"writeOnly": True})


_MAX_IMPORT_ENCODED_PAYLOAD_CHARS = 2_000_000
_MAX_IMPORT_DECODED_PAYLOAD_BYTES = 1_500_000


class PreviewOAuthImportRequest(StrictSchema):
    format: Literal["openai", "cpa", "sub2api", "zhipu"]
    payloadEncoding: Literal["json", "base64"] = "json"
    payload: SecretStr = Field(
        min_length=1,
        max_length=_MAX_IMPORT_ENCODED_PAYLOAD_CHARS,
        json_schema_extra={"writeOnly": True},
    )
    filename: str | None = Field(default=None, max_length=255)

    @field_validator("payload")
    @classmethod
    def validate_encoded_payload(cls, value: SecretStr, info):
        if info.data.get("payloadEncoding") == "base64":
            cls._decode_base64(value.get_secret_value())
        return value

    @staticmethod
    def _decode_base64(value: str) -> bytes:
        try:
            decoded = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("payload must be valid base64") from exc
        if len(decoded) > _MAX_IMPORT_DECODED_PAYLOAD_BYTES:
            raise ValueError("decoded payload exceeds the 1500000 byte limit")
        return decoded

    def parser_payload(self) -> str | bytes:
        value = self.payload.get_secret_value()
        if self.payloadEncoding == "json":
            return value
        return self._decode_base64(value)


class OAuthImportCandidateData(StrictSchema):
    candidateId: str
    provider: OAuthProvider
    identity: str
    displayName: str
    conflictAccountId: str | None = None


class OAuthImportProblemData(StrictSchema):
    index: int | None = None
    code: str
    message: str


class OAuthImportPreviewData(StrictSchema):
    importId: str
    importSecret: str
    candidates: list[OAuthImportCandidateData]
    errors: list[OAuthImportProblemData]
    expiresAt: datetime


class OAuthImportDecisionRequest(StrictSchema):
    candidateId: str = Field(min_length=1, max_length=128)
    action: Literal["keep", "overwrite"]


class CommitOAuthImportRequest(StrictSchema):
    importSecret: SecretStr = Field(min_length=16, max_length=200, json_schema_extra={"writeOnly": True})
    decisions: list[OAuthImportDecisionRequest] = Field(max_length=10_000)


class OAuthImportCommitData(StrictSchema):
    added: list[str]
    replaced: list[str]
    skipped: list[str]


class InvalidOAuthDeletionPlanRequest(StrictSchema):
    all: bool = False
    accountIds: list[str] | None = Field(default=None, max_length=10_000)


class OAuthDeletionPlanData(StrictSchema):
    planToken: str
    accountIds: list[str]
    expiresAt: datetime
    revision: str


class CommitPlanRequest(StrictSchema):
    planToken: SecretStr = Field(json_schema_extra={"writeOnly": True})


class OAuthDeletedCountData(StrictSchema):
    deleted: int


class OAuthQuotaResetPlanRequest(StrictSchema):
    """OpenAI may pick one reset card (creditId from listOAuthOpenAiResetCredits);
    omitted → the upstream's default card. Other providers send ``{}``."""
    creditId: str | None = Field(default=None, min_length=1, max_length=200)


class OAuthQuotaResetPlanData(StrictSchema):
    planToken: str
    accountId: str
    provider: OAuthProvider
    creditCount: int | None = None
    creditId: str | None = None
    expiresAt: datetime


class OAuthClearedCountData(StrictSchema):
    cleared: int


class OAuthOperationEnvelope(StrictSchema):
    data: ManagementOperationData
    meta: "OAuthRequestMeta"


class OAuthRequestMeta(StrictSchema):
    requestId: str


OAuthOperationEnvelope.model_rebuild()


OAuthUsageItemData.model_rebuild()
OAuthAccountCardData.model_rebuild()
OAuthAccountSummaryData.model_rebuild()
OAuthAccountDetailData.model_rebuild()
