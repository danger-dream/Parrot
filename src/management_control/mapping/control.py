"""Shared model mapping and metadata use cases.

This module owns validation and orchestration. Telegram keeps rendering/state while
FastAPI converts these DTOs into public schemas.
"""

from __future__ import annotations

import threading
from typing import Any, Mapping

from src import compact_rescue, config, model_mapping, model_metadata, model_names, model_pricing
from src.channel import registry
from src.management_auth.principal import Capability
from src.management_control.context import AuditSink, ManagementContext
from src.management_control.errors import ManagementError, ManagementErrorCode
from src.management_control.models.common import DomainControl, ListPage, stable_revision
from src.management_control.operations import OperationStore
from src.management_control.routing_account_ids import (
    oauth_account_id_from_channel_key,
    oauth_channel_key_from_account_id,
)
from src.oauth_ids import provider_from_channel_key


from .contracts import (
    CatalogRecord, IngressDefaultRecord, InventoryRecord, MappingRecord,
    MetadataOverridePatch, MetadataRecord, MetadataSyncMode, MetadataSyncTarget,
)
from .sync import MetadataSyncMixin


class MappingControl(MetadataSyncMixin, DomainControl):
    INGRESS_LINES = model_mapping.INGRESS_LINES
    GLOBAL_MAPPING_LINE = model_mapping.GLOBAL_MAPPING_LINE
    INGRESS_LABEL = model_mapping.INGRESS_LABEL
    MetadataBinding = model_metadata.MetadataBinding
    ModelInventoryItem = model_metadata.ModelInventoryItem

    _sync_lock = threading.Lock()
    _sync_running = False

    def __init__(
        self,
        *,
        audit_sink: AuditSink | None = None,
        operation_store: OperationStore | None = None,
    ) -> None:
        super().__init__(audit_sink=audit_sink)
        self._operation_store = operation_store

    @staticmethod
    def _mapping_snapshot() -> dict[str, Any]:
        cfg = config.get()
        return {
            "modelMapping": cfg.get("modelMapping") or {},
            "ingressDefaultModel": cfg.get("ingressDefaultModel") or {},
        }

    @classmethod
    def _mapping_revision(cls) -> str:
        return stable_revision(cls._mapping_snapshot())

    @staticmethod
    def _mapping_sources() -> dict[str, str]:
        root = config.get().get("modelMapping") or {}
        if not isinstance(root, dict):
            return {}
        if root and not any(isinstance(value, dict) for value in root.values()):
            return {str(alias): "global" for alias in root}
        sources: dict[str, str] = {}
        for line in model_mapping.INGRESS_LINES:
            values = root.get(line) or {}
            if isinstance(values, dict):
                sources.update({str(alias): line for alias in values})
        values = root.get(model_mapping.GLOBAL_MAPPING_LINE) or {}
        if isinstance(values, dict):
            sources.update({str(alias): "global" for alias in values})
        return sources

    def list_mappings(
        self,
        context: ManagementContext,
        *,
        query: str | None,
        sort: str,
        page: int,
        page_size: int,
    ) -> ListPage[MappingRecord]:
        self._read(context)
        revision = self._mapping_revision()
        sources = self._mapping_sources()
        items = [
            MappingRecord(alias, real, sources.get(alias, "legacy"), revision)
            for alias, real in model_mapping.get_ingress_map("global").items()
        ]
        needle = (query or "").strip().casefold()
        if needle:
            items = [
                item for item in items
                if needle in item.alias.casefold() or needle in item.real_model.casefold()
            ]
        key = {
            "alias": lambda item: item.alias.casefold(),
            "realModel": lambda item: item.real_model.casefold(),
            "sourceLine": lambda item: (item.source_line, item.alias.casefold()),
        }[sort]
        return self._paginate(
            sorted(items, key=key), page, page_size, revision=revision
        )

    def put_mapping(
        self,
        context: ManagementContext,
        alias: str,
        real_model: str,
        *,
        expected_revision: str | None = None,
    ) -> MappingRecord:
        actual = self._write(context)
        alias = str(alias or "").strip()
        real = str(real_model or "").strip()
        if not alias:
            raise self._validation("alias", "EMPTY", "alias must not be empty")
        if not real:
            raise self._validation("realModel", "EMPTY", "realModel must not be empty")
        if alias == real:
            raise self._validation("realModel", "SELF_MAPPING", "alias and realModel must differ")
        with config.serialized_updates():
            self._check_revision(expected_revision, self._mapping_revision())
            if alias in self._configured_model_names():
                raise ManagementError(ManagementErrorCode.RESOURCE_CONFLICT)
            model_mapping.set_mapping("global", alias, real)
        revision = self._mapping_revision()
        self._audit(actual, "model_mapping.put", alias, "succeeded")
        return MappingRecord(alias, real, "global", revision)

    @staticmethod
    def _configured_model_names() -> set[str]:
        cfg = config.get()
        names: set[str] = set()
        for channel in cfg.get("channels") or ():
            if not isinstance(channel, dict):
                continue
            for item in channel.get("models") or ():
                if isinstance(item, dict):
                    value = item.get("alias") or item.get("real")
                else:
                    value = item
                if isinstance(value, str) and value.strip():
                    names.add(value.strip())
        for account in cfg.get("oauthAccounts") or ():
            if not isinstance(account, dict):
                continue
            from src import oauth_manager
            provider = oauth_manager.provider_of(account)
            selection = oauth_manager.account_model_selection(account)
            for value in selection["models"]:
                if isinstance(value, str) and value.strip():
                    names.add(model_names.public_id(provider, value.strip()))
            for value in account.get("imageModels") or ():
                if isinstance(value, str) and value.strip():
                    names.add(value.strip())
        for section, fields in (
            ("images", ("mainModel", "toolModel")),
            ("xaiOAuth", ("imageModels", "videoModels")),
            ("antigravityOAuth", ("imageModels",)),
        ):
            values = cfg.get(section) or {}
            if not isinstance(values, dict):
                continue
            for field in fields:
                raw = values.get(field)
                raw_values = raw if isinstance(raw, list) else (raw,)
                for value in raw_values:
                    if isinstance(value, str) and value.strip():
                        names.add(value.strip())
        return names

    def update_mapping(
        self,
        context: ManagementContext,
        old_alias: str,
        *,
        new_alias: str,
        real_model: str,
        expected_revision: str | None,
    ) -> MappingRecord:
        """Atomically rename and/or retarget one global mapping record.

        Only the mapping record is changed. API-key allowlists, defaults, load
        balancing, compression, metadata and aliases pointing at the old alias are
        intentionally not rewritten.
        """
        actual = self._write(context)
        old = str(old_alias or "").strip()
        new = str(new_alias or "").strip()
        real = str(real_model or "").strip()
        for path, value in (("oldAlias", old), ("newAlias", new), ("realModel", real)):
            if not value:
                raise self._validation(path, "EMPTY", f"{path} must not be empty")
            if len(value) > 300:
                raise self._validation(path, "TOO_LONG", f"{path} must not exceed 300 characters")
        if new == real:
            raise self._validation("realModel", "SELF_MAPPING", "alias and realModel must differ")

        with config.serialized_updates():
            self._check_revision(
                expected_revision, self._mapping_revision(), required=True,
            )
            visible = model_mapping.get_ingress_map("global")
            if old not in visible:
                raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
            if new in self._configured_model_names() or (new != old and new in visible):
                raise ManagementError(ManagementErrorCode.RESOURCE_CONFLICT)

            def mutate(cfg: dict) -> None:
                model_mapping.remove_aliases_from_config(cfg, {old})
                root = model_mapping._structured_root_for_write(cfg)
                # Taking ownership in global also removes any same-name legacy
                # shadow. It can only be present for new==old here.
                if new == old:
                    model_mapping.remove_aliases_from_config(cfg, {new})
                line = root.setdefault(model_mapping.GLOBAL_MAPPING_LINE, {})
                if not isinstance(line, dict):
                    line = {}
                    root[model_mapping.GLOBAL_MAPPING_LINE] = line
                line[new] = real

            config.update(mutate, skip_if_unchanged=True)

        revision = self._mapping_revision()
        self._audit(actual, "model_mapping.update", old, "succeeded")
        return MappingRecord(new, real, "global", revision)

    def delete_mapping(
        self,
        context: ManagementContext,
        alias: str,
        *,
        expected_revision: str | None = None,
    ) -> None:
        actual = self._write(context, Capability.DESTRUCTIVE)
        with config.serialized_updates():
            self._check_revision(expected_revision, self._mapping_revision())
            if not model_mapping.remove_mapping("global", alias):
                raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
        self._audit(actual, "model_mapping.delete", alias, "succeeded")

    def get_ingress_default(
        self, context: ManagementContext, ingress: str,
    ) -> IngressDefaultRecord:
        self._read(context)
        self._validate_ingress(ingress)
        return IngressDefaultRecord(
            ingress, model_mapping.get_default_model(ingress), self._mapping_revision()
        )

    def put_ingress_default(
        self,
        context: ManagementContext,
        ingress: str,
        model_id: str,
        *,
        expected_revision: str | None = None,
    ) -> IngressDefaultRecord:
        self._write(context)
        self._validate_ingress(ingress)
        # Kept as an explicit compatibility endpoint: inference now requires a
        # client-supplied model, so accepting this write would silently persist
        # an ineffective setting. GET/DELETE remain available for migration.
        raise ManagementError(
            ManagementErrorCode.UNSUPPORTED_VALUE,
            "ingress default models are no longer used; clients must send model",
        )

    def delete_ingress_default(
        self,
        context: ManagementContext,
        ingress: str,
        *,
        expected_revision: str | None = None,
    ) -> None:
        actual = self._write(context, Capability.DESTRUCTIVE)
        self._validate_ingress(ingress)
        with config.serialized_updates():
            self._check_revision(expected_revision, self._mapping_revision())
            if not model_mapping.clear_default(ingress):
                raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
        self._audit(actual, "ingress_default.delete", ingress, "succeeded")

    @staticmethod
    def _validate_ingress(ingress: str) -> None:
        if ingress not in model_mapping.INGRESS_LINES:
            raise MappingControl._validation(
                "ingress", "UNSUPPORTED_INGRESS", "unsupported ingress"
            )

    def list_inventory(
        self,
        context: ManagementContext,
        *,
        provider: str | None,
        family: str | None,
        query: str | None,
        sort: str,
        page: int,
        page_size: int,
    ) -> ListPage[InventoryRecord]:
        self._read(context)
        inventory = list(model_metadata.inventory_items())
        revision = self._metadata_revision(inventory)
        channel_by_key = {channel.key: channel for channel in registry.all_channels()}
        values: list[InventoryRecord] = []
        for item in inventory:
            channel = channel_by_key.get(item.scope_key)
            protocol = str(getattr(channel, "protocol", "anthropic") or "anthropic")
            channel_family = "anthropic" if protocol == "anthropic" else "openai"
            channel_provider = str(getattr(channel, "provider", "") or "")
            if not channel_provider:
                channel_provider = provider_from_channel_key(item.scope_key) or channel_family
            values.append(InventoryRecord(
                model_id=item.client_visible_model,
                family=channel_family,
                provider=channel_provider,
                channel_id=item.scope_key,
                account_id=(
                    oauth_account_id_from_channel_key(item.scope_key)
                    if item.scope_type == "oauth" else None
                ),
                outbound_model=item.outbound_model,
                revision=revision,
            ))
        needle = (query or "").strip().casefold()
        if provider:
            values = [item for item in values if item.provider == provider]
        if family:
            values = [item for item in values if item.family == family]
        if needle:
            values = [
                item for item in values
                if needle in item.model_id.casefold()
                or needle in item.channel_id.casefold()
                or needle in item.provider.casefold()
            ]
        key = {
            "modelId": lambda item: (item.model_id.casefold(), item.channel_id),
            "provider": lambda item: (item.provider, item.model_id.casefold()),
            "channelId": lambda item: (item.channel_id, item.model_id.casefold()),
        }[sort]
        return self._paginate(
            sorted(values, key=key), page, page_size, revision=revision
        )

    @staticmethod
    def _metadata_record(
        model_id: str,
        binding: model_metadata.MetadataBinding | None,
        revision: str,
        *,
        scope: str = "global",
        scope_id: str | None = None,
    ) -> MetadataRecord:
        if binding is None:
            public_scope_id = (
                oauth_account_id_from_channel_key(scope_id)
                if scope == "oauth" and scope_id is not None else scope_id
            )
            return MetadataRecord(
                model_id=model_id,
                target=None,
                provider_id=None,
                catalog_model_id=None,
                scope=scope,
                scope_id=public_scope_id,
                outbound_model=None,
                source="none",
                authority="none",
                effective={},
                raw={},
                value_source={},
                constrained_by={},
                common_override={},
                source_override={},
                revision=revision,
            )
        raw = (
            dict(binding.auto_snapshot.get("metadata") or {})
            if isinstance(binding.auto_snapshot, Mapping) else
            model_pricing.catalog_model(binding.target) or {}
        )
        if "serviceTiers" in raw:
            raw = {**raw, "serviceTiers": model_metadata.service_tier_ids(raw["serviceTiers"])}
        binding_scope = "global" if binding.scope_key is None else (
            "oauth" if binding.scope_key.startswith("oauth:") else "api"
        )
        public_scope_id = (
            oauth_account_id_from_channel_key(binding.scope_key)
            if binding_scope == "oauth" and binding.scope_key is not None
            else binding.scope_key
        )
        return MetadataRecord(
            model_id=model_id,
            target=binding.target,
            provider_id=binding.provider_id,
            catalog_model_id=binding.catalog_model_id,
            scope=binding_scope,
            scope_id=public_scope_id,
            outbound_model=binding.outbound_model,
            source=binding.source,
            authority=binding.authority,
            effective=dict(binding.metadata),
            raw=dict(raw),
            value_source=dict(binding.value_source),
            constrained_by={key: tuple(value) for key, value in binding.constrained_by.items()},
            common_override=dict(binding.common_override),
            source_override=dict(binding.source_override),
            revision=revision,
        )

    def list_metadata(
        self,
        context: ManagementContext,
        *,
        scope: str | None,
        scope_id: str | None,
        query: str | None,
        sort: str,
        page: int,
        page_size: int,
    ) -> ListPage[MetadataRecord]:
        self._read(context)
        inventory = list(model_metadata.inventory_items())
        revision = self._metadata_revision(inventory)
        bindings = list(model_metadata.list_bindings())
        records: list[MetadataRecord] = []
        if scope_id:
            if scope == "global":
                raise self._validation(
                    "scopeId", "NOT_ALLOWED", "global scope does not accept scopeId"
                )
            internal_scope_id, requested_scope = self._scope_resource(
                scope_id, expected_scope=scope,
            )
            current_outbound = {
                item.client_visible_model: item.outbound_model
                for item in inventory
                if item.scope_key == internal_scope_id
            }
            models = set(current_outbound)
            models.update(
                item.client_visible_model for item in bindings
                if item.scope_key == internal_scope_id
            )
            for model_id in sorted(models, key=str.casefold):
                outbound_model = current_outbound.get(model_id)
                binding = None
                if outbound_model is not None:
                    binding = model_metadata.resolve_binding(
                        model_id,
                        scope_key=internal_scope_id,
                        outbound_model=outbound_model,
                    )
                records.append(self._metadata_record(
                    model_id, binding, revision,
                    scope=requested_scope, scope_id=internal_scope_id,
                ))
        else:
            records.extend(
                self._metadata_record(
                    binding.client_visible_model,
                    model_metadata.resolve_binding(
                        binding.client_visible_model,
                        scope_key=binding.scope_key,
                        outbound_model=binding.outbound_model,
                    ),
                    revision,
                )
                for binding in bindings
            )
            represented_globals = {
                item.model_id for item in records if item.scope == "global"
            }
            for model_id in sorted(
                {item.client_visible_model for item in inventory} - represented_globals,
                key=str.casefold,
            ):
                binding = model_metadata.resolve_binding(model_id)
                records.append(self._metadata_record(model_id, binding, revision))
        if scope and not scope_id:
            records = [item for item in records if item.scope == scope]
        needle = (query or "").strip().casefold()
        if needle:
            records = [
                item for item in records
                if needle in item.model_id.casefold()
                or needle in (item.target or "").casefold()
            ]
        key = {
            "modelId": lambda item: item.model_id.casefold(),
            "provider": lambda item: ((item.provider_id or ""), item.model_id.casefold()),
            "source": lambda item: (item.source, item.model_id.casefold()),
        }[sort]
        return self._paginate(
            sorted(records, key=key), page, page_size, revision=revision
        )

    def get_metadata(
        self,
        context: ManagementContext,
        model_id: str,
        *,
        scope_id: str | None = None,
    ) -> MetadataRecord:
        self._read(context)
        inventory = list(model_metadata.inventory_items())
        known = {item.client_visible_model for item in inventory}
        known.update(item.client_visible_model for item in model_metadata.list_bindings())
        if model_id not in known:
            raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
        requested_scope = "global"
        outbound_model = None
        internal_scope_id = scope_id
        if scope_id:
            internal_scope_id, requested_scope = self._scope_resource(scope_id)
            match = next(
                (
                    item for item in inventory
                    if item.scope_key == internal_scope_id
                    and item.client_visible_model == model_id
                ),
                None,
            )
            if match is None:
                raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
            outbound_model = match.outbound_model
        binding = model_metadata.resolve_binding(
            model_id,
            scope_key=internal_scope_id,
            outbound_model=outbound_model,
        )
        return self._metadata_record(
            model_id,
            binding,
            self._metadata_revision(inventory),
            scope=requested_scope,
            scope_id=internal_scope_id,
        )

    @staticmethod
    def _scope_resource(
        scope_id: str,
        *,
        expected_scope: str | None = None,
    ) -> tuple[str, str]:
        oauth_key = oauth_channel_key_from_account_id(scope_id)
        if expected_scope == "api":
            candidates = (scope_id,)
        elif expected_scope == "oauth":
            candidates = (
                (scope_id,) if oauth_key == scope_id else (oauth_key, scope_id)
            )
        elif oauth_key == scope_id:
            candidates = (scope_id,)
        else:
            # Canonical OAuth account IDs must resolve before any coincidentally
            # named API channel when the generic scopeId selector is used.
            candidates = (oauth_key, scope_id)
        channel = None
        internal_scope_id = scope_id
        for candidate in candidates:
            channel = registry.get_channel(candidate)
            if channel is not None:
                internal_scope_id = candidate
                break
        if channel is None:
            raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
        actual_scope = str(getattr(channel, "type", "") or "")
        if actual_scope not in {"oauth", "api"}:
            raise MappingControl._validation(
                "scopeId", "UNSUPPORTED_SCOPE_TYPE", "scopeId is not an OAuth or API scope"
            )
        if expected_scope is not None and expected_scope != actual_scope:
            raise MappingControl._validation(
                "scopeId", "SCOPE_TYPE_MISMATCH", "scopeId type does not match scope"
            )
        return internal_scope_id, actual_scope

    @staticmethod
    def _scope_key(
        scope: str,
        *,
        account_id: str | None,
        channel_id: str | None,
    ) -> str | None:
        if scope == "global":
            extra = "accountId" if account_id is not None else (
                "channelId" if channel_id is not None else None
            )
            if extra:
                raise MappingControl._validation(
                    extra, "NOT_ALLOWED", f"global scope does not accept {extra}"
                )
            return None
        if scope not in {"oauth", "api"}:
            raise MappingControl._validation(
                "scope", "UNSUPPORTED_SCOPE", "unsupported metadata scope"
            )
        if scope == "oauth" and channel_id is not None:
            raise MappingControl._validation(
                "channelId", "NOT_ALLOWED", "oauth scope does not accept channelId"
            )
        if scope == "api" and account_id is not None:
            raise MappingControl._validation(
                "accountId", "NOT_ALLOWED", "api scope does not accept accountId"
            )
        value = account_id if scope == "oauth" else channel_id
        field = "accountId" if scope == "oauth" else "channelId"
        if not value:
            raise MappingControl._validation(field, "REQUIRED", f"{field} is required")
        internal_scope_id, _ = MappingControl._scope_resource(
            value, expected_scope=scope,
        )
        return internal_scope_id

    def put_binding(
        self,
        context: ManagementContext,
        model_id: str,
        *,
        scope: str,
        target_model_id: str,
        provider_id: str,
        account_id: str | None,
        channel_id: str | None,
        outbound_model: str | None,
        expected_revision: str | None = None,
    ) -> MetadataRecord:
        actual = self._write(context)
        scope_key = self._scope_key(scope, account_id=account_id, channel_id=channel_id)
        if scope == "global" and outbound_model is not None:
            raise self._validation(
                "outboundModel", "NOT_ALLOWED", "global scope does not accept outboundModel"
            )
        target = str(target_model_id or "").strip().lower()
        if not target.startswith(provider_id.lower() + "/"):
            raise self._validation(
                "providerId", "TARGET_PROVIDER_MISMATCH", "providerId must match targetModelId"
            )
        if scope_key and not outbound_model:
            raise self._validation(
                "outboundModel", "REQUIRED", "scoped binding requires outboundModel"
            )
        try:
            with config.serialized_updates():
                self._check_revision(expected_revision, self._metadata_revision())
                model_metadata.set_binding(
                    model_id,
                    target,
                    scope_key=scope_key,
                    outbound_model=outbound_model,
                    source="management-api" if context.actor.session_id else "manual",
                )
        except ValueError as exc:
            raise self._validation("targetModelId", "UNKNOWN_CATALOG_MODEL", str(exc)) from exc
        binding = model_metadata.resolve_binding(
            model_id, scope_key=scope_key, outbound_model=outbound_model
        )
        self._audit(actual, "model_metadata.binding.put", model_id, "succeeded")
        return self._metadata_record(model_id, binding, self._metadata_revision())

    def delete_binding_control(
        self,
        context: ManagementContext,
        model_id: str,
        *,
        scope: str,
        account_id: str | None,
        channel_id: str | None,
        expected_revision: str | None = None,
    ) -> None:
        actual = self._write(context, Capability.DESTRUCTIVE)
        scope_key = self._scope_key(scope, account_id=account_id, channel_id=channel_id)
        with config.serialized_updates():
            self._check_revision(expected_revision, self._metadata_revision())
            if not model_metadata.delete_binding(model_id, scope_key=scope_key):
                raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
        self._audit(actual, "model_metadata.binding.delete", model_id, "succeeded")

    def _override_scope(
        self,
        model_id: str,
        *,
        scope: str,
        account_id: str | None,
        channel_id: str | None,
        outbound_model: str | None,
    ) -> tuple[str | None, str | None, str]:
        scope_key = self._scope_key(
            scope, account_id=account_id, channel_id=channel_id,
        )
        inventory = list(model_metadata.inventory_items())
        requested_outbound = str(outbound_model or "").strip() or None
        if scope_key is None:
            if requested_outbound is not None:
                raise self._validation(
                    "outboundModel", "NOT_ALLOWED",
                    "global scope does not accept outboundModel",
                )
            known = {item.client_visible_model for item in inventory}
            known.update(item.client_visible_model for item in model_metadata.list_bindings())
            common, _ = model_metadata.get_override_fields(model_id)
            if model_id not in known and not common:
                raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
            return None, None, "global"
        match = next((
            item for item in inventory
            if item.scope_key == scope_key and item.client_visible_model == model_id
        ), None)
        if match is None:
            raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
        if requested_outbound is not None and requested_outbound != match.outbound_model:
            raise self._validation(
                "outboundModel", "OUTBOUND_MISMATCH",
                "outboundModel does not match the current source model",
            )
        return scope_key, match.outbound_model, scope

    def patch_metadata_overrides(
        self,
        context: ManagementContext,
        model_id: str,
        *,
        scope: str,
        account_id: str | None,
        channel_id: str | None,
        outbound_model: str | None,
        patch: MetadataOverridePatch,
        expected_revision: str | None,
    ) -> MetadataRecord:
        actual = self._write(context)
        scope_key, current_outbound, requested_scope = self._override_scope(
            model_id, scope=scope, account_id=account_id, channel_id=channel_id,
            outbound_model=outbound_model,
        )
        try:
            with config.serialized_updates():
                self._check_revision(
                    expected_revision, self._metadata_revision(), required=True,
                )
                model_metadata.patch_override_fields(
                    model_id,
                    scope_key=scope_key,
                    outbound_model=current_outbound,
                    set_fields=patch.set_fields,
                    unset_fields=patch.unset_fields,
                )
        except ValueError as exc:
            raise self._validation("overrides", "INVALID_OVERRIDE", str(exc)) from exc
        binding = model_metadata.resolve_binding(
            model_id, scope_key=scope_key, outbound_model=current_outbound,
        )
        revision = self._metadata_revision()
        self._audit(actual, "model_metadata.overrides.patch", model_id, "succeeded")
        return self._metadata_record(
            model_id, binding, revision,
            scope=requested_scope, scope_id=scope_key,
        )

    def delete_metadata_overrides(
        self,
        context: ManagementContext,
        model_id: str,
        *,
        scope: str,
        account_id: str | None,
        channel_id: str | None,
        expected_revision: str | None,
    ) -> None:
        actual = self._write(context, Capability.DESTRUCTIVE)
        scope_key, _, _ = self._override_scope(
            model_id, scope=scope, account_id=account_id, channel_id=channel_id,
            outbound_model=None,
        )
        with config.serialized_updates():
            self._check_revision(
                expected_revision, self._metadata_revision(), required=True,
            )
            if not model_metadata.delete_override_layer(model_id, scope_key=scope_key):
                raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
        self._audit(actual, "model_metadata.overrides.delete", model_id, "succeeded")

    def search_catalog(
        self,
        context: ManagementContext,
        *,
        provider: str | None,
        query: str | None,
        sort: str,
        page: int,
        page_size: int,
    ) -> ListPage[CatalogRecord]:
        self._read(context)
        needle = " ".join((query or "").strip().casefold().split())
        tokens = needle.split()
        values = []
        catalog_revision = stable_revision(model_pricing.catalog_status())
        for item in model_pricing.catalog_models():
            if provider and item["provider_id"] != provider:
                continue
            combined = " ".join(str(value).casefold() for value in item.values())
            if tokens and not all(token in combined for token in tokens):
                continue
            values.append(CatalogRecord(
                key=item["key"],
                model_id=item["id"],
                name=item["name"],
                provider_id=item["provider_id"],
                provider_name=item["provider_name"],
                metadata=model_pricing.catalog_metadata(item["key"]) or {},
                revision=catalog_revision,
            ))
        key = {
            "name": lambda item: (item.name.casefold(), item.provider_name.casefold()),
            "provider": lambda item: (item.provider_name.casefold(), item.name.casefold()),
            "modelId": lambda item: (item.model_id.casefold(), item.provider_name.casefold()),
        }[sort]
        return self._paginate(
            sorted(values, key=key), page, page_size, revision=catalog_revision
        )

    def get_compression(self, context: ManagementContext) -> tuple[str | None, str]:
        self._read(context)
        return model_metadata.get_compression_model(), self._metadata_revision()

    def put_compression(
        self,
        context: ManagementContext,
        model_id: str,
        *,
        expected_revision: str | None = None,
    ) -> tuple[str, str]:
        actual = self._write(context)
        known = {item.client_visible_model for item in model_metadata.inventory_items()}
        if model_id not in known:
            raise self._validation("modelId", "UNKNOWN_MODEL", "model is not in inventory")
        with config.serialized_updates():
            self._check_revision(expected_revision, self._metadata_revision())
            model_metadata.set_compression_model(model_id)
        self._audit(actual, "compression_model.put", model_id, "succeeded")
        return model_id, self._metadata_revision()

    def delete_compression(
        self,
        context: ManagementContext,
        *,
        expected_revision: str | None = None,
    ) -> None:
        actual = self._write(context, Capability.DESTRUCTIVE)
        with config.serialized_updates():
            self._check_revision(expected_revision, self._metadata_revision())
            if not model_metadata.clear_compression_model():
                raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
        self._audit(actual, "compression_model.delete", "compression-model", "succeeded")

    # Telegram compatibility façade. Every call remains a control use case while
    # preserving existing renderer input objects and exact exception behavior.
    def get_ingress_map(self, ingress: str):
        self._read(None)
        return model_mapping.get_ingress_map(ingress)

    def get_default_model(self, ingress: str):
        self._read(None)
        return model_mapping.get_default_model(ingress)

    def list_available_models_for(self, ingress: str):
        self._read(None)
        return model_mapping.list_available_models_for(ingress)

    def set_mapping(self, ingress: str, alias: str, real: str) -> None:
        actual = self._write(None)
        with config.serialized_updates():
            if str(alias or "").strip() in self._configured_model_names():
                raise ManagementError(ManagementErrorCode.RESOURCE_CONFLICT)
            model_mapping.set_mapping(ingress, alias, real)
        self._audit(actual, "model_mapping.put", alias, "succeeded")

    def remove_mapping(self, ingress: str, alias: str) -> bool:
        actual = self._write(None, Capability.DESTRUCTIVE)
        result = model_mapping.remove_mapping(ingress, alias)
        self._audit(actual, "model_mapping.delete", alias, "succeeded" if result else "unchanged")
        return result

    def set_default(self, ingress: str, real: str) -> None:
        actual = self._write(None)
        model_mapping.set_default(ingress, real)
        self._audit(actual, "ingress_default.put", ingress, "succeeded")

    def clear_default(self, ingress: str) -> bool:
        actual = self._write(None, Capability.DESTRUCTIVE)
        result = model_mapping.clear_default(ingress)
        self._audit(actual, "ingress_default.delete", ingress, "succeeded" if result else "unchanged")
        return result

    def list_bindings(self):
        self._read(None)
        return model_metadata.list_bindings()

    def resolve_binding(self, *args, **kwargs):
        self._read(None)
        return model_metadata.resolve_binding(*args, **kwargs)

    def inventory_items(self):
        self._read(None)
        return model_metadata.inventory_items()

    def auto_sync_metadata(self):
        return self.perform_metadata_sync()

    def set_binding(self, *args, **kwargs):
        actual = self._write(None)
        result = model_metadata.set_binding(*args, **kwargs)
        self._audit(actual, "model_metadata.binding.put", str(args[0]), "succeeded")
        return result

    def delete_binding(self, *args, **kwargs):
        actual = self._write(None, Capability.DESTRUCTIVE)
        result = model_metadata.delete_binding(*args, **kwargs)
        self._audit(actual, "model_metadata.binding.delete", str(args[0]), "succeeded" if result else "unchanged")
        return result

    def get_compression_model(self):
        self._read(None)
        return model_metadata.get_compression_model()

    def set_compression_model(self, model: str):
        actual = self._write(None)
        result = model_metadata.set_compression_model(model)
        self._audit(actual, "compression_model.put", model, "succeeded")
        return result

    def clear_compression_model(self):
        actual = self._write(None, Capability.DESTRUCTIVE)
        result = model_metadata.clear_compression_model()
        self._audit(actual, "compression_model.delete", "compression-model", "succeeded" if result else "unchanged")
        return result

    def compact_trigger_tokens(self, model: str) -> int:
        self._read(None)
        return model_metadata.compact_trigger_tokens(model)

    def chunk_target_tokens(self) -> int:
        self._read(None)
        return compact_rescue.chunk_target_tokens()

    def catalog_status(self):
        self._read(None)
        return model_pricing.catalog_status()

    def catalog_models(self):
        self._read(None)
        return model_pricing.catalog_models()

    def catalog_metadata(self, key: str):
        self._read(None)
        return model_pricing.catalog_metadata(key)

    def catalog_model(self, key: str):
        self._read(None)
        return model_pricing.catalog_model(key)

    def canonical_official_model(self, model: str):
        self._read(None)
        return model_pricing.canonical_official_model(model)

    def catalog_providers(self):
        self._read(None)
        return model_pricing.catalog_providers()

    def catalog_provider_models(self, provider: str):
        self._read(None)
        return model_pricing.catalog_provider_models(provider)

    def refresh_remote_catalog_sync(self):
        self._write(None)
        return model_pricing.refresh_remote_catalog_sync()

    def reload_local_catalog(self):
        self._read(None)
        return model_pricing.reload_local_catalog()


mapping_control = MappingControl()
