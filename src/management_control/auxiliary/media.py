"""GPT image and xAI Imagine media settings control."""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from src import config, image_db, oauth_manager
from src.openai import images_simple
from src.management_auth.principal import Capability

from ..context import AuditSink, ManagementContext
from ..errors import ErrorField, ManagementError, ManagementErrorCode
from ..models.common import ModelKind, ModelOwnerRef, ModelSourceType
from .common import (
    ConfigGateway,
    ModuleConfigGateway,
    audit,
    ensure_revision,
    invalid_field,
    require,
    revision_for,
    rfc3339_utc,
    string_list,
)


@dataclass(frozen=True, slots=True)
class ImageSettings:
    enabled: bool
    cache_enabled: bool
    main_model: str
    tool_model: str
    cache_path: str
    cache_retention_days: int
    cache_max_bytes: int
    revision: str


@dataclass(frozen=True, slots=True)
class ImageAccountState:
    account_id: str
    email: str
    oauth_enabled: bool
    image_enabled: bool
    image_cooldown_until: str | None
    missing_account_id: bool
    revision: str


@dataclass(frozen=True, slots=True)
class XaiMediaSettings:
    image_models: tuple[str, ...]
    video_models: tuple[str, ...]
    job_ttl_seconds: int
    request_timeout_seconds: int
    revision: str


@dataclass(frozen=True, slots=True)
class AntigravityMediaSettings:
    image_models: tuple[str, ...]
    account_overrides: tuple[tuple[str, tuple[str, ...]], ...]
    revision: str


@dataclass(frozen=True, slots=True)
class MediaModelMutationResult:
    provider: str
    kind: ModelKind
    owner: ModelOwnerRef
    model_id: str
    models: tuple[str, ...]
    status: str
    revision: str


@dataclass(frozen=True, slots=True)
class CachedImageLog:
    id: int
    action: str
    account_email: str
    paths: tuple[str, ...]


def _require_revision(expected_revision: str | None, current_revision: str) -> None:
    if expected_revision is None:
        raise ManagementError(
            ManagementErrorCode.CONFIRMATION_REQUIRED,
            fields=(ErrorField(
                path="If-Match", code="REQUIRED", message="If-Match is required",
            ),),
        )
    ensure_revision(expected_revision, current_revision)


def _media_model(
    value: Any, field: str = "modelId", *, max_length: int = 128,
) -> str:
    if not isinstance(value, str):
        raise invalid_field(field, "INVALID_MODEL", "model must be a string")
    name = value.strip()
    if not name or len(name) > max_length:
        raise invalid_field(
            field, "INVALID_MODEL",
            f"model must contain 1 to {max_length} characters",
        )
    return name


class MediaGateway(Protocol):
    @property
    def image_defaults(self) -> dict[str, Any]: ...
    @property
    def xai_defaults(self) -> dict[str, Any]: ...
    @property
    def data_dir(self) -> str: ...
    def image_settings(self) -> dict[str, Any]: ...
    def image_accounts(self) -> list[dict[str, Any]]: ...
    def image_log(self, log_id: int) -> dict[str, Any] | None: ...


class ModuleMediaGateway:
    @property
    def image_defaults(self) -> dict[str, Any]:
        return copy.deepcopy(images_simple._DEFAULTS)

    @property
    def xai_defaults(self) -> dict[str, Any]:
        section = config.DEFAULT_CONFIG.get("xaiOAuth") or {}
        return copy.deepcopy(section if isinstance(section, dict) else {})

    @property
    def data_dir(self) -> str:
        return str(config.DATA_DIR)

    def image_settings(self) -> dict[str, Any]:
        return images_simple.settings()

    def image_accounts(self) -> list[dict[str, Any]]:
        return images_simple.list_image_accounts(include_disabled=True)

    def image_log(self, log_id: int) -> dict[str, Any] | None:
        return image_db.get_log(log_id)


