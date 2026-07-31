"""模型 Token 费用估算。

价格与供应商模型 ID 来自 models.dev ``api.json``，``models.json`` 仅用于
校验规范模型身份并建立无歧义的裸模型别名。运行时先加载随仓库提供的本地快照，
再异步刷新两份远端目录并原子写入 data 缓存；远端不可用不会影响请求处理。

当前 ``request_log`` 的 input_tokens 已扣除 cache_read_tokens，因此四类 Token
可直接分别计价。旧日志若无法确认这一口径，由 log_db 标记为未计价，不能再从
input 中猜测扣减缓存命中。
"""

from __future__ import annotations

import asyncio
import copy
import gzip
import hashlib
import json
import math
import os
import tempfile
import threading
from dataclasses import dataclass, fields
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Mapping

from . import config

TICKS_PER_USD = 10_000_000_000
_DEFAULT_SOURCE_URL = "https://models.dev/api.json"
_DEFAULT_MODELS_URL = "https://models.dev/models.json"
_BUNDLED_PATH = os.path.join(
    os.path.dirname(__file__), "resources", "models_dev_catalog.json.gz"
)
_CACHE_FILENAME = "models_dev_catalog.json.gz"
_MAX_REMOTE_CATALOG_BYTES = 16 * 1024 * 1024
_MAX_BILLING_INTEGER = (1 << 63) - 1
_PROVIDER_ALIASES = {
    "claude": "anthropic",
    "gemini": "google",
    "vertex_ai": "google-vertex",
    "bedrock": "amazon-bedrock",
}
_STANDARD_SERVICE_TIERS = {"default", "standard", "auto"}
_PRIORITY_SERVICE_TIERS = {"priority", "fast"}


@dataclass(frozen=True)
class PricingEntry:
    input_per_token: float
    output_per_token: float
    cache_write_per_token: float
    cache_read_per_token: float
    priority_input_per_token: float | None = None
    priority_output_per_token: float | None = None
    priority_cache_write_per_token: float | None = None
    priority_cache_read_per_token: float | None = None
    long_context_input_threshold: int = 0
    long_context_input_multiplier: float = 1.0
    long_context_output_multiplier: float = 1.0
    above_input_per_token: float | None = None
    above_output_per_token: float | None = None
    above_cache_write_per_token: float | None = None
    above_cache_read_per_token: float | None = None
    priority_above_input_per_token: float | None = None
    priority_above_output_per_token: float | None = None
    priority_above_cache_write_per_token: float | None = None
    priority_above_cache_read_per_token: float | None = None
    fast_multiplier: float = 1.0
    cache_write_1h_per_token: float | None = None
    cache_write_ttl_ambiguous: bool = False


@dataclass(frozen=True)
class CostEstimate:
    total_ticks: int
    input_ticks: int
    output_ticks: int
    cache_write_ticks: int
    cache_read_ticks: int
    pricing_model: str


@dataclass(frozen=True)
class PricingSettings:
    enabled: bool
    aliases: Mapping[str, str]
    overrides: Mapping[str, PricingEntry]
    channel_providers: Mapping[str, str]


@dataclass(frozen=True)
class PricingBinding:
    """Immutable dispatch-time route identity and complete tariff snapshot."""

    channel_key: str
    channel_type: str
    upstream_protocol: str | None
    client_visible_model: str
    outbound_model_id: str
    provider_id: str | None
    model_id: str
    pricing_key: str | None
    binding_source: str
    source_revision: str | None
    tariff: PricingEntry | None
    tariff_source: str | None
    binding_json: str
    binding_version: str


@dataclass(frozen=True)
class NormalizedBilling:
    """Billing facts observed on one real upstream response.

    ``usage_observed`` is deliberately independent from token values: an
    explicit all-zero usage object is observed and can be priced at zero, while
    a missing usage object remains unpriced.
    """

    usage_observed: bool = False
    usage_invalid: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    service_tier: str | None = None
    actual_cost_ticks: int | None = None


_lock = threading.RLock()
_catalog: dict[str, PricingEntry] = {}
_catalog_aliases: dict[str, str] = {}
_catalog_providers: set[str] = set()
# The same downloaded bundle also backs model metadata bindings. These indexes
# retain source records in memory; persistent config stores identities only.
_catalog_models: dict[str, dict[str, Any]] = {}
_catalog_provider_names: dict[str, str] = {}
_canonical_official_models: dict[str, str] = {}
_initialized = False
_catalog_source = "none"
_catalog_revision = "none"


def _nonnegative_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result < 0 or not math.isfinite(result):
        return None
    return result


