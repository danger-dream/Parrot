"""Public schemas for proxies, groups, probes and routing rules."""

from __future__ import annotations

from enum import Enum

from pydantic import Field, SecretStr, model_validator

from .base import ResponseMeta, StrictSchema
from .mapping import MappingListMeta
from .operations import ManagementOperationData


class ProxyType(str, Enum):
    SOCKS5 = "socks5"
    SS2022 = "ss2022"


class ProxySort(str, Enum):
    NAME = "name"
    TYPE = "type"
    REQUESTS = "requests"


class ProxyGroupSort(str, Enum):
    NAME = "name"
    MEMBER_COUNT = "memberCount"
    REQUESTS = "requests"


class ProxyRuntimeStats(StrictSchema):
    requests: int = 0
    successes: int = 0
    failures: int = 0
    inputTokens: int = 0
    outputTokens: int = 0
    cacheCreationTokens: int = 0
    cacheReadTokens: int = 0
    totalTokens: int = 0
    bytesUp: int = 0
    bytesDown: int = 0
    totalBytes: int = 0
    avgConnectMilliseconds: int = 0
    avgFirstByteMilliseconds: int = 0
    avgIdleMilliseconds: int = 0
    avgTotalMilliseconds: int = 0


class ProxyData(StrictSchema):
    proxyId: str
    name: str
    type: ProxyType
    maskedUrl: str
    server: str | None
    port: int | None
    cipher: str | None
    runtimeStats: ProxyRuntimeStats
    revision: str


class ProxyEnvelope(StrictSchema):
    data: ProxyData
    meta: ResponseMeta


class ProxyListEnvelope(StrictSchema):
    data: list[ProxyData]
    meta: MappingListMeta


class CreateProxyRequest(StrictSchema):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,30}$")
    url: SecretStr = Field(
        min_length=1,
        max_length=4096,
        json_schema_extra={"writeOnly": True},
    )


class UpdateProxyRequest(StrictSchema):
    name: str | None = Field(
        default=None,
        pattern=r"^[a-z0-9][a-z0-9_-]{0,30}$",
    )
    url: SecretStr | None = Field(
        default=None,
        min_length=1,
        max_length=4096,
        json_schema_extra={"writeOnly": True},
    )

    @model_validator(mode="after")
    def require_change(self):
        if self.name is None and self.url is None:
            raise ValueError("at least one field is required")
        return self


class ProxyGroupData(StrictSchema):
    groupId: str
    name: str
    members: list[str]
    runtimeStats: ProxyRuntimeStats
    revision: str


class ProxyGroupEnvelope(StrictSchema):
    data: ProxyGroupData
    meta: ResponseMeta


class ProxyGroupListEnvelope(StrictSchema):
    data: list[ProxyGroupData]
    meta: MappingListMeta


class CreateProxyGroupRequest(StrictSchema):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,30}$")
    members: list[str] = Field(min_length=1, max_length=200)


class UpdateProxyGroupRequest(StrictSchema):
    name: str | None = Field(
        default=None,
        pattern=r"^[a-z0-9][a-z0-9_-]{0,30}$",
    )
    members: list[str] | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def require_change(self):
        if self.name is None and self.members is None:
            raise ValueError("at least one field is required")
        return self


class ProxyRoutingData(StrictSchema):
    default: str
    directFallback: bool
    functions: dict[str, str]
    providers: dict[str, str]
    accounts: dict[str, str]
    channels: dict[str, str]
    models: dict[str, str]
    revision: str


class ProxyRoutingEnvelope(StrictSchema):
    data: ProxyRoutingData
    meta: ResponseMeta


class UpdateProxyRoutingRequest(StrictSchema):
    # A None default marks an omitted sparse-PATCH field internally.  The public
    # types deliberately exclude null, while mapping values keep null=delete.
    default: str = Field(default=None, min_length=1, max_length=100)
    directFallback: bool = None
    functions: dict[str, str | None] = None
    providers: dict[str, str | None] = Field(
        default=None,
        description="Upstream routes: openai, xai, cursor, antigravity, workbuddy, zhipu, claude. Null values remove overrides; accounts need not exist.",
    )
    accounts: dict[str, str | None] = None
    channels: dict[str, str | None] = None
    models: dict[str, str | None] = None

    @model_validator(mode="after")
    def require_change(self):
        if not self.model_fields_set:
            raise ValueError("at least one field is required")
        return self


class ProxyOperationEnvelope(StrictSchema):
    data: ManagementOperationData
    meta: ResponseMeta
