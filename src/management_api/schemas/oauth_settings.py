"""Strict OAuth settings, Telegram preference schemas."""

from __future__ import annotations

from pydantic import Field

from src.management_control.oauth.models import CchMode, OAuthUsageDisplayMode

from .base import StrictSchema


class QuotaMonitorData(StrictSchema):
    enabled: bool
    intervalSeconds: int
    thresholdPercent: float


class OAuthSettingsData(StrictSchema):
    quotaMonitor: QuotaMonitorData
    cchMode: CchMode
    revision: str


class UpdateQuotaMonitorRequest(StrictSchema):
    enabled: bool | None = None
    intervalSeconds: int | None = Field(default=None, ge=10, le=86_400)
    thresholdPercent: float | None = Field(default=None, ge=1, le=100)


class UpdateOAuthSettingsRequest(StrictSchema):
    quotaMonitor: UpdateQuotaMonitorRequest | None = None
    cchMode: CchMode | None = None


class TelegramOAuthPreferencesData(StrictSchema):
    usageDisplayMode: OAuthUsageDisplayMode
    quotaProgressBar: bool
    revision: str


class UpdateTelegramOAuthPreferencesRequest(StrictSchema):
    usageDisplayMode: OAuthUsageDisplayMode | None = None
    quotaProgressBar: bool | None = None
