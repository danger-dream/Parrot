"""models.dev metadata bindings and independent compact-model selection.

Persistent configuration stores only binding identities:

``modelBindings.defaults[client model] -> provider/model``
``modelBindings.scoped[scope key][client model] -> provider/model``

The catalog record itself always comes from :mod:`model_pricing`, which owns the
single bundled/cache/remote models.dev catalog lifecycle. Effective resolution
is strictly scoped, then default, then none.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from . import config, model_pricing

SUMMARY_OUTPUT_RESERVE_TOKENS = 20_000
DEFAULT_COMPACT_BUFFER_TOKENS = 20_000
_LEGACY_MIGRATION_VERSION = 1


@dataclass(frozen=True)
class MetadataBinding:
    client_visible_model: str
    target: str
    provider_id: str
    catalog_model_id: str
    scope_key: str | None
    outbound_model: str | None
    source: str
    metadata: Mapping[str, Any]

    @property
    def kind(self) -> str:
        return "scoped" if self.scope_key else "default"


@dataclass(frozen=True)
class ModelInventoryItem:
    scope_key: str
    scope_type: str
    scope_label: str
    client_visible_model: str
    outbound_model: str


# Legacy input normalization remains available for config migration/tests, but
# normalized user values are never a runtime metadata source after this refactor.
_INT_KEYS = {"contextWindow", "maxOutputTokens"}
_FLOAT_KEYS = {
    "inputPricePer1M", "outputPricePer1M",
    "cacheReadPricePer1M", "cacheWritePricePer1M",
}
_BOOL_KEYS = {"vision", "compressionModel"}
_LIST_KEYS = {"reasoningEfforts"}
_STR_KEYS = {"defaultReasoningEffort"}
_FIELD_ALIASES = {
    "context_window": "contextWindow", "context_window_tokens": "contextWindow",
    "context": "contextWindow", "max_context": "contextWindow",
    "max_context_tokens": "contextWindow", "contextLength": "contextWindow",
    "context_length": "contextWindow", "maxOutput": "maxOutputTokens",
    "max_output": "maxOutputTokens", "max_output_tokens": "maxOutputTokens",
    "maxTokens": "maxOutputTokens", "max_tokens": "maxOutputTokens",
    "canVision": "vision", "can_vision": "vision", "image": "vision",
    "images": "vision", "visionSupport": "vision", "supportsImages": "vision",
    "input_price": "inputPricePer1M", "input_price_per_1m": "inputPricePer1M",
    "output_price": "outputPricePer1M", "output_price_per_1m": "outputPricePer1M",
    "cache_read_price": "cacheReadPricePer1M",
    "cache_read_price_per_1m": "cacheReadPricePer1M",
    "cache_write_price": "cacheWritePricePer1M",
    "cache_write_price_per_1m": "cacheWritePricePer1M",
    "cache_output_price": "cacheWritePricePer1M",
    "cache_output_price_per_1m": "cacheWritePricePer1M",
    "compact": "compressionModel", "compression": "compressionModel",
    "compression_model": "compressionModel", "isCompressionModel": "compressionModel",
    "reasoning": "reasoningEfforts", "reasoning_efforts": "reasoningEfforts",
    "reasoningEffort": "reasoningEfforts", "thinking": "reasoningEfforts",
    "thinking_efforts": "reasoningEfforts", "thinkingEfforts": "reasoningEfforts",
    "efforts": "reasoningEfforts", "support_reasoning": "reasoningEfforts",
    "supported_reasoning": "reasoningEfforts",
    "supported_reasoning_efforts": "reasoningEfforts",
    "default_reasoning": "defaultReasoningEffort",
    "default_reasoning_effort": "defaultReasoningEffort",
    "defaultReasoning": "defaultReasoningEffort",
    "default_thinking": "defaultReasoningEffort",
    "default_thinking_effort": "defaultReasoningEffort",
    "thinking_default": "defaultReasoningEffort",
}


def normalize_model_name(model: Any) -> str:
    return str(model or "").strip()


def _to_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        parsed = int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _to_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return None
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on", "是", "支持", "启用", "设为"}:
        return True
    if text in {"0", "false", "no", "n", "off", "否", "不支持", "关闭", "禁用"}:
        return False
    return None


def _to_effort_list(value: Any) -> list[str]:
    if value is None:
        return []
    items = (
        [str(item) for item in value]
        if isinstance(value, (list, tuple, set))
        else re.split(r"[,，、;；\n]+", str(value))
    )
    result: list[str] = []
    for item in items:
        normalized = re.sub(r"\s+", "", item).lower()
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def normalize_metadata(raw: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    result: dict[str, Any] = {}
    for key, value in raw.items():
        canonical = _FIELD_ALIASES.get(str(key or "").strip(), str(key or "").strip())
        if canonical in _INT_KEYS:
            parsed = _to_int(value)
        elif canonical in _FLOAT_KEYS:
            parsed = _to_float(value)
        elif canonical in _BOOL_KEYS:
            parsed = _to_bool(value)
        elif canonical in _LIST_KEYS:
            parsed = _to_effort_list(value) or None
        elif canonical in _STR_KEYS:
            parsed = (_to_effort_list(value) or [None])[0]
        else:
            continue
        if parsed is not None:
            result[canonical] = parsed
    return result


def _binding_roots(cfg: Mapping[str, Any] | None = None) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    source = config.get() if cfg is None else cfg
    root = source.get("modelBindings") or {}
    if not isinstance(root, Mapping):
        return {}, {}
    defaults = root.get("defaults") or {}
    scoped = root.get("scoped") or {}
    return (
        defaults if isinstance(defaults, Mapping) else {},
        scoped if isinstance(scoped, Mapping) else {},
    )


def _binding_fields(raw: Any) -> tuple[str, str, str | None] | None:
    if isinstance(raw, str):
        target, source, outbound = raw, "config", None
    elif isinstance(raw, Mapping):
        target = raw.get("target")
        source = raw.get("source") or "config"
        outbound = raw.get("outboundModel")
    else:
        return None
    target_name = str(target or "").strip().lower()
    source_name = str(source or "config").strip() or "config"
    outbound_name = normalize_model_name(outbound) or None
    if not target_name or "/" not in target_name:
        return None
    return target_name, source_name, outbound_name


def _legacy_default_target(model: str, cfg: Mapping[str, Any]) -> str | None:
    binding_root = cfg.get("modelBindings") or {}
    if (
        isinstance(binding_root, Mapping)
        and binding_root.get("legacyMigrationVersion") == _LEGACY_MIGRATION_VERSION
    ):
        return None
    legacy = cfg.get("modelMetadata") or {}
    if not isinstance(legacy, Mapping) or model not in legacy:
        return None
    return model_pricing.canonical_official_model(model)


def _record_for(
    client_visible_model: str,
    raw: Any,
    *,
    scope_key: str | None,
    known_outbound_model: str | None,
    allow_missing_catalog: bool = False,
) -> MetadataBinding | None:
    fields = _binding_fields(raw)
    if fields is None:
        return None
    target, source, saved_outbound = fields
    # A scope-specific binding is tied to the actual model that existed when it
    # was selected. If that channel alias is later repointed, it no longer
    # applies and the resolver may continue to the default binding.
    known = normalize_model_name(known_outbound_model) or None
    if scope_key and saved_outbound and known and saved_outbound != known:
        return None
    metadata = model_pricing.catalog_metadata(target)
    if metadata is None and not allow_missing_catalog:
        return None
    metadata = metadata or {}
    provider, catalog_model = target.split("/", 1)
    return MetadataBinding(
        client_visible_model=client_visible_model,
        target=target,
        provider_id=provider,
        catalog_model_id=catalog_model,
        scope_key=scope_key,
        outbound_model=saved_outbound,
        source=source,
        metadata=metadata,
    )


def resolve_binding(
    client_visible_model: Any,
    *,
    scope_key: str | None = None,
    outbound_model: str | None = None,
) -> MetadataBinding | None:
    """Resolve effective metadata as ``scoped > default > none``."""
    model = normalize_model_name(client_visible_model)
    scope = normalize_model_name(scope_key) or None
    if not model:
        return None
    cfg = config.get()
    defaults, scoped = _binding_roots(cfg)
    if scope:
        scope_bindings = scoped.get(scope) or {}
        if isinstance(scope_bindings, Mapping) and model in scope_bindings:
            binding = _record_for(
                model, scope_bindings.get(model), scope_key=scope,
                known_outbound_model=outbound_model,
            )
            if binding is not None:
                return binding
    if model in defaults:
        binding = _record_for(
            model, defaults.get(model), scope_key=None,
            known_outbound_model=None,
        )
        if binding is not None:
            return binding
    # Read-through compatibility before the one-time startup migration has run.
    legacy_target = _legacy_default_target(model, cfg)
    if legacy_target:
        return _record_for(
            model, {"target": legacy_target, "source": "legacy"},
            scope_key=None, known_outbound_model=None,
        )
    return None


def set_binding(
    client_visible_model: str,
    target: str,
    *,
    scope_key: str | None = None,
    outbound_model: str | None = None,
    source: str = "manual",
) -> None:
    model = normalize_model_name(client_visible_model)
    exact_target = str(target or "").strip().lower()
    scope = normalize_model_name(scope_key) or None
    outbound = normalize_model_name(outbound_model) or None
    if not model:
        raise ValueError("client-visible model is required")
    if model_pricing.catalog_model(exact_target) is None:
        raise ValueError("models.dev provider/model does not exist")
    if scope and not outbound:
        raise ValueError("scoped binding requires outbound model")
    entry: dict[str, Any] = {
        "target": exact_target,
        "source": str(source or "manual").strip() or "manual",
    }
    if scope:
        entry["outboundModel"] = outbound

    def mutate(cfg: dict) -> None:
        root = cfg.setdefault("modelBindings", {})
        if not isinstance(root, dict):
            root = {}
            cfg["modelBindings"] = root
        if scope:
            scopes = root.setdefault("scoped", {})
            if not isinstance(scopes, dict):
                scopes = {}
                root["scoped"] = scopes
            values = scopes.setdefault(scope, {})
            if not isinstance(values, dict):
                values = {}
                scopes[scope] = values
            values[model] = entry
        else:
            values = root.setdefault("defaults", {})
            if not isinstance(values, dict):
                values = {}
                root["defaults"] = values
            values[model] = entry

    config.update(mutate)


def delete_binding(client_visible_model: str, *, scope_key: str | None = None) -> bool:
    model = normalize_model_name(client_visible_model)
    scope = normalize_model_name(scope_key) or None
    removed = [False]

    def mutate(cfg: dict) -> None:
        defaults, scoped = _binding_roots(cfg)
        if scope:
            values = scoped.get(scope) if isinstance(scoped, dict) else None
        else:
            values = defaults
        if isinstance(values, dict) and model in values:
            del values[model]
            removed[0] = True
        if scope and isinstance(scoped, dict) and not values:
            scoped.pop(scope, None)

    config.update(mutate, skip_if_unchanged=True)
    return removed[0]


def list_bindings() -> list[MetadataBinding]:
    cfg = config.get()
    defaults, scoped = _binding_roots(cfg)
    result: list[MetadataBinding] = []
    for model, raw in defaults.items():
        binding = _record_for(
            str(model), raw, scope_key=None, known_outbound_model=None,
            allow_missing_catalog=True,
        )
        if binding:
            result.append(binding)
    for scope, values in scoped.items():
        if not isinstance(values, Mapping):
            continue
        for model, raw in values.items():
            fields = _binding_fields(raw)
            known = fields[2] if fields else None
            binding = _record_for(
                str(model), raw, scope_key=str(scope), known_outbound_model=known,
                allow_missing_catalog=True,
            )
            if binding:
                result.append(binding)
    return sorted(result, key=lambda item: (item.scope_key or "", item.client_visible_model))


def all_metadata() -> dict[str, dict[str, Any]]:
    """Compatibility view of resolved default metadata, keyed by visible model."""
    result: dict[str, dict[str, Any]] = {}
    defaults, _ = _binding_roots()
    for model in defaults:
        binding = resolve_binding(model)
        if binding:
            result[str(model)] = dict(binding.metadata)
    return result


def get_metadata(
    model: Any,
    *,
    scope_key: str | None = None,
    outbound_model: str | None = None,
) -> dict[str, Any]:
    binding = resolve_binding(model, scope_key=scope_key, outbound_model=outbound_model)
    return dict(binding.metadata) if binding else {}


def list_models() -> list[str]:
    return sorted({binding.client_visible_model for binding in list_bindings()})


def set_metadata(model: str, meta: dict[str, Any]) -> None:
    """Retain old config-writing API for compatibility; runtime ignores values."""
    name = normalize_model_name(model)
    normalized = normalize_metadata(meta)
    if not name or not normalized:
        raise ValueError("metadata is empty or invalid")
    default_effort = normalized.get("defaultReasoningEffort")
    if default_effort and default_effort not in normalized.get("reasoningEfforts", []):
        raise ValueError("default reasoning effort must be one of reasoning efforts")

    def mutate(cfg: dict) -> None:
        legacy = cfg.setdefault("modelMetadata", {})
        if not isinstance(legacy, dict):
            legacy = {}
            cfg["modelMetadata"] = legacy
        current = legacy.get(name) if isinstance(legacy.get(name), dict) else {}
        current = dict(current)
        current.update(normalized)
        legacy[name] = current

    config.update(mutate)
    if normalized.get("compressionModel") is True:
        set_compression_model(name)


def delete_metadata(model: str) -> bool:
    """Compatibility delete removes the default binding, not catalog metadata."""
    return delete_binding(model)


def inventory_items() -> list[ModelInventoryItem]:
    """Enumerate every current OAuth/API scope without cross-scope deduplication."""
    from .channel import registry

    result: list[ModelInventoryItem] = []
    for channel in registry.all_channels():
        scope_key = normalize_model_name(getattr(channel, "key", ""))
        if not scope_key:
            continue
        scope_type = normalize_model_name(getattr(channel, "type", "")) or "unknown"
        scope_label = normalize_model_name(getattr(channel, "display_name", "")) or scope_key
        try:
            visible_models = channel.list_client_models()
        except Exception:
            continue
        for visible in visible_models:
            model = normalize_model_name(visible)
            if not model:
                continue
            try:
                outbound = normalize_model_name(channel.supports_model(model)) or model
            except Exception:
                outbound = model
            result.append(ModelInventoryItem(
                scope_key=scope_key,
                scope_type=scope_type,
                scope_label=scope_label,
                client_visible_model=model,
                outbound_model=outbound,
            ))
    return sorted(result, key=lambda item: (item.scope_key, item.client_visible_model))


def auto_sync_metadata(
    items: Iterable[ModelInventoryItem] | None = None,
) -> dict[str, Any]:
    """Create/update canonical exact default bindings for all visible models."""
    inventory = list(items if items is not None else inventory_items())
    visible_models = sorted({item.client_visible_model for item in inventory if item.client_visible_model})
    cfg = config.get()
    defaults, _ = _binding_roots(cfg)
    changes: dict[str, dict[str, str]] = {}
    created: list[str] = []
    updated: list[str] = []
    unchanged: list[str] = []
    unmatched: list[str] = []
    for model in visible_models:
        target = model_pricing.canonical_official_model(model)
        if target is None:
            unmatched.append(model)
            continue
        current = _binding_fields(defaults.get(model)) if model in defaults else None
        if current and current[0] == target:
            unchanged.append(model)
            continue
        changes[model] = {"target": target, "source": "auto"}
        (updated if model in defaults else created).append(model)

    if changes:
        def mutate(current_cfg: dict) -> None:
            root = current_cfg.setdefault("modelBindings", {})
            if not isinstance(root, dict):
                root = {}
                current_cfg["modelBindings"] = root
            values = root.setdefault("defaults", {})
            if not isinstance(values, dict):
                values = {}
                root["defaults"] = values
            values.update(changes)

        config.update(mutate)
    return {
        "scanned": len(visible_models),
        "created": created,
        "updated": updated,
        "unchanged": unchanged,
        "unmatched": unmatched,
        "success": len(created) + len(updated),
    }


def migrate_legacy_config() -> dict[str, int]:
    """Persist the minimal exact legacy migration once the catalog is loaded."""
    cfg = config.get()
    root = cfg.get("modelBindings") or {}
    if isinstance(root, Mapping) and root.get("legacyMigrationVersion") == _LEGACY_MIGRATION_VERSION:
        return {"bindings": 0, "compression": 0}
    legacy = cfg.get("modelMetadata") or {}
    if not isinstance(legacy, Mapping):
        legacy = {}
    defaults, _ = _binding_roots(cfg)
    additions: dict[str, dict[str, str]] = {}
    compression_model = normalize_model_name(cfg.get("compressionModel"))
    for model, raw in legacy.items():
        name = normalize_model_name(model)
        if not name or not isinstance(raw, Mapping):
            continue
        if not compression_model and _to_bool(raw.get("compressionModel")) is True:
            compression_model = name
        if name in defaults:
            continue
        target = model_pricing.canonical_official_model(name)
        if target:
            additions[name] = {"target": target, "source": "legacy"}

    compression_changed = bool(compression_model and not normalize_model_name(cfg.get("compressionModel")))

    def mutate(current_cfg: dict) -> None:
        binding_root = current_cfg.setdefault("modelBindings", {})
        if not isinstance(binding_root, dict):
            binding_root = {}
            current_cfg["modelBindings"] = binding_root
        values = binding_root.setdefault("defaults", {})
        if not isinstance(values, dict):
            values = {}
            binding_root["defaults"] = values
        for model, entry in additions.items():
            values.setdefault(model, entry)
        binding_root.setdefault("scoped", {})
        binding_root["legacyMigrationVersion"] = _LEGACY_MIGRATION_VERSION
        if compression_changed:
            current_cfg["compressionModel"] = compression_model

    config.update(mutate)
    return {"bindings": len(additions), "compression": 1 if compression_changed else 0}


def set_compression_model(model: str) -> None:
    name = normalize_model_name(model)
    if not name:
        raise ValueError("compression model is required")
    config.update(lambda cfg: cfg.__setitem__("compressionModel", name))


def clear_compression_model(model: str | None = None) -> bool:
    current = get_compression_model()
    expected = normalize_model_name(model) or None
    if not current or (expected and current != expected):
        return False
    config.update(lambda cfg: cfg.__setitem__("compressionModel", ""))
    return True


def get_compression_model() -> str | None:
    cfg = config.get()
    current = normalize_model_name(cfg.get("compressionModel"))
    if current:
        return current
    # Read-through keeps an old selection available even before startup migration.
    legacy = cfg.get("modelMetadata") or {}
    if isinstance(legacy, Mapping):
        for model, raw in legacy.items():
            if isinstance(raw, Mapping) and _to_bool(raw.get("compressionModel")) is True:
                return normalize_model_name(model) or None
    return None


def context_window(
    model: Any,
    *,
    scope_key: str | None = None,
    outbound_model: str | None = None,
) -> int | None:
    return _to_int(get_metadata(
        model, scope_key=scope_key, outbound_model=outbound_model,
    ).get("contextWindow"))


def max_output_tokens(
    model: Any,
    *,
    scope_key: str | None = None,
    outbound_model: str | None = None,
) -> int | None:
    return _to_int(get_metadata(
        model, scope_key=scope_key, outbound_model=outbound_model,
    ).get("maxOutputTokens"))


def _compact_rescue_int(key: str, default: int) -> int:
    root = config.get().get("compactRescue") or {}
    if not isinstance(root, Mapping):
        return default
    try:
        parsed = int(float(str(root.get(key)).replace(",", "").strip()))
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def summary_reserve_tokens(
    model: Any,
    *,
    scope_key: str | None = None,
    outbound_model: str | None = None,
) -> int:
    reserve = _compact_rescue_int("summaryReserveTokens", SUMMARY_OUTPUT_RESERVE_TOKENS)
    max_output = max_output_tokens(model, scope_key=scope_key, outbound_model=outbound_model)
    return reserve if max_output is None else max(1, min(max_output, reserve))


def compact_buffer_tokens() -> int:
    return _compact_rescue_int("safetyBufferTokens", DEFAULT_COMPACT_BUFFER_TOKENS)


def safe_prompt_limit(
    model: Any,
    *,
    scope_key: str | None = None,
    outbound_model: str | None = None,
    buffer_tokens: int | None = None,
) -> int | None:
    window = context_window(model, scope_key=scope_key, outbound_model=outbound_model)
    if window is None:
        return None
    buffer = compact_buffer_tokens() if buffer_tokens is None else max(0, int(buffer_tokens))
    reserve = summary_reserve_tokens(model, scope_key=scope_key, outbound_model=outbound_model)
    return max(0, window - reserve - buffer)


def required_context_for_compact(
    prompt_tokens: int,
    model: Any,
    *,
    scope_key: str | None = None,
    outbound_model: str | None = None,
    buffer_tokens: int | None = None,
) -> int:
    buffer = compact_buffer_tokens() if buffer_tokens is None else max(0, int(buffer_tokens))
    reserve = summary_reserve_tokens(model, scope_key=scope_key, outbound_model=outbound_model)
    return max(0, int(prompt_tokens)) + reserve + buffer


def can_fit_for_compact(
    model: Any,
    prompt_tokens: int,
    *,
    scope_key: str | None = None,
    outbound_model: str | None = None,
    buffer_tokens: int | None = None,
) -> bool:
    window = context_window(model, scope_key=scope_key, outbound_model=outbound_model)
    if window is None:
        return False
    required = required_context_for_compact(
        prompt_tokens, model, scope_key=scope_key, outbound_model=outbound_model,
        buffer_tokens=buffer_tokens,
    )
    return window >= required