def _catalog_number(value: Any) -> float | None:
    """Accept only finite JSON numbers from the models.dev schema."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return _nonnegative_float(value)


def _catalog_positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None


def _first_context_tier_threshold(cost: Any) -> int | None:
    """Return the first valid models.dev context-price tier threshold."""
    if not isinstance(cost, Mapping):
        return None
    tiers = cost.get("tiers")
    if not isinstance(tiers, list):
        return None
    thresholds: list[int] = []
    for item in tiers:
        if not isinstance(item, Mapping):
            continue
        tier = item.get("tier")
        if not isinstance(tier, Mapping) or str(tier.get("type") or "context") != "context":
            continue
        threshold = _catalog_positive_int(tier.get("size"))
        if (
            threshold is None
            or _catalog_number(item.get("input")) is None
            or _catalog_number(item.get("output")) is None
        ):
            continue
        thresholds.append(threshold)
    return min(thresholds) if thresholds else None


def _default_compact_trigger_tokens(
    context_window: int | None,
    max_output_tokens: int | None,
) -> int | None:
    """Derive the compact boundary when models.dev has no context-price tier."""
    if (
        context_window is None
        or max_output_tokens is None
        or context_window <= max_output_tokens
    ):
        return None
    return ((context_window - max_output_tokens) * 4) // 5


def _strict_nonnegative_int(value: Any) -> int | None:
    """Parse one billing integer without coercing corruption to zero."""

    if isinstance(value, bool):
        return None
    if isinstance(value, float) and (not math.isfinite(value) or not value.is_integer()):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed < 0 or parsed > _MAX_BILLING_INTEGER:
        return None
    return parsed


def _entry_from_models_dev(raw: Any) -> PricingEntry | None:
    """Parse one models.dev API model entry (prices are USD / 1M tokens)."""

    if not isinstance(raw, Mapping):
        return None
    cost = raw.get("cost")
    if not isinstance(cost, Mapping):
        return None

    numeric_cost_fields = (
        "input", "output", "cache_write", "cache_read",
        "reasoning", "input_audio", "output_audio",
    )
    if any(
        field in cost and _catalog_number(cost.get(field)) is None
        for field in numeric_cost_fields
    ):
        return None
    input_per_million = _catalog_number(cost.get("input"))
    output_per_million = _catalog_number(cost.get("output"))
    # Image/video/per-request-only entries cannot become fake zero-cost token models.
    if input_per_million is None or output_per_million is None:
        return None
    # The attempt ledger currently retains aggregate input/output tokens only.
    # A catalog entry that bills reasoning or audio at a different rate needs
    # a token-dimension split we do not have, so estimating it would be false
    # precision. Entries whose specialist rate equals the aggregate rate are
    # safe because the split cannot change the total.
    for special, aggregate in (
        ("reasoning", output_per_million),
        ("input_audio", input_per_million),
        ("output_audio", output_per_million),
    ):
        if special in cost and _catalog_number(cost.get(special)) != aggregate:
            return None

    def per_token(value: Any, fallback: float | None = None) -> float | None:
        parsed = _catalog_number(value)
        return fallback if parsed is None else parsed / 1_000_000

    input_price = input_per_million / 1_000_000
    output_price = output_per_million / 1_000_000
    # models.dev omission means this token class is not billed. This differs
    # deliberately from operator overrides, whose legacy input-price fallback is
    # retained in _entry_from_override().
    cache_write = per_token(cost.get("cache_write"), 0.0)
    cache_read = per_token(cost.get("cache_read"), 0.0)

    tiers_raw = cost.get("tiers", [])
    if tiers_raw is None:
        tiers_raw = []
    if not isinstance(tiers_raw, list):
        return None
    context_tiers: list[tuple[int, Mapping[str, Any]]] = []
    for item in tiers_raw:
        if not isinstance(item, Mapping) or not isinstance(item.get("tier"), Mapping):
            return None
        tier_meta = item["tier"]
        if tier_meta.get("type") != "context":
            return None
        threshold = _catalog_positive_int(tier_meta.get("size"))
        if threshold is None:
            return None
        if any(
            field in item and _catalog_number(item.get(field)) is None
            for field in (
                "input", "output", "cache_write", "cache_read",
                "reasoning", "input_audio", "output_audio",
            )
        ):
            return None
        if _catalog_number(item.get("input")) is None or _catalog_number(
            item.get("output")
        ) is None:
            return None
        for special, aggregate_field in (
            ("reasoning", "output"),
            ("input_audio", "input"),
            ("output_audio", "output"),
        ):
            if (
                special in item
                and _catalog_number(item.get(special))
                != _catalog_number(item.get(aggregate_field))
            ):
                return None
        context_tiers.append((threshold, item))
    # The settlement schema intentionally represents one request-wide context
    # threshold. Multiple models.dev context tiers must remain unpriced rather
    # than being flattened into a plausible but wrong tariff.
    if len(context_tiers) > 1:
        return None

    legacy_above = cost.get("context_over_200k")
    if legacy_above is not None and not isinstance(legacy_above, Mapping):
        return None
    tier_threshold = 0
    tier_cost: Mapping[str, Any] | None = None
    if context_tiers:
        tier_threshold, tier_cost = context_tiers[0]
    elif isinstance(legacy_above, Mapping):
        tier_threshold, tier_cost = 200_000, legacy_above
    if tier_cost is not None and any(
        field in tier_cost and _catalog_number(tier_cost.get(field)) is None
        for field in (
            "input", "output", "cache_write", "cache_read",
            "reasoning", "input_audio", "output_audio",
        )
    ):
        return None
    if tier_cost is not None:
        for special, aggregate_field in (
            ("reasoning", "output"),
            ("input_audio", "input"),
            ("output_audio", "output"),
        ):
            if (
                special in tier_cost
                and _catalog_number(tier_cost.get(special))
                != _catalog_number(tier_cost.get(aggregate_field))
            ):
                return None

    modes = raw.get("experimental", {})
    modes = modes.get("modes", {}) if isinstance(modes, Mapping) else {}
    fast = modes.get("fast") if isinstance(modes, Mapping) else None
    fast_cost = fast.get("cost") if isinstance(fast, Mapping) else None
    if fast_cost is not None and not isinstance(fast_cost, Mapping):
        return None
    if isinstance(fast_cost, Mapping) and any(
        field in fast_cost and _catalog_number(fast_cost.get(field)) is None
        for field in (
            "input", "output", "cache_write", "cache_read",
            "reasoning", "input_audio", "output_audio",
        )
    ):
        return None
    if isinstance(fast_cost, Mapping):
        fast_input_raw = _catalog_number(fast_cost.get("input"))
        fast_output_raw = _catalog_number(fast_cost.get("output"))
        if fast_input_raw is None or fast_output_raw is None:
            return None
        for special, aggregate in (
            ("reasoning", fast_output_raw),
            ("input_audio", fast_input_raw),
            ("output_audio", fast_output_raw),
        ):
            if special in fast_cost and _catalog_number(fast_cost.get(special)) != aggregate:
                return None

    def tier_price(field: str, fallback: float) -> float | None:
        if tier_cost is None:
            return None
        return per_token(tier_cost.get(field), fallback)

    def fast_price(field: str, fallback: float) -> float | None:
        if not isinstance(fast_cost, Mapping):
            return None
        return per_token(fast_cost.get(field), fallback)

    fast_input = fast_price("input", input_price)
    fast_output = fast_price("output", output_price)
    fast_input_fallback = fast_input if fast_input is not None else input_price
    fast_cache_write = fast_price("cache_write", 0.0)
    fast_cache_read = fast_price("cache_read", 0.0)

    return PricingEntry(
        input_per_token=input_price,
        output_per_token=output_price,
        cache_write_per_token=cache_write if cache_write is not None else 0.0,
        cache_read_per_token=cache_read if cache_read is not None else 0.0,
        priority_input_per_token=fast_input,
        priority_output_per_token=fast_output,
        priority_cache_write_per_token=fast_cache_write,
        priority_cache_read_per_token=fast_cache_read,
        long_context_input_threshold=tier_threshold,
        above_input_per_token=tier_price("input", input_price),
        above_output_per_token=tier_price("output", output_price),
        above_cache_write_per_token=tier_price("cache_write", 0.0),
        above_cache_read_per_token=tier_price("cache_read", 0.0),
        # models.dev fast mode is an exact replacement tariff, not a multiplier.
        # Keep the same fast tariff above a context threshold unless the source
        # eventually publishes an explicit fast-context tier.
        priority_above_input_per_token=fast_input if tier_threshold else None,
        priority_above_output_per_token=fast_output if tier_threshold else None,
        priority_above_cache_write_per_token=(
            fast_cache_write if tier_threshold else None
        ),
        priority_above_cache_read_per_token=(fast_cache_read if tier_threshold else None),
        # Anthropic usage combines 5-minute and 1-hour cache writes, while
        # models.dev currently publishes one cache_write tariff. request_log
        # does not retain the TTL split, so aggregated cache writes must fail
        # closed instead of assuming every write used the cheaper TTL.
        cache_write_ttl_ambiguous=bool(
            str(raw.get("family") or "").lower().startswith("claude")
            and "cache_write" in cost
        ),
    )


def _parse_models_dev_catalog(
    api_payload: Any,
    models_payload: Any,
) -> tuple[dict[str, PricingEntry], dict[str, str], set[str]]:
    if not isinstance(api_payload, Mapping) or not isinstance(models_payload, Mapping):
        raise ValueError("models.dev catalogs must be JSON objects")
    parsed: dict[str, PricingEntry] = {}
    aliases: dict[str, str] = {}
    providers: set[str] = set()
    raw_model_targets: dict[str, set[str]] = {}

    for provider_key, provider_raw in api_payload.items():
        if not isinstance(provider_key, str) or not isinstance(provider_raw, Mapping):
            continue
        provider = provider_key.strip().lower()
        models = provider_raw.get("models")
        if not provider or not isinstance(models, Mapping):
            continue
        providers.add(provider)
        provider_id_aliases: dict[str, str | None] = {}
        for model_key, raw in models.items():
            if not isinstance(model_key, str) or not model_key.strip():
                continue
            model = model_key.strip().lower()
            entry = _entry_from_models_dev(raw)
            if entry is None:
                continue
            target = f"{provider}/{model}"
            parsed[target] = entry
            raw_model_targets.setdefault(model, set()).add(target)
            raw_id = raw.get("id") if isinstance(raw, Mapping) else None
            if isinstance(raw_id, str) and raw_id.strip():
                raw_id_normalized = raw_id.strip().lower()
                raw_model_targets.setdefault(raw_id_normalized, set()).add(target)
                alias = f"{provider}/{raw_id_normalized}"
                previous = provider_id_aliases.get(alias)
                provider_id_aliases[alias] = target if previous in (None, target) else ""
        for alias, target in provider_id_aliases.items():
            if target and alias not in parsed:
                aliases[alias] = target

    for canonical_key in models_payload:
        if not isinstance(canonical_key, str) or "/" not in canonical_key:
            continue
        lab, model = canonical_key.strip().lower().split("/", 1)
        canonical_target = f"{lab}/{model}"
        direct_target = (
            canonical_target
            if canonical_target in parsed
            else aliases.get(canonical_target, "")
        )
        targets = raw_model_targets.get(model, set())
        # A bare model ID does not identify a serving provider. Only add the
        # convenience alias when models.json confirms the canonical identity
        # and api.json contains exactly one provider-price target for that raw
        # ID. Otherwise callers must use <provider>/<Model ID> explicitly.
        if len(targets) == 1:
            sole_target = next(iter(targets))
            if not direct_target or direct_target == sole_target:
                aliases[model] = sole_target

    if not parsed:
        raise ValueError("pricing catalog contains no token-priced models")
    return parsed, aliases, providers


def _parse_models_dev_metadata_indexes(
    api_payload: Any,
    models_payload: Any,
) -> tuple[dict[str, dict[str, Any]], dict[str, str], dict[str, str]]:
    """Build exact provider/model metadata and canonical-official indexes.

    Unlike the tariff index this includes models without token pricing. The
    canonical matcher only accepts a models.json root whose provider/model also
    exists exactly in api.json; it never guesses providers from model prefixes.
    """
    if not isinstance(api_payload, Mapping) or not isinstance(models_payload, Mapping):
        raise ValueError("models.dev catalogs must be JSON objects")
    entries: dict[str, dict[str, Any]] = {}
    provider_names: dict[str, str] = {}
    for provider_key, provider_raw in api_payload.items():
        if not isinstance(provider_key, str) or not isinstance(provider_raw, Mapping):
            continue
        provider = provider_key.strip().lower()
        models = provider_raw.get("models")
        if not provider or not isinstance(models, Mapping):
            continue
        display_name = provider_raw.get("name")
        provider_names[provider] = (
            display_name.strip()
            if isinstance(display_name, str) and display_name.strip()
            else provider
        )
        for model_key, raw in models.items():
            if (
                not isinstance(model_key, str)
                or not model_key.strip()
                or not isinstance(raw, Mapping)
            ):
                continue
            model = model_key.strip().lower()
            entries[f"{provider}/{model}"] = copy.deepcopy(dict(raw))

    candidates: dict[str, set[str]] = {}
    for canonical_key in models_payload:
        if not isinstance(canonical_key, str) or "/" not in canonical_key:
            continue
        provider, model = canonical_key.strip().lower().split("/", 1)
        target = f"{provider}/{model}"
        if target not in entries:
            continue
        candidates.setdefault(model, set()).add(target)
        candidates.setdefault(target, set()).add(target)
    official = {
        name: next(iter(targets))
        for name, targets in candidates.items()
        if len(targets) == 1
    }
    return entries, provider_names, official


def _parse_catalog(
    api_payload: Any,
    models_payload: Any,
) -> dict[str, PricingEntry]:
    """Test/helper compatibility: return the parsed models.dev price entries."""

    return _parse_models_dev_catalog(api_payload, models_payload)[0]


def _load_json(path: str) -> Any:
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def _catalog_payload_parts(payload: Any) -> tuple[Any, Any]:
    if not isinstance(payload, Mapping) or payload.get("schema") != 1:
        raise ValueError("invalid models.dev catalog bundle")
    return payload.get("api"), payload.get("models")


def _catalog_revision_for_payloads(api_payload: Any, models_payload: Any) -> str:
    canonical = json.dumps(
        {"api": api_payload, "models": models_payload},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")
    return "models-dev-sha256:" + hashlib.sha256(canonical).hexdigest()


def initialize() -> None:
    """同步加载缓存或随包快照；不发网络请求，可安全放在 lifespan 启动阶段。"""

    global _catalog, _catalog_aliases, _catalog_providers, _catalog_models
    global _catalog_provider_names, _canonical_official_models
    global _initialized, _catalog_source, _catalog_revision
    with _lock:
        if _initialized:
            return
        candidates = (
            (os.path.join(config.DATA_DIR, _CACHE_FILENAME), "cache"),
            (_BUNDLED_PATH, "bundled"),
        )
        last_error: Exception | None = None
        for path, source in candidates:
            try:
                api_payload, models_payload = _catalog_payload_parts(_load_json(path))
                parsed, aliases, providers = _parse_models_dev_catalog(
                    api_payload, models_payload
                )
                metadata_models, provider_names, official_models = (
                    _parse_models_dev_metadata_indexes(api_payload, models_payload)
                )
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                last_error = exc
                continue
            _catalog = parsed
            _catalog_aliases = aliases
            _catalog_providers = providers
            _catalog_models = metadata_models
            _catalog_provider_names = provider_names
            _canonical_official_models = official_models
            _catalog_source = source
            _catalog_revision = _catalog_revision_for_payloads(api_payload, models_payload)
            _initialized = True
            return
        raise RuntimeError(f"failed to load model pricing catalog: {last_error}")


def reload_local_catalog() -> bool:
    """Reload the last successfully saved models.dev bundle.

    A missing or invalid cache leaves the currently active in-memory catalog
    untouched.  This is used by the manual metadata sync path so matching is
    always performed from the local last-known-good bundle, never directly from
    a partially downloaded response.
    """

    global _catalog, _catalog_aliases, _catalog_providers, _catalog_models
    global _catalog_provider_names, _canonical_official_models
    global _initialized, _catalog_source, _catalog_revision
    cache_path = os.path.join(config.DATA_DIR, _CACHE_FILENAME)
    try:
        api_payload, models_payload = _catalog_payload_parts(_load_json(cache_path))
        parsed, aliases, providers = _parse_models_dev_catalog(
            api_payload, models_payload
        )
        metadata_models, provider_names, official_models = (
            _parse_models_dev_metadata_indexes(api_payload, models_payload)
        )
        revision = _catalog_revision_for_payloads(api_payload, models_payload)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False

    with _lock:
        _catalog = parsed
        _catalog_aliases = aliases
        _catalog_providers = providers
        _catalog_models = metadata_models
        _catalog_provider_names = provider_names
        _canonical_official_models = official_models
        _catalog_source = "cache"
        _catalog_revision = revision
        _initialized = True
    return True


def reset_for_tests() -> None:
    global _catalog, _catalog_aliases, _catalog_providers, _catalog_models
    global _catalog_provider_names, _canonical_official_models
    global _initialized, _catalog_source, _catalog_revision
    with _lock:
        _catalog = {}
        _catalog_aliases = {}
        _catalog_providers = set()
        _catalog_models = {}
        _catalog_provider_names = {}
        _canonical_official_models = {}
        _initialized = False
        _catalog_source = "none"
        _catalog_revision = "none"


def _ensure_catalog() -> bool:
    if _initialized:
        return True
    try:
        initialize()
    except Exception:
        return False
    return True


def catalog_model(provider_model: str) -> dict[str, Any] | None:
    """Return one exact models.dev api.json model record.

    The key must be the persisted ``provider/model`` identity. Catalog aliases
    and bare model IDs are deliberately not accepted by metadata bindings.
    """
    key = str(provider_model or "").strip().lower()
    if not key or "/" not in key or not _ensure_catalog():
        return None
    with _lock:
        raw = _catalog_models.get(key)
        return copy.deepcopy(raw) if raw is not None else None


def catalog_metadata(provider_model: str) -> dict[str, Any] | None:
    """Project one exact catalog record into Parrot's runtime metadata shape."""
    raw = catalog_model(provider_model)
    if raw is None:
        return None
    limit = raw.get("limit") if isinstance(raw.get("limit"), Mapping) else {}
    modalities = raw.get("modalities") if isinstance(raw.get("modalities"), Mapping) else {}
    input_modalities = modalities.get("input") if isinstance(modalities, Mapping) else []
    reasoning_efforts: list[str] = []
    options = raw.get("reasoning_options")
    if isinstance(options, list):
        for option in options:
            if not isinstance(option, Mapping) or option.get("type") != "effort":
                continue
            values = option.get("values")
            if isinstance(values, list):
                reasoning_efforts = [
                    str(value).strip().lower() for value in values
                    if str(value).strip()
                ]
                break
    context = _catalog_positive_int(limit.get("context"))
    output = _catalog_positive_int(limit.get("output"))
    cost = raw.get("cost") if isinstance(raw.get("cost"), Mapping) else None
    compact_trigger = _first_context_tier_threshold(cost)
    if compact_trigger is None:
        compact_trigger = _default_compact_trigger_tokens(context, output)
    result: dict[str, Any] = {
        "name": str(raw.get("name") or raw.get("id") or provider_model),
        "description": str(raw.get("description") or ""),
        "family": str(raw.get("family") or ""),
        "vision": bool(
            raw.get("attachment") is True
            or (isinstance(input_modalities, list) and "image" in input_modalities)
        ),
        "reasoning": bool(raw.get("reasoning")),
        "reasoningEfforts": reasoning_efforts,
        "toolCall": bool(raw.get("tool_call")),
        "structuredOutput": bool(raw.get("structured_output")),
        "temperature": bool(raw.get("temperature")),
        "modalities": copy.deepcopy(dict(modalities)),
        "releaseDate": str(raw.get("release_date") or ""),
        # Preserve the complete catalog pricing object for display/consumers.
        "cost": copy.deepcopy(cost),
    }
    if context is not None:
        result["contextWindow"] = context
    if output is not None:
        result["maxOutputTokens"] = output
    if compact_trigger is not None:
        # An explicit context-price tier wins. Without one, reserve the model's
        # maximum output and use 80% of the remaining input capacity.
        result["compactTriggerTokens"] = compact_trigger
    return result


