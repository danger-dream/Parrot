"""Schemas for GPT image and xAI media settings operations."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated

from pydantic import Field, model_validator

from .base import StrictSchema


class ImageSettingsData(StrictSchema):
    enabled: bool
    cacheEnabled: bool
    mainModel: str
    toolModel: str
    cachePath: str
    cacheRetentionDays: int
    cacheMaxBytes: int
    revision: str


class ImageSettingsPatch(StrictSchema):
    enabled: bool = True
    cacheEnabled: bool = False
    mainModel: str = Field(default="gpt-5.4-mini", min_length=1, max_length=128)
    toolModel: str = Field(default="gpt-image-2", min_length=1, max_length=128)
    cachePath: str = Field(default="images", min_length=1, max_length=4096)
    cacheRetentionDays: int = Field(default=0, ge=0, le=36500)
    cacheMaxBytes: int = Field(default=1073741824, ge=0, le=2**63 - 1)


class ImageAccountStateData(StrictSchema):
    accountId: str
    email: str
    oauthEnabled: bool
    imageEnabled: bool
    imageCooldownUntil: datetime | None
    missingAccountId: bool
    revision: str


class ImageAccountStatePatch(StrictSchema):
    enabled: bool


class XaiMediaSettingsData(StrictSchema):
    imageModels: list[str]
    videoModels: list[str]
    jobTtlSeconds: int
    requestTimeoutSeconds: int
    revision: str


class XaiMediaSettingsPatch(StrictSchema):
    imageModels: list[str] = Field(default_factory=list, max_length=50)
    videoModels: list[str] = Field(default_factory=list, max_length=50)
    jobTtlSeconds: int = Field(default=10800, ge=1, le=2_147_483_647)
    requestTimeoutSeconds: int = Field(default=180, ge=1, le=2_147_483_647)


class MediaKind(str, Enum):
    IMAGE = "image"
    VIDEO = "video"


class MediaOwnerType(str, Enum):
    GLOBAL = "global"
    OAUTH = "oauth"


class MediaOwnerData(StrictSchema):
    type: MediaOwnerType
    id: str | None = Field(default=None, min_length=1, max_length=512)

    @model_validator(mode="after")
    def validate_owner(self):
        if self.type is MediaOwnerType.GLOBAL and self.id is not None:
            raise ValueError("global owner does not accept id")
        if self.type is MediaOwnerType.OAUTH and self.id is None:
            raise ValueError("oauth owner requires id")
        return self


class MediaModelAddRequest(StrictSchema):
    modelId: str = Field(min_length=1, max_length=128)


class MediaModelRenameRequest(StrictSchema):
    newModelId: str = Field(min_length=1, max_length=128)


class AntigravityMediaModelAddRequest(MediaModelAddRequest):
    modelId: str = Field(min_length=1, max_length=80)
    owner: MediaOwnerData


class AntigravityMediaModelRenameRequest(MediaModelRenameRequest):
    newModelId: str = Field(min_length=1, max_length=80)
    owner: MediaOwnerData


class MediaModelMutationData(StrictSchema):
    provider: str
    kind: MediaKind
    owner: MediaOwnerData
    modelId: str
    models: list[str]
    status: str
    revision: str


class AntigravityMediaAccountOverrideData(StrictSchema):
    accountId: str
    imageModels: list[str]
    editable: bool = False


class AntigravityMediaSettingsData(StrictSchema):
    imageModels: list[str]
    accountOverrides: list[AntigravityMediaAccountOverrideData]
    revision: str


class AntigravityMediaSettingsPatch(StrictSchema):
    imageModels: list[Annotated[str, Field(min_length=1, max_length=80)]] = Field(
        max_length=80,
    )
