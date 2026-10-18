"""Translation settings, cache and test control."""

from __future__ import annotations

from src import safe_diagnostics

import asyncio
import copy
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from src import translation
from src.channel import registry
from src.management_auth.principal import Capability

from ..context import AuditSink, ManagementContext
from ..errors import ManagementError, ManagementErrorCode
from ..operations import ManagementOperation, OperationRegistry, OperationStore
from .common import (
    ConfigGateway,
    ModuleConfigGateway,
    audit,
    ensure_revision,
    invalid_field,
    require,
    revision_for,
    string_list,
)


TRANSLATION_LANGUAGES: tuple[tuple[str, str], ...] = (
    ("English", "🇬🇧 English"),
    ("Japanese", "🇯🇵 日本語"),
    ("Korean", "🇰🇷 한국어"),
    ("French", "🇫🇷 Français"),
    ("German", "🇩🇪 Deutsch"),
    ("Italian", "🇮🇹 Italiano"),
    ("Spanish", "🇪🇸 Español"),
    ("Portuguese", "🇵🇹 Português"),
    ("Russian", "🇷🇺 Русский"),
    ("Malay", "🇲🇾 Bahasa Melayu"),
    ("Thai", "🇹🇭 ภาษาไทย"),
    ("Vietnamese", "🇻🇳 Tiếng Việt"),
    ("Arabic", "🇸🇦 العربية"),
    ("Chinese", "🇨🇳 中文"),
)

_NUMERIC_RANGES: dict[str, tuple[int, int]] = {
    "timeoutSeconds": (1, 60),
    "maxHistoryMessages": (1, 200),
    "cacheTtlDays": (1, 30),
    "cachePreloadCount": (0, 1000),
    "failureAlertThreshold": (0, 100),
    "memoryCacheMaxMb": (0, 1024),
    "memoryCacheTtlSeconds": (0, 86400),
}


@dataclass(frozen=True, slots=True)
class TranslationScope:
    models: tuple[str, ...]
    channels: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TranslationSettings:
    enabled: bool
    model: str
    fallback_model: str
    target_language: str
    timeout_seconds: int
    max_history_messages: int
    cache_ttl_days: int
    cache_preload_count: int
    failure_alert_threshold: int
    memory_cache_max_mb: int
    memory_cache_ttl_seconds: int
    translate_system_messages: bool
    scope: TranslationScope
    model_overrides: dict[str, dict[str, Any]]
    prompt: str
    revision: str


@dataclass(frozen=True, slots=True)
class TranslationCacheStats:
    entries: int
    memory_entries: int
    memory_bytes: int
    hits: int
    misses: int
    revision: str


@dataclass(frozen=True, slots=True)
class TranslationLanguage:
    id: str
    display_name: str


@dataclass(frozen=True, slots=True)
class TranslationChannel:
    id: str
    type: str
    display_name: str


class TranslationGateway(Protocol):
    @property
    def default_prompt(self) -> str: ...
    @property
    def defaults(self) -> dict[str, Any]: ...
    def settings(self) -> dict[str, Any]: ...
    def validate_ready(self, settings: dict[str, Any], *, require_enabled: bool) -> tuple[bool, str]: ...
    def cache_count(self) -> int: ...
    def cache_stats(self) -> dict[str, Any]: ...
    def clear_cache(self) -> int: ...
    async def test_text(self, text: str) -> dict[str, Any]: ...
    def available_models(self) -> list[str]: ...
    def available_channels(self) -> list[TranslationChannel]: ...


