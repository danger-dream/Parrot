"""Public mapping/metadata DTOs; re-exported by the historical control module."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

@dataclass(frozen=True, slots=True)
class MappingRecord:
    alias: str
    real_model: str
    source_line: str
    revision: str


@dataclass(frozen=True, slots=True)
class IngressDefaultRecord:
    ingress: str
    model_id: str | None
    revision: str


@dataclass(frozen=True, slots=True)
class InventoryRecord:
    model_id: str
    family: str
    provider: str
    channel_id: str
    account_id: str | None
    outbound_model: str
    revision: str


@dataclass(frozen=True, slots=True)
class MetadataRecord:
    model_id: str
    target: str | None
    provider_id: str | None
    catalog_model_id: str | None
    scope: str
    scope_id: str | None
    outbound_model: str | None
    source: str
    authority: str
    effective: Mapping[str, Any]
    raw: Mapping[str, Any]
    value_source: Mapping[str, str]
    constrained_by: Mapping[str, tuple[str, ...]]
    common_override: Mapping[str, Any]
    source_override: Mapping[str, Any]
    revision: str


class MetadataSyncMode(str, Enum):
    ONE = "one"
    SELECTED = "selected"
    SOURCE = "source"
    FULL = "full"


@dataclass(frozen=True, slots=True)
class MetadataOverridePatch:
    set_fields: Mapping[str, Any]
    unset_fields: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MetadataSyncTarget:
    model_id: str
    source: Any | None = None


@dataclass(frozen=True, slots=True)
class CatalogRecord:
    key: str
    model_id: str
    name: str
    provider_id: str
    provider_name: str
    metadata: Mapping[str, Any]
    revision: str
