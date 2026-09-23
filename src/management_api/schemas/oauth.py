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


class OAuthRuntimeErrorData(StrictSchema):
    modelId: str | None = None
    message: str | None = None
    cooldownUntil: datetime | None = None
    cooldownPermanent: bool


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


class ReorderOAuthAccountsRequest(StrictSchema):
    accountIds: list[str] = Field(min_length=0, max_length=10_000)


class OAuthRevisionData(StrictSchema):
    revision: str


class OAuthMutationData(StrictSchema):
    accountId: str
    revision: str
    status: str


class OAuthReplaceConflictData(StrictSchema):
    accountId: str
    replacePlanToken: str


class OAuthIdentityConflictEnvelope(StrictSchema):
    error: ErrorDetailSchema
    conflict: OAuthReplaceConflictData


class StartOAuthLoginFlowRequest(StrictSchema):
    provider: OAuthProvider
    realm: Literal["cn", "global"] | None = None
    clientProfile: Literal["cli", "ide"] | None = None
    site: Literal["bigmodel", "zai"] | None = None

    @model_validator(mode="after")
    def validate_provider_fields(self):
        if self.provider is OAuthProvider.ZHIPU and self.site is None:
            raise ValueError("Zhipu site is required")
        if self.provider is not OAuthProvider.ZHIPU and self.site is not None:
            raise ValueError("site is Zhipu-only")
        if self.provider is not OAuthProvider.WORKBUDDY and (self.realm is not None or self.clientProfile is not None):
            raise ValueError("realm/clientProfile are WorkBuddy-only login options")
        return self


class OAuthLoginFlowData(StrictSchema):
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
    status: Literal["pending", "identity_pending", "select_project", "ready", "completed", "cancelled", "expired", "failed"]
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
    """Explicit empty body; the current domain exposes one reset action."""


class OAuthQuotaResetPlanData(StrictSchema):
    planToken: str
    accountId: str
    provider: OAuthProvider
    creditCount: int | None = None
    expiresAt: datetime


class OAuthClearedCountData(StrictSchema):
    cleared: int


class OAuthOperationEnvelope(StrictSchema):
    data: ManagementOperationData
    meta: "OAuthRequestMeta"


class OAuthRequestMeta(StrictSchema):
    requestId: str


OAuthOperationEnvelope.model_rebuild()