def catalog_providers() -> list[dict[str, str]]:
    if not _ensure_catalog():
        return []
    with _lock:
        return [
            {"id": provider, "name": _catalog_provider_names.get(provider, provider)}
            for provider in sorted(_catalog_provider_names)
        ]


def catalog_provider_models(provider_id: str) -> list[dict[str, Any]]:
    provider = str(provider_id or "").strip().lower()
    if not provider or not _ensure_catalog():
        return []
    prefix = f"{provider}/"
    with _lock:
        keys = sorted(key for key in _catalog_models if key.startswith(prefix))
        return [
            {
                "key": key,
                "id": key[len(prefix):],
                "name": str(_catalog_models[key].get("name") or key[len(prefix):]),
            }
            for key in keys
        ]


def catalog_models() -> list[dict[str, str]]:
    """Return lightweight descriptors for every exact models.dev model.

    Telegram's metadata-binding picker uses this single snapshot to rank exact
    same-name candidates and to filter the catalog without repeatedly scanning
    every provider.  Persisted bindings still use the exact ``key`` identity.
    """

    if not _ensure_catalog():
        return []
    with _lock:
        result: list[dict[str, str]] = []
        for key in sorted(_catalog_models):
            provider, model_id = key.split("/", 1)
            raw = _catalog_models[key]
            result.append({
                "key": key,
                "id": model_id,
                "name": str(raw.get("name") or model_id),
                "provider_id": provider,
                "provider_name": _catalog_provider_names.get(provider, provider),
            })
        return result


