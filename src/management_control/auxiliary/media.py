"""GPT image and xAI Imagine media settings control."""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from src import config, image_db, oauth_manager, media_config, image_catalog, channel_state
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
    models: dict[str, list[str]]
    request_timeout_seconds: int
    job_ttl_seconds: int | None
    cache_path: str
    cache_retention_days: int
    cache_max_bytes: int
    revision: str
    # 未指定模型时用它；空表示按可用列表自动选择。接受全局目录及来源专属模型。
    default_model: str = ""


@dataclass(frozen=True, slots=True)
class ImageAccountState:
    account_id: str
    email: str
    oauth_enabled: bool
    image_enabled: bool
    image_cooldown_until: str | None
    missing_account_id: bool
    revision: str
    independent_enabled: bool = False
    independent_allowed: bool = False
    effective_available: bool = False
    unavailable_reason: str | None = None


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
    kind = "image"
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
        return {**media_config.settings(self.kind, root), 'models': media_config.model_map(self.kind, root)}

    @staticmethod
    def _dto(value: dict[str, Any]) -> ImageSettings:
        stable = {key: value.get(key) for key in ('enabled', 'cacheEnabled', 'cachePath', 'cacheRetentionDays', 'cacheMaxBytes', 'models', 'requestTimeoutSeconds', 'jobTtlSeconds', 'defaultModel')}
        return ImageSettings(
            enabled=bool(value.get('enabled', True)), cache_enabled=bool(value.get('cacheEnabled', False)),
            models=copy.deepcopy(value.get('models') or {}),
            request_timeout_seconds=int(value.get('requestTimeoutSeconds', 180)),
            job_ttl_seconds=value.get('jobTtlSeconds'), cache_path=str(value.get('cachePath') or 'images'),
            cache_retention_days=int(value.get('cacheRetentionDays') or 0),
            cache_max_bytes=int(value.get('cacheMaxBytes') or 0), revision=revision_for(stable),
            default_model=str(value.get('defaultModel') or ''))

    def get_settings(self, context: ManagementContext) -> ImageSettings:
        require(context, Capability.READ)
        return self._dto(self._effective_from_root(self._config.get()))

    def settings_raw_direct(self, context: ManagementContext) -> dict[str, Any]:
        require(context, Capability.READ)
        return self._effective_from_root(self._config.get())

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
        allowed = {'enabled', 'cacheEnabled', 'cachePath', 'cacheRetentionDays', 'cacheMaxBytes', 'models', 'requestTimeoutSeconds', 'defaultModel'}
        if self.kind == 'video': allowed.add('jobTtlSeconds')
        unknown = set(value) - allowed
        if unknown: raise invalid_field(sorted(unknown)[0], 'UNKNOWN_FIELD', 'unsupported media setting')
        for field in ('enabled', 'cacheEnabled'):
            if field in value and not isinstance(value[field], bool):
                raise invalid_field(field, 'INVALID_TYPE', 'must be boolean')
        if 'models' in value:
            mapping = value['models']
            if not isinstance(mapping, dict) or any(not isinstance(k, str) or not k.strip() or not isinstance(v, list) for k,v in mapping.items()):
                raise invalid_field('models', 'INVALID_MODELS', 'models must map provider names to arrays')
            for provider, models in mapping.items():
                if len(models) > 50: raise invalid_field('models.' + provider, 'TOO_MANY_MODELS', 'at most 50 models per provider')
                for model in models:
                    if not isinstance(model, str) or not model or len(model) > 128 or any(ch.isspace() for ch in model):
                        raise invalid_field('models.' + provider, 'INVALID_MODEL', 'model must be 1-128 non-whitespace characters')
                mapping[provider] = list(dict.fromkeys(models))
        for field in ('requestTimeoutSeconds', 'jobTtlSeconds'):
            if field in value and (type(value[field]) is not int or not 1 <= value[field] <= 2147483647):
                raise invalid_field(field, 'OUT_OF_RANGE', 'must be between 1 and 2147483647')
        if 'defaultModel' in value:
            # 空串清除偏好。模型成员校验必须在 CAS 内合并本次 models 后执行。
            chosen = value['defaultModel']
            if not isinstance(chosen, str):
                raise invalid_field('defaultModel', 'INVALID_TYPE', 'must be a string')
            chosen = chosen.strip()
            value['defaultModel'] = chosen
        if "cachePath" in value:
            value["cachePath"] = self._validate_path(value["cachePath"])
        for field, high in (("cacheRetentionDays", 36500), ("cacheMaxBytes", 2**63 - 1)):
            if field in value:
                number = value[field]
                if not isinstance(number, int) or isinstance(number, bool) or not 0 <= number <= high:
                    raise invalid_field(field, "OUT_OF_RANGE", f"must be between 0 and {high}")

        def mutate(root: dict[str, Any]) -> None:
            current = self._dto(self._effective_from_root(root))
            _require_revision(expected_revision, current.revision)
            media_config.freeze_cache_inheritance(root, self.kind)
            effective = media_config.settings(self.kind, root)
            media_config.materialize_models(root, self.kind)
            target = root.setdefault(media_config.section(self.kind), {})
            for field in media_config.CACHE_FIELDS:
                target.setdefault(field, copy.deepcopy(effective[field]))
            for field, item in value.items():
                if field == 'models':
                    # Provider map PATCH preserves unmentioned providers/custom lists.
                    root[self.kind + '_models'].update(copy.deepcopy(item))
                else: target[field] = copy.deepcopy(item)
            if value.get('defaultModel') and value['defaultModel'] not in image_catalog.models(root, kind=self.kind):
                raise invalid_field('defaultModel', 'UNKNOWN_MODEL', 'must be one of the configured models')
        committed = self._config.update(mutate)
        audit(self._audit_sink, context, action=self.kind + '.settings.update', target=self.kind)
        return self._dto(self._effective_from_root(committed))

    def mutate_direct(self, context: ManagementContext, mutator) -> ImageSettings:
        current = self.get_settings(context)
        value = self.settings_raw_direct(context)
        before = copy.deepcopy(value)
        mutator(value)
        return self.update_settings(context, {k:v for k,v in value.items() if before.get(k) != v}, expected_revision=current.revision)

    def _sources(self, root: dict) -> list[dict]:
        result = []
        catalog = image_catalog.sources(root, kind=self.kind)
        for account in root.get('oauthAccounts') or []:
            provider = account.get('provider')
            if provider not in (('openai', 'xai') if self.kind == 'image' else ('xai',)): continue
            state = media_config.oauth_state(account, self.kind, root)
            row = dict(source_id='oauth:' + oauth_manager.get_account_key(account), state_key=state['state_key'],
                       label=image_catalog.oauth_source_label(account, root), provider=provider,
                       enabled=state['purpose_enabled'], effective_available=state['effective_available'],
                       can_enable=state['independent_allowed'], unavailable_reason=state['unavailable_reason'])
            if row['effective_available'] and not any(source.key == row['source_id'] and source.available for source in catalog):
                row['effective_available'] = False
                row['unavailable_reason'] = '当前没有启用的媒体模型，请通过配置 / 管理 API 维护。'
            row['revision'] = revision_for(row)
            result.append(row)
        seen = set()
        for source in catalog:
            if not source.key.startswith('api:') or source.key in seen: continue
            seen.add(source.key)
            cfg = media_config.settings(self.kind, root)
            purpose_enabled = source.state_key not in (cfg.get('disabledSources') or [])
            available = any(item.key == source.key and item.available for item in catalog)
            reason = None
            if not available:
                if not purpose_enabled:
                    reason = '图片用途已停用。'
                elif not cfg.get('enabled', True):
                    reason = '图片接口已关闭，请开启接口。'
                elif not source.enabled:
                    reason = '渠道已停用或异常；图片用途开关不会修改渠道的普通对话状态。'
                else:
                    reason = '当前没有启用的图片模型，请通过配置 / 管理 API 维护。'
            row = dict(source_id=source.key, state_key=source.state_key, label=source.label, provider=source.provider,
                       enabled=purpose_enabled, effective_available=available,
                       can_enable=True, unavailable_reason=reason)
            row['revision'] = revision_for(row)
            result.append(row)
        return result

    def list_sources(self, context: ManagementContext) -> list[dict]:
        require(context, Capability.READ)
        return self._sources(self._config.get())

    def update_source(self, context: ManagementContext, source_id: str, *, enabled: bool, expected_revision: str | None) -> dict:
        require(context, Capability.WRITE)
        if not isinstance(enabled, bool): raise invalid_field('enabled', 'INVALID_TYPE', 'must be boolean')
        def mutate(root):
            row = next((item for item in self._sources(root) if item['source_id'] == source_id), None)
            if row is None: raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
            _require_revision(expected_revision, row['revision'])
            if enabled and not row['can_enable']:
                raise ManagementError(ManagementErrorCode.UNSUPPORTED_VALUE, row['unavailable_reason'])
            target = root.setdefault(media_config.section(self.kind), {})
            disabled = list(target.get('disabledSources') or [])
            independent = list(target.get('independentAccounts') or [])
            state_key = row['state_key']
            disabled = [item for item in disabled if item != state_key]
            independent = [item for item in independent if item != state_key]
            if not enabled: disabled.append(state_key)
            if source_id.startswith('oauth:'):
                account = next(item for item in root['oauthAccounts'] if 'oauth:' + oauth_manager.get_account_key(item) == source_id)
                account['generationId'] = channel_state.generation_id(state_key)
                if enabled: independent.append(state_key)
                if self.kind == 'image' and account.get('provider') == 'openai':
                    aliases = self._account_aliases(source_id[6:], account.get('email', ''))
                    target['disabledAccounts'] = [item for item in target.get('disabledAccounts') or [] if str(item).lower() not in aliases]
            else:
                channel = next(item for item in root['channels'] if 'api:' + item['name'] == source_id)
                channel['generationId'] = channel_state.generation_id(state_key)
            target['disabledSources'] = disabled
            target['independentAccounts'] = independent
        committed = self._config.update(mutate)
        audit(self._audit_sink, context, action=self.kind + '.source.update', target=source_id)
        return next(item for item in self._sources(committed) if item['source_id'] == source_id)

    def statistics(self, context: ManagementContext) -> dict:
        require(context, Capability.READ)
        from src import media_cache
        return {'models': image_db.model_statistics(self.kind),
                'cache': media_cache.occupancy(media_config.settings(self.kind, self._config.get()))}

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
        extras = dict(independent_enabled=bool(row.get('independent_enabled')),
                      independent_allowed=bool(row.get('independent_allowed')),
                      effective_available=bool(row.get('effective_available', row.get('enabled') and not row.get('image_disabled'))),
                      unavailable_reason=row.get('unavailable_reason'))
        if row.get('state_key'):
            stable.update(extras, stateKey=row['state_key'])
        return ImageAccountState(
            **extras,
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
        authoritative = self._account_from_root(self._config.get(), account_id)
        if authoritative is not None: return authoritative
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

    @classmethod
    def _account_from_root(cls, root: dict, account_id: str) -> ImageAccountState | None:
        from src.image_catalog import openai_account_state
        for account in root.get('oauthAccounts') or []:
            if oauth_manager.provider_of(account) != 'openai' or oauth_manager.get_account_key(account) != account_id:
                continue
            state = openai_account_state(account, root)
            return cls._account({**state, 'account_key': account_id, 'email': account.get('email', ''),
                'enabled': state['oauth_enabled'], 'effective_available': state['effective_available'],
                'image_disabled': not state['image_enabled'],
                'image_cooldown_until': images_simple._IMAGE_COOLDOWNS.get(account_id, 0)})
        return None

    def _update_independent(self, context, account_id, enabled, expected_revision):
        from src import channel_state
        from src.image_catalog import openai_account_state
        if not isinstance(enabled, bool):
            raise invalid_field('independentEnabled', 'INVALID_VALUE', 'must be a boolean')

        def mutate(root):
            current = self._account_from_root(root, account_id)
            if current is None:
                raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
            _require_revision(expected_revision, current.revision)
            if enabled and not current.independent_allowed:
                raise ManagementError(ManagementErrorCode.UNSUPPORTED_VALUE, current.unavailable_reason)
            account = next(item for item in root['oauthAccounts']
                           if oauth_manager.provider_of(item) == 'openai' and oauth_manager.get_account_key(item) == account_id)
            state = openai_account_state(account, root)
            section = root.setdefault('images', {})
            overrides = list(section.get('independentAccounts') or [])
            if enabled:
                # Persist legacy runtime generation only on this explicit admin
                # mutation, not while listing sources or during proxy requests.
                account['generationId'] = channel_state.generation_id(state['state_key'])
                if state['state_key'] not in overrides:
                    overrides.append(state['state_key'])
            else:
                overrides = [item for item in overrides if item != state['state_key']]
            section['independentAccounts'] = overrides

        committed = self._config.update(mutate)
        audit(self._audit_sink, context, action='images.account.independent.update', target=account_id)
        return self._account_from_root(committed, account_id)

    def update_account(
        self,
        context: ManagementContext,
        account_id: str,
        *,
        enabled: bool | None = None,
        independent_enabled: bool | None = None,
        expected_revision: str | None = None,
    ) -> ImageAccountState:
        require(context, Capability.WRITE)
        if independent_enabled is not None:
            if enabled is not None:
                raise invalid_field('independentEnabled', 'CONFLICT', 'change image participation and independent authorization separately')
            return self._update_independent(context, account_id, independent_enabled, expected_revision)
        if enabled is None:
            raise invalid_field('enabled', 'REQUIRED', 'enabled or independentEnabled is required')
        if not isinstance(enabled, bool):
            raise invalid_field('enabled', 'INVALID_TYPE', 'must be boolean')
        def mutate(root):
            current = self._account_from_root(root, account_id)
            if current is None: raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
            _require_revision(expected_revision, current.revision)
            aliases = self._account_aliases(account_id, current.email)
            section = root.setdefault('images', {})
            values = list(section.get('disabledAccounts') or [])
            if enabled: values = [item for item in values if str(item).strip().lower() not in aliases]
            elif not any(str(item).strip().lower() in aliases for item in values): values.append(account_id)
            section['disabledAccounts'] = values
            if enabled:
                state_key = next(row['state_key'] for row in self._sources(root) if row['source_id'] == 'oauth:' + account_id)
                section['disabledSources'] = [key for key in section.get('disabledSources') or [] if key != state_key]
        committed = self._config.update(mutate)
        audit(self._audit_sink, context, action='images.account.update', target=account_id)
        return self._account_from_root(committed, account_id)

    def toggle_account_direct(self, context: ManagementContext, account_id: str) -> None:
        current = self.get_account(context, account_id)
        self.update_account(context, account_id, enabled=not current.image_enabled, expected_revision=current.revision)

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


class VideoControl(ImageControl):
    kind = "video"


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
            "imageModels": media_config.model_map("image", root).get("xai", []),
            "videoModels": media_config.model_map("video", root).get("xai", []),
            "jobTtlSeconds": self._positive(media_config.settings("video", root).get("jobTtlSeconds"), 10800),
            "requestTimeoutSeconds": self._positive(media_config.settings("video", root).get("requestTimeoutSeconds"), 180),
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
        unknown = set(value) - {'imageModels', 'videoModels', 'requestTimeoutSeconds', 'jobTtlSeconds'}
        if unknown: raise invalid_field(sorted(unknown)[0], 'UNKNOWN_FIELD', 'unsupported media setting')
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
            _require_revision(expected_revision, current.revision)
            for field, item in value.items():
                if field in ('imageModels', 'videoModels'):
                    kind = 'image' if field == 'imageModels' else 'video'
                    media_config.materialize_models(root, kind)
                    root[kind + '_models']['xai'] = copy.deepcopy(item)
                else:
                    root.setdefault('videos', {})[field] = copy.deepcopy(item)

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
            media_config.materialize_models(root, normalized_kind.value)
            root[normalized_kind.value + '_models']['xai'] = values

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
        field = {'videoJobTtlSeconds': 'jobTtlSeconds', 'mediaRequestTimeoutSeconds': 'requestTimeoutSeconds'}.get(key, key)
        current = self.get_settings(context)
        return self.update_settings(context, {field: value}, expected_revision=current.revision)


class AntigravityMediaControl:
    """Compatibility tombstone for old management bindings, never mutates AG state."""
    def __init__(self, **kwargs):
        pass

    def get_settings(self, context):
        require(context, Capability.READ)
        return AntigravityMediaSettings(image_models=(), account_overrides=(), revision=revision_for({"retired": True}))

    def update_settings(self, *args, **kwargs):
        raise ManagementError(ManagementErrorCode.UNSUPPORTED_VALUE, "Antigravity image support has been removed")

    add_model = update_settings
    rename_model = update_settings
    remove_model = update_settings