class ModuleTranslationGateway:
    @property
    def default_prompt(self) -> str:
        return translation.DEFAULT_TRANSLATION_PROMPT

    @property
    def defaults(self) -> dict[str, Any]:
        return copy.deepcopy(translation.DEFAULT_TRANSLATION_CONFIG)

    def settings(self) -> dict[str, Any]:
        return translation._get_cfg()

    def validate_ready(self, settings: dict[str, Any], *, require_enabled: bool) -> tuple[bool, str]:
        return translation.validate_ready(settings, require_enabled=require_enabled)

    def cache_count(self) -> int:
        return translation.cache_count()

    def cache_stats(self) -> dict[str, Any]:
        return translation.cache_hit_stats()

    def clear_cache(self) -> int:
        return translation.clear_cache()

    async def test_text(self, text: str) -> dict[str, Any]:
        return await translation.translate_text_for_test(text)

    def available_models(self) -> list[str]:
        return list(registry.available_models())

    def available_channels(self) -> list[TranslationChannel]:
        rows: list[TranslationChannel] = []
        for channel in registry.all_channels():
            key = str(getattr(channel, "key", "") or "")
            if not key:
                continue
            rows.append(
                TranslationChannel(
                    id=key,
                    type=str(getattr(channel, "type", "") or ""),
                    display_name=str(
                        getattr(channel, "display_name", "")
                        or getattr(channel, "name", "")
                        or key
                    ),
                )
            )
        return rows


Scheduler = Callable[[Callable[[], None], str], None]