def canonical_official_model(model: str) -> str | None:
    """Resolve an exact canonical model ID to its official provider/model."""
    name = str(model or "").strip().lower()
    if not name or not _ensure_catalog():
        return None
    with _lock:
        return _canonical_official_models.get(name)


def catalog_tariff(provider_model: str) -> PricingEntry | None:
    """Return tariff only for one exact provider/model binding identity."""
    key = str(provider_model or "").strip().lower()
    if not key or not _ensure_catalog():
        return None
    with _lock:
        return _catalog.get(key)


def _entry_from_override(raw: Any) -> PricingEntry | None:
    """解析配置覆盖；价格单位均为 USD / 1M Token。"""

    if not isinstance(raw, Mapping):
        return None
    numeric_fields = (
        "inputPerMillion", "outputPerMillion", "cacheWritePerMillion",
        "cacheReadPerMillion", "priorityInputPerMillion",
        "priorityOutputPerMillion", "priorityCacheWritePerMillion",
        "priorityCacheReadPerMillion",
    )
    if any(
        name in raw and raw.get(name) is not None
        and _nonnegative_float(raw.get(name)) is None
        for name in numeric_fields
    ):
        return None
    input_m = _nonnegative_float(raw.get("inputPerMillion"))
    output_m = _nonnegative_float(raw.get("outputPerMillion"))
    if input_m is None or output_m is None:
        return None

    def per_token(name: str, fallback: float | None = None) -> float | None:
        value = _nonnegative_float(raw.get(name))
        if value is None:
            return fallback
        return value / 1_000_000

    input_p = input_m / 1_000_000
    output_p = output_m / 1_000_000
    return PricingEntry(
        input_per_token=input_p,
        output_per_token=output_p,
        cache_write_per_token=per_token("cacheWritePerMillion", input_p) or 0.0,
        cache_read_per_token=per_token("cacheReadPerMillion", input_p) or 0.0,
        priority_input_per_token=per_token("priorityInputPerMillion"),
        priority_output_per_token=per_token("priorityOutputPerMillion"),
        priority_cache_write_per_token=per_token("priorityCacheWritePerMillion"),
        priority_cache_read_per_token=per_token("priorityCacheReadPerMillion"),
    )


def settings(cfg: Mapping[str, Any] | None = None) -> PricingSettings:
    pricing_cfg = (cfg or config.get()).get("pricing", {})
    if not isinstance(pricing_cfg, Mapping):
        pricing_cfg = {}
    aliases_raw = pricing_cfg.get("aliases", {})
    aliases: dict[str, str] = {}
    if isinstance(aliases_raw, Mapping):
        for alias, target in aliases_raw.items():
            if isinstance(alias, str) and isinstance(target, str) and alias.strip() and target.strip():
                aliases[alias.strip().lower()] = target.strip().lower()
    overrides_raw = pricing_cfg.get("overrides", {})
    overrides: dict[str, PricingEntry] = {}
    if isinstance(overrides_raw, Mapping):
        for model, raw in overrides_raw.items():
            if not isinstance(model, str) or not model.strip():
                continue
            entry = _entry_from_override(raw)
            if entry is not None:
                overrides[model.strip().lower()] = entry
    channel_providers_raw = pricing_cfg.get("channelProviders", {})
    channel_providers: dict[str, str] = {}
    if isinstance(channel_providers_raw, Mapping):
        for channel_key, provider in channel_providers_raw.items():
            if (
                isinstance(channel_key, str)
                and isinstance(provider, str)
                and channel_key.strip()
                and provider.strip()
            ):
                channel_providers[channel_key.strip().lower()] = provider.strip().lower()
    return PricingSettings(
        enabled=bool(pricing_cfg.get("enabled", True)),
        aliases=aliases,
        overrides=overrides,
        channel_providers=channel_providers,
    )


def _model_candidates(model: str, aliases: Mapping[str, str]) -> list[str]:
    normalized = str(model or "").strip().lower()
    if not normalized:
        return []
    normalized = aliases.get(normalized, normalized)
    candidates: list[str] = [normalized]
    if "/" in normalized:
        provider, remainder = normalized.split("/", 1)
        mapped_provider = _PROVIDER_ALIASES.get(provider)
        if mapped_provider:
            candidates.append(f"{mapped_provider}/{remainder}")
    # Never strip provider or dated-version segments. models.dev prices are
    # provider/model specific; a plausible fallback can silently select a
    # different upstream tariff. Custom variants require an explicit alias.
    return list(dict.fromkeys(candidates))


def provider_pricing_model(
    model: str,
    channel_key: str | None = None,
    *,
    pricing_settings: PricingSettings | None = None,
) -> str:
    """Qualify a provider Model ID only when the route proves its provider."""

    normalized = str(model or "").strip()
    if not normalized:
        return normalized
    current = pricing_settings or settings()
    explicit_alias = current.aliases.get(normalized.lower())
    if explicit_alias:
        # An explicit alias is already a complete pricing lookup decision and
        # therefore takes precedence over automatic route qualification.
        return explicit_alias
    if normalized.lower() in current.overrides:
        # A user-supplied tariff is already an explicit pricing decision. Do
        # not make a naked override unreachable merely because the channel is
        # also mapped to a models.dev provider.
        return normalized
    channel = str(channel_key or "").strip().lower()
    provider = ""
    parts = channel.split(":", 2)
    if len(parts) >= 2 and parts[0] == "oauth":
        provider = parts[1]
    else:
        provider = current.channel_providers.get(channel, "")
    provider = _PROVIDER_ALIASES.get(provider, provider)
    if provider:
        if normalized.lower().startswith(f"{provider}/"):
            return normalized
        return f"{provider}/{normalized}"
    return normalized


def _normalize_provider_id(value: Any) -> str | None:
    provider = str(value or "").strip().lower()
    provider = _PROVIDER_ALIASES.get(provider, provider)
    if not provider or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789._-" for ch in provider):
        return None
    return provider


def _provider_from_explicit_target(target: str) -> str | None:
    if "/" not in target:
        return None
    return _normalize_provider_id(target.partition("/")[0])


def _pricing_entry_payload(entry: PricingEntry) -> dict[str, Any]:
    return {item.name: getattr(entry, item.name) for item in fields(PricingEntry)}


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    )


def _binding_price(
    pricing_key: str,
    current: PricingSettings,
) -> tuple[str, PricingEntry, str, str] | None:
    """Read a tariff only from an exact metadata-binding catalog identity."""
    key = str(pricing_key or "").strip().lower()
    if not key or not current.enabled or not _ensure_catalog():
        return None
    with _lock:
        entry = _catalog.get(key)
        if entry is None:
            return None
        return key, entry, f"models.dev:{_catalog_source}", _catalog_revision