class ImageControl:
    def __init__(
        self,
        *,
        config_gateway: ConfigGateway | None = None,
        media_gateway: MediaGateway | None = None,
        audit_sink: AuditSink | None = None,
    ) -> None:
        self._config = config_gateway or ModuleConfigGateway()
        self._media = media_gateway or ModuleMediaGateway()
        self._audit_sink = audit_sink

    def _effective_from_root(self, root: dict[str, Any]) -> dict[str, Any]:
        value = self._media.image_defaults
        raw = root.get("images") or {}
        if isinstance(raw, dict):
            value.update(copy.deepcopy(raw))
        return value

    @staticmethod
    def _dto(value: dict[str, Any]) -> ImageSettings:
        stable = {
            "enabled": bool(value.get("enabled", True)),
            "cacheEnabled": bool(value.get("cacheEnabled", False)),
            "mainModel": str(value.get("mainModel") or ""),
            "toolModel": str(value.get("toolModel") or ""),
            "cachePath": str(value.get("cachePath") or ""),
            "cacheRetentionDays": int(value.get("cacheRetentionDays") or 0),
            "cacheMaxBytes": int(value.get("cacheMaxBytes") or 0),
        }
        return ImageSettings(
            enabled=stable["enabled"],
            cache_enabled=stable["cacheEnabled"],
            main_model=stable["mainModel"],
            tool_model=stable["toolModel"],
            cache_path=stable["cachePath"],
            cache_retention_days=stable["cacheRetentionDays"],
            cache_max_bytes=stable["cacheMaxBytes"],
            revision=revision_for(stable),
        )

    def get_settings(self, context: ManagementContext) -> ImageSettings:
        require(context, Capability.READ)
        return self._dto(self._media.image_settings())

    def settings_raw_direct(self, context: ManagementContext) -> dict[str, Any]:
        require(context, Capability.READ)
        return self._media.image_settings()

    def _validate_path(self, value: str) -> str:
        raw = str(value or "").strip()
        if not raw or "\x00" in raw:
            raise invalid_field("cachePath", "INVALID_PATH", "cachePath must not be empty")
        if not os.path.isabs(raw):
            root = (Path(self._media.data_dir) / raw).resolve()
            try:
                root.relative_to(Path(self._media.data_dir).resolve())
            except ValueError as exc:
                raise invalid_field("cachePath", "PATH_ESCAPE", "relative cachePath escapes data directory") from exc
        return raw

    def update_settings(
        self,
        context: ManagementContext,
        patch: dict[str, Any],
        *,
        expected_revision: str | None = None,
    ) -> ImageSettings:
        require(context, Capability.WRITE)
        value = copy.deepcopy(patch)
        for field in ("mainModel", "toolModel"):
            if field in value:
                normalized = str(value[field] or "").strip()
                if not normalized or len(normalized) > 128:
                    raise invalid_field(field, "INVALID_MODEL", "model must contain 1 to 128 characters")
                value[field] = normalized
        if "cachePath" in value:
            value["cachePath"] = self._validate_path(value["cachePath"])
        for field, high in (("cacheRetentionDays", 36500), ("cacheMaxBytes", 2**63 - 1)):
            if field in value:
                number = value[field]
                if not isinstance(number, int) or isinstance(number, bool) or not 0 <= number <= high:
                    raise invalid_field(field, "OUT_OF_RANGE", f"must be between 0 and {high}")

        def mutate(root: dict[str, Any]) -> None:
            current = self._dto(self._effective_from_root(root))
            ensure_revision(expected_revision, current.revision)
            root.setdefault("images", {}).update(copy.deepcopy(value))

        self._config.update(mutate)
        audit(self._audit_sink, context, action="images.settings.update", target="images")
        return self._dto(self._media.image_settings())

    def mutate_direct(self, context: ManagementContext, mutator) -> ImageSettings:
        require(context, Capability.WRITE)

        def mutate(root: dict[str, Any]) -> None:
            section = root.setdefault("images", {})
            mutator(section)

        self._config.update(mutate)
        audit(self._audit_sink, context, action="images.settings.update", target="images")
        return self._dto(self._media.image_settings())

    @staticmethod
    def _account(row: dict[str, Any]) -> ImageAccountState:
        stable = {
            "accountId": str(row.get("account_key") or ""),
            "email": str(row.get("email") or ""),
            "oauthEnabled": bool(row.get("enabled")),
            "imageEnabled": not bool(row.get("image_disabled")),
            "imageCooldownUntil": (
                rfc3339_utc(row.get("image_cooldown_until"))
                if row.get("image_cooldown_until") not in (None, "", 0, 0.0, "0")
                else None
            ),
            "missingAccountId": bool(row.get("missing_account_id")),
        }
        return ImageAccountState(
            account_id=stable["accountId"],
            email=stable["email"],
            oauth_enabled=stable["oauthEnabled"],
            image_enabled=stable["imageEnabled"],
            image_cooldown_until=stable["imageCooldownUntil"],
            missing_account_id=stable["missingAccountId"],
            revision=revision_for(stable),
        )

    def list_accounts(self, context: ManagementContext) -> tuple[ImageAccountState, ...]:
        require(context, Capability.READ)
        return tuple(self._account(row) for row in self._media.image_accounts())

    def accounts_raw_direct(self, context: ManagementContext) -> list[dict[str, Any]]:
        require(context, Capability.READ)
        return self._media.image_accounts()

    def get_account(self, context: ManagementContext, account_id: str) -> ImageAccountState:
        require(context, Capability.READ)
        for row in self._media.image_accounts():
            if str(row.get("account_key") or "") == account_id:
                return self._account(row)
        raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)

    @staticmethod
    def _account_aliases(account_id: str, email: str) -> set[str]:
        aliases = {account_id.strip().lower(), f"oauth:{account_id.strip()}".lower()}
        normalized_email = email.strip().lower()
        if normalized_email:
            aliases.update({normalized_email, f"openai:{normalized_email}"})
        return aliases

    def update_account(
        self,
        context: ManagementContext,
        account_id: str,
        *,
        enabled: bool,
        expected_revision: str | None = None,
    ) -> ImageAccountState:
        require(context, Capability.WRITE)
        current = self.get_account(context, account_id)
        ensure_revision(expected_revision, current.revision)
        aliases = self._account_aliases(current.account_id, current.email)

        def mutate(root: dict[str, Any]) -> None:
            section = root.setdefault("images", {})
            values = list(section.get("disabledAccounts") or [])
            if enabled:
                values = [item for item in values if str(item).strip().lower() not in aliases]
            elif not any(str(item).strip().lower() in aliases for item in values):
                values.append(current.account_id)
            section["disabledAccounts"] = values

        committed = self._config.update(mutate)
        committed_values = list((committed.get("images") or {}).get("disabledAccounts") or [])
        image_enabled = not any(str(item).strip().lower() in aliases for item in committed_values)
        audit(self._audit_sink, context, action="images.account.update", target=account_id)
        # The OAuth list adapter may be eventually consistent in production. Derive
        # the returned image flag and revision from the committed authoritative set.
        stable = {
            "accountId": current.account_id,
            "email": current.email,
            "oauthEnabled": current.oauth_enabled,
            "imageEnabled": image_enabled,
            "imageCooldownUntil": current.image_cooldown_until,
            "missingAccountId": current.missing_account_id,
        }
        return ImageAccountState(
            account_id=current.account_id,
            email=current.email,
            oauth_enabled=current.oauth_enabled,
            image_enabled=image_enabled,
            image_cooldown_until=current.image_cooldown_until,
            missing_account_id=current.missing_account_id,
            revision=revision_for(stable),
        )

    def toggle_account_direct(self, context: ManagementContext, account_id: str) -> None:
        require(context, Capability.WRITE)

        def mutate(root: dict[str, Any]) -> None:
            section = root.setdefault("images", {})
            values = list(section.get("disabledAccounts") or [])
            positions = {str(item).lower(): index for index, item in enumerate(values)}
            if account_id.lower() in positions:
                values.pop(positions[account_id.lower()])
            else:
                values.append(account_id)
            section["disabledAccounts"] = values

        self._config.update(mutate)
        audit(self._audit_sink, context, action="images.account.toggle", target=account_id)

    def cached_image_log(self, context: ManagementContext, log_id: int) -> CachedImageLog | None:
        require(context, Capability.READ)
        row = self._media.image_log(log_id)
        if not row:
            return None
        try:
            raw_paths = json.loads(row.get("cache_paths") or "[]")
        except Exception:
            raw_paths = []
        paths = tuple(
            item for item in raw_paths
            if isinstance(item, str) and os.path.exists(item)
        )
        return CachedImageLog(
            id=int(row.get("id") or log_id),
            action=str(row.get("action") or ""),
            account_email=str(row.get("account_email") or ""),
            paths=paths,
        )


