"""Mapping-domain sync orchestration, inherited by the public MappingControl."""

from __future__ import annotations

from typing import Any

from src import config, model_metadata, model_pricing
from src.channel import registry
from src.management_control.context import ManagementContext
from src.management_control.errors import ManagementError, ManagementErrorCode
from src.management_control.models.common import stable_revision
from src.management_control.routing_account_ids import oauth_channel_key_from_account_id
from src.oauth_ids import provider_from_channel_key
from .contracts import MetadataSyncMode, MetadataSyncTarget


class MetadataSyncMixin:
    @staticmethod
    def _metadata_snapshot(inventory: list[Any] | None = None) -> dict[str, Any]:
        cfg = config.get()
        items = list(model_metadata.inventory_items()) if inventory is None else inventory
        return {
            "modelBindings": cfg.get("modelBindings") or {},
            "modelMetadataOverrides": cfg.get("modelMetadataOverrides") or {},
            "compressionModel": cfg.get("compressionModel") or "",
            "catalog": model_pricing.catalog_status(),
            "inventory": [
                (
                    item.scope_key,
                    item.scope_type,
                    item.client_visible_model,
                    item.outbound_model,
                )
                for item in items
            ],
        }

    @classmethod
    def _metadata_revision(cls, inventory: list[Any] | None = None) -> str:
        return stable_revision(cls._metadata_snapshot(inventory))

    def perform_metadata_sync(self, context: ManagementContext | None = None) -> dict[str, Any]:
        actual = self._write(context)
        with self._sync_lock:
            if self.__class__._sync_running:
                raise ManagementError(
                    ManagementErrorCode.OPERATION_ALREADY_RUNNING,
                    retryable=True,
                )
            self.__class__._sync_running = True
        try:
            catalog_updated = False
            try:
                catalog_updated = model_pricing.refresh_remote_catalog_sync()
            except Exception as exc:
                print(f"[metadata] models.dev refresh failed; using local catalog: {exc}")
            model_pricing.reload_local_catalog()
            result = dict(model_metadata.auto_sync_metadata())
            result["catalog"] = "updated" if catalog_updated else "local"
            self._audit(actual, "model_metadata.sync", "catalog", "succeeded")
            return result
        finally:
            with self._sync_lock:
                self.__class__._sync_running = False

    @classmethod
    def _sync_source_parts(cls, source: Any) -> tuple[str, str]:
        if source is None:
            raise cls._validation(
                "source", "REQUIRED", "source is required",
            )
        source_type = getattr(source, "type", None)
        source_type = getattr(source_type, "value", source_type)
        source_id = str(getattr(source, "id", "") or "").strip()
        if source_type not in {"oauth", "api"} or not source_id:
            raise cls._validation(
                "source", "INVALID_SOURCE", "source must identify an oauth or api source",
            )
        internal, actual = cls._scope_resource(
            source_id, expected_scope=str(source_type),
        )
        return internal, actual

    def start_metadata_sync(
        self,
        context: ManagementContext,
        *,
        mode: MetadataSyncMode | str | None = None,
        targets: tuple[MetadataSyncTarget, ...] = (),
        source: Any | None = None,
        refresh_catalog: bool = True,
        expected_revision: str | None = None,
        # Legacy selector shape is retained as a compatibility façade.
        scope: str | None = None,
        provider_id: str | None = None,
        account_id: str | None = None,
        channel_id: str | None = None,
    ):
        self._write(context)
        if self._operation_store is None:
            raise ManagementError(ManagementErrorCode.SERVICE_NOT_READY)
        selected_mode = str(getattr(mode, "value", mode) or scope or "full")
        legacy_scope = scope or (selected_mode if selected_mode in {"provider", "account", "channel"} else None)
        fingerprint = stable_revision({
            "mode": selected_mode,
            "targets": [
                {
                    "modelId": str(getattr(target, "model_id", "") or ""),
                    "source": (
                        {
                            "type": str(getattr(getattr(getattr(target, "source", None), "type", ""), "value", getattr(getattr(target, "source", None), "type", ""))),
                            "id": str(getattr(getattr(target, "source", None), "id", "") or ""),
                        }
                        if getattr(target, "source", None) is not None else None
                    ),
                }
                for target in targets
            ],
            "source": (
                {
                    "type": str(getattr(getattr(source, "type", ""), "value", getattr(source, "type", ""))),
                    "id": str(getattr(source, "id", "") or ""),
                }
                if source is not None else None
            ),
            "providerId": provider_id,
            "accountId": account_id,
            "channelId": channel_id,
            "refreshCatalog": bool(refresh_catalog),
        })
        replay = self._idempotent_replay(
            context,
            action="model_metadata.sync",
            fingerprint=fingerprint,
            operation_store=self._operation_store,
        )
        if replay is not None:
            return replay
        inventory = list(model_metadata.inventory_items())
        normalized_targets: list[tuple[str, str | None, str | None]] = []

        if selected_mode == "full":
            if targets or source is not None:
                raise self._validation("targets", "NOT_ALLOWED", "full sync does not accept selectors")
        elif selected_mode in {"one", "selected"}:
            if (selected_mode == "one" and len(targets) != 1) or not targets:
                raise self._validation("targets", "INVALID_COUNT", f"{selected_mode} sync target count is invalid")
            for index, target in enumerate(targets):
                model_id = str(getattr(target, "model_id", "") or "").strip()
                if not model_id:
                    raise self._validation(
                        f"targets[{index}].modelId", "EMPTY", "modelId is required",
                    )
                target_source = getattr(target, "source", None)
                if target_source is None:
                    if not any(item.client_visible_model == model_id for item in inventory):
                        raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
                    normalized_targets.append((model_id, None, None))
                    continue
                internal, _ = self._sync_source_parts(target_source)
                match = next((
                    item for item in inventory
                    if item.scope_key == internal and item.client_visible_model == model_id
                ), None)
                if match is None:
                    raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
                normalized_targets.append((model_id, internal, match.outbound_model))
        elif selected_mode == "source":
            if targets:
                raise self._validation("targets", "NOT_ALLOWED", "source sync does not accept targets")
            internal, actual = self._sync_source_parts(source)
            normalized_targets = [
                (item.client_visible_model, item.scope_key, item.outbound_model)
                for item in inventory
                if item.scope_key == internal and item.scope_type == actual
            ]
        elif legacy_scope in {"provider", "account", "channel"}:
            if legacy_scope == "provider":
                channels = {channel.key: channel for channel in registry.all_channels()}
                selected_items = []
                for item in inventory:
                    channel = channels.get(item.scope_key)
                    provider = str(getattr(channel, "provider", "") or "")
                    provider = provider or provider_from_channel_key(item.scope_key) or ""
                    if provider == provider_id:
                        selected_items.append(item)
            elif legacy_scope == "account":
                internal = oauth_channel_key_from_account_id(str(account_id or ""))
                selected_items = [
                    item for item in inventory
                    if item.scope_key == internal and item.scope_type == "oauth"
                ]
            else:
                selected_items = [
                    item for item in inventory
                    if item.scope_key == channel_id and item.scope_type == "api"
                ]
            normalized_targets = [
                (item.client_visible_model, item.scope_key, item.outbound_model)
                for item in selected_items
            ]
        else:
            raise self._validation("mode", "UNSUPPORTED_MODE", "unsupported metadata sync mode")

        if selected_mode != "full" and not normalized_targets:
            raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
        start_revision = self._metadata_revision(inventory)
        # This endpoint predates revision preconditions for legacy full/provider/
        # account/channel selectors. New one/selected/source modes require one.
        require_revision = selected_mode in {"one", "selected", "source"}
        self._check_revision(
            expected_revision, start_revision, required=require_revision,
        )
        with self._sync_lock:
            if self.__class__._sync_running:
                raise ManagementError(
                    ManagementErrorCode.OPERATION_ALREADY_RUNNING,
                    retryable=True,
                )
            self.__class__._sync_running = True
        try:
            operation = self._operation_store.create(
                context, kind="model_metadata.sync", cancellable=False,
            )
            self._remember_idempotency(
                context,
                action="model_metadata.sync",
                fingerprint=fingerprint,
                operation_id=operation.id,
            )
        except Exception:
            with self._sync_lock:
                self.__class__._sync_running = False
            raise

        def worker() -> None:
            try:
                self._operation_store.mark_running(operation.id)
                if selected_mode == "full":
                    with config.serialized_updates():
                        self._check_revision(
                            expected_revision, self._metadata_revision(),
                            required=require_revision,
                        )
                    catalog_updated = False
                    if refresh_catalog:
                        try:
                            catalog_updated = model_pricing.refresh_remote_catalog_sync()
                        except Exception:
                            catalog_updated = False
                    if not catalog_updated:
                        model_pricing.reload_local_catalog()
                    try:
                        sync_result = model_metadata.auto_sync_metadata(
                            inventory, include_results=True,
                        )
                    except TypeError:
                        # Compatibility with existing plugins/tests that wrapped
                        # the historical one-positional-argument helper.
                        sync_result = model_metadata.auto_sync_metadata(inventory)
                    result = dict(sync_result)
                    result["catalog"] = "updated" if catalog_updated else "local"
                else:
                    candidate = None
                    catalog_source = "active-lkg"
                    if refresh_catalog:
                        try:
                            candidate = model_pricing.fetch_catalog_candidate_sync()
                            catalog_source = "candidate"
                        except Exception:
                            candidate = None
                    if candidate is None and model_pricing.catalog_status().get("revision") == "none":
                        raise ManagementError(
                            ManagementErrorCode.DEPENDENCY_UNAVAILABLE,
                            "metadata catalog is unavailable", retryable=True,
                        )
                    with config.serialized_updates():
                        self._check_revision(
                            expected_revision or start_revision,
                            self._metadata_revision(), required=True,
                        )
                        result = dict(model_metadata.sync_auto_snapshots(
                            normalized_targets, candidate=candidate,
                        ))
                    result["catalog"] = catalog_source
                result["mode"] = selected_mode
                self._operation_store.succeed(operation.id, result)
                self._audit(context, "model_metadata.sync", selected_mode, "succeeded")
            except model_metadata.MetadataSyncConflict:
                self._operation_store.fail(
                    operation.id, code=ManagementErrorCode.REVISION_CONFLICT,
                    message="metadata sync configuration changed before commit",
                    retryable=False,
                )
            except ManagementError as exc:
                self._operation_store.fail(
                    operation.id, code=exc.code, message=exc.message,
                    retryable=exc.retryable,
                )
            except Exception:
                self._operation_store.fail(
                    operation.id,
                    code=ManagementErrorCode.DEPENDENCY_UNAVAILABLE,
                    message="metadata sync failed",
                    retryable=True,
                )
            finally:
                with self._sync_lock:
                    self.__class__._sync_running = False

        try:
            self._operation_store.submit(operation.id, worker)
        except Exception:
            with self._sync_lock:
                self.__class__._sync_running = False
            raise
        return operation
