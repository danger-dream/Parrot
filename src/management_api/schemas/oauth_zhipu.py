"""Zhipu confirmation/selection schemas; credential material is write-only."""
from datetime import datetime
from typing import Literal
from pydantic import Field, SecretStr
from .base import StrictSchema


class ZhipuProjectRequest(StrictSchema):
    flowSecret: SecretStr = Field(min_length=16, max_length=200, json_schema_extra={"writeOnly": True})
    organizationId: str = Field(min_length=1, max_length=500)
    projectId: str = Field(min_length=1, max_length=500)


class ZhipuPlanRequest(StrictSchema):
    action: Literal["opportunity", "use", "create_key"]
    resetType: Literal["FIVE_HOUR", "WEEK"] | None = None
    cardIndex: int = Field(default=0, ge=0, le=100)


class ZhipuPlanData(StrictSchema):
    planToken: str
    accountId: str
    action: str
    expiresAt: datetime
    organizationId: str | None = None
    projectId: str | None = None
    resetType: str | None = None
    cardIndex: int
    expireAt: float | None = None
    resumeOnly: bool = False


class ZhipuViewData(StrictSchema):
    snapshot: dict
    actions: list[dict]
    reset: dict | None = None


class ZhipuActionData(StrictSchema):
    action: str
    status: str
    createdAt: float
    updatedAt: float | None = None
    nextTryAt: float | None = None
    usedAt: float | None = None
    quotaStatus: str | None = None
    httpStatus: int | None = None
    code: int | None = None
    errorKind: str | None = None
    errorStage: str | None = None
    timeoutPhase: str | None = None
    requestNotSent: bool | None = None
    networkPhase: str | None = None
    targetHost: str | None = None
    proxyRoute: str | None = None
    fallbackUsed: bool | None = None
    initialization: dict | None = None
