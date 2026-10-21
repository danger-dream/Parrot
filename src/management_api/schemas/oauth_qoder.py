"""Qoder-specific management schemas; no credentials in read responses."""
from typing import Literal

from pydantic import Field, SecretStr, StrictBool

from .base import StrictSchema


class QoderViewData(StrictSchema):
    realm: str | None = None
    uid: str | None = None
    email: str | None = None
    nickname: str | None = None
    label: str | None = None
    route: str
    authMethod: str
    deviceChat: bool
    reloginRequired: bool
    autoCheckin: bool
    checkinSupported: bool
    checkinUnsupportedReason: str | None = None
    checkin: dict
    usage: dict
    device: dict = Field(default_factory=dict)
    refreshStatus: dict[str, str] = Field(default_factory=dict)
    proxy: str | list[str] | None = None


class QoderCheckinData(StrictSchema):
    businessDate: str
    status: str
    source: str
    at: float
    realm: str | None = None
    credit: float | None = None
    until: float | None = None
    errorKind: str | None = None
    errorStatus: int | None = None


class QoderDeviceRequest(StrictSchema):
    flowSecret: SecretStr = Field(min_length=1, max_length=200)
    revision: str = Field(min_length=64, max_length=64)
    pat: SecretStr | None = Field(default=None, min_length=4, max_length=4096)


class QoderDeviceRegenerateRequest(QoderDeviceRequest):
    platform: Literal["linux-x64", "linux-arm64", "macos-x64", "macos-arm64", "windows-x64", "windows-arm64"] | None = None


class QoderSettingsRequest(StrictSchema):
    autoCheckin: StrictBool


class QoderSettingsData(StrictSchema):
    autoCheckin: bool
    timezone: str
    revision: str
