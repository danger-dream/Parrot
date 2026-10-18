"""WorkBuddy-specific management schemas; no credentials in read responses."""
from datetime import datetime
from typing import Literal

from pydantic import Field, StrictBool

from .base import StrictSchema


class WorkBuddyViewData(StrictSchema):
    snapshot: dict
    actions: list[dict]


class WorkBuddyPlanRequest(StrictSchema):
    action: Literal["checkin", "claim_trial", "claim-trial"]
    allowUnknown: StrictBool = False
    freeTrialConfirmed: StrictBool = False
    retryFailed: StrictBool = False


class WorkBuddyPlanData(StrictSchema):
    planToken: str
    accountId: str
    action: str
    businessDate: str
    expiresAt: datetime
    observedStatus: dict | None = None
    priorResult: dict | None = None
    allowUnknown: bool
    freeTrialConfirmed: bool
    retryFailed: bool


class WorkBuddyRecordPageData(StrictSchema):
    items: list[dict]
    page: int
    pageSize: int
    total: int
    hasNext: bool


class WorkBuddySettingsRequest(StrictSchema):
    autoCheckin: StrictBool


class WorkBuddySettingsData(StrictSchema):
    autoCheckin: bool
    timezone: str
    scheduledTime: str
    scheduledTimes: list[str]
    effectsEnabled: bool
    revision: str


class WorkBuddyPolicyData(StrictSchema):
    clientProfile: str
    clientProfilesByRealm: dict[str, str]
    supportedClientProfilesByRealm: dict[str, list[str]]
    clientRoutes: dict[str, str]
    browserLoginRealms: list[str]
    importRealms: list[str]
    effectsEnabled: bool
    autoCheckinDefault: bool
    autoCheckinTime: str
    autoCheckinTimes: list[str]
    timezone: str
    autoTrial: bool
