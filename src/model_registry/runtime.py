"""The live registry: rebuilt with the channels; routing, listing and switches read it.

Inactive unless ``modelRegistry.enabled`` is true; callers then keep the
per-channel naming path.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .names import compare_key
from .offers import Offer, collect, with_legacy
from .pipeline import CatalogIndex, Registry, build
from .resolver import DEFAULT_EFFORT, Choice, choose, resolve

MIGRATION_VERSION = 1

_lock = threading.Lock()
_registry: Registry | None = None
_owners: dict[tuple[str, str], str] = {}
_catalog: tuple[str, CatalogIndex] | None = None


def settings(cfg: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    from .. import config
    root = (cfg if cfg is not None else config.get()).get("modelRegistry")
    return root if isinstance(root, Mapping) else {}


def enabled(cfg: Mapping[str, Any] | None = None) -> bool:
    return settings(cfg).get("enabled") is True


def _catalog_index() -> CatalogIndex:
    global _catalog
    from .. import model_pricing
    revision = str(model_pricing.catalog_status().get("revision") or "")
    if _catalog is None or _catalog[0] != revision:
        _catalog = (revision, CatalogIndex())
    return _catalog[1]


def compute(cfg: Mapping[str, Any], *, legacy: Mapping[tuple[str, str], str] | None = None) -> Registry:
    root = settings(cfg)
    offers = collect(cfg)
    if legacy is None:
        # Undecided offers keep the names they had when the registry was switched on.
        saved = root.get("pendingNames") or {}
        legacy = {tuple(key.split("|", 1)): name for key, name in saved.items() if "|" in key}
    if legacy:
        offers = with_legacy(offers, legacy)
    return build(offers, rules=root.get("rules") or (), catalog=_catalog_index(),
                 known=(root.get("models") or {}).keys(), aliases=root.get("aliases") or {})


def rebuild(cfg: Mapping[str, Any] | None = None) -> None:
    global _registry, _owners
    from .. import config
    cfg = cfg if cfg is not None else config.get()
    registry = compute(cfg) if enabled(cfg) else None
    owners = {} if registry is None else {
        (offer.source, offer.wire): model.id for model in registry.models.values() for offer in model.offers}
    with _lock:
        _registry, _owners = registry, owners


def current() -> Registry | None:
    return _registry


def model_of(source: str, wire: str) -> str | None:
    return _owners.get((source, wire))


def wires_of(source: str, model_id: str) -> list[str]:
    registry = _registry
    model = registry.models.get(model_id) if registry else None
    return [] if model is None else [offer.wire for offer in model.offers if offer.source == source]


# ─── switches ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Switches:
    """Model-center state read by model spelling: ``MiniMax-M3`` and ``minimax-m3`` are one switch."""
    disabled: frozenset[str]
    hidden: frozenset[str]
    exceptions: Mapping[str, frozenset[str]]
    api_disabled: Mapping[str, frozenset[str]]

    @classmethod
    def read(cls, cfg: Mapping[str, Any] | None = None) -> "Switches":
        from .. import model_state
        state = model_state.state_snapshot(cfg)
        keys = lambda values: frozenset(compare_key(value) for value in values)
        return cls(keys(state["disabledModels"]), keys(state["hiddenModels"]),
                   {source: keys(values) for source, values in state["sourceEnabledModels"].items()},
                   {source: keys(values) for source, values in state["apiSourceDisabledModels"].items()})

    def model_disabled(self, model_id: str) -> bool:
        return compare_key(model_id) in self.disabled

    def hidden_model(self, model_id: str) -> bool:
        return compare_key(model_id) in self.hidden

    def usable(self, offer: Offer, model_id: str) -> bool:
        from ..channel import registry as channels
        channel = channels.get_channel(offer.source)
        if channel is None or not channel.enabled or channel.disabled_reason or not offer.available:
            return False
        key = compare_key(model_id)
        api_off = self.api_disabled.get(offer.source)
        if api_off and api_off & {key, compare_key(offer.name), compare_key(offer.wire)}:
            return False
        return key not in self.disabled or key in self.exceptions.get(offer.source, frozenset())


# ─── requests ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class Reasoning:
    effort: str | None = None        # an explicit level in the body
    thinking: str | None = None      # explicit on/off in the body
    derived: str | None = None       # level implied by Anthropic thinking settings
    controlled: bool = False         # the body carries any reasoning control


def _level(raw: Any) -> str | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    value = raw.strip().lower()
    return "xhigh" if value == "extra-high" else value


def request_reasoning(body: Mapping[str, Any], ingress: str) -> Reasoning:
    if ingress == "chat":
        raw = _level(body.get("reasoning_effort"))
    elif ingress == "responses":
        reasoning = body.get("reasoning")
        raw = _level(reasoning.get("effort")) if isinstance(reasoning, Mapping) else None
    else:
        output = body.get("output_config")
        raw = _level(output.get("effort")) if isinstance(output, Mapping) else None
        thinking = body.get("thinking")
        if raw is None and isinstance(thinking, Mapping):
            kind = str(thinking.get("type") or "").strip().lower()
            if kind == "disabled":
                return Reasoning(thinking="off", controlled=True)
            if kind in ("enabled", "adaptive"):
                from ..openai.transform.common import resolve_anthropic_reasoning_effort
                return Reasoning(thinking="on", derived=resolve_anthropic_reasoning_effort(body), controlled=True)
    if raw == "none":
        return Reasoning(thinking="off", controlled=True)
    return Reasoning(effort=raw, controlled=raw is not None)


@dataclass
class Plan:
    model: str | None
    choices: dict[str, Choice] = field(default_factory=dict)
    reasoning: Reasoning = Reasoning()
    error: str | None = None


def plan(name: str, body: Mapping[str, Any], ingress: str, api_key: str | None) -> Plan | None:
    """Which upstream id, effort and thinking each source would serve ``name`` with; None when inactive."""
    registry = _registry
    if registry is None:
        return None
    from .. import auth
    found = resolve(registry, name)
    if found is None:
        return Plan(None, error=f"unknown model {name!r}")
    reasoning = request_reasoning(body, ingress)
    for kind, wanted in (("effort", reasoning.effort), ("thinking", reasoning.thinking)):
        fixed = found.traits.get(kind)
        if fixed and wanted and fixed != wanted:
            return Plan(found.model, reasoning=reasoning,
                        error=f"model {name!r} fixes {kind}={fixed} but the request asks for {wanted}")
    switches = Switches.read()
    model_settings = (settings().get("models") or {}).get(found.model) or {}
    default = (model_settings.get("defaultEffort") if isinstance(model_settings, Mapping) else None) \
        or reasoning.derived or settings().get("defaultEffort") or DEFAULT_EFFORT
    choices = choose(registry, found,
                     lambda offer: switches.usable(offer, found.model) and auth.channel_allowed(api_key, offer.source),
                     effort=reasoning.effort, thinking=reasoning.thinking, default_effort=default)
    return Plan(found.model, {choice.offer.source: choice for choice in choices}, reasoning)


def serves(channel, name: str) -> str | None:
    """The upstream id ``channel`` serves ``name`` with, by naming alone (switches are not consulted)."""
    registry = _registry
    if registry is None:
        return channel.supports_model(name)
    found = resolve(registry, name)
    if found is None:
        return None
    choices = choose(registry, found, lambda offer: offer.source == channel.key)
    return choices[0].offer.wire if choices else None


def route_on(channel, name: str, body: Mapping[str, Any], ingress: str, api_key: str | None) -> Choice | None:
    """This channel's choice for a request, with every switch applied; registry must be active."""
    routed = plan(name, body, ingress, api_key)
    return None if routed is None or routed.error else routed.choices.get(channel.key)


