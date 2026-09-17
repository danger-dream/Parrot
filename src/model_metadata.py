"""models.dev metadata bindings and independent compact-model selection.

Persistent configuration stores only binding identities:

``modelBindings.defaults[client model] -> provider/model``
``modelBindings.scoped[scope key][client model] -> provider/model``

The models.dev catalog record comes from :mod:`model_pricing`, which owns the
single bundled/cache/remote catalog lifecycle.  For OAuth account scopes,
account/LKG catalog metadata is the final fallback after explicit scoped and
default bindings. Cursor protocol mechanics remain an account-native overlay.
"""

from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable, Mapping

from . import config, model_names, model_pricing

SUMMARY_OUTPUT_RESERVE_TOKENS = 20_000
DEFAULT_COMPACT_BUFFER_TOKENS = 20_000
_LEGACY_MIGRATION_VERSION = 1

# Stable public field names for sparse operator overrides. Cost fields are
# stored flattened so key presence (including 0) remains unambiguous.
OVERRIDE_FIELDS = (
    "contextWindow", "maxInputTokens", "maxOutputTokens", "compactTriggerTokens",
    "vision", "toolCall", "structuredOutput", "reasoningEfforts", "serviceTiers",
    "knowledgeCutoff", "cost.input", "cost.output", "cost.cacheRead",
    "cost.cacheWrite", "cost.longContextInput", "cost.longContextOutput",
)
_OVERRIDE_TOKEN_FIELDS = {
    "contextWindow", "maxInputTokens", "maxOutputTokens", "compactTriggerTokens",
}
_OVERRIDE_BOOL_FIELDS = {"vision", "toolCall", "structuredOutput"}
_OVERRIDE_LIST_FIELDS = {"reasoningEfforts", "serviceTiers"}
_OVERRIDE_PRICE_FIELDS = {name for name in OVERRIDE_FIELDS if name.startswith("cost.")}
_COST_PATHS = {
    "cost.input": ("cost", "input"),
    "cost.output": ("cost", "output"),
    "cost.cacheRead": ("cost", "cache_read"),
    "cost.cacheWrite": ("cost", "cache_write"),
    "cost.longContextInput": ("cost", "context_over_200k", "input"),
    "cost.longContextOutput": ("cost", "context_over_200k", "output"),
}


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
    authority: str = "models.dev"
    value_source: Mapping[str, str] = field(default_factory=dict)
    constrained_by: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    common_override: Mapping[str, Any] = field(default_factory=dict)
    source_override: Mapping[str, Any] = field(default_factory=dict)
    auto_snapshot: Mapping[str, Any] | None = None

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
_INT_KEYS = {"contextWindow", "contextWindowMaxMode", "maxOutputTokens", "compactTriggerTokens"}
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
    "context_length": "contextWindow",
    "context_window_max_mode": "contextWindowMaxMode",
    "max_context_window": "contextWindowMaxMode",
    "max_context_window_tokens": "contextWindowMaxMode",
    "maxOutput": "maxOutputTokens",
    "max_output": "maxOutputTokens", "max_output_tokens": "maxOutputTokens",
    "maxTokens": "maxOutputTokens", "max_tokens": "maxOutputTokens",
    "compact_trigger_tokens": "compactTriggerTokens",
    "compactThreshold": "compactTriggerTokens",
    "compression_threshold": "compactTriggerTokens",
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


