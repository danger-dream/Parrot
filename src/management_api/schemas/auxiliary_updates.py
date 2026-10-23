"""Schemas for application Update Management API operations."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from .base import ResponseMeta, StrictSchema
from .operations import ManagementOperationData


class UpdateSettingsData(StrictSchema):
    enabled: bool
    includePrerelease: bool
    autoUpdate: bool
    intervalSeconds: int
    ignoredVersions: list[str]
    revision: str


class UpdateSettingsPatch(StrictSchema):
    enabled: bool = True
    includePrerelease: bool = True
    autoUpdate: bool = False
    intervalSeconds: int = Field(default=3600, ge=300, le=604800)
    ignoredVersions: list[str] = Field(default_factory=list, max_length=500)


class UpdateCheckData(StrictSchema):
    currentVersion: str
    candidateVersion: str | None
    candidateName: str | None
    changelog: str | None
    publishedAt: datetime | None
    prerelease: bool
    releaseUrl: str | None
    newer: bool
    ignored: bool
    revision: str


class UpdateBackupQuery(StrictSchema):
    mode: Literal["docker", "systemd", "bare", "src"] | None = None
    sort: Literal["createdAtAsc", "createdAtDesc"] = "createdAtDesc"
    page: int = Field(default=1, ge=1)
    pageSize: int = Field(default=50, ge=1, le=200)


class UpdateBackupData(StrictSchema):
    ref: str
    version: str
    targetVersion: str
    mode: str
    createdAt: datetime | None
    revision: str


class UpdateBackupListData(StrictSchema):
    items: list[UpdateBackupData]


class UpdatePageMeta(ResponseMeta):
    page: int
    pageSize: int
    total: int
    hasNext: bool


class UpdateBackupListEnvelope(StrictSchema):
    data: UpdateBackupListData
    meta: UpdatePageMeta


class UpdateFailureLogData(StrictSchema):
    content: str
    revision: str


class StageUpdateOperationData(ManagementOperationData):
    activationPlanToken: str | None = Field(
        default=None,
        min_length=16,
        max_length=256,
    )


class ActivateStagedUpdateRequest(StrictSchema):
    planToken: str = Field(
        min_length=16,
        max_length=256,
        json_schema_extra={"writeOnly": True},
    )


class UpdateStateData(StrictSchema):
    stage: str
    mode: str
    fromVersion: str | None = None
    toVersion: str | None = None
    targetVersion: str | None = None
    message: str | None = None
    repository: str
    currentVersion: str
    busy: bool
    activationAvailable: bool
    revision: str


class UpdateActivationPlanData(StrictSchema):
    planToken: str
    targetVersion: str
    revision: str
    expiresAt: datetime | None = None
