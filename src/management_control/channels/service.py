"""Authoritative transport-neutral channel queries, mutations, and actions."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from src import (
    affinity,
    channel_state,
    config,
    cooldown,
    load_balancing,
    log_db,
    probe,
    provider_usage,
    quota_errors,
    scorer,
)
from src.channel import api_channel, registry
from src.channel.compatibility import normalize_mode, normalize_models
from src.channel.url_utils import detect_suffix_protocol, split_base_url
from src.management_auth.policy import CapabilityDenied, authorize
from src.management_auth.principal import Capability
from src.model_pricing import TICKS_PER_USD
from src.providers.catalog import PROVIDER_CATALOG

from ..context import AuditSink, ManagementContext, audit_record
from ..errors import ErrorField, ManagementError, ManagementErrorCode
from ..operations import ManagementOperation, OperationRegistry, OperationStore
from .discovery import discover_models, run_model_discovery
from .preflight import validate_existing_operation
from .validation import validate_base_url, validated_preset
from .models import (
    _UNSET,
    ActionResult,
    ChannelCatalog,
    ChannelCompatibility,
    ChannelCreateCommand,
    ChannelDetail,
    ChannelHealth,
    ChannelListQuery,
    ChannelModel,
    ChannelMutationResult,
    ChannelPage,
    ChannelProtocol,
    ChannelSort,
    ChannelUpdateCommand,
    ChannelView,
    CompatibilityFeature,
    CompatibilityMode,
    CooldownView,
    DeleteResult,
    DiscoveryCommand,
    DiscoveryResult,
    DraftProbeCommand,
    MonthlyStats,
    ParsedChannelUrl,
    PerformanceView,
    ProbeResult,
    ProgressCallback,
    ProviderBrandView,
    ProviderPresetView,
    SortDirection,
    UsageView,
)


_DISCOVERY_KIND = "channel.models.discover"
_DRAFT_PROBE_KIND = "channel.draft.probe"
_DIAGNOSTIC_PROBE_KIND = "channel.diagnostic.probe"
_USAGE_KIND = "channel.provider_usage.refresh"
_PROTOCOLS = {item.value for item in ChannelProtocol}
_HEALTH_ORDER = {value: index for index, value in enumerate(ChannelHealth)}


from .operation_workers import ChannelOperationWorkers


class ChannelControl(ChannelOperationWorkers):
    """One business implementation shared by Telegram and Management API."""

    def __init__(
        self,
        *,
        operation_registry: OperationRegistry | None = None,
        operation_store: OperationStore | None = None,
        audit_sink: AuditSink | None = None,
    ) -> None:
        self._operation_registry = operation_registry
        self._operation_store = operation_store
        self._audit_sink = audit_sink
        if operation_registry is not None:
            operation_registry.register(_DISCOVERY_KIND, self._start_discovery_owner)
            operation_registry.register(_DRAFT_PROBE_KIND, self._start_draft_probe_owner)
            operation_registry.register(_DIAGNOSTIC_PROBE_KIND, self._start_diagnostic_owner)
            operation_registry.register(_USAGE_KIND, self._start_usage_owner)

    @staticmethod
    def _authorize(context: ManagementContext, capability: Capability) -> None:
        try:
            authorize(context.actor, capability)
        except CapabilityDenied as exc:
            raise ManagementError(ManagementErrorCode.CAPABILITY_DENIED) from exc

    def _audit(self, context: ManagementContext, action: str, target: str, result: str) -> None:
        if self._audit_sink is not None:
            self._audit_sink.record(
                audit_record(context, action=action, target=target, result=result)
            )

    @staticmethod
    def _name_from_id(channel_id: str) -> str:
        if not isinstance(channel_id, str) or not channel_id.startswith("api:"):
            raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
        name = channel_id[4:]
        if not name:
            raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
        return name

    @classmethod
    def _domain_channel(cls, channel_id: str):
        name = cls._name_from_id(channel_id)
        channel = registry.get_channel(f"api:{name}")
        if channel is None or getattr(channel, "type", None) != "api":
            raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
        return channel

    @staticmethod
    def _entry(name: str) -> dict[str, Any] | None:
        return next(
            (row for row in config.get().get("channels", ()) if row.get("name") == name),
            None,
        )

    @staticmethod
    def _revision(entry: dict[str, Any]) -> str:
        serialized = json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return "chrev_" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def _order_revision() -> str:
        rows = config.get().get("channels", ())
        value = [(row.get("name"), row.get("generationId")) for row in rows]
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        return "chorder_" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def _masked_hint(value: str) -> str | None:
        if not value:
            return None
        if len(value) <= 10:
            return value[0] + "***"
        return value[:6] + "***" + value[-4:]

    @staticmethod
    def _compatibility(channel: Any) -> ChannelCompatibility:
        return ChannelCompatibility(
            context_1m=CompatibilityFeature(
                mode=CompatibilityMode(normalize_mode(getattr(channel, "context_1m_mode", None))),
                models=tuple(normalize_models(getattr(channel, "context_1m_models", ()))),
            ),
            fast=CompatibilityFeature(
                mode=CompatibilityMode(normalize_mode(getattr(channel, "fast_mode", None))),
                models=tuple(normalize_models(getattr(channel, "fast_models", ()))),
            ),
        )

    @staticmethod
    def _performance(channel_key: str) -> dict[str, PerformanceView]:
        result: dict[str, PerformanceView] = {}
        for row in scorer.snapshot():
            if row.get("channel_key") != channel_key:
                continue
            model = str(row.get("model") or "")
            result[model] = PerformanceView(
                model=model,
                recent_requests=int(row.get("recent_requests") or 0),
                recent_success_count=int(row.get("recent_success_count") or 0),
                total_requests=int(row.get("total_requests") or 0),
                avg_connect_ms=row.get("avg_connect_ms"),
                avg_first_byte_ms=row.get("avg_first_byte_ms"),
                score=row.get("score"),
            )
        return result

    @staticmethod
    def _cooldowns(channel_key: str) -> dict[str, CooldownView]:
        result: dict[str, CooldownView] = {}
        now = int(datetime.now(timezone.utc).timestamp() * 1000)
        for row in cooldown.active_entries():
            if row.get("channel_key") != channel_key:
                continue
            model = str(row.get("model") or "")
            result[model] = CooldownView(
                model=model,
                error_count=int(row.get("error_count") or 0),
                cooldown_until=row.get("cooldown_until"),
                last_error_message=row.get("last_error_message"),
                quota=bool(quota_errors.active_quota_cooldown(row, now_ms=now)),
            )
        return result

    @staticmethod
    def _usage(channel: Any) -> UsageView:
        supported = provider_usage.spec_for(channel) is not None
        if not supported:
            return UsageView(status="unsupported", supported=False)
        raw = copy.deepcopy(provider_usage.cached(channel))
        return UsageView(
            status=str(raw.get("status") or "not_fetched"),
            supported=True,
            stale=bool(raw.get("stale")),
            partial=bool(raw.get("partial")),
            source=raw.get("source"),
            fetched_at=raw.get("fetched_at"),
            error=raw.get("error"),
            error_at=raw.get("error_at"),
            snapshot=raw.get("snapshot"),
        )

    @classmethod
    def _view(cls, channel: Any) -> ChannelView:
        entry = cls._entry(channel.display_name) or {}
        perfs = cls._performance(channel.key)
        cooldowns = cls._cooldowns(channel.key)
        enabled = bool(channel.enabled and not channel.disabled_reason)
        rates = [
            row.recent_success_count / row.recent_requests * 100
            for row in perfs.values()
            if row.recent_requests > 0
        ]
        recent_rate = min(rates) if rates else None
        if not channel.enabled:
            health = ChannelHealth.DISABLED
        elif any(row.cooldown_until == -1 for row in cooldowns.values()):
            health = ChannelHealth.PERMANENT_COOLDOWN
        elif cooldowns and all(row.quota for row in cooldowns.values()):
            health = ChannelHealth.QUOTA_COOLDOWN
        elif cooldowns:
            health = ChannelHealth.COOLDOWN
        elif recent_rate is None:
            health = ChannelHealth.UNKNOWN
        elif recent_rate >= 80:
            health = ChannelHealth.HEALTHY
        elif recent_rate >= 50:
            health = ChannelHealth.DEGRADED
        else:
            health = ChannelHealth.UNHEALTHY
        secret = str(getattr(channel, "api_key", "") or "")
        server_affinity = sum(
            1 for value in affinity.snapshot().values()
            if value.get("channel_key") == channel.key
        )
        client_affinity = sum(
            1 for value in affinity.client_snapshot().values()
            if value.get("channel_key") == channel.key
        )
        return ChannelView(
            id=channel.key,
            revision=cls._revision(entry),
            display_name=channel.display_name,
            base_url=channel.base_url,
            api_path=getattr(channel, "api_path", None),
            protocol=ChannelProtocol(getattr(channel, "protocol", "anthropic")),
            provider_id=getattr(channel, "provider_id", None),
            provider_preset_id=getattr(channel, "provider_preset_id", None),
            models=tuple(
                ChannelModel(real=str(item.get("real") or ""), alias=str(item.get("alias") or item.get("real") or ""))
                for item in (getattr(channel, "models", ()) or ())
            ),
            enabled=enabled,
            disabled_reason=getattr(channel, "disabled_reason", None),
            max_concurrent=int(getattr(channel, "max_concurrent", 0) or 0),
            cc_mimicry=bool(getattr(channel, "cc_mimicry", False)),
            omit_temperature=bool(getattr(channel, "omit_temperature", False)),
            omit_thinking=bool(getattr(channel, "omit_thinking", False)),
            compatibility=cls._compatibility(channel),
            api_key_configured=bool(secret),
            api_key_masked_hint=cls._masked_hint(secret),
            health=health,
            recent_success_rate=recent_rate,
            cooldown_count=len(cooldowns),
            permanent_cooldown_count=sum(
                row.cooldown_until == -1 for row in cooldowns.values()
            ),
            performance_by_model=perfs,
            cooldown_by_model=cooldowns,
            affinity_count=server_affinity,
            client_affinity_count=client_affinity,
            provider_usage=cls._usage(channel),
        )

    def list_all(self, context: ManagementContext) -> tuple[ChannelView, ...]:
        self._authorize(context, Capability.READ)
        return tuple(
            self._view(channel)
            for channel in registry.all_channels()
            if getattr(channel, "type", None) == "api"
        )

    def list_channels(self, context: ManagementContext, query: ChannelListQuery) -> ChannelPage:
        self._authorize(context, Capability.READ)
        if query.page < 1 or not 1 <= query.page_size <= 200:
            raise ManagementError(
                ManagementErrorCode.VALIDATION_FAILED,
                fields=(ErrorField("page", "out_of_range", "Invalid pagination"),),
            )
        items = list(self.list_all(context))
        if query.search:
            needle = query.search.casefold()
            items = [item for item in items if needle in item.display_name.casefold()]
        if query.enabled is not None:
            items = [item for item in items if item.enabled is query.enabled]
        if query.protocol is not None:
            items = [item for item in items if item.protocol is query.protocol]
        if query.provider_id is not None:
            items = [item for item in items if item.provider_id == query.provider_id]
        if query.health is not None:
            items = [item for item in items if item.health is query.health]
        key = {
            ChannelSort.NAME: lambda item: item.display_name.casefold(),
            ChannelSort.ENABLED: lambda item: item.enabled,
            ChannelSort.HEALTH: lambda item: _HEALTH_ORDER[item.health],
            ChannelSort.PROTOCOL: lambda item: item.protocol.value,
            ChannelSort.PROVIDER: lambda item: item.provider_id or "",
            ChannelSort.MODEL_COUNT: lambda item: len(item.models),
            ChannelSort.ORDER: lambda item: 0,
        }[query.sort]
        if query.sort is not ChannelSort.ORDER:
            items.sort(key=key, reverse=query.direction is SortDirection.DESC)
        elif query.direction is SortDirection.DESC:
            items.reverse()
        total = len(items)
        start = (query.page - 1) * query.page_size
        selected = tuple(items[start : start + query.page_size])
        return ChannelPage(
            items=selected,
            page=query.page,
            page_size=query.page_size,
            total=total,
            has_next=start + query.page_size < total,
            order_revision=self._order_revision(),
        )

    def get_channel(self, context: ManagementContext, channel_id: str) -> ChannelView:
        self._authorize(context, Capability.READ)
        return self._view(self._domain_channel(channel_id))

    @staticmethod
    def _month_start() -> float:
        bjt = timezone(timedelta(hours=8))
        now = datetime.now(bjt)
        return datetime(now.year, now.month, 1, tzinfo=bjt).timestamp()

    @staticmethod
    def _month_stats(raw: dict[str, Any] | None) -> MonthlyStats:
        raw = raw or {}
        return MonthlyStats(
            total=int(raw.get("total") or 0),
            success_count=int(raw.get("success_count") or 0),
            error_count=int(raw.get("error_count") or 0),
            input=int(raw.get("input") or 0),
            output=int(raw.get("output") or 0),
            cache_creation=int(raw.get("cache_creation") or 0),
            cache_read=int(raw.get("cache_read") or 0),
            avg_tps=raw.get("avg_tps"),
            max_tps=raw.get("max_tps"),
            min_tps=raw.get("min_tps"),
            cost=format(Decimal(raw.get("cost_ticks") or 0) / TICKS_PER_USD, "f") if raw.get("costed_success") else None,
        )

    def get_channel_detail(self, context: ManagementContext, channel_id: str) -> ChannelDetail:
        channel = self.get_channel(context, channel_id)
        since = self._month_start()
        month = self._month_stats(log_db.tokens_for_channel(channel.id, since))
        models = tuple(copy.deepcopy(log_db.channel_model_stats(channel.id, since_ts=since)))
        return ChannelDetail(channel=channel, month_stats=month, model_stats=models)

    def channel_model_stats(
        self, context: ManagementContext, channel_id: str, *, since_ts: float
    ) -> list[dict[str, Any]]:
        self._authorize(context, Capability.READ)
        self._domain_channel(channel_id)
        return copy.deepcopy(log_db.channel_model_stats(channel_id, since_ts=since_ts))

    def channel_name_exists(self, context: ManagementContext, name: str) -> bool:
        self._authorize(context, Capability.READ)
        return self._entry(name) is not None

    def get_channel_secret_for_edit(self, context: ManagementContext, channel_id: str) -> str:
        """Preserve the frozen TG edit draft; Management API never calls this method."""
        self._authorize(context, Capability.SECRETS_WRITE)
        return str(getattr(self._domain_channel(channel_id), "api_key", "") or "")

    @staticmethod
    def parse_models_input(raw: str) -> tuple[ChannelModel, ...]:
        return tuple(ChannelModel(**item) for item in api_channel.parse_models_input(raw))

    @staticmethod
    def parse_url(raw: str) -> ParsedChannelUrl:
        base, path = split_base_url((raw or "").strip().rstrip("/"))
        detected = detect_suffix_protocol(path) if path else None
        return ParsedChannelUrl(
            base_url=base,
            api_path=path,
            detected_protocol=ChannelProtocol(detected) if detected else None,
        )

    def get_catalog(self, context: ManagementContext) -> ChannelCatalog:
        self._authorize(context, Capability.READ)
        providers = []
        for brand in PROVIDER_CATALOG:
            presets = []
            for preset in brand.presets:
                presets.append(ProviderPresetView(
                    id=preset.id,
                    display_name=preset.display_name,
                    models_url_configured=bool(preset.models_url),
                    models_auth=preset.models_auth,
                    models_parser=preset.models_parser,
                    protocols=dict(preset.protocols),
                    static_models=tuple(preset.static_models),
                    cc_mimicry=preset.cc_mimicry,
                    usage_supported=(brand.id, preset.id) in provider_usage.SPECS,
                ))
            providers.append(ProviderBrandView(brand.id, brand.display_name, tuple(presets)))
        return ChannelCatalog(
            providers=tuple(providers),
            protocols=tuple(ChannelProtocol),
            compatibility_modes=tuple(CompatibilityMode),
            features=("context1m", "fast", "omitTemperature", "omitThinking", "ccMimicry"),
        )

    @staticmethod
    def _canonical_models(models: tuple[ChannelModel, ...]) -> tuple[ChannelModel, ...]:
        return tuple(
            ChannelModel(real=model.real.strip(), alias=model.alias.strip())
            for model in models
        )

    @staticmethod
    def _validate_models(models: tuple[ChannelModel, ...]) -> None:
        if not models:
            raise ManagementError(
                ManagementErrorCode.VALIDATION_FAILED,
                fields=(ErrorField("models", "empty", "At least one model is required"),),
            )
        aliases: set[str] = set()
        for index, model in enumerate(models):
            if not model.real.strip() or not model.alias.strip():
                raise ManagementError(
                    ManagementErrorCode.VALIDATION_FAILED,
                    fields=(ErrorField(f"models[{index}]", "empty", "Model names must not be empty"),),
                )
            if model.alias in aliases:
                raise ManagementError(
                    ManagementErrorCode.VALIDATION_FAILED,
                    fields=(ErrorField(f"models[{index}].alias", "duplicate", "Model alias must be unique"),),
                )
            aliases.add(model.alias)

    @staticmethod
    def _domain_failure(exc: Exception) -> ManagementError:
        message = str(exc)
        if "already exists" in message:
            return ManagementError(ManagementErrorCode.RESOURCE_CONFLICT, message)
        if "not found" in message:
            return ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND, message)
        return ManagementError(ManagementErrorCode.VALIDATION_FAILED, message)

    @staticmethod
    def _entry_from_create(command: ChannelCreateCommand) -> dict[str, Any]:
        name = command.name.strip()
        if not name or len(name) > 64:
            raise ManagementError(
                ManagementErrorCode.VALIDATION_FAILED,
                fields=(ErrorField("name", "invalid_length", "Channel name must be 1 to 64 characters"),),
            )
        api_key = command.api_key.strip()
        if len(api_key) < 5:
            raise ManagementError(
                ManagementErrorCode.VALIDATION_FAILED,
                fields=(ErrorField("apiKey", "too_short", "API key is too short"),),
            )
        if command.max_concurrent < 0:
            raise ManagementError(ManagementErrorCode.VALIDATION_FAILED)
        models = ChannelControl._canonical_models(command.models)
        ChannelControl._validate_models(models)
        base_url = command.base_url.strip() if command.base_url is not None else None
        api_path = command.api_path.strip() if command.api_path is not None else None
        cc_mimicry = command.cc_mimicry
        preset = validated_preset(
            command.provider_id, command.provider_preset_id, command.protocol,
        )
        if preset is not None:
            parsed = ChannelControl.parse_url(preset.protocols[command.protocol.value])
            base_url, api_path = parsed.base_url, parsed.api_path
            if cc_mimicry is None:
                cc_mimicry = bool(command.protocol is ChannelProtocol.ANTHROPIC and preset.cc_mimicry)
        validate_base_url(base_url)
        return {
            "name": name,
            "baseUrl": base_url,
            "apiPath": api_path,
            "apiKey": api_key,
            "protocol": command.protocol.value,
            "models": [dict(model) for model in models],
            "maxConcurrent": command.max_concurrent,
            "cc_mimicry": bool(
                command.protocol is ChannelProtocol.ANTHROPIC
                if cc_mimicry is None else cc_mimicry
            ),
            "omitTemperature": command.omit_temperature,
            "omitThinking": command.omit_thinking,
            "context1mMode": command.compatibility.context_1m.mode.value,
            "context1mModels": list(command.compatibility.context_1m.models),
            "fastMode": command.compatibility.fast.mode.value,
            "fastModels": list(command.compatibility.fast.models),
            "providerId": command.provider_id,
            "providerPresetId": command.provider_preset_id,
            "enabled": command.enabled,
        }

    def create_channel(
        self, context: ManagementContext, command: ChannelCreateCommand
    ) -> ChannelMutationResult:
        self._authorize(context, Capability.WRITE)
        self._authorize(context, Capability.SECRETS_WRITE)
        entry = self._entry_from_create(command)
        try:
            registry.add_api_channel(entry)
            created = self._domain_channel(f"api:{entry['name']}")
            effect_key = channel_state.effect_key(created)
            for model, result in command.initial_probe_results.items():
                if not result.ok:
                    cooldown.record_error(
                        effect_key, model, f"initial probe failed: {result.reason}"
                    )
        except (KeyError, ValueError) as exc:
            raise self._domain_failure(exc) from exc
        view = self._view(created)
        self._audit(context, "channel.create", view.id, "succeeded")
        return ChannelMutationResult(view, load_balancing.is_initialized())

    @staticmethod
    def _patch_from_update(command: ChannelUpdateCommand) -> dict[str, Any]:
        patch: dict[str, Any] = {}
        values = {
            "name": command.name.strip() if command.name is not None else None,
            "baseUrl": command.base_url.strip() if command.base_url is not None else None,
            "apiKey": command.api_key.strip() if command.api_key is not None else None,
            "protocol": command.protocol.value if command.protocol else None,
            "maxConcurrent": command.max_concurrent,
            "cc_mimicry": command.cc_mimicry,
            "omitTemperature": command.omit_temperature,
            "omitThinking": command.omit_thinking,
            "enabled": command.enabled,
        }
        patch.update({key: value for key, value in values.items() if value is not None})
        if command.models is not None:
            models = ChannelControl._canonical_models(command.models)
            ChannelControl._validate_models(models)
            patch["models"] = [dict(model) for model in models]
        if command.api_path is not _UNSET:
            patch["apiPath"] = (
                command.api_path.strip() if command.api_path is not None else None
            )
        if command.provider_id is not _UNSET:
            patch["providerId"] = command.provider_id
        if command.provider_preset_id is not _UNSET:
            patch["providerPresetId"] = command.provider_preset_id
        if command.compatibility is not None:
            patch.update({
                "context1mMode": command.compatibility.context_1m.mode.value,
                "context1mModels": list(command.compatibility.context_1m.models),
                "fastMode": command.compatibility.fast.mode.value,
                "fastModels": list(command.compatibility.fast.models),
            })
        if not patch:
            raise ManagementError(ManagementErrorCode.VALIDATION_FAILED)
        if command.name is not None and not command.name.strip():
            raise ManagementError(ManagementErrorCode.VALIDATION_FAILED)
        if command.base_url is not None:
            validate_base_url(command.base_url)
        if command.api_key is not None and len(command.api_key.strip()) < 5:
            raise ManagementError(ManagementErrorCode.VALIDATION_FAILED)
        if command.max_concurrent is not None and command.max_concurrent < 0:
            raise ManagementError(ManagementErrorCode.VALIDATION_FAILED)
        return patch

    def update_channel(
        self,
        context: ManagementContext,
        channel_id: str,
        command: ChannelUpdateCommand,
        *,
        expected_revision: str | None = None,
        telegram_compatibility: bool = False,
    ) -> ChannelMutationResult:
        self._authorize(context, Capability.WRITE)
        if command.api_key is not None:
            self._authorize(context, Capability.SECRETS_WRITE)
        name = self._name_from_id(channel_id)
        patch = self._patch_from_update(command)
        with config.serialized_updates():
            current = self._entry(name)
            if current is None:
                raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
            if expected_revision is not None and expected_revision != self._revision(current):
                raise ManagementError(ManagementErrorCode.REVISION_CONFLICT)
            if not telegram_compatibility:
                validated_preset(
                    patch.get("providerId", current.get("providerId")),
                    patch.get("providerPresetId", current.get("providerPresetId")),
                    patch.get("protocol", current.get("protocol") or "anthropic"),
                )
            try:
                registry.update_api_channel(name, patch)
            except (KeyError, ValueError) as exc:
                raise self._domain_failure(exc) from exc
        new_id = f"api:{patch.get('name', name)}"
        view = self._view(self._domain_channel(new_id))
        self._audit(context, "channel.update", channel_id, "succeeded")
        return ChannelMutationResult(view, load_balancing.is_initialized())

    def delete_channel(
        self, context: ManagementContext, channel_id: str, *, expected_revision: str
    ) -> DeleteResult:
        self._authorize(context, Capability.DESTRUCTIVE)
        name = self._name_from_id(channel_id)
        with config.serialized_updates():
            current = self._entry(name)
            if current is None:
                raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
            if expected_revision != self._revision(current):
                raise ManagementError(ManagementErrorCode.REVISION_CONFLICT)
            deleted = registry.delete_api_channel(name)
        result = DeleteResult(deleted=deleted, load_balancing_initialized=load_balancing.is_initialized())
        self._audit(context, "channel.delete", channel_id, "succeeded" if deleted else "not-found")
        return result

    def reorder_channels(
        self,
        context: ManagementContext,
        channel_ids: tuple[str, ...],
        *,
        expected_revision: str,
    ) -> str:
        self._authorize(context, Capability.WRITE)
        names = [self._name_from_id(value) for value in channel_ids]
        with config.serialized_updates():
            if expected_revision != self._order_revision():
                raise ManagementError(ManagementErrorCode.REVISION_CONFLICT)
            current = [row.get("name") for row in config.get().get("channels", ())]
            if len(names) != len(set(names)) or set(names) != set(current):
                raise ManagementError(
                    ManagementErrorCode.VALIDATION_FAILED,
                    fields=(ErrorField("channelIds", "incomplete_set", "Complete API channel set required"),),
                )
            order = {name: index for index, name in enumerate(names)}
            config.update(lambda cfg: cfg.__setitem__(
                "channels", sorted(cfg.get("channels", ()), key=lambda row: order[row.get("name")])
            ))
            registry.rebuild_from_config()
        revision = self._order_revision()
        self._audit(context, "channel.reorder", "channels", "succeeded")
        return revision

    def clear_channel_errors(
        self, context: ManagementContext, channel_id: str, *, telegram_compatibility: bool = False,
    ) -> ActionResult:
        self._authorize(context, Capability.WRITE)
        live = registry.get_channel(channel_id)
        if not telegram_compatibility or live is not None:
            self._domain_channel(channel_id)
        before = sum(1 for row in cooldown.active_entries() if row.get("channel_key") == channel_id)
        cooldown.clear(
            channel_id, model=None,
            resolve_alias=not telegram_compatibility or live is not None,
        )
        self._audit(context, "channel.errors.clear", channel_id, "succeeded")
        return ActionResult(affected=before)

    def clear_model_errors(
        self,
        context: ManagementContext,
        channel_id: str,
        model_id: str,
    ) -> ActionResult:
        """Clear cooldown for one public model in one API channel only."""
        self._authorize(context, Capability.WRITE)
        channel = self._domain_channel(channel_id)
        requested = str(model_id or "").strip()
        outbound = next((
            model.real for model in channel.models
            if (model.alias or model.real) == requested
        ), None)
        if not outbound:
            raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
        before = sum(
            1 for row in cooldown.active_entries()
            if row.get("channel_key") == channel_id and row.get("model") == outbound
        )
        cooldown.clear(channel_id, model=outbound)
        self._audit(context, "channel.model_errors.clear", f"{channel_id}:{requested}", "succeeded")
        return ActionResult(affected=before)

    def clear_all_errors(self, context: ManagementContext) -> ActionResult:
        self._authorize(context, Capability.WRITE)
        before = len(cooldown.active_entries())
        cooldown.clear_all()
        self._audit(context, "channel.errors.clear-all", "channels", "succeeded")
        return ActionResult(affected=before)

    def clear_channel_affinity(
        self, context: ManagementContext, channel_id: str, *, telegram_compatibility: bool = False,
    ) -> ActionResult:
        self._authorize(context, Capability.WRITE)
        if not telegram_compatibility or registry.get_channel(channel_id) is not None:
            self._domain_channel(channel_id)
        before = sum(1 for row in affinity.snapshot().values() if row.get("channel_key") == channel_id)
        if not telegram_compatibility:
            before += sum(1 for row in affinity.client_snapshot().values() if row.get("channel_key") == channel_id)
        affinity.delete_by_channel(channel_id)
        if not telegram_compatibility:
            affinity.client_delete_by_channel(channel_id)
        self._audit(context, "channel.affinity.clear", channel_id, "succeeded")
        return ActionResult(affected=before)

    def clear_all_affinity(
        self, context: ManagementContext, *, telegram_compatibility: bool = False,
    ) -> ActionResult:
        self._authorize(context, Capability.WRITE)
        before = len(affinity.snapshot())
        if not telegram_compatibility:
            before += len(affinity.client_snapshot())
        affinity.delete_all()
        if not telegram_compatibility:
            affinity.client_delete_all()
        self._audit(context, "channel.affinity.clear-all", "channels", "succeeded")
        return ActionResult(affected=before)

    def schedule_provider_usage_hint(
        self, context: ManagementContext, channel_id: str, *, force: bool | None = None,
    ) -> bool:
        """Preserve TG's cache warm-up calls, including unsupported channels."""
        self._authorize(context, Capability.READ)
        channel = self._domain_channel(channel_id)
        return (provider_usage.schedule_refresh(channel) if force is None else
                provider_usage.schedule_refresh(channel, force=force))

    def schedule_provider_usage(
        self, context: ManagementContext, channel_id: str, *, force: bool = False
    ) -> ActionResult:
        self._authorize(context, Capability.WRITE if force else Capability.READ)
        channel = self._domain_channel(channel_id)
        if provider_usage.spec_for(channel) is None:
            raise ManagementError(ManagementErrorCode.UNSUPPORTED_VALUE)
        queued = provider_usage.schedule_refresh(channel, force=force)
        return ActionResult(affected=1 if queued else 0, queued=queued)

    def get_compatibility(
        self, context: ManagementContext, channel_id: str
    ) -> tuple[str, ChannelCompatibility]:
        channel = self.get_channel(context, channel_id)
        return channel.revision, channel.compatibility

    def update_compatibility(
        self,
        context: ManagementContext,
        channel_id: str,
        compatibility: ChannelCompatibility,
        *,
        expected_revision: str | None = None,
    ) -> ChannelMutationResult:
        return self.update_channel(
            context,
            channel_id,
            ChannelUpdateCommand(compatibility=compatibility),
            expected_revision=expected_revision,
        )

    async def discover_model_ids(
        self, context: ManagementContext, command: DiscoveryCommand, *,
        discoverer=discover_models,
    ) -> DiscoveryResult:
        return await run_model_discovery(
            self, context, command, discoverer=discoverer,
        )

    @staticmethod
    def _draft_channel(command: DraftProbeCommand):
        validate_base_url(command.base_url)
        entry = {
            "name": command.name + "__wiz",
            "type": "api",
            "baseUrl": command.base_url,
            "apiPath": command.api_path,
            "apiKey": command.api_key,
            "protocol": command.protocol.value,
            "models": [{"real": command.model, "alias": command.model}],
            "cc_mimicry": bool(
                command.protocol is ChannelProtocol.ANTHROPIC
                if command.cc_mimicry is None else command.cc_mimicry
            ),
            "providerId": command.provider_id,
            "providerPresetId": command.provider_preset_id,
            "enabled": True,
        }
        if command.protocol is ChannelProtocol.ANTHROPIC:
            return api_channel.ApiChannel(entry)
        from src.openai.channel.api_channel import OpenAIApiChannel
        return OpenAIApiChannel(entry)

    @staticmethod
    async def _probe(
        channel: Any,
        model: str,
        *,
        progress_cb: ProgressCallback | None,
        pre_save: bool,
    ) -> ProbeResult:
        ok, elapsed, reason = await probe.probe_with_progress(
            channel,
            model,
            progress_cb=progress_cb,
            timeout_s=None,
            progress_interval=10,
        )
        cleared = False
        permanent = False
        if ok:
            try:
                previous = cooldown.get_state(channel.key, model)
                if previous and (
                    previous.get("cooldown_until") is not None
                    or int(previous.get("error_count", 0)) > 0
                ):
                    permanent = previous.get("cooldown_until") == -1
                    cooldown.clear(channel.key, model)
                    cleared = True
            except Exception:
                pass
        elif pre_save:
            cooldown.record_error(
                channel.key, model, f"initial probe failed: {reason}"
            )
        return ProbeResult(ok, elapsed, reason, cleared, permanent)

    async def probe_draft(
        self,
        context: ManagementContext,
        command: DraftProbeCommand,
        *,
        progress_cb: ProgressCallback | None = None,
        telegram_compatibility: bool = False,
    ) -> ProbeResult:
        self._authorize(context, Capability.WRITE)
        self._authorize(context, Capability.SECRETS_WRITE)
        channel = self._draft_channel(command)
        return await self._probe(
            channel, command.model, progress_cb=progress_cb,
            pre_save=not telegram_compatibility,
        )

    async def probe_existing(
        self,
        context: ManagementContext,
        channel_id: str,
        model: str,
        *,
        progress_cb: ProgressCallback | None = None,
    ) -> ProbeResult:
        self._authorize(context, Capability.WRITE)
        channel = self._domain_channel(channel_id)
        if model not in {item.get("real") for item in getattr(channel, "models", ())}:
            raise ManagementError(ManagementErrorCode.VALIDATION_FAILED)
        return await self._probe(channel, model, progress_cb=progress_cb, pre_save=False)

    def _require_operations(self) -> tuple[OperationRegistry, OperationStore]:
        if self._operation_registry is None or self._operation_store is None:
            raise ManagementError(ManagementErrorCode.SERVICE_NOT_READY, retryable=True)
        return self._operation_registry, self._operation_store

    def start_model_discovery(
        self, context: ManagementContext, command: DiscoveryCommand
    ) -> ManagementOperation:
        operations, _ = self._require_operations()
        if command.channel_id: validate_existing_operation(self, context, command.channel_id)
        if not command.channel_id:
            self._authorize(context, Capability.SECRETS_WRITE)
            if command.base_url is not None: validate_base_url(command.base_url)
        return operations.create(context, kind=_DISCOVERY_KIND, payload=command, cancellable=False)

    def start_draft_probe(
        self, context: ManagementContext, command: DraftProbeCommand
    ) -> ManagementOperation:
        operations, _ = self._require_operations()
        self._authorize(context, Capability.SECRETS_WRITE)
        validate_base_url(command.base_url)
        return operations.create(context, kind=_DRAFT_PROBE_KIND, payload=command, cancellable=False)

    def start_existing_probe(
        self, context: ManagementContext, channel_id: str, model: str
    ) -> ManagementOperation:
        operations, _ = self._require_operations()
        validate_existing_operation(self, context, channel_id, model=model)
        return operations.create(
            context, kind=_DIAGNOSTIC_PROBE_KIND,
            payload={"channel_id": channel_id, "model": model}, cancellable=False,
        )

    def start_provider_usage_refresh(
        self, context: ManagementContext, channel_id: str
    ) -> ManagementOperation:
        operations, _ = self._require_operations()
        validate_existing_operation(self, context, channel_id, require_provider_usage=True)
        return operations.create(
            context, kind=_USAGE_KIND, payload={"channel_id": channel_id}, cancellable=False,
        )