# Providers refuse or starve thinking below this output cap (Zhipu requires
# max_tokens > 1024 once thinking is on), so a default level must not turn
# thinking on for short requests such as titles.
MIN_DEFAULT_REASONING_OUTPUT = 1024


def _output_cap(body: Mapping[str, Any], ingress: str) -> int | None:
    keys = {"chat": ("max_completion_tokens", "max_tokens"), "responses": ("max_output_tokens",)}.get(ingress, ("max_tokens",))
    for key in keys:
        value = body.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def apply(body: dict, choice: Choice | None, ingress: str) -> dict:
    """The candidate's own body: the chosen level is written only where the request left it open.

    A request that already carries any reasoning control keeps it, except that
    an id fixed to one level must see that level (Antigravity checks it). A
    default level is not written into a request capped to a short output.
    """
    if choice is None:
        return body
    reasoning = request_reasoning(body, ingress)
    fixed = "effort" in choice.offer.traits
    if choice.thinking == "off" and reasoning.thinking != "off" and reasoning.effort is None:
        return _write(body, ingress, None, off=True)
    if not choice.effort or reasoning.effort is not None or choice.thinking == "off":
        return body
    if fixed:
        return _write(body, ingress, choice.effort, off=False)
    if reasoning.controlled:
        return body
    cap = _output_cap(body, ingress)
    if not choice.explicit and cap is not None and cap <= MIN_DEFAULT_REASONING_OUTPUT:
        return body
    return _write(body, ingress, choice.effort, off=False)


