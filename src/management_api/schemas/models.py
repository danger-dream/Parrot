"""Strict public schemas for the unified model center."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from src.management_control.models import ModelKind, ModelSourceType, ModelStatus

from .base import ResponseMeta, StrictSchema


class ModelOwnerData(StrictSchema):
    type: ModelSourceType
    id: str | None = None


class ModelIdentityData(StrictSchema):
    type: ModelKind
    modelId: str
    provider: str | None = None
    owner: ModelOwnerData | None = None


class ModelSourceData(StrictSchema):
    type: ModelSourceType
    id: str
    label: str
    provider: str
    outboundModel: str
    sourceEnabled: bool
    containerEnabled: bool
    effectiveRoutable: bool
    effectiveMetadata: dict[str, Any]
    valueSource: dict[str, str]
    constrainedBy: dict[str, list[str]]


class ModelData(StrictSchema):
    resourceKey: str
    identity: ModelIdentityData
    modelId: str
    aliases: list[str] = Field(default_factory=list)
    globalEnabled: bool | None = None
    visible: bool | None = None
    sourceCount: int
    commonMetadata: dict[str, Any] = Field(default_factory=dict)
    sources: list[ModelSourceData] = Field(default_factory=list)
    editable: bool
    revision: str


class ModelListMeta(ResponseMeta):
    page: int
    pageSize: int
    total: int
    hasNext: bool
    revision: str


class ModelListEnvelope(StrictSchema):
    data: list[ModelData]
    meta: ModelListMeta


class ModelEnvelope(StrictSchema):
    data: ModelData
    meta: ResponseMeta


class ModelSourceRequest(StrictSchema):
    type: ModelSourceType
    id: str | None = None

    @model_validator(mode="after")
    def validate_source(self) -> "ModelSourceRequest":
        if self.type is ModelSourceType.GLOBAL and self.id is not None:
            raise ValueError("global scope does not accept id")
        if self.type is not ModelSourceType.GLOBAL and not (self.id or "").strip():
            raise ValueError("oauth/api scope requires id")
        return self


class ModelFilterRequest(StrictSchema):
    type: list[ModelKind] = Field(default_factory=list)
    text: str | None = Field(default=None, max_length=300)
    sourceType: ModelSourceType | None = None
    sourceId: str | None = None
    status: list[ModelStatus] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_source(self) -> "ModelFilterRequest":
        if self.sourceType is None and self.sourceId is not None:
            raise ValueError("sourceId requires sourceType")
        if self.sourceType is ModelSourceType.GLOBAL and self.sourceId is not None:
            raise ValueError("global source does not accept sourceId")
        if self.sourceType in {ModelSourceType.OAUTH, ModelSourceType.API} and not (self.sourceId or "").strip():
            raise ValueError("oauth/api source requires sourceId")
        return self


class ModelSelectionRequest(StrictSchema):
    mode: Literal["ids", "filter"]
    modelIds: list[str] = Field(default_factory=list, max_length=10_000)
    filter: ModelFilterRequest | None = None
    excludedModelIds: list[str] = Field(default_factory=list, max_length=10_000)

    @model_validator(mode="after")
    def validate_mode(self) -> "ModelSelectionRequest":
        if self.mode == "ids":
            if not self.modelIds:
                raise ValueError("ids selection requires modelIds")
            if self.filter is not None or self.excludedModelIds:
                raise ValueError("ids selection does not accept filter or excludedModelIds")
        else:
            if self.filter is None:
                raise ValueError("filter selection requires filter")
            if self.modelIds:
                raise ValueError("filter selection does not accept modelIds")
        return self


class ModelStateTargetRequest(StrictSchema):
    enabled: bool | None = None
    visible: bool | None = None

    @model_validator(mode="after")
    def exactly_one(self) -> "ModelStateTargetRequest":
        if (self.enabled is None) == (self.visible is None):
            raise ValueError("target must contain exactly one of enabled or visible")
        return self


class ModelStateRequest(StrictSchema):
    scope: ModelSourceRequest
    selection: ModelSelectionRequest
    target: ModelStateTargetRequest


class ModelStateItemData(StrictSchema):
    modelId: str
    status: Literal["updated", "unchanged"]


class ModelStateData(StrictSchema):
    items: list[ModelStateItemData]
    revision: str


class ModelStateEnvelope(StrictSchema):
    data: ModelStateData
    meta: ResponseMeta
