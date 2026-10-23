"""The live registry: rebuilt with the channels; routing, listing and switches read it.

Inactive unless ``modelRegistry.enabled`` is true; callers then keep the
per-channel naming path.
"""
from __future__ import annotations

import copy
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .names import compare_key
from .offers import Offer, collect, with_legacy
from .pipeline import CatalogIndex, Registry, build
from .resolver import DEFAULT_EFFORT, Choice, choose, resolve

MIGRATION_VERSION = 2   # 1: switches re-keyed by model; 2: metadata also keyed by model

_lock = threading.Lock()
_registry: Registry | None = None
_failure: str | None = None
_bad_rules: set[str] = set()
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


NAMING_BUTTON = {"inline_keyboard": [[{"text": "🧩 统一命名", "callback_data": "nt:open:naming"}]]}


def alert(text: str) -> None:
    from .. import notifier
    print(f"[model-registry] {text}")
    notifier.notify_event("model_registry", "🧩 <b>模型命名</b>\n\n" + notifier.escape_html(text), reply_markup=NAMING_BUTTON)


def _valid_rules(rules: Iterable[Any]) -> list[dict]:
    """Each malformed rule is skipped on its own (and reported once) instead of failing the whole build."""
    kept = []
    for rule in rules or ():
        try:
            kept += normalize_rules([rule])
        except ValueError:
            text = repr(rule)[:200]
            if text not in _bad_rules:
                _bad_rules.add(text)
                alert(f"忽略一条格式不对的人工规则：{text}")
    return kept


def compute(cfg: Mapping[str, Any], *, legacy: Mapping[tuple[str, str], str] | None = None) -> Registry:
    root = settings(cfg)
    offers = collect(cfg)
    if legacy is None:
        # Undecided offers keep the names they had when the registry was switched on.
        saved = root.get("pendingNames") or {}
        legacy = {tuple(key.split("|", 1)): name for key, name in saved.items() if "|" in key}
    if legacy:
        offers = with_legacy(offers, legacy)
    return build(offers, rules=_valid_rules(root.get("rules")), catalog=_catalog_index(),
                 known=(root.get("models") or {}).keys(), aliases=root.get("aliases") or {})


def rebuild(cfg: Mapping[str, Any] | None = None) -> None:
    """Raises only when no registry was ever built; otherwise a failed build keeps the last good one."""
    global _registry, _owners, _failure
    from .. import config
    cfg = cfg if cfg is not None else config.get()
    try:
        registry = compute(cfg) if enabled(cfg) else None
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        if message != _failure:
            _failure = message
            alert("统一模型名重建失败，" + ("继续使用上一份可用的注册表" if _registry is not None
                                        else "暂时退回按渠道命名") + f"：{message}")
        if _registry is not None:
            return
        raise
    _failure = None
    owners = {} if registry is None else {
        (offer.source, offer.wire): model.id for model in registry.models.values() for offer in model.offers}
    with _lock:
        _registry, _owners = registry, owners
    if registry is not None:
        _queue_drift(registry, cfg)


# ─── drift ──────────────────────────────────────────────────────────────────

def _snapshot(registry: Registry, cfg: Mapping[str, Any]) -> dict:
    placements, pending, manual = {}, [], []
    for model in registry.models.values():
        for placement in model.placements:
            key = f"{placement.offer.source}|{placement.offer.wire}"
            placements[key] = model.id
            if placement.status == "pending":
                pending.append(key)
            elif placement.status == "manual":
                manual.append(key)
    stale = []
    for rule in settings(cfg).get("rules") or ():
        pin = rule.get("pin") if isinstance(rule, Mapping) else None
        if isinstance(pin, Mapping) and pin.get("catalog"):
            now = registry.catalog_names.get(pin["catalog"], "")
            if now != pin.get("name"):
                stale.append(f"{rule.get('from')}>{rule.get('into')}|{now}")
    return {"placements": placements, "pending": sorted(pending), "manual": sorted(manual),
            "stalePins": sorted(stale)}


def drift_lines(previous: Mapping[str, Any], current: Mapping[str, Any], label=lambda source: source) -> list[str]:
    """What an admin must look at: a known id moving model, a new pending id, a manual rule losing its evidence."""
    lines = []
    before = previous.get("placements") or {}
    # A rule an admin added or removed moves ids on purpose.
    ruled = set(previous.get("manual") or ()) | set(current.get("manual") or ())
    for key, model_id in sorted(current["placements"].items()):
        old = before.get(key)
        if old and old != model_id and key not in ruled:
            source, wire = key.split("|", 1)
            lines.append(f"{label(source)} 的 {wire} 从 {old} 改归 {model_id}")
    for key in sorted(set(current["pending"]) - set(previous.get("pending") or ())):
        source, wire = key.split("|", 1)
        lines.append(f"新的待确认：{label(source)} 的 {wire}")
    for item in sorted(set(current["stalePins"]) - set(previous.get("stalePins") or ())):
        pair, _, now = item.partition("|")
        source, _, target = pair.partition(">")
        lines.append(f"人工合并 {source} → {target} 的依据变了：目录里现在叫 “{now or '已下架'}”")
    return lines


