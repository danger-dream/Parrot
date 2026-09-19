"""Authoritative transport-neutral control for downstream inference API keys."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from src import apikey_limiter, config, log_db
from src.channel import registry
from src.management_auth import (
    AuthMethod,
    Capability,
    CapabilityDenied,
    ManagementStateStore,
    authorize,
)
from src.management_control.context import AuditSink, ManagementContext, audit_record
from src.management_control.errors import ErrorField, ManagementError, ManagementErrorCode

from .models import (
    ApiKeyEnabledFilter,
    ApiKeyLimitOverride,
    ApiKeyLimiterSnapshot,
    ApiKeyModelUsage,
    ApiKeyPage,
    ApiKeyProvenance,
    ApiKeyReplacementPlan,
    ApiKeySecretResult,
    ApiKeySort,
    ApiKeySource,
    ApiKeyStats,
    ApiKeyUsage,
    ApiKeyView,
)


_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_SECRET_RE = re.compile(r"^[A-Za-z0-9\-_.~+/=]{8,256}$")
_BJT = timezone(timedelta(hours=8))
_PLAN_TTL_SECONDS = 300


@dataclass(slots=True)
class _Plan:
    plan_id: str
    token_digest: bytes
    key_id: str
    revision: str
    actor: str
    expires_at: float
    consumed: bool = False


class ApiKeyControl:
    """One business implementation shared by Management API and Telegram.

    Dependencies are injectable only at business/dynamic boundaries.  Defaults
    are the application's existing atomic config, limiter, statistics and model
    registries, preserving their established persistence/runtime semantics.
    """

    def __init__(
        self,
        *,
        config_store: Any = config,
        provenance_store: ManagementStateStore | None = None,
        limiter: Any = apikey_limiter,
        statistics: Any = log_db,
        model_registry: Any = registry,
        audit_sink: AuditSink | None = None,
        clock: Any = time.time,
        token_factory: Any = secrets.token_urlsafe,
        generated_secret_factory: Any | None = None,
    ) -> None:
        self._config = config_store
        self._provenance_store = provenance_store
        self._limiter = limiter
        self._stats = statistics
        self._models = model_registry
        self._audit = audit_sink
        self._clock = clock
        self._token_factory = token_factory
        self._generated_secret_factory = generated_secret_factory or (
            lambda: f"ccp-{secrets.token_hex(24)}"
        )
        self._plans: dict[str, _Plan] = {}
        self._plan_lock = threading.RLock()

    # ----- public queries -------------------------------------------------

    def list_api_keys(
        self,
        context: ManagementContext,
        *,
        page: int = 1,
        page_size: int = 50,
        enabled: ApiKeyEnabledFilter = ApiKeyEnabledFilter.ALL,
        source: ApiKeyProvenance | None = None,
        name_contains: str | None = None,
        sort: ApiKeySort = ApiKeySort.ORDER_ASC,
        include_secret: bool = False,
        include_stats: bool = True,
    ) -> ApiKeyPage:
        self._require(context, Capability.SECRETS_WRITE if include_secret else Capability.READ)
        if page < 1 or not 1 <= page_size <= 200:
            raise self._validation("page", "OUT_OF_RANGE", "page/pageSize is out of range")
        raw_keys = self._raw_keys()
        stats = self._period_stats() if include_stats else {}
        rows = [
            self._view(name, entry, index, stats.get(name), (), include_secret)
            for index, (name, entry) in enumerate(raw_keys.items(), start=1)
        ]
        if enabled is ApiKeyEnabledFilter.ENABLED:
            rows = [item for item in rows if item.enabled]
        elif enabled is ApiKeyEnabledFilter.DISABLED:
            rows = [item for item in rows if not item.enabled]
        if source is not None:
            rows = [item for item in rows if item.source.value == source.value]
        if name_contains:
            needle = name_contains.casefold()
            rows = [item for item in rows if needle in item.name.casefold()]
        if sort is ApiKeySort.ORDER_DESC:
            rows.reverse()
        elif sort is ApiKeySort.NAME_ASC:
            rows.sort(key=lambda item: (item.name.casefold(), item.order))
        elif sort is ApiKeySort.NAME_DESC:
            rows.sort(key=lambda item: (item.name.casefold(), item.order), reverse=True)
        elif sort is ApiKeySort.MONTH_CALLS_DESC:
            rows.sort(key=lambda item: (-item.month_stats.total, item.order))
        total = len(rows)
        start = (page - 1) * page_size
        items = tuple(rows[start:start + page_size])
        return ApiKeyPage(
            items=items,
            page=page,
            page_size=page_size,
            total=total,
            has_next=start + page_size < total,
            revision=self._collection_revision(raw_keys),
        )

    def snapshot_api_keys(
        self,
        context: ManagementContext,
        *,
        include_secret: bool = False,
    ) -> tuple[ApiKeyView, ...]:
        """Unpaged compatibility snapshot for adapters with frozen local paging."""
        self._require(context, Capability.SECRETS_WRITE if include_secret else Capability.READ)
        keys = self._raw_keys()
        return tuple(
            self._view(name, entry, index, None, (), include_secret)
            for index, (name, entry) in enumerate(keys.items(), start=1)
        )

    def get_api_key(
        self,
        context: ManagementContext,
        key_id: str,
        *,
        include_secret: bool = False,
        include_stats: bool = True,
    ) -> ApiKeyView:
        self._require(context, Capability.SECRETS_WRITE if include_secret else Capability.READ)
        raw_keys = self._raw_keys()
        raw = raw_keys.get(key_id)
        if raw is None:
            raise self._not_found(key_id)
        month = self._period_stats(key_id).get(key_id) if include_stats else None
        models = self._model_stats(key_id) if include_stats else ()
        order = list(raw_keys).index(key_id) + 1
        return self._view(key_id, raw, order, month, models, include_secret)

    def get_api_key_stats(
        self,
        context: ManagementContext,
        key_id: str,
    ) -> ApiKeyStats:
        self._require(context, Capability.READ)
        raw_keys = self._raw_keys()
        raw = raw_keys.get(key_id)
        if raw is None:
            raise self._not_found(key_id)
        since_ts = self._month_start_ts()
        try:
            overall = self._usage(self._stats.tokens_for_apikey(key_id, since_ts))
            models = self._model_stats(key_id, since_ts=since_ts)
        except ManagementError:
            raise
        except Exception as exc:
            raise ManagementError(
                ManagementErrorCode.DEPENDENCY_UNAVAILABLE,
                "API key statistics are unavailable",
                retryable=True,
            ) from exc
        return ApiKeyStats(
            key_id=key_id,
            since=datetime.fromtimestamp(since_ts, timezone.utc),
            overall=overall,
            by_model=models,
            revision=self._entry_revision(key_id, raw),
        )

    def limiter_snapshot(
        self, context: ManagementContext, key_id: str,
    ) -> ApiKeyLimiterSnapshot:
        self._require(context, Capability.READ)
        if key_id not in self._raw_keys():
            raise self._not_found(key_id)
        return self._limiter_snapshot(key_id)

    @classmethod
    def _configured_image_models(cls, cfg: dict) -> tuple[str, ...]:
        from src import image_catalog, media_config
        # Keep the existing xAI configuration order, then include models from
        # actual OAuth/API sources (including disabled sources). A default GPT
        # model with no configured source is not a grantable inventory item.
        xai = cfg.get("xaiOAuth") or {}
        legacy = cls._clean_models((cfg.get("image_models") or {}).get("xai") if isinstance(cfg.get("image_models"), dict) else xai.get("imageModels") if isinstance(xai, dict) else None)
        sourced = sorted({source.model for source in image_catalog.sources(cfg)})
        return tuple(dict.fromkeys((*legacy, *sourced)))

    @classmethod
    def _configured_video_models(cls, cfg: dict) -> tuple[str, ...]:
        mapping = cfg.get('video_models')
        if isinstance(mapping, dict):
            global_models = tuple(model for values in mapping.values() for model in cls._clean_models(values))
        else:
            global_models = cls._clean_models((cfg.get('xaiOAuth') or {}).get('videoModels'))
        # Account-scoped video inventories are explicit overrides at runtime.
        # Include disabled sources as grantable inventory, just as for images.
        from src import image_catalog
        sourced = sorted({source.model for source in image_catalog.sources(cfg, kind="video")})
        return tuple(dict.fromkeys((*global_models, *sourced)))

    def available_permission_models(self, context: ManagementContext) -> tuple[str, ...]:
        self._require(context, Capability.READ)
        cfg = self._config.get()
        xai = cfg.get("xaiOAuth") or {}
        images = self._configured_image_models(cfg)
        videos = self._configured_video_models(cfg)
        result: list[str] = []
        for item in (*self._models.available_models(), *images, *videos):
            model = str(item or "").strip()
            if model and model not in result:
                result.append(model)
        return tuple(result)

    def configured_media_models(
        self, context: ManagementContext,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        self._require(context, Capability.READ)
        xai = self._config.get().get("xaiOAuth") or {}
        if not isinstance(xai, dict):
            return (), ()
        return self._configured_image_models(self._config.get()), self._configured_video_models(self._config.get())

    def existing_secret_values(
        self, context: ManagementContext, *, exclude_key_id: str | None = None,
    ) -> tuple[str, ...]:
        """Privileged compatibility query needed by frozen Telegram validation."""
        self._require(context, Capability.SECRETS_WRITE)
        return tuple(
            str(entry.get("key") or "")
            for name, entry in self._raw_keys().items()
            if name != exclude_key_id and entry.get("key")
        )

    def load_model_stats(
        self, context: ManagementContext, key_id: str, since_ts: float,
    ) -> list[dict]:
        """Scheduler-safe business query; returns a detached stable data copy."""
        self._require(context, Capability.READ)
        rows = self._stats.apikey_model_stats(key_id, since_ts=since_ts)
        return [dict(row) for row in rows]

    # ----- public commands -----------------------------------------------

    def create_api_key(
        self,
        context: ManagementContext,
        *,
        name: str,
        mode: ApiKeySource,
        custom_secret: str | None = None,
    ) -> ApiKeySecretResult:
        self._require(context, Capability.SECRETS_WRITE)
        self.validate_name(name)
        if mode is ApiKeySource.GENERATED:
            if custom_secret is not None:
                raise self._validation("customSecret", "NOT_ALLOWED", "customSecret is not allowed in generated mode")
            secret = str(self._generated_secret_factory())
        elif mode is ApiKeySource.CUSTOM:
            if custom_secret is None:
                raise self._validation("customSecret", "REQUIRED", "customSecret is required in custom mode")
            secret = custom_secret
        else:
            raise self._validation("mode", "UNSUPPORTED_VALUE", "unsupported creation mode")
        self.validate_custom_secret(secret)

        def mutate(cfg: dict) -> None:
            keys = cfg.setdefault("apiKeys", {})
            if not isinstance(keys, dict):
                keys = {}
                cfg["apiKeys"] = keys
            if name in keys:
                raise ManagementError(ManagementErrorCode.RESOURCE_CONFLICT, "API key name already exists")
            for raw in keys.values():
                if self._normalize_entry(raw).get("key") == secret:
                    raise ManagementError(ManagementErrorCode.RESOURCE_CONFLICT, "API key secret is already in use")
            keys[name] = {
                "key": secret,
                **(
                    {"source": mode.value}
                    if context.actor.auth_method is not AuthMethod.TELEGRAM_ADMIN
                    else {}
                ),
                "enabled": True,
                "allowedModels": [],
                "allowImages": False,
                "allowVideos": False,
                "allowMcp": False,
                "mcpTools": [],
            }

        self._config.update(mutate)
        self._save_provenance(name, secret, mode.value)
        item = self._current_view(name)
        self._record(context, "apikey.create", name)
        return ApiKeySecretResult(api_key=item, secret=secret)

    def update_api_key(
        self,
        context: ManagementContext,
        key_id: str,
        *,
        changes: Mapping[str, Any],
        if_match: str | None = None,
    ) -> ApiKeyView:
        self._require(context, Capability.WRITE)
        known = {"enabled", "allow_images", "allow_videos", "allow_mcp", "mcp_tools",
                 "allowed_models", "limit_override"}
        unknown = set(changes) - known
        if unknown or not changes:
            path = sorted(unknown)[0] if unknown else "body"
            raise self._validation(path, "UNKNOWN_FIELD" if unknown else "EMPTY_UPDATE", "invalid update fields")
        allowed_models = changes.get("allowed_models")
        if allowed_models is not None:
            self._validate_allowed_models(allowed_models)
        if "mcp_tools" in changes:
            self._validate_mcp_tools(changes["mcp_tools"])
        limit_change = changes.get("limit_override", ...)
        if limit_change is not ... and limit_change is not None and not isinstance(limit_change, Mapping):
            raise self._validation("limitOverride", "INVALID_TYPE", "limitOverride must be an object or null")
        if isinstance(limit_change, Mapping):
            self._validate_limits(limit_change)

        def mutate(cfg: dict) -> None:
            keys = cfg.get("apiKeys") or {}
            raw = keys.get(key_id) if isinstance(keys, dict) else None
            if raw is None:
                raise self._not_found(key_id)
            entry = self._normalize_entry(raw)
            self._check_revision(key_id, entry, if_match)
            keys[key_id] = entry
            if "enabled" in changes:
                entry["enabled"] = bool(changes["enabled"])
            if "allow_images" in changes:
                entry["allowImages"] = bool(changes["allow_images"])
            if "allow_videos" in changes:
                entry["allowVideos"] = bool(changes["allow_videos"])
            if "allow_mcp" in changes:
                entry["allowMcp"] = bool(changes["allow_mcp"])
            if "mcp_tools" in changes:
                entry["mcpTools"] = [str(item) for item in (changes["mcp_tools"] or ())]
            if "allowed_models" in changes:
                entry["allowedModels"] = list(changes["allowed_models"] or ())
            if limit_change is None:
                entry.pop("limits", None)
            elif isinstance(limit_change, Mapping):
                limits = dict(entry.get("limits") or {})
                field_map = {
                    "enabled": "enabled",
                    "max_concurrent": "maxConcurrent",
                    "max_queue": "maxQueue",
                    "queue_wait_seconds": "queueWaitSeconds",
                }
                for field, stored in field_map.items():
                    if field not in limit_change:
                        continue
                    value = limit_change[field]
                    if value is None:
                        limits.pop(stored, None)
                    else:
                        limits[stored] = value
                if limits:
                    entry["limits"] = limits
                else:
                    entry.pop("limits", None)

        self._config.update(mutate)
        self._record(context, "apikey.update", key_id)
        return self._current_view(key_id)

    def delete_api_key(
        self,
        context: ManagementContext,
        key_id: str,
        *,
        if_match: str | None,
        require_confirmation: bool = True,
        missing_ok: bool = False,
    ) -> None:
        self._require(context, Capability.DESTRUCTIVE)
        if require_confirmation and not if_match:
            raise ManagementError(ManagementErrorCode.CONFIRMATION_REQUIRED, "If-Match is required")

        def mutate(cfg: dict) -> None:
            keys = cfg.get("apiKeys") or {}
            raw = keys.get(key_id) if isinstance(keys, dict) else None
            if raw is None:
                if missing_ok:
                    return
                raise self._not_found(key_id)
            entry = self._normalize_entry(raw)
            self._check_revision(key_id, entry, if_match)
            keys.pop(key_id, None)

        self._config.update(mutate)
        self._forget_provenance(key_id)
        self._limiter.forget_key(key_id)
        self._record(context, "apikey.delete", key_id)

    def plan_regeneration(
        self, context: ManagementContext, key_id: str,
    ) -> ApiKeyReplacementPlan:
        self._require(context, Capability.SECRETS_WRITE)
        raw = self._raw_keys().get(key_id)
        if raw is None:
            raise self._not_found(key_id)
        revision = self._entry_revision(key_id, raw)
        plan_id = "akplan_" + str(self._token_factory(18))
        token = "akpt_" + str(self._token_factory(32))
        expires = self._clock() + _PLAN_TTL_SECONDS
        with self._plan_lock:
            self._prune_plans_locked()
            self._plans[plan_id] = _Plan(
                plan_id=plan_id,
                token_digest=self._digest(token),
                key_id=key_id,
                revision=revision,
                actor=self._actor_key(context),
                expires_at=expires,
            )
        self._record(context, "apikey.regeneration.plan", key_id)
        return ApiKeyReplacementPlan(
            plan_id=plan_id,
            plan_token=token,
            key_id=key_id,
            revision=revision,
            expires_at=datetime.fromtimestamp(expires, timezone.utc),
            impact="Existing clients using the current secret will fail immediately after commit",
        )

    def regenerate_api_key(
        self,
        context: ManagementContext,
        key_id: str,
        *,
        plan_id: str | None = None,
        plan_token: str | None = None,
        require_plan: bool = True,
        reset_runtime: bool = True,
    ) -> ApiKeySecretResult:
        self._require(context, Capability.SECRETS_WRITE)
        if key_id not in self._raw_keys():
            raise self._not_found(key_id)
        plan: _Plan | None = None
        if require_plan:
            if not plan_id or not plan_token:
                raise ManagementError(ManagementErrorCode.CONFIRMATION_REQUIRED, "A replacement plan is required")
            plan = self._validated_plan(context, key_id, plan_id, plan_token)
        secret = str(self._generated_secret_factory())
        self.validate_custom_secret(secret)
        self._replace_secret_atomic(
            key_id,
            secret,
            expected_revision=plan.revision if plan else None,
            source=ApiKeyProvenance.GENERATED,
            record_source=context.actor.auth_method is not AuthMethod.TELEGRAM_ADMIN,
        )
        if plan is not None:
            with self._plan_lock:
                plan.consumed = True
        if reset_runtime:
            self._limiter.forget_key(key_id)
        item = self._current_view(key_id)
        self._record(context, "apikey.regenerate", key_id)
        return ApiKeySecretResult(api_key=item, secret=secret)

    def replace_api_key_secret(
        self,
        context: ManagementContext,
        key_id: str,
        *,
        custom_secret: str,
        if_match: str | None,
        require_revision: bool = True,
        reset_runtime: bool = True,
    ) -> ApiKeySecretResult:
        self._require(context, Capability.SECRETS_WRITE)
        self.validate_custom_secret(custom_secret)
        if require_revision and not if_match:
            raise ManagementError(ManagementErrorCode.CONFIRMATION_REQUIRED, "If-Match is required")
        self._replace_secret_atomic(
            key_id,
            custom_secret,
            expected_revision=if_match,
            source=ApiKeyProvenance.CUSTOM,
            record_source=context.actor.auth_method is not AuthMethod.TELEGRAM_ADMIN,
        )
        if reset_runtime:
            self._limiter.forget_key(key_id)
        item = self._current_view(key_id)
        self._record(context, "apikey.secret.replace", key_id)
        return ApiKeySecretResult(api_key=item, secret=custom_secret)

    def reorder_api_keys(
        self,
        context: ManagementContext,
        order: list[str] | tuple[str, ...],
        *,
        if_match: str | None,
        require_revision: bool = True,
        require_complete: bool = True,
    ) -> str:
        self._require(context, Capability.WRITE)
        if len(set(order)) != len(order):
            raise self._validation("keyIds", "DUPLICATE_ID", "keyIds contains duplicates")
        if require_revision and not if_match:
            raise ManagementError(ManagementErrorCode.CONFIRMATION_REQUIRED, "If-Match is required")

        def mutate(cfg: dict) -> None:
            keys = cfg.get("apiKeys") or {}
            if not isinstance(keys, dict):
                keys = {}
            current_revision = self._collection_revision(self._normalized_keys(keys))
            if if_match is not None and not hmac.compare_digest(if_match, current_revision):
                raise ManagementError(ManagementErrorCode.REVISION_CONFLICT)
            current = set(keys)
            supplied = set(order)
            if require_complete and supplied != current:
                raise self._validation("keyIds", "INCOMPLETE_SET", "keyIds must be the complete current set")
            unknown = supplied - current
            if unknown:
                raise self._validation("keyIds", "UNKNOWN_ID", "keyIds contains an unknown API key")
            ordered = {name: keys[name] for name in order if name in keys}
            ordered.update({name: value for name, value in keys.items() if name not in supplied})
            cfg["apiKeys"] = ordered

        snapshot = self._config.update(mutate)
        result = self._collection_revision(self._normalized_keys(snapshot.get("apiKeys") or {}))
        self._record(context, "apikey.reorder", "api-keys")
        return result

    def reset_api_key_limiter(self, context: ManagementContext, key_id: str) -> ApiKeyLimiterSnapshot:
        self._require(context, Capability.WRITE)
        if key_id not in self._raw_keys():
            raise self._not_found(key_id)
        self._limiter.forget_key(key_id)
        self._record(context, "apikey.limiter.reset", key_id)
        return self._limiter_snapshot(key_id)

    # ----- validators exposed for the Telegram adapter -------------------

    @staticmethod
    def validate_name(name: str) -> None:
        if not _NAME_RE.fullmatch(str(name or "")):
            raise ApiKeyControl._validation("name", "INVALID_NAME", "name must match [A-Za-z0-9_.-]{1,64}")

    @staticmethod
    def validate_custom_secret(secret: str) -> None:
        value = str(secret or "")
        if len(value) < 8:
            raise ApiKeyControl._validation("customSecret", "TOO_SHORT", "secret must contain at least 8 characters")
        if len(value) > 256:
            raise ApiKeyControl._validation("customSecret", "TOO_LONG", "secret must contain at most 256 characters")
        if not _SECRET_RE.fullmatch(value):
            raise ApiKeyControl._validation("customSecret", "INVALID_CHARACTERS", "secret contains unsupported characters")

    # ----- internals ------------------------------------------------------

    def _replace_secret_atomic(
        self,
        key_id: str,
        secret: str,
        *,
        expected_revision: str | None,
        source: ApiKeyProvenance,
        record_source: bool,
    ) -> None:
        def mutate(cfg: dict) -> None:
            keys = cfg.get("apiKeys") or {}
            raw = keys.get(key_id) if isinstance(keys, dict) else None
            if raw is None:
                raise self._not_found(key_id)
            entry = self._normalize_entry(raw)
            self._check_revision(key_id, entry, expected_revision)
            for name, other in keys.items():
                if name != key_id and self._normalize_entry(other).get("key") == secret:
                    raise ManagementError(ManagementErrorCode.RESOURCE_CONFLICT, "API key secret is already in use")
            entry["key"] = secret
            if record_source or "source" in entry:
                entry["source"] = source.value
            keys[key_id] = entry
        self._config.update(mutate)
        self._save_provenance(key_id, secret, source.value)

    def _validated_plan(
        self, context: ManagementContext, key_id: str, plan_id: str, token: str,
    ) -> _Plan:
        with self._plan_lock:
            plan = self._plans.get(plan_id)
            if plan is None or plan.consumed or not hmac.compare_digest(plan.token_digest, self._digest(token)):
                raise ManagementError(ManagementErrorCode.INVALID_OPERATION_STATE, "Replacement plan is invalid or already consumed")
            if plan.expires_at <= self._clock():
                plan.consumed = True
                raise ManagementError(ManagementErrorCode.INVALID_OPERATION_STATE, "Replacement plan has expired")
            if plan.actor != self._actor_key(context):
                raise ManagementError(ManagementErrorCode.CAPABILITY_DENIED)
            if plan.key_id != key_id:
                raise ManagementError(ManagementErrorCode.INVALID_OPERATION_STATE, "Replacement plan target does not match")
            return plan

    def _prune_plans_locked(self) -> None:
        now = self._clock()
        if len(self._plans) < 512:
            return
        for key in list(self._plans):
            plan = self._plans[key]
            if plan.consumed or plan.expires_at <= now:
                self._plans.pop(key, None)
        while len(self._plans) >= 512:
            self._plans.pop(next(iter(self._plans)))

    def _period_stats(self, key_id: str | None = None) -> dict[str, Mapping[str, Any]]:
        try:
            if key_id is not None:
                return {key_id: self._stats.tokens_for_apikey(key_id, self._month_start_ts())}
            snapshot = self._stats.stats_period_snapshot(self._month_start_ts())
            return dict((snapshot or {}).get("by_apikey") or {})
        except Exception as exc:
            raise ManagementError(
                ManagementErrorCode.DEPENDENCY_UNAVAILABLE,
                "API key statistics are unavailable",
                retryable=True,
            ) from exc

    def _model_stats(self, key_id: str, *, since_ts: float | None = None) -> tuple[ApiKeyModelUsage, ...]:
        try:
            rows = self._stats.apikey_model_stats(key_id, since_ts=since_ts or self._month_start_ts())
        except Exception as exc:
            raise ManagementError(
                ManagementErrorCode.DEPENDENCY_UNAVAILABLE,
                "API key model statistics are unavailable",
                retryable=True,
            ) from exc
        return tuple(
            ApiKeyModelUsage(model=str(row.get("final_model") or "?"), usage=self._usage(row))
            for row in rows
        )

    def _view(
        self,
        name: str,
        raw: Any,
        order: int,
        stats: Mapping[str, Any] | None,
        model_stats: tuple[ApiKeyModelUsage, ...],
        include_secret: bool,
    ) -> ApiKeyView:
        entry = self._normalize_entry(raw)
        secret = str(entry.get("key") or "")
        limits = entry.get("limits") if isinstance(entry.get("limits"), dict) else None
        override = None
        if limits:
            override = ApiKeyLimitOverride(
                enabled=limits.get("enabled"),
                max_concurrent=limits.get("maxConcurrent"),
                max_queue=limits.get("maxQueue"),
                queue_wait_seconds=limits.get("queueWaitSeconds"),
            )
        return ApiKeyView(
            key_id=name,
            name=name,
            order=order,
            enabled=entry.get("enabled") is not False,
            source=self._provenance(name, entry),
            masked_hint=self._masked(secret),
            allow_images=bool(entry.get("allowImages")),
            allow_videos=bool(entry.get("allowVideos")),
            allow_mcp=bool(entry.get("allowMcp")),
            mcp_tools=tuple(
                str(item) for item in (entry.get("mcpTools") or ()) if isinstance(item, str)
            ),
            allowed_models=tuple(entry.get("allowedModels") or ()),
            limit_override=override,
            limiter=self._limiter_snapshot(name),
            month_stats=self._usage(stats),
            model_stats=model_stats,
            revision=self._entry_revision(name, entry),
            secret=secret if include_secret else None,
        )

    def _current_view(self, key_id: str, *, include_secret: bool = False) -> ApiKeyView:
        keys = self._raw_keys()
        raw = keys.get(key_id)
        if raw is None:
            raise self._not_found(key_id)
        return self._view(
            key_id, raw, list(keys).index(key_id) + 1, None, (), include_secret,
        )

    def _limiter_snapshot(self, key_id: str) -> ApiKeyLimiterSnapshot:
        raw = self._limiter.key_snapshot(key_id)
        return ApiKeyLimiterSnapshot(
            enabled=bool(raw.get("enabled", True)),
            in_flight=int(raw.get("in_flight") or 0),
            max_concurrent=int(raw.get("max_concurrent") or 0),
            max_queue=int(raw.get("max_queue") or 0),
            queue_wait_seconds=int(raw.get("queue_wait_seconds") or 0),
            waiting=int(raw.get("waiting") or 0),
            oldest_wait_seconds=int(raw.get("oldest_wait_seconds") or 0),
            unlimited=bool(raw.get("unlimited")),
            enabled_source=str(raw.get("enabled_source") or "global"),
            max_concurrent_source=str(raw.get("max_concurrent_source") or "global"),
            max_queue_source=str(raw.get("max_queue_source") or "global"),
            queue_wait_source=str(raw.get("queue_wait_source") or "global"),
        )

    @staticmethod
    def _usage(raw: Mapping[str, Any] | None) -> ApiKeyUsage:
        row = raw or {}
        return ApiKeyUsage(
            total=int(row.get("total") or 0),
            success_count=int(row.get("success_count") or 0),
            error_count=int(row.get("error_count") or 0),
            input_tokens=int(row.get("input") or 0),
            output_tokens=int(row.get("output") or 0),
            cache_creation_tokens=int(row.get("cache_creation") or 0),
            cache_read_tokens=int(row.get("cache_read") or 0),
            avg_tps=row.get("avg_tps"),
            max_tps=row.get("max_tps"),
            min_tps=row.get("min_tps"),
            cost_ticks=int(row.get("cost_ticks") or 0),
            actual_cost_ticks=int(row.get("actual_cost_ticks") or 0),
            estimated_cost_ticks=int(row.get("estimated_cost_ticks") or 0),
            costed_success=int(row.get("costed_success") or 0),
            unpriced_success=int(row.get("unpriced_success") or 0),
        )

    def _raw_keys(self) -> dict[str, dict]:
        return self._normalized_keys(self._config.get().get("apiKeys") or {})

    @classmethod
    def _normalized_keys(cls, value: Any) -> dict[str, dict]:
        if not isinstance(value, dict):
            return {}
        return {str(name): cls._normalize_entry(raw) for name, raw in value.items() if cls._normalize_entry(raw).get("key")}

    def _provenance(self, key_id: str, entry: Mapping[str, Any]) -> ApiKeyProvenance:
        source = entry.get("source")
        if source not in {ApiKeyProvenance.CUSTOM.value, ApiKeyProvenance.GENERATED.value}:
            source = None
            if self._provenance_store is not None:
                try:
                    source = self._provenance_store.get_api_key_provenance(
                        key_id=key_id,
                        secret_fingerprint=self._provenance_fingerprint(
                            key_id, str(entry.get("key") or ""),
                        ),
                    )
                except Exception:
                    pass
        return ApiKeyProvenance(source or ApiKeyProvenance.UNKNOWN.value)

    def _save_provenance(self, key_id: str, secret: str, source: str) -> None:
        if self._provenance_store is not None:
            try:
                self._provenance_store.set_api_key_provenance(
                    key_id=key_id,
                    secret_fingerprint=self._provenance_fingerprint(key_id, secret),
                    source=source,
                )
            except Exception:
                # Auxiliary provenance must not turn a successful business write
                # into failure. A record for an older secret will not match.
                pass

    def _forget_provenance(self, key_id: str) -> None:
        if self._provenance_store is None:
            return
        try:
            self._provenance_store.forget_api_key_provenance(key_id=key_id)
        except Exception:
            # Deletion already succeeded; provenance cleanup is best-effort.
            return

    @staticmethod
    def _provenance_fingerprint(key_id: str, secret: str) -> str:
        payload = b"parrot:api-key-provenance:v1\0" + str(key_id).encode("utf-8")
        payload += b"\0" + str(secret).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _normalize_entry(raw: Any) -> dict:
        if isinstance(raw, str):
            return {"key": raw, "enabled": True, "allowedModels": [], "allowImages": False,
                    "allowVideos": False, "allowMcp": False, "mcpTools": []}
        if isinstance(raw, dict):
            result = dict(raw)
            result.setdefault("enabled", True)
            result.setdefault("allowedModels", [])
            result.setdefault("allowImages", False)
            result.setdefault("allowVideos", False)
            # 既有条目没有 MCP 字段时必须保持关闭，不能因缺字段就放行。
            result.setdefault("allowMcp", False)
            result.setdefault("mcpTools", [])
            return result
        return {}

    def _validate_allowed_models(self, values: Any) -> None:
        if not isinstance(values, (list, tuple)):
            raise self._validation("allowedModels", "INVALID_TYPE", "allowedModels must be an array")
        seen: set[str] = set()
        available = set(self.available_permission_models_unchecked())
        for index, value in enumerate(values):
            model = str(value or "").strip()
            if not model:
                raise self._validation(f"allowedModels[{index}]", "EMPTY_MODEL", "model must not be empty")
            if model in seen:
                raise self._validation(f"allowedModels[{index}]", "DUPLICATE_MODEL", "model is duplicated")
            if model not in available:
                raise self._validation(f"allowedModels[{index}]", "UNKNOWN_MODEL", "model is not currently available")
            seen.add(model)

    def _validate_mcp_tools(self, values: Any) -> None:
        """校验该 Key 选用的 MCP 工具名。空列表 = 跟随全局开关。"""
        from src.mcp.catalog import TOOL_NAMES

        if not isinstance(values, (list, tuple)):
            raise self._validation("mcpTools", "INVALID_TYPE", "mcpTools must be an array")
        seen: set[str] = set()
        for index, value in enumerate(values):
            name = str(value or "").strip()
            if not name:
                raise self._validation(f"mcpTools[{index}]", "EMPTY_TOOL", "tool name must not be empty")
            if name in seen:
                raise self._validation(f"mcpTools[{index}]", "DUPLICATE_TOOL", "tool name is duplicated")
            if name not in TOOL_NAMES:
                raise self._validation(f"mcpTools[{index}]", "UNKNOWN_TOOL", "unknown MCP tool name")
            seen.add(name)

    def available_permission_models_unchecked(self) -> tuple[str, ...]:
        cfg = self._config.get()
        xai = cfg.get("xaiOAuth") or {}
        images = self._configured_image_models(cfg)
        videos = self._configured_video_models(cfg)
        out: list[str] = []
        for value in (*self._models.available_models(), *images, *videos):
            model = str(value or "").strip()
            if model and model not in out:
                out.append(model)
        return tuple(out)

    @classmethod
    def _validate_limits(cls, values: Mapping[str, Any]) -> None:
        known = {"enabled", "max_concurrent", "max_queue", "queue_wait_seconds"}
        unknown = set(values) - known
        if unknown:
            raise cls._validation("limitOverride", "UNKNOWN_FIELD", "limitOverride contains an unknown field")
        for field in ("max_concurrent", "max_queue", "queue_wait_seconds"):
            value = values.get(field)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise cls._validation(field, "OUT_OF_RANGE", f"{field} must be an integer greater than or equal to zero")
        enabled = values.get("enabled")
        if enabled is not None and not isinstance(enabled, bool):
            raise cls._validation("enabled", "INVALID_TYPE", "enabled must be boolean or null")

    def _check_revision(self, key_id: str, entry: Mapping[str, Any], if_match: str | None) -> None:
        if if_match is not None and not hmac.compare_digest(if_match, self._entry_revision(key_id, entry)):
            raise ManagementError(ManagementErrorCode.REVISION_CONFLICT)

    @staticmethod
    def _entry_revision(key_id: str, entry: Mapping[str, Any]) -> str:
        payload = json.dumps([key_id, entry], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return '"ak-' + hashlib.sha256(payload.encode()).hexdigest()[:24] + '"'

    @classmethod
    def _collection_revision(cls, keys: Mapping[str, Mapping[str, Any]]) -> str:
        payload = json.dumps(list(keys.items()), ensure_ascii=False, sort_keys=False, separators=(",", ":"))
        return '"aks-' + hashlib.sha256(payload.encode()).hexdigest()[:24] + '"'

    @staticmethod
    def _masked(secret: str) -> str:
        if not secret:
            return "not-configured"
        if len(secret) <= 8:
            return "•" * len(secret)
        return f"{secret[:4]}…{secret[-4:]}"

    @staticmethod
    def _clean_models(raw: Any) -> tuple[str, ...]:
        if not isinstance(raw, list):
            return ()
        out: list[str] = []
        for item in raw:
            value = str(item or "").strip()
            if value and value not in out:
                out.append(value)
        return tuple(out)

    def _month_start_ts(self) -> float:
        return datetime.fromtimestamp(self._clock(), _BJT).replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()

    @staticmethod
    def _actor_key(context: ManagementContext) -> str:
        return context.actor.session_id or context.actor.subject_id

    @staticmethod
    def _digest(value: str) -> bytes:
        return hashlib.sha256(value.encode()).digest()

    @staticmethod
    def _validation(path: str, code: str, message: str) -> ManagementError:
        return ManagementError(
            ManagementErrorCode.VALIDATION_FAILED,
            fields=(ErrorField(path=path, code=code, message=message),),
        )

    @staticmethod
    def _not_found(key_id: str) -> ManagementError:
        return ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND, f"API key {key_id!r} was not found")

    @staticmethod
    def _require(context: ManagementContext, capability: Capability) -> None:
        try:
            authorize(context.actor, capability)
        except CapabilityDenied as exc:
            raise ManagementError(ManagementErrorCode.CAPABILITY_DENIED) from exc

    def _record(self, context: ManagementContext, action: str, target: str) -> None:
        if self._audit is not None:
            self._audit.record(audit_record(context, action=action, target=target, result="succeeded"))
