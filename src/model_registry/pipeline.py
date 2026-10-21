"""Group every source's offers into models, each listed under one lower-case id.

Placement trusts official names and the models.dev catalog only as evidence
that two spellings are one model; anything the evidence cannot settle keeps
today's public name and is reported as pending.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .names import compare_key, display_traits, fold, plain_display, slug, suffix_traits
from .offers import Offer

_DATE_TAIL = re.compile(r"-(?:20\d{6}|\d{4})$")


@dataclass(frozen=True)
class Placement:
    offer: Offer
    key: str
    spelling: str
    status: str          # auto | product | pending | manual
    reason: str = ""


@dataclass
class Model:
    id: str
    name: str
    kind: str            # vendor | product
    placements: list[Placement]
    catalog: tuple[str, ...] = ()

    @property
    def offers(self) -> list[Offer]:
        return [placement.offer for placement in self.placements]

    @property
    def pending(self) -> list[Placement]:
        return [placement for placement in self.placements if placement.status == "pending"]


@dataclass
class Registry:
    models: dict[str, Model]
    aliases: dict[str, tuple[str, Mapping[str, str]]] = field(default_factory=dict)
    conflicts: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)

    def lookup(self, name: str) -> tuple[str, Mapping[str, str]] | None:
        if name in self.models:
            return name, {}
        return self.aliases.get(compare_key(name))


class CatalogIndex:
    """models.dev entries keyed for matching; never used to choose spelling."""

    def __init__(self, entries: Iterable[Mapping[str, Any]] | None = None):
        if entries is None:
            from .. import model_pricing
            entries = model_pricing.catalog_models()
        self.by_id: dict[str, set[str]] = defaultdict(set)
        self.names: dict[str, str] = {}
        for entry in entries:
            ref, model_id = str(entry.get("key") or ""), str(entry.get("id") or "")
            if ref and model_id:
                self.by_id[compare_key(model_id)].add(ref)
                self.names[ref] = str(entry.get("name") or model_id)

    def find(self, key: str) -> frozenset[str]:
        return frozenset(self.by_id.get(key, ()))


def place(offer: Offer, catalog: CatalogIndex) -> Placement:
    if offer.product:
        spelling = f"{offer.provider}-{slug(plain_display(offer.name) or offer.wire)}"
        return Placement(offer, compare_key(spelling), spelling, "product")
    id_base = suffix_traits(offer.wire)[0] if offer.basis == "name" else fold(offer.wire)
    name_base = display_traits(offer.name)[0]
    by_id, by_name = compare_key(id_base), compare_key(name_base)
    if offer.basis == "id":
        return Placement(offer, by_id, id_base, "auto")
    if by_name in (by_id, compare_key(_DATE_TAIL.sub("", id_base))):
        return Placement(offer, by_name if offer.basis == "name" else by_id,
                         name_base if offer.basis == "name" else id_base, "auto")
    in_id, in_name = catalog.find(by_id), catalog.find(by_name)
    split = bool(in_id and in_name and not in_id & in_name)
    if offer.basis == "name":
        if split:
            return _pending(offer, f"ID `{offer.wire}` 是目录里的 {id_base}，显示名 “{offer.name}” 却是 {name_base}")
        return Placement(offer, by_name, name_base, "auto")
    if in_id and not in_name:
        return Placement(offer, by_id, id_base, "auto", "ID 在目录中")
    if in_name and not in_id:
        return Placement(offer, by_name, name_base, "auto", "显示名在目录中")
    if in_id and in_name and not split:
        return Placement(offer, by_id, id_base, "auto")
    return _pending(offer, f"ID `{offer.wire}` 与显示名 “{offer.name}” 不一致，目录也无法判断")


def _pending(offer: Offer, reason: str) -> Placement:
    current = offer.legacy or offer.wire
    return Placement(offer, compare_key(current), fold(current), "pending", reason)


def _same_source_duplicates(placements: list[Placement]) -> list[Placement]:
    """Two ids of one source that land on the same model and traits cannot both be that model.

    The id spelled like the model keeps the place; the others stay under their
    current names until someone decides (``hy3`` and ``hy3-x`` both shown as "Hy3").
    """
    groups: dict[tuple, list[int]] = defaultdict(list)
    for index, p in enumerate(placements):
        if p.status == "auto":
            groups[(p.offer.source, p.key, tuple(sorted(p.offer.traits.items())))].append(index)
    result = list(placements)
    for indexes in groups.values():
        wires = {placements[i].offer.wire for i in indexes}
        if len(wires) < 2:
            continue
        named = {placements[i].offer.wire for i in indexes if compare_key(placements[i].offer.wire) == placements[i].key}
        for i in indexes:
            p = placements[i]
            if p.offer.wire not in named:
                others = "、".join(f"`{wire}`" for wire in sorted(wires - {p.offer.wire}))
                result[i] = _pending(p.offer, f"同一来源的 {others} 也叫 “{p.offer.name}”")
    return result


def _apply_rules(placements: list[Placement], rules: Iterable[Mapping[str, Any]]) -> tuple[list[Placement], list[tuple[str, str]]]:
    aliases: list[tuple[str, str]] = []
    result = list(placements)
    for rule in rules or ():
        kind = rule.get("type")
        if kind == "merge":
            source, target = compare_key(str(rule.get("from") or "")), fold(str(rule.get("into") or ""))
            if not source or not target:
                continue
            result = [Placement(p.offer, compare_key(target), target, "manual", f"人工合并自 {rule.get('from')}")
                      if p.key == source else p for p in result]
            aliases.append((str(rule["from"]), target))
        elif kind == "assign":
            target = fold(str(rule.get("model") or ""))
            if not target:
                continue
            result = [Placement(p.offer, compare_key(target), target, "manual", "人工指定")
                      if p.offer.source == rule.get("source") and p.offer.wire == rule.get("wire") else p
                      for p in result]
    return result, aliases


def _spelling(key: str, placements: list[Placement]) -> str:
    forced = [p.spelling for p in placements if p.status in ("manual", "product")]
    if forced:
        return Counter(forced).most_common(1)[0][0]
    for candidates in (
        [fold(p.offer.legacy) for p in placements if p.offer.legacy and compare_key(p.offer.legacy) == key],
        [fold(p.offer.wire) for p in placements if p.offer.basis != "name" and compare_key(p.offer.wire) == key],
        [p.spelling for p in placements],
    ):
        if candidates:
            counts = Counter(candidates)
            return min(counts, key=lambda item: (-counts[item], item))
    return key


def _display(model_id: str, key: str, placements: list[Placement]) -> str:
    names = [plain_display(p.offer.name) for p in placements
             if p.offer.name and (p.status == "product" or compare_key(display_traits(p.offer.name)[0]) == key)]
    if not names:
        return model_id
    counts = Counter(names)
    return min(counts, key=lambda item: (-counts[item], item))


def build(offers: Iterable[Offer], *, rules: Iterable[Mapping[str, Any]] = (),
          catalog: CatalogIndex | None = None) -> Registry:
    catalog = catalog if catalog is not None else CatalogIndex()
    placements = _same_source_duplicates([place(offer, catalog) for offer in offers])
    placements, rule_aliases = _apply_rules(placements, rules)
    groups: dict[str, list[Placement]] = defaultdict(list)
    for placement in placements:
        groups[placement.key].append(placement)
    registry = Registry(models={})
    for key, members in groups.items():
        model_id = _spelling(key, members)
        kind = "product" if any(p.status == "product" for p in members) else "vendor"
        refs = tuple(sorted(catalog.find(key))) if kind == "vendor" else ()
        registry.models[model_id] = Model(model_id, _display(model_id, key, members), kind, members, refs)
    _index_aliases(registry, rule_aliases)
    _suggest(registry, catalog)
    return registry


def _index_aliases(registry: Registry, rule_aliases: list[tuple[str, str]]) -> None:
    ranks: dict[str, int] = {}

    def add(name: str, model_id: str, traits: Mapping[str, str], rank: int) -> None:
        key = compare_key(name)
        if not key:
            return
        entry = (model_id, dict(traits))
        if key in registry.aliases:
            if registry.aliases[key] == entry or ranks[key] < rank:
                return
            if ranks[key] == rank:
                registry.conflicts.append(f"`{name}` 同时指向 {registry.aliases[key][0]} 和 {model_id}，保留前者")
                return
        registry.aliases[key], ranks[key] = entry, rank

    for model in registry.models.values():
        add(model.id, model.id, {}, 0)
    for model in registry.models.values():
        for p in model.placements:
            if p.offer.legacy:
                add(p.offer.legacy, model.id, p.offer.traits, 1)
    for source, target in rule_aliases:
        if target in registry.models:
            add(source, target, {}, 1)
    for model in registry.models.values():
        for p in model.placements:
            if p.offer.basis != "name" and not p.offer.product:
                add(p.offer.wire, model.id, p.offer.traits, 2)


def _suggest(registry: Registry, catalog: CatalogIndex) -> None:
    """Hints only: a merge needs a human, and resellers in the catalog disagree often."""
    keys = {compare_key(model_id): model_id for model_id in registry.models}
    seen: set[frozenset[str]] = set()

    def hint(first: str, second: str, text: str) -> None:
        pair = frozenset((first, second))
        if pair not in seen:
            seen.add(pair)
            registry.suggestions.append(text)

    for model in sorted(registry.models.values(), key=lambda item: item.id):
        dated = re.match(r"^(.*)-(20\d{6})$", model.id)
        if dated and dated.group(1) in registry.models:
            hint(dated.group(1), model.id, f"`{dated.group(1)}` 可能是 `{model.id}` 的浮动名")
        bases = Counter(compare_key(_DATE_TAIL.sub("", display_traits(catalog.names.get(ref, ""))[0]))
                        for ref in model.catalog)
        if not bases:
            continue
        base, count = bases.most_common(1)[0]
        other = keys.get(base)
        if other and other != model.id and count * 2 > len(model.catalog):
            example = next(ref for ref in model.catalog
                           if compare_key(_DATE_TAIL.sub("", display_traits(catalog.names.get(ref, ""))[0])) == base)
            hint(model.id, other, f"`{model.id}` 在目录中名为 “{catalog.names[example]}”（{count}/{len(model.catalog)} 条一致），"
                                  f"可能与 `{other}` 是同一模型")
