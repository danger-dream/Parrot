"""Shared load-balancing, channel-order and affinity use cases."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from src import affinity, config, load_balancing, model_mapping
from src.channel import registry
from src.model_registry import runtime as model_registry
from src.management_auth.principal import Capability
from src.management_control.context import AuditSink, ManagementContext
from src.management_control.errors import ErrorField, ManagementError, ManagementErrorCode
from src.management_control.models.common import DomainControl, stable_revision
from src.oauth_ids import provider_from_channel_key


@dataclass(frozen=True, slots=True)
class LoadBalancingRecord:
    mode: str
    revision: str


@dataclass(frozen=True, slots=True)
class OrderRecord:
    model_id: str | None
    order: tuple[str, ...]
    source: str
    revision: str


@dataclass(frozen=True, slots=True)
class AffinityClearResult:
    family: str | None
    fingerprint_count: int
    client_count: int
    revision: str


class LoadBalancingControl(DomainControl):
    FAMILIES = load_balancing.FAMILIES

    def __init__(
        self, *, audit_sink: AuditSink | None = None, backend=load_balancing,
    ) -> None:
        super().__init__(audit_sink=audit_sink)
        self.backend = backend
        self.channel_registry = registry

    @staticmethod
    def _snapshot() -> dict:
        cfg = config.get()
        return {
            "channelSelection": cfg.get("channelSelection") or "smart",
            "loadBalancing": cfg.get("loadBalancing") or {},
            "channels": [channel.key for channel in registry.all_channels()],
        }

    @classmethod
    def _revision(cls) -> str:
        return stable_revision(cls._snapshot())

    @staticmethod
    def _all_channels():
        return list(registry.all_channels())

    @classmethod
    def _client_models(cls) -> list[str]:
        unified = model_registry.current()
        if unified is not None:
            live = {channel.key for channel in cls._all_channels()}
            return sorted((model.id for model in unified.models.values()
                           if any(offer.source in live for offer in model.offers)), key=str.lower)
        models: set[str] = set()
        mapping = model_mapping.get_ingress_map(model_mapping.GLOBAL_MAPPING_LINE)
        for channel in cls._all_channels():
            try:
                values = channel.list_client_models()
            except Exception:
                values = getattr(channel, "models", []) or []
            for raw in values or []:
                model = str(raw or "").strip()
                if model:
                    models.add(str(mapping.get(model) or model).strip())
        return sorted(models, key=lambda value: value.lower())

    @classmethod
    def _channels_for_model(cls, model_id: str):
        result = []
        for channel in cls._all_channels():
            try:
                if model_registry.serves(channel, model_id) is not None:
                    result.append(channel)
            except Exception:
                continue
        return result

    def get(self, context: ManagementContext) -> LoadBalancingRecord:
        self._read(context)
        mode = str(config.get().get("channelSelection") or "smart").lower()
        return LoadBalancingRecord(mode=mode, revision=self._revision())

    def update_mode(
        self,
        context: ManagementContext,
        mode: str,
        *,
        expected_revision: str | None = None,
    ) -> LoadBalancingRecord:
        actual = self._write(context)
        if mode not in {"smart", "order", "priority"}:
            raise self._validation("mode", "UNSUPPORTED_MODE", "unsupported mode")
        with config.serialized_updates():
            self._check_revision(expected_revision, self._revision())
            load_balancing.set_mode(mode)
        record = LoadBalancingRecord(mode=mode, revision=self._revision())
        self._audit(actual, "load_balancing.mode.update", "load-balancing", "succeeded")
        return record

    def get_channel_order(self, context: ManagementContext) -> OrderRecord:
        self._read(context)
        return OrderRecord(
            model_id=None,
            order=tuple(load_balancing.normalize_channel_order()),
            source="channelDefault",
            revision=self._revision(),
        )

    @staticmethod
    def _exact_order(order: Iterable[str], expected: Iterable[str], path: str) -> list[str]:
        values = [str(value or "").strip() for value in order]
        wanted = [str(value or "").strip() for value in expected]
        duplicate = next((value for value in values if values.count(value) > 1), None)
        if duplicate:
            raise ManagementError(
                ManagementErrorCode.VALIDATION_FAILED,
                fields=(ErrorField(path=path, code="DUPLICATE_CHANNEL", message=duplicate),),
            )
        unknown = sorted(set(values) - set(wanted))
        missing = sorted(set(wanted) - set(values))
        if unknown or missing or len(values) != len(wanted):
            fields = []
            if unknown:
                fields.append(ErrorField(path=path, code="UNKNOWN_CHANNEL", message=", ".join(unknown)))
            if missing:
                fields.append(ErrorField(path=path, code="MISSING_CHANNEL", message=", ".join(missing)))
            raise ManagementError(ManagementErrorCode.VALIDATION_FAILED, fields=fields)
        return values

    def replace_channel_order(
        self,
        context: ManagementContext,
        order: Iterable[str],
        *,
        expected_revision: str | None,
    ) -> OrderRecord:
        actual = self._write(context)
        values = self._exact_order(
            order, (channel.key for channel in self._all_channels()), "order"
        )
        with config.serialized_updates():
            self._check_revision(expected_revision, self._revision(), required=True)
            load_balancing.save_channel_order(values)
        result = OrderRecord(None, tuple(values), "channelDefault", self._revision())
        self._audit(actual, "load_balancing.channel_order.replace", "channel-order", "succeeded")
        return result

    def _require_model(self, model_id: str) -> None:
        if model_id not in set(self._client_models()):
            raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)

    def get_model_order(
        self, context: ManagementContext, model_id: str,
    ) -> OrderRecord:
        self._read(context)
        self._require_model(model_id)
        channels = self._channels_for_model(model_id)
        return OrderRecord(
            model_id=model_id,
            order=tuple(load_balancing.effective_order_for_model(model_id, channels)),
            source=(
                "modelOverride" if load_balancing.has_model_priority(model_id)
                else "channelDefault"
            ),
            revision=self._revision(),
        )

    def replace_model_order(
        self,
        context: ManagementContext,
        model_id: str,
        order: Iterable[str],
        *,
        expected_revision: str | None,
    ) -> OrderRecord:
        actual = self._write(context)
        self._require_model(model_id)
        supported = [channel.key for channel in self._channels_for_model(model_id)]
        values = self._exact_order(order, supported, "order")
        with config.serialized_updates():
            self._check_revision(expected_revision, self._revision(), required=True)
            load_balancing.save_model_order(model_id, values)
        result = OrderRecord(model_id, tuple(values), "modelOverride", self._revision())
        self._audit(actual, "load_balancing.model_order.replace", model_id, "succeeded")
        return result

    def delete_model_order(
        self,
        context: ManagementContext,
        model_id: str,
        *,
        expected_revision: str | None,
    ) -> None:
        actual = self._write(context, Capability.DESTRUCTIVE)
        self._require_model(model_id)
        with config.serialized_updates():
            self._check_revision(expected_revision, self._revision(), required=True)
            if not load_balancing.clear_model_orders([model_id]):
                raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
        self._audit(actual, "load_balancing.model_order.delete", model_id, "succeeded")

    def bulk_replace_model_orders(
        self,
        context: ManagementContext,
        model_ids: Iterable[str],
        order: Iterable[str],
        *,
        expected_revision: str | None,
    ) -> tuple[OrderRecord, ...]:
        actual = self._write(context)
        models = [str(value or "").strip() for value in model_ids]
        if not models or len(models) != len(set(models)):
            raise self._validation(
                "modelIds", "INVALID_MODEL_SET", "modelIds must be non-empty and unique"
            )
        for model_id in models:
            self._require_model(model_id)
        union: list[str] = []
        for model_id in models:
            for channel in self._channels_for_model(model_id):
                if channel.key not in union:
                    union.append(channel.key)
        values = self._exact_order(order, union, "order")
        orders: dict[str, list[str]] = {}
        supported_by_model: dict[str, set[str]] = {}
        for model_id in models:
            supported = {channel.key for channel in self._channels_for_model(model_id)}
            supported_by_model[model_id] = supported
            orders[model_id] = [key for key in values if key in supported]
        with config.serialized_updates():
            self._check_revision(expected_revision, self._revision(), required=True)
            load_balancing.save_model_orders(orders)
        revision = self._revision()
        result = tuple(
            OrderRecord(model_id, tuple(orders[model_id]), "modelOverride", revision)
            for model_id in models
        )
        self._audit(actual, "load_balancing.model_orders.bulk_replace", ",".join(models), "succeeded")
        return result

    def clear_all_affinity(self, context: ManagementContext) -> AffinityClearResult:
        actual = self._write(context, Capability.DESTRUCTIVE)
        fingerprints = affinity.count()
        clients = affinity.client_count()
        affinity.delete_all()
        affinity.client_delete_all()
        result = AffinityClearResult(None, fingerprints, clients, self._revision())
        self._audit(actual, "affinity.clear_all", "affinity", "succeeded")
        return result

    def clear_family_affinity(
        self, context: ManagementContext, family: str,
    ) -> AffinityClearResult:
        actual = self._write(context, Capability.DESTRUCTIVE)
        if family not in self.FAMILIES:
            raise self._validation("family", "UNSUPPORTED_FAMILY", "unsupported family")
        fingerprints = affinity.delete_by_protocol(family)
        clients = affinity.client_delete_by_protocol(family)
        result = AffinityClearResult(family, fingerprints, clients, self._revision())
        self._audit(actual, "affinity.clear_family", family, "succeeded")
        return result

    # Telegram compatibility façade.
    def all_channels(self):
        self._read(None)
        return self._all_channels()

    def get_channel(self, key: str):
        self._read(None)
        return registry.get_channel(key)

    def provider_from_channel_key(self, key: str):
        self._read(None)
        return provider_from_channel_key(key)

    def get_mode(self) -> str:
        self._read(None)
        return str(config.get().get("channelSelection") or "smart").lower()

    def mode_description(self, mode: str) -> str:
        self._read(None)
        return self.backend.mode_description(mode)

    def display_mode(self, mode: str) -> str:
        self._read(None)
        return self.backend.display_mode(mode)

    def family_for_channel(self, channel) -> str:
        self._read(None)
        return self.backend.family_for_channel(channel)

    def client_models(self) -> list[str]:
        self._read(None)
        return self._client_models()

    def channels_for_model(self, model: str):
        self._read(None)
        return self._channels_for_model(model)

    def effective_order_for_model(self, model: str, channels) -> list[str]:
        self._read(None)
        return load_balancing.effective_order_for_model(model, channels)

    def has_model_priority(self, model: str) -> bool:
        self._read(None)
        return load_balancing.has_model_priority(model)

    def normalize_channel_order(self) -> list[str]:
        self._read(None)
        return load_balancing.normalize_channel_order()

    def set_mode(self, mode: str) -> None:
        actual = self._write(None)
        self.backend.set_mode(mode)
        self._audit(actual, "load_balancing.mode.update", "load-balancing", "succeeded")

    def save_channel_order(self, order: list[str]) -> None:
        actual = self._write(None)
        load_balancing.save_channel_order(order)
        self._audit(actual, "load_balancing.channel_order.replace", "channel-order", "succeeded")

    def save_model_orders(self, orders: dict[str, list[str]]) -> None:
        actual = self._write(None)
        load_balancing.save_model_orders(orders)
        self._audit(actual, "load_balancing.model_orders.bulk_replace", ",".join(orders), "succeeded")

    def clear_model_orders(self, models: Iterable[str]) -> int:
        actual = self._write(None, Capability.DESTRUCTIVE)
        values = list(models)
        result = load_balancing.clear_model_orders(values)
        self._audit(actual, "load_balancing.model_order.delete", ",".join(values), "succeeded" if result else "unchanged")
        return result

    def affinity_counts(self) -> tuple[int, int]:
        self._read(None)
        return affinity.count(), affinity.client_count()

    def clear_all_affinity_telegram(self) -> tuple[int, int]:
        actual = self._write(None, Capability.DESTRUCTIVE)
        counts = (affinity.count(), affinity.client_count())
        affinity.delete_all()
        affinity.client_delete_all()
        self._audit(actual, "affinity.clear_all", "affinity", "succeeded")
        return counts

    def clear_family_affinity_telegram(self, family: str) -> tuple[int, int]:
        actual = self._write(None, Capability.DESTRUCTIVE)
        result = (
            affinity.delete_by_protocol(family),
            affinity.client_delete_by_protocol(family),
        )
        self._audit(actual, "affinity.clear_family", family, "succeeded")
        return result


load_balancing_control = LoadBalancingControl()