_drift_ready = threading.Condition()
_drift_next: tuple[Registry, Mapping[str, Any]] | None = None
_drift_worker: threading.Thread | None = None


def _queue_drift(registry: Registry, cfg: Mapping[str, Any]) -> None:
    """Compare and store drift off the reload path: the durable snapshot rewrites a large file.

    Only the latest build matters; builds queued behind it are coalesced.
    """
    global _drift_next, _drift_worker
    with _drift_ready:
        _drift_next = (registry, cfg)
        if _drift_worker is None or not _drift_worker.is_alive():
            _drift_worker = threading.Thread(target=_drift_loop, name="model-registry-drift", daemon=True)
            _drift_worker.start()
        _drift_ready.notify()


def _drift_loop() -> None:
    global _drift_next
    while True:
        with _drift_ready:
            while _drift_next is None:
                _drift_ready.wait()
            registry, cfg = _drift_next
            _drift_next = None
        _report_drift(registry, cfg)


def _report_drift(registry: Registry, cfg: Mapping[str, Any]) -> None:
    from .. import notifier, state_db
    from .report import source_label
    current = _snapshot(registry, cfg)
    try:
        previous = state_db.model_registry_snapshot_load()
        if previous == current:
            return
        state_db.model_registry_snapshot_save(current)
    except Exception as exc:
        print(f"[model-registry] drift snapshot unavailable: {type(exc).__name__}: {exc}")
        return
    if previous is None:
        return
    lines = drift_lines(previous, current, lambda source: source_label(source, cfg))
    if not lines:
        return
    shown = lines[:12] + ([f"……另有 {len(lines) - 12} 条"] if len(lines) > 12 else [])
    notifier.notify_event("model_registry", "🧩 <b>模型命名变化</b>\n\n" + "\n".join(
        "• " + notifier.escape_html(line) for line in shown) + "\n\n处理：在「统一命名」里确认或改名。", reply_markup=NAMING_BUTTON)


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
        return self.blocked(offer, model_id) is None

    def blocked(self, offer: Offer, model_id: str) -> str | None:
        """Why ``offer`` cannot serve ``model_id``, in the scheduler's exclusion words; None when it can."""
        from ..channel import registry as channels
        channel = channels.get_channel(offer.source)
        if channel is None or not channel.enabled or channel.disabled_reason:
            return "disabled"
        if not offer.available:
            return "source_model_disabled"
        key = compare_key(model_id)
        api_off = self.api_disabled.get(offer.source)
        if api_off and api_off & {key, compare_key(offer.name), compare_key(offer.wire)}:
            return "source_model_disabled"
        if key in self.disabled and key not in self.exceptions.get(offer.source, frozenset()):
            return "global_model_disabled"
        return None


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
    rejected: dict[str, str] = field(default_factory=dict)   # source -> exclusion reason


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
        or reasoning.derived or global_default_effort()
    rejected: dict[str, str] = {}
    usable: set[tuple[str, str]] = set()
    for offer in registry.models[found.model].offers:
        reason = switches.blocked(offer, found.model)
        if reason is None and auth.channel_allowed(api_key, offer.source):
            usable.add((offer.source, offer.wire))
        elif reason is not None:
            rejected.setdefault(offer.source, reason)
    choices = choose(registry, found, lambda offer: (offer.source, offer.wire) in usable,
                     effort=reasoning.effort, thinking=reasoning.thinking, default_effort=default)
    chosen = {choice.offer.source: choice for choice in choices}
    for source, _wire in usable:
        if source not in chosen:
            # Usable, but none of its ids fits the asked effort (e.g. xhigh on -low/-high ids).
            rejected[source] = "reasoning_unsupported"
    return Plan(found.model, chosen, reasoning, rejected={k: v for k, v in rejected.items() if k not in chosen})


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
        and any(offer.listed and switches.usable(offer, model.id) and auth.channel_allowed(api_key, offer.source)
                and family_ok(offer) for offer in model.offers))


def global_default_effort(cfg: Mapping[str, Any] | None = None) -> str:
    return settings(cfg).get("defaultEffort") or DEFAULT_EFFORT


