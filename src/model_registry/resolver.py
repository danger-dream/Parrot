"""Resolve a requested model name and choose each source's upstream id for it."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Mapping

from .names import EFFORTS, fold, nearest_effort, suffix_traits
from .offers import Offer
from .pipeline import Registry

DEFAULT_EFFORT = "high"


@dataclass(frozen=True)
class Resolution:
    model: str
    traits: Mapping[str, str]
    via: str             # id | alias | shortcut


@dataclass(frozen=True)
class Choice:
    offer: Offer
    effort: str | None
    thinking: str | None
    explicit: bool = False   # the level came from the name or the body, not a default


def resolve(registry: Registry, name: str) -> Resolution | None:
    """Exact ids and aliases win; only then is a trailing ``-high`` / ``-thinking`` read as a shortcut."""
    name = str(name or "").strip()
    found = registry.lookup(name)
    if found:
        return Resolution(found[0], dict(found[1]), "id" if name == found[0] else "alias")
    base, traits = suffix_traits(name)
    if not traits or base == fold(name):
        return None
    found = registry.lookup(base)
    if not found:
        return None
    fixed = dict(found[1])
    if any(fixed.get(kind, value) != value for kind, value in traits.items()):
        return None
    return Resolution(found[0], {**fixed, **traits}, "shortcut")


def _fixed_thinking(offer: Offer) -> str | None:
    """Whether this upstream id always or never thinks; an id fixed to an effort level thinks."""
    if "thinking" in offer.traits:
        return offer.traits["thinking"]
    if offer.traits.get("effort", "none") != "none" or offer.thinking == "always":
        return "on"
    return None


def choose(registry: Registry, resolution: Resolution, usable: Callable[[Offer], bool], *,
           effort: str | None = None, thinking: str | None = None,
           default_effort: str | None = None) -> list[Choice]:
    """One choice per source that can serve the request, in registry order.

    A name's traits and the body's parameters must already agree (the caller
    rejects a conflict). Without any effort, ``default_effort`` - the model's
    own default or the global ``high`` - is fitted to each source's levels.
    """
    explicit = resolution.traits.get("effort") or effort
    wanted = explicit or default_effort or DEFAULT_EFFORT
    think = resolution.traits.get("thinking") or thinking
    by_source: OrderedDict[str, list[Offer]] = OrderedDict()
    for offer in registry.models[resolution.model].offers:
        if usable(offer):
            by_source.setdefault(offer.source, []).append(offer)
    result = []
    for offers in by_source.values():
        choice = _choose_in_source(offers, wanted, bool(explicit), think, named=bool(resolution.traits.get("thinking")))
        if choice is not None:
            result.append(choice)
    return result


def _choose_in_source(offers: list[Offer], wanted: str, explicit: bool, think: str | None,
                      named: bool = False) -> Choice | None:
    if think == "off":
        can_stop = [offer for offer in offers if _fixed_thinking(offer) != "on"]
        # A name such as -disabled asks for an id that stops thinking. A body parameter is
        # served by any source, as before the registry; one that always thinks just keeps thinking.
        offers, think = (can_stop, think) if can_stop or named else (offers, None)
    else:
        offers = [offer for offer in offers if _fixed_thinking(offer) != "off"]
    fixed = [offer for offer in offers if "effort" in offer.traits]
    free = [offer for offer in offers if "effort" not in offer.traits]
    exact = next((offer for offer in fixed if offer.traits["effort"] == wanted), None)
    if exact is not None and think != "off":
        return Choice(exact, wanted, think, explicit)
    if free:
        offer = free[0]
        if think == "off":
            level = None
        elif explicit and wanted not in EFFORTS:
            level = wanted
        elif offer.efforts:
            level = nearest_effort(wanted, offer.efforts)
        else:
            level = wanted if explicit else None
        return Choice(offer, level, think, explicit)
    if fixed and not explicit:
        level = nearest_effort(wanted, [offer.traits["effort"] for offer in fixed])
        return next(Choice(offer, level, think) for offer in fixed if offer.traits["effort"] == level)
    return None
