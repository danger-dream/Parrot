"""Directory-key thinking tiers observed on the Hub 2.32.1 generation path."""
from __future__ import annotations

from collections.abc import Iterable, Mapping

from ..openai.transform.guard import GuardError
from . import argon
from .catalog import thinking_level

TIERS = ("minimal", "low", "medium", "high", "xhigh")

FAMILIES = {
    "gemini-4-carbon": ("low", "medium", "high"),
    "gemini-4-hydrogen": ("low", "medium", "high", "xhigh"),
    "gemini-4-neon": ("low", "medium", "high", "xhigh"),
}


def _ordered_tiers(tiers: Iterable[str]) -> tuple[str, ...]:
    found = set(tiers)
    return tuple(tier for tier in TIERS if tier in found)


def _split_variant(model: str) -> tuple[str, str] | None:
    if not isinstance(model, str):
        return None
    lowered = model.lower()
    if not lowered.startswith("gemini-") or argon.is_argon(model):
        return None
    for tier in TIERS:
        suffix = f"-{tier}"
        if model.endswith(suffix):
            base = model[:-len(suffix)]
            if base and not argon.is_argon(base):
                return base, tier
    return None


def catalog_families(catalog_models: Mapping[str, Mapping[str, object]] | None) -> dict[str, tuple[str, ...]]:
    """Discover Gemini <base>-<tier> families declared with thinkingLevel in the active catalog."""
    if not isinstance(catalog_models, Mapping):
        return {}
    grouped: dict[str, set[str]] = {}
    for model_id, meta in catalog_models.items():
        if not isinstance(meta, Mapping) or thinking_level(meta.get("thinkingLevel")) is None:
            continue
        split = _split_variant(model_id)
        if split is None:
            continue
        base, tier = split
        grouped.setdefault(base, set()).add(tier)
    return {base: _ordered_tiers(tiers) for base, tiers in grouped.items()}


def _effective_families(
    catalog_models: Mapping[str, Mapping[str, object]] | None = None,
    *,
    model: str | None = None,
    metadata: Mapping[str, object] | None = None,
) -> dict[str, tuple[str, ...]]:
    if not catalog_models and not (model and metadata):
        return FAMILIES
    merged: dict[str, set[str]] = {base: set(tiers) for base, tiers in FAMILIES.items()}
    for base, tiers in catalog_families(catalog_models).items():
        merged.setdefault(base, set()).update(tiers)
    if model and isinstance(metadata, Mapping) and thinking_level(metadata.get("thinkingLevel")) is not None:
        split = _split_variant(model)
        if split is not None:
            merged.setdefault(split[0], set()).add(split[1])
    return {base: _ordered_tiers(tiers) for base, tiers in merged.items()}


def family(
    model: str,
    *,
    catalog_models: Mapping[str, Mapping[str, object]] | None = None,
    metadata: Mapping[str, object] | None = None,
) -> str | None:
    families = _effective_families(catalog_models, model=model, metadata=metadata)
    return next((base for base, tiers in families.items()
                 if model == base or model in {f"{base}-{tier}" for tier in tiers}), None)


def _fail(message: str) -> None:
    raise GuardError(
        400, "invalid_request_error", message,
        param="reasoning", scope="candidate", code="antigravity_thinking_unsupported",
    )


def intent(
    model: str,
    body: dict,
    ingress: str,
    *,
    catalog_models: Mapping[str, Mapping[str, object]] | None = None,
    metadata: Mapping[str, object] | None = None,
) -> tuple[str | None, bool]:
    families = _effective_families(catalog_models, model=model, metadata=metadata)
    base = next((b for b, tiers in families.items()
                 if model == b or model in {f"{b}-{tier}" for tier in tiers}), None)
    if base is None:
        _fail("Unknown Antigravity fixed-thinking model")
    summary = None
    if ingress == "anthropic":
        thinking = body.get("thinking")
        if thinking is not None and not isinstance(thinking, dict):
            _fail("thinking must be an object")
        thinking = thinking or {}
        if thinking.get("budget_tokens") is not None:
            _fail(f"{base} uses directory-key tiers, not numeric thinking budgets")
        mode = thinking.get("type")
        if mode not in (None, "enabled", "adaptive", "disabled"):
            _fail("Unsupported thinking type")
        if mode == "disabled":
            _fail(f"{base} fixed-thinking tiers cannot promise disabled reasoning")
        output = body.get("output_config") or {}
        if not isinstance(output, dict):
            _fail("output_config must be an object")
        effort = output.get("effort")
    elif ingress == "chat":
        effort, summary = body.get("reasoning_effort"), body.get("reasoning_summary")
    else:
        reasoning = body.get("reasoning")
        if reasoning is not None and not isinstance(reasoning, dict):
            _fail("reasoning must be an object")
        reasoning = reasoning or {}
        if reasoning.get("budget_tokens") is not None:
            _fail(f"{base} uses directory-key tiers, not numeric thinking budgets")
        effort, summary = reasoning.get("effort"), reasoning.get("summary")
    allowed = families[base]
    if effort is not None and (not isinstance(effort, str) or effort not in allowed):
        _fail(f"{base} supports only explicit {'/'.join(allowed)} effort; no fallback is performed")
    if summary is not None and (not isinstance(summary, str) or summary not in {"auto", "concise", "detailed", "none"}):
        _fail("reasoning summary must be auto, concise, detailed or none")
    return effort, summary != "none"


def resolve(
    model: str,
    selected: list[str],
    body: dict,
    ingress: str,
    *,
    allow_tier_alias: bool = True,
    catalog_models: Mapping[str, Mapping[str, object]] | None = None,
) -> str | None:
    if not allow_tier_alias and model not in selected:
        return None
    base = family(model, catalog_models=catalog_models)
    if base is None:
        return model if model in selected else None
    if not any(family(item, catalog_models=catalog_models) == base for item in selected):
        return None
    effort, _ = intent(model, body, ingress, catalog_models=catalog_models)
    if model != base:
        if effort is not None and model != f"{base}-{effort}":
            _fail(f"Model {model} conflicts with effort {effort}")
        return model if model in selected else None
    if not allow_tier_alias:
        return model
    if effort is None:
        if base in selected:
            return base
        _fail(f"Bare {base} requires an explicit effort or an exact selected variant")
    target = f"{base}-{effort}"
    if target not in selected:
        _fail(f"Variant {target} is not selected, enabled and available at this account's generation endpoint")
    return target


def thinking_config(
    model: str,
    body: dict,
    ingress: str = "responses",
    *,
    metadata: dict | None = None,
    catalog_models: Mapping[str, Mapping[str, object]] | None = None,
) -> dict:
    effort, summary = intent(model, body, ingress, catalog_models=catalog_models, metadata=metadata)
    base = family(model, catalog_models=catalog_models, metadata=metadata)
    if model != base and effort is not None and model != f"{base}-{effort}":
        _fail(f"Model {model} conflicts with effort {effort}")
    result = {"includeThoughts": summary}
    level = thinking_level((metadata or {}).get("thinkingLevel"))
    if level is not None:
        result["thinkingLevel"] = level
    return result