def build_pricing_binding(
    *,
    channel_key: str,
    channel_type: str,
    upstream_protocol: str | None,
    outbound_model_id: str,
    client_visible_model: str | None = None,
    pricing_settings: PricingSettings | None = None,
) -> PricingBinding:
    """Freeze an effective metadata binding and its exact models.dev tariff.

    Route/provider naming is never a pricing fallback. A typed OAuth provider is
    retained only as proof for provider-reported actual cost (not estimation).
    """
    current = pricing_settings or settings()
    stable_key = str(channel_key or "").strip()
    stable_type = str(channel_type or "").strip().lower()
    protocol = str(upstream_protocol or "").strip().lower() or None
    exact_model = str(outbound_model_id or "").strip()
    visible_model = str(client_visible_model or exact_model).strip()
    channel_normalized = stable_key.lower()

    oauth_provider: str | None = None
    if stable_type == "oauth" and channel_normalized.startswith("oauth:"):
        typed = channel_normalized.split(":", 2)
        if len(typed) == 3 and typed[2]:
            candidate = _normalize_provider_id(typed[1])
            if candidate in {"anthropic", "openai", "xai"}:
                oauth_provider = candidate

    from . import model_metadata

    metadata_binding = model_metadata.resolve_binding(
        visible_model,
        scope_key=stable_key,
        outbound_model=exact_model,
    )
    if metadata_binding is not None:
        provider_id = metadata_binding.provider_id
        catalog_model_id = metadata_binding.catalog_model_id
        pricing_key: str | None = metadata_binding.target
        binding_source = f"metadata_{metadata_binding.kind}"
    else:
        provider_id = oauth_provider
        catalog_model_id = exact_model
        pricing_key = None
        binding_source = "unbound"

    priced = _binding_price(pricing_key, current) if pricing_key else None
    tariff: PricingEntry | None = None
    tariff_source: str | None = None
    source_revision: str | None = None
    if priced is not None:
        pricing_key, tariff, tariff_source, source_revision = priced

    payload: dict[str, Any] = {
        "schema": 1,
        "dispatch": {
            "channel_key": stable_key,
            "channel_type": stable_type,
            "upstream_protocol": protocol,
            "client_visible_model": visible_model,
            "outbound_model_id": exact_model,
        },
        "metadata": {
            "provider_id": provider_id,
            "model_id": catalog_model_id,
            "pricing_key": pricing_key,
            "binding_source": binding_source,
            "source_revision": source_revision,
        },
        "tariff": (
            {"source": tariff_source, **_pricing_entry_payload(tariff)}
            if tariff is not None else None
        ),
    }
    raw = _canonical_json(payload)
    version = "binding-v1:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return PricingBinding(
        channel_key=stable_key,
        channel_type=stable_type,
        upstream_protocol=protocol,
        client_visible_model=visible_model,
        outbound_model_id=exact_model,
        provider_id=provider_id,
        model_id=catalog_model_id,
        pricing_key=pricing_key,
        binding_source=binding_source,
        source_revision=source_revision,
        tariff=tariff,
        tariff_source=tariff_source,
        binding_json=raw,
        binding_version=version,
    )