def _write(body: dict, ingress: str, effort: str | None, *, off: bool) -> dict:
    result = dict(body)
    if ingress == "chat":
        result["reasoning_effort"] = "none" if off else effort
    elif ingress == "responses":
        result["reasoning"] = {**(body.get("reasoning") if isinstance(body.get("reasoning"), Mapping) else {}),
                               "effort": "none" if off else effort}
    elif off:
        result["thinking"] = {"type": "disabled"}
    else:
        result["output_config"] = {**(body.get("output_config") if isinstance(body.get("output_config"), Mapping) else {}),
                                   "effort": effort}
    return result


# ─── listing and permissions ─────────────────────────────────────────

def permits(allowed: Iterable[str], name: str) -> bool:
    """An API key's model allowlist, matched by model rather than by spelling once active."""
    allowed = [str(item) for item in allowed or () if str(item).strip()]
    if not allowed or name in allowed:
        return True
    registry = _registry
    if registry is None:
        return False
    found = resolve(registry, name)
    return found is not None and any((hit := resolve(registry, item)) and hit.model == found.model for item in allowed)


def visible_models(api_key: str | None, families: set[str] | None = None) -> list[str]:
    """Listed models for a key; ``families`` keeps models some anthropic/openai channel serves."""
    registry = _registry
    if registry is None:
        return []
    from .. import auth
    from ..channel import registry as channels
    allowed = list((auth.api_key_entry(api_key) or {}).get("allowedModels") or [])
    permitted = {hit.model for item in allowed if (hit := resolve(registry, item))} if allowed else None
    switches = Switches.read()

    def family_ok(offer: Offer) -> bool:
        if not families:
            return True
        channel = channels.get_channel(offer.source)
        protocol = getattr(channel, "protocol", "anthropic")
        return ("anthropic" if protocol == "anthropic" else "openai") in families

    return sorted(
        model.id for model in registry.models.values()
        if not switches.hidden_model(model.id) and (permitted is None or model.id in permitted)
        and any(switches.usable(offer, model.id) and auth.channel_allowed(api_key, offer.source) and family_ok(offer)
                for offer in model.offers))


def same_model(name: str, keys: Iterable[str]) -> str | None:
    """The first of ``keys`` naming the same model as ``name`` (per-model settings keyed by any spelling)."""
    registry = _registry
    found = resolve(registry, name) if registry else None
    if found is None:
        return None
    return next((key for key in keys if (hit := resolve(registry, key)) and hit.model == found.model), None)