class XaiMediaControl:
    def __init__(
        self,
        *,
        config_gateway: ConfigGateway | None = None,
        media_gateway: MediaGateway | None = None,
        audit_sink: AuditSink | None = None,
    ) -> None:
        self._config = config_gateway or ModuleConfigGateway()
        self._media = media_gateway or ModuleMediaGateway()
        self._audit_sink = audit_sink

    def _effective(self, root: dict[str, Any]) -> dict[str, Any]:
        defaults = self._media.xai_defaults
        raw = root.get("xaiOAuth") or {}
        if isinstance(raw, dict):
            defaults.update(copy.deepcopy(raw))
        return {
            "imageModels": string_list(defaults.get("imageModels")),
            "videoModels": string_list(defaults.get("videoModels")),
            "jobTtlSeconds": self._positive(defaults.get("videoJobTtlSeconds"), 10800),
            "requestTimeoutSeconds": self._positive(defaults.get("mediaRequestTimeoutSeconds"), 180),
        }

    @staticmethod
    def _positive(value: Any, default: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        return parsed if parsed > 0 else default

    @staticmethod
    def _dto(value: dict[str, Any]) -> XaiMediaSettings:
        stable = {
            "imageModels": string_list(value["imageModels"]),
            "videoModels": string_list(value["videoModels"]),
            "jobTtlSeconds": int(value["jobTtlSeconds"]),
            "requestTimeoutSeconds": int(value["requestTimeoutSeconds"]),
        }
        return XaiMediaSettings(
            image_models=tuple(stable["imageModels"]),
            video_models=tuple(stable["videoModels"]),
            job_ttl_seconds=stable["jobTtlSeconds"],
            request_timeout_seconds=stable["requestTimeoutSeconds"],
            revision=revision_for(stable),
        )

    def get_settings(self, context: ManagementContext) -> XaiMediaSettings:
        require(context, Capability.READ)
        return self._dto(self._effective(self._config.get()))

    def settings_raw_direct(self, context: ManagementContext) -> dict[str, Any]:
        require(context, Capability.READ)
        raw = self._config.get().get("xaiOAuth") or {}
        return raw if isinstance(raw, dict) else {}

    @staticmethod
    def validate_models(models: Any, field: str) -> list[str]:
        values = string_list(models)
        if len(values) > 50:
            raise invalid_field(field, "TOO_MANY_MODELS", "at most 50 models are allowed")
        for index, model in enumerate(values):
            if len(model) > 128:
                raise invalid_field(f"{field}[{index}]", "MODEL_TOO_LONG", "model must be at most 128 characters")
        return values

    def update_settings(
        self,
        context: ManagementContext,
        patch: dict[str, Any],
        *,
        expected_revision: str | None = None,
    ) -> XaiMediaSettings:
        require(context, Capability.WRITE)
        value = copy.deepcopy(patch)
        if "imageModels" in value:
            value["imageModels"] = self.validate_models(value["imageModels"], "imageModels")
        if "videoModels" in value:
            value["videoModels"] = self.validate_models(value["videoModels"], "videoModels")
        for field in ("jobTtlSeconds", "requestTimeoutSeconds"):
            if field in value:
                number = value[field]
                if not isinstance(number, int) or isinstance(number, bool) or not 1 <= number <= 2_147_483_647:
                    raise invalid_field(field, "OUT_OF_RANGE", "must be between 1 and 2147483647")

        def mutate(root: dict[str, Any]) -> None:
            current = self._dto(self._effective(root))
            ensure_revision(expected_revision, current.revision)
            section = root.get("xaiOAuth")
            if not isinstance(section, dict):
                section = {}
                root["xaiOAuth"] = section
            for field, item in value.items():
                raw_field = {
                    "jobTtlSeconds": "videoJobTtlSeconds",
                    "requestTimeoutSeconds": "mediaRequestTimeoutSeconds",
                }.get(field, field)
                section[raw_field] = copy.deepcopy(item)

        self._config.update(mutate)
        audit(self._audit_sink, context, action="xai.media-settings.update", target="xai-media")
        return self._dto(self._effective(self._config.get()))

    @staticmethod
    def _kind(kind: ModelKind | str) -> tuple[ModelKind, str]:
        try:
            normalized = kind if isinstance(kind, ModelKind) else ModelKind(str(kind))
        except ValueError as exc:
            raise invalid_field("kind", "UNSUPPORTED_KIND", "kind must be image or video") from exc
        field = {
            ModelKind.IMAGE: "imageModels",
            ModelKind.VIDEO: "videoModels",
        }.get(normalized)
        if field is None:
            raise invalid_field("kind", "UNSUPPORTED_KIND", "kind must be image or video")
        return normalized, field

    def _mutate_model(
        self,
        context: ManagementContext,
        *,
        kind: ModelKind | str,
        old_model_id: str | None,
        new_model_id: str | None,
        action: str,
        expected_revision: str | None,
    ) -> MediaModelMutationResult:
        capability = Capability.DESTRUCTIVE if action == "remove" else Capability.WRITE
        require(context, capability)
        normalized_kind, field = self._kind(kind)
        old_name = _media_model(old_model_id, "modelId") if old_model_id is not None else None
        new_name = _media_model(
            new_model_id, "newModelId" if action == "rename" else "modelId",
        ) if new_model_id is not None else None
        outcome = [""]

        def mutate(root: dict[str, Any]) -> None:
            current = self._dto(self._effective(root))
            _require_revision(expected_revision, current.revision)
            values = list(current.image_models if normalized_kind is ModelKind.IMAGE else current.video_models)
            if action == "add":
                assert new_name is not None
                if new_name in values:
                    outcome[0] = "unchanged"
                else:
                    if len(values) >= 50:
                        raise invalid_field(field, "TOO_MANY_MODELS", "at most 50 models are allowed")
                    values.append(new_name)
                    outcome[0] = "added"
            elif action == "rename":
                assert old_name is not None and new_name is not None
                if old_name not in values:
                    raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
                if old_name == new_name:
                    outcome[0] = "unchanged"
                elif new_name in values:
                    raise ManagementError(ManagementErrorCode.RESOURCE_CONFLICT)
                else:
                    values[values.index(old_name)] = new_name
                    outcome[0] = "renamed"
            else:
                assert old_name is not None
                if old_name not in values:
                    raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
                values.pop(values.index(old_name))
                outcome[0] = "removed"
            section = root.get("xaiOAuth")
            if not isinstance(section, dict):
                section = {}
                root["xaiOAuth"] = section
            section[field] = values

        self._config.update(mutate)
        current = self.get_settings(context)
        models = current.image_models if normalized_kind is ModelKind.IMAGE else current.video_models
        result_model = new_name if action != "remove" else old_name
        audit(
            self._audit_sink, context, action=f"xai.media-model.{action}",
            target=f"{normalized_kind.value}:{result_model}",
        )
        return MediaModelMutationResult(
            provider="xai", kind=normalized_kind,
            owner=ModelOwnerRef(ModelSourceType.GLOBAL),
            model_id=str(result_model), models=tuple(models), status=outcome[0],
            revision=current.revision,
        )

    def add_model(
        self, context: ManagementContext, *, kind: ModelKind | str,
        model_id: str, expected_revision: str | None,
    ) -> MediaModelMutationResult:
        return self._mutate_model(
            context, kind=kind, old_model_id=None, new_model_id=model_id,
            action="add", expected_revision=expected_revision,
        )

    def rename_model(
        self, context: ManagementContext, *, kind: ModelKind | str,
        old_model_id: str, new_model_id: str, expected_revision: str | None,
    ) -> MediaModelMutationResult:
        return self._mutate_model(
            context, kind=kind, old_model_id=old_model_id, new_model_id=new_model_id,
            action="rename", expected_revision=expected_revision,
        )

    def remove_model(
        self, context: ManagementContext, *, kind: ModelKind | str,
        model_id: str, expected_revision: str | None,
    ) -> MediaModelMutationResult:
        return self._mutate_model(
            context, kind=kind, old_model_id=model_id, new_model_id=None,
            action="remove", expected_revision=expected_revision,
        )

    def set_raw_field(self, context: ManagementContext, key: str, value: Any) -> XaiMediaSettings:
        require(context, Capability.WRITE)

        def mutate(root: dict[str, Any]) -> None:
            section = root.get("xaiOAuth")
            if not isinstance(section, dict):
                section = {}
                root["xaiOAuth"] = section
            section[key] = copy.deepcopy(value)

        self._config.update(mutate)
        audit(self._audit_sink, context, action="xai.media-settings.update", target=key)
        return self._dto(self._effective(self._config.get()))


class AntigravityMediaControl:
    def __init__(
        self,
        *,
        config_gateway: ConfigGateway | None = None,
        audit_sink: AuditSink | None = None,
    ) -> None:
        self._config = config_gateway or ModuleConfigGateway()
        self._audit_sink = audit_sink

    @staticmethod
    def _account_overrides(root: dict[str, Any]) -> tuple[tuple[str, tuple[str, ...]], ...]:
        result: list[tuple[str, tuple[str, ...]]] = []
        for account in root.get("oauthAccounts") or ():
            if not isinstance(account, dict) or "imageModels" not in account:
                continue
            try:
                if oauth_manager.provider_of(account) != "antigravity":
                    continue
                account_id = oauth_manager.get_account_key(account)
            except Exception:
                continue
            result.append((str(account_id), tuple(string_list(account.get("imageModels")))))
        return tuple(sorted(result, key=lambda item: item[0]))

    @classmethod
    def _dto(cls, root: dict[str, Any]) -> AntigravityMediaSettings:
        section = copy.deepcopy(config.DEFAULT_CONFIG.get("antigravityOAuth") or {})
        raw = root.get("antigravityOAuth")
        if isinstance(raw, dict):
            section.update(copy.deepcopy(raw))
        stable = {
            "imageModels": string_list(section.get("imageModels")),
            "accountOverrides": cls._account_overrides(root),
        }
        return AntigravityMediaSettings(
            image_models=tuple(stable["imageModels"]),
            account_overrides=stable["accountOverrides"],
            revision=revision_for(stable),
        )

    def get_settings(self, context: ManagementContext) -> AntigravityMediaSettings:
        require(context, Capability.READ)
        return self._dto(self._config.get())

    @staticmethod
    def _validate_models(models: Any) -> list[str]:
        values = string_list(models)
        if len(values) > 80:
            raise invalid_field(
                "imageModels", "TOO_MANY_MODELS", "at most 80 models are allowed",
            )
        for index, model in enumerate(values):
            if len(model) > 80:
                raise invalid_field(
                    f"imageModels[{index}]", "MODEL_TOO_LONG",
                    "model must be at most 80 characters",
                )
        return values

    def update_settings(
        self,
        context: ManagementContext,
        *,
        image_models: tuple[str, ...] | list[str],
        expected_revision: str | None,
    ) -> AntigravityMediaSettings:
        require(context, Capability.WRITE)
        values = self._validate_models(image_models)

        def mutate(root: dict[str, Any]) -> None:
            current = self._dto(root)
            _require_revision(expected_revision, current.revision)
            section = root.get("antigravityOAuth")
            if not isinstance(section, dict):
                section = {}
                root["antigravityOAuth"] = section
            section["imageModels"] = list(values)

        self._config.update(mutate)
        result = self.get_settings(context)
        audit(
            self._audit_sink, context,
            action="antigravity.media-settings.update", target="antigravity-media",
        )
        return result

    @staticmethod
    def _owner_type(owner: ModelOwnerRef) -> ModelSourceType:
        try:
            return owner.type if isinstance(owner.type, ModelSourceType) else ModelSourceType(str(owner.type))
        except (AttributeError, ValueError) as exc:
            raise invalid_field("owner.type", "UNSUPPORTED_OWNER", "owner must be global or oauth") from exc

    @classmethod
    def _assert_mutable_owner(cls, root: dict[str, Any], owner: ModelOwnerRef) -> None:
        owner_type = cls._owner_type(owner)
        if owner_type is ModelSourceType.GLOBAL:
            if owner.id not in (None, ""):
                raise invalid_field("owner.id", "NOT_ALLOWED", "global owner does not accept id")
            return
        if owner_type is not ModelSourceType.OAUTH or not str(owner.id or "").strip():
            raise invalid_field("owner", "UNSUPPORTED_OWNER", "oauth owner requires id")
        owner_id = str(owner.id)
        found = False
        for account in root.get("oauthAccounts") or ():
            if not isinstance(account, dict):
                continue
            try:
                if (
                    oauth_manager.provider_of(account) == "antigravity"
                    and oauth_manager.get_account_key(account) == owner_id
                    and "imageModels" in account
                ):
                    found = True
                    break
            except Exception:
                continue
        if not found:
            raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
        raise ManagementError(
            ManagementErrorCode.UNSUPPORTED_VALUE,
            fields=(ErrorField(
                path="owner", code="READ_ONLY_SCOPE",
                message="Antigravity account image models are read-only",
            ),),
        )

    def _mutate_model(
        self,
        context: ManagementContext,
        *,
        owner: ModelOwnerRef,
        old_model_id: str | None,
        new_model_id: str | None,
        action: str,
        expected_revision: str | None,
    ) -> MediaModelMutationResult:
        capability = Capability.DESTRUCTIVE if action == "remove" else Capability.WRITE
        require(context, capability)
        old_name = _media_model(
            old_model_id, "modelId", max_length=80,
        ) if old_model_id is not None else None
        new_name = _media_model(
            new_model_id, "newModelId" if action == "rename" else "modelId",
            max_length=80,
        ) if new_model_id is not None else None
        outcome = [""]

        def mutate(root: dict[str, Any]) -> None:
            self._assert_mutable_owner(root, owner)
            current = self._dto(root)
            _require_revision(expected_revision, current.revision)
            values = list(current.image_models)
            if action == "add":
                assert new_name is not None
                if new_name in values:
                    outcome[0] = "unchanged"
                else:
                    if len(values) >= 80:
                        raise invalid_field("imageModels", "TOO_MANY_MODELS", "at most 80 models are allowed")
                    values.append(new_name)
                    outcome[0] = "added"
            elif action == "rename":
                assert old_name is not None and new_name is not None
                if old_name not in values:
                    raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
                if old_name == new_name:
                    outcome[0] = "unchanged"
                elif new_name in values:
                    raise ManagementError(ManagementErrorCode.RESOURCE_CONFLICT)
                else:
                    values[values.index(old_name)] = new_name
                    outcome[0] = "renamed"
            else:
                assert old_name is not None
                if old_name not in values:
                    raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
                values.pop(values.index(old_name))
                outcome[0] = "removed"
            section = root.get("antigravityOAuth")
            if not isinstance(section, dict):
                section = {}
                root["antigravityOAuth"] = section
            section["imageModels"] = values

        self._config.update(mutate)
        current = self.get_settings(context)
        result_model = new_name if action != "remove" else old_name
        audit(
            self._audit_sink, context,
            action=f"antigravity.media-model.{action}", target=f"image:{result_model}",
        )
        return MediaModelMutationResult(
            provider="antigravity", kind=ModelKind.IMAGE,
            owner=ModelOwnerRef(ModelSourceType.GLOBAL),
            model_id=str(result_model), models=current.image_models,
            status=outcome[0], revision=current.revision,
        )

    def add_model(
        self, context: ManagementContext, *, owner: ModelOwnerRef,
        model_id: str, expected_revision: str | None,
    ) -> MediaModelMutationResult:
        return self._mutate_model(
            context, owner=owner, old_model_id=None, new_model_id=model_id,
            action="add", expected_revision=expected_revision,
        )

    def rename_model(
        self, context: ManagementContext, *, owner: ModelOwnerRef,
        old_model_id: str, new_model_id: str, expected_revision: str | None,
    ) -> MediaModelMutationResult:
        return self._mutate_model(
            context, owner=owner, old_model_id=old_model_id, new_model_id=new_model_id,
            action="rename", expected_revision=expected_revision,
        )

    def remove_model(
        self, context: ManagementContext, *, owner: ModelOwnerRef,
        model_id: str, expected_revision: str | None,
    ) -> MediaModelMutationResult:
        return self._mutate_model(
            context, owner=owner, old_model_id=model_id, new_model_id=None,
            action="remove", expected_revision=expected_revision,
        )