def normalize_override_fields(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate and flatten the public 16-field sparse override contract."""
    if not isinstance(raw, Mapping):
        raise ValueError("set must be a JSON object")
    expanded: dict[str, Any] = {}
    for key, value in raw.items():
        name = str(key or "").strip()
        if name == "cost":
            if not isinstance(value, Mapping):
                raise ValueError("cost must be a JSON object")
            for child, child_value in value.items():
                expanded[f"cost.{str(child or '').strip()}"] = child_value
        else:
            expanded[name] = value
    result: dict[str, Any] = {}
    for name, value in expanded.items():
        if name not in OVERRIDE_FIELDS:
            raise ValueError(f"unsupported metadata override field: {name}")
        if name in _OVERRIDE_TOKEN_FIELDS:
            if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= 2_147_483_647:
                raise ValueError(f"{name} must be a positive integer <= 2147483647")
            parsed: Any = value
        elif name in _OVERRIDE_PRICE_FIELDS:
            if (
                isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value < 0
            ):
                raise ValueError(f"{name} must be a finite non-negative number")
            parsed = float(value)
        elif name in _OVERRIDE_BOOL_FIELDS:
            if not isinstance(value, bool):
                raise ValueError(f"{name} must be a boolean")
            parsed = value
        elif name in _OVERRIDE_LIST_FIELDS:
            if not isinstance(value, list) or len(value) > 20:
                raise ValueError(f"{name} must be a list with at most 20 items")
            parsed_items: list[str] = []
            for item in value:
                if not isinstance(item, str) or not item.strip() or len(item.strip()) > 80:
                    raise ValueError(f"{name} items must be non-empty strings <= 80 characters")
                normalized = item.strip()
                if normalized not in parsed_items:
                    parsed_items.append(normalized)
            parsed = parsed_items
        else:
            if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}(?:-\d{2})?", value):
                raise ValueError("knowledgeCutoff must use YYYY-MM or YYYY-MM-DD")
            try:
                parts = [int(item) for item in value.split("-")]
                if len(parts) == 2:
                    date(parts[0], parts[1], 1)
                else:
                    date(*parts)
            except ValueError as exc:
                raise ValueError("knowledgeCutoff is not a valid calendar date") from exc
            parsed = value
        result[name] = parsed
    return result


def normalize_unset_fields(raw: Iterable[Any]) -> tuple[str, ...]:
    result: list[str] = []
    for value in raw:
        name = str(value or "").strip()
        if name not in OVERRIDE_FIELDS:
            raise ValueError(f"unsupported metadata override field: {name}")
        if name not in result:
            result.append(name)
    return tuple(result)


def _override_roots(
    cfg: Mapping[str, Any] | None = None,
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    source = config.get() if cfg is None else cfg
    root = source.get("modelMetadataOverrides") or {}
    if not isinstance(root, Mapping):
        return {}, {}
    defaults = root.get("defaults") or {}
    scoped = root.get("scoped") or {}
    return (
        defaults if isinstance(defaults, Mapping) else {},
        scoped if isinstance(scoped, Mapping) else {},
    )


def _override_entry_fields(
    raw: Any, *, known_outbound_model: str | None = None,
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        return {}
    saved_outbound = normalize_model_name(raw.get("outboundModel")) or None
    known = normalize_model_name(known_outbound_model) or None
    if saved_outbound and known and saved_outbound != known:
        return {}
    fields_raw = raw.get("fields") if isinstance(raw.get("fields"), Mapping) else raw
    try:
        return normalize_override_fields(fields_raw)
    except ValueError:
        # Externally edited invalid config fails closed field-by-field rather than
        # making the whole service unavailable.
        result: dict[str, Any] = {}
        for key, value in fields_raw.items():
            try:
                result.update(normalize_override_fields({str(key): value}))
            except ValueError:
                continue
        return result


def get_override_fields(
    model: Any, *, scope_key: str | None = None, outbound_model: str | None = None,
    cfg: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    name = normalize_model_name(model)
    scope = normalize_model_name(scope_key) or None
    defaults, scoped = _override_roots(cfg)
    common = _override_entry_fields(defaults.get(name))
    source: dict[str, Any] = {}
    if scope:
        values = scoped.get(scope) or {}
        if isinstance(values, Mapping):
            source = _override_entry_fields(
                values.get(name), known_outbound_model=outbound_model,
            )
    return common, source


def _metadata_path(name: str) -> tuple[str, ...]:
    return _COST_PATHS.get(name, (name,))


def _metadata_get(metadata: Mapping[str, Any], name: str) -> tuple[bool, Any]:
    current: Any = metadata
    for part in _metadata_path(name):
        if not isinstance(current, Mapping) or part not in current:
            return False, None
        current = current[part]
    return True, current


def _metadata_set(metadata: dict[str, Any], name: str, value: Any) -> None:
    path = _metadata_path(name)
    current = metadata
    for part in path[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    current[path[-1]] = copy.deepcopy(value)


def _metadata_value_sources(metadata: Mapping[str, Any], source: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for name in OVERRIDE_FIELDS:
        present, _ = _metadata_get(metadata, name)
        if present:
            result[name] = source
    # Preserve provenance for additional compatibility/native fields returned by
    # the resolver even though they are not operator-editable.
    for name in metadata:
        if name != "cost":
            result.setdefault(str(name), source)
    return result


def _apply_fields(
    metadata: dict[str, Any], provenance: dict[str, str],
    values: Mapping[str, Any], source: str,
) -> None:
    for name, value in values.items():
        _metadata_set(metadata, name, value)
        provenance[name] = source


def _constrain_to_native(
    metadata: dict[str, Any], provenance: dict[str, str],
    native_metadata: Mapping[str, Any], *, cursor: bool,
) -> dict[str, tuple[str, ...]]:
    constrained: dict[str, tuple[str, ...]] = {}
    for name, native_value in native_metadata.items():
        if name not in metadata:
            metadata[name] = copy.deepcopy(native_value)
            provenance[name] = "account-native"

    numeric_pairs = (
        ("contextWindow", "contextWindow"),
        ("maxInputTokens", "maxInputTokens"),
        ("maxOutputTokens", "maxOutputTokens"),
    )
    for target, native_name in numeric_pairs:
        native_value = _to_int(native_metadata.get(native_name))
        current_value = _to_int(metadata.get(target))
        if native_value is not None and current_value is not None and current_value > native_value:
            metadata[target] = native_value
            provenance[target] = "account-native"
            constrained[target] = ("native-hard",)
    for name in ("vision", "toolCall", "structuredOutput"):
        if native_metadata.get(name) is False and metadata.get(name) is not False:
            metadata[name] = False
            provenance[name] = "account-native"
            constrained[name] = ("native-hard",)
    for name in ("reasoningEfforts", "serviceTiers", "inputModalities", "outputModalities"):
        native_values = native_metadata.get(name)
        current_values = metadata.get(name)
        if isinstance(native_values, list) and isinstance(current_values, list):
            allowed = {str(item) for item in native_values}
            narrowed = [item for item in current_values if str(item) in allowed]
            if narrowed != current_values:
                metadata[name] = narrowed
                provenance[name] = "derived"
                constrained[name] = ("native-hard",)
    return constrained


def _derive_consistent_limits(
    metadata: dict[str, Any], provenance: dict[str, str],
    constrained: dict[str, tuple[str, ...]],
) -> None:
    context = _to_int(metadata.get("contextWindow"))
    max_input = _to_int(metadata.get("maxInputTokens"))
    if context is not None and max_input is not None and max_input > context:
        metadata["maxInputTokens"] = context
        provenance["maxInputTokens"] = "derived"
        constrained["maxInputTokens"] = (*constrained.get("maxInputTokens", ()), "contextWindow")
        max_input = context
    input_budget = max_input
    if input_budget is None and context is not None:
        # Context and output maxima are independent per-request ceilings for
        # models.dev records; do not subtract one declared maximum from the other.
        input_budget = context
    trigger = _to_int(metadata.get("compactTriggerTokens"))
    if input_budget is not None and trigger is not None and trigger > input_budget:
        metadata["compactTriggerTokens"] = input_budget
        provenance["compactTriggerTokens"] = "derived"
        constrained["compactTriggerTokens"] = (
            *constrained.get("compactTriggerTokens", ()), "effectiveInputBudget",
        )
    if "vision" in metadata:
        vision = bool(metadata["vision"])
        metadata["supportsImages"] = vision
        provenance["supportsImages"] = "derived"
        if not vision and isinstance(metadata.get("inputModalities"), list):
            metadata["inputModalities"] = [
                item for item in metadata["inputModalities"]
                if str(item).lower() != "image"
            ]
            provenance["inputModalities"] = "derived"


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
    if (
        scope_key and not scope_key.startswith("oauth:cursor:")
        and saved_outbound and known and saved_outbound != known
    ):
        return None
    snapshot = raw.get("autoSnapshot") if isinstance(raw, Mapping) else None
    snapshot = copy.deepcopy(dict(snapshot)) if isinstance(snapshot, Mapping) else None
    snapshot_metadata = snapshot.get("metadata") if snapshot is not None else None
    if isinstance(snapshot_metadata, Mapping):
        metadata = copy.deepcopy(dict(snapshot_metadata))
    else:
        snapshot = None
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
        auto_snapshot=snapshot,
    )


def _oauth_account_record(
    model: str, *, scope_key: str | None, outbound_model: str | None,
) -> tuple[Mapping[str, Any], str] | None:
    """Return a record only from the account's current real/LKG directory.

    An ID must exist in both the account's selected ``models`` list and its
    provider-native catalog. This prevents either a legacy list or a stale
    catalog from independently decorating a model after discovery has failed.
    """
    scope = normalize_model_name(scope_key)
    if not scope.startswith("oauth:"):
        return None
    wanted_key = scope[len("oauth:"):]
    try:
        from .oauth_ids import account_key as make_account_key

        account = next((item for item in config.get().get("oauthAccounts", [])
                        if make_account_key(item) == wanted_key), None)
    except Exception:
        return None
    models = {normalize_model_name(item) for item in (account or {}).get("models") or []}
    if not account or not models:
        return None
    candidate = model if model in models else normalize_model_name(outbound_model)
    if not candidate or candidate not in models:
        return None
    provider = str(account.get("provider") or account.get("type") or "").strip().lower()
    if provider == "cursor":
        try:
            from .cursor_bridge.catalog import find_record
            record = find_record(account, candidate)
        except Exception:
            record = None
    else:
        records = ((account.get("account_model_catalog") or {}).get("models") or [])
        record = next((item for item in records if isinstance(item, Mapping)
                       and normalize_model_name(item.get("id")) == candidate), None)
    return (record, provider) if isinstance(record, Mapping) else None


def service_tier_ids(values: Any) -> list[str]:
    """Normalize authenticated native tier objects without inventing tiers."""
    if not isinstance(values, list):
        return []
    result: list[str] = []
    for item in values:
        value = item.get("id") if isinstance(item, Mapping) else item
        if isinstance(value, str) and value.strip() and value.strip() not in result:
            result.append(value.strip())
    return result


def _upstream_metadata(record: Mapping[str, Any], provider: str) -> dict[str, Any]:
    """Map normalized OAuth records to effective generic metadata explicitly."""
    if provider == "cursor":
        from .cursor_bridge.catalog import metadata_from_record
        return metadata_from_record(record)
    result = dict(record)
    result.update(normalize_metadata(dict(record)))
    if isinstance(record.get("serviceTiers"), list):
        result["serviceTiers"] = service_tier_ids(record["serviceTiers"])
    modalities = [str(item) for item in record.get("inputModalities") or [] if str(item)]
    if "vision" not in result:
        if "supportsImages" in record:
            parsed = _to_bool(record.get("supportsImages"))
            if parsed is not None:
                result["vision"] = parsed
        elif modalities:
            result["vision"] = "image" in {item.lower() for item in modalities}
    return result


def _merge_effective_metadata(
    binding_metadata: Mapping[str, Any], native: tuple[Mapping[str, Any], str] | None,
) -> dict[str, Any]:
    """Merge catalog pricing/descriptions with account-native capabilities.

    A models.dev binding still owns pricing and descriptive fields. Cursor's
    account catalog, however, is authoritative for every transport capability:
    normal/Max Context limits, output limit, effort variants, and the effective
    image/tool intersection implemented by the bridge. Otherwise an old default
    binding can hide a newly discovered Cursor capability or bypass a disabled
    Max Context tier during preflight.
    """
    upstream = _upstream_metadata(*native) if native else {}
    if native and native[1] == "cursor":
        result = dict(binding_metadata)
        cursor_transport_keys = (
            "name", "family", "vision", "supportsImages", "cursorUpstreamVision", "reasoning",
            "reasoningEfforts", "defaultReasoningEffort", "toolCall",
            "structuredOutput", "temperature", "modalities", "contextWindow",
            "contextWindowMaxMode", "maxOutputTokens", "compactTriggerTokens",
            "metadataSource", "supportsFast", "supportsThinking", "variants",
            "aliases",
        )
        for key in cursor_transport_keys:
            if key in upstream:
                result[key] = upstream[key]
        # Keep a useful models.dev description when Cursor supplies no tagline;
        # otherwise prefer the account-specific upstream wording.
        if upstream.get("description"):
            result["description"] = upstream["description"]
        for key, value in upstream.items():
            result.setdefault(key, value)
    else:
        result = dict(upstream)
        result.update(dict(binding_metadata))

    # Canonical vision is the effective capability. Do not leave contradictory
    # aliases/modalities that could turn an explicit bridge-level false back on.
    if "vision" in result:
        vision = bool(result["vision"])
        result["supportsImages"] = vision
        if not vision and isinstance(result.get("inputModalities"), list):
            result["inputModalities"] = [
                item for item in result["inputModalities"]
                if str(item).lower() != "image"
            ]
    return result


def _effective_binding(
    binding: MetadataBinding | None,
    *,
    model: str,
    scope_key: str | None,
    outbound_model: str | None,
    native: tuple[Mapping[str, Any], str] | None,
) -> MetadataBinding | None:
    common_override, source_override = get_override_fields(
        model, scope_key=scope_key, outbound_model=outbound_model,
    )
    if binding is None and native is None and not common_override and not source_override:
        return None
    if binding is None:
        binding = MetadataBinding(
            client_visible_model=model,
            target=f"unbound/{model}",
            provider_id="unbound",
            catalog_model_id=model,
            scope_key=scope_key,
            outbound_model=normalize_model_name(outbound_model) or None,
            source="override",
            metadata={},
            authority="operator",
        )
    metadata = copy.deepcopy(dict(binding.metadata))
    base_source = (
        "account-native" if binding.authority == "account-upstream"
        else "catalog-snapshot" if binding.auto_snapshot is not None
        else "catalog"
    )
    provenance = _metadata_value_sources(metadata, base_source)
    upstream = _upstream_metadata(*native) if native else {}
    # Cursor native transport facts replace stale catalog facts BEFORE the
    # operator layers. Native hard ceilings must not undo valid tightening.
    cursor_fields = {
        "contextWindow", "contextWindowMaxMode", "maxInputTokens",
        "maxOutputTokens", "vision", "toolCall", "structuredOutput",
        "reasoningEfforts", "defaultReasoningEffort", "inputModalities",
        "outputModalities", "serviceTiers", "supportsFast", "supportsThinking",
        "variants", "compactTriggerTokens",
    } if native and native[1] == "cursor" else set()
    for name, value in upstream.items():
        if name not in metadata or name in cursor_fields:
            metadata[name] = copy.deepcopy(value)
            provenance[name] = "account-native"
    _apply_fields(metadata, provenance, common_override, "common-override")
    _apply_fields(metadata, provenance, source_override, "source-override")
    constrained = _constrain_to_native(
        metadata, provenance, upstream, cursor=bool(native and native[1] == "cursor"),
    )
    operator_fields = {**common_override, **source_override}
    context_cap = operator_fields.get("contextWindow")
    maximum = _to_int(metadata.get("contextWindowMaxMode"))
    if context_cap is not None and maximum is not None and maximum > context_cap:
        metadata["contextWindowMaxMode"] = context_cap
        provenance["contextWindowMaxMode"] = "derived"
        constrained["contextWindowMaxMode"] = ("contextWindow",)
    _derive_consistent_limits(metadata, provenance, constrained)
    return MetadataBinding(**{
        **binding.__dict__,
        "client_visible_model": model,
        "metadata": metadata,
        "value_source": provenance,
        "constrained_by": constrained,
        "common_override": common_override,
        "source_override": source_override,
    })


def _account_native_binding(
    model: str, *, scope_key: str | None, outbound_model: str | None,
) -> MetadataBinding | None:
    native = _oauth_account_record(model, scope_key=scope_key, outbound_model=outbound_model)
    if native is None:
        return None
    record, provider = native
    metadata = _upstream_metadata(record, provider)
    if not metadata:
        return None
    return MetadataBinding(
        client_visible_model=model,
        target=f"{provider or 'upstream'}/{model}",
        provider_id=provider or "upstream",
        catalog_model_id=model,
        scope_key=normalize_model_name(scope_key) or None,
        outbound_model=normalize_model_name(outbound_model) or model,
        source="cursor.AvailableModels" if provider == "cursor" else "account_model_catalog",
        metadata=metadata,
        authority="account-upstream",
    )


def resolve_binding(
    client_visible_model: Any,
    *,
    scope_key: str | None = None,
    outbound_model: str | None = None,
) -> MetadataBinding | None:
    """Resolve matching, sparse inheritance and provider-native hard limits."""
    model = normalize_model_name(client_visible_model)
    scope = normalize_model_name(scope_key) or None
    if not model:
        return None
    legacy_model = model_names.upstream_for_channel(scope, model)
    lookup_models = [model] if legacy_model == model else [model, legacy_model]
    if legacy_model != model and not outbound_model:
        outbound_model = legacy_model
    native = _oauth_account_record(model, scope_key=scope, outbound_model=outbound_model)
    if scope and scope.startswith("oauth:cursor:") and native is None:
        return None
    cfg = config.get()
    defaults, scoped = _binding_roots(cfg)
    binding: MetadataBinding | None = None
    if scope:
        scope_bindings = scoped.get(scope) or {}
        for lookup in lookup_models:
            if isinstance(scope_bindings, Mapping) and lookup in scope_bindings:
                binding = _record_for(
                    model, scope_bindings.get(lookup), scope_key=scope,
                    known_outbound_model=outbound_model,
                )
                if binding is not None:
                    break
    if binding is None:
        for lookup in lookup_models:
            if lookup in defaults:
                binding = _record_for(
                    model, defaults.get(lookup), scope_key=None,
                    known_outbound_model=None,
                )
                if binding is not None:
                    break
    if binding is None:
        legacy_target = _legacy_default_target(model, cfg)
        if legacy_target:
            binding = _record_for(
                model, {"target": legacy_target, "source": "legacy"},
                scope_key=None, known_outbound_model=None,
            )
    if binding is None:
        binding = _account_native_binding(
            legacy_model, scope_key=scope, outbound_model=outbound_model,
        )
        if binding is not None and legacy_model != model:
            binding = MetadataBinding(**{**binding.__dict__, "client_visible_model": model})
    return _effective_binding(
        binding, model=model, scope_key=scope,
        outbound_model=outbound_model, native=native,
    )


def set_binding(
    client_visible_model: str,
    target: str,
    *,
    scope_key: str | None = None,
    outbound_model: str | None = None,
    source: str = "manual",
    auto_snapshot: Mapping[str, Any] | None = None,
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
    if auto_snapshot is not None:
        snapshot = copy.deepcopy(dict(auto_snapshot))
        if not isinstance(snapshot.get("metadata"), Mapping):
            raise ValueError("auto snapshot metadata is required")
        if not str(snapshot.get("catalogRevision") or "").strip():
            raise ValueError("auto snapshot catalog revision is required")
        entry["autoSnapshot"] = snapshot

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


def _validate_override_constraints(
    model: str,
    values: Mapping[str, Any],
    *,
    scope_key: str | None,
    outbound_model: str | None,
) -> None:
    binding = resolve_binding(model, scope_key=scope_key, outbound_model=outbound_model)
    effective = copy.deepcopy(dict(binding.metadata)) if binding is not None else {}
    _apply_fields(effective, {}, values, "candidate")
    context = _to_int(effective.get("contextWindow"))
    max_input = _to_int(effective.get("maxInputTokens"))
    trigger = _to_int(effective.get("compactTriggerTokens"))
    if (
        context is not None and max_input is not None and max_input > context
        and "maxInputTokens" in values
    ):
        raise ValueError("maxInputTokens must not exceed contextWindow")
    input_budget = (
        min(max_input, context)
        if max_input is not None and context is not None else max_input
    )
    if input_budget is None and context is not None:
        # Context and output maxima are independent per-request ceilings for
        # models.dev records; do not subtract one declared maximum from the other.
        input_budget = context
    if (
        input_budget is not None and trigger is not None and trigger > input_budget
        and "compactTriggerTokens" in values
    ):
        raise ValueError("compactTriggerTokens must not exceed effectiveInputBudget")
    native = _oauth_account_record(
        model, scope_key=scope_key, outbound_model=outbound_model,
    )
    native_metadata = _upstream_metadata(*native) if native else {}
    for name in ("contextWindow", "maxInputTokens", "maxOutputTokens"):
        if name not in values:
            continue
        ceiling = _to_int(native_metadata.get(name))
        if name == "contextWindow" and native and native[1] == "cursor":
            ceiling = _to_int(native_metadata.get("contextWindowMaxMode")) or ceiling
        if ceiling is not None and int(values[name]) > ceiling:
            raise ValueError(f"{name} cannot exceed provider native ceiling {ceiling}")
    for name in ("vision", "toolCall", "structuredOutput"):
        if values.get(name) is True and native_metadata.get(name) is False:
            raise ValueError(f"{name} cannot enable a provider-disabled capability")
    for name in ("reasoningEfforts", "serviceTiers"):
        if name not in values or not isinstance(native_metadata.get(name), list):
            continue
        allowed = {str(item) for item in native_metadata[name]}
        if any(str(item) not in allowed for item in values[name]):
            raise ValueError(f"{name} must be a subset of provider native values")


def patch_override_fields(
    model: str,
    *,
    scope_key: str | None,
    outbound_model: str | None,
    set_fields: Mapping[str, Any],
    unset_fields: Iterable[str] = (),
) -> bool:
    name = normalize_model_name(model)
    scope = normalize_model_name(scope_key) or None
    outbound = normalize_model_name(outbound_model) or None
    if not name:
        raise ValueError("model is required")
    normalized_set = normalize_override_fields(set_fields)
    normalized_unset = normalize_unset_fields(unset_fields)
    overlap = set(normalized_set).intersection(normalized_unset)
    if overlap:
        raise ValueError(f"fields cannot be both set and unset: {', '.join(sorted(overlap))}")
    if not normalized_set and not normalized_unset:
        raise ValueError("set or unset must contain at least one field")
    if scope and not outbound:
        raise ValueError("scoped override requires outbound model")
    _validate_override_constraints(
        name, normalized_set, scope_key=scope, outbound_model=outbound,
    )
    changed = [False]

    def mutate(cfg: dict) -> None:
        root = cfg.setdefault("modelMetadataOverrides", {})
        if not isinstance(root, dict):
            root = {}
            cfg["modelMetadataOverrides"] = root
        if scope:
            scopes = root.setdefault("scoped", {})
            if not isinstance(scopes, dict):
                scopes = {}
                root["scoped"] = scopes
            values = scopes.setdefault(scope, {})
            if not isinstance(values, dict):
                values = {}
                scopes[scope] = values
        else:
            values = root.setdefault("defaults", {})
            if not isinstance(values, dict):
                values = {}
                root["defaults"] = values
        raw = values.get(name)
        existing = _override_entry_fields(raw, known_outbound_model=outbound)
        updated = dict(existing)
        updated.update(copy.deepcopy(normalized_set))
        for field_name in normalized_unset:
            updated.pop(field_name, None)
        if updated == existing and (
            not scope or not isinstance(raw, Mapping)
            or normalize_model_name(raw.get("outboundModel")) == outbound
        ):
            return
        changed[0] = True
        if updated:
            entry: dict[str, Any] = {"fields": updated}
            if scope:
                entry["outboundModel"] = outbound
            values[name] = entry
        else:
            values.pop(name, None)
            if scope and not values:
                scopes.pop(scope, None)

    config.update(mutate, skip_if_unchanged=True)
    return changed[0]


def delete_override_layer(model: str, *, scope_key: str | None = None) -> bool:
    name = normalize_model_name(model)
    scope = normalize_model_name(scope_key) or None
    removed = [False]

    def mutate(cfg: dict) -> None:
        defaults, scoped = _override_roots(cfg)
        values = scoped.get(scope) if scope and isinstance(scoped, dict) else defaults
        if isinstance(values, dict) and name in values:
            values.pop(name, None)
            removed[0] = True
        if scope and isinstance(scoped, dict) and not values:
            scoped.pop(scope, None)

    config.update(mutate, skip_if_unchanged=True)
    return removed[0]


def clear_scoped_metadata_in_config(
    cfg: dict, scope_key: str, model_ids: Iterable[str],
) -> None:
    """Remove matching and override state for exact source+public-model pairs."""
    scope = normalize_model_name(scope_key)
    names = {normalize_model_name(item) for item in model_ids if normalize_model_name(item)}
    if not scope or not names:
        return
    for root_name in ("modelBindings", "modelMetadataOverrides"):
        root = cfg.get(root_name)
        scopes = root.get("scoped") if isinstance(root, dict) else None
        values = scopes.get(scope) if isinstance(scopes, dict) else None
        if not isinstance(values, dict):
            continue
        for name in names:
            values.pop(name, None)
        if not values:
            scopes.pop(scope, None)


def clear_metadata_scope_in_config(cfg: dict, scope_key: str) -> None:
    scope = normalize_model_name(scope_key)
    if not scope:
        return
    for root_name in ("modelBindings", "modelMetadataOverrides"):
        root = cfg.get(root_name)
        scopes = root.get("scoped") if isinstance(root, dict) else None
        if isinstance(scopes, dict):
            scopes.pop(scope, None)


def rename_scoped_metadata_in_config(cfg: dict, old_scope: str, new_scope: str) -> None:
    old = normalize_model_name(old_scope)
    new = normalize_model_name(new_scope)
    if not old or not new or old == new:
        return
    for root_name in ("modelBindings", "modelMetadataOverrides"):
        root = cfg.get(root_name)
        scopes = root.get("scoped") if isinstance(root, dict) else None
        if not isinstance(scopes, dict) or old not in scopes:
            continue
        old_values = scopes.pop(old)
        if not isinstance(old_values, dict):
            continue
        existing = scopes.get(new)
        if isinstance(existing, dict):
            for key, value in old_values.items():
                existing.setdefault(key, value)
        else:
            scopes[new] = old_values


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

    # Cursor account metadata is generated from each account's live model
    # catalog and is read-only in the binding UI. Include it in the scoped view
    # without persisting duplicate modelBindings entries.
    existing = {(item.scope_key, item.client_visible_model) for item in result}
    try:
        cursor_items = [
            item for item in inventory_items()
            if item.scope_key.startswith("oauth:cursor:")
        ]
    except Exception:
        cursor_items = []
    for item in cursor_items:
        key = (item.scope_key, item.client_visible_model)
        if key in existing:
            continue
        binding = _account_native_binding(
            item.client_visible_model,
            scope_key=item.scope_key,
            outbound_model=item.outbound_model,
        )
        if binding is not None:
            result.append(binding)
            existing.add(key)
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
    """Enumerate complete management membership, including disabled OAuth IDs."""
    from .channel import registry
    from . import oauth_manager
    from .oauth_ids import account_key

    accounts = {
        f"oauth:{account_key(account)}": account
        for account in config.get().get("oauthAccounts") or ()
        if isinstance(account, dict)
    }
    result: list[ModelInventoryItem] = []
    for channel in registry.all_channels():
        scope_key = normalize_model_name(getattr(channel, "key", ""))
        if not scope_key:
            continue
        scope_type = normalize_model_name(getattr(channel, "type", "")) or "unknown"
        scope_label = normalize_model_name(getattr(channel, "display_name", "")) or scope_key
        try:
            account = accounts.get(scope_key) if scope_type == "oauth" else None
            if account is not None:
                selection = oauth_manager.account_model_selection(account)
                provider = oauth_manager.provider_of(account)
                visible_models = [
                    model_names.public_id(provider, model)
                    for model in selection["models"]
                ]
            else:
                visible_models = channel.list_client_models()
        except Exception:
            continue
        for visible in visible_models:
            model = normalize_model_name(visible)
            if not model:
                continue
            try:
                outbound = normalize_model_name(channel.supports_model(model))
            except Exception:
                outbound = ""
            if not outbound:
                outbound = model_names.upstream_for_channel(scope_key, model)
            result.append(ModelInventoryItem(
                scope_key=scope_key,
                scope_type=scope_type,
                scope_label=scope_label,
                client_visible_model=model,
                outbound_model=outbound,
            ))
    # Image source detail uses the same metadata editor/sync entry points as
    # chat detail, but image-only OAuth membership is not in chat model lists.
    from . import image_catalog
    represented = {(item.scope_key, item.client_visible_model) for item in result}
    for source in image_catalog.sources():
        if (source.key, source.model) not in represented:
            result.append(ModelInventoryItem(
                scope_key=source.key, scope_type='oauth' if source.key.startswith('oauth:') else 'api',
                scope_label=source.label, client_visible_model=source.model, outbound_model=source.upstream,
            ))
            represented.add((source.key, source.model))
    return sorted(result, key=lambda item: (item.scope_key, item.client_visible_model))


class MetadataSyncConflict(RuntimeError):
    """The configuration changed while an automatic snapshot was computed."""


def sync_auto_snapshots(
    targets: Iterable[tuple[str, str | None, str | None]],
    *,
    candidate: Any | None = None,
) -> dict[str, Any]:
    """Atomically write exact auto bindings/snapshots for an explicit target set."""
    normalized: list[tuple[str, str | None, str | None]] = []
    seen: set[tuple[str, str | None]] = set()
    for model, scope_key, outbound_model in targets:
        name = normalize_model_name(model)
        scope = normalize_model_name(scope_key) or None
        outbound = normalize_model_name(outbound_model) or None
        key = (name, scope)
        if name and key not in seen:
            normalized.append((name, scope, outbound))
            seen.add(key)
    # Compute without holding the config lifecycle lock; the commit below CASes
    # the exact input snapshot so a concurrent manual write can never be lost.
    cfg = copy.deepcopy(config.get())
    defaults, scoped = _binding_roots(cfg)
    changes: list[tuple[str, str | None, dict[str, Any]]] = []
    results: list[dict[str, Any]] = []
    for model, scope, outbound in normalized:
        values = (
            scoped.get(scope) if scope and isinstance(scoped, Mapping) else defaults
        )
        values = values if isinstance(values, Mapping) else {}
        current_raw = values.get(model)
        current = _binding_fields(current_raw)
        current_source = current[1] if current else ""
        source_ref = scope
        if current_source in {"manual", "management-api"}:
            results.append({
                "modelId": model, "source": source_ref, "status": "protected",
                "catalogSource": None, "catalogRevision": None,
            })
            continue
        match_name = outbound or model
        target = (
            model_pricing.candidate_canonical_official_model(candidate, match_name)
            if candidate is not None
            else model_pricing.canonical_official_model(match_name)
        )
        if target is None and match_name != model:
            target = (
                model_pricing.candidate_canonical_official_model(candidate, model)
                if candidate is not None else model_pricing.canonical_official_model(model)
            )
        snapshot = (
            model_pricing.candidate_binding_snapshot(candidate, target)
            if candidate is not None and target is not None
            else model_pricing.binding_snapshot(target) if target is not None else None
        )
        if target is None or snapshot is None:
            results.append({
                "modelId": model, "source": source_ref, "status": "unmatched",
                "catalogSource": "candidate" if candidate is not None else "active-lkg",
                "catalogRevision": (
                    candidate.revision if candidate is not None
                    else model_pricing.catalog_status().get("revision")
                ),
            })
            continue
        entry: dict[str, Any] = {
            "target": target, "source": "auto", "autoSnapshot": snapshot,
        }
        if scope:
            if not outbound:
                results.append({
                    "modelId": model, "source": source_ref, "status": "missing",
                    "catalogSource": snapshot.get("catalogSource"),
                    "catalogRevision": snapshot.get("catalogRevision"),
                })
                continue
            entry["outboundModel"] = outbound
        status = "unchanged" if current_raw == entry else (
            "updated" if model in values else "created"
        )
        if status != "unchanged":
            changes.append((model, scope, entry))
        results.append({
            "modelId": model, "source": source_ref, "status": status,
            "catalogSource": snapshot.get("catalogSource"),
            "catalogRevision": snapshot.get("catalogRevision"),
        })

    if changes:
        def mutate(current_cfg: dict) -> None:
            if current_cfg != cfg:
                raise MetadataSyncConflict("metadata sync configuration changed before commit")
            root = current_cfg.setdefault("modelBindings", {})
            if not isinstance(root, dict):
                root = {}
                current_cfg["modelBindings"] = root
            for model, scope, entry in changes:
                if scope:
                    scopes = root.setdefault("scoped", {})
                    if not isinstance(scopes, dict):
                        scopes = {}
                        root["scoped"] = scopes
                    values = scopes.setdefault(scope, {})
                    if not isinstance(values, dict):
                        values = {}
                        scopes[scope] = values
                else:
                    values = root.setdefault("defaults", {})
                    if not isinstance(values, dict):
                        values = {}
                        root["defaults"] = values
                values[model] = copy.deepcopy(entry)
        config.update(mutate)
    statuses = {
        name: [] for name in (
            "created", "updated", "unchanged", "protected",
            "unmatched", "missing", "failed",
        )
    }
    for item in results:
        statuses[item["status"]].append(item["modelId"])
    return {
        "scanned": len(normalized),
        **statuses,
        "success": len(statuses["created"]) + len(statuses["updated"]),
        "results": results,
    }


def reconcile_auto_snapshots() -> dict[str, Any]:
    """Refresh every existing auto snapshot from the active published catalog."""
    cfg = config.get()
    defaults, scoped = _binding_roots(cfg)
    targets: list[tuple[str, str | None, str | None]] = []
    for model, raw in defaults.items():
        fields = _binding_fields(raw)
        if fields and fields[1] == "auto":
            targets.append((str(model), None, None))
    for scope, values in scoped.items():
        if not isinstance(values, Mapping):
            continue
        for model, raw in values.items():
            fields = _binding_fields(raw)
            if fields and fields[1] == "auto":
                targets.append((str(model), str(scope), fields[2]))
    return sync_auto_snapshots(targets)


def auto_sync_metadata(
    items: Iterable[ModelInventoryItem] | None = None,
    *,
    include_results: bool = False,
) -> dict[str, Any]:
    """Full/global reconcile; manual matching and sparse overrides are protected."""
    inventory = list(items if items is not None else inventory_items())
    scopes_by_model: dict[str, set[str]] = {}
    for item in inventory:
        if item.client_visible_model:
            scopes_by_model.setdefault(item.client_visible_model, set()).add(item.scope_key)
    requested_targets = [
        (model, None, None)
        for model in sorted(scopes_by_model)
        if not all(scope.startswith("oauth:cursor:") for scope in scopes_by_model[model])
    ]
    targets = list(requested_targets)
    # Reconcile every pre-existing auto binding in the same config commit as the
    # newly discovered global targets. Manual bindings remain protected.
    cfg = config.get()
    defaults, scoped = _binding_roots(cfg)
    for model, raw in defaults.items():
        fields = _binding_fields(raw)
        if fields and fields[1] == "auto":
            targets.append((str(model), None, None))
    for scope, values in scoped.items():
        if not isinstance(values, Mapping):
            continue
        for model, raw in values.items():
            fields = _binding_fields(raw)
            if fields and fields[1] == "auto":
                targets.append((str(model), str(scope), fields[2]))
    result = sync_auto_snapshots(targets)
    if include_results:
        return result
    # Preserve the historical direct-call result shape. Full management
    # operations opt into detailed per-target statuses, while existing callers
    # continue to see only the newly discovered global targets.
    requested_models = {model for model, _, _ in requested_targets}
    requested_results = [
        item for item in result["results"]
        if item["source"] is None and item["modelId"] in requested_models
    ]
    legacy_statuses = {name: [] for name in ("created", "updated", "unchanged", "unmatched")}
    for item in requested_results:
        if item["status"] in legacy_statuses:
            legacy_statuses[item["status"]].append(item["modelId"])
    return {
        "scanned": len(requested_targets),
        **legacy_statuses,
        "success": len(legacy_statuses["created"]) + len(legacy_statuses["updated"]),
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
    use_max_context: bool = False,
) -> int | None:
    metadata = get_metadata(
        model, scope_key=scope_key, outbound_model=outbound_model,
    )
    normal = _to_int(metadata.get("contextWindow"))
    if use_max_context:
        maximum = _to_int(metadata.get("contextWindowMaxMode"))
        if maximum is not None and (normal is None or maximum > normal):
            return maximum
    return normal


def max_output_tokens(
    model: Any,
    *,
    scope_key: str | None = None,
    outbound_model: str | None = None,
) -> int | None:
    return _to_int(get_metadata(
        model, scope_key=scope_key, outbound_model=outbound_model,
    ).get("maxOutputTokens"))


def compact_trigger_tokens(
    model: Any,
    *,
    scope_key: str | None = None,
    outbound_model: str | None = None,
    use_max_context: bool = False,
) -> int | None:
    metadata = get_metadata(
        model, scope_key=scope_key, outbound_model=outbound_model,
    )
    if use_max_context:
        common, source = get_override_fields(
            model, scope_key=scope_key, outbound_model=outbound_model,
        )
        if "compactTriggerTokens" in common or "compactTriggerTokens" in source:
            return _to_int(metadata.get("compactTriggerTokens"))
        normal = _to_int(metadata.get("contextWindow"))
        maximum = _to_int(metadata.get("contextWindowMaxMode"))
        if maximum is not None and (normal is None or maximum > normal):
            output = _to_int(metadata.get("maxOutputTokens")) or 0
            return max(1, int(max(1, maximum - output) * 0.8))
    return _to_int(metadata.get("compactTriggerTokens"))


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


@dataclass(frozen=True, slots=True)
class EffectiveRequestBudget:
    """Candidate-specific effective limits for one final upstream payload."""

    model_id: str
    scope_key: str | None
    outbound_model: str | None
    context_window: int | None
    max_input_tokens: int | None
    max_output_tokens: int | None
    compact_trigger_tokens: int | None
    requested_output_tokens: int
    protocol_reserve_tokens: int
    effective_input_budget: int | None
    budget_known: bool
    output_within_limit: bool

    def can_fit(self, prompt_tokens: int) -> bool:
        return (
            self.output_within_limit
            and (
                self.effective_input_budget is None
                or max(0, int(prompt_tokens)) <= self.effective_input_budget
            )
        )


def _requested_output_tokens(request_shape: Mapping[str, Any] | None) -> int | None:
    if not isinstance(request_shape, Mapping):
        return None
    shapes: list[Mapping[str, Any]] = [request_shape]
    nested = request_shape.get("response")
    if isinstance(nested, Mapping):
        shapes.insert(0, nested)
    for shape in shapes:
        for key in ("max_output_tokens", "max_completion_tokens", "max_tokens"):
            if key in shape:
                return _to_int(shape.get(key))
    return None


def effective_request_budget(
    model: Any,
    *,
    scope_key: str | None = None,
    outbound_model: str | None = None,
    request_shape: Mapping[str, Any] | None = None,
    requested_output_tokens: int | None = None,
    protocol_reserve_tokens: int = 0,
    use_max_context: bool = False,
) -> EffectiveRequestBudget:
    """Resolve final input/output limits for one concrete route candidate.

    Input and output maxima are independent: a requested output maximum is not
    occupied input space. Only an explicit protocol safety reserve reduces the
    context-side input limit. The compact trigger never reduces the hard input
    budget. Unknown metadata stays unknown; no provider coupling is inferred.
    """

    metadata = get_metadata(
        model, scope_key=scope_key, outbound_model=outbound_model,
    )
    normal_context = _to_int(metadata.get("contextWindow"))
    window = normal_context
    if use_max_context:
        maximum = _to_int(metadata.get("contextWindowMaxMode"))
        if maximum is not None and (window is None or maximum > window):
            window = maximum
    max_input = _to_int(metadata.get("maxInputTokens"))
    # A present maxInputTokens is an independent ceiling, including catalog and
    # native values equal to normal context. Only an absent input limit falls
    # back to the selected normal/Max Context window; equality is not provenance.
    max_output = _to_int(metadata.get("maxOutputTokens"))
    requested = (
        _to_int(requested_output_tokens)
        if requested_output_tokens is not None
        else _requested_output_tokens(request_shape)
    )
    if requested is None:
        # No explicit output request means the provider applies its own default;
        # reserving the advertised maximum would turn input-only maxima into a
        # zero budget for models whose independent output cap equals context.
        requested = 0
    reserve = max(0, int(protocol_reserve_tokens))
    limits: list[int] = []
    if max_input is not None:
        limits.append(max_input)
    if window is not None:
        limits.append(max(0, window - reserve))
    effective_input = min(limits) if limits else None
    trigger = compact_trigger_tokens(
        model,
        scope_key=scope_key,
        outbound_model=outbound_model,
        use_max_context=use_max_context,
    )
    if trigger is not None and effective_input is not None:
        trigger = min(trigger, effective_input)
    return EffectiveRequestBudget(
        model_id=normalize_model_name(model),
        scope_key=scope_key,
        outbound_model=normalize_model_name(outbound_model) or None,
        context_window=window,
        max_input_tokens=max_input,
        max_output_tokens=max_output,
        compact_trigger_tokens=trigger,
        requested_output_tokens=requested,
        protocol_reserve_tokens=reserve,
        effective_input_budget=effective_input,
        budget_known=effective_input is not None,
        output_within_limit=(max_output is None or requested <= max_output),
    )


def should_compact(
    model: Any,
    prompt_tokens: int,
    *,
    scope_key: str | None = None,
    outbound_model: str | None = None,
    request_shape: Mapping[str, Any] | None = None,
    use_max_context: bool = False,
) -> bool:
    budget = effective_request_budget(
        model,
        scope_key=scope_key,
        outbound_model=outbound_model,
        request_shape=request_shape,
        use_max_context=use_max_context,
    )
    prompt = max(0, int(prompt_tokens))
    return bool(
        (budget.compact_trigger_tokens is not None and prompt >= budget.compact_trigger_tokens)
        or (budget.effective_input_budget is not None and prompt > budget.effective_input_budget)
    )


def safe_prompt_limit(
    model: Any,
    *,
    scope_key: str | None = None,
    outbound_model: str | None = None,
    buffer_tokens: int | None = None,
    use_max_context: bool = False,
) -> int | None:
    """Hard compact fit limit; the trigger is intentionally not a fit limit."""

    buffer = compact_buffer_tokens() if buffer_tokens is None else max(0, int(buffer_tokens))
    reserve = summary_reserve_tokens(model, scope_key=scope_key, outbound_model=outbound_model)
    return effective_request_budget(
        model,
        scope_key=scope_key,
        outbound_model=outbound_model,
        requested_output_tokens=reserve,
        protocol_reserve_tokens=buffer,
        use_max_context=use_max_context,
    ).effective_input_budget


def required_context_for_compact(
    prompt_tokens: int,
    model: Any,
    *,
    scope_key: str | None = None,
    outbound_model: str | None = None,
    buffer_tokens: int | None = None,
) -> int:
    buffer = compact_buffer_tokens() if buffer_tokens is None else max(0, int(buffer_tokens))
    # Output size is validated independently, just as in the final wire guard.
    return max(0, int(prompt_tokens)) + buffer


def can_fit_for_compact(
    model: Any,
    prompt_tokens: int,
    *,
    scope_key: str | None = None,
    outbound_model: str | None = None,
    buffer_tokens: int | None = None,
    use_max_context: bool = False,
) -> bool:
    limit = safe_prompt_limit(
        model, scope_key=scope_key, outbound_model=outbound_model,
        buffer_tokens=buffer_tokens, use_max_context=use_max_context,
    )
    return limit is not None and max(0, int(prompt_tokens)) <= limit
