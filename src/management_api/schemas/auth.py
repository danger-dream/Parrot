"""Typed public schemas for management session grants and approvals."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, SecretStr, StringConstraints

from src.management_auth.principal import AuthMethod, Capability, Role

from .base import StrictSchema


SecretInput = Annotated[SecretStr, StringConstraints(min_length=1, max_length=4096)]
SessionTransport = Literal["bearer", "cookie"]
_TRANSPORT_DESCRIPTION = (
    "bearer (default) returns the credential once in the response; cookie sets "
    "the HttpOnly parrot_management_session cookie and returns credential=null"
)
ClientName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
DeviceSummary = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]


class ManagementKeyGrant(StrictSchema):
    grantType: Literal["managementKey"]
    managementKey: SecretInput = Field(
        json_schema_extra={"writeOnly": True, "examples": ["<write-only>"]}
    )
    sessionTransport: SessionTransport = Field(
        default="bearer", description=_TRANSPORT_DESCRIPTION,
    )
    clientName: ClientName | None = None
    deviceSummary: DeviceSummary | None = None


class TelegramApprovalGrant(StrictSchema):
    grantType: Literal["telegramApproval"]
    approvalId: Annotated[str, StringConstraints(min_length=8, max_length=128)]
    exchangeSecret: SecretInput = Field(
        json_schema_extra={"writeOnly": True, "examples": ["<write-only>"]}
    )
    sessionTransport: SessionTransport = Field(
        default="bearer", description=_TRANSPORT_DESCRIPTION,
    )


SessionGrant = Annotated[
    ManagementKeyGrant | TelegramApprovalGrant,
    Field(discriminator="grantType"),
]


class SessionSummary(StrictSchema):
    sessionId: str
    subjectId: str
    authMethod: AuthMethod
    roles: list[Role]
    capabilities: list[Capability]
    issuedAt: datetime
    expiresAt: datetime
    idleExpiresAt: datetime


class SessionCredentialData(StrictSchema):
    credential: str | None = Field(
        description=(
            "One-time session credential returned only by the exchange response; "
            "null for sessionTransport=cookie (the credential lives only in the "
            "HttpOnly cookie)"
        ),
        examples=["<one-time-credential>"],
    )
    session: SessionSummary


class SessionDeviceData(StrictSchema):
    sessionId: str
    authMethod: AuthMethod
    clientName: str | None = None
    deviceSummary: str | None = None
    sourceAddress: str | None = None
    issuedAt: datetime
    lastSeenAt: datetime
    expiresAt: datetime
    idleExpiresAt: datetime
    current: bool


class RevokeOtherSessionsData(StrictSchema):
    revoked: int = Field(ge=0)


class TelegramApprovalLoginOption(StrictSchema):
    available: bool
    ttlSeconds: int = Field(ge=0)


class ManagementKeyLoginOption(StrictSchema):
    available: bool


class LoginOptionsData(StrictSchema):
    webUiEnabled: bool
    telegramApproval: TelegramApprovalLoginOption
    managementKey: ManagementKeyLoginOption


class TelegramApprovalCreateRequest(StrictSchema):
    clientName: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    deviceSummary: Annotated[
        str | None,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=160),
    ] = None


class TelegramApprovalCreatedData(StrictSchema):
    approvalId: str
    exchangeSecret: str = Field(
        description="One-time challenge secret returned only by the create response",
        examples=["<one-time-exchange-secret>"],
    )
    expiresAt: datetime
    pollAfterSeconds: int = Field(ge=1, le=30)
    confirmCode: str = Field(
        description="4-character code also shown in the Telegram approval message",
        examples=["7KQ2"],
    )


class TelegramApprovalStatusData(StrictSchema):
    approvalId: str
    status: Literal["pending", "approved", "denied", "expired", "consumed"]
    expiresAt: datetime
    pollAfterSeconds: int = Field(ge=1, le=30)
