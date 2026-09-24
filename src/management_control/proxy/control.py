"""Shared proxy CRUD, probes, groups and routing use cases."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from ipaddress import ip_address
from typing import Any, Mapping

from src import config, log_db, oauth_manager
from src.channel import registry
from src.management_auth.principal import Capability
from src.management_control.context import AuditSink, ManagementContext
from src.management_control.errors import ErrorField, ManagementError, ManagementErrorCode
from src.management_control.models.common import DomainControl, ListPage, stable_revision
from src.management_control.operations import OperationStore
from src.management_control.routing_account_ids import (
    oauth_account_id_from_channel_key,
    oauth_channel_key_from_account_id,
)
from src.proxy import manager as proxy_manager
from src.proxy.connector import _mask_url, parse_proxy_url
from src.proxy.ss2022 import ss_family_label
from src.proxy.routing_types import PROVIDER_KEYS


_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,30}$")
_FUNCTION_ROUTES = frozenset({"telegram", "oauth_anthropic", "oauth_openai"})


@dataclass(frozen=True, slots=True)
class ProxyRecord:
    proxy_id: str
    name: str
    type: str
    masked_url: str
    server: str | None
    port: int | None
    cipher: str | None
    runtime_stats: Mapping[str, Any]
    revision: str


@dataclass(frozen=True, slots=True)
class ProxyGroupRecord:
    group_id: str
    name: str
    members: tuple[str, ...]
    runtime_stats: Mapping[str, Any]
    revision: str


@dataclass(frozen=True, slots=True)
class ProxyRoutingRecord:
    default: str
    direct_fallback: bool
    functions: Mapping[str, str]
    providers: Mapping[str, str]
    accounts: Mapping[str, str]
    channels: Mapping[str, str]
    models: Mapping[str, str]
    revision: str


@dataclass(slots=True)
class TelegramProxyView:
    name: str
    type: str
    url: str
    server: str | None
    port: int | None
    cipher: str | None
    stats: Any

    def display(self) -> str:
        if self.type == "direct":
            return "direct"
        return self.url or f"{self.server}:{self.port}"


class ProxyControl(DomainControl):
    def __init__(
        self,
        *,
        audit_sink: AuditSink | None = None,
        operation_store: OperationStore | None = None,
    ) -> None:
        super().__init__(audit_sink=audit_sink)
        self._operation_store = operation_store

    @staticmethod
    def _network_snapshot() -> dict[str, Any]:
        network = config.get().get("network") or {}
        return {
            "proxies": network.get("proxies") or {},
            "groups": network.get("groups") or {},
            "routing": network.get("routing") or {},
        }

    @classmethod
    def _revision(cls) -> str:
        return stable_revision(cls._network_snapshot())

    @staticmethod
    def _stats() -> dict[str, dict]:
        return {
            str(item.get("proxy_name")): dict(item)
            for item in log_db.proxy_stats(limit=1000)
            if item.get("proxy_name")
        }

    @staticmethod
    def _merged_stats(members: list[str], stats: Mapping[str, Mapping[str, Any]]) -> dict:
        sums = {
            "requests": 0,
            "successes": 0,
            "failures": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_tokens": 0,
            "cache_read_tokens": 0,
            "total_tokens": 0,
            "bytes_up": 0,
            "bytes_down": 0,
            "total_bytes": 0,
            "connect_sum_ms": 0,
            "connect_sample_count": 0,
            "first_byte_sum_ms": 0,
            "first_byte_sample_count": 0,
            "idle_sum_ms": 0,
            "idle_sample_count": 0,
            "total_sum_ms": 0,
            "total_sample_count": 0,
        }
        for member in members:
            item = stats.get(member) or {}
            for key in sums:
                sums[key] += int(item.get(key, 0) or 0)
        for average, total, count in (
            ("avg_connect_ms", "connect_sum_ms", "connect_sample_count"),
            ("avg_first_byte_ms", "first_byte_sum_ms", "first_byte_sample_count"),
            ("avg_idle_ms", "idle_sum_ms", "idle_sample_count"),
            ("avg_total_ms", "total_sum_ms", "total_sample_count"),
        ):
            sums[average] = round(sums[total] / sums[count]) if sums[count] else 0
        return sums

    @staticmethod
    def _masked_config_url(proxy_cfg: Mapping[str, Any]) -> str:
        if proxy_cfg.get("url"):
            return _mask_url(str(proxy_cfg.get("url") or ""))
        if proxy_cfg.get("type") == "ss2022":
            cipher = str(proxy_cfg.get("cipher") or "")
            server = str(proxy_cfg.get("server") or "")
            port = proxy_cfg.get("port") or ""
            return f"ss://{cipher}:***@{server}:{port}"
        return ""

    @classmethod
    def _record(
        cls,
        name: str,
        proxy_cfg: Mapping[str, Any],
        stats: Mapping[str, Any],
        revision: str,
    ) -> ProxyRecord:
        return ProxyRecord(
            proxy_id=name,
            name=name,
            type=str(proxy_cfg.get("type") or "unknown"),
            masked_url=cls._masked_config_url(proxy_cfg),
            server=str(proxy_cfg.get("server") or "") or None,
            port=int(proxy_cfg["port"]) if proxy_cfg.get("port") is not None else None,
            cipher=str(proxy_cfg.get("cipher") or "") or None,
            runtime_stats=dict(stats),
            revision=revision,
        )

    def list_proxies(
        self,
        context: ManagementContext,
        *,
        type_filter: str | None,
        query: str | None,
        sort: str,
        page: int,
        page_size: int,
    ) -> ListPage[ProxyRecord]:
        self._read(context)
        snapshot = self._network_snapshot()
        revision = stable_revision(snapshot)
        stats = self._stats()
        values = [
            self._record(str(name), raw, stats.get(str(name), {}), revision)
            for name, raw in snapshot["proxies"].items()
            if isinstance(raw, Mapping)
        ]
        if type_filter:
            values = [item for item in values if item.type == type_filter]
        needle = (query or "").strip().casefold()
        if needle:
            values = [item for item in values if needle in item.name.casefold()]
        key = {
            "name": lambda item: item.name.casefold(),
            "type": lambda item: (item.type, item.name.casefold()),
            "requests": lambda item: (-int(item.runtime_stats.get("requests", 0)), item.name.casefold()),
        }[sort]
        return self._paginate(
            sorted(values, key=key), page, page_size, revision=revision
        )

    @staticmethod
    def _validate_name(name: str, path: str = "name") -> str:
        value = str(name or "").strip().lower()
        if value == "direct" or not _NAME_RE.fullmatch(value):
            raise ProxyControl._validation(
                path,
                "INVALID_PROXY_NAME",
                "name must be 1-31 lowercase letters, digits, hyphen or underscore and not direct",
            )
        return value

    @staticmethod
    def _parse_url(url: str) -> dict[str, Any]:
        try:
            parsed = dict(parse_proxy_url(str(url or "").strip()))
        except ValueError:
            raise ProxyControl._validation(
                "url", "INVALID_PROXY_URL", "proxy URL is invalid"
            ) from None
        parsed.pop("name", None)
        return parsed

    def create_proxy(
        self,
        context: ManagementContext,
        *,
        name: str,
        url: str,
    ) -> ProxyRecord:
        actual = self._write(context, Capability.SECRETS_WRITE)
        name = self._validate_name(name)
        proxy_cfg = self._parse_url(url)
        with config.serialized_updates():
            snapshot = self._network_snapshot()
            if name in snapshot["proxies"] or name in snapshot["groups"]:
                raise ManagementError(ManagementErrorCode.RESOURCE_CONFLICT)
            proxy_manager.add_proxy(name, proxy_cfg)
        result = self.get_proxy(context, name)
        self._audit(actual, "proxy.create", name, "succeeded")
        return result

    def get_proxy(self, context: ManagementContext, proxy_id: str) -> ProxyRecord:
        self._read(context)
        snapshot = self._network_snapshot()
        raw = snapshot["proxies"].get(proxy_id)
        if not isinstance(raw, Mapping):
            raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
        return self._record(
            proxy_id, raw, self._stats().get(proxy_id, {}), stable_revision(snapshot)
        )

    def update_proxy(
        self,
        context: ManagementContext,
        proxy_id: str,
        *,
        name: str | None,
        url: str | None,
        expected_revision: str | None,
    ) -> ProxyRecord:
        actual = self._write(
            context, Capability.SECRETS_WRITE if url is not None else Capability.WRITE
        )
        current_revision = self._revision()
        self._check_revision(expected_revision, current_revision)
        new_name = self._validate_name(name) if name is not None else proxy_id
        parsed = self._parse_url(url) if url is not None else None
        snapshot = self._network_snapshot()
        current = snapshot["proxies"].get(proxy_id)
        if not isinstance(current, Mapping):
            raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
        if new_name != proxy_id and (
            new_name in snapshot["proxies"] or new_name in snapshot["groups"]
        ):
            raise ManagementError(ManagementErrorCode.RESOURCE_CONFLICT)

        def mutate(candidate: dict) -> None:
            network = candidate.setdefault("network", {})
            proxies = network.setdefault("proxies", {})
            raw = dict(proxies.pop(proxy_id))
            if parsed is not None:
                raw = parsed
            proxies[new_name] = raw
            if new_name != proxy_id:
                for members in (network.get("groups") or {}).values():
                    if isinstance(members, list):
                        members[:] = [new_name if item == proxy_id else item for item in members]
                self._replace_routing_target(network.get("routing") or {}, proxy_id, new_name)

        with config.serialized_updates():
            self._check_revision(expected_revision, self._revision(), required=True)
            config.update(mutate)
        result = self.get_proxy(context, new_name)
        self._audit(actual, "proxy.update", proxy_id, "succeeded")
        return result

    @staticmethod
    def _replace_routing_target(routing: dict, old: str, new: str | None) -> None:
        for key, value in list(routing.items()):
            if value == old:
                if new is None:
                    del routing[key]
                else:
                    routing[key] = new
            elif isinstance(value, dict):
                for child, target in list(value.items()):
                    if target == old:
                        if new is None:
                            del value[child]
                        else:
                            value[child] = new

    @staticmethod
    def _routing_references(routing: Mapping[str, Any], target: str) -> list[str]:
        references: list[str] = []
        for key, value in routing.items():
            if value == target:
                references.append(f"routing.{key}")
            elif isinstance(value, Mapping):
                for child, child_target in value.items():
                    if child_target != target:
                        continue
                    public_child = (
                        oauth_account_id_from_channel_key(child)
                        if key == "accounts" else child
                    )
                    references.append(f"routing.{key}.{public_child}")
        return references

    @staticmethod
    def _referenced_error(references: list[str]) -> ManagementError:
        return ManagementError(
            ManagementErrorCode.RESOURCE_CONFLICT,
            fields=tuple(
                ErrorField(path, "RESOURCE_IN_USE", "resource is still referenced")
                for path in references
            ),
        )

    def delete_proxy(
        self,
        context: ManagementContext,
        proxy_id: str,
        *,
        expected_revision: str | None,
    ) -> None:
        actual = self._write(context, Capability.DESTRUCTIVE)
        with config.serialized_updates():
            self._check_revision(expected_revision, self._revision(), required=True)
            snapshot = self._network_snapshot()
            if proxy_id not in snapshot["proxies"]:
                raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
            references = [
                f"groups.{group_id}[{index}]"
                for group_id, members in snapshot["groups"].items()
                if isinstance(members, list)
                for index, member in enumerate(members)
                if member == proxy_id
            ]
            references.extend(self._routing_references(snapshot["routing"], proxy_id))
            if references:
                raise self._referenced_error(references)
            proxy_manager.remove_proxy(proxy_id)
        self._audit(actual, "proxy.delete", proxy_id, "succeeded")

    def list_groups(
        self,
        context: ManagementContext,
        *,
        query: str | None,
        sort: str,
        page: int,
        page_size: int,
    ) -> ListPage[ProxyGroupRecord]:
        self._read(context)
        snapshot = self._network_snapshot()
        revision = stable_revision(snapshot)
        stats = self._stats()
        values = [
            ProxyGroupRecord(
                group_id=str(name),
                name=str(name),
                members=tuple(str(item) for item in members),
                runtime_stats=self._merged_stats(list(members), stats),
                revision=revision,
            )
            for name, members in snapshot["groups"].items()
            if isinstance(members, list)
        ]
        needle = (query or "").strip().casefold()
        if needle:
            values = [item for item in values if needle in item.name.casefold()]
        key = {
            "name": lambda item: item.name.casefold(),
            "memberCount": lambda item: (-len(item.members), item.name.casefold()),
            "requests": lambda item: (-int(item.runtime_stats.get("requests", 0)), item.name.casefold()),
        }[sort]
        return self._paginate(
            sorted(values, key=key), page, page_size, revision=revision
        )

    def _validated_members(self, members: list[str], *, allow_empty: bool) -> list[str]:
        values = [str(item or "").strip() for item in members]
        if not allow_empty and not values:
            raise self._validation("members", "EMPTY_GROUP", "group must contain a member")
        if len(values) != len(set(values)):
            raise self._validation("members", "DUPLICATE_MEMBER", "members must be unique")
        known = set(self._network_snapshot()["proxies"]) | {"direct"}
        unknown = [item for item in values if item not in known]
        if unknown:
            raise ManagementError(
                ManagementErrorCode.VALIDATION_FAILED,
                fields=(ErrorField("members", "UNKNOWN_PROXY", ", ".join(unknown)),),
            )
        return values

    def create_group(
        self,
        context: ManagementContext,
        *,
        name: str,
        members: list[str],
    ) -> ProxyGroupRecord:
        actual = self._write(context)
        name = self._validate_name(name)
        values = self._validated_members(members, allow_empty=False)
        with config.serialized_updates():
            snapshot = self._network_snapshot()
            if name in snapshot["groups"] or name in snapshot["proxies"]:
                raise ManagementError(ManagementErrorCode.RESOURCE_CONFLICT)
            proxy_manager.add_group(name, values)
        result = self.get_group(context, name)
        self._audit(actual, "proxy_group.create", name, "succeeded")
        return result

    def get_group(self, context: ManagementContext, group_id: str) -> ProxyGroupRecord:
        self._read(context)
        snapshot = self._network_snapshot()
        members = snapshot["groups"].get(group_id)
        if not isinstance(members, list):
            raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
        return ProxyGroupRecord(
            group_id=group_id,
            name=group_id,
            members=tuple(str(item) for item in members),
            runtime_stats=self._merged_stats(members, self._stats()),
            revision=stable_revision(snapshot),
        )

    def update_group(
        self,
        context: ManagementContext,
        group_id: str,
        *,
        name: str | None,
        members: list[str] | None,
        expected_revision: str | None,
    ) -> ProxyGroupRecord:
        actual = self._write(context)
        self._check_revision(expected_revision, self._revision())
        snapshot = self._network_snapshot()
        current = snapshot["groups"].get(group_id)
        if not isinstance(current, list):
            raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
        new_name = self._validate_name(name) if name is not None else group_id
        if new_name != group_id and (
            new_name in snapshot["groups"] or new_name in snapshot["proxies"]
        ):
            raise ManagementError(ManagementErrorCode.RESOURCE_CONFLICT)
        values = self._validated_members(members, allow_empty=True) if members is not None else list(current)

        def mutate(candidate: dict) -> None:
            network = candidate.setdefault("network", {})
            groups = network.setdefault("groups", {})
            groups.pop(group_id, None)
            groups[new_name] = values
            if new_name != group_id:
                self._replace_routing_target(network.get("routing") or {}, group_id, new_name)

        with config.serialized_updates():
            self._check_revision(expected_revision, self._revision(), required=True)
            config.update(mutate)
        result = self.get_group(context, new_name)
        self._audit(actual, "proxy_group.update", group_id, "succeeded")
        return result

    def delete_group(
        self,
        context: ManagementContext,
        group_id: str,
        *,
        expected_revision: str | None,
    ) -> None:
        actual = self._write(context, Capability.DESTRUCTIVE)
        with config.serialized_updates():
            self._check_revision(expected_revision, self._revision(), required=True)
            snapshot = self._network_snapshot()
            if group_id not in snapshot["groups"]:
                raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
            references = [
                f"groups.{parent_id}[{index}]"
                for parent_id, members in snapshot["groups"].items()
                if parent_id != group_id and isinstance(members, list)
                for index, member in enumerate(members)
                if member == group_id
            ]
            references.extend(self._routing_references(snapshot["routing"], group_id))
            if references:
                raise self._referenced_error(references)
            proxy_manager.remove_group(group_id)
        self._audit(actual, "proxy_group.delete", group_id, "succeeded")

    @staticmethod
    def _routing_record(snapshot: Mapping[str, Any], revision: str) -> ProxyRoutingRecord:
        routing = snapshot.get("routing") or {}
        functions = {
            key: str(routing[key]) for key in _FUNCTION_ROUTES if routing.get(key)
        }
        return ProxyRoutingRecord(
            default=str(routing.get("default") or "direct"),
            direct_fallback=bool(routing.get("directFallback", False)),
            functions=functions,
            providers=dict(routing.get("providers") or {}),
            accounts={
                oauth_account_id_from_channel_key(key): target
                for key, target in (routing.get("accounts") or {}).items()
            },
            channels=dict(routing.get("channels") or {}),
            models=dict(routing.get("models") or {}),
            revision=revision,
        )

    def get_routing(self, context: ManagementContext) -> ProxyRoutingRecord:
        self._read(context)
        snapshot = self._network_snapshot()
        return self._routing_record(snapshot, stable_revision(snapshot))

    def _validate_target(self, target: str | None, path: str) -> None:
        if target is None:
            return
        snapshot = self._network_snapshot()
        known = {"direct"} | set(snapshot["proxies"]) | set(snapshot["groups"])
        if target not in known:
            raise self._validation(path, "UNKNOWN_TARGET", "unknown proxy routing target")

    def update_routing(
        self,
        context: ManagementContext,
        patch: Mapping[str, Any],
        *,
        expected_revision: str | None,
    ) -> ProxyRoutingRecord:
        actual = self._write(context)
        self._check_revision(expected_revision, self._revision())
        normalized_patch = dict(patch)
        if "accounts" in patch:
            normalized_accounts: dict[str, Any] = {}
            public_ids: dict[str, str] = {}
            for account_id, target in (patch.get("accounts") or {}).items():
                channel_key = oauth_channel_key_from_account_id(account_id)
                if channel_key in normalized_accounts:
                    raise self._validation(
                        f"accounts.{account_id}",
                        "DUPLICATE_ACCOUNT_ID",
                        f"duplicates accountId {public_ids[channel_key]}",
                    )
                normalized_accounts[channel_key] = target
                public_ids[channel_key] = account_id
            normalized_patch["accounts"] = normalized_accounts
        patch = normalized_patch
        for field in ("default",):
            if field in patch:
                self._validate_target(patch[field], field)
        for section in ("functions", "providers", "accounts", "channels", "models"):
            for key, target in (patch.get(section) or {}).items():
                self._validate_target(target, f"{section}.{key}")
        unknown_functions = set((patch.get("functions") or {})) - _FUNCTION_ROUTES
        if unknown_functions:
            raise self._validation(
                "functions", "UNKNOWN_FUNCTION", ", ".join(sorted(unknown_functions))
            )
        unknown_providers = set((patch.get("providers") or {})) - PROVIDER_KEYS
        if unknown_providers:
            raise self._validation(
                "providers", "UNKNOWN_PROVIDER", ", ".join(sorted(unknown_providers))
            )
        channel_by_key = {channel.key: channel for channel in registry.all_channels()}
        for key in (patch.get("accounts") or {}):
            if key not in channel_by_key or channel_by_key[key].type != "oauth":
                raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
        for key in (patch.get("channels") or {}):
            if key not in channel_by_key or channel_by_key[key].type != "api":
                raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
        known_models = set(registry.available_models())
        for key in (patch.get("models") or {}):
            if key not in known_models:
                raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)

        def mutate(candidate: dict) -> None:
            routing = candidate.setdefault("network", {}).setdefault("routing", {})
            if "default" in patch:
                routing["default"] = patch["default"]
            if "directFallback" in patch:
                routing["directFallback"] = bool(patch["directFallback"])
            for section in ("functions", "providers", "accounts", "channels", "models"):
                if section not in patch:
                    continue
                target_section = routing if section == "functions" else routing.setdefault(section, {})
                for key, target in patch[section].items():
                    if target is None:
                        target_section.pop(key, None)
                    else:
                        target_section[key] = target

        with config.serialized_updates():
            self._check_revision(expected_revision, self._revision())
            config.update(mutate)
        result = self.get_routing(context)
        self._audit(actual, "proxy_routing.update", "proxy-routing", "succeeded")
        return result

    @staticmethod
    def _probe_number(raw: Mapping[str, Any], *keys: str) -> int | float | None:
        value = next((raw[key] for key in keys if key in raw), None)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            return None
        return value

    @classmethod
    def _public_probe_item(
        cls,
        raw: Any,
        *,
        member_name: str | None = None,
    ) -> dict[str, Any]:
        item = raw if isinstance(raw, Mapping) else {}
        address = ""
        try:
            address = str(ip_address(str(item.get("ip") or "")))
        except ValueError:
            pass
        ok = item.get("ok") is True
        result: dict[str, Any] = {
            "ok": ok,
            "ip": address,
            "latencyMilliseconds": cls._probe_number(
                item, "latency_ms", "latencyMilliseconds"
            ),
            "traffic": {
                "bytesUp": cls._probe_number(item, "bytes_up", "bytesUp") or 0,
                "bytesDown": cls._probe_number(item, "bytes_down", "bytesDown") or 0,
                "totalBytes": cls._probe_number(item, "total_bytes", "totalBytes") or 0,
            },
            "error": None if ok else {
                "code": ManagementErrorCode.UPSTREAM_ERROR.value,
                "message": "Proxy probe failed",
            },
        }
        if member_name is not None:
            result["name"] = member_name
        return result

    @classmethod
    def _public_probe_results(
        cls,
        raw: Any,
        *,
        group_members: tuple[str, ...] | None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        if group_members is None:
            return cls._public_probe_item(raw)
        values = raw if isinstance(raw, list) else []
        return [
            cls._public_probe_item(
                values[index] if index < len(values) else {},
                member_name=member,
            )
            for index, member in enumerate(group_members)
        ]

    def _start_probe(
        self,
        context: ManagementContext,
        *,
        kind: str,
        target: str,
        group: bool,
    ):
        self._write(context)
        if self._operation_store is None:
            raise ManagementError(ManagementErrorCode.SERVICE_NOT_READY)
        fingerprint = stable_revision({"kind": kind, "target": target})
        replay = self._idempotent_replay(
            context,
            action=kind,
            fingerprint=fingerprint,
            operation_store=self._operation_store,
        )
        if replay is not None:
            return replay
        snapshot = self._network_snapshot()
        container = snapshot["groups"] if group else snapshot["proxies"]
        if target not in container:
            raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
        group_members = (
            tuple(str(member) for member in container[target]) if group else None
        )
        operation = self._operation_store.create(context, kind=kind, cancellable=False)
        self._remember_idempotency(
            context,
            action=kind,
            fingerprint=fingerprint,
            operation_id=operation.id,
        )

        def worker() -> None:
            try:
                self._operation_store.mark_running(operation.id)
                result = asyncio.run(
                    proxy_manager.test_group(target, timeout=10)
                    if group else proxy_manager.test_proxy(target, timeout=10)
                )
                public_result = self._public_probe_results(
                    result, group_members=group_members,
                )
                self._operation_store.succeed(
                    operation.id, {"target": target, "results": public_result}
                )
                self._audit(context, kind, target, "succeeded")
            except Exception:
                self._operation_store.fail(
                    operation.id,
                    code=ManagementErrorCode.UPSTREAM_ERROR,
                    message="proxy probe failed",
                    retryable=True,
                )
                self._audit(context, kind, target, "failed")

        self._operation_store.submit(operation.id, worker)
        return operation

    def start_proxy_test(self, context: ManagementContext, proxy_id: str):
        return self._start_probe(
            context, kind="proxy.test", target=proxy_id, group=False
        )

    def start_group_test(self, context: ManagementContext, group_id: str):
        return self._start_probe(
            context, kind="proxy_group.test", target=group_id, group=True
        )

    # Telegram compatibility façade; return only masked connector views.
    def init(self) -> None:
        self._read(None)
        proxy_manager.init()

    def all_connectors(self) -> dict[str, TelegramProxyView]:
        self._read(None)
        values = {}
        for name, connector in proxy_manager.all_connectors().items():
            values[name] = TelegramProxyView(
                name=name,
                type=connector.type,
                url=_mask_url(str(getattr(connector, "url", "") or "")),
                server=str(getattr(connector, "server", "") or "") or None,
                port=getattr(connector, "port", None),
                cipher=str(getattr(connector, "cipher", "") or "") or None,
                stats=connector.stats,
            )
        return values

    def all_groups(self):
        self._read(None)
        return proxy_manager.all_groups()

    def get_group_members(self, name: str):
        self._read(None)
        groups = proxy_manager.all_groups()
        return list(groups[name]) if name in groups else None

    def get_routing_dict(self):
        self._read(None)
        return proxy_manager.get_routing()

    def get_proxy_stats(self):
        self._read(None)
        return self._stats()

    def parse_proxy_url(self, text: str):
        self._write(None, Capability.SECRETS_WRITE)
        return parse_proxy_url(text)

    def mask_url(self, url: str):
        self._read(None)
        return _mask_url(url)

    def ss_family_label(self, cipher: str):
        self._read(None)
        return ss_family_label(cipher)

    def provider_of(self, account: str):
        self._read(None)
        return oauth_manager.provider_of(account)

    def available_models(self):
        self._read(None)
        return registry.available_models()

    def all_channels(self):
        self._read(None)
        return registry.all_channels()

    def add_proxy(self, name: str, proxy_cfg: dict):
        actual = self._write(None, Capability.SECRETS_WRITE)
        result = proxy_manager.add_proxy(name, proxy_cfg)
        self._audit(actual, "proxy.create", name, "succeeded")
        return result

    def remove_proxy(self, name: str):
        actual = self._write(None, Capability.DESTRUCTIVE)
        result = proxy_manager.remove_proxy(name)
        self._audit(actual, "proxy.delete", name, "succeeded")
        return result

    def test_proxy(self, name: str, *, timeout: float = 8.0):
        self._write(None)
        return proxy_manager.test_proxy(name, timeout=timeout)

    def add_group(self, name: str, members: list[str]):
        actual = self._write(None)
        result = proxy_manager.add_group(name, members)
        self._audit(actual, "proxy_group.update", name, "succeeded")
        return result

    def remove_group(self, name: str):
        actual = self._write(None, Capability.DESTRUCTIVE)
        result = proxy_manager.remove_group(name)
        self._audit(actual, "proxy_group.delete", name, "succeeded")
        return result

    def test_group(self, name: str, *, timeout: float = 8.0):
        self._write(None)
        return proxy_manager.test_group(name, timeout=timeout)

    def direct_fallback_enabled(self):
        self._read(None)
        return proxy_manager.direct_fallback_enabled()

    def set_direct_fallback(self, enabled: bool):
        actual = self._write(None)
        result = proxy_manager.set_direct_fallback(enabled)
        self._audit(actual, "proxy_routing.update", "directFallback", "succeeded")
        return result

    def set_routing(self, key: str, value: str, *, section: str = ""):
        actual = self._write(None)
        result = proxy_manager.set_routing(key, value, section=section)
        self._audit(actual, "proxy_routing.update", f"{section}:{key}", "succeeded")
        return result

    def remove_routing(self, key: str, *, section: str = ""):
        actual = self._write(None)
        result = proxy_manager.remove_routing(key, section=section)
        self._audit(actual, "proxy_routing.update", f"{section}:{key}", "succeeded")
        return result


proxy_control = ProxyControl()
