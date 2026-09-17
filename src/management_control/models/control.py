"""Unified model-center query and state use cases.

This control is transport-neutral and owns no persistent cache. Management API
and Telegram receive the same lifecycle instance from ``ManagementControls``.
Existing specialist controls remain authoritative for their domains.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Mapping

from src import config, model_mapping, model_metadata, model_names, model_state
from src.management_control.auxiliary.media import (
    AntigravityMediaControl,
    ImageControl,
    VideoControl,
    XaiMediaControl,
)
from src.management_control.channels.service import ChannelControl
from src.management_control.context import AuditSink, ManagementContext
from src.management_control.errors import ErrorField, ManagementError, ManagementErrorCode
from src.management_control.mapping.control import MappingControl
from src.management_control.oauth.control import OAuthControl
from src.management_control.operations import ManagementOperation, OperationStore
from src.management_control.routing_account_ids import oauth_channel_key_from_account_id

from .upstream_sync import UpstreamSync
from .common import (
    DomainControl,
    ModelKind,
    ModelOwnerRef,
    ModelSourceType,
    stable_revision,
)


class ModelStatus(str, Enum):
    ENABLED = "enabled"
    DISABLED = "disabled"
    VISIBLE = "visible"
    HIDDEN = "hidden"


@dataclass(frozen=True, slots=True)
class ModelSourceRef:
    type: ModelSourceType
    id: str


@dataclass(frozen=True, slots=True)
class ModelIdentity:
    kind: ModelKind
    model_id: str
    provider: str | None = None
    owner: ModelOwnerRef | None = None


@dataclass(frozen=True, slots=True)
class ModelFilters:
    kinds: tuple[ModelKind, ...] = ()
    text: str | None = None
    source: ModelSourceRef | None = None
    statuses: tuple[ModelStatus, ...] = ()


@dataclass(frozen=True, slots=True)
class ModelSourceView:
    type: ModelSourceType
    id: str
    label: str
    provider: str
    outbound_model: str
    source_enabled: bool
    container_enabled: bool
    effective_routable: bool
    effective_metadata: Mapping[str, Any]
    value_source: Mapping[str, str]
    constrained_by: Mapping[str, tuple[str, ...]]
    unavailable_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ModelView:
    resource_key: str
    identity: ModelIdentity
    model_id: str
    aliases: tuple[str, ...]
    global_enabled: bool | None
    visible: bool | None
    common_metadata: Mapping[str, Any]
    sources: tuple[ModelSourceView, ...]
    editable: bool
    revision: str

    def available_in(self, source: ModelSourceRef | None = None) -> bool:
        """Effective availability in this query, independent of discovery visibility."""
        return self.identity.kind in (ModelKind.CHAT, ModelKind.IMAGE, ModelKind.VIDEO) and self.global_enabled is True and any(
            row.effective_routable
            for row in self.sources
            if source is None or source.type is ModelSourceType.GLOBAL
            or (row.type is source.type and row.id == source.id)
        )


@dataclass(frozen=True, slots=True)
class ModelPage:
    items: tuple[ModelView, ...]
    page: int
    page_size: int
    total: int
    has_next: bool
    revision: str


class ModelSelectionMode(str, Enum):
    IDS = "ids"
    FILTER = "filter"


@dataclass(frozen=True, slots=True)
class ModelSelection:
    mode: ModelSelectionMode
    model_ids: tuple[str, ...] = ()
    filters: ModelFilters | None = None
    excluded_model_ids: tuple[str, ...] = ()


class ModelStateField(str, Enum):
    ENABLED = "enabled"
    VISIBLE = "visible"


@dataclass(frozen=True, slots=True)
class ModelStateTarget:
    field: ModelStateField
    value: bool


@dataclass(frozen=True, slots=True)
class ModelStateItemResult:
    model_id: str
    status: str


@dataclass(frozen=True, slots=True)
class ModelStateResult:
    items: tuple[ModelStateItemResult, ...]
    revision: str


def _resource_key(identity: ModelIdentity) -> str:
    owner = identity.owner
    payload = {
        "kind": identity.kind.value,
        "modelId": identity.model_id,
        "provider": identity.provider,
        "owner": None if owner is None else {"type": owner.type.value, "id": owner.id},
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).digest()[:18]
    return "mdl_" + base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return list(dict.fromkeys(
        item.strip() for item in value if isinstance(item, str) and item.strip()
    ))


class ModelCenterControl(DomainControl):
    def __init__(
        self,
        *,
        mapping: MappingControl | None = None,
        oauth: OAuthControl | None = None,
        channels: ChannelControl | None = None,
        images: ImageControl | None = None,
        videos: VideoControl | None = None,
        xai_media: XaiMediaControl | None = None,
        antigravity_media: AntigravityMediaControl | None = None,
        operations: OperationStore | None = None,
        audit_sink: AuditSink | None = None,
    ) -> None:
        super().__init__(audit_sink=audit_sink)
        self._mapping = mapping or MappingControl(audit_sink=audit_sink, operation_store=operations)
        self._oauth = oauth or OAuthControl(audit_sink=audit_sink)
        self._channels = channels or ChannelControl(audit_sink=audit_sink)
        self._images = images or ImageControl(audit_sink=audit_sink)
        self._videos = videos or VideoControl(audit_sink=audit_sink)
        self._xai_media = xai_media or XaiMediaControl(audit_sink=audit_sink)
        self._antigravity_media = antigravity_media or AntigravityMediaControl(
            audit_sink=audit_sink,
        )
        self._operations = operations
        self._upstream_sync = UpstreamSync(self)

    @property
    def mapping(self) -> MappingControl:
        return self._mapping

    @property
    def oauth(self) -> OAuthControl:
        return self._oauth

    @property
    def channels(self) -> ChannelControl:
        return self._channels

    @property
    def images(self) -> ImageControl:
        return self._images

    @property
    def videos(self) -> VideoControl:
        return self._videos

    @property
    def xai_media(self) -> XaiMediaControl:
        return self._xai_media

    @property
    def antigravity_media(self) -> AntigravityMediaControl:
        return self._antigravity_media

    @property
    def operations(self) -> OperationStore:
        if self._operations is None:
            raise ManagementError(ManagementErrorCode.SERVICE_NOT_READY)
        return self._operations

    @staticmethod
    def _binding_view(
        model_id: str, *, scope_key: str | None, outbound_model: str | None,
    ) -> tuple[dict[str, Any], dict[str, str], dict[str, tuple[str, ...]]]:
        try:
            binding = model_metadata.resolve_binding(
                model_id, scope_key=scope_key, outbound_model=outbound_model,
            )
        except Exception:
            binding = None
        if binding is None:
            return {}, {}, {}
        return (
            dict(binding.metadata),
            dict(getattr(binding, "value_source", {}) or {}),
            {
                key: tuple(value)
                for key, value in (
                    getattr(binding, "constrained_by", {}) or {}
                ).items()
            },
        )

    @staticmethod
    def _common_metadata(model_id: str) -> dict[str, Any]:
        try:
            common, _ = model_metadata.get_override_fields(model_id)
        except Exception:
            return {}
        return common

    def _chat_views(self, cfg: Mapping[str, Any]) -> list[ModelView]:
        by_model: dict[str, list[ModelSourceView]] = {}
        state = model_state.state_snapshot(cfg)
        api_disabled = {
            source: set(values)
            for source, values in state["apiSourceDisabledModels"].items()
        }

        for entry in cfg.get("channels") or ():
            if not isinstance(entry, Mapping):
                continue
            name = str(entry.get("name") or "").strip()
            if not name:
                continue
            source_id = f"api:{name}"
            provider = str(entry.get("providerId") or "").strip()
            if not provider:
                provider = "claude" if str(entry.get("protocol") or "anthropic") == "anthropic" else "openai"
            container_enabled = bool(entry.get("enabled", True)) and not bool(entry.get("disabled_reason"))
            for raw_model in entry.get("models") or ():
                if isinstance(raw_model, Mapping):
                    outbound = str(raw_model.get("real") or "").strip()
                    model_id = str(raw_model.get("alias") or outbound).strip()
                else:
                    outbound = str(raw_model or "").strip()
                    model_id = outbound
                if not model_id or not outbound:
                    continue
                source_enabled = model_id not in api_disabled.get(source_id, set())
                global_enabled = model_state.is_global_enabled(model_id, cfg)
                metadata, value_source, constrained = self._binding_view(
                    model_id, scope_key=source_id, outbound_model=outbound,
                )
                by_model.setdefault(model_id, []).append(ModelSourceView(
                    type=ModelSourceType.API, id=source_id, label=name, provider=provider,
                    outbound_model=outbound, source_enabled=source_enabled,
                    container_enabled=container_enabled,
                    effective_routable=global_enabled and source_enabled and container_enabled,
                    effective_metadata=metadata, value_source=value_source,
                    constrained_by=constrained,
                ))

        for account in self._oauth.backend.list_accounts():
            if not isinstance(account, dict):
                continue
            try:
                account_id = self._oauth.backend.account_id(account)
                provider = self._oauth.backend.provider_of(account)
                selection = self._oauth.backend.account_model_selection(account)
            except Exception:
                continue
            if not account_id:
                continue
            label = str(account.get("label") or account_id)
            container_enabled = bool(account.get("enabled", True)) and not bool(account.get("disabled_reason"))
            disabled = set(selection.get("disabled_models") or ())
            scope_key = oauth_channel_key_from_account_id(account_id)
            for outbound in selection.get("models") or ():
                outbound = str(outbound or "").strip()
                if not outbound:
                    continue
                model_id = model_names.public_id(provider, outbound)
                source_enabled = outbound not in disabled
                global_enabled = model_state.is_global_enabled(model_id, cfg)
                metadata, value_source, constrained = self._binding_view(
                    model_id, scope_key=scope_key, outbound_model=outbound,
                )
                by_model.setdefault(model_id, []).append(ModelSourceView(
                    type=ModelSourceType.OAUTH, id=account_id, label=label, provider=provider,
                    outbound_model=outbound, source_enabled=source_enabled,
                    container_enabled=container_enabled,
                    effective_routable=global_enabled and source_enabled and container_enabled,
                    effective_metadata=metadata, value_source=value_source,
                    constrained_by=constrained,
                ))

        aliases_by_target: dict[str, list[str]] = {}
        for alias, target in model_mapping.get_global_map().items():
            aliases_by_target.setdefault(target, []).append(alias)
        views: list[ModelView] = []
        hidden = set(state["hiddenModels"])
        disabled = set(state["disabledModels"])
        for model_id, sources in by_model.items():
            identity = ModelIdentity(ModelKind.CHAT, model_id)
            views.append(ModelView(
                resource_key=_resource_key(identity), identity=identity, model_id=model_id,
                aliases=tuple(sorted(aliases_by_target.get(model_id, ()), key=str.casefold)),
                global_enabled=model_id not in disabled, visible=model_id not in hidden,
                common_metadata=self._common_metadata(model_id),
                sources=tuple(sorted(sources, key=lambda item: (item.type.value, item.id))),
                editable=True, revision="",
            ))
        return views

    @staticmethod
    def _media_view(
        *, kind: ModelKind, provider: str, owner: ModelOwnerRef, model_id: str,
        editable: bool,
    ) -> ModelView:
        identity = ModelIdentity(kind, model_id, provider=provider, owner=owner)
        return ModelView(
            resource_key=_resource_key(identity), identity=identity, model_id=model_id,
            aliases=(), global_enabled=None, visible=None, common_metadata={}, sources=(),
            editable=editable, revision="",
        )

    def _media_views(self, cfg: Mapping[str, Any]) -> list[ModelView]:
        views: list[ModelView] = []
        from src import image_catalog
        by_model = {}
        for row in image_catalog.sources(dict(cfg)):
            source_type = ModelSourceType.API if row.key.startswith("api:") else ModelSourceType.OAUTH
            source_id = row.key if source_type is ModelSourceType.API else row.key[6:]
            metadata, value_source, constrained = self._binding_view(
                row.model, scope_key=row.key, outbound_model=row.upstream,
            )
            by_model.setdefault(row.model, []).append(ModelSourceView(
                type=source_type, id=source_id, label=row.label, provider=row.provider,
                outbound_model=row.upstream, source_enabled=row.source_enabled,
                container_enabled=row.enabled, effective_routable=row.available,
                unavailable_reason=row.unavailable_reason or (
                    '图片模型或本来源已停用，请在模型中心启用。' if not row.available else None),
                effective_metadata=metadata, value_source=value_source, constrained_by=constrained,
            ))
        # Configured/reserved models stay visible to administrators, but lack
        # source availability unless a real account/channel supplies them.
        from src import media_config
        for values in media_config.model_map('image', dict(cfg)).values():
            for model in values: by_model.setdefault(model, [])
        mapping = model_mapping.get_global_map()
        hidden = set(model_state.state_snapshot(cfg)["hiddenModels"])
        for model, sources in by_model.items():
            identity = ModelIdentity(ModelKind.IMAGE, model)
            views.append(ModelView(
                resource_key=_resource_key(identity), identity=identity, model_id=model,
                aliases=tuple(sorted(alias for alias, real in mapping.items() if real == model)),
                global_enabled=model_state.is_global_enabled(model, cfg), visible=model not in hidden,
                common_metadata=self._common_metadata(model), sources=tuple(sources), editable=True, revision="",
            ))

        videos = {}
        for provider, values in media_config.model_map('video', dict(cfg)).items():
            for model in values: videos.setdefault(model, (provider, []))
        for row in image_catalog.sources(dict(cfg), kind='video'):
            videos.setdefault(row.model, (row.provider, []))[1].append(ModelSourceView(
                type=ModelSourceType.OAUTH, id=row.key[6:], label=row.label, provider=row.provider,
                outbound_model=row.upstream, source_enabled=row.source_enabled, container_enabled=row.enabled,
                effective_routable=row.available, unavailable_reason=row.unavailable_reason,
                effective_metadata={}, value_source={}, constrained_by={}))
        for model, (provider, sources) in videos.items():
            identity = ModelIdentity(ModelKind.VIDEO, model)
            views.append(ModelView(resource_key=_resource_key(identity), identity=identity, model_id=model,
                aliases=tuple(sorted(alias for alias, real in mapping.items() if real == model)),
                global_enabled=model_state.is_global_enabled(model, cfg), visible=model not in hidden,
                common_metadata={}, sources=tuple(sources), editable=False, revision=''))

        return views

    @staticmethod
    def _revision_payload(view: ModelView) -> dict[str, Any]:
        identity = view.identity
        return {
            "resourceKey": view.resource_key,
            "identity": {
                "kind": identity.kind.value, "modelId": identity.model_id,
                "provider": identity.provider,
                "owner": None if identity.owner is None else (
                    identity.owner.type.value, identity.owner.id
                ),
            },
            "aliases": view.aliases, "globalEnabled": view.global_enabled,
            "visible": view.visible, "commonMetadata": dict(view.common_metadata),
            "sources": [
                {
                    "type": source.type.value, "id": source.id,
                    "label": source.label,
                    "outbound": source.outbound_model,
                    "sourceEnabled": source.source_enabled,
                    "containerEnabled": source.container_enabled,
                    "metadata": dict(source.effective_metadata),
                    "valueSource": dict(source.value_source),
                    "constrainedBy": dict(source.constrained_by),
                }
                for source in view.sources
            ],
            "editable": view.editable,
        }

    def _all_views(self) -> tuple[list[ModelView], str]:
        cfg = config.get()
        media = self._media_views(cfg)
        image_ids = {item.model_id for item in media if item.identity.kind is ModelKind.IMAGE}
        from src.image_catalog import is_image_name
        chat = [item for item in self._chat_views(cfg) if item.model_id not in image_ids
                and not (is_image_name(item.model_id) and all(source.provider == "antigravity" for source in item.sources))]
        values = chat + media
        values.sort(key=lambda item: (
            {ModelKind.CHAT: 0, ModelKind.IMAGE: 1, ModelKind.VIDEO: 2}[item.identity.kind],
            item.model_id.casefold(), (item.identity.provider or "").casefold(),
            "" if item.identity.owner is None else item.identity.owner.type.value,
            "" if item.identity.owner is None else (item.identity.owner.id or ""),
        ))
        revision = stable_revision([self._revision_payload(item) for item in values])
        return [replace(item, revision=revision) for item in values], revision

    @staticmethod
    def _source_exists(values: list[ModelView], source: ModelSourceRef) -> bool:
        if source.type is ModelSourceType.GLOBAL:
            return not source.id
        for item in values:
            if any(row.type is source.type and row.id == source.id for row in item.sources):
                return True
            owner = item.identity.owner
            if owner is not None and owner.type is source.type and owner.id == source.id:
                return True
        return False

    @staticmethod
    def _source_matches(item: ModelView, source: ModelSourceRef) -> bool:
        if source.type is ModelSourceType.GLOBAL:
            owner = item.identity.owner
            return item.identity.kind in (ModelKind.CHAT, ModelKind.IMAGE) or (
                owner is not None and owner.type is ModelSourceType.GLOBAL
            )
        if any(row.type is source.type and row.id == source.id for row in item.sources):
            return True
        owner = item.identity.owner
        return owner is not None and owner.type is source.type and owner.id == source.id

    @staticmethod
    def _status_matches(item: ModelView, filters: ModelFilters) -> bool:
        if not filters.statuses:
            return True
        if item.identity.kind not in (ModelKind.CHAT, ModelKind.IMAGE):
            return False
        enabled = item.global_enabled is True
        source = filters.source
        if source is not None and source.type in {ModelSourceType.OAUTH, ModelSourceType.API}:
            match = next(
                (row for row in item.sources if row.type is source.type and row.id == source.id),
                None,
            )
            enabled = bool(match and match.source_enabled)
        checks = {
            ModelStatus.ENABLED: enabled, ModelStatus.DISABLED: not enabled,
            ModelStatus.VISIBLE: item.visible is True, ModelStatus.HIDDEN: item.visible is False,
        }
        return any(checks[value] for value in filters.statuses)

    def _filtered_views(self, values: list[ModelView], filters: ModelFilters) -> list[ModelView]:
        if filters.source is not None:
            source = filters.source
            if source.type is ModelSourceType.GLOBAL and source.id:
                raise self._validation("sourceId", "NOT_ALLOWED", "global source does not accept sourceId")
            if source.type is not ModelSourceType.GLOBAL and not source.id:
                raise self._validation("sourceId", "REQUIRED", "sourceId is required")
            if not self._source_exists(values, source):
                raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
        kinds = set(filters.kinds)
        needle = " ".join((filters.text or "").strip().casefold().split())
        result: list[ModelView] = []
        for item in values:
            if kinds and item.identity.kind not in kinds:
                continue
            if filters.source is not None and not self._source_matches(item, filters.source):
                continue
            if needle:
                text = " ".join((
                    item.model_id, *item.aliases,
                    *(source.outbound_model for source in item.sources),
                    *(source.label for source in item.sources),
                )).casefold()
                if needle not in text:
                    continue
            if not self._status_matches(item, filters):
                continue
            result.append(item)
        return result

    def list_models(
        self,
        context: ManagementContext | None = None,
        *,
        filters: ModelFilters | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> ModelPage:
        self._read(context)
        if page < 1:
            raise self._validation("page", "OUT_OF_RANGE", "page must be at least 1")
        if page_size < 1 or page_size > 200:
            raise self._validation("pageSize", "OUT_OF_RANGE", "pageSize must be between 1 and 200")
        filters = filters or ModelFilters()
        values, revision = self._all_views()
        values = self._filtered_views(values, filters)
        # Stable sort over the canonical name order, BEFORE pagination. Keep
        # unavailable models manageable, but never let them displace live ones.
        # A source-filtered list uses that source, not another account's state.
        values.sort(key=lambda item: (
            {ModelKind.CHAT: 0, ModelKind.IMAGE: 1, ModelKind.VIDEO: 2}[item.identity.kind],
            item.identity.kind in (ModelKind.CHAT, ModelKind.IMAGE) and not item.available_in(filters.source),
        ))
        start = (page - 1) * page_size
        return ModelPage(
            items=tuple(values[start:start + page_size]), page=page, page_size=page_size,
            total=len(values), has_next=start + page_size < len(values), revision=revision,
        )

    def get_model(
        self, context: ManagementContext | None, resource_key: str,
    ) -> ModelView:
        self._read(context)
        key = str(resource_key or "").strip()
        values, _ = self._all_views()
        matches = [item for item in values if item.resource_key == key]
        if len(matches) != 1:
            raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
        return matches[0]

    @staticmethod
    def _chat_by_id(values: list[ModelView]) -> dict[str, ModelView]:
        return {item.model_id: item for item in values if item.identity.kind in (ModelKind.CHAT, ModelKind.IMAGE)}

    def _selection_models(
        self, values: list[ModelView], selection: ModelSelection,
    ) -> list[str]:
        chat = self._chat_by_id(values)
        if selection.mode is ModelSelectionMode.IDS:
            raw = list(selection.model_ids)
            if not raw or len(raw) > 10_000:
                raise self._validation("modelIds", "SIZE", "modelIds must contain 1 to 10000 items")
            model_ids = list(dict.fromkeys(str(value or "").strip() for value in raw))
            if any(not value for value in model_ids):
                raise self._validation("modelIds", "EMPTY", "modelIds must not contain empty values")
            missing = [value for value in model_ids if value not in chat]
            if missing:
                if any(item.model_id in missing for item in values):
                    raise ManagementError(ManagementErrorCode.UNSUPPORTED_VALUE)
                raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
            return model_ids
        if selection.mode is not ModelSelectionMode.FILTER or selection.filters is None:
            raise self._validation("selection", "INVALID", "filter selection requires filters")
        matched = self._filtered_views(values, selection.filters)
        if any(item.identity.kind not in (ModelKind.CHAT, ModelKind.IMAGE) for item in matched):
            raise ManagementError(ManagementErrorCode.UNSUPPORTED_VALUE)
        excluded = {str(value or "").strip() for value in selection.excluded_model_ids}
        return [item.model_id for item in matched if item.model_id not in excluded]

    def set_state(
        self,
        context: ManagementContext | None,
        *,
        scope: ModelSourceRef | None,
        selection: ModelSelection,
        target: ModelStateTarget,
        expected_revision: str | None,
    ) -> ModelStateResult:
        actual = self._write(context)
        if target.field is ModelStateField.VISIBLE and scope is not None:
            raise ManagementError(ManagementErrorCode.UNSUPPORTED_VALUE)
        if scope is not None and scope.type not in {ModelSourceType.OAUTH, ModelSourceType.API}:
            raise self._validation("scope.type", "UNSUPPORTED_SCOPE", "state scope must be oauth or api")
        statuses: list[ModelStateItemResult] = []

        with config.serialized_updates():
            values, current_revision = self._all_views()
            self._check_revision(expected_revision, current_revision, required=True)
            if scope is not None and not self._source_exists(values, scope):
                raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
            selected_models = self._selection_models(values, selection)
            if not selected_models:
                raise self._validation("selection", "EMPTY", "selection resolved to no models")
            chat = self._chat_by_id(values)
            for model_id in selected_models:
                view = chat[model_id]
                if scope is None:
                    before = view.visible if target.field is ModelStateField.VISIBLE else view.global_enabled
                else:
                    source_view = next(
                        (row for row in view.sources if row.type is scope.type and row.id == scope.id),
                        None,
                    )
                    if source_view is None:
                        raise self._validation("modelIds", "CROSS_SCOPE", "model is not in selected source")
                    before = source_view.source_enabled
                statuses.append(ModelStateItemResult(
                    model_id=model_id,
                    status="unchanged" if before is target.value else "updated",
                ))

            def mutate(cfg: dict[str, Any]) -> None:
                if scope is None:
                    if target.field is ModelStateField.VISIBLE:
                        model_state.set_visible_in_config(cfg, selected_models, target.value)
                    else:
                        model_state.set_global_enabled_in_config(cfg, selected_models, target.value)
                    return
                if scope.type is ModelSourceType.API:
                    model_state.set_api_source_enabled_in_config(
                        cfg, scope.id, selected_models, target.value,
                    )
                    return
                account = next((
                    item for item in cfg.get("oauthAccounts") or ()
                    if isinstance(item, dict) and self._oauth.backend.account_id(item) == scope.id
                ), None)
                if account is None:
                    raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
                provider = self._oauth.backend.provider_of(account)
                disabled = self._oauth.backend.account_disabled_models(account)
                upstream_ids = {
                    model_names.upstream_id(provider, model_id) for model_id in selected_models
                }
                if target.value:
                    disabled.difference_update(upstream_ids)
                else:
                    disabled.update(upstream_ids)
                field = "cursor_disabled_models" if provider == "cursor" else "disabledModels"
                account[field] = sorted(disabled)

            with config.observe_reload_failures() as reload_failures:
                config.update(mutate, skip_if_unchanged=True)

        if reload_failures:
            self._audit(actual, "model_center.state.update", "runtime", "saved_reload_unconfirmed")
            message = "配置已保存，运行时重载未确认；请刷新状态，勿重放旧操作"
            raise ManagementError(
                ManagementErrorCode.DEPENDENCY_UNAVAILABLE, message,
                fields=(ErrorField(
                    path="runtime", code="SAVED_RELOAD_UNCONFIRMED", message=message,
                ),),
                retryable=False,
            )
        _, new_revision = self._all_views()
        target_name = "global" if scope is None else f"{scope.type.value}:{scope.id}"
        self._audit(actual, "model_center.state.update", target_name, "succeeded")
        return ModelStateResult(tuple(statuses), new_revision)

    def start_upstream_sync(
        self, context: ManagementContext | None, source: ModelSourceRef | None = None,
    ) -> ManagementOperation:
        return self._upstream_sync.start(self._write(context), source)

    def sync_source_models(
        self, context: ManagementContext | None, source: ModelSourceRef,
    ) -> ManagementOperation:
        return self.start_upstream_sync(context, source)

    def clear_model_errors(
        self,
        context: ManagementContext | None,
        *,
        source: ModelSourceRef,
        model_id: str,
    ) -> None:
        actual = self._write(context)
        model = str(model_id or "").strip()
        if not model:
            raise self._validation("modelId", "EMPTY", "modelId is required")
        if source.type is ModelSourceType.OAUTH:
            self._oauth.clear_model_error(actual, source.id, model)
            return
        if source.type is ModelSourceType.API:
            self._channels.clear_model_errors(actual, source.id, model)
            return
        raise self._validation("source.type", "UNSUPPORTED_SCOPE", "source must be oauth or api")
