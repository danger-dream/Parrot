"""What /v1/models declares for a model: what a request can rely on whichever source serves it.

Only the sources the API key can be scheduled to count. Failover may land on any
of them, so limits take the smallest value and a capability holds only when
every source says so. A source that declares nothing about a field does not
count for that field; a field no source declares is left out.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from . import runtime
from .names import EFFORTS
from .offers import Offer


def serving_offers(model_id: str, api_key: str | None) -> list[Offer]:
    registry = runtime.current()
    model = registry.models.get(model_id) if registry else None
    if model is None:
        return []
    from .. import auth
    switches = runtime.Switches.read()
    return [offer for offer in model.offers
            if switches.usable(offer, model_id) and auth.channel_allowed(api_key, offer.source)]


def _metadata(model_id: str, offer: Offer) -> Mapping[str, Any]:
    from .. import model_metadata
    try:
        binding = model_metadata.resolve_binding(model_id, scope_key=offer.source, outbound_model=offer.wire)
    except Exception:
        return {}
    return dict(binding.metadata) if binding is not None else {}


def _tokens(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _least(values: Iterable[int | None]) -> int | None:
    known = [value for value in values if value is not None]
    return min(known) if known else None


def describe(model_id: str, api_key: str | None) -> dict[str, Any]:
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
