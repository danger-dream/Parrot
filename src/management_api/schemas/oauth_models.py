"""Strict OAuth account-model API schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from .base import StrictSchema
from .oauth import OAuthPageMeta


class OAuthModelData(StrictSchema):
    modelId: str
    name: str
    disabled: bool
    cooldownUntil: datetime | None = None
    cooldownPermanent: bool
    metadataSource: str | None = None
    contextWindow: int | None = None
    maxContextWindow: int | None = None
    serviceTier: str | None = None
    maxContextDefault: bool | None = None
    maxInputTokens: int | None = None
    maxOutputTokens: int | None = None
    reasoningEfforts: list[str] = Field(default_factory=list)
    # Disabled in the model center and not re-enabled for this account.
    globallyDisabled: bool = False


class OAuthModelListData(StrictSchema):
    items: list[OAuthModelData]
    revision: str


class OAuthModelListEnvelope(StrictSchema):
    data: OAuthModelListData
    meta: OAuthPageMeta


class UpdateOAuthAccountModelsRequest(StrictSchema):
    modelIds: list[str] = Field(min_length=1, max_length=10_000)
    disabled: bool


class UpdateOAuthAccountModelSettingsRequest(StrictSchema):
    modelId: str = Field(min_length=1, max_length=500)
    maxContextDefault: bool