def display_name(model_id: str) -> str:
    registry = _registry
    model = registry.models.get(model_id) if registry else None
    return model.name if model else model_id


def listed(name: str, api_key: str | None) -> bool:
    registry = _registry
    found = resolve(registry, name) if registry else None
    return found is not None and found.model in visible_models(api_key)


# ─── one-time switch migration ───────────────────────────────────────

def migrate_if_needed() -> list[str] | None:
    """Re-key today's switches by model, once, when the registry is first enabled.

    Runs in the new code at startup: an old process would read the re-keyed
    switches with per-spelling semantics.
    """
    from .. import config
    from .report import legacy_names
    cfg = config.get()
    if not enabled(cfg) or settings(cfg).get("migrated") == MIGRATION_VERSION:
        return None
    registry = compute(cfg, legacy=legacy_names())
    notes: list[str] = []
    config.update(lambda current: migrate_config(current, registry, notes))
    return notes


def migrate_config(cfg: dict, registry: Registry, notes: list[str]) -> None:
    root = cfg.setdefault("modelRegistry", {})
    models = root.setdefault("models", {})
    for model_id in registry.models:
        models.setdefault(model_id, {})
    aliases = root.setdefault("aliases", {})
    pending = root.setdefault("pendingNames", {})
    for model in registry.models.values():
        for placement in model.placements:
            offer = placement.offer
            if placement.status == "pending" and offer.legacy:
                pending.setdefault(f"{offer.source}|{offer.wire}", offer.legacy)
            if offer.legacy and compare_key(offer.legacy) != compare_key(model.id):
                aliases.setdefault(offer.legacy, {"model": model.id, "traits": dict(offer.traits)}
                                   if offer.traits else model.id)

    center = cfg.setdefault("modelCenter", {})
    disabled: set[str] = set()
    for entry in center.get("disabledModels") or ():
        found = registry.lookup(entry)
        if found is None:
            disabled.add(entry)
        elif not found[1] and compare_key(entry) == compare_key(found[0]):
            disabled.add(found[0])
        else:
            for offer in registry.models[found[0]].offers:
                if offer.legacy == entry:
                    _disable_offer(cfg, offer)
                    notes.append(f"全局停用的旧名 {entry} 改为只停用 {offer.source} 的 {offer.wire}")
    center["disabledModels"] = sorted(disabled)
    hidden: set[str] = set()
    for entry in center.get("hiddenModels") or ():
        found = registry.lookup(entry)
        if found is None or (not found[1] and compare_key(entry) == compare_key(found[0])):
            hidden.add(found[0] if found else entry)
        else:
            notes.append(f"隐藏的旧名 {entry} 不再单独生效（它属于 {found[0]}）")
    center["hiddenModels"] = sorted(hidden)
    exceptions = center.get("sourceEnabledModels") or {}
    for source, values in list(exceptions.items()):
        exceptions[source] = sorted({(found[0] if (found := registry.lookup(value)) else value) for value in values})
    root["migrated"] = MIGRATION_VERSION
    root["migratedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _disable_offer(cfg: dict, offer: Offer) -> None:
    if offer.source.startswith("api:"):
        center = cfg.setdefault("modelCenter", {})
        values = center.setdefault("apiSourceDisabledModels", {}).setdefault(offer.source, [])
        if offer.name not in values:
            values.append(offer.name)
        return
    from ..oauth_ids import account_key
    for account in cfg.get("oauthAccounts") or ():
        if not isinstance(account, dict):
            continue
        try:
            if "oauth:" + account_key(account) != offer.source:
                continue
        except (KeyError, ValueError, TypeError):
            continue
        field_name = "cursor_disabled_models" if offer.provider == "cursor" else "disabledModels"
        values = list(account.get(field_name) or [])
        if offer.wire not in values:
            account[field_name] = sorted(values + [offer.wire])
        return