def pricing_binding_from_json(
    raw: Any,
    expected_version: str | None = None,
) -> PricingBinding | None:
    """Strictly validate a persisted canonical binding; corruption fails closed."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        payload = json.loads(raw)
        if not isinstance(payload, Mapping) or payload.get("schema") != 1:
            return None
        if _canonical_json(payload) != raw:
            return None
        version = "binding-v1:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
        if expected_version is not None and version != expected_version:
            return None
        dispatch = payload.get("dispatch")
        metadata = payload.get("metadata")
        if not isinstance(dispatch, Mapping) or not isinstance(metadata, Mapping):
            return None
        channel_key = dispatch.get("channel_key")
        channel_type = dispatch.get("channel_type")
        outbound_model_id = dispatch.get("outbound_model_id")
        client_visible_model = dispatch.get("client_visible_model", outbound_model_id)
        protocol = dispatch.get("upstream_protocol")
        if not all(
            isinstance(value, str)
            for value in (channel_key, channel_type, outbound_model_id, client_visible_model)
        ):
            return None
        if protocol is not None and not isinstance(protocol, str):
            return None
        catalog_model_id = metadata.get("model_id")
        if not isinstance(catalog_model_id, str):
            return None
        provider_id = metadata.get("provider_id")
        pricing_key = metadata.get("pricing_key")
        binding_source = metadata.get("binding_source")
        source_revision = metadata.get("source_revision")
        if provider_id is not None and not isinstance(provider_id, str):
            return None
        if pricing_key is not None and not isinstance(pricing_key, str):
            return None
        if not isinstance(binding_source, str):
            return None
        if source_revision is not None and not isinstance(source_revision, str):
            return None

        tariff_raw = payload.get("tariff")
        tariff: PricingEntry | None = None
        tariff_source: str | None = None
        if tariff_raw is not None:
            if not isinstance(tariff_raw, Mapping):
                return None
            tariff_source = tariff_raw.get("source")
            if not isinstance(tariff_source, str) or not tariff_source:
                return None
            values: dict[str, Any] = {}
            for item in fields(PricingEntry):
                if item.name not in tariff_raw:
                    return None
                value = tariff_raw[item.name]
                if item.name == "cache_write_ttl_ambiguous":
                    if not isinstance(value, bool):
                        return None
                    values[item.name] = value
                elif item.name == "long_context_input_threshold":
                    parsed = _strict_nonnegative_int(value)
                    if parsed is None:
                        return None
                    values[item.name] = parsed
                elif value is None:
                    values[item.name] = None
                else:
                    parsed_float = _nonnegative_float(value)
                    if parsed_float is None:
                        return None
                    values[item.name] = parsed_float
            tariff = PricingEntry(**values)
        return PricingBinding(
            channel_key=channel_key,
            channel_type=channel_type,
            upstream_protocol=protocol,
            client_visible_model=client_visible_model,
            outbound_model_id=outbound_model_id,
            provider_id=provider_id,
            model_id=catalog_model_id,
            pricing_key=pricing_key,
            binding_source=binding_source,
            source_revision=source_revision,
            tariff=tariff,
            tariff_source=tariff_source,
            binding_json=raw,
            binding_version=version,
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def resolve_price(
    model: str,
    *,
    pricing_settings: PricingSettings | None = None,
) -> tuple[str, PricingEntry] | None:
    current = pricing_settings or settings()
    if not current.enabled:
        return None
    candidates = _model_candidates(model, current.aliases)
    for candidate in candidates:
        override = current.overrides.get(candidate)
        if override is not None:
            return candidate, override
    # Explicit overrides remain usable even if a packaged/cache catalog is
    # unavailable. Only catalog-backed resolution depends on initialization.
    if not _initialized:
        try:
            initialize()
        except Exception:
            return None
    with _lock:
        for candidate in candidates:
            entry = _catalog.get(candidate)
            if entry is not None:
                return candidate, entry
            target = _catalog_aliases.get(candidate)
            if target:
                entry = _catalog.get(target)
                if entry is not None:
                    return target, entry
    return None


def long_context_threshold(
    model: str,
    *,
    pricing_settings: PricingSettings | None = None,
) -> int:
    """Return the resolved request-wide long-context threshold, or ``0``."""

    resolved = resolve_price(model, pricing_settings=pricing_settings)
    return int(resolved[1].long_context_input_threshold) if resolved is not None else 0


def has_ambiguous_cache_write_ttl(
    model: str,
    *,
    pricing_settings: PricingSettings | None = None,
) -> bool:
    """Whether cache-write tokens need a TTL split that logs do not retain."""

    resolved = resolve_price(model, pricing_settings=pricing_settings)
    if resolved is None:
        return False
    entry = resolved[1]
    if entry.cache_write_ttl_ambiguous:
        return True
    one_hour = entry.cache_write_1h_per_token
    return bool(
        one_hour is not None
        and one_hour > 0
        and abs(one_hour - entry.cache_write_per_token) > 1e-18
    )


def _ticks(tokens: int, price_per_token: float) -> int:
    token_count = _strict_nonnegative_int(tokens)
    price = _nonnegative_float(price_per_token)
    if token_count is None or price is None:
        raise ValueError("invalid billing component")
    try:
        value = Decimal(token_count) * Decimal(str(price)) * Decimal(TICKS_PER_USD)
        result = int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, OverflowError, ValueError) as exc:
        raise ValueError("billing component exceeds numeric limits") from exc
    if result < 0 or result > _MAX_BILLING_INTEGER:
        raise ValueError("billing component exceeds SQLite integer range")
    return result


def _effective_price(
    entry: PricingEntry,
    base_name: str,
    *,
    priority: bool,
    long_context: bool,
    input_side: bool,
) -> float:
    base = float(getattr(entry, base_name))
    priority_name = f"priority_{base_name}"
    priority_price = getattr(entry, priority_name)
    used_priority_price = bool(priority and priority_price is not None)
    if used_priority_price:
        base = float(priority_price)

    if long_context:
        above_name = f"above_{base_name}"
        if priority:
            priority_above = getattr(entry, f"priority_{above_name}")
            above = priority_above if priority_above is not None else getattr(entry, above_name)
            if priority_above is not None:
                used_priority_price = True
        else:
            above = getattr(entry, above_name)
        if above is not None:
            base = float(above)
        else:
            multiplier = (
                entry.long_context_input_multiplier
                if input_side
                else entry.long_context_output_multiplier
            )
            if multiplier > 1:
                base *= multiplier

    # Anthropic Fast uses a catalog multiplier instead of *_priority fields.
    # Do not stack it on providers that already supplied an explicit priority
    # tariff for this component.
    if priority and not used_priority_price and entry.fast_multiplier > 1:
        base *= entry.fast_multiplier
    return base


def priority_from_service_tier(value: Any) -> bool | None:
    """Map a proven upstream tier to standard/priority, else fail closed.

    ``None`` means the provider exposed a tier whose tariff models.dev does not
    describe (for example ``flex``), not that the request was standard.
    """

    if value is None:
        return False
    normalized = str(value).strip().lower()
    if not normalized or normalized in _STANDARD_SERVICE_TIERS:
        return False
    if normalized in _PRIORITY_SERVICE_TIERS:
        return True
    return None


def _has_priority_tariff(entry: PricingEntry) -> bool:
    return bool(
        entry.fast_multiplier > 1
        or any(
            value is not None
            for value in (
                entry.priority_input_per_token,
                entry.priority_output_per_token,
                entry.priority_cache_write_per_token,
                entry.priority_cache_read_per_token,
            )
        )
    )


def estimate_cost_with_snapshot(
    model: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
    priority: bool = False,
    long_context: bool | None = None,
    pricing_settings: PricingSettings | None = None,
) -> tuple[CostEstimate, str, str] | None:
    """Resolve once and return a cost plus the exact immutable tariff snapshot."""

    parsed_tokens = tuple(
        _strict_nonnegative_int(value)
        for value in (
            input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,
        )
    )
    if any(value is None for value in parsed_tokens):
        return None
    input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens = (
        int(value) for value in parsed_tokens
    )
    resolved = resolve_price(model, pricing_settings=pricing_settings)
    if resolved is None:
        return None
    pricing_model, entry = resolved
    # A proven priority/fast request needs an explicit replacement tariff.
    # Falling back to standard prices would be a plausible-looking undercount.
    if priority and not _has_priority_tariff(entry):
        return None
    # Anthropic exposes different 5-minute and 1-hour cache-write tariffs,
    # while request_log currently retains only their combined token count.
    # Returning a precise-looking estimate would silently choose the wrong
    # tariff for one of the two cases, so fail closed for those requests.
    if (
        cache_creation_tokens > 0
        and (
            entry.cache_write_ttl_ambiguous
            or (
                entry.cache_write_1h_per_token is not None
                and entry.cache_write_1h_per_token > 0
                and abs(entry.cache_write_1h_per_token - entry.cache_write_per_token) > 1e-18
            )
        )
    ):
        return None
    prompt_tokens = (
        input_tokens + cache_creation_tokens + cache_read_tokens
    )
    if prompt_tokens > _MAX_BILLING_INTEGER:
        return None
    use_long_context = (
        bool(long_context)
        if long_context is not None
        else bool(
            entry.long_context_input_threshold > 0
            and prompt_tokens > entry.long_context_input_threshold
        )
    )
    input_price = _effective_price(
        entry,
        "input_per_token",
        priority=priority,
        long_context=use_long_context,
        input_side=True,
    )
    output_price = _effective_price(
        entry,
        "output_per_token",
        priority=priority,
        long_context=use_long_context,
        input_side=False,
    )
    cache_write_price = _effective_price(
        entry,
        "cache_write_per_token",
        priority=priority,
        long_context=use_long_context,
        input_side=True,
    )
    cache_read_price = _effective_price(
        entry,
        "cache_read_per_token",
        priority=priority,
        long_context=use_long_context,
        input_side=True,
    )
    try:
        input_ticks = _ticks(input_tokens, input_price)
        output_ticks = _ticks(output_tokens, output_price)
        cache_write_ticks = _ticks(cache_creation_tokens, cache_write_price)
        cache_read_ticks = _ticks(cache_read_tokens, cache_read_price)
    except ValueError:
        return None
    total_ticks = input_ticks + output_ticks + cache_write_ticks + cache_read_ticks
    if total_ticks > _MAX_BILLING_INTEGER:
        return None
    estimate = CostEstimate(
        total_ticks=total_ticks,
        input_ticks=input_ticks,
        output_ticks=output_ticks,
        cache_write_ticks=cache_write_ticks,
        cache_read_ticks=cache_read_ticks,
        pricing_model=pricing_model,
    )
    snapshot = {
        "schema": 1,
        "model": pricing_model,
        "priority": bool(priority),
        "long_context": use_long_context,
        "input_per_token": input_price,
        "output_per_token": output_price,
        "cache_write_per_token": cache_write_price,
        "cache_read_per_token": cache_read_price,
        "long_context_threshold": entry.long_context_input_threshold,
    }
    raw = json.dumps(
        snapshot, sort_keys=True, separators=(",", ":"), allow_nan=False,
    )
    version = "pricing-v1:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return estimate, raw, version


def estimate_cost_from_binding(
    binding: PricingBinding,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
    priority: bool = False,
) -> tuple[CostEstimate, str, str] | None:
    """Price solely from a validated dispatch-time binding and its frozen tariff."""
    if (
        binding.tariff is None
        or not binding.pricing_key
        or not binding.channel_key
        or binding.channel_type not in {"api", "oauth"}
        or binding.upstream_protocol not in {
            "anthropic", "openai-chat", "openai-responses",
        }
        or not binding.outbound_model_id
    ):
        return None
    parsed_tokens = tuple(
        _strict_nonnegative_int(value)
        for value in (
            input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,
        )
    )
    if any(value is None for value in parsed_tokens):
        return None
    input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens = (
        int(value) for value in parsed_tokens
    )
    entry = binding.tariff
    if priority and not _has_priority_tariff(entry):
        return None
    if (
        cache_creation_tokens > 0
        and (
            entry.cache_write_ttl_ambiguous
            or (
                entry.cache_write_1h_per_token is not None
                and entry.cache_write_1h_per_token > 0
                and abs(entry.cache_write_1h_per_token - entry.cache_write_per_token) > 1e-18
            )
        )
    ):
        return None
    prompt_tokens = input_tokens + cache_creation_tokens + cache_read_tokens
    if prompt_tokens > _MAX_BILLING_INTEGER:
        return None
    long_context = bool(
        entry.long_context_input_threshold > 0
        and prompt_tokens > entry.long_context_input_threshold
    )
    prices = (
        _effective_price(
            entry, "input_per_token", priority=priority,
            long_context=long_context, input_side=True,
        ),
        _effective_price(
            entry, "output_per_token", priority=priority,
            long_context=long_context, input_side=False,
        ),
        _effective_price(
            entry, "cache_write_per_token", priority=priority,
            long_context=long_context, input_side=True,
        ),
        _effective_price(
            entry, "cache_read_per_token", priority=priority,
            long_context=long_context, input_side=True,
        ),
    )
    try:
        component_ticks = tuple(
            _ticks(tokens, price)
            for tokens, price in zip(
                (input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens),
                prices,
            )
        )
    except ValueError:
        return None
    total_ticks = sum(component_ticks)
    if total_ticks > _MAX_BILLING_INTEGER:
        return None
    estimate = CostEstimate(
        total_ticks=total_ticks,
        input_ticks=component_ticks[0],
        output_ticks=component_ticks[1],
        cache_write_ticks=component_ticks[2],
        cache_read_ticks=component_ticks[3],
        pricing_model=binding.pricing_key,
    )
    snapshot = {
        "schema": 2,
        "binding_version": binding.binding_version,
        "pricing_key": binding.pricing_key,
        "tariff_source": binding.tariff_source,
        "source_revision": binding.source_revision,
        "priority": bool(priority),
        "long_context": long_context,
        "long_context_threshold": entry.long_context_input_threshold,
        "input_per_token": prices[0],
        "output_per_token": prices[1],
        "cache_write_per_token": prices[2],
        "cache_read_per_token": prices[3],
    }
    raw = _canonical_json(snapshot)
    version = "pricing-v2:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return estimate, raw, version


def estimate_cost(
    model: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
    priority: bool = False,
    long_context: bool | None = None,
    pricing_settings: PricingSettings | None = None,
) -> CostEstimate | None:
    settled = estimate_cost_with_snapshot(
        model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_tokens=cache_creation_tokens,
        cache_read_tokens=cache_read_tokens,
        priority=priority,
        long_context=long_context,
        pricing_settings=pricing_settings,
    )
    return settled[0] if settled is not None else None


def _strict_response_objects(response_body: Any):
    """Yield only whole JSON objects or JSON from exact SSE ``data:`` lines.

    Arbitrary text/metadata is never searched.  This is intentionally stricter
    than the old regex fallback, which could mistake echoed request metadata for
    an official xAI usage cost.
    """

    if isinstance(response_body, Mapping):
        yield response_body
        return
    if response_body is None:
        return
    if isinstance(response_body, bytes):
        text = response_body.decode("utf-8", errors="replace")
    else:
        text = str(response_body)
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        parsed = None
    if isinstance(parsed, Mapping):
        yield parsed
        return
    for raw_line in text.splitlines():
        line = raw_line.strip()
        # Native Responses WebSocket logs are one complete JSON frame per line.
        if line.startswith("{") and line.endswith("}"):
            try:
                obj = json.loads(line)
            except (TypeError, ValueError, json.JSONDecodeError):
                obj = None
            if isinstance(obj, Mapping):
                yield obj
            continue
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(obj, Mapping):
            yield obj


def _billing_candidates(obj: Mapping[str, Any]):
    """Yield protocol-defined containers and whether actual cost is trusted.

    Token usage may legitimately live under Anthropic ``message`` or a provider
    ``data`` envelope. xAI actual cost is stricter: accept it only on a complete
    non-event JSON response or a terminal Responses event/response object.
    """

    event_type = str(obj.get("type") or "").strip().lower()
    terminal = not event_type or event_type in {
        "response.completed", "response.failed", "response.incomplete",
    }
    yield obj, terminal
    response = obj.get("response")
    if isinstance(response, Mapping):
        yield response, terminal
    message = obj.get("message")
    if isinstance(message, Mapping):
        yield message, False
    data = obj.get("data")
    if isinstance(data, Mapping):
        yield data, False


def normalize_response_billing(response_body: Any) -> NormalizedBilling:
    observed = False
    usage_invalid = False
    tier_invalid = False
    input_tokens = output_tokens = cache_creation = cache_read = 0
    service_tier: str | None = None
    actual_ticks: int | None = None

    for obj in _strict_response_objects(response_body):
        for candidate, allow_actual_cost in _billing_candidates(obj):
            tier = candidate.get("service_tier")
            if isinstance(tier, str) and tier.strip():
                service_tier = tier.strip().lower()
            elif "service_tier" in candidate and tier is not None:
                tier_invalid = True
            if "usage" not in candidate:
                continue
            usage = candidate.get("usage")
            if not isinstance(usage, Mapping):
                usage_invalid = True
                continue
            token_fields = {
                "input_tokens", "prompt_tokens", "output_tokens",
                "completion_tokens", "cache_read_input_tokens",
                "cache_read_tokens", "cache_creation_input_tokens",
                "cache_creation_tokens",
            }
            has_token_fields = any(field in usage for field in token_fields)
            if has_token_fields:
                valid = True
                next_input = input_tokens
                next_output = output_tokens
                next_cache_creation = cache_creation
                next_cache_read = cache_read

                has_prompt = "input_tokens" in usage or "prompt_tokens" in usage
                details_obj = None
                details_present = False
                if "input_tokens_details" in usage:
                    details_present = True
                    details_obj = usage.get("input_tokens_details")
                elif "prompt_tokens_details" in usage:
                    details_present = True
                    details_obj = usage.get("prompt_tokens_details")
                if details_present and not isinstance(details_obj, Mapping):
                    valid = False

                cached_from_details = 0
                if isinstance(details_obj, Mapping) and "cached_tokens" in details_obj:
                    parsed = _strict_nonnegative_int(details_obj.get("cached_tokens"))
                    if parsed is None:
                        valid = False
                    else:
                        cached_from_details = parsed

                cache_read_present = bool(
                    "cache_read_input_tokens" in usage
                    or "cache_read_tokens" in usage
                    or (isinstance(details_obj, Mapping) and "cached_tokens" in details_obj)
                    or has_prompt
                )
                if "cache_read_input_tokens" in usage:
                    parsed_cache_read = _strict_nonnegative_int(
                        usage.get("cache_read_input_tokens")
                    )
                elif "cache_read_tokens" in usage:
                    parsed_cache_read = _strict_nonnegative_int(usage.get("cache_read_tokens"))
                else:
                    parsed_cache_read = cached_from_details
                if cache_read_present:
                    if parsed_cache_read is None:
                        valid = False
                    else:
                        next_cache_read = parsed_cache_read

                if "cache_creation_input_tokens" in usage:
                    parsed_cache_creation = _strict_nonnegative_int(
                        usage.get("cache_creation_input_tokens")
                    )
                elif "cache_creation_tokens" in usage:
                    parsed_cache_creation = _strict_nonnegative_int(
                        usage.get("cache_creation_tokens")
                    )
                else:
                    parsed_cache_creation = cache_creation
                if parsed_cache_creation is None:
                    valid = False
                else:
                    next_cache_creation = parsed_cache_creation

                if has_prompt:
                    prompt = _strict_nonnegative_int(
                        usage.get("input_tokens", usage.get("prompt_tokens"))
                    )
                    if prompt is None:
                        valid = False
                    else:
                        # OpenAI prompt/input totals include cached tokens;
                        # Anthropic exposes cache fields beside uncached input.
                        is_openai_shape = "prompt_tokens" in usage or isinstance(
                            usage.get("input_tokens_details"), Mapping
                        )
                        if is_openai_shape and next_cache_read > prompt:
                            valid = False
                        else:
                            next_input = prompt - next_cache_read if is_openai_shape else prompt

                if "output_tokens" in usage or "completion_tokens" in usage:
                    parsed_output = _strict_nonnegative_int(
                        usage.get("output_tokens", usage.get("completion_tokens"))
                    )
                    if parsed_output is None:
                        valid = False
                    else:
                        next_output = parsed_output

                if valid:
                    input_tokens = next_input
                    output_tokens = next_output
                    cache_creation = next_cache_creation
                    cache_read = next_cache_read
                    observed = True
                    usage_invalid = False
                else:
                    usage_invalid = True
            if allow_actual_cost and "cost_in_usd_ticks" in usage:
                value = _strict_nonnegative_int(usage.get("cost_in_usd_ticks"))
                if value is not None:
                    actual_ticks = value
                else:
                    usage_invalid = True

    return NormalizedBilling(
        usage_observed=bool(observed and not usage_invalid and not tier_invalid),
        usage_invalid=bool(usage_invalid or tier_invalid),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_tokens=cache_creation,
        cache_read_tokens=cache_read,
        service_tier=service_tier,
        actual_cost_ticks=actual_ticks,
    )


def preserve_billing_evidence_tail(
    response_text: str,
    *,
    usage: Mapping[str, Any] | None,
    usage_observed: bool,
    service_tier: str | None,
    actual_cost_ticks: int | None,
    event_type: str | None,
    max_chars: int = 200_000,
) -> str:
    """Bound a WS transcript without discarding already-parsed billing facts.

    A single terminal frame can exceed the log-body limit. Blindly keeping its
    final characters may remove ``service_tier`` or xAI's official cost field
    from the beginning of that same JSON object. Append a compact, explicitly
    marked protocol-shaped evidence line only when truncation is necessary.
    """

    raw = str(response_text or "")
    limit = max(1, int(max_chars))
    if len(raw) <= limit:
        return raw

    response: dict[str, Any] = {}
    tier = str(service_tier or "").strip().lower()
    if tier:
        response["service_tier"] = tier
    usage_obj: dict[str, Any] = {}
    if usage_observed and isinstance(usage, Mapping):
        uncached = _strict_nonnegative_int(usage.get("input_tokens")) or 0
        cached = _strict_nonnegative_int(
            usage.get("cache_read", usage.get("cache_read_tokens"))
        ) or 0
        usage_obj.update({
            "input_tokens": uncached + cached,
            "output_tokens": _strict_nonnegative_int(usage.get("output_tokens")) or 0,
            "input_tokens_details": {"cached_tokens": cached},
            "cache_creation_tokens": _strict_nonnegative_int(
                usage.get("cache_creation", usage.get("cache_creation_tokens"))
            ) or 0,
        })
    actual = _strict_nonnegative_int(actual_cost_ticks)
    if actual is not None:
        usage_obj["cost_in_usd_ticks"] = actual
    if usage_obj:
        response["usage"] = usage_obj

    if response:
        typ = str(event_type or "response.in_progress").strip().lower()
        if typ not in {
            "response.completed", "response.failed", "response.incomplete",
            "response.in_progress",
        }:
            typ = "response.in_progress"
        evidence = json.dumps({
            "type": typ,
            "response": response,
            "_parrot_truncated_billing_evidence": True,
        }, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        if len(evidence) + 1 < limit:
            return raw[-(limit - len(evidence) - 1):] + "\n" + evidence
    return raw[-limit:]


def resolved_pricing_snapshot(
    model: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
    priority: bool = False,
    long_context: bool | None = None,
    pricing_settings: PricingSettings | None = None,
) -> tuple[str | None, str | None]:
    """Freeze the exact tariff inputs used by one immutable settlement."""
    settled = estimate_cost_with_snapshot(
        model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_tokens=cache_creation_tokens,
        cache_read_tokens=cache_read_tokens,
        priority=priority,
        long_context=long_context,
        pricing_settings=pricing_settings,
    )
    return (settled[1], settled[2]) if settled is not None else (None, None)


def extract_actual_cost_ticks(response_body: Any) -> int | None:
    """Extract official cost only from strict JSON/SSE usage paths."""

    return normalize_response_billing(response_body).actual_cost_ticks


async def _download_catalog_bounded(client: Any, url: str, budget: int) -> bytes:
    """Stream one source under the remaining shared uncompressed byte budget."""
    if budget < 0:
        raise ValueError("negative catalog download budget")
    async with client.stream("GET", url, timeout=20.0) as response:
        response.raise_for_status()
        raw_length = response.headers.get("content-length")
        if raw_length is not None:
            value = str(raw_length).strip()
            if not value or not value.isascii() or not value.isdecimal():
                raise ValueError(f"invalid Content-Length for {url}")
            declared = int(value)
            if declared > budget:
                raise ValueError(
                    f"models.dev catalogs exceed {_MAX_REMOTE_CATALOG_BYTES} bytes"
                )
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            if not isinstance(chunk, (bytes, bytearray)):
                raise ValueError("catalog stream produced non-bytes data")
            if not chunk:
                continue
            total += len(chunk)
            if total > budget:
                raise ValueError(
                    f"models.dev catalogs exceed {_MAX_REMOTE_CATALOG_BYTES} bytes"
                )
            chunks.append(bytes(chunk))
        # Content-Encoding can make decoded aiter_bytes() length differ from the
        # wire Content-Length. The header is an early upper-bound check; the
        # decoded stream is independently bounded above.
        return b"".join(chunks)


async def refresh_once(*, force: bool = False, client: Any = None) -> bool:
    """Refresh both models.dev catalogs and atomically replace one gzip bundle.

    ``force`` is reserved for the user-triggered metadata sync action.  It
    bypasses the background ``autoUpdate`` switch because clicking that action
    is an explicit request to refresh the local catalog first.
    """

    pricing_cfg = config.get().get("pricing", {})
    if not isinstance(pricing_cfg, Mapping):
        pricing_cfg = {}
    if not force and (
        not pricing_cfg.get("enabled", True)
        or not pricing_cfg.get("autoUpdate", True)
    ):
        return False
    url = str(pricing_cfg.get("sourceUrl") or _DEFAULT_SOURCE_URL).strip()
    models_url = str(pricing_cfg.get("modelsUrl") or _DEFAULT_MODELS_URL).strip()
    if not url.startswith("https://") or not models_url.startswith("https://"):
        raise ValueError("pricing.sourceUrl and pricing.modelsUrl must use https://")
    if client is None:
        from . import upstream
        client = upstream.get_client()
    # Sequential streaming naturally closes a failed/oversized response before
    # the peer starts, and the second source receives only the shared remainder.
    raw_api = await _download_catalog_bounded(
        client, url, _MAX_REMOTE_CATALOG_BYTES,
    )
    raw_models = await _download_catalog_bounded(
        client, models_url, _MAX_REMOTE_CATALOG_BYTES - len(raw_api),
    )
    cache_path = os.path.join(config.DATA_DIR, _CACHE_FILENAME)

    def parse_and_store() -> tuple[
        dict[str, PricingEntry], dict[str, str], set[str],
        dict[str, dict[str, Any]], dict[str, str], dict[str, str], str,
    ]:
        api_payload = json.loads(raw_api)
        models_payload = json.loads(raw_models)
        parsed_catalog, aliases, providers = _parse_models_dev_catalog(
            api_payload, models_payload
        )
        metadata_models, provider_names, official_models = (
            _parse_models_dev_metadata_indexes(api_payload, models_payload)
        )
        # 防止上游异常页或被截断的小对象覆盖可用缓存。
        if len(parsed_catalog) < 500 or len(models_payload) < 100:
            raise ValueError(
                f"pricing catalog unexpectedly small: {len(parsed_catalog)}"
            )
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            prefix=f".{os.path.basename(cache_path)}.",
            suffix=".tmp",
            dir=os.path.dirname(cache_path),
        )
        try:
            bundle = json.dumps(
                {"schema": 1, "api": api_payload, "models": models_payload},
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                fd = -1
                with gzip.GzipFile(
                    fileobj=handle, mode="wb", compresslevel=9, mtime=0
                ) as zipped:
                    zipped.write(bundle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, cache_path)
        finally:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except OSError:
                pass
        revision = _catalog_revision_for_payloads(api_payload, models_payload)
        return (
            parsed_catalog, aliases, providers, metadata_models,
            provider_names, official_models, revision,
        )

    (
        parsed, aliases, providers, metadata_models,
        provider_names, official_models, revision,
    ) = await asyncio.to_thread(parse_and_store)
    global _catalog, _catalog_aliases, _catalog_providers, _catalog_models
    global _catalog_provider_names, _canonical_official_models
    global _catalog_source, _catalog_revision, _initialized
    with _lock:
        _catalog = parsed
        _catalog_aliases = aliases
        _catalog_providers = providers
        _catalog_models = metadata_models
        _catalog_provider_names = provider_names
        _canonical_official_models = official_models
        _catalog_source = "remote"
        _catalog_revision = revision
        _initialized = True
    return True


def refresh_remote_catalog_sync() -> bool:
    """Fetch models.dev with a loop-local client for a synchronous worker."""

    async def _run() -> bool:
        from . import network

        client = network.async_client(timeout=20.0, http2=False)
        try:
            return await refresh_once(force=True, client=client)
        finally:
            await client.aclose()

    return asyncio.run(_run())


async def _auto_sync_startup_metadata(catalog_source: str) -> dict[str, Any]:
    """Bind visible models from the current catalog without blocking startup."""

    from . import model_metadata

    result = await asyncio.to_thread(model_metadata.auto_sync_metadata)
    print(
        f"[Metadata] startup sync ({catalog_source}): "
        f"scanned={int(result.get('scanned') or 0)} "
        f"created={len(result.get('created') or [])} "
        f"updated={len(result.get('updated') or [])} "
        f"unchanged={len(result.get('unchanged') or [])} "
        f"unmatched={len(result.get('unmatched') or [])}"
    )
    return result


async def startup_metadata_sync() -> None:
    """One non-blocking startup workflow: local bindings, refresh, then reconcile."""

    try:
        await _auto_sync_startup_metadata("local")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        print(f"[Metadata] startup local sync failed: {exc}")

    refreshed = False
    try:
        refreshed = await refresh_once()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        print(f"[Pricing] refresh failed, keeping {_catalog_source} catalog: {exc}")

    if refreshed:
        try:
            await _auto_sync_startup_metadata("remote")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[Metadata] startup remote sync failed: {exc}")


async def refresh_loop() -> None:
    """后台更新循环。启动同步和周期刷新共用这一个任务。"""

    first_iteration = True
    while True:
        if first_iteration:
            await startup_metadata_sync()
            first_iteration = False
        else:
            try:
                await refresh_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[Pricing] refresh failed, keeping {_catalog_source} catalog: {exc}")
        pricing_cfg = config.get().get("pricing", {})
        raw_hours = pricing_cfg.get("refreshHours", 24) if isinstance(pricing_cfg, Mapping) else 24
        try:
            parsed_hours = float(raw_hours)
            hours = max(1.0, parsed_hours) if math.isfinite(parsed_hours) else 24.0
        except (TypeError, ValueError):
            hours = 24.0
        await asyncio.sleep(hours * 3600)


def catalog_status() -> dict[str, Any]:
    with _lock:
        return {
            "source": _catalog_source,
            "revision": _catalog_revision,
            "models": len(_catalog),
            "metadata_models": len(_catalog_models),
            "aliases": len(_catalog_aliases),
            "providers": len(_catalog_provider_names),
            "canonical_official": len(_canonical_official_models),
        }