_NAME_CACHE_LIMIT = 10_000
_name_cache: tuple[Registry | None, dict[str, str | None]] = (None, {})


def _model_named(registry: Registry, name: str) -> str | None:
    """``resolve(registry, name).model``, remembered until the registry is rebuilt."""
    global _name_cache
    owner, cache = _name_cache
    if owner is not registry or len(cache) > _NAME_CACHE_LIMIT:
        cache = {}
        _name_cache = (registry, cache)
    if name not in cache:
        hit = resolve(registry, name)
        cache[name] = hit.model if hit else None
    return cache[name]


def same_models(name: str, keys: Iterable[Any]) -> list[str]:
    """Those of ``keys`` naming the same model as ``name``, the model's own spelling first."""
    registry = _registry
    target = _model_named(registry, name) if registry else None
    if target is None:
        return []
    hits = [key for key in keys if isinstance(key, str) and _model_named(registry, key) == target]
    return sorted(hits, key=lambda key: (compare_key(key) != compare_key(target), key))


def same_model(name: str, keys: Iterable[str]) -> str | None:
    """The key naming the same model as ``name`` (per-model settings keyed by any spelling)."""
    return next(iter(same_models(name, keys)), None)


def display_name(model_id: str) -> str:
    registry = _registry
    model = registry.models.get(model_id) if registry else None
    return model.name if model else model_id


def listed(name: str, api_key: str | None) -> bool:
    registry = _registry
    found = resolve(registry, name) if registry else None
    return found is not None and found.model in visible_models(api_key)


# ─── administration ──────────────────────────────────────────────────

RULE_FIELDS = {"merge": ("from", "into"), "assign": ("source", "wire", "model")}
SWITCH_LISTS = ("disabledModels", "hiddenModels")


@dataclass(frozen=True)
class PendingItem:
    source: str
    provider: str
    wire: str
    name: str
    model: str
    reason: str
    options: tuple[str, ...]


@dataclass(frozen=True)
class Overview:
    enabled: bool
    pending: tuple[PendingItem, ...]
    suggestions: tuple
    conflicts: tuple[str, ...]
    rules: tuple[Mapping[str, Any], ...]
    dismissed: tuple[str, ...]
    defaults: Mapping[str, str]


def overview(cfg: Mapping[str, Any] | None = None) -> Overview:
    root = settings(cfg)
    registry = _registry
    dismissed = tuple(str(item) for item in root.get("dismissed") or ())
    defaults = {model_id: item["defaultEffort"] for model_id, item in (root.get("models") or {}).items()
                if isinstance(item, Mapping) and item.get("defaultEffort")}
    if registry is None or not enabled(cfg):
        return Overview(enabled(cfg), (), (), (), tuple(root.get("rules") or ()), dismissed, defaults)
    pending = tuple(
        PendingItem(p.offer.source, p.offer.provider, p.offer.wire, p.offer.name, model.id, p.reason, p.options)
        for model in sorted(registry.models.values(), key=lambda item: item.id) for p in model.pending)
    suggestions = tuple(item for item in registry.suggestions if item.key not in dismissed)
    return Overview(True, pending, suggestions, tuple(registry.conflicts),
                    tuple(root.get("rules") or ()), dismissed, defaults)


def normalize_rules(rules: Iterable[Mapping[str, Any]]) -> list[dict]:
    """Rules as stored; raises ValueError naming the first malformed rule."""
    result: list[dict] = []
    for index, rule in enumerate(rules or ()):
        kind = rule.get("type") if isinstance(rule, Mapping) else None
        if kind not in RULE_FIELDS:
            raise ValueError(f"rules[{index}].type")
        clean = {"type": kind}
        for name in RULE_FIELDS[kind]:
            value = str(rule.get(name) or "").strip()
            if not value or len(value) > 300:
                raise ValueError(f"rules[{index}].{name}")
            clean[name] = value
        if kind == "merge" and compare_key(clean["from"]) == compare_key(clean["into"]):
            raise ValueError(f"rules[{index}].into")
        pin = rule.get("pin")
        if isinstance(pin, Mapping) and pin.get("catalog"):
            clean["pin"] = {"catalog": str(pin["catalog"]), "name": str(pin.get("name") or "")}
        if clean not in result:
            result.append(clean)
    return result


def _rule_key(rule: Mapping[str, Any]) -> tuple:
    return (rule.get("type"), *(compare_key(str(rule.get(name) or "")) for name in RULE_FIELDS.get(rule.get("type"), ())))


