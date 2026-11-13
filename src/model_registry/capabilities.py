"""What /v1/models declares for a model: what a request can rely on whichever source serves it.

Only the sources the API key can be scheduled to count. Failover may land on any
of them, so limits take the smallest value and a capability holds only when
every source says so. A source that declares nothing about a field does not
count for that field; a field no source declares is left out.
"""
from __future__ import annotations

import time
from typing import Any, Iterable, Mapping

from . import runtime
from .names import EFFORTS
from .offers import Offer


def serving_offers(model_id: str, api_key: str | None) -> list[Offer]:
    registry = runtime.current()
    if registry is None:
        from .. import model_names
        from ..channel import registry as channel_registry
        return [
            Offer(source, model_names.channel_provider(source), wire, model_id)
            for source, wire in channel_registry.serving_channels(model_id, api_key_name=api_key)
        ]
    model = registry.models.get(model_id)
    if model is None:
        return []
    from .. import auth
    switches = runtime.Switches.read()
    return [offer for offer in model.offers
            if switches.usable(offer, model_id) and auth.channel_allowed(api_key, offer.source)]


def _metadata(model_id: str, offer: Offer) -> Mapping[str, Any]:
    from .. import model_metadata
    try:
        binding = model_metadata.resolve_binding(
            model_id, scope_key=offer.source, outbound_model=offer.wire,
            fallback_to_outbound=True,
        )
    except Exception:
        return {}
    return dict(binding.metadata) if binding is not None else {}


def _tokens(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _least(values: Iterable[int | None]) -> int | None:
    known = [value for value in values if value is not None]
    return min(known) if known else None


def _clean_modalities(raw: Any) -> list[str]:
    if not isinstance(raw, (list, tuple)):
        return []
    seen: list[str] = []
    for item in raw:
        val = str(item).strip().lower() if isinstance(item, str) else ""
        if val and val not in seen:
            seen.append(val)
    return seen


def _source_modalities(md: Mapping[str, Any]) -> tuple[list[str] | None, list[str] | None]:
    nested = md.get("modalities") if isinstance(md.get("modalities"), Mapping) else {}
    in_mods = _clean_modalities(md.get("inputModalities") or nested.get("input"))
    out_mods = _clean_modalities(md.get("outputModalities") or nested.get("output"))
    vision = md.get("vision") if isinstance(md.get("vision"), bool) else None
    if in_mods:
        if vision is True and "image" not in in_mods:
            in_mods = [*in_mods, "image"]
        elif vision is False:
            in_mods = [m for m in in_mods if m != "image"] or ["text"]
    elif vision is not None:
        in_mods = ["text", "image"] if vision else ["text"]
    if in_mods and not out_mods:
        out_mods = ["text"]
    return (in_mods or None, out_mods or None)


def _intersect_modalities(groups: list[list[str]]) -> list[str]:
    if not groups:
        return []
    common = set(groups[0]).intersection(*groups[1:])
    return [item for item in groups[0] if item in common]


# Config changes rebuild the registry or replace the active config snapshot,
# dropping this cache; the age limit covers channels failing at runtime.
_CACHE_SECONDS = 60
_cache: tuple[Any, dict[tuple[str, str | None], tuple[float, dict[str, Any]]]] = (None, {})


def describe(model_id: str, api_key: str | None) -> dict[str, Any]:
    global _cache
    from .. import config
    cache_owner = (runtime.current(), id(config.get()))
    owner, entries = _cache
    if owner != cache_owner:
        entries = {}
        _cache = (cache_owner, entries)
    now = time.monotonic()
    cached = entries.get((model_id, api_key))
    if cached is not None and now - cached[0] < _CACHE_SECONDS:
        return dict(cached[1])
    result = _describe(model_id, api_key)
    entries[(model_id, api_key)] = (now, result)
    return dict(result)


def _describe(model_id: str, api_key: str | None) -> dict[str, Any]:
    by_source: dict[str, list[Offer]] = {}
    for offer in serving_offers(model_id, api_key):
        by_source.setdefault(offer.source, []).append(offer)
    sources = []
    for offers in by_source.values():
        metadata = _metadata(model_id, offers[0])
        fixed = {offer.traits["effort"] for offer in offers if offer.traits.get("effort") in EFFORTS}
        # An id fixed to one level offers only that level, whatever the catalog lists.
        efforts = fixed or {item for item in metadata.get("reasoningEfforts") or () if item in EFFORTS}
        sources.append((metadata, efforts))
    if not sources:
        return {}
    window = _least(_tokens(md.get("contextWindow")) or _tokens(md.get("maxInputTokens")) for md, _ in sources)
    result: dict[str, Any] = {
        "context_window": window,
        "context_length": window,
        "max_input_tokens": _least(_tokens(md.get("maxInputTokens")) or _tokens(md.get("contextWindow"))
                                   for md, _ in sources),
        "max_output_tokens": _least(_tokens(md.get("maxOutputTokens")) for md, _ in sources),
    }
    in_groups, out_groups = [], []
    for md, _ in sources:
        in_mods, out_mods = _source_modalities(md)
        if in_mods:
            in_groups.append(in_mods)
        if out_mods:
            out_groups.append(out_mods)
    in_shared = _intersect_modalities(in_groups)
    out_shared = _intersect_modalities(out_groups)
    if in_shared or out_shared:
        arch: dict[str, Any] = {}
        if in_shared:
            arch["input_modalities"] = in_shared
        if out_shared:
            arch["output_modalities"] = out_shared
        result["architecture"] = arch
    capabilities: dict[str, Any] = {}
    for name, key in (("vision", "vision"), ("tool_call", "toolCall"), ("structured_output", "structuredOutput")):
        values = [md[key] for md, _ in sources if isinstance(md.get(key), bool)]
        if values:
            capabilities[name] = all(values)
    levels = [efforts for _, efforts in sources if efforts]
    if levels:
        shared = set.intersection(*levels)
        capabilities["reasoning_efforts"] = [level for level in EFFORTS if level in shared]
    if capabilities:
        result["capabilities"] = capabilities
    return {key: value for key, value in result.items() if value is not None}