class TranslationControl:
    TEST_KIND = "translation.test"

    def __init__(
        self,
        *,
        config_gateway: ConfigGateway | None = None,
        translation_gateway: TranslationGateway | None = None,
        audit_sink: AuditSink | None = None,
        scheduler: Scheduler | None = None,
    ) -> None:
        self._config = config_gateway or ModuleConfigGateway()
        self._translation = translation_gateway or ModuleTranslationGateway()
        self._audit_sink = audit_sink
        self._scheduler = scheduler
        self._operation_store: OperationStore | None = None
        self._operation_registry: OperationRegistry | None = None
        self._bound_registries: set[int] = set()

    @property
    def default_prompt(self) -> str:
        return self._translation.default_prompt

    def bind_operations(self, store: OperationStore, registry_: OperationRegistry) -> None:
        identity = id(registry_)
        if identity not in self._bound_registries:
            registry_.register(self.TEST_KIND, self._start_test)
            self._bound_registries.add(identity)
        self._operation_store = store
        self._operation_registry = registry_

    def _effective_from_root(self, root: dict[str, Any]) -> dict[str, Any]:
        raw = root.get("translation") or {}
        value = self._translation.defaults
        value.update({key: copy.deepcopy(item) for key, item in raw.items() if key in value})
        scope = value.get("scope") if isinstance(value.get("scope"), dict) else {}
        value["scope"] = {
            "models": string_list(scope.get("models")),
            "channels": string_list(scope.get("channels")),
        }
        overrides = value.get("modelOverrides")
        value["modelOverrides"] = copy.deepcopy(overrides) if isinstance(overrides, dict) else {}
        return value

    @staticmethod
    def _dto(value: dict[str, Any]) -> TranslationSettings:
        scope = value.get("scope") or {}
        overrides = value.get("modelOverrides") or {}
        stable = {
            "enabled": bool(value.get("enabled")),
            "model": str(value.get("model") or ""),
            "fallbackModel": str(value.get("fallbackModel") or ""),
            "targetLanguage": str(value.get("targetLanguage") or "English"),
            "timeoutSeconds": int(value.get("timeoutSeconds", 10)),
            "maxHistoryMessages": int(value.get("maxHistoryMessages", 20)),
            "cacheTtlDays": int(value.get("cacheTtlDays", 3)),
            "cachePreloadCount": int(value.get("cachePreloadCount", 100)),
            "failureAlertThreshold": int(value.get("failureAlertThreshold", 10)),
            "memoryCacheMaxMb": int(value.get("memoryCacheMaxMb", 100)),
            "memoryCacheTtlSeconds": int(value.get("memoryCacheTtlSeconds", 7200)),
            "translateSystemMessages": bool(value.get("translateSystemMessages", False)),
            "scope": {
                "models": string_list(scope.get("models")),
                "channels": string_list(scope.get("channels")),
            },
            "modelOverrides": copy.deepcopy(overrides) if isinstance(overrides, dict) else {},
            "prompt": str(value.get("prompt") or ""),
        }
        return TranslationSettings(
            enabled=stable["enabled"],
            model=stable["model"],
            fallback_model=stable["fallbackModel"],
            target_language=stable["targetLanguage"],
            timeout_seconds=stable["timeoutSeconds"],
            max_history_messages=stable["maxHistoryMessages"],
            cache_ttl_days=stable["cacheTtlDays"],
            cache_preload_count=stable["cachePreloadCount"],
            failure_alert_threshold=stable["failureAlertThreshold"],
            memory_cache_max_mb=stable["memoryCacheMaxMb"],
            memory_cache_ttl_seconds=stable["memoryCacheTtlSeconds"],
            translate_system_messages=stable["translateSystemMessages"],
            scope=TranslationScope(
                models=tuple(stable["scope"]["models"]),
                channels=tuple(stable["scope"]["channels"]),
            ),
            model_overrides=stable["modelOverrides"],
            prompt=stable["prompt"],
            revision=revision_for(stable),
        )

    def get_settings(self, context: ManagementContext) -> TranslationSettings:
        require(context, Capability.READ)
        return self._dto(self._translation.settings())

    def readiness(self, context: ManagementContext, *, enabled: bool = False) -> tuple[bool, str]:
        require(context, Capability.READ)
        value = self._translation.settings()
        if enabled:
            value = dict(value)
            value["enabled"] = True
        return self._translation.validate_ready(value, require_enabled=enabled)

    @staticmethod
    def _validated_patch(patch: dict[str, Any]) -> dict[str, Any]:
        value = copy.deepcopy(patch)
        for field, limits in _NUMERIC_RANGES.items():
            if field in value:
                number = value[field]
                if not isinstance(number, int) or isinstance(number, bool) or not limits[0] <= number <= limits[1]:
                    raise invalid_field(field, "OUT_OF_RANGE", f"must be between {limits[0]} and {limits[1]}")
        for field in ("model", "fallbackModel", "targetLanguage"):
            if field in value and not isinstance(value[field], str):
                raise invalid_field(field, "INVALID_TYPE", "must be a string")
        if "model" in value and not value["model"].strip() and value.get("enabled"):
            raise invalid_field("model", "REQUIRED", "model is required when enabling translation")
        if "prompt" in value:
            prompt = value["prompt"]
            if prompt is None:
                value["prompt"] = ""
            elif not isinstance(prompt, str) or len(prompt) > 20000:
                raise invalid_field("prompt", "INVALID_PROMPT", "prompt must be a string of at most 20000 characters")
        if "scope" in value:
            if not isinstance(value["scope"], dict):
                raise invalid_field("scope", "INVALID_TYPE", "must be an object")
            value["scope"] = {
                "models": string_list(value["scope"].get("models")),
                "channels": string_list(value["scope"].get("channels")),
            }
        if "modelOverrides" in value:
            overrides = value["modelOverrides"]
            if not isinstance(overrides, dict):
                raise invalid_field("modelOverrides", "INVALID_TYPE", "must be an object")
            cleaned: dict[str, dict[str, Any]] = {}
            for model, override in overrides.items():
                if not isinstance(model, str) or not model.strip() or not isinstance(override, dict):
                    raise invalid_field("modelOverrides", "INVALID_OVERRIDE", "model override must be an object")
                body = override.get("body", {})
                if not isinstance(body, dict):
                    raise invalid_field(f"modelOverrides.{model}.body", "INVALID_TYPE", "body must be an object")
                for key in body:
                    if not isinstance(key, str) or not key or key.startswith("_parrot_"):
                        raise invalid_field(
                            f"modelOverrides.{model}.body.{key}",
                            "RESERVED_FIELD",
                            "body keys must be non-empty and must not start with _parrot_",
                        )
                cleaned[model.strip()] = {"body": copy.deepcopy(body)} if body else {}
            value["modelOverrides"] = cleaned
        return value

    def update_settings(
        self,
        context: ManagementContext,
        patch: dict[str, Any],
        *,
        expected_revision: str | None = None,
    ) -> TranslationSettings:
        require(context, Capability.WRITE)
        patch = self._validated_patch(patch)

        def mutate(root: dict[str, Any]) -> None:
            current = self._dto(self._effective_from_root(root))
            ensure_revision(expected_revision, current.revision)
            candidate = self._effective_from_root(root)
            candidate.update(copy.deepcopy(patch))
            if bool(candidate.get("enabled")):
                ok, reason = self._translation.validate_ready(candidate, require_enabled=True)
                if not ok:
                    raise invalid_field("enabled", "NOT_READY", reason)
            raw = root.setdefault("translation", {})
            raw.update(copy.deepcopy(patch))

        self._config.update(mutate)
        audit(self._audit_sink, context, action="translation.settings.update", target="translation")
        return self.get_settings(context)

    def set_field_direct(self, context: ManagementContext, field: str, value: Any) -> TranslationSettings:
        """TG adapter mutation after its frozen field-specific validation."""
        require(context, Capability.WRITE)

        def mutate(root: dict[str, Any]) -> None:
            root.setdefault("translation", {})[field] = copy.deepcopy(value)

        self._config.update(mutate)
        audit(self._audit_sink, context, action="translation.settings.update", target=field)
        return self._dto(self._translation.settings())

    def toggle_scope(self, context: ManagementContext, kind: str, value: str) -> TranslationSettings:
        require(context, Capability.WRITE)
        if kind not in {"models", "channels"} or not str(value or "").strip():
            return self.get_settings(context)

        def mutate(root: dict[str, Any]) -> None:
            section = root.setdefault("translation", {})
            scope = section.setdefault("scope", {})
            if not isinstance(scope, dict):
                scope = {}
                section["scope"] = scope
            values = string_list(scope.get(kind))
            if value in values:
                values.remove(value)
            else:
                values.append(value)
            scope[kind] = values

        self._config.update(mutate)
        audit(self._audit_sink, context, action="translation.scope.update", target=kind)
        return self.get_settings(context)

    def clear_scope(self, context: ManagementContext, kind: str) -> TranslationSettings:
        require(context, Capability.WRITE)
        if kind not in {"models", "channels"}:
            return self.get_settings(context)

        def mutate(root: dict[str, Any]) -> None:
            section = root.setdefault("translation", {})
            scope = section.setdefault("scope", {})
            if not isinstance(scope, dict):
                scope = {}
                section["scope"] = scope
            scope[kind] = []

        self._config.update(mutate)
        audit(self._audit_sink, context, action="translation.scope.clear", target=kind)
        return self.get_settings(context)

    def update_current_model_body(
        self,
        context: ManagementContext,
        mutator: Callable[[dict[str, Any]], None],
    ) -> tuple[bool, str]:
        require(context, Capability.WRITE)
        model = str(self._translation.settings().get("model") or "").strip()
        if not model:
            return False, "请先设置翻译模型"

        def mutate(root: dict[str, Any]) -> None:
            section = root.setdefault("translation", {})
            overrides = section.setdefault("modelOverrides", {})
            if not isinstance(overrides, dict):
                overrides = {}
                section["modelOverrides"] = overrides
            item = overrides.get(model)
            if not isinstance(item, dict):
                item = {}
            body = item.get("body")
            body = dict(body) if isinstance(body, dict) else {}
            mutator(body)
            body = {
                key: value for key, value in body.items()
                if isinstance(key, str) and key and not key.startswith("_parrot_")
            }
            if body:
                item["body"] = body
                overrides[model] = item
            else:
                item.pop("body", None)
                if item:
                    overrides[model] = item
                else:
                    overrides.pop(model, None)

        self._config.update(mutate)
        audit(self._audit_sink, context, action="translation.model-override.update", target=model)
        return True, model

    def clear_current_model_override(self, context: ManagementContext) -> tuple[bool, str]:
        require(context, Capability.WRITE)
        model = str(self._translation.settings().get("model") or "").strip()
        if not model:
            return False, "请先设置翻译模型"

        def mutate(root: dict[str, Any]) -> None:
            overrides = root.setdefault("translation", {}).setdefault("modelOverrides", {})
            if isinstance(overrides, dict):
                overrides.pop(model, None)

        self._config.update(mutate)
        audit(self._audit_sink, context, action="translation.model-override.clear", target=model)
        return True, model

    def cache_stats(self, context: ManagementContext) -> TranslationCacheStats:
        require(context, Capability.READ)
        count = int(self._translation.cache_count())
        stats = self._translation.cache_stats()
        stable = {
            "entries": count,
            "memoryEntries": int(stats.get("memoryEntries", 0) or 0),
            "memoryBytes": int(stats.get("memoryBytes", 0) or 0),
            "hits": int(stats.get("hits", 0) or 0),
            "misses": int(stats.get("misses", 0) or 0),
        }
        return TranslationCacheStats(
            entries=stable["entries"],
            memory_entries=stable["memoryEntries"],
            memory_bytes=stable["memoryBytes"],
            hits=stable["hits"],
            misses=stable["misses"],
            revision=revision_for(stable),
        )

    def clear_cache(
        self,
        context: ManagementContext,
        *,
        expected_revision: str | None = None,
    ) -> int:
        require(context, Capability.WRITE)
        if expected_revision is not None:
            ensure_revision(expected_revision, self.cache_stats(context).revision)
        cleared = int(self._translation.clear_cache())
        audit(self._audit_sink, context, action="translation.cache.clear", target="translation-cache")
        return cleared

    def list_languages(self, context: ManagementContext) -> tuple[TranslationLanguage, ...]:
        require(context, Capability.READ)
        return tuple(TranslationLanguage(id=value, display_name=label) for value, label in TRANSLATION_LANGUAGES)

    def available_models(self, context: ManagementContext) -> list[str]:
        require(context, Capability.READ)
        return self._translation.available_models()

    def available_channels(self, context: ManagementContext) -> list[TranslationChannel]:
        require(context, Capability.READ)
        return self._translation.available_channels()

    async def test_text_direct(self, context: ManagementContext, text: str) -> dict[str, Any]:
        require(context, Capability.WRITE)
        return await self._translation.test_text(text)

    def test_translation(self, context: ManagementContext, text: str) -> ManagementOperation:
        require(context, Capability.WRITE)
        if not text.strip() or len(text) > 20000:
            raise invalid_field("text", "INVALID_LENGTH", "text must contain 1 to 20000 characters")
        if self._operation_registry is None:
            raise ManagementError(ManagementErrorCode.SERVICE_NOT_READY, retryable=True)
        return self._operation_registry.create(
            context,
            kind=self.TEST_KIND,
            payload={"text": text},
            cancellable=False,
        )

    def _start_test(self, operation_id: str, context: ManagementContext, payload: Any) -> None:
        store = self._operation_store
        if store is None:
            raise ManagementError(ManagementErrorCode.SERVICE_NOT_READY, retryable=True)

        def run() -> None:
            store.mark_running(operation_id)
            try:
                with safe_diagnostics.collect_failures() as failures:
                    result = asyncio.run(self._translation.test_text(str(payload["text"])))
                if result.get("ok"):
                    store.succeed(operation_id, result)
                else:
                    store.fail(
                        operation_id,
                        code=ManagementErrorCode.UPSTREAM_ERROR,
                        message=ManagementErrorCode.UPSTREAM_ERROR.value,
                        retryable=True,
                        diagnostic=failures[-1] if failures else None,
                    )
            except Exception:
                store.fail(
                    operation_id,
                    code=ManagementErrorCode.UPSTREAM_ERROR,
                    message=ManagementErrorCode.UPSTREAM_ERROR.value,
                    retryable=True,
                )

        if self._scheduler is not None:
            self._scheduler(run, "management-translation-test")
        else:
            store.submit(operation_id, run)
        audit(self._audit_sink, context, action="translation.test", target=operation_id, result="queued")