def write_rules(cfg: dict, rules: list[dict], dismissed: Iterable[str]) -> None:
    """Store rules; a merge into a name no model has yet is a rename and takes the old model's switches."""
    root = cfg.setdefault("modelRegistry", {})
    registry = _registry
    previous = {_rule_key(rule) for rule in root.get("rules") or () if isinstance(rule, Mapping)}
    for rule in rules:
        if rule["type"] != "merge" or _rule_key(rule) in previous or registry is None:
            continue
        found = registry.lookup(rule["from"])
        # Already merged: the old name resolves to the target itself; keep the rule as written.
        if found is None or compare_key(found[0]) == compare_key(rule["into"]):
            continue
        # Merges act on a model; an old alias names the model it points at.
        rule["from"] = found[0]
        if compare_key(rule["into"]) not in {compare_key(model_id) for model_id in registry.models}:
            _carry_switches(cfg, found[0], rule["into"])
    root["rules"] = list({_rule_key(rule): rule for rule in rules}.values())
    root["dismissed"] = sorted(set(str(item) for item in dismissed))


def _carry_switches(cfg: dict, old: str, new: str) -> None:
    key = compare_key(old)
    center = cfg.setdefault("modelCenter", {})
    rename = lambda values: sorted({new if compare_key(value) == key else value for value in values or ()})
    for name in SWITCH_LISTS:
        center[name] = rename(center.get(name))
    for name in ("sourceEnabledModels", "apiSourceDisabledModels"):
        center[name] = {source: rename(values) for source, values in (center.get(name) or {}).items()}
    models = cfg.setdefault("modelRegistry", {}).setdefault("models", {})
    for model_id in list(models):
        if compare_key(model_id) == key:
            models.setdefault(new, models.pop(model_id))


def write_default_effort(cfg: dict, model_id: str, effort: str | None) -> None:
    models = cfg.setdefault("modelRegistry", {}).setdefault("models", {})
    entry = dict(models.get(model_id) or {})
    if effort:
        entry["defaultEffort"] = effort
    else:
        entry.pop("defaultEffort", None)
    models[model_id] = entry


def admin_revision(cfg: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    """What the registry's admin revision covers: rules, dismissals and per-model defaults."""
    root = settings(cfg)
    return {"rules": root.get("rules") or [], "dismissed": root.get("dismissed") or [],
            "defaults": overview(cfg).defaults}


# ─── one-time switch migration ───────────────────────────────────────

def migrate_if_needed() -> list[str] | None:
    """Re-key today's switches and metadata by model, once per migration version.

    Runs in the new code at startup: an old process would read the re-keyed
    switches with per-spelling semantics.
    """
    from .. import config
    from .report import legacy_names
    cfg = config.get()
    done = settings(cfg).get("migrated")
    if not enabled(cfg) or (isinstance(done, int) and done >= MIGRATION_VERSION):
        return None
    registry = compute(cfg, legacy=legacy_names())
    notes: list[str] = []
    config.update(lambda current: migrate_config(current, registry, notes))
    return notes


def migrate_config(cfg: dict, registry: Registry, notes: list[str]) -> None:
    root = cfg.setdefault("modelRegistry", {})
    done = root.get("migrated") or 0
    if done < 1:
        _migrate_switches(cfg, registry, notes)
    if done < 2:
        _key_metadata_by_model(cfg, registry, notes)
    root["migrated"] = MIGRATION_VERSION
    root["migratedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _key_metadata_by_model(cfg: dict, registry: Registry, notes: list[str]) -> None:
    """Add each model's own key next to metadata saved under other names; the old keys stay for rollback."""
    for root_name, label in (("modelBindings", "元数据匹配"), ("modelMetadataOverrides", "元数据覆盖")):
        root = cfg.get(root_name)
        if not isinstance(root, dict):
            continue
        layers = [(None, root.get("defaults"))] + list((root.get("scoped") or {}).items())
        for scope, values in layers:
            if not isinstance(values, dict):
                continue
            by_model: dict[str, list[str]] = {}
            for key in values:
                found = registry.lookup(key) if isinstance(key, str) else None
                if found is not None and found[0] not in values:
                    by_model.setdefault(found[0], []).append(key)
            for model_id, keys in sorted(by_model.items()):
                keys.sort(key=lambda key: (compare_key(key) != compare_key(model_id), key))
                values[model_id] = copy.deepcopy(values[keys[0]])
                if any(values[key] != values[keys[0]] for key in keys[1:]):
                    where = f"{scope} 的" if scope else ""
                    notes.append(f"{where}{label}里 {'、'.join(keys)} 都属于 {model_id}，按模型查询时采用 {keys[0]} 的设置")


def _migrate_switches(cfg: dict, registry: Registry, notes: list[str]) -> None:
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
