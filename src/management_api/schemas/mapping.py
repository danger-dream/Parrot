"""Public schemas for model mappings, ingress defaults and compression model."""

from __future__ import annotations

from enum import Enum

from pydantic import Field

from .base import ResponseMeta, StrictSchema


class MappingSort(str, Enum):
    ALIAS = "alias"
    REAL_MODEL = "realModel"
    SOURCE_LINE = "sourceLine"


class Ingress(str, Enum):
    ANTHROPIC = "anthropic"
    OPENAI_CHAT = "openai-chat"
    OPENAI_RESPONSES = "openai-responses"


class MappingData(StrictSchema):
    alias: str
    realModel: str
    sourceLine: str
    revision: str


class MappingListMeta(ResponseMeta):
    page: int
    pageSize: int
    total: int
    hasNext: bool
    revision: str


class MappingListEnvelope(StrictSchema):
    data: list[MappingData]
    meta: MappingListMeta


class MappingEnvelope(StrictSchema):
    data: MappingData
    meta: ResponseMeta


class PutMappingRequest(StrictSchema):
    realModel: str = Field(min_length=1, max_length=300)


class UpdateMappingRequest(StrictSchema):
    alias: str = Field(min_length=1, max_length=300)
    realModel: str = Field(min_length=1, max_length=300)


class IngressDefaultData(StrictSchema):
    ingress: Ingress
    modelId: str | None
    revision: str


class IngressDefaultEnvelope(StrictSchema):
    data: IngressDefaultData
    meta: ResponseMeta


class PutIngressDefaultRequest(StrictSchema):
    modelId: str = Field(min_length=1, max_length=300)


class CompressionModelData(StrictSchema):
    modelId: str | None
    revision: str


class CompressionModelEnvelope(StrictSchema):
    data: CompressionModelData
    meta: ResponseMeta


class PutCompressionModelRequest(StrictSchema):
    modelId: str = Field(min_length=1, max_length=300)
